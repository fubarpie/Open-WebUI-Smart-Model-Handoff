"""v0.8.0: API key save-back uses the real function id (__id__), not the module name.

Regression: Open WebUI loads functions as modules named 'function_<id>_<32 hex>', so the old
`type(self).__module__` lookup never matched and the key stayed in plaintext.
"""
import asyncio
import json
import sys
import types

import pytest

import model_handoff
from model_handoff import Filter, decrypt_secret, is_encrypted

SECRET = b"unit-test-secret"
REAL_MODULE = "function_model_handoff_" + "a" * 32


def _install_fake(monkeypatch, saved, real_id="model_handoff"):
    class FakeFunctions:
        @staticmethod
        async def get_function_by_id(fid):  # async, like current Open WebUI
            return object() if fid == real_id else None

        @staticmethod
        async def update_function_valves_by_id(fid, valves):
            if fid != real_id:
                return None
            saved.update(fid=fid, valves=valves)
            return valves

    mod = types.ModuleType("open_webui.models.functions")
    mod.Functions = FakeFunctions
    for name in ("open_webui", "open_webui.models"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "open_webui.models.functions", mod)


def _flt(monkeypatch):
    monkeypatch.setenv("WEBUI_SECRET_KEY", "unit-test-secret")
    f = Filter()
    f.valves.api_key = "sk-plain"
    f.valves.encrypt_api_keys = True
    return f


def test_function_id_from_dunder_id(monkeypatch):
    f = _flt(monkeypatch)
    f._fid = "model_handoff"
    assert f._function_id() == "model_handoff"


@pytest.mark.parametrize("fid", ["model_handoff", "my.filter-2", "a_b_c"])
def test_function_id_parsed_from_real_module_name(monkeypatch, fid):
    f = _flt(monkeypatch)
    monkeypatch.setattr(Filter, "__module__", f"function_{fid}_" + "0f" * 16)
    assert f._function_id() == fid


def test_function_id_unknown_for_plain_module(monkeypatch):
    f = _flt(monkeypatch)
    monkeypatch.setattr(Filter, "__module__", "model_handoff")
    assert f._function_id() == ""


def test_save_back_works_with_real_module_name_via_fallback(monkeypatch):
    saved = {}
    _install_fake(monkeypatch, saved)
    monkeypatch.setattr(Filter, "__module__", REAL_MODULE)
    f = _flt(monkeypatch)
    asyncio.run(f._persist_encrypted())
    assert saved["fid"] == "model_handoff"
    assert is_encrypted(saved["valves"]["api_key"])
    assert decrypt_secret(saved["valves"]["api_key"], SECRET) == "sk-plain"
    assert "sk-plain" not in json.dumps(saved["valves"])


def test_save_back_prefers_dunder_id(monkeypatch):
    saved = {}
    _install_fake(monkeypatch, saved, real_id="custom-id")
    monkeypatch.setattr(Filter, "__module__", REAL_MODULE)  # would resolve to the wrong id
    f = _flt(monkeypatch)
    f._fid = "custom-id"
    asyncio.run(f._persist_encrypted())
    assert saved["fid"] == "custom-id" and is_encrypted(saved["valves"]["api_key"])


def test_old_behaviour_would_have_failed(monkeypatch, caplog):
    """Using the module name as the id (the 0.7.0 bug) never matches, so nothing is saved."""
    saved = {}
    _install_fake(monkeypatch, saved)
    monkeypatch.setattr(Filter, "__module__", "something_else")  # unparsable and no __id__
    f = _flt(monkeypatch)
    with caplog.at_level("WARNING", logger="model_handoff"):
        asyncio.run(f._persist_encrypted())
    assert saved == {}
    assert any("could not encrypt" in r.getMessage() for r in caplog.records)
    assert "sk-plain" not in " ".join(r.getMessage() for r in caplog.records)


def test_inlet_records_dunder_id(monkeypatch):
    f = _flt(monkeypatch)
    asyncio.run(f.inlet({"model": "x", "messages": []}, __id__="model_handoff"))
    assert f._fid == "model_handoff"
