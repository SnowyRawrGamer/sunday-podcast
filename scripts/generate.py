#!/usr/bin/env python3
"""Generate and mix a continuity-aware Sunday Podcast episode."""
import argparse
import datetime as dt
import html
import json
import os
import pathlib
import random
import subprocess
import sys
import urllib.request

import numpy as np
import soundfile as sf
from kokoro import KPipeline
from kokoro.model import KModel
from gtts import gTTS

ROOT = pathlib.Path(__file__).resolve().parents[1]
_MODEL = None
_PIPELINES = {}
_PIPELINE_ERRORS = {}


def call_gemini(prompt):
    key = os.environ.get('GEMINI_API_KEY')
    if not key:
        raise RuntimeError('GEMINI_API_KEY secret is required')
    url = 'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=' + key
    payload = {'contents': [{'parts': [{'text': prompt}]}], 'generationConfig': {'responseMimeType': 'application/json', 'temperature': 0.8}}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=90) as response:
        data = json.load(response)
    return data['candidates'][0]['content']['parts'][0]['text']


def kokoro_language(speaker):
    return 'b' if speaker == 'Jasper' else 'a'


def kokoro_voice(speaker):
    return 'bm_george' if speaker == 'Jasper' else 'am_adam'


def synthesize_line(text, speaker, out_path, work):
    """Prefer local Kokoro neural speech; keep gTTS as a last-resort fallback."""
    global _MODEL
    lang = kokoro_language(speaker)
    voice = kokoro_voice(speaker)
    if lang not in _PIPELINE_ERRORS:
        try:
            if lang not in _PIPELINES:
                if _MODEL is None:
                    print('Loading shared Kokoro model on CPU', flush=True)
                    _MODEL = KModel(repo_id='hexgrad/Kokoro-82M').to('cpu').eval()
                print(f'Loading Kokoro CPU pipeline for language {lang}', flush=True)
                _PIPELINES[lang] = KPipeline(lang_code=lang, model=_MODEL, device='cpu')
            chunks = [audio for _, _, audio in _PIPELINES[lang](text, voice=voice) if audio is not None and len(audio)]
            if not chunks:
                raise RuntimeError('Kokoro returned no audio chunks')
            wav_path = work / (out_path.stem + '.wav')
            sf.write(str(wav_path), np.concatenate(chunks), 24000)
            subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(wav_path), '-codec:a', 'libmp3lame', '-b:a', '128k', str(out_path)], check=True)
            print(f'TTS backend=kokoro voice={voice} language={lang}', flush=True)
            return
        except Exception as exc:
            _PIPELINE_ERRORS[lang] = f'{type(exc).__name__}: {exc}'
            print(f'Kokoro failed for {speaker} ({voice}): {_PIPELINE_ERRORS[lang]}; using gTTS fallback', file=sys.stderr, flush=True)
    fallback_tld = 'co.uk' if speaker == 'Jasper' else 'com'
    try:
        gTTS(text=text, lang='en', tld=fallback_tld).save(str(out_path))
        print(f'TTS backend=gTTS fallback speaker={speaker} tld={fallback_tld}', flush=True)
    except Exception as exc:
        raise RuntimeError(f'Kokoro and gTTS both failed for {speaker}: Kokoro={_PIPELINE_ERRORS.get(lang, "unknown")}; gTTS={type(exc).__name__}: {exc}') from exc


def run(*cmd):
    subprocess.run(cmd, check=True)


