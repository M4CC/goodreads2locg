"""
Submit a single issue to League of Comic Geeks via Playwright browser automation.
"""

import asyncio
import datetime
import json
import logging
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


async def preview_issue_from_goodreads(goodreads_url: str, cfg) -> dict:
    try:
        return await asyncio.to_thread(_scrape_goodreads_issue_no_browser, goodreads_url, cfg)
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

        issue["moderator_comment"] = f"Goodreads source: {goodreads_url}"

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

        issue["moderator_comment"] = f"Goodreads source: {goodreads_url}"

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

        result = await _submit_issue(locg_page, locg_series_url, issue)
        await browser.close()
        return result


async def _scrape_goodreads_issue(page, url: str, cfg) -> dict:
    html = None
    headers = GOODREADS_HEADERS.copy()
    if getattr(cfg, "goodreads_session_cookie", ""):
        headers["Cookie"] = cfg.goodreads_session_cookie

    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        log.warning(f"Failed to fetch Goodreads issue page by requests ({exc}), falling back to browser scraping.")
        await page.goto(url, wait_until="load", timeout=60000)
        await page.wait_for_timeout(1500)
        html = await page.content()

    if not html:
        log.error("Could not load Goodreads issue page content")
        return {}

    soup = BeautifulSoup(html, "html.parser")
    parsed = _parse_goodreads_issue_page(soup, url)

    if not parsed.get("isbn10") or not parsed.get("isbn13"):
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

    parsed = _enrich_isbn_data(parsed)
    return parsed


