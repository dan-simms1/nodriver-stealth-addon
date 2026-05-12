"""HTTP flow runner backed by nodriver.

Exposes the same `POST /run-flow` and `GET /healthz` endpoints as the
sister `playwright-stealth-addon`, accepting the same action-list
JSON payload. Callers can talk to either addon interchangeably - the
only difference is the underlying browser stack.

Why a second addon: Patchright (a Playwright fork with a patched
Chromium binary) and nodriver (Python; raw CDP, no WebDriver
protocol layer at all) defeat different sets of bot-detection
heuristics. nodriver tends to perform better against Cloudflare
Turnstile; Patchright is generally stronger for sites that lean on
JS-fingerprint cross-checks. Running both means callers can A/B and
pick whichever scores higher on a given site without rewriting their
flow code.

Action vocabulary (subset of the patchright addon's, focused on what
real flows actually need):
   { goto: "https://..." }
   { wait_for_url_host: "host.example", timeout_ms?: number }
   { wait_for_url_contains: "substring", timeout_ms?: number }
   { wait_for_url_not_contains: "substring", timeout_ms?: number }
   { wait_for_selector: "css", state?: "visible"|"attached", timeout_ms?: number }
   { wait_for_selector_visible_via_css: "css", timeout_ms?: number }
   { click: "css", timeout_ms?: number }
   { click_if_present: "css", timeout_ms?: number }
   { hover: "css", timeout_ms?: number }
   { mouse_move: { x, y, steps? } | { selector, steps?, timeout_ms? } }
   { scroll: { y } | { selector } }
   { set_value: { selector, value } }
   { type: { selector, value, delay_ms?, delay_jitter_ms? } }
   { sleep_ms: number }
   { sleep_ms_jitter: [min, max] }
   { screenshot: "/path/to/file.png" }
   { save_state: true }
   { assert_url_host: "host.example" }
   { get_cookies: { domain_filter?: "substring" } }

Storage state format is intentionally compatible with Playwright's
`storageState` JSON shape, so profiles seasoned via the patchright
addon can be reused here without conversion.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import nodriver as uc
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from uvicorn import Config, Server

LOG_LEVEL = os.environ.get("LOG_LEVEL", "info").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)sZ [runner] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("flow-runner")

PROFILE_DIR = Path("/data/profiles")
PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.IGNORECASE)

app = FastAPI(title="nodriver flow runner")


def _profile_path(name: str) -> Path:
    if not isinstance(name, str) or not PROFILE_NAME_RE.match(name):
        msg = f"invalid profile name: {name!r}"
        raise ValueError(msg)
    return PROFILE_DIR / f"{name}.json"


def _subst_args(value: Any, args: dict[str, Any]) -> Any:
    if not isinstance(value, str):
        return value
    return re.sub(
        r"\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}",
        lambda m: str(args[m.group(1)]) if m.group(1) in args else m.group(0),
        value,
    )


async def _load_storage_state(profile: str | None) -> dict[str, Any] | None:
    """Return the saved storage-state dict for `profile`, or None."""
    if not profile:
        return None
    path = _profile_path(profile)
    if not path.exists():
        log.info("profile '%s': no saved state at %s", profile, path)
        return None
    try:
        data = json.loads(path.read_text())
        log.info("profile '%s': loaded storage state from %s", profile, path)
    except (OSError, ValueError) as err:
        log.warning("profile '%s': could not load %s: %s", profile, path, err)
        return None
    if not isinstance(data, dict):
        return None
    return data


async def _apply_storage_state(tab: Any, state: dict[str, Any] | None) -> None:
    """Inject saved cookies + localStorage into the running browser."""
    if not state:
        return
    cookies = state.get("cookies") or []
    if cookies:
        # nodriver exposes cookie helpers; fall back to the underlying
        # CDP Network.setCookie for fields like sameSite that the
        # high-level helper does not pass through cleanly.
        for c in cookies:
            try:
                await tab.send(
                    uc.cdp.network.set_cookie(
                        name=c["name"],
                        value=c["value"],
                        domain=c.get("domain"),
                        path=c.get("path", "/"),
                        secure=c.get("secure"),
                        http_only=c.get("httpOnly"),
                        same_site=_to_cdp_samesite(c.get("sameSite")),
                        expires=(
                            float(c["expires"])
                            if isinstance(c.get("expires"), (int, float))
                            and c["expires"] > 0
                            else None
                        ),
                    ),
                )
            except (KeyError, TypeError, ValueError) as err:
                log.debug("skip cookie %s: %s", c.get("name"), err)
    origins = state.get("origins") or []
    for origin in origins:
        url = origin.get("origin")
        items = origin.get("localStorage") or []
        if not url or not items:
            continue
        # Visit a blank page on the origin to set its localStorage.
        # We use about:blank with a fetch trick instead of fully
        # navigating to avoid drawing attention to a fresh visit.
        # Simpler: navigate, set storage, navigate away.
        try:
            await tab.get(url)
            assignments = ";".join(
                f"localStorage.setItem({json.dumps(item['name'])},"
                f"{json.dumps(item['value'])})"
                for item in items
                if "name" in item and "value" in item
            )
            if assignments:
                await tab.evaluate(assignments)
        except Exception as err:
            log.debug("skip origin %s: %s", url, err)


def _to_cdp_samesite(value: Any) -> Any:
    if not value:
        return None
    if isinstance(value, str):
        v = value.lower()
        if v == "lax":
            return uc.cdp.network.CookieSameSite.LAX
        if v == "strict":
            return uc.cdp.network.CookieSameSite.STRICT
        if v == "none":
            return uc.cdp.network.CookieSameSite.NONE
    return None


async def _save_storage_state(
    cookie_jar: list[dict[str, Any]],
    profile: str | None,
) -> None:
    """Persist the in-memory cookie jar to disk.

    Dedupes to last-write-wins per (name, domain, path) since the
    same cookie can be Set-Cookie'd multiple times during one flow
    (login, refresh, etc.) and we want the latest value.
    """
    if not profile:
        return
    path = _profile_path(profile)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for c in cookie_jar:
        key = (
            c.get("name") or "",
            c.get("domain") or "",
            c.get("path") or "/",
        )
        if key[0]:
            deduped[key] = c
    cookies = list(deduped.values())

    payload = {
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cookies": cookies,
        "origins": [],
    }
    path.write_text(json.dumps(payload))
    log.info(
        "profile '%s': saved storage state (%d cookies)",
        profile,
        len(cookies),
    )


def _parse_set_cookie_line(line: str, request_host: str) -> dict[str, Any] | None:
    """Parse one Set-Cookie header line into a Playwright-shape dict.

    Set-Cookie syntax: `name=value; Domain=...; Path=...; ...`
    Domain attribute defaults to the request's host (host-only cookie).
    Path defaults to `/`.
    """
    line = line.strip()
    if not line or "=" not in line:
        return None
    parts = [p.strip() for p in line.split(";")]
    nv = parts[0].split("=", 1)
    if len(nv) != 2:
        return None
    name = nv[0].strip()
    value = nv[1].strip()
    if not name:
        return None

    domain = request_host
    path = "/"
    secure = False
    http_only = False
    same_site = "None"
    for attr in parts[1:]:
        al = attr.lower()
        if al.startswith("domain="):
            domain = attr.split("=", 1)[1].strip().lstrip(".")
        elif al.startswith("path="):
            path = attr.split("=", 1)[1].strip() or "/"
        elif al == "secure":
            secure = True
        elif al == "httponly":
            http_only = True
        elif al.startswith("samesite="):
            same_site = attr.split("=", 1)[1].strip().capitalize() or "None"

    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": path,
        "expires": -1,
        "secure": secure,
        "httpOnly": http_only,
        "sameSite": same_site,
    }


def _make_cookie_capture(
    cookie_jar: list[dict[str, Any]],
    request_urls: dict[str, list[str]],
) -> tuple[Any, Any]:
    """Return (request_handler, response_extra_info_handler) callbacks.

    Closes over the shared `cookie_jar` and `request_urls` mapping.
    `request_urls` is needed because the ResponseReceivedExtraInfo
    event does not include the URL directly - we have to correlate
    via requestId which we caught at RequestWillBeSent time.

    CDP reuses requestId across HTTP redirects (the `requestId` is a
    network-request identifier, not an HTTP-request identifier), and
    extra-info events can arrive out of order relative to the next
    requestWillBeSent that overwrites the URL. Storing a list rather
    than overwriting means we keep the original-URL host available
    for cookies set on intermediate 302 responses and only fall back
    to a later URL if the original was already filtered out.
    """

    async def on_request(event: Any) -> None:
        try:
            url = event.request.url
            request_urls.setdefault(event.request_id, []).append(url)
        except (AttributeError, KeyError):
            pass

    async def on_extra_info(event: Any) -> None:
        try:
            headers = event.headers or {}
            # headers is a CDP Headers object that behaves like a
            # case-insensitive dict (in nodriver it usually
            # serialises to a plain dict).
            if isinstance(headers, dict):
                items = headers.items()
            else:
                items = list(getattr(headers, "items", lambda: [])())
            set_cookie_raw: str | None = None
            for k, v in items:
                if str(k).lower() == "set-cookie":
                    set_cookie_raw = str(v)
                    break
            if not set_cookie_raw:
                return
            urls = request_urls.get(event.request_id) or []
            # Match the latest-but-not-future URL we have seen for
            # this requestId. In well-ordered streams that is the
            # request that produced this response. In re-ordered
            # streams the worst we can do is attribute a 302's
            # cookie to the redirect target instead of the
            # redirect source, which still keeps the cookie on the
            # same host tree if Domain= is set explicitly.
            url = urls[-1] if urls else ""
            request_host = (urlparse(url).hostname or "").lower()
            # Multiple Set-Cookie headers in one response come back
            # newline-joined in CDP's Headers serialisation.
            for line in set_cookie_raw.split("\n"):
                cookie = _parse_set_cookie_line(line, request_host)
                if cookie is not None:
                    cookie_jar.append(cookie)
        except Exception as err:
            log.debug("cookie capture handler raised: %s", err)

    return on_request, on_extra_info


def _chromium_user_data_dir(browser: Any) -> str | None:
    """Return the user-data-dir chromium was launched with.

    nodriver stores it on the browser instance after launch. Field
    name varies a little between releases so we check a few
    candidates and fall back to scanning /tmp.
    """
    for attr in ("user_data_dir", "_user_data_dir", "config"):
        v = getattr(browser, attr, None)
        if isinstance(v, str) and v.startswith("/"):
            return v
        if v is not None:
            inner = getattr(v, "user_data_dir", None)
            if isinstance(inner, str) and inner.startswith("/"):
                return inner
    # Fallback: pick the most recent uc_* directory under /tmp.
    try:
        candidates = sorted(
            (
                p for p in os.listdir("/tmp")
                if p.startswith("uc_")
            ),
            key=lambda n: os.path.getmtime(os.path.join("/tmp", n)),
            reverse=True,
        )
        if candidates:
            return os.path.join("/tmp", candidates[0])
    except OSError:
        pass
    return None


def _read_chromium_cookies_from_disk(
    browser: Any,
    domain_filter: str,
) -> dict[str, str]:
    """Return name->value cookies, optionally filtered by domain."""
    udd = _chromium_user_data_dir(browser)
    if udd is None:
        log.warning("could not locate chromium user-data-dir for cookie read")
        return {}
    db_path = os.path.join(udd, "Default", "Cookies")
    if not os.path.exists(db_path):
        log.warning("chromium cookies DB missing at %s", db_path)
        return {}

    import shutil
    import sqlite3
    import tempfile
    # Chromium holds an exclusive lock while running; copy the DB to a
    # temp file before reading so we do not contend with the live
    # process. WAL mode means the snapshot might be slightly stale,
    # but it includes everything that was committed at the last
    # journal flush, which covers session cookies after login.
    with tempfile.NamedTemporaryFile(suffix=".sqlite") as tmp:
        try:
            shutil.copy2(db_path, tmp.name)
            for ext in ("-wal", "-shm"):
                src = db_path + ext
                if os.path.exists(src):
                    shutil.copy2(src, tmp.name + ext)
        except OSError as err:
            log.warning("could not snapshot cookies DB: %s", err)
            return {}
        try:
            conn = sqlite3.connect(f"file:{tmp.name}?mode=ro", uri=True)
            cur = conn.execute(
                "SELECT host_key, name, value FROM cookies",
            )
            out: dict[str, str] = {}
            for host_key, name, value in cur.fetchall():
                domain = (host_key or "").lower().lstrip(".")
                if domain_filter:
                    if not (
                        domain == domain_filter
                        or domain.endswith("." + domain_filter)
                    ):
                        continue
                if isinstance(name, str) and isinstance(value, str):
                    out[name] = value
            conn.close()
            return out
        except sqlite3.Error as err:
            log.warning("sqlite read of cookies DB failed: %s", err)
            return {}


def _read_chromium_cookies_from_disk_full(
    browser: Any,
) -> list[dict[str, Any]]:
    """Return full cookie records (Playwright storageState shape)."""
    udd = _chromium_user_data_dir(browser)
    if udd is None:
        return []
    db_path = os.path.join(udd, "Default", "Cookies")
    if not os.path.exists(db_path):
        return []

    import shutil
    import sqlite3
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".sqlite") as tmp:
        try:
            shutil.copy2(db_path, tmp.name)
            for ext in ("-wal", "-shm"):
                src = db_path + ext
                if os.path.exists(src):
                    shutil.copy2(src, tmp.name + ext)
        except OSError:
            return []
        try:
            conn = sqlite3.connect(f"file:{tmp.name}?mode=ro", uri=True)
            cur = conn.execute(
                "SELECT host_key, name, value, path, "
                "expires_utc, is_secure, is_httponly, samesite "
                "FROM cookies",
            )
            out: list[dict[str, Any]] = []
            for row in cur.fetchall():
                host_key, name, value, c_path, expires_utc, is_secure, is_httponly, samesite = row
                # Chromium stores expires_utc as microseconds since 1601-01-01.
                # Playwright's storageState wants Unix seconds.
                if expires_utc:
                    unix_seconds = (expires_utc / 1_000_000) - 11644473600
                else:
                    unix_seconds = -1
                samesite_str = {0: "None", 1: "Lax", 2: "Strict"}.get(
                    samesite, "None",
                )
                out.append(
                    {
                        "name": name,
                        "value": value,
                        "domain": host_key,
                        "path": c_path or "/",
                        "expires": unix_seconds,
                        "httpOnly": bool(is_httponly),
                        "secure": bool(is_secure),
                        "sameSite": samesite_str,
                    },
                )
            conn.close()
            return out
        except sqlite3.Error:
            return []


# --------------------------------------------------------------- actions


def _action_type(action: dict[str, Any]) -> str:
    for k in action:
        if k not in ("timeout_ms", "state"):
            return k
    return "unknown"


def _jitter_pick(rng: Any) -> int:
    if not isinstance(rng, list) or len(rng) != 2:
        msg = "sleep_ms_jitter expects [min, max]"
        raise ValueError(msg)
    lo, hi = int(rng[0]), int(rng[1])
    if lo < 0 or hi < lo:
        msg = f"sleep_ms_jitter expects non-negative [min, max]; got {rng}"
        raise ValueError(msg)
    return random.randint(lo, hi)


async def _wait_for_url(
    tab: Any,
    predicate: Any,
    timeout_ms: int,
) -> None:
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        if predicate(tab.url or ""):
            return
        await asyncio.sleep(0.2)
    msg = f"timeout {timeout_ms}ms waiting for URL predicate; current url={tab.url!r}"
    raise TimeoutError(msg)


async def _wait_for_selector(
    tab: Any,
    selector: str,
    state: str,
    timeout_ms: int,
) -> Any:
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            elem = await tab.select(selector, timeout=1)
            if elem is None:
                last_err = LookupError(f"missing {selector}")
            elif state == "attached":
                return elem
            elif state == "visible":
                box = await elem.get_position()
                if box and box.width and box.height:
                    return elem
                last_err = LookupError(f"{selector} not visible")
        except Exception as err:
            last_err = err
        await asyncio.sleep(0.25)
    msg = f"timeout {timeout_ms}ms waiting for selector {selector!r}: {last_err}"
    raise TimeoutError(msg)


async def _run_action(
    tab: Any,
    browser: Any,
    cookie_jar: list[dict[str, Any]],
    action: dict[str, Any],
    args: dict[str, Any],
    profile: str | None,
) -> dict[str, Any] | None:
    timeout = int(action.get("timeout_ms", 30_000))

    if "goto" in action:
        url = _subst_args(action["goto"], args)
        await tab.get(url)
        return None

    if "wait_for_url_host" in action:
        host = _subst_args(action["wait_for_url_host"], args)
        await _wait_for_url(
            tab,
            lambda u, h=host: urlparse(u).hostname == h,
            timeout,
        )
        return None

    if "wait_for_url_contains" in action:
        needle = _subst_args(action["wait_for_url_contains"], args)
        await _wait_for_url(tab, lambda u, n=needle: n in u, timeout)
        return None

    if "wait_for_url_not_contains" in action:
        needle = _subst_args(action["wait_for_url_not_contains"], args)
        await _wait_for_url(tab, lambda u, n=needle: n not in u, timeout)
        return None

    if "wait_for_selector" in action:
        sel = _subst_args(action["wait_for_selector"], args)
        state = action.get("state", "visible")
        await _wait_for_selector(tab, sel, state, timeout)
        return None

    if "wait_for_selector_visible_via_css" in action:
        sel = _subst_args(action["wait_for_selector_visible_via_css"], args)
        deadline = time.monotonic() + (timeout / 1000.0)
        while time.monotonic() < deadline:
            visible = await tab.evaluate(
                f"""(() => {{
                    const el = document.querySelector({json.dumps(sel)});
                    return !!(el && getComputedStyle(el).display !== 'none');
                }})()""",
            )
            if visible:
                return None
            await asyncio.sleep(0.25)
        msg = f"timeout {timeout}ms waiting for {sel!r} display!=none"
        raise TimeoutError(msg)

    if "click" in action:
        sel = _subst_args(action["click"], args)
        elem = await _wait_for_selector(tab, sel, "visible", timeout)
        await elem.click()
        return None

    if "click_if_present" in action:
        sel = _subst_args(action["click_if_present"], args)
        try:
            elem = await tab.select(sel, timeout=int(action.get("timeout_ms", 2_000)) / 1000)
            if elem:
                await elem.click()
        except Exception:
            pass
        return None

    if "hover" in action:
        sel = _subst_args(action["hover"], args)
        elem = await _wait_for_selector(tab, sel, "visible", timeout)
        # nodriver's .mouse_move on an element drives stepped CDP events.
        await elem.mouse_move()
        return None

    if "mouse_move" in action:
        spec = action["mouse_move"]
        steps = max(1, int(spec.get("steps", 25)))
        if "selector" in spec:
            sel = _subst_args(spec["selector"], args)
            elem = await _wait_for_selector(tab, sel, "visible", timeout)
            await elem.mouse_move()
        else:
            x = float(spec["x"])
            y = float(spec["y"])
            # Drive a stepped move via CDP Input.dispatchMouseEvent.
            for i in range(1, steps + 1):
                xi = x * (i / steps)
                yi = y * (i / steps)
                await tab.send(
                    uc.cdp.input_.dispatch_mouse_event(
                        type_="mouseMoved",
                        x=xi,
                        y=yi,
                    ),
                )
                await asyncio.sleep(0.01)
        return None

    if "scroll" in action:
        spec = action["scroll"]
        if "selector" in spec:
            sel = _subst_args(spec["selector"], args)
            elem = await _wait_for_selector(tab, sel, "attached", timeout)
            await elem.scroll_into_view()
        else:
            dy = float(spec["y"])
            await tab.evaluate(f"window.scrollBy(0, {dy})")
        return None

    if "set_value" in action:
        spec = action["set_value"]
        sel = _subst_args(spec["selector"], args)
        val = _subst_args(spec["value"], args)
        elem = await _wait_for_selector(tab, sel, "visible", timeout)
        await tab.evaluate(
            f"""(() => {{
                const el = document.querySelector({json.dumps(sel)});
                if (!el) throw new Error('missing');
                const proto = el.tagName === 'TEXTAREA'
                    ? HTMLTextAreaElement.prototype
                    : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                setter.call(el, {json.dumps(val)});
                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                el.dispatchEvent(new Event('blur', {{ bubbles: true }}));
            }})()""",
        )
        return None

    if "type" in action:
        spec = action["type"]
        sel = _subst_args(spec["selector"], args)
        val = _subst_args(spec["value"], args)
        delay_ms = int(spec.get("delay_ms", 50))
        jitter_ms = int(spec.get("delay_jitter_ms", 0))
        elem = await _wait_for_selector(tab, sel, "visible", timeout)
        await elem.click()  # focus via real mouse event

        # CDP-level keystrokes (Input.insertText and
        # dispatchKeyEvent type=char) both proved unable to update
        # React's controlled-input state on YW's email/password form
        # in nodriver. The form's onChange listener is wired through
        # React's synthetic event system, which only sees value
        # changes that go through the prototype's value setter
        # interceptor. So: set the value via the prototype setter
        # (the same trick our patchright addon's set_value uses),
        # and ALSO dispatch per-char key events so reCAPTCHA's
        # keystroke-timing fingerprint sees realistic input.
        await tab.evaluate(
            f"""(() => {{
                const el = document.querySelector({json.dumps(sel)});
                if (!el) throw new Error('missing ' + {json.dumps(sel)});
                el.focus();
                const proto = el.tagName === 'TEXTAREA'
                    ? HTMLTextAreaElement.prototype
                    : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                setter.call(el, {json.dumps(val)});
                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                el.dispatchEvent(new Event('change', {{ bubbles: true }}));
            }})()""",
        )
        # Phantom key events: do not affect the field value (already
        # set above) but produce the keystroke-timing telemetry that
        # reCAPTCHA scores. Per-char delay + jitter so the cadence
        # is non-uniform.
        for ch in val:
            d = max(0, delay_ms + (
                random.randint(-jitter_ms // 2, jitter_ms // 2)
                if jitter_ms > 0
                else 0
            ))
            try:
                await tab.send(
                    uc.cdp.input_.dispatch_key_event(
                        type_="keyDown",
                        key=ch,
                    ),
                )
                await tab.send(
                    uc.cdp.input_.dispatch_key_event(
                        type_="keyUp",
                        key=ch,
                    ),
                )
            except Exception as err:
                log.debug("phantom keystroke for %r failed: %s", ch, err)
            await asyncio.sleep(d / 1000.0)
        return None

    if "sleep_ms" in action:
        await asyncio.sleep(int(action["sleep_ms"]) / 1000.0)
        return None

    if "sleep_ms_jitter" in action:
        ms = _jitter_pick(action["sleep_ms_jitter"])
        await asyncio.sleep(ms / 1000.0)
        return None

    if "screenshot" in action:
        out = _subst_args(action["screenshot"], args)
        await tab.save_screenshot(filename=out, full_page=True)
        return None

    if "save_state" in action:
        await _save_storage_state(cookie_jar, profile)
        return None

    if "assert_url_host" in action:
        expected = _subst_args(action["assert_url_host"], args)
        got = urlparse(tab.url or "").hostname
        if got != expected:
            msg = f"expected URL host {expected!r}, got {got!r}"
            raise AssertionError(msg)
        return None

    if "get_cookies" in action:
        spec = action["get_cookies"] or {}
        domain_filter = (
            _subst_args(spec.get("domain_filter") or "", args).lower()
        )
        # Read from the in-memory cookie jar that's been populated
        # via Network.responseReceivedExtraInfo events throughout
        # the flow. No post-flow CDP query needed - cookies were
        # captured the moment they arrived. Same model Playwright
        # uses internally for context.cookies(). Last-write-wins on
        # name collisions since cookies can be Set-Cookie'd multiple
        # times during one flow.
        out: dict[str, str] = {}
        for c in cookie_jar:
            domain = (c.get("domain") or "").lower().lstrip(".")
            if domain_filter and not (
                domain == domain_filter
                or domain.endswith("." + domain_filter)
            ):
                continue
            name = c.get("name")
            value = c.get("value")
            if isinstance(name, str) and isinstance(value, str):
                out[name] = value
        log.info(
            "get_cookies: %d cookies (filter=%r) from jar of %d records",
            len(out),
            domain_filter,
            len(cookie_jar),
        )
        return {"cookies": out}

    msg = f"unknown action keys: {list(action.keys())}"
    raise ValueError(msg)


# --------------------------------------------------------------- HTTP


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.post("/run-flow")
async def run_flow(req: Request) -> JSONResponse:
    body = await req.json()
    actions = body.get("actions")
    args = body.get("args") or {}
    ctx_opts = body.get("context") or {}
    profile = body.get("profile")

    if not isinstance(actions, list) or not actions:
        return JSONResponse(
            status_code=400,
            content={"error": "actions must be a non-empty array"},
        )
    if not isinstance(args, dict):
        return JSONResponse(
            status_code=400,
            content={"error": "args must be an object"},
        )

    try:
        state = await _load_storage_state(profile)
    except ValueError as err:
        return JSONResponse(status_code=400, content={"error": str(err)})

    headless = not bool(os.environ.get("DISPLAY"))
    locale = ctx_opts.get("locale", "en-GB")
    tz = ctx_opts.get("timezone_id", "Europe/London")
    viewport = ctx_opts.get("viewport") or {"width": 1920, "height": 1080}

    start = time.monotonic()
    cookies: dict[str, str] | None = None
    browser = None

    chrome_args = [
        f"--lang={locale}",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        # Suppress Chromium's password-manager UI (the "Save password?"
        # bubble that overlaid the page on every login). The bubble
        # itself does not block cookies, but suppressing the password
        # manager removes a bunch of post-form-submit Chromium
        # internal activity that may be one of the contributors to
        # the CDP wedge we saw with getAllCookies.
        "--disable-features=PasswordManagerOnboarding,PasswordCheck,"
        "AutofillServerCommunication,PasswordImport,PasswordExport,"
        "AutofillEnableAccountWalletStorage",
        "--disable-save-password-bubble",
    ]
    if headless:
        chrome_args.append("--headless=new")

    try:
        browser = await uc.start(
            browser_args=chrome_args,
            headless=headless,
            lang=locale,
        )
        tab = browser.main_tab
        # Apply timezone + viewport via CDP Emulation.
        await tab.send(uc.cdp.emulation.set_timezone_override(timezone_id=tz))
        await tab.send(
            uc.cdp.emulation.set_device_metrics_override(
                width=int(viewport["width"]),
                height=int(viewport["height"]),
                device_scale_factor=1,
                mobile=False,
            ),
        )

        # Wire up the cookie-capture pipeline before we do anything
        # else on the tab. Network domain has to be enabled for the
        # extra-info events to fire; we listen to RequestWillBeSent
        # to track URL by requestId, and to ResponseReceivedExtraInfo
        # to receive the actual Set-Cookie headers (these are the
        # full headers post-CORS, including HttpOnly cookies that
        # never reach document.cookie).
        cookie_jar: list[dict[str, Any]] = []
        request_urls: dict[str, list[str]] = {}
        on_request, on_extra_info = _make_cookie_capture(
            cookie_jar, request_urls,
        )
        try:
            await tab.send(uc.cdp.network.enable())
            tab.add_handler(uc.cdp.network.RequestWillBeSent, on_request)
            tab.add_handler(
                uc.cdp.network.ResponseReceivedExtraInfo, on_extra_info,
            )
        except Exception as err:
            log.warning(
                "cookie capture pipeline failed to attach: %s; "
                "get_cookies will return empty",
                err,
            )

        await _apply_storage_state(tab, state)

        for i, action in enumerate(actions):
            atype = _action_type(action)
            preview = json.dumps(action.get(atype, {}))[:120]
            log.info("action %d: %s %s", i, atype, preview)
            # Hard wall-clock cap on every action. Defends against CDP
            # calls (especially network.get_all_cookies after a complex
            # redirect chain) hanging indefinitely - those would leave
            # chromium running forever and block the next request.
            action_timeout = float(action.get("timeout_ms", 30_000)) / 1000.0
            try:
                result = await asyncio.wait_for(
                    _run_action(
                        tab, browser, cookie_jar, action, args, profile,
                    ),
                    timeout=action_timeout + 5.0,
                )
                if result and "cookies" in result:
                    cookies = result["cookies"]
                log.info("action %d: %s ok", i, atype)
            except (TimeoutError, asyncio.TimeoutError) as err:
                log.warning("action %d (%s) hard-timed out", i, atype)
                stamp = int(time.time() * 1000)
                try:
                    await tab.save_screenshot(
                        filename=f"/tmp/runner_fail_{stamp}.png",
                        full_page=True,
                    )
                except Exception:
                    pass
                return JSONResponse(
                    status_code=502,
                    content={
                        "error": f"action {i} ({atype}) timed out",
                        "failed_action_index": i,
                        "elapsed_ms": int((time.monotonic() - start) * 1000),
                    },
                )
            except Exception as err:
                msg = str(err).splitlines()[0]
                log.warning("action %d failed (%s): %s", i, atype, msg)
                # Best-effort failure capture
                stamp = int(time.time() * 1000)
                try:
                    await tab.save_screenshot(
                        filename=f"/tmp/runner_fail_{stamp}.png",
                        full_page=True,
                    )
                except Exception:
                    pass
                return JSONResponse(
                    status_code=502,
                    content={
                        "error": msg,
                        "failed_action_index": i,
                        "failure_url": tab.url,
                        "elapsed_ms": int((time.monotonic() - start) * 1000),
                    },
                )

        # Implicit save on success. Bounded with a generous-but-real
        # timeout so a hung CDP call cannot keep the browser alive
        # forever - the call MUST return so the finally block can
        # tear down chromium and the next request can proceed.
        if profile:
            try:
                await asyncio.wait_for(
                    _save_storage_state(cookie_jar, profile),
                    timeout=5.0,
                )
            except (TimeoutError, asyncio.TimeoutError):
                log.warning("save_state for %s timed out", profile)
            except Exception as err:
                log.warning("could not save profile %s: %s", profile, err)

        elapsed_ms = int((time.monotonic() - start) * 1000)
        cookie_count = len(cookies) if cookies else 0
        log.info(
            "flow OK in %dms (%d actions, %d cookies)",
            elapsed_ms,
            len(actions),
            cookie_count,
        )
        return JSONResponse(
            content={
                "result": "ok",
                "elapsed_ms": elapsed_ms,
                "cookies": cookies or {},
            },
        )
    except Exception as err:
        msg = str(err).splitlines()[0]
        log.exception("flow setup failed")
        return JSONResponse(
            status_code=502,
            content={
                "error": msg,
                "elapsed_ms": int((time.monotonic() - start) * 1000),
            },
        )
    finally:
        if browser is not None:
            # browser.stop() is a sync call that blocks on the CDP
            # connection. After a wedged action 29 (post-login),
            # CDP can hang and stop() never returns - which means
            # the finally block never reaches the pkill below and
            # chromium piles up. Run stop() in a thread with a
            # short timeout so we always proceed to the SIGKILL
            # fallback below.
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(browser.stop),
                    timeout=3.0,
                )
            except (TimeoutError, asyncio.TimeoutError):
                log.warning("browser.stop() timed out; falling through to pkill")
            except Exception as err:
                log.debug("browser.stop() raised: %s", err)
        # Belt-and-braces: SIGKILL any leftover chromium PIDs even if
        # we never opened a browser (e.g. an exception before launch).
        try:
            subprocess.run(
                ["pkill", "-9", "-f", "chromium"],
                check=False,
                timeout=3,
            )
        except Exception as err:
            log.debug("leftover-chromium reap failed: %s", err)
        # Reap zombie children. Our Python is PID 1 inside the addon
        # container, so any chromium subprocess that gets killed
        # leaves a defunct entry until we waitpid() on it. Non-
        # blocking loop catches all of them.
        await asyncio.sleep(0.2)
        reaped = 0
        try:
            while True:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
                reaped += 1
        except ChildProcessError:
            pass
        except Exception as err:
            log.debug("waitpid loop raised: %s", err)
        if reaped:
            log.debug("reaped %d zombie children", reaped)


def main() -> None:
    port = int(os.environ.get("RUNNER_PORT", "3002"))
    log.info("listening on 0.0.0.0:%d", port)
    config = Config(
        app=app,
        host="0.0.0.0",
        port=port,
        log_level="info",
    )
    server = Server(config)
    asyncio.run(server.serve())


if __name__ == "__main__":
    sys.exit(main())
