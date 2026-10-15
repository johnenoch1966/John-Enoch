"""Shared fixtures: isolate all file output in a temp directory."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")

from adoption_agent import config  # noqa: E402


class FakeToolContext:
    """Minimal stand-in for ADK's ToolContext in unit tests."""

    def __init__(self, state: dict | None = None, agent_name: str = "test_agent"):
        self.state = state if state is not None else {"run_id": "test-run"}
        self.agent_name = agent_name


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "EVIDENCE_DB", data / "evidence.db")
    monkeypatch.setattr(config, "USE_CASE_CATALOG", data / "use_cases.json")
    monkeypatch.setattr(config, "REPORT_JSON", data / "report.json")
    config.ensure_dirs()
    return tmp_path


@pytest.fixture
def tool_context():
    return FakeToolContext()
