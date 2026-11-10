"""Gmail SMTP alerts."""
import html
import smtplib
from email.mime.text import MIMEText

from . import swa
from .config import settings


def send(to, subject, body_html) -> bool:
    if not to:
        return False
    if not (settings.gmail_user and settings.gmail_password):
        from . import applog
        applog.warn(f"Gmail not configured; email '{subject}' to {to} not sent", source="mail")
        return False
    from . import applog
    msg = MIMEText(body_html, "html")
    msg["Subject"], msg["From"], msg["To"] = subject, settings.gmail_user, to
    password = settings.gmail_password.replace(" ", "")   # app passwords are shown as "abcd efgh ..."
    errors = []
    # Port 465 (SSL) first, then 587 (STARTTLS): some networks and ISPs block or cut one of them,
    # which shows up as "Connection unexpectedly closed"
    for port in _ports():
        try:
            if port == 465:
                conn = smtplib.SMTP_SSL(settings.gmail_host, 465, timeout=30)
            else:
                conn = smtplib.SMTP(settings.gmail_host, port, timeout=30)
            with conn as s:
                if port != 465:
                    s.ehlo()
                    s.starttls()
                    s.ehlo()
                s.login(settings.gmail_user, password)
                s.sendmail(settings.gmail_user, [to], msg.as_string())
            applog.info(f"Email sent to {to} (port {port}): {subject}", source="mail")
            return True
        except smtplib.SMTPAuthenticationError as e:
            applog.error(f"Email to {to} failed: Gmail rejected the login ({e.smtp_code}). GMAIL_USER must be the "
                         "full address and GMAIL_APP_PASSWORD a 16-letter app password "
                         "(myaccount.google.com/apppasswords, needs 2-Step Verification).", source="mail")
            return False
        except Exception as e:
            errors.append(f"port {port}: {e!r}")
    applog.error(f"Email to {to} failed ({'; '.join(errors)}). If both ports fail, outgoing mail from this "
                 "server is probably blocked; check the firewall/ISP, or run `python -m app test-email`.",
                 source="mail")
    return False


def _ports():
    first = settings.gmail_port
    return [first] + [p for p in (465, 587) if p != first]


def fmt_dur(m):
    return f"{m // 60}h{m % 60:02d}"


def render_hits(hits):
    e = html.escape
    cols = ["Watch", "Date", "Flights", "Dep–Arr", "Stops", "Duration", "Cash", "Points", "¢/pt", "Use"]
    head = "".join(f"<th style='text-align:left;padding:4px 8px'>{c}</th>" for c in cols)
    body = ""
    for h in hits:
        t = h.get("trip")
        if t:  # round trip: one row, both legs
            def leg(f, url=None):
                if not f:
                    return "—"
                txt = (f'{e(str(f["flights"]))} {e(f["depart"])}–{e(f["arrive"])} '
                       f'({e(swa.stops_label(f["stops"], f.get("via") or ""))})')
                if f.get("origin"):
                    txt = f'{e(f["origin"])}→{e(f["dest"])} {txt}'
                return f'<a href="{e(url)}">{txt}</a>' if url else txt
            pts = f'{int(t["points"]):,} + ${t["taxes"]:.2f}' if t["points"] else "—"
            cells = [
                f'<a href="{e(h["url"])}">{e(h["watch"])}</a>',
                f'{e(t["out_date"])} → {e(t["ret_date"])} ({t["nights"]} night{"s" if t["nights"] != 1 else ""})',
                f'{leg(t["out_cash"])}<br>{leg(t["ret_cash"], h.get("ret_url"))}', "", "", "",
                f'${t["cash"]:.0f}' if t["cash"] else "—", pts,
                f'{t["cpp"]:.2f}' if t["cpp"] else "—", e(h["use"]),
            ]
            body += "<tr>" + "".join(f"<td style='padding:4px 8px;vertical-align:top'>{c}</td>" for c in cells) + "</tr>"
            continue
        r = h["row"]
        pts = f'{int(r["points"]):,} + ${r["points_taxes"]:.2f}' if r["points"] else "—"
        cells = [
            f'<a href="{e(h["url"])}">{e(h["watch"])}</a>', e(h["date"]), e(str(r["flights"])),
            f'{e(r["depart"])}–{e(r["arrive"])}', e(swa.stops_label(r["stops"], r.get("via") or "")),
            fmt_dur(r["duration"]),
            f'${r["cash"]:.0f}' if r["cash"] else "—", pts,
            f'{r["cpp"]:.2f}' if r["cpp"] else "—", h["use"],
        ]
        body += "<tr>" + "".join(f"<td style='padding:4px 8px'>{c}</td>" for c in cells) + "</tr>"
    return (f"<table style='border-collapse:collapse;font-family:sans-serif;font-size:14px'>"
            f"<tr>{head}</tr>{body}</table>"
            f"<p style='font-family:sans-serif;font-size:13px'>"
            f"<a href='{e(settings.base_url)}'>Open SWA Fare Watcher</a></p>")
