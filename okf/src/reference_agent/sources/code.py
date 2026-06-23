from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from reference_agent.sources.base import ConceptRef, Source

# Lightweight Java parsing — good enough to derive a concept id, an OKF type,
# and a structured hint. The full source is also handed to the LLM, so this
# parse only needs to be approximately right, not a real compiler frontend.
_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)
_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.*]+)\s*;", re.MULTILINE)
_TYPE_DECL_RE = re.compile(
    r"(?P<mods>(?:(?:public|final|abstract|sealed|non-sealed|strictfp)\s+)*)"
    r"(?P<kind>class|interface|enum|record|@interface)\s+"
    r"(?P<name>[A-Za-z_]\w*)"
    r"(?P<rest>[^{]*)\{",
)
_EXTENDS_RE = re.compile(r"\bextends\s+([\w.<>,\s]+?)(?:\bimplements\b|$)")
_IMPLEMENTS_RE = re.compile(r"\bimplements\s+([\w.<>,\s]+)$")
# A best-effort public-method signature matcher: a `public` declaration whose
# parameter list is followed by `{`, `;`, or `throws` (skips field decls).
_METHOD_RE = re.compile(
    r"public\s+(?:(?:static|final|synchronized|abstract|default|native)\s+)*"
    r"(?:<[^>]+>\s+)?"
    r"(?P<ret>[\w.$<>\[\],?\s]+?)\s+"
    r"(?P<name>[A-Za-z_]\w*)\s*\((?P<params>[^)]*)\)"
    r"\s*(?:throws[\w.,\s]+)?[{;]",
)

_KIND_TO_TYPE = {
    "class": "Java Class",
    "interface": "Java Interface",
    "enum": "Java Enum",
    "record": "Java Record",
    "@interface": "Java Annotation",
}

_SKIP_DIR_PARTS = {".git", "build", "target", "out", "bin", "node_modules", ".gradle"}
_MAX_SOURCE_CHARS = 12_000

# Gradle coordinate detection (best-effort, for the `artifact` frontmatter).
_GRADLE_GROUP_RE = re.compile(r"""^\s*group\s*=?\s*['"]([\w.\-]+)['"]""", re.MULTILINE)
_GRADLE_VERSION_RE = re.compile(r"""^\s*version\s*=?\s*['"]([\w.\-]+)['"]""", re.MULTILINE)
_GRADLE_ROOTNAME_RE = re.compile(
    r"""rootProject\.name\s*=\s*['"]([\w.\-]+)['"]"""
)


def _detect_artifact(root: Path) -> str | None:
    """Best-effort Maven/Gradle coordinate (group:artifact:version) read from
    build.gradle + settings.gradle at the source root. Returns None if not
    determinable."""
    build = root / "build.gradle"
    settings = root / "settings.gradle"
    group = version = name = None
    if build.is_file():
        text = build.read_text(encoding="utf-8", errors="replace")
        gm = _GRADLE_GROUP_RE.search(text)
        vm = _GRADLE_VERSION_RE.search(text)
        group = gm.group(1) if gm else None
        version = vm.group(1) if vm else None
    if settings.is_file():
        nm = _GRADLE_ROOTNAME_RE.search(
            settings.read_text(encoding="utf-8", errors="replace")
        )
        name = nm.group(1) if nm else None
    if not name:
        name = root.name
    if group and name and version:
        return f"{group}:{name}:{version}"
    return None


def _segment(s: str) -> str:
    """Coerce an arbitrary path/identifier fragment into a valid concept-id
    segment ([A-Za-z0-9_][A-Za-z0-9_.-]*)."""
    cleaned = re.sub(r"[^A-Za-z0-9_.\-]", "_", s)
    if not cleaned or not re.match(r"[A-Za-z0-9_]", cleaned[0]):
        cleaned = "_" + cleaned
    return cleaned


def _common_package_prefix(packages: list[tuple[str, ...]]) -> tuple[str, ...]:
    if not packages:
        return ()
    prefix = list(packages[0])
    for pkg in packages[1:]:
        i = 0
        while i < len(prefix) and i < len(pkg) and prefix[i] == pkg[i]:
            i += 1
        prefix = prefix[:i]
        if not prefix:
            break
    return tuple(prefix)


