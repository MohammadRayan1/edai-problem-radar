# EdAI Problem Radar

Generates short, cited, AI-narrated discovery videos for [EdAI's World View program](https://edai.fun/programs/world-view) — one video per real-world problem, across the program's 10 focus domains. Students watch a video, and if the problem grabs them, that's their on-ramp to EdAI's Founder Platform.

See [`CLAUDE.md`](./CLAUDE.md) for the full architecture and design rationale. This doc is setup + day-to-day usage.

## Pipeline

```
research  →  script  →  video  →  review
```

1. **research** — searches a domain, has Claude extract 5 real problem candidates, each scored 1–10 on Consequence / Urgency / Neglect / Teen Accessibility, with real citations (no fabricated sources)
2. **script** — turns a problem into a ~50s script following a fixed 5-beat formula (Hook → Why It Matters → Why Now → Opportunity → Teen Challenge), modeled on Y Combinator's Request for Startups tone. Every factual claim must trace back to a real citation — enforced by code, not just prompted for
3. **video** — narrates the script (ElevenLabs), times karaoke-style captions to the real audio, and composites a vertical 9:16 draft with topic-matched icon animations
4. **review** — a local approval queue (list / show / approve / reject / request-changes) — nothing ships without a human approving it here

`radar batch` runs research → script → video for a domain's top N problems in one command, instead of running each stage by hand per video.

## Setup

Requires Python 3.11+.

```bash
git clone <this repo>
cd edai-problem-radar
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Fill in `.env` with your API keys (see below), then confirm everything's wired up:

```bash
pytest -q        # should show 53 passed
radar --help
```

### API keys

| Key | Required | Get it at | Notes |
|---|---|---|---|
| `TAVILY_API_KEY` | Yes | [app.tavily.com](https://app.tavily.com) | Search — finds real sources for each domain |
| `ANTHROPIC_API_KEY` | Yes | [console.anthropic.com](https://console.anthropic.com) | Used throughout: research scoring, script writing, icon selection |
| `ELEVENLABS_API_KEY` | Yes | [elevenlabs.io](https://elevenlabs.io) | Narration. **The key needs the "Text to Speech" permission enabled** (and ideally "Voices → Read") — a key without it will fail with a `missing_permissions` error. Free tier works for testing; check your plan's rate limits before running large batches |

No key needed for Iconify (icon visuals) — it's a free public API.

## Commands

```bash
# Research one of the 10 official domains (exact names required — tab-complete via --help)
radar research "Aerospace and Space Systems"

# Turn a researched problem into a script (defaults to the highest-scored problem in the file)
radar script data/raw/aerospace_and_space_systems_<timestamp>.json

# Assemble the draft video from a script
radar video data/scripts/<problem>_<timestamp>.json

# Do all three in one shot, for a domain's top N problems
radar batch "Healthcare" --count 3

# Review the queue (CLI)
radar review list
radar review show <id>
radar review approve <id>
radar review reject <id> --note "why"
radar review request-changes <id> --note "what should change"

# Review the queue (browser page — for non-technical reviewers, e.g. a manager)
radar review-web

# Stamp the "EdAI Inc" watermark onto existing draft videos, in place — free, no API calls
radar watermark

# See what you've spent so far
radar usage show
```

### Browser review page (`radar review-web`)

A local, password-gated web page for reviewing drafts without touching the CLI — watch each video, read the script with citation markers, see the evidence ledger (with citation-stretch warnings highlighted), then "Post it" (approve) or "Flag it" (reject, with an optional note).

- Password comes from `REVIEW_PASSWORD` in `.env` — set a real one before sharing this with anyone.
- By default it only listens on `127.0.0.1` (this machine only). To let someone review from their own device on the same WiFi/network, run `radar review-web --host 0.0.0.0` and give them `http://<this-machine's-LAN-IP>:8000` (find your LAN IP with `ipconfig getifaddr en0` on macOS). Anyone on that network who has the password can reach it, so only do this on a trusted network.
- `--port` overrides the port (default 8000 / `REVIEW_WEB_PORT` in `.env`).

It also has a **Generate videos** page (link in the header) so a non-technical user can kick off new generation without the CLI: pick a domain and count (capped at 5 per click), see a price estimate, confirm, then watch a live log until it's done. Runs as a background process — closing the browser tab doesn't stop it, and the resulting drafts just show up in the review queue.

The 10 valid domains (must match exactly): Aerospace and Space Systems, Defense, Agriculture, Energy, Public Safety and Emergency Response, Supply Chain and Critical Logistics, Industrials/Manufacturing and Small and Medium-Sized Enterprises, Education, Healthcare, Housing.

## What to expect from a run

- `radar batch` shows a rough cost estimate and asks for confirmation before spending (skip with `--yes`)
- Script generation retries automatically (up to 3x) if a script fails the evidence-ledger or pacing gate — this is normal and by design, not a bug. If a specific problem fails all 3 attempts, `batch` skips it and continues with the rest rather than aborting the whole run
- A single video costs roughly $0.05–$0.15 in Anthropic usage plus ElevenLabs narration cost (character-based — check your ElevenLabs plan). Real costs are logged as they happen; `radar usage show` is the source of truth, not the pre-flight estimate
- Everything lands in `data/`: `raw/` (research), `scripts/`, `drafts/` (videos + `meta.json`), and `radar.db` (the review queue)

## Known limitations (as of launch)

- **Single-machine, single-reviewer.** The review queue is a local SQLite file — there's no shared multi-user dashboard yet. If multiple people need to review, that's the next scope, not this one.
- **ElevenLabs plan limits.** Free/low tiers cap concurrent requests; `video_engine` retries on rate limits but a very large batch may still be slow on a constrained plan.
- **Icon matches aren't always perfect.** Icon selection is best-effort semantic matching (via Iconify), not guaranteed on-topic for every line — that's exactly what the review step is for.
- **No publish step.** An approved video sits in `data/drafts/` — getting it onto the actual World View platform is a manual step for now.