def _scrape_goodreads_issue_no_browser(url: str, cfg) -> dict:
    headers = GOODREADS_HEADERS.copy()
    if getattr(cfg, "goodreads_session_cookie", ""):
        headers["Cookie"] = cfg.goodreads_session_cookie

    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        parsed = _parse_goodreads_issue_page(soup, url)
        return _enrich_isbn_data(parsed)
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

    parsed = {}
    if json_ld:
        try:
            parsed = json.loads(json_ld)
        except Exception:
            parsed = {}

    fmt = "Trade Paperback"
    cover = parsed.get("image") or txt("meta[property='og:image']", "content")
    synopsis = parsed.get("description") or txt("#description span:last-child") or txt('[data-testid="description"]') or ""

    publication_info = txt('p[data-testid="publicationInfo"]') or _find_detail_local("First published") or ""
    release_date = publication_info.replace("First published", "", 1).strip()

    pages = None
    pages_format = txt('p[data-testid="pagesFormat"]') or _find_detail_local("Format") or ""
    if pages_format:
        pages_match = re.search(r"(\d+)\s+pages", pages_format)
        if pages_match:
            pages = pages_match.group(1)

    isbn_raw = (
        parsed.get("isbn")
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
        m10 = re.search(r"(\d{9}[\dXx])", isbn_raw)
        if m10:
            isbn10 = m10.group(1)
        else:
            m10b = re.search(r"ISBN10[:\s]*([0-9Xx\-\s]{10,})", isbn_raw)
            if m10b:
                isbn10 = re.sub(r"[^0-9Xx]", "", m10b.group(1))

    if not title and parsed.get("name"):
        title = parsed.get("name")

    if not issue_number:
        m = re.search(r"#\s*(\d+)", title)
        if m:
            issue_number = m.group(1)

    if not series_name and parsed.get("name"):
        series_name = parsed.get("name")

    return {
        "goodreads_url": url,
        "goodreads_title": title,
        "author": author,
        "language": parsed.get("inLanguage") or "Portuguese",
        "format": fmt,
        "dimensions": "Oversized",
        "cover_url": cover,
        "synopsis": synopsis,
        "issue_number": issue_number,
        "series_name": series_name,
        "release_date": release_date,
        "pages": pages,
        "isbn": isbn_raw,
        "isbn10": isbn10,
        "isbn13": isbn13,
        "publication_info": publication_info,
    }


def _enrich_isbn_data(parsed: dict) -> dict:
    if parsed.get("isbn10") and parsed.get("isbn13"):
        return parsed

    title = parsed.get("goodreads_title") or parsed.get("title")
    author = parsed.get("author")
    if not title:
        return parsed

    query = f'intitle:"{title}"'
    if author:
        query += f' inauthor:"{author}"'

    try:
        resp = requests.get(
            "https://www.googleapis.com/books/v1/volumes",
            params={"q": query, "maxResults": 5},
            timeout=15,
            headers=GOODREADS_HEADERS,
        )
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("items", []):
            identifiers = item.get("volumeInfo", {}).get("industryIdentifiers", [])
            found_isbn10 = None
            found_isbn13 = None
            for identifier in identifiers:
                id_type = identifier.get("type")
                value = identifier.get("identifier")
                if id_type == "ISBN_10":
                    found_isbn10 = re.sub(r"[^0-9Xx]", "", value)
                elif id_type == "ISBN_13":
                    found_isbn13 = re.sub(r"[^0-9]", "", value)

            if found_isbn10 and not parsed.get("isbn10"):
                parsed["isbn10"] = found_isbn10
            if found_isbn13 and not parsed.get("isbn13"):
                parsed["isbn13"] = found_isbn13

            if parsed.get("isbn10") and parsed.get("isbn13"):
                break

        if parsed.get("isbn10") or parsed.get("isbn13"):
            log.info("Enriched Goodreads issue with ISBN data from Google Books")
    except Exception as exc:
        log.warning(f"Could not enrich ISBN data from Google Books: {exc}")

    if not parsed.get("isbn10") or not parsed.get("isbn13"):
        parsed = _enrich_isbn_data_from_openlibrary(parsed)

    return parsed


def _enrich_isbn_data_from_openlibrary(parsed: dict) -> dict:
    if parsed.get("isbn10") and parsed.get("isbn13"):
        return parsed

    title = parsed.get("goodreads_title") or parsed.get("title")
    author = parsed.get("author")
    if not title:
        return parsed

    params = {"title": title, "limit": 5}
    if author:
        params["author"] = author

    try:
        resp = requests.get(
            "https://openlibrary.org/search.json",
            params=params,
            timeout=15,
            headers=GOODREADS_HEADERS,
        )
        resp.raise_for_status()
        data = resp.json()
        for doc in data.get("docs", []):
            work_key = doc.get("key")
            if not work_key or not work_key.startswith("/works/"):
                continue

            editions_url = f"https://openlibrary.org{work_key}/editions.json?limit=20"
            try:
                editions_resp = requests.get(editions_url, headers=GOODREADS_HEADERS, timeout=15)
                editions_resp.raise_for_status()
                editions = editions_resp.json().get("entries", [])
            except Exception:
                continue

            for edition in editions:
                for isbn10 in edition.get("isbn_10", []) or []:
                    if not parsed.get("isbn10"):
                        parsed["isbn10"] = re.sub(r"[^0-9Xx]", "", str(isbn10))
                for isbn13 in edition.get("isbn_13", []) or []:
                    if not parsed.get("isbn13"):
                        parsed["isbn13"] = re.sub(r"[^0-9]", "", str(isbn13))
                if parsed.get("isbn10") and parsed.get("isbn13"):
                    break
            if parsed.get("isbn10") and parsed.get("isbn13"):
                break

        if parsed.get("isbn10") or parsed.get("isbn13"):
            log.info("Enriched Goodreads issue with ISBN data from Open Library")
    except Exception as exc:
        log.warning(f"Could not enrich ISBN data from Open Library: {exc}")

    return parsed


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
    import imghdr
    import mimetypes

    suffix = imghdr.what(None, data)
    if suffix:
        ext = f".{suffix}"
        mime = mimetypes.types_map.get(ext, "image/jpeg")
    else:
        ext = Path(file_url).suffix or ".jpg"
        mime = mimetypes.guess_type(file_url)[0] or "image/jpeg"

    fname = f"cover{ext}"
    try:
        await el.set_input_files([{"name": fname, "mimeType": mime, "buffer": data}])
        return True
    except Exception as exc:
        log.error(f"Failed to attach cover file: {exc}")
        return False


async def _fill_issue_form(page, series_url: str, issue: dict) -> dict:
    name = issue.get("goodreads_title", "Unknown")

    try:
        submit_url = series_url.rstrip("/") + "/submit-new-issue"
        await page.goto(submit_url, wait_until="networkidle", timeout=30000)

        form_selector = "form[action*='/submit-new-issue'], form:has(input[name='issue_number']), form:has(input[name='title'])"
        try:
            await page.wait_for_selector(form_selector, timeout=30000)
        except Exception:
            return {"goodreads_title": name, "success": False, "error": "no_issue_form_found", "locg_series_url": series_url}

        if issue.get("issue_number"):
            await _fill_if_exists(page, 'input[name="issue_number"], #issue_number', str(issue["issue_number"]))
        else:
            await _fill_if_exists(page, 'input[name="issue_number"], #issue_number', "1")

        if issue.get("goodreads_title"):
            await _fill_if_exists(page, 'input[name="title"], #title', issue["goodreads_title"])

        if issue.get("release_date"):
            formatted_date = _parse_date(issue["release_date"])
            await _fill_if_exists(page, 'input[name="date_release"], input[name="publication_date"], input[type="date"], #date_release, #publication_date', formatted_date)

        if issue.get("format"):
            await _select_if_exists(page, 'select[name="format"], #format', issue["format"])

        if issue.get("dimensions"):
            await _fill_if_exists(
                page,
                'input[name="dimensions"], input[name="dimension"], select[name="dimensions"], select[name="dimension"], textarea[name="dimensions"], textarea[name="dimension"], #dimensions, #dimension',
                issue["dimensions"],
            )

        if issue.get("pages"):
            await _fill_if_exists(page, 'input[name="pages"], #pages', str(issue["pages"]))

        if issue.get("isbn10"):
            await _fill_if_exists(page, 'input[name="isbn10"], #isbn10', issue["isbn10"])
        if issue.get("isbn13"):
            await _fill_if_exists(page, 'input[name="isbn13"], #isbn13', issue["isbn13"])

        if issue.get("cover_url"):
            await _attach_file_if_exists(page, 'input[type="file"][name*="cover"], input[type="file"][id*="cover"], #cover', issue["cover_url"])

        if issue.get("synopsis"):
            await _fill_if_exists(page, 'textarea[name="description"], textarea[name="synopsis"], #description, #synopsis', issue["synopsis"][:2000])

        moderator_comment = issue.get("moderator_comment") or issue.get("goodreads_url", "")
        if moderator_comment:
            await _fill_if_exists(
                page,
                'textarea[name*="mod"], textarea[name*="comment"], textarea[name*="note"], textarea[name*="source"], #notes, #comment, #moderator_comment',
                moderator_comment,
            )

        await page.wait_for_timeout(2000)
        return {"goodreads_title": name, "success": True, "locg_series_url": series_url}

    except Exception as exc:
        return {"goodreads_title": name, "success": False, "error": str(exc), "locg_series_url": series_url}


async def _submit_issue(page, series_url: str, issue: dict) -> dict:
    name = issue.get("goodreads_title", "Unknown")

    try:
        submit_url = series_url.rstrip("/") + "/submit-new-issue"
        await page.goto(submit_url, wait_until="networkidle", timeout=30000)

        form_selector = "form[action*='/submit-new-issue'], form:has(input[name='issue_number']), form:has(input[name='title'])"
        try:
            await page.wait_for_selector(form_selector, timeout=30000)
        except Exception:
            return {"goodreads_title": name, "success": False, "error": "no_issue_form_found", "locg_series_url": series_url}

        if issue.get("issue_number"):
            await _fill_if_exists(page, 'input[name="issue_number"], #issue_number', str(issue["issue_number"]))
        else:
            await _fill_if_exists(page, 'input[name="issue_number"], #issue_number', "1")

        if issue.get("goodreads_title"):
            await _fill_if_exists(page, 'input[name="title"], #title', issue["goodreads_title"])

        if issue.get("release_date"):
            formatted_date = _parse_date(issue["release_date"])
            await _fill_if_exists(page, 'input[name="date_release"], input[name="publication_date"], input[type="date"], #date_release, #publication_date', formatted_date)

        if issue.get("format"):
            await _select_if_exists(page, 'select[name="format"], #format', issue["format"])

        if issue.get("dimensions"):
            await _fill_if_exists(
                page,
                'input[name="dimensions"], input[name="dimension"], select[name="dimensions"], select[name="dimension"], textarea[name="dimensions"], textarea[name="dimension"], #dimensions, #dimension',
                issue["dimensions"],
            )

        if issue.get("pages"):
            await _fill_if_exists(page, 'input[name="pages"], #pages', str(issue["pages"]))

        if issue.get("isbn10"):
            await _fill_if_exists(page, 'input[name="isbn10"], #isbn10', issue["isbn10"])
        if issue.get("isbn13"):
            await _fill_if_exists(page, 'input[name="isbn13"], #isbn13', issue["isbn13"])

        if issue.get("cover_url"):
            await _attach_file_if_exists(page, 'input[type="file"][name*="cover"], input[type="file"][id*="cover"], #cover', issue["cover_url"])

        if issue.get("synopsis"):
            await _fill_if_exists(page, 'textarea[name="description"], textarea[name="synopsis"], #description, #synopsis', issue["synopsis"][:2000])

        moderator_comment = issue.get("moderator_comment") or issue.get("goodreads_url", "")
        if moderator_comment:
            await _fill_if_exists(
                page,
                'textarea[name*="mod"], textarea[name*="comment"], textarea[name*="note"], textarea[name*="source"], #notes, #comment, #moderator_comment',
                moderator_comment,
            )

        submit_btn = await page.query_selector('button[type="submit"], input[type="submit"]')
        form = None
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
                        await form.evaluate(
                            "form => form.requestSubmit ? form.requestSubmit() : form.submit()"
                        )
                    except Exception as exc2:
                        return {"goodreads_title": name, "success": False, "error": f"form_submit_failed: {exc}, form_submit_failed: {exc2}", "locg_series_url": series_url}
        else:
            form = await page.query_selector(form_selector)
            if not form:
                return {"goodreads_title": name, "success": False, "error": "no_submit_button", "locg_series_url": series_url}
            try:
                await form.evaluate(
                    "form => form.requestSubmit ? form.requestSubmit() : form.submit()"
                )
            except Exception as exc:
                return {"goodreads_title": name, "success": False, "error": f"form_submit_failed: {exc}", "locg_series_url": series_url}

        try:
            await page.wait_for_navigation(timeout=15000)
        except Exception:
            pass

        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        current_url = page.url
        log.info(f"After submit, URL: {current_url}")

        await page.wait_for_timeout(1000)

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
                await page.wait_for_load_state("networkidle", timeout=10000)
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
