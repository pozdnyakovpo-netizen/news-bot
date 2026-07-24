#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import html
import uuid
import random
import feedparser
import requests
import urllib3
from urllib.parse import urlparse, quote
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
SEND_DELAY = 1.5                   # сек, пауза между отправками отдельных постов (анти-флуд)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NewsDigestBot/1.0; +https://t.me/)"
}

# Эмодзи и русские названия категорий — определяем по ключевым словам
# в заголовке + кратком описании новости. Порядок важен: первое совпадение побеждает.
CATEGORY_RULES = [
    (["спорт", "футбол", "хоккей", "олимпиад", "чемпионат", "матч", "турнир",
      "сборная", "тренер", "гол ", "теннис", "баскетбол", "волейбол", "марафон"],
     "⚽", "Спорт", "#спорт"),
    (["экономик", "рубл", "доллар", "евро", "нефт", "банк", "рынок", "инфляц",
      "бюджет", "актив", "приватиз", "компани", "завод", "бизнес", "инвестиц",
      "налог", "цена", "товар", "экспорт", "импорт", "производств", "бренд",
      "предприяти", "прибыл", "убыт", "акци", "биржа"],
     "💰", "Экономика", "#экономика"),
    (["технолог", "ии ", "искусственн", "робот", "гаджет", "смартфон",
      "интернет", "приложени", "разработ", "стартап", "программ", "цифров",
      "нейросет", "чип", "процессор", "софт", "гейминг", "видеоигр"],
     "💻", "Технологии", "#технологии"),
    (["наука", "учен", "исследован", "космос", "открыт", "эксперимент",
      "лаборатор", "генетик", "вселенн", "спутник", "ракет", "археолог"],
     "🔬", "Наука", "#наука"),
    (["погод", "климат", "ураган", "снег", "морож", "дожд", "жара",
      "гроза", "шторм", "потепл", "заморозк"],
     "🌦", "Погода", "#погода"),
    (["здоровь", "медицин", "врач", "болезн", "вирус", "больниц", "вакцин",
      "эпидеми", "пациент", "лечени", "препарат", "клиник"],
     "🩺", "Здоровье", "#здоровье"),
    (["политик", "президент", "правительств", "министр", "закон", "госдум",
      "депутат", "указ", "санкц", "переговор", "парламент", "выбор", "чиновник"],
     "🏛", "Политика", "#политика"),
    (["происшеств", "авари", "пожар", "взрыв", "трагед", "погиб", "пострада",
      "дтп", "эвакуац", "преступлен", "ограничил", "чрезвычайн", "разыскива",
      "задержан", "суд ", "уголовн"],
     "🚨", "Происшествия", "#происшествия"),
    (["культур", "кино", "музык", "театр", "выставк", "концерт", "фестивал",
      "книга", "актер", "актрис", "режиссер", "премьер"],
     "🎭", "Культура", "#культура"),
    (["аэропорт", "рейс", "самолет", "поезд", "автомоб", "дорог", "метро",
      "трасс", "маршрут", "перевозк", "транспорт", "вокзал"],
     "🚗", "Транспорт", "#транспорт"),
]
DEFAULT_EMOJI = "📰"
DEFAULT_LABEL = "Разное"
DEFAULT_HASHTAG = "#новости"

WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

DIVIDER = "┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"

CHANNEL_USERNAME = "deepdailyfact"
CHANNEL_LINK = f"https://t.me/{CHANNEL_USERNAME}"
SHARE_URL = (
    "https://t.me/share/url?url="
    + quote(CHANNEL_LINK, safe="")
    + "&text="
    + quote("Нашёл крутой новостной канал — залетай 👇", safe="")
)

# Разные формулировки призыва поделиться — чтобы не приедалось при частой публикации
CTA_VARIANTS = [
    f"🚀 <a href=\"{SHARE_URL}\">Поделиться каналом с друзьями</a>",
    f"📣 <a href=\"{SHARE_URL}\">Расскажи друзьям про канал</a>",
    f"✉️ <a href=\"{SHARE_URL}\">Отправить другу</a>",
    f"🔥 <a href=\"{SHARE_URL}\">Знаешь, кому это будет интересно? Поделись</a>",
]

MILESTONES = [10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000]
MILESTONES_FILE = "milestones.json"


