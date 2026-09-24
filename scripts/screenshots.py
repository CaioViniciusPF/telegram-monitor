"""Gera as 4 imagens do README com dados fictícios (não precisa de Telegram).

Uso:
    pip install -e ".[screenshots]"
    python -m playwright install --with-deps chromium   # no Linux/WSL pode pedir sudo
    python scripts/screenshots.py                       # grava em docs/screenshots/
"""

from __future__ import annotations

import argparse
import asyncio
import io
import tempfile
import time
from pathlib import Path

import uvicorn
from PIL import Image
from playwright.async_api import BrowserContext, Page, async_playwright

from tgmonitor import db
from tgmonitor.dedup import fingerprints
from tgmonitor.models import MatchedRuleRef, RuleIn
from tgmonitor.pipeline import Pipeline
from tgmonitor.repository import Repository
from tgmonitor.service import _Server
from tgmonitor.web.api import AppDeps, create_app

TOKEN = "demo-" + "x" * 40
PORT = 8799
ROOT = Path(__file__).resolve().parents[1]

PROMO = (
    "🔥 Galaxy S25 Ultra 256GB Titânio\n"
    "de R$ 7.999 por R$ 5.499, com cupom: R$ 5.299\n"
    "ou 12x de R$ 458 sem juros\n"
    "🚚 Frete R$ 19,90 | Carregador de brinde\n"
    "https://amzn.to/exemplo"
)


class NullSender:
    async def send_alert(self, alert: object) -> bool:
        return True


async def seed(repo: Repository) -> Pipeline:
    await repo.upsert_chats(
        [
            (-1001000000001, "Promoções Tech", "promotech", "channel"),
            (-1001000000002, "Achadinhos BR", None, "megagroup"),
            (-1001000000003, "Ofertas Samsung Oficial", "samsungofertas", "channel"),
            (-1001000000004, "Cupons e Descontos", "cuponsdescontos", "channel"),
            (-1001000000005, "Família", None, "megagroup"),
            (-100200000, "Futebol de quinta", None, "group"),
        ]
    )
    for cid in (-1001000000001, -1001000000002, -1001000000003):
        await repo.set_monitored(cid, True)

    s25 = await repo.create_rule(
        RuleIn(
            name="Galaxy S25/S26 Plus ou Ultra",
            include_terms=[r"/s2[56]\s*(\+|plus|ultra)/"],
            exclude_terms=["capa", "capinha", "película", "fe", "usado"],
            max_price_cents=550000,
        )
    )
    await repo.create_rule(
        RuleIn(name="Fone Galaxy Buds", include_terms=["galaxy buds", "buds3 pro"], enabled=False)
    )
    await repo.create_rule(
        RuleIn(
            name="Monitor 4K",
            include_terms=["monitor 4k", '/\\b27"?\\s*4k/'],
            max_price_cents=180000,
        )
    )

    now = int(time.time())
    samples = [
        (
            -1001000000001,
            "Promoções Tech",
            101,
            "Galaxy S25 Ultra 256GB de R$ 7.999 por R$ 5.299 no Pix. Link na bio.",
            529900,
            True,
            25 * 60,
        ),
        (
            -1001000000002,
            "Achadinhos BR",
            55,
            "S26+ 512GB lançamento, preço só no app!\n"
            "Corre que acaba rápido, estoque limitado nas cores azul e prata.\n"
            "Aproveite também o cashback exclusivo para clientes do cartão.",
            None,
            False,
            3 * 3600,
        ),
        (
            -1001000000003,
            "Ofertas Samsung Oficial",
            9,
            "Galaxy S25+ 256GB por R$ 4.899 à vista",
            489900,
            True,
            27 * 3600,
        ),
    ]
    for cid, title, mid, text, price, delivered, ago in samples:
        aid = await repo.insert_alert(
            chat_id=cid,
            chat_title=title,
            message_id=mid,
            link=f"https://t.me/c/{-cid - 1_000_000_000_000}/{mid}",
            text=text,
            price_cents=price,
            matched=[MatchedRuleRef(id=s25.id, name=s25.name)],
            is_edit=mid == 9,
            fps=fingerprints(text, [s25.id], price),
            created_at=now - ago,
        )
        if delivered and aid:
            await repo.mark_delivered(aid)

    # Post editado cujo preço baixou (aparece com "Baixou de R$ ..." no histórico).
    drop_text = "ATUALIZADO: Galaxy S25 Ultra 512GB baixou para R$ 5.199 no Pix"
    drop_id = await repo.insert_alert(
        chat_id=-1001000000001, chat_title="Promoções Tech", message_id=140,
        link="https://t.me/promotech/140", text="Galaxy S25 Ultra 512GB por R$ 5.699 no Pix",
        price_cents=569900, matched=[MatchedRuleRef(id=s25.id, name=s25.name)],
        is_edit=False, fps=[], created_at=now - 90 * 60,
    )  # fmt: skip
    assert drop_id is not None
    await repo.record_price_drop(
        drop_id, text=drop_text, price_cents=519900, previous_price_cents=569900,
        matched=[MatchedRuleRef(id=s25.id, name=s25.name)],
        fps=fingerprints(drop_text, [s25.id], 519900), created_at=now - 5 * 60,
    )  # fmt: skip
    await repo.mark_delivered(drop_id)

    p = Pipeline(repo, NullSender())
    await p.reload()
    return p


