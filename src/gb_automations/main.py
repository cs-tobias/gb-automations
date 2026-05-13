from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from gb_automations.config import settings
from gb_automations.db import engine
from gb_automations.jobs.scheduler import shutdown_scheduler, start_scheduler
from gb_automations.routes import debug as debug_routes
from gb_automations.routes import webhooks as webhook_routes


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    try:
        yield
    finally:
        shutdown_scheduler()


app = FastAPI(title="gb-automations", version="0.1.0", lifespan=lifespan)
app.include_router(debug_routes.router)
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
