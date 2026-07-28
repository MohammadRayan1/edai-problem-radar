from __future__ import annotations

import json
from pathlib import Path

import typer
import uvicorn
from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from starlette.middleware.sessions import SessionMiddleware

from radar.config import get_settings
from radar.review_cli import CITATION_STRETCH_THRESHOLD, _decide, _sync_drafts
from radar.storage import ReviewRecord, get_engine

app = typer.Typer(add_completion=False)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

DRAFTS_DIR = Path("data/drafts")
DB_PATH = Path("data/radar.db")


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

    @web_app.get("/")
    async def dashboard(request: Request, _: None = Depends(require_login)) -> object:
        engine = get_engine(DB_PATH)
        with Session(engine) as session:
            _sync_drafts(session, DRAFTS_DIR)
            records = session.exec(select(ReviewRecord).order_by(ReviewRecord.id.desc())).all()

        flagged_ids = set()
        for r in records:
            script_path = Path(r.script_path)
            if script_path.exists():
                script_data = json.loads(script_path.read_text())
                if _stretched_indices(script_data):
                    flagged_ids.add(r.id)

        pending_count = sum(1 for r in records if r.status == "pending")
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "authenticated": True,
                "records": records,
                "flagged_ids": flagged_ids,
                "pending_count": pending_count,
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
