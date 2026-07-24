#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import feedparser
import requests
import time
from datetime import datetime
from anthropic import Anthropic, APIError, RateLimitError

# --- Настройки ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
FEEDS_FILE = "feeds.txt"           # список RSS-ссылок (по одной на строку)
POSTED_FILE = "posted.json"        # хранит ID уже отправленных новостей
MAX_ITEMS = 5                      # максимум новостей за один запуск

# --- Инициализация Claude ---
claude = None
if ANTHROPIC_API_KEY:
    try:
        claude = Anthropic(api_key=ANTHROPIC_API_KEY)
        print("[INFO] Claude client initialized.")
    except Exception as e:
        print(f"[WARN] Could not init Claude: {e}")
else:
    print("[WARN] ANTHROPIC_API_KEY not set, AI rewriting disabled.")

# --- Загрузка уже отправленных ID ---
def load_posted():
    if os.path.exists(POSTED_FILE):
        with open(POSTED_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_posted(posted_set):
    with open(POSTED_FILE, "w") as f:
        json.dump(list(posted_set), f)

# --- Перефразирование через Claude ---
def rewrite_with_ai(title, summary):
    if not claude:
        return None
    try:
        prompt = f"""
Ты — редактор новостного дайджеста. Перепиши следующую новость в кратком, живом, фактологическом стиле (1–2 предложения). Сохрани все ключевые факты, но убери канцелярит и воду.

Заголовок: {title}
Краткое содержание: {summary if summary else "нет"}

Ответ (только переписанный текст, без кавычек и пояснений):"""
        
        response = claude.messages.create(
            model="claude-3-haiku-20240307",  # дешёвая и быстрая модель
            max_tokens=150,
            temperature=0.7,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text.strip()
    except RateLimitError:
        print("[ERROR] Claude rate limit reached.")
        return None
    except APIError as e:
        print(f"[ERROR] Claude API error: {e}")
        return None
    except Exception as e:
        print(f"[ERROR] Unexpected AI error: {e}")
        return None

# --- Основной сбор ---
def fetch_news():
    posted = load_posted()
    new_items = []
    feed_urls = []

    if not os.path.exists(FEEDS_FILE):
        print("[ERROR] feeds.txt not found!")
        return []

    with open(FEEDS_FILE, "r") as f:
        feed_urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    for url in feed_urls:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:MAX_ITEMS]:  # берём не больше лимита с каждого
                entry_id = entry.get("id") or entry.get("link")
                if not entry_id:
                    continue
                if entry_id in posted:
                    continue

                title = entry.get("title", "Без заголовка")
                summary = entry.get("summary", entry.get("description", ""))
                # Убираем HTML-теги из summary
                import re
                summary = re.sub(r"<[^>]+>", "", summary)
                link = entry.get("link", "")

                # Перефразируем
                rewritten = rewrite_with_ai(title, summary)
                if rewritten:
                    text = rewritten
                else:
                    # Fallback: используем заголовок + краткий отрывок
                    text = f"{title}. {summary[:200]}..." if summary else title

                # Добавляем ссылку, если есть
                if link:
                    text += f"\n\n🔗 {link}"

                new_items.append({
                    "id": entry_id,
                    "text": text,
                    "published": entry.get("published", datetime.now().isoformat())
                })

                if len(new_items) >= MAX_ITEMS:
                    break
        except Exception as e:
            print(f"[ERROR] Failed to parse {url}: {e}")

    return new_items

# --- Отправка в Telegram ---
def send_to_telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("[ERROR] Telegram credentials missing.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            return True
        else:
            print(f"[ERROR] Telegram send failed: {resp.text}")
            return False
    except Exception as e:
        print(f"[ERROR] Telegram request error: {e}")
        return False

# --- Формирование дайджеста ---
def format_digest(items):
    if not items:
        return None
    header = f"📅 <b>Факт дня – {datetime.now().strftime('%d.%m.%Y')}</b>\n\n"
    body = ""
    for i, item in enumerate(items, 1):
        body += f"{i}. {item['text']}\n\n"
    footer = "\n👍 Если понравился дайджест — ставьте реакции!"
    return header + body + footer

# --- Главная ---
def main():
    print(f"[START] {datetime.now().isoformat()}")
    news = fetch_news()
    if not news:
        print("No new news.")
        return

    digest = format_digest(news)
    if not digest:
        return

    # Отправляем одной большой порцией (Telegram лимит 4096 символов)
    if len(digest) > 4096:
        # разбиваем по частям, но в нашем случае обычно меньше
        for i in range(0, len(digest), 4096):
            send_to_telegram(digest[i:i+4096])
    else:
        send_to_telegram(digest)

    # Обновляем список отправленных
    posted = load_posted()
    for item in news:
        posted.add(item["id"])
    save_posted(posted)
    print(f"[DONE] Sent {len(news)} items.")

if __name__ == "__main__":
    main()