class CodeSource(Source):
    """A Source that treats a local source-code tree as a knowledge corpus.

    Each Java source file becomes one OKF concept. The concept id mirrors the
    package hierarchy with the longest shared package prefix stripped, so a
    file in ``kr.co.openlabs.fico.framework.utils`` lands at
    ``utils/StringUtils``.
    """

    name = "code"

    def __init__(
        self,
        root: str | Path,
        *,
        include_tests: bool = False,
        repo_name: str | None = None,
        artifact: str | None = None,
    ):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError(f"--path is not a directory: {self.root}")
        self.include_tests = include_tests
        self.repo_name = repo_name or self.root.name
        self.artifact = artifact or _detect_artifact(self.root)
        self._concepts_cache: list[ConceptRef] | None = None
        self._by_id: dict[tuple[str, ...], Path] = {}

    def source_path(self, ref: ConceptRef) -> Path:
        """Absolute path to the source file backing a concept."""
        return self._path_for(ref)

    def full_source(self, ref: ConceptRef) -> str:
        """Complete (untruncated) source text for a concept's file."""
        return self._path_for(ref).read_text(encoding="utf-8", errors="replace")

    def fqcn(self, ref: ConceptRef) -> str:
        """Fully-qualified class name: package + simple class name."""
        pkg = ref.hint.get("package") or ""
        name = ref.hint.get("class_name") or ref.id[-1]
        return f"{pkg}.{name}" if pkg else name

    def find_by_path(self, path: str | Path) -> ConceptRef | None:
        """Return the concept backed by the given source file, or None if the
        path is not a documented source file (e.g. a deleted file, a test file
        while tests are excluded, or a non-Java file)."""
        self.list_concepts()
        target = Path(path).resolve()
        for ref in self._concepts_cache or []:
            backing = self._by_id.get(ref.id)
            if backing is not None and backing.resolve() == target:
                return ref
        return None

    # -- discovery ---------------------------------------------------------

    def _iter_java_files(self) -> list[Path]:
        files: list[Path] = []
        for p in sorted(self.root.rglob("*.java")):
            parts = set(p.relative_to(self.root).parts)
            if parts & _SKIP_DIR_PARTS:
                continue
            if not self.include_tests and "test" in parts:
                continue
            files.append(p)
        return files

    @staticmethod
    def _parse_header(text: str) -> dict[str, Any]:
        pkg_m = _PACKAGE_RE.search(text)
        package = pkg_m.group(1) if pkg_m else ""
        decl_m = _TYPE_DECL_RE.search(text)
        if decl_m:
            kind = decl_m.group("kind")
            name = decl_m.group("name")
            rest = decl_m.group("rest") or ""
        else:
            kind, name, rest = "class", "", ""
        return {"package": package, "kind": kind, "name": name, "rest": rest}

    def list_concepts(self) -> list[ConceptRef]:
        if self._concepts_cache is not None:
            return self._concepts_cache

        parsed: list[tuple[Path, dict[str, Any]]] = []
        for path in self._iter_java_files():
            try:
                head = path.read_text(encoding="utf-8", errors="replace")[:4000]
            except OSError:
                continue
            parsed.append((path, self._parse_header(head)))

        packages = [
            tuple(h["package"].split(".")) for _, h in parsed if h["package"]
        ]
        base = _common_package_prefix(packages)

        concepts: list[ConceptRef] = []
        self._by_id = {}
        for path, head in parsed:
            name = head["name"] or path.stem
            pkg_parts = tuple(head["package"].split(".")) if head["package"] else ()
            sub = pkg_parts[len(base):] if pkg_parts[: len(base)] == base else pkg_parts
            id_parts = tuple(_segment(s) for s in (*sub, name))
            rel = path.relative_to(self.root).as_posix()
            ref = ConceptRef(
                id=id_parts,
                type=_KIND_TO_TYPE.get(head["kind"], "Java Class"),
                resource=rel,
                hint={
                    "package": head["package"],
                    "kind": head["kind"],
                    "class_name": name,
                },
            )
            concepts.append(ref)
            self._by_id[id_parts] = path

        self._concepts_cache = concepts
        return concepts

    # -- reading -----------------------------------------------------------

    def _path_for(self, ref: ConceptRef) -> Path:
        if not self._by_id:
            self.list_concepts()
        path = self._by_id.get(ref.id)
        if path is None:
            raise ValueError(f"No source file for concept: {ref.id_str}")
        return path

    @staticmethod
    def _public_methods(text: str) -> list[str]:
        methods: list[str] = []
        for m in _METHOD_RE.finditer(text):
            ret = " ".join(m.group("ret").split())
            params = " ".join(m.group("params").split())
            methods.append(f"{ret} {m.group('name')}({params})")
        # De-dup while preserving order; cap to keep the hint compact.
        seen: set[str] = set()
        out: list[str] = []
        for sig in methods:
            if sig not in seen:
                seen.add(sig)
                out.append(sig)
        return out[:40]

    def read_concept(self, ref: ConceptRef) -> dict[str, Any]:
        path = self._path_for(ref)
        text = path.read_text(encoding="utf-8", errors="replace")
        head = self._parse_header(text)
        rest = head["rest"]
        extends_m = _EXTENDS_RE.search(rest)
        implements_m = _IMPLEMENTS_RE.search(rest)
        imports = _IMPORT_RE.findall(text)

        source = text
        truncated = False
        if len(source) > _MAX_SOURCE_CHARS:
            source = source[:_MAX_SOURCE_CHARS]
            truncated = True

        return {
            "id": ref.id_str,
            "type": ref.type,
            "package": head["package"],
            "class_name": head["name"] or path.stem,
            "kind": head["kind"],
            "extends": [s.strip() for s in (extends_m.group(1).split(",") if extends_m else []) if s.strip()],
            "implements": [s.strip() for s in (implements_m.group(1).split(",") if implements_m else []) if s.strip()],
            "imports": imports,
            "public_methods": self._public_methods(text),
            "loc": text.count("\n") + 1,
            "path": ref.resource,
            "repo": self.repo_name,
            "source_truncated": truncated,
            "source": source,
        }
