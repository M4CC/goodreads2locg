# Goodreads to League of Comic Geeks Issue Submitter

> **Disclaimer:** This project was built through vibe coding and is provided as-is, with no guarantees whatsoever. The author takes no responsibility for malfunctions, data loss, security vulnerabilities, misuse of the script, or any other issues that may arise from using it. Use at your own risk.

Scrapes a Goodreads book page and auto-submits it as a new issue into an existing [League of Comic Geeks](https://leagueofcomicgeeks.com) series, filling in title, release date, format, pages, ISBN, cover image, and contributor credits.

## Setup

1. Install dependencies:

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

2. Copy the example config and fill in your credentials:

```bash
cp config.example.json config.json
```

```json
{
  "goodreads_email": "YOUR_GOODREADS_EMAIL",
  "goodreads_password": "YOUR_GOODREADS_PASSWORD",
  "locg_username": "YOUR_LOCG_USERNAME",
  "locg_password": "YOUR_LOCG_PASSWORD",
  "headless": false,
  "delay_between_ms": 1500
}
```

> `config.json` is gitignored — your credentials will never be committed.

## Usage

### Interactive mode (recommended)

Just run the script with no arguments. It will prompt for the LOCG series URL and the Goodreads book URL, show a full summary of what was parsed, and ask if you want to submit another issue. It remembers the last series URL between runs.

```bash
python main.py
```

### Command-line mode

```bash
python main.py "GOODREADS_URL" "LOCG_SERIES_URL"
```

### Preview without submitting

Scrapes Goodreads and prints the parsed metadata — no browser, no submission.

```bash
python main.py --preview "GOODREADS_URL" "LOCG_SERIES_URL"
```

### Review mode

Fills the LOCG form in a visible browser window and waits for you to review before submitting manually.

```bash
python main.py --review "GOODREADS_URL" "LOCG_SERIES_URL"
```

### Headless mode

Override `headless: false` in config without editing the file:

```bash
python main.py --headless "GOODREADS_URL" "LOCG_SERIES_URL"
```

## Notes

- Goodreads credentials are used to log in when the book page requires authentication.
- Browser sessions are persisted to `session.json` so subsequent runs skip login.
- LOCG submissions are retried automatically if the server is temporarily down (Cloudflare 5xx).
- Successful submissions are appended to `done.json`; failures to `skipped.json`.
- Format is mapped from Goodreads (Hardcover / Trade Paperback / Softcover); dimensions are always set to Oversized.
- Contributor credits (writer, artist, colorist, etc.) are scraped from Goodreads and filled into the LOCG credits field.
