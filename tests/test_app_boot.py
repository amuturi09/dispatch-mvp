"""
Smoke test: the FastAPI app module must import cleanly.

Importing main.py exercises the whole package layout (core.engine,
integrations.*, db.*) and the module-level app wiring. It also guards a
specific regression: in dev mode (no ADMIN_AUTH_TOKEN) the no-op
`require_admin` must stay a bare callable, not a Depends(...) object --
otherwise the routes build Depends(Depends(...)) and FastAPI raises at import.
"""

import importlib
import sys

import pytest
from fastapi import FastAPI


@pytest.fixture
def fresh_main(monkeypatch):
    # In-memory DB so importing main leaves no file behind; dev mode (no token).
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("ADMIN_AUTH_TOKEN", raising=False)
    sys.modules.pop("main", None)
    module = importlib.import_module("main")
    yield module
    sys.modules.pop("main", None)


def test_app_imports_in_dev_mode(fresh_main):
    assert isinstance(fresh_main.app, FastAPI)
    assert len(fresh_main.app.routes) > 0


def test_dev_mode_require_admin_is_callable(fresh_main):
    # Regression guard: must be callable, not a Depends(...) instance.
    from fastapi.params import Depends as DependsClass
    assert callable(fresh_main.require_admin)
    assert not isinstance(fresh_main.require_admin, DependsClass)
