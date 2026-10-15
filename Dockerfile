# Container for running a research cycle as a Cloud Run Job (or locally).
#   docker build -t adoption-agent .
#   docker run --rm -e GOOGLE_CLOUD_PROJECT=... -v ~/.config/gcloud:/root/.config/gcloud adoption-agent
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GOOGLE_GENAI_USE_VERTEXAI=TRUE \
    GOOGLE_CLOUD_LOCATION=global \
    ADOPTION_AGENT_HOME=/app

WORKDIR /app
COPY pyproject.toml README.md ./
COPY adoption_agent ./adoption_agent
RUN pip install --no-cache-dir . && useradd -m agent && mkdir -p data reports runs && chown -R agent /app
COPY --chown=agent data ./data
USER agent

# Default: one research cycle. Override with `adk web --host 0.0.0.0 --port 8080 .` for the dev UI.
CMD ["python", "-m", "adoption_agent.cli", "run"]
