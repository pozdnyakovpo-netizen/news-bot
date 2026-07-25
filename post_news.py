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

# Разные формулировки призыва поделиться — чтобы не приедалось при частой публикации
CTA_VARIANTS = [
    f"🚀 <a href=\"{SHARE_URL}\">Поделиться каналом с друзьями</a>",
    f"📣 <a href=\"{SHARE_URL}\">Расскажи друзьям про канал</a>",
    f"✉️ <a href=\"{SHARE_URL}\">Отправить другу</a>",
    f"🔥 <a href=\"{SHARE_URL}\">Знаешь, кому это будет интересно? Поделись</a>",
]

MILESTONES = [10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000]
MILESTONES_FILE = "milestones.json"

# Ключевые слова, по которым новость помечается как важная — влияет только на эмодзи, без текста.
# Список специально узкий: если помечать срочным каждую новость про "атаку" или "обстрел",
# метка перестаёт что-либо значить (как у каналов, где "🚨СРОЧНО🚨" стоит через пост).
URGENT_KEYWORDS = [
    "погиб", "убит", "жертв", "экстренн", "чрезвычайн", "эвакуац",
    "взрыв", "теракт", "катастроф", "введен режим чс",
]

# Эмодзи-метки для важных новостей — выбирается случайно, без подписи "СРОЧНО"
URGENT_EMOJIS = ["🔥", "🚨", "❗️", "⚡️"]

# Единый эмодзи-маркер канала — стоит в начале КАЖДОГО поста, всегда один и тот же.
# Со временем читатель узнаёт его в общей ленте подписок с первого взгляда,
# даже не читая название канала — это и есть "почерк" канала.
CHANNEL_MARK = "🔷"


def is_urgent(title, summary=""):
    t = (title + " " + summary).lower()
    return any(kw.lower() in t for kw in URGENT_KEYWORDS)


# Признаки "не новостного" контента: лайфхаки, списки советов, гороскопы и т.п.
# Такие статьи иногда попадают в общую RSS-ленту сайта вместе с настоящими новостями.
NOT_NEWS_PATTERNS = [
    "лайфхак", "топ-", "5 способов", "10 способов", "7 способов",
    "5 причин", "10 причин", "5 признаков", "10 признаков",
    "интересные факты", "полезные советы", "как выбрать", "как избавиться",
    "чем опасен", "чем опасны", "чем полезен", "чем полезны", "рецепт",
    "гороскоп", "приметы", "что будет если", "5 фактов", "10 фактов",
    "простые способы", "правила ухода", "как правильно",
    # лайфстайл/развлекательный контент — не новости в строгом смысле
    "интервью", "колонка", "личный опыт", "рассказала о себе", "рассказал о себе",
    "подкаст", "блог", "мнение:", "спросили у", "разбираем", "объясняем",
    "путеводитель", "подборка", "рейтинг", "рекомендуем", "что посмотреть",
    "что почитать", "что послушать", "тест-драйв", "обзор:",
    # сводки из нескольких новостей сразу — не годятся под формат "одна новость = один пост":
    # получаются рваные посты со списком через буллеты вместо связного текста
    "главные новости дня", "главные новости к этому часу", "итоги дня",
    "коротко о главном", "новости к этому часу", "главное к этому часу",
]

# Символы-маркеры списка, которыми РИА и похожие каналы оформляют сводки
# из нескольких новостей в одном посте — если таких маркеров 2 и больше,
# это точно не единичная новость, а дайджест, который не стоит публиковать как есть
DIGEST_BULLET_CHARS = ["◆", "▪", "‣", "🔹", "🔸"]


def is_digest_post(text):
    return any(text.count(ch) >= 2 for ch in DIGEST_BULLET_CHARS)


def is_not_news(title, summary=""):
    t = (title + " " + summary).lower()
    if any(p in t for p in NOT_NEWS_PATTERNS):
        return True
    if is_digest_post(title + " " + summary):
        return True
    return False


MAX_SENTENCES = 7  # максимум предложений в теле поста — только самое важное, без воды


def limit_sentences(text, max_sentences=MAX_SENTENCES):
    """Обрезает текст по границе предложения, а не посимвольно —
    чтобы пост не заканчивался на середине слова/мысли."""
    if not text:
        return text
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    sentences = [s for s in sentences if s]
    return " ".join(sentences[:max_sentences]).strip()


