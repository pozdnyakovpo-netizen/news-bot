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


def request_with_retry(method, url, max_attempts=4, **kwargs):
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.request(method, url, **kwargs)
        except requests.exceptions.RequestException as e:
            last_exc = e
            if attempt == max_attempts:
                break
            backoff = min(1.5 * (2 ** (attempt - 1)), 15) + random.uniform(0, 0.5)
            print(f"[WARN] network error on {url} (attempt {attempt}/{max_attempts}): {e} — retry in {backoff:.1f}s")
            time.sleep(backoff)
            continue

        if resp.status_code == 429:
            retry_after = None
            try:
                retry_after = resp.json().get("parameters", {}).get("retry_after")
            except Exception:
                pass
            if retry_after is None:
                retry_after = int(resp.headers.get("Retry-After", 3))
            if attempt == max_attempts:
                return resp
            print(f"[WARN] 429 from {url}, waiting {retry_after}s as instructed (attempt {attempt}/{max_attempts})")
            time.sleep(retry_after + 0.5)
            continue

        return resp

    raise last_exc if last_exc else RuntimeError(f"request_with_retry exhausted attempts for {url}")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID")
SILENCE_ALERT_HOURS = 3
ALERT_STATE_FILE = "last_alert.json"
STATUS_FILE = "status.json"
GIGACHAT_AUTH_KEY = os.environ.get("GIGACHAT_AUTH_KEY")
GIGACHAT_SCOPE = os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
GIGACHAT_OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
GIGACHAT_CHAT_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
FEEDS_FILE = "feeds.txt"
POSTED_FILE = "posted.json"
LAST_RUN_FILE = "last_run.json"
PUBLISH_INTERVAL = 300  # ФИКС: было 600 (10 мин) — цель "раз в 5-10 минут"
                        # требует нижней границы диапазона как базовый
                        # интервал, а не верхней.
URGENT_INTERVAL = 120
# ФИКС (по прямому запросу "новости раз в 5-10 минут"): раньше не было
# никакого "аварийного" механизма — если строгий дедуп (по смыслу, по
# именным стемам, по мнению ИИ) отклонял ВСЕ кандидаты несколько циклов
# подряд, бот просто ждал следующего запуска без каких-либо гарантий, и
# реальный промежуток между постами мог растянуться на часы. Теперь если
# с последней публикации прошло больше этого времени, а строгий дедуп
# всё ещё ничего не пропускает, бот берёт первую ещё НЕ опубликованную
# новость, ослабив только смысловые проверки (по стему/смыслу/ИИ) — но
# не публикуя дважды буквально один и тот же пост (id/title_key всё
# равно исключаются). Это гарантирует частоту ценой редкого риска
# почти-дубля вместо часов тишины.
GUARANTEED_CADENCE_SECONDS = 600
ITEMS_PER_RUN = 1
FETCH_POOL_SIZE = 60
FETCH_TIMEOUT = 15
AI_CALL_DELAY = 1.5
SEND_DELAY = 1.5

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

MSK_OFFSET = timedelta(hours=3)
DIGEST_TIMES = ["08:00", "21:00"]
DIGEST_SIZE = 5
DIGEST_WINDOW_MINUTES = 4
DIGEST_STATE_FILE = "last_digest.json"

POLL_TIME = "12:00"
POLL_VARIANTS = [
    {"question": "Какая тема сейчас интереснее всего?",
     "options": ["Политика", "Происшествия", "Спорт", "Технологии", "Экономика"]},
    {"question": "Что для вас важнее всего в новостях?",
     "options": ["Скорость", "Точность фактов", "Без рекламы", "Разнообразие тем"]},
    {"question": "Как часто хотите видеть дайджест?",
     "options": ["Чаще", "Как сейчас — 2 раза в день", "Реже", "Только срочные новости"]},
    {"question": "Каких новостей хотелось бы больше?",
     "options": ["Происшествия", "Экономика", "Международные", "Всё устраивает"]},
]


def pick_poll_variant(day_key):
    digest = hashlib.md5(day_key.encode("utf-8")).hexdigest()
    idx = int(digest, 16) % len(POLL_VARIANTS)
    return POLL_VARIANTS[idx]


POLL_STATE_FILE = "last_poll.json"
# ФИКС (см. журнал изменений внизу файла): минимальный запас по времени
# между двумя отправками опроса/квиза/дайджеста/рекапа НЕЗАВИСИМО от
# булевых флагов "sent" в state-файлах. Это защита на случай, если
# git push состояния не удался (см. persist_state_to_git) — раньше
# именно это привело к тому, что один и тот же ежедневный опрос ушёл в
# канал дважды подряд (12:00 и 12:02), потому что второй запуск читал
# ещё не обновлённое состояние из origin/main.
MIN_POLL_GAP_SECONDS = 20 * 3600
MIN_QUIZ_GAP_SECONDS = 20 * 3600
MIN_DIGEST_GAP_SECONDS = 4 * 3600
MIN_WEEKLY_RECAP_GAP_SECONDS = 3 * 24 * 3600

URGENT_KEYWORDS = [
    "погиб", "убит", "жертв", "экстренн", "чрезвычайн", "эвакуац",
    "взрыв", "теракт", "катастроф", "введен режим чс",
]

URGENT_EMOJIS = ["🔥", "🚨", "❗️", "⚡️"]
CHANNEL_MARK = "🔷"

CATEGORY_RULES = [
    ("⚽️", "#спорт", [
        "футбол", "хокке", "теннис", "матч", "турнир", "чемпионат", "сборная",
        "тренер", "клуб", "олимпиад", "спортсмен", "чм-", "забил гол",
    ]),
    ("🎖", "#сво", [
        "спецоперац", "донбасс", "лнр", "днр", "запорожь", "херсонск",
        "линия фронта", "зсу", "вс рф", "мобилизац",
    ]),
    ("🚨", "#происшествия", [
        "погиб", "убит", "жертв", "дтп", "авари", "пожар", "взрыв", "теракт",
        "эвакуац", "чрезвычайн", "пострадал", "разыскива", "задержан", "суд",
    ]),
    ("💹", "#экономика", [
        "рубл", "доллар", "евро", "инфляц", "цб", "банк", "бюджет", "налог",
        "экспорт", "импорт", "санкц", "нефт", "газ", "акци", "биржа",
    ]),
    ("🏛", "#политика", [
        "путин", "госдума", "правительств", "министр", "закон", "указ",
        "переговор", "саммит", "президент", "депутат", "заседан",
    ]),
    ("🏙", "#москва", [
        "мэр москвы", "мэрия москвы", "департамент москвы", "подмосков",
        "метро москв", "мкад", "новая москва", "правительство москвы",
    ]),
    ("💻", "#технологии", [
        "ии-", "искусственн интеллект", "нейросет", "чат-бот", "чатgpt",
        "смартфон", "приложен", "стартап", "кибер", "робот", "гаджет",
        "разработчик",
    ]),
]


def detect_category(title, summary=""):
    t = (title + " " + summary).lower()
    for emoji, hashtag, keywords in CATEGORY_RULES:
        if any(kw in t for kw in keywords):
            return emoji, hashtag
    return CHANNEL_MARK, None


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


HEADLINE_MAX_LEN = 130


def truncate_at_word(text, max_len=HEADLINE_MAX_LEN):
    if not text or len(text) <= max_len:
        return text
    cut = text[:max_len]
    last_comma = cut.rfind(",")
    if last_comma >= max_len * 0.7:
        cut = cut[:last_comma]
    else:
        last_space = cut.rfind(" ")
        if last_space > 0:
            cut = cut[:last_space]
    return cut.rstrip(" ,.;:—-") + "…"


CLICHE_OPENER_PATTERNS = [
    r'^как\s+(сообщается|стало известно|уточняется|сообщают|отмечается)[,:]?\s*',
    r'^по\s+(имеющимся\s+)?данным(\s+источник[а-я]*)?[,:]?\s*',
    r'^по\s+словам\s+[^,]+[,:]?\s*',
    r'^стало\s+известно,?\s+что\s*',
    r'^напомним,?\s*',
    r'^следует\s+отметить,?\s+что\s*',
]


def strip_cliche_openers(text):
    if not text:
        return text
    result = text
    for pattern in CLICHE_OPENER_PATTERNS:
        result = re.sub(pattern, "", result, flags=re.IGNORECASE)
    if result:
        result = result[0].upper() + result[1:]
    return result.strip()


def fix_shouty_caps(text):
    if not text:
        return text
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return text
    upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
    if upper_ratio > 0.6:
        text = text.lower()
        text = text[0].upper() + text[1:] if text else text
    return text


_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\u2B00-\u2BFF\u2190-\u21FF\u2300-\u23FF"
    "]+",
    flags=re.UNICODE,
)


def strip_decorative_emoji(text):
    if not text:
        return text
    text = _EMOJI_PATTERN.sub("", text)
    return re.sub(r'\s{2,}', ' ', text).strip()


def collapse_repeated_punctuation(text):
    if not text:
        return text
    text = re.sub(r'!{2,}', '!', text)
    text = re.sub(r'\?{2,}', '?', text)
    text = re.sub(r'\.{4,}', '…', text)
    return text


def sanitize_text(text):
    text = strip_decorative_emoji(text)
    text = collapse_repeated_punctuation(text)
    text = re.sub(r'\s{2,}', ' ', text).strip()
    return text


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
        resp = request_with_retry("POST", GIGACHAT_OAUTH_URL, headers=headers, data=data, verify=False, timeout=(5, 15))
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
    words = set()
    for w in t.split():
        if len(w) <= 2 or w in TITLE_STOPWORDS:
            continue
        stem = w[:6] if len(w) > 6 else w
        words.add(stem)
    return words


def significant_words(text):
    return significant_title_words(text or "")


def content_words(title, summary):
    return significant_title_words(title) | significant_words(summary)


COMMON_ENTITY_STOPWORD_STEMS = {
    "росси", "москв", "украи", "путин", "минюс", "госдум", "кремл",
    "россия", "мчс", "мвд", "фсб", "цб",
}


def extract_entity_stems(text):
    if not text:
        return set()
    words = re.findall(r'[А-ЯЁ][а-яё]+', text)
    stems = set()
    for w in words:
        wl = w.lower()
        if len(wl) <= 3 or wl in TITLE_STOPWORDS:
            continue
        stem = wl[:6] if len(wl) > 6 else wl
        if stem in COMMON_ENTITY_STOPWORD_STEMS:
            continue
        stems.add(stem)
    return stems


# ФИКС (найдено при разборе жалобы "давно нет новостей" — реальный кейс:
# два парафраза одной новости "Отключение электроэнергии в Севастополе..."
# с ИДЕНТИЧНЫМИ именными стемами (алуште/севаст/крыма/...) не были
# признаны дублем и оба ушли в канал отдельными постами, а "прямой эфир"
# не смог их связать). Причина: min_count=5 / max_fraction=0.15 — слишком
# низкий порог для канала, где много новостей и так крутится вокруг
# Крыма/СВО/т.п. — специфичные для КОНКРЕТНОГО события имена (Севастополь,
# Крым) успевают набрать 5 упоминаний за счёт совершенно других историй и
# начинают ошибочно считаться "фоновым шумом", из-за чего пропадает сама
# возможность связать парафразы одной истории. Подняли оба порога — теперь
# исключаются только действительно вездесущие имена (Трамп и т.п.), а не
# любое достаточно популярное направление новостей.
def compute_common_entity_stems(recent_posts, min_count=10, max_fraction=0.35):
    if not recent_posts:
        return set()
    counter = {}
    for post in recent_posts:
        stems = extract_entity_stems(post.get("headline", "")) | extract_entity_stems(post.get("summary", ""))
        for stem in stems:
            counter[stem] = counter.get(stem, 0) + 1
    total = len(recent_posts)
    threshold = max(min_count, total * max_fraction)
    return {stem for stem, count in counter.items() if count >= threshold}


