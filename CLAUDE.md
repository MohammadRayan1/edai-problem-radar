# EdAI Problem Radar

Internal tool that finds cited, evidence-backed domain problems and turns them into short vertical (9:16) video scripts and draft videos for review.

## Program context

This tool produces the discovery content for **EdAI's World View program**
(https://edai.fun/programs/world-view) — an 8-week, faith-centered exploration
program for Muslim students in grades 6–12. World View walks students through a
consequential field, its people/systems, and its unmet needs, so they can pick a
real problem to investigate.

Problem Radar exists to seed that exploration: for each of the program's 10
focus domains, it generates short (<60s) AI-narrated videos, styled after
Y Combinator's RFS shorts, that introduce students to real, cited problems in
that domain. A student who's drawn to a video's problem can carry it forward —
research it, then eventually build a tool to address it, which is the on-ramp
into EdAI's **Founder Platform**.

The 10 domains (locked in `research_engine.Domain`, not free text):
1. Aerospace and Space Systems
2. Defense
3. Agriculture
4. Energy
5. Public Safety and Emergency Response
6. Supply Chain and Critical Logistics
7. Industrials, Manufacturing and Small and Medium-Sized Enterprises
8. Education
9. Healthcare
10. Housing

Because the output is discovery content for teenagers, not internal ops
tooling: problem selection should skew toward what's genuinely interesting and
graspable to a teen (this is what the Teen Accessibility score in
`research_engine` is for), and video visuals (relevance-checked stock video
clips, motion-graphic icon badges as fallback, karaoke-style word-highlighted
captions) should be engaging, not just functional captions-on-a-background.

Launch scope: internal tool. The team uses `radar batch` / `radar review`
directly from the CLI — no web dashboard yet. (Domain experts getting their
own login on a review website is a known future step, not built.)

## Pipeline

```
research_engine  -->  script_engine  -->  video_engine  -->  review_cli
  (find + cite         (<60s script        (TTS + icon         (approve /
   2+ problems,         + evidence ledger)  animation +         reject / notes,
   distinct sources)                        karaoke captions)   citation-stretch
                                                                 audit)
```

Each stage writes its output to disk as a versioned artifact (JSON/MP4) so any
stage can be re-run independently without re-running upstream stages.
`batch.py` chains all three generation stages for N problems in one command.

## Core modules

### 1. `research_engine.py`
Finds domain problems worth making content about, backed by citations.

- Searches Tavily across several query templates for a given domain, dedupes
  by URL.
- Uses Claude (forced tool-use) to extract exactly 5 candidate problems per
  domain, each scored 1-10 on Consequence / Urgency / Neglect / Teen
  Accessibility.
- **Citation diversity is enforced by code, not just prompted for**:
  `_enforce_citation_diversity()` drops any problem whose citations don't come
  from 2+ genuinely distinct source URLs, regardless of what the model
  returns. This exists because a single (or duplicate-URL) citation
  structurally pressures `script_engine` into stretching that one source
  across unrelated claims.
- Output: `Problem` objects — each with a claim, 2+ citations, and a score.

### 2. `script_engine.py`
Turns a `Problem` into a <60s spoken script in a 5-beat YC-RFS-style formula
(Hook → Why It Matters → Why Now → Opportunity → Teen Challenge).

- Enforces a strict **evidence ledger**: every citation index a script line
  references must resolve to a real citation object from the upstream
  `Problem` — the ledger is built from that citation, never from
  model-generated text, so fabrication is structurally impossible.
- The model is explicitly instructed not to force a citation onto a line it
  doesn't verify (e.g. hypothetical "Opportunity" solution-sketch lines stay
  uncited), and not to reuse one citation across many unrelated claims just to
  satisfy "at least one cited line per section."
- Pacing is enforced two ways: a per-section word budget, and a total-script
  word cap (`_total_word_cap`, tied to `TOTAL_DURATION_CAP_SEC = 58`) as a
  structural safety net independent of per-section distribution.
- Output: `Script` object — sections of timed lines plus the evidence ledger
  (citation → lines it backs) for auditability.

### 3. `video_engine.py`
Assembles a vertical draft video from a `Script`.

- Narration via ElevenLabs TTS (`with-timestamps` endpoint), giving
  character-level alignment; word boundaries are derived by grouping
  consecutive non-whitespace characters, guaranteeing exact correspondence
  with the script text.
- Per-line visuals: a relevance-checked stock video clip (`stock_video.py`,
  Pexels) or an icon badge (`visuals.py`, Iconify) — but never a mix within
  one video. A video only uses stock footage if *every* line found a
  relevant clip; if even one line comes up empty, the whole video falls back
  to icons throughout instead, so the visual style stays consistent
  (`_use_video_for_all_lines`).
  - The relevance check is what makes stock video usable at all — an earlier
    attempt at this used stock footage without one and shipped an unrelated
    clip behind a real line. A lexical search match doesn't mean visual
    relevance (a first fix using just the clip's text description still let
    through a stock-market-chart clip for a line about spacecraft engineers),
    so the check sends Claude the candidate's actual thumbnail *image*
    alongside the line and asks it to judge the real visible content, not
    just whether the search query shared a keyword.
  - Coverage varies a lot by topic (spaceflight: usually 0% real video,
    general industries: often 20-30% per line) — combined with the
    all-or-nothing rule, most videos end up all-icon, and that's expected,
    not a bug. All-video only happens when a topic's real footage coverage
    happens to hit 100% of lines.
- Karaoke-style captions: each spoken word highlights as it's said. Rendered
  via direct PIL image compositing (`captions.py`), not MoviePy `TextClip` —
  compositing many differently-sized TextClips together has real glyph-sizing
  bugs; PIL-rendered frames sidestep that entirely.
