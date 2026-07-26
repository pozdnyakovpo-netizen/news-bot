#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import html
import uuid
import random
import hashlib
import requests
import urllib3
from bs4 import BeautifulSoup
from urllib.parse import urlparse, quote
from datetime import datetime, timedelta

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
LAST_RUN_FILE = "last_run.json"    # хранит время последней успешной публикации
PUBLISH_INTERVAL = 600             # сек, интервал между ОБЫЧНЫМИ публикациями (10 минут)
URGENT_INTERVAL = 120              # сек, интервал между СРОЧНЫМИ публикациями (2 минуты)
ITEMS_PER_RUN = 1                  # сколько реально публикуем за один допустимый запуск
FETCH_POOL_SIZE = 12               # сколько кандидатов собрать перед выбором самой важной новости
FETCH_TIMEOUT = 15                 # сек, таймаут на загрузку страницы канала
AI_CALL_DELAY = 1.5                # сек, пауза между вызовами GigaChat
SEND_DELAY = 1.5                   # сек, пауза между отправками отдельных постов

CHANNEL_USERNAME = "deepdailyfact"
CHANNEL_LINK = f"https://t.me/{CHANNEL_USERNAME}"
SHARE_URL = (
    "https://t.me/share/url?url="
    + quote(CHANNEL_LINK, safe="")
    + "&text="
    + quote("Нашёл крутой новостной канал — залетай 👇", safe="")
)

CTA_VARIANTS = [
    f"🚀 <a href=\"{SHARE_URL}\">Поделиться каналом с друзьями</a>",
    f"📣 <a href=\"{SHARE_URL}\">Расскажи друзьям про канал</a>",
    f"✉️ <a href=\"{SHARE_URL}\">Отправить другу</a>",
    f"🔥 <a href=\"{SHARE_URL}\">Знаешь, кому это будет интересно? Поделись</a>",
]

MILESTONES = [10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000]
MILESTONES_FILE = "milestones.json"

# --- Вовлечённость: дайджесты по расписанию, опрос, реакции ---
MSK_OFFSET = timedelta(hours=3)  # Москва не переходит на летнее/зимнее время с 2014 года
DIGEST_TIMES = ["08:00", "21:00"]  # время публикации сводок по МСК
DIGEST_SIZE = 5                     # сколько новостей включать в одну сводку
DIGEST_WINDOW_MINUTES = 4           # окно срабатывания (cron раз в 2 минуты — берём с запасом)
DIGEST_STATE_FILE = "last_digest.json"

POLL_TIME = "12:00"  # МСК, время ежедневного вовлекающего опроса
POLL_QUESTION = "Какая тема сейчас интереснее всего?"
POLL_OPTIONS = ["Политика", "Происшествия", "Спорт", "Технологии", "Экономика"]
POLL_STATE_FILE = "last_poll.json"

CHANNEL_REACTIONS = ["👍", "🔥", "😱", "😢", "🤔"]
REACTIONS_STATE_FILE = "reactions_enabled.json"

URGENT_KEYWORDS = [
    "погиб", "убит", "жертв", "экстренн", "чрезвычайн", "эвакуац",
    "взрыв", "теракт", "катастроф", "введен режим чс",
]

URGENT_EMOJIS = ["🔥", "🚨", "❗️", "⚡️"]
CHANNEL_MARK = "🔷"


def is_urgent(title, summary=""):
    t = (title + " " + summary).lower()
    return any(kw.lower() in t for kw in URGENT_KEYWORDS)


NOT_NEWS_PATTERNS = [
    "лайфхак", "топ-", "5 способов", "10 способов", "7 способов",
    "5 причин", "10 причин", "5 признаков", "10 признаков",
    "интересные факты", "полезные советы", "как выбрать", "как избавиться",
    "чем опасен", "чем опасны", "чем полезен", "чем полезны", "рецепт",
    "гороскоп", "приметы", "что будет если", "5 фактов", "10 фактов",
    "простые способы", "правила ухода", "как правильно",
    "интервью", "колонка", "личный опыт", "рассказала о себе", "рассказал о себе",
    "подкаст", "блог", "мнение:", "спросили у", "разбираем", "объясняем",
    "путеводитель", "подборка", "рейтинг", "рекомендуем", "что посмотреть",
    "что почитать", "что послушать", "тест-драйв", "обзор:",
    "главные новости дня", "главные новости к этому часу", "итоги дня",
    "коротко о главном", "новости к этому часу", "главное к этому часу",
    "дайджест",
]

DIGEST_BULLET_CHARS = ["◆", "▪", "‣", "🔹", "🔸"]


def is_digest_post(text):
    if any(text.count(ch) >= 2 for ch in DIGEST_BULLET_CHARS):
        return True
    if text.count(" — ") >= 3:
        return True
    return False


