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
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parents[1]
ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ET.register_namespace("itunes", ITUNES)
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 2


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
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                with urllib.request.urlopen(request, timeout=180) as response:
                    result = json.load(response)
                image_bytes = None
                for candidate in result.get("candidates", []):
                    for part in candidate.get("content", {}).get("parts", []):
                        data = part.get("inlineData") or part.get("inline_data")
                        if data and data.get("data"):
                            image_bytes = base64.b64decode(data["data"])
                            break
                    if image_bytes:
                        break
                if not image_bytes:
                    raise RuntimeError(f"{model} returned no image bytes")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(image_bytes)
                print(f"Generated {destination.relative_to(ROOT)} with {model}", flush=True)
                return
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code == 429 and attempt < MAX_ATTEMPTS:
                    delay = BACKOFF_SECONDS * (2 ** (attempt - 1))
                    print(f"Artwork model {model} rate limited (429); retry {attempt + 1}/{MAX_ATTEMPTS} in {delay}s", flush=True)
                    time.sleep(delay)
                    continue
                print(f"Artwork model {model} failed: {exc}; trying next model if available", flush=True)
                break
            except Exception as exc:
                last_error = exc
                print(f"Artwork model {model} failed: {exc}; trying next model if available", flush=True)
                break
    raise RuntimeError(f"Unable to generate artwork: {last_error}")


def fallback_artwork(destination, fallback_paths):
    for source in fallback_paths:
        if source and source.is_file() and source.resolve() != destination.resolve():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            print(f"WARNING: using fallback artwork {source} for {destination}", flush=True)
            return True
    print(f"WARNING: no fallback artwork available for {destination}; continuing without generated art", flush=True)
    return False


def ensure_artwork(path, prompt, fallback_paths=(), force=False):
    if path.is_file() and not force:
        print(f"Using existing artwork {path.relative_to(ROOT)}", flush=True)
        return True
    try:
        generate_image(prompt, path)
        return True
    except Exception as exc:
        print(f"WARNING: artwork generation failed for {path}: {exc}", flush=True)
        return fallback_artwork(path, fallback_paths)


def esc(value):
    return html.escape(str(value), quote=True)


def main():
    base_url = os.environ["PODCAST_BASE_URL"].rstrip("/")
    force = os.environ.get("FORCE_ARTWORK", "").lower() in ("1", "true", "yes")
    assets = ROOT / "assets"
    site = ROOT / "site"
    assets.mkdir(parents=True, exist_ok=True)
    site.mkdir(parents=True, exist_ok=True)
    bible_path = ROOT / "show_bible.json"
    bible = json.loads(bible_path.read_text(encoding="utf-8"))
    cover_path = assets / "cover.png"
    show_cover_prompt = (
        "Square premium illustrated gaming podcast cover, exact large title SNOWY SUNDAY PODCAST and subtitle SUNDAY RADAR. "
        "Two original fictional friendly male hosts: Felix calm and analytical with notebook/headset, Jasper energetic with "
        "headset/controller. Midnight indigo snowy background, clean radar sweep, cyan and orange accents, polished vector-like "
        "art, crisp readable typography, no logos or watermark, safe central composition."
    )
    has_cover = ensure_artwork(cover_path, show_cover_prompt, force=force)
    for episode in bible.get("episodes", []):
        image_name = episode.get("image") or f"episode-{int(episode['number']):03}.png"
        episode["image"] = image_name
        image_path = assets / image_name
        fallback_paths = [cover_path]
        # Reuse prior episode artwork or a checked-in image when the canonical cover is unavailable.
        fallback_paths.extend(sorted(p for p in assets.glob("*") if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp") and p != image_path and p != cover_path))
        prompt = (
            f"Square illustrated podcast thumbnail for {episode.get('title', 'Sunday Podcast')}. "
            f"Show title {bible.get('show_title', 'Sunday Podcast')}. Felix is a calm analytical fictional gaming host; "
            "Jasper is an energetic fictional gaming host. Premium midnight-indigo snowy gaming aesthetic, cyan/orange accents, "
            "clear central composition, original characters, no logos or watermark. Include only short readable title text based on the episode title."
        )
        ensure_artwork(image_path, prompt, fallback_paths=fallback_paths, force=force)
    bible_path.write_text(json.dumps(bible, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    site_assets = site / "assets"
    site_assets.mkdir(parents=True, exist_ok=True)
    if cover_path.is_file():
        shutil.copy2(cover_path, site_assets / "cover.png")
    for episode in bible.get("episodes", []):
        image_path = assets / episode["image"]
        if image_path.is_file():
            shutil.copy2(image_path, site_assets / episode["image"])

    channel = ET.Element("channel")
    ET.SubElement(channel, "title").text = bible.get("show_title", "Sunday Podcast")
    ET.SubElement(channel, "link").text = base_url + "/"
    ET.SubElement(channel, "description").text = "Sunday Podcast with Felix and Jasper"
    if cover_path.is_file():
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
