#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import html
import uuid
import feedparser
import requests
import urllib3
from urllib.parse import urlparse
from datetime import datetime

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- Настройки ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GIGACHAT_AUTH_KEY = os.environ.get("GIGACHAT_AUTH_KEY")  # "Ключ авторизации" из личного кабинета Sber
GIGACHAT_SCOPE = os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
GIGACHAT_OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
GIGACHAT_CHAT_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
FEEDS_FILE = "feeds.txt"           # список RSS-ссылок (по одной на строку)
POSTED_FILE = "posted.json"        # хранит ID уже отправленных новостей
MAX_ITEMS = 5                      # максимум новостей за один запуск (глобально)
FETCH_RETRIES = 3                  # попыток скачать RSS при сбое
FETCH_TIMEOUT = 15                 # сек, таймаут на загрузку одного фида
AI_CALL_DELAY = 0.5                # сек, пауза между вызовами GigaChat (анти-рейтлимит)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NewsDigestBot/1.0; +https://t.me/)"
}

# Эмодзи по категориям — определяем по ключевым словам в заголовке/домене.
# Порядок важен: первое совпадение побеждает.
CATEGORY_RULES = [
    (["спорт", "футбол", "хоккей", "олимпиад", "чемпионат"], "⚽"),
    (["экономик", "рубл", "доллар", "нефт", "банк", "рынок", "инфляц"], "💰"),
    (["технолог", "ии ", "искусственн", "робот", "гаджет", "смартфон"], "💻"),
    (["наука", "учен", "исследован", "космос", "открыт"], "🔬"),
    (["погод", "климат", "ураган", "снег", "морож"], "🌦"),
    (["здоровь", "медицин", "врач", "болезн", "вирус"], "🩺"),
    (["политик", "президент", "правительств", "министр", "закон"], "🏛"),
    (["происшеств", "авари", "пожар", "взрыв", "трагед"], "🚨"),
    (["культур", "кино", "музык", "театр", "выставк"], "🎭"),
]
DEFAULT_EMOJI = "📰"


def pick_emoji(title):
    t = title.lower()
    for keywords, emoji in CATEGORY_RULES:
        if any(kw in t for kw in keywords):
            return emoji
    return DEFAULT_EMOJI


def source_name(link):
    try:
        domain = urlparse(link).netloc
        domain = domain.replace("www.", "")
        return domain
    except Exception:
        return ""


# --- Инициализация GigaChat (получение access_token по OAuth) ---
_gigachat_token = None
_gigachat_token_expires_at = 0  # unix-время истечения токена

