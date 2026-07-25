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
LAST_RUN_FILE = "last_run.json"    # хранит время последней успешной публикации
PUBLISH_INTERVAL = 600             # сек, интервал между ОБЫЧНЫМИ публикациями (10 минут)
URGENT_INTERVAL = 120              # сек, интервал между СРОЧНЫМИ публикациями (2 минуты)
ITEMS_PER_RUN = 1                  # сколько реально публикуем за один допустимый запуск
FETCH_POOL_SIZE = 12               # сколько кандидатов собрать перед выбором самой важной новости
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

# Редкая подпись канала — добавляется примерно в 1 из 25 постов.
# Крупные новостные каналы (РБК, ТАСС) вообще не рекламируют себя в обычных постах —
# держим частоту минимальной, чтобы не выглядеть как "продающий" канал.
CHANNEL_SIGNATURE_CHANCE = 0.04
CHANNEL_SIGNATURE = f'📎 <a href="{CHANNEL_LINK}">@{CHANNEL_USERNAME}</a>'

# Ключевые слова, по которым новость помечается как важная — влияет только на эмодзи, без текста.
# Список специально узкий: если помечать срочным каждую новость про "атаку" или "обстрел",
# метка перестаёт что-либо значить (как у каналов, где "🚨СРОЧНО🚨" стоит через пост).
URGENT_KEYWORDS = [
    "погиб", "убит", "жертв", "экстренн", "чрезвычайн", "эвакуац",
    "взрыв", "теракт", "катастроф", "введен режим чс",
]

# Эмодзи-метки для важных новостей — выбирается случайно, без подписи "СРОЧНО"
URGENT_EMOJIS = ["🔥", "🚨", "❗️", "⚡️"]


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
]


def is_not_news(title, summary=""):
    t = (title + " " + summary).lower()
    return any(p in t for p in NOT_NEWS_PATTERNS)


def pick_category(title, summary=""):
    t = (title + " " + summary).lower()
    for keywords, emoji, label, hashtag in CATEGORY_RULES:
        if any(kw in t for kw in keywords):
            return emoji, label, hashtag
    return DEFAULT_EMOJI, DEFAULT_LABEL, DEFAULT_HASHTAG


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


# --- Извлечение фото/видео из RSS-записи (media RSS, enclosure, content, <img> в тексте) ---
def extract_media(entry, raw_summary=""):
    photo = None
    video = None

    for m in entry.get("media_content", []) or []:
        murl = m.get("url")
        mtype = (m.get("medium") or m.get("type") or "").lower()
        if not murl:
            continue
        if "video" in mtype or murl.lower().endswith((".mp4", ".mov", ".m4v")):
            video = video or murl
        elif "image" in mtype or murl.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            photo = photo or murl

    if not photo:
        thumbs = entry.get("media_thumbnail", []) or []
        if thumbs and thumbs[0].get("url"):
            photo = thumbs[0]["url"]

    for enc in entry.get("links", []) or []:
        if enc.get("rel") != "enclosure":
            continue
        eurl = enc.get("href")
        etype = (enc.get("type") or "").lower()
        if not eurl:
            continue
        if "video" in etype:
            video = video or eurl
        elif "image" in etype:
            photo = photo or eurl

    # некоторые фиды (WordPress и др.) кладут картинку в отдельное поле image
    if not photo:
        img_field = entry.get("image")
        if isinstance(img_field, dict) and img_field.get("href"):
            photo = img_field["href"]

    # полный текст статьи (content:encoded) часто содержит <img>, которого нет в summary
    html_sources = [raw_summary]
    for c in entry.get("content", []) or []:
        if c.get("value"):
            html_sources.append(c["value"])

    if not photo:
        for html_block in html_sources:
            if not html_block:
                continue
            img_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html_block)
            if img_match:
                photo = img_match.group(1)
                break

    if not video:
        for html_block in html_sources:
            if not html_block:
                continue
            vid_match = re.search(r'<source[^>]+src=["\']([^"\']+\.mp4[^"\']*)["\']', html_block)
            if vid_match:
                video = vid_match.group(1)
                break

    return photo, video


