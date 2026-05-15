# Goodreads to League of Comic Geeks Issue Submitter

This project scrapes a Goodreads issue/book page and submits it as a new issue
into an existing League of Comic Geeks series.

## Setup

1. Install Python dependencies:

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

2. Fill `config.json` with your credentials:

```json
{
  "goodreads_session_cookie": "YOUR_GOODREADS_SESSION_COOKIE",
  "locg_username": "YOUR_LOCG_USERNAME",
  "locg_password": "YOUR_LOCG_PASSWORD",
  "headless": false,
  "delay_between_ms": 1500
}
```

## Usage

Preview the Goodreads metadata without submitting:

```bash
python main.py --preview "GOODREADS_URL" "LOCG_SERIES_URL"
```

Open the LOCG form in browser and review before submit:

```bash
python main.py --review "GOODREADS_URL" "LOCG_SERIES_URL"
```

Submit the issue automatically:

```bash
python main.py "GOODREADS_URL" "LOCG_SERIES_URL"
```

## Notes

- The script attempts to populate ISBN-10 and ISBN-13.
- It also sets format to `Trade Paperback` and dimensions to `Oversized`.
- Submitted results are appended to `done.json`.
- Failed submissions are recorded in `skipped.json`.
