"""Read WB login and delivery from the selected browser context, not Moscow API defaults."""

from __future__ import annotations

import asyncio
import datetime
import re
from typing import Any

from mcp_core import resilience as R
from mcp_core.transport import chrome_cdp
from mcp_core.transport.browser_handoff import has_pending_handoff, read_with_handoff

ORIGIN = "https://www.wildberries.ru"
HOSTS = frozenset({"www.wildberries.ru", "wildberries.ru"})
_DATE = re.compile(
    r"сегодня|послезавтра|завтра|\d{1,2}(?:\s*[-–—]\s*\d{1,2})?\s+(?:янв(?:аря)?|фев(?:раля)?|мар(?:та)?|апр(?:еля)?|мая|июн[ья]?|июл[ья]?|авг(?:уста)?|сен(?:тября)?|окт(?:ября)?|ноя(?:бря)?|дек(?:абря)?)\.?",
    re.IGNORECASE,
)

# Text is read only from this SKU's main purchase block. In particular, neither
# recommendation prices, review dates, wallet balance nor address text is read.
_READ = r"""async ({nm}) => {
  const deadline=Date.now()+4000;
  let root=null;
  if(nm) {
    do {
      root=document.querySelector('[data-testid="'+nm+'"]');
      if(root?.querySelector('[class*="priceBlock--"]'))break;
      await new Promise(r=>setTimeout(r,100));
    } while(Date.now()<deadline);
  }
  const visible=e=>!!e && !!(e.getClientRects().length);
  const header=document.querySelector('header');
  const profile=header?.querySelector('a[href="/lk"],a[href="/lk/"]');
  const headerText=profile?.textContent||'';
  const loginForm=[...document.querySelectorAll('input[type="tel"],input[autocomplete="tel"]')].some(visible);
  const loginHeader=/войти|вход/i.test(headerText);
  const wallet=!!header?.querySelector('a.header__balance--wallet[href="/lk/mywallet"]');
  const signals={login_form:loginForm,login_header:loginHeader,account_header:wallet&&!!profile};
  if(!nm)return {signals};
  const block=root?.querySelector('[class*="priceBlock--"]');
  const unavailable=!!root?.querySelector('[data-testid="price-out-of-stock"]');
  const add=root?.querySelector('[data-testid="add-to-basket-button"]');
  const walletWrap=!!block?.querySelector('[class*="priceBlockPriceWrapWallet--"]');
  const price=block?.querySelector('[class*="priceBlockPrice--"]')?.textContent||'';
  const walletPrice=block?.querySelector('[class*="priceBlockWalletPrice--"]')?.textContent||'';
  const delivery=root?.querySelector('[class*="productPageDelivery--"] [class*="deliveryTitleWrapper--"]');
  const deliveryText=delivery?.textContent||'';
  const match=deliveryText.match(/сегодня|послезавтра|завтра|\d{1,2}(?:\s*[-–—]\s*\d{1,2})?\s+(?:янв(?:аря)?|фев(?:раля)?|мар(?:та)?|апр(?:еля)?|мая|июн[ья]?|июл[ья]?|авг(?:уста)?|сен(?:тября)?|окт(?:ября)?|ноя(?:бря)?|дек(?:абря)?)\.?/i);
  const seller=root?.querySelector('[data-testid="product-seller-link"]');
  return {signals,root_present:!!root,price_block_present:!!block,unavailable,
    can_add:!!add&&!add.disabled,price,wallet_price:walletPrice,wallet_primary:walletWrap,
    delivery_date:match?match[0]:null,
    title:root?.querySelector('[data-testid="product-line-name"]')?.textContent?.trim().slice(0,350)||'',
    seller:(seller?.querySelector('span')?.textContent||seller?.textContent||'').trim().slice(0,150)};
}"""


def session_from_signals(signals: Any) -> dict[str, Any]:
    signals = signals if isinstance(signals, dict) else {}
    login = signals.get("login_form") is True or signals.get("login_header") is True
    account = signals.get("account_header") is True
    authenticated = False if login and not account else True if account and not login else None
    return {
        "authenticated": authenticated,
        "status": "authenticated" if authenticated is True else "logged_out" if authenticated is False else "unknown",
        "evidence": "account_header"
        if authenticated is True
        else "login_control"
        if authenticated is False
        else "no_unambiguous_signal",
        "source": "chrome_cdp",
        "checked_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }


def card_from_snapshot(nm: int, data: dict[str, Any]) -> dict[str, Any]:
    unavailable = data.get("unavailable") is True
    available = data.get("can_add") is True and not unavailable
    price = R.coerce_price(data.get("price")) if not unavailable else None
    wallet_price = R.coerce_price(data.get("wallet_price")) if not unavailable else None
    # A wallet price must never masquerade as the ordinary price.
    if data.get("wallet_primary") is True:
        wallet_price = wallet_price or price
        price = None
    date = data.get("delivery_date")
    match = _DATE.fullmatch(date.strip()) if isinstance(date, str) else None
    return {
        "nm_id": nm,
        "name": data.get("title") or "",
        "supplier": data.get("seller") or "",
        "price_rub": price,
        "wallet_price_rub": wallet_price,
        "price_context": "browser_delivery_context",
        "availability": "unavailable" if unavailable else "available" if available else "unknown",
        "in_stock": available and (price is not None or wallet_price is not None),
        "total_quantity": 0 if unavailable else None,
        "delivery": {"text": match.group(0), "source": "product_purchase_block", "scope": "browser_delivery_context"}
        if match and not unavailable
        else None,
        "session": session_from_signals(data.get("signals")),
    }


async def browser_read(nm: int | None = None, *, scope: str | None = None, keep_open: bool = False) -> dict[str, Any]:
    if nm is not None and (isinstance(nm, bool) or not isinstance(nm, int) or nm <= 0):
        raise ValueError("nm must be a positive integer")
    if keep_open and not scope:
        raise ValueError("retaining a login tab requires an active MCP session")
    url = ORIGIN + (f"/catalog/{nm}/detail.aspx" if nm is not None else "/lk")

    async def read(page: Any) -> dict[str, Any]:
        chrome_cdp._check_final_host(await chrome_cdp.current_page_url(page), HOSTS)
        data = await asyncio.wait_for(page.evaluate(_READ, {"nm": nm}), timeout=15)
        chrome_cdp._check_final_host(await chrome_cdp.current_page_url(page), HOSTS)
        if not isinstance(data, dict):
            raise ValueError("WB browser extractor returned non-object data")
        return (
            card_from_snapshot(nm, data) if nm is not None else {"session": session_from_signals(data.get("signals"))}
        )

    if scope:
        result, expires = await read_with_handoff(
            url=url,
            wait_ms=5000,
            scope=scope,
            operation="wb_session",
            read=read,
            challenge=lambda data: "login" if keep_open and data["session"]["authenticated"] is not True else None,
            allowed_hosts=HOSTS,
            retention_s=300 if keep_open or has_pending_handoff(scope=scope, operation="wb_session", url=url) else 0,
        )
        result["session"].update(login_tab_retained=expires is not None, handoff_expires_at=expires)
        return result
    async with chrome_cdp.open_page(url, wait_ms=5000, allowed_hosts=HOSTS) as page:
        return await read(page)