def fetch_page_media(link, timeout=6):
    """Резервный способ найти фото и видео: заходим на страницу статьи
    и ищем стандартную обложку (og:image) и реальное видео —
    через og:video или через JSON-LD разметку (contentUrl), которую
    такие сайты, как ТАСС и РИА, используют для SEO у видеоновостей.
    Один запрос к странице вместо двух отдельных."""
    photo, video = None, None
    if not link:
        return photo, video
    try:
        resp = requests.get(link, headers=HEADERS, timeout=timeout, verify=False)
        if resp.status_code != 200:
            return photo, video
        chunk = resp.text[:300000]

        img_match = re.search(
            r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            chunk, re.IGNORECASE
        )
        if not img_match:
            img_match = re.search(
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image["\']',
                chunk, re.IGNORECASE
            )
        if img_match:
            photo = img_match.group(1)

        # 1) og:video — прямая ссылка на файл, если сайт её публикует
        vid_match = re.search(
            r'<meta[^>]+(?:property|name)=["\']og:video(?::url)?["\'][^>]+content=["\']([^"\']+\.mp4[^"\']*)["\']',
            chunk, re.IGNORECASE
        )
        # 2) JSON-LD VideoObject: "contentUrl":"....mp4" — частый паттерн у ТАСС/РИА
        if not vid_match:
            vid_match = re.search(r'"contentUrl"\s*:\s*"([^"]+\.mp4[^"]*)"', chunk, re.IGNORECASE)
        # 3) прямой <video><source src="....mp4">
        if not vid_match:
            vid_match = re.search(r'<source[^>]+src=["\']([^"\']+\.mp4[^"\']*)["\']', chunk, re.IGNORECASE)
        if vid_match:
            video = vid_match.group(1).replace("\\/", "/")
    except Exception as e:
        print(f"[WARN] page media fetch failed for {link}: {e}")
    return photo, video


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


# --- Загрузка уже отправленных ID ---
def load_posted():
    if os.path.exists(POSTED_FILE):
        with open(POSTED_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_posted(posted_set):
    with open(POSTED_FILE, "w") as f:
        json.dump(list(posted_set), f)


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
    if os.path.exists(RECENT_TITLES_FILE):
        try:
            with open(RECENT_TITLES_FILE, "r") as f:
                data = json.load(f)
            return [set(words) for words in data]
        except Exception:
            return []
    return []


def save_recent_title_words(list_of_word_sets):
    trimmed = list_of_word_sets[-RECENT_TITLES_LIMIT:]
    with open(RECENT_TITLES_FILE, "w") as f:
        json.dump([list(s) for s in trimmed], f)


def is_duplicate_by_meaning(words, recent_word_sets):
    return any(titles_are_similar(words, other) for other in recent_word_sets)


def seconds_since_last_publish():
    """Сколько секунд прошло с последней успешной публикации.
    Возвращает None, если публикаций ещё не было (можно публиковать сразу)."""
    if not os.path.exists(LAST_RUN_FILE):
        return None
    try:
        with open(LAST_RUN_FILE, "r") as f:
            data = json.load(f)
        last_publish = data.get("last_publish")
        if not last_publish:
            return None
        return time.time() - last_publish
    except Exception:
        return None


def mark_published_now():
    with open(LAST_RUN_FILE, "w") as f:
        json.dump({"last_publish": time.time()}, f)


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
    recent_title_words = load_recent_title_words()  # заголовки за последние ~300 публикаций — для нечёткого сравнения
    new_items = []
    seen_title_keys = set()   # точные дубли в ЭТОМ запуске
    seen_title_words = []     # смысловые дубли в ЭТОМ запуске (разные формулировки одной новости)

    if not os.path.exists(FEEDS_FILE):
        print("[ERROR] feeds.txt not found!")
        return []

    with open(FEEDS_FILE, "r") as f:
        feed_urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    random.shuffle(feed_urls)  # разный порядок каждый запуск — не даём первым источникам "съедать" весь лимит

    for url in feed_urls:
        if len(new_items) >= FETCH_POOL_SIZE:
            break

        feed = fetch_feed_with_retry(url)
        if feed is None:
            continue

        for entry in feed.entries:
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
                continue  # та же новость, но другими словами (другое издание) — тоже пропускаем

            raw_summary = entry.get("summary", entry.get("description", ""))
            # html.unescape убирает &nbsp; и другие HTML-сущности, которые иначе
            # попадали в пост как есть буквами (видно было на скриншоте канала)
            summary = strip_source_mentions(html.unescape(re.sub(r"<[^>]+>", "", raw_summary)))
            link = entry.get("link", "")

            if is_not_news(title, summary):
                continue  # лайфхак/список советов/интервью/колонка — пропускаем, это не новость

            photo, video = extract_media(entry, raw_summary)
            if not video:
                # заходим на страницу статьи: ищем настоящее видео (og:video/JSON-LD),
                # а заодно и обложку, если её тоже не было в RSS — один запрос вместо двух
                page_photo, page_video = fetch_page_media(link)
                video = video or page_video
                photo = photo or page_photo
            if not photo and not video:
                continue  # без фото и видео не публикуем — оставляем только визуально насыщенные посты

            rewritten = rewrite_with_ai(title, summary)
            if rewritten:
                headline = html.escape(rewritten["headline"])
                body = html.escape(limit_sentences(strip_source_mentions(rewritten["body"])))
            else:
                headline = html.escape(truncate_at_word(title, 90))
                body = html.escape(limit_sentences(summary if summary else title))

            emoji, label, hashtag = pick_category(title, summary)
            src = source_name(link)
            urgent = is_urgent(title, summary)
            print(f"[INFO] '{title[:50]}' ({src}) — photo={'yes' if photo else 'no'}, video={'yes' if video else 'no'}")

            new_items.append({
                "id": entry_id,
                "title_key": title_key,
                "title_words": list(title_words),
                "emoji": emoji,
                "label": label,
                "hashtag": hashtag,
                "source": src,
                "urgent": urgent,
                "headline": headline,
                "body": body,
                "link": link,
                "photo": photo,
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


def send_photo_to_telegram(photo_url, caption=None):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    payload = {"chat_id": CHAT_ID, "photo": photo_url}
    if caption is not None:
        payload["caption"] = caption[:1024]  # Telegram: подпись к медиа ограничена 1024 символами
        payload["parse_mode"] = "HTML"
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            return True
        print(f"[WARN] sendPhoto failed: {resp.text}")
        return False
    except Exception as e:
        print(f"[WARN] sendPhoto error: {e}")
        return False


def send_video_to_telegram(video_url, caption=None):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendVideo"
    payload = {"chat_id": CHAT_ID, "video": video_url}
    if caption is not None:
        payload["caption"] = caption[:1024]
        payload["parse_mode"] = "HTML"
    try:
        resp = requests.post(url, json=payload, timeout=30)
        if resp.status_code == 200:
            return True
        print(f"[WARN] sendVideo failed: {resp.text}")
        return False
    except Exception as e:
        print(f"[WARN] sendVideo error: {e}")
        return False


def send_post(item, text):
    """Отправляет медиа из источника (если есть) + полный текст.
    Если текст помещается в подпись (≤1024 симв.) — идёт вместе с медиа одним сообщением.
    Если текст длиннее — медиа уходит отдельно (без подписи), следом полным сообщением текст,
    чтобы новость никогда не обрывалась."""
    media_url = item.get("video") or item.get("photo")
    is_video = bool(item.get("video"))

    if media_url:
        if len(text) <= 1024:
            sender = send_video_to_telegram if is_video else send_photo_to_telegram
            if sender(media_url, text):
                return True
            # медиа с подписью не ушло — пробуем совсем без медиа
            return send_to_telegram(text)
        else:
            sender = send_video_to_telegram if is_video else send_photo_to_telegram
            media_ok = sender(media_url)  # без подписи
            if media_ok:
                time.sleep(0.5)
            return send_to_telegram(text)

    return send_to_telegram(text)


# --- Текст одного поста в стиле крупных СМИ-каналов: моноширинное название + жирный заголовок + текст ---
def format_post(item, extra=""):
    lead = f"{random.choice(URGENT_EMOJIS)} " if item.get("urgent") else ""
    # <code> рендерится моноширинным шрифтом — визуально отличается от обычного
    # жирного текста заголовка/тела поста ниже, это и есть "другой шрифт" в рамках Telegram
    text = f"<code>ФАКТЫ ДНЯ</code>\n\n{lead}<b>{item['headline']}</b>\n\n{item['body']}"
    if random.random() < CHANNEL_SIGNATURE_CHANCE:
        text += f"\n\n{CHANNEL_SIGNATURE}"
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
    # Сухое, фактическое описание без "рекламного" тона и эмодзи-хайпа —
    # по тому же принципу, что у РБК/ТАСС: канал новостей, а не продающая страница.
    description = (
        f"Новостной дайджест. Одна главная новость за раз, без дублей.\n"
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


STATE_FILES = [POSTED_FILE, RECENT_TITLES_FILE, LAST_RUN_FILE, MILESTONES_FILE]


def persist_state_to_git():
    """Критично для GitHub Actions: раннер каждый раз стартует с чистого чек-аута
    репозитория, поэтому posted.json / recent_titles.json / last_run.json /
    milestones.json нужно закоммитить обратно — иначе на следующем запуске бот
    забывает всё, что уже публиковал, и начинает дублировать новости.
    Работает молча, если запущено не в GitHub Actions (например, локально/на сервере)."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    import subprocess
    try:
        existing = [f for f in STATE_FILES if os.path.exists(f)]
        if not existing:
            return
        subprocess.run(["git", "config", "user.name", "news-bot"], check=False)
        subprocess.run(["git", "config", "user.email", "news-bot@users.noreply.github.com"], check=False)
        subprocess.run(["git", "add", *existing], check=False)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if diff.returncode == 0:
            print("[INFO] State files unchanged, nothing to commit.")
            return
        subprocess.run(["git", "commit", "-m", "chore: update bot state [skip ci]"], check=False)

        # Подтягиваем свежие изменения (другие/предыдущие запуски могли успеть
        # запушить раньше нас) — без этого push будет отклонён как "rejected".
        pull = subprocess.run(
            ["git", "pull", "--rebase", "--autostash", "origin", "main"],
            capture_output=True, text=True
        )
        if pull.returncode != 0:
            print(f"[WARN] git pull --rebase failed, aborting rebase: {pull.stderr}")
            subprocess.run(["git", "rebase", "--abort"], check=False)
            return

        push = subprocess.run(["git", "push"], capture_output=True, text=True)
        if push.returncode != 0:
            print(f"[WARN] git push failed: {push.stderr}")
        else:
            print("[INFO] State files committed and pushed.")
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

    if urgent_items:
        chosen = urgent_items[0]
    else:
        # обычные новости всё равно ждут полный десятиминутный интервал
        if elapsed is not None and elapsed < PUBLISH_INTERVAL:
            print(f"[INFO] Skipping run — с последней публикации прошло {int(elapsed)} сек "
                  f"(меньше {PUBLISH_INTERVAL} сек), срочных новостей нет.")
            return
        featured_idx = pick_featured_index(normal_items)
        chosen = normal_items[featured_idx]

    news = [chosen][:ITEMS_PER_RUN]  # публикуем только самую важную — 1 новость за запуск

    posted = load_posted()
    sent_count = 0
    for i, item in enumerate(news):
        text = format_post(item)

        ok = send_post(item, text)
        if ok:
            # помечаем как отправленное СРАЗУ — если следующий пост не уйдёт,
            # уже опубликованные не задвоятся при повторном запуске
            posted.add(item["id"])
            posted.add(item["title_key"])  # чтобы та же новость с другого источника не прошла повторно
            save_posted(posted)
            if item.get("title_words"):
                recent_words = load_recent_title_words()
                recent_words.append(set(item["title_words"]))
                save_recent_title_words(recent_words)
            sent_count += 1
            time.sleep(SEND_DELAY)
        else:
            print(f"[WARN] Failed to send item {item['id']} — will retry next run.")
            break

    if sent_count > 0:
        mark_published_now()

    persist_state_to_git()

    print(f"[DONE] Sent {sent_count}/{len(news)} items as separate posts.")


if __name__ == "__main__":
    main()
