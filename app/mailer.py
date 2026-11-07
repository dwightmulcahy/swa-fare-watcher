"""Gmail SMTP alerts."""
import html
import smtplib
import sys
from email.mime.text import MIMEText

from .config import settings


def send(to, subject, body_html) -> bool:
    if not to:
        return False
    if not (settings.gmail_user and settings.gmail_password):
        print(f"[mail] Gmail not configured; skipped '{subject}' to {to}", file=sys.stderr)
        return False
    msg = MIMEText(body_html, "html")
    msg["Subject"], msg["From"], msg["To"] = subject, settings.gmail_user, to
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(settings.gmail_user, settings.gmail_password)
            s.sendmail(settings.gmail_user, [to], msg.as_string())
        return True
    except Exception as e:
        print(f"[mail] send failed: {e!r}", file=sys.stderr)
        return False


def fmt_dur(m):
    return f"{m // 60}h{m % 60:02d}"


def render_hits(hits):
    e = html.escape
    cols = ["Watch", "Date", "Flights", "Dep–Arr", "Stops", "Duration", "Cash", "Points", "¢/pt", "Use"]
    head = "".join(f"<th style='text-align:left;padding:4px 8px'>{c}</th>" for c in cols)
    body = ""
    for h in hits:
        r = h["row"]
        pts = f'{int(r["points"]):,} + ${r["points_taxes"]:.2f}' if r["points"] else "—"
        cells = [
            f'<a href="{e(h["url"])}">{e(h["watch"])}</a>', e(h["date"]), e(str(r["flights"])),
            f'{e(r["depart"])}–{e(r["arrive"])}', r["stops"], fmt_dur(r["duration"]),
            f'${r["cash"]:.0f}' if r["cash"] else "—", pts,
            f'{r["cpp"]:.2f}' if r["cpp"] else "—", h["use"],
        ]
        body += "<tr>" + "".join(f"<td style='padding:4px 8px'>{c}</td>" for c in cells) + "</tr>"
    return (f"<table style='border-collapse:collapse;font-family:sans-serif;font-size:14px'>"
            f"<tr>{head}</tr>{body}</table>"
            f"<p style='font-family:sans-serif;font-size:13px'>"
            f"<a href='{e(settings.base_url)}'>Open SWA Fare Watcher</a></p>")
