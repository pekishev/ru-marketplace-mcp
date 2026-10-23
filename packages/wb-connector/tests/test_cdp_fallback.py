import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from wb_connector import server


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://card.wb.ru/cards/v4/detail?nm=123", "/__internal/u-card/cards/v4/detail?nm=123"),
        (
            "https://search.wb.ru/exactmatch/ru/common/v9/search?query=oil",
            "/__internal/u-search/exactmatch/ru/common/v18/search?query=oil",
        ),
        ("https://evil.test/cards/v4/detail", None),
        ("https://card.wb.ru.evil.test/cards/v4/detail", None),
        ("https://card.wb.ru/personal/orders", None),
        ("https://catalog.wb.ru/catalog/oils/catalog", None),
    ],
)
def test_cdp_fallback_only_maps_known_catalog_routes(url, expected):
    assert server._cdp_route(url) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled", "http_success", "expected_calls"),
    [
        (True, False, 1),
        (False, False, 0),
        (True, True, 0),
    ],
)
async def test_cdp_is_only_a_last_resort_and_success_is_cached(monkeypatch, enabled, http_success, expected_calls):
    server._cache.clear()
    monkeypatch.setattr(server, "_cdp_fallback", enabled)
    monkeypatch.setattr(
        server,
        "_impersonated_get_text",
        AsyncMock(return_value=(200, '{"products":[]}', None) if http_success else (403, "blocked", None)),
    )
    monkeypatch.setattr(server, "_budgeted_get_text", AsyncMock(return_value=(403, "blocked", None)))
    cdp = AsyncMock(return_value=(200, '{"products":[{"id":123}]}', None))
    monkeypatch.setattr(server, "_cdp_get_text", cdp)
    url = "https://card.wb.ru/cards/v4/detail?nm=123"
    try:
        result = await server._safe_get_text(None, url)
        assert cdp.await_count == expected_calls
        if enabled or http_success:
            assert result[0] == 200
            assert await server._safe_get_text(None, url) == result
            assert cdp.await_count == expected_calls
        else:
            assert result[0] == 403
    finally:
        server._cache.clear()


@pytest.mark.asyncio
async def test_stale_edge_token_opens_a_fresh_tab_and_closes_both(monkeypatch):
    from mcp_core.transport import chrome_cdp

    page = SimpleNamespace(
        evaluate=AsyncMock(
            side_effect=[
                json.dumps({"status": 498, "text": "expired"}),
                json.dumps({"status": 200, "text": '{"products":[]}'}),
            ]
        )
    )
    opened = []
    closed = []

    @asynccontextmanager
    async def open_page(*args, **kwargs):
        opened.append(args)
        try:
            yield page
        finally:
            closed.append(True)

    monkeypatch.setattr(chrome_cdp, "open_page", open_page)
    assert await server._cdp_get_text("https://card.wb.ru/cards/v4/detail?nm=123") == (200, '{"products":[]}', None)
    assert len(opened) == len(closed) == 2