- Assembles with MoviePy, outputs 1080×1920 (9:16) MP4 + a `meta.json`
  sidecar (title, domain, duration, paths back to the script/research source).

### 4. `review_cli.py`
Human-in-the-loop approval gate. No content ships without a human approving it
here.

- `list` / `show` / `approve` / `reject` / `request-changes`, backed by a
  SQLite `ReviewRecord` table (`storage.py`).
- Automated **citation-stretch detector**: flags any evidence-ledger entry
  reused across more than `CITATION_STRETCH_THRESHOLD` (3) lines — a signal
  a citation may be verifying claims it doesn't actually support. Shown as a
  column in `list` and a highlighted panel in `show`. This is a heuristic
  flag for the human reviewer, not a hard gate.
- `reject` / `request-changes` persist a free-text note for traceability.

### Supporting modules
- `batch.py` — `radar batch <domain>` chains research → script (with retry on
  `ScriptValidationError`) → video for the top N scored problems, with a
  pre-flight cost estimate and a Rich summary table.
- `costs.py` — real per-call usage logging (`data/usage.jsonl`) for both
  Anthropic token usage and ElevenLabs character usage, plus
  `radar usage show` and a batch cost estimator.
- `storage.py` — `ReviewRecord` SQLModel + SQLite engine helper.
- `config.py` — `pydantic-settings`-based `.env` config (API keys, model IDs,
  voice ID, font path).
- `watermark.py` — `radar watermark` stamps the EdAI wordmark onto existing
  draft videos in place, as pure local video compositing (no API calls, so
  it's free to run against already-generated drafts).

## Tech stack (as built)

| Concern | Choice | Notes |
|---|---|---|
| Language | Python 3.11+ | |
| Search/research | Tavily API | |
| HTTP | `httpx` (async) | concurrency-limited + retry for TTS calls |
| Data models | `pydantic` v2 | `Problem`, `Script`, `EvidenceLedgerEntry`, etc. (`models.py`) |
| LLM calls | Anthropic SDK, model `claude-sonnet-5`, forced tool-use + adaptive thinking | research extraction, script generation, icon/video query generation, vision-based stock-clip relevance check |
| TTS | ElevenLabs (`with-timestamps` endpoint) | character-level alignment; free tier is 10k chars/month — watch quota; default voice is George (warm storyteller) |
| Stock video | Pexels Video API (free) | per-line search, portrait-orientation preferred; every candidate is vision-checked before use — see `video_engine.py` above |
| Icons | Iconify API (free, no key) | AND-based search — use compound/single-word queries; fallback when no stock video passes the relevance check |
| Video assembly | `moviepy` 2.x + PIL-rendered caption/badge frames | MoviePy `TextClip` avoided for captions due to compositing bugs |
| CLI | `typer` + `rich` | root `radar` app wires up all subcommands |
| Storage | local filesystem + SQLite (`sqlmodel`) | artifact files on disk, review state in SQLite |
| Config/secrets | `pydantic-settings` + `.env` | |
| Testing | `pytest` + `pytest-asyncio` | `httpx.MockTransport` for async HTTP code |

## Actual project file structure

```
edai-problem-radar/
├── CLAUDE.md
├── README.md
├── pyproject.toml
├── .env.example / .env
├── src/radar/
│   ├── cli.py              # root Typer app
│   ├── config.py           # pydantic-settings config
│   ├── models.py           # shared pydantic models
│   ├── storage.py          # SQLModel ReviewRecord + engine
│   ├── costs.py            # usage logging + `radar usage show`
│   ├── research_engine.py  # Domain enum, search, extract+score, citation-diversity gate
│   ├── script_engine.py    # section specs, evidence ledger, pacing gates
│   ├── video_engine.py     # TTS, timeline, assembly
│   ├── visuals.py          # icon query gen + Iconify sourcing + badge rendering
│   ├── stock_video.py      # video query gen + Pexels sourcing + vision relevance check
│   ├── watermark.py        # `radar watermark` — free, in-place post-process stamp
│   ├── captions.py         # PIL karaoke caption layout/rendering
│   └── batch.py            # `radar batch` — chains all 3 generation stages
├── data/
│   ├── raw/                # research_engine output
│   ├── scripts/            # script_engine output
│   ├── drafts/             # video_engine output (MP4 + meta.json)
│   ├── radar.db            # SQLite review state
│   └── usage.jsonl         # cost/usage log
└── tests/
    ├── research_engine/
    ├── script_engine/
    ├── video_engine/
    ├── visuals/
    ├── stock_video/
    ├── captions/
    └── review_cli/
```

## Invariants

- No claim in a `Script` may lack a citation traceable to a real `Problem`
  citation — enforced by `_build_evidence_ledger` / `_validate_evidence_ledger`
  in `script_engine.py`, not just prompted for.
- No `Problem` reaches `script_engine` unless it has 2+ citations from
  distinct source URLs — enforced by `_enforce_citation_diversity` in
  `research_engine.py`, not just prompted for.
- No video reaches "approved" state without passing through `review_cli`.
- No stock video clip is used as a line's visual unless it passed the
  vision-based relevance check in `stock_video.check_relevance_batch` — a
  rejected or unchecked candidate always falls back to an icon, never gets
  used on a hunch.
- A single video never mixes stock video and icon visuals — it's all one or
  all the other (`_use_video_for_all_lines`).
- Every artifact (`Problem`, `Script`, video draft) is versioned and traceable
  back to its upstream input for auditability.
