#!/usr/bin/env python3
"""Generate and mix a continuity-aware Sunday Podcast episode."""
import argparse
import datetime as dt
import html
import json
import os
import pathlib
import random
import socket
import subprocess
import sys
import time
import urllib.error
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
_TTS_BACKENDS = {'kokoro': 0, 'gtts_fallback': 0}


def _is_retryable_network_error(exc):
    if isinstance(exc, urllib.error.URLError):
        return isinstance(exc.reason, (OSError, TimeoutError))
    return isinstance(exc, (socket.timeout, TimeoutError, OSError))


def _request_gemini_model(model, payload, key):
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}'
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'}, method='POST')
    retry_delays = (5, 10, 20, 40, 60)
    for attempt in range(len(retry_delays) + 1):
        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                data = json.load(response)
            return data['candidates'][0]['content']['parts'][0]['text']
        except urllib.error.HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt >= len(retry_delays):
                raise
            delay = retry_delays[attempt]
            print(f'Gemini model {model} returned HTTP {exc.code}; retrying in {delay}s (retry {attempt + 1}/{len(retry_delays)}).', file=sys.stderr, flush=True)
            time.sleep(delay)
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            if not _is_retryable_network_error(exc) or attempt >= len(retry_delays):
                raise
            delay = retry_delays[attempt]
            print(f'Gemini model {model} connection/timeout error ({exc}); retrying in {delay}s (retry {attempt + 1}/{len(retry_delays)}).', file=sys.stderr, flush=True)
            time.sleep(delay)


def call_gemini(prompt):
    key = os.environ.get('GEMINI_API_KEY')
    if not key:
        raise RuntimeError('GEMINI_API_KEY secret is required')
    payload = {'contents': [{'parts': [{'text': prompt}]}], 'generationConfig': {'responseMimeType': 'application/json', 'temperature': 0.8}}
    models = ('gemini-2.5-flash', 'gemini-2.5-flash-lite')
    for index, model in enumerate(models):
        try:
            result = _request_gemini_model(model, payload, key)
            print(f'Gemini generation succeeded with model {model}.', flush=True)
            return result
        except (urllib.error.HTTPError, urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            if isinstance(exc, urllib.error.HTTPError):
                can_fallback = exc.code in {404, 410, 429, 500, 502, 503, 504}
            else:
                can_fallback = _is_retryable_network_error(exc)
            if index == len(models) - 1 or not can_fallback:
                raise
            print(f'Gemini model {model} remained unavailable; trying fallback model {models[index + 1]}.', file=sys.stderr, flush=True)
    raise RuntimeError('Gemini generation failed for all configured models')


def kokoro_language(speaker):
    return 'b' if speaker == 'Jasper' else 'a'


def kokoro_voice(speaker):
    return 'bm_fable' if speaker == 'Jasper' else 'am_michael'


def kokoro_speed(speaker):
    return 1.05 if speaker == 'Jasper' else 0.98


def synthesize_line(text, speaker, out_path, work, pause_seconds=0.0):
    """Use local Kokoro by default; add natural pacing and allow only explicit gTTS fallback."""
    global _MODEL
    lang = kokoro_language(speaker)
    voice = kokoro_voice(speaker)
    speed = kokoro_speed(speaker)
    if lang not in _PIPELINE_ERRORS:
        try:
            if lang not in _PIPELINES:
                if _MODEL is None:
                    print('Loading shared Kokoro model on CPU (hexgrad/Kokoro-82M)', flush=True)
                    _MODEL = KModel(repo_id='hexgrad/Kokoro-82M').to('cpu').eval()
                print(f'Loading Kokoro CPU pipeline: language={lang}, voice={voice}', flush=True)
                _PIPELINES[lang] = KPipeline(lang_code=lang, model=_MODEL, device='cpu')
            chunks = [audio for _, _, audio in _PIPELINES[lang](text, voice=voice, speed=speed) if audio is not None and len(audio)]
            if not chunks:
                raise RuntimeError('Kokoro returned no audio chunks')
            audio = np.concatenate(chunks)
            if pause_seconds > 0:
                silence = np.zeros(round(24000 * pause_seconds), dtype=audio.dtype)
                audio = np.concatenate((audio, silence))
            wav_path = work / (out_path.stem + '.wav')
            sf.write(str(wav_path), audio, 24000)
            subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(wav_path), '-codec:a', 'libmp3lame', '-b:a', '128k', str(out_path)], check=True)
            _TTS_BACKENDS['kokoro'] += 1
            print(f'TTS_RESULT backend=kokoro speaker={speaker} voice={voice} language={lang} speed={speed} pause_after={pause_seconds:.2f}s', flush=True)
            return
        except Exception as exc:
            _PIPELINE_ERRORS[lang] = f'{type(exc).__name__}: {exc}'
            print(f'KOKORO_ERROR speaker={speaker} voice={voice} language={lang}: {_PIPELINE_ERRORS[lang]}', file=sys.stderr, flush=True)
    else:
        print(f'KOKORO_UNAVAILABLE language={lang}: {_PIPELINE_ERRORS[lang]}', file=sys.stderr, flush=True)

    if os.getenv('ALLOW_GTTS_FALLBACK', '').strip().lower() not in {'1', 'true', 'yes'}:
        raise RuntimeError(f'Kokoro failed for {speaker}; gTTS fallback is disabled. Set ALLOW_GTTS_FALLBACK=true only if a less natural emergency voice is acceptable. Error: {_PIPELINE_ERRORS.get(lang, "unknown Kokoro error")}')

    fallback_tld = 'co.uk' if speaker == 'Jasper' else 'com'
    gTTS(text=text, lang='en', tld=fallback_tld).save(str(out_path))
    filters = [f'atempo={speed}']
    if pause_seconds > 0:
        filters.append(f'apad=pad_dur={pause_seconds}')
    processed_path = out_path.with_name(out_path.stem + '-processed.mp3')
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(out_path), '-af', ','.join(filters), '-codec:a', 'libmp3lame', '-b:a', '128k', str(processed_path)], check=True)
    os.replace(processed_path, out_path)
    _TTS_BACKENDS['gtts_fallback'] += 1
    print(f'TTS_RESULT backend=gTTS_fallback speaker={speaker} tld={fallback_tld} speed={speed} pause_after={pause_seconds:.2f}s', flush=True)


