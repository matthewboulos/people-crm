from collections.abc import Iterator
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from people_crm.store import SqliteCrmStore, create_and_initialize_store


@pytest.fixture
def crm_store(tmp_path: Path) -> Iterator[SqliteCrmStore]:
    store = create_and_initialize_store(tmp_path / "crm.db")
    yield store
    store.close()


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def fixed_today() -> date:
    return date(2026, 7, 6)