def is_not_news(title, summary=""):
    t = (title + " " + summary).lower()
    if any(p in t for p in NOT_NEWS_PATTERNS):
        return True
    if is_digest_post(title + " " + summary):
        return True
    return False


MAX_SENTENCES = 7


def limit_sentences(text, max_sentences=MAX_SENTENCES):
    if not text:
        return text
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    sentences = [s for s in sentences if s]
    return " ".join(sentences[:max_sentences]).strip()


SOURCE_MENTION_PATTERNS = [
    r'https?://\S+',
    r'\bwww\.\S+',
    r'читать\s+(далее|полностью|на сайте)[^.!?]*[.!?]?',
    r'источник\s*:\s*\S+',
    r'подробнее\s+(на|в|у)\s+\S+',
    r'фото\s*:\s*\S+',
    r'видео\s*:\s*\S+',
    r'не\s+грузятся\s+фото\s+и\s+видео\?[^.!?]*[.!?]?',
    r'читайте\s+нас\s+(в|на)\s+\S+[^.!?]*[.!?]?',
    r'подпис(ывайтесь|ка)\s+на\s+(наш\s+)?канал[^.!?]*[.!?]?',
    r'скачайте?\s+(наше\s+)?приложение[^.!?]*[.!?]?',
]


def strip_source_mentions(text):
    if not text:
        return text
    for pattern in SOURCE_MENTION_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    text = re.sub(r'\s{2,}', ' ', text).strip()
    return text


def truncate_at_word(text, max_len=90):
    if not text or len(text) <= max_len:
        return text
    cut = text[:max_len]
    last_space = cut.rfind(" ")
    if last_space > 0:
        cut = cut[:last_space]
    return cut.rstrip(" ,.;:—-") + "…"


def source_name(link):
    try:
        domain = urlparse(link).netloc
        domain = domain.replace("www.", "")
        return domain
    except Exception:
        return ""


_gigachat_token = None
_gigachat_token_expires_at = 0

