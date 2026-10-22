"""Runtime configuration, resolved from environment variables.

Everything that differs between a laptop, CI and Cloud Run lives here so the
agent code itself stays environment-agnostic.
"""

from __future__ import annotations

import os
from pathlib import Path

# Load a local .env if present (never committed; see .env.example).
try:  # pragma: no cover - optional convenience
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

# --- Models ---------------------------------------------------------------
# Fast model for search / extraction, stronger model for synthesis & critique.
MODEL: str = os.getenv("ADOPTION_AGENT_MODEL", "gemini-3.8-flash")
REASONING_MODEL: str = os.getenv("ADOPTION_AGENT_REASONING_MODEL", MODEL)

# --- Paths ----------------------------------------------------------------
PROJECT_ROOT: Path = Path(os.getenv("ADOPTION_AGENT_HOME", Path(__file__).resolve().parent.parent))
DATA_DIR: Path = PROJECT_ROOT / "data"
REPORTS_DIR: Path = PROJECT_ROOT / "reports"
RUNS_DIR: Path = PROJECT_ROOT / "runs"
EVIDENCE_DB: Path = Path(os.getenv("ADOPTION_AGENT_DB", DATA_DIR / "evidence.db"))
USE_CASE_CATALOG: Path = DATA_DIR / "use_cases.json"
REPORT_JSON: Path = DATA_DIR / "report.json"
TAXONOMY_FILE: Path = Path(__file__).resolve().parent / "taxonomy.yaml"

# --- Research scope -------------------------------------------------------
TARGET_USE_CASES: int = int(os.getenv("ADOPTION_AGENT_USE_CASES", "50"))
# Canonical industries used for tagging evidence, the industry research
# fan-out and the industry selector in the report.
INDUSTRIES: dict[str, str] = {
    "fsi": "Financial Services & Insurance (FSI)",
    "hcls": "Healthcare & Life Sciences (HCLS)",
    "manufacturing": "Manufacturing & Automotive",
    "retail_cpg": "Retail & Consumer Goods",
    "tmt": "Telecom, Media & Technology (TMT)",
    "public_sector": "Public Sector & Government",
    "energy_utilities": "Energy & Utilities",
}
TOP_N_PER_INDUSTRY: int = 5
# Cluster 0 is always the copilot / desktop-assistant segment; the rest are
# workflow-agent domains. This fixes the width of the parallel research fan-out.
NUM_WORKFLOW_CLUSTERS: int = 4
NUM_CLUSTERS: int = NUM_WORKFLOW_CLUSTERS + 1
MIN_SOURCES_PER_CLUSTER: int = int(os.getenv("ADOPTION_AGENT_MIN_SOURCES", "5"))
MAX_SOURCES_PER_CLUSTER: int = int(os.getenv("ADOPTION_AGENT_MAX_SOURCES", "10"))

# --- Tool limits (context-window hygiene) -----------------------------------
MAX_PAGE_CHARS: int = int(os.getenv("ADOPTION_AGENT_MAX_PAGE_CHARS", "8000"))
FETCH_TIMEOUT_S: float = float(os.getenv("ADOPTION_AGENT_FETCH_TIMEOUT", "20"))
MAPPING_BATCH_SIZE: int = 30

# --- Loop limits ------------------------------------------------------------
MAX_MAPPING_ITERATIONS: int = 30
MAX_REVIEW_ITERATIONS: int = 3
# Human-in-the-loop policy for publishing: prompt | auto | reject (see hitl.py).
APPROVAL_MODE: str = os.getenv("ADOPTION_AGENT_APPROVAL", "prompt")
# Guard rail on total model calls per run (ADK default is 500).
MAX_LLM_CALLS: int = int(os.getenv("ADOPTION_AGENT_MAX_LLM_CALLS", "4000"))


def ensure_dirs() -> None:
    """Create output directories on demand."""
    for d in (DATA_DIR, REPORTS_DIR / "history", RUNS_DIR):
        d.mkdir(parents=True, exist_ok=True)
