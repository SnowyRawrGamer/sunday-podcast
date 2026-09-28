#!/usr/bin/env python3
"""Create two original, loopable lo-fi instrumentals and encode them as MP3."""
import math
import random
import struct
import subprocess
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RATE = 22050
BARS = 4
PROGRESSIONS = [
    [(130.81, 164.81, 196.00, 246.94), (110.00, 130.81, 164.81, 196.00), (87.31, 110.00, 130.81, 164.81), (98.00, 123.47, 146.83, 196.00)],
    [(146.83, 174.61, 220.00, 261.63), (116.54, 146.83, 174.61, 220.00), (98.00, 123.47, 146.83, 185.00), (110.00, 138.59, 164.81, 220.00)],
]


def make_loop(path, index):
    bpm = (76, 82)[index]
    beat_len = RATE * 60.0 / bpm
    bar_len = beat_len * 4
    total = int(bar_len * BARS)
    progression = PROGRESSIONS[index]
    rng = random.Random(410 + index)
    notes = []
    for bar, chord in enumerate(progression):
        for beat in range(4):
            start = int((bar * 4 + beat) * beat_len)
            notes.append((start, chord[beat % len(chord)] * (2 if beat in (1, 3) else 1), 0.11 if beat in (1, 3) else 0.15))
    kicks = [int((bar * 4 + beat) * beat_len) for bar in range(BARS) for beat in (0, 2)]
    snares = [int((bar * 4 + beat) * beat_len) for bar in range(BARS) for beat in (1, 3)]
    hats = [int((step + 0.5) * beat_len) for step in range(BARS * 4)]
    noise = [rng.uniform(-1.0, 1.0) for _ in range(int(RATE * 0.20))]
    pcm = bytearray()
    for i in range(total):
        t = i / RATE
        bar = min(int(i / bar_len), BARS - 1)
        chord = progression[bar]
        # Warm, soft chord pad with mellow upper harmonics.
        pad = sum(math.sin(2 * math.pi * f * t) + 0.22 * math.sin(4 * math.pi * f * t) for f in chord) * 0.026
        # Rounded bass pulse on each beat.
        beat_phase = (i % int(beat_len)) / beat_len
        bass_f = chord[0] / 2
        bass = math.sin(2 * math.pi * bass_f * t) * 0.075 * math.exp(-beat_phase * 3.2)
        drum = 0.0
        for start in kicks:
            age = i - start
            if 0 <= age < int(RATE * 0.22):
                x = age / RATE
                drum += 0.24 * math.sin(2 * math.pi * (56 - 25 * x) * x) * math.exp(-20 * x)
        for start in snares:
            age = i - start
            if 0 <= age < int(RATE * 0.16):
                x = age / RATE
                ni = min(int(age * len(noise) / (RATE * 0.20)), len(noise) - 1)
                drum += 0.10 * noise[ni] * math.exp(-22 * x) + 0.035 * math.sin(2 * math.pi * 180 * x) * math.exp(-18 * x)
        for start in hats:
            age = i - start
            if 0 <= age < int(RATE * 0.045):
                x = age / RATE
                ni = min(int(age * len(noise) / (RATE * 0.20)), len(noise) - 1)
                drum += 0.027 * noise[ni] * math.exp(-75 * x)
        # Sparse plucked melody, one note per other beat.
        melody = 0.0
        for start, freq, amp in notes:
            age = i - start
            if 0 <= age < int(RATE * 0.30):
                x = age / RATE
                melody += amp * math.sin(2 * math.pi * freq * x) * math.exp(-8 * x)
        sample = max(-0.88, min(0.88, (pad + bass + drum + melody) * 0.68))
        value = int(sample * 32767)
        pcm.extend(struct.pack('<hh', value, value))
    with wave.open(str(path), 'wb') as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(pcm)


def main():
    music = ROOT / 'music'
    work = ROOT / 'build' / 'music'
    music.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    for i in range(2):
        wav_path = work / f'lofi_{i + 1}.wav'
        mp3_path = music / f'lofi_{i + 1}.mp3'
        if mp3_path.exists() and mp3_path.stat().st_size > 1000:
            continue
        make_loop(wav_path, i)
        subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(wav_path), '-codec:a', 'libmp3lame', '-b:a', '96k', str(mp3_path)], check=True)
        print(f'Created {mp3_path.relative_to(ROOT)}')


if __name__ == '__main__':
    main()
