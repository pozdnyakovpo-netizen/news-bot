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
    # Признаки 1/2: единая точка retry для ЛЮБОГО HTTP-вызова в боте.
    # Раньше сетевые обрывы (timeout, connection reset) не перехватывались
    # почти нигде, кроме ручной проверки статус-кода 401/429 у GigaChat —
    # обрыв соединения приводил к падению функции с первого раза.
    # Дополнительно уважаем Retry-After, который Telegram присылает при 429.
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

# --- Настройки ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
# Признак 3 (уведомление админу): отдельный чат/личка для служебных
# алертов, чтобы не засорять сам новостной канал системными сообщениями.
# Необязательный — если не задан, алерты просто логируются.
ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID")
SILENCE_ALERT_HOURS = 3  # если публикаций не было дольше этого — сигнал админу
ALERT_STATE_FILE = "last_alert.json"
STATUS_FILE = "status.json"  # признак 4: снимок состояния бота для внешнего мониторинга
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
FETCH_POOL_SIZE = 60                # ФИКС: было 12 — с 20 источниками в feeds.txt
                                     # это обрывало сбор кандидатов ещё до того, как
                                     # бот успевал заглянуть во все каналы (список
                                     # перемешивается случайно, и первые попавшиеся
                                     # 12 не обязательно самые важные). 60 даёт
                                     # запас, чтобы обойти практически весь список
                                     # источников за один запуск перед выбором.
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
# ФИКС: было 4 варианта — теперь несколько вариантов, ротируются по дню,
# чтобы подписчики не видели один и тот же вопрос каждый день.
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
    # ФИКС: встроенный hash() в Python рандомизируется между запусками
    # процесса (PYTHONHASHSEED) — на GitHub Actions каждый запуск это новый
    # процесс, так что hash(day_key) был бы разным при каждом прогоне, а не
    # стабильным в течение дня. hashlib.md5 даёт одинаковый результат
    # всегда для одного и того же day_key, независимо от процесса.
    digest = hashlib.md5(day_key.encode("utf-8")).hexdigest()
    idx = int(digest, 16) % len(POLL_VARIANTS)
    return POLL_VARIANTS[idx]


POLL_STATE_FILE = "last_poll.json"

URGENT_KEYWORDS = [
    "погиб", "убит", "жертв", "экстренн", "чрезвычайн", "эвакуац",
    "взрыв", "теракт", "катастроф", "введен режим чс",
]

URGENT_EMOJIS = ["🔥", "🚨", "❗️", "⚡️"]
CHANNEL_MARK = "🔷"

