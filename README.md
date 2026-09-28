# Sunday Podcast

A manually triggered GitHub Actions workflow writes a two-host gaming podcast script with Gemini, synthesizes voices locally with Kokoro on CPU, mixes audio with ffmpeg, and publishes MP3/RSS output to GitHub Pages.

## One-time setup
1. In Settings → Secrets and variables → Actions, add repository secret `GEMINI_API_KEY` (Google AI Studio free-tier quotas and availability depend on Google's current terms).
2. In Settings → Pages, set Build and deployment source to **GitHub Actions**.
3. Run Actions → Sunday Podcast. The workflow generates two original, loopable instrumental MP3s in `music/` if they are missing. The compositions are dedicated to CC0 1.0; see `music/CC0.txt`.
4. Edit `topics.json` for the next episode and keep `show_bible.json` continuity notes accurate.
5. Set `test_mode` true for a short test, or false for a full episode.

The workflow installs ffmpeg, eSpeak NG, and libsndfile, installs requirements, caches Hugging Face model files between runs, then generates audio and publishes `site/`. The first Kokoro run downloads model data and can take longer. GitHub runners and Pages have plan limits and are not guaranteed to be unlimited or cost-free.

## Voices and music
Kokoro runs on CPU without a paid TTS API key. Felix uses `am_adam` (US English) and Jasper uses `bm_george` (British English). Kokoro is required by default: an inference error is logged and stops the run rather than silently publishing a gTTS version. Each successful line logs `TTS_RESULT backend=kokoro`, and the episode summary reports Kokoro versus fallback line counts. To explicitly permit the less-natural gTTS emergency fallback, set the Actions environment variable `ALLOW_GTTS_FALLBACK=true`; that fallback requires network access. For each episode, one of the MP3s in `music/` is randomly selected, looped, and ducked beneath speech with ffmpeg sidechain compression. The music bed was raised to be more audible while retaining speech ducking.

## Output
The workflow commits the generated CC0 music and show-bible update, then uploads `site/` to GitHub Pages. The episode MP3 and `podcast.xml` are served from the repository's Pages site after deployment.
