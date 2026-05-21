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
            # Compact, colorized, human-readable console output. Color auto-
            # disables when piped to a file or when NO_COLOR is set; force with
            # LOG_COLOR=0/1. See gb_automations.obs.ColorFormatter.
            "default": {
                "()": "gb_automations.obs.ColorFormatter",
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
    # effort: if the host's native Ollama isn't running yet, log and move on.
    # The tagging client already treats Ollama failures as `[]` per project
    # convention.
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


async def _seed_and_watch_mailboxes() -> None:
    # Make a fresh `docker compose up` fully self-sufficient: reconcile the
    # `users` table to SYNCED_MAILBOXES, then start their Gmail watches. Both
    # are idempotent and best-effort — on an already-running box this just
    # confirms the existing state. Without this, a brand-new DB volume (e.g. a
    # freshly-cloned machine) has zero users and zero watches, so nothing
    # syncs until someone runs the scripts by hand.
    log = logging.getLogger(__name__)

    from gb_automations.scripts.seed_users import reconcile_users

    wanted = settings.synced_mailbox_list
    if not wanted:
        log.warning(
            "SYNCED_MAILBOXES is empty — no mailboxes will be synced. "
            "Set it in .env (comma-separated) and `--force-recreate api`."
        )

    try:
        result = await reconcile_users(wanted)
        log.info(
            "Mailboxes reconciled: active=%s deactivated=%s",
            result["active"] or "(none)",
            result["deactivated"] or "(none)",
        )
    except Exception:
        log.exception("Mailbox reconcile failed — sync may not work until fixed")
        return

    if not settings.pubsub_topic:
        log.warning(
            "PUBSUB_TOPIC not set — skipping Gmail watch startup. "
            "No emails will push until Pub/Sub is configured (see google-setup.md)."
        )
        return

    from gb_automations.sync.watches import renew_all_watches

    try:
        results = await renew_all_watches()
        ok = [r["email"] for r in results if r.get("ok")]
        failed = [r["email"] for r in results if not r.get("ok")]
        log.info("Gmail watches started: ok=%s failed=%s", ok or "(none)", failed or "(none)")
    except Exception:
        log.exception("Starting Gmail watches failed — emails won't push until renewed")


@asynccontextmanager
def _check_nas_mount() -> None:
    # Non-fatal: a misconfigured/unmounted NAS share must never block boot (the
    # Gmail side is the team's hard dependency). But surface it loudly at startup
    # so a wrong mount path / CIFS uid mismatch (see gotchas.md) is visible
    # before the first project click silently reports nas:failed.
    if not settings.sync_nas_folders:
        return
    from gb_automations.clients import nas as nas_client

    log = logging.getLogger(__name__)
    if not settings.nas_projects_root:
        log.warning("SYNC_NAS_FOLDERS=true but NAS_PROJECTS_ROOT is empty — NAS step inert")
    elif not nas_client.nas_available():
        log.warning(
            "NAS root %r is not a writable directory — project-folder sync will report "
            "nas:failed until the share is mounted (see docs/nas-setup.md)",
            settings.nas_projects_root,
        )
    else:
        log.info("NAS project root %r is mounted and writable", settings.nas_projects_root)


async def _recover_and_reconcile_queue() -> None:
    # Boot-time guarantee work, in order:
    #   1. reset_in_progress(): a row left `in_progress` means a previous process
    #      crashed mid-sync — flip it back to pending so the worker re-runs it
    #      (sync_thread is idempotent, so re-running is safe).
    #   2. enqueue_missing_for_all_projects(): catch threads labeled while the app
    #      was completely DOWN (never enqueued — the push was dropped / outside
    #      Gmail's history window). This is the only thing the live queue can't
    #      cover on its own. Best-effort: log and continue on failure.
    from gb_automations.sync.queue import reset_in_progress
    from gb_automations.sync.resync_project import enqueue_missing_for_all_projects

    log = logging.getLogger(__name__)
    try:
        reset = await reset_in_progress()
        if reset:
            log.info("Recovered %d in-progress sync task(s) left by a previous run", reset)
    except Exception:
        log.exception("Queue crash-recovery (reset_in_progress) failed")

    try:
        await enqueue_missing_for_all_projects()
    except Exception:
        log.exception("Boot reconcile (enqueue_missing_for_all_projects) failed")


async def lifespan(app: FastAPI):
    from gb_automations.jobs import queue_worker

    _check_nas_mount()
    start_scheduler()
    pull_task = asyncio.create_task(_ensure_model_present())
    seed_task = asyncio.create_task(_seed_and_watch_mailboxes())
    recover_task = asyncio.create_task(_recover_and_reconcile_queue())
    worker_task = asyncio.create_task(queue_worker.run_worker())
    try:
        yield
    finally:
        # Stop the worker gracefully so an in-flight sync settles; anything still
        # mid-flight is recovered on next boot by reset_in_progress().
        queue_worker.request_shutdown()
        pull_task.cancel()
        seed_task.cancel()
        recover_task.cancel()
        try:
            await asyncio.wait_for(worker_task, timeout=30)
        except (TimeoutError, asyncio.CancelledError):
            worker_task.cancel()
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
