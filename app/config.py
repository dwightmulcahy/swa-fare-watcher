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
        # Each day's run starts at a random time within ± this many minutes of daily_at
        self.daily_jitter_min = max(0, int(_env("SWA_DAILY_JITTER_MINUTES", "45")))
        # Each user keeps their own usual time within ± this many minutes of daily_at (spread apart)
        self.user_spread_min = max(0, int(_env("SWA_USER_SPREAD_MINUTES", "120")))
        # Chance a user's search is skipped on a given day (never two days running; 0 = never)
        self.skip_day_chance = max(0.0, min(0.5, float(_env("SWA_SKIP_DAY_CHANCE", "0.12"))))
        self.scheduler = _bool("SWA_SCHEDULER", True)
        self.base_url = _env("BASE_URL", "http://localhost:8080").rstrip("/")
        self.cookie_secure = _bool("COOKIE_SECURE", False)
        self.session_days = int(_env("SESSION_DAYS", "14"))
        self.gmail_user = _env("GMAIL_USER")
        self.gmail_password = _env("GMAIL_APP_PASSWORD")
        self.gmail_host = _env("GMAIL_SMTP_HOST", "smtp.gmail.com")
        self.gmail_port = int(_env("GMAIL_SMTP_PORT", "465"))   # 587 (STARTTLS) is tried too
        self.admin_username = _env("ADMIN_USERNAME", "admin")
        self.admin_password = _env("ADMIN_PASSWORD")
        self.headless = _bool("SWA_HEADLESS", False)
        # auto = Google Chrome if installed, else Playwright's bundled Chromium
        self.browser_channel = _env("SWA_BROWSER_CHANNEL", "auto")
        self.browser_timezone = _env("SWA_TIMEZONE")  # default: the container's TZ
        self.humanize = _bool("SWA_HUMANIZE", True)
        # Automatic Southwest sign-in with saved credentials (users opt in under Settings)
        self.auto_login = _bool("SWA_AUTO_LOGIN", True)
        self.code_wait_seconds = int(_env("SWA_CODE_WAIT_SECONDS", "600"))
        self.code_poll_seconds = float(_env("SWA_CODE_POLL_SECONDS", "3"))
        self.home_url = _env("SWA_HOME_URL", "https://www.southwest.com/")  # tests point this at a fake site
        # Low-fare calendar: one page load per month instead of one per date; full searches
        # only for the days that can beat the target (at most this many per direction).
        # After Southwest shows a challenge or block page: stop the run and skip that user's
        # scheduled runs for this many hours ("Search now" still works)
        self.block_pause_hours = float(_env("SWA_BLOCK_PAUSE_HOURS", "36"))
        # Share of flight-search page loads started from the home page's Book a Flight form
        # (like a person) rather than the search address (like a bookmark); 0 turns it off
        self.form_share = max(0.0, min(1.0, float(_env("SWA_FORM_SHARE", "0.5"))))
        self.book_url = _env("SWA_BOOK_URL", "https://www.southwest.com/air/booking/")   # Book a Flight page
        self.calendar = _bool("SWA_CALENDAR", True)
        self.calendar_url = _env("SWA_CALENDAR_URL", "https://www.southwest.com/air/low-fare-calendar/select-dates")
        self.calendar_detail_dates = int(_env("SWA_CALENDAR_DETAIL_DATES", "6"))
        lo, hi = (float(x) for x in _env("SWA_SEARCH_DELAY", "8,20").split(","))
        self.search_delay = (lo, hi)
        # A run is split into visits of this many page loads (random in range; 0,0 = one visit)...
        lo, hi = (int(x) for x in _env("SWA_VISIT_SEARCHES", "6,14").split(","))
        self.visit_searches = (max(1, lo), max(hi, lo)) if hi > 0 else (0, 0)
        # ...with a break of this many minutes between visits (browser closed)
        lo, hi = (float(x) for x in _env("SWA_VISIT_BREAK", "20,90").split(","))
        self.visit_break = (lo, max(hi, lo))
        self.max_dates_per_watch = int(_env("MAX_DATES_PER_WATCH", "31"))
        # Defaults applied to new users (admin can change per user; blank = unlimited)
        self.default_max_watches = int(_env("DEFAULT_MAX_WATCHES", "5"))
        self.default_max_searches = int(_env("DEFAULT_MAX_SEARCHES_PER_DAY", "40"))
        # Nightly copy of the database into <data>/backups, keeping this many days (0 = off)
        self.backup_keep = max(0, int(_env("SWA_BACKUP_KEEP", "7")))
        # Email the admins when no search run has finished for this many hours (0 = off)
        self.stale_hours = max(0.0, float(_env("SWA_STALE_HOURS", "26")))
        # Daily/weekly digest emails go out at this local time (users choose the mode in Settings)
        self.digest_at = _env("SWA_DIGEST_AT", "18:00")
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
