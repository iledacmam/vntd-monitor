from datetime import datetime, timedelta, timezone
import email
from email.header import decode_header
import html
import imaplib
import json
import os
import re
import socket
import urllib.parse
import urllib.request

socket.setdefaulttimeout(20)

# Lista fraz w temacie maila, które bot ignoruje (nie wysyła na Telegram)
IGNORED_SUBJECTS = [
    "pobierz nasz regulamin",
    "nasz regulamin zawsze pod ręką",
    "dodał(a) nowy przedmiot",
    "dodała nowy przedmiot",
    "dodał nowy przedmiot",
    "dodana do ulubionych",
    "dodany do ulubionych",
    "polubił(a) twój przedmiot",
    "dostępna z rabatem",
    "obniżył(a) cenę",
    "numer telefonu, który dodałeś",
    "numer telefonu, który dodałaś",
    "nowy komentarz w twoim profilu",
    "zaproś znajomych",
    "poleć vinted",
]

IMAP_SERVERS = {
    "wp.pl": "imap.wp.pl",
    "o2.pl": "poczta.o2.pl",
    "tlen.pl": "poczta.o2.pl",
    "go2.pl": "poczta.o2.pl",
    "interia.pl": "poczta.interia.pl",
    "interia.eu": "poczta.interia.pl",
    "poczta.fm": "poczta.interia.pl",
    "vip.interia.pl": "poczta.interia.pl",
    "1gb.pl": "poczta.interia.pl",
    "2gb.pl": "poczta.interia.pl",
}

MONTHS_EN = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def get_imap_since_date(days_back=2):
    dt = datetime.now(timezone.utc) - timedelta(days=days_back)
    return f"{dt.day:02d}-{MONTHS_EN[dt.month - 1]}-{dt.year}"


def decode_mime_str(value):
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            for enc in (charset, "utf-8", "iso-8859-2", "windows-1250", "latin-1"):
                if not enc:
                    continue
                try:
                    decoded.append(part.decode(enc, errors="replace"))
                    break
                except LookupError:
                    continue
        else:
            decoded.append(str(part))
    return "".join(decoded).strip()


