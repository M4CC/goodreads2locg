"""
Goodreads → League of Comic Geeks Issue Submitter

Fetch a Goodreads issue/book page and submit it as a new issue to an existing
League of Comic Geeks series.
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from config import Config
from submitter_locg import (
    preview_issue_from_goodreads,
    review_issue_from_goodreads,
    submit_issue_from_goodreads,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("pipeline.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

DONE_FILE = Path("done.json")
SKIP_FILE = Path("skipped.json")


def load_json(path: Path) -> list:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return []


def save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


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

    if not goodreads_issue_url or not locg_series_url:
        log.error(
            "Missing required URLs.\n"
            "Usage: python main.py GOODREADS_ISSUE_URL LOCG_SERIES_URL\n"
            "Or: python main.py --goodreads-issue-url URL --locg-series-url URL"
        )
        sys.exit(1)

    if args.preview:
        log.info("=== Previewing Goodreads issue metadata (no submission) ===")
        preview_data = await preview_issue_from_goodreads(goodreads_issue_url, cfg)
        if not preview_data:
            log.error("Failed to preview Goodreads issue metadata.")
            sys.exit(1)
        print(json.dumps({
            "goodreads_issue_url": goodreads_issue_url,
            "locg_series_url": locg_series_url,
            "issue_data": preview_data,
        }, indent=2, ensure_ascii=False))
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
