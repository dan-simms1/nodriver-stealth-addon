#!/usr/bin/env bash
set -eu

CONFIG=/data/options.json

RUNNER_PORT=3002
LOG_LEVEL=info
VNC_ENABLED="false"
VNC_PASSWORD=""
if [ -f "$CONFIG" ]; then
    RUNNER_PORT="$(jq -r '.runner_port // 3002' "$CONFIG")"
    LOG_LEVEL="$(jq -r '.log_level // "info"' "$CONFIG")"
    VNC_ENABLED="$(jq -r '.vnc_enabled // false' "$CONFIG")"
    VNC_PASSWORD="$(jq -r '.vnc_password // empty' "$CONFIG")"
fi

# When VNC is enabled, start Xvfb + x11vnc + noVNC alongside the
# flow runner. The runner inspects $DISPLAY and launches Chrome
# headed when it is set; otherwise headless. noVNC bridges x11vnc
# (port 5901) to a websocket on 7902 - port 7902 deliberately not
# 7901 to avoid clashing with the playwright-stealth addon.
if [ "$VNC_ENABLED" = "true" ]; then
    if [ -z "${VNC_PASSWORD:-}" ]; then
        echo "ERROR: vnc_enabled=true but vnc_password is empty."
        echo "       Refusing to start VNC services without a password."
        echo "       Set vnc_password in the add-on Configuration tab"
        echo "       and restart, or set vnc_enabled=false to disable"
        echo "       the VNC viewer."
        echo "       The flow runner will start as normal; only the"
        echo "       VNC viewer (port 7902) is suppressed."
    else
        DISPLAY_NUMBER=98
        export DISPLAY=":${DISPLAY_NUMBER}"

        echo "Starting Xvfb on ${DISPLAY}..."
        Xvfb "${DISPLAY}" -screen 0 1920x1080x24 -ac +extension RANDR &
        sleep 1

        echo "Starting x11vnc against ${DISPLAY}..."
        x11vnc \
            -display "${DISPLAY}" \
            -forever -shared \
            -passwd "${VNC_PASSWORD}" \
            -rfbport 5901 \
            -quiet &

        echo "Starting noVNC websocket bridge on 0.0.0.0:7902..."
        websockify --web=/usr/share/novnc 7902 localhost:5901 >/dev/null 2>&1 &
    fi
else
    echo "noVNC disabled (vnc_enabled=false). Port 7902 will refuse connections."
fi

# Foreground: the FastAPI flow runner. Server.py picks up DISPLAY
# from env to choose headed vs headless.
echo "Starting nodriver flow runner on 0.0.0.0:${RUNNER_PORT}..."
exec env RUNNER_PORT="${RUNNER_PORT}" LOG_LEVEL="${LOG_LEVEL}" \
    python3 /srv/runner/server.py
