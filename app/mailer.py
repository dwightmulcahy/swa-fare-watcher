"""Gmail SMTP alerts."""
import datetime as dt
import html
import smtplib
from email.mime.text import MIMEText
from pathlib import Path

from . import swa
from .config import settings


def send(to, subject, body_html, attachments=()) -> bool:
    """Email `body_html`, with optional file attachments (paths)."""
    if not to:
        return False
    if not (settings.gmail_user and settings.gmail_password):
        from . import applog
        applog.warn(f"Gmail not configured; email '{subject}' to {to} not sent", source="mail")
        return False
    from . import applog
    msg = MIMEText(body_html, "html")
    if attachments:
        from email.mime.application import MIMEApplication
        from email.mime.multipart import MIMEMultipart
        outer = MIMEMultipart("mixed")
        outer.attach(msg)
        for path in attachments:
            part = MIMEApplication(Path(path).read_bytes())
            part.add_header("Content-Disposition", "attachment", filename=Path(path).name)
            outer.attach(part)
        msg = outer
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


# Southwest palette: Bold Blue, Warm Red, Sunrise Yellow, Summit Silver, dark navy text
BLUE, RED, YELLOW, SILVER, NAVY = "#304CB2", "#C6182A", "#FFBF27", "#CCCCCC", "#111B40"
FONT = "font-family:Arial,Helvetica,sans-serif"


def _leg_line(label, date, f, url=None):
    e = html.escape
    if not f:
        return (f"<tr><td style='{FONT};font-size:12px;color:#636363;padding:2px 0;width:44px'>{label}</td>"
                f"<td style='{FONT};font-size:14px;color:{NAVY}'>{e(date)} · —</td></tr>")
    where = f'{e(f["origin"])} → {e(f["dest"])} · ' if f.get("origin") else ""
    when = f'{e(f["depart"])} – {e(f["arrive"])}'
    detail = (f'{where}#{e(str(f["flights"]))} · {e(swa.stops_label(f["stops"], f.get("via") or ""))}'
              + (f' · {fmt_dur(f["duration"])}' if f.get("duration") else ""))
    head = f"<a href='{e(url)}' style='color:{BLUE};text-decoration:none'>{e(date)}</a>" if url else e(date)
    return (f"<tr><td style='{FONT};font-size:12px;color:#636363;padding:4px 0;width:44px;vertical-align:top'>"
            f"{label}</td><td style='{FONT};font-size:14px;color:{NAVY};padding:4px 0'>"
            f"<b>{head}</b> &nbsp;<b>{when}</b><br>"
            f"<span style='font-size:12px;color:#636363'>{detail}</span></td></tr>")


def _history(h):
    """'↓ $48 since your last alert · lowest seen before $310 on Mar 3' line, or ''."""
    bits = []
    if h.get("drop"):
        bits.append(f"<b style='color:#1a7f37'>↓ {html.escape(h['drop'])}</b> since your last alert")
    if h.get("cpp_note"):
        bits.append(f"<b style='color:#1a7f37'>★ {html.escape(h['cpp_note'])}</b>")
    low = h.get("low")
    if low:
        when = dt.date.fromisoformat(low[1][:10]).strftime("%b %-d")
        bits.append(f"lowest seen before: ${low[0]:,.0f} on {when}")
    if not bits:
        return ""
    return f"<div style='{FONT};font-size:12px;color:#636363;padding:0 0 8px'>{' · '.join(bits)}</div>"


MANAGE_URL = "https://www.southwest.com/air/manage-reservation/"


