#!/usr/bin/env python3
import os
import json
import time
import html
import re
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

FEEDS_FILE = os.path.join(os.path.dirname(__file__), "feeds.txt")
POSTED_FILE = os.path.join(os.path.dirname(__file__), "posted.json")

MAX_ITEMS_PER_FEED = 4
MAX_POSTS_PER_RUN = 15
DESCRIPTION_MAX_LEN_TEXT = 2500
DESCRIPTION_MAX_LEN_CAPTION = 1800

DIGEST_MAX_ITEMS_PER_FEED = 2
DIGEST_MAX_ITEMS_TOTAL = 12
DIGEST_MESSAGE_LIMIT = 3800

MEDIA_NS = {"media": "http://search.yahoo.com/mrss/"}


def load_posted():
    if os.path.exists(POSTED_FILE):
        with open(POSTED_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_posted(posted_ids):
    with open(POSTED_FILE, "w", encoding="utf-8") as f:
        json.dump(list(posted_ids)[-2000:], f, ensure_ascii=False)


def load_feeds():
    if not os.path.exists(FEEDS_FILE):
        raise FileNotFoundError(f"Не найден {FEEDS_FILE}.")
    with open(FEEDS_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def strip_html(text):
    text = re.sub(r"<[^>]+>", "", text or "")
    return html.unescape(text).strip()


def find_image_in_html(text):
    if not text:
        return None
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', text)
    return m.group(1) if m else None


def find_video_in_html(text):
    if not text:
        return None
    m = re.search(r'<(?:video|source)[^>]+src=["\']([^"\']+)["\']', text)
    return m.group(1) if m else None


def extract_media(item_el):
    """Возвращает (media_url, media_type) где media_type = 'photo' или 'video', либо (None, None)."""
    enclosure = item_el.find("enclosure")
    if enclosure is not None:
        url = enclosure.get("url")
        mime = (enclosure.get("type") or "").lower()
        if url:
            if "video" in mime:
                return url, "video"
            if "image" in mime:
                return url, "photo"

    for tag in ("media:content", "media:thumbnail"):
        el = item_el.find(tag, MEDIA_NS)
        if el is not None:
            url = el.get("url")
            mime = (el.get("type") or "").lower()
            if url:
                if "video" in mime:
                    return url, "video"
                return url, "photo"

    group = item_el.find("media:group", MEDIA_NS)
    if group is not None:
        for el in group.findall("media:content", MEDIA_NS):
            url = el.get("url")
            mime = (el.get("type") or "").lower()
            if url:
                if "video" in mime:
                    return url, "video"
                return url, "photo"

    raw_desc = item_el.findtext("description", "") or ""
    content_encoded_el = item_el.find("{http://purl.org/rss/1.0/modules/content/}encoded")
    raw_content = content_encoded_el.text if content_encoded_el is not None else ""
    combined = (raw_desc or "") + (raw_content or "")

    video_url = find_video_in_html(combined)
    if video_url:
        return video_url, "video"

    img_url = find_image_in_html(combined)
    if img_url:
        return img_url, "photo"

    return None, None


def fetch_feed(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (news-bot)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = resp.read()

    root = ET.fromstring(data)
    items = []

    for item_el in root.findall(".//item"):
        title = strip_html(item_el.findtext("title", ""))
        link = (item_el.findtext("link", "") or "").strip()
        desc_raw = item_el.findtext("description", "") or ""
        content_encoded_el = item_el.find("{http://purl.org/rss/1.0/modules/content/}encoded")
        if content_encoded_el is not None and content_encoded_el.text:
            desc_raw = content_encoded_el.text
        desc = strip_html(desc_raw)
        guid = (item_el.findtext("guid", "") or link or title).strip()

        media_url, media_type = extract_media(item_el)

        if title and link:
            items.append({
                "id": guid, "title": title, "link": link, "desc": desc,
                "media_url": media_url, "media_type": media_type,
            })

    if not items:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for entry in root.findall(".//a:entry", ns):
            title = strip_html(entry.findtext("a:title", "", ns))
            link_el = entry.find("a:link", ns)
            link = link_el.get("href") if link_el is not None else ""
            desc = strip_html(entry.findtext("a:summary", "", ns))
            guid = (entry.findtext("a:id", "", ns) or link or title).strip()
            if title and link:
                items.append({
                    "id": guid, "title": title, "link": link, "desc": desc,
                    "media_url": None, "media_type": None,
                })

    return items


def digest_summarize(title, desc):
    """Короткий пересказ для карточки внутри дайджеста: эмодзи + мини-заголовок + 2-3 предложения."""
    fallback_title = f"📌 {title}"
    fallback_text = (desc or "")[:280]
    if not ANTHROPIC_API_KEY or not desc:
        return fallback_title, fallback_text
    try:
        prompt = (
            "Кратко перескажи эту новость для карточки в дайджесте Telegram-канала. "
            "Без изменения фактов, без своих оценок. "
            "Заголовок — с ОДНИМ подходящим по смыслу эмодзи в начале, короткий и ёмкий (до 8 слов). "
            "Текст — 2-3 предложения, только самая суть, без воды.\n\n"
            "Ответь СТРОГО в формате:\n"
            "ЗАГОЛОВОК: <эмодзи> <заголовок>\n"
            "ТЕКСТ: <2-3 предложения>\n\n"
            f"Исходный заголовок: {title}\n"
            f"Исходный текст: {desc}"
        )
        payload = json.dumps({
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 400,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        result = data["content"][0]["text"].strip()

        new_title, new_text = fallback_title, fallback_text
        m_title = re.search(r"ЗАГОЛОВОК:\s*(.+)", result)
        m_text = re.search(r"ТЕКСТ:\s*(.+)", result, re.DOTALL)
        if m_title:
            new_title = m_title.group(1).strip()
        if m_text:
            new_text = m_text.group(1).strip()
        return new_title, new_text
    except Exception as e:
        print(f"[дайджест] не удалось перефразировать, использую оригинал: {e}")
        return fallback_title, fallback_text


def digest_label_from_env():
    """Заголовок дайджеста: берётся из переменной DIGEST_LABEL (задаётся в workflow по расписанию),
    либо подбирается по текущему часу UTC для ручного запуска."""
    env_label = os.environ.get("DIGEST_LABEL", "").strip()
    if env_label:
        return env_label
    hour_utc = time.gmtime().tm_hour
    if 3 <= hour_utc < 12:
        return "☀️ Утренний дайджест"
    elif 12 <= hour_utc < 19:
        return "🗞 Дневной дайджест"
    else:
        return "🌙 Вечерний дайджест"


def build_digest_text(label, entries):
    date_str = time.strftime("%d.%m.%Y")
    header = f"<b>{html.escape(label)} — {date_str}</b>\n<i>Главное за это время, коротко:</i>"

    number_emoji = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    separator = "▫️▫️▫️▫️▫️"

    def make_blocks(items):
        blocks = []
        for idx, (entry_title, entry_text) in enumerate(items):
            num = number_emoji[idx] if idx < len(number_emoji) else f"{idx + 1}."
            block = f"{num} <b>{html.escape(entry_title)}</b>"
            if entry_text:
                block += f"\n{html.escape(entry_text)}"
            blocks.append(block)
        return blocks

    footer = "Если понравился дайджест — оставьте реакцию 🔥, это помогает каналу расти!\n\n#дайджест #новости"

    def assemble(items):
        blocks = make_blocks(items)
        body = f"\n\n{separator}\n\n".join(blocks)
        return header + "\n\n" + body + "\n\n" + separator + "\n\n" + footer

    text = assemble(entries)
    while len(text) > DIGEST_MESSAGE_LIMIT and entries:
        entries = entries[:-1]
        text = assemble(entries)
    return text


def tg_api_call(method, params):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Telegram API ошибка {e.code}: {e.read().decode()}")
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API вернул ошибку: {result}")
    return result


def send_digest(text, label, cover_media_url, cover_media_type):
    """Отправляет дайджест: если есть обложка — сначала короткое фото/видео-интро,
    затем сам дайджест отдельным текстовым сообщением."""
    intro = f"<b>{html.escape(label)}</b>"

    if cover_media_type == "photo" and cover_media_url:
        tg_api_call("sendPhoto", {
            "chat_id": CHANNEL_ID, "photo": cover_media_url,
            "caption": intro, "parse_mode": "HTML",
        })
    elif cover_media_type == "video" and cover_media_url:
        tg_api_call("sendVideo", {
            "chat_id": CHANNEL_ID, "video": cover_media_url,
            "caption": intro, "parse_mode": "HTML",
        })

    tg_api_call("sendMessage", {
        "chat_id": CHANNEL_ID, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true",
    })


def main():
    if not BOT_TOKEN or not CHANNEL_ID:
        raise SystemExit("Не заданы TELEGRAM_BOT_TOKEN и/или TELEGRAM_CHANNEL_ID.")

    posted = load_posted()
    feeds = load_feeds()

    entries = []
    entry_ids = []
    cover_media_url = None
    cover_media_type = None

    for feed_url in feeds:
        if len(entries) >= DIGEST_MAX_ITEMS_TOTAL:
            break
        try:
            items = fetch_feed(feed_url)
        except Exception as e:
            print(f"[ошибка] не удалось прочитать {feed_url}: {e}")
            continue

        new_items = [it for it in items if it["id"] not in posted][:DIGEST_MAX_ITEMS_PER_FEED]

        for item in reversed(new_items):
            if len(entries) >= DIGEST_MAX_ITEMS_TOTAL:
                break
            entry_title, entry_text = digest_summarize(item["title"], item["desc"])
            entries.append((entry_title, entry_text))
            entry_ids.append(item["id"])

            if cover_media_url is None and item["media_url"]:
                cover_media_url = item["media_url"]
                cover_media_type = item["media_type"]

            print(f"[ок] добавлено в дайджест: {item['title'][:60]}")

    if not entries:
        print("Готово. Новых новостей для дайджеста не найдено.")
        return

    label = digest_label_from_env()
    text = build_digest_text(label, entries)

    try:
        send_digest(text, label, cover_media_url, cover_media_type)
        for eid in entry_ids:
            posted.add(eid)
        save_posted(posted)
        print(f"Готово. Дайджест опубликован, новостей в нём: {len(entries)}")
    except Exception as e:
        print(f"[ошибка] не удалось опубликовать дайджест: {e}")


if __name__ == "__main__":
    main()
