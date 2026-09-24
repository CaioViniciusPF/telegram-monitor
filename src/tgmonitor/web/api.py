"""API de configuração + frontend estático.

Camadas de proteção (a API só escuta em 127.0.0.1, acessada via túnel SSH):
- TrustedHostMiddleware: só Host 127.0.0.1/localhost (bloqueia DNS rebinding).
- X-API-Token obrigatório em /api/* (compare_digest).
- Sem CORS, sem /docs, CSP restritiva no frontend.
- Nenhuma rota de login do Telegram (login só pela CLI).
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from tgmonitor.matching.matcher import CompiledRule, evaluate
from tgmonitor.matching.price import PriceKind
from tgmonitor.models import Alert, Chat, Rule, RuleIn, RuntimeSettings
from tgmonitor.pipeline import Pipeline
from tgmonitor.repository import Repository

STATIC_DIR = Path(__file__).parent / "static"
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]

_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


@dataclass
class AppDeps:
    api_token: str
    repo: Repository
    pipeline: Pipeline
    refresh_chats: Callable[[], Awaitable[int]]
    health: Callable[[], dict[str, Any]]


class MonitoredIn(BaseModel):
    monitored: bool


class TestIn(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    rule_id: int | None = None  # testar uma regra salva
    draft: RuleIn | None = None  # ou uma regra ainda não salva (formulário)


def create_app(deps: AppDeps) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers.update(_SECURITY_HEADERS)
        return response

    expected = deps.api_token.encode()

    async def require_token(
        x_api_token: Annotated[str | None, Header()] = None,
    ) -> None:
        if not x_api_token or not secrets.compare_digest(x_api_token.encode(), expected):
            raise HTTPException(status_code=401, detail="token inválido")

    api = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

    # ------------------------------------------------------------ health

    @api.get("/health")
    async def health() -> dict[str, Any]:
        return deps.health()

    # ------------------------------------------------------------- chats

    @api.get("/chats")
    async def list_chats() -> list[Chat]:
        return await deps.repo.list_chats()

    @api.post("/chats/refresh")
    async def refresh_chats() -> dict[str, int]:
        try:
            count = await deps.refresh_chats()
        except Exception as e:
            raise HTTPException(
                503, f"não foi possível listar os chats ({type(e).__name__})"
            ) from e
        return {"count": count}

    @api.put("/chats/{chat_id}")
    async def set_monitored(chat_id: int, body: MonitoredIn) -> dict[str, bool]:
        if not await deps.repo.set_monitored(chat_id, body.monitored):
            raise HTTPException(404, "chat não encontrado")
        await deps.pipeline.reload()
        return {"monitored": body.monitored}

    # ------------------------------------------------------------- rules

    @api.get("/rules")
    async def list_rules() -> list[Rule]:
        return await deps.repo.list_rules()

    @api.post("/rules", status_code=201)
    async def create_rule(body: RuleIn) -> Rule:
        rule = await deps.repo.create_rule(body)
        await deps.pipeline.reload()
        return rule

    @api.put("/rules/{rule_id}")
    async def update_rule(rule_id: int, body: RuleIn) -> Rule:
        rule = await deps.repo.update_rule(rule_id, body)
        if rule is None:
            raise HTTPException(404, "regra não encontrada")
        await deps.pipeline.reload()
        return rule

    @api.delete("/rules/{rule_id}", status_code=204)
    async def delete_rule(rule_id: int) -> Response:
        if not await deps.repo.delete_rule(rule_id):
            raise HTTPException(404, "regra não encontrada")
        await deps.pipeline.reload()
        return Response(status_code=204)

    @api.post("/rules/test")
    async def test_rule(body: TestIn) -> dict[str, Any]:
        """Avalia um texto colado. Não grava nada e não envia alerta."""
        if body.draft is not None:
            specs = [Rule(id=0, created_at=0, updated_at=0, **body.draft.model_dump()).to_spec()]
        elif body.rule_id is not None:
            rule = await deps.repo.get_rule(body.rule_id)
            if rule is None:
                raise HTTPException(404, "regra não encontrada")
            specs = [rule.to_spec()]
        else:
            specs = [r.to_spec() for r in await deps.repo.list_rules()]
        ev = evaluate(body.text, [CompiledRule.compile(s) for s in specs], explain=True)
        return {
            "price_cents": ev.price_cents,
            "price_candidates": [
                {"raw": c.raw, "cents": c.cents, "kind": PriceKind(c.kind).name.lower()}
                for c in ev.price_candidates
            ],
            "results": [
                {
                    "rule_id": r.rule_id,
                    "rule_name": r.rule_name,
                    "matched": r.matched,
                    "included_by": r.included_by,
                    "excluded_by": r.excluded_by,
                    "price_over_limit": r.price_over_limit,
                    "reason": r.reason,
                }
                for r in ev.results
            ],
        }

    # ------------------------------------------------------ alerts/settings

    @api.get("/alerts")
    async def list_alerts(limit: int = 100) -> list[Alert]:
        return await deps.repo.list_alerts(max(1, min(limit, 500)))

    @api.get("/settings")
    async def get_settings() -> RuntimeSettings:
        return await deps.repo.get_settings()

    @api.put("/settings")
    async def put_settings(body: RuntimeSettings) -> RuntimeSettings:
        await deps.repo.save_settings(body)
        await deps.pipeline.reload()
        return body

    app.include_router(api)

    # ------------------------------------------------------------ frontend

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