def _card(title, url, sub, legs, cash, points, cpp, use, extra="", button="Book on Southwest", button_url=None):
    e = html.escape
    price = f"<div style='{FONT};font-size:26px;font-weight:bold;color:{NAVY}'>{cash}</div>" if cash else ""
    pts = f"<div style='{FONT};font-size:13px;color:#636363'>{points}</div>" if points else ""
    cpp_txt = f"<div style='{FONT};font-size:12px;color:#636363'>{cpp}¢ per point</div>" if cpp else ""
    return f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
       style="border:1px solid {SILVER};border-radius:6px;margin:0 0 16px;background:#fff">
  <tr><td colspan="2" style="background:{BLUE};padding:10px 14px;border-radius:6px 6px 0 0">
    <a href="{e(url)}" style="{FONT};font-size:16px;font-weight:bold;color:#fff;text-decoration:none">{e(title)}</a>
    <div style="{FONT};font-size:12px;color:#dfe5ff">{sub}</div></td></tr>
  <tr><td style="padding:10px 14px;vertical-align:top">
    <table role="presentation" cellpadding="0" cellspacing="0">{legs}</table></td>
    <td style="padding:10px 14px;text-align:right;vertical-align:top;white-space:nowrap">{price}{pts}{cpp_txt}</td></tr>
  <tr><td colspan="2" style="padding:0 14px 12px">{extra}
    <span style="{FONT};font-size:13px;color:{NAVY};background:#FFF4D6;border-left:4px solid {YELLOW};
                 padding:6px 10px;display:inline-block">Pay with: <b>{e(use)}</b></span>
    <a href="{e(button_url or url)}" style="{FONT};float:right;background:{YELLOW};color:{NAVY};font-weight:bold;font-size:14px;
       padding:8px 16px;border-radius:4px;text-decoration:none">{e(button)}</a></td></tr>
</table>"""


def render_hits(hits):
    e = html.escape
    cards = ""
    for h in hits:
        t = h.get("trip")
        if t:
            n = t["nights"]
            legs = (_leg_line("Out", t["out_date"], t["out_cash"] or t["out_points"], h["url"])
                    + _leg_line("Back", t["ret_date"], t["ret_cash"] or t["ret_points"], h.get("ret_url")))
            pts = f'{int(t["points"]):,} pts + ${t["taxes"] or 0:.2f}' if t["points"] else ""
            cards += _card(h["watch"], h["url"], f"Round trip · {n} night{'s' if n != 1 else ''}", legs,
                           f'${t["cash"]:,.0f}' if t["cash"] else "", pts,
                           f'{t["cpp"]:.2f}' if t["cpp"] else "", h["use"], _history(h))
            continue
        r = h["row"]
        pts = f'{int(r["points"]):,} pts + ${r["points_taxes"] or 0:.2f}' if r["points"] else ""
        if h.get("rebook"):
            cards += _card(h["watch"], h["url"], "Change to the lower fare: Southwest returns the difference "
                           "(flight credit, or to your card on refundable fares; points go back to your account)",
                           _leg_line("Flight", h["date"], r, h["url"]),
                           f'${r["cash"]:,.0f}' if r["cash"] else "", pts, "", h["use"],
                           button="Change flight", button_url=h.get("manage_url") or MANAGE_URL)
            continue
        cards += _card(h["watch"], h["url"], "One way", _leg_line("Flight", h["date"], r, None),
                       f'${r["cash"]:,.0f}' if r["cash"] else "", pts,
                       f'{r["cpp"]:.2f}' if r["cpp"] else "", h["use"], _history(h))
    n = len(hits)
    return f"""<!doctype html><html><body style="margin:0;padding:0;background:#F2F2F2">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F2F2F2">
<tr><td align="center" style="padding:16px 8px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:640px">
  <tr><td style="background:{NAVY};padding:14px 16px;border-radius:6px 6px 0 0">
    <span style="{FONT};font-size:20px;font-weight:bold;color:#fff">SWA Fare Watcher</span>
    <span style="{FONT};font-size:13px;color:{YELLOW};float:right;padding-top:4px">{n} new deal{'s' if n != 1 else ''}</span>
  </td></tr>
  <tr><td style="font-size:0;line-height:0;height:4px">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
      <td height="4" style="background:{BLUE}"></td><td height="4" style="background:{RED}"></td>
      <td height="4" style="background:{YELLOW}"></td></tr></table></td></tr>
  <tr><td style="padding:16px 0 0">{cards}</td></tr>
  <tr><td style="{FONT};font-size:12px;color:#636363;text-align:center;padding:4px 0 16px">
    Prices are what Southwest showed at search time and can change.
    <a href="{e(settings.base_url)}" style="color:{BLUE}">Open SWA Fare Watcher</a></td></tr>
