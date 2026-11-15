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
from zoneinfo import ZoneInfo

socket.setdefaulttimeout(20)
WARSAW_TZ = ZoneInfo("Europe/Warsaw")
TASKS_FILE = "tasks.json"

# 1. Marki i słowa kluczowe przedmiotów rozgrzewkowych (ignorowane w alertach i zadaniach)
IGNORED_ITEMS = [
    "zara",
    "massimo dutti",
    "h&m",
    "reserved",
    "bershka",
    "stradivarius",
    "pull&bear",
    "mango",
    "cos",
    "arket",
]

# 2. Zbędne powiadomienia systemowe i marketingowe Vinted
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

TYPE_ORDER = {"pakowanie": 0, "oferta": 1, "wiadomosc": 2}

FOLDER_KEYWORDS = (
    "spam",
    "junk",
    "inne",
    "oferty",
    "powiadomienia",
    "społeczności",
    "spolecznosci",
    "spo&aui-eczno",
    "newsletter",
    "other",
    "promotions",
    "social",
    "updates",
)


def now_warsaw_str():
    return datetime.now(WARSAW_TZ).strftime("%d.%m %H:%M")


def get_imap_since_date(days_back=2):
    dt = datetime.now(timezone.utc) - timedelta(days=days_back)
    return f"{dt.day:02d}-{MONTHS_EN[dt.month - 1]}-{dt.year}"