SOURCE_MENTION_PATTERNS = [
    r'https?://\S+',                                   # голые ссылки
    r'\bwww\.\S+',                                      # ссылки без схемы
    r'читать\s+(далее|полностью|на сайте)[^.!?]*[.!?]?',
    r'источник\s*:\s*\S+',
    r'подробнее\s+(на|в|у)\s+\S+',
    r'фото\s*:\s*\S+',
    r'видео\s*:\s*\S+',
    # чужая само-реклама источника: "Читайте нас в MAX/Дзен/VK", "Подписывайтесь на канал" и т.п. —
    # это реклама ИХ канала, не наша новость, не должна попадать в текст поста
    r'не\s+грузятся\s+фото\s+и\s+видео\?[^.!?]*[.!?]?',
    r'читайте\s+нас\s+(в|на)\s+\S+[^.!?]*[.!?]?',
    r'подпис(ывайтесь|ка)\s+на\s+(наш\s+)?канал[^.!?]*[.!?]?',
    r'скачайте?\s+(наше\s+)?приложение[^.!?]*[.!?]?',
]


def strip_source_mentions(text):
    """Убирает из текста ссылки и явные упоминания источника
    ('читать далее на...', 'источник: ...' и т.п.), чтобы в посте
    не оставалось следов, откуда взята новость."""
    if not text:
        return text
    for pattern in SOURCE_MENTION_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    text = re.sub(r'\s{2,}', ' ', text).strip()
    return text


def truncate_at_word(text, max_len=90):
    """Обрезает текст до max_len символов по границе слова (не разрывая слово
    пополам) и добавляет многоточие, если текст был обрезан."""
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
    """Ключ для отсева ТОЧНЫХ дублей (одна и та же RSS-запись): приводим
    к нижнему регистру, убираем пунктуацию и берём хэш."""
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
    """Ключевые слова заголовка (имена, числа, суть) без предлогов и мусора —
    для нечёткого сравнения: разные издания формулируют заголовок по-разному,
    но совпадающие имена/цифры/факты выдают одну и ту же новость."""
    t = title.lower()
    t = re.sub(r'[^a-zа-яё0-9\s]', ' ', t, flags=re.IGNORECASE)
    words = {w for w in t.split() if len(w) > 2 and w not in TITLE_STOPWORDS}
    return words


def titles_are_similar(words_a, words_b, threshold=0.5):
    """Похожи ли два заголовка по смыслу (пересечение ключевых слов).
    threshold=0.5 — половина значимых слов должна совпасть."""
    if not words_a or not words_b:
        return False
    union = words_a | words_b
    if not union:
        return False
    return (len(words_a & words_b) / len(union)) >= threshold


RECENT_TITLES_FILE = "recent_titles.json"
RECENT_TITLES_LIMIT = 300  # сколько последних заголовков храним для сравнения


def load_recent_title_words():
    return [set(words) for words in _load_json(RECENT_TITLES_FILE, [])]


def save_recent_title_words(list_of_word_sets):
    trimmed = list_of_word_sets[-RECENT_TITLES_LIMIT:]
    _save_json(RECENT_TITLES_FILE, [list(s) for s in trimmed])


def is_duplicate_by_meaning(words, recent_word_sets):
    return any(titles_are_similar(words, other) for other in recent_word_sets)


def seconds_since_last_publish():
    """Сколько секунд прошло с последней успешной публикации.
    Возвращает None, если публикаций ещё не было (можно публиковать сразу)."""
    last_publish = _load_json(LAST_RUN_FILE, {}).get("last_publish")
    return (time.time() - last_publish) if last_publish else None


def mark_published_now():
    _save_json(LAST_RUN_FILE, {"last_publish": time.time()})


