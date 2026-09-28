#!/usr/bin/env python3
"""Generate and mix a continuity-aware Sunday Podcast episode."""
import argparse, asyncio, datetime as dt, json, os, pathlib, subprocess, sys, urllib.request
import edge_tts
ROOT = pathlib.Path(__file__).resolve().parents[1]


def call_gemini(prompt):
    key = os.environ.get('GEMINI_API_KEY')
    if not key:
        raise RuntimeError('GEMINI_API_KEY secret is required')
    url = 'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=' + key
    payload = {'contents': [{'parts': [{'text': prompt}]}], 'generationConfig': {'responseMimeType': 'application/json', 'temperature': 0.8}}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=90) as r:
        data = json.load(r)
    return data['candidates'][0]['content']['parts'][0]['text']


async def synthesize_line(text, voice, out_path):
    """Synthesize one line with retries and report the actual edge-tts exception."""
    for attempt in range(1, 4):
        try:
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(str(out_path))
            return
        except Exception as exc:
            print(f'edge-tts failed for voice {voice!r}, attempt {attempt}/3: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
            if attempt == 3:
                raise RuntimeError(f'edge-tts synthesis failed after 3 attempts for voice {voice!r}: {type(exc).__name__}: {exc}') from exc
            await asyncio.sleep(attempt * 2)


def run(*cmd):
    subprocess.run(cmd, check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--test', action='store_true')
    args = ap.parse_args()
    bible_path = ROOT / 'show_bible.json'
    bible = json.loads(bible_path.read_text())
    topics = json.loads((ROOT / 'topics.json').read_text())
    date = dt.datetime.now(dt.timezone.utc).date().isoformat()
    n = topics['episode_number'] if topics.get('episode_number') is not None else len(bible.get('episodes', [])) + 1
    prompt = f'''Write a natural, entertaining two-host gaming podcast dialogue in JSON only, format {{"title":"...","lines":[{{"speaker":"Felix","text":"..."}},{{"speaker":"Jasper","text":"..."}}],"continuity_update":"..."}}. The episode title is {json.dumps(topics.get('title', 'Sunday Podcast'))}; preserve it exactly. Make 12-20 short exchanges; {'make this a brief 4-6 exchange test, under 90 seconds' if args.test else 'target about 8-12 minutes when spoken'}. Hosts and personas: {json.dumps(bible['hosts'], ensure_ascii=False)}. Show bible: {json.dumps(bible, ensure_ascii=False)}. This week's title/topics: {json.dumps(topics, ensure_ascii=False)}. Felix and Jasper are fictional, independent third-party hosts covering the Snowy ecosystem, not Joel or Madden. Do not claim to be Joel or Madden. Do not invent facts, stats, news, dev-log claims, or personal stories; use only supplied topics. Felix is analytical and measured; Jasper is energetic and questions strategies. Use callbacks sparingly and maintain continuity.'''
    data = json.loads(call_gemini(prompt))
    lines = data['lines']
    allowed = {'Felix', 'Jasper'}
    if not lines or any(x.get('speaker') not in allowed or not x.get('text') for x in lines):
        raise ValueError('Model returned invalid dialogue; expected Felix and Jasper lines')
    work = ROOT / 'build'
    work.mkdir(exist_ok=True)
    parts = []
    voices = {'Felix': os.getenv('FELIX_VOICE', 'en-US-ChristopherNeural'), 'Jasper': os.getenv('JASPER_VOICE', 'en-US-EricNeural')}
    for i, line in enumerate(lines):
        mp3 = work / f'line-{i:03}.mp3'
        print(f'Synthesizing line {i + 1}/{len(lines)} with {voices[line["speaker"]]}', flush=True)
        asyncio.run(synthesize_line(line['text'], voices[line['speaker']], mp3))
        parts.append(mp3)
    listing = work / 'concat.txt'
    listing.write_text(''.join("file '" + p.name + "'\n" for p in parts))
    speech = work / 'speech.mp3'
    run('ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', str(listing), '-c:a', 'libmp3lame', '-b:a', '128k', str(speech))
    site = ROOT / 'site'
    site.mkdir(exist_ok=True)
    slug = f'episode-{n:03}-{date}'
    out = site / f'{slug}.mp3'
    music = ROOT / 'music' / 'lofi.mp3'
    if music.exists():
        run('ffmpeg', '-y', '-i', str(speech), '-stream_loop', '-1', '-i', str(music), '-filter_complex', '[1:a]volume=0.16[bed];[bed][0:a]sidechaincompress=threshold=0.025:ratio=8:attack=20:release=500[ducked];[0:a][ducked]amix=inputs=2:duration=first:dropout_transition=2[out]', '-map', '[out]', '-c:a', 'libmp3lame', '-b:a', '128k', str(out))
    else:
        run('ffmpeg', '-y', '-i', str(speech), '-c:a', 'libmp3lame', '-b:a', '128k', str(out))
    title = topics.get('title') or data.get('title', 'Sunday Podcast')
    bible.setdefault('episodes', []).append({'number': n, 'date': date, 'title': title, 'audio': out.name, 'continuity_update': data.get('continuity_update', '')})
    bible_path.write_text(json.dumps(bible, indent=2, ensure_ascii=False) + '\n')
    base = os.environ['PODCAST_BASE_URL'].rstrip('/') + '/'
    items = []
    for ep in reversed(bible['episodes']):
        items.append(f'''<item><title>{esc(ep['title'])}</title><guid isPermaLink="false">{esc(ep['audio'])}</guid><pubDate>{dt.datetime.fromisoformat(ep['date']).strftime('%a, %d %b %Y 00:00:00 +0000')}</pubDate><enclosure url="{base}{esc(ep['audio'])}" length="0" type="audio/mpeg"/><description>{esc(ep.get('continuity_update', ''))}</description></item>''')
    (site / 'podcast.xml').write_text('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Sunday Podcast</title><link>' + esc(base) + '</link><description>Independent Snowy ecosystem gaming podcast</description>' + ''.join(items) + '</channel></rss>')
    (site / 'index.html').write_text('<!doctype html><title>Sunday Podcast</title><h1>Sunday Podcast</h1><p><a href="podcast.xml">RSS feed</a></p>' + ''.join(f'<p><a href="{esc(ep["audio"])}">{esc(ep["title"])}</a></p>' for ep in reversed(bible['episodes'])))


def esc(s):
    import html
    return html.escape(str(s), quote=True)


if __name__ == '__main__':
    main()
