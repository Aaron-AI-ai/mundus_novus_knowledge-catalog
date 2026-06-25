"""Multi-project (multi-root) collection and cross-project relationship graph.

Combines several code projects into one OKF bundle, namespaced by project, and
resolves dependencies (imports / extends / implements) between concepts across
projects into markdown links — deterministically, from the parsed source, with
no LLM. The bundle viewer turns those links into graph edges, so the result is a
cross-project dependency graph.

This is the structure-only path (frontmatter + a `# 관계`/Relationships section);
prose enrichment can be layered on top later via the normal agent pipeline.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from reference_agent.bundle.document import OKFDocument
from reference_agent.sources.code import CodeSource

_GENERIC_RE = re.compile(r"<.*>")


def _simple_name(type_ref: str) -> str:
    """Simple class name from a possibly-qualified, possibly-generic type."""
    bare = _GENERIC_RE.sub("", type_ref).strip()
    return bare.split(".")[-1].strip()


class CodeWorkspace:
    """A set of code projects collected into one namespaced bundle."""

    def __init__(self, roots: list[str | Path]):
        self.projects: list[tuple[str, CodeSource]] = []
        for root in roots:
            src = CodeSource(root)
            ns = src.repo_name  # directory name, used as the project namespace
            self.projects.append((ns, src))

    def all_concepts(self) -> list[tuple[str, object, CodeSource]]:
        out: list[tuple[str, object, CodeSource]] = []
        for ns, src in self.projects:
            for ref in src.list_concepts():
                out.append((ns, ref, src))
        return out

    def fqcn_index(self) -> dict[str, tuple[str, object, CodeSource]]:
        index: dict[str, tuple[str, object, CodeSource]] = {}
        for ns, ref, src in self.all_concepts():
            index[src.fqcn(ref)] = (ns, ref, src)
        return index


def _doc_relpath(ns: str, ref) -> Path:
    """Path of a concept doc within the combined bundle: <ns>/<id...>.md"""
    return Path(ns, *ref.id).with_suffix(".md")


def _relationships(meta: dict, self_fqcn: str, index: dict) -> list[tuple[str, str]]:
    """Resolve a concept's in-workspace dependencies.

    Returns a list of (kind, target_fqcn) where kind is extends/implements/uses,
    deduplicated, for imports/supertypes that map to a concept in the workspace.
    """
    extends_simple = {_simple_name(e) for e in meta.get("extends", [])}
    implements_simple = {_simple_name(i) for i in meta.get("implements", [])}
    edges: list[tuple[str, str]] = []
    seen: set[str] = set()
    for imp in meta.get("imports", []):
        if imp not in index or imp == self_fqcn or imp in seen:
            continue
        simple = imp.split(".")[-1]
        if simple in extends_simple:
            kind = "extends"
        elif simple in implements_simple:
            kind = "implements"
        else:
            kind = "uses"
        seen.add(imp)
        edges.append((kind, imp))
    return edges


_KIND_LABEL_KO = {"extends": "상속", "implements": "구현", "uses": "사용"}
_KIND_LABEL_EN = {"extends": "extends", "implements": "implements", "uses": "uses"}

_REL_BEGIN = "<!-- okf:rel -->"
_REL_END = "<!-- okf:rel:end -->"


def _render_relationship_block(
    edges: list[tuple[str, str]],
    doc_dir: Path,
    out: Path,
    index: dict,
    *,
    is_ko: bool,
) -> str:
    """Marker-wrapped # 관계 / # Relationships block, or '' when no edges."""
    if not edges:
        return ""
    labels = _KIND_LABEL_KO if is_ko else _KIND_LABEL_EN
    heading = "# 관계" if is_ko else "# Relationships"
    lines = [_REL_BEGIN, "", heading, ""]
    for kind, target_fqcn in edges:
        t_ns, t_ref, t_src = index[target_fqcn]
        rel = os.path.relpath(out / _doc_relpath(t_ns, t_ref), start=doc_dir)
        coord = t_src.artifact or t_ns
        lines.append(
            f"- {labels[kind]}: [{t_src.fqcn(t_ref).split('.')[-1]}]({rel}) — `{coord}`"
        )
    lines += ["", _REL_END]
    return "\n".join(lines)


