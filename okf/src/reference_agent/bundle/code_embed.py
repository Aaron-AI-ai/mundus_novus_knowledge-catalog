"""Deterministic code embedding for the code source.

The LLM writes the prose; this module appends an authoritative, verbatim code
section (extracted by regex from the real source — never model-generated) so a
downstream consumer can use the library from the document alone, without access
to the source repository.
"""

from __future__ import annotations

import re
from pathlib import Path

from reference_agent.bundle.document import OKFDocument

# Markers delimiting the auto-injected block, so re-runs replace rather than
# duplicate it.
_BEGIN = "<!-- okf:code -->"
_END = "<!-- okf:code:end -->"

# Below this size a file is embedded in full; above it, hybrid mode embeds a
# truncated excerpt plus the complete signature list.
_SMALL_FILE_CHARS = 6_000
_TRUNCATE_CHARS = 6_000

_TYPE_DECL_RE = re.compile(
    r"(?P<mods>(?:(?:public|final|abstract|sealed|non-sealed|strictfp)\s+)*)"
    r"(?P<kind>class|interface|enum|record|@interface)\s+"
    r"(?P<name>[A-Za-z_]\w*)"
    r"(?P<rest>[^{]*)\{",
)
_METHOD_RE = re.compile(
    r"(?P<vis>public|protected)\s+"
    r"(?P<pre>(?:(?:static|final|synchronized|abstract|default|native)\s+)*)"
    r"(?:<[^>]+>\s+)?"
    r"(?P<ret>[\w.$<>\[\],?\s]+?)\s+"
    r"(?P<name>[A-Za-z_]\w*)\s*\((?P<params>[^)]*)\)"
    r"\s*(?P<throws>throws[\w.,\s]+?)?[{;]",
)

# Modifiers that affect the call contract and are worth preserving in the API
# skeleton; impl-only ones (final/synchronized/native) are dropped for brevity.
_CONTRACT_MODS = ("static", "abstract", "default")
_FIELD_RE = re.compile(
    r"^\s*(?P<vis>public)\s+"
    r"(?P<mods>(?:static\s+|final\s+|volatile\s+|transient\s+)*)"
    r"(?P<type>[\w.$<>\[\],?]+)\s+"
    r"(?P<name>[A-Za-z_]\w*)\s*(?:=[^;]+)?;",
    re.MULTILINE,
)


