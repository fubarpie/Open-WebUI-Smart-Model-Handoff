# Integration tests for model_handoff.py against a real open_webui install.
#
# These tests require the ``open_webui`` package and a working database.
# They are skipped automatically (module-level pytest.importorskip) when
# open_webui is not installed, and deselected by default via the ``integration``
# marker (see pytest.ini in the repository root).