def load_tasks():
    if not os.path.exists(TASKS_FILE):
        return []
    try:
        with open(TASKS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("tasks", []) if isinstance(data, dict) else []
    except Exception:
        return []


def save_tasks(tasks):
    sorted_tasks = sorted(tasks, key=lambda t: TYPE_ORDER.get(t.get("type", ""), 9))
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump({"tasks": sorted_tasks}, f, ensure_ascii=False, indent=2)
    return sorted_tasks


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

    cleaned = re.split(r"Ten e-mail został wysłany przez Vinted", cleaned, flags=re.IGNORECASE)[0].strip()
    return cleaned


def get_folders_to_scan(mail):
    folders = ["INBOX"]
    status, folder_list = mail.list()
    if status == "OK" and folder_list:
        for item in folder_list:
            if not item:
                continue
            line = item.decode("utf-8", errors="replace").strip()
            m = re.match(r'^\(([^)]*)\)\s+(?:"[^"]*"|NIL|\S+)\s+(.+)$', line)
            if m:
                flags = m.group(1).lower()
                if "\\noselect" in flags:
                    continue
                folder_name = m.group(2).strip()
                if folder_name.startswith('"') and folder_name.endswith('"'):
                    folder_name = folder_name[1:-1]
            else:
                match = re.search(r'"([^"]+)"\s*$', line)
                folder_name = match.group(1) if match else line.split()[-1].strip('"')

            lower_name = folder_name.lower()
            if any(kw in lower_name for kw in FOLDER_KEYWORDS) and folder_name not in folders:
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


def is_warmup_item(item_title, fallback_subject=""):
    target = (item_title or fallback_subject).lower()
    return any(brand in target for brand in IGNORED_ITEMS)


def extract_sale_details(body):
    m = re.search(
        r"([a-zA-Z0-9._-]+)\s+kupił\(a\)\s+(.+?)\s+(\d[\d\s,.]*\s*(?:zł|PLN|EUR|€))",
        body,
        flags=re.IGNORECASE,
    )
    if m:
        return m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
    return None, "", ""


def extract_offer_details(subject, body):
    buyer_m = re.search(r"([a-zA-Z0-9._-]+)\s+chce kupić od Ciebie", body)
    buyer = buyer_m.group(1).strip() if buyer_m else None

    item = ""
    sub_m = re.search(r"ogłoszenia:\s*(.+)$", subject, flags=re.IGNORECASE)
    if sub_m:
        item = sub_m.group(1).strip()
    else:
        body_m = re.search(r"chce kupić od Ciebie\s+(.+?)\s+w niższej cenie", body, flags=re.IGNORECASE)
        if body_m:
            item = body_m.group(1).strip()

    price_m = re.search(
        r"Nowa oferta:\s*(\d[\d\s,.]*\s*(?:zł|PLN|EUR|€)(?:\s*zamiast\s*\d[\d\s,.]*\s*(?:zł|PLN|EUR|€))?)",
        body,
        flags=re.IGNORECASE,
    )
    price = price_m.group(1).strip() if price_m else ""
    return buyer, item, price


def extract_message_details(subject, body):
    buyer_m = re.search(r"([a-zA-Z0-9._-]+)\s+przesyła Ci wiadomość", body)
    buyer = buyer_m.group(1).strip() if buyer_m else None

    item = ""
    sub_m = re.search(r"ogłoszenia:\s*(.+)$", subject, flags=re.IGNORECASE)
    if sub_m:
        item = sub_m.group(1).strip()
    else:
        body_m = re.search(r"na temat ogłoszenia:\s*(.+?)\.", body, flags=re.IGNORECASE)
        if body_m:
            item = body_m.group(1).strip()
    return buyer, item


def extract_label_item(subject, body):
    body_m = re.search(
        r"Przedmiot:\s*(.+?)\s*(?:Rozmiar przesyłki|Numer śledzenia|Wysłać do|Numer transakcji|$)",
        body,
        flags=re.IGNORECASE,
    )
    if body_m:
        return body_m.group(1).strip()
    sub_m = re.search(r"^(.+?)\s+etykieta wysyłkowa", subject, flags=re.IGNORECASE)
    if sub_m:
        return sub_m.group(1).strip()
    return ""


def norm_text(val):
    return re.sub(r"\s+", " ", (val or "").strip().lower())


def upsert_task(tasks, task_type, profile, buyer, item, price=""):
    buyer_n = norm_text(buyer)
    item_n = norm_text(item)

    if task_type == "pakowanie":
        tasks = [
            t for t in tasks
            if not (
                t.get("profile") == profile
                and t.get("type") in ("oferta", "wiadomosc")
                and (
                    (buyer_n and norm_text(t.get("buyer")) == buyer_n)
                    or (item_n and norm_text(t.get("item")) == item_n)
                )
            )
        ]

    for t in tasks:
        if (
            t.get("type") == task_type
            and t.get("profile") == profile
            and norm_text(t.get("buyer")) == buyer_n
            and norm_text(t.get("item")) == item_n
        ):
            if price:
                t["price"] = price
            t["created_at"] = now_warsaw_str()
            return tasks

    tasks.append({
        "type": task_type,
        "profile": profile,
        "buyer": buyer or "nieznany",
        "item": item or "(brak nazwy)",
        "price": price,
        "created_at": now_warsaw_str(),
    })
    return tasks


def complete_packing_task(tasks, profile, item):
    item_n = norm_text(item)
    removed = False
    new_tasks = []

    for t in tasks:
        if not removed and t.get("type") == "pakowanie" and t.get("profile") == profile:
            t_item = norm_text(t.get("item"))
            if item_n and (item_n in t_item or t_item in item_n):
                removed = True
                continue
        new_tasks.append(t)

    if not removed:
        new_tasks = []
        for t in tasks:
            if not removed and t.get("type") == "pakowanie" and t.get("profile") == profile:
                removed = True
                continue
            new_tasks.append(t)

    return new_tasks


def format_tasks_message(tasks):
    if not tasks:
        return "✨ <b>Lista zadań jest pusta!</b>\nNie masz żadnych zaległych wiadomości, ofert ani paczek do wygenerowania."

    sorted_tasks = sorted(tasks, key=lambda t: TYPE_ORDER.get(t.get("type", ""), 9))
    sections = {
        "pakowanie": ("📦 <b>DO SPAKOWANIA / ETYKIETY:</b>", []),
        "oferta": ("🏷️ <b>OFERTY DO POTWIERDZENIA:</b>", []),
        "wiadomosc": ("💬 <b>WIADOMOŚCI DO ODPISANIA:</b>", []),
    }

    for idx, t in enumerate(sorted_tasks, start=1):
        t_type = t.get("type", "wiadomosc")
        profile = html.escape(t.get("profile", ""))
        buyer = html.escape(t.get("buyer", ""))
        item = html.escape(t.get("item", ""))
        price = html.escape(t.get("price", ""))
        ts = html.escape(t.get("created_at", ""))

        price_part = f" — <b>{price}</b>" if price else ""
        line = (
            f"<b>{idx}.</b> [{profile}] <b>{item}</b>{price_part}\n"
            f"    └ 👤 <code>{buyer}</code> <i>({ts})</i> ➔ /done{idx}"
        )
        sections.setdefault(t_type, ("📌 <b>INNE:</b>", []))[1].append(line)

    out = ["📋 <b>TWOJA LISTA ZADAŃ VINTED:</b>\n"]
    for key in ("pakowanie", "oferta", "wiadomosc"):
        title, lines = sections[key]
        if lines:
            out.append(title)
            out.extend(lines)
            out.append("")

    out.append("💡 <i>Odhacz: <code>/done1</code>, <code>/done1 2</code> lub <code>/wyczysc</code></i>")
    return "\n".join(out).strip()


def handle_client_command(command, token, chat_id):
    cmd = command.strip()
    lower = cmd.lower()
    tasks = save_tasks(load_tasks())

    if lower.startswith("/wyczysc") or lower in ("/done all", "/doneall"):
        save_tasks([])
        send_telegram(token, chat_id, "🧹 <b>Wyczyszczono wszystkie zadania!</b>")
        return

    if lower.startswith("/done"):
        nums = [int(n) for n in re.findall(r"\d+", lower)]
        if not nums:
            send_telegram(token, chat_id, "⚠️ Podaj numer zadania, np. <code>/done1</code> lub <code>/done1 2</code>.")
            return

        valid_indices = {n - 1 for n in nums if 1 <= n <= len(tasks)}
        if not valid_indices:
            send_telegram(token, chat_id, f"⚠️ Nie ma zadania o tym numerze (obecnie masz {len(tasks)} zadań).")
            return

        remaining = [t for i, t in enumerate(tasks) if i not in valid_indices]
        remaining = save_tasks(remaining)
        done_str = ", ".join(f"#{n}" for n in sorted(nums) if 1 <= n <= len(tasks))
        header = f"✅ <b>Odhaczono: {done_str}</b>\n\n"
        send_telegram(token, chat_id, header + format_tasks_message(remaining))
        return

    send_telegram(token, chat_id, format_tasks_message(tasks))


def scan_mailbox(host, login_email, password, tasks, redirected_map=None, single_profile=None):
    found_messages = []
    since_date = get_imap_since_date(days_back=2)
    mail = imaplib.IMAP4_SSL(host, 993)
    try:
        mail.login(login_email, password)
        for folder in get_folders_to_scan(mail):
            safe_folder = folder.replace('"', '\\"')
            status, _ = mail.select(f'"{safe_folder}"')
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

                lower_subject = subject.lower()
                lower_body = body.lower()

                if any(phrase in lower_subject for phrase in IGNORED_SUBJECTS):
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

                    profile_label = f"{matched_name} ({matched_addr})" if matched_name else "Nieznany profil (inbox.lv)"

                if "etykieta wysyłkowa" in lower_subject or "twoja etykieta wysyłkowa jest załączona" in lower_body:
                    item_title = extract_label_item(subject, body)
                    if not is_warmup_item(item_title, subject):
                        tasks = complete_packing_task(tasks, profile_label, item_title)
                    continue

                if "twój przedmiot został sprzedany" in lower_subject or ("kupił(a)" in lower_body and "chce kupić" not in lower_body):
                    buyer, item_title, price = extract_sale_details(body)
                    if is_warmup_item(item_title, subject):
                        continue
                    tasks = upsert_task(tasks, "pakowanie", profile_label, buyer, item_title, price)
                    header = "<b>SPRZEDANE ! 💶</b>"

                elif "nowa oferta" in lower_subject or "chce kupić od ciebie" in lower_body or "w niższej cenie" in lower_body:
                    buyer, item_title, price = extract_offer_details(subject, body)
                    if is_warmup_item(item_title, subject):
                        continue
                    tasks = upsert_task(tasks, "oferta", profile_label, buyer, item_title, price)
                    header = "<b>PROPOZYCJA CENY 🏷️</b>"

                else:
                    buyer, item_title = extract_message_details(subject, body)
                    if is_warmup_item(item_title, subject):
                        continue
                    if "nowa wiadomość" in lower_subject or "przesyła ci wiadomość" in lower_body:
                        tasks = upsert_task(tasks, "wiadomosc", profile_label, buyer, item_title)
                    header = "🛍️ <b>Nowa wiadomość Vinted!</b>"

                snippet = body[:350] + ("..." if len(body) > 350 else "")
                found_messages.append({
                    "header": header,
                    "profile": profile_label,
                    "buyer": buyer,
                    "subject": subject or "(Brak tematu)",
                    "snippet": snippet or "(Brak treści tekstowej)",
                })
    finally:
        try:
            mail.logout()
        except Exception:
            pass

    return found_messages, tasks


def main():
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("CHAT_ID") or "").strip()
    accounts_raw = os.environ.get("ACCOUNTS_JSON", "").strip()
    event_name = os.environ.get("GITHUB_EVENT_NAME", "").strip()
    client_command = os.environ.get("CLIENT_COMMAND", "").strip()

    if not token or not chat_id:
        raise RuntimeError("Brakuje sekretu TELEGRAM_TOKEN lub TELEGRAM_CHAT_ID.")

    if client_command:
        handle_client_command(client_command, token, chat_id)
        return

    if not accounts_raw:
        raise RuntimeError("Brakuje sekretu ACCOUNTS_JSON.")

    config = json.loads(accounts_raw)
    collector_email = config.get("collector_email", "").strip()
    collector_pass = config.get("collector_haslo", "")
    redirected_accounts = config.get("konta", {})
    direct_imap_accounts = config.get("direct_imap", {})

    tasks = load_tasks()
    all_new_messages = []
    login_errors = []
    active_redirected = 0
    active_direct = 0

    if collector_email and collector_pass and redirected_accounts:
        try:
            msgs, tasks = scan_mailbox(
                host="mail.inbox.lv",
                login_email=collector_email,
                password=collector_pass,
                tasks=tasks,
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
            msgs, tasks = scan_mailbox(
                host=imap_host,
                login_email=clean_addr,
                password=passwd,
                tasks=tasks,
                single_profile=label,
            )
            all_new_messages.extend(msgs)
            active_direct += 1
        except Exception as exc:
            login_errors.append(f"• {label} ({clean_addr}): {exc}")

    save_tasks(tasks)

    for item in all_new_messages:
        buyer_line = f"🛒 <b>Kupujący:</b> <code>{html.escape(item['buyer'])}</code>\n" if item.get("buyer") else ""
        alert = (
            f"{item['header']}\n"
            f"👤 <b>Profil:</b> {html.escape(item['profile'])}\n"
            f"{buyer_line}"
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
