#!/usr/bin/env python3
"""Generate podcast cover/episode art and emit an iTunes-compatible RSS feed."""
import base64
import datetime as dt
import email.utils
import html
import json
import os
import pathlib
import shutil
import urllib.request
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parents[1]
ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ET.register_namespace("itunes", ITUNES)


def generate_image(prompt, destination):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is required to generate missing podcast artwork")
    models = ("gemini-3.1-flash-image", "gemini-2.5-flash-image")
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }
    last_error = None
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                result = json.load(response)
            for candidate in result.get("candidates", []):
                for part in candidate.get("content", {}).get("parts", []):
                    data = part.get("inlineData") or part.get("inline_data")
                    if data and data.get("data"):
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(base64.b64decode(data["data"]))
                        print(f"Generated {destination.relative_to(ROOT)} with {model}", flush=True)
                        return
            raise RuntimeError(f"{model} returned no image bytes")
        except Exception as exc:
            last_error = exc
            print(f"Artwork model {model} failed: {exc}; trying fallback if available", flush=True)
    raise RuntimeError(f"Unable to generate artwork: {last_error}")


def ensure_artwork(path, prompt):
    if not path.is_file():
        generate_image(prompt, path)


def esc(value):
    return html.escape(str(value), quote=True)


def main():
    base_url = os.environ["PODCAST_BASE_URL"].rstrip("/")
    assets = ROOT / "assets"
    site = ROOT / "site"
    assets.mkdir(parents=True, exist_ok=True)
    site.mkdir(parents=True, exist_ok=True)
    bible_path = ROOT / "show_bible.json"
    bible = json.loads(bible_path.read_text(encoding="utf-8"))
    ensure_artwork(
        assets / "cover.png",
        "Square premium illustrated gaming podcast cover, exact large title SNOWY SUNDAY PODCAST and subtitle SUNDAY RADAR. Two original fictional friendly male hosts: Felix calm and analytical with notebook/headset, Jasper energetic with headset/controller. Midnight indigo snowy background, clean radar sweep, cyan and orange accents, polished vector-like art, crisp readable typography, no logos or watermark, safe central composition.",
    )
    for episode in bible.get("episodes", []):
        image_name = episode.get("image") or f"episode-{int(episode['number']):03}.png"
        episode["image"] = image_name
        ensure_artwork(
            assets / image_name,
            f"Square illustrated podcast thumbnail for {episode.get('title', 'Sunday Podcast')}. Show title {bible.get('show_title', 'Sunday Podcast')}. Felix is a calm analytical fictional gaming host; Jasper is an energetic fictional gaming host. Premium midnight-indigo snowy gaming aesthetic, cyan/orange accents, clear central composition, original characters, no logos or watermark. Include only short readable title text based on the episode title.",
        )
    bible_path.write_text(json.dumps(bible, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    site_assets = site / "assets"
    site_assets.mkdir(parents=True, exist_ok=True)
    shutil.copy2(assets / "cover.png", site_assets / "cover.png")
    for episode in bible.get("episodes", []):
        shutil.copy2(assets / episode["image"], site_assets / episode["image"])

    channel = ET.Element("channel")
    ET.SubElement(channel, "title").text = bible.get("show_title", "Sunday Podcast")
    ET.SubElement(channel, "link").text = base_url + "/"
    ET.SubElement(channel, "description").text = "Sunday Podcast with Felix and Jasper"
    ET.SubElement(channel, f"{{{ITUNES}}}image", {"href": f"{base_url}/assets/cover.png"})
    for episode in reversed(bible.get("episodes", [])):
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = episode.get("title", "Sunday Podcast")
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = episode["audio"]
        pubdate = dt.datetime.fromisoformat(episode["date"]).replace(tzinfo=dt.timezone.utc)
        ET.SubElement(item, "pubDate").text = email.utils.format_datetime(pubdate)
        ET.SubElement(item, "enclosure", {
            "url": f"{base_url}/{episode['audio']}", "length": "0", "type": "audio/mpeg"
        })
        ET.SubElement(item, "description").text = episode.get("continuity_update", "")
        image = episode.get("image")
        if image and (assets / image).is_file():
            ET.SubElement(item, f"{{{ITUNES}}}image", {"href": f"{base_url}/assets/{image}"})
    rss = ET.Element("rss", {"version": "2.0"})
    rss.append(channel)
    xml = ET.tostring(rss, encoding="utf-8", xml_declaration=True)
    (site / "feed.xml").write_bytes(xml)
    (site / "podcast.xml").write_bytes(xml)
    print(f"Wrote iTunes image tags and feeds for {len(bible.get('episodes', []))} episode(s)", flush=True)


if __name__ == "__main__":
    main()
