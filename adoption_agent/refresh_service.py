"""Refresh service: runs the web research cycle inside Google Cloud.

Deployed as a Cloud Run Job in the Google Cloud project and started every 10
minutes by Cloud Scheduler (`python -m adoption_agent.cli poll`). It runs as the
project's service account, so no personal credentials are involved or expire.

Coordination happens through `status.json` on the `research-data` branch
(no GitHub API calls, so there are no rate limits from shared cloud egress IPs):

    idle / published / needs_review / failed   ->  (GitHub "Refresh research" request)
    queued   (written by the GitHub admit job, with the request issue numbers)
    running  (written here when a cycle starts)
    published | needs_review | failed          (written here when it ends;
                                                GitHub then comments on and closes the requests)

A cycle restores long-term memory from `research-data`, runs the full
pipeline (approval policy "auto"), applies the automated publication quality
gate, then pushes either to `research-data` (live) or to a `refresh/<ts>`
review branch.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import config

logger = logging.getLogger("adoption_agent.refresh")

REPO_SSH = os.getenv(
    "ADOPTION_AGENT_REPO_SSH", "ssh://git@ssh.github.com:443/johnenoch1966/John-Enoch.git"
)
DATA_BRANCH = "research-data"
STALE_RUNNING = timedelta(hours=6)  # a 'running' marker older than this is abandoned


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


# ---------------------------------------------------------------------------
# Decision logic (pure, unit-tested)
# ---------------------------------------------------------------------------
def decide(status: dict[str, Any], now: datetime, force: bool = False) -> tuple[bool, str]:
    """Should this poll start a research cycle? Returns (start, reason)."""
    state = status.get("state", "idle")
    if state == "running":
        started = _parse(status.get("started_at"))
        if started and now - started < STALE_RUNNING:
            return False, "a cycle is already running"
        return True, "previous run marker is stale; restarting"
    if state == "queued" or force:
        return True, "refresh requested" if state == "queued" else "forced refresh"
    return False, "no pending request"


# ---------------------------------------------------------------------------
# Git plumbing (deploy key from the private state bucket)
# ---------------------------------------------------------------------------
def _fetch_deploy_key(dest: Path) -> None:
    bucket = os.getenv("ADOPTION_AGENT_SECRETS_BUCKET", "").removeprefix("gs://").strip("/")
    obj = os.getenv("ADOPTION_AGENT_DEPLOY_KEY_OBJECT", "secrets/github_deploy_key")
    if not bucket:
        raise RuntimeError("ADOPTION_AGENT_SECRETS_BUCKET is not set")
    from urllib.parse import quote

    from .state_sync import _session

    resp = _session().get(
        f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{quote(obj, safe='')}",
        params={"alt": "media"},
        timeout=30,
    )
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    dest.chmod(0o600)


class Git:
    def __init__(self, workdir: Path, key: Path):
        self.workdir = workdir
        self.env = {
            **os.environ,
            "GIT_SSH_COMMAND": f"ssh -i {key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new",
            "GIT_AUTHOR_NAME": "adoption-agent-cloudrun",
            "GIT_AUTHOR_EMAIL": "adoption-agent@users.noreply.github.com",
            "GIT_COMMITTER_NAME": "adoption-agent-cloudrun",
            "GIT_COMMITTER_EMAIL": "adoption-agent@users.noreply.github.com",
        }

    def __call__(self, *args: str, cwd: Path | None = None) -> str:
        out = subprocess.run(
            ["git", *args],
            cwd=cwd or self.workdir,
            env=self.env,
            capture_output=True,
            text=True,
        )
        if out.returncode != 0:
            logger.error("git %s failed: %s", args[0], out.stderr.strip()[-500:])
            raise subprocess.CalledProcessError(out.returncode, out.args, out.stdout, out.stderr)
        return out.stdout.strip()

    def clone(self) -> None:
        self(
            "clone",
            "--single-branch",
            "--branch",
            DATA_BRANCH,
            REPO_SSH,
            str(self.workdir),
            cwd=Path("/"),
        )

    def commit(self, message: str, *paths: str) -> None:
        self("add", "-A", *paths)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=self.workdir).returncode:
            self("commit", "-m", message)

    def push_branch(self, branch: str) -> None:
        self("push", "-f", "origin", f"HEAD:{branch}")

    def sync_and_push(self, update_status: Any, message: str, attempts: int = 5) -> None:
        """Rebase on the latest research-data, apply a status update, push.

        The GitHub admit job may append request numbers to status.json while a
        cycle runs, so the status is re-read and merged on every attempt.
        """
        for attempt in range(attempts):
            self("fetch", "origin", DATA_BRANCH)
            self("rebase", "-X", "theirs", f"origin/{DATA_BRANCH}")
            _write_status(self.workdir, update_status(_read_status(self.workdir)))
            self.commit(message, "status.json")
            try:
                self("push", "origin", f"HEAD:{DATA_BRANCH}")
                return
            except subprocess.CalledProcessError:
                logger.warning("push rejected (attempt %d), retrying", attempt + 1)
        raise RuntimeError("could not push status after retries")


def _read_status(workdir: Path) -> dict[str, Any]:
    p = workdir / "status.json"
    return json.loads(p.read_text()) if p.exists() else {"state": "idle"}


def _write_status(workdir: Path, status: dict[str, Any]) -> None:
    (workdir / "status.json").write_text(json.dumps(status, indent=2) + "\n")


def _sync(src: Path, dst: Path) -> None:
    """Mirror data/ and reports/ from src to dst (keeps data/README.md)."""
    for sub in ("data", "reports"):
        s, d = src / sub, dst / sub
        if not s.exists():
            continue
        if d.exists():
            for child in d.iterdir():
                if child.name == "README.md":
                    continue
                shutil.rmtree(child) if child.is_dir() else child.unlink()
        d.mkdir(parents=True, exist_ok=True)
        for item in s.iterdir():
            if item.name in ("README.md", "draft_report.json", "draft.html"):
                continue
            target = d / item.name
            shutil.copytree(item, target) if item.is_dir() else shutil.copy2(item, target)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def poll() -> int:
    force = os.getenv("ADOPTION_AGENT_FORCE_REFRESH", "") == "1"
    tmp = Path(tempfile.mkdtemp(prefix="refresh-"))
    key = tmp / "deploy_key"
    _fetch_deploy_key(key)
    git = Git(tmp / "state", key)
    git.clone()
    status = _read_status(git.workdir)
    start, reason = decide(status, _now(), force)
    logger.info("poll: state=%s -> %s (%s)", status.get("state"), start, reason)
    if not start:
        return 0

    execution = os.getenv("CLOUD_RUN_EXECUTION", "local")

    def merged(new: dict[str, Any]):
        def apply(current: dict[str, Any]) -> dict[str, Any]:
            issues = sorted(set(current.get("issues", [])) | set(status.get("issues", [])))
            return {**new, "execution": execution, "issues": issues}

        return apply

    git.sync_and_push(
        merged({"state": "running", "started_at": _iso(_now())}),
        f"status: research cycle running ({execution})",
    )

    try:
        _sync(git.workdir, config.PROJECT_ROOT)  # restore long-term memory
        from .cli import run_pipeline
        from .observability import setup_tracing

        setup_tracing()
        rc = asyncio.run(run_pipeline(approval="auto"))
        if rc != 0:
            raise RuntimeError(f"research pipeline exited with {rc}")

        from evals.gate_report import gate

        passed, _scores, failures = gate(config.REPORT_JSON)
        logger.info("quality gate passed=%s failures=%s", passed, failures)
        _sync(config.PROJECT_ROOT, git.workdir)
        finished = _iso(_now())
        if passed:
            git.commit(
                f"chore(report): refresh {execution} (quality gate passed)", "data", "reports"
            )
            git.sync_and_push(
                merged({"state": "published", "finished_at": finished}),
                f"status: refresh {execution} published",
            )
        else:
            branch = f"refresh/{execution}"
            git.commit(f"chore(report): refresh {execution} (needs review)", "data", "reports")
            git.push_branch(branch)
            git("reset", "--hard", "HEAD~1")  # keep research-data unchanged
            git.sync_and_push(
                merged(
                    {
                        "state": "needs_review",
                        "finished_at": finished,
                        "review_branch": branch,
                        "gate_failures": failures,
                    }
                ),
                f"status: refresh {execution} needs review",
            )
        return 0
    except Exception as e:
        logger.exception("research cycle failed")
        git("reset", "--hard", f"origin/{DATA_BRANCH}")
        git.sync_and_push(
            merged({"state": "failed", "finished_at": _iso(_now()), "error": str(e)[:300]}),
            f"status: refresh {execution} failed",
        )
        return 1
