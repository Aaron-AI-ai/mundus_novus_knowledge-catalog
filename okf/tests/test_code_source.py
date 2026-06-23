from __future__ import annotations

from pathlib import Path

from reference_agent.bundle.code_embed import build_api_block, inject_code
from reference_agent.sources.code import CodeSource

_STRING_UTILS = """\
package com.acme.lib.utils;

import java.util.List;

public class StringUtils {
    public static final String EMPTY = "";

    public StringUtils() {}

    public static boolean isBlank(String s) { return s == null; }
    public String trimRight(String s) { return s; }
    private void helper() {}
}
"""

_FOO_IFACE = """\
package com.acme.lib.api;

public interface Foo {
    void run();
}
"""


def _make_tree(root: Path) -> None:
    base = root / "src/main/java/com/acme/lib"
    (base / "utils").mkdir(parents=True)
    (base / "api").mkdir(parents=True)
    (base / "utils/StringUtils.java").write_text(_STRING_UTILS, encoding="utf-8")
    (base / "api/Foo.java").write_text(_FOO_IFACE, encoding="utf-8")
    (root / "settings.gradle").write_text(
        "rootProject.name = 'acme-lib'\n", encoding="utf-8"
    )
    (root / "build.gradle").write_text(
        "group = 'com.acme'\nversion = '1.2.3'\n", encoding="utf-8"
    )


def test_list_concepts_ids_types_and_artifact(tmp_path: Path):
    _make_tree(tmp_path)
    src = CodeSource(tmp_path)
    assert src.artifact == "com.acme:acme-lib:1.2.3"

    by_id = {c.id_str: c for c in src.list_concepts()}
    # Shared package prefix (com.acme.lib) is stripped.
    assert set(by_id) == {"utils/StringUtils", "api/Foo"}
    assert by_id["utils/StringUtils"].type == "Java Class"
    assert by_id["api/Foo"].type == "Java Interface"
    assert src.fqcn(by_id["utils/StringUtils"]) == "com.acme.lib.utils.StringUtils"


def test_build_api_block_preserves_static_and_skips_private(tmp_path: Path):
    _make_tree(tmp_path)
    src = CodeSource(tmp_path)
    ref = src.find(("utils", "StringUtils"))
    api = build_api_block(src.full_source(ref), "StringUtils", "English")
    assert "public static boolean isBlank(String s)" in api
    assert "public String trimRight(String s)" in api
    assert "public static final String EMPTY" in api
    assert "helper" not in api  # private methods excluded


def test_inject_code_sets_coordinates_and_is_idempotent(tmp_path: Path):
    _make_tree(tmp_path)
    src = CodeSource(tmp_path)
    ref = src.find(("utils", "StringUtils"))

    doc = tmp_path / "StringUtils.md"
    doc.write_text(
        "---\ntype: Java Class\ntitle: StringUtils\n"
        "description: util.\nresource: src/main/java/x.java\n"
        "timestamp: '2026-01-01T00:00:00+00:00'\n---\n\n# Desc\n\nprose.\n",
        encoding="utf-8",
    )
    for _ in range(2):  # idempotency
        inject_code(
            doc,
            fqcn=src.fqcn(ref),
            artifact=src.artifact,
            full_source=src.full_source(ref),
            class_name="StringUtils",
            mode="hybrid",
            language="English",
        )
    text = doc.read_text(encoding="utf-8")
    assert text.count("<!-- okf:code -->") == 1
    assert "fqcn: com.acme.lib.utils.StringUtils" in text
    assert "artifact: com.acme:acme-lib:1.2.3" in text
    assert "resource:" not in text
    assert "# API" in text
    assert "# Source" in text  # small file → full source embedded
    assert "prose." in text  # original body preserved


def test_inject_signatures_mode_omits_source(tmp_path: Path):
    _make_tree(tmp_path)
    src = CodeSource(tmp_path)
    ref = src.find(("utils", "StringUtils"))
    doc = tmp_path / "S.md"
    doc.write_text(
        "---\ntype: Java Class\ntitle: StringUtils\ndescription: u.\n"
        "timestamp: '2026-01-01T00:00:00+00:00'\n---\n\nbody\n",
        encoding="utf-8",
    )
    inject_code(
        doc, fqcn=src.fqcn(ref), artifact=src.artifact,
        full_source=src.full_source(ref), class_name="StringUtils",
        mode="signatures", language="English",
    )
    text = doc.read_text(encoding="utf-8")
    assert "# API" in text
    assert "# Source" not in text
