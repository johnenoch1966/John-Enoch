"""GCS state sync for Cloud Run Jobs, tested against an in-memory fake bucket."""

from __future__ import annotations

from urllib.parse import unquote

from adoption_agent import config
from adoption_agent.state_sync import download_state, state_bucket, upload_state


class _Resp:
    def __init__(self, json_body=None, content=b""):
        self._json, self.content = json_body, content

    def raise_for_status(self):
        return None

    def json(self):
        return self._json


class FakeGcs:
    """Implements just enough of the GCS JSON API used by state_sync."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def post(self, url, params, data, headers, timeout):
        self.objects[params["name"]] = data
        return _Resp({})

    def get(self, url, params, timeout):
        if params.get("alt") == "media":
            return _Resp(content=self.objects[unquote(url.rsplit("/o/", 1)[1])])
        names = [n for n in self.objects if n.startswith(params["prefix"])]
        return _Resp({"items": [{"name": n} for n in names]})


def test_state_bucket_env(monkeypatch):
    monkeypatch.setenv("ADOPTION_AGENT_STATE_BUCKET", "gs://my-bucket/")
    assert state_bucket() == "my-bucket"
    monkeypatch.delenv("ADOPTION_AGENT_STATE_BUCKET")
    assert state_bucket() == ""


def test_round_trip_preserves_long_term_memory(tmp_home):
    (config.DATA_DIR / "evidence.db").write_bytes(b"sqlite-bytes")
    (config.REPORTS_DIR / "history" / "r1.html").write_text("<html>r1</html>")
    gcs = FakeGcs()
    assert upload_state("b", session=gcs) == 2
    assert set(gcs.objects) == {"data/evidence.db", "reports/history/r1.html"}

    (config.DATA_DIR / "evidence.db").unlink()  # simulate a fresh container
    (config.REPORTS_DIR / "history" / "r1.html").unlink()
    assert download_state("b", session=gcs) == 2
    assert (config.DATA_DIR / "evidence.db").read_bytes() == b"sqlite-bytes"
    assert (config.REPORTS_DIR / "history" / "r1.html").read_text() == "<html>r1</html>"