def run(*cmd):
    subprocess.run(cmd, check=True)


def make_prompt(bible, topics, test_mode):
    title = topics.get('title', 'Sunday Podcast')
    if test_mode:
        length = 'a concise 4-6 exchange test episode under 90 seconds'
        coverage_rule = 'This is only a short test, so a subset of segments may be sampled.'
    else:
        length = 'a natural full 8-12 minute episode, approximately 1,050-1,500 spoken words'
        required_ids = [segment['id'] for segment in topics.get('required_segments', [])]
        coverage_rule = (
            'The full episode must cover every required segment exactly in this order: '
            + ', '.join(required_ids)
            + '. Give each segment its own contiguous discussion block, do not skip or merge blocks, '
            'and include concrete supplied details rather than merely naming topics.'
        )
    return f'''Return valid JSON only in this exact shape: {{"title":"...","lines":[{{"speaker":"Felix","text":"...","segment_id":"opening_callback","topic_transition_after":false}},{{"speaker":"Jasper","text":"...","segment_id":"opening_callback","topic_transition_after":false}}],"continuity_update":"..."}}.
The show title is {json.dumps(title)}; keep that title exactly. Write {length}, with a lively back-and-forth and short spoken turns. {coverage_rule} Every line must have one segment_id chosen from the supplied required segment IDs. All lines in one topic block use the same ID. Set topic_transition_after to true on the last line of each block only when the next line starts a distinct required segment; set the final line false. Make all required segments audible, natural, and substantive, without rushing through a checklist. The offline messages must be read verbatim, with their supplied sender attribution; do not interpret fragmentary messages.

Felix and Jasper are two close buddies chatting casually on Discord / on their gaming podcast. They follow Snowy's dev logs, gaming matches, Bone TD, BattleTabs, and projects, and react like real friends: tease each other, hype good plays, roast bad strategies, disagree playfully, and land funny callbacks. Felix is observant and clear; Jasper is energetic and quick with a hot take. Make it sound spontaneous and warm, not like presenters reading a corporate briefing. They are fictional third-party podcast hosts, not Joel or Madden; never claim to be either person or invent personal experiences for them.

STRICT STYLE BAN: Never use corporate, press-release, or generic AI phrases, including these exact phrases or close variants: "the Snowy ecosystem", "today in the ecosystem", "on our radar today", "let's dive in", "in today's episode", "we're excited to announce", "stay tuned", "at the end of the day", "game changer", "let's unpack", "without further ado", "in the ever-evolving world". Do not describe the show as covering an "ecosystem". Start with a natural conversational line, not a formal introduction. No fake sponsor reads, fabricated stats, made-up developer statements, invented match results, personal anecdotes, or claims beyond the supplied weekly topics and show bible. Treat developer-log facts only as confirmed when actually supplied in the topics or bible; distinguish official announcements, community experiments, and Joel's personal reports. When research notes say something was not verified, say that plainly or skip the unsupported claim; never bluff.

Use only the supplied topics and continuity notes. Keep analysis accurate, banter genuinely conversational, and callbacks occasional rather than forced. Hosts: {json.dumps(bible['hosts'], ensure_ascii=False)}. Show bible: {json.dumps(bible, ensure_ascii=False)}. This week's topics and source notes: {json.dumps(topics, ensure_ascii=False)}.'''


