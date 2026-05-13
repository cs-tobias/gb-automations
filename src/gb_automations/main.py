from fastapi import FastAPI
from sqlalchemy import text

from gb_automations.config import settings
from gb_automations.db import engine

app = FastAPI(title="gb-automations", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    db_status = "ok"
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        db_status = "down"
    return {"status": "ok", "env": settings.env, "db": db_status}
