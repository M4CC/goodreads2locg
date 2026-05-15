import json
from pathlib import Path


class Config:
    def __init__(
        self,
        goodreads_session_cookie: str = "",
        locg_username: str = "",
        locg_password: str = "",
        headless: bool = False,
        delay_between_ms: int = 1500,
    ):
        self.goodreads_session_cookie = goodreads_session_cookie
        self.locg_username = locg_username
        self.locg_password = locg_password
        self.headless = headless
        self.delay_between_ms = delay_between_ms

    @staticmethod
    def load(path: Path = Path("config.json")) -> "Config":
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        raw = json.loads(path.read_text(encoding="utf-8"))
        return Config(
            goodreads_session_cookie=raw.get("goodreads_session_cookie", ""),
            locg_username=raw.get("locg_username", ""),
            locg_password=raw.get("locg_password", ""),
            headless=bool(raw.get("headless", False)),
            delay_between_ms=int(raw.get("delay_between_ms", 1500)),
        )