</table></td></tr></table></body></html>"""


def _seat_report(report):
    """Per flight and passenger: their seat and the best free seats, with scores (higher = better
    for your preferences)."""
    e = html.escape
    out = ""
    for r in report or []:
        cur = r.get("current")
        head = " · ".join(x for x in (r.get("flight"), r.get("passenger")) if x)
        rows = ""
        if cur:
            rows += (f"<tr style='background:#FFF4D6'><td style='padding:4px 8px'><b>{e(cur['describe'])}</b> "
                     f"<span style='color:#636363'>(your seat)</span></td><td style='padding:4px 8px;text-align:right'>"
                     f"{cur['score']}</td></tr>")
        for o in r.get("options") or []:
            mark = " ✓ chosen" if o["seat"] == r.get("picked") else ""
            rows += (f"<tr><td style='padding:4px 8px'>{e(o['describe'])}<b style='color:#1a7f37'>{mark}</b></td>"
                     f"<td style='padding:4px 8px;text-align:right'>{o['score']}</td></tr>")
        if not r.get("options"):
            rows += "<tr><td colspan='2' style='padding:4px 8px;color:#636363'>No free seats you can get were open.</td></tr>"
        out += (f"<p style='{FONT};font-size:13px;color:{NAVY};margin:14px 0 4px'><b>{e(head or 'Seats')}</b></p>"
                f"<table role='presentation' cellpadding='0' cellspacing='0' style='{FONT};font-size:13px;color:{NAVY};"
                f"border:1px solid {SILVER};border-collapse:collapse;width:100%'>"
                f"<tr style='background:#F2F2F2'><th style='padding:4px 8px;text-align:left'>Free seats you can get</th>"
                f"<th style='padding:4px 8px;text-align:right'>Score</th></tr>{rows}</table>")
    return out


def render_checkin(title, positions, seats, note, ok, url):
    """Email after an automatic check-in attempt."""
    e = html.escape
    body = ""
    if positions:
        body = (f"<p style='{FONT};font-size:15px;color:{NAVY}'>Boarding: <b>{e(', '.join(positions))}</b>"
                + (f"<br>Seat{'s' if len(seats) > 1 else ''}: <b>{e(', '.join(seats))}</b>" if seats else "") + "</p>")
    colour = "#1a7f37" if ok else RED
    return f"""<!doctype html><html><body style="margin:0;background:#F2F2F2">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:16px 8px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;background:#fff;border:1px solid {SILVER};border-radius:6px">
  <tr><td style="background:{NAVY};padding:14px 16px;border-radius:6px 6px 0 0">
    <span style="{FONT};font-size:18px;font-weight:bold;color:#fff">{e(title)}</span></td></tr>
  <tr><td style="padding:14px 16px">{body}
    <p style="{FONT};font-size:14px;color:{colour}">{e(note)}</p>
    <a href="{e(url)}" style="{FONT};background:{YELLOW};color:{NAVY};font-weight:bold;font-size:14px;
       padding:8px 16px;border-radius:4px;text-decoration:none;display:inline-block">{'Get my boarding pass' if ok else 'Check in now'}</a></td></tr>
</table></td></tr></table></body></html>"""


def render_seat_result(title, changes, note, ok, manage_url, report=None):
    """Email after a seat-selection attempt, with the scored free seats it chose from."""
    e = html.escape
    if changes:
        rows = "".join(f"<li><b>{e(new)}</b>" + (f" (was {e(cur)})" if cur else "") + "</li>" for cur, new in changes)
        body = f"<p style='{FONT};font-size:15px;color:{NAVY}'>New seat{'s' if len(changes) > 1 else ''}:</p><ul>{rows}</ul>"
    else:
        body = ""
    colour = "#1a7f37" if ok else RED
    return f"""<!doctype html><html><body style="margin:0;background:#F2F2F2">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:16px 8px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;background:#fff;border:1px solid {SILVER};border-radius:6px">
  <tr><td style="background:{NAVY};padding:14px 16px;border-radius:6px 6px 0 0">
    <span style="{FONT};font-size:18px;font-weight:bold;color:#fff">{e(title)}</span></td></tr>
  <tr><td style="padding:14px 16px">{body}
    <p style="{FONT};font-size:14px;color:{colour}">{e(note)}</p>{_seat_report(report)}
    <p style="{FONT};font-size:12px;color:#636363">Score = how well a seat matches your ranked preferences (100 = all of them); only seats priced $0 for you are listed.</p>
    <a href="{e(manage_url)}" style="{FONT};background:{YELLOW};color:{NAVY};font-weight:bold;font-size:14px;
       padding:8px 16px;border-radius:4px;text-decoration:none;display:inline-block">Open my reservation</a></td></tr>
</table></td></tr></table></body></html>"""
