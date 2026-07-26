import os
import json
import time
import hashlib
import feedparser
import requests
from bs4 import BeautifulSoup

# ---------- Настройки ----------
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GIGACHAT_AUTH_KEY = os.environ.get("GIGACHAT_AUTH_KEY")

SEEN_FILE = "seen.json"
MAX_SEEN_ITEMS = 500  # чтобы файл не рос бесконечно

# Список RSS-лент — впишите свои
RSS_FEEDS = [
    "https://www.example.com/rss",
]

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


# ---------- Хранение истории ----------
def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()
    with open(SEEN_FILE, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
            return set(data)
        except json.JSONDecodeError:
            return set()


def save_seen(seen_set):
    # обрезаем, чтобы файл не рос бесконечно (оставляем последние N)
    trimmed = list(seen_set)[-MAX_SEEN_ITEMS:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(trimmed, f, ensure_ascii=False, indent=2)


def make_id(entry):
    # используем guid/link, а если их нет — хеш заголовка
    raw = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------- Получение новостей ----------
def fetch_new_entries(seen_ids):
    new_entries = []
    for feed_url in RSS_FEEDS:
        parsed = feedparser.parse(feed_url)
        for entry in parsed.entries:
            entry_id = make_id(entry)
            if entry_id not in seen_ids:
                new_entries.append((entry_id, entry))
    return new_entries


def clean_html(raw_html):
    soup = BeautifulSoup(raw_html or "", "html.parser")
    return soup.get_text(separator=" ", strip=True)


# ---------- Отправка в Telegram ----------
def send_to_telegram(text):
    resp = requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    if not resp.ok:
        print(f"[WARN] sendMessage failed: {resp.json()}")
    return resp.ok


# ---------- Основная логика ----------
def main():
    print(f"[START] {__import__('datetime').datetime.utcnow().isoformat()}")

    seen_ids = load_seen()
    new_entries = fetch_new_entries(seen_ids)

    if not new_entries:
        print("[INFO] No new news.")
        return

    for entry_id, entry in new_entries:
        title = entry.get("title", "Без заголовка")
        summary = clean_html(entry.get("summary", ""))
        link = entry.get("link", "")

        text = f"<b>{title}</b>\n\n{summary[:500]}\n\n{link}"

        ok = send_to_telegram(text)
        if ok:
            seen_ids.add(entry_id)
            print(f"[INFO] Posted: {title}")
        else:
            print(f"[WARN] Failed to post: {title}")

        time.sleep(1)  # чтобы не упереться в rate limit Telegram

    save_seen(seen_ids)
    print(f"[DONE] Posted {len(new_entries)} new items.")


if __name__ == "__main__":
    main()
