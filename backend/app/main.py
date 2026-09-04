"""FastAPI application entry point.

Layout:
  /api/health              → liveness
  /api/auth/*              → register / login / demo-login (JWT issuance)
  /api/watchlist           → add / list-with-diff / delete (auth required)
  /api/stocks/:symbol/*    → per-stock details (symbol-keyed, no auth — shared data)
  /api/dev/*               → dev-only pipeline hooks (populate/history)
  /ws/notifications        → live push for significant events (§08)

The dev router is gated by an env var so a real deploy wouldn't expose it
by default; the scheduler is gated similarly so a local `--reload` dev loop
doesn't accidentally hammer live market sources on every file save.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .auth import decode_user_id
from .db import Base, engine
from .narrator import resolve_provider
from .notifications import manager
from .pipeline import shutdown_reconciler
from .routers import auth, stocks, watchlist
from .routers.dev import router as dev_router
from .scheduler import start_scheduler, stop_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

DEV_ROUTES_ENABLED = os.getenv("DEV_ROUTES", "1") == "1"
SCHEDULER_ENABLED = os.getenv("SCHEDULER_ENABLED", "1") == "1"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(bind=engine)
    if SCHEDULER_ENABLED:
        start_scheduler()
    yield
    if SCHEDULER_ENABLED:
        stop_scheduler()
    await shutdown_reconciler()


app = FastAPI(
    title="Smart Market Watchlist",
    description=(
        "Diff-since-last-visit stock watchlist with multi-source price "
        "reconciliation and volatility-normalised significance detection. "
        "Built for Code, by Groww 2026."
    ),
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],           # tighten in prod
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(watchlist.router, prefix="/api/watchlist", tags=["watchlist"])
app.include_router(stocks.router, prefix="/api/stocks", tags=["stocks"])
if DEV_ROUTES_ENABLED:
    app.include_router(dev_router, prefix="/api/dev", tags=["dev"])


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "smart-market-watchlist",
        "scheduler_enabled": SCHEDULER_ENABLED,
        # Reports the truth about narration config rather than making the
        # operator guess whether their key was picked up. "none" means the
        # narrate endpoint will cite real news but skip LLM synthesis.
        "narrator_provider": resolve_provider(),
    }


@app.websocket("/ws/notifications")
async def ws_notifications(websocket: WebSocket, token: str = ""):
    """§08 live push channel. Browser can't set an Authorization header on
    the WebSocket handshake, so the JWT travels as a query param instead —
    same token, same validation path as every REST call."""
    user_id = decode_user_id(token) if token else None
    if user_id is None:
        await websocket.close(code=4401)
        return
    await manager.connect(user_id, websocket)
    try:
        while True:
            await websocket.receive_text()   # keep-alive; payload content unused
    except WebSocketDisconnect:
        manager.disconnect(user_id, websocket)


# Mount the frontend LAST so the /api/* and /ws/* routes above take precedence.
# StaticFiles with html=True serves index.html for /, so opening
# http://127.0.0.1:8765/ shows the app.
_STATIC_DIR = Path(__file__).parent.parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")