def validate_full_episode(lines, topics):
    required_ids = [segment['id'] for segment in topics.get('required_segments', [])]
    if not required_ids:
        raise ValueError('Full episode has no required segment IDs in topics.json')
    line_ids = []
    for line in lines:
        segment_id = line.get('segment_id')
        if segment_id not in required_ids:
            raise ValueError(f'Full episode contains unknown or missing segment_id: {segment_id!r}')
        line_ids.append(segment_id)
    block_order = []
    for segment_id in line_ids:
        if not block_order or block_order[-1] != segment_id:
            block_order.append(segment_id)
    if block_order != required_ids:
        raise ValueError(f'Full episode segment coverage/order mismatch: expected {required_ids}, got {block_order}')
    for index, line in enumerate(lines):
        should_transition = index < len(lines) - 1 and line_ids[index] != line_ids[index + 1]
        if line.get('topic_transition_after') is not should_transition:
            raise ValueError(f'Incorrect topic_transition_after at dialogue line {index + 1}')


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
    if not args.test:
        validate_full_episode(lines, topics)
    work = ROOT / 'build'
    work.mkdir(exist_ok=True)
    parts = []
    for index, line in enumerate(lines):
        output = work / f'line-{index:03}.mp3'
        is_last_line = index == len(lines) - 1
        pause_seconds = 0.0 if is_last_line else (0.5 if line.get('topic_transition_after') is True else 0.3)
        print(f'Synthesizing line {index + 1}/{len(lines)} for {line["speaker"]}; pause_after={pause_seconds:.2f}s', flush=True)
        synthesize_line(line['text'], line['speaker'], output, work, pause_seconds)
        parts.append(output)
    print(f'TTS_SUMMARY kokoro_lines={_TTS_BACKENDS["kokoro"]} gtts_fallback_lines={_TTS_BACKENDS["gtts_fallback"]}', flush=True)
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
        run('ffmpeg', '-y', '-i', str(speech), '-stream_loop', '-1', '-i', str(music), '-filter_complex', '[1:a]volume=0.30[musicbed];[musicbed][0:a]sidechaincompress=threshold=0.04:ratio=6:attack=20:release=500[ducked];[0:a][ducked]amix=inputs=2:duration=first:dropout_transition=2[out]', '-map', '[out]', '-c:a', 'libmp3lame', '-b:a', '128k', str(output))
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