# --- Перефразирование через GigaChat: жирный заголовок + текст, как у крупных СМИ-каналов ---
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
            # Лимит запросов в секунду — источников теперь больше (10 Telegram-каналов),
            # поэтому кандидатов на рерайт стало больше. Пауза подольше и явный лог,
            # чтобы не заливать логи повторяющейся ошибкой без объяснения.
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
    """Читает последние посты публичного Telegram-канала через t.me/s/<username> —
    это официальная веб-версия предпросмотра канала, доступная без токена бота
    и без подписки. Возвращает список записей в формате, совместимом с остальным
    пайплайном (id/title/summary/link/photo/video/published)."""
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
            continue  # пост без текста (стикер/чистое медиа) — нечего рерайтить, пропускаем

        first_line = raw_text.split("\n")[0].strip()
        title = first_line[:200] if first_line else raw_text[:200]

        # Для тела убираем ЛЮБЫЕ переносы строк (не только двойные) — иначе из-за
        # <br> внутри исходного поста текст выходит рваным: обрывки фраз на разных
        # строках вместо связного абзаца.
        text = re.sub(r'\s+', ' ', raw_text).strip()

        photo = None
        photo_bytes = None
        photo_el = msg.select_one(".tgme_widget_message_photo_wrap")
        if photo_el and photo_el.get("style"):
            m = re.search(r"url\('([^']+)'\)", photo_el["style"])
            if m:
                photo = m.group(1)
                # CDN Telegram часто блокирует скачивание без Referer, указывающего
                # на страницу канала — берём сразу здесь, пока ссылка точно рабочая,
                # чтобы не пытаться скачать её повторно позже (может уже не открыться).
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
            "source_channel": username,  # флаг: этот пункт пришёл из Telegram, а не из RSS
        })

    return entries[::-1]  # новые посты в начало списка


# --- Основной сбор ---
def fetch_news():
    posted = load_posted()
    recent_title_words = load_recent_title_words()  # заголовки за последние ~300 публикаций — для нечёткого сравнения
    new_items = []
    seen_title_keys = set()   # точные дубли в ЭТОМ запуске
    seen_title_words = []     # смысловые дубли в ЭТОМ запуске (разные формулировки одной новости)

    if not os.path.exists(FEEDS_FILE):
        print("[ERROR] feeds.txt not found!")
        return []

    with open(FEEDS_FILE, "r") as f:
        # feeds.txt теперь содержит юзернеймы Telegram-каналов (без @), по одному на строку —
        # так бот читает именно те 10 каналов, что были отобраны, а не произвольные RSS-ленты.
        channels = [line.strip().lstrip("@") for line in f if line.strip() and not line.startswith("#")]

    random.shuffle(channels)  # разный порядок каждый запуск — не даём первым каналам "съедать" весь лимит

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
                continue  # та же новость уже публиковалась (или уже в очереди) под другим источником/ссылкой

            title_words = significant_title_words(title)
            if is_duplicate_by_meaning(title_words, recent_title_words) or \
               any(titles_are_similar(title_words, w) for w in seen_title_words):
                continue  # та же новость, но другими словами (другой канал) — тоже пропускаем

            raw_summary = entry.get("summary", entry.get("description", ""))
            # html.unescape убирает &nbsp; и другие HTML-сущности, которые иначе
            # попадали в пост как есть буквами (видно было на скриншоте канала)
            summary = strip_source_mentions(html.unescape(re.sub(r"<[^>]+>", "", raw_summary)))
            link = entry.get("link", "")

            if is_not_news(title, summary):
                continue  # лайфхак/список советов/интервью/колонка — пропускаем, это не новость

            # фото/видео уже извлечены прямо со страницы Telegram-канала (fetch_telegram_channel)
            photo = entry.get("photo")
            photo_bytes = entry.get("photo_bytes")
            video = entry.get("video")
            if not photo and not video:
                continue  # без фото и видео не публикуем — оставляем только визуально насыщенные посты

            rewritten = rewrite_with_ai(title, summary)
            if rewritten:
                headline = html.escape(rewritten["headline"])
                body = html.escape(limit_sentences(strip_source_mentions(rewritten["body"])))
            else:
                # Без AI-рерайта берём заголовок из первого ПРЕДЛОЖЕНИЯ, а тело —
                # из оставшихся: раньше заголовок был просто обрезкой первых 90 символов
                # текста, а тело — тем же текстом целиком, из-за чего пост дублировал
                # сам себя (заголовок обрывался "…" и тут же повторялся в теле).
                source_text = summary if summary else title
                sentences = [s for s in re.split(r'(?<=[.!?])\s+', source_text.strip()) if s]
                headline = html.escape(truncate_at_word(sentences[0], 90)) if sentences else html.escape(truncate_at_word(title, 90))
                rest = " ".join(sentences[1:MAX_SENTENCES + 1])
                body = html.escape(rest) if rest else ""

            src = f"@{entry['source_channel']}" if entry.get("source_channel") else source_name(link)
            urgent = is_urgent(title, summary)
            print(f"[INFO] '{title[:50]}' ({src}) — photo={'yes' if photo else 'no'}, video={'yes' if video else 'no'}")

            new_items.append({
                "id": entry_id,
                "title_key": title_key,
                "title_words": list(title_words),
                "source": src,
                "urgent": urgent,
                "headline": headline,
                "body": body,
                "link": link,
                "photo": photo,
                "photo_bytes": photo_bytes,
                "video": video,
                "published": entry.get("published", datetime.now().isoformat())
            })
            seen_title_keys.add(title_key)
            seen_title_words.append(title_words)

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
    """Общая отправка фото/видео с тремя попытками по убыванию надёжности:
    1) уже скачанные байты (если есть — самый надёжный путь);
    2) отправка по ссылке (Telegram сам скачивает);
    3) скачиваем сами и шлём файлом (на случай, если CDN не даёт скачать без Referer)."""
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
    """Отправляет медиа из источника (если есть) + полный текст.
    Если текст помещается в подпись (≤1024 симв.) — идёт вместе с медиа одним сообщением.
    Если текст длиннее — медиа уходит отдельно (без подписи), следом полным сообщением текст,
    чтобы новость никогда не обрывалась."""
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
            # медиа с подписью не ушло — пробуем совсем без медиа
            return send_to_telegram(text)
        else:
            media_ok = sender(media_url)  # без подписи
            if media_ok:
                time.sleep(0.5)
            return send_to_telegram(text)

    return send_to_telegram(text)


