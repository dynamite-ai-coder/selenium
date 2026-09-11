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
    BROWSER_HEADLESS=false

RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium \
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
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

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
 && chown -R agent:agent /app

USER agent
ENV HOME=/home/agent

EXPOSE 10000

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-10000}/health" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["./scripts/start.sh"]
