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
    # enclosure
    enclosure = item_el.find("enclosure")
    if enclosure is not None:
        url = enclosure.get("url")
        mime = (enclosure.get("type") or "").lower()
        if url:
            if "video" in mime:
                return url, "video"
            if "image" in mime:
                return url, "photo"

    # media:content / media:thumbnail
    for tag in ("media:content", "media:thumbnail"):
        el = item_el.find(tag, MEDIA_NS)
        if el is not None:
            url = el.get("url")
            mime = (el.get("type") or "").lower()
            if url:
                if "video" in mime:
                    return url, "video"
                return url, "photo"

    # media:group/media:content
    group = item_el.find("media:group", MEDIA_NS)
    if group is not None:
        for el in group.findall("media:content", MEDIA_NS):
            url = el.get("url")
            mime = (el.get("type") or "").lower()
            if url:
                if "video" in mime:
                    return url, "video"
                return url, "photo"

    # искать в теле description/content:encoded
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


def paraphrase(title, desc):
    """Перефразирует заголовок и описание своими словами через Claude. При ошибке возвращает оригинал."""
    if not ANTHROPIC_API_KEY or not desc:
        return title, desc
    try:
        prompt = (
            "Перефразируй эту новость своими словами для Telegram-канала: "
            "живо, подробно, без изменения фактов, без своих оценок и мнений. "
            "Раскрой все детали, контекст, предысторию и подробности, которые есть в исходном тексте — "
            "не сокращай информацию, а перескажи её максимально полно и понятно, добавляя нужный контекст, "
            "предысторию события и возможные последствия, если это уместно. "
            "Объём — 10-15 предложений, если в исходном тексте достаточно материала для этого; "
            "если материала мало — пиши столько, сколько позволяет исходный текст, но не сокращай искусственно. "
            "Ответь СТРОГО в формате:\n"
            "ЗАГОЛОВОК: <новый короткий заголовок>\n"
            "ТЕКСТ: <10-15 предложений текста>\n\n"
            f"Исходный заголовок: {title}\n"
            f"Исходный текст: {desc}"
        )
        payload = json.dumps({
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1800,
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

        new_title, new_desc = title, desc
        m_title = re.search(r"ЗАГОЛОВОК:\s*(.+)", result)
        m_text = re.search(r"ТЕКСТ:\s*(.+)", result, re.DOTALL)
        if m_title:
            new_title = m_title.group(1).strip()
        if m_text:
            new_desc = m_text.group(1).strip()
        return new_title, new_desc
    except Exception as e:
        print(f"[перефразирование] не удалось, публикую оригинал: {e}")
        return title, desc


def build_caption(title, desc, source_name):
    text = f"<b>{html.escape(title)}</b>"
    if desc:
        d = desc[:DESCRIPTION_MAX_LEN_CAPTION].rstrip()
        text += f"\n\n{html.escape(d)}"
    return text


def build_text(title, desc, source_name):
    text = f"<b>{html.escape(title)}</b>"
    if desc:
        d = desc[:DESCRIPTION_MAX_LEN_TEXT].rstrip()
        text += f"\n\n{html.escape(d)}"
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


def send_to_telegram(item, source_name):
    title, desc, media_url, media_type = item["title"], item["desc"], item["media_url"], item["media_type"]

    if media_type == "photo" and media_url:
        caption = build_caption(title, desc, source_name)
        tg_api_call("sendPhoto", {
            "chat_id": CHANNEL_ID,
            "photo": media_url,
            "caption": caption,
            "parse_mode": "HTML",
        })
    elif media_type == "video" and media_url:
        caption = build_caption(title, desc, source_name)
        tg_api_call("sendVideo", {
            "chat_id": CHANNEL_ID,
            "video": media_url,
            "caption": caption,
            "parse_mode": "HTML",
        })
    else:
        text = build_text(title, desc, source_name)
        tg_api_call("sendMessage", {
            "chat_id": CHANNEL_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        })


def source_name_from_url(url):
    m = re.search(r"https?://(?:www\.)?([^/]+)", url)
    return m.group(1) if m else url


def main():
    if not BOT_TOKEN or not CHANNEL_ID:
        raise SystemExit("Не заданы TELEGRAM_BOT_TOKEN и/или TELEGRAM_CHANNEL_ID.")

    posted = load_posted()
    feeds = load_feeds()
    total_sent = 0

    for feed_url in feeds:
        if total_sent >= MAX_POSTS_PER_RUN:
            break
        try:
            items = fetch_feed(feed_url)
        except Exception as e:
            print(f"[ошибка] не удалось прочитать {feed_url}: {e}")
            continue

        source_name = source_name_from_url(feed_url)
        new_items = [it for it in items if it["id"] not in posted][:MAX_ITEMS_PER_FEED]

        for item in reversed(new_items):
            if total_sent >= MAX_POSTS_PER_RUN:
                break
            item["title"], item["desc"] = paraphrase(item["title"], item["desc"])
            try:
                send_to_telegram(item, source_name)
                posted.add(item["id"])
                total_sent += 1
                print(f"[ок] опубликовано: {item['title'][:60]}")
                time.sleep(2)
            except Exception as e:
                print(f"[ошибка] не удалось опубликовать: {e}")

    save_posted(posted)
    print(f"Готово. Опубликовано новых постов: {total_sent}")


if __name__ == "__main__":
    main()
