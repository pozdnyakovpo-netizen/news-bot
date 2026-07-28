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
FEEDS_BACKUP_FILE = "feeds_backup.txt"
SELF_HEAL_LOG_FILE = "self_heal_log.json"
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


def find_matching_post(candidate_title, candidate_summary, recent_posts, exclude_entities=None):
    # То же самое, что is_duplicate_word_or_entity, но возвращает САМ
    # найденный пост (а не просто True/False) — нужен, чтобы понять, какой
    # именно ранее опубликованный пост совпал по теме, и сравнить цифры
    # (см. find_updated_fact ниже — "крутая" фича автоматических уточнений).
    for post in recent_posts:
        if is_same_event(candidate_title, candidate_summary,
                          post.get("headline", ""), post.get("summary", ""),
                          exclude_entities=exclude_entities):
            return post
    return None


# --- "Крутая" фича: автоматические уточнения по изменившимся фактам ---
# Когда новый кандидат оказывается дублем уже опубликованной новости (то же
# место/событие), но при этом ключевая цифра — число погибших/раненых/
# пострадавших/жертв — ИЗМЕНИЛАСЬ (обычная ситуация: сначала "трое
# погибших", через час "пятеро погибших"), бот раньше просто тихо
# отбрасывал такую новость как дубль — читатель никогда не узнавал, что
# цифра выросла. Теперь вместо этого публикуется короткое явное
# "🔄 Уточнение" с новой цифрой и ссылкой на исходный пост — редкая для
# автоматических каналов функция точности, а не только скорости.
FACT_UPDATE_KEYWORDS = ["погиб", "ранен", "пострадал", "жертв"]
RU_NUMBER_WORDS = {
    "один": 1, "одна": 1, "одного": 1, "одну": 1,
    "двое": 2, "два": 2, "две": 2, "двух": 2,
    "трое": 3, "три": 3, "трёх": 3, "трех": 3,
    "четверо": 4, "четыре": 4, "четырёх": 4, "четырех": 4,
    "пятеро": 5, "пять": 5, "пятерых": 5,
    "шестеро": 6, "шесть": 6, "шестерых": 6,
    "семеро": 7, "семь": 7, "семерых": 7,
    "восьмеро": 8, "восемь": 8, "восьмерых": 8,
    "девятеро": 9, "девять": 9, "девятерых": 9,
    "десять": 10, "десятеро": 10, "десятерых": 10,
    "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13,
    "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16,
    "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19, "двадцать": 20,
}


def _word_to_number(token):
    if token.isdigit():
        return int(token)
    return RU_NUMBER_WORDS.get(token)


def extract_fact_numbers(text, keywords=FACT_UPDATE_KEYWORDS, window=3):
    if not text:
        return {}
    tokens = re.findall(r'[а-яё\d]+', text.lower())
    result = {}
    for kw in keywords:
        for i, tok in enumerate(tokens):
            if tok.startswith(kw):
                nearby = tokens[max(0, i - window):i] + tokens[i + 1:i + window + 1]
                for w in nearby:
                    n = _word_to_number(w)
                    if n is not None:
                        result[kw] = n
                        break
                if kw in result:
                    break
    return result


def find_updated_fact(candidate_title, candidate_summary, matched_post):
    # Сравнивает цифры кандидата с цифрами уже опубликованного поста,
    # которому он соответствует по теме. Возвращает (ключевое_слово,
    # старое_число, новое_число), если хоть одна цифра изменилась, иначе
    # None. Публикуем уточнение только когда новое число БОЛЬШЕ старого
    # (типичная динамика таких новостей — уточнения почти всегда идут в
    # сторону роста; если число вдруг меньше, это, скорее всего, ошибка
    # распознавания источника, а не реальное опровержение, — на такое не
    # реагируем, чтобы не публиковать ложные "уточнения").
    candidate_numbers = extract_fact_numbers(candidate_title + " " + (candidate_summary or ""))
    old_numbers = extract_fact_numbers(
        (matched_post.get("headline", "") or "") + " " + (matched_post.get("summary", "") or "")
    )
    for kw, new_val in candidate_numbers.items():
        old_val = old_numbers.get(kw)
        if old_val is not None and new_val > old_val:
            return (kw, old_val, new_val)
    return None


FACT_UPDATE_LABELS = {
    "погиб": "погибших",
    "ранен": "раненых",
    "пострадал": "пострадавших",
    "жертв": "жертв",
}


def format_fact_update(candidate_title, matched_post, kw, old_val, new_val):
    label = FACT_UPDATE_LABELS.get(kw, kw)
    original_headline = matched_post.get("headline") or matched_post.get("title", "")
    lines = [
        f"🔄 <b>Уточнение</b>",
        "",
        f"«{html.escape(truncate_at_word(original_headline, 110))}»",
        f"Число {label} выросло: было {old_val} → стало {new_val}.",
    ]
    msg_id = matched_post.get("message_id")
    if msg_id:
        link = f"https://t.me/{CHANNEL_USERNAME}/{msg_id}"
        lines.append(f'<a href="{link}">Исходный пост</a>')
    return "\n".join(lines)


STORY_CONTINUATION_WINDOW_HOURS = 48


def find_story_continuation(candidate_title, candidate_summary, recent_posts,
                             hours=STORY_CONTINUATION_WINDOW_HOURS, exclude_entities=None):
    # ФИКС (жалоба: "продолжение истории возвращается к совершенно другим
    # новостям"): раньше связь искалась ПО ЕДИНСТВЕННОМУ совпавшему
    # именному стему (первые 6 букв слова) БЕЗ какой-либо дополнительной
    # проверки — а стем это грубая эвристика: "Донецк" и "Донецкая
    # область", "Иванов" и "Иванова" дают один и тот же обрубок "донецк"/
    # "иванов", хотя это разные сущности. Функция дедупа (is_same_event)
    # всегда требует ЕЩЁ и реального пересечения слов темы в подкрепление
    # совпавшей сущности — у "продолжения истории" такой защиты не было
    # вовсе. Теперь требуем то же самое: совпавшая сущность — необходимое,
    # но не достаточное условие, плюс минимальное пересечение общих слов
    # заголовка/текста (тот же порог 0.15, что и в is_same_event).
    exclude_entities = exclude_entities or set()
    c_entities = (extract_entity_stems(candidate_title) | extract_entity_stems(candidate_summary)) - exclude_entities
    if not c_entities:
        return None
    c_words = content_words(candidate_title, candidate_summary)
    now = time.time()
    best = None
    best_ts = -1
    for post in recent_posts:
        if not post.get("message_id"):
            continue
        ts = post.get("ts")
        if ts and now - ts > hours * 3600:
            continue
        p_entities = (extract_entity_stems(post.get("headline", "")) | extract_entity_stems(post.get("summary", ""))) - exclude_entities
        if not (c_entities & p_entities):
            continue
        p_words = content_words(post.get("headline", ""), post.get("summary", ""))
        if not titles_are_similar(c_words, p_words, threshold=0.15):
            continue
        if (ts or 0) >= best_ts:
            best = post
            best_ts = ts or 0
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
    # ФИКС (жалоба: "закреплённые новости цепляет все новости подряд") —
    # та же причина и то же решение, что и в find_story_continuation выше:
    # совпадение ОДНОГО именного стема (обрубок в 6 букв) — это не
    # достаточное доказательство того, что новость про то же самое
    # событие. Добавлена обязательная проверка пересечения слов темы
    # (заголовок эфира + текст всех его обновлений) в подкрепление
    # совпавшей сущности — иначе бы эфир мог "поглотить" любую новость,
    # где случайно встретился похожий обрубок слова.
    exclude_entities = exclude_entities or set()
    c_entities = (extract_entity_stems(candidate_title) | extract_entity_stems(candidate_summary)) - exclude_entities
    if not c_entities:
        return None
    c_words = content_words(candidate_title, candidate_summary)
    now = time.time()
    for thread in threads:
        if now - thread.get("last_update_ts", 0) > LIVE_STORY_MAX_AGE_HOURS * 3600:
            continue
        t_entities = set(thread.get("entities", [])) - exclude_entities
        if not (c_entities & t_entities):
            continue
        thread_text = thread.get("headline", "") + " " + " ".join(
            u.get("text", "") for u in thread.get("updates", [])
        )
        t_words = significant_title_words(thread_text)
        if not titles_are_similar(c_words, t_words, threshold=0.15):
            continue
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


# --- "Вселенского масштаба" фича: реальные метрики вовлечённости для
# продажи канала ---
# Число подписчиков — самая слабая метрика при оценке канала (легко
# накрутить, ничего не говорит о реальной активности аудитории).
# Настоящую ценность показывают ПРОСМОТРЫ постов — а Telegram публикует
# их прямо на открытой странице предпросмотра канала (t.me/s/<канал>),
# той же самой, которую бот уже парсит для источников. Дополнительно
# парсим СВОЙ ЖЕ канал тем же способом, копим историю просмотров по
# каждому посту и строим из этого профессиональный "Медиакит" — то, что
# реально смотрит покупатель канала, а не просто "у вас N подписчиков".
CHANNEL_VIEWS_FILE = "channel_views.json"
CHANNEL_VIEWS_LIMIT = 500


def _parse_view_count(text):
    if not text:
        return None
    t = text.strip().upper().replace(",", ".").replace(" ", "")
    try:
        if t.endswith("K"):
            return int(float(t[:-1]) * 1000)
        if t.endswith("M"):
            return int(float(t[:-1]) * 1_000_000)
        return int(t)
    except ValueError:
        return None


