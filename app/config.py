"""Settings from environment variables."""
import os
import secrets
from pathlib import Path


def _env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _bool(name, default=False):
    v = _env(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


class Settings:
    def __init__(self):
        self.data_dir = Path(_env("SWA_DATA_DIR", Path.cwd() / "data")).resolve()
        self.db_path = self.data_dir / "app.db"
        self.debug_dir = self.data_dir / "debug"
        self.daily_at = _env("SWA_DAILY_AT", "07:20")
        self.scheduler = _bool("SWA_SCHEDULER", True)
        self.base_url = _env("BASE_URL", "http://localhost:8080").rstrip("/")
        self.cookie_secure = _bool("COOKIE_SECURE", False)
        self.session_days = int(_env("SESSION_DAYS", "14"))
        self.gmail_user = _env("GMAIL_USER")
        self.gmail_password = _env("GMAIL_APP_PASSWORD")
        self.admin_username = _env("ADMIN_USERNAME", "admin")
        self.admin_password = _env("ADMIN_PASSWORD")
        self.headless = _bool("SWA_HEADLESS", False)
        self.browser_channel = _env("SWA_BROWSER_CHANNEL")
        lo, hi = (float(x) for x in _env("SWA_SEARCH_DELAY", "8,20").split(","))
        self.search_delay = (lo, hi)
        self.max_dates_per_watch = int(_env("MAX_DATES_PER_WATCH", "31"))
        # Defaults applied to new users (admin can change per user; blank = unlimited)
        self.default_max_watches = int(_env("DEFAULT_MAX_WATCHES", "5"))
        self.default_max_searches = int(_env("DEFAULT_MAX_SEARCHES_PER_DAY", "40"))
        self._secret = _env("SECRET_KEY")

    @property
    def secret_key(self):
        if self._secret:
            return self._secret
        path = self.data_dir / "secret.key"
        if not path.exists():
            self.data_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(secrets.token_urlsafe(48))
            path.chmod(0o600)
        self._secret = path.read_text().strip()
        return self._secret


settings = Settings()