def get_gigachat_token():
    """Получает (и кэширует) access_token GigaChat. Токен живёт ~30 минут."""
    global _gigachat_token, _gigachat_token_expires_at
    if not GIGACHAT_AUTH_KEY:
        return None
    if _gigachat_token and time.time() < _gigachat_token_expires_at - 60:
        return _gigachat_token  # ещё не истёк — используем кэш

    headers = {
        "Authorization": f"Basic {GIGACHAT_AUTH_KEY}",
        "RqUID": str(uuid.uuid4()),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    data = {"scope": GIGACHAT_SCOPE}
    try:
        # verify=False: GigaChat использует сертификат УЦ Минцифры, которого нет
        # в стандартном наборе доверенных сертификатов на большинстве серверов/раннеров.
        # Для продакшена лучше установить сертификат Минцифры и использовать verify=<путь к .pem>.
        resp = requests.post(GIGACHAT_OAUTH_URL, headers=headers, data=data, verify=False, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
        _gigachat_token = payload["access_token"]
        # expires_at приходит в миллисекундах unix-времени
        _gigachat_token_expires_at = payload["expires_at"] / 1000
        print("[INFO] GigaChat token obtained.")
        return _gigachat_token
    except Exception as e:
        print(f"[WARN] Could not obtain GigaChat token: {e}")
        return None


if GIGACHAT_AUTH_KEY:
    # Пробуем получить токен один раз при старте, чтобы сразу увидеть проблему в логах
    get_gigachat_token()
else:
    print("[WARN] GIGACHAT_AUTH_KEY not set, AI rewriting disabled.")


# --- Загрузка уже отправленных ID ---
def load_posted():
    if os.path.exists(POSTED_FILE):
        with open(POSTED_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_posted(posted_set):
    with open(POSTED_FILE, "w") as f:
        json.dump(list(posted_set), f)


# --- Перефразирование через GigaChat ---
def rewrite_with_ai(title, summary):
    token = get_gigachat_token()
    if not token:
        return None
    try:
        prompt = f"""Ты — редактор новостного дайджеста для Telegram-канала. Перепиши следующую новость живым, кратким и цепляющим языком (1–2 предложения, не больше 250 символов). Сохрани все ключевые факты, убери канцелярит и воду. Пиши так, чтобы хотелось дочитать.

Заголовок: {title}
Краткое содержание: {summary if summary else "нет"}

Ответ (только переписанный текст, без кавычек и пояснений):"""

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = {
            "model": "GigaChat",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            "max_tokens": 150,
        }
        resp = requests.post(GIGACHAT_CHAT_URL, headers=headers, json=payload, verify=False, timeout=20)
        if resp.status_code == 401:
            # токен мог протухнуть раньше времени — обновляем и пробуем один раз ещё
            global _gigachat_token
            _gigachat_token = None
            token = get_gigachat_token()
            if not token:
                return None
            headers["Authorization"] = f"Bearer {token}"
            resp = requests.post(GIGACHAT_CHAT_URL, headers=headers, json=payload, verify=False, timeout=20)

        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()
        time.sleep(AI_CALL_DELAY)  # небольшая пауза между вызовами
        return text
    except Exception as e:
        print(f"[ERROR] GigaChat rewrite error: {e}")
        return None


# --- Загрузка одного RSS с ретраями ---
def fetch_feed_with_retry(url):
    last_error = None
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=FETCH_TIMEOUT)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            if feed.bozo and not feed.entries:
                raise ValueError(f"bozo parse error: {feed.bozo_exception}")
            return feed
        except Exception as e:
            last_error = e
            print(f"[WARN] Attempt {attempt}/{FETCH_RETRIES} failed for {url}: {e}")
            if attempt < FETCH_RETRIES:
                time.sleep(2 * attempt)  # backoff: 2s, 4s...
    print(f"[ERROR] Failed to parse {url} after {FETCH_RETRIES} attempts: {last_error}")
    return None


# --- Основной сбор ---
def fetch_news():
    posted = load_posted()
    new_items = []

    if not os.path.exists(FEEDS_FILE):
        print("[ERROR] feeds.txt not found!")
        return []

    with open(FEEDS_FILE, "r") as f:
        feed_urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    for url in feed_urls:
        if len(new_items) >= MAX_ITEMS:
            break  # глобальный лимит достигнут — прекращаем сбор со всех фидов

        feed = fetch_feed_with_retry(url)
        if feed is None:
            continue

        for entry in feed.entries:
            if len(new_items) >= MAX_ITEMS:
                break  # глобальный лимит — выходим из внутреннего цикла тоже

            entry_id = entry.get("id") or entry.get("link")
            if not entry_id or entry_id in posted:
                continue

            title = entry.get("title", "Без заголовка")
            summary = entry.get("summary", entry.get("description", ""))
            summary = re.sub(r"<[^>]+>", "", summary)  # убираем HTML-теги
            link = entry.get("link", "")

            rewritten = rewrite_with_ai(title, summary)
            if rewritten:
                text = html.escape(rewritten)
            else:
                fallback = f"{title}. {summary[:200]}..." if summary else title
                text = html.escape(fallback)

            emoji = pick_emoji(title)
            src = source_name(link)

            new_items.append({
                "id": entry_id,
                "emoji": emoji,
                "source": src,
                "text": text,
                "link": link,
                "published": entry.get("published", datetime.now().isoformat())
            })

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


# --- Формирование текста одной новости ---
def format_item(item):
    src_line = f" · <i>{html.escape(item['source'])}</i>" if item["source"] else ""
    body = f"{item['emoji']} {item['text']}{src_line}"
    if item["link"]:
        body += f"\n🔗 <a href=\"{item['link']}\">Читать полностью</a>"
    return body


# --- Формирование дайджеста (с разбивкой по элементам, а не по символам) ---
def build_digest_messages(items):
    if not items:
        return []

    header = f"📅 <b>Дайджест новостей — {datetime.now().strftime('%d.%m.%Y')}</b>\n\n"
    footer = "\n👍 Ставьте реакции, если дайджест понравился!"

    messages = []
    current = header
    for item in items:
        block = format_item(item) + "\n\n"
        # если добавление блока превысит лимит Telegram — закрываем текущее сообщение
        if len(current) + len(block) + len(footer) > 4096:
            messages.append(current.rstrip())
            current = ""
        current += block

    current += footer
    messages.append(current.rstrip())
    return messages


# --- Главная ---
def main():
    print(f"[START] {datetime.now().isoformat()}")
    news = fetch_news()
    if not news:
        print("[INFO] No new news.")
        return

    messages = build_digest_messages(news)

    all_ok = True
    for msg in messages:
        ok = send_to_telegram(msg)
        if not ok:
            all_ok = False
            break  # не продолжаем, если Telegram недоступен

    if all_ok:
        # помечаем как отправленные ТОЛЬКО если реально ушло в Telegram
        posted = load_posted()
        for item in news:
            posted.add(item["id"])
        save_posted(posted)
        print(f"[DONE] Sent {len(news)} items in {len(messages)} message(s).")
    else:
        print("[WARN] Send failed — items NOT marked as posted, will retry next run.")


if __name__ == "__main__":
    main()
