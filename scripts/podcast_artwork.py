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

from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).resolve().parents[1]
ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ET.register_namespace("itunes", ITUNES)
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 2
ART_SIZE = 3000


def generate_image(prompt, destination):
    if os.environ.get("DISABLE_GEMINI_IMAGE", "").lower() in ("1", "true", "yes"):
        raise RuntimeError("Gemini image generation disabled by DISABLE_GEMINI_IMAGE")
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


def _font(size, bold=False):
    candidates = (
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"]
        if bold else
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"]
    )
    for candidate in candidates:
        if pathlib.Path(candidate).is_file():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def generate_procedural_artwork(destination, title, subtitle=""):
    """Create a polished, square 3000px neon-winter PNG without network access."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (ART_SIZE, ART_SIZE))
    draw = ImageDraw.Draw(image)
    # Deep indigo-to-blue vertical gradient.
    top, bottom = (10, 16, 43), (19, 54, 83)
    for y in range(ART_SIZE):
        t = y / (ART_SIZE - 1)
        color = tuple(round(top[i] * (1 - t) + bottom[i] * t) for i in range(3))
        draw.line((0, y, ART_SIZE, y), fill=color)
    # Subtle radar rings and sweeping accent.
    cx, cy = ART_SIZE // 2, 1120
    for radius, color, width in ((1030, (27, 91, 124), 5), (760, (31, 101, 137), 4), (490, (37, 109, 145), 4)):
        draw.ellipse((cx-radius, cy-radius, cx+radius, cy+radius), outline=color, width=width)
    draw.pieslice((cx-1030, cy-1030, cx+1030, cy+1030), start=205, end=258, fill=(17, 79, 108))
    draw.line((cx, cy, cx+910, cy-480), fill=(70, 225, 231), width=10)
    # Snow dots and geometric sparkles.
    for x, y, r in ((330,370,13),(2670,480,16),(480,1450,10),(2500,1510,12),(670,650,8),(2340,800,9),(850,1730,11),(2150,1770,8)):
        draw.ellipse((x-r,y-r,x+r,y+r), fill=(188,242,247))
    # Framed title panel and accent bars.
    draw.rounded_rectangle((230, 1880, 2770, 2710), radius=100, fill=(11, 23, 50), outline=(64, 196, 214), width=8)
    draw.rounded_rectangle((390, 1990, 420, 2595), radius=15, fill=(255, 155, 93))
    title_font = _font(176, bold=True)
    sub_font = _font(102, bold=True)
    small_font = _font(70)
    display_title = title.upper()
    # Wrap long episode titles to fit the cover panel.
    words, lines, line = display_title.split(), [], ""
    for word in words:
        candidate = (line + " " + word).strip()
        if draw.textbbox((0, 0), candidate, font=title_font)[2] > 2180 and line:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    if len(lines) > 2:
        title_font = _font(140, bold=True)
        lines = [" ".join(words[:3]), " ".join(words[3:])]
    y = 2050
    for text in lines[:2]:
        draw.text((520, y), text, font=title_font, fill=(244, 250, 255), stroke_width=1, stroke_fill=(244, 250, 255))
        y += 205
    if subtitle:
        draw.text((520, 2500, ), subtitle.upper(), font=sub_font, fill=(92, 225, 232))
    draw.text((230, 2800), "FELIX  •  JASPER  •  SNOWY ECOSYSTEM", font=small_font, fill=(184, 214, 231))
    image.save(destination, format="PNG", optimize=True)
    print(f"Generated procedural 3000x3000 artwork at {destination.relative_to(ROOT)}", flush=True)


def ensure_artwork(path, prompt, title, subtitle="", force=False):
    if path.is_file() and not force:
        print(f"Using existing artwork {path.relative_to(ROOT)}", flush=True)
        return True
    try:
        generate_image(prompt, path)
    except Exception as exc:
        print(f"WARNING: artwork generation failed for {path}: {exc}; creating Pillow fallback", flush=True)
        generate_procedural_artwork(path, title, subtitle)
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"Artwork file was not created: {path}")
    return True


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
    ensure_artwork(cover_path, show_cover_prompt, "SNOWY SUNDAY PODCAST", "SUNDAY RADAR", force=force)
    for episode in bible.get("episodes", []):
        image_name = episode.get("image") or f"episode-{int(episode['number']):03}.png"
        episode["image"] = image_name
        image_path = assets / image_name
        prompt = (
            f"Square illustrated podcast thumbnail for {episode.get('title', 'Sunday Podcast')}. "
            f"Show title {bible.get('show_title', 'Sunday Podcast')}. Felix is a calm analytical fictional gaming host; "
            "Jasper is an energetic fictional gaming host. Premium midnight-indigo snowy gaming aesthetic, cyan/orange accents, "
            "clear central composition, original characters, no logos or watermark. Include only short readable title text based on the episode title."
        )
        ensure_artwork(image_path, prompt, f"EPISODE {int(episode['number']):02d}", episode.get("title", "SUNDAY RADAR"), force=force)
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
