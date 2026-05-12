# nodriver Stealth Browser add-on for Home Assistant

A Home Assistant add-on that wraps [`nodriver`][nodriver] (the Python
successor to `undetected-chromedriver`) in a generic HTTP flow runner.

nodriver drives Chromium directly via the DevTools Protocol; there is no
WebDriver layer at all. Combined with carefully chosen launch args, that
gives a fingerprint that is genuinely real Chrome on Linux — no JS-layer
lies, no patched binary needed.

This add-on is unofficial and not affiliated with the nodriver project or
any browser vendor.

This is a generic helper. It has no site-specific knowledge. Any caller
that wants stealth-aware browser automation over HTTP can use it.

A sister add-on, [playwright-stealth-addon](https://github.com/dan-simms1/playwright-stealth-addon),
exposes the same HTTP API on top of a Node.js + Patchright stack. The two
are interchangeable from a caller's perspective; pick whichever scores
higher on your target site.

## Why two add-ons

The two expose **the same HTTP API** so callers can talk to either
interchangeably. The integration speaks identically to both and you can
switch via its Options.

| | playwright-stealth-addon | this addon |
|---|---|---|
| **Engine** | Patchright (Playwright fork with patched Chromium) | nodriver (raw CDP, no WebDriver layer) |
| **Language** | Node.js | Python |
| **Browser** | Patchright's patched Chromium binary | Debian's `chromium` package |
| **Default port** | 3001 | 3002 |
| **noVNC port** | 7901 | 7902 |

The two reach the same goal by different routes. Patchright wins on some
sites, nodriver on others. Running both side by side and flipping engines
when one starts failing is a viable strategy.

## What this provides

- Flow runner HTTP service on port 3002 with two endpoints:
  - `GET /healthz` — liveness check.
  - `POST /run-flow` — runs a structured action list, returns cookies.
- Per-request profile persistence so cookie history carries across runs.
- Library-level cookie capture via `Network.responseReceivedExtraInfo`
  events: cookies are accumulated as Set-Cookie headers arrive, no
  post-flow CDP query needed (which is the same model Playwright uses
  internally).
- Optional Xvfb + x11vnc + noVNC stack on port 7902 for live observation.
- Hard timeout per action and zombie-process reaping in the cleanup path
  so no flow can leave Chromium hanging around.

## Installing

1. **Settings → Add-ons → Add-on Store** in Home Assistant.
2. Three-dot menu → **Repositories**.
3. Add `https://github.com/dan-simms1/nodriver-stealth-addon`.
4. Install **nodriver Stealth Browser**, set options if you want, then
   **Start**.

## Configuration

| Option | Default | Notes |
|---|---|---|
| `runner_port` | `3002` | Port the flow runner listens on. |
| `log_level` | `info` | Server log verbosity. |
| `vnc_enabled` | `false` | When true, runs Xvfb + x11vnc + noVNC and switches the browser to headed mode. |
| `vnc_password` | _empty_ | Password for the noVNC viewer. **Required** when `vnc_enabled=true`. The add-on refuses to start VNC services with an empty password (the flow runner still starts; only the viewer is suppressed). |

## Endpoints

### `GET /healthz`

Returns `{"ok": true}`.

### `POST /run-flow`

Body shape (identical to the playwright addon's):

```json
{
  "actions": [
    { "goto": "https://example.com/login" },
    { "wait_for_selector": "#email" },
    { "type": { "selector": "#email", "value": "${user}", "delay_ms": 90, "delay_jitter_ms": 60 } },
    { "click": "#submit" },
    { "wait_for_url_host": "example.com" },
    { "get_cookies": { "domain_filter": "example.com" } }
  ],
  "args": { "user": "...", "pass": "..." },
  "context": { "locale": "en-GB", "timezone_id": "Europe/London" },
  "profile": "example-profile"
}
```

Returns `{ "result": "ok", "elapsed_ms": <int>, "cookies": { ... } }` on
success, or `{ "error": "<message>", "failed_action_index": <int> }` with a
4xx/5xx status on failure.

### Action vocabulary

| Action | Notes |
|---|---|
| `{ goto: "url" }` | Navigate. |
| `{ wait_for_url_host: "host" }` | Wait until hostname matches. |
| `{ wait_for_url_contains: "substring" }` / `{ wait_for_url_not_contains }` | URL substring waits. |
| `{ wait_for_selector: "css", state? }` | Standard wait. State `attached` or `visible` (default). |
| `{ wait_for_selector_visible_via_css: "css" }` | Wait until `getComputedStyle(el).display !== 'none'`. |
| `{ click: "css" }` / `{ click_if_present: "css" }` | Click; the second is best-effort. |
| `{ hover: "css" }` | Move cursor to the element. |
| `{ mouse_move: { x, y, steps? } }` / `{ mouse_move: { selector, steps? } }` | Stepped cursor movement. |
| `{ scroll: { y } }` / `{ scroll: { selector } }` | Pixel scroll or scrollIntoView. |
| `{ set_value: { selector, value } }` | Set an input via the React-friendly prototype setter. |
| `{ type: { selector, value, delay_ms?, delay_jitter_ms? } }` | Realistic typing. The text is set via the JS prototype setter (so React sees it) AND per-character `keyDown`/`keyUp` events fire for keystroke-timing fingerprinting. |
| `{ sleep_ms: <int> }` / `{ sleep_ms_jitter: [min, max] }` | Sleeps. |
| `{ screenshot: "/path" }` | Full-page screenshot. |
| `{ save_state: true }` | Commit profile mid-flow. |
| `{ assert_url_host: "host" }` | Throw if hostname differs. |
| `{ get_cookies: { domain_filter? } }` | Read cookies from the in-flow capture jar. |

All actions accept an optional `timeout_ms` (default 30000) which is also
the wall-clock cap (with a 5s buffer) before the runner aborts the action.

### Argument substitution

Strings in actions can include `${name}` placeholders that are substituted
from the request's `args` object before the action runs.

### Persistent profiles

If a request includes `"profile": "name"`, the runner reads
`/data/profiles/<name>.json` at launch (Playwright's `storageState` shape;
profiles are interchangeable with the playwright-stealth-addon) and writes
the in-flow cookie jar back on success. `save_state: true` commits mid-flow.

### How cookies are captured

Unlike CDP-query-on-demand approaches (which can wedge after long redirect
chains), this addon mirrors Playwright's internal model: at flow start, two
CDP event listeners attach to the tab — `Network.requestWillBeSent` (to map
`requestId → URL`) and `Network.responseReceivedExtraInfo` (to receive the
full response headers including HttpOnly Set-Cookie). Each Set-Cookie line
is parsed into a Playwright-shape dict and appended to a Python list. By
the time `get_cookies` runs there is nothing to query — the jar is already
populated.

## Live observation

With `vnc_enabled: true` the runner launches Chromium headed against the
Xvfb display. Open `http://<ha-ip>:7902/vnc.html` and enter the
`vnc_password`. Port 7902 deliberately not 7901 so this addon can run
alongside the playwright addon without conflict.

## Security

Same caveats as the playwright addon. **The flow runner does not
authenticate.** Anyone who can reach port 3002 or 7902 on your network can
drive a Chromium session on your hardware. Do not forward those ports from
your router; keep the home network isolated; treat this as you would treat
a remote-execution endpoint.

Set a strong `vnc_password` if you enable VNC. The add-on refuses to start
VNC at all when the password is empty.

## What is inside the image

Layered on `python:3.12-slim-bookworm`. Adds:

- Debian's `chromium` package (works on amd64 AND aarch64; Google Chrome's
  apt package is amd64-only, no good on Mac minis or RPis).
- Xvfb + x11vnc + noVNC for the optional VNC viewer.
- A FastAPI flow runner under `/srv/runner` (FastAPI + uvicorn + nodriver,
  pinned versions).
- A launcher script that reads `/data/options.json`, optionally starts
  Xvfb / x11vnc / noVNC, and runs the flow runner.

[nodriver]: https://github.com/ultrafunkamsterdam/nodriver

## Licence

MIT. See `LICENSE`.

nodriver itself is GPL-3.0. Chromium is BSD and others.
