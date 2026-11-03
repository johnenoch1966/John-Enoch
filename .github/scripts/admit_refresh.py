"""Admit or decline a 'Refresh research' request (GitHub Actions, Google mode).

Reads status.json on the research-data branch, applies the guardrails
(one cycle at a time, cooldown) and either queues the request for the Cloud
Run job in Google Cloud or explains why it cannot start. Issue text is never
read or executed; only the issue number is used.

Env: ISSUE (number, optional), GH_TOKEN, COOLDOWN_HOURS, REPORT_URL.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

BRANCH = "research-data"


def sh(*args: str, check: bool = True) -> str:
    return subprocess.run(args, check=check, capture_output=True, text=True).stdout.strip()


def read_status() -> dict:
    sh("git", "fetch", "-q", "origin", BRANCH)
    try:
        return json.loads(sh("git", "show", f"origin/{BRANCH}:status.json"))
    except subprocess.CalledProcessError:
        return {"state": "idle"}


def decide(status: dict, now: datetime, cooldown: timedelta) -> tuple[str, str]:
    """Return (action, message): action in {queue, join, decline}."""
    state = status.get("state", "idle")
    if state in ("queued", "running"):
        return "join", (
            "A refresh is already "
            + ("queued" if state == "queued" else "running")
            + ". Your request is included, and I'll comment here when it's done."
        )
    finished = status.get("finished_at")
    if finished:
        wait = (timedelta(hours=2) if state == "failed" else cooldown) - (
            now - datetime.fromisoformat(finished)
        )
        if wait > timedelta(0):
            nxt = (now + wait).strftime("%Y-%m-%d %H:%M UTC")
            return "decline", f"The report was refreshed recently. The next refresh is possible after {nxt}."
    return "queue", (
        "Refresh queued. It will start on Google Cloud within about 10 minutes and takes "
        "about 3 hours. I'll comment here when the page is updated."
    )


def push_status(update) -> None:
    for _ in range(5):
        sh("git", "fetch", "-q", "origin", BRANCH)
        sh("git", "checkout", "-q", "-B", "status-update", f"origin/{BRANCH}")
        try:
            current = json.loads(open("status.json").read())
        except FileNotFoundError:
            current = {"state": "idle"}
        with open("status.json", "w") as f:
            json.dump(update(current), f, indent=2)
            f.write("\n")
        sh("git", "add", "status.json")
        sh("git", "-c", "user.name=adoption-agent-bot", "-c",
           "user.email=actions@users.noreply.github.com", "commit", "-q", "-m",
           "status: refresh requested")
        if subprocess.run(["git", "push", "-q", "origin", f"HEAD:{BRANCH}"]).returncode == 0:
            return
    raise SystemExit("could not update status.json")


def main() -> int:
    issue = os.getenv("ISSUE", "")
    report_url = os.getenv("REPORT_URL", "")
    status = read_status()
    action, msg = decide(status, datetime.now(UTC), timedelta(hours=int(os.getenv("COOLDOWN_HOURS", "24"))))
    num = [int(issue)] if issue.isdigit() else []

    if action == "queue":
        push_status(lambda cur: {"state": "queued", "requested_at": datetime.now(UTC).isoformat(timespec="seconds"),
                                 "issues": sorted(set(cur.get("issues", []) if cur.get("state") == "queued" else []) | set(num))})
    elif action == "join" and num:
        push_status(lambda cur: {**cur, "issues": sorted(set(cur.get("issues", [])) | set(num))})

    print(f"{action}: {msg}")
    with open(os.environ.get("GITHUB_STEP_SUMMARY", os.devnull), "a") as f:
        f.write(f"### Refresh request: {action}\n{msg}\n")
    if num:
        sh("gh", "issue", "comment", issue, "--body", f"Thanks! {msg} Current report: {report_url}")
        if action == "decline":
            sh("gh", "issue", "close", issue, "--reason", "not planned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
