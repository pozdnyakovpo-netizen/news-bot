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

FEEDS_FILE = os.path.join(os.path.dirname(__file__), "feeds.txt")
POSTED_FILE = os.path.join(os.path.dirname(__file__), "posted.json")

MAX_ITEMS_PER_FEED = 3
MAX_POSTS_PER_RUN = 8
DESCRIPTION_MAX_LEN_TEXT = 600      # для обычного текстового поста
DESCRIPTION_MAX_LEN_CAPTION = 800   # для подписи к фото/видео (лимит Telegram — 1024)

MEDIA_NS = {"media": "http://search.yahoo.com/mrss/"}

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"


# ---------- служебные функции ----------

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
        raise FileNotFoundError(f"Не найден {FEEDS_FILE}. Создайте файл со списком RSS-ссылок (по одной на строку).")
    with open(FEEDS_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def strip_html(text):
    text = re.sub(r"<[^>]+>", "", text or "")
    return html.unescape(text).strip()


def truncate(text, max_len):
    text = " ".join(text.split())  # убираем лишние пробелы/переносы
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    # обрезаем по последнему пробелу, чтобы не рвать слово
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(",.;: ") + "…"


def get_source_name(url):
    """Достаём человекочитаемое имя источника из домена."""
    m = re.search(r"https?://(www\.)?([^/]+)", url)
    return m.group(2) if m else url


def find_image_in_html(text):
    if not text:
        return None
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', text)
    return m.group(1) if m else None


def extract_media(item):
    """Возвращает (media_url, media_type) где media_type = 'photo' или 'video', либо (None, None)."""
    # media:content / media:thumbnail
    media_content = item.find("media:content", MEDIA_NS)
    if media_content is not None:
        url = media_content.get("url")
        mtype = media_content.get("medium") or media_content.get("type", "")
        if url:
            if "video" in mtype:
                return url, "video"
            return url, "photo"

    media_thumb = item.find("media:thumbnail", MEDIA_NS)
    if media_thumb is not None and media_thumb.get("url"):
        return media_thumb.get("url"), "photo"

    # enclosure (частый способ для видео/фото в RSS)
    enclosure = item.find("enclosure")
    if enclosure is not None:
        url = enclosure.get("url")
        etype = enclosure.get("type", "")
        if url:
            if "video" in etype:
                return url, "video"
            if "image" in etype:
                return url, "photo"

    # картинка внутри description/content:encoded
    for tag in ("description", "{http://purl.org/rss/1.0/modules/content/}encoded"):
        el = item.find(tag)
        if el is not None and el.text:
            img = find_image_in_html(el.text)
            if img:
                return img, "photo"

    return None, None


def parse_feed(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (news-bot)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = resp.read()
    root = ET.fromstring(data)

    items = []
    channel = root.find("channel")
    if channel is not None:
        for item in channel.findall("item")[:MAX_ITEMS_PER_FEED]:
            title = strip_html(item.findtext("title", ""))
            link = item.findtext("link", "")
            desc = item.findtext("description", "") or item.findtext(
                "{http://purl.org/rss/1.0/modules/content/}encoded", ""
            )
            desc = strip_html(desc)
            guid = item.findtext("guid", link) or link
            media_url, media_type = extract_media(item)
            items.append({
                "id": guid,
                "title": title,
                "description": desc,
                "link": link,
                "media_url": media_url,
                "media_type": media_type,
                "source": get_source_name(link or url),
            })
    else:
        # Atom
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("a:entry", ns)[:MAX_ITEMS_PER_FEED]:
            title = strip_html(entry.findtext("a:title", "", ns))
            link_el = entry.find("a:link", ns)
            link = link_el.get("href") if link_el is not None else ""
            summary = strip_html(entry.findtext("a:summary", "", ns) or entry.findtext("a:content", "", ns))
            guid = entry.findtext("a:id", link, ns) or link
            items.append({
                "id": guid,
                "title": title,
                "description": summary,
                "link": link,
                "media_url": None,
                "media_type": None,
                "source": get_source_name(link or url),
            })

    return items


# ---------- отправка в Telegram ----------

def send_request(method, params, files=None):
    if files:
        # multipart не требуется — используем прямые URL для фото/видео (по ссылке), без загрузки файлов
        pass
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(f"{API_URL}/{method}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        return {"ok": False, "error": str(e), "body": body}


def build_caption(item):
    title = item["title"]
    desc = truncate(item["description"], DESCRIPTION_MAX_LEN_CAPTION - len(title) - 40)
    caption = f"<b>{html.escape(title)}</b>"
    if desc:
        caption += f"\n\n{html.escape(desc)}"
    caption += f"\n\n<i>{html.escape(item['source'])}</i>"
    return caption


def build_text(item):
    title = item["title"]
    desc = truncate(item["description"], DESCRIPTION_MAX_LEN_TEXT - len(title) - 40)
    text = f"<b>{html.escape(title)}</b>"
    if desc:
        text += f"\n\n{html.escape(desc)}"
    text += f"\n\n<i>{html.escape(item['source'])}</i>"
    return text


def post_item(item):
    if item["media_type"] == "photo" and item["media_url"]:
        params = {
            "chat_id": CHANNEL_ID,
            "photo": item["media_url"],
            "caption": build_caption(item),
            "parse_mode": "HTML",
        }
        result = send_request("sendPhoto", params)
        if result.get("ok"):
            return True
        # если Telegram не смог загрузить фото по ссылке — публикуем как текст
        return post_as_text(item)

    if item["media_type"] == "video" and item["media_url"]:
        params = {
            "chat_id": CHANNEL_ID,
            "video": item["media_url"],
            "caption": build_caption(item),
            "parse_mode": "HTML",
        }
        result = send_request("sendVideo", params)
        if result.get("ok"):
            return True
        return post_as_text(item)

    return post_as_text(item)


def post_as_text(item):
    params = {
        "chat_id": CHANNEL_ID,
        "text": build_text(item),
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    result = send_request("sendMessage", params)
    return bool(result.get("ok"))


# ---------- основной запуск ----------

def main():
    if not BOT_TOKEN or not CHANNEL_ID:
        raise RuntimeError("Не заданы TELEGRAM_BOT_TOKEN или TELEGRAM_CHANNEL_ID (переменные окружения / secrets).")

    posted = load_posted()
    feeds = load_feeds()

    total_sent = 0
    for feed_url in feeds:
        if total_sent >= MAX_POSTS_PER_RUN:
            break
        try:
            items = parse_feed(feed_url)
        except Exception as e:
            print(f"Ошибка при чтении {feed_url}: {e}")
            continue

        for item in items:
            if total_sent >= MAX_POSTS_PER_RUN:
                break
            if item["id"] in posted:
                continue

            ok = post_item(item)
            if ok:
                posted.add(item["id"])
                total_sent += 1
                time.sleep(2)  # небольшая пауза между постами
            else:
                print(f"Не удалось опубликовать: {item['title']}")

    save_posted(posted)
    print(f"Опубликовано новых постов: {total_sent}")


if __name__ == "__main__":
    main()
