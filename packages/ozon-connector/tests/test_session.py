import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from ozon_connector import server, session


def profile(caption, link="/my/main"):
    return {
        "widgetStates": {
            "profileLogo-1": json.dumps({"iconHeader": {"subtitle": {"text": caption}, "action": {"link": link}}})
        }
    }


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (profile("Test buyer"), True),
        (profile("Войти"), False),
        (profile("Войти в Ozon"), False),
        (profile("Кабинет"), None),
        (profile("Test buyer", "/other"), None),
        ({"widgetStates": {"profileLogo-1": "not json"}}, None),
        ({"widgetStates": {"profileLogo-1": "[]"}}, None),
        ({"widgetStates": []}, None),
        ({}, None),
    ],
)
def test_auth_requires_profile_proof_and_redacts_identity(payload, expected):
    parsed = session.profile_session(payload)
    assert parsed["authenticated"] is expected
    assert "Test buyer" not in json.dumps(parsed)


def test_conflicting_profile_evidence_is_unknown():
    p = profile("Test buyer")
    p["widgetStates"]["profileLogo-2"] = profile("Войти")["widgetStates"]["profileLogo-1"]
    assert session.profile_session(p)["authenticated"] is None


def test_delivery_drops_private_address_and_keeps_display_date():
    assert (
        session.delivery_from_labels(["В корзину\nДоставим 5 ноября, улица Secret 12"])["text"] == "Доставим 5 ноября"
    )
    assert session.delivery_from_labels(["В корзину\nпослезавтра"])["text"] == "послезавтра"
    assert session.delivery_from_labels(["Нет в наличии"]) is None
    assert session.delivery_from_labels(None) is None


@pytest.mark.asyncio
async def test_live_card_bypasses_anonymous_transport_and_cache(monkeypatch):
    payload = profile("Test buyer")
    payload["widgetStates"]["webPrice-1"] = {"price": "553 ₽", "cardPrice": "536 ₽"}
    live = {
        "status": 200,
        "payload": payload,
        "session": session.profile_session(payload),
        "delivery": session.delivery_from_labels(["Доставим завтра"]),
    }
    browser = AsyncMock(return_value=live)
    default = AsyncMock(side_effect=AssertionError("anonymous/cached path must not run"))
    monkeypatch.setattr(server, "browser_payload", browser)
    monkeypatch.setattr(server, "_fetch_composer", default)
    server._cache.set("/product/123/", (200, '{"stale":true}'))
    result = await server.ozon_card("123", browser_session=True)
    assert result.session.authenticated is True
    assert result.delivery.text == "Доставим завтра"
    assert result.price == 553
    assert result.tier_used == "cdp_session"
    default.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_card_never_claims_checked_login(monkeypatch):
    monkeypatch.setattr(
        server, "_fetch_composer", AsyncMock(return_value=(200, json.dumps(profile("Test buyer")), "cache"))
    )
    result = await server.ozon_card("123")
    assert result.session is None
    assert result.delivery is None


@pytest.mark.asyncio
async def test_browser_search_is_uncached_and_includes_auth(monkeypatch):
    payload = profile("Войти")
    monkeypatch.setattr(
        server,
        "browser_payload",
        AsyncMock(return_value={"status": 200, "payload": payload, "session": session.profile_session(payload)}),
    )
    monkeypatch.setattr(server, "_fetch_composer", AsyncMock(side_effect=AssertionError("cached path")))
    result = await server.ozon_search("oil", browser_session=True)
    assert result.session.authenticated is False
    assert result.tier_used == "cdp_session"


@pytest.mark.asyncio
async def test_session_clears_old_cache(monkeypatch):
    server._cache.set("/product/123/", (200, "old"))
    monkeypatch.setattr(
        server,
        "browser_payload",
        AsyncMock(return_value={"status": 200, "session": session.profile_session(profile("Test buyer"))}),
    )
    assert (await server.ozon_session()).session.authenticated is True
    assert server._cache.get("/product/123/") is None


@pytest.mark.asyncio
async def test_login_retains_exact_page_then_status_resumes_and_closes(monkeypatch):
    from mcp_core.transport import browser_handoff, chrome_cdp

    monkeypatch.delenv("CHROME_CHALLENGE_HANDOFF_S", raising=False)
    monkeypatch.setattr(chrome_cdp, "HEADLESS", False)
    monkeypatch.setattr(chrome_cdp, "reveal_owned_page", AsyncMock(return_value=True))
    monkeypatch.setattr(chrome_cdp, "_hide_chrome_windows", lambda: None)
    page = SimpleNamespace(
        url="https://www.ozon.ru/",
        closed=False,
        evaluate=AsyncMock(
            side_effect=[
                {"status": 200, "payload": profile("Войти"), "labels": []},
                {"status": 200, "payload": profile("Test buyer"), "labels": []},
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
        first = await session.browser_payload("/", cap=1000, timeout=1, scope="test", keep_open=True)
        assert first["session"]["authenticated"] is False
        assert first["session"]["login_tab_retained"] is True
        assert not page.closed
        second = await session.browser_payload("/", cap=1000, timeout=1, scope="test")
        assert second["session"]["authenticated"] is True
        assert second["session"]["login_tab_retained"] is False
        assert len(opens) == 1 and page.closed
    finally:
        await browser_handoff.close_handoffs()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/my/orders", "https://evil.test/", "/product/1/?url=evil"])
async def test_browser_paths_do_not_allow_private_routes(path):
    with pytest.raises(ValueError):
        await session.browser_payload(path, cap=1000, timeout=1)
