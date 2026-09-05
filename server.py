import os
import time
import logging
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from telegram import Update

import bot  # your existing, untouched bot.py

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD")
SESSION_SECRET = os.getenv("SESSION_SECRET", bot.BOT_TOKEN)  # fine as a fallback: already secret
COOKIE_NAME = "max_dashboard_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 7  # 7 days

DASHBOARD_DIR = Path(__file__).parent / "dashboard"

serializer = URLSafeTimedSerializer(SESSION_SECRET)
logger = logging.getLogger(__name__)

START_TIME = time.time()
bot_paused = False

# ---------------------------------------------------------------------------
# In-memory log tail
# ---------------------------------------------------------------------------
# Captures everything the bot's own logging already produces (handler
# activity, Gemini fallback attempts, Telegram errors, apscheduler jobs)
# without changing a single log line in bot.py.

LOG_BUFFER: deque[str] = deque(maxlen=500)


class BufferLogHandler(logging.Handler):
    def emit(self, record):
        LOG_BUFFER.append(self.format(record))


def attach_log_capture():
    handler = BufferLogHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S"
    ))
    logging.getLogger().addHandler(handler)  # root logger — catches bot.py + libraries too


# ---------------------------------------------------------------------------
# Telegram Application lifecycle
# ---------------------------------------------------------------------------

telegram_app = None  # set during startup


@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app

    if not DASHBOARD_PASSWORD:
        raise RuntimeError(
            "DASHBOARD_PASSWORD environment variable is not set. "
            "Set it in Render to whatever password you want to log into the dashboard with."
        )
    if not bot.WEBHOOK_URL:
        raise RuntimeError(
            "WEBHOOK_URL environment variable is not set. "
            "Set it to this service's public Render URL."
        )

    attach_log_capture()

    telegram_app = bot.build_application()
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.bot.set_webhook(
        url=f"{bot.WEBHOOK_URL}/webhook/{bot.BOT_TOKEN}",
        allowed_updates=Update.ALL_TYPES,
    )
    logger.info("💪 Max is running (dashboard + webhook mode)...")

    yield

    await telegram_app.stop()
    await telegram_app.shutdown()


app = FastAPI(lifespan=lifespan)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def is_authed(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return False
    try:
        serializer.loads(token, max_age=COOKIE_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


def require_auth(request: Request):
    if not is_authed(request):
        raise HTTPException(status_code=401, detail="Not logged in")


# ---------------------------------------------------------------------------
# Telegram webhook
# ---------------------------------------------------------------------------

@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if token != bot.BOT_TOKEN:
        raise HTTPException(status_code=404)

    if bot_paused:
        # Bot is paused from the dashboard — silently drop updates.
        return JSONResponse({"ok": True, "paused": True})

    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Auth API
# ---------------------------------------------------------------------------

@app.post("/api/login")
async def login(request: Request, response: Response):
    body = await request.json()
    if body.get("password") != DASHBOARD_PASSWORD:
        raise HTTPException(status_code=401, detail="Wrong password")

    token = serializer.dumps("ok")
    response = JSONResponse({"ok": True})
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=True,
    )
    return response


@app.post("/api/logout")
async def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME)
    return response


# ---------------------------------------------------------------------------
# Dashboard data + control API
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def status(request: Request):
    require_auth(request)
    return {
        "bot_username": telegram_app.bot.username if telegram_app else None,
        "uptime_seconds": int(time.time() - START_TIME),
        "paused": bot_paused,
        "rate_limit": bot.RATE_LIMIT,
        "rate_window": bot.RATE_WINDOW,
        "prompt_template": bot.PROMPT_TEMPLATE,
    }


@app.get("/api/logs")
async def logs(request: Request):
    require_auth(request)
    return {"logs": list(LOG_BUFFER)}


@app.post("/api/pause")
async def toggle_pause(request: Request):
    require_auth(request)
    global bot_paused
    bot_paused = not bot_paused
    logger.info(f"Dashboard: bot {'paused' if bot_paused else 'resumed'}")
    return {"paused": bot_paused}


@app.post("/api/trigger-tip")
async def trigger_tip(request: Request):
    require_auth(request)
    telegram_app.job_queue.run_once(bot.daily_tip, when=1)
    logger.info("Dashboard: manually triggered daily tip")
    return {"ok": True}


@app.post("/api/config")
async def update_config(request: Request):
    require_auth(request)
    body = await request.json()

    if "prompt_template" in body and body["prompt_template"].strip():
        bot.PROMPT_TEMPLATE = body["prompt_template"]
        logger.info("Dashboard: prompt template updated")

    if "rate_limit" in body:
        bot.RATE_LIMIT = int(body["rate_limit"])
        logger.info(f"Dashboard: rate limit set to {bot.RATE_LIMIT}")

    if "rate_window" in body:
        bot.RATE_WINDOW = int(body["rate_window"])
        logger.info(f"Dashboard: rate window set to {bot.RATE_WINDOW}s")

    return {"ok": True}


# ---------------------------------------------------------------------------
# Static dashboard files
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=DASHBOARD_DIR), name="static")


@app.get("/")
async def dashboard_page():
    return FileResponse(DASHBOARD_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
