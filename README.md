# Sunday Podcast

Free-to-run GitHub Actions pipeline that uses Gemini's free-tier API for a two-host script, edge-tts for speech, ffmpeg for mixing, and GitHub Pages for MP3/RSS hosting.

## One-time setup
1. In Settings → Secrets and variables → Actions, add repository secret `GEMINI_API_KEY` (get a key from Google AI Studio; free-tier availability/quotas depend on Google's current terms). No paid API is intentionally used by this workflow.
2. In Settings → Pages, set Build and deployment source to **GitHub Actions**.
3. Add a properly licensed royalty-free instrumental MP3 at `music/lofi.mp3`. The workflow works without it, but then has no background bed. You are responsible for verifying music rights; no track is bundled.
4. Edit `topics.json` for the next episode. Update `show_bible.json` with lore/personas and use episode history as canonical continuity.
5. Actions → Sunday Podcast → Run workflow. Set `test_mode` to true for a short test. The default is a short sample episode; add weekly topics first for a real show.

The workflow_dispatch run generates audio, appends the episode and continuity notes to the show bible, then publishes `site/` to Pages. The episode MP3 and `podcast.xml` are available at `https://<owner>.github.io/sunday-podcast/` once Pages is enabled. First-time Pages setup may require repository admin action. GitHub-hosted runners and Pages have free-plan limits and are not a guarantee of unlimited free use.

## Voices and output
Configure the two neural voice names using `HOST_VOICE` / `COHOST_VOICE` repository variables or workflow environment defaults. The script expects `en-US-GuyNeural` and `en-US-JennyNeural`. Audio is synthesized per line, joined and optionally mixed with `music/lofi.mp3` using ffmpeg sidechain compression (ducking). `show_bible.json` records completed episodes only after successful audio generation.

## Test
Run workflow_dispatch with `test_mode=true`. Check the run logs and Pages deployment, then open the feed in a podcast client. If it fails, inspect the Actions log; common setup issue is missing `GEMINI_API_KEY` or Pages not enabled. Feed URLs are absolute GitHub Pages URLs configured from the repository owner/name in the workflow.