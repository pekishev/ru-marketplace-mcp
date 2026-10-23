from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from wb_connector import server, session


@pytest.mark.parametrize(
    ("signals", "expected"),
    [
        ({"account_header": True}, True),
        ({"login_header": True}, False),
        ({"login_form": True}, False),
        ({"account_header": True, "login_form": True}, None),
        ({}, None),
        (None, None),
    ],
)
def test_auth_is_explicit_and_never_inferred_from_transport(signals, expected):
    assert session.session_from_signals(signals)["authenticated"] is expected


def available(**kwargs):
    return {
        "title": "Test oil",
        "seller": "Seller",
        "can_add": True,
        "price": "608 ₽",
        "wallet_price": "608 ₽",
        "wallet_primary": True,
        "delivery_date": "15 ноября",
        "signals": {"account_header": True},
        **kwargs,
    }


def test_wallet_price_is_never_reported_as_regular_price():
    result = session.card_from_snapshot(123, available())
    assert result["price_rub"] is None
    assert result["wallet_price_rub"] == 608
    assert result["in_stock"] is True
    assert result["total_quantity"] is None
    assert result["delivery"]["text"] == "15 ноября"


def test_unavailable_for_browser_address_does_not_inherit_moscow_price():
    result = session.card_from_snapshot(123, available(unavailable=True))
    assert result["availability"] == "unavailable"
    assert result["in_stock"] is False
    assert result["total_quantity"] == 0
    assert result["price_rub"] is result["wallet_price_rub"] is result["delivery"] is None


def test_missing_purchase_block_is_unknown_and_not_a_sellout():
    result = session.card_from_snapshot(123, {})
    assert result["availability"] == "unknown"
    assert result["total_quantity"] is None
    assert result["delivery"] is None
    assert result["session"]["authenticated"] is None


def test_delivery_is_only_the_date_not_address_text():
    result = session.card_from_snapshot(123, available(delivery_date="15 ноября, Secret Street 12"))
    assert result["delivery"] is None


@pytest.mark.asyncio
async def test_browser_mode_skips_region_api_and_cached_prices(monkeypatch):
    read = AsyncMock(return_value=session.card_from_snapshot(123, available()))
    api = AsyncMock(side_effect=AssertionError("region API must not be used"))
    monkeypatch.setattr(session, "browser_read", read)
    monkeypatch.setattr(server, "_safe_get_text", api)
    result = await server.wb_card([123], browser_session=True)
    assert result.dest == "browser"
    assert result.items[0].price_rub is None
    assert result.items[0].wallet_price_rub == 608
    assert result.items[0].session.authenticated is True
    api.assert_not_awaited()


@pytest.mark.asyncio
async def test_browser_mode_has_a_bounded_batch(monkeypatch):
    read = AsyncMock(side_effect=AssertionError("must reject before browser"))
    monkeypatch.setattr(session, "browser_read", read)
    with pytest.raises(Exception, match="at most 10"):
        await server.wb_card(list(range(1, 12)), browser_session=True)
    read.assert_not_awaited()


@pytest.mark.asyncio
async def test_login_tab_is_retained_and_then_reused_after_manual_login(monkeypatch):
    from mcp_core.transport import browser_handoff, chrome_cdp

    monkeypatch.delenv("CHROME_CHALLENGE_HANDOFF_S", raising=False)
    monkeypatch.setattr(chrome_cdp, "HEADLESS", False)
    monkeypatch.setattr(chrome_cdp, "reveal_owned_page", AsyncMock(return_value=True))
    monkeypatch.setattr(chrome_cdp, "_hide_chrome_windows", lambda: None)
    page = SimpleNamespace(
        url=session.ORIGIN + "/lk",
        closed=False,
        evaluate=AsyncMock(
            side_effect=[
                {"signals": {"login_form": True}},
                {"signals": {"account_header": True}},
            ]
        ),
    )
    opens = []

    @asynccontextmanager
    async def open_page(*args, **kwargs):
        opens.append(args)
        try:
            yield page
        finally:
            page.closed = True

    monkeypatch.setattr(chrome_cdp, "open_page", open_page)
    try:
        first = await session.browser_read(scope="test", keep_open=True)
        assert first["session"]["authenticated"] is False
        assert first["session"]["login_tab_retained"] is True
        assert not page.closed
        second = await session.browser_read(scope="test")
        assert second["session"]["authenticated"] is True
        assert second["session"]["login_tab_retained"] is False
        assert len(opens) == 1 and page.closed
    finally:
        await browser_handoff.close_handoffs()


@pytest.mark.asyncio
@pytest.mark.parametrize("nm", [True, 0, -1, "123", "/lk", "https://evil.test"])
async def test_public_browser_reader_accepts_only_positive_numeric_skus(nm):
    with pytest.raises(ValueError):
        await session.browser_read(nm)
