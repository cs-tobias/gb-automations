import logging
import logging.config
import os
from contextlib import asynccontextmanager

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    try:
        yield
    finally:
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
