import imaplib
import email
from email.header import decode_header
import urllib.request
import urllib.parse
import json
import os
import datetime

COLLECTOR_SERVERS = {
    "inbox.lv": "mail.inbox.lv",
    "seznam.cz": "imap.seznam.cz",
    "email.cz": "imap.seznam.cz",
    "post.cz": "imap.seznam.cz",
    "gmx.com": "imap.gmx.com",
    "gmail.com": "imap.gmail.com"
}

KEYWORDS = [
    "kupił", "kupiono", "sprzedano", "wiadomość", "oferta", "ofertę",
    "etykiet", "przesyłk", "zamówien", "nadaj", "paczkomat", "inpost"
]

def decode_str(header_val):
    if not header_val:
        return ""
    decoded_parts = decode_header(header_val)
    result = []
    for content, encoding in decoded_parts:
        if isinstance(content, bytes):
            result.append(content.decode(encoding or "utf-8", errors="ignore"))
        else:
            result.append(str(content))
    return "".join(result)

def extract_raw_text(msg):
    parts_text = []
    for k, v in msg.items():
        parts_text.append(f"{k}: {decode_str(v)}")
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    parts_text.append(payload.decode(charset, errors="ignore"))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = part.get_content_charset() or "utf-8"
            parts_text.append(payload.decode(charset, errors="ignore"))
    return "\n".join(parts_text).lower()

def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    urllib.request.urlopen(req, timeout=15)

def main():
    tg_token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    accounts_raw = os.environ.get("ACCOUNTS_JSON", "").strip()
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")

    if not (tg_token and tg_chat_id and accounts_raw):
        print("Brak sekretów w GitHub Secrets!")
        return

    try:
        config = json.loads(accounts_raw)
        if isinstance(config, str):
            config = json.loads(config)
    except Exception as e:
        err_msg = f"⚠️ Błąd składni w sekrecie ACCOUNTS_JSON (sprawdź przecinki i cudzysłowy):\n{e}"
        print(err_msg)
        if event_name == "workflow_dispatch":
            send_telegram(tg_token, tg_chat_id, err_msg)
        return

    collector_email = config["collector_email"].strip()
    collector_pass = config["collector_haslo"].strip()
    domain = collector_email.split("@")[-1].lower()
    imap_host = COLLECTOR_SERVERS.get(domain, f"imap.{domain}")
    konta_map = {k.strip().lower(): v for k, v in config.get("konta", {}).items()}

    try:
        mail = imaplib.IMAP4_SSL(imap_host, 993, timeout=20)
        mail.login(collector_email, collector_pass)

        since_date = (datetime.date.today() - datetime.timedelta(days=2)).strftime("%d-%b-%Y")
        found_count = 0

        for folder in ["INBOX", "Spam", "INBOX.Spam", "Junk"]:
            status_sel, _ = mail.select(folder)
            if status_sel != "OK":
                continue

            status, response = mail.search(None, f'(UNSEEN SINCE "{since_date}")')
            if status != "OK" or not response[0]:
                continue

            for num in response[0].split():
                res, msg_data = mail.fetch(num, "(BODY.PEEK[])")
                if res != "OK" or not msg_data or not msg_data[0]:
                    continue

                raw_bytes = msg_data[0][1]
                msg = email.message_from_bytes(raw_bytes)
                subject = decode_str(msg.get("Subject", "")).strip()
                sender = decode_str(msg.get("From", "")).lower()
                full_text_low = extract_raw_text(msg)

                is_vinted = (
                    "vinted" in sender
                    or "vinted" in full_text_low
                    or any(k in subject.lower() for k in KEYWORDS)
                )

                if is_vinted:
                    matched_label = "Nieznany profil (sprawdź adres w mailu)"
                    matched_mail = "brak dopasowania"
                    for acc_mail, acc_label in konta_map.items():
                        if acc_mail in full_text_low:
                            matched_label = acc_label
                            matched_mail = acc_mail
                            break

                    sub_low = subject.lower()
                    if any(w in sub_low for w in ["kupił", "kupiono", "sprzedano", "etykiet", "nadaj"]):
                        icon = "💰 SPRZEDANE!"
                    elif any(w in sub_low for w in ["ofert", "proponuje"]):
                        icon = "🏷️ NOWA OFERTA!"
                    else:
                        icon = "💬 WIADOMOŚĆ / INFO"

                    alert = f"{icon}\n👤 Profil: {matched_label}\n📧 Mail: {matched_mail}\n📌 Temat: {subject}"
                    send_telegram(tg_token, tg_chat_id, alert)
                    mail.store(num, "+FLAGS", "\\Seen")
                    found_count += 1

        mail.logout()

        if event_name == "workflow_dispatch":
            send_telegram(
                tg_token,
                tg_chat_id,
                f"✅ Połączono ze skrzynką zbiorczą ({collector_email})!\nAktywna lista kont: {len(konta_map)}.\nNowe powiadomienia Vinted: {found_count}."
            )
    except Exception as e:
        print("Błąd logowania do skrzynki zbiorczej:", e)
        if event_name == "workflow_dispatch":
            send_telegram(tg_token, tg_chat_id, f"⚠️ Błąd logowania do skrzynki zbiorczej:\n{e}")

if __name__ == "__main__":
    main()
