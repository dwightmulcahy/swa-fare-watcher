"""Every setting the app (and its compose files) read from the environment, with its default,
a one-line explanation and how to edit it. The admin Settings panel and the generated .env file
come from this list; tests keep it in step with config.py and the README."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Setting:
    name: str
    default: str
    help: str
    kind: str = "text"        # text | int | float | bool | time | range | secret | choice | url
    choices: tuple = field(default_factory=tuple)
    compose: bool = False     # read by docker compose itself: applies after `docker compose up -d`
    advanced: bool = False    # rarely changed: shown folded away


GROUPS = [
    ("App", [
        Setting("BASE_URL", "http://localhost:8080", "Public URL of the app, used in email links", "url"),
        Setting("COOKIE_SECURE", "false", "Set true when the app is served over HTTPS", "bool"),
        Setting("SESSION_DAYS", "14", "How long a web sign-in lasts (days)", "int"),
        Setting("SECRET_KEY", "", "Encrypts stored Southwest sessions. Blank = auto-generated /data/secret.key. "
                "Never change it once set: stored sessions become unreadable", "secret"),
        Setting("SWA_SKIP_TZ_CHECK", "false", "Skip the start-up time zone check (e.g. behind a VPN you trust)",
                "bool", advanced=True),
    ]),
    ("First admin account (created on first start only)", [
        Setting("ADMIN_USERNAME", "admin", "Username of the first admin account"),
        Setting("ADMIN_PASSWORD", "", "Its password. Blank = a temporary one is printed to the container log "
                "and must be changed at sign-in", "secret"),
    ]),
    ("Email (Gmail app password: myaccount.google.com/apppasswords, needs 2-step verification)", [
        Setting("GMAIL_USER", "", "Gmail address alerts are sent from"),
        Setting("GMAIL_APP_PASSWORD", "", "Its 16-character app password", "secret"),
        Setting("GMAIL_SMTP_HOST", "smtp.gmail.com", "SMTP server", advanced=True),
        Setting("GMAIL_SMTP_PORT", "465", "SMTP port (SSL)", "int", advanced=True),
    ]),
    ("Schedule", [
        Setting("SWA_SCHEDULER", "true", "false turns off scheduled runs (Search now still works)", "bool"),
        Setting("SWA_HABIT_SLOTS", "true", "Each user checks at 2-3 habitual times of day (morning, lunch, "
                "evening; later on weekends), one visit each; the rest continues at the next", "bool"),
        Setting("SWA_DAILY_AT", "07:20", "Daily run time, when habitual times are off", "time"),
        Setting("SWA_DAILY_JITTER_MINUTES", "45", "Each day starts within ± this many minutes of SWA_DAILY_AT "
                "(habitual times off)", "int"),
        Setting("SWA_USER_SPREAD_MINUTES", "120", "Each user's own usual time within ± this of SWA_DAILY_AT "
                "(habitual times off)", "int"),
        Setting("SWA_SKIP_DAY_CHANCE", "0.12", "Chance a user takes a day off (never two in a row, never with a "
                "booked flight to watch); 0 = every day", "float"),
    ]),
    ("Searching", [
        Setting("SWA_CALENDAR", "true", "Read the low-fare calendar first and fully search only the cheapest "
                "days", "bool"),
        Setting("SWA_CALENDAR_DETAIL_DATES", "6", "Most days (round trip: date pairs) fully searched after the "
                "calendar, per direction", "int"),
        Setting("SWA_ROTATE_DATES", "true", "Scheduled runs search far-off dates less often and skip dates "
                "whose calendar price hasn't changed", "bool"),
        Setting("SWA_DAILY_PAGE_BUDGET", "20,35", "Page loads per account per day, a random number in this "
                "range (0,0 = no budget)", "range"),
        Setting("SWA_SEARCH_DELAY", "8,20", "Seconds between searches: log-normal around the middle, with an "
                "occasional 45-180 s break", "range"),
        Setting("SWA_VISIT_SEARCHES", "6,14", "Page loads per visit; the browser closes between visits "
                "(0,0 = one visit per run)", "range"),
        Setting("SWA_VISIT_BREAK", "20,90", "Minutes between visits when a run has several (habitual times "
                "off)", "range"),
        Setting("SWA_FORM_SHARE", "0.5", "Share of searches started from the Book a Flight form instead of "
                "a bookmarked address", "float"),
        Setting("SWA_BLOCK_PAUSE_HOURS", "36", "After a block or challenge page, pause that user's scheduled "
                "runs this long", "int"),
        Setting("MAX_DATES_PER_WATCH", "31", "Hard cap on dates per watch", "int"),
        Setting("SWA_HOME_URL", "https://www.southwest.com/", "Southwest home page", "url", advanced=True),
        Setting("SWA_BOOK_URL", "https://www.southwest.com/air/booking/", "Book a Flight page", "url",
                advanced=True),
        Setting("SWA_CALENDAR_URL", "https://www.southwest.com/air/low-fare-calendar/select-dates",
                "Low-fare calendar page", "url", advanced=True),
    ]),
    ("Browser", [
        Setting("SWA_GPU", "false", "Draw with the NAS's Intel/AMD GPU instead of Chrome's software renderer "
                "(needs devices: /dev/dri and chmod 666 on the render node)", "bool"),
        Setting("SWA_GPU_DEVICE", "/dev/dri/renderD128", "GPU render node used with SWA_GPU", advanced=True),
        Setting("SWA_HUMANIZE", "true", "Human-like mouse, keyboard, scrolling and reading around searches",
                "bool"),
        Setting("SWA_TIMEZONE", "", "Browser time zone (blank = the container's TZ); match where the IP is"),
        Setting("SWA_HEADLESS", "false", "true runs Chrome headless (no virtual screen; more likely blocked)",
                "bool", advanced=True),
        Setting("SWA_BROWSER_CHANNEL", "auto", "auto = Google Chrome if installed, else Chromium", "choice",
                ("auto", "chrome", "chromium", "msedge"), advanced=True),
    ]),
    ("Southwest sign-in", [
        Setting("SWA_AUTO_LOGIN", "true", "Let users turn on automatic sign-in", "bool"),
        Setting("SWA_CODE_WAIT_SECONDS", "600", "How long a sign-in waits for a verification code", "int"),
        Setting("SWA_CODE_POLL_SECONDS", "3", "How often it checks for the code", "int", advanced=True),
    ]),
    ("Defaults for new users (an admin can change them per user)", [
        Setting("DEFAULT_MAX_WATCHES", "5", "Active watches per user", "int"),
        Setting("DEFAULT_MAX_SEARCHES_PER_DAY", "40", "Searches per user per day", "int"),
    ]),
    ("Alerts and upkeep", [
        Setting("SWA_DIGEST_AT", "18:00", "When daily and weekly digest emails go out", "time"),
        Setting("SWA_STALE_HOURS", "26", "Email the admins when no run has finished for this long; 0 = off",
                "float"),
        Setting("SWA_BACKUP_KEEP", "7", "Daily database backups kept in data/backups; 0 = off", "int"),
        Setting("SWA_DATA_DIR", "/data", "Where the database, browser profiles and logs live", advanced=True),
    ]),
    ("Docker (read by docker compose; apply with docker compose up -d)", [
        Setting("TZ", "UTC", "Time zone of the container (e.g. America/Costa_Rica)", compose=True),
        Setting("IMAGE", "ghcr.io/dwightmulcahy/swa-fare-watcher:latest", "Image to run", compose=True),
        Setting("HOST_PORT", "8080", "Port on the NAS the app is reachable on", "int", compose=True),
        Setting("QNAP_DATA_DIR", "/share/Container/swa-fare-watcher/data", "Data folder on the NAS "
                "(compose.qnap.yaml)", compose=True),
        Setting("DATA_DIR", "./data", "Data folder (compose.yaml)", compose=True),
        Setting("ENV_FILE", "./.env", "This file, as seen by docker compose; mounted into the container so the "
                "admin Settings page can edit it", compose=True),
    ]),
    ("Only for python -m app login-test on your own computer (the server ignores these)", [
        Setting("SWA_USERNAME", "", "Southwest username for the sign-in test", advanced=True),
        Setting("SWA_PASSWORD", "", "Its password", "secret", advanced=True),
    ]),
]

ALL = {s.name: s for _, items in GROUPS for s in items}