def clean_html_to_text(raw_html):
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw_html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def extract_body(msg):
    plain_parts = []
    html_parts = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition") or "")
            if "attachment" in disposition.lower():
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except LookupError:
                text = payload.decode("utf-8", errors="replace")

            if content_type == "text/plain":
                plain_parts.append(text)
            elif content_type == "text/html":
                html_parts.append(text)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except LookupError:
                text = payload.decode("utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                html_parts.append(text)
            else:
                plain_parts.append(text)

    if plain_parts:
        raw = "\n".join(plain_parts)
        cleaned = re.sub(r"\s+", " ", raw).strip()
    elif html_parts:
        cleaned = clean_html_to_text("\n".join(html_parts))
    else:
        return ""

    # Wycięcie stałej stopki prawnej Vinted na końcu wiadomości
    cleaned = re.split(r"Ten e-mail został wysłany przez Vinted", cleaned, flags=re.IGNORECASE)[0].strip()
    return cleaned


def get_folders_to_scan(mail):
    folders = ["INBOX"]
    status, folder_list = mail.list()
    if status == "OK" and folder_list:
        for item in folder_list:
            if not item:
                continue
            line = item.decode("utf-8", errors="replace")
            match = re.search(r'"([^"]+)"\s*$', line)
            folder_name = match.group(1) if match else line.split()[-1].strip('"')
            lower_name = folder_name.lower()
            if ("spam" in lower_name or "junk" in lower_name) and folder_name not in folders:
                folders.append(folder_name)
    return folders


def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def is_vinted_message(sender, subject, body):
    combined = f"{sender}\n{subject}\n{body[:1500]}".lower()
    return "vinted" in combined


def is_ignored_noise(subject):
    lower_subject = subject.lower()
    return any(phrase in lower_subject for phrase in IGNORED_SUBJECTS)


def classify_alert_header(subject, body):
    s = subject.lower()
    b = body.lower()

    if "twój przedmiot został sprzedany" in s or ("kupił(a)" in b and "chce kupić" not in b):
        return "<b>SPRZEDANE ! 💶</b>"

    if "nowa oferta" in s or "chce kupić od ciebie" in b or "w niższej cenie" in b:
        return "<b>PROPOZYCJA CENY 🏷️</b>"

    return "🛍️ <b>Nowa wiadomość Vinted!</b>"


def scan_mailbox(host, login_email, password, redirected_map=None, single_profile=None):
    found_messages = []
    since_date = get_imap_since_date(days_back=2)
    mail = imaplib.IMAP4_SSL(host, 993)
    try:
        mail.login(login_email, password)
        for folder in get_folders_to_scan(mail):
            status, _ = mail.select(f'"{folder}"')
            if status != "OK":
                continue

            status, data = mail.search(None, "UNSEEN", "SINCE", since_date)
            if status != "OK" or not data or not data[0]:
                continue

            for num in data[0].split():
                res, msg_data = mail.fetch(num, "(BODY.PEEK[])")
                if res != "OK" or not msg_data or not msg_data[0]:
                    continue

                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                sender = decode_mime_str(msg.get("From"))
                subject = decode_mime_str(msg.get("Subject"))
                body = extract_body(msg)

                if not is_vinted_message(sender, subject, body):
                    continue

                mail.store(num, "+FLAGS", "\\Seen")

                if is_ignored_noise(subject):
                    continue

                if single_profile:
                    profile_label = f"{single_profile} ({login_email})"
                else:
                    routing_headers = " ".join([
                        decode_mime_str(msg.get("To")),
                        decode_mime_str(msg.get("Delivered-To")),
                        decode_mime_str(msg.get("X-Original-To")),
                        decode_mime_str(msg.get("X-Forwarded-To")),
                        body[:1000],
                    ]).lower()

                    matched_name = None
                    matched_addr = None
                    for addr, name in (redirected_map or {}).items():
                        if addr.strip().lower() in routing_headers:
                            matched_name = name
                            matched_addr = addr.strip()
                            break

                    if matched_name:
                        profile_label = f"{matched_name} ({matched_addr})"
                    else:
                        profile_label = "Nieznany profil (inbox.lv)"

                snippet = body[:350] + ("..." if len(body) > 350 else "")
                found_messages.append({
                    "header": classify_alert_header(subject, body),
                    "profile": profile_label,
                    "subject": subject or "(Brak tematu)",
                    "snippet": snippet or "(Brak treści tekstowej)",
                })
    finally:
        try:
            mail.logout()
        except Exception:
            pass

    return found_messages


def main():
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("CHAT_ID") or "").strip()
    accounts_raw = os.environ.get("ACCOUNTS_JSON", "").strip()
    event_name = os.environ.get("GITHUB_EVENT_NAME", "").strip()

    if not token or not chat_id or not accounts_raw:
        raise RuntimeError("Brakuje jednego z sekretów: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID lub ACCOUNTS_JSON.")

    config = json.loads(accounts_raw)
    collector_email = config.get("collector_email", "").strip()
    collector_pass = config.get("collector_haslo", "")
    redirected_accounts = config.get("konta", {})
    direct_imap_accounts = config.get("direct_imap", {})

    all_new_messages = []
    login_errors = []
    active_redirected = 0
    active_direct = 0

    if collector_email and collector_pass and redirected_accounts:
        try:
            msgs = scan_mailbox(
                host="mail.inbox.lv",
                login_email=collector_email,
                password=collector_pass,
                redirected_map=redirected_accounts,
            )
            all_new_messages.extend(msgs)
            active_redirected = len(redirected_accounts)
        except Exception as exc:
            login_errors.append(f"• Agregator ({collector_email}): {exc}")

    for addr, details in direct_imap_accounts.items():
        clean_addr = addr.strip()
        passwd = details.get("haslo", "")
        label = details.get("nazwa", clean_addr)
        domain = clean_addr.split("@")[-1].lower() if "@" in clean_addr else ""
        imap_host = details.get("imap_server") or IMAP_SERVERS.get(domain)

        if not imap_host:
            login_errors.append(f"• {label} ({clean_addr}): nieznany serwer IMAP dla domeny {domain}")
            continue

        try:
            msgs = scan_mailbox(
                host=imap_host,
                login_email=clean_addr,
                password=passwd,
                single_profile=label,
            )
            all_new_messages.extend(msgs)
            active_direct += 1
        except Exception as exc:
            login_errors.append(f"• {label} ({clean_addr}): {exc}")

    for item in all_new_messages:
        alert = (
            f"{item['header']}\n"
            f"👤 <b>Profil:</b> {html.escape(item['profile'])}\n"
            f"📌 <b>Temat:</b> {html.escape(item['subject'])}\n"
            f"📝 <b>Treść:</b> {html.escape(item['snippet'])}"
        )
        send_telegram(token, chat_id, alert)

    if event_name == "workflow_dispatch" or all_new_messages or login_errors:
        total_active = active_redirected + active_direct
        summary_lines = [
            "✅ <b>Skanowanie zakończone!</b>",
            f"📊 Aktywne konta: <b>{total_active}</b> (przekierowane: {active_redirected}, direct IMAP: {active_direct})",
            f"📬 Nowe powiadomienia Vinted: <b>{len(all_new_messages)}</b>",
        ]
        if login_errors:
            summary_lines.append("\n⚠️ <b>Błędy logowania:</b>")
            summary_lines.extend(html.escape(err) for err in login_errors)

        send_telegram(token, chat_id, "\n".join(summary_lines))


if __name__ == "__main__":
    main()
