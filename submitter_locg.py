"""
Submit a single issue to League of Comic Geeks via Playwright browser automation.
"""

import asyncio
import datetime
import json
import logging
import mimetypes
import re
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

GOODREADS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

LOCG_BASE = "https://leagueofcomicgeeks.com"
LOGIN_URL = f"{LOCG_BASE}/login"

_MAGIC_BYTES: list[tuple[bytes, str, str]] = [
    (b"\xff\xd8", ".jpg", "image/jpeg"),
    (b"\x89PNG", ".png", "image/png"),
    (b"GIF8", ".gif", "image/gif"),
    (b"RIFF", ".webp", "image/webp"),
]

_CF_ERROR_CODES = ("521", "522", "523", "524", "Error 5")

_CONTRIBUTOR_ROLE_MAP: dict[str, str] = {
    "writer": "1",
    "author": "1",
    "story": "16",
    "illustrator": "2",
    "artist": "2",
    "penciller": "9",
    "pencils": "9",
    "inker": "10",
    "inks": "10",
    "colorist": "8",
    "colourist": "8",
    "colours": "8",
    "colors": "8",
    "letterer": "7",
    "letters": "7",
    "translator": "21",
    "translation": "21",
    "cover artist": "3",
    "cover": "3",
    "designer": "19",
}

LOCG_RETRY_ATTEMPTS = 10
LOCG_RETRY_DELAY_S = 30
SESSION_FILE = Path("session.json")
GOODREADS_LOGIN_URL = "https://www.goodreads.com/user/sign_in"


class CloudflareDownError(Exception):
    pass


async def _check_cloudflare_down(page) -> None:
    """Raise CloudflareDownError if the current page is a Cloudflare server-down error."""
    try:
        snippet = await page.inner_text("body")
    except Exception:
        return
    snippet = snippet[:500]
    if "cloudflare" in snippet.lower() and any(code in snippet for code in _CF_ERROR_CODES):
        code = next((c for c in _CF_ERROR_CODES if c in snippet), "5xx")
        raise CloudflareDownError(f"LOCG is down (Cloudflare {code})")

_FORMAT_MAP: list[tuple[str, str]] = [
    ("hardcover", "Hardcover"),
    ("mass market paperback", "Softcover"),
    ("paperback", "Trade Paperback"),
    ("trade paperback", "Trade Paperback"),
    ("softcover", "Softcover"),
    ("graphic novel", "Trade Paperback"),
]


def _parse_contributors(soup: BeautifulSoup) -> list[dict]:
    candidates = []
    seen: set[str] = set()
    for link in soup.select(".ContributorLinksList .ContributorLink"):
        name_el = link.select_one(".ContributorLink__name, [data-testid='name']")
        role_el = link.select_one(".ContributorLink__role, [data-testid='role']")
        if not name_el:
            continue
        name = name_el.get_text(strip=True)
        if not name or name in seen:
            continue
        seen.add(name)
        if role_el:
            role_raw = role_el.get_text(strip=True).strip("()").strip()
            role_value = _CONTRIBUTOR_ROLE_MAP.get(role_raw.lower())
            if role_value is None:
                continue  # unrecognised role — skip rather than guess
            candidates.append({"name": name, "role_values": [role_value]})
        else:
            if not candidates:
                candidates.append({"name": name, "role_values": None})  # resolved below
            # subsequent with no role → skip

    # Single author with no explicit role → assume Writer + Artist + Cover Artist
    if len(candidates) == 1 and candidates[0]["role_values"] is None:
        candidates[0]["role_values"] = ["1", "2", "3"]
    elif candidates and candidates[0]["role_values"] is None:
        candidates[0]["role_values"] = ["1"]

    return [c for c in candidates if c["role_values"]]


def _mime_from_bytes(data: bytes) -> tuple[str, str]:
    for magic, ext, mime in _MAGIC_BYTES:
        if data[:len(magic)] == magic:
            return ext, mime
    return ".jpg", "image/jpeg"


def _isbn13_to_isbn10(isbn13: str) -> str:
    """Compute ISBN-10 from an ISBN-13 that starts with 978."""
    if len(isbn13) != 13 or not isbn13.startswith("978"):
        return ""
    digits = isbn13[3:12]
    total = sum((10 - i) * int(d) for i, d in enumerate(digits))
    check = (11 - (total % 11)) % 11
    return digits + ("X" if check == 10 else str(check))


