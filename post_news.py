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
LAST_RUN_FILE = "last_run.json"    # хранит время последней успешной публикации
PUBLISH_INTERVAL = 600             # сек, интервал между ОБЫЧНЫМИ публикациями (10 минут)
URGENT_INTERVAL = 120              # сек, интервал между СРОЧНЫМИ публикациями (2 минуты)
ITEMS_PER_RUN = 1                  # сколько реально публикуем за один допустимый запуск
FETCH_POOL_SIZE = 12               # сколько кандидатов собрать перед выбором самой важной новости
FETCH_TIMEOUT = 15                 # сек, таймаут на загрузку страницы канала
AI_CALL_DELAY = 1.5                # сек, пауза между вызовами GigaChat (анти-рейтлимит — увеличена из-за роста числа источников)
SEND_DELAY = 1.5                   # сек, пауза между отправками отдельных постов (анти-флуд)

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


# --- Инициализация GigaChat (получение access_token по OAuth) ---
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
    "что", "все", "уже", "также", "будет", "были", "есть", "может", "самый", "или",
}

# Грубый стеммер для русских слов: отбрасываем типичные падежные/родовые окончания,
# чтобы "Спенс" / "Спенса" / "Спенсом" считались ОДНИМ словом при сравнении заголовков.
_RU_SUFFIXES = sorted([
    "иями", "ями", "ами", "его", "ому", "ему", "ыми", "ими",
    "ой", "ей", "ом", "ем", "ов", "ев", "ий", "ый", "ая", "яя",
    "ое", "ее", "их", "ых", "ам", "ям", "ах", "ях",
    "у", "ю", "е", "и", "ы", "а", "я", "й", "ь",
], key=len, reverse=True)


def _stem_ru(word):
    if len(word) <= 4 or not re.match(r'^[а-яё]+$', word):
        return word
    for suf in _RU_SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= 3:
            return word[:-len(suf)]
    return word


def significant_words(text):
    t = text.lower()
    t = re.sub(r'[^a-zа-яё0-9\s]', ' ', t, flags=re.IGNORECASE)
    return {_stem_ru(w) for w in t.split() if len(w) > 2 and w not in TITLE_STOPWORDS}


def proper_noun_stems(text):
    words = re.findall(r'\b[А-ЯЁ][а-яё]+\b', text)
    return {_stem_ru(w.lower()) for w in words if len(w) > 2}


def build_fingerprint(title, summary=""):
    combined = f"{title} {summary}".strip()
    return {
        "words": significant_words(combined),
        "names": proper_noun_stems(combined),
    }


def fingerprints_match(fp_a, fp_b, word_threshold=0.35, min_shared_names=2):
    wa, wb = fp_a["words"], fp_b["words"]
    union = wa | wb
    jaccard = (len(wa & wb) / len(union)) if union else 0.0
    shared_names = len(fp_a["names"] & fp_b["names"])
    return (jaccard >= word_threshold) or (shared_names >= min_shared_names)


RECENT_TITLES_FILE = "recent_titles.json"
RECENT_TITLES_LIMIT = 300  # сколько последних заголовков храним для сравнения


def load_recent_fingerprints():
    raw = _load_json(RECENT_TITLES_FILE, [])
    result = []
    for entry in raw:
        if isinstance(entry, dict):
            result.append({"words": set(entry.get("words", [])), "names": set(entry.get("names", []))})
        elif isinstance(entry, list) and len(entry) == 2 and all(isinstance(x, list) for x in entry):
            result.append({"words": set(entry[0]), "names": set(entry[1])})
        elif isinstance(entry, list):
            result.append({"words": set(entry), "names": set()})
    return result


def save_recent_fingerprints(fingerprints):
    trimmed = fingerprints[-RECENT_TITLES_LIMIT:]
    _save_json(RECENT_TITLES_FILE, [[list(fp["words"]), list(fp["names"])] for fp in trimmed])


def is_duplicate_by_meaning(fp, recent_fingerprints):
    return any(fingerprints_match(fp, other) for other in recent_fingerprints)


def seconds_since_last_publish(last_run_data=None):
    if last_run_data is None:
        last_run_data = _load_json(LAST_RUN_FILE, {})
    last_publish = last_run_data.get("last_publish")
    return (time.time() - last_publish) if last_publish else None


def mark_published_now():
    _save_json(LAST_RUN_FILE, {"last_publish": time.time()})


