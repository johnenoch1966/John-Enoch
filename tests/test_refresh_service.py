"""Refresh coordination logic (Google Cloud runner and GitHub admit step)."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

from adoption_agent.refresh_service import _sync, decide

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def test_poll_starts_only_when_requested():
    assert decide({"state": "queued"}, NOW) == (True, "refresh requested")
    assert decide({"state": "published"}, NOW)[0] is False
    assert decide({"state": "idle"}, NOW, force=True) == (True, "forced refresh")


def test_poll_respects_running_marker_but_recovers_stale_runs():
    fresh = {"state": "running", "started_at": _iso(NOW - timedelta(hours=1))}
    stale = {"state": "running", "started_at": _iso(NOW - timedelta(hours=7))}
    assert decide(fresh, NOW)[0] is False
    assert decide(stale, NOW)[0] is True


def _admit():
    path = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "admit_refresh.py"
    spec = importlib.util.spec_from_file_location("admit_refresh", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_admit_queues_joins_and_enforces_cooldown():
    admit = _admit()
    day = timedelta(hours=24)
    assert admit.decide({"state": "idle"}, NOW, day)[0] == "queue"
    assert admit.decide({"state": "running"}, NOW, day)[0] == "join"
    assert admit.decide({"state": "queued"}, NOW, day)[0] == "join"
    recent = {"state": "published", "finished_at": _iso(NOW - timedelta(hours=3))}
    action, msg = admit.decide(recent, NOW, day)
    assert action == "decline" and "2026-11-04 09:00 UTC" in msg
    old = {"state": "published", "finished_at": _iso(NOW - timedelta(hours=30))}
    assert admit.decide(old, NOW, day)[0] == "queue"
    failed = {"state": "failed", "finished_at": _iso(NOW - timedelta(hours=3))}
    assert admit.decide(failed, NOW, day)[0] == "queue"  # failures retry after 2 h


def test_sync_mirrors_data_and_reports_but_keeps_readme(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "data" / "history").mkdir(parents=True)
    (src / "data" / "evidence.db").write_text("db")
    (src / "data" / "history" / "r1.json").write_text("{}")
    (src / "data" / "draft_report.json").write_text("draft")  # never published
    (src / "reports").mkdir()
    (src / "reports" / "index.html").write_text("<html>")
    (dst / "data").mkdir(parents=True)
    (dst / "data" / "README.md").write_text("keep")
    (dst / "data" / "stale.json").write_text("old")
    _sync(src, dst)
    assert (dst / "data" / "README.md").read_text() == "keep"
    assert not (dst / "data" / "stale.json").exists()
    assert not (dst / "data" / "draft_report.json").exists()
    assert (dst / "data" / "history" / "r1.json").exists()
    assert (dst / "reports" / "index.html").read_text() == "<html>"
