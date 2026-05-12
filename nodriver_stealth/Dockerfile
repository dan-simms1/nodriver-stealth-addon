FROM python:3.12-slim-bookworm

ENV LANG=C.UTF-8 \
    DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1

# System deps:
# - chromium from Debian repos (works on amd64 AND aarch64; Google
#   Chrome's apt package is amd64-only, which is no good on Mac
#   minis or any ARM HA host). nodriver auto-detects chromium on
#   PATH, so a single install path covers both architectures.
# - Xvfb + x11vnc + novnc for the optional headed viewer
# - jq for parsing /data/options.json at runtime
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg jq \
        xvfb x11vnc novnc websockify \
        fonts-liberation libnss3 libgbm1 libxshmfence1 libxkbcommon0 \
        libdrm2 libxrandr2 libatk-bridge2.0-0 libgtk-3-0 libpango-1.0-0 \
        libasound2 libxcomposite1 libxdamage1 libxfixes3 libcups2 \
        chromium \
 && rm -rf /var/lib/apt/lists/*

COPY rootfs/ /

RUN pip install --no-cache-dir -r /srv/runner/requirements.txt \
 && chmod +x /usr/local/bin/run.sh

LABEL \
    io.hass.name="nodriver Stealth Browser" \
    io.hass.description="nodriver-based stealth browser automation, generic HTTP flow runner." \
    io.hass.type="addon" \
    io.hass.version="1.0.0" \
    maintainer="Dan Simms <daniel@simmsfamily.me>"

CMD ["/usr/local/bin/run.sh"]
