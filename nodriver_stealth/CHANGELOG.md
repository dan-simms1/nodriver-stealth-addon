# Changelog

## 1.0.0 - 2026-05-12

First stable public release. The pre-release iteration (v0.1.x) has been squashed; this is the maintained public surface.

### What this is

A Home Assistant add-on wrapping [nodriver](https://github.com/ultrafunkamsterdam/nodriver) (the Python successor to `undetected-chromedriver`) in a generic HTTP flow runner. nodriver drives Chromium directly via the DevTools Protocol; there is no WebDriver layer at all, so the browser is genuinely real Chrome on Linux without JS-layer lies.

### Features

- **HTTP flow runner** on port 3002 (deliberately not 3001 so this can run alongside the sister `playwright-stealth-addon`).
  - `GET /healthz` - liveness check.
  - `POST /run-flow` - runs a structured action list, returns cookies.
- **Identical action vocabulary** to the sister patchright addon so callers can switch backends without rewriting flow code: `goto`, `wait_for_url_*`, `wait_for_selector_*`, `click`, `click_if_present`, `hover`, `mouse_move`, `scroll`, `set_value`, `type` (per-keystroke jitter), `sleep_ms`, `sleep_ms_jitter`, `screenshot`, `save_state`, `assert_url_host`, `get_cookies`.
- **Library-level cookie jar** populated via `Network.responseReceivedExtraInfo` CDP events. Captures cookies as they arrive (same model Playwright uses internally), no post-flow CDP query needed - avoids the wedges CDP can hit after long redirect chains.
- **Argument substitution** via `${name}` placeholders.
- **Persistent profiles** via Playwright-compatible `storageState` JSON. Interchangeable with the patchright addon's profile format.
- **Real Debian Chromium** - no patched fork. nodriver's avoidance of WebDriver and careful flag choice does the stealth work.
- **Optional Xvfb + x11vnc + noVNC viewer** on port 7902 (deliberately not 7901 to coexist with the sister addon). Refuses to start with an empty `vnc_password`.
- **Multi-arch image**: `amd64` and `aarch64` (uses Debian's `chromium` package which is available on both, unlike Google Chrome's apt repo).

### Security defaults

- VNC viewer **off** by default. When enabled, refuses to start without a non-empty `vnc_password`.
- The flow runner does NOT authenticate. Do not expose to untrusted networks.

### Installation

\`\`\`
Settings -> Add-ons -> Add-on Store -> three-dot menu -> Repositories
Add: https://github.com/dan-simms1/nodriver-stealth-addon
\`\`\`

### Sister addon

[playwright-stealth-addon](https://github.com/dan-simms1/playwright-stealth-addon) exposes the same HTTP API via Patchright + Node.js. The two are interchangeable from a caller's perspective; pick whichever scores higher on your target site.
