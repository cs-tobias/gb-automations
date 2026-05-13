import httpx
import pytest

BASE_URL = "http://localhost:8000"


@pytest.mark.asyncio
async def test_health_endpoint_returns_ok():
    """Smoke test — requires `docker compose up` to be running."""
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=5.0) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert "env" in body