def get_gigachat_token():
    global _gigachat_token, _gigachat_token_expires_at
    if not GIGACHAT_AUTH_KEY:
        return None
    if _gigachat_token and time.time() < _gigachat_token_expires_at - 60:
        return _gigachat_token

    headers = {
        "Authorization": f"Basic {GIGACHAT_AUTH_KEY}",
        "RqUID": str(uuid.uuid4()),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    data = {"scope": GIGACHAT_SCOPE}
    try:
        resp = requests.post(GIGACHAT_OAUTH_URL, headers=headers, data=data, verify=False, timeout=(5, 15))
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


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


def load_posted():
    return set(_load_json(POSTED_FILE, []))


def save_posted(posted_set):
    _save_json(POSTED_FILE, list(posted_set))


def title_dedup_key(title):
    t = title.lower()
    t = re.sub(r'[^a-zа-яё0-9\s]', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\s+', ' ', t).strip()
    return "title:" + hashlib.md5(t.encode("utf-8")).hexdigest()


TITLE_STOPWORDS = {
    "и", "в", "на", "с", "со", "по", "за", "для", "от", "к", "из", "у", "о", "об",
    "при", "до", "под", "над", "же", "ли", "бы", "не", "но", "а", "то", "там", "тут",
    "эта", "этот", "эти", "это", "как", "их", "его", "её", "стал", "стала", "стали",
    "новый", "новая", "новые", "после", "более", "менее", "который", "которая",
}


def significant_title_words(title):
    t = title.lower()
    t = re.sub(r'[^a-zа-яё0-9\s]', ' ', t, flags=re.IGNORECASE)
    words = {w for w in t.split() if len(w) > 2 and w not in TITLE_STOPWORDS}
    return words


def significant_words(text):
    # То же самое, но для произвольного текста (используется для summary)
    return significant_title_words(text or "")


def content_words(title, summary):
    # ВАЖНО (фикс): раньше дедуп сравнивал только заголовки. Разные каналы
    # часто формулируют заголовок про одно и то же событие совершенно
    # по-разному ("вернулся для восстановления" / "вернулся после ЧМ"),
    # из-за чего похожая новость проходила как "новая". Слова из summary
    # (имена, цифры, места) обычно совпадают гораздо надёжнее, чем слова
    # в заголовке — добавляем их в сравнение.
    return significant_title_words(title) | significant_words(summary)


def titles_are_similar(words_a, words_b, threshold=0.5):
    # Коэффициент перекрытия: общие слова / слова в БОЛЕЕ КОРОТКОМ наборе —
    # если меньший набор почти целиком содержится в большем, речь об одном
    # и том же факте. Порог снижен с 0.6 до 0.5 после перехода на
    # title+summary (наборы слов стали крупнее, и полезный сигнал
    # разбавляется словами, не относящимися к сути события).
    if not words_a or not words_b:
        return False
    smaller = min(len(words_a), len(words_b))
    if smaller == 0:
        return False
    return (len(words_a & words_b) / smaller) >= threshold


RECENT_TITLES_FILE = "recent_titles.json"
RECENT_TITLES_LIMIT = 300


def load_recent_title_words():
    raw = _load_json(RECENT_TITLES_FILE, [])
    result = []
    for words in raw:
        if not isinstance(words, list):
            continue
        try:
            result.append(set(w for w in words if isinstance(w, str)))
        except TypeError:
            continue
    return result


def save_recent_title_words(list_of_word_sets):
    trimmed = list_of_word_sets[-RECENT_TITLES_LIMIT:]
    _save_json(RECENT_TITLES_FILE, [list(s) for s in trimmed])


def is_duplicate_by_meaning(words, recent_word_sets):
    return any(titles_are_similar(words, other) for other in recent_word_sets)


def seconds_since_last_publish():
    last_publish = _load_json(LAST_RUN_FILE, {}).get("last_publish")
    return (time.time() - last_publish) if last_publish else None


def mark_published_now():
    _save_json(LAST_RUN_FILE, {"last_publish": time.time()})


def rewrite_with_ai(title, summary):
    token = get_gigachat_token()
    if not token:
        return None
    try:
        prompt_lines = [
            "Ты — редактор новостного Telegram-канала уровня РБК. Сделай из новости пост в 2 частях.",
            "",
            "1) ЗАГОЛОВОК — короткий, конкретный, без кавычек и точки в конце (5-9 слов)",
            "2) ТЕКСТ — СТРОГО не более 7 предложений, живым языком, без канцелярита. "
            "Только самое важное: что произошло, кто участвует, ключевые цифры/факты и "
            "главное последствие. Без второстепенных деталей, без предыстории и лишних "
            "подробностей — только суть.",
            "",
            "ВАЖНО: если в исходной новости событие описано как предположение, план или "
            "условие (может быть, планируется, по данным источника, предположительно) — "
            "сохрани эту неопределённость и в заголовке, и в тексте. Не выдавай "
            "предположение или чьё-то заявление за свершившийся факт.",
            "",
            f"Заголовок исходной новости: {title}",
            f"Краткое содержание: {summary if summary else 'нет'}",
            "",
            "Ответь строго в формате:",
            "ЗАГОЛОВОК: <текст>",
            "ТЕКСТ: <текст>",
        ]
        prompt = "\n".join(prompt_lines)

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = {
            "model": "GigaChat",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            "max_tokens": 800,
        }
        resp = requests.post(GIGACHAT_CHAT_URL, headers=headers, json=payload, verify=False, timeout=(5, 20))
        if resp.status_code == 401:
            global _gigachat_token
            _gigachat_token = None
            token = get_gigachat_token()
            if not token:
                return None
            headers["Authorization"] = f"Bearer {token}"
            resp = requests.post(GIGACHAT_CHAT_URL, headers=headers, json=payload, verify=False, timeout=(5, 20))

        if resp.status_code == 429:
            print("[WARN] GigaChat rate limit (429) — backing off.")
            time.sleep(3)
            return None

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


TELEGRAM_PREVIEW_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}


def fetch_telegram_channel(username, limit=20):
    url = f"https://t.me/s/{username}"
    try:
        resp = requests.get(url, headers=TELEGRAM_PREVIEW_HEADERS, timeout=FETCH_TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        print(f"[WARN] Telegram fetch failed for @{username}: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    messages = soup.select("div.tgme_widget_message")[-limit:]
    entries = []

    for msg in messages:
        post_id = msg.get("data-post")
        if not post_id:
            continue
        link = f"https://t.me/{post_id}"

        text_el = msg.select_one(".tgme_widget_message_text")
        raw_text = text_el.get_text("\n", strip=True) if text_el else ""
        if not raw_text:
            continue

        first_line = raw_text.split("\n")[0].strip()
        title = first_line[:200] if first_line else raw_text[:200]

        text = re.sub(r'\s+', ' ', raw_text).strip()

        photo = None
        photo_bytes = None
        photo_el = msg.select_one(".tgme_widget_message_photo_wrap")
        if photo_el and photo_el.get("style"):
            m = re.search(r"url\('([^']+)'\)", photo_el["style"])
            if m:
                photo = m.group(1)
                try:
                    img_headers = dict(TELEGRAM_PREVIEW_HEADERS)
                    img_headers["Referer"] = url
                    img_resp = requests.get(photo, headers=img_headers, timeout=10)
                    if img_resp.status_code == 200 and img_resp.content:
                        photo_bytes = img_resp.content
                except Exception as e:
                    print(f"[WARN] photo download failed for {post_id}: {e}")

        video = None
        video_el = msg.select_one("video.tgme_widget_message_video")
        if video_el and video_el.get("src"):
            video = video_el["src"]

        time_el = msg.select_one("time")
        published = time_el.get("datetime") if time_el and time_el.get("datetime") else datetime.now().isoformat()

        entries.append({
            "id": link,
            "title": title,
            "summary": text,
            "link": link,
            "photo": photo,
            "photo_bytes": photo_bytes,
            "video": video,
            "published": published,
            "source_channel": username,
        })

    return entries[::-1]


def fetch_news():
    posted = load_posted()
    recent_content_words = load_recent_title_words()
    new_items = []
    seen_title_keys = set()
    seen_content_words = []

    if not os.path.exists(FEEDS_FILE):
        print("[ERROR] feeds.txt not found!")
        return []

    with open(FEEDS_FILE, "r") as f:
        channels = [line.strip().lstrip("@") for line in f if line.strip() and not line.startswith("#")]

    random.shuffle(channels)

    for channel in channels:
        if len(new_items) >= FETCH_POOL_SIZE:
            break

        channel_entries = fetch_telegram_channel(channel)
        if not channel_entries:
            continue

        for entry in channel_entries:
            if len(new_items) >= FETCH_POOL_SIZE:
                break

            entry_id = entry.get("id") or entry.get("link")
            if not entry_id or entry_id in posted:
                continue

            title = html.unescape(entry.get("title", "Без заголовка"))
            title_key = title_dedup_key(title)
            if title_key in posted or title_key in seen_title_keys:
                continue

            raw_summary = entry.get("summary", entry.get("description", ""))
            summary = strip_source_mentions(html.unescape(re.sub(r"<[^>]+>", "", raw_summary)))
            link = entry.get("link", "")

            # ФИКС: сравниваем по словам заголовка + summary вместе (не
            # только заголовка), потому что разные каналы часто по-разному
            # формулируют заголовок про одно и то же событие, а факты в
            # тексте (summary) обычно совпадают.
            c_words = content_words(title, summary)
            if is_duplicate_by_meaning(c_words, recent_content_words) or \
               any(titles_are_similar(c_words, w) for w in seen_content_words):
                continue

            if is_not_news(title, summary):
                continue

            photo = entry.get("photo")
            photo_bytes = entry.get("photo_bytes")
            video = entry.get("video")
            if not photo and not video:
                continue

            src = f"@{entry['source_channel']}" if entry.get("source_channel") else source_name(link)
            urgent = is_urgent(title, summary)
            print(f"[INFO] '{title[:50]}' ({src}) — photo={'yes' if photo else 'no'}, video={'yes' if video else 'no'}")

            new_items.append({
                "id": entry_id,
                "title_key": title_key,
                "content_words": list(c_words),
                "source": src,
                "urgent": urgent,
                "title": title,
                "summary": summary,
                "link": link,
                "photo": photo,
                "photo_bytes": photo_bytes,
                "video": video,
                "published": entry.get("published", datetime.now().isoformat())
            })
            seen_title_keys.add(title_key)
            seen_content_words.append(c_words)

    return new_items


def finalize_item(item):
    rewritten = rewrite_with_ai(item["title"], item["summary"])
    if rewritten:
        item["headline"] = html.escape(rewritten["headline"])
        item["body"] = html.escape(limit_sentences(strip_source_mentions(rewritten["body"])))
    else:
        source_text = item["summary"] if item["summary"] else item["title"]
        sentences = [s for s in re.split(r'(?<=[.!?])\s+', source_text.strip()) if s]
        item["headline"] = html.escape(truncate_at_word(sentences[0], 90)) if sentences else html.escape(truncate_at_word(item["title"], 90))
        rest = " ".join(sentences[1:MAX_SENTENCES + 1])
        item["body"] = html.escape(rest) if rest else ""
    return item


def pick_featured_index(items):
    if len(items) <= 1:
        return 0
    token = get_gigachat_token()
    if not token:
        return 0
    try:
        listing = "\n".join(f"{i}. {it['title']} — {it['summary'][:120]}" for i, it in enumerate(items))
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
        resp = requests.post(GIGACHAT_CHAT_URL, headers=headers, json=payload, verify=False, timeout=(5, 15))
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


def pick_non_duplicate(items):
    # ФИКС (защита от гонки между запусками): к моменту, когда мы
    # действительно готовы отправлять пост, могло пройти время — например,
    # только что отработал дайджест и опубликовал что-то очень похожее на
    # нашего кандидата. Перечитываем posted/recent прямо перед отправкой и
    # берём первого кандидата, который всё ещё не дубликат.
    posted_now = load_posted()
    recent_now = load_recent_title_words()
    for it in items:
        if it["id"] in posted_now or it["title_key"] in posted_now:
            continue
        cw = set(it.get("content_words") or [])
        if cw and is_duplicate_by_meaning(cw, recent_now):
            continue
        return it
    return None


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


def _send_media_to_telegram(method, field, media_url, caption=None, media_bytes=None):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/send{method}"
    cap = {"caption": caption[:1024], "parse_mode": "HTML"} if caption is not None else {}
    filename = "photo.jpg" if field == "photo" else "video.mp4"

    if media_bytes:
        try:
            resp = requests.post(url, data={"chat_id": CHAT_ID, **cap},
                                  files={field: (filename, media_bytes)}, timeout=30)
            if resp.status_code == 200:
                return True
            print(f"[WARN] send{method} by cached bytes failed: {resp.text}")
        except Exception as e:
            print(f"[WARN] send{method} by cached bytes error: {e}")

    try:
        resp = requests.post(url, json={"chat_id": CHAT_ID, field: media_url, **cap}, timeout=15)
        if resp.status_code == 200:
            return True
        print(f"[WARN] send{method} by URL failed: {resp.text}")
    except Exception as e:
        print(f"[WARN] send{method} by URL error: {e}")

    try:
        dl = requests.get(media_url, headers=TELEGRAM_PREVIEW_HEADERS, timeout=20)
        dl.raise_for_status()
        resp = requests.post(url, data={"chat_id": CHAT_ID, **cap},
                              files={field: (filename, dl.content)}, timeout=30)
        if resp.status_code == 200:
            return True
        print(f"[WARN] send{method} by upload failed: {resp.text}")
        return False
    except Exception as e:
        print(f"[WARN] send{method} by upload error: {e}")
        return False


def send_photo_to_telegram(photo_url, caption=None, photo_bytes=None):
    return _send_media_to_telegram("Photo", "photo", photo_url, caption, photo_bytes)


def send_video_to_telegram(video_url, caption=None):
    return _send_media_to_telegram("Video", "video", video_url, caption)


def send_post(item, text):
    media_url = item.get("video") or item.get("photo")
    is_video = bool(item.get("video"))
    photo_bytes = item.get("photo_bytes") if not is_video else None

    def sender(url_, caption_=None):
        if is_video:
            return send_video_to_telegram(url_, caption_)
        return send_photo_to_telegram(url_, caption_, photo_bytes=photo_bytes)

    if media_url:
        if len(text) <= 1024:
            if sender(media_url, text):
                return True
            return send_to_telegram(text)
        else:
            media_ok = sender(media_url)
            if media_ok:
                time.sleep(0.5)
            return send_to_telegram(text)

    return send_to_telegram(text)


def format_post(item, extra=""):
    lead = f"{random.choice(URGENT_EMOJIS)} " if item.get("urgent") else ""
    text = f"{CHANNEL_MARK} {lead}<b>{item['headline']}</b>"
    if item.get("body"):
        text += f"\n\n{item['body']}"
    if extra:
        text += f"\n\n{extra}"
    return text


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
    description = (
        f"Коротко о главном. Один пост — одна новость, без вороха дублей и рекламы. "
        f"Источники — проверенные федеральные СМИ и Telegram-каналы.\n"
        f"{count} подписчиков"
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
    return _load_json(MILESTONES_FILE, {}).get("last", 0)


def save_last_milestone(value):
    _save_json(MILESTONES_FILE, {"last": value})


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


def now_msk():
    return datetime.utcnow() + MSK_OFFSET


def today_key(dt):
    return dt.strftime("%Y-%m-%d")


def due_digest_slot(dt, already_done_slots):
    now_minutes = dt.hour * 60 + dt.minute
    for slot in DIGEST_TIMES:
        if slot in already_done_slots:
            continue
        slot_h, slot_m = map(int, slot.split(":"))
        slot_minutes = slot_h * 60 + slot_m
        if 0 <= (now_minutes - slot_minutes) <= DIGEST_WINDOW_MINUTES:
            return slot
    return None


def due_poll(dt, already_sent_today):
    if already_sent_today:
        return False
    now_minutes = dt.hour * 60 + dt.minute
    slot_h, slot_m = map(int, POLL_TIME.split(":"))
    slot_minutes = slot_h * 60 + slot_m
    return 0 <= (now_minutes - slot_minutes) <= DIGEST_WINDOW_MINUTES


def send_poll_to_telegram(question, options):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPoll"
    payload = {
        "chat_id": CHAT_ID,
        "question": question,
        "options": options,
        "is_anonymous": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        data = resp.json()
        if not data.get("ok"):
            print(f"[WARN] sendPoll failed: {data}")
            return False
        return True
    except Exception as e:
        print(f"[WARN] sendPoll error: {e}")
        return False


def enable_reactions():
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setChatAvailableReactions"
    payload = {
        "chat_id": CHAT_ID,
        "available_reactions": [{"type": "emoji", "emoji": e} for e in CHANNEL_REACTIONS],
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        data = resp.json()
        if not data.get("ok"):
            print(f"[WARN] setChatAvailableReactions failed: {data}")
            return False
        print("[INFO] Reactions enabled.")
        return True
    except Exception as e:
        print(f"[WARN] setChatAvailableReactions error: {e}")
        return False


def format_digest(items, slot_label):
    lines = [f"{CHANNEL_MARK} <b>{slot_label}</b>", ""]
    for i, it in enumerate(items, 1):
        lines.append(f"{i}. <b>{it['headline']}</b>")
        if it.get("body"):
            first_sentence = it["body"].split(". ")[0].rstrip(".") + "."
            lines.append(first_sentence)
        lines.append("")
    lines.append(random.choice(CTA_VARIANTS))
    return "\n".join(lines).strip()


STATE_FILES = [
    POSTED_FILE, RECENT_TITLES_FILE, LAST_RUN_FILE, MILESTONES_FILE,
    DIGEST_STATE_FILE, POLL_STATE_FILE, REACTIONS_STATE_FILE,
]


def _git_show_json(ref_path, default):
    import subprocess
    result = subprocess.run(["git", "show", ref_path], capture_output=True, text=True)
    if result.returncode != 0:
        return default
    try:
        return json.loads(result.stdout)
    except Exception:
        return default


def persist_state_to_git(new_posted_ids=None, new_title_words_list=None, new_last_publish=None,
                          new_digest_state=None, new_poll_state=None, new_reactions_enabled=None):
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    import subprocess

    new_posted_ids = new_posted_ids or []
    # ФИКС: раньше сюда передавался только ОДИН набор слов (от одиночного
    # поста), а дайджест вообще ничего не передавал. Теперь это список
    # наборов слов — по одному на каждый реально отправленный пост, включая
    # все элементы дайджеста.
    new_title_words_list = new_title_words_list or []

    try:
        subprocess.run(["git", "config", "user.name", "news-bot"], check=False)
        subprocess.run(["git", "config", "user.email", "news-bot@users.noreply.github.com"], check=False)

        # ФИКС: было всего 3 попытки с фиксированной паузой 2 сек — этого
        # мало для транзиентных конфликтов git push. Если все попытки
        # проваливались, пост уже уходил в Telegram, а запись о нём в
        # posted.json так и не попадала в git — следующий запуск не знал
        # о публикации и мог отправить ту же новость повторно (см.
        # скриншот с дублем СУ-34 от РИА Новости). Увеличиваем число
        # попыток и делаем паузу растущей + со случайным джиттером, чтобы
        # конфликтующие процессы не сталкивались раз за разом синхронно.
        MAX_PUSH_ATTEMPTS = 8
        for attempt in range(1, MAX_PUSH_ATTEMPTS + 1):
            fetch = subprocess.run(["git", "fetch", "origin", "main"], capture_output=True, text=True)
            if fetch.returncode != 0:
                print(f"[WARN] git fetch failed (attempt {attempt}): {fetch.stderr}")

            remote_posted = _git_show_json(f"origin/main:{POSTED_FILE}", [])
            remote_recent = _git_show_json(f"origin/main:{RECENT_TITLES_FILE}", [])
            remote_last_run = _git_show_json(f"origin/main:{LAST_RUN_FILE}", {})
            remote_milestones = _git_show_json(f"origin/main:{MILESTONES_FILE}", {"last": 0})
            remote_digest = _git_show_json(f"origin/main:{DIGEST_STATE_FILE}", {"date": None, "slots": []})
            remote_poll = _git_show_json(f"origin/main:{POLL_STATE_FILE}", {"date": None, "sent": False})
            remote_reactions = _git_show_json(f"origin/main:{REACTIONS_STATE_FILE}", {"enabled": False})

            merged_posted = set(remote_posted) | set(new_posted_ids)
            merged_recent = []
            for w in remote_recent:
                if not isinstance(w, list):
                    continue
                try:
                    merged_recent.append(set(x for x in w if isinstance(x, str)))
                except TypeError:
                    continue
            for words in new_title_words_list:
                if words:
                    merged_recent.append(set(words))
            merged_recent = merged_recent[-RECENT_TITLES_LIMIT:]

            merged_last_run = remote_last_run
            if new_last_publish and new_last_publish > remote_last_run.get("last_publish", 0):
                merged_last_run = {"last_publish": new_last_publish}

            local_milestone = load_last_milestone()
            merged_milestones = {"last": max(remote_milestones.get("last", 0), local_milestone)}

            merged_digest = new_digest_state if new_digest_state is not None else remote_digest
            merged_poll = new_poll_state if new_poll_state is not None else remote_poll
            merged_reactions = {
                "enabled": bool(new_reactions_enabled) or bool(remote_reactions.get("enabled"))
            }

            reset = subprocess.run(["git", "reset", "--hard", "origin/main"], capture_output=True, text=True)
            if reset.returncode != 0:
                print(f"[WARN] git reset failed (attempt {attempt}): {reset.stderr}")
                continue

            save_posted(merged_posted)
            save_recent_title_words(merged_recent)
            with open(LAST_RUN_FILE, "w") as f:
                json.dump(merged_last_run, f)
            save_last_milestone(merged_milestones["last"])
            _save_json(DIGEST_STATE_FILE, merged_digest)
            _save_json(POLL_STATE_FILE, merged_poll)
            _save_json(REACTIONS_STATE_FILE, merged_reactions)

            subprocess.run(["git", "add", *STATE_FILES], check=False)
            diff = subprocess.run(["git", "diff", "--cached", "--quiet"])
            if diff.returncode == 0:
                print("[INFO] State files unchanged, nothing to commit.")
                return

            subprocess.run(["git", "commit", "-m", "chore: update bot state [skip ci]"], check=False)
            push = subprocess.run(["git", "push"], capture_output=True, text=True)
            if push.returncode == 0:
                print("[INFO] State files committed and pushed.")
                return
            backoff = min(2 * (2 ** (attempt - 1)), 30) + random.uniform(0, 1.5)
            print(f"[WARN] git push failed (attempt {attempt}/{MAX_PUSH_ATTEMPTS}), "
                  f"retrying in {backoff:.1f}s with fresh fetch: {push.stderr}")
            time.sleep(backoff)

        print(f"[WARN] Could not push state after {MAX_PUSH_ATTEMPTS} attempts — "
              f"next run may briefly re-see this item.")
        raise RuntimeError(f"Failed to persist bot state to git after {MAX_PUSH_ATTEMPTS} attempts")
    except Exception as e:
        print(f"[WARN] persist_state_to_git error: {e}")
        raise


def main():
    print(f"[START] {datetime.now().isoformat()}")

    dt_msk = now_msk()
    day_key = today_key(dt_msk)

    # --- Реакции: включаем один раз, статус запоминаем, чтобы не дёргать API зря ---
    reactions_state = _load_json(REACTIONS_STATE_FILE, {"enabled": False})
    new_reactions_enabled = None
    if not reactions_state.get("enabled"):
        if enable_reactions():
            new_reactions_enabled = True

    # --- Ежедневный вовлекающий опрос ---
    poll_state = _load_json(POLL_STATE_FILE, {"date": None, "sent": False})
    if poll_state.get("date") != day_key:
        poll_state = {"date": day_key, "sent": False}
    new_poll_state = None
    if due_poll(dt_msk, poll_state.get("sent")):
        if send_poll_to_telegram(POLL_QUESTION, POLL_OPTIONS):
            poll_state["sent"] = True
            new_poll_state = poll_state
            print("[INFO] Engagement poll sent.")

    count = get_subscriber_count()
    update_channel_description(count)
    maybe_celebrate_milestone(count)

    # --- Дайджест по расписанию (утро/вечер) ---
    digest_state = _load_json(DIGEST_STATE_FILE, {"date": None, "slots": []})
    if digest_state.get("date") != day_key:
        digest_state = {"date": day_key, "slots": []}
    new_digest_state = None

    slot = due_digest_slot(dt_msk, digest_state.get("slots", []))
    if slot:
        digest_items = fetch_news()
        digest_items.sort(key=lambda it: not it.get("urgent"))  # срочные — в начало
        digest_items = digest_items[:DIGEST_SIZE]

        if digest_items:
            finalized = [finalize_item(it) for it in digest_items]
            if slot == DIGEST_TIMES[0]:
                slot_label = "Утренний дайджест"
            elif len(DIGEST_TIMES) > 1 and slot == DIGEST_TIMES[1]:
                slot_label = "Вечерний дайджест"
            else:
                slot_label = "Дайджест"

            text = format_digest(finalized, slot_label)
            if send_to_telegram(text):
                posted = load_posted()
                new_posted_ids = []
                # ФИКС: раньше слова заголовков элементов дайджеста нигде не
                # сохранялись — из-за этого meaning-дедуп "не знал" про
                # новости, ушедшие в дайджест, и потом мог пропустить точно
                # такую же новость от другого канала как "новую".
                recent_words = load_recent_title_words()
                new_title_words_list = []
                for it in finalized:
                    posted.add(it["id"])
                    posted.add(it["title_key"])
                    new_posted_ids.extend([it["id"], it["title_key"]])
                    if it.get("content_words"):
                        recent_words.append(set(it["content_words"]))
                        new_title_words_list.append(it["content_words"])
                save_posted(posted)
                save_recent_title_words(recent_words)

                digest_state["slots"] = digest_state.get("slots", []) + [slot]
                new_digest_state = digest_state
                mark_published_now()

                persist_state_to_git(
                    new_posted_ids=new_posted_ids,
                    new_title_words_list=new_title_words_list,
                    new_last_publish=time.time(),
                    new_digest_state=new_digest_state,
                    new_poll_state=new_poll_state,
                    new_reactions_enabled=new_reactions_enabled,
                )
                print(f"[DONE] Digest sent ({slot}): {len(finalized)} items.")
                return
            else:
                print(f"[WARN] Digest send failed for slot {slot}, will retry next run.")
        else:
            digest_state["slots"] = digest_state.get("slots", []) + [slot]
            new_digest_state = digest_state
            print(f"[INFO] No candidates for digest slot {slot}, marking as done anyway.")

    # --- Обычный поток: одна новость за запуск, как раньше ---
    elapsed = seconds_since_last_publish()

    if elapsed is not None and elapsed < URGENT_INTERVAL:
        print(f"[INFO] Skipping run — с последней публикации прошло {int(elapsed)} сек "
              f"(меньше {URGENT_INTERVAL} сек), рано даже для срочной новости.")
        if new_digest_state or new_poll_state or new_reactions_enabled:
            persist_state_to_git(
                new_digest_state=new_digest_state,
                new_poll_state=new_poll_state,
                new_reactions_enabled=new_reactions_enabled,
            )
        return

    news = fetch_news()
    if not news:
        print("[INFO] No new news.")
        if new_digest_state or new_poll_state or new_reactions_enabled:
            persist_state_to_git(
                new_digest_state=new_digest_state,
                new_poll_state=new_poll_state,
                new_reactions_enabled=new_reactions_enabled,
            )
        return

    urgent_items = [it for it in news if it.get("urgent")]
    normal_items = [it for it in news if not it.get("urgent")]

    def prefer_video(items):
        with_video = [it for it in items if it.get("video")]
        return with_video if with_video else items

    if urgent_items:
        ordered = prefer_video(urgent_items)
    else:
        if elapsed is not None and elapsed < PUBLISH_INTERVAL:
            print(f"[INFO] Skipping run — с последней публикации прошло {int(elapsed)} сек "
                  f"(меньше {PUBLISH_INTERVAL} сек), срочных новостей нет.")
            if new_digest_state or new_poll_state or new_reactions_enabled:
                persist_state_to_git(
                    new_digest_state=new_digest_state,
                    new_poll_state=new_poll_state,
                    new_reactions_enabled=new_reactions_enabled,
                )
            return
        normal_items = prefer_video(normal_items)
        featured_idx = pick_featured_index(normal_items)
        # AI-выбранный кандидат идёт первым, остальные — запасные варианты
        ordered = [normal_items[featured_idx]] + [it for i, it in enumerate(normal_items) if i != featured_idx]

    # ФИКС: финальная проверка на дубликат прямо перед отправкой (см.
    # pick_non_duplicate) — на случай, если что-то очень похожее было
    # опубликовано (например, дайджестом) уже после того, как мы собрали
    # список кандидатов.
    chosen = pick_non_duplicate(ordered)
    if chosen is None:
        print("[INFO] All candidates turned out to be duplicates of already-posted news.")
        if new_digest_state or new_poll_state or new_reactions_enabled:
            persist_state_to_git(
                new_digest_state=new_digest_state,
                new_poll_state=new_poll_state,
                new_reactions_enabled=new_reactions_enabled,
            )
        return

    chosen = finalize_item(chosen)
    news = [chosen][:ITEMS_PER_RUN]

    posted = load_posted()
    sent_count = 0
    new_posted_ids = []
    new_title_words_list = []
    new_last_publish = None
    for i, item in enumerate(news):
        text = format_post(item)

        ok = send_post(item, text)
        if ok:
            posted.add(item["id"])
            posted.add(item["title_key"])
            save_posted(posted)
            new_posted_ids.extend([item["id"], item["title_key"]])
            if item.get("content_words"):
                recent_words = load_recent_title_words()
                recent_words.append(set(item["content_words"]))
                save_recent_title_words(recent_words)
                new_title_words_list.append(item["content_words"])
            sent_count += 1
            time.sleep(SEND_DELAY)
        else:
            print(f"[WARN] Failed to send item {item['id']} — will retry next run.")
            break

    if sent_count > 0:
        mark_published_now()
        new_last_publish = time.time()

    persist_state_to_git(
        new_posted_ids=new_posted_ids,
        new_title_words_list=new_title_words_list,
        new_last_publish=new_last_publish,
        new_digest_state=new_digest_state,
        new_poll_state=new_poll_state,
        new_reactions_enabled=new_reactions_enabled,
    )

    print(f"[DONE] Sent {sent_count}/{len(news)} items as separate posts.")


if __name__ == "__main__":
    main()
