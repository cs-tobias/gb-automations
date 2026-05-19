import asyncio
import logging
import logging.config
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from sqlalchemy import text

from gb_automations.config import settings
from gb_automations.db import engine
from gb_automations.jobs.scheduler import shutdown_scheduler, start_scheduler
from gb_automations.routes import debug as debug_routes
from gb_automations.routes import oauth as oauth_routes
from gb_automations.routes import webhooks as webhook_routes

# Log level is env-driven so ops can flip to DEBUG (shows outbound Notion/Gmail
# API call lines) without rebuilding the image. Defaults to INFO.
_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# Make application INFO logs visible in `docker compose logs`. uvicorn's default
# config swallows logger.info() from application code; this explicit dictConfig
# wires our package's loggers + uvicorn's together with a single formatter.
#
# The `request_id` filter stamps every record with a `[prefix:abcd]` tag while
# a webhook is in flight (see obs.py). The tag lets you grep one request's
# entire call tree out of interleaved logs.
logging.config.dictConfig(
    {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "request_id": {"()": "gb_automations.obs.RequestIdFilter"},
        },
        "formatters": {
            "default": {
                "format": "%(asctime)s %(levelname)-7s %(name)s | %(request_id)s%(message)s",
                "datefmt": "%H:%M:%S",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "filters": ["request_id"],
            },
        },
        "loggers": {
            "gb_automations": {
                "level": _LOG_LEVEL,
                "handlers": ["console"],
                "propagate": False,
            },
            "uvicorn": {"level": "INFO", "handlers": ["console"], "propagate": False},
            "uvicorn.error": {"level": "INFO", "handlers": ["console"], "propagate": False},
            # Quiet uvicorn access logs (POST /webhooks/X HTTP/1.1 200 OK) — our
            # application-level logs paint the same picture with more context.
            "uvicorn.access": {"level": "WARNING", "handlers": ["console"], "propagate": False},
            "httpx": {"level": "WARNING", "handlers": ["console"], "propagate": False},
        },
        "root": {"level": "WARNING", "handlers": ["console"]},
    }
)


def _validate_required_settings() -> None:
    if settings.pubsub_topic and not settings.pubsub_audience:
        raise RuntimeError(
            "PUBSUB_AUDIENCE is required when PUBSUB_TOPIC is set. "
            "Set it to the audience configured on the GCP push subscription, "
            "typically https://hub.{your-domain}/webhooks/gmail."
        )


_validate_required_settings()


async def _ensure_model_present() -> None:
    # Auto-pull the configured Ollama model on startup so a fresh `docker
    # compose up` is sufficient — no one needs to remember a manual step. Best
    # effort: if Ollama is unreachable (e.g. Mac dev opted out and the native
    # process isn't running), log and move on. The tagging client already
    # treats Ollama failures as `[]` per project convention.
    base = settings.ollama_base_url.rstrip("/")
    want = settings.ollama_model
    log = logging.getLogger(__name__)

    await asyncio.sleep(3)

    deadline = asyncio.get_event_loop().time() + 180
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                resp = await client.get(f"{base}/api/tags")
                resp.raise_for_status()
                tags = resp.json().get("models") or []
                if any(m.get("name") == want for m in tags):
                    log.info("Ollama model %s already present, skipping pull", want)
                    return
                break
            except Exception as err:
                if asyncio.get_event_loop().time() >= deadline:
                    log.warning(
                        "Ollama not reachable at %s after 3 min (%s) — "
                        "skipping auto-pull. Tagging will be a no-op until "
                        "Ollama is available.",
                        base,
                        err,
                    )
                    return
                await asyncio.sleep(10)

    try:
        from gb_automations.scripts.pull_llm_model import pull

        log.info("Ollama model %s missing — starting background pull", want)
        rc = await pull()
        if rc == 0:
            log.info("Ollama model %s pulled successfully", want)
        else:
            log.warning("Ollama model pull returned non-zero status %s", rc)
    except Exception as err:
        log.warning("Auto-pull of Ollama model failed: %s", err)


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    pull_task = asyncio.create_task(_ensure_model_present())
    try:
        yield
    finally:
        pull_task.cancel()
        shutdown_scheduler()


app = FastAPI(title="gb-automations", version="0.1.0", lifespan=lifespan)
app.include_router(debug_routes.router)
app.include_router(oauth_routes.router)
app.include_router(webhook_routes.router)


@app.get("/health")
async def health() -> dict[str, str]:
    db_status = "ok"
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        db_status = "down"
    return {"status": "ok", "env": settings.env, "db": db_status}
