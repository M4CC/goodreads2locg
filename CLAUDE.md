# goodreads2comic

Automation tool that scrapes Goodreads book pages and submits them as issues to League of Comic Geeks (LOCG).

## Stack
- Python 3
- Playwright (browser automation for LOCG submission)
- requests + BeautifulSoup (Goodreads scraping)

## Key files
- `main.py` — entry point, orchestrates the pipeline
- `submitter_locg.py` — Playwright-based LOCG submission logic
- `config.json` — credentials and settings (copy from `config.example.json`)
- `done.json` — tracks submitted books (do not delete)
- `skipped.json` — tracks manually skipped books
- `session.json` / `locg_session.json` — saved browser sessions

## Run
```
python main.py
```
Modes: interactive, CLI, preview, review, headless.

## Notes
- `done.json` and `skipped.json` are state files — losing them means re-processing already submitted books.
- LOCG session is cached in `locg_session.json` to avoid repeated login.
