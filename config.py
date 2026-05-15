import json
from pathlib import Path


class Config:
    def __init__(
        self,
        goodreads_email: str = "",
        goodreads_password: str = "",
        locg_username: str = "",
        locg_password: str = "",
        headless: bool = False,
        delay_between_ms: int = 1500,
    ):
        self.goodreads_email = goodreads_email
        self.goodreads_password = goodreads_password
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
            goodreads_email=raw.get("goodreads_email", ""),
            goodreads_password=raw.get("goodreads_password", ""),
            locg_username=raw.get("locg_username", ""),
            locg_password=raw.get("locg_password", ""),
            headless=bool(raw.get("headless", False)),
            delay_between_ms=int(raw.get("delay_between_ms", 1500)),
        )