def make_prompt(bible, topics, test_mode):
    title = topics.get('title', 'Sunday Podcast')
    length = 'a concise 4-6 exchange test, under 90 seconds' if test_mode else 'a natural 8-12 minute episode'
    return f'''Return valid JSON only in this exact shape: {{"title":"...","lines":[{{"speaker":"Felix","text":"..."}},{{"speaker":"Jasper","text":"..."}}],"continuity_update":"..."}}.
The show title is {json.dumps(title)}; keep that title exactly. Write {length}, with a lively back-and-forth and short spoken turns.

Felix and Jasper are two close buddies chatting casually on Discord / on their gaming podcast. They follow Snowy's dev logs, gaming matches, Bone TD, BattleTabs, and projects, and react like real friends: tease each other, hype good plays, roast bad strategies, disagree playfully, and land funny callbacks. Felix is observant and clear; Jasper is energetic and quick with a hot take. Make it sound spontaneous and warm, not like presenters reading a corporate briefing. They are fictional third-party podcast hosts, not Joel or Madden; never claim to be either person or invent personal experiences for them.

STRICT STYLE BAN: Never use corporate, press-release, or generic AI phrases, including these exact phrases or close variants: "the Snowy ecosystem", "today in the ecosystem", "on our radar today", "let's dive in", "in today's episode", "we're excited to announce", "stay tuned", "at the end of the day", "game changer", "let's unpack", "without further ado", "in the ever-evolving world". Do not describe the show as covering an "ecosystem". Start with a natural conversational line, not a formal introduction. No fake sponsor reads, fabricated stats, made-up developer statements, invented match results, personal anecdotes, or claims beyond the supplied weekly topics and show bible. Treat developer-log facts only as confirmed when actually supplied in the topics or bible; otherwise speak generally and say what is unknown rather than bluffing.

Use only the supplied topics and continuity notes. Keep analysis accurate, banter genuinely conversational, and callbacks occasional rather than forced. Hosts: {json.dumps(bible['hosts'], ensure_ascii=False)}. Show bible: {json.dumps(bible, ensure_ascii=False)}. This week's topics: {json.dumps(topics, ensure_ascii=False)}.'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--test', action='store_true')
    args = parser.parse_args()
    bible_path = ROOT / 'show_bible.json'
    bible = json.loads(bible_path.read_text())
    topics = json.loads((ROOT / 'topics.json').read_text())
    date = dt.datetime.now(dt.timezone.utc).date().isoformat()
    number = topics['episode_number'] if topics.get('episode_number') is not None else len(bible.get('episodes', [])) + 1
    data = json.loads(call_gemini(make_prompt(bible, topics, args.test)))
    lines = data.get('lines', [])
    allowed = {'Felix', 'Jasper'}
    if not lines or any(line.get('speaker') not in allowed or not line.get('text') for line in lines):
        raise ValueError('Model returned invalid dialogue; expected non-empty Felix and Jasper lines')
    work = ROOT / 'build'
    work.mkdir(exist_ok=True)
    parts = []
    for index, line in enumerate(lines):
        output = work / f'line-{index:03}.mp3'
        print(f'Synthesizing line {index + 1}/{len(lines)} for {line["speaker"]}', flush=True)
        synthesize_line(line['text'], line['speaker'], output, work)
        parts.append(output)
    concat_list = work / 'concat.txt'
    concat_list.write_text(''.join("file '" + part.name + "'\n" for part in parts))
    speech = work / 'speech.mp3'
    run('ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', str(concat_list), '-c:a', 'libmp3lame', '-b:a', '128k', str(speech))

    music_dir = ROOT / 'music'
    music_files = sorted(music_dir.glob('*.mp3')) if music_dir.exists() else []
    if not music_files:
        run(sys.executable, str(ROOT / 'scripts' / 'generate_music.py'))
        music_files = sorted(music_dir.glob('*.mp3'))
    site = ROOT / 'site'
    site.mkdir(exist_ok=True)
    slug = f'episode-{number:03}-{date}'
    output = site / f'{slug}.mp3'
    if music_files:
        music = random.choice(music_files)
        print(f'Mixing background track: {music.name}', flush=True)
        run('ffmpeg', '-y', '-i', str(speech), '-stream_loop', '-1', '-i', str(music), '-filter_complex', '[1:a]volume=0.18[musicbed];[musicbed][0:a]sidechaincompress=threshold=0.025:ratio=8:attack=20:release=500[ducked];[0:a][ducked]amix=inputs=2:duration=first:dropout_transition=2[out]', '-map', '[out]', '-c:a', 'libmp3lame', '-b:a', '128k', str(output))
    else:
        print('No music tracks found; exporting speech only', file=sys.stderr, flush=True)
        run('ffmpeg', '-y', '-i', str(speech), '-c:a', 'libmp3lame', '-b:a', '128k', str(output))

    title = topics.get('title') or data.get('title', 'Sunday Podcast')
    bible.setdefault('episodes', []).append({'number': number, 'date': date, 'title': title, 'audio': output.name, 'continuity_update': data.get('continuity_update', '')})
    bible_path.write_text(json.dumps(bible, indent=2, ensure_ascii=False) + '\n')
    base_url = os.environ['PODCAST_BASE_URL'].rstrip('/') + '/'
    items = []
    for episode in reversed(bible['episodes']):
        items.append(f'''<item><title>{esc(episode['title'])}</title><guid isPermaLink="false">{esc(episode['audio'])}</guid><pubDate>{dt.datetime.fromisoformat(episode['date']).strftime('%a, %d %b %Y 00:00:00 +0000')}</pubDate><enclosure url="{base_url}{esc(episode['audio'])}" length="0" type="audio/mpeg"/><description>{esc(episode.get('continuity_update', ''))}</description></item>''')
    (site / 'podcast.xml').write_text('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Sunday Podcast</title><link>' + esc(base_url) + '</link><description>Sunday Podcast with Felix and Jasper</description>' + ''.join(items) + '</channel></rss>')
    (site / 'index.html').write_text('<!doctype html><title>Sunday Podcast</title><h1>Sunday Podcast</h1><p><a href="podcast.xml">RSS feed</a></p>' + ''.join(f'<p><a href="{esc(ep["audio"])}">{esc(ep["title"])}</a></p>' for ep in reversed(bible['episodes'])))


def esc(value):
    return html.escape(str(value), quote=True)


if __name__ == '__main__':
    main()
