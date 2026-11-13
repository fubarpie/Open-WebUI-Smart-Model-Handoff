"""Test 1: load model_handoff.py via Open WebUI's own plugin loader/frontmatter."""

import pytest

from .conftest import maybe_await  # noqa: F401  (ensures env bootstrap ordering)


pytestmark = pytest.mark.integration


def test_loads_via_owui_plugin_loader(filter_module):
    result = filter_module
    # load_function_module_by_id returns (module_instance, type, frontmatter, module)
    assert result, "loader returned nothing"
    if isinstance(result, tuple) and len(result) == 4:
        instance, ftype, frontmatter, module = result
    else:  # defensive: older/newer return shapes
        instance, ftype, frontmatter, module = result, "filter", None, None
    assert instance is not None
    assert callable(getattr(instance, "inlet", None)), "loaded filter has no inlet"
    assert hasattr(instance, "Valves"), "loaded filter has no Valves"
    assert getattr(instance, "valves", None) is not None, "valves not set on instance"
    # frontmatter contract
    fm = frontmatter or {}
    assert fm.get("title") == "Model Handoff", fm.get("title")
    assert str(fm.get("version", "")).count(".") >= 2, "frontmatter version missing/malformed"
    assert fm.get("requirements"), "frontmatter requirements missing"


def test_frontmatter_extractor_sees_metadata(filter_source):
    from open_webui.utils.plugin import extract_frontmatter

    fm = extract_frontmatter(filter_source)
    assert fm.get("title") == "Model Handoff"
    assert "version" in fm and "requirements" in fm
