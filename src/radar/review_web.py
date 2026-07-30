from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import typer
import uvicorn
from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from starlette.middleware.sessions import SessionMiddleware

from radar.config import get_settings
from radar.costs import estimate_batch_cost
from radar.research_engine import Domain
from radar.review_cli import CITATION_STRETCH_THRESHOLD, _decide, _sync_drafts
from radar.storage import ReviewRecord, get_engine

app = typer.Typer(add_completion=False)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

DRAFTS_DIR = Path("data/drafts")
DB_PATH = Path("data/radar.db")
GENERATE_LOG_DIR = Path("data/generate_logs")
MAX_GENERATE_COUNT = 5  # matches `radar batch`'s own cap — keeps a single click bounded in cost

_JOB_ID_RE = re.compile(r"^[a-z0-9_]+_\d{8}T\d{6}Z$")
_DRAFT_TIMESTAMP_RE = re.compile(r"_(\d{8}T\d{6}Z)$")


def generated_date(draft_dir: str) -> str:
    """The date a draft was generated, read from the timestamp embedded in its
    directory name (not the file's mtime — that changes any time the video file
    itself is touched, e.g. by `radar watermark`, so it isn't a reliable source)."""
    match = _DRAFT_TIMESTAMP_RE.search(Path(draft_dir).name)
    if not match:
        return ""
    try:
        dt_utc = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    return dt_utc.astimezone().strftime("%b %-d")


_RESULT_LINE_PREFIX = "RESULT_JSON:"


def parse_batch_results(log_text: str) -> list[dict]:
    """Pull out the machine-readable per-problem outcomes `batch.py` emits, so the
    generate-status page can show a clear success/failure summary instead of making
    someone read the full console log to find out what actually happened."""
    results = []
    for line in log_text.splitlines():
        if not line.startswith(_RESULT_LINE_PREFIX):
            continue
        try:
            results.append(json.loads(line[len(_RESULT_LINE_PREFIX) :]))
        except json.JSONDecodeError:
            continue
    return results


class NotAuthenticated(Exception):
    pass


def require_login(request: Request) -> None:
    if not request.session.get("authenticated"):
        raise NotAuthenticated()


def _stretched_indices(script_data: dict) -> set[int]:
    return {
        i
        for i, entry in enumerate(script_data.get("evidence_ledger", []))
        if len(entry.get("used_in", [])) > CITATION_STRETCH_THRESHOLD
    }


