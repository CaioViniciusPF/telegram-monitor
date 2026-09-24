from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tgmonitor import db
from tgmonitor.repository import Repository


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[Repository]:
    conn = await db.connect(tmp_path / "test.db")
    try:
        yield Repository(conn)
    finally:
        await conn.close()
