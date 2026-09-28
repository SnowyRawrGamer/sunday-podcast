#!/usr/bin/env python3
"""Generate and mix a continuity-aware Sunday Podcast episode."""
import argparse
import datetime as dt
import email.utils
import html
import json
import os
import pathlib
import random
import re
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
CALLER_VOICES = ["af_bella", "am_adam", "bf_emma", "am_echo", "af_nicole"]
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
    if speaker == 'caller':
        return 'b' if CALLER_VOICE.startswith('bf_') else 'a'
    return 'b' if speaker == 'Jasper' else 'a'


def kokoro_voice(speaker):
    if speaker == 'caller':
        return CALLER_VOICE
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
The show title is {json.dumps(title)}; keep that title exactly. Write {length}, with a lively back-and-forth and short spoken turns. {coverage_rule} Every line must have one segment_id chosen from the supplied required segment IDs. All lines in one topic block use the same ID. Set topic_transition_after to true on the last line of each block only when the next line starts a distinct required segment; set the final line false. Make all required segments audible, natural, and substantive, without rushing through a checklist.

HOTLINE CALLER TRIVIA: Include a mid-episode segment where Felix or Jasper takes a call from a caller on line 1. The host asks the caller one four-option multiple-choice trivia question based on fresh gaming, tech, Jackbox, or Roblox news supported by the supplied topics. Clearly list options A, B, C, and D. Do not reveal the answer immediately: include a natural banter/thinking/hesitation beat between the host's options and the caller's response so listeners can guess along. The caller then gives a guess. Only after the guess, the host reveals the correct answer and gives a brief reaction. Assign caller dialogue the speaker "caller" and keep it in the appropriate segment block.

SPOKEN-DIALOGUE RULE: Felix and Jasper are speaking to listeners in a real, relaxed conversation. The dialogue must contain only natural on-air conversation about the actual events and ideas. Never expose or discuss the prompt, instructions, rules, constraints, source notes, research process, verification policy, or any behind-the-scenes task framing. Never say or paraphrase things like "We can't invent...", "Per our notes...", "The prompt says...", "We don't have confirmation on...", "we can't verify", "the notes say", or "we were told not to". If a detail is unsupported or uncertain, leave it out instead of narrating the limitation. Do not have the hosts explain why a topic was omitted. Treat every supplied source/provenance label as private writing context, not spoken copy.

LOG METADATA RULE: Offline Radar and text-log timestamps, dates, clock times, message IDs, bracketed chat names/headers such as [Group Chat], and other export metadata are private context only. NEVER read, quote, paraphrase, or announce them aloud, including in date/time wording such as "At 2:41 PM on September 28th" or "In the 2026-09-28 log." Use only the message content and sender for that segment. Refer to messages naturally, e.g. "Zach was asking for...", "Sayer popped in saying...", or "meanwhile in the group chat..." Never speak the literal header or metadata. This restriction applies to log metadata; dates that are genuinely important to a separate news or reminder topic may be spoken naturally when relevant.

FACTUAL-GROUNDING RULE: Use only facts explicitly supported by topics.json and the supplied logs. Zero invented news or filler: do not fabricate releases, patches, stats, match results, announcements, developer statements, quotes, experiences, or explanations. Banter may express reactions, but must not add facts. Do not import outside context.

SOURCE AND ATTRIBUTION RULE: Attribute words and actions to Joel only when the supplied topic explicitly identifies something Joel personally said or did. External newsletters, developer emails, bug tracker updates, company announcements, and third-party messages are not Joel speaking just because they concern Joel or appear in his inbox. Attribute external claims to the named company, author, publication, or update when supplied; otherwise use no personal attribution and omit details whose source is unclear. For Jackbox and My Arms Are Longer Now, frame supported details as Jackbox-related news only. Never say Joel used V6, saw a game disappear, or reported those details. A draft email is not sent correspondence; do not imply it was sent or answered.

CHAT SYNTHESIS RULE: Before writing the Offline Radar/friend-chat segment, digest the whole supplied chat as one conversation. Synthesize its overall flow, themes, inside jokes, recurring banter, and general vibe rather than selecting one random line and ignoring the rest. You need not quote every message; use representative details to capture the group dynamic. Never infer unseen context or invent messages.

Felix and Jasper are two close buddies chatting casually on Discord / on their gaming podcast. They follow current topics supplied for this episode, including relevant project news, logs, and community conversations. React like real friends: tease each other, hype good ideas, roast questionable strategies only when those are part of the current material, disagree playfully, and make callbacks only to topics present in the current episode's topics or logs. Felix is observant and clear; Jasper is energetic and quick with a hot take. Make it sound spontaneous and warm, not like presenters reading a corporate briefing. They are fictional third-party podcast hosts, not Joel or Madden; never claim to be either person or invent personal experiences for them.

