#!/usr/bin/env python3
import os
import json
import time
import html
import re
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")

FEEDS_FILE = os.path.join(os.path.dirname(__file__), "feeds.txt")
POSTED_FILE = os.path.join(os.path.dirname(__file__), "posted.json")

MAX_ITEMS_PER_FEED = 3
MAX_POSTS_PER_RUN = 8
DESCRIPTION_MAX_LEN = 280


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


def fetch_feed(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (news-bot)"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = resp.read()
    root = ET.fromstring(data)
    items = []
    for item in root.findall(".//item"):
        title = strip_html(item.findtext("title", ""))
        link = (item.findtext("link", "") or "").strip()
        desc = strip_html(item.findtext("description", ""))
        guid = (item.findtext("guid", "") or link or title).strip()
        if title and link:
            items.append({"id": guid, "title": title, "link": link, "desc": desc})
    if not items:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for entry in root.findall(".//a:entry", ns):
            title = strip_html(entry.findtext("a:title", "", ns))
            link_el = entry.find("a:link", ns)
            link = link_el.get("href") if link_el is not None else ""
            desc = strip_html(entry.findtext("a:summary", "", ns))
            guid = (entry.findtext("a:id", "", ns) or link or title).strip()
            if title and link:
                items.append({"id": guid, "title": title, "link": link, "desc": desc})
    return items


def format_post(item, source_name):
    desc = item["desc"]
    if len(desc) > DESCRIPTION_MAX_LEN:
        desc = desc[:DESCRIPTION_MAX_LEN].rsplit(" ", 1)[0] + "…"
    text = f"<b>{html.escape(item['title'])}</b>"
    if desc:
        text += f"\n{html.escape(desc)}"
    text += f"\n\n<a href=\"{item['link']}\">Читать полностью</a> · {html.escape(source_name)}"
    return text


def send_to_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "false",
    }).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


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
            text = format_post(item, source_name)
            try:
                send_to_telegram(text)
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
