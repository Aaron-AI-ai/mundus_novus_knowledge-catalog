"""A tiny live web service for browsing an OKF bundle.

Unlike the static `visualize` output (a snapshot HTML), this serves the
visualization fresh on every request, so edits to the bundle on disk show up on
refresh. It also exposes small JSON endpoints for integrations.

Routes:
  GET /                  the interactive graph + document viewer (live)
  GET /api/graph         the graph JSON (nodes, edges, bodies, types)
  GET /api/concept/<id>  one concept's {frontmatter, body}
  GET /api/search?q=     concepts matching q in id/title/tags
  GET /healthz           liveness probe

Implemented on the standard library only (no extra dependencies).
"""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from reference_agent.bundle.document import OKFDocument, OKFDocumentError
from reference_agent.bundle.paths import concept_id_to_path, parse_concept_id
from reference_agent.viewer.generator import (
    _build_graph,
    _walk_concepts,
    render_visualization_html,
)

log = logging.getLogger(__name__)


def _search(bundle_root: Path, q: str) -> list[dict]:
    q = q.strip().lower()
    if not q:
        return []
    out: list[dict] = []
    for c in _walk_concepts(bundle_root):
        hay = " ".join([c.id, c.title, c.type, " ".join(c.tags)]).lower()
        if q in hay:
            out.append({"id": c.id, "title": c.title, "type": c.type,
                        "description": c.description})
    return out


class _Handler(BaseHTTPRequestHandler):
    # Set per-server via a subclass in serve().
    bundle_root: Path = Path(".")
    bundle_name: str | None = None

    server_version = "okf-serve"

    def _send(self, code: int, body, ctype: str = "text/html; charset=utf-8") -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        path = u.path
        try:
            if path in ("/", "/index.html"):
                html = render_visualization_html(
                    self.bundle_root, bundle_name=self.bundle_name
                )
                self._send(200, html)
            elif path == "/healthz":
                self._send(200, "ok", "text/plain; charset=utf-8")
            elif path == "/api/graph":
                graph = _build_graph(_walk_concepts(self.bundle_root))
                self._json(200, graph)
            elif path == "/api/search":
                q = parse_qs(u.query).get("q", [""])[0]
                self._json(200, {"results": _search(self.bundle_root, q)})
            elif path.startswith("/api/concept/"):
                cid = unquote(path[len("/api/concept/"):])
                self._concept(cid)
            else:
                self._concept_page(unquote(path))
        except Exception as e:  # never crash the server on one bad request
            log.exception("request failed: %s", path)
            self._send(500, f"Internal error: {e}", "text/plain; charset=utf-8")

    do_HEAD = do_GET

    def _concept_page(self, path: str) -> None:
        """Deep link: a bare concept path (with or without .md) opens the viewer
        focused on that concept. Unknown paths 404."""
        cid = path.strip("/")
        if cid.endswith(".md"):
            cid = cid[:-3]
        try:
            parts = parse_concept_id(cid)
        except ValueError:
            self._send(404, "Not found", "text/plain; charset=utf-8")
            return
        if not concept_id_to_path(self.bundle_root, parts).exists():
            self._send(404, "Not found", "text/plain; charset=utf-8")
            return
        html = render_visualization_html(
            self.bundle_root, bundle_name=self.bundle_name, initial_concept=cid
        )
        self._send(200, html)

    def _concept(self, cid: str) -> None:
        try:
            parts = parse_concept_id(cid)
        except ValueError:
            self._json(400, {"error": f"invalid concept id: {cid}"})
            return
        path = concept_id_to_path(self.bundle_root, parts)
        if not path.exists():
            self._json(404, {"error": f"unknown concept: {cid}"})
            return
        try:
            doc = OKFDocument.parse(path.read_text(encoding="utf-8"))
        except OKFDocumentError as e:
            self._json(500, {"error": str(e)})
            return
        self._json(200, {"id": cid, "frontmatter": doc.frontmatter, "body": doc.body})

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s %s", self.address_string(), fmt % args)


def serve(
    bundle_root: str | Path,
    *,
    host: str = "0.0.0.0",
    port: int = 8000,
    bundle_name: str | None = None,
) -> None:
    """Run the live OKF web service until interrupted."""
    bundle_root = Path(bundle_root)
    if not bundle_root.is_dir():
        raise SystemExit(f"--bundle is not a directory: {bundle_root}")

    handler = type("BoundHandler", (_Handler,), {
        "bundle_root": bundle_root, "bundle_name": bundle_name,
    })
    httpd = ThreadingHTTPServer((host, port), handler)
    shown = host if host not in ("0.0.0.0", "") else "localhost"
    log.info("Serving OKF bundle %s at http://%s:%d/  (Ctrl-C to stop)",
             bundle_root, shown, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down.")
    finally:
        httpd.server_close()
