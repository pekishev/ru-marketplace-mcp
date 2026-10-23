"""Marks this directory as its own pytest rootdir package.

Several connectors have a ``test_server.py``; without a conftest per directory
pytest cannot tell the identically-named modules apart during collection.
"""

import pytest


@pytest.fixture(autouse=True)
def _no_cdp_fallback(request, monkeypatch):
    """Offline tests must never drive the operator's Chrome when a mocked host refuses."""

    if request.node.get_closest_marker("live") is None:
        monkeypatch.setattr("wb_connector.server._cdp_fallback", False)