def _map_goodreads_format(pages_format_str: str) -> str:
    lower = pages_format_str.lower()
    for keyword, locg_format in _FORMAT_MAP:
        if keyword in lower:
            return locg_format
    return "Trade Paperback"


async def preview_issue_from_goodreads(goodreads_url: str) -> dict:
    try:
        return await asyncio.to_thread(_scrape_goodreads_issue_no_browser, goodreads_url)
    except Exception as exc:
        log.error(f"Preview scrape failed: {exc}")
        return {}


async def review_issue_from_goodreads(goodreads_url: str, locg_series_url: str, cfg) -> dict:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise ImportError(
            "Playwright is not installed.\n"
            "Run: pip install playwright && playwright install chromium"
        )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=0)
        context = await browser.new_context(
            user_agent=GOODREADS_HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 900},
        )

        page = await context.new_page()
        issue = await _scrape_goodreads_issue(page, goodreads_url, cfg)
        await page.close()

        if not issue:
            await browser.close()
            return {
                "goodreads_url": goodreads_url,
                "locg_series_url": locg_series_url,
                "success": False,
                "error": "goodreads_scrape_failed",
            }

        issue["moderator_comment"] = f"Information retrieved from {goodreads_url}"

        locg_page = await context.new_page()
        logged_in = await _login(locg_page, cfg.locg_username, cfg.locg_password)
        if not logged_in:
            await browser.close()
            return {
                "goodreads_url": goodreads_url,
                "locg_series_url": locg_series_url,
                "goodreads_title": issue.get("goodreads_title", ""),
                "success": False,
                "error": "login_failed",
            }

        result = await _fill_issue_form(locg_page, locg_series_url, issue)
        if not result.get("success"):
            await browser.close()
            return result

        review_image = Path("locg_issue_review.png")
        review_html = Path("locg_issue_review.html")
        try:
            await locg_page.screenshot(path=str(review_image), full_page=True)
            print(f"Saved filled issue form screenshot to: {review_image}")
        except Exception as exc:
            log.warning(f"Could not save review screenshot: {exc}")

        try:
            html_content = await locg_page.content()
            review_html.write_text(html_content, encoding="utf-8")
            print(f"Saved review page HTML to: {review_html}")
        except Exception as exc:
            log.warning(f"Could not save review page HTML: {exc}")

        print("Review the filled issue form in Chromium. Close the browser tab to finish.")
        try:
            await locg_page.wait_for_event("close")
        except Exception:
            pass
        await browser.close()
        return {"success": True, "message": "review_complete"}


def _is_goodreads_login_page(url: str) -> bool:
    return "sign_in" in url or "/ap/signin" in url


async def _login_goodreads(page, email: str, password: str) -> bool:
    if not email or not password:
        return False
    try:
        log.info("Logging in to Goodreads...")
        if not _is_goodreads_login_page(page.url):
            await page.goto(GOODREADS_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)

        # Goodreads login is behind Amazon's sign-in — click "Sign in with email" if needed
        if "/user/sign_in" in page.url:
            email_signin = page.locator('a[href*="/ap/signin"]').last
            await email_signin.click()
            await page.wait_for_load_state("domcontentloaded", timeout=15000)

        await page.wait_for_selector("#ap_email", timeout=15000)
        await page.fill("#ap_email", email)
        await page.fill("#ap_password", password)
        await page.click("#signInSubmit")
        await page.wait_for_load_state("domcontentloaded", timeout=15000)

        if _is_goodreads_login_page(page.url):
            log.error("Goodreads login failed — still on login page")
            return False
        log.info("Logged in to Goodreads successfully")
        return True
    except Exception as exc:
        log.error(f"Goodreads login error: {exc}")
        return False


def _session_file() -> Optional[str]:
    if SESSION_FILE.exists():
        return str(SESSION_FILE)
    legacy = Path("locg_session.json")
    if legacy.exists():
        return str(legacy)
    return None


