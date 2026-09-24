from collections.abc import AsyncIterator

import httpx
import pytest

from tgmonitor.pipeline import Pipeline
from tgmonitor.repository import Repository
from tgmonitor.web.api import AppDeps, create_app

TOKEN = "t" * 40


class NullSender:
    async def send_alert(self, alert: object) -> bool:
        return True


@pytest.fixture
async def client(repo: Repository) -> AsyncIterator[httpx.AsyncClient]:
    pipeline = Pipeline(repo, NullSender())

    async def refresh() -> int:
        return 0

    app = create_app(
        AppDeps(
            api_token=TOKEN,
            repo=repo,
            pipeline=pipeline,
            refresh_chats=refresh,
            health=lambda: {"ok": True},
        )
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as c:
        yield c


AUTH = {"X-API-Token": TOKEN}


async def test_foreign_host_rejected(client: httpx.AsyncClient) -> None:
    """DNS rebinding: Host diferente de 127.0.0.1/localhost é recusado antes de tudo."""
    r = await client.get("/api/health", headers={**AUTH, "Host": "evil.example.com"})
    assert r.status_code == 400
    r = await client.get("/", headers={"Host": "evil.example.com"})
    assert r.status_code == 400


async def test_localhost_host_accepted(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/health", headers={**AUTH, "Host": "localhost:8000"})
    assert r.status_code == 200


async def test_token_required(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/health")).status_code == 401
    assert (await client.get("/api/rules", headers={"X-API-Token": "errado"})).status_code == 401
    assert (await client.get("/api/health", headers=AUTH)).status_code == 200


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/chats"),
        ("POST", "/api/chats/refresh"),
        ("PUT", "/api/chats/1"),
        ("GET", "/api/rules"),
        ("POST", "/api/rules"),
        ("PUT", "/api/rules/1"),
        ("DELETE", "/api/rules/1"),
        ("POST", "/api/rules/test"),
        ("GET", "/api/alerts"),
        ("GET", "/api/settings"),
        ("PUT", "/api/settings"),
    ],
)
async def test_every_api_route_requires_token(
    client: httpx.AsyncClient, method: str, path: str
) -> None:
    r = await client.request(method, path, json={})
    assert r.status_code == 401


async def test_docs_disabled(client: httpx.AsyncClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await client.get(path)).status_code == 404


async def test_security_headers_on_index(client: httpx.AsyncClient) -> None:
    r = await client.get("/")
    assert r.status_code == 200
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"


async def test_rule_crud_and_test(client: httpx.AsyncClient) -> None:
    body = {
        "name": "S25",
        "include_terms": ["/s2[56]\\s*ultra/"],
        "exclude_terms": ["capa"],
        "max_price_cents": 600000,
    }
    r = await client.post("/api/rules", json=body, headers=AUTH)
    assert r.status_code == 201, r.text
    rid = r.json()["id"]

    r = await client.post(
        "/api/rules/test",
        json={"text": "Galaxy S25 Ultra de R$ 7.999 por R$ 5.499", "rule_id": rid},
        headers=AUTH,
    )
    res = r.json()
    assert res["price_cents"] == 549900
    assert res["results"][0]["matched"]

    r = await client.post(
        "/api/rules/test",
        json={"text": "capa s25 ultra", "draft": {**body, "name": "rascunho"}},
        headers=AUTH,
    )
    assert r.json()["results"][0]["excluded_by"] == ["capa"]

    assert (await client.delete(f"/api/rules/{rid}", headers=AUTH)).status_code == 204
    assert (await client.get("/api/rules", headers=AUTH)).json() == []


async def test_invalid_regex_rejected(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/rules", json={"name": "x", "include_terms": ["/s2[5/"]}, headers=AUTH
    )
    assert r.status_code == 422


async def test_rule_requires_include(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/rules", json={"name": "x", "include_terms": ["  "]}, headers=AUTH)
    assert r.status_code == 422
