"""Live Ozon browser reads; never expose names, cookies, tokens or addresses."""

from __future__ import annotations

import asyncio
import datetime
import json
import re
import urllib.parse
from typing import Any

from mcp_core.transport import chrome_cdp
from mcp_core.transport.browser_handoff import has_pending_handoff, read_with_handoff

HOSTS = frozenset({"www.ozon.ru", "ozon.ru"})
_GENERIC_CAPTIONS = {"кабинет", "профиль", "личный кабинет", "ozon", ""}
_DELIVERY_RE = re.compile(
    r"(?:доставим\s+)?(?:сегодня|послезавтра|завтра|(?:с\s+)?\d{1,2}(?:\s*[-–—]\s*\d{1,2})?\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря))",
    re.IGNORECASE,
)


def profile_session(payload: dict[str, Any]) -> dict[str, Any]:
    """Only classify the site's profile widget, never recommendation/review text.

    A personalized caption AND account redirect are required for positive proof.
    An absent/generic widget is unknown, not a logged-out verdict.
    """
    signals: set[bool] = set()
    widgets = payload.get("widgetStates")
    for key, raw in widgets.items() if isinstance(widgets, dict) else []:
        if not key.startswith("profileLogo-"):
            continue
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            continue
        header = data.get("iconHeader") if isinstance(data, dict) else None
        if not isinstance(header, dict):
            continue
        subtitle, action = header.get("subtitle"), header.get("action")
        caption = subtitle.get("text") if isinstance(subtitle, dict) else None
        caption = caption.strip().casefold() if isinstance(caption, str) else ""
        link = action.get("link") if isinstance(action, dict) else None
        if re.match(r"^(?:войти|вход|sign in|log in)\b", caption):
            signals.add(False)
        elif caption not in _GENERIC_CAPTIONS and link in {"/my/main", "/my/main/"}:
            signals.add(True)
    authenticated = next(iter(signals)) if len(signals) == 1 else None
    return {
        "authenticated": authenticated,
        "status": "authenticated" if authenticated is True else "logged_out" if authenticated is False else "unknown",
        "evidence": "profile_widget" if authenticated is not None else "no_unambiguous_profile_signal",
        "source": "chrome_cdp",
        "checked_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }


def delivery_from_labels(labels: Any) -> dict[str, str] | None:
    if not isinstance(labels, list):
        return None
    for label in labels:
        if isinstance(label, str):
            for line in label.splitlines():
                # Read dates from the purchase widget only. Never return a
                # delivery address, warehouse location or unrelated ETA in ads.
                line = line.strip()
                match = _DELIVERY_RE.search(line)
                if match:
                    return {"text": match.group(0), "source": "webAddToCart", "scope": "browser_delivery_context"}
    return None


async def browser_payload(
    path: str,
    *,
    cap: int,
    timeout: float,
    scope: str | None = None,
    keep_open: bool = False,
) -> dict[str, Any]:
    """Fetch composer in Chrome and read the purchase widget after hydration.

    Callers must canonicalize product/search paths. The root is used only by
    ozon_session. This mode deliberately bypasses both anonymous HTTP and cache.
    """
    if path != "/" and not (re.fullmatch(r"/product/\d+/", path) or path.startswith("/search/?")):
        raise ValueError("unsupported browser path")
    api_url = "https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=" + urllib.parse.quote(path, safe="/")

    async def read(page: Any) -> dict[str, Any]:
        chrome_cdp._check_final_host(await chrome_cdp.current_page_url(page), HOSTS)
        result = await asyncio.wait_for(
            page.evaluate(
                r"""async ({url,cap}) => {
                  const res = await fetch(url, {credentials:'include', headers:{'Accept':'application/json'}});
                  const reader=res.body.getReader(); const chunks=[]; let total=0;
                  while(true) {
                    const {done,value}=await reader.read(); if(done) break;
                    total+=value.length; if(total>cap) {await reader.cancel(); throw new Error('BODY_CAP_EXCEEDED');}
                    chunks.push(value);
                  }
                  const buf=new Uint8Array(total);let off=0;
                  for(const c of chunks){buf.set(c,off);off+=c.length;}
                  let payload=null;try{payload=JSON.parse(new TextDecoder().decode(buf));}catch{}
                  // Delivery widgets arrive asynchronously after the page shell.
                  // Bound the extra wait, and collect only this product's CTA.
                  const deadline=Date.now()+2500;
                  let delivery=[];
                  do {
                    delivery=[...document.querySelectorAll('[data-widget="webAddToCart"]')].map(e=>e.innerText||'');
                    if(delivery.some(t=>/доставим|сегодня|завтра|[0-9]+\s+(январ|феврал|март|апрел|мая|июн|июл|август|сентябр|октябр|ноябр|декабр)/i.test(t)))break;
                    if(!/\/product\//.test(location.pathname))break;
                    await new Promise(resolve=>setTimeout(resolve,100));
                  } while(Date.now()<deadline);
                  return {status:res.status,payload,labels:delivery};
                }""",
                {"url": api_url, "cap": cap},
            ),
            timeout=timeout,
        )
        chrome_cdp._check_final_host(await chrome_cdp.current_page_url(page), HOSTS)
        payload = result.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("browser composer response is not a JSON object")
        return {
            "status": result.get("status", 0),
            "payload": payload,
            "session": profile_session(payload),
            "delivery": delivery_from_labels(result.get("labels")),
        }

    if keep_open and not scope:
        raise ValueError("keeping a login tab requires an active MCP session")
    if scope:
        result, expires = await read_with_handoff(
            url="https://www.ozon.ru" + path,
            wait_ms=4000,
            scope=scope,
            operation="ozon_session",
            read=read,
            challenge=lambda data: "login" if keep_open and data["session"]["authenticated"] is not True else None,
            allowed_hosts=HOSTS,
            retention_s=300
            if keep_open or has_pending_handoff(scope=scope, operation="ozon_session", url="https://www.ozon.ru" + path)
            else 0,
        )
        result["session"]["handoff_expires_at"] = expires
        result["session"]["login_tab_retained"] = expires is not None
        return result
    async with chrome_cdp.open_page("https://www.ozon.ru" + path, wait_ms=4000, allowed_hosts=HOSTS) as page:
        return await read(page)