async def submit_issue_from_goodreads(goodreads_url: str, locg_series_url: str, cfg) -> dict:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise ImportError(
            "Playwright is not installed.\n"
            "Run: pip install playwright && playwright install chromium"
        )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=cfg.headless, slow_mo=0)

        ctx_kwargs = dict(
            user_agent=GOODREADS_HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 900},
        )
        saved_session = _session_file()
        if saved_session:
            ctx_kwargs["storage_state"] = saved_session

        context = await browser.new_context(**ctx_kwargs)

        page = await context.new_page()
        issue = await _scrape_goodreads_issue(page, goodreads_url, cfg)
        await context.storage_state(path=str(SESSION_FILE))
        await page.close()

        if not issue:
            await browser.close()
            return {
                "goodreads_url": goodreads_url,
                "locg_series_url": locg_series_url,
                "success": False,
                "error": "goodreads_scrape_failed",
            }

        issue["moderator_comment"] = f"Information retrieved from {goodreads_url}"

        submit_url = locg_series_url.rstrip("/") + "/submit-new-issue"
        form_selector = "form[action*='/submit-new-issue'], form:has(input[name='issue_number']), form:has(input[name='title'])"

        for attempt in range(1, LOCG_RETRY_ATTEMPTS + 1):
            locg_page = await context.new_page()
            try:
                # Go straight to the form — skip the login page entirely if session is valid
                await locg_page.goto(submit_url, wait_until="domcontentloaded", timeout=30000)
                await _check_cloudflare_down(locg_page)

                # Login only if redirected away from the form
                if "/login" in locg_page.url or not await locg_page.query_selector(form_selector):
                    log.info("Session expired or missing — logging in to LOCG...")
                    logged_in = await _login(locg_page, cfg.locg_username, cfg.locg_password)
                    if not logged_in:
                        await locg_page.close()
                        await browser.close()
                        return {
                            "goodreads_url": goodreads_url,
                            "locg_series_url": locg_series_url,
                            "goodreads_title": issue.get("goodreads_title", ""),
                            "success": False,
                            "error": "login_failed",
                        }
                    await locg_page.goto(submit_url, wait_until="domcontentloaded", timeout=30000)

                # Persist session so next run skips login
                await context.storage_state(path=str(SESSION_FILE))

                result = await _submit_issue(locg_page, locg_series_url, issue)
                await locg_page.close()
                await browser.close()
                return result

            except CloudflareDownError as exc:
                await locg_page.close()
                if attempt >= LOCG_RETRY_ATTEMPTS:
                    await browser.close()
                    return {
                        "goodreads_url": goodreads_url,
                        "locg_series_url": locg_series_url,
                        "goodreads_title": issue.get("goodreads_title", ""),
                        "success": False,
                        "error": str(exc),
                    }
                log.warning(f"{exc} — retrying in {LOCG_RETRY_DELAY_S}s (attempt {attempt}/{LOCG_RETRY_ATTEMPTS})")
                print(f"\n{exc} — retrying in {LOCG_RETRY_DELAY_S}s (attempt {attempt}/{LOCG_RETRY_ATTEMPTS})...")
                await asyncio.sleep(LOCG_RETRY_DELAY_S)

        await browser.close()
        return {"goodreads_url": goodreads_url, "locg_series_url": locg_series_url, "success": False, "error": "max_retries_exceeded"}


async def _ensure_goodreads_logged_in(page, cfg) -> None:
    email = getattr(cfg, "goodreads_email", "")
    password = getattr(cfg, "goodreads_password", "")
    if not email or not password:
        return
    try:
        await page.goto("https://www.goodreads.com", wait_until="domcontentloaded", timeout=20000)
        logged_in = await page.query_selector(
            '[data-testid="userMenu"], .userMenu, a[href*="/user/show"], '
            'a[href*="/shelf/show"], [aria-label="Profile"]'
        )
        if logged_in:
            log.info("Goodreads session active")
            return
    except Exception:
        pass
    await _login_goodreads(page, email, password)


async def _scrape_goodreads_issue(page, url: str, cfg) -> dict:
    html = None
    headers = GOODREADS_HEADERS.copy()

    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        if _is_goodreads_login_page(resp.url):
            raise ValueError("Goodreads redirected to login page")
        html = resp.text
        if "cloudflare" in html[:2000].lower() and any(code in html[:2000] for code in _CF_ERROR_CODES):
            raise ValueError("Cloudflare error page detected")
    except Exception as exc:
        log.warning(f"Failed to fetch Goodreads issue page by requests ({exc}), falling back to browser scraping.")
        await _ensure_goodreads_logged_in(page, cfg)
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        html = await page.content()

    if not html:
        log.error("Could not load Goodreads issue page content")
        return {}

    soup = BeautifulSoup(html, "html.parser")
    parsed = _parse_goodreads_issue_page(soup, url)

    needs_details = (
        not parsed.get("isbn10")
        or not parsed.get("isbn13")
        or parsed.get("publication_info", "").lower().startswith("first published")
    )
    if needs_details:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=120000)
            await page.wait_for_timeout(2000)
            details_button = page.locator('button:has-text("Book details & editions"), button:has-text("Book Details & Editions")').first
            if await details_button.count() > 0:
                try:
                    await details_button.click()
                    await page.wait_for_timeout(2000)
                except Exception:
                    pass
            html = await page.content()
            soup = BeautifulSoup(html, "html.parser")
            parsed = _parse_goodreads_issue_page(soup, url)
        except Exception:
            pass

    return parsed


