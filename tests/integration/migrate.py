"""DB bootstrap: run open_webui's alembic migrations against the test database."""

import pathlib

import alembic.config
import alembic.command


def run_migrations():
    pkg = pathlib.Path(__import__("open_webui", fromlist=["x"]).__file__).parent
    cfg = alembic.config.Config(str(pkg / "alembic.ini"))
    cfg.set_main_option("script_location", str(pkg / "migrations"))
    # DATABASE_URL was already consumed by open_webui.env; env.py reads it from
    # the open_webui.config/engine module, so reuse the live engine URL.
    from open_webui.internal import db

    url = str(db.engine.url)
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    alembic.command.upgrade(cfg, "head")
