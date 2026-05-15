"""
Goodreads → League of Comic Geeks Issue Submitter

Fetch a Goodreads issue/book page and submit it as a new issue to an existing
League of Comic Geeks series.
"""

import argparse
import asyncio
import json
import logging
import re
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=Warning, module="urllib3")

from config import Config
from submitter_locg import (
    preview_issue_from_goodreads,
    review_issue_from_goodreads,
    submit_issue_from_goodreads,
)

_stream_handler = logging.StreamHandler()
_stream_handler.setLevel(logging.INFO)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("pipeline.log"),
        _stream_handler,
    ],
)
log = logging.getLogger(__name__)

DONE_FILE = Path("done.json")
SKIP_FILE = Path("skipped.json")
UI_SESSION_FILE = Path("ui_session.json")


def _contributions_url(locg_series_url: str) -> str:
    m = re.search(r"/series/(\d+)/", locg_series_url)
    if m:
        return f"https://leagueofcomicgeeks.com/community/contributions/new-issues?series_id={m.group(1)}"
    return locg_series_url


def load_json(path: Path) -> list:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return []


def save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_session() -> dict:
    if UI_SESSION_FILE.exists():
        try:
            return json.loads(UI_SESSION_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_session(session: dict):
    UI_SESSION_FILE.write_text(json.dumps(session, indent=2, ensure_ascii=False), encoding="utf-8")


_ROLE_LABELS = {
    "1": "Writer", "16": "Story", "2": "Artist", "9": "Penciller",
    "10": "Inker", "8": "Colorist", "7": "Letterer", "4": "Editor",
    "21": "Translator", "3": "Cover Artist", "19": "Designer",
}


def print_summary(issue: dict):
    print()
    print("─" * 52)
    print("  Issue Summary")
    print("─" * 52)
    fields = [
        ("Issue #",      issue.get("issue_number")),
        ("Title",        issue.get("goodreads_title")),
        ("Release date", issue.get("release_date")),
        ("Language",     issue.get("language")),
        ("Format",       issue.get("format")),
        ("Dimensions",   "Oversized"),
        ("Pages",        issue.get("pages")),
        ("ISBN-13",      issue.get("isbn13")),
        ("ISBN-10",      issue.get("isbn10")),
        ("Cover",        issue.get("cover_url")),
        ("Notes",        issue.get("moderator_comment")),
    ]
    for label, value in fields:
        if value:
            print(f"  {label:<14} {value}")

    contributors = issue.get("contributors") or []
    if contributors:
        print(f"  {'Credits':<14}", end="")
        for i, c in enumerate(contributors):
            roles = ", ".join(_ROLE_LABELS.get(r, r) for r in (c.get("role_values") or []))
            prefix = " " * 16 if i > 0 else ""
            print(f"{prefix}{c['name']} ({roles})")
    else:
        print(f"  {'Credits':<14} (none)")

    print("─" * 52)
    print()


async def interactive_flow(cfg: Config):
    while True:
        session = load_session()
        last_series = session.get("last_locg_series_url", "")

        print()
        if last_series:
            answer = input(f"Use last series URL? {last_series}\n[y/n]: ").strip().lower()
            locg_series_url = last_series if answer == "y" else input("LOCG series URL: ").strip()
        else:
            locg_series_url = input("LOCG series URL: ").strip()

        if not locg_series_url:
            print("No series URL provided. Exiting.")
            return

        goodreads_url = input("Goodreads URL: ").strip()
        if not goodreads_url:
            print("No Goodreads URL provided. Exiting.")
            return

        session["last_locg_series_url"] = locg_series_url
        save_session(session)

        _stream_handler.setLevel(logging.WARNING)

        print("\nSubmitting to LOCG...")
        result = await submit_issue_from_goodreads(goodreads_url, locg_series_url, cfg)

        _stream_handler.setLevel(logging.INFO)

        done = load_json(DONE_FILE)
        skipped = load_json(SKIP_FILE)

        if result.get("success"):
            done.append(result)
            save_json(DONE_FILE, done)
            print_summary(result.get("issue_data") or {})
            print(f"Submission completed.")
            print(f"View submissions: {_contributions_url(locg_series_url)}")
        else:
            skipped.append({
                "goodreads_title": result.get("goodreads_title", ""),
                "goodreads_url": goodreads_url,
                "locg_series_url": locg_series_url,
                "reason": result.get("error", "issue_submit_failed"),
                "details": result,
            })
            save_json(SKIP_FILE, skipped)
            print(f"\nSubmission failed: {result.get('error')}")

        another = input("\nSubmit another issue? [y/n]: ").strip().lower()
        if another != "y":
            print()
            print("╔══════════════════════════╗")
            print("║     Happy reading! 📚     ║")
            print("╚══════════════════════════╝")
            print()
            return


def parse_args():
    parser = argparse.ArgumentParser(
        description="Submit a Goodreads comic issue/book page as a new issue into an existing LOCG series."
    )
    parser.add_argument(
        "goodreads_issue_url",
        nargs="?",
        help="Goodreads issue/book URL to scrape and submit",
    )
    parser.add_argument(
        "locg_series_url",
        nargs="?",
        help="Existing LOCG series URL to submit the issue into",
    )
    parser.add_argument(
        "--goodreads-issue-url",
        dest="goodreads_issue_url_flag",
        help="Goodreads issue/book URL to scrape and submit",
    )
    parser.add_argument(
        "--locg-series-url",
        dest="locg_series_url_flag",
        help="Existing LOCG series URL to submit the issue into",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Playwright in headless mode, overriding config.json",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        dest="preview",
        help="Scrape Goodreads and print the issue metadata without submitting it to LOCG",
    )
    parser.add_argument(
        "--review",
        action="store_true",
        dest="review",
        help="Open Chromium, log into LOCG, fill the issue form, and wait for manual review before submitting",
    )
    return parser.parse_args()


async def main():
    args = parse_args()
    cfg = Config.load()
    if args.headless:
        cfg.headless = True

    goodreads_issue_url = args.goodreads_issue_url_flag or args.goodreads_issue_url
    locg_series_url = args.locg_series_url_flag or args.locg_series_url

    # No args → interactive flow
    if not goodreads_issue_url and not locg_series_url and not args.preview and not args.review:
        await interactive_flow(cfg)
        return

    if not goodreads_issue_url or not locg_series_url:
        log.error(
            "Missing required URLs.\n"
            "Usage: python main.py GOODREADS_ISSUE_URL LOCG_SERIES_URL\n"
            "Or just: python main.py  (for interactive mode)"
        )
        sys.exit(1)

    if args.preview:
        log.info("=== Previewing Goodreads issue metadata (no submission) ===")
        preview_data = await preview_issue_from_goodreads(goodreads_issue_url)
        if not preview_data:
            log.error("Failed to preview Goodreads issue metadata.")
            sys.exit(1)
        print_summary(preview_data)
        return

    if args.review:
        cfg.headless = False
        log.info("=== Opening browser for review and manual submission ===")
        await review_issue_from_goodreads(goodreads_issue_url, locg_series_url, cfg)
        return

    log.info("=== Submitting one issue to an existing LOCG series ===")
    result = await submit_issue_from_goodreads(goodreads_issue_url, locg_series_url, cfg)

    done = load_json(DONE_FILE)
    skipped = load_json(SKIP_FILE)

    if result.get("success"):
        done.append(result)
        save_json(DONE_FILE, done)
        log.info(f"Submitted issue: {result.get('goodreads_title', goodreads_issue_url)}")
        log.info(f"Target series: {locg_series_url}")
    else:
        skipped.append(
            {
                "goodreads_title": result.get("goodreads_title", ""),
                "goodreads_url": goodreads_issue_url,
                "locg_series_url": locg_series_url,
                "reason": result.get("error", "issue_submit_failed"),
                "details": result,
            }
        )
        save_json(SKIP_FILE, skipped)
        log.error(f"Issue submission failed: {result.get('error')}")


if __name__ == "__main__":
    asyncio.run(main())
