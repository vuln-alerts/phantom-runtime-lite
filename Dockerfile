# Phantom Conversational Runtime — S1 Docker Runtime (v1.9)
#
# Containerizes the EXISTING monolithic runtime (src/phantom_runtime.py)
# WITHOUT any source/logic change.  Canonical Provider = OpenAI (OPENAI_API_KEY).
# Persistence (src/memory, src/sessions) stays external via volumes — NOT baked in.
#
# Out of S1 scope: docker-compose (next step), PostgreSQL (S2), VPS (S3),
# root prompts/ (S1 excluded), audio decoupling (v1.10).

# --- Base image ------------------------------------------------------------
# 3.14.x is the runtime's stated Python version. Tag confirmed via
# `docker manifest inspect python:3.14-slim` (resolves successfully).
# Override with: --build-arg BASE_IMAGE=<alternate-tag>.
ARG BASE_IMAGE=python:3.14-slim
FROM ${BASE_IMAGE}

# --- System native dependency: PortAudio (required by sounddevice) ------------
# libportaudio2 = runtime lib; portaudio19-dev + build-essential = build
# sounddevice/numpy from source if no wheel exists for this Python.
# procps = pgrep, used by the liveness healthcheck.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        libportaudio2 \
        portaudio19-dev \
        build-essential \
        procps \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- Python dependencies (from requirements.txt; logic unchanged) ----------
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# --- Application source --------------------------------------------------------
# .dockerignore excludes src/memory, src/sessions, src/backup, caches,
# src/test_*.py, the v21 runtime, and .env.  PROFILES_DIR (src/profiles) IS
# included (required).  Root prompts/ is intentionally NOT copied (S1 excluded;
# --mode full falls back to built-in prompts).
COPY src/ /app/

# --- Persistence mount points (external volumes overlay these at run time) -----
ENV PYTHONUNBUFFERED=1 \
    SESSION_OUTPUT_DIR=/app/sessions \
    PORT=8080
RUN mkdir -p /app/sessions

# --- Liveness: process-presence healthcheck (no source change; no HTTP endpoint)
# Bracket trick "[p]hantom..." avoids the pgrep -f self-match: the regex matches
# "phantom..." but the healthcheck shell's own cmdline contains "[p]hantom...",
# so the checker no longer matches itself (false-positive healthy fixed).
# Docker HEALTHCHECK is a local/Docker-only construct; Cloud Run ignores it and
# uses the HTTP readiness endpoint served by runtime.cloud_run_shell instead
# (v1.11 H2 Cloud Run Compatibility — see docs/V1_11_H2_CLOUD_RUN_COMPATIBILITY_CONTRACT.md).
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD pgrep -f "[p]hantom_runtime.py" > /dev/null || exit 1

# OPENAI_API_KEY is injected externally at run time (never hardcoded).
# Default args use built-in light prompts; overridable at run/compose time.
# runtime.cloud_run_shell (v1.11 H2) spawns phantom_runtime.py
# as an unmodified child process and forwards all args after "--" to it verbatim.
CMD ["python", "-m", "runtime.cloud_run_shell", "--", "--profile", "default", "--mode", "light", "--no-color"]