def titles_are_similar(words_a, words_b, threshold=0.5):
    if not words_a or not words_b:
        return False
    smaller = min(len(words_a), len(words_b))
    if smaller == 0:
        return False
    return (len(words_a & words_b) / smaller) >= threshold


def is_same_event(title_a, summary_a, title_b, summary_b, exclude_entities=None):
    words_a = content_words(title_a, summary_a)
    words_b = content_words(title_b, summary_b)
    if titles_are_similar(words_a, words_b, threshold=0.5):
        return True
    exclude_entities = exclude_entities or set()
    entities_a = (extract_entity_stems(title_a) | extract_entity_stems(summary_a)) - exclude_entities
    entities_b = (extract_entity_stems(title_b) | extract_entity_stems(summary_b)) - exclude_entities
    if entities_a & entities_b and titles_are_similar(words_a, words_b, threshold=0.15):
        return True
    return False


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


RECENT_POSTS_FILE = "recent_posts.json"
RECENT_POSTS_LIMIT = 300


def load_recent_posts():
    raw = _load_json(RECENT_POSTS_FILE, [])
    return [p for p in raw if isinstance(p, dict) and p.get("headline")]


def save_recent_posts(posts):
    trimmed = posts[-RECENT_POSTS_LIMIT:]
    _save_json(RECENT_POSTS_FILE, trimmed)


def is_duplicate_word_or_entity(candidate_title, candidate_summary, recent_posts, exclude_entities=None):
    for post in recent_posts:
        if is_same_event(candidate_title, candidate_summary,
                          post.get("headline", ""), post.get("summary", ""),
                          exclude_entities=exclude_entities):
            return True
    return False


STORY_CONTINUATION_WINDOW_HOURS = 48


def find_story_continuation(candidate_title, candidate_summary, recent_posts,
                             hours=STORY_CONTINUATION_WINDOW_HOURS, exclude_entities=None):
    exclude_entities = exclude_entities or set()
    c_entities = (extract_entity_stems(candidate_title) | extract_entity_stems(candidate_summary)) - exclude_entities
    if not c_entities:
        return None
    now = time.time()
    best = None
    for post in recent_posts:
        if not post.get("message_id"):
            continue
        ts = post.get("ts")
        if ts and now - ts > hours * 3600:
            continue
        p_entities = (extract_entity_stems(post.get("headline", "")) | extract_entity_stems(post.get("summary", ""))) - exclude_entities
        if c_entities & p_entities:
            best = post
    return best


LIVE_STORIES_FILE = "live_stories.json"
LIVE_STORY_MAX_AGE_HOURS = 12
LIVE_STORY_MAX_UPDATES = 15


def load_live_threads():
    raw = _load_json(LIVE_STORIES_FILE, [])
    return [t for t in raw if isinstance(t, dict) and t.get("message_id")]


def save_live_threads(threads):
    _save_json(LIVE_STORIES_FILE, threads)


def find_active_live_thread(candidate_title, candidate_summary, threads, exclude_entities=None):
    exclude_entities = exclude_entities or set()
    now = time.time()
    c_entities = (extract_entity_stems(candidate_title) | extract_entity_stems(candidate_summary)) - exclude_entities
    if not c_entities:
        return None
    for thread in threads:
        if now - thread.get("last_update_ts", 0) > LIVE_STORY_MAX_AGE_HOURS * 3600:
            continue
        t_entities = set(thread.get("entities", [])) - exclude_entities
        if c_entities & t_entities:
            return thread
    return None


def build_live_text(thread):
    lines = [f"🔴 <b>ПРЯМОЙ ЭФИР: {thread['headline']}</b>", ""]
    for update in thread.get("updates", [])[-LIVE_STORY_MAX_UPDATES:]:
        lines.append(f"⏱ {update['time']} — {update['text']}")
    lines.append("")
    lines.append("<i>Обновляется по мере поступления новых данных.</i>")
    return "\n".join(lines)


def pin_message(message_id):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/pinChatMessage"
    try:
        resp = request_with_retry("POST", url, json={
            "chat_id": CHAT_ID, "message_id": message_id, "disable_notification": True,
        }, timeout=10)
        return bool(resp.json().get("ok"))
    except Exception as e:
        print(f"[WARN] pinChatMessage error: {e}")
        return False


def unpin_message(message_id):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/unpinChatMessage"
    try:
        resp = request_with_retry("POST", url, json={"chat_id": CHAT_ID, "message_id": message_id}, timeout=10)
        return bool(resp.json().get("ok"))
    except Exception as e:
        print(f"[WARN] unpinChatMessage error: {e}")
        return False


