"""Keep legacy behavior tests on an explicit fixture, never the runtime default."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def explicit_shop_fixture(monkeypatch):
    monkeypatch.setenv("WANG_SHOP_FILE", str(Path(__file__).parents[1] / "wang/shops/demo.json"))