def _dedup(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for s in seq:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _header(src: str) -> str:
    m = _TYPE_DECL_RE.search(src)
    if not m:
        return ""
    mods = " ".join(m.group("mods").split())
    rest = " ".join(m.group("rest").split())
    parts = [p for p in (mods, m.group("kind"), m.group("name"), rest) if p]
    return " ".join(parts).strip()


def _constructors(src: str, class_name: str) -> list[str]:
    if not class_name:
        return []
    ctor_re = re.compile(
        r"(?P<vis>public|protected)\s+" + re.escape(class_name)
        + r"\s*\((?P<params>[^)]*)\)\s*(?:throws[\w.,\s]+?)?\{"
    )
    out = []
    for m in ctor_re.finditer(src):
        params = " ".join(m.group("params").split())
        out.append(f"{m.group('vis')} {class_name}({params})")
    return _dedup(out)


def _methods(src: str) -> list[str]:
    out = []
    for m in _METHOD_RE.finditer(src):
        pre_mods = [w for w in m.group("pre").split() if w in _CONTRACT_MODS]
        pre = (" ".join(pre_mods) + " ") if pre_mods else ""
        ret = " ".join(m.group("ret").split())
        params = " ".join(m.group("params").split())
        throws = (" " + " ".join(m.group("throws").split())) if m.group("throws") else ""
        out.append(f"{m.group('vis')} {pre}{ret} {m.group('name')}({params}){throws}")
    return _dedup(out)


def _fields(src: str) -> list[str]:
    out = []
    for m in _FIELD_RE.finditer(src):
        mods = " ".join(m.group("mods").split())
        decl = " ".join(p for p in ("public", mods, m.group("type"), m.group("name")) if p)
        out.append(decl + ";")
    return _dedup(out)


def build_api_block(full_source: str, class_name: str, language: str) -> str:
    header = _header(full_source)
    ctors = _constructors(full_source, class_name)
    fields = _fields(full_source)
    methods = _methods(full_source)

    lines: list[str] = ["```java"]
    if header:
        lines.append(header + " {")
    if fields:
        lines.append("    // fields")
        lines.extend(f"    {f}" for f in fields)
    if ctors:
        if fields:
            lines.append("")
        lines.append("    // constructors")
        lines.extend(f"    {c};" for c in ctors)
    if methods:
        if fields or ctors:
            lines.append("")
        lines.append("    // methods")
        lines.extend(f"    {m};" for m in methods)
    if header:
        lines.append("}")
    lines.append("```")
    return "\n".join(lines)


def build_source_block(full_source: str, mode: str, max_chars: int) -> tuple[str, bool]:
    """Return (fenced source markdown, truncated?). Empty string when mode does
    not embed source body."""
    if mode == "signatures":
        return "", False
    src = full_source
    truncated = False
    if mode == "hybrid" and len(src) > _SMALL_FILE_CHARS:
        src = src[:max_chars]
        truncated = True
    elif mode == "full":
        pass  # full source, no truncation
    return "```java\n" + src.rstrip() + "\n```", truncated


def _strip_existing(body: str) -> str:
    start = body.find(_BEGIN)
    if start == -1:
        return body.rstrip()
    end = body.find(_END, start)
    if end == -1:
        return body[:start].rstrip()
    return (body[:start] + body[end + len(_END):]).rstrip()


def _reorder_frontmatter(fm: dict) -> dict:
    order = ("type", "title", "description", "fqcn", "artifact", "tags", "timestamp")
    out: dict = {}
    for k in order:
        if k in fm:
            out[k] = fm[k]
    for k, v in fm.items():
        if k not in out:
            out[k] = v
    return out


def inject_code(
    path: Path,
    *,
    fqcn: str,
    artifact: str | None,
    full_source: str,
    class_name: str,
    mode: str,
    language: str,
) -> None:
    """Set coordinate frontmatter (fqcn/artifact, dropping any link-style
    resource) and append the verbatim API (+ source) block to the document at
    `path`. Idempotent: an existing injected block is replaced."""
    doc = OKFDocument.parse(path.read_text(encoding="utf-8"))

    fm = dict(doc.frontmatter)
    fm["fqcn"] = fqcn
    if artifact:
        fm["artifact"] = artifact
    # Drop any local-path / URL resource — the embedded code is the anchor now.
    fm.pop("resource", None)
    doc.frontmatter = _reorder_frontmatter(fm)

    is_ko = (language or "").strip().lower() == "korean"
    api_heading = "# API"
    src_heading = "# 소스 코드" if is_ko else "# Source"

    api_md = build_api_block(full_source, class_name, language)
    source_md, truncated = build_source_block(full_source, mode, _TRUNCATE_CHARS)

    blocks = [_BEGIN, "", api_heading, "", api_md]
    if source_md:
        note = ""
        if truncated:
            total = full_source.count("\n") + 1
            note = (
                f"\n\n> 전체 {total}줄 중 일부만 포함했습니다. 전체 공개 API는 위 `API` 절을 참고하세요."
                if is_ko
                else f"\n\n> Truncated excerpt of a {total}-line file. The full public API is in the `API` section above."
            )
        blocks += ["", src_heading, "", source_md.rstrip() + note]
    blocks += ["", _END]

    body = _strip_existing(doc.body)
    doc.body = (body + "\n\n" + "\n".join(blocks)).strip() + "\n"

    path.write_text(doc.serialize(), encoding="utf-8")