def build_app() -> FastAPI:
    settings = get_settings()

    web_app = FastAPI()
    web_app.add_middleware(SessionMiddleware, secret_key=settings.review_session_secret)

    @web_app.exception_handler(NotAuthenticated)
    async def _redirect_to_login(request: Request, exc: NotAuthenticated) -> RedirectResponse:
        return RedirectResponse("/login", status_code=303)

    @web_app.get("/login")
    async def login_form(request: Request) -> object:
        return templates.TemplateResponse(request, "login.html", {"authenticated": False})

    @web_app.post("/login")
    async def login_submit(request: Request, password: str = Form(...)) -> object:
        if password == settings.review_password:
            request.session["authenticated"] = True
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(
            request, "login.html", {"authenticated": False, "error": "Wrong password."}
        )

    @web_app.get("/logout")
    async def logout(request: Request) -> RedirectResponse:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    STATUS_ORDER = {"pending": 0, "changes_requested": 1, "approved": 2, "rejected": 3}

    @web_app.get("/")
    async def dashboard(
        request: Request,
        domain: str = "",
        show_rejected: bool = False,
        _: None = Depends(require_login),
    ) -> object:
        engine = get_engine(DB_PATH)
        with Session(engine) as session:
            _sync_drafts(session, DRAFTS_DIR)
            records = session.exec(select(ReviewRecord)).all()

        flagged_ids = set()
        for r in records:
            script_path = Path(r.script_path)
            if script_path.exists():
                script_data = json.loads(script_path.read_text())
                if _stretched_indices(script_data):
                    flagged_ids.add(r.id)

        pending_count = sum(1 for r in records if r.status == "pending")
        dates = {r.id: generated_date(r.draft_dir) for r in records}

        # Normalize case so old records from before the domain list was locked down
        # (e.g. a stray "Aerospace and space systems") group with their canonical column
        # instead of splintering into their own one-off group.
        domain_order = [d.value for d in Domain]
        canonical_by_lower = {d.lower(): d for d in domain_order}

        def canonical_domain(r: ReviewRecord) -> str:
            return canonical_by_lower.get(r.domain.lower(), r.domain)

        visible = records
        if domain:
            visible = [r for r in visible if canonical_domain(r) == domain]
        if not show_rejected:
            visible = [r for r in visible if r.status != "rejected"]
        visible.sort(key=lambda r: (STATUS_ORDER.get(r.status, 9), -r.id))

        by_domain: dict[str, list[ReviewRecord]] = {}
        for r in visible:
            by_domain.setdefault(canonical_domain(r), []).append(r)
        groups = [(d, by_domain[d]) for d in domain_order if d in by_domain]
        # anything with a domain string that still doesn't match the official list after
        # normalization (shouldn't happen, but don't silently drop it) goes at the end
        groups += [(d, rs) for d, rs in by_domain.items() if d not in domain_order]

        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "authenticated": True,
                "groups": groups,
                "flagged_ids": flagged_ids,
                "dates": dates,
                "pending_count": pending_count,
                "all_domains": domain_order,
                "selected_domain": domain,
                "show_rejected": show_rejected,
            },
        )

    @web_app.get("/draft/{item_id}")
    async def draft_detail(request: Request, item_id: int, _: None = Depends(require_login)) -> object:
        engine = get_engine(DB_PATH)
        with Session(engine) as session:
            record = session.get(ReviewRecord, item_id)
        if record is None:
            return RedirectResponse("/", status_code=303)

        script_data = json.loads(Path(record.script_path).read_text())
        stretched = _stretched_indices(script_data)

        return templates.TemplateResponse(
            request,
            "detail.html",
            {
                "authenticated": True,
                "record": record,
                "sections": script_data["sections"],
                "ledger": script_data["evidence_ledger"],
                "stretched": stretched,
                "date": generated_date(record.draft_dir),
            },
        )

    @web_app.get("/video/{item_id}")
    async def video(item_id: int, _: None = Depends(require_login)) -> FileResponse:
        engine = get_engine(DB_PATH)
        with Session(engine) as session:
            record = session.get(ReviewRecord, item_id)
        return FileResponse(record.video_path, media_type="video/mp4")

    @web_app.get("/video/{item_id}/download")
    async def video_download(item_id: int, _: None = Depends(require_login)) -> FileResponse:
        engine = get_engine(DB_PATH)
        with Session(engine) as session:
            record = session.get(ReviewRecord, item_id)
        slug = record.problem_title.lower().replace(" ", "_").replace("/", "_")
        return FileResponse(record.video_path, media_type="video/mp4", filename=f"{slug}.mp4")

    @web_app.post("/draft/{item_id}/approve")
    async def approve(item_id: int, _: None = Depends(require_login)) -> RedirectResponse:
        _decide(item_id, "approved", None, DB_PATH)
        return RedirectResponse("/", status_code=303)

    @web_app.post("/draft/{item_id}/reject")
    async def reject(item_id: int, note: str = Form(""), _: None = Depends(require_login)) -> RedirectResponse:
        _decide(item_id, "rejected", note or None, DB_PATH)
        return RedirectResponse("/", status_code=303)

    @web_app.get("/generate")
    async def generate_form(request: Request, _: None = Depends(require_login)) -> object:
        return templates.TemplateResponse(
            request,
            "generate.html",
            {"authenticated": True, "domains": [d.value for d in Domain], "max_count": MAX_GENERATE_COUNT},
        )

    @web_app.post("/generate/estimate")
    async def generate_estimate(
        request: Request,
        domain: list[str] = Form(default=[]),
        count: list[int] = Form(default=[]),
        _: None = Depends(require_login),
    ) -> object:
        valid_domains = {d.value for d in Domain}
        selections = []
        for d, c in zip(domain, count):
            c = max(0, min(MAX_GENERATE_COUNT, c))
            if c > 0 and d in valid_domains:
                estimate = estimate_batch_cost(c, settings.anthropic_model)
                selections.append({"domain": d, "count": c, "estimate": estimate})

        grand_total = round(sum(s["estimate"]["total_usd"] for s in selections), 3)
        return templates.TemplateResponse(
            request,
            "generate_confirm.html",
            {"authenticated": True, "selections": selections, "grand_total": grand_total},
        )

    @web_app.post("/generate/confirm")
    async def generate_confirm(
        domain: list[str] = Form(default=[]), count: list[int] = Form(default=[]), _: None = Depends(require_login)
    ) -> RedirectResponse:
        valid_domains = {d.value for d in Domain}
        pairs = [
            (d, max(1, min(MAX_GENERATE_COUNT, c)))
            for d, c in zip(domain, count)
            if c > 0 and d in valid_domains
        ]
        if not pairs:
            return RedirectResponse("/generate", status_code=303)

        GENERATE_LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        job_id = f"batch_{timestamp}"
        log_path = GENERATE_LOG_DIR / f"{job_id}.log"

        # A small driver script run as its own process, looping domains sequentially
        # (ElevenLabs' account-level concurrency cap means parallel domains would just
        # collide on rate limits). No shell involved — pairs is embedded as a literal
        # Python list of (str, int) tuples, not interpolated into a shell string.
        driver = (
            "import subprocess, sys\n"
            f"pairs = {pairs!r}\n"
            "for domain, count in pairs:\n"
            "    print(f'=== {domain} ===', flush=True)\n"
            "    subprocess.run([sys.executable, '-m', 'radar.cli', 'batch', domain, "
            "'--count', str(count), '--yes'])\n"
            "print('ALL DOMAINS COMPLETE', flush=True)\n"
        )

        with open(log_path, "w") as log_file:
            subprocess.Popen(
                [sys.executable, "-c", driver],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=Path.cwd(),
                start_new_session=True,
            )

        return RedirectResponse(f"/generate/status/{job_id}", status_code=303)

    @web_app.get("/generate/status/{job_id}")
    async def generate_status(request: Request, job_id: str, _: None = Depends(require_login)) -> object:
        if not _JOB_ID_RE.match(job_id):
            return RedirectResponse("/generate", status_code=303)
        log_path = GENERATE_LOG_DIR / f"{job_id}.log"
        log_text = log_path.read_text() if log_path.exists() else "Starting…"
        done = "ALL DOMAINS COMPLETE" in log_text
        results = parse_batch_results(log_text)
        succeeded = [r for r in results if r["status"] == "done"]
        failed = [r for r in results if r["status"] != "done"]
        return templates.TemplateResponse(
            request,
            "generate_status.html",
            {
                "authenticated": True,
                "job_id": job_id,
                "log_text": log_text,
                "done": done,
                "succeeded": succeeded,
                "failed": failed,
            },
        )

    @web_app.get("/generate/status/{job_id}/raw")
    async def generate_status_raw(job_id: str, _: None = Depends(require_login)) -> PlainTextResponse:
        if not _JOB_ID_RE.match(job_id):
            return PlainTextResponse("invalid job id", status_code=404)
        log_path = GENERATE_LOG_DIR / f"{job_id}.log"
        return PlainTextResponse(log_path.read_text() if log_path.exists() else "Starting…")

    return web_app


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address — keep this local-only"),
    port: int | None = typer.Option(None, help="Port (defaults to REVIEW_WEB_PORT / 8000)"),
) -> None:
    """Launch the browser-based review page (local-only, password-gated)."""
    settings = get_settings()
    resolved_port = port or settings.review_web_port
    web_app = build_app()
    print(f"EdAI Problem Radar review page: http://{host}:{resolved_port}  (password in .env)")
    uvicorn.run(web_app, host=host, port=resolved_port, log_level="warning")


if __name__ == "__main__":
    app()