def _scrape_goodreads_issue_no_browser(url: str) -> dict:
    headers = GOODREADS_HEADERS.copy()

    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        html = resp.text
        if "cloudflare" in html[:2000].lower() and any(code in html[:2000] for code in _CF_ERROR_CODES):
            raise ValueError("Cloudflare error page detected")
        soup = BeautifulSoup(html, "html.parser")
        parsed = _parse_goodreads_issue_page(soup, url)
        return parsed
    except Exception as exc:
        log.error(f"Goodreads preview fetch failed: {exc}")
        return {}


def _parse_goodreads_issue_page(soup: BeautifulSoup, url: str) -> dict:
    def _find_detail_local(label: str) -> Optional[str]:
        for dt in soup.select("dt, .DescListItem dt"):
            if label.lower() in dt.get_text(strip=True).lower():
                dd = dt.find_next_sibling("dd")
                if dd:
                    return dd.get_text(strip=True)
        details_div = soup.select_one("#details, .BookDetails, [data-testid='bookDetails']")
        if details_div:
            text = details_div.get_text(separator="\n")
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            for idx, line in enumerate(lines):
                if label.lower() in line.lower() and idx + 1 < len(lines):
                    return lines[idx + 1]
        return None

    def _find_detail_exact(label: str) -> Optional[str]:
        """Find a detail whose label matches exactly (case-insensitive)."""
        for dt in soup.select("dt, .DescListItem dt"):
            if dt.get_text(strip=True).lower() == label.lower():
                dd = dt.find_next_sibling("dd")
                if dd:
                    return dd.get_text(strip=True)
        details_div = soup.select_one("#details, .BookDetails, [data-testid='bookDetails']")
        if details_div:
            text = details_div.get_text(separator="\n")
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            for idx, line in enumerate(lines):
                if line.lower() == label.lower() and idx + 1 < len(lines):
                    return lines[idx + 1]
        return None

    def txt(selector: str, attr: Optional[str] = None) -> Optional[str]:
        el = soup.select_one(selector)
        if not el:
            return None
        return el.get(attr, "").strip() if attr else el.get_text(strip=True)

    title = (
        txt("h1#bookTitle")
        or txt("h1.Text__title1")
        or txt('[data-testid="bookTitle"]')
        or txt("h1")
        or ""
    )

    author = ""
    author_el = soup.select_one(".authorName span[itemprop='name'], [data-testid='name'], .ContributorLink__name")
    if author_el:
        author = author_el.get_text(strip=True)

    issue_number = None
    series_name = None
    series_link = soup.select_one("#bookSeries a, [data-testid='seriesHeader'] a, .SeriesHeader a")
    if series_link:
        text = series_link.get_text(strip=True)
        series_name = text.split("#")[0].strip().strip("()")
        match = re.search(r"#\s*(\d+)", text)
        if not match:
            match = re.search(r"Book\s*(\d+)", series_link.get("aria-label", ""))
        if match:
            issue_number = match.group(1)

    json_ld = None
    json_el = soup.select_one('script[type="application/ld+json"]')
    if json_el:
        json_ld = json_el.get_text()

    ld = {}
    if json_ld:
        try:
            ld = json.loads(json_ld)
        except Exception:
            ld = {}

    cover = ld.get("image") or txt("meta[property='og:image']", "content")
    edition_published = _find_detail_exact("Published")
    first_published_info = txt('p[data-testid="publicationInfo"]') or _find_detail_local("First published") or ""
    if edition_published:
        publication_info = edition_published
        release_date = re.split(r"\s+by\s+", edition_published, maxsplit=1)[0].strip()
    else:
        publication_info = first_published_info
        release_date = first_published_info.replace("First published", "", 1).strip()

    pages = None
    pages_format_raw = txt('p[data-testid="pagesFormat"]') or _find_detail_local("Format") or ""
    if pages_format_raw:
        pages_match = re.search(r"(\d+)\s+pages", pages_format_raw)
        if pages_match:
            pages = pages_match.group(1)

    fmt = _map_goodreads_format(pages_format_raw) if pages_format_raw else "Trade Paperback"

    language = (
        ld.get("inLanguage")
        or _find_detail_local("Language")
        or _find_detail_local("Idioma")
        or ""
    )

    isbn_raw = (
        ld.get("isbn")
        or txt("[itemprop='isbn']")
        or _find_detail_local("ISBN13")
        or _find_detail_local("ISBN")
        or _find_detail_local("ASIN")
        or ""
    )
    isbn10 = ""
    isbn13 = ""
    if isbn_raw:
        m13 = re.search(r"(97[89][\-\s]*\d[\d\-\s]{7,}\d)", isbn_raw)
        if m13:
            isbn13 = re.sub(r"[^0-9]", "", m13.group(1))
        else:
            m13b = re.search(r"(\d{13})", isbn_raw)
            if m13b:
                isbn13 = m13b.group(1)

        # Explicit "ISBN10:" label takes priority
        m10_label = re.search(r"ISBN10[:\s]+([0-9Xx\-]{9,13})", isbn_raw, re.IGNORECASE)
        if m10_label:
            isbn10 = re.sub(r"[^0-9Xx]", "", m10_label.group(1))[:10]
        else:
            # Goodreads often shows: "9789724138923 (ISBN10: 9724138925)"
            m10_paren = re.search(r"\(([0-9Xx]{10})\)", isbn_raw)
            if m10_paren:
                isbn10 = m10_paren.group(1)
            else:
                # Generic: 10-digit sequence not already contained in isbn13
                for m in re.finditer(r"(?<!\d)(\d{9}[\dXx])(?!\d)", isbn_raw):
                    candidate = m.group(1)
                    if not isbn13 or candidate not in isbn13:
                        isbn10 = candidate
                        break

    if not isbn10 and isbn13:
        isbn10 = _isbn13_to_isbn10(isbn13)

    if not title and ld.get("name"):
        title = ld.get("name")

    if not issue_number:
        m = re.search(r"#\s*(\d+)", title)
        if m:
            issue_number = m.group(1)

    if not series_name and ld.get("name"):
        series_name = ld.get("name")

    contributors = _parse_contributors(soup)

    return {
        "goodreads_url": url,
        "goodreads_title": title,
        "author": author,
        "contributors": contributors,
        "language": language,
        "format": fmt,
        "cover_url": cover,
        "issue_number": issue_number,
        "series_name": series_name,
        "release_date": release_date,
        "pages": pages,
        "isbn": isbn_raw,
        "isbn10": isbn10,
        "isbn13": isbn13,
        "publication_info": publication_info,
    }


