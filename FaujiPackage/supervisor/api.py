"""FastAPI app — auth, dashboard, control, settings, setup wizard, WebSocket."""
from __future__ import annotations
import asyncio
import json
from typing import Any

from fastapi import FastAPI, Request, Response, HTTPException, Depends, WebSocket, WebSocketDisconnect, Form, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, config_store
from .bot_manager import manager
from .paths import UI_DIR
from .aws import public_ip

app = FastAPI(title="FaujiBot Supervisor", version="0.1.0")
templates = Jinja2Templates(directory=str(UI_DIR))
app.mount("/static", StaticFiles(directory=str(UI_DIR)), name="static")


# ------------------------- auth helpers -------------------------
def _current_user(request: Request) -> bool:
    token = request.cookies.get(auth.COOKIE_NAME)
    return auth.verify_token(token)


def require_auth(request: Request) -> None:
    if not _current_user(request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required")


# ------------------------- root routing -------------------------
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if not auth.is_password_set() or not config_store.is_configured():
        return RedirectResponse("/setup", status_code=302)
    if not _current_user(request):
        return RedirectResponse("/login", status_code=302)
    snap = manager.snapshot()
    return templates.TemplateResponse(
        request, "index.html",
        {"snapshot": snap, "phone_url_hint": _phone_url_hint(request)},
    )


def _phone_url_hint(request: Request) -> str | None:
    ip = public_ip()
    if not ip:
        return None
    port = request.url.port or (443 if request.url.scheme == "https" else 80)
    return f"https://{ip}:{port}"


# ------------------------- setup wizard -------------------------
@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    return templates.TemplateResponse(
        request, "setup_wizard.html",
        {
            "password_set": auth.is_password_set(),
            "configured": config_store.is_configured(),
            "phone_url_hint": _phone_url_hint(request),
        },
    )


@app.post("/setup/password")
async def setup_password(password: str = Form(...), confirm: str = Form(...)):
    if password != confirm:
        return JSONResponse({"ok": False, "error": "Passwords do not match."}, status_code=400)
    try:
        auth.set_password(password)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True}


@app.post("/setup/config")
async def setup_config(payload: dict[str, Any]):
    try:
        cfg = config_store.update(payload)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True, "config": cfg.model_dump()}


# ------------------------- login -------------------------
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if not auth.is_password_set():
        return RedirectResponse("/setup", status_code=302)
    return templates.TemplateResponse(request, "login.html", {})


@app.post("/login")
async def login(response: Response, password: str = Form(...)):
    if not auth.verify_password(password):
        return JSONResponse({"ok": False, "error": "Wrong password."}, status_code=401)
    token = auth.issue_token()
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        auth.COOKIE_NAME, token,
        max_age=auth.TOKEN_TTL_SECONDS,
        httponly=True, samesite="lax", secure=False,  # secure=True only when behind HTTPS — set by uvicorn
    )
    return resp


@app.post("/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


# ------------------------- bot control -------------------------
@app.post("/control/play", dependencies=[Depends(require_auth)])
async def control_play():
    try:
        manager.start()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True, "state": manager.state}


@app.post("/control/pause", dependencies=[Depends(require_auth)])
async def control_pause():
    manager.pause()
    return {"ok": True, "state": manager.state}


@app.post("/control/stop", dependencies=[Depends(require_auth)])
async def control_stop():
    manager.stop()
    return {"ok": True, "state": manager.state}


# ------------------------- snapshot / settings -------------------------
@app.get("/api/snapshot", dependencies=[Depends(require_auth)])
async def api_snapshot():
    return {
        "snapshot": manager.snapshot(),
        "logs": manager.logs[-50:],
    }


@app.get("/api/config", dependencies=[Depends(require_auth)])
async def api_config():
    cfg = config_store.load()
    return {"config": cfg.model_dump() if cfg else None}


@app.post("/api/config", dependencies=[Depends(require_auth)])
async def api_config_update(payload: dict[str, Any]):
    try:
        cfg = config_store.update(payload)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True, "config": cfg.model_dump()}


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    if not _current_user(request):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "settings.html", {})


# ------------------------- websocket live stream -------------------------
@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    token = ws.cookies.get(auth.COOKIE_NAME)
    if not auth.verify_token(token):
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        while True:
            payload = {
                "snapshot": manager.snapshot(),
                "logs": manager.logs[-50:],
            }
            await ws.send_text(json.dumps(payload, default=str))
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        return
    except Exception:
        try:
            await ws.close()
        except Exception:
            pass


# ------------------------- favicon -------------------------
@app.get("/favicon.ico")
async def favicon():
    f = UI_DIR / "favicon.ico"
    if f.exists():
        return FileResponse(str(f))
    return Response(status_code=204)
