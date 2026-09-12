# ---------------------------------------------------------------------------
# AI Browser Agent - FastAPI + Browser Use + DeepSeek + noVNC
#
# The image contains the whole graphical stack (Xvfb, XFCE, Chromium,
# x11vnc, websockify/noVNC), so it works on Render without any manual setup.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DISPLAY=:99 \
    SCREEN_WIDTH=1920 \
    SCREEN_HEIGHT=1080 \
    NOVNC_DIR=/usr/share/novnc \
    BROWSER_HEADLESS=false \
    INVISIBLE_PLAYWRIGHT_CACHE_DIR=/opt/invisible-playwright

# Use the real Google Chrome (not Debian's Chromium build) on amd64 to keep
# the TLS/UA/JS fingerprint consistent with a normal browser. On other
# architectures fall back to the distro Chromium package.
RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb \
      x11vnc \
      x11-utils \
      x11-xserver-utils \
      xauth \
      xfce4-session \
      xfwm4 \
      xfce4-panel \
      xfdesktop4 \
      xfce4-settings \
      xfconf \
      dbus-x11 \
      novnc \
      websockify \
      fonts-liberation \
      fonts-dejavu-core \
      fonts-noto-color-emoji \
      curl \
      ca-certificates \
      procps \
      tini \
      libgtk-3-0 \
      libdbus-glib-1-2 \
      libasound2 \
      libx11-xcb1 \
      libxt6 \
      libxtst6 \
      libxcomposite1 \
      libxdamage1 \
      libxfixes3 \
      libxrandr2 \
      libgbm1 \
      libxshmfence1 \
      libatk1.0-0 \
      libatk-bridge2.0-0 \
      libatspi2.0-0 \
      libcups2 \
      libdrm2 \
      libnspr4 \
      libnss3 \
      libpango-1.0-0 \
      libcairo2 \
    && if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
         curl -fsSL -o /tmp/google-chrome.deb \
           https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
         && apt-get install -y --no-install-recommends /tmp/google-chrome.deb \
         && rm -f /tmp/google-chrome.deb; \
       else \
         apt-get install -y --no-install-recommends chromium; \
       fi \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# Pre-download the stealth Firefox used for Cloudflare bypass (invisible_playwright).
# Doing it at build time keeps container startup fast and lets the runtime user
# run read-only against the cache.
RUN python -m invisible_playwright fetch \
 && chmod -R a+rX /opt/invisible-playwright

# SeleniumBase drives the visible Chromium in Pure CDP Mode. Its latest
# dependency pins (typing-extensions, requests, rich, ...) conflict with
# Browser Use, so it lives in a separate, isolated virtual environment.
RUN python -m venv /opt/seleniumbase-venv \
 && /opt/seleniumbase-venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/seleniumbase-venv/bin/pip install --no-cache-dir seleniumbase==4.54.1

COPY . .

# Runtime as a non-root user. The directories that hold mutable state are
# created up front and owned by that user.
RUN useradd --create-home --shell /bin/bash --uid 1000 agent \
  && mkdir -p /app/browser_profile /app/uploads /app/downloads /app/logs \
  && chmod +x /app/scripts/*.sh \
  && chown -R agent:agent /app \
  && chown -R agent:agent /opt/invisible-playwright

USER agent
ENV HOME=/home/agent

EXPOSE 10000

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-10000}/health" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["./scripts/start.sh"]