# --- Текст одного поста в стиле крупных СМИ-каналов: моноширинное название + жирный заголовок + текст ---
def format_post(item, extra=""):
    lead = f"{random.choice(URGENT_EMOJIS)} " if item.get("urgent") else ""
    text = f"{CHANNEL_MARK} {lead}<b>{item['headline']}</b>"
    if item.get("body"):
        text += f"\n\n{item['body']}"
    if extra:
        text += f"\n\n{extra}"
    return text


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
    # Сухое, фактическое описание без "рекламного" тона и эмодзи-хайпа —
    # по тому же принципу, что у РБК/ТАСС: канал новостей, а не продающая страница.
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


def _git_show_json(ref_path, default):
    """Читает JSON-файл из указанной git-ссылки (например 'origin/main:posted.json'),
    не трогая рабочую копию. Возвращает default, если файла там нет или он битый."""
    import subprocess
    result = subprocess.run(["git", "show", ref_path], capture_output=True, text=True)
    if result.returncode != 0:
        return default
    try:
        return json.loads(result.stdout)
    except Exception:
        return default


def persist_state_to_git(new_posted_ids=None, new_title_words=None, new_last_publish=None):
    """Критично для GitHub Actions: раннер каждый раз стартует с чистого чек-аута
    репозитория, поэтому posted.json / recent_titles.json / last_run.json /
    milestones.json нужно закоммитить обратно — иначе на следующем запуске бот
    забывает всё, что уже публиковал, и начинает дублировать новости.

    Раньше слияние делалось через `git pull --rebase`, но это ТЕКСТОВОЕ слияние —
    оно регулярно ломалось на JSON-файлах, если два запуска (например, по расписанию
    каждые 2 минуты) пытались сохранить состояние почти одновременно. При конфликте
    бот просто не сохранял прогресс — а значит на следующем запуске уже опубликованная
    новость снова считалась "новой" и могла уйти повторно.

    Теперь вместо текстового merge — программное: перед записью забираем САМУЮ свежую
    версию файлов прямо с сервера (git fetch + git show) и добавляем к ней новые записи
    в Python (объединение множеств), а не полагаемся на то, что уже лежит в рабочей копии
    с начала запуска. Конфликтов при таком подходе быть не может в принципе.
    Работает молча, если запущено не в GitHub Actions (например, локально/на сервере)."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    import subprocess

    new_posted_ids = new_posted_ids or []
    new_title_words = new_title_words or []

    try:
        subprocess.run(["git", "config", "user.name", "news-bot"], check=False)
        subprocess.run(["git", "config", "user.email", "news-bot@users.noreply.github.com"], check=False)

        for attempt in range(1, 4):
            fetch = subprocess.run(["git", "fetch", "origin", "main"], capture_output=True, text=True)
            if fetch.returncode != 0:
                print(f"[WARN] git fetch failed (attempt {attempt}): {fetch.stderr}")

            remote_posted = _git_show_json(f"origin/main:{POSTED_FILE}", [])
            remote_recent = _git_show_json(f"origin/main:{RECENT_TITLES_FILE}", [])
            remote_last_run = _git_show_json(f"origin/main:{LAST_RUN_FILE}", {})
            remote_milestones = _git_show_json(f"origin/main:{MILESTONES_FILE}", {"last": 0})

            merged_posted = set(remote_posted) | set(new_posted_ids)
            merged_recent = [set(w) for w in remote_recent]
            if new_title_words:
                merged_recent.append(set(new_title_words))
            merged_recent = merged_recent[-RECENT_TITLES_LIMIT:]

            merged_last_run = remote_last_run
            if new_last_publish and new_last_publish > remote_last_run.get("last_publish", 0):
                merged_last_run = {"last_publish": new_last_publish}

            local_milestone = load_last_milestone()
            merged_milestones = {"last": max(remote_milestones.get("last", 0), local_milestone)}

            # Синхронизируем локальную ветку с самой свежей версией на сервере,
            # чтобы коммит лёг ровно поверх неё (без расхождений и конфликтов).
            reset = subprocess.run(["git", "reset", "--hard", "origin/main"], capture_output=True, text=True)
            if reset.returncode != 0:
                print(f"[WARN] git reset failed (attempt {attempt}): {reset.stderr}")
                continue

            save_posted(merged_posted)
            save_recent_title_words(merged_recent)
            with open(LAST_RUN_FILE, "w") as f:
                json.dump(merged_last_run, f)
            save_last_milestone(merged_milestones["last"])

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
            print(f"[WARN] git push failed (attempt {attempt}/3), retrying with fresh fetch: {push.stderr}")

        print("[WARN] Could not push state after 3 attempts — next run may briefly re-see this item.")
    except Exception as e:
        print(f"[WARN] persist_state_to_git error: {e}")


# --- Главная ---
def main():
    print(f"[START] {datetime.now().isoformat()}")

    elapsed = seconds_since_last_publish()

    # Даже срочная новость не публикуется чаще, чем раз в URGENT_INTERVAL —
    # это защита от флуда в Telegram, а не искусственная задержка.
    if elapsed is not None and elapsed < URGENT_INTERVAL:
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

    # срочные новости — в безусловном приоритете; среди обычных AI выбирает самую важную/интересную
    urgent_items = [it for it in news if it.get("urgent")]
    normal_items = [it for it in news if not it.get("urgent")]

    def prefer_video(items):
        """Если среди кандидатов есть новости с видео — выбираем только из них
        (видео заметнее фото); иначе берём весь список как есть."""
        with_video = [it for it in items if it.get("video")]
        return with_video if with_video else items

    if urgent_items:
        chosen = prefer_video(urgent_items)[0]
    else:
        # обычные новости всё равно ждут полный десятиминутный интервал
        if elapsed is not None and elapsed < PUBLISH_INTERVAL:
            print(f"[INFO] Skipping run — с последней публикации прошло {int(elapsed)} сек "
                  f"(меньше {PUBLISH_INTERVAL} сек), срочных новостей нет.")
            return
        normal_items = prefer_video(normal_items)
        featured_idx = pick_featured_index(normal_items)
        chosen = normal_items[featured_idx]

    news = [chosen][:ITEMS_PER_RUN]  # публикуем только самую важную — 1 новость за запуск

    posted = load_posted()
    sent_count = 0
    new_posted_ids = []
    new_title_words = None
    new_last_publish = None
    for i, item in enumerate(news):
        text = format_post(item)

        ok = send_post(item, text)
        if ok:
            # помечаем как отправленное СРАЗУ — если следующий пост не уйдёт,
            # уже опубликованные не задвоятся при повторном запуске
            posted.add(item["id"])
            posted.add(item["title_key"])  # чтобы та же новость с другого источника не прошла повторно
            save_posted(posted)
            new_posted_ids.extend([item["id"], item["title_key"]])
            if item.get("title_words"):
                recent_words = load_recent_title_words()
                recent_words.append(set(item["title_words"]))
                save_recent_title_words(recent_words)
                new_title_words = item["title_words"]
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
        new_title_words=new_title_words,
        new_last_publish=new_last_publish,
    )

    print(f"[DONE] Sent {sent_count}/{len(news)} items as separate posts.")


if __name__ == "__main__":
    main()
