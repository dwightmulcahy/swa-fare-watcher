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


def _card(title, url, sub, legs, cash, points, cpp, use):
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
  <tr><td colspan="2" style="padding:0 14px 12px">
    <span style="{FONT};font-size:13px;color:{NAVY};background:#FFF4D6;border-left:4px solid {YELLOW};
                 padding:6px 10px;display:inline-block">Pay with: <b>{e(use)}</b></span>
    <a href="{e(url)}" style="{FONT};float:right;background:{YELLOW};color:{NAVY};font-weight:bold;font-size:14px;
       padding:8px 16px;border-radius:4px;text-decoration:none">Book on Southwest</a></td></tr>
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
                           f'{t["cpp"]:.2f}' if t["cpp"] else "", h["use"])
            continue
        r = h["row"]
        pts = f'{int(r["points"]):,} pts + ${r["points_taxes"] or 0:.2f}' if r["points"] else ""
        cards += _card(h["watch"], h["url"], "One way", _leg_line("Flight", h["date"], r, None),
                       f'${r["cash"]:,.0f}' if r["cash"] else "", pts,
                       f'{r["cpp"]:.2f}' if r["cpp"] else "", h["use"])
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