def _strip_rel_block(body: str) -> str:
    start = body.find(_REL_BEGIN)
    if start == -1:
        return body.rstrip()
    end = body.find(_REL_END, start)
    if end == -1:
        return body[:start].rstrip()
    return (body[:start] + body[end + len(_REL_END):]).rstrip()


def inject_relationships(
    roots: list[str | Path], bundle_root: str | Path, *, language: str = "Korean"
) -> dict:
    """Append a deterministic cross-project # 관계 block to each already-written
    doc in a combined, project-namespaced bundle. Idempotent. No LLM.

    Returns {concepts, edges, cross_project_edges, missing_docs}.
    """
    out = Path(bundle_root)
    ws = CodeWorkspace(roots)
    index = ws.fqcn_index()
    is_ko = (language or "").strip().lower() == "korean"

    edges_total = cross = missing = 0
    for ns, ref, src in ws.all_concepts():
        doc_path = out / _doc_relpath(ns, ref)
        if not doc_path.exists():
            missing += 1
            continue
        meta = src.read_concept(ref)
        edges = _relationships(meta, src.fqcn(ref), index)
        block = _render_relationship_block(
            edges, doc_path.parent, out, index, is_ko=is_ko
        )
        doc = OKFDocument.parse(doc_path.read_text(encoding="utf-8"))
        body = _strip_rel_block(doc.body)
        if block:
            body = (body + "\n\n" + block).strip() + "\n"
            edges_total += len(edges)
            cross += sum(1 for _, t in edges if index[t][0] != ns)
        else:
            body = body + "\n" if body and not body.endswith("\n") else body
        doc.body = body
        doc_path.write_text(doc.serialize(), encoding="utf-8")

    return {
        "concepts": len(ws.all_concepts()),
        "edges": edges_total,
        "cross_project_edges": cross,
        "missing_docs": missing,
    }


def build_relationship_bundle(
    roots: list[str | Path], out: str | Path, *, language: str = "Korean"
) -> dict:
    """Write a structure-only, cross-project relationship bundle. No LLM.

    Returns counts: {concepts, projects, edges, cross_project_edges}.
    """
    out = Path(out)
    ws = CodeWorkspace(roots)
    index = ws.fqcn_index()
    concepts = ws.all_concepts()
    is_ko = (language or "").strip().lower() == "korean"
    labels = _KIND_LABEL_KO if is_ko else _KIND_LABEL_EN
    rel_heading = "# 관계" if is_ko else "# Relationships"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    total_edges = 0
    cross_edges = 0
    for ns, ref, src in concepts:
        meta = src.read_concept(ref)
        self_fqcn = src.fqcn(ref)
        edges = _relationships(meta, self_fqcn, index)

        doc_path = out / _doc_relpath(ns, ref)
        doc_dir = doc_path.parent

        lines: list[str] = []
        if edges:
            lines.append(rel_heading)
            lines.append("")
            for kind, target_fqcn in edges:
                t_ns, t_ref, t_src = index[target_fqcn]
                target_path = out / _doc_relpath(t_ns, t_ref)
                rel = os.path.relpath(target_path, start=doc_dir)
                label = labels[kind]
                coord = t_src.artifact or t_ns
                lines.append(
                    f"- {label}: [{t_src.fqcn(t_ref).split('.')[-1]}]({rel}) "
                    f"— `{coord}`"
                )
                total_edges += 1
                if t_ns != ns:
                    cross_edges += 1
            lines.append("")

        class_name = meta.get("class_name") or ref.id[-1]
        desc = (
            f"{ns}의 {class_name} (관계 그래프; 본문 미생성)"
            if is_ko
            else f"{class_name} in {ns} (relationship graph; body not generated)"
        )
        fm = {
            "type": ref.type,
            "title": class_name,
            "description": desc,
            "fqcn": self_fqcn,
            "artifact": src.artifact or ns,
            "project": ns,
            "tags": [ns, ref.type],
            "timestamp": now,
        }
        body = "\n".join(lines) if lines else (
            "관계 없음(외부 의존만)." if is_ko else "No in-workspace relationships."
        )
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text(OKFDocument(frontmatter=fm, body=body).serialize(), encoding="utf-8")

    return {
        "projects": len(ws.projects),
        "concepts": len(concepts),
        "edges": total_edges,
        "cross_project_edges": cross_edges,
    }
