"""Shared fixtures for open_webui-backed integration tests.

Environment (DATA_DIR, DATABASE_URL, WEBUI_SECRET_KEY) must be set BEFORE
``import open_webui``: ``open_webui.env`` reads them at import time. This module
is imported by pytest as the package conftest, which happens before any test
module import, so importing it first from the test modules keeps the ordering.
"""

import asyncio
import os
import pathlib
import sys
import uuid

# ---------------------------------------------------------------------------
# Env bootstrap: MUST run before any open_webui import anywhere in the process.
# ---------------------------------------------------------------------------
_HERE = pathlib.Path(__file__).resolve().parent
_DATA_DIR = os.environ.setdefault(
    "OWUI_TEST_DATA_DIR",
    str(_HERE / ".data" / uuid.uuid4().hex[:8]),
)
pathlib.Path(_DATA_DIR).mkdir(parents=True, exist_ok=True)
os.environ["DATA_DIR"] = _DATA_DIR
os.environ["DATABASE_URL"] = os.environ.get("DATABASE_URL", f"sqlite:///{_DATA_DIR}/webui.db")
os.environ.setdefault("WEBUI_SECRET_KEY", "test-secret")
os.environ.setdefault("WEBUI_AUTH", "False")
os.environ.setdefault("OFFLINE_MODE", "True")
os.environ.setdefault("ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS", "False")
# Keep the loader from trying to pip-install frontmatter requirements.
os.environ.setdefault("ENABLE_PIP_INSTALL", "False")
# 0.12.5+/0.13.0: config.py requires a frontend build dir containing static/;
# 0.13.0 also raises at import when it is missing. Point it at a scratch build.
_frontend = pathlib.Path(_DATA_DIR) / "frontend"
(_frontend / "static").mkdir(parents=True, exist_ok=True)
os.environ.setdefault("FRONTEND_BUILD_DIR", str(_frontend))
sys.path.insert(0, str(_HERE.parent.parent))  # worktree root (model_handoff.py)

import pytest  # noqa: E402

pytest.importorskip("open_webui")

# Create the schema before anything touches the models.
from .migrate import run_migrations  # noqa: E402

run_migrations()

import open_webui.env as env  # noqa: E402

env.DATABASE_URL = os.environ["DATABASE_URL"]
if not getattr(env, "SECRET_KEY", None):
    env.SECRET_KEY = os.environ["WEBUI_SECRET_KEY"]

try:  # 0.12.x ships a sync-wrappers helper; 0.13.x dropped it
    from open_webui.internal.wrappers import sync_wrappers  # noqa: E402

    sync_wrappers()
except Exception:
    pass

async def maybe_await(value):
    """Await open_webui model-layer results that may be coroutines (0.12) or
    plain values (0.13 sync wrappers)."""
    if asyncio.iscoroutine(value) or asyncio.isfuture(value):
        return await value
    return value


ADMIN_USER_ID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture(scope="session")
def filter_source():
    src = _HERE.parent.parent / "model_handoff.py"
    assert src.is_file(), f"model_handoff.py not found at {src}"
    return src.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def function_id():
    return "model_handoff_integration_test_function"

# Pull the DB-backed fixtures into the conftest namespace so pytest sees them.
from .db import filter_module, function_row  # noqa: E402,F401
