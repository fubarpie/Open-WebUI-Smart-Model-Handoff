import asyncio
import copy
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from model_handoff import Filter


TARGETS = [
    {"id": "private", "name": "Private specialist", "use_when": "private tasks",
     "avoid_when": "", "hint": ""},
    {"id": "allowed", "name": "Allowed specialist", "use_when": "allowed tasks",
     "avoid_when": "", "hint": ""},
    {"id": "owned", "name": "Owned specialist", "use_when": "owned tasks",
     "avoid_when": "", "hint": ""},
    {"id": "unknown", "name": "Unknown specialist", "use_when": "unknown tasks",
     "avoid_when": "", "hint": ""},
    {"id": "arena:secret", "name": "Arena specialist", "use_when": "arena tasks",
     "avoid_when": "", "hint": ""},
]


def info(model_id, owner="someone-else"):
    return SimpleNamespace(id=model_id, user_id=owner, is_active=True)


@pytest.fixture
def access_modules(monkeypatch):
    calls = {}

    class FakeModels:
        infos = [info("private"), info("allowed"), info("owned", owner="u1")]
        error = None

        @classmethod
        async def get_all_models(cls):
            calls["requested_ids"] = True
            if cls.error:
                raise cls.error
            return list(cls.infos)

    class FakeGroups:
        @staticmethod
        async def get_groups_by_member_id(user_id, include_inherited=False):
            calls["groups"] = (user_id, include_inherited)
            return [SimpleNamespace(id="child"), SimpleNamespace(id="inherited")]

    class FakeAccessGrants:
        accessible = {"allowed"}
        error = None

        @classmethod
        async def get_accessible_resource_ids(cls, **kwargs):
            calls["access"] = kwargs
            if cls.error:
                raise cls.error
            return cls.accessible

    modules = {}
    for name in ("open_webui", "open_webui.models"):
        module = ModuleType(name)
        module.__path__ = []
        modules[name] = module
    for name, attrs in {
        "open_webui.config": {"BYPASS_ADMIN_ACCESS_CONTROL": True},
        "open_webui.env": {"BYPASS_MODEL_ACCESS_CONTROL": False},
        "open_webui.models.access_grants": {"AccessGrants": FakeAccessGrants},
        "open_webui.models.groups": {"Groups": FakeGroups},
        "open_webui.models.models": {"Models": FakeModels},
    }.items():
        module = ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        modules[name] = module
    monkeypatch.setitem(sys.modules, "open_webui.config", modules["open_webui.config"])
    monkeypatch.setitem(sys.modules, "open_webui.env", modules["open_webui.env"])
    for name in ("open_webui", "open_webui.models", "open_webui.models.access_grants",
                 "open_webui.models.groups", "open_webui.models.models"):
        monkeypatch.setitem(sys.modules, name, modules[name])
    return SimpleNamespace(models=FakeModels, groups=FakeGroups, grants=FakeAccessGrants,
                           config=modules["open_webui.config"], env=modules["open_webui.env"],
                           calls=calls)


def run_filter(access_modules, role="user", user_id="u1", include_context=True):
    f = Filter()
    f.valves.enabled = False  # Avoid network/persistence; _accessible_targets is called directly.
    return asyncio.run(f._accessible_targets(copy.deepcopy(TARGETS), {"id": user_id, "role": role}))


def test_restricted_user_gets_only_allowed_owned_and_admin_rules(access_modules):
    allowed = run_filter(access_modules)
    assert [t["id"] for t in allowed] == ["allowed", "owned"]
    assert access_modules.calls["requested_ids"] is True
    assert access_modules.calls["groups"] == ("u1", True)
    assert access_modules.calls["access"] == {
        "user_id": "u1", "resource_type": "model",
        "resource_ids": ["private", "allowed", "owned", "unknown"],
        "permission": "read",
        "user_group_ids": {"child", "inherited"},
    }


def test_admin_bypass_and_unknown_model_rule(access_modules):
    assert [t["id"] for t in run_filter(access_modules, role="admin")] == [
        t["id"] for t in TARGETS if not t["id"].startswith("arena:")
    ]
    access_modules.config.BYPASS_ADMIN_ACCESS_CONTROL = False
    assert [t["id"] for t in run_filter(access_modules, role="admin")] == ["allowed", "owned", "unknown"]


def test_global_model_bypass(access_modules):
    access_modules.env.BYPASS_MODEL_ACCESS_CONTROL = True
    assert [t["id"] for t in run_filter(access_modules)] == [
        t["id"] for t in TARGETS if not t["id"].startswith("arena:")
    ]
    assert "access" not in access_modules.calls


def test_lookup_exception_fails_closed(access_modules, caplog):
    access_modules.grants.error = RuntimeError("db unavailable")
    assert run_filter(access_modules) == []
    assert "target access lookup failed" in caplog.text


async def event_call(event):
    return False


def decision_filter(seen):
    f = Filter()
    f.valves.api_key = "test-key"
    f.valves.decision_backend = "chat"
    f.valves.show_model_card = False
    f.valves.targets_json = json.dumps(TARGETS)

    async def fake_llm(model, messages, json_mode, timeout):
        seen.append(messages[1]["content"])
        return json.dumps({"scores": {"stay": 1.0}, "reason": "stay"})

    f._llm = fake_llm
    return f


def request_kwargs(user=None, model=None):
    kwargs = {
        "__event_call__": event_call,
        "__metadata__": {"chat_id": "permissions-test"},
    }
    if user is not None:
        kwargs["__user__"] = user
    if model is not None:
        kwargs["__model__"] = model
    return kwargs


def test_decision_never_receives_inaccessible_target_names(access_modules):
    seen = []
    f = decision_filter(seen)
    body = {"model": "current", "messages": [{"role": "user", "content": "help"}]}
    original = copy.deepcopy(body)
    kwargs = request_kwargs({"id": "u1", "role": "user"}, {"id": "current"})
    assert asyncio.run(f.inlet(body, **kwargs)) == original
    assert len(seen) == 1
    assert "Allowed specialist" in seen[0]
    assert "Owned specialist" in seen[0]
    for forbidden in ("Private specialist", "Unknown specialist", "Arena specialist"):
        assert forbidden not in seen[0]


def test_permission_failure_skips_decision_and_preserves_body(access_modules):
    access_modules.models.error = RuntimeError("model lookup failed")
    seen = []
    f = decision_filter(seen)
    body = {"model": "current", "messages": [{"role": "user", "content": "help"}]}
    original = copy.deepcopy(body)
    kwargs = request_kwargs({"id": "u1", "role": "user"}, {"id": "current"})
    assert asyncio.run(f.inlet(body, **kwargs)) == original
    assert seen == []


@pytest.mark.parametrize("with_user,with_model", [(False, False), (True, False), (False, True)])
def test_missing_context_preserves_legacy_behavior(access_modules, with_user, with_model):
    seen = []
    f = decision_filter(seen)
    body = {"model": "current", "messages": [{"role": "user", "content": "help"}]}
    kwargs = request_kwargs(
        {"id": "u1", "role": "user"} if with_user else None,
        {"id": "current"} if with_model else None,
    )
    asyncio.run(f.inlet(body, **kwargs))
    assert len(seen) == 1
    assert "Private specialist" in seen[0]
    assert "Arena specialist" in seen[0]
    assert access_modules.calls == {}