async def _attach_file_if_exists(page, selector: str, file_url: str) -> bool:
    if not file_url:
        return False
    el = await page.query_selector(selector)
    if not el:
        return False

    try:
        resp = requests.get(file_url, headers=GOODREADS_HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as exc:
        log.error(f"Cover download failed: {exc}")
        return False

    data = resp.content
    content_type = resp.headers.get("Content-Type", "").split(";")[0].strip()
    if content_type.startswith("image/"):
        ext = mimetypes.guess_extension(content_type) or ".jpg"
        mime = content_type
    else:
        ext, mime = _mime_from_bytes(data)

    fname = f"cover{ext}"
    try:
        await el.set_input_files([{"name": fname, "mimeType": mime, "buffer": data}])
        return True
    except Exception as exc:
        log.error(f"Failed to attach cover file: {exc}")
        return False


async def _navigate_to_form(page, series_url: str) -> Optional[str]:
    """Navigate to the submit-new-issue form and return the form selector, or None on failure."""
    submit_url = series_url.rstrip("/") + "/submit-new-issue"
    await page.goto(submit_url, wait_until="domcontentloaded", timeout=30000)

    form_selector = "form[action*='/submit-new-issue'], form:has(input[name='issue_number']), form:has(input[name='title'])"
    try:
        await page.wait_for_selector(form_selector, timeout=15000)
    except Exception:
        return None
    return form_selector


async def _populate_form_fields(page, issue: dict):
    """Fill all known issue fields into the currently loaded form."""
    if issue.get("issue_number"):
        await _fill_if_exists(page, 'input[name="issue_number"], #issue_number', str(issue["issue_number"]))
    else:
        await _fill_if_exists(page, 'input[name="issue_number"], #issue_number', "1")

    if issue.get("goodreads_title"):
        await _fill_if_exists(page, 'input[name="title"], #title', issue["goodreads_title"])

    if issue.get("release_date"):
        formatted_date = _parse_date(issue["release_date"])
        await _fill_if_exists(
            page,
            'input[name="date_release"], input[name="publication_date"], input[type="date"], #date_release, #publication_date',
            formatted_date,
        )

    if issue.get("format"):
        await _select_if_exists(page, 'select[name="format"], #format', issue["format"])

    if issue.get("pages"):
        await _fill_if_exists(page, 'input[name="pages"], #pages', str(issue["pages"]))

    isbn = issue.get("isbn13") or issue.get("isbn10") or ""
    if isbn:
        await _fill_if_exists(page, 'input[name="isbn"], #isbn', isbn)

    if issue.get("cover_url"):
        await _attach_file_if_exists(
            page,
            'input[type="file"][name*="cover"], input[type="file"][id*="cover"], #cover',
            issue["cover_url"],
        )

    await _select_if_exists(page, 'select[name="dimensions"], #dimensions', "Oversized")

    contributors = issue.get("contributors") or []
    if contributors:
        log.info(f"Filling credits: {[(c['name'], c['role_values']) for c in contributors]}")
        await _fill_credits(page, contributors)
    else:
        log.info("No contributors parsed from Goodreads")

    moderator_comment = issue.get("moderator_comment") or ""
    if moderator_comment:
        await _fill_wysiwyg(page, 'textarea[name="notes"], #notes', moderator_comment)


async def _fill_credits(page, credits: list[dict]):
    try:
        for i, credit in enumerate(credits):
            if i > 0:
                add_more = await page.query_selector('.action-add-more-creators')
                if add_more:
                    await add_more.click()
                    await page.wait_for_timeout(500)

            rows = await page.query_selector_all('.form-item-clonee')
            if i >= len(rows):
                log.warning(f"Credits: no row available for index {i}")
                break
            row = rows[i]

            # Type name into typeahead input to trigger autocomplete
            tt_input = await row.query_selector('input[name="people_names[]"].tt-input')
            orig_input = await row.query_selector(
                'input[name="people_names[]"]:not(.tt-hint):not(.tt-input)'
            )
            if tt_input:
                await tt_input.click()
                await tt_input.fill('')
                await tt_input.type(credit['name'], delay=50)
                suggestion_clicked = False
                try:
                    await page.wait_for_selector('.tt-suggestion', timeout=2000)
                    first = await page.query_selector('.tt-suggestion')
                    if first:
                        await first.click()
                        suggestion_clicked = True
                        await page.wait_for_timeout(300)
                except Exception:
                    pass
                if not suggestion_clicked and orig_input:
                    await orig_input.evaluate('(el, v) => { el.value = v; }', credit['name'])
            elif orig_input:
                await orig_input.evaluate('(el, v) => { el.value = v; }', credit['name'])

            role_values = credit.get('role_values') or []
            if not role_values:
                continue

            select_el = await row.query_selector('select[name="people_roles[][]"]')
            if not select_el:
                continue

            # Use jQuery Bootstrap Multiselect API when available
            used_jquery = await select_el.evaluate(
                """(sel, vals) => {
                    var $ = window.jQuery;
                    if (!$ || typeof $.fn.multiselect !== 'function') return false;
                    var $sel = $(sel);
                    vals.forEach(function(v) {
                        try { $sel.multiselect('select', v); } catch(e) {}
                    });
                    return true;
                }""",
                role_values,
            )

            if not used_jquery:
                # Fallback: JS-click the checkboxes inside the Bootstrap Multiselect container
                await select_el.evaluate(
                    """(sel, vals) => {
                        var container = sel.closest('.btn-group') || sel.parentElement;
                        if (!container) return;
                        vals.forEach(function(v) {
                            var cb = container.querySelector(
                                'input[type="checkbox"][value="' + v + '"]'
                            );
                            if (cb && !cb.checked) cb.click();
                        });
                    }""",
                    role_values,
                )

    except Exception as exc:
        log.warning(f"Credits fill failed: {exc}")


async def _fill_issue_form(page, series_url: str, issue: dict) -> dict:
    name = issue.get("goodreads_title", "Unknown")
    try:
        form_selector = await _navigate_to_form(page, series_url)
        if not form_selector:
            return {"goodreads_title": name, "success": False, "error": "no_issue_form_found", "locg_series_url": series_url}

        await _populate_form_fields(page, issue)
        return {"goodreads_title": name, "success": True, "locg_series_url": series_url}
    except Exception as exc:
        return {"goodreads_title": name, "success": False, "error": str(exc), "locg_series_url": series_url}


async def _submit_issue(page, series_url: str, issue: dict) -> dict:
    name = issue.get("goodreads_title", "Unknown")
    try:
        form_selector = await _navigate_to_form(page, series_url)
        if not form_selector:
            return {"goodreads_title": name, "success": False, "error": "no_issue_form_found", "locg_series_url": series_url}

        await _populate_form_fields(page, issue)

        submit_btn = await page.query_selector('button[type="submit"], input[type="submit"]')
        if submit_btn:
            try:
                await submit_btn.scroll_into_view_if_needed()
                await submit_btn.click()
            except Exception as exc:
                try:
                    await submit_btn.click(force=True)
                except Exception:
                    form = await page.query_selector(form_selector)
                    if not form:
                        return {"goodreads_title": name, "success": False, "error": f"form_submit_failed: {exc}", "locg_series_url": series_url}
                    try:
                        await form.evaluate("form => form.requestSubmit ? form.requestSubmit() : form.submit()")
                    except Exception as exc2:
                        return {"goodreads_title": name, "success": False, "error": f"form_submit_failed: {exc}, {exc2}", "locg_series_url": series_url}
        else:
            form = await page.query_selector(form_selector)
            if not form:
                return {"goodreads_title": name, "success": False, "error": "no_submit_button", "locg_series_url": series_url}
            try:
                await form.evaluate("form => form.requestSubmit ? form.requestSubmit() : form.submit()")
            except Exception as exc:
                return {"goodreads_title": name, "success": False, "error": f"form_submit_failed: {exc}", "locg_series_url": series_url}

        try:
            await page.wait_for_navigation(timeout=15000)
        except Exception:
            pass

        current_url = page.url
        log.info(f"After submit, URL: {current_url}")

        error_el = None
        try:
            error_el = await page.query_selector(".alert-danger, .error-message, .alert-error")
        except Exception:
            error_el = None

        if error_el:
            try:
                error_text = await error_el.inner_text()
            except Exception:
                error_text = "Unknown submission error"
            return {"goodreads_title": name, "success": False, "error": error_text.strip(), "locg_series_url": series_url, "submitted_url": current_url}

        page_text = ""
        try:
            page_text = (await page.inner_text("body")).lower()
        except Exception:
            pass

        if "pending moderation" in page_text or "pending approval" in page_text or "awaiting moderation" in page_text:
            status = "pending_moderation"
        else:
            status = "submitted"

        return {
            "goodreads_title": name,
            "success": True,
            "issue_number": issue.get("issue_number"),
            "locg_series_url": series_url,
            "status": status,
            "submitted_url": current_url,
            "issue_data": issue,
        }

    except Exception as exc:
        return {"goodreads_title": name, "success": False, "error": str(exc), "locg_series_url": series_url}


async def _login(page, username: str, password: str) -> bool:
    if not username or not password:
        raise ValueError(
            "locg_username and locg_password are required in config.json to submit contributions."
        )

    try:
        log.info("Logging in to LOCG...")
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        await _check_cloudflare_down(page)

        current_url = page.url
        log.debug(f"Navigated to {current_url}")
        if "/login" not in current_url and "/dashboard" in current_url:
            log.info("Already logged in (session found)")
            return True

        try:
            await page.wait_for_function(
                'document.title.toLowerCase().indexOf("just a moment") === -1',
                timeout=45000,
            )
            log.debug("Cloudflare challenge cleared or bypassed")
        except Exception:
            log.debug("Cloudflare challenge did not clear within timeout")

        login_selectors = [
            'input[name="username"]',
            'input[name="email"]',
            'input[type="email"]',
        ]
        password_selectors = [
            'input[name="password"]',
            'input[type="password"]',
        ]

        login_selector = None
        password_selector = None
        for selector in login_selectors:
            try:
                await page.wait_for_selector(selector, timeout=30000)
                login_selector = selector
                break
            except Exception:
                continue

        if not login_selector:
            body_text = await page.inner_text('body') if await page.query_selector('body') else ''
            if 'security verification' in body_text.lower() or 'just a moment' in (await page.title()).lower():
                log.info("Cloudflare challenge detected. Please solve the challenge in the opened browser window.")
                await page.wait_for_selector(','.join(login_selectors), timeout=0)
                for selector in login_selectors:
                    try:
                        await page.wait_for_selector(selector, timeout=10000)
                        login_selector = selector
                        break
                    except Exception:
                        continue

        if not login_selector:
            log.error("Could not find LOCG username/email field after challenge")
            return False

        for selector in password_selectors:
            try:
                await page.wait_for_selector(selector, timeout=30000)
                password_selector = selector
                break
            except Exception:
                continue

        if not password_selector:
            log.error("Could not find LOCG password field")
            return False

        await page.fill(login_selector, username)
        await page.fill(password_selector, password)

        login_btn = page.locator(
            'form[action*="/login"] button[type="submit"], '
            'form[action*="/login"] input[type="submit"], '
            'button:has-text("Log in"), '
            'button:has-text("Login"), '
            'button:has-text("Sign in"), '
            'input[type="submit"][value*="Log" i], '
            'input[type="submit"][value*="Sign" i]'
        ).first
        if await login_btn.count() > 0:
            try:
                await login_btn.click()
            except Exception:
                await login_btn.click(force=True)
            try:
                await page.wait_for_url("**/dashboard", timeout=45000)
            except Exception:
                log.debug("Login did not redirect to dashboard within timeout")
        else:
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(2000)

        continue_btn = await page.query_selector(
            'button:has-text("Continue"), button:has-text("continue"), .btn-continue, [data-action="continue"]'
        )
        if continue_btn:
            try:
                await continue_btn.click()
                await page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                log.debug("Could not click continue button")

        current_url = page.url
        log.info(f"After login, URL: {current_url}")

        if "/login" in current_url:
            log.error("Still on login page - login failed")
            return False

        dashboard_text = ""
        try:
            dashboard_text = await page.inner_text("body")
        except Exception:
            pass

        if username.lower() in dashboard_text.lower() or "logout" in dashboard_text.lower() or "dashboard" in dashboard_text.lower():
            log.info("Login verified on dashboard page")
        else:
            log.warning("Login not verified on dashboard page")
            await page.screenshot(path="login_dashboard.png")

        log.info("Logged in successfully")
        return True

    except Exception as exc:
        log.error(f"Login error: {exc}")
        return False


async def _fill_wysiwyg(page, selector: str, value: str):
    """Fill a field that may be a Summernote WYSIWYG editor."""
    try:
        await page.evaluate(
            """([selector, value]) => {
                const ta = document.querySelector(selector);
                if (!ta) return;
                // Try Summernote API first
                if (window.jQuery && window.jQuery(ta).data('summernote')) {
                    window.jQuery(ta).summernote('code', value);
                    return;
                }
                // Fall back: set textarea value and sync the contenteditable sibling
                ta.value = value;
                ta.dispatchEvent(new Event('input', {bubbles: true}));
                ta.dispatchEvent(new Event('change', {bubbles: true}));
                // Also populate the Summernote editable div if present
                const wrapper = ta.closest('.note-editor, .summernote-wrapper')
                    || ta.parentElement;
                if (wrapper) {
                    const editable = wrapper.querySelector('.note-editable[contenteditable]');
                    if (editable) editable.innerHTML = value;
                }
            }""",
            [selector, value],
        )
    except Exception as exc:
        log.warning(f"WYSIWYG fill failed ({selector}): {exc}")
        await _fill_if_exists(page, selector, value)


async def _fill_if_exists(page, selector: str, value: str):
    try:
        el = await page.query_selector(selector)
        if el:
            await el.fill(value)
    except Exception:
        pass


async def _select_if_exists(page, selector: str, value: str):
    try:
        el = await page.query_selector(selector)
        if el:
            await el.select_option(label=value)
    except Exception:
        try:
            el = await page.query_selector(selector)
            if el:
                await el.select_option(value=value)
        except Exception:
            pass


def _parse_date(date_str: str) -> str:
    if not date_str:
        return ""
    for fmt in ["%B %d, %Y", "%Y-%m-%d", "%B %Y"]:
        try:
            dt = datetime.datetime.strptime(date_str, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return date_str