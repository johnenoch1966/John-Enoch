"""Persistent SQLite evidence store.

This is the agent's long-term memory. Each run *adds* to it rather than
starting from scratch: sources are de-duplicated by normalised URL and claims
by (source, factor, polarity), so re-running the pipeline enriches the
evidence base and the report becomes progressively more robust.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .schemas import ClaimInput, RootCauseMappingInput, SourceInput

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url_hash TEXT UNIQUE NOT NULL,
  url TEXT NOT NULL,
  title TEXT,
  publisher TEXT,
  published TEXT,
  source_type TEXT,
  vendor_affiliated INTEGER,
  methodology TEXT,
  run_id TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS claims (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  claim_hash TEXT UNIQUE NOT NULL,
  source_id INTEGER NOT NULL REFERENCES sources(id),
  stated_factor TEXT NOT NULL,
  polarity TEXT NOT NULL,
  segment TEXT NOT NULL,
  use_case_ids TEXT,
  evidence_quote TEXT,
  statistic TEXT,
  run_id TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS mappings (
  claim_id INTEGER PRIMARY KEY REFERENCES claims(id),
  root_cause_ids TEXT NOT NULL,
  inference TEXT,
  confidence TEXT,
  framing_bias TEXT,
  run_id TEXT,
  created_at TEXT
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def normalise_url(url: str) -> str:
    """Lower-case host, drop fragments/query tracking and trailing slashes."""
    parts = urlsplit(url.strip())
    query = "&".join(
        p
        for p in parts.query.split("&")
        if p and not p.lower().startswith(("utm_", "gclid", "fbclid"))
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def _hash(*parts: str) -> str:
    return hashlib.sha256("||".join(p.strip().lower() for p in parts).encode()).hexdigest()


class EvidenceStore:
    """Thin repository over SQLite. Safe to instantiate per call."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------ write
    def add_source(self, src: SourceInput, run_id: str) -> tuple[int, bool]:
        """Insert a source; returns (source_id, is_new)."""
        url = normalise_url(src.url)
        h = _hash(url)
        with self._conn() as c:
            row = c.execute("SELECT id FROM sources WHERE url_hash=?", (h,)).fetchone()
            if row:
                return int(row["id"]), False
            cur = c.execute(
                "INSERT INTO sources (url_hash,url,title,publisher,published,"
                "source_type,vendor_affiliated,methodology,run_id,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    h,
                    url,
                    src.title,
                    src.publisher,
                    src.published,
                    src.source_type,
                    int(src.vendor_affiliated),
                    src.methodology,
                    run_id,
                    _now(),
                ),
            )
            return int(cur.lastrowid), True

    def add_claim(self, source_id: int, claim: ClaimInput, run_id: str) -> tuple[int, bool]:
        """Insert a claim; returns (claim_id, is_new)."""
        h = _hash(str(source_id), claim.stated_factor, claim.polarity, claim.segment)
        with self._conn() as c:
            row = c.execute("SELECT id FROM claims WHERE claim_hash=?", (h,)).fetchone()
            if row:
                return int(row["id"]), False
            cur = c.execute(
                "INSERT INTO claims (claim_hash,source_id,stated_factor,polarity,"
                "segment,use_case_ids,evidence_quote,statistic,run_id,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    h,
                    source_id,
                    claim.stated_factor,
                    claim.polarity,
                    claim.segment,
                    json.dumps(claim.use_case_ids),
                    claim.evidence_quote,
                    claim.statistic,
                    run_id,
                    _now(),
                ),
            )
            return int(cur.lastrowid), True

    def add_mapping(self, m: RootCauseMappingInput, run_id: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO mappings (claim_id,root_cause_ids,inference,"
                "confidence,framing_bias,run_id,created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    m.claim_id,
                    json.dumps(m.root_cause_ids),
                    m.inference,
                    m.confidence,
                    m.framing_bias,
                    run_id,
                    _now(),
                ),
            )

    # ------------------------------------------------------------------- read
    def existing_claim_ids(self, ids: list[int]) -> set[int]:
        if not ids:
            return set()
        q = ",".join("?" * len(ids))
        with self._conn() as c:
            rows = c.execute(f"SELECT id FROM claims WHERE id IN ({q})", ids).fetchall()
        return {int(r["id"]) for r in rows}

    def unmapped_claims(self, limit: int = 25) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT c.id, c.stated_factor, c.polarity, c.segment, c.evidence_quote,"
                " s.publisher, s.source_type, s.vendor_affiliated"
                " FROM claims c JOIN sources s ON s.id=c.source_id"
                " LEFT JOIN mappings m ON m.claim_id=c.id"
                " WHERE m.claim_id IS NULL ORDER BY c.id LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_unmapped(self) -> int:
        with self._conn() as c:
            return int(
                c.execute(
                    "SELECT COUNT(*) FROM claims c LEFT JOIN mappings m"
                    " ON m.claim_id=c.id WHERE m.claim_id IS NULL"
                ).fetchone()[0]
            )

    def all_claims(self) -> list[dict[str, Any]]:
        """Claims joined with their source and mapping (for digest + report)."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT c.id, c.stated_factor, c.polarity, c.segment, c.use_case_ids,"
                " c.evidence_quote, c.statistic, c.run_id,"
                " s.id AS source_id, s.url, s.title, s.publisher, s.published,"
                " s.source_type, s.vendor_affiliated, s.methodology,"
                " m.root_cause_ids, m.inference, m.confidence, m.framing_bias"
                " FROM claims c JOIN sources s ON s.id=c.source_id"
                " LEFT JOIN mappings m ON m.claim_id=c.id ORDER BY c.id"
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["use_case_ids"] = json.loads(d["use_case_ids"] or "[]")
            d["root_cause_ids"] = json.loads(d["root_cause_ids"] or "[]")
            d["vendor_affiliated"] = bool(d["vendor_affiliated"])
            out.append(d)
        return out

    def stats(self, run_id: str | None = None) -> dict[str, int]:
        with self._conn() as c:

            def one(sql: str, *a: Any) -> int:
                return int(c.execute(sql, a).fetchone()[0])

            s = {
                "sources_total": one("SELECT COUNT(*) FROM sources"),
                "claims_total": one("SELECT COUNT(*) FROM claims"),
                "mapped_total": one("SELECT COUNT(*) FROM mappings"),
            }
            if run_id:
                s["sources_new"] = one("SELECT COUNT(*) FROM sources WHERE run_id=?", run_id)
                s["claims_new"] = one("SELECT COUNT(*) FROM claims WHERE run_id=?", run_id)
            return s

    # ----------------------------------------------------------------- digest
    def digest(self, max_examples: int = 4) -> dict[str, Any]:
        """Compact, aggregated view of all evidence for the synthesis prompt.

        Raw quotes are capped per factor so the prompt stays bounded even as
        the evidence base grows across many runs.
        """
        claims = self.all_claims()
        by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for cl in claims:
            root = cl["root_cause_ids"][0] if cl["root_cause_ids"] else "unmapped"
            by_key[(cl["segment"], cl["polarity"], root)].append(cl)

        groups = []
        for (segment, polarity, root), items in sorted(by_key.items(), key=lambda kv: -len(kv[1])):
            groups.append(
                {
                    "segment": segment,
                    "polarity": polarity,
                    "root_cause_id": root,
                    "claim_count": len(items),
                    "distinct_sources": len({i["source_id"] for i in items}),
                    "vendor_share": round(
                        sum(i["vendor_affiliated"] for i in items) / len(items), 2
                    ),
                    "examples": [
                        {
                            "claim_id": i["id"],
                            "stated_factor": i["stated_factor"],
                            "statistic": i["statistic"],
                            "publisher": i["publisher"],
                            "inference": i["inference"],
                            "framing_bias": i["framing_bias"],
                        }
                        for i in items[:max_examples]
                    ],
                }
            )
        source_types = Counter((cl["source_id"], cl["source_type"]) for cl in claims)
        return {
            "totals": self.stats(),
            "source_type_mix": dict(Counter(t for (_, t) in source_types)),
            "groups": groups,
        }
