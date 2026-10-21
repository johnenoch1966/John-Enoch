# syntax=docker/dockerfile:1.7
# Multi-stage, reproducible, non-root image for the research agent.
#   docker build -t adoption-agent .
#   docker run --rm -e GOOGLE_CLOUD_PROJECT=... -v ~/.config/gcloud:/home/agent/.config/gcloud:ro adoption-agent
# Deployed as a Cloud Run Job (see deployment/terraform). Long-term memory is
# synced with GCS via ADOPTION_AGENT_STATE_BUCKET.

# ---- builder: resolve pinned dependencies into a virtualenv -----------------
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY requirements.lock ./
RUN pip install -r requirements.lock
COPY pyproject.toml README.md ./
COPY adoption_agent ./adoption_agent
RUN pip install --no-deps .

# ---- runtime: minimal image, non-root ---------------------------------------
FROM python:3.12-slim AS runtime
LABEL org.opencontainers.image.title="adoption-research-agent" \
      org.opencontainers.image.source="https://github.com/johnenoch1966/John-Enoch" \
      org.opencontainers.image.licenses="Apache-2.0"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    GOOGLE_GENAI_USE_VERTEXAI=TRUE \
    GOOGLE_CLOUD_LOCATION=global \
    ADOPTION_AGENT_HOME=/app
RUN useradd --create-home --uid 10001 agent
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=agent data ./data
RUN mkdir -p reports/history runs && chown -R agent /app
USER agent

# Default: one research cycle. Override for the dev UI:
#   docker run -p 8080:8080 adoption-agent adk web --host 0.0.0.0 --port 8080 /opt/venv/lib/python3.12/site-packages
CMD ["python", "-m", "adoption_agent.cli", "run"]