def edit_message_text(message_id, text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText"
    try:
        resp = request_with_retry("POST", url, json={
            "chat_id": CHAT_ID, "message_id": message_id, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        }, timeout=10)
        data = resp.json()
        if not data.get("ok"):
            if "not modified" not in str(data.get("description", "")):
                print(f"[WARN] editMessageText failed: {data}")
            return "not modified" in str(data.get("description", ""))
        return True
    except Exception as e:
        print(f"[WARN] editMessageText error: {e}")
        return False


def start_live_thread(item, message_id):
    now = time.time()
    now_msk_time = (datetime.utcnow() + MSK_OFFSET).strftime("%H:%M")
    entities = list((extract_entity_stems(item["title"]) | extract_entity_stems(item.get("summary", ""))))
    return {
        "message_id": message_id,
        "headline": item.get("headline") or item.get("title", ""),
        "entities": entities,
        "started_ts": now,
        "last_update_ts": now,
        "updates": [{"time": now_msk_time, "text": item.get("body") or item.get("title", "")}],
    }


def append_live_update(thread, item):
    now_msk_time = (datetime.utcnow() + MSK_OFFSET).strftime("%H:%M")
    thread = dict(thread)
    thread["last_update_ts"] = time.time()
    thread["updates"] = thread.get("updates", []) + [{
        "time": now_msk_time,
        "text": item.get("body") or item.get("title", ""),
    }]
    new_entities = extract_entity_stems(item["title"]) | extract_entity_stems(item.get("summary", ""))
    thread["entities"] = list(set(thread.get("entities", [])) | new_entities)
    return thread


MAX_AI_DEDUPE_CHECKS = 5


# ФИКС: раньше сюда передавался recent_posts целиком (до 300 записей) без
# ограничения — огромный промпт с сотнями пунктов при max_tokens=10 на
# ответ повышает риск, что GigaChat не сможет корректно сопоставить или
# ответ обрежется непредсказуемо. Смысловая проверка на практике не
# требует всей истории — последних записей достаточно, чтобы поймать
# "то же событие, другими словами", а промпт остаётся компактным и
# надёжным.
AI_DEDUP_RECENT_POSTS_LIMIT = 30


def check_semantic_duplicate_via_ai(candidate_title, candidate_summary, recent_posts):
    token = get_gigachat_token()
    if not token or not recent_posts:
        return None
    recent_posts = recent_posts[-AI_DEDUP_RECENT_POSTS_LIMIT:]
    try:
        listing = "\n".join(
            f"{i}. {p.get('headline', '')} — {p.get('summary', '')[:150]}"
            for i, p in enumerate(recent_posts)
        )
        prompt = (
            "Вот новость-кандидат для публикации в новостном канале:\n"
            f"Заголовок: {candidate_title}\n"
            f"Текст: {(candidate_summary or '')[:300]}\n\n"
            "А вот пронумерованный список уже опубликованных в этом канале "
            "недавних постов:\n" + listing + "\n\n"
            "Описывает ли новость-кандидат ТО ЖЕ САМОЕ реальное событие, что "
            "и один из уже опубликованных постов — даже если сформулировано "
            "совершенно другими словами и с другим акцентом (например, один "
            "текст про эвакуацию тел погибших, а другой — про сам факт их "
            "гибели на том же месте: это одно и то же событие)? "
            "Ответь СТРОГО одним словом или числом: номер поста, если да, "
            "иначе слово 'нет'. Без пояснений."
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = {
            "model": "GigaChat",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 10,
        }
        resp = request_with_retry("POST", GIGACHAT_CHAT_URL, headers=headers, json=payload, verify=False, timeout=(5, 15))
        resp.raise_for_status()
        answer = resp.json()["choices"][0]["message"]["content"].strip().lower()
        if "нет" in answer and not re.search(r'\d', answer):
            return None
        match = re.search(r'\d+', answer)
        if match:
            idx = int(match.group())
            if 0 <= idx < len(recent_posts):
                return idx
        return None
    except Exception as e:
        print(f"[WARN] check_semantic_duplicate_via_ai error: {e}")
        return None


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
            "Ты — редактор новостного Telegram-канала уровня РБК. Сделай из новости пост в 3 частях.",
            "",
            "1) ЗАГОЛОВОК — короткий, конкретный, без кавычек и точки в конце. "
            "Старайся уложиться в 60-80 символов, но ГЛАВНОЕ ПРАВИЛО: заголовок "
            "должен быть законченной мыслью — никогда не обрывай его на середине "
            "слова или фразы. Законченность важнее длины: лучше на 20 символов "
            "длиннее, чем оборванный на полуслове.",
            "2) ТЕКСТ — СТРОГО не более 7 предложений, живым языком, без канцелярита. "
            "Только самое важное: что произошло, кто участвует, ключевые цифры/факты и "
            "главное последствие. Без второстепенных деталей, без предыстории и лишних "
            "подробностей — только суть. Одно предложение — одна мысль, без вложенных "
            "оборотов через запятую.",
            "3) КОНТЕКСТ — ОДНО короткое предложение (до 15 слов), объясняющее, "
            "почему это важно или что это значит на практике для читателя "
            "(например: сколько людей это затронет, это уже который случай за "
            "период, к чему это может привести). Это НЕ пересказ новости другими "
            "словами — если добавить нечего (нет очевидного практического "
            "следствия), напиши ровно слово 'нет', не выдумывай значимость.",
            "",
            "ВАЖНО: если в исходной новости событие описано как предположение, план или "
            "условие (может быть, планируется, по данным источника, предположительно) — "
            "сохрани эту неопределённость и в заголовке, и в тексте. Не выдавай "
            "предположение или чьё-то заявление за свершившийся факт.",
            "",
            "Не используй кликбейт-слова и штампы: 'шок', 'невероятно', 'вы не поверите', "
            "'сенсация', а также вводные канцеляризмы в начале текста вроде 'как сообщается', "
            "'как стало известно', 'по имеющимся данным' — начинай сразу с сути. "
            "Не добавляй свои эмодзи ни в заголовок, ни в текст — они не нужны, "
            "оформление уже добавляет их отдельно. Не пиши заголовок КАПСОМ. "
            "Сохраняй нейтральный тон агентства: не используй оценочные слова "
            "('ужасный', 'прекрасный', 'отвратительно', 'блестящий', 'катастрофический') "
            "и не выражай своё отношение к событию — только факты.",
            "",
            f"Заголовок исходной новости: {title}",
            f"Краткое содержание: {summary if summary else 'нет'}",
            "",
            "Ответь строго в формате:",
            "ЗАГОЛОВОК: <текст>",
            "ТЕКСТ: <текст>",
            "КОНТЕКСТ: <текст или 'нет'>",
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
        resp = request_with_retry("POST", GIGACHAT_CHAT_URL, headers=headers, json=payload, verify=False, timeout=(5, 20))
        if resp.status_code == 401:
            global _gigachat_token
            _gigachat_token = None
            token = get_gigachat_token()
            if not token:
                return None
            headers["Authorization"] = f"Bearer {token}"
            resp = request_with_retry("POST", GIGACHAT_CHAT_URL, headers=headers, json=payload, verify=False, timeout=(5, 20))

        if resp.status_code == 429:
            print("[WARN] GigaChat rate limit (429) — backing off.")
            time.sleep(3)
            return None

        resp.raise_for_status()
        data = resp.json()
        answer = data["choices"][0]["message"]["content"].strip()
        time.sleep(AI_CALL_DELAY)

        headline_match = re.search(r"ЗАГОЛОВОК:\s*(.+)", answer)
        body_match = re.search(r"ТЕКСТ:\s*(.+?)(?=\n\s*КОНТЕКСТ:|\Z)", answer, re.S)
        context_match = re.search(r"КОНТЕКСТ:\s*(.+)", answer)
        if headline_match and body_match:
            headline = headline_match.group(1).strip()
            body = body_match.group(1).strip()
            context_line = None
            if context_match:
                raw_context = context_match.group(1).strip().rstrip(".")
                if raw_context and raw_context.lower() not in ("нет", "нету", "-", "—"):
                    context_line = raw_context
            return {"headline": headline, "body": body, "context": context_line}
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

        raw_lines = [ln.strip() for ln in raw_text.split("\n") if ln.strip()]
        joined_parts = []
        for line in raw_lines:
            if joined_parts and not joined_parts[-1].endswith((".", "!", "?", ":", "…", ";")):
                joined_parts[-1] += "."
            joined_parts.append(line)
        text = re.sub(r'\s+', ' ', " ".join(joined_parts)).strip()

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
    seen_items_meta = []

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

            if len(significant_title_words(title)) < 3:
                continue

            raw_summary = entry.get("summary", entry.get("description", ""))
            summary = strip_source_mentions(html.unescape(re.sub(r"<[^>]+>", "", raw_summary)))
            link = entry.get("link", "")
            source_channel = entry.get("source_channel") or ""

            c_words = content_words(title, summary)
            if is_duplicate_by_meaning(c_words, recent_content_words):
                continue

            dup_meta = next((m for m in seen_items_meta if titles_are_similar(c_words, m["words"])), None)
            if dup_meta is not None:
                if dup_meta["source_channel"] != source_channel:
                    new_items[dup_meta["idx"]]["confirmed_multi_source"] = True
                continue

            if is_not_news(title, summary):
                continue

            # ФИКС (найдено при разборе жалобы "давно нет новостей"): раньше
            # ЛЮБАЯ новость без фото/видео отбрасывалась безусловно — в том
            # числе СРОЧНАЯ (is_urgent), хотя срочные алерты у источников
            # почти всегда текстовые (эвакуация/теракт/ЧС публикуются раньше,
            # чем появляется фото с места). Из-за этого бот мог полностью
            # "молчать" часами, если у всех источников в моменте не оказалось
            # ни одной новости с медиа, даже когда реальные срочные события
            # были. Срочные новости больше не требуют фото/видео.
            urgent = is_urgent(title, summary)
            photo = entry.get("photo")
            photo_bytes = entry.get("photo_bytes")
            video = entry.get("video")
            if not photo and not video and not urgent:
                continue

            src = f"@{source_channel}" if source_channel else source_name(link)
            print(f"[INFO] '{title[:50]}' ({src}) — photo={'yes' if photo else 'no'}, video={'yes' if video else 'no'}, urgent={urgent}")

            new_items.append({
                "id": entry_id,
                "title_key": title_key,
                "content_words": list(c_words),
                "source": src,
                "source_channel": source_channel,
                "urgent": urgent,
                "confirmed_multi_source": False,
                "title": title,
                "summary": summary,
                "link": link,
                "photo": photo,
                "photo_bytes": photo_bytes,
                "video": video,
                "published": entry.get("published", datetime.now().isoformat())
            })
            seen_title_keys.add(title_key)
            seen_items_meta.append({
                "words": c_words,
                "idx": len(new_items) - 1,
                "source_channel": source_channel,
            })

    return new_items


def split_long_sentences(text, max_words=25):
    if not text:
        return text
    sentences = [s for s in re.split(r'(?<=[.!?])\s+', text.strip()) if s]
    result = []
    for s in sentences:
        words = s.split()
        if len(words) <= max_words:
            result.append(s)
            continue
        comma_positions = [i for i, w in enumerate(words) if w.endswith(",")]
        if not comma_positions:
            result.append(s)
            continue
        mid = len(words) // 2
        split_at = min(comma_positions, key=lambda i: abs(i - mid))
        first = " ".join(words[:split_at + 1]).rstrip(",") + "."
        second = " ".join(words[split_at + 1:])
        if second:
            second = second[0].upper() + second[1:]
            result.append(first)
            result.append(second)
        else:
            result.append(s)
    return " ".join(result)


def paragraphize(text, sentences_per_para=2):
    if not text:
        return text
    sentences = [s for s in re.split(r'(?<=[.!?])\s+', text.strip()) if s]
    if len(sentences) <= sentences_per_para:
        return text
    paras = []
    for i in range(0, len(sentences), sentences_per_para):
        paras.append(" ".join(sentences[i:i + sentences_per_para]))
    return "\n\n".join(paras)


def finalize_item(item):
    rewritten = rewrite_with_ai(item["title"], item["summary"])
    if rewritten:
        headline_raw = rewritten["headline"]
        body_raw = limit_sentences(strip_source_mentions(rewritten["body"]))
    else:
        source_text = item["summary"] if item["summary"] else item["title"]
        sentences = [s for s in re.split(r'(?<=[.!?])\s+', source_text.strip()) if s]
        headline_raw = sentences[0] if sentences else item["title"]
        body_raw = " ".join(sentences[1:MAX_SENTENCES + 1])

    headline_raw = fix_shouty_caps(strip_cliche_openers(sanitize_text(headline_raw)))
    body_raw = split_long_sentences(strip_cliche_openers(sanitize_text(body_raw)))

    item["headline"] = html.escape(truncate_at_word(headline_raw)) if headline_raw else html.escape(truncate_at_word(item["title"]))

    if item.get("urgent"):
        urgent_body = limit_sentences(body_raw, max_sentences=2)
        item["body"] = html.escape(urgent_body) if urgent_body else ""
    else:
        item["body"] = html.escape(paragraphize(body_raw)) if body_raw else ""

    item["category"] = detect_category(item["title"], item.get("summary", ""))

    if item.get("source_channel") == TASS_CHANNEL:
        item["attribution"] = f"📡 {item.get('source', '')} — приоритетный источник"
    else:
        item["attribution"] = item.get("source") if item.get("source_channel") else None

    context_raw = rewritten.get("context") if rewritten else None
    if context_raw and not item.get("urgent"):
        context_raw = fix_shouty_caps(strip_cliche_openers(sanitize_text(context_raw)))
        item["context_line"] = html.escape(truncate_at_word(context_raw, 150)) if context_raw else None
    else:
        item["context_line"] = None

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
        resp = request_with_retry("POST", GIGACHAT_CHAT_URL, headers=headers, json=payload, verify=False, timeout=(5, 15))
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
    # ФИКС (диагностика): раньше два из четырёх шагов отбраковки ("уже
    # опубликовано" и "дубль по смыслу через recent_titles") не писали
    # НИЧЕГО в лог — из-за этого в реальном прогоне было видно "дубль по
    # именному стему" только для 2 из 60 кандидатов, а куда делись
    # остальные 58 — было совершенно непонятно. Теперь причина отсева
    # логируется на каждом шаге, плюс в конце — сводка по количеству
    # причин, чтобы сразу было видно, какой именно фильтр съедает кандидатов.
    posted_now = load_posted()
    recent_now = load_recent_title_words()
    recent_posts_now = load_recent_posts()
    common_entities = compute_common_entity_stems(recent_posts_now)
    ai_checks_used = 0
    reasons = {"already_posted": 0, "meaning_dup": 0, "entity_dup": 0, "ai_dup": 0}
    for it in items:
        title_short = it["title"][:50]
        if it["id"] in posted_now or it["title_key"] in posted_now:
            reasons["already_posted"] += 1
            print(f"[INFO] '{title_short}' — уже было опубликовано ранее, пропускаем.")
            continue
        cw = set(it.get("content_words") or [])
        if cw and is_duplicate_by_meaning(cw, recent_now):
            reasons["meaning_dup"] += 1
            print(f"[INFO] '{title_short}' — дубль по смыслу (пересечение слов с недавними "
                  f"заголовками), пропускаем.")
            continue
        if is_duplicate_word_or_entity(it["title"], it.get("summary", ""), recent_posts_now,
                                        exclude_entities=common_entities):
            reasons["entity_dup"] += 1
            print(f"[INFO] '{title_short}' — дубль по именному стему, пропускаем.")
            continue
        if ai_checks_used < MAX_AI_DEDUPE_CHECKS:
            ai_checks_used += 1
            dup_idx = check_semantic_duplicate_via_ai(it["title"], it.get("summary", ""), recent_posts_now)
            if dup_idx is not None:
                reasons["ai_dup"] += 1
                print(f"[INFO] GigaChat считает '{title_short}' тем же событием, "
                      f"что и недавний пост #{dup_idx} — пропускаем.")
                continue
        return it
    print(f"[INFO] pick_non_duplicate: все {len(items)} кандидатов отклонены — "
          f"уже опубликовано: {reasons['already_posted']}, дубль по смыслу: {reasons['meaning_dup']}, "
          f"дубль по стему: {reasons['entity_dup']}, дубль по мнению ИИ: {reasons['ai_dup']}.")
    return None


def pick_any_not_posted(items):
    # ФИКС ("гарантированная частота публикаций"): используется только
    # как аварийный fallback, когда строгий pick_non_duplicate() отклонил
    # ВСЁ, а с последней публикации прошло больше GUARANTEED_CADENCE_
    # SECONDS. Пропускаем смысловые/энтити/ИИ-проверки — оставляем только
    # защиту от публикации буквально одного и того же поста дважды
    # (id/title_key). Так канал не молчит часами из-за перестраховки
    # дедупа, ценой редкого риска почти-дубля, сформулированного другими
    # словами.
    posted_now = load_posted()
    for it in items:
        if it["id"] in posted_now or it["title_key"] in posted_now:
            continue
        print(f"[INFO] pick_any_not_posted: берём '{it['title'][:50]}' в аварийном режиме "
              f"(гарантия частоты публикаций), смысловые проверки ослаблены.")
        return it
    return None



def send_to_telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("[ERROR] Telegram credentials missing.")
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        resp = request_with_retry("POST", url, json=payload, timeout=10)
        if resp.status_code == 200:
            try:
                return resp.json().get("result", {}).get("message_id")
            except Exception:
                return True
        else:
            print(f"[ERROR] Telegram send failed: {resp.text}")
            return None
    except Exception as e:
        print(f"[ERROR] Telegram request error: {e}")
        return None


def _send_media_to_telegram(method, field, media_url, caption=None, media_bytes=None):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/send{method}"
    cap = {"caption": caption[:1024], "parse_mode": "HTML"} if caption is not None else {}
    filename = "photo.jpg" if field == "photo" else "video.mp4"

    def _extract_id(resp):
        try:
            return resp.json().get("result", {}).get("message_id")
        except Exception:
            return True

    if media_bytes:
        try:
            resp = request_with_retry("POST", url, data={"chat_id": CHAT_ID, **cap},
                                       files={field: (filename, media_bytes)}, timeout=30)
            if resp.status_code == 200:
                return _extract_id(resp)
            print(f"[WARN] send{method} by cached bytes failed: {resp.text}")
        except Exception as e:
            print(f"[WARN] send{method} by cached bytes error: {e}")

    try:
        resp = request_with_retry("POST", url, json={"chat_id": CHAT_ID, field: media_url, **cap}, timeout=15)
        if resp.status_code == 200:
            return _extract_id(resp)
        print(f"[WARN] send{method} by URL failed: {resp.text}")
    except Exception as e:
        print(f"[WARN] send{method} by URL error: {e}")

    try:
        dl = requests.get(media_url, headers=TELEGRAM_PREVIEW_HEADERS, timeout=20)
        dl.raise_for_status()
        resp = request_with_retry("POST", url, data={"chat_id": CHAT_ID, **cap},
                                   files={field: (filename, dl.content)}, timeout=30)
        if resp.status_code == 200:
            return _extract_id(resp)
        print(f"[WARN] send{method} by upload failed: {resp.text}")
        return None
    except Exception as e:
        print(f"[WARN] send{method} by upload error: {e}")
        return None


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
            msg_id = sender(media_url, text)
            if msg_id:
                return msg_id
            return send_to_telegram(text)
        else:
            media_ok = sender(media_url)
            if media_ok:
                time.sleep(0.5)
            return send_to_telegram(text)

    return send_to_telegram(text)


def format_post(item, extra=""):
    category_emoji, hashtag = item.get("category", (CHANNEL_MARK, None))
    if item.get("urgent"):
        mark = random.choice(URGENT_EMOJIS)
    else:
        mark = category_emoji
    text = f"{mark} <b>{item['headline']}</b>"

    continuation = item.get("continuation_of")
    if continuation and continuation.get("message_id"):
        link = f"https://t.me/{CHANNEL_USERNAME}/{continuation['message_id']}"
        text += f"\n🔄 <a href=\"{link}\">Продолжение истории</a>"

    if item.get("body"):
        text += f"\n\n{item['body']}"

    if item.get("context_line"):
        text += f"\n\n💡 {item['context_line']}"

    if item.get("confirmed_multi_source"):
        text += "\n\n✅ Подтверждено несколькими источниками"

    if extra:
        text += f"\n\n{extra}"
    if hashtag and not item.get("urgent"):
        text += f"\n\n{hashtag}"

    if item.get("attribution"):
        text += f"\n\n{html.escape(item['attribution'])}"

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


# --- Данные для публичного дашборда (см. build_dashboard_html ниже) ---
# Один сэмпл в день достаточен для графика роста подписчиков — не нужна
# поминутная точность, а хранить историю тогда компактно и просто:
# список {"date": "YYYY-MM-DD", "count": N}, один элемент на дату.
SUBSCRIBER_HISTORY_FILE = "subscriber_history.json"
SUBSCRIBER_HISTORY_LIMIT = 365


def load_subscriber_history():
    raw = _load_json(SUBSCRIBER_HISTORY_FILE, [])
    return [p for p in raw if isinstance(p, dict) and p.get("date") and isinstance(p.get("count"), (int, float))]


def save_subscriber_history(history):
    trimmed = history[-SUBSCRIBER_HISTORY_LIMIT:]
    _save_json(SUBSCRIBER_HISTORY_FILE, trimmed)


def merge_subscriber_history(history_a, history_b):
    # Мёржим по дате, оставляя последнее известное значение на дату
    # (порядок неважен — обе истории уже отсортированы по дате добавления).
    by_date = {}
    for entry in list(history_a) + list(history_b):
        by_date[entry["date"]] = entry["count"]
    merged = [{"date": d, "count": c} for d, c in by_date.items()]
    merged.sort(key=lambda e: e["date"])
    return merged[-SUBSCRIBER_HISTORY_LIMIT:]


def update_channel_description(count):
    if count is None or not TELEGRAM_TOKEN or not CHAT_ID:
        return
    description = (
        f"⚡️ Самое важное за день — коротко, без воды и рекламы. Один пост — "
        f"одна новость.\n"
        f"Источники: РИА, РБК, ТАСС, Readovka, Mash, Baza и ещё 15 проверенных СМИ.\n"
        f"🕗 Дайджесты в 08:00 и 21:00 мск.\n"
        f"{count} подписчиков"
    )[:255]
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setChatDescription"
    try:
        resp = request_with_retry("POST", url, json={"chat_id": CHAT_ID, "description": description}, timeout=10)
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


def send_admin_alert(text):
    if not TELEGRAM_TOKEN:
        return False
    if not ADMIN_CHAT_ID:
        print(f"[INFO] (admin alert, no ADMIN_CHAT_ID set): {text}")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        resp = request_with_retry("POST", url, json={"chat_id": ADMIN_CHAT_ID, "text": text}, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        print(f"[WARN] send_admin_alert error: {e}")
        return False


def check_silence_alert(elapsed):
    if elapsed is None or elapsed < SILENCE_ALERT_HOURS * 3600:
        return None
    alert_state = _load_json(ALERT_STATE_FILE, {"last_alert": 0})
    if time.time() - alert_state.get("last_alert", 0) < 24 * 3600:
        return None
    hours = elapsed / 3600
    if send_admin_alert(
        f"⚠️ Бот молчит уже {hours:.1f} ч. Проверьте фиды, GigaChat-квоту "
        f"и логи workflow — возможно, источники недоступны или упал токен."
    ):
        return time.time()
    return None


SPEED_STATS_FILE = "speed_stats.json"
SPEED_STATS_LIMIT = 200


def load_speed_stats():
    return _load_json(SPEED_STATS_FILE, [])


def save_speed_stats(samples):
    _save_json(SPEED_STATS_FILE, samples[-SPEED_STATS_LIMIT:])


def parse_source_published(published_str):
    if not published_str:
        return None
    try:
        s = published_str.replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def compute_publish_latency_seconds(source_published_str, publish_time=None):
    src_ts = parse_source_published(source_published_str)
    if src_ts is None:
        return None
    publish_time = publish_time if publish_time is not None else time.time()
    latency = publish_time - src_ts
    if latency < 0 or latency > 24 * 3600:
        return None
    return latency


def speed_stats_summary(samples):
    latencies = [s.get("latency_seconds") for s in samples if isinstance(s.get("latency_seconds"), (int, float))]
    if not latencies:
        return {"count": 0, "avg_minutes": None, "median_minutes": None}
    latencies_sorted = sorted(latencies)
    avg = sum(latencies) / len(latencies)
    mid = len(latencies_sorted) // 2
    if len(latencies_sorted) % 2:
        median = latencies_sorted[mid]
    else:
        median = (latencies_sorted[mid - 1] + latencies_sorted[mid]) / 2
    return {
        "count": len(latencies),
        "avg_minutes": round(avg / 60, 1),
        "median_minutes": round(median / 60, 1),
    }


def source_contribution_summary(recent_posts):
    counts = {}
    for post in recent_posts:
        src = post.get("source") or "неизвестно"
        counts[src] = counts.get(src, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))


PRIORITY_CATEGORY_TAGS = {
    "#политика", "#экономика", "#сво", "#москва", "#происшествия", "#спорт",
}
TASS_CHANNEL = "tass_agency"
TARGET_TASS_SHARE = 0.30
PRIORITY_CATEGORY_BONUS = 1.5


def compute_tass_weight_multiplier(recent_posts, min_sample=5):
    contribution = source_contribution_summary(recent_posts)
    total = sum(contribution.values())
    if total < min_sample:
        return 2.0
    tass_count = contribution.get(f"@{TASS_CHANNEL}", 0)
    current_share = tass_count / total
    if current_share >= TARGET_TASS_SHARE:
        return 1.0
    deficit = TARGET_TASS_SHARE - current_share
    return min(1.0 + deficit * 12, 5.0)


def compute_candidate_priority_weight(item, tass_multiplier):
    weight = 1.0
    _, cat_tag = detect_category(item.get("title", ""), item.get("summary", ""))
    if cat_tag in PRIORITY_CATEGORY_TAGS:
        weight += PRIORITY_CATEGORY_BONUS
    if item.get("source_channel") == TASS_CHANNEL:
        weight *= tass_multiplier
    return weight


def order_candidates_by_priority(items, recent_posts):
    if not items:
        return []
    tass_multiplier = compute_tass_weight_multiplier(recent_posts)
    weighted = [(compute_candidate_priority_weight(it, tass_multiplier), it) for it in items]
    weighted.sort(key=lambda pair: pair[0], reverse=True)
    return [it for _, it in weighted]


def build_status_snapshot(last_publish_elapsed, sent_count=None, note=""):
    speed = speed_stats_summary(load_speed_stats())
    return {
        "last_check": datetime.utcnow().isoformat(),
        "seconds_since_last_publish": last_publish_elapsed,
        "healthy": last_publish_elapsed is None or last_publish_elapsed < SILENCE_ALERT_HOURS * 3600,
        "sent_count_last_run": sent_count,
        "note": note,
        "avg_publish_latency_minutes": speed["avg_minutes"],
        "median_publish_latency_minutes": speed["median_minutes"],
        "speed_samples_count": speed["count"],
        "source_contribution": source_contribution_summary(load_recent_posts()),
    }


# ФИКС (найдено при разборе жалобы "новостей нет очень давно"): status.json
# (и публичный дашборд, который из него строится) раньше обновлялся ТОЛЬКО
# когда что-то реально произошло — новая публикация, опрос, квиз, дайджест
# или сводка рынков. Если ни одно из этого не сработало (самый частый
# случай — бот просто не нашёл неопубликованную новость), main() вообще не
# вызывал persist_with_status, и status.json застревал на данных из
# прошлого — из-за этого "Последняя публикация" на дашборде могла
# показывать одно и то же значение часами, создавая ложное впечатление,
# что бот молчит, хотя он исправно пытался каждые 5 минут. Теперь дашборд
# принудительно обновляется хотя бы раз в STATUS_REFRESH_MAX_AGE_SECONDS,
# даже если больше ничего не изменилось — коммит в git при этом всё равно
# не чаще, чем раз в это окно (не при каждом из ~288 прогонов в день).
STATUS_REFRESH_MAX_AGE_SECONDS = 30 * 60


def status_is_stale(max_age_seconds=STATUS_REFRESH_MAX_AGE_SECONDS):
    status = _load_json(STATUS_FILE, {})
    last_check = status.get("last_check")
    if not last_check:
        return True
    try:
        last_check_ts = datetime.fromisoformat(last_check).timestamp()
    except Exception:
        return True
    return (time.time() - last_check_ts) >= max_age_seconds


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
        resp = request_with_retry("POST", url, json=payload, timeout=10)
        data = resp.json()
        if not data.get("ok"):
            print(f"[WARN] sendPoll failed: {data}")
            return False
        return True
    except Exception as e:
        print(f"[WARN] sendPoll error: {e}")
        return False


QUIZ_TIME = "15:00"
QUIZ_WINDOW_MINUTES = DIGEST_WINDOW_MINUTES
QUIZ_STATE_FILE = "quiz_state.json"
QUIZ_LOOKBACK_HOURS = 24


def due_quiz(dt, already_sent_today):
    if already_sent_today:
        return False
    now_minutes = dt.hour * 60 + dt.minute
    slot_h, slot_m = map(int, QUIZ_TIME.split(":"))
    slot_minutes = slot_h * 60 + slot_m
    return 0 <= (now_minutes - slot_minutes) <= QUIZ_WINDOW_MINUTES


def pick_quiz_source_post(recent_posts, hours=QUIZ_LOOKBACK_HOURS):
    now = time.time()
    candidates = [
        p for p in recent_posts
        if (not p.get("ts") or now - p["ts"] <= hours * 3600)
        and len(p.get("summary", "")) >= 60
    ]
    return candidates[-1] if candidates else None


def build_quiz_from_post(post):
    token = get_gigachat_token()
    if not token or not post:
        return None
    try:
        prompt = (
            "Вот новость, которая уже была опубликована в новостном Telegram-канале:\n"
            f"Заголовок: {post.get('headline', '')}\n"
            f"Текст: {post.get('summary', '')[:400]}\n\n"
            "Составь по этой новости викторинный вопрос с 4 вариантами ответа "
            "(только на основе фактов из текста выше, ничего не выдумывай "
            "сверх того, что там написано). Один вариант верный, три — "
            "правдоподобные, но неверные. Плюс короткое объяснение "
            "(1 предложение, до 15 слов) правильного ответа.\n\n"
            "Ответь СТРОГО в формате JSON без пояснений и без markdown-разметки:\n"
            '{"question": "...", "options": ["...", "...", "...", "..."], '
            '"correct_index": 0, "explanation": "..."}'
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = {
            "model": "GigaChat",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.4,
            "max_tokens": 400,
        }
        resp = request_with_retry("POST", GIGACHAT_CHAT_URL, headers=headers, json=payload, verify=False, timeout=(5, 20))
        resp.raise_for_status()
        answer = resp.json()["choices"][0]["message"]["content"].strip()
        match = re.search(r'\{.*\}', answer, re.S)
        if not match:
            return None
        data = json.loads(match.group())
        question = str(data.get("question", "")).strip()
        options = [str(o).strip() for o in data.get("options", [])]
        correct_index = data.get("correct_index")
        explanation = str(data.get("explanation", "")).strip()
        if (not question or len(options) != 4 or not isinstance(correct_index, int)
                or not (0 <= correct_index < 4) or any(not o for o in options)):
            return None
        if len(question) > 300 or any(len(o) > 100 for o in options):
            return None
        return {
            "question": question,
            "options": options,
            "correct_index": correct_index,
            "explanation": explanation[:200],
        }
    except Exception as e:
        print(f"[WARN] build_quiz_from_post error: {e}")
        return None


def send_quiz_poll(quiz):
    if not TELEGRAM_TOKEN or not CHAT_ID or not quiz:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPoll"
    payload = {
        "chat_id": CHAT_ID,
        "question": quiz["question"],
        "options": quiz["options"],
        "type": "quiz",
        "correct_option_id": quiz["correct_index"],
        "is_anonymous": True,
    }
    if quiz.get("explanation"):
        payload["explanation"] = quiz["explanation"]
    try:
        resp = request_with_retry("POST", url, json=payload, timeout=10)
        data = resp.json()
        if not data.get("ok"):
            print(f"[WARN] sendQuizPoll failed: {data}")
            return False
        return True
    except Exception as e:
        print(f"[WARN] sendQuizPoll error: {e}")
        return False


# --- "Крутая" фича: утренняя сводка по рынкам ---
# Формат, который используют топовые деловые СМИ (РБК, Коммерсантъ, Frank
# Media) каждое утро: курсы валют + индекс биржи со стрелочками роста/
# падения к предыдущему дню. Данные берутся из ДВУХ официальных бесплатных
# источников без ключей API:
#   - ЦБ РФ (cbr.ru) — официальные курсы валют, публикуются раз в сутки;
#   - Мосбиржа (MOEX ISS API, iss.moex.com) — текущее значение индекса
#     IMOEX прямо с торгов, тоже открытый JSON без авторизации.
# Индекс МосБиржи — необязательная "вишенка": если формат ответа MOEX
# API вдруг изменится или сервис недоступен, сводка всё равно уйдёт с
# одними курсами валют, просто без строки по индексу — деградация
# плавная, а не отказ всей фичи.
MARKET_SNAPSHOT_TIME = "09:30"  # МСК, после утреннего дайджеста (08:00)
MARKET_WINDOW_MINUTES = DIGEST_WINDOW_MINUTES
MARKET_STATE_FILE = "market_state.json"
MIN_MARKET_GAP_SECONDS = 20 * 3600
CBR_RATES_URL = "https://www.cbr.ru/scripts/XML_daily.asp"
MOEX_IMOEX_URL = (
    "https://iss.moex.com/iss/engines/stock/markets/index/boards/SNDX/"
    "securities/IMOEX.json?iss.meta=off"
)
MARKET_CURRENCIES = [
    ("USD", "$", "Доллар"),
    ("EUR", "€", "Евро"),
    ("CNY", "¥", "Юань"),
]


def due_market_snapshot(dt, already_sent_today):
    if already_sent_today:
        return False
    now_minutes = dt.hour * 60 + dt.minute
    slot_h, slot_m = map(int, MARKET_SNAPSHOT_TIME.split(":"))
    slot_minutes = slot_h * 60 + slot_m
    return 0 <= (now_minutes - slot_minutes) <= MARKET_WINDOW_MINUTES


def fetch_cbr_rates():
    # Официальный ежедневный XML ЦБ РФ — без ключей, без авторизации.
    try:
        import xml.etree.ElementTree as ET
        resp = request_with_retry("GET", CBR_RATES_URL, timeout=(5, 15))
        resp.raise_for_status()
        resp.encoding = "windows-1251"
        root = ET.fromstring(resp.text)
        rates = {}
        for valute in root.findall("Valute"):
            char_code = valute.findtext("CharCode", "")
            if char_code not in {c for c, _, _ in MARKET_CURRENCIES}:
                continue
            nominal_raw = valute.findtext("Nominal", "1").replace(",", ".")
            value_raw = valute.findtext("Value", "").replace(",", ".")
            try:
                nominal = float(nominal_raw) or 1.0
                value = float(value_raw)
            except ValueError:
                continue
            rates[char_code] = value / nominal
        return rates or None
    except Exception as e:
        print(f"[WARN] fetch_cbr_rates error: {e}")
        return None


def fetch_moex_imoex():
    # Индекс МосБиржи в реальном времени — публичный JSON MOEX ISS API.
    # Обёрнуто максимально defensively: если структура ответа окажется
    # другой (поле переименовали, торги не идут и т.п.) — просто
    # возвращаем None, и сводка уйдёт без строки по индексу.
    try:
        resp = request_with_retry("GET", MOEX_IMOEX_URL, timeout=(5, 15))
        resp.raise_for_status()
        data = resp.json()
        md = data.get("marketdata", {})
        columns = md.get("columns", [])
        rows = md.get("data", [])
        if not columns or not rows:
            return None
        row = rows[0]
        col_idx = {name: i for i, name in enumerate(columns)}
        last_idx = col_idx.get("LAST") or col_idx.get("CURRENTVALUE")
        change_idx = col_idx.get("LASTTOPREVPRICE") or col_idx.get("LASTCHANGEPRC")
        if last_idx is None or row[last_idx] is None:
            return None
        result = {"value": float(row[last_idx])}
        if change_idx is not None and row[change_idx] is not None:
            result["change_pct"] = float(row[change_idx])
        return result
    except Exception as e:
        print(f"[WARN] fetch_moex_imoex error: {e}")
        return None


def _format_change_arrow(current, previous):
    if previous is None:
        return ""
    diff = current - previous
    if abs(diff) < 1e-9:
        return " ▬ 0.00"
    arrow = "▲" if diff > 0 else "▼"
    return f" {arrow} {diff:+.2f}"


def format_market_snapshot(rates, index_data, prev_state):
    prev_rates = (prev_state or {}).get("rates", {})
    prev_index = (prev_state or {}).get("index_value")

    lines = [f"💹 <b>Рынки к {MARKET_SNAPSHOT_TIME} мск</b>", ""]
    for code, symbol, _name in MARKET_CURRENCIES:
        value = rates.get(code)
        if value is None:
            continue
        arrow = _format_change_arrow(value, prev_rates.get(code))
        lines.append(f"{symbol} {value:.2f}{arrow}")

    if index_data and index_data.get("value") is not None:
        idx_val = index_data["value"]
        arrow = _format_change_arrow(idx_val, prev_index)
        lines.append("")
        lines.append(f"Индекс МосБиржи: {idx_val:.1f}{arrow}")

    lines.append("")
    lines.append("<i>Курсы ЦБ РФ на сегодня, индекс МосБиржи — текущие торги.</i>")
    return "\n".join(lines).strip()


WEEKLY_RECAP_WEEKDAY = 6
WEEKLY_RECAP_TIME = "20:00"
WEEKLY_RECAP_WINDOW_MINUTES = 4
WEEKLY_RECAP_STATE_FILE = "weekly_recap_state.json"
WEEKLY_RECAP_MAX_ITEMS = 7


def week_key_for(dt):
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def due_weekly_recap(dt, current_week_key, state):
    if state.get("week_key") == current_week_key and state.get("sent"):
        return False
    if dt.weekday() != WEEKLY_RECAP_WEEKDAY:
        return False
    now_minutes = dt.hour * 60 + dt.minute
    slot_h, slot_m = map(int, WEEKLY_RECAP_TIME.split(":"))
    slot_minutes = slot_h * 60 + slot_m
    return 0 <= (now_minutes - slot_minutes) <= WEEKLY_RECAP_WINDOW_MINUTES


def pick_weekly_recap_items(recent_posts, max_items=WEEKLY_RECAP_MAX_ITEMS):
    now = time.time()
    week_posts = [p for p in recent_posts if not p.get("ts") or now - p["ts"] <= 7 * 24 * 3600]
    if not week_posts:
        week_posts = recent_posts
    candidates = week_posts[-100:]
    if not candidates:
        return []
    if len(candidates) <= max_items:
        return candidates

    token = get_gigachat_token()
    if not token:
        return candidates[-max_items:]
    try:
        listing = "\n".join(
            f"{i}. {p.get('headline', '')} — {p.get('summary', '')[:100]}"
            for i, p in enumerate(candidates)
        )
        prompt = (
            f"Ниже список новостей, опубликованных каналом за последнюю неделю. "
            f"Выбери {max_items} самых важных и значимых для еженедельного "
            f"рекапа — по возможности из разных тем, а не всё об одном. "
            f"Ответь СТРОГО номерами через запятую, без пояснений, "
            f"например: 2,5,9,14,20,33,41\n\n" + listing
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = {
            "model": "GigaChat",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 60,
        }
        resp = request_with_retry("POST", GIGACHAT_CHAT_URL, headers=headers, json=payload, verify=False, timeout=(5, 15))
        resp.raise_for_status()
        answer = resp.json()["choices"][0]["message"]["content"].strip()
        indices = [int(x) for x in re.findall(r'\d+', answer)][:max_items]
        chosen = [candidates[i] for i in indices if 0 <= i < len(candidates)]
        return chosen if chosen else candidates[-max_items:]
    except Exception as e:
        print(f"[WARN] pick_weekly_recap_items error: {e}")
        return candidates[-max_items:]


def format_weekly_recap(items):
    lines = [f"{CHANNEL_MARK} <b>Главное за неделю</b>", ""]
    for i, p in enumerate(items, 1):
        cat_emoji, _ = detect_category(p.get("headline", ""), p.get("summary", ""))
        headline = html.escape(truncate_at_word(p.get("headline", "")))
        lines.append(f"{i}. {cat_emoji} {headline}")
    lines.append("")
    lines.append(random.choice(CTA_VARIANTS))
    return "\n".join(lines).strip()


def format_digest(items, slot_label):
    lines = [f"{CHANNEL_MARK} <b>{slot_label}</b>", ""]
    for i, it in enumerate(items, 1):
        cat_emoji, _ = it.get("category", (CHANNEL_MARK, None))
        lines.append(f"{i}. {cat_emoji} <b>{it['headline']}</b>")
        if it.get("body"):
            first_sentence = it["body"].split(". ")[0].rstrip(".") + "."
            lines.append(first_sentence)
        lines.append("")
    lines.append(random.choice(CTA_VARIANTS))
    return "\n".join(lines).strip()


# --- "Крутая" фича: публичный live-дашборд канала ---
# Статическая HTML-страница со статистикой бота — как status-страница у
# серьёзных сервисов (health, скорость публикаций, рост подписчиков,
# вклад источников, последние заголовки). Генерируется и коммитится в
# docs/index.html ТЕМ ЖЕ механизмом, что уже пишет posted.json и другие
# state-файлы (см. persist_state_to_git) — отдельный деплой-пайплайн не
# нужен. Чтобы страница стала доступна по ссылке, один раз включите в
# репозитории: Settings → Pages → Source: Deploy from a branch → main → /docs.
# Никаких внешних JS-библиотек не используется (график — инлайновый SVG),
# страница работает даже без интернета у посетителя, кроме загрузки самой
# страницы.
DOCS_DIR = "docs"
DASHBOARD_FILE = os.path.join(DOCS_DIR, "index.html")
# ФИКС: по умолчанию GitHub Pages прогоняет содержимое /docs через Jekyll,
# который трактует одиночные фигурные скобки в инлайновом CSS/JS как
# потенциальный Liquid-синтаксис ({{ }} / {% %}) и может упасть на сборке
# (см. "pages build and deployment" — красный крест, сайт отдаёт 404).
# .nojekyll — стандартный флаг-файл, который отключает эту обработку:
# наш дашборд — чистый статический HTML, Jekyll ему не нужен.
NOJEKYLL_FILE = os.path.join(DOCS_DIR, ".nojekyll")


def _svg_sparkline(history, width=640, height=140, pad=24):
    if not history or len(history) < 2:
        return ""
    values = [h["count"] for h in history]
    min_v, max_v = min(values), max(values)
    span = (max_v - min_v) or 1
    n = len(values)
    step = (width - 2 * pad) / (n - 1)

    def x(i):
        return pad + i * step

    def y(v):
        return height - pad - ((v - min_v) / span) * (height - 2 * pad)

    points = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))
    last_x, last_y = x(n - 1), y(values[-1])
    first_date = html.escape(history[0]["date"])
    last_date = html.escape(history[-1]["date"])
    return f"""
    <svg viewBox="0 0 {width} {height}" class="sparkline" xmlns="http://www.w3.org/2000/svg">
      <polyline fill="none" stroke="#4fd1c5" stroke-width="2.5" points="{points}" />
      <circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="4.5" fill="#4fd1c5" />
      <text x="{pad}" y="{height - 4}" class="spark-label">{first_date}</text>
      <text x="{width - pad}" y="{height - 4}" class="spark-label" text-anchor="end">{last_date}</text>
    </svg>
    """


def build_dashboard_html(status, recent_posts, subscriber_history, source_contribution, generated_at_msk):
    status = status or {}
    healthy = status.get("healthy")
    health_badge = ("🟢 Работает штатно" if healthy else "🔴 Возможен сбой — новостей давно не было")
    seconds_since = status.get("seconds_since_last_publish")
    if seconds_since is not None:
        minutes = int(seconds_since // 60)
        last_publish_label = f"{minutes} мин назад" if minutes < 60 else f"{minutes // 60} ч {minutes % 60} мин назад"
    else:
        last_publish_label = "нет данных"

    avg_latency = status.get("avg_publish_latency_minutes")
    median_latency = status.get("median_publish_latency_minutes")
    speed_samples = status.get("speed_samples_count") or 0

    current_subs = subscriber_history[-1]["count"] if subscriber_history else None
    sparkline_svg = _svg_sparkline(subscriber_history)

    top_sources = list(source_contribution.items())[:8]
    source_rows = "".join(
        f'<div class="bar-row"><span class="bar-label">{html.escape(src)}</span>'
        f'<div class="bar-track"><div class="bar-fill" style="width:{min(100, count / (top_sources[0][1] or 1) * 100):.0f}%"></div></div>'
        f'<span class="bar-count">{count}</span></div>'
        for src, count in top_sources
    ) if top_sources else '<p class="muted">Пока нет данных</p>'

    recent_rows = "".join(
        f'<li><span class="cat-emoji">{detect_category(p.get("headline", ""), p.get("summary", ""))[0]}</span>'
        f'{html.escape(truncate_at_word(p.get("headline", ""), 110))}</li>'
        for p in list(reversed(recent_posts))[:12]
    ) if recent_posts else '<li class="muted">Пока нет опубликованных новостей</li>'

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(CHANNEL_USERNAME)} — статус канала</title>
<style>
  :root {{
    --bg: #0d1117; --card: #161b22; --border: #30363d;
    --text: #e6edf3; --muted: #8b949e; --accent: #4fd1c5;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 32px 16px 64px; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  .wrap {{ max-width: 880px; margin: 0 auto; }}
  h1 {{ font-size: 1.5rem; margin-bottom: 4px; }}
  .subtitle {{ color: var(--muted); margin-top: 0; margin-bottom: 28px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 18px 20px; }}
  .card .label {{ color: var(--muted); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.04em; }}
  .card .value {{ font-size: 1.6rem; font-weight: 600; margin-top: 6px; }}
  .card.wide {{ grid-column: 1 / -1; }}
  .sparkline {{ width: 100%; height: auto; margin-top: 8px; }}
  .spark-label {{ fill: var(--muted); font-size: 11px; }}
  .bar-row {{ display: flex; align-items: center; gap: 10px; margin: 8px 0; }}
  .bar-label {{ width: 140px; font-size: 0.85rem; color: var(--muted); flex-shrink: 0; }}
  .bar-track {{ flex: 1; background: #21262d; border-radius: 6px; height: 10px; overflow: hidden; }}
  .bar-fill {{ background: var(--accent); height: 100%; border-radius: 6px; }}
  .bar-count {{ width: 36px; text-align: right; font-size: 0.85rem; color: var(--muted); }}
  ul {{ list-style: none; padding: 0; margin: 8px 0 0; }}
  li {{ padding: 8px 0; border-bottom: 1px solid var(--border); font-size: 0.92rem; }}
  li:last-child {{ border-bottom: none; }}
  .cat-emoji {{ margin-right: 8px; }}
  .muted {{ color: var(--muted); }}
  footer {{ color: var(--muted); font-size: 0.8rem; margin-top: 32px; text-align: center; }}
  a {{ color: var(--accent); }}
</style>
</head>
<body>
<div class="wrap">
  <h1>📡 {html.escape(CHANNEL_USERNAME)}</h1>
  <p class="subtitle">Живая статистика новостного канала · <a href="{html.escape(CHANNEL_LINK)}">открыть канал</a></p>

  <div class="grid">
    <div class="card">
      <div class="label">Статус</div>
      <div class="value" style="font-size:1.1rem">{health_badge}</div>
    </div>
    <div class="card">
      <div class="label">Последняя публикация</div>
      <div class="value" style="font-size:1.1rem">{last_publish_label}</div>
    </div>
    <div class="card">
      <div class="label">Подписчиков</div>
      <div class="value">{current_subs if current_subs is not None else "—"}</div>
    </div>
    <div class="card">
      <div class="label">Скорость публикации (медиана)</div>
      <div class="value">{f"{median_latency:.0f} мин" if median_latency is not None else "—"}</div>
    </div>
  </div>

  <div class="card wide">
    <div class="label">Рост подписчиков</div>
    {sparkline_svg or '<p class="muted">Накапливаем историю — график появится через несколько дней</p>'}
  </div>

  <div class="grid" style="grid-template-columns: 1fr 1fr;">
    <div class="card">
      <div class="label">Вклад источников (последние посты)</div>
      {source_rows}
    </div>
    <div class="card">
      <div class="label">Последние новости</div>
      <ul>{recent_rows}</ul>
    </div>
  </div>

  <footer>Обновляется автоматически ботом · последнее обновление: {html.escape(generated_at_msk)} мск</footer>
</div>
</body>
</html>"""



STATE_FILES = [
    POSTED_FILE, RECENT_TITLES_FILE, LAST_RUN_FILE, MILESTONES_FILE,
    DIGEST_STATE_FILE, POLL_STATE_FILE,
    ALERT_STATE_FILE, STATUS_FILE, RECENT_POSTS_FILE, WEEKLY_RECAP_STATE_FILE,
    SPEED_STATS_FILE, QUIZ_STATE_FILE, LIVE_STORIES_FILE, MARKET_STATE_FILE,
    SUBSCRIBER_HISTORY_FILE, DASHBOARD_FILE, NOJEKYLL_FILE,
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
                          new_digest_state=None, new_poll_state=None,
                          new_alert_timestamp=None, new_status=None, new_weekly_recap_state=None,
                          new_quiz_state=None, new_market_state=None):
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return True
    import subprocess

    new_posted_ids = new_posted_ids or []
    new_title_words_list = new_title_words_list or []

    try:
        subprocess.run(["git", "config", "user.name", "news-bot"], check=False)
        subprocess.run(["git", "config", "user.email", "news-bot@users.noreply.github.com"], check=False)

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
            remote_quiz = _git_show_json(f"origin/main:{QUIZ_STATE_FILE}", {"date": None, "sent": False})
            remote_market = _git_show_json(f"origin/main:{MARKET_STATE_FILE}", {"date": None, "sent": False})
            remote_weekly_recap = _git_show_json(f"origin/main:{WEEKLY_RECAP_STATE_FILE}", {"week_key": None, "sent": False})
            remote_alert = _git_show_json(f"origin/main:{ALERT_STATE_FILE}", {"last_alert": 0})
            remote_recent_posts = _git_show_json(f"origin/main:{RECENT_POSTS_FILE}", [])
            remote_speed_stats = _git_show_json(f"origin/main:{SPEED_STATS_FILE}", [])
            remote_live_threads = _git_show_json(f"origin/main:{LIVE_STORIES_FILE}", [])
            remote_subscriber_history = _git_show_json(f"origin/main:{SUBSCRIBER_HISTORY_FILE}", [])

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

            local_recent_posts = load_recent_posts()
            seen_keys = set()
            merged_recent_posts = []
            for post in (list(remote_recent_posts) + local_recent_posts):
                if not isinstance(post, dict) or not post.get("headline"):
                    continue
                key = (post.get("headline"), post.get("summary"))
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                merged_recent_posts.append(post)
            merged_recent_posts = merged_recent_posts[-RECENT_POSTS_LIMIT:]

            local_speed_stats = load_speed_stats()
            merged_speed_stats = (list(remote_speed_stats) + local_speed_stats)[-SPEED_STATS_LIMIT:]

            local_live_threads = load_live_threads()
            by_message_id = {}
            for t in (list(remote_live_threads) + local_live_threads):
                if not isinstance(t, dict) or not t.get("message_id"):
                    continue
                mid = t["message_id"]
                if mid not in by_message_id or t.get("last_update_ts", 0) > by_message_id[mid].get("last_update_ts", 0):
                    by_message_id[mid] = t
            _now_ts = time.time()
            merged_live_threads = [
                t for t in by_message_id.values()
                if _now_ts - t.get("last_update_ts", 0) <= LIVE_STORY_MAX_AGE_HOURS * 3600
            ]

            # ФИКС: раньше "новое" состояние опроса/квиза/дайджеста/рекапа
            # просто ПЕРЕЗАПИСЫВАЛО remote-версию целиком (new_* или remote_*
            # целиком, без слияния). Если два запуска почти одновременно
            # решили, что опрос "нужно отправить" (см. MIN_*_GAP_SECONDS
            # выше — теперь это отдельная защита), при пуше более позднего
            # из них он просто перетирал remote своим "sent": True — то есть
            # само по себе перезаписывание не было причиной дубликата, но
            # мёржим по last_sent_ts (берём максимум), чтобы гонка попыток
            # пуша не могла откатить уже более свежую метку отправки назад.
            def _merge_daily_state(remote, new, ts_key="last_sent_ts"):
                if new is None:
                    return remote
                if remote and remote.get(ts_key, 0) > new.get(ts_key, 0):
                    return remote
                return new

            merged_digest = _merge_daily_state(remote_digest, new_digest_state)
            merged_poll = _merge_daily_state(remote_poll, new_poll_state)
            merged_quiz = _merge_daily_state(remote_quiz, new_quiz_state)
            merged_market = _merge_daily_state(remote_market, new_market_state)
            merged_weekly_recap = _merge_daily_state(remote_weekly_recap, new_weekly_recap_state)
            merged_alert = {
                "last_alert": max(remote_alert.get("last_alert", 0), new_alert_timestamp or 0)
            }
            merged_status = new_status if new_status is not None else _git_show_json(f"origin/main:{STATUS_FILE}", {})

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
            _save_json(QUIZ_STATE_FILE, merged_quiz)
            _save_json(MARKET_STATE_FILE, merged_market)
            _save_json(WEEKLY_RECAP_STATE_FILE, merged_weekly_recap)
            _save_json(ALERT_STATE_FILE, merged_alert)
            _save_json(STATUS_FILE, merged_status)
            _save_json(RECENT_POSTS_FILE, merged_recent_posts)
            _save_json(SPEED_STATS_FILE, merged_speed_stats)
            _save_json(LIVE_STORIES_FILE, merged_live_threads)

            local_subscriber_history = load_subscriber_history()
            merged_subscriber_history = merge_subscriber_history(remote_subscriber_history, local_subscriber_history)
            save_subscriber_history(merged_subscriber_history)

            # Дашборд генерируется из уже смёрженных данных (та же логика,
            # что видит статус-снапшот) — так публичная страница не может
            # разъехаться с тем, что реально записано в state-файлах.
            try:
                os.makedirs(DOCS_DIR, exist_ok=True)
                with open(NOJEKYLL_FILE, "w", encoding="utf-8"):
                    pass
                dashboard_html = build_dashboard_html(
                    status=merged_status,
                    recent_posts=merged_recent_posts,
                    subscriber_history=merged_subscriber_history,
                    source_contribution=source_contribution_summary(merged_recent_posts),
                    generated_at_msk=now_msk().strftime("%d.%m.%Y %H:%M"),
                )
                with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
                    f.write(dashboard_html)
            except Exception as e:
                print(f"[WARN] build_dashboard_html error: {e}")

            subprocess.run(["git", "add", *STATE_FILES], check=False)
            diff = subprocess.run(["git", "diff", "--cached", "--quiet"])
            if diff.returncode == 0:
                print("[INFO] State files unchanged, nothing to commit.")
                return True

            subprocess.run(["git", "commit", "-m", "chore: update bot state [skip ci]"], check=False)
            push = subprocess.run(["git", "push"], capture_output=True, text=True)
            if push.returncode == 0:
                print("[INFO] State files committed and pushed.")
                return True
            backoff = min(2 * (2 ** (attempt - 1)), 30) + random.uniform(0, 1.5)
            print(f"[WARN] git push failed (attempt {attempt}/{MAX_PUSH_ATTEMPTS}), "
                  f"retrying in {backoff:.1f}s with fresh fetch: {push.stderr}")
            time.sleep(backoff)

        # ФИКС (см. также разбор дублирующегося опроса выше): раньше здесь
        # стоял `raise RuntimeError(...)`, который убивал весь процесс.
        # Это НЕ давало никакого дополнительного шанса на повторную
        # попытку (следующий запуск всё равно стартует по cron независимо
        # от того, упал ли этот процесс с исключением или тихо завершился) —
        # единственным эффектом был красный крест в истории запусков и,
        # что важнее, прерывание скрипта ДО того, как успевали
        # записаться в лог остальные детали. Теперь просто предупреждаем и
        # возвращаем False — вызывающий код это не считает фатальной
        # ошибкой (Telegram-отправка уже состоялась, лучше сохранить
        # видимость и не падать), а MIN_*_GAP_SECONDS выше защищает от
        # повторной отправки в следующем запуске, даже если состояние не
        # запушилось.
        print(f"[WARN] Could not push state after {MAX_PUSH_ATTEMPTS} attempts — "
              f"next run may briefly re-see this item (mitigated by MIN_*_GAP_SECONDS guards).")
        return False
    except Exception as e:
        print(f"[WARN] persist_state_to_git error: {e}")
        return False


def main():
    print(f"[START] {datetime.now().isoformat()}")

    dt_msk = now_msk()
    day_key = today_key(dt_msk)
    now_ts = time.time()

    elapsed_at_start = seconds_since_last_publish()
    new_alert_timestamp = check_silence_alert(elapsed_at_start)

    def persist_with_status(sent_count=None, note="", **kwargs):
        status = build_status_snapshot(elapsed_at_start, sent_count=sent_count, note=note)
        persist_state_to_git(
            new_alert_timestamp=new_alert_timestamp,
            new_status=status,
            **kwargs,
        )

    # --- Ежедневный вовлекающий опрос ---
    poll_state = _load_json(POLL_STATE_FILE, {"date": None, "sent": False, "last_sent_ts": 0})
    if poll_state.get("date") != day_key:
        poll_state = {"date": day_key, "sent": False, "last_sent_ts": poll_state.get("last_sent_ts", 0)}
    new_poll_state = None
    # ФИКС: добавлена защита по минимальному интервалу (MIN_POLL_GAP_SECONDS)
    # в дополнение к флагу "sent за сегодня" — см. комментарий у константы.
    if due_poll(dt_msk, poll_state.get("sent")) and (now_ts - poll_state.get("last_sent_ts", 0)) >= MIN_POLL_GAP_SECONDS:
        variant = pick_poll_variant(day_key)
        if send_poll_to_telegram(variant["question"], variant["options"]):
            poll_state["sent"] = True
            poll_state["last_sent_ts"] = now_ts
            new_poll_state = poll_state
            print(f"[INFO] Engagement poll sent: '{variant['question']}'")

    # --- Ежедневный квиз по реальным опубликованным новостям ---
    quiz_state = _load_json(QUIZ_STATE_FILE, {"date": None, "sent": False, "last_sent_ts": 0})
    if quiz_state.get("date") != day_key:
        quiz_state = {"date": day_key, "sent": False, "last_sent_ts": quiz_state.get("last_sent_ts", 0)}
    new_quiz_state = None
    if due_quiz(dt_msk, quiz_state.get("sent")) and (now_ts - quiz_state.get("last_sent_ts", 0)) >= MIN_QUIZ_GAP_SECONDS:
        source_post = pick_quiz_source_post(load_recent_posts())
        quiz = build_quiz_from_post(source_post)
        if quiz and send_quiz_poll(quiz):
            quiz_state["sent"] = True
            quiz_state["last_sent_ts"] = now_ts
            new_quiz_state = quiz_state
            print(f"[INFO] Daily quiz sent: '{quiz['question']}'")
        else:
            quiz_state["sent"] = True
            quiz_state["last_sent_ts"] = now_ts
            new_quiz_state = quiz_state
            print("[INFO] No material/quiz for today, marking as done anyway.")

    # --- Утренняя сводка по рынкам (курсы валют ЦБ + индекс МосБиржи) ---
    market_state = _load_json(MARKET_STATE_FILE, {"date": None, "sent": False, "last_sent_ts": 0})
    if market_state.get("date") != day_key:
        market_state = {"date": day_key, "sent": False, "last_sent_ts": market_state.get("last_sent_ts", 0)}
    new_market_state = None
    if due_market_snapshot(dt_msk, market_state.get("sent")) and \
            (now_ts - market_state.get("last_sent_ts", 0)) >= MIN_MARKET_GAP_SECONDS:
        rates = fetch_cbr_rates()
        if rates:
            index_data = fetch_moex_imoex()
            prev_state = _load_json(MARKET_STATE_FILE, {})
            text = format_market_snapshot(rates, index_data, prev_state)
            if send_to_telegram(text):
                market_state["sent"] = True
                market_state["last_sent_ts"] = now_ts
                market_state["rates"] = rates
                if index_data and index_data.get("value") is not None:
                    market_state["index_value"] = index_data["value"]
                new_market_state = market_state
                print("[INFO] Market snapshot sent.")
        else:
            # Курсы ЦБ недоступны (например, сервис лёг) — помечаем день
            # пройденным, чтобы не долбить cbr.ru каждые 5 минут в это же
            # окно; попробуем снова завтра утром.
            market_state["sent"] = True
            market_state["last_sent_ts"] = now_ts
            new_market_state = market_state
            print("[WARN] CBR rates unavailable, skipping market snapshot for today.")

    count = get_subscriber_count()
    update_channel_description(count)
    maybe_celebrate_milestone(count)
    if count is not None:
        # Для графика роста на дашборде достаточно одной точки в день —
        # merge_subscriber_history сам заменит сегодняшнюю точку, если она
        # уже была записана более ранним прогоном сегодня.
        save_subscriber_history(
            merge_subscriber_history(load_subscriber_history(), [{"date": day_key, "count": count}])
        )

    # --- Еженедельный рекап "Главное за неделю" ---
    week_key = week_key_for(dt_msk)
    weekly_recap_state = _load_json(WEEKLY_RECAP_STATE_FILE, {"week_key": None, "sent": False, "last_sent_ts": 0})
    new_weekly_recap_state = None
    if due_weekly_recap(dt_msk, week_key, weekly_recap_state) and \
            (now_ts - weekly_recap_state.get("last_sent_ts", 0)) >= MIN_WEEKLY_RECAP_GAP_SECONDS:
        recap_items = pick_weekly_recap_items(load_recent_posts())
        if recap_items:
            recap_text = format_weekly_recap(recap_items)
            if send_to_telegram(recap_text):
                new_weekly_recap_state = {"week_key": week_key, "sent": True, "last_sent_ts": now_ts}
                print(f"[INFO] Weekly recap sent: {len(recap_items)} items.")
        else:
            new_weekly_recap_state = {"week_key": week_key, "sent": True, "last_sent_ts": now_ts}
            print("[INFO] No material for weekly recap, marking week as done anyway.")

    # --- Дайджест по расписанию (утро/вечер) ---
    digest_state = _load_json(DIGEST_STATE_FILE, {"date": None, "slots": [], "last_sent_ts": 0})
    if digest_state.get("date") != day_key:
        digest_state = {"date": day_key, "slots": [], "last_sent_ts": digest_state.get("last_sent_ts", 0)}
    new_digest_state = None

    slot = due_digest_slot(dt_msk, digest_state.get("slots", []))
    if slot and (now_ts - digest_state.get("last_sent_ts", 0)) >= MIN_DIGEST_GAP_SECONDS:
        digest_items = fetch_news()
        digest_items = order_candidates_by_priority(digest_items, load_recent_posts())
        digest_items.sort(key=lambda it: not it.get("urgent"))

        recent_posts_for_digest = load_recent_posts()
        common_entities_digest = compute_common_entity_stems(recent_posts_for_digest)
        digest_items = [
            it for it in digest_items
            if not is_duplicate_word_or_entity(it["title"], it.get("summary", ""), recent_posts_for_digest,
                                                exclude_entities=common_entities_digest)
        ]
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
            digest_msg_id = send_to_telegram(text)
            if digest_msg_id:
                posted = load_posted()
                new_posted_ids = []
                recent_words = load_recent_title_words()
                new_title_words_list = []
                new_recent_posts_entries = []
                for it in finalized:
                    posted.add(it["id"])
                    posted.add(it["title_key"])
                    new_posted_ids.extend([it["id"], it["title_key"]])
                    if it.get("content_words"):
                        recent_words.append(set(it["content_words"]))
                        new_title_words_list.append(it["content_words"])
                    new_recent_posts_entries.append({
                        "headline": it.get("title", ""),
                        "summary": it.get("summary", ""),
                        "ts": time.time(),
                        "message_id": digest_msg_id if isinstance(digest_msg_id, int) else None,
                        "source": it.get("source", ""),
                    })
                save_posted(posted)
                save_recent_title_words(recent_words)
                save_recent_posts(load_recent_posts() + new_recent_posts_entries)

                digest_state["slots"] = digest_state.get("slots", []) + [slot]
                digest_state["last_sent_ts"] = now_ts
                new_digest_state = digest_state
                mark_published_now()

                persist_with_status(
                    sent_count=len(finalized),
                    note=f"digest:{slot}",
                    new_posted_ids=new_posted_ids,
                    new_title_words_list=new_title_words_list,
                    new_last_publish=time.time(),
                    new_digest_state=new_digest_state,
                    new_poll_state=new_poll_state,
                new_weekly_recap_state=new_weekly_recap_state,
                new_quiz_state=new_quiz_state,
                new_market_state=new_market_state,
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
        if new_digest_state or new_poll_state or new_alert_timestamp or new_weekly_recap_state or new_quiz_state or new_market_state or status_is_stale():
            persist_with_status(
                note="skip:too_soon_urgent",
                new_digest_state=new_digest_state,
                new_poll_state=new_poll_state,
                new_weekly_recap_state=new_weekly_recap_state,
                new_quiz_state=new_quiz_state,
                new_market_state=new_market_state,
            )
        return

    news = fetch_news()
    if not news:
        print("[INFO] No new news.")
        if new_digest_state or new_poll_state or new_alert_timestamp or new_weekly_recap_state or new_quiz_state or new_market_state or status_is_stale():
            persist_with_status(
                note="skip:no_news",
                new_digest_state=new_digest_state,
                new_poll_state=new_poll_state,
                new_weekly_recap_state=new_weekly_recap_state,
                new_quiz_state=new_quiz_state,
                new_market_state=new_market_state,
            )
        return

    urgent_items = [it for it in news if it.get("urgent")]
    normal_items = [it for it in news if not it.get("urgent")]

    def prefer_video(items):
        with_video = [it for it in items if it.get("video")]
        return with_video if with_video else items

    _recent_posts_for_priority = load_recent_posts()

    if urgent_items:
        ordered = order_candidates_by_priority(prefer_video(urgent_items), _recent_posts_for_priority)
    else:
        if elapsed is not None and elapsed < PUBLISH_INTERVAL:
            print(f"[INFO] Skipping run — с последней публикации прошло {int(elapsed)} сек "
                  f"(меньше {PUBLISH_INTERVAL} сек), срочных новостей нет.")
            if new_digest_state or new_poll_state or new_alert_timestamp or new_weekly_recap_state or new_quiz_state or new_market_state or status_is_stale():
                persist_with_status(
                    note="skip:too_soon_normal",
                    new_digest_state=new_digest_state,
                    new_poll_state=new_poll_state,
                new_weekly_recap_state=new_weekly_recap_state,
                new_quiz_state=new_quiz_state,
                new_market_state=new_market_state,
                )
            return
        normal_items = prefer_video(normal_items)
        ordered = order_candidates_by_priority(normal_items, _recent_posts_for_priority)

    chosen = pick_non_duplicate(ordered)

    if chosen is None and urgent_items:
        if elapsed is None or elapsed >= PUBLISH_INTERVAL:
            print("[INFO] Все срочные кандидаты — дубли, пробуем обычные новости из того же пула.")
            fallback_ordered = order_candidates_by_priority(prefer_video(normal_items), _recent_posts_for_priority)
            chosen = pick_non_duplicate(fallback_ordered)
        else:
            print(f"[INFO] Все срочные кандидаты — дубли, но с последней публикации прошло "
                  f"{int(elapsed)} сек (меньше {PUBLISH_INTERVAL} сек) — обычные новости пока не пробуем.")

    if chosen is None and elapsed is not None and elapsed >= GUARANTEED_CADENCE_SECONDS:
        print(f"[INFO] С последней публикации прошло {int(elapsed)} сек "
              f"(больше {GUARANTEED_CADENCE_SECONDS} сек) — строгий дедуп ничего не пропустил, "
              f"пробуем аварийный режим для гарантии частоты публикаций.")
        chosen = pick_any_not_posted(news)

    if chosen is None:
        print("[INFO] All candidates turned out to be duplicates of already-posted news.")
        if new_digest_state or new_poll_state or new_alert_timestamp or new_weekly_recap_state or new_quiz_state or new_market_state or status_is_stale():
            persist_with_status(
                note="skip:all_duplicates",
                new_digest_state=new_digest_state,
                new_poll_state=new_poll_state,
                new_weekly_recap_state=new_weekly_recap_state,
                new_quiz_state=new_quiz_state,
                new_market_state=new_market_state,
            )
        return

    _recent_posts_for_continuation = load_recent_posts()
    chosen["continuation_of"] = find_story_continuation(
        chosen["title"], chosen.get("summary", ""), _recent_posts_for_continuation,
        exclude_entities=compute_common_entity_stems(_recent_posts_for_continuation),
    )

    chosen = finalize_item(chosen)

    if chosen.get("urgent"):
        live_threads_now = load_live_threads()
        common_entities_live = compute_common_entity_stems(load_recent_posts())
        active_thread = find_active_live_thread(
            chosen["title"], chosen.get("summary", ""), live_threads_now,
            exclude_entities=common_entities_live,
        )
        if active_thread:
            updated_thread = append_live_update(active_thread, chosen)
            if edit_message_text(active_thread["message_id"], build_live_text(updated_thread)):
                other_threads = [t for t in live_threads_now if t.get("message_id") != active_thread["message_id"]]
                save_live_threads(other_threads + [updated_thread])
                posted = load_posted()
                posted.add(chosen["id"])
                posted.add(chosen["title_key"])
                save_posted(posted)
                new_posted_ids = [chosen["id"], chosen["title_key"]]
                new_title_words_list = []
                if chosen.get("content_words"):
                    recent_words = load_recent_title_words()
                    recent_words.append(set(chosen["content_words"]))
                    save_recent_title_words(recent_words)
                    new_title_words_list.append(chosen["content_words"])
                # ФИКС (найдено при доскональной проверке жалобы "давно нет
                # новостей"): здесь раньше стояли mark_published_now() и
                # new_last_publish=time.time() — но editMessageText НИЧЕГО
                # не публикует в канал: подписчики не видят правку, нет
                # уведомления. Тем не менее это обнуляло тот же самый
                # таймер "последняя публикация", который используется (а)
                # чтобы не постить чаще, чем раз в URGENT_INTERVAL/
                # PUBLISH_INTERVAL, и (б) для расчёта "здоровья" бота на
                # дашборде/алертах. Из-за широкого списка URGENT_KEYWORDS
                # и активного "эфира" (живёт до 12 часов) бот мог правку за
                # правкой обнулять этот таймер, откладывая любую другую,
                # по-настоящему новую публикацию ещё на 10 минут каждый
                # раз — а дашборд при этом показывал "всё ок", маскируя
                # реальную тишину в канале. Теперь тихая правка НЕ считается
                # публикацией: таймер и health-статус отражают только то,
                # что реально появилось в канале.
                persist_with_status(
                    sent_count=1,
                    note="live_thread_update",
                    new_posted_ids=new_posted_ids,
                    new_title_words_list=new_title_words_list,
                    new_digest_state=new_digest_state,
                    new_poll_state=new_poll_state,
                    new_weekly_recap_state=new_weekly_recap_state,
                    new_quiz_state=new_quiz_state,
                    new_market_state=new_market_state,
                )
                print(f"[DONE] Live thread updated (message {active_thread['message_id']}): "
                      f"'{chosen['title'][:50]}'")
                return

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
            save_recent_posts(load_recent_posts() + [{
                "headline": item.get("title", ""),
                "summary": item.get("summary", ""),
                "ts": time.time(),
                "message_id": ok if isinstance(ok, int) else None,
                "source": item.get("source", ""),
            }])
            latency = compute_publish_latency_seconds(item.get("published"))
            if latency is not None:
                save_speed_stats(load_speed_stats() + [{"latency_seconds": latency, "ts": time.time()}])
            if item.get("urgent") and isinstance(ok, int):
                # ФИКС: раньше каждая новая "срочная" новость закреплялась
                # (pin_message), но НИКОГДА не открепляла предыдущую — со
                # временем в канале накапливалось несколько одновременно
                # закреплённых сообщений (видно на реальном кейсе:
                # несколько раз подряд "Факты Дня закрепил(а) ..."). Перед
                # тем как закрепить новый эфир, открепляем все ещё
                # "живые" (не остывшие) прошлые эфиры — в любой момент
                # закреплённым остаётся только самый свежий.
                previous_threads = load_live_threads()
                for prev_thread in previous_threads:
                    prev_msg_id = prev_thread.get("message_id")
                    if prev_msg_id and prev_msg_id != ok:
                        unpin_message(prev_msg_id)
                new_thread = start_live_thread(item, ok)
                save_live_threads(previous_threads + [new_thread])
                pin_message(ok)
                print(f"[INFO] Started new live thread (message {ok}).")
            sent_count += 1
            time.sleep(SEND_DELAY)
        else:
            print(f"[WARN] Failed to send item {item['id']} — will retry next run.")
            break

    if sent_count > 0:
        mark_published_now()
        new_last_publish = time.time()

    persist_with_status(
        sent_count=sent_count,
        note="normal_post",
        new_posted_ids=new_posted_ids,
        new_title_words_list=new_title_words_list,
        new_last_publish=new_last_publish,
        new_digest_state=new_digest_state,
        new_poll_state=new_poll_state,
                new_weekly_recap_state=new_weekly_recap_state,
                new_quiz_state=new_quiz_state,
                new_market_state=new_market_state,
    )

    print(f"[DONE] Sent {sent_count}/{len(news)} items as separate posts.")


if __name__ == "__main__":
    main()