# --- Перефразирование через GigaChat: жирный заголовок + текст ---
def rewrite_with_ai(title, summary):
    token = get_gigachat_token()
    if not token:
        return None
    try:
        prompt = f"""Ты — редактор новостного Telegram-канала уровня РБК. Сделай из новости пост в 2 частях.

1) ЗАГОЛОВОК — короткий, конкретный, без кавычек и точки в конце (5–9 слов)
2) ТЕКСТ — СТРОГО не более 7 предложений, живым языком, без канцелярита. Только самое важное: что произошло, кто участвует, ключевые цифры/факты и главное последствие. Без второстепенных деталей, без предыстории и лишних подробностей — только суть.

ВАЖНО: если в исходной новости событие описано как предположение, план или условие ("может быть", "планируется", "по данным источника", "предположительно") — сохрани эту неопределённость и в заголовке, и в тексте. Не выдавай предположение или чьё-то заявление за свершившийся факт.

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


# --- Основной сбор ---
def fetch_news():
    posted = load_posted()
    recent_fingerprints = load_recent_fingerprints()
    new_items = []
    seen_title_keys = set()
    seen_fingerprints = []

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

            fp = build_fingerprint(title, summary)
            if is_duplicate_by_meaning(fp, recent_fingerprints) or \
               any(fingerprints_match(fp, other) for other in seen_fingerprints):
                continue

            link = entry.get("link", "")

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
                "fingerprint": fp,
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
            seen_fingerprints.append(fp)

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


STATE_FILES = [POSTED_FILE, RECENT_TITLES_FILE, LAST_RUN_FILE, MILESTONES_FILE]


def _in_github_actions():
    return os.environ.get("GITHUB_ACTIONS") == "true"


def _git_show_json(ref_path, default):
    import subprocess
    result = subprocess.run(["git", "show", ref_path], capture_output=True, text=True)
    if result.returncode != 0:
        return default
    try:
        return json.loads(result.stdout)
    except Exception:
        return default


def check_interval_from_remote(min_interval):
    if _in_github_actions():
        import subprocess
        subprocess.run(["git", "fetch", "origin", "main"], capture_output=True, text=True)
        remote_last_run = _git_show_json(f"origin/main:{LAST_RUN_FILE}", {})
    else:
        remote_last_run = _load_json(LAST_RUN_FILE, {})
    elapsed = seconds_since_last_publish(remote_last_run)
    ok = elapsed is None or elapsed >= min_interval
    return ok, elapsed


def claim_publish_slot():
    if not _in_github_actions():
        mark_published_now()
        return True

    import subprocess
    for attempt in range(1, 4):
        subprocess.run(["git", "fetch", "origin", "main"], capture_output=True, text=True)

        remote_last_run = _git_show_json(f"origin/main:{LAST_RUN_FILE}", {})
        elapsed = seconds_since_last_publish(remote_last_run)
        if elapsed is not None and elapsed < URGENT_INTERVAL:
            print(f"[INFO] Publish slot already taken ({int(elapsed)}s ago) — skipping to avoid duplicate.")
            return False

        reset = subprocess.run(["git", "reset", "--hard", "origin/main"], capture_output=True, text=True)
        if reset.returncode != 0:
            print(f"[WARN] git reset failed while claiming slot (attempt {attempt}): {reset.stderr}")
            continue

        with open(LAST_RUN_FILE, "w") as f:
            json.dump({"last_publish": time.time()}, f)
        subprocess.run(["git", "add", LAST_RUN_FILE], check=False)
        commit = subprocess.run(["git", "commit", "-m", "chore: reserve publish slot [skip ci]"], capture_output=True, text=True)
        if commit.returncode != 0:
            return True

        push = subprocess.run(["git", "push"], capture_output=True, text=True)
        if push.returncode == 0:
            return True
        print(f"[INFO] Lost publish-slot race (attempt {attempt}/3) — a parallel run claimed it first: {push.stderr.strip()[-200:]}")

    print("[WARN] Could not claim publish slot after 3 attempts — skipping this run to avoid a duplicate.")
    return False


def persist_state_to_git(new_posted_ids=None, new_fingerprint=None):
    if not _in_github_actions():
        return
    import subprocess

    new_posted_ids = new_posted_ids or []

    try:
        subprocess.run(["git", "config", "user.name", "news-bot"], check=False)
        subprocess.run(["git", "config", "user.email", "news-bot@users.noreply.github.com"], check=False)

        for attempt in range(1, 4):
            fetch = subprocess.run(["git", "fetch", "origin", "main"], capture_output=True, text=True)
            if fetch.returncode != 0:
                print(f"[WARN] git fetch failed (attempt {attempt}): {fetch.stderr}")

            remote_posted = _git_show_json(f"origin/main:{POSTED_FILE}", [])
            remote_recent_raw = _git_show_json(f"origin/main:{RECENT_TITLES_FILE}", [])
            remote_milestones = _git_show_json(f"origin/main:{MILESTONES_FILE}", {"last": 0})

            merged_posted = set(remote_posted) | set(new_posted_ids)

            merged_recent = []
            for entry in remote_recent_raw:
                if isinstance(entry, list) and len(entry) == 2 and all(isinstance(x, list) for x in entry):
                    merged_recent.append({"words": set(entry[0]), "names": set(entry[1])})
                elif isinstance(entry, list):
                    merged_recent.append({"words": set(entry), "names": set()})
            if new_fingerprint:
                merged_recent.append(new_fingerprint)
            merged_recent = merged_recent[-RECENT_TITLES_LIMIT:]

            local_milestone = load_last_milestone()
            merged_milestones = {"last": max(remote_milestones.get("last", 0), local_milestone)}

            reset = subprocess.run(["git", "reset", "--hard", "origin/main"], capture_output=True, text=True)
            if reset.returncode != 0:
                print(f"[WARN] git reset failed (attempt {attempt}): {reset.stderr}")
                continue

            save_posted(merged_posted)
            save_recent_fingerprints(merged_recent)
            save_last_milestone(merged_milestones["last"])

            subprocess.run(["git", "add", POSTED_FILE, RECENT_TITLES_FILE, MILESTONES_FILE], check=False)
            diff = subprocess.run(["git", "diff", "--cached", "--quiet"])
            if diff.returncode == 0:
                print("[INFO] State files unchanged, nothing to commit.")
                return

            subprocess.run(["git", "commit", "-m", "chore: update bot state [skip ci]"], check=False)
            push = subprocess.run(["git", "push"], capture_output=True, text=True)
            if push.returncode == 0:
                print("[INFO] State files committed and pushed.")
                return
            print(f"[WARN] git push failed (attempt {attempt}/3), retrying with fresh fetch: {push.stderr}")

        print("[WARN] Could not push state after 3 attempts — next run may briefly re-see this item.")
    except Exception as e:
        print(f"[WARN] persist_state_to_git error: {e}")


# --- Главная ---
def main():
    print(f"[START] {datetime.now().isoformat()}")

    ok, elapsed = check_interval_from_remote(URGENT_INTERVAL)
    if not ok:
        print(f"[INFO] Skipping run — с последней публикации прошло {int(elapsed)} сек "
              f"(меньше {URGENT_INTERVAL} сек), рано даже для срочной новости.")
        return

    count = get_subscriber_count()
    update_channel_description(count)
    maybe_celebrate_milestone(count)

    news = fetch_news()
    if not news:
        print("[INFO] No new news.")
        return

    urgent_items = [it for it in news if it.get("urgent")]
    normal_items = [it for it in news if not it.get("urgent")]

    def prefer_video(items):
        with_video = [it for it in items if it.get("video")]
        return with_video if with_video else items

    if urgent_items:
        chosen = prefer_video(urgent_items)[0]
        min_interval = URGENT_INTERVAL
    else:
        ok, elapsed = check_interval_from_remote(PUBLISH_INTERVAL)
        if not ok:
            print(f"[INFO] Skipping run — с последней публикации прошло {int(elapsed)} сек "
                  f"(меньше {PUBLISH_INTERVAL} сек), срочных новостей нет.")
            return
        normal_items = prefer_video(normal_items)
        featured_idx = pick_featured_index(normal_items)
        chosen = normal_items[featured_idx]
        min_interval = PUBLISH_INTERVAL

    ok, elapsed = check_interval_from_remote(min_interval)
    if not ok:
        print(f"[INFO] Skipping run — слот публикации только что занят параллельным запуском "
              f"({int(elapsed)} сек назад).")
        return
    if not claim_publish_slot():
        return

    chosen = finalize_item(chosen)
    news = [chosen][:ITEMS_PER_RUN]

    posted = load_posted()
    sent_count = 0
    new_posted_ids = []
    new_fingerprint = None
    for item in news:
        text = format_post(item)

        ok = send_post(item, text)
        if ok:
            posted.add(item["id"])
            posted.add(item["title_key"])
            save_posted(posted)
            new_posted_ids.extend([item["id"], item["title_key"]])
            if item.get("fingerprint"):
                recent = load_recent_fingerprints()
                recent.append(item["fingerprint"])
                save_recent_fingerprints(recent)
                new_fingerprint = item["fingerprint"]
            sent_count += 1
            time.sleep(SEND_DELAY)
        else:
            print(f"[WARN] Failed to send item {item['id']} — will retry next run.")
            break

    persist_state_to_git(
        new_posted_ids=new_posted_ids,
        new_fingerprint=new_fingerprint,
    )

    print(f"[DONE] Sent {sent_count}/{len(news)} items as separate posts.")


if __name__ == "__main__":
    main()