def pick_category(title, summary=""):
    t = (title + " " + summary).lower()
    for keywords, emoji, label, hashtag in CATEGORY_RULES:
        if any(kw in t for kw in keywords):
            return emoji, label, hashtag
    return DEFAULT_EMOJI, DEFAULT_LABEL, DEFAULT_HASHTAG


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
        resp = requests.post(GIGACHAT_OAUTH_URL, headers=headers, data=data, verify=False, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
        _gigachat_token = payload["access_token"]
        _gigachat_token_expires_at = payload["expires_at"] / 1000
        print("[INFO] GigaChat token obtained.")
        return _gigachat_token
    except Exception as e:
        print(f"[WARN] Could not obtain GigaChat token: {e}")
        return None


if GIGACHAT_AUTH_KEY:
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


# --- Перефразирование через GigaChat: жирный заголовок + текст, как у крупных СМИ-каналов ---
def rewrite_with_ai(title, summary):
    token = get_gigachat_token()
    if not token:
        return None
    try:
        prompt = f"""Ты — редактор новостного Telegram-канала уровня РБК. Сделай из новости пост в 2 частях.

1) ЗАГОЛОВОК — короткий, конкретный, без кавычек и точки в конце (5–9 слов)
2) ТЕКСТ — 1–2 предложения по существу, живым языком, без канцелярита, со всеми ключевыми фактами и цифрами

Заголовок исходной новости: {title}
Краткое содержание: {summary if summary else "нет"}

Ответь строго в формате:
ЗАГОЛОВОК: <текст>
ТЕКСТ: <текст>"""

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = {
            "model": "GigaChat",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            "max_tokens": 220,
        }
        resp = requests.post(GIGACHAT_CHAT_URL, headers=headers, json=payload, verify=False, timeout=20)
        if resp.status_code == 401:
            global _gigachat_token
            _gigachat_token = None
            token = get_gigachat_token()
            if not token:
                return None
            headers["Authorization"] = f"Bearer {token}"
            resp = requests.post(GIGACHAT_CHAT_URL, headers=headers, json=payload, verify=False, timeout=20)

        resp.raise_for_status()
        data = resp.json()
        answer = data["choices"][0]["message"]["content"].strip()
        time.sleep(AI_CALL_DELAY)

        headline_match = re.search(r"ЗАГОЛОВОК:\s*(.+)", answer)
        body_match = re.search(r"ТЕКСТ:\s*(.+)", answer, re.S)
        if headline_match and body_match:
            headline = headline_match.group(1).strip()
            body = body_match.group(1).strip()
            return {"headline": headline, "body": body}
        return None
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
                time.sleep(2 * attempt)
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
            break

        feed = fetch_feed_with_retry(url)
        if feed is None:
            continue

        for entry in feed.entries:
            if len(new_items) >= MAX_ITEMS:
                break

            entry_id = entry.get("id") or entry.get("link")
            if not entry_id or entry_id in posted:
                continue

            title = entry.get("title", "Без заголовка")
            summary = entry.get("summary", entry.get("description", ""))
            summary = re.sub(r"<[^>]+>", "", summary)
            link = entry.get("link", "")

            rewritten = rewrite_with_ai(title, summary)
            if rewritten:
                headline = html.escape(rewritten["headline"])
                body = html.escape(rewritten["body"])
            else:
                headline = html.escape(title[:90])
                body = html.escape(summary[:200] + "..." if summary else title)

            emoji, label, hashtag = pick_category(title, summary)
            src = source_name(link)

            new_items.append({
                "id": entry_id,
                "emoji": emoji,
                "label": label,
                "hashtag": hashtag,
                "source": src,
                "headline": headline,
                "body": body,
                "link": link,
                "published": entry.get("published", datetime.now().isoformat())
            })

    return new_items


# --- AI выбирает самую интересную новость для блока "Главное" ---
def pick_featured_index(items):
    if len(items) <= 1:
        return 0
    token = get_gigachat_token()
    if not token:
        return 0
    try:
        listing = "\n".join(f"{i}. {it['headline']} — {it['body'][:120]}" for i, it in enumerate(items))
        prompt = (
            "Ниже список новостей дайджеста, пронумерованных с 0. "
            "Выбери номер самой интересной и цепляющей новости для широкой аудитории — "
            "ту, что достойна идти первой как главная. "
            "Ответь только числом, без пояснений и текста.\n\n" + listing
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = {
            "model": "GigaChat",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 10,
        }
        resp = requests.post(GIGACHAT_CHAT_URL, headers=headers, json=payload, verify=False, timeout=15)
        resp.raise_for_status()
        answer = resp.json()["choices"][0]["message"]["content"].strip()
        match = re.search(r"\d+", answer)
        if match:
            idx = int(match.group())
            if 0 <= idx < len(items):
                return idx
    except Exception as e:
        print(f"[WARN] pick_featured_index error: {e}")
    return 0


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


# --- Текст одного поста в стиле крупных СМИ-каналов: жирный заголовок + текст ---
def format_post(item, extra=""):
    src_line = f" — <i>{html.escape(item['source'])}</i>" if item["source"] else ""
    text = f"{item['emoji']} <b>{item['headline']}</b>\n\n{item['body']}{src_line}"
    if item["link"]:
        text += f"\n\n🔗 <a href=\"{item['link']}\">Читать полностью</a>"
    text += f"\n\n{item['hashtag']}"
    if extra:
        text += f"\n\n{extra}"
    return text


def build_growth_line(subscriber_count):
    if subscriber_count is None:
        return ""
    upcoming = next((m for m in MILESTONES if m > subscriber_count), None)
    if upcoming is None:
        return ""
    remaining = upcoming - subscriber_count
    return f"🎯 До {upcoming} подписчиков осталось {remaining} — приведи друга!\n"


# --- Умное самопродвижение: авто-поздравление с вехами подписчиков ---
def get_subscriber_count():
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getChatMemberCount"
    try:
        resp = requests.get(url, params={"chat_id": CHAT_ID}, timeout=10)
        data = resp.json()
        if data.get("ok"):
            return data["result"]
        print(f"[WARN] getChatMemberCount failed: {data}")
        return None
    except Exception as e:
        print(f"[WARN] getChatMemberCount error: {e}")
        return None


def update_channel_description(count):
    if count is None or not TELEGRAM_TOKEN or not CHAT_ID:
        return
    now = datetime.now()
    description = (
        f"📰 Новостной дайджест каждые 40 минут\n"
        f"🔥 {count} подписчиков\n"
        f"🕒 Обновлено {now.strftime('%d.%m %H:%M')}"
    )[:255]
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setChatDescription"
    try:
        resp = requests.post(url, json={"chat_id": CHAT_ID, "description": description}, timeout=10)
        data = resp.json()
        if not data.get("ok"):
            print(f"[WARN] setChatDescription failed: {data}")
    except Exception as e:
        print(f"[WARN] setChatDescription error: {e}")


def load_last_milestone():
    if os.path.exists(MILESTONES_FILE):
        with open(MILESTONES_FILE, "r") as f:
            return json.load(f).get("last", 0)
    return 0


def save_last_milestone(value):
    with open(MILESTONES_FILE, "w") as f:
        json.dump({"last": value}, f)


def maybe_celebrate_milestone(count):
    if count is None:
        return
    last = load_last_milestone()
    crossed = [m for m in MILESTONES if last < m <= count]
    if not crossed:
        return
    new_milestone = max(crossed)
    text = (
        f"🎉 <b>Нас уже {count}!</b>\n\n"
        f"Спасибо, что читаете — канал растёт благодаря вам.\n"
        f"Если ещё не поделились с друзьями — самое время 👇\n\n"
        f"{random.choice(CTA_VARIANTS)}"
    )
    if send_to_telegram(text):
        save_last_milestone(new_milestone)
        print(f"[INFO] Milestone celebrated: {new_milestone} subscribers.")


# --- Главная ---
def main():
    print(f"[START] {datetime.now().isoformat()}")

    count = get_subscriber_count()
    update_channel_description(count)
    maybe_celebrate_milestone(count)

    news = fetch_news()
    if not news:
        print("[INFO] No new news.")
        return

    # самую интересную новость ставим первой
    featured_idx = pick_featured_index(news)
    if featured_idx != 0:
        news.insert(0, news.pop(featured_idx))

    growth_line = build_growth_line(count)

    posted = load_posted()
    sent_count = 0
    for i, item in enumerate(news):
        is_last = (i == len(news) - 1)
        extra = f"{growth_line}{random.choice(CTA_VARIANTS)}" if is_last else ""
        text = format_post(item, extra=extra)

        ok = send_to_telegram(text)
        if ok:
            # помечаем как отправленное СРАЗУ — если следующий пост не уйдёт,
            # уже опубликованные не задвоятся при повторном запуске
            posted.add(item["id"])
            save_posted(posted)
            sent_count += 1
            time.sleep(SEND_DELAY)
        else:
            print(f"[WARN] Failed to send item {item['id']} — will retry next run.")
            break

    print(f"[DONE] Sent {sent_count}/{len(news)} items as separate posts.")


if __name__ == "__main__":
    main()