def fetch_own_channel_views(username=CHANNEL_USERNAME, limit=50):
    url = f"https://t.me/s/{username}"
    try:
        resp = requests.get(url, headers=TELEGRAM_PREVIEW_HEADERS, timeout=FETCH_TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        print(f"[WARN] fetch_own_channel_views failed: {e}")
        return {}
    soup = BeautifulSoup(resp.text, "html.parser")
    messages = soup.select("div.tgme_widget_message")[-limit:]
    result = {}
    for msg in messages:
        post_id = msg.get("data-post")
        if not post_id:
            continue
        views_el = msg.select_one(".tgme_widget_message_views")
        views = _parse_view_count(views_el.get_text(strip=True)) if views_el else None
        if views is not None:
            result[post_id] = views
    return result


def load_channel_views():
    return _load_json(CHANNEL_VIEWS_FILE, {})


def save_channel_views(data):
    # Храним только последние CHANNEL_VIEWS_LIMIT постов по времени первого
    # появления, чтобы файл не рос бесконечно.
    items = sorted(data.items(), key=lambda kv: kv[1].get("first_seen_ts", 0))
    trimmed = dict(items[-CHANNEL_VIEWS_LIMIT:])
    _save_json(CHANNEL_VIEWS_FILE, trimmed)


def update_channel_views_history():
    snapshot = fetch_own_channel_views()
    if not snapshot:
        return load_channel_views()
    history = load_channel_views()
    now = time.time()
    for post_id, views in snapshot.items():
        entry = history.get(post_id)
        if entry is None:
            history[post_id] = {"views": views, "first_seen_ts": now, "last_seen_ts": now}
        else:
            entry["views"] = max(entry.get("views", 0), views)
            entry["last_seen_ts"] = now
    save_channel_views(history)
    return history


def compute_engagement_stats(views_history):
    entries = sorted(views_history.values(), key=lambda e: e.get("first_seen_ts", 0))
    view_counts = [e["views"] for e in entries if isinstance(e.get("views"), (int, float))]
    if not view_counts:
        return {"tracked_posts": 0, "avg_views": None, "median_views": None,
                "total_views": 0, "trend_pct": None}
    total = sum(view_counts)
    avg = total / len(view_counts)
    sorted_counts = sorted(view_counts)
    mid = len(sorted_counts) // 2
    median = sorted_counts[mid] if len(sorted_counts) % 2 else (sorted_counts[mid - 1] + sorted_counts[mid]) / 2
    trend_pct = None
    if len(view_counts) >= 20:
        recent = view_counts[-10:]
        previous = view_counts[-20:-10]
        prev_avg = sum(previous) / len(previous)
        recent_avg = sum(recent) / len(recent)
        if prev_avg > 0:
            trend_pct = round((recent_avg - prev_avg) / prev_avg * 100, 1)
    return {
        "tracked_posts": len(view_counts),
        "avg_views": round(avg, 1),
        "median_views": median,
        "total_views": total,
        "trend_pct": trend_pct,
    }


# --- "Хочу RSS, но чтобы не было конфликта" — параллельный источник
# новостей из официальных RSS-лент федеральных изданий ---
# Полностью отдельный конфиг-файл (rss_feeds.txt) и отдельная функция
# сбора — никак не пересекается по коду с Telegram-скрейпингом. Все
# кандидаты, независимо от источника, проходят через ОДИН и тот же
# _process_candidate_entry() (см. ниже), поэтому дедуп/фильтры работают
# идентично и не могут разойтись между источниками. Сбой одной ленты
# (сеть, невалидный XML) ловится локально и не мешает остальным.
RSS_FEEDS_FILE = "rss_feeds.txt"
RSS_FETCH_LIMIT_PER_FEED = 20


def load_rss_feeds_list():
    if not os.path.exists(RSS_FEEDS_FILE):
        return []
    urls = []
    with open(RSS_FEEDS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if not (line.startswith("http://") or line.startswith("https://")):
                # Строка не похожа на URL — пропускаем её одну, а не всю
                # ленту: та же философия самодиагностики, что и для
                # feeds.txt, только здесь достаточно проверки построчно,
                # без риска "выключить" весь список из-за одной опечатки.
                print(f"[WARN] rss_feeds.txt: строка не похожа на URL, пропускаем: {line[:60]!r}")
                continue
            urls.append(line)
    return urls


def fetch_rss_feed(url, limit=RSS_FETCH_LIMIT_PER_FEED):
    try:
        resp = request_with_retry("GET", url, timeout=FETCH_TIMEOUT, headers=TELEGRAM_PREVIEW_HEADERS)
        resp.raise_for_status()
    except Exception as e:
        print(f"[WARN] RSS fetch failed for {url}: {e}")
        return []

    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(resp.content)
    except Exception as e:
        print(f"[WARN] RSS parse failed for {url}: {e}")
        return []

    channel = root.find("channel")
    items = channel.findall("item") if channel is not None else root.findall(".//item")

    entries = []
    for item in items[:limit]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        description_raw = item.findtext("description") or ""
        description = re.sub(r"<[^>]+>", "", html.unescape(description_raw)).strip()

        published = None
        pub_date_raw = item.findtext("pubDate")
        if pub_date_raw:
            try:
                from email.utils import parsedate_to_datetime
                published = parsedate_to_datetime(pub_date_raw).isoformat()
            except Exception:
                published = None

        photo = None
        enclosure = item.find("enclosure")
        if enclosure is not None:
            enc_type = enclosure.get("type", "") or ""
            enc_url = enclosure.get("url")
            if enc_url and enc_type.startswith("image"):
                photo = enc_url

        entries.append({
            "id": link,
            "title": title[:200],
            "summary": description,
            "link": link,
            "photo": photo,
            "photo_bytes": None,
            "video": None,
            "published": published or datetime.now().isoformat(),
            # Префикс "rss:" явно отличает источник от Telegram-username в
            # статистике (source_contribution на дашборде/медиаките) — не
            # смешивается и не путается с @channel.
            "source_channel": f"rss:{source_name(link) or url}",
        })
    return entries


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


def _process_candidate_entry(entry, posted, seen_title_keys, seen_items_meta, recent_content_words,
                              new_items, require_media, skip_not_news_filter, force_allow_no_media=False):
    # ФИКС ("хочу RSS, но чтобы не было конфликта"): раньше вся эта логика
    # жила прямо внутри цикла по Telegram-каналам в fetch_news(). Чтобы
    # добавить второй источник (RSS) без риска рассинхронизировать
    # правила фильтрации/дедупа между двумя источниками, вся проверка
    # кандидата вынесена в ОДНУ общую функцию — и Telegram-каналы, и
    # RSS-ленты проходят через неё одинаково. Правило для отдельного
    # источника — только force_allow_no_media (RSS официальных агентств
    # часто не даёт фото/видео, но сама новость уже проверена редакцией
    # источника, поэтому требование медиа для RSS не применяется).
    entry_id = entry.get("id") or entry.get("link")
    if not entry_id or entry_id in posted:
        return False

    title = html.unescape(entry.get("title", "Без заголовка"))
    title_key = title_dedup_key(title)
    if title_key in posted or title_key in seen_title_keys:
        return False

    if len(significant_title_words(title)) < 3:
        return False

    raw_summary = entry.get("summary", entry.get("description", ""))
    summary = strip_source_mentions(html.unescape(re.sub(r"<[^>]+>", "", raw_summary)))
    link = entry.get("link", "")
    source_channel = entry.get("source_channel") or ""

    c_words = content_words(title, summary)
    if is_duplicate_by_meaning(c_words, recent_content_words):
        return False

    dup_meta = next((m for m in seen_items_meta if titles_are_similar(c_words, m["words"])), None)
    if dup_meta is not None:
        if dup_meta["source_channel"] != source_channel:
            new_items[dup_meta["idx"]]["confirmed_multi_source"] = True
        return False

    if not skip_not_news_filter and is_not_news(title, summary):
        return False

    urgent = is_urgent(title, summary)
    photo = entry.get("photo")
    photo_bytes = entry.get("photo_bytes")
    video = entry.get("video")
    if require_media and not force_allow_no_media and not photo and not video and not urgent:
        return False

    src = f"@{source_channel}" if (source_channel and not source_channel.startswith("rss:")) else (
        source_channel[4:] if source_channel.startswith("rss:") else source_name(link)
    )
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
    return True


def fetch_news(require_media=True, skip_not_news_filter=False):
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
            _process_candidate_entry(entry, posted, seen_title_keys, seen_items_meta,
                                      recent_content_words, new_items, require_media, skip_not_news_filter)

    # --- RSS-источники (федеральные агентства) — второй, независимый
    # источник кандидатов, обрабатывается ТЕМ ЖЕ helper'ом выше, поэтому
    # дедуп/фильтры для него ничем не отличаются от Telegram-каналов.
    # Один сломанный RSS-фид (сеть, невалидный XML) не мешает остальным —
    # ошибка ловится внутри fetch_rss_feed и просто пропускается.
    if len(new_items) < FETCH_POOL_SIZE:
        rss_urls = load_rss_feeds_list()
        random.shuffle(rss_urls)
        for url in rss_urls:
            if len(new_items) >= FETCH_POOL_SIZE:
                break
            rss_entries = fetch_rss_feed(url)
            for entry in rss_entries:
                if len(new_items) >= FETCH_POOL_SIZE:
                    break
                _process_candidate_entry(entry, posted, seen_title_keys, seen_items_meta,
                                          recent_content_words, new_items, require_media,
                                          skip_not_news_filter, force_allow_no_media=True)

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
        matched_post = find_matching_post(it["title"], it.get("summary", ""), recent_posts_now,
                                           exclude_entities=common_entities)
        if matched_post is not None:
            update = find_updated_fact(it["title"], it.get("summary", ""), matched_post)
            if update is not None:
                kw, old_val, new_val = update
                print(f"[INFO] '{title_short}' — дубль по теме, но цифра «{kw}» выросла "
                      f"({old_val} → {new_val}) — публикуем как уточнение, а не пропускаем.")
                it["fact_update"] = {
                    "matched_post": matched_post, "keyword": kw,
                    "old_value": old_val, "new_value": new_val,
                }
                return it
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


def build_status_snapshot(last_publish_elapsed, sent_count=None, note="", self_check=None):
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
        "self_check": self_check or {},
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


# --- "Гениальная" фича: автоматическая хроника развивающихся историй ---
# Бот сам находит среди своих же опубликованных постов те, что на самом
# деле части ОДНОЙ большой истории (Эльбрус, конкретный теракт, серия
# атак на один город и т.п.) — используя те же именные стемы, что уже
# применяются для дедупа и "прямого эфира", но здесь не для проверки
# дублей, а для КЛАСТЕРИЗАЦИИ: строим граф "какие посты связаны общим
# местом/персоной" и ищем связные компоненты (union-find). Когда кластер
# набирает достаточно постов, GigaChat пишет из них связную хронику —
# не пересказ последнего поста, а синтез ВСЕЙ истории в хронологическом
# порядке, как делает редакция при подготовке разбора, а не бот, гонящий
# отдельные новости. Публикуется не чаще раза в сутки и никогда дважды
# для одного и того же набора постов (отслеживаем по "подписи" кластера).
STORY_TIMELINE_TIME = "19:00"  # МСК
STORY_TIMELINE_WINDOW_MINUTES = DIGEST_WINDOW_MINUTES
STORY_TIMELINE_STATE_FILE = "story_timeline_state.json"
MIN_STORY_TIMELINE_GAP_SECONDS = 20 * 3600
STORY_TIMELINE_MIN_POSTS = 4          # минимальный размер кластера
STORY_TIMELINE_LOOKBACK_DAYS = 14     # кластеризуем только недавние посты
STORY_TIMELINE_MAX_TRACKED_SIGNATURES = 200


def due_story_timeline(dt, already_sent_today):
    if already_sent_today:
        return False
    now_minutes = dt.hour * 60 + dt.minute
    slot_h, slot_m = map(int, STORY_TIMELINE_TIME.split(":"))
    slot_minutes = slot_h * 60 + slot_m
    return 0 <= (now_minutes - slot_minutes) <= STORY_TIMELINE_WINDOW_MINUTES


def build_entity_clusters(recent_posts, exclude_entities=None, lookback_days=STORY_TIMELINE_LOOKBACK_DAYS):
    exclude_entities = exclude_entities or set()
    now = time.time()
    eligible = [
        p for p in recent_posts
        if p.get("message_id") and (not p.get("ts") or now - p["ts"] <= lookback_days * 24 * 3600)
    ]
    n = len(eligible)
    if n == 0:
        return []

    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    entity_sets = [
        (extract_entity_stems(p.get("headline", "")) | extract_entity_stems(p.get("summary", ""))) - exclude_entities
        for p in eligible
    ]
    for i in range(n):
        if not entity_sets[i]:
            continue
        for j in range(i + 1, n):
            if entity_sets[i] & entity_sets[j]:
                union(i, j)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(eligible[i])
    return [sorted(g, key=lambda p: p.get("ts", 0)) for g in groups.values()]


def cluster_signature(cluster):
    return tuple(sorted(p["message_id"] for p in cluster if p.get("message_id")))


def pick_story_timeline_cluster(recent_posts, already_published_signatures, exclude_entities=None,
                                 min_posts=STORY_TIMELINE_MIN_POSTS):
    clusters = build_entity_clusters(recent_posts, exclude_entities=exclude_entities)
    candidates = [c for c in clusters if len(c) >= min_posts]
    candidates = [c for c in candidates if cluster_signature(c) not in already_published_signatures]
    if not candidates:
        return None
    candidates.sort(key=lambda c: (len(c), c[-1].get("ts", 0)), reverse=True)
    return candidates[0]


def build_story_timeline_via_ai(cluster):
    token = get_gigachat_token()
    if not token or len(cluster) < 2:
        return None
    try:
        listing_lines = []
        for p in cluster:
            ts = p.get("ts")
            time_label = datetime.fromtimestamp(ts + MSK_OFFSET.total_seconds()).strftime("%d.%m %H:%M") if ts else "?"
            listing_lines.append(f"[{time_label}] {p.get('headline', '')} — {p.get('summary', '')[:200]}")
        listing = "\n".join(listing_lines)
        prompt = (
            "Ниже — посты одного новостного Telegram-канала об ОДНОЙ и той же "
            "развивающейся истории, в хронологическом порядке (метка времени "
            "дд.мм чч:мм — заголовок — краткое содержание):\n\n" + listing + "\n\n"
            "Напиши связную хронику этой истории для читателей, которые могли "
            "пропустить отдельные посты. Формат:\n"
            "1) Короткий заголовок хроники (до 60 символов, без кавычек).\n"
            "2) 4-7 хронологических пунктов вида «дд.мм чч:мм — что произошло», "
            "используя ТОЛЬКО факты из текста выше, без домыслов.\n"
            "3) Одно заключительное предложение — что известно на данный момент "
            "и что означает эта история в целом.\n\n"
            "Ответь СТРОГО в формате:\n"
            "ЗАГОЛОВОК: <текст>\n"
            "ХРОНИКА:\n<пункты>\n"
            "ИТОГ: <текст>"
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
            "max_tokens": 700,
        }
        resp = request_with_retry("POST", GIGACHAT_CHAT_URL, headers=headers, json=payload, verify=False, timeout=(5, 20))
        resp.raise_for_status()
        answer = resp.json()["choices"][0]["message"]["content"].strip()
        title_match = re.search(r"ЗАГОЛОВОК:\s*(.+)", answer)
        chronicle_match = re.search(r"ХРОНИКА:\s*(.+?)(?=\n\s*ИТОГ:|\Z)", answer, re.S)
        summary_match = re.search(r"ИТОГ:\s*(.+)", answer)
        if not (title_match and chronicle_match):
            return None
        return {
            "title": title_match.group(1).strip(),
            "chronicle": chronicle_match.group(1).strip(),
            "summary": summary_match.group(1).strip() if summary_match else "",
        }
    except Exception as e:
        print(f"[WARN] build_story_timeline_via_ai error: {e}")
        return None


def format_story_timeline(ai_result, cluster):
    lines = [f"🧵 <b>Хроника: {html.escape(ai_result['title'])}</b>", ""]
    chronicle_clean = sanitize_text(ai_result["chronicle"])
    lines.append(html.escape(chronicle_clean))
    if ai_result.get("summary"):
        lines.append("")
        lines.append(f"💡 {html.escape(sanitize_text(ai_result['summary']))}")
    lines.append("")
    links = []
    for p in cluster:
        msg_id = p.get("message_id")
        if msg_id:
            links.append(f'<a href="https://t.me/{CHANNEL_USERNAME}/{msg_id}">{len(links) + 1}</a>')
    if links:
        lines.append("Все посты по теме: " + " · ".join(links))
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
MEDIA_KIT_FILE = os.path.join(DOCS_DIR, "media-kit.html")
RSS_FEED_FILE = os.path.join(DOCS_DIR, "rss.xml")
CORRECTIONS_LOG_FILE = "corrections_log.json"
CORRECTIONS_PAGE_FILE = os.path.join(DOCS_DIR, "corrections.html")
EDITORIAL_POLICY_PAGE_FILE = os.path.join(DOCS_DIR, "o-redakcii.html")
ENTITY_INDEX_FILE = "entity_index.json"
DOSSIERS_INDEX_FILE = os.path.join(DOCS_DIR, "dossiers.html")
DOSSIERS_DIR = os.path.join(DOCS_DIR, "dossiers")
MIN_MENTIONS_FOR_DOSSIER = 3
MAX_DOSSIER_PROFILE_UPDATES_PER_RUN = 2
DOSSIER_TIME = "21:30"  # МСК
DOSSIER_WINDOW_MINUTES = 4
DOSSIER_STATE_FILE = "dossier_schedule_state.json"
MIN_DOSSIER_GAP_SECONDS = 20 * 3600
CORRECTIONS_LOG_LIMIT = 300


def _svg_sparkline(history, width=640, height=140, pad=24, uid="a"):
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
    area_points = f"{x(0):.1f},{height - pad} " + points + f" {x(n-1):.1f},{height - pad}"
    last_x, last_y = x(n - 1), y(values[-1])
    first_date = html.escape(history[0]["date"])
    last_date = html.escape(history[-1]["date"])
    grad_id = f"sparkgrad-{uid}"
    glow_id = f"sparkglow-{uid}"
    return f"""
    <svg viewBox="0 0 {width} {height}" class="sparkline" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <linearGradient id="{grad_id}" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#4fd1c5" stop-opacity="0.35"/>
          <stop offset="100%" stop-color="#4fd1c5" stop-opacity="0"/>
        </linearGradient>
        <filter id="{glow_id}" x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur stdDeviation="3" result="blur"/>
          <feMerge>
            <feMergeNode in="blur"/>
            <feMergeNode in="SourceGraphic"/>
          </feMerge>
        </filter>
      </defs>
      <polygon points="{area_points}" fill="url(#{grad_id})" stroke="none"/>
      <polyline class="spark-line" fill="none" stroke="#4fd1c5" stroke-width="2.5"
                stroke-linecap="round" stroke-linejoin="round" points="{points}" filter="url(#{glow_id})"/>
      <circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="5" fill="#4fd1c5" filter="url(#{glow_id})">
        <animate attributeName="r" values="4;6;4" dur="2s" repeatCount="indefinite"/>
      </circle>
      <text x="{pad}" y="{height - 4}" class="spark-label">{first_date}</text>
      <text x="{width - pad}" y="{height - 4}" class="spark-label" text-anchor="end">{last_date}</text>
    </svg>
    """


def build_dashboard_html(status, recent_posts, subscriber_history, source_contribution, generated_at_msk,
                          self_heal_log=None):
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

    self_check = status.get("self_check") or {}
    feeds_ok = self_check.get("feeds_ok")
    telegram_ok = self_check.get("telegram_ok")
    if feeds_ok is None and telegram_ok is None:
        self_check_badge = "— нет данных —"
    else:
        parts = []
        parts.append("🟢 Список каналов" if feeds_ok else "🔴 Список каналов")
        parts.append("🟢 Telegram-токен" if telegram_ok else "🔴 Telegram-токен")
        self_check_badge = " · ".join(parts)

    self_heal_log = self_heal_log or []
    self_heal_rows = "".join(
        f'<li>{html.escape(datetime.fromtimestamp(e["ts"]).strftime("%d.%m %H:%M"))} — '
        f'{html.escape(e.get("message", ""))}</li>'
        for e in list(reversed(self_heal_log))[:6]
    ) if self_heal_log else '<li class="muted">Пока не потребовалось ни одного автоматического исправления</li>'

    current_subs = subscriber_history[-1]["count"] if subscriber_history else None
    sparkline_svg = _svg_sparkline(subscriber_history, uid="dash")

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
    --bg: #0a0e14; --card: rgba(22, 29, 39, 0.6); --border: rgba(255,255,255,0.08);
    --text: #eef2f6; --muted: #8b96a5; --accent: #4fd1c5; --accent2: #7c5cff;
  }}
  * {{ box-sizing: border-box; }}
  html {{ scroll-behavior: smooth; }}
  body {{
    margin: 0; padding: 40px 16px 72px; color: var(--text); min-height: 100vh;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background:
      radial-gradient(circle at 15% 0%, rgba(124,92,255,0.16), transparent 45%),
      radial-gradient(circle at 85% 15%, rgba(79,209,197,0.14), transparent 45%),
      radial-gradient(circle at 50% 100%, rgba(79,209,197,0.08), transparent 55%),
      var(--bg);
    background-attachment: fixed;
  }}
  .wrap {{ max-width: 900px; margin: 0 auto; }}
  h1 {{
    font-size: 1.7rem; margin-bottom: 4px; font-weight: 800; letter-spacing: -0.02em;
    background: linear-gradient(120deg, #ffffff 20%, var(--accent) 60%, var(--accent2) 100%);
    -webkit-background-clip: text; background-clip: text; color: transparent;
    display: inline-flex; align-items: center; gap: 10px;
  }}
  .subtitle {{ color: var(--muted); margin-top: 2px; margin-bottom: 32px; font-size: 0.92rem; }}
  .subtitle a {{ transition: color 0.2s ease; }}
  .subtitle a:hover {{ color: var(--accent); }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 20px; }}
  .card {{
    background: var(--card); border: 1px solid var(--border); border-radius: 16px;
    padding: 20px 22px; backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px);
    box-shadow: 0 4px 24px rgba(0,0,0,0.25);
    transition: transform 0.25s ease, box-shadow 0.25s ease, border-color 0.25s ease;
    position: relative; overflow: hidden;
  }}
  .card::before {{
    content: ""; position: absolute; inset: 0; border-radius: 16px; padding: 1px;
    background: linear-gradient(135deg, rgba(79,209,197,0.35), rgba(124,92,255,0.05) 40%, transparent 70%);
    -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
    -webkit-mask-composite: xor; mask-composite: exclude; pointer-events: none;
  }}
  .card:hover {{
    transform: translateY(-3px);
    box-shadow: 0 12px 40px rgba(0,0,0,0.4), 0 0 0 1px rgba(79,209,197,0.25);
    border-color: rgba(79,209,197,0.3);
  }}
  .card .label {{ color: var(--muted); font-size: 0.76rem; text-transform: uppercase; letter-spacing: 0.06em; }}
  .card .value {{
    font-size: 1.7rem; font-weight: 700; margin-top: 8px;
    background: linear-gradient(120deg, #fff, var(--accent));
    -webkit-background-clip: text; background-clip: text; color: transparent;
  }}
  .card.wide {{ grid-column: 1 / -1; }}
  .sparkline {{ width: 100%; height: auto; margin-top: 8px; overflow: visible; }}
  .spark-line {{ animation: draw-line 1.4s ease-out forwards; }}
  @keyframes draw-line {{ from {{ opacity: 0; transform: scaleY(0.85); }} to {{ opacity: 1; transform: scaleY(1); }} }}
  .spark-label {{ fill: var(--muted); font-size: 11px; }}
  .bar-row {{ display: flex; align-items: center; gap: 10px; margin: 10px 0; }}
  .bar-label {{ width: 140px; font-size: 0.85rem; color: var(--muted); flex-shrink: 0; }}
  .bar-track {{ flex: 1; background: rgba(255,255,255,0.06); border-radius: 8px; height: 10px; overflow: hidden; }}
  .bar-fill {{
    background: linear-gradient(90deg, var(--accent2), var(--accent));
    height: 100%; border-radius: 8px; box-shadow: 0 0 12px rgba(79,209,197,0.5);
    width: 0; transition: width 1.1s cubic-bezier(.2,.8,.2,1);
  }}
  .bar-count {{ width: 36px; text-align: right; font-size: 0.85rem; color: var(--muted); }}
  ul {{ list-style: none; padding: 0; margin: 8px 0 0; }}
  li {{ padding: 9px 0; border-bottom: 1px solid var(--border); font-size: 0.92rem; }}
  li:last-child {{ border-bottom: none; }}
  .cat-emoji {{ margin-right: 8px; }}
  .muted {{ color: var(--muted); }}
  .status-line {{ display: flex; align-items: center; gap: 10px; font-size: 1.1rem; }}
  .pulse-dot {{
    width: 11px; height: 11px; border-radius: 50%; flex-shrink: 0;
    background: {"#22c55e" if healthy else "#ef4444"};
    box-shadow: 0 0 0 0 {"rgba(34,197,94,0.6)" if healthy else "rgba(239,68,68,0.6)"};
    animation: pulse 2s infinite;
  }}
  @keyframes pulse {{
    0% {{ box-shadow: 0 0 0 0 {"rgba(34,197,94,0.55)" if healthy else "rgba(239,68,68,0.55)"}; }}
    70% {{ box-shadow: 0 0 0 12px rgba(0,0,0,0); }}
    100% {{ box-shadow: 0 0 0 0 rgba(0,0,0,0); }}
  }}
  footer {{ color: var(--muted); font-size: 0.8rem; margin-top: 36px; text-align: center; }}
  a {{ color: var(--accent); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>📡 {html.escape(CHANNEL_USERNAME)}</h1>
  <p class="subtitle">Живая статистика новостного канала · <a href="{html.escape(CHANNEL_LINK)}">открыть канал</a> · <a href="media-kit.html">медиакит</a> · <a href="corrections.html">архив уточнений</a> · <a href="o-redakcii.html">редполитика</a> · <a href="dossiers.html">база знаний</a> · <a href="rss.xml">RSS</a></p>

  <div class="grid">
    <div class="card">
      <div class="label">Статус</div>
      <div class="value status-line" style="font-size:1.1rem"><span class="pulse-dot"></span>{("Работает штатно" if healthy else "Возможен сбой")}</div>
    </div>
    <div class="card">
      <div class="label">Последняя публикация</div>
      <div class="value" style="font-size:1.1rem">{last_publish_label}</div>
    </div>
    <div class="card">
      <div class="label">Подписчиков</div>
      <div class="value" data-count="{current_subs if current_subs is not None else 0}">{current_subs if current_subs is not None else "—"}</div>
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

  <div class="card wide">
    <div class="label">Автономная диагностика</div>
    <div class="value" style="font-size:1.05rem; margin-bottom:10px;">{self_check_badge}</div>
    <div class="label" style="margin-top:8px;">Последние автоматические исправления</div>
    <ul>{self_heal_rows}</ul>
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
<script>
  // Плавная заливка полос "вклад источников" при загрузке страницы.
  document.querySelectorAll('.bar-fill').forEach(function(el, i) {{
    var target = el.style.width;
    el.style.width = '0%';
    setTimeout(function() {{ el.style.width = target; }}, 80 + i * 60);
  }});
  // Анимированный счётчик подписчиков.
  document.querySelectorAll('[data-count]').forEach(function(el) {{
    var target = parseInt(el.getAttribute('data-count'), 10);
    if (!target) return;
    var start = 0, duration = 900, startTime = null;
    function step(ts) {{
      if (!startTime) startTime = ts;
      var progress = Math.min((ts - startTime) / duration, 1);
      var eased = 1 - Math.pow(1 - progress, 3);
      el.textContent = Math.round(start + (target - start) * eased);
      if (progress < 1) requestAnimationFrame(step);
      else el.textContent = target;
    }}
    requestAnimationFrame(step);
  }});
</script>
</body>
</html>"""



def build_media_kit_html(status, subscriber_history, engagement_stats, self_heal_log, generated_at_msk):
    status = status or {}
    current_subs = subscriber_history[-1]["count"] if subscriber_history else None
    first_subs = subscriber_history[0]["count"] if subscriber_history else None
    growth_pct = None
    if current_subs is not None and first_subs and first_subs > 0:
        growth_pct = round((current_subs - first_subs) / first_subs * 100, 1)
    sparkline_svg = _svg_sparkline(subscriber_history, uid="media")

    median_latency = status.get("median_publish_latency_minutes")
    healthy = status.get("healthy")
    uptime_label = "Стабильно, без длительных простоев" if healthy else "Были перебои — см. дашборд"

    avg_views = engagement_stats.get("avg_views")
    median_views = engagement_stats.get("median_views")
    total_views = engagement_stats.get("total_views") or 0
    tracked_posts = engagement_stats.get("tracked_posts") or 0
    trend_pct = engagement_stats.get("trend_pct")
    trend_label = (
        f"{'▲' if trend_pct >= 0 else '▼'} {abs(trend_pct):.1f}% к предыдущим 10 постам"
        if trend_pct is not None else "накапливаем историю"
    )

    features = [
        "Публикация из 20+ проверенных источников с приоритетом официальных агентств (ТАСС)",
        "Многоуровневый дедуп: по смыслу, по именным сущностям и через ИИ-сверку — не дублирует уже освещённые события",
        "Автоматическое обнаружение изменившихся фактов (число погибших/пострадавших) — публикует явные уточнения, а не тихо теряет обновления",
        "«Прямые эфиры» — развивающиеся истории обновляются в одном закреплённом посте, а не спамят лентой",
        "Автоматическая хроника: бот сам находит связанные посты за 2 недели и синтезирует связный разбор истории через ИИ",
        "Ежедневный опрос, викторина по реальным новостям канала и еженедельный дайджест для вовлечения аудитории",
        "Утренняя сводка по рынкам (курсы ЦБ, индекс МосБиржи) — не только новости, но и деловая ценность",
        "Публичный live-дашборд статистики и автономный слой самодиагностики: бот сам обнаруживает и чинит сбои (отозванный токен, испорченные конфиги, зависшие процессы)",
        "Гарантированная частота публикаций (аварийный режим не даёт каналу замолчать надолго)",
    ]
    features_html = "".join(f"<li>{html.escape(f)}</li>" for f in features)

    self_heal_count = len(self_heal_log or [])

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(CHANNEL_USERNAME)} — медиакит</title>
<style>
  :root {{
    --bg: #0b0f14; --card: #151b23; --border: #2a323d;
    --text: #eef2f6; --muted: #93a0ad; --accent: #f0b429; --accent2: #4fd1c5;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 48px 20px 72px; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  .wrap {{ max-width: 920px; margin: 0 auto; }}
  .eyebrow {{ color: var(--accent); text-transform: uppercase; letter-spacing: 0.1em; font-size: 0.8rem; font-weight: 600; }}
  h1 {{ font-size: 2.2rem; margin: 8px 0 4px; }}
  .subtitle {{ color: var(--muted); margin: 0 0 36px; font-size: 1.05rem; }}
  .stat-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 16px; margin-bottom: 28px; }}
  .stat-card {{ background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 20px; }}
  .stat-card .label {{ color: var(--muted); font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.05em; }}
  .stat-card .value {{ font-size: 1.9rem; font-weight: 700; margin-top: 8px; color: var(--accent2); }}
  .stat-card .sub {{ color: var(--muted); font-size: 0.85rem; margin-top: 4px; }}
  .section {{ background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 24px 26px; margin-bottom: 20px; }}
  .section h2 {{ font-size: 1.1rem; margin: 0 0 14px; color: var(--accent); }}
  .sparkline {{ width: 100%; height: auto; margin-top: 6px; }}
  .spark-label {{ fill: var(--muted); font-size: 11px; }}
  ul.features {{ list-style: none; padding: 0; margin: 0; }}
  ul.features li {{ padding: 10px 0 10px 28px; border-bottom: 1px solid var(--border); position: relative; font-size: 0.95rem; }}
  ul.features li:last-child {{ border-bottom: none; }}
  ul.features li::before {{ content: "✓"; position: absolute; left: 0; color: var(--accent2); font-weight: 700; }}
  footer {{ color: var(--muted); font-size: 0.8rem; margin-top: 32px; text-align: center; }}
  a {{ color: var(--accent2); }}
</style>
</head>
<body>
<div class="wrap">
  <div class="eyebrow">Медиакит канала</div>
  <h1>📡 {html.escape(CHANNEL_USERNAME)}</h1>
  <p class="subtitle">Автоматизированный новостной канал полного цикла — от сбора до аналитики.
  <a href="{html.escape(CHANNEL_LINK)}">Открыть канал</a> ·
  <a href="index.html">Живой дашборд</a></p>

  <div class="stat-grid">
    <div class="stat-card">
      <div class="label">Подписчиков</div>
      <div class="value">{current_subs if current_subs is not None else "—"}</div>
      <div class="sub">{f"рост {growth_pct:+.1f}% за период наблюдения" if growth_pct is not None else "накапливаем историю"}</div>
    </div>
    <div class="stat-card">
      <div class="label">Просмотров на пост (медиана)</div>
      <div class="value">{median_views if median_views is not None else "—"}</div>
      <div class="sub">{trend_label}</div>
    </div>
    <div class="stat-card">
      <div class="label">Всего просмотров отслежено</div>
      <div class="value">{total_views:,}</div>
      <div class="sub">по {tracked_posts} последним постам</div>
    </div>
    <div class="stat-card">
      <div class="label">Скорость публикации</div>
      <div class="value">{f"{median_latency:.0f} мин" if median_latency is not None else "—"}</div>
      <div class="sub">от появления новости у источника</div>
    </div>
  </div>

  <div class="section">
    <h2>Рост аудитории</h2>
    {sparkline_svg or '<p style="color:var(--muted)">Накапливаем историю — график появится через несколько дней</p>'}
  </div>

  <div class="section">
    <h2>Надёжность инфраструктуры</h2>
    <p style="margin:0 0 6px">{uptime_label}</p>
    <p style="margin:0; color:var(--muted); font-size:0.9rem">
      Автономная система самодиагностики выполнила {self_heal_count} автоматических
      исправлений без вмешательства человека — полный журнал доступен на
      <a href="index.html">дашборде</a>.
    </p>
  </div>

  <div class="section">
    <h2>Технологический стек и конкурентные преимущества</h2>
    <ul class="features">{features_html}</ul>
  </div>

  <footer>
    Страница формируется автоматически ботом на основе собственных данных канала ·
    последнее обновление: {html.escape(generated_at_msk)} мск<br>
    Это информационная сводка, а не формальная оценка стоимости актива.
  </footer>
</div>
</body>
</html>"""


# --- "Только у федеральных каналов": RSS-синдикация, публичный архив
# опровержений и редакционная политика ---
# Небольшие агрегаторы почти никогда этого не публикуют — а у серьёзных
# изданий (и это буквально требование многих агрегаторов новостей и
# рекламных сетей) есть: (1) машиночитаемая RSS-лента для синдикации,
# (2) публичный, постоянный архив исправлений/уточнений — стандарт
# AP/Reuters/NYT, а не просто "тихо поправили пост", и (3) открыто
# опубликованная редакционная политика/методология. Всё это — реальные
# признаки профессионального медиа, которые проверяют при оценке актива.
def _rfc822_date(ts):
    if not ts:
        return ""
    return datetime.utcfromtimestamp(ts).strftime("%a, %d %b %Y %H:%M:%S +0000")


def build_rss_feed(recent_posts, generated_at_msk):
    items_xml = []
    for p in list(reversed(recent_posts))[:60]:
        msg_id = p.get("message_id")
        if not msg_id:
            continue
        link = f"https://t.me/{CHANNEL_USERNAME}/{msg_id}"
        title = html.escape(p.get("headline", "") or p.get("title", "") or "")
        description = html.escape((p.get("summary", "") or "")[:500])
        pub_date = _rfc822_date(p.get("ts"))
        items_xml.append(
            "    <item>\n"
            f"      <title>{title}</title>\n"
            f"      <link>{link}</link>\n"
            f"      <guid isPermaLink=\"true\">{link}</guid>\n"
            f"      <description>{description}</description>\n"
            f"      <pubDate>{pub_date}</pubDate>\n"
            "    </item>"
        )
    items_block = "\n".join(items_xml)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>{html.escape(CHANNEL_USERNAME)}</title>
    <link>{html.escape(CHANNEL_LINK)}</link>
    <description>Автоматизированный новостной канал — RSS-синдикация последних публикаций</description>
    <language>ru</language>
    <lastBuildDate>{html.escape(generated_at_msk)}</lastBuildDate>
{items_block}
  </channel>
</rss>"""


def load_corrections_log():
    return _load_json(CORRECTIONS_LOG_FILE, [])


def save_corrections_log(entries):
    _save_json(CORRECTIONS_LOG_FILE, entries[-CORRECTIONS_LOG_LIMIT:])


def record_correction(original_headline, keyword, old_value, new_value, message_id):
    log = load_corrections_log()
    log.append({
        "ts": time.time(),
        "original_headline": original_headline,
        "keyword": keyword,
        "old_value": old_value,
        "new_value": new_value,
        "message_id": message_id,
    })
    save_corrections_log(log)


def build_corrections_page(corrections_log, generated_at_msk):
    rows = "".join(
        f'<tr><td>{html.escape(datetime.fromtimestamp(c["ts"]).strftime("%d.%m.%Y %H:%M"))}</td>'
        f'<td>{html.escape(c.get("original_headline", ""))}</td>'
        f'<td>{html.escape(FACT_UPDATE_LABELS.get(c.get("keyword", ""), c.get("keyword", "")))}</td>'
        f'<td>{c.get("old_value", "")} → {c.get("new_value", "")}</td></tr>'
        for c in reversed(corrections_log)
    ) if corrections_log else '<tr><td colspan="4" style="color:#93a0ad">Пока не потребовалось ни одного уточнения — все опубликованные факты остаются актуальными.</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(CHANNEL_USERNAME)} — архив уточнений</title>
<style>
  body {{ margin:0; padding:40px 20px 64px; background:#0b0f14; color:#eef2f6;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
  .wrap {{ max-width:880px; margin:0 auto; }}
  h1 {{ font-size:1.6rem; }}
  p.subtitle {{ color:#93a0ad; }}
  table {{ width:100%; border-collapse:collapse; margin-top:20px; }}
  th, td {{ text-align:left; padding:10px 12px; border-bottom:1px solid #2a323d; font-size:0.92rem; }}
  th {{ color:#93a0ad; font-size:0.78rem; text-transform:uppercase; letter-spacing:0.04em; }}
  a {{ color:#4fd1c5; }}
  footer {{ color:#93a0ad; font-size:0.8rem; margin-top:32px; text-align:center; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>📋 Архив уточнений</h1>
  <p class="subtitle">Публичный и постоянный журнал всех случаев, когда ключевой факт
  (число погибших/раненых/пострадавших) в опубликованной новости менялся после
  выхода поста — по стандарту крупных информагентств. Ничего не удаляется и не
  скрывается. · <a href="index.html">← Дашборд</a></p>
  <table>
    <tr><th>Когда</th><th>Исходная новость</th><th>Что уточнено</th><th>Было → стало</th></tr>
    {rows}
  </table>
  <footer>Страница формируется автоматически · последнее обновление: {html.escape(generated_at_msk)} мск</footer>
</div>
</body>
</html>"""


EDITORIAL_POLICY_TEXT_RU = """
<h2>Источники и верификация</h2>
<p>Канал агрегирует новости из более чем 20 проверенных Telegram-источников,
включая федеральные информагентства. Каждая новость проходит многоуровневую
проверку: сопоставление по смыслу и по именным сущностям (место, персона,
организация) с уже опубликованными материалами, а при необходимости —
дополнительную смысловую сверку через ИИ, чтобы не публиковать одно и то же
событие дважды под разными формулировками.</p>

<h2>Политика уточнений и исправлений</h2>
<p>Если после публикации новости ключевой факт (число погибших, раненых,
пострадавших) меняется, канал публикует явное уточнение со ссылкой на исходный
пост — а не молча редактирует или замалчивает изменение. Полный и постоянный
архив всех уточнений доступен по ссылке "Архив уточнений" и никогда не
удаляется.</p>

<h2>Срочные новости и обновления</h2>
<p>Развивающиеся события (природные и техногенные происшествия, атаки,
чрезвычайные ситуации) освещаются в формате единого обновляемого поста
("прямой эфир"), чтобы не дублировать один и тот же сюжет множеством
разрозненных публикаций.</p>

<h2>Нейтральность</h2>
<p>Канал не выражает собственную оценочную позицию по излагаемым событиям и
не использует эмоционально окрашенную лексику при пересказе источников.
Заголовки формулируются по фактам, без домыслов сверх того, что сообщил
первоисточник.</p>

<h2>Автоматизация и человеческий контроль</h2>
<p>Публикация и часть редактуры выполняются автоматизированной системой.
Система включает автономный слой самодиагностики (проверка целостности
списка источников, работоспособности каналов публикации, очистка устаревших
служебных сообщений) и ведёт публичный журнал собственной работы.</p>
"""


def build_editorial_policy_page(generated_at_msk):
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(CHANNEL_USERNAME)} — редакционная политика</title>
<style>
  body {{ margin:0; padding:40px 20px 64px; background:#0b0f14; color:#eef2f6;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; line-height:1.6; }}
  .wrap {{ max-width:760px; margin:0 auto; }}
  h1 {{ font-size:1.6rem; }}
  h2 {{ font-size:1.05rem; color:#f0b429; margin-top:28px; }}
  p {{ color:#d7dee5; }}
  p.subtitle {{ color:#93a0ad; }}
  a {{ color:#4fd1c5; }}
  footer {{ color:#93a0ad; font-size:0.8rem; margin-top:32px; text-align:center; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>📜 Редакционная политика</h1>
  <p class="subtitle"><a href="index.html">← Дашборд</a> · <a href="corrections.html">Архив уточнений</a></p>
  {EDITORIAL_POLICY_TEXT_RU}
  <footer>Опубликовано автоматически · последнее обновление: {html.escape(generated_at_msk)} мск</footer>
</div>
</body>
</html>"""


# --- "То, за что ценят федеральные каналы": автоматическая база знаний
# / система досье по повторяющимся персонам, местам и организациям ---
# Обычный агрегатор публикует новости и забывает их. Серьёзное издание
# накапливает СМЫСЛ: у Bloomberg/Reuters есть постоянные "профили" тем и
# персон, куда стекаются все упоминания. Бот строит это сам: проходит по
# всей своей истории постов, находит именные сущности (те же стемы, что
# уже используются для дедупа и кластеризации историй), но теперь не
# просто исключает совпадения — а СОБИРАЕТ каждое упоминание в единое
# досье, и раз в сутки просит GigaChat синтезировать из разрозненных
# фактов связный профиль: кто/что это, какая роль в описываемых
# событиях, как менялась история со временем. Получается растущая со
# временем энциклопедия — то, что превращает поток новостей в
# структурированное знание, а не просто ленту.
def extract_entity_mentions_with_names(text):
    # То же самое, что extract_entity_stems, но сохраняет ИСХОДНОЕ слово
    # (с оригинальным регистром) для каждого стема — нужно для красивого
    # отображения имени в досье, а не обрубленного 6-буквенного стема.
    if not text:
        return {}
    words = re.findall(r'[А-ЯЁ][а-яё]+', text)
    result = {}
    for w in words:
        wl = w.lower()
        if len(wl) <= 3 or wl in TITLE_STOPWORDS:
            continue
        stem = wl[:6] if len(wl) > 6 else wl
        if stem in COMMON_ENTITY_STOPWORD_STEMS:
            continue
        result.setdefault(stem, w)
    return result


def load_entity_index():
    return _load_json(ENTITY_INDEX_FILE, {})


def save_entity_index(index):
    _save_json(ENTITY_INDEX_FILE, index)


def build_entity_index(recent_posts, exclude_entities=None):
    exclude_entities = exclude_entities or set()
    index = {}
    for post in recent_posts:
        msg_id = post.get("message_id")
        if not msg_id:
            continue
        mentions = extract_entity_mentions_with_names(post.get("headline", "") or post.get("summary", ""))
        mentions.update(extract_entity_mentions_with_names(post.get("summary", "")))
        for stem, display_name in mentions.items():
            if stem in exclude_entities:
                continue
            entry = index.setdefault(stem, {"display_name": display_name, "posts": []})
            if not any(p.get("message_id") == msg_id for p in entry["posts"]):
                entry["posts"].append({
                    "message_id": msg_id,
                    "headline": post.get("headline", ""),
                    "ts": post.get("ts", 0),
                })
    for entry in index.values():
        entry["posts"].sort(key=lambda p: p.get("ts", 0))
    return index


def merge_entity_index(remote_index, local_index):
    merged = {}
    for stem in set(remote_index) | set(local_index):
        remote_entry = remote_index.get(stem, {})
        local_entry = local_index.get(stem, {})
        posts_by_id = {}
        for p in remote_entry.get("posts", []) + local_entry.get("posts", []):
            if p.get("message_id"):
                posts_by_id[p["message_id"]] = p
        posts = sorted(posts_by_id.values(), key=lambda p: p.get("ts", 0))
        # Профиль сохраняем от того, кто обновлялся позже.
        profile = None
        profile_updated_ts = 0
        for entry in (remote_entry, local_entry):
            if entry.get("profile") and entry.get("profile_updated_ts", 0) >= profile_updated_ts:
                profile = entry["profile"]
                profile_updated_ts = entry.get("profile_updated_ts", 0)
        merged[stem] = {
            "display_name": local_entry.get("display_name") or remote_entry.get("display_name") or stem,
            "posts": posts,
            "profile": profile,
            "profile_updated_ts": profile_updated_ts,
        }
    return merged


def build_entity_profile_via_ai(display_name, posts):
    token = get_gigachat_token()
    if not token or len(posts) < MIN_MENTIONS_FOR_DOSSIER:
        return None
    try:
        listing = "\n".join(
            f"- {html.unescape(p.get('headline', ''))}" for p in posts[-30:]
        )
        prompt = (
            f"Ниже — заголовки постов новостного канала, где упоминается "
            f"«{display_name}», в хронологическом порядке:\n\n{listing}\n\n"
            "Напиши короткий нейтральный профиль (3-5 предложений): кто или "
            "что это, в каком контексте фигурирует в этих новостях, как "
            "развивалась связанная с этим история во времени. Используй "
            "ТОЛЬКО факты из заголовков выше, ничего не домысливай сверх "
            "них. Без оценочных суждений. Ответь только текстом профиля, "
            "без заголовков и пояснений."
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
            "max_tokens": 350,
        }
        resp = request_with_retry("POST", GIGACHAT_CHAT_URL, headers=headers, json=payload, verify=False, timeout=(5, 20))
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[WARN] build_entity_profile_via_ai error: {e}")
        return None


def _entity_slug(stem):
    return re.sub(r'[^a-z0-9а-яё]', '', stem.lower()) or "x"


def build_dossiers_index_page(entity_index, generated_at_msk):
    notable = [
        (stem, e) for stem, e in entity_index.items()
        if len(e.get("posts", [])) >= MIN_MENTIONS_FOR_DOSSIER
    ]
    notable.sort(key=lambda kv: len(kv[1]["posts"]), reverse=True)
    rows = "".join(
        f'<li><a href="dossiers/{_entity_slug(stem)}.html">{html.escape(e["display_name"])}</a>'
        f' <span style="color:#93a0ad">— {len(e["posts"])} упоминаний</span></li>'
        for stem, e in notable[:100]
    ) if notable else '<li style="color:#93a0ad">Пока накапливаем историю — досье появятся, когда персона/место упомянутся не менее трёх раз.</li>'

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(CHANNEL_USERNAME)} — база знаний</title>
<style>
  body {{ margin:0; padding:40px 20px 64px; background:#0b0f14; color:#eef2f6;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
  .wrap {{ max-width:820px; margin:0 auto; }}
  h1 {{ font-size:1.6rem; }}
  p.subtitle {{ color:#93a0ad; }}
  ul {{ list-style:none; padding:0; }}
  li {{ padding:10px 0; border-bottom:1px solid #2a323d; }}
  a {{ color:#4fd1c5; text-decoration:none; font-weight:600; }}
  a:hover {{ text-decoration:underline; }}
  footer {{ color:#93a0ad; font-size:0.8rem; margin-top:32px; text-align:center; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>🗂 База знаний</h1>
  <p class="subtitle">Автоматически собранные досье по персонам, местам и организациям,
  которые повторяются в новостях канала. · <a href="../index.html" style="font-weight:400">← Дашборд</a></p>
  <ul>{rows}</ul>
  <footer>Формируется автоматически · последнее обновление: {html.escape(generated_at_msk)} мск</footer>
</div>
</body>
</html>"""


def build_entity_page(stem, entry, generated_at_msk):
    display_name = entry.get("display_name", stem)
    profile = entry.get("profile")
    posts = entry.get("posts", [])
    profile_html = (
        f'<p style="font-size:1.05rem; line-height:1.6">{html.escape(profile)}</p>'
        if profile else '<p style="color:#93a0ad">Профиль появится, когда накопится достаточно упоминаний.</p>'
    )
    mentions_html = "".join(
        f'<li>{html.escape(datetime.fromtimestamp(p["ts"]).strftime("%d.%m.%Y %H:%M")) if p.get("ts") else ""} — '
        f'<a href="https://t.me/{CHANNEL_USERNAME}/{p["message_id"]}">{html.escape(p.get("headline", ""))}</a></li>'
        for p in reversed(posts)
    )
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(display_name)} — досье — {html.escape(CHANNEL_USERNAME)}</title>
<style>
  body {{ margin:0; padding:40px 20px 64px; background:#0b0f14; color:#eef2f6;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
  .wrap {{ max-width:760px; margin:0 auto; }}
  h1 {{ font-size:1.7rem; }}
  p.subtitle {{ color:#93a0ad; }}
  h2 {{ font-size:1rem; color:#f0b429; margin-top:28px; }}
  ul {{ list-style:none; padding:0; }}
  li {{ padding:8px 0; border-bottom:1px solid #2a323d; font-size:0.92rem; }}
  a {{ color:#4fd1c5; }}
  footer {{ color:#93a0ad; font-size:0.8rem; margin-top:32px; text-align:center; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>📇 {html.escape(display_name)}</h1>
  <p class="subtitle"><a href="../dossiers.html">← База знаний</a></p>
  {profile_html}
  <h2>Все упоминания ({len(posts)})</h2>
  <ul>{mentions_html}</ul>
  <footer>Формируется автоматически · последнее обновление: {html.escape(generated_at_msk)} мск</footer>
</div>
</body>
</html>"""


def due_dossier_update(dt, already_sent_today):
    if already_sent_today:
        return False
    now_minutes = dt.hour * 60 + dt.minute
    slot_h, slot_m = map(int, DOSSIER_TIME.split(":"))
    slot_minutes = slot_h * 60 + slot_m
    return 0 <= (now_minutes - slot_minutes) <= DOSSIER_WINDOW_MINUTES


STATE_FILES = [
    POSTED_FILE, RECENT_TITLES_FILE, LAST_RUN_FILE, MILESTONES_FILE,
    DIGEST_STATE_FILE, POLL_STATE_FILE,
    ALERT_STATE_FILE, STATUS_FILE, RECENT_POSTS_FILE, WEEKLY_RECAP_STATE_FILE,
    SPEED_STATS_FILE, QUIZ_STATE_FILE, LIVE_STORIES_FILE, MARKET_STATE_FILE,
    SUBSCRIBER_HISTORY_FILE, DASHBOARD_FILE, NOJEKYLL_FILE, STORY_TIMELINE_STATE_FILE,
    FEEDS_FILE, FEEDS_BACKUP_FILE, SELF_HEAL_LOG_FILE,
    MEDIA_KIT_FILE, CHANNEL_VIEWS_FILE,
    RSS_FEED_FILE, CORRECTIONS_LOG_FILE, CORRECTIONS_PAGE_FILE, EDITORIAL_POLICY_PAGE_FILE,
    ENTITY_INDEX_FILE, DOSSIERS_INDEX_FILE, DOSSIER_STATE_FILE,
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
                          new_quiz_state=None, new_market_state=None, new_story_timeline_state=None,
                          new_dossier_state=None):
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
            remote_story_timeline = _git_show_json(f"origin/main:{STORY_TIMELINE_STATE_FILE}",
                                                     {"date": None, "sent": False, "signatures": []})
            remote_weekly_recap = _git_show_json(f"origin/main:{WEEKLY_RECAP_STATE_FILE}", {"week_key": None, "sent": False})
            remote_alert = _git_show_json(f"origin/main:{ALERT_STATE_FILE}", {"last_alert": 0})
            remote_recent_posts = _git_show_json(f"origin/main:{RECENT_POSTS_FILE}", [])
            remote_speed_stats = _git_show_json(f"origin/main:{SPEED_STATS_FILE}", [])
            remote_live_threads = _git_show_json(f"origin/main:{LIVE_STORIES_FILE}", [])
            remote_subscriber_history = _git_show_json(f"origin/main:{SUBSCRIBER_HISTORY_FILE}", [])
            remote_channel_views = _git_show_json(f"origin/main:{CHANNEL_VIEWS_FILE}", {})
            remote_entity_index = _git_show_json(f"origin/main:{ENTITY_INDEX_FILE}", {})
            remote_dossier_state = _git_show_json(f"origin/main:{DOSSIER_STATE_FILE}", {"date": None, "sent": False})

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
            merged_dossier_state = _merge_daily_state(remote_dossier_state, new_dossier_state)

            def _merge_story_timeline_state(remote, new):
                base = _merge_daily_state(remote, new)
                sig_lists = (remote or {}).get("signatures", []) + (new or {}).get("signatures", [])
                seen = set()
                merged_sigs = []
                for sig in sig_lists:
                    key = tuple(sig)
                    if key in seen:
                        continue
                    seen.add(key)
                    merged_sigs.append(list(key))
                base = dict(base or {})
                base["signatures"] = merged_sigs[-STORY_TIMELINE_MAX_TRACKED_SIGNATURES:]
                return base

            merged_story_timeline = _merge_story_timeline_state(remote_story_timeline, new_story_timeline_state)
            merged_weekly_recap = _merge_daily_state(remote_weekly_recap, new_weekly_recap_state)
            merged_alert = {
                "last_alert": max(remote_alert.get("last_alert", 0), new_alert_timestamp or 0)
            }
            merged_status = new_status if new_status is not None else _git_show_json(f"origin/main:{STATUS_FILE}", {})

            # Захватываем ТЕКУЩЕЕ (возможно, только что самоисцелённое в
            # этом же прогоне) содержимое feeds.txt/feeds_backup.txt ДО
            # git reset --hard — иначе reset откатил бы файл к версии из
            # origin/main (то есть могло бы вернуть испорченную версию
            # обратно, стерев автоматическое восстановление).
            local_feeds_content = None
            if os.path.exists(FEEDS_FILE):
                with open(FEEDS_FILE, "r", encoding="utf-8", errors="ignore") as f:
                    local_feeds_content = f.read()
            local_feeds_backup_content = None
            if os.path.exists(FEEDS_BACKUP_FILE):
                with open(FEEDS_BACKUP_FILE, "r", encoding="utf-8", errors="ignore") as f:
                    local_feeds_backup_content = f.read()
            local_self_heal_log = load_self_heal_log()
            local_corrections_log = load_corrections_log()
            local_channel_views = load_channel_views()
            local_entity_index = load_entity_index()

            reset = subprocess.run(["git", "reset", "--hard", "origin/main"], capture_output=True, text=True)
            if reset.returncode != 0:
                print(f"[WARN] git reset failed (attempt {attempt}): {reset.stderr}")
                continue

            if local_feeds_content is not None:
                with open(FEEDS_FILE, "w", encoding="utf-8") as f:
                    f.write(local_feeds_content)
            if local_feeds_backup_content is not None:
                with open(FEEDS_BACKUP_FILE, "w", encoding="utf-8") as f:
                    f.write(local_feeds_backup_content)
            remote_self_heal_log = _git_show_json(f"origin/main:{SELF_HEAL_LOG_FILE}", [])
            merged_self_heal_log = (list(remote_self_heal_log) + local_self_heal_log)[-SELF_HEAL_LOG_LIMIT:]
            remote_corrections_log = _git_show_json(f"origin/main:{CORRECTIONS_LOG_FILE}", [])
            merged_corrections_log = (list(remote_corrections_log) + local_corrections_log)[-CORRECTIONS_LOG_LIMIT:]
            save_corrections_log(merged_corrections_log)
            save_self_heal_log(merged_self_heal_log)

            save_posted(merged_posted)
            save_recent_title_words(merged_recent)
            with open(LAST_RUN_FILE, "w") as f:
                json.dump(merged_last_run, f)
            save_last_milestone(merged_milestones["last"])
            _save_json(DIGEST_STATE_FILE, merged_digest)
            _save_json(POLL_STATE_FILE, merged_poll)
            _save_json(QUIZ_STATE_FILE, merged_quiz)
            _save_json(MARKET_STATE_FILE, merged_market)
            _save_json(DOSSIER_STATE_FILE, merged_dossier_state)
            _save_json(STORY_TIMELINE_STATE_FILE, merged_story_timeline)
            _save_json(WEEKLY_RECAP_STATE_FILE, merged_weekly_recap)
            _save_json(ALERT_STATE_FILE, merged_alert)
            _save_json(STATUS_FILE, merged_status)
            _save_json(RECENT_POSTS_FILE, merged_recent_posts)
            _save_json(SPEED_STATS_FILE, merged_speed_stats)
            _save_json(LIVE_STORIES_FILE, merged_live_threads)

            local_subscriber_history = load_subscriber_history()
            merged_subscriber_history = merge_subscriber_history(remote_subscriber_history, local_subscriber_history)
            save_subscriber_history(merged_subscriber_history)

            merged_channel_views = dict(remote_channel_views or {})
            for post_id, entry in (local_channel_views or {}).items():
                existing = merged_channel_views.get(post_id)
                if existing is None:
                    merged_channel_views[post_id] = entry
                else:
                    existing["views"] = max(existing.get("views", 0), entry.get("views", 0))
                    existing["first_seen_ts"] = min(
                        existing.get("first_seen_ts", entry.get("first_seen_ts", 0)),
                        entry.get("first_seen_ts", existing.get("first_seen_ts", 0)),
                    )
                    existing["last_seen_ts"] = max(
                        existing.get("last_seen_ts", 0), entry.get("last_seen_ts", 0)
                    )
            save_channel_views(merged_channel_views)

            merged_entity_index = merge_entity_index(remote_entity_index, local_entity_index)
            save_entity_index(merged_entity_index)

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
                    self_heal_log=merged_self_heal_log,
                )
                with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
                    f.write(dashboard_html)

                media_kit_html = build_media_kit_html(
                    status=merged_status,
                    subscriber_history=merged_subscriber_history,
                    engagement_stats=compute_engagement_stats(merged_channel_views),
                    self_heal_log=merged_self_heal_log,
                    generated_at_msk=now_msk().strftime("%d.%m.%Y %H:%M"),
                )
                with open(MEDIA_KIT_FILE, "w", encoding="utf-8") as f:
                    f.write(media_kit_html)

                generated_label = now_msk().strftime("%d.%m.%Y %H:%M")
                with open(RSS_FEED_FILE, "w", encoding="utf-8") as f:
                    f.write(build_rss_feed(merged_recent_posts, generated_label))
                with open(CORRECTIONS_PAGE_FILE, "w", encoding="utf-8") as f:
                    f.write(build_corrections_page(merged_corrections_log, generated_label))
                with open(EDITORIAL_POLICY_PAGE_FILE, "w", encoding="utf-8") as f:
                    f.write(build_editorial_policy_page(generated_label))

                os.makedirs(DOSSIERS_DIR, exist_ok=True)
                with open(DOSSIERS_INDEX_FILE, "w", encoding="utf-8") as f:
                    f.write(build_dossiers_index_page(merged_entity_index, generated_label))
                for stem, entry in merged_entity_index.items():
                    if len(entry.get("posts", [])) < MIN_MENTIONS_FOR_DOSSIER:
                        continue
                    entity_page_path = os.path.join(DOSSIERS_DIR, f"{_entity_slug(stem)}.html")
                    with open(entity_page_path, "w", encoding="utf-8") as f:
                        f.write(build_entity_page(stem, entry, generated_label))
            except Exception as e:
                print(f"[WARN] build_dashboard_html error: {e}")

            # ФИКС: досье по сущностям создают файлы с ЗАРАНЕЕ НЕИЗВЕСТНЫМИ
            # именами (docs/dossiers/<slug>.html — один на каждую
            # обнаруженную персону/место) — их нельзя перечислить заранее
            # в STATE_FILES. Добавляем всю папку docs целиком в git add,
            # чтобы новые файлы досье коммитились наравне со всем
            # остальным контентом сайта.
            subprocess.run(["git", "add", *STATE_FILES, DOCS_DIR], check=False)
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


# --- "Ещё круче и глобальнее": автономный слой самодиагностики и
# самовосстановления ---
# За время работы бота реально случались: порча feeds.txt (в него попал
# код Python вместо списка каналов), накопление "осиротевших"
# закреплённых сообщений, скрытые сбои git-персистенции. Раньше все эти
# случаи разбирались вручную, по скриншотам, шаг за шагом. Теперь бот
# каждый прогон САМ проверяет себя по нескольким направлениям и, где
# может, чинит проблему без участия человека — а где не может, оставляет
# явный, видимый след (self_heal_log + карточка на дашборде), а не тихо
# ломается.
SELF_HEAL_LOG_LIMIT = 100


def load_self_heal_log():
    return _load_json(SELF_HEAL_LOG_FILE, [])


def save_self_heal_log(entries):
    _save_json(SELF_HEAL_LOG_FILE, entries[-SELF_HEAL_LOG_LIMIT:])


def record_self_heal_event(kind, message):
    log = load_self_heal_log()
    log.append({"ts": time.time(), "kind": kind, "message": message})
    save_self_heal_log(log)
    print(f"[SELF-HEAL] {kind}: {message}")


# 1) Защита от порчи feeds.txt — именно так один раз реально сломался бот
# (список каналов оказался заменён содержимым post_news.py). Эвристика:
# настоящий список каналов — это короткие однословные строки без
# пробелов и без явных признаков кода; если заметная доля строк на это
# не похожа — считаем файл испорченным и не пытаемся кормить бота
# "каналами" вроде "def request_with_retry(...)".
FEEDS_CORRUPTION_MARKERS = ("def ", "import ", "class ", "return ", "elif ", "except ", "= {", "==")
FEEDS_CORRUPTION_BAD_FRACTION = 0.2


def feeds_content_looks_corrupted(raw_text):
    lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        return False
    bad = 0
    for ln in lines:
        if len(ln) > 60 or " " in ln or any(marker in ln for marker in FEEDS_CORRUPTION_MARKERS):
            bad += 1
    return (bad / len(lines)) > FEEDS_CORRUPTION_BAD_FRACTION


def ensure_feeds_file_healthy():
    if not os.path.exists(FEEDS_FILE):
        return {"feeds_ok": False, "healed": False}
    try:
        with open(FEEDS_FILE, "r", encoding="utf-8", errors="ignore") as f:
            raw = f.read()
    except Exception as e:
        record_self_heal_event("feeds_read_error", f"Не удалось прочитать feeds.txt: {e}")
        return {"feeds_ok": False, "healed": False}

    if not feeds_content_looks_corrupted(raw):
        # Файл в порядке — обновляем "последнюю здоровую" резервную копию,
        # чтобы было куда откатиться, если он испортится в будущем.
        try:
            with open(FEEDS_BACKUP_FILE, "w", encoding="utf-8") as f:
                f.write(raw)
        except Exception:
            pass
        return {"feeds_ok": True, "healed": False}

    # Похоже на порчу — пробуем восстановить из резервной копии.
    if os.path.exists(FEEDS_BACKUP_FILE):
        try:
            with open(FEEDS_BACKUP_FILE, "r", encoding="utf-8", errors="ignore") as f:
                backup_raw = f.read()
        except Exception:
            backup_raw = ""
        if backup_raw.strip() and not feeds_content_looks_corrupted(backup_raw):
            with open(FEEDS_FILE, "w", encoding="utf-8") as f:
                f.write(backup_raw)
            record_self_heal_event(
                "feeds_restored",
                "feeds.txt выглядел испорченным (похож на код, а не на список каналов) — "
                "автоматически восстановлен из резервной копии feeds_backup.txt."
            )
            return {"feeds_ok": True, "healed": True}

    record_self_heal_event(
        "feeds_corrupted_no_backup",
        "feeds.txt выглядит испорченным, а валидной резервной копии нет — "
        "нужно вмешательство человека (восстановить файл из истории git)."
    )
    return {"feeds_ok": False, "healed": False}


# 2) Проверка, что Telegram-токен вообще ещё действителен. Если токен
# отозван/просрочен, каждый send_to_telegram будет молча проваливаться —
# лучше явно понять причину сразу, а не гадать по логам отправки.
def check_telegram_token_health():
    if not TELEGRAM_TOKEN:
        return False
    try:
        resp = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe", timeout=10)
        data = resp.json()
        return bool(data.get("ok"))
    except Exception:
        return False


TOKEN_ALERT_MIN_GAP_SECONDS = 24 * 3600


def check_and_alert_token_health():
    healthy = check_telegram_token_health()
    if healthy:
        return True
    alert_state = _load_json(ALERT_STATE_FILE, {"last_alert": 0, "last_token_alert": 0})
    if time.time() - alert_state.get("last_token_alert", 0) < TOKEN_ALERT_MIN_GAP_SECONDS:
        return False
    record_self_heal_event(
        "telegram_token_invalid",
        "Telegram-токен не прошёл проверку (getMe вернул ошибку) — бот не сможет публиковать, "
        "пока токен не будет обновлён в секретах репозитория."
    )
    send_admin_alert(
        "🔴 Telegram-токен бота недействителен (getMe вернул ошибку). "
        "Проверьте TELEGRAM_BOT_TOKEN в секретах репозитория — возможно, токен отозван или истёк."
    )
    alert_state["last_token_alert"] = time.time()
    _save_json(ALERT_STATE_FILE, alert_state)
    return False


# 3) Автономная уборка "осиротевших" закреплённых сообщений — на случай,
# если по любой причине (сбой git, ручное вмешательство, старый баг) в
# live_stories.json остались "живые" по счётчику, но фактически устаревшие
# записи. Открепляем то, что старше LIVE_STORY_MAX_AGE_HOURS, и явно
# логируем это как самостоятельное действие, а не полагаемся только на
# то, что при старте нового эфира открепляются прошлые.
def self_heal_expired_pins():
    threads = load_live_threads()
    now = time.time()
    still_active = []
    healed_count = 0
    for thread in threads:
        if now - thread.get("last_update_ts", 0) > LIVE_STORY_MAX_AGE_HOURS * 3600:
            msg_id = thread.get("message_id")
            if msg_id:
                unpin_message(msg_id)
                healed_count += 1
        else:
            still_active.append(thread)
    if healed_count:
        save_live_threads(still_active)
        record_self_heal_event(
            "expired_pins_cleaned",
            f"Откреплено {healed_count} устаревших сообщений ('прямых эфиров' старше "
            f"{LIVE_STORY_MAX_AGE_HOURS} ч), которые больше не отслеживались активно."
        )
    return healed_count


def run_self_healing_checks():
    feeds_status = ensure_feeds_file_healthy()
    telegram_ok = check_and_alert_token_health()
    expired_pins_healed = self_heal_expired_pins()
    return {
        "feeds_ok": feeds_status["feeds_ok"],
        "feeds_healed_this_run": feeds_status["healed"],
        "telegram_ok": telegram_ok,
        "expired_pins_healed_this_run": expired_pins_healed,
        "checked_at": datetime.utcnow().isoformat(),
    }


def main():
    print(f"[START] {datetime.now().isoformat()}")

    self_check = run_self_healing_checks()

    dt_msk = now_msk()
    day_key = today_key(dt_msk)
    now_ts = time.time()

    elapsed_at_start = seconds_since_last_publish()
    new_alert_timestamp = check_silence_alert(elapsed_at_start)

    def persist_with_status(sent_count=None, note="", **kwargs):
        status = build_status_snapshot(elapsed_at_start, sent_count=sent_count, note=note, self_check=self_check)
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

    # --- Хроника развивающихся историй (кластеризация + синтез через ИИ) ---
    story_timeline_state = _load_json(STORY_TIMELINE_STATE_FILE,
                                       {"date": None, "sent": False, "last_sent_ts": 0, "signatures": []})
    if story_timeline_state.get("date") != day_key:
        story_timeline_state = {
            "date": day_key, "sent": False,
            "last_sent_ts": story_timeline_state.get("last_sent_ts", 0),
            "signatures": story_timeline_state.get("signatures", []),
        }
    new_story_timeline_state = None
    if due_story_timeline(dt_msk, story_timeline_state.get("sent")) and \
            (now_ts - story_timeline_state.get("last_sent_ts", 0)) >= MIN_STORY_TIMELINE_GAP_SECONDS:
        recent_posts_for_timeline = load_recent_posts()
        common_entities_timeline = compute_common_entity_stems(recent_posts_for_timeline)
        already_published_sigs = {tuple(s) for s in story_timeline_state.get("signatures", [])}
        cluster = pick_story_timeline_cluster(
            recent_posts_for_timeline, already_published_sigs, exclude_entities=common_entities_timeline
        )
        if cluster:
            ai_result = build_story_timeline_via_ai(cluster)
            if ai_result:
                timeline_text = format_story_timeline(ai_result, cluster)
                if send_to_telegram(timeline_text):
                    sig = list(cluster_signature(cluster))
                    story_timeline_state["sent"] = True
                    story_timeline_state["last_sent_ts"] = now_ts
                    story_timeline_state["signatures"] = (
                        story_timeline_state.get("signatures", []) + [sig]
                    )[-STORY_TIMELINE_MAX_TRACKED_SIGNATURES:]
                    new_story_timeline_state = story_timeline_state
                    print(f"[INFO] Story timeline published: '{ai_result['title']}' "
                          f"({len(cluster)} постов).")
                else:
                    print("[WARN] Story timeline send failed, will retry next eligible run.")
            else:
                print("[INFO] Story timeline cluster found but AI synthesis failed, skipping for now.")
                story_timeline_state["sent"] = True
                story_timeline_state["last_sent_ts"] = now_ts
                new_story_timeline_state = story_timeline_state
        else:
            print("[INFO] No story cluster big enough for a timeline right now.")
            story_timeline_state["sent"] = True
            story_timeline_state["last_sent_ts"] = now_ts
            new_story_timeline_state = story_timeline_state

    # --- База знаний: сбор упоминаний сущностей + синтез досье через ИИ ---
    dossier_state = _load_json(DOSSIER_STATE_FILE, {"date": None, "sent": False, "last_sent_ts": 0})
    if dossier_state.get("date") != day_key:
        dossier_state = {"date": day_key, "sent": False, "last_sent_ts": dossier_state.get("last_sent_ts", 0)}
    new_dossier_state = None
    if due_dossier_update(dt_msk, dossier_state.get("sent")) and \
            (now_ts - dossier_state.get("last_sent_ts", 0)) >= MIN_DOSSIER_GAP_SECONDS:
        recent_posts_for_entities = load_recent_posts()
        common_entities_dossier = compute_common_entity_stems(recent_posts_for_entities)
        fresh_index = build_entity_index(recent_posts_for_entities, exclude_entities=common_entities_dossier)
        working_index = merge_entity_index(load_entity_index(), fresh_index)

        candidates = []
        for stem, entry in working_index.items():
            posts = entry.get("posts", [])
            if len(posts) < MIN_MENTIONS_FOR_DOSSIER:
                continue
            last_mention_ts = max((p.get("ts", 0) for p in posts), default=0)
            if not entry.get("profile") or last_mention_ts > entry.get("profile_updated_ts", 0):
                candidates.append((stem, entry, len(posts)))
        candidates.sort(key=lambda c: c[2], reverse=True)

        updates_done = 0
        for stem, entry, _ in candidates:
            if updates_done >= MAX_DOSSIER_PROFILE_UPDATES_PER_RUN:
                break
            profile = build_entity_profile_via_ai(entry["display_name"], entry["posts"])
            if profile:
                entry["profile"] = profile
                entry["profile_updated_ts"] = time.time()
                updates_done += 1
                print(f"[INFO] Dossier profile updated for '{entry['display_name']}'.")

        save_entity_index(working_index)
        dossier_state["sent"] = True
        dossier_state["last_sent_ts"] = now_ts
        new_dossier_state = dossier_state
        print(f"[INFO] Dossier update pass done: {updates_done} profile(s) refreshed, "
              f"{len(candidates)} candidate(s) found.")

    update_channel_views_history()

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
    if slot and (now_ts - digest_state.get("last_sent_ts", 0)) >= MIN_DIGEST_GAP_SECONDS and self_check["feeds_ok"]:
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
                new_story_timeline_state=new_story_timeline_state,
                new_dossier_state=new_dossier_state,
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
        if new_digest_state or new_poll_state or new_alert_timestamp or new_weekly_recap_state or new_quiz_state or new_market_state or new_story_timeline_state or new_dossier_state or status_is_stale():
            persist_with_status(
                note="skip:too_soon_urgent",
                new_digest_state=new_digest_state,
                new_poll_state=new_poll_state,
                new_weekly_recap_state=new_weekly_recap_state,
                new_quiz_state=new_quiz_state,
                new_market_state=new_market_state,
                new_story_timeline_state=new_story_timeline_state,
                new_dossier_state=new_dossier_state,
            )
        return

    news = fetch_news() if self_check["feeds_ok"] else []
    if not news:
        # ФИКС ("радикально и наверняка"): раньше, если строгий fetch_news()
        # (с требованием фото/видео и фильтром "не новость") не находил
        # вообще НИЧЕГО, бот сдавался здесь безвозвратно — аварийный режим
        # гарантии частоты (pick_any_not_posted) ниже до этой точки просто
        # не доходил, потому что применялся только к уже найденным
        # кандидатам. Это была последняя лазейка, через которую тишина
        # могла тянуться сколько угодно. Теперь, если давно не было
        # публикации, пробуем СМЯГЧЁННЫЙ повторный проход по тем же
        # источникам — без требования фото/видео и без фильтра "не
        # новость" — и берём оттуда что угодно ещё не опубликованное.
        # Если же сам feeds.txt сейчас нездоров (self_check провалился) —
        # смягчённый проход тоже пропускаем: нет смысла повторно читать
        # тот же испорченный/пустой список источников.
        if self_check["feeds_ok"] and elapsed is not None and elapsed >= GUARANTEED_CADENCE_SECONDS:
            print(f"[INFO] Строгий поиск не нашёл вообще ничего, а с последней публикации "
                  f"прошло {int(elapsed)} сек — пробуем смягчённый поиск (без требования "
                  f"фото/видео, без фильтра «не новость») для гарантии частоты публикаций.")
            news = fetch_news(require_media=False, skip_not_news_filter=True)
        if not news:
            print("[INFO] No new news." if self_check["feeds_ok"]
                  else "[WARN] feeds.txt нездоров (self-check) — пропускаем поиск новостей в этом прогоне.")
            if new_digest_state or new_poll_state or new_alert_timestamp or new_weekly_recap_state or new_quiz_state or new_market_state or new_story_timeline_state or new_dossier_state or status_is_stale():
                persist_with_status(
                    note="skip:no_news",
                    new_digest_state=new_digest_state,
                    new_poll_state=new_poll_state,
                    new_weekly_recap_state=new_weekly_recap_state,
                    new_quiz_state=new_quiz_state,
                    new_market_state=new_market_state,
                    new_story_timeline_state=new_story_timeline_state,
                    new_dossier_state=new_dossier_state,
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
            if new_digest_state or new_poll_state or new_alert_timestamp or new_weekly_recap_state or new_quiz_state or new_market_state or new_story_timeline_state or new_dossier_state or status_is_stale():
                persist_with_status(
                    note="skip:too_soon_normal",
                    new_digest_state=new_digest_state,
                    new_poll_state=new_poll_state,
                new_weekly_recap_state=new_weekly_recap_state,
                new_quiz_state=new_quiz_state,
                new_market_state=new_market_state,
                new_story_timeline_state=new_story_timeline_state,
                new_dossier_state=new_dossier_state,
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
        if new_digest_state or new_poll_state or new_alert_timestamp or new_weekly_recap_state or new_quiz_state or new_market_state or new_story_timeline_state or new_dossier_state or status_is_stale():
            persist_with_status(
                note="skip:all_duplicates",
                new_digest_state=new_digest_state,
                new_poll_state=new_poll_state,
                new_weekly_recap_state=new_weekly_recap_state,
                new_quiz_state=new_quiz_state,
                new_market_state=new_market_state,
                new_story_timeline_state=new_story_timeline_state,
                new_dossier_state=new_dossier_state,
            )
        return

    if chosen.get("fact_update"):
        fu = chosen["fact_update"]
        correction_text = format_fact_update(
            chosen["title"], fu["matched_post"], fu["keyword"], fu["old_value"], fu["new_value"]
        )
        ok = send_to_telegram(correction_text)
        if ok:
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
            # Сохраняем обновлённую цифру как новую запись в recent_posts —
            # так следующее сравнение (extract_fact_numbers) увидит уже
            # ВЫРОСШЕЕ число, а не старое, и не будет повторно предлагать
            # то же самое уточнение по кругу.
            save_recent_posts(load_recent_posts() + [{
                "headline": chosen.get("title", ""),
                "summary": chosen.get("summary", ""),
                "ts": time.time(),
                "message_id": ok if isinstance(ok, int) else None,
                "source": chosen.get("source", ""),
            }])
            mark_published_now()
            record_correction(
                original_headline=fu["matched_post"].get("headline") or fu["matched_post"].get("title", ""),
                keyword=fu["keyword"], old_value=fu["old_value"], new_value=fu["new_value"],
                message_id=ok if isinstance(ok, int) else None,
            )
            persist_with_status(
                sent_count=1,
                note="fact_update",
                new_posted_ids=new_posted_ids,
                new_title_words_list=new_title_words_list,
                new_last_publish=time.time(),
                new_digest_state=new_digest_state,
                new_poll_state=new_poll_state,
                new_weekly_recap_state=new_weekly_recap_state,
                new_quiz_state=new_quiz_state,
                new_market_state=new_market_state,
                new_story_timeline_state=new_story_timeline_state,
                new_dossier_state=new_dossier_state,
            )
            print(f"[DONE] Fact update published: {fu['keyword']} {fu['old_value']} → {fu['new_value']}.")
        else:
            print("[WARN] Failed to send fact update — will retry next run.")
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
                    new_story_timeline_state=new_story_timeline_state,
                    new_dossier_state=new_dossier_state,
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
                new_story_timeline_state=new_story_timeline_state,
                new_dossier_state=new_dossier_state,
    )

    print(f"[DONE] Sent {sent_count}/{len(news)} items as separate posts.")


if __name__ == "__main__":
    main()