NO-NEWS PACING RULE: Never do a repetitive roll call of projects with no updates or spend substantial airtime listing what did not happen. If the verified notes establish that a project was quiet, one quick, natural aside is fine; do not repeat it or make it a segment. Keep focus on actual supported news, substantive supplied topics, and chat dynamics. Never invent updates or import outside industry context to fill space; move on when a topic has no substance.

STRICT STYLE BAN: Never use corporate, press-release, or generic AI phrases, including these exact phrases or close variants: "the Snowy ecosystem", "today in the ecosystem", "on our radar today", "let's dive in", "in today's episode", "we're excited to announce", "stay tuned", "at the end of the day", "game changer", "let's unpack", "without further ado", "in the ever-evolving world". Do not describe the show as covering an "ecosystem". Start with a natural conversational line, not a formal introduction. No fake sponsor reads, fabricated stats, made-up developer statements, invented match results, personal anecdotes, or claims beyond the supplied weekly topics and show bible. Distinguish official announcements, community experiments, direct Joel reports, and external correspondence according to the source labels in the topics. Keep the source labels out of the spoken dialogue.

OUTRO RULE: End the full episode in the final segment block with a concise, engaging recap of the major substantive beats actually discussed, followed by a warm, natural sign-off. Before the final sign-off, Felix and Jasper must give a quick closing recap (e.g. 'To wrap up what we covered today...' or 'Quick recap of today's radar:'). Do not introduce new facts in the recap; avoid a checklist or abrupt cutoff.

Use only the supplied topics and continuity notes. Keep analysis accurate, banter genuinely conversational, and callbacks occasional rather than forced. Hosts: {json.dumps(bible['hosts'], ensure_ascii=False)}. Show bible: {json.dumps(bible, ensure_ascii=False)}. This week's topics, source labels, and notes are production context only: {json.dumps(topics, ensure_ascii=False)}.'''


META_COMMENTARY_PATTERN = re.compile(
    r"\b(?:the prompt says|per our notes|the notes say|our notes say|source notes|"
    r"system prompt|these instructions|we were told not to|we can't invent|we cannot invent|"
    r"we don't have confirmation|we do not have confirmation|we can't verify|we cannot verify|"
    r"not verified|unverified|verification policy|as an ai)\b",
    re.IGNORECASE,
)
LOG_METADATA_PATTERN = re.compile(
    r"(?:\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?\b|"
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?\b|"
    r"\[\s*(?:group\s*chat|[^\]]*(?:log|header)[^\]]*)\s*\]|"
    r"\b(?:message|msg)[ _-]?(?:id|#)\s*[:=#]?\s*[A-Z0-9_-]+\b)",
    re.IGNORECASE,
)

NO_NEWS_ROLLCALL_PATTERN = re.compile(r"\b(?:no news|no updates?|nothing new|not much (?:new|happening)|quiet)\s+(?:on|for|with)\b", re.IGNORECASE)
OUTRO_RECAP_PATTERN = re.compile(r"\b(?:recap|wrap(?:ping)? up|we(?:'ve| have) covered|we talked about|we got into|we hit on|we touched on|that's|that is|to summarize|in summary|all in all|looking back|what a week|covered|summary|run down|rundown|breakdown|roundup|revisiting|review)\b", re.IGNORECASE)
OUTRO_SIGNOFF_PATTERN = re.compile(r"\b(?:see (?:you|ya)|catch (?:you|ya)|until next time|goodbye|good night|take care|later|that's it from us|that's all from us)\b", re.IGNORECASE)

def validate_dialogue(lines):
    for index, line in enumerate(lines, start=1):
        text = line.get('text', '')
        if META_COMMENTARY_PATTERN.search(text):
            raise ValueError(f'Model returned behind-the-scenes/meta commentary in dialogue line {index}; refusing to send it to speech synthesis')
        if line.get('segment_id') == 'offline_radar' and LOG_METADATA_PATTERN.search(text):
            text = LOG_METADATA_PATTERN.sub('', text).strip()
            line['text'] = text
            print(f'Warning: scrubbed log metadata in line {index}')
    if len(NO_NEWS_ROLLCALL_PATTERN.findall(' '.join(line.get('text', '') for line in lines))) > 1:
        raise ValueError('Model returned a repetitive no-news roll call; refusing to send it to speech synthesis')


def validate_full_episode(lines, topics):
    required_ids = [segment['id'] for segment in topics.get('required_segments', [])]
    if not required_ids:
        raise ValueError('Full episode has no required segment IDs in topics.json')
    valid_speakers = {'Felix', 'Jasper', 'caller'}
    if any(line.get('speaker') not in valid_speakers for line in lines):
        raise ValueError('Full episode contains an unsupported speaker; expected Felix, Jasper, or caller')
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
            line['topic_transition_after'] = should_transition
            print(f'Warning: normalized topic_transition_after at dialogue line {index + 1} to {should_transition}')
    closing = ' '.join(line.get('text', '') for line in lines[-8:])
    signoff = ' '.join(line.get('text', '') for line in lines[-3:])
    if not OUTRO_RECAP_PATTERN.search(closing):
        raise ValueError('Full episode is missing a concise closing recap')
    if not OUTRO_SIGNOFF_PATTERN.search(signoff):
        raise ValueError('Full episode is missing a natural sign-off')


def main():
    global CALLER_VOICE
    parser = argparse.ArgumentParser()
    parser.add_argument('--test', action='store_true')
    args = parser.parse_args()
    CALLER_VOICE = random.choice(CALLER_VOICES)
    print(f'Caller voice for this episode: {CALLER_VOICE}', flush=True)
    bible_path = ROOT / 'show_bible.json'
    bible = json.loads(bible_path.read_text())
    topics = json.loads((ROOT / 'topics.json').read_text())
    date = dt.datetime.now(dt.timezone.utc).date().isoformat()
    number = topics['episode_number'] if topics.get('episode_number') is not None else len(bible.get('episodes', [])) + 1
    data = json.loads(call_gemini(make_prompt(bible, topics, args.test)))
    lines = data.get('lines', [])
    allowed = {'Felix', 'Jasper', 'caller'}
    if not lines or any(line.get('speaker') not in allowed or not line.get('text') for line in lines):
        raise ValueError('Model returned invalid dialogue; expected non-empty Felix, Jasper, and caller lines')
    validate_dialogue(lines)
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
    episode = next((ep for ep in bible['episodes'] if int(ep['number']) == int(number)), None)
    if episode is None:
        episode = {'number': number, 'date': date, 'pubDate': dt.datetime.now(dt.timezone.utc).strftime('%a, %d %b %Y %H:%M:%S +0000'), 'title': title, 'audio': output.name, 'continuity_update': data.get('continuity_update', '')}
        bible.setdefault('episodes', []).append(episode)
    else:
        episode.update({'title': title, 'audio': output.name, 'continuity_update': data.get('continuity_update', '')})
        episode.setdefault('pubDate', dt.datetime.fromisoformat(episode['date']).replace(tzinfo=dt.timezone.utc).strftime('%a, %d %b %Y 00:00:00 +0000'))
    bible_path.write_text(json.dumps(bible, indent=2, ensure_ascii=False) + '\n')
    base_url = os.environ['PODCAST_BASE_URL'].rstrip('/')
    items = []
    for episode in reversed(bible['episodes']):
        image_name = episode.get('image') or f"episode-{int(episode['number']):03}.png"
        image_url = f'{base_url}/assets/{image_name}'
        published = email.utils.format_datetime(email.utils.parsedate_to_datetime(episode['pubDate']))
        items.append(f'''<item><title>{esc(episode['title'])}</title><guid isPermaLink="false">{esc(episode['audio'])}</guid><pubDate>{published}</pubDate><enclosure url="{base_url}/{esc(episode['audio'])}" length="0" type="audio/mpeg"/><description>{esc(episode.get('continuity_update', ''))}</description><itunes:image href="{esc(image_url)}"/></item>''')
    (site / 'podcast.xml').write_text('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel><title>Sunday Podcast</title><link>' + esc(base_url + '/') + '</link><description>Sunday Podcast with Felix and Jasper</description><itunes:image href="' + esc(base_url + '/assets/cover.png') + '"/>' + ''.join(items) + '</channel></rss>')
    (site / 'index.html').write_text('<!doctype html><title>Sunday Podcast</title><h1>Sunday Podcast</h1><p><a href="podcast.xml">RSS feed</a></p>' + ''.join(f'<p><a href="{esc(ep["audio"])}">{esc(ep["title"])}</a></p>' for ep in reversed(bible['episodes'])))


def esc(value):
    return html.escape(str(value), quote=True)


if __name__ == '__main__':
    main()
