from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from reference_agent.bundle.document import OKFDocument
from reference_agent.server import _Handler


def _write(bundle: Path) -> None:
    (bundle / "utils").mkdir(parents=True)
    (bundle / "utils" / "StringUtils.md").write_text(
        OKFDocument(
            frontmatter={"type": "Java Class", "title": "StringUtils",
                         "description": "string helpers", "tags": ["utils"],
                         "timestamp": "2026-01-01T00:00:00+00:00"},
            body="# 개요\n\nUses [Other](Other.md).\n",
        ).serialize(),
        encoding="utf-8",
    )
    (bundle / "utils" / "Other.md").write_text(
        OKFDocument(
            frontmatter={"type": "Java Class", "title": "Other",
                         "description": "other", "timestamp": "2026-01-01T00:00:00+00:00"},
            body="# 개요\n\nother.\n",
        ).serialize(),
        encoding="utf-8",
    )


@pytest.fixture()
def server(tmp_path: Path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write(bundle)
    handler = type("H", (_Handler,), {"bundle_root": bundle, "bundle_name": "T"})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield httpd.server_address
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(addr, path):
    conn = HTTPConnection(addr[0], addr[1], timeout=5)
    conn.request("GET", path)
    r = conn.getresponse()
    body = r.read().decode("utf-8")
    conn.close()
    return r.status, body


def test_index_serves_live_html(server):
    status, body = _get(server, "/")
    assert status == 200
    assert "StringUtils" in body  # bundle data embedded live


def test_healthz(server):
    status, body = _get(server, "/healthz")
    assert status == 200 and body == "ok"


def test_api_graph(server):
    status, body = _get(server, "/api/graph")
    assert status == 200
    g = json.loads(body)
    ids = {n["data"]["id"] for n in g["nodes"]}
    assert {"utils/StringUtils", "utils/Other"} <= ids
    # The cross-link becomes an edge.
    assert any(e["data"]["source"] == "utils/StringUtils"
               and e["data"]["target"] == "utils/Other" for e in g["edges"])


def test_api_search(server):
    status, body = _get(server, "/api/search?q=string")
    assert status == 200
    res = json.loads(body)["results"]
    assert any(r["id"] == "utils/StringUtils" for r in res)


def test_api_concept(server):
    status, body = _get(server, "/api/concept/utils/StringUtils")
    assert status == 200
    obj = json.loads(body)
    assert obj["frontmatter"]["title"] == "StringUtils"
    assert "개요" in obj["body"]


def test_api_concept_unknown(server):
    status, _ = _get(server, "/api/concept/utils/Nope")
    assert status == 404


def test_deep_link_md_path_opens_concept(server):
    status, body = _get(server, "/utils/StringUtils.md")
    assert status == 200
    # The viewer is served focused on that concept.
    assert '"utils/StringUtils"' in body
    assert "OKF_INITIAL" in body


def test_deep_link_without_extension(server):
    status, body = _get(server, "/utils/StringUtils")
    assert status == 200


def test_deep_link_unknown_404(server):
    status, _ = _get(server, "/utils/DoesNotExist.md")
    assert status == 404