def health() -> dict[str, object]:
    return {
        "listener": {"state": "connected", "healthy": True, "total_failures": 0},
        "paused": False,
        "active_rules": 2,
        "monitored_chats": 3,
        "last_message_at": time.time() - 120,
        "stats": {"seen": 412, "alerts": 3, "dedup": 7},
        "uptime_s": 86400 * 2,
        "heartbeat_enabled": True,
    }


async def login(page: Page) -> None:
    await page.goto(f"http://127.0.0.1:{PORT}/")
    await page.fill("#token-input", TOKEN)
    await page.click("#token-form button[type=submit]")
    await page.wait_for_selector("#chat-list li")


def optimize_png(data: bytes) -> bytes:
    """Reduz para uma paleta de 256 cores (a interface tem poucas cores; a perda é invisível)."""
    img = Image.open(io.BytesIO(data)).convert("RGB")
    buf = io.BytesIO()
    img.quantize(colors=256, method=Image.Quantize.MEDIANCUT).save(buf, "PNG", optimize=True)
    return buf.getvalue()


async def shoot(page: Page, out: Path, name: str) -> None:
    await page.wait_for_timeout(250)
    raw = await page.screenshot(full_page=True)
    data = optimize_png(raw)
    path = out / f"{name}.png"
    await asyncio.to_thread(path.write_bytes, data)
    print(f"   {path.name}: {len(raw) // 1024} KB -> {len(data) // 1024} KB")


async def run(out: Path) -> None:
    tmp = Path(tempfile.mkdtemp())
    conn = await db.connect(tmp / "demo.db")
    repo = Repository(conn)
    pipeline = await seed(repo)

    async def refresh() -> int:
        return len(await repo.list_chats())

    app = create_app(AppDeps(TOKEN, repo, pipeline, refresh, health))
    server = _Server(
        uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning", lifespan="off")
    )
    task = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110 - uvicorn não expõe um Event
        await asyncio.sleep(0.05)

    desktop = {"width": 1200, "height": 900}
    mobile = {"width": 390, "height": 844}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()

        async def open_page(scheme: str, viewport: dict[str, int]) -> tuple[BrowserContext, Page]:
            ctx = await browser.new_context(
                color_scheme=scheme, viewport=viewport, locale="pt-BR",
                timezone_id="America/Sao_Paulo",
            )  # fmt: skip
            page = await ctx.new_page()
            await login(page)
            return ctx, page

        async def test_tab(page: Page) -> None:
            await page.click("[data-tab=test]")
            await page.fill("#test-text", PROMO)
            await page.click("#test-run")
            await page.wait_for_selector(".result")

        # As 4 imagens usadas no README.
        ctx, page = await open_page("light", desktop)
        await test_tab(page)
        await shoot(page, out, "3-testar-light")
        await page.click("[data-tab=rules]")
        await page.wait_for_selector("#rule-list li")
        await shoot(page, out, "2-regras-light")
        await ctx.close()

        ctx, page = await open_page("dark", desktop)
        await page.click("[data-tab=alerts]")
        await page.wait_for_selector("#alert-list li")
        await shoot(page, out, "4-historico-dark")
        await ctx.close()

        ctx, page = await open_page("light", mobile)
        await test_tab(page)
        await shoot(page, out, "3-testar-light-mobile")
        await ctx.close()

        await browser.close()

    server.should_exit = True
    await task
    await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "screenshots")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    asyncio.run(run(args.out))


if __name__ == "__main__":
    main()