# Признак топ-каналов (РИА/ТАСС/РБК): категорийный эмодзи-маркер вместо
# одного статичного значка — ускоряет сканирование ленты — плюс хэштег
# категории в конце поста для поиска по темам внутри канала.
CATEGORY_RULES = [
    # (эмодзи, хэштег, ключевые слова для распознавания)
    ("⚽️", "#спорт", [
        # ФИКС: убраны "гол" (совпадало с "Голд" — название пляжа высадки
        # в Нормандии, из-за чего историческая новость попала в #спорт)
        # и "лига" (совпадает с "олигарх") — слишком короткие и опасные
        # как совпадение-подстрока. Более длинные и специфичные слова
        # такую коллизию дают заметно реже.
        "футбол", "хокке", "теннис", "матч", "турнир", "чемпионат", "сборная",
        "тренер", "клуб", "олимпиад", "спортсмен", "чм-", "забил гол",
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
    ("💻", "#технологии", [
        # ФИКС: было короткое "ии" для ловли аббревиатуры ИИ — но это
        # совпадало как подстрока с окончанием "-ии" в тысячах обычных
        # слов ("полиции", "территории", "срабатывании" и т.д.), из-за
        # чего происшествия и другие новости ошибочно помечались как
        # #технологии. "ии-" с дефисом безопаснее (так пишут "ИИ-стартап"),
        # обычные слова с таким сочетанием почти не встречаются.
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


HEADLINE_MAX_LEN = 130  # ФИКС: было 55, потом 100 — по-прежнему резало на
                        # середине мысли для длинных "сырых" заголовков без
                        # ИИ-редактуры (fallback-путь без GigaChat), где
                        # заголовок — это просто первое предложение источника
                        # со всеми его придаточными. 130 — редкий аварийный
                        # лимит, а не рабочий режим; вместе с более умной
                        # обрезкой по запятой (см. truncate_at_word) это
                        # покрывает подавляющее большинство реальных случаев.


def truncate_at_word(text, max_len=HEADLINE_MAX_LEN):
    if not text or len(text) <= max_len:
        return text
    cut = text[:max_len]
    # ФИКС: сначала пробуем найти последнюю запятую в пределах окна — если
    # она оставляет хотя бы 70% лимита, режем по ней: это обычно граница
    # придаточного предложения ("...после наезда автомобиля, ..."), и
    # результат читается как законченная мысль, а не оборванная на полуслове
    # деталь. Если подходящей запятой нет — как раньше, режем по пробелу.
    last_comma = cut.rfind(",")
    if last_comma >= max_len * 0.7:
        cut = cut[:last_comma]
    else:
        last_space = cut.rfind(" ")
        if last_space > 0:
            cut = cut[:last_space]
    return cut.rstrip(" ,.;:—-") + "…"


# Признак 4: типовые канцелярские вводные, которыми AI (и журналисты
# низкого качества) любят открывать новость, ничего не добавляя по сути.
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
    # Признак 5: заголовок КАПСОМ выглядит как спам — если больше 60%
    # букв заглавные, приводим к обычному предложенческому регистру.
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
    # Признак 3: убираем эмодзи, которые могла добавить сама AI —
    # маркер категории/срочности бот уже ставит сам, дублирование эмодзи
    # выглядит любительски.
    if not text:
        return text
    text = _EMOJI_PATTERN.sub("", text)
    return re.sub(r'\s{2,}', ' ', text).strip()


def collapse_repeated_punctuation(text):
    # Признак 7: "!!!", "???", "...." — убираем до одного знака.
    if not text:
        return text
    text = re.sub(r'!{2,}', '!', text)
    text = re.sub(r'\?{2,}', '?', text)
    text = re.sub(r'\.{4,}', '…', text)
    return text


def sanitize_text(text):
    # Финальная санитаризация перед отправкой (признак 10): прогоняет
    # все точечные чистки разом и убирает случайные двойные пробелы,
    # которые могли остаться после предыдущих замен.
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
    # ФИКС (радикальный): раньше сравнивались точные словоформы, а русский
    # язык склоняет существительные и прилагательные по падежам —
    # "Эльбрусе" (предложный падеж) и "Эльбруса" (родительный) для
    # компьютера были РАЗНЫМИ словами, хотя означают одно и то же место.
    # Из-за этого дубль одной новости от двух каналов ("тела эвакуированы
    # с Эльбруса" / "погибших на Эльбрусе") мог не набрать порог схожести
    # и проходил как "новая" новость. Обрезаем длинные слова до первых
    # 6 символов — это не настоящая лемматизация, а грубая эвристика, но
    # она гасит подавляющее большинство падежных/числовых окончаний
    # ("-е", "-а", "-ов", "-ых" и т.п.), не путая при этом разные по сути
    # слова (например "полиция" и "политика" всё равно не совпадут).
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


# УРОВЕНЬ B дедупа (без ИИ, бесплатно): "именные" стемы — слова с
# заглавной буквы не в начале фразы почти всегда имена/топонимы
# ("Эльбрус", "Одесса"), а не случайное слово. Если у двух новостей
# совпадает такой стем — это гораздо более сильный сигнал одного
# события, чем совпадение обычных слов ("погиб", "человек" и т.п.),
# и позволяет распознать дубль даже при низком общем словесном
# перекрытии (см. кейс "эвакуированы тела" / "погibли двое альпинистов"
# на Эльбрусе — общих слов мало, но оба содержат "Эльбрус").
# ИСТОРИЯ ФИКСОВ: сначала сюда пытались добавить "Трамп"/"Германия"/
# "Берлин" статически и навсегда, потому что их частое появление приводило
# к тому, что почти любая пара новостей с упоминанием, например, Трампа
# считалась "тем же событием" — из-за чего целый прогон однажды отбросил
# ВСЕ 68 кандидатов пула как дубли. Но постоянный бан оказался слишком
# грубым: он же выключал полезную функцию "🔄 Продолжение истории" именно
# для горячих тем (например, для реально развивающегося теракта в
# Берлине) — где связывать посты как раз важнее всего. Поэтому здесь
# остаются только слова, фоновые ВСЕГДА, а не временно; актуальные
# "горячие" имена ловит ДИНАМИЧЕСКИЙ механизм ниже
# (compute_common_entity_stems) — он подстраивается сам, пока тема
# остаётся частой, и сам же перестаёт исключать имя, когда она остывает.
COMMON_ENTITY_STOPWORD_STEMS = {
    # ФИКС (обратный откат части вчерашнего): "герман"/"берлин" и другие
    # geo/political имена держать здесь ПОСТОЯННО было ошибкой — Берлин
    # прямо сейчас в центре реально развивающейся истории (теракт), и
    # из-за постоянного бана функция "🔄 Продолжение истории" перестала
    # связывать посты именно там, где это нужнее всего. Здесь оставляем
    # только слова, которые фоновые ВСЕГДА, а не временно — актуальные
    # "горячие" имена (Трамп, Берлин и т.п.) пусть ловит ДИНАМИЧЕСКИЙ
    # механизм ниже (compute_common_entity_stems), который сам понимает,
    # когда конкретное имя стало слишком частым, и сам же перестаёт его
    # исключать, когда тема остывает — в отличие от постоянного списка.
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


def compute_common_entity_stems(recent_posts, min_count=5, max_fraction=0.15):
    # РАДИКАЛЬНЫЙ ФИКС: вместо того чтобы вручную и бесконечно пополнять
    # статический список "слишком общих" имён (что мы уже дважды делали —
    # для "ии" и для "гол"/"лига", теперь для "Трамп"/"Германия"), считаем
    # частоту каждого именного стема по РЕАЛЬНОЙ недавней истории постов.
    # Стем, который встречается больше чем в max_fraction всех недавних
    # постов (и не реже min_count раз), считается "фоновым словом", а не
    # уникальным идентификатором конкретного события, и исключается из
    # сравнения — это адаптируется само по себе к любой новой часто
    # повторяющейся теме, а не только к тем именам, что мы уже заметили.
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


def is_same_event(title_a, summary_a, title_b, summary_b, exclude_entities=None):
    # Комбинированная проверка: сначала обычный порог (0.5), а если он не
    # пройден — смотрим, есть ли общий именной стем (см. выше); если да,
    # порог резко снижается (0.15), потому что совпадение конкретного
    # места/персоны — уже само по себе сильное доказательство того же
    # события, даже если остальные слова текста совсем разные.
    # exclude_entities — стемы, которые слишком часто встречаются в
    # недавней истории (см. compute_common_entity_stems), чтобы считаться
    # уникальным признаком конкретного события — их не учитываем.
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


# УРОВЕНЬ B/C дедупа: раньше "память" дедупа хранила только наборы слов
# (без исходного текста), поэтому не было возможности сравнить именные
# стемы или спросить ИИ про смысл — приходилось восстанавливать текст
# из ничего. Теперь дополнительно храним сам текст (заголовок + начало
# тела) последних опубликованных постов — это даёт материал и для
# entity-проверки, и для смысловой проверки через GigaChat.
RECENT_POSTS_FILE = "recent_posts.json"
RECENT_POSTS_LIMIT = 300  # было 30 — увеличено, чтобы (а) дедуп уровня B/C
                          # видел более длинную историю и (б) было из чего
                          # собирать еженедельный рекап "Главное за неделю"


def load_recent_posts():
    raw = _load_json(RECENT_POSTS_FILE, [])
    return [p for p in raw if isinstance(p, dict) and p.get("headline")]


def save_recent_posts(posts):
    trimmed = posts[-RECENT_POSTS_LIMIT:]
    _save_json(RECENT_POSTS_FILE, trimmed)


def is_duplicate_word_or_entity(candidate_title, candidate_summary, recent_posts, exclude_entities=None):
    # Уровень B применительно к реально опубликованным постам (не только
    # к текущему пулу кандидатов) — без вызова ИИ, бесплатно и мгновенно.
    for post in recent_posts:
        if is_same_event(candidate_title, candidate_summary,
                          post.get("headline", ""), post.get("summary", ""),
                          exclude_entities=exclude_entities):
            return True
    return False


STORY_CONTINUATION_WINDOW_HOURS = 48


def find_story_continuation(candidate_title, candidate_summary, recent_posts,
                             hours=STORY_CONTINUATION_WINDOW_HOURS, exclude_entities=None):
    # "Продолжение истории": кандидат уже ПРОШЁЛ проверку на дубликат (иначе
    # его бы не публиковали вовсе) — эта функция не про дедуп, а про то,
    # чтобы связать НОВУЮ, но связанную новость с предыдущим постом на ту же
    # тему/место/персону (общий именной стем), опубликованным недавно.
    # Ищем самое СВЕЖЕЕ совпадение — если их несколько, ссылаемся на
    # последний пост по теме, а не на самый первый.
    # exclude_entities — см. compute_common_entity_stems: без этого
    # "Трамп"/"Германия" и т.п. связывали бы совершенно не связанные
    # новости пометкой "продолжение истории".
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
            best = post  # recent_posts в порядке добавления — последнее совпадение самое свежее
    return best


# --- "Крутая" фича: живая трансляция развивающегося события ---
# Вместо серии разрозненных постов об одном теракте/происшествии — ОДНО
# закреплённое сообщение, которое бот сам редактирует (editMessageText)
# по мере поступления новых деталей, как live-блог у крупных изданий.
# Полноценной непрерывной трансляции без постоянно работающего сервера
# не сделать (GitHub Actions — это периодические запуски, а не процесс
# 24/7), но между запусками разница обычно секунды-минуты, и для
# читателя это выглядит как живое обновление одного и того же поста.
# Намеренное ограничение: одновременно ведётся только ОДНА живая
# трансляция — так проще гарантировать, что ничего не потеряется и не
# перепутается между параллельными сюжетами.
LIVE_STORIES_FILE = "live_stories.json"
LIVE_STORY_MAX_AGE_HOURS = 12   # трансляция считается завершённой, если
                                 # обновлений не было дольше этого времени
LIVE_STORY_MAX_UPDATES = 15     # старые обновления обрезаются, чтобы не
                                 # упереться в лимит длины сообщения Telegram


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
            continue  # трансляция "остыла" — считаем её завершённой
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
            # "message is not modified" — не ошибка, а идемпотентность;
            # остальное — реальная проблема (например, пост слишком старый
            # для редактирования, Telegram лимитирует правки старше 48ч).
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
    # Новые упомянутые имена тоже добавляем — сюжет мог "прирасти" новыми
    # действующими лицами по ходу развития (например, назвали подозреваемого).
    new_entities = extract_entity_stems(item["title"]) | extract_entity_stems(item.get("summary", ""))
    thread["entities"] = list(set(thread.get("entities", [])) | new_entities)
    return thread


MAX_AI_DEDUPE_CHECKS = 5  # ограничиваем число вызовов ИИ на дедуп за один запуск


def check_semantic_duplicate_via_ai(candidate_title, candidate_summary, recent_posts):
    # УРОВЕНЬ C (радикальный): если словарная проверка и проверка по
    # именным стемам не нашли дубль, но новость всё равно может
    # описывать то же самое событие совершенно другими словами (разный
    # акцент: "эвакуировали тела" vs "погибли на восхождении") — это
    # единственный способ поймать такой случай: спросить сам GigaChat.
    # Дороже по времени/токенам, поэтому вызывается только для реально
    # выбранного кандидата перед отправкой, а не для всего пула.
    token = get_gigachat_token()
    if not token or not recent_posts:
        return None
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
        # ФИКС: без нелчадности (?=...) старый regex с re.S жадно захватывал
        # ВСЁ до конца строки, включая последующую строку КОНТЕКСТ: — теперь
        # текст останавливается перед меткой КОНТЕКСТ: или концом ответа.
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

        # ФИКС: раньше переносы строк просто схлопывались в пробел —
        # если исходный пост оформлен как "лид-строка\nосновной текст"
        # БЕЗ точки в конце строки (частый стиль в новостных каналах),
        # два предложения сливались в одно без разделителя ("...
        # подозреваемых Инцидент произошел..."). Из-за этого весь
        # последующий разбор по предложениям (лимит для срочных постов,
        # дедуп по словам) ломался именно на этом стыке. Теперь при
        # склейке строк добавляем точку, если строка ещё не заканчивается
        # знаком препинания — это восстанавливает границу предложения,
        # которая была потеряна при вёрстке исходного поста.
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
    # Признак 6/9: список содержит и слова, и источник, и индекс в
    # new_items — нужен, чтобы при встрече похожей новости от ДРУГОГО
    # канала не просто отбросить дубль, а пометить уже сохранённый
    # оригинал как «подтверждено несколькими источниками».
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

            # Признак 8: защита от обрубленных заголовков — если в
            # заголовке меньше 3 значимых слов, это почти всегда обрывок
            # текста (например, канал разбил пост на несколько строк, а
            # мы забрали только первую), публиковать такое нельзя.
            if len(significant_title_words(title)) < 3:
                continue

            raw_summary = entry.get("summary", entry.get("description", ""))
            summary = strip_source_mentions(html.unescape(re.sub(r"<[^>]+>", "", raw_summary)))
            link = entry.get("link", "")
            source_channel = entry.get("source_channel") or ""

            # ФИКС: сравниваем по словам заголовка + summary вместе (не
            # только заголовка), потому что разные каналы часто по-разному
            # формулируют заголовок про одно и то же событие, а факты в
            # тексте (summary) обычно совпадают.
            c_words = content_words(title, summary)
            if is_duplicate_by_meaning(c_words, recent_content_words):
                continue

            dup_meta = next((m for m in seen_items_meta if titles_are_similar(c_words, m["words"])), None)
            if dup_meta is not None:
                if dup_meta["source_channel"] != source_channel:
                    # Другой канал независимо сообщает о том же событии —
                    # не публикуем второй раз, но помечаем оригинал как
                    # подтверждённый несколькими источниками.
                    new_items[dup_meta["idx"]]["confirmed_multi_source"] = True
                continue

            if is_not_news(title, summary):
                continue

            photo = entry.get("photo")
            photo_bytes = entry.get("photo_bytes")
            video = entry.get("video")
            if not photo and not video:
                continue

            src = f"@{source_channel}" if source_channel else source_name(link)
            urgent = is_urgent(title, summary)
            print(f"[INFO] '{title[:50]}' ({src}) — photo={'yes' if photo else 'no'}, video={'yes' if video else 'no'}")

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
    # Признак: слишком длинное предложение (>25 слов) читается тяжело —
    # разбиваем по ближайшей запятой к середине, если такая есть, иначе
    # оставляем как есть (лучше длинное предложение, чем испорченный смысл).
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
    # Признак топ-каналов: короткие абзацы (1-2 предложения), а не
    # сплошной блок текста — читается заметно быстрее с телефона.
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

    # Признаки 3/4/5/7: убираем decorative-эмодзи от AI, клише-вставки,
    # КАПС и повторяющуюся пунктуацию — до экранирования HTML и до обрезки.
    headline_raw = fix_shouty_caps(strip_cliche_openers(sanitize_text(headline_raw)))
    body_raw = split_long_sentences(strip_cliche_openers(sanitize_text(body_raw)))

    item["headline"] = html.escape(truncate_at_word(headline_raw)) if headline_raw else html.escape(truncate_at_word(item["title"]))

    if item.get("urgent"):
        # Признак «молния»: срочная новость — коротко и без разбивки на
        # абзацы (1-2 предложения), как экстренный формат у РИА/ТАСС —
        # читатель должен понять суть за секунду, без прокрутки.
        urgent_body = limit_sentences(body_raw, max_sentences=2)
        item["body"] = html.escape(urgent_body) if urgent_body else ""
    else:
        item["body"] = html.escape(paragraphize(body_raw)) if body_raw else ""

    item["category"] = detect_category(item["title"], item.get("summary", ""))

    # Признак 5: короткая атрибуция источника в конце поста — просто
    # @handle канала-первоисточника, без ссылки и без цитирования текста.
    item["attribution"] = item.get("source") if item.get("source_channel") else None

    # "Почему это важно" — контекстная строка от AI (см. rewrite_with_ai).
    # Только для НЕсрочных постов: у срочных и так формат-"молния", лишняя
    # строка там мешает мгновенному считыванию сути.
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
    # ФИКС (защита от гонки между запусками): к моменту, когда мы
    # действительно готовы отправлять пост, могло пройти время — например,
    # только что отработал дайджест и опубликовал что-то очень похожее на
    # нашего кандидата. Перечитываем posted/recent прямо перед отправкой и
    # берём первого кандидата, который всё ещё не дубликат.
    #
    # РАДИКАЛЬНЫЙ ФИКС (3 уровня, от дешёвого к дорогому):
    #   A. точный хэш заголовка / пересечение словарных стемов (было)
    #   B. пересечение "именных" стемов (Эльбрус, Одесса...) с более
    #      низким порогом — ловит одно событие, описанное разными
    #      словами, но упомянувшее общее место/персону
    #   C. смысловая проверка через GigaChat против РЕАЛЬНО
    #      опубликованных постов (а не только текущего пула) — ловит
    #      случаи, когда даже общих именных стемов нет, но по смыслу
    #      это то же самое событие
    posted_now = load_posted()
    recent_now = load_recent_title_words()
    recent_posts_now = load_recent_posts()
    common_entities = compute_common_entity_stems(recent_posts_now)
    ai_checks_used = 0
    for it in items:
        if it["id"] in posted_now or it["title_key"] in posted_now:
            continue
        cw = set(it.get("content_words") or [])
        if cw and is_duplicate_by_meaning(cw, recent_now):
            continue
        if is_duplicate_word_or_entity(it["title"], it.get("summary", ""), recent_posts_now,
                                        exclude_entities=common_entities):
            print(f"[INFO] '{it['title'][:50]}' — дубль по именному стему, пропускаем.")
            continue
        if ai_checks_used < MAX_AI_DEDUPE_CHECKS:
            ai_checks_used += 1
            dup_idx = check_semantic_duplicate_via_ai(it["title"], it.get("summary", ""), recent_posts_now)
            if dup_idx is not None:
                print(f"[INFO] GigaChat считает '{it['title'][:50]}' тем же событием, "
                      f"что и недавний пост #{dup_idx} — пропускаем.")
                continue
        return it
    return None



def send_to_telegram(text):
    # ФИКС/расширение: возвращает message_id (число) при успехе вместо
    # True, или None при неудаче — вызовы вида "if send_to_telegram(...)"
    # продолжают работать как раньше (число истинно, None ложно), но
    # теперь можно сохранить message_id для построения ссылки на пост
    # (нужно для "🔄 Продолжение истории").
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
                return True  # отправилось, но не смогли распарсить id — не считаем ошибкой
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
    # Срочная новость — обычный "срочный" эмодзи-акцент, как раньше.
    # Иначе — категорийный маркер (⚽️/🚨/💹/🏛/💻) вместо статичного 🔷,
    # плюс хэштег категории в конце поста, если категория распознана.
    category_emoji, hashtag = item.get("category", (CHANNEL_MARK, None))
    if item.get("urgent"):
        mark = random.choice(URGENT_EMOJIS)
    else:
        mark = category_emoji
    text = f"{mark} <b>{item['headline']}</b>"

    # "🔄 Продолжение истории" — если новость связана с недавним постом на
    # ту же тему (общее место/персона), но не является его дубликатом
    # (иначе была бы отфильтрована раньше) — даём читателю ссылку на
    # предыдущий пост, чтобы он видел развитие сюжета, а не разрозненные
    # посты об одном и том же.
    continuation = item.get("continuation_of")
    if continuation and continuation.get("message_id"):
        link = f"https://t.me/{CHANNEL_USERNAME}/{continuation['message_id']}"
        text += f"\n🔄 <a href=\"{link}\">Продолжение истории</a>"

    if item.get("body"):
        text += f"\n\n{item['body']}"

    # "Почему это важно" — короткая контекстная строка от AI, не пересказ,
    # а объяснение значимости/следствия. Только если AI реально нашёл, что
    # сказать (rewrite_with_ai возвращает None, если добавить нечего).
    if item.get("context_line"):
        text += f"\n\n💡 {item['context_line']}"

    # Признак 6: если новость независимо подтвердили 2+ разных канала в
    # пуле кандидатов — это реальный сигнал достоверности, как у крупных
    # агентств, которые не публикуют неподтверждённые вбросы одного канала.
    if item.get("confirmed_multi_source"):
        text += "\n\n✅ Подтверждено несколькими источниками"

    if extra:
        text += f"\n\n{extra}"
    if hashtag and not item.get("urgent"):
        text += f"\n\n{hashtag}"

    # Признак 5: короткая атрибуция первоисточника — без ссылки, без
    # цитирования текста, просто @handle канала (не нарушает копирайт,
    # но повышает прозрачность/доверие).
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
    # Признак 3: короткое уведомление в личный/служебный чат админа —
    # НЕ в новостной канал, чтобы не засорять его системными сообщениями.
    # Если ADMIN_CHAT_ID не задан, просто логируем — работа бота не рвётся.
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
    # Признак 3: если публикаций не было дольше SILENCE_ALERT_HOURS —
    # шлём алерт админу, но не чаще раза в сутки. ВАЖНО: не пишем
    # ALERT_STATE_FILE здесь напрямую — persist_state_to_git делает
    # git reset --hard перед коммитом и стёр бы эту запись. Вместо этого
    # возвращаем новую метку времени, чтобы main() передал её в persist.
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


# --- Пункт 6: "скорость как метрика доверия" ---
# Время между появлением новости у источника (Telegram-канала) и
# публикацией у нас. Не выводится в сами посты (это было бы навязчиво),
# а копится в отдельном файле и попадает в status.json — как внутренняя
# аналитика, которую можно использовать хоть в закреплённом сообщении,
# хоть просто чтобы знать, насколько оперативно работает бот.
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
        # Формат из Telegram (<time datetime="...">) — ISO 8601, иногда с
        # суффиксом 'Z' вместо явного смещения таймзоны.
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
    # Отбрасываем заведомо некорректные значения: отрицательные (разъехались
    # часовые пояса при парсинге) или больше суток (источник явно не "живой",
    # смысла считать это скоростью публикации нет).
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


def build_status_snapshot(last_publish_elapsed, sent_count=None, note=""):
    # Признак 4: снимок состояния для внешнего мониторинга (например,
    # UptimeRobot может проверять поле "healthy" в сыром файле status.json).
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
    }


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


# --- "Крутая" фича: ежедневный квиз по РЕАЛЬНЫМ опубликованным новостям ---
# Использует нативный Quiz Poll в Telegram (type="quiz") — Telegram сам
# подсвечивает правильный ответ и показывает объяснение всем, кто
# проголосовал, сразу после голосования. Это не отдельная вручную
# написанная механика, а встроенный формат самого Telegram, которым
# редко пользуются небольшие каналы. Вопрос строится строго по фактам
# уже опубликованного поста — никакие новости не выдумываются, квиз
# только проверяет внимательность к тому, что реально было в канале.
QUIZ_TIME = "15:00"  # МСК
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
    # Берём пост за последние сутки с достаточно содержательным текстом
    # (короткие/технические записи не дают материала для вопроса) —
    # предпочитаем самый свежий подходящий, чтобы квиз был об актуальном.
    now = time.time()
    candidates = [
        p for p in recent_posts
        if (not p.get("ts") or now - p["ts"] <= hours * 3600)
        and len(p.get("summary", "")) >= 60
    ]
    return candidates[-1] if candidates else None


def build_quiz_from_post(post):
    # Просим GigaChat сформулировать вопрос СТРОГО по фактам из текста
    # поста — с одним верным и тремя правдоподобными неверными вариантами,
    # плюс короткое объяснение (Telegram покажет его после ответа).
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
        # Валидация: ровно 4 варианта, корректный индекс, вопрос и варианты
        # укладываются в лимиты самого Telegram (question ≤300, option ≤100).
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


# --- Еженедельный рекап "Главное за неделю" ---
WEEKLY_RECAP_WEEKDAY = 6         # 0=понедельник ... 6=воскресенье (МСК)
WEEKLY_RECAP_TIME = "20:00"      # МСК
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
    # Берём посты за последние 7 дней (по ts, если он есть — старые записи
    # без ts, оставшиеся от версии до этого фикса, просто не отфильтровываем
    # агрессивно, чтобы не остаться совсем без материала первую неделю).
    now = time.time()
    week_posts = [p for p in recent_posts if not p.get("ts") or now - p["ts"] <= 7 * 24 * 3600]
    if not week_posts:
        week_posts = recent_posts
    candidates = week_posts[-100:]  # ограничиваем размер промпта разумным пределом
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
        # Признак 7: категорийный эмодзи и в дайджесте, не только в
        # одиночных постах — единообразие визуального языка канала.
        cat_emoji, _ = it.get("category", (CHANNEL_MARK, None))
        lines.append(f"{i}. {cat_emoji} <b>{it['headline']}</b>")
        if it.get("body"):
            first_sentence = it["body"].split(". ")[0].rstrip(".") + "."
            lines.append(first_sentence)
        lines.append("")
    lines.append(random.choice(CTA_VARIANTS))
    return "\n".join(lines).strip()


STATE_FILES = [
    POSTED_FILE, RECENT_TITLES_FILE, LAST_RUN_FILE, MILESTONES_FILE,
    DIGEST_STATE_FILE, POLL_STATE_FILE,
    ALERT_STATE_FILE, STATUS_FILE, RECENT_POSTS_FILE, WEEKLY_RECAP_STATE_FILE,
    SPEED_STATS_FILE, QUIZ_STATE_FILE, LIVE_STORIES_FILE,
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
                          new_quiz_state=None):
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
            remote_quiz = _git_show_json(f"origin/main:{QUIZ_STATE_FILE}", {"date": None, "sent": False})
            remote_weekly_recap = _git_show_json(f"origin/main:{WEEKLY_RECAP_STATE_FILE}", {"week_key": None, "sent": False})
            remote_alert = _git_show_json(f"origin/main:{ALERT_STATE_FILE}", {"last_alert": 0})
            remote_recent_posts = _git_show_json(f"origin/main:{RECENT_POSTS_FILE}", [])
            remote_speed_stats = _git_show_json(f"origin/main:{SPEED_STATS_FILE}", [])
            remote_live_threads = _git_show_json(f"origin/main:{LIVE_STORIES_FILE}", [])

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

            # recent_posts.json уже дописан локально (main() вызывает
            # save_recent_posts до persist_state_to_git) — читаем его
            # ДО git reset --hard, точно так же, как local_milestone выше,
            # и мёржим с версией из origin/main по ключу (headline, summary),
            # чтобы не потерять записи, добавленные другим успевшим запуском.
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

            # speed_stats.json — та же логика: локальный файл уже дописан
            # до reset --hard, просто конкатенируем с origin/main и
            # обрезаем до лимита. Мягкая аналитика, не критичная для
            # дедупа — точный дедуп записей здесь не нужен.
            local_speed_stats = load_speed_stats()
            merged_speed_stats = (list(remote_speed_stats) + local_speed_stats)[-SPEED_STATS_LIMIT:]

            # live_stories.json — мёржим по message_id, оставляя для каждого
            # треда версию с более свежим last_update_ts (та, что "видела"
            # больше обновлений), и сразу отбрасываем остывшие трансляции
            # (без обновлений дольше LIVE_STORY_MAX_AGE_HOURS), чтобы файл
            # не рос бесконечно завершёнными сюжетами.
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

            merged_digest = new_digest_state if new_digest_state is not None else remote_digest
            merged_poll = new_poll_state if new_poll_state is not None else remote_poll
            merged_quiz = new_quiz_state if new_quiz_state is not None else remote_quiz
            merged_weekly_recap = new_weekly_recap_state if new_weekly_recap_state is not None else remote_weekly_recap
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
            _save_json(WEEKLY_RECAP_STATE_FILE, merged_weekly_recap)
            _save_json(ALERT_STATE_FILE, merged_alert)
            _save_json(STATUS_FILE, merged_status)
            _save_json(RECENT_POSTS_FILE, merged_recent_posts)
            _save_json(SPEED_STATS_FILE, merged_speed_stats)
            _save_json(LIVE_STORIES_FILE, merged_live_threads)

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

    # Признаки 3/4: считаем "тишину" один раз в начале запуска — до того,
    # как что-либо в этом запуске может обновить last_run.json — иначе
    # алерт никогда не сработает (свежая публикация всегда обнулит elapsed).
    elapsed_at_start = seconds_since_last_publish()
    new_alert_timestamp = check_silence_alert(elapsed_at_start)

    def persist_with_status(sent_count=None, note="", **kwargs):
        status = build_status_snapshot(elapsed_at_start, sent_count=sent_count, note=note)
        persist_state_to_git(
            new_alert_timestamp=new_alert_timestamp,
            new_status=status,
            **kwargs,
        )

    # --- Реакции: включаем один раз, статус запоминаем, чтобы не дёргать API зря ---
    # --- Ежедневный вовлекающий опрос ---
    poll_state = _load_json(POLL_STATE_FILE, {"date": None, "sent": False})
    if poll_state.get("date") != day_key:
        poll_state = {"date": day_key, "sent": False}
    new_poll_state = None
    if due_poll(dt_msk, poll_state.get("sent")):
        variant = pick_poll_variant(day_key)
        if send_poll_to_telegram(variant["question"], variant["options"]):
            poll_state["sent"] = True
            new_poll_state = poll_state
            print(f"[INFO] Engagement poll sent: '{variant['question']}'")

    # --- Ежедневный квиз по реальным опубликованным новостям ---
    quiz_state = _load_json(QUIZ_STATE_FILE, {"date": None, "sent": False})
    if quiz_state.get("date") != day_key:
        quiz_state = {"date": day_key, "sent": False}
    new_quiz_state = None
    if due_quiz(dt_msk, quiz_state.get("sent")):
        source_post = pick_quiz_source_post(load_recent_posts())
        quiz = build_quiz_from_post(source_post)
        if quiz and send_quiz_poll(quiz):
            quiz_state["sent"] = True
            new_quiz_state = quiz_state
            print(f"[INFO] Daily quiz sent: '{quiz['question']}'")
        else:
            # Не нашлось материала или GigaChat не смог составить вопрос —
            # помечаем день пройденным, чтобы не пытаться на каждом
            # следующем тике в это же окно.
            quiz_state["sent"] = True
            new_quiz_state = quiz_state
            print("[INFO] No material/quiz for today, marking as done anyway.")

    count = get_subscriber_count()
    update_channel_description(count)
    maybe_celebrate_milestone(count)

    # --- Еженедельный рекап "Главное за неделю" ---
    week_key = week_key_for(dt_msk)
    weekly_recap_state = _load_json(WEEKLY_RECAP_STATE_FILE, {"week_key": None, "sent": False})
    new_weekly_recap_state = None
    if due_weekly_recap(dt_msk, week_key, weekly_recap_state):
        recap_items = pick_weekly_recap_items(load_recent_posts())
        if recap_items:
            recap_text = format_weekly_recap(recap_items)
            if send_to_telegram(recap_text):
                new_weekly_recap_state = {"week_key": week_key, "sent": True}
                print(f"[INFO] Weekly recap sent: {len(recap_items)} items.")
        else:
            # Нечего показывать (например, самая первая неделя работы бота) —
            # всё равно помечаем неделю пройденной, чтобы не пытаться на
            # каждом следующем тике воскресенья в это же окно.
            new_weekly_recap_state = {"week_key": week_key, "sent": True}
            print("[INFO] No material for weekly recap, marking week as done anyway.")

    # --- Дайджест по расписанию (утро/вечер) ---
    digest_state = _load_json(DIGEST_STATE_FILE, {"date": None, "slots": []})
    if digest_state.get("date") != day_key:
        digest_state = {"date": day_key, "slots": []}
    new_digest_state = None

    slot = due_digest_slot(dt_msk, digest_state.get("slots", []))
    if slot:
        digest_items = fetch_news()
        digest_items.sort(key=lambda it: not it.get("urgent"))  # срочные — в начало

        # РАДИКАЛЬНЫЙ ФИКС: та же проверка по словам+именным стемам
        # (Уровень B), что и в pick_non_duplicate — без неё дайджест мог
        # включить новость, уже опубликованную одиночным постом другими
        # словами. ИИ-проверку (Уровень C) здесь не делаем — дайджест и
        # так собирает до 5 новостей, дороже по времени/токенам смысла
        # мало, Уровня B обычно достаточно для этого сценария.
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
                # ФИКС: раньше слова заголовков элементов дайджеста нигде не
                # сохранялись — из-за этого meaning-дедуп "не знал" про
                # новости, ушедшие в дайджест, и потом мог пропустить точно
                # такую же новость от другого канала как "новую".
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
                    })
                save_posted(posted)
                save_recent_title_words(recent_words)
                save_recent_posts(load_recent_posts() + new_recent_posts_entries)

                digest_state["slots"] = digest_state.get("slots", []) + [slot]
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
        if new_digest_state or new_poll_state or new_alert_timestamp or new_weekly_recap_state or new_quiz_state:
            persist_with_status(
                note="skip:too_soon_urgent",
                new_digest_state=new_digest_state,
                new_poll_state=new_poll_state,
                new_weekly_recap_state=new_weekly_recap_state,
                new_quiz_state=new_quiz_state,
            )
        return

    news = fetch_news()
    if not news:
        print("[INFO] No new news.")
        if new_digest_state or new_poll_state or new_alert_timestamp or new_weekly_recap_state or new_quiz_state:
            persist_with_status(
                note="skip:no_news",
                new_digest_state=new_digest_state,
                new_poll_state=new_poll_state,
                new_weekly_recap_state=new_weekly_recap_state,
                new_quiz_state=new_quiz_state,
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
            if new_digest_state or new_poll_state or new_alert_timestamp or new_weekly_recap_state or new_quiz_state:
                persist_with_status(
                    note="skip:too_soon_normal",
                    new_digest_state=new_digest_state,
                    new_poll_state=new_poll_state,
                new_weekly_recap_state=new_weekly_recap_state,
                new_quiz_state=new_quiz_state,
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
        if new_digest_state or new_poll_state or new_alert_timestamp or new_weekly_recap_state or new_quiz_state:
            persist_with_status(
                note="skip:all_duplicates",
                new_digest_state=new_digest_state,
                new_poll_state=new_poll_state,
                new_weekly_recap_state=new_weekly_recap_state,
                new_quiz_state=new_quiz_state,
            )
        return

    # "Продолжение истории": кандидат уже прошёл все проверки на дубликат
    # выше — если он всё же связан с недавним постом (общее место/персона),
    # добавим ссылку на предыдущий пост вместо того, чтобы публиковать
    # несвязанные друг с другом посты об одном и том же сюжете.
    _recent_posts_for_continuation = load_recent_posts()
    chosen["continuation_of"] = find_story_continuation(
        chosen["title"], chosen.get("summary", ""), _recent_posts_for_continuation,
        exclude_entities=compute_common_entity_stems(_recent_posts_for_continuation),
    )

    chosen = finalize_item(chosen)

    # ЖИВАЯ ТРАНСЛЯЦИЯ: только для срочных новостей. Если кандидат — явное
    # развитие уже идущей трансляции (общее место/персона, трансляция не
    # "остыла"), редактируем существующий закреплённый пост вместо
    # публикации нового — читатель видит один растущий живой пост, а не
    # ленту разрозненных сообщений об одном и том же событии.
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
                mark_published_now()
                persist_with_status(
                    sent_count=1,
                    note="live_thread_update",
                    new_posted_ids=new_posted_ids,
                    new_title_words_list=new_title_words_list,
                    new_last_publish=time.time(),
                    new_digest_state=new_digest_state,
                    new_poll_state=new_poll_state,
                    new_weekly_recap_state=new_weekly_recap_state,
                    new_quiz_state=new_quiz_state,
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
            }])
            latency = compute_publish_latency_seconds(item.get("published"))
            if latency is not None:
                save_speed_stats(load_speed_stats() + [{"latency_seconds": latency, "ts": time.time()}])
            if item.get("urgent") and isinstance(ok, int):
                # Срочная новость без подходящей активной трансляции —
                # начинаем новую и закрепляем пост, чтобы дальнейшие
                # обновления по этой теме редактировали именно его.
                new_thread = start_live_thread(item, ok)
                save_live_threads(load_live_threads() + [new_thread])
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
    )

    print(f"[DONE] Sent {sent_count}/{len(news)} items as separate posts.")


if __name__ == "__main__":
    main()
