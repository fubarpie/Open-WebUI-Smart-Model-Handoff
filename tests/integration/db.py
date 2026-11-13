"""Function-row fixtures for the real Open WebUI plugin loader."""

import asyncio
import pytest

from .conftest import ADMIN_USER_ID, maybe_await


async def _insert_function(function_id: str, filter_source: str):
    """Insert the filter source as a Functions row; tolerant of 0.12/0.13 drift."""
    from open_webui.models.functions import Functions

    try:
        from open_webui.models.functions import FunctionForm, FunctionMeta

        form = FunctionForm(
            id=function_id,
            name="Model Handoff",
            type="filter",
            content=filter_source,
            meta=FunctionMeta(),
        )
        res = await maybe_await(Functions.insert_new_function(ADMIN_USER_ID, "filter", form))
        if res is None:
            raise RuntimeError("insert_new_function returned None")
        return
    except Exception:
        pass
    # Fallback: 0.12.5-style dict form
    Functions.insert_new_function(
        ADMIN_USER_ID,
        "filter",
        {"id": function_id, "name": "Model Handoff", "content": filter_source},
    )


async def _delete_function(function_id: str):
    from open_webui.models.functions import Functions

    try:
        await maybe_await(Functions.delete_function_by_id(function_id))
    except Exception:
        try:
            Functions.delete_function_by_id(function_id)
        except Exception:
            pass


@pytest.fixture(scope="session")
def function_row(function_id, filter_source):
    asyncio.run(_insert_function(function_id, filter_source))
    yield function_id
    asyncio.run(_delete_function(function_id))


@pytest.fixture(scope="session")
def filter_module(function_row):
    """Load model_handoff.py through Open WebUI's own plugin loader."""
    import asyncio

    from open_webui.utils.plugin import load_function_module_by_id

    result = asyncio.run(load_function_module_by_id(function_row))
    module_instance, ftype, frontmatter, module = result
    return module_instance, ftype, frontmatter, module
