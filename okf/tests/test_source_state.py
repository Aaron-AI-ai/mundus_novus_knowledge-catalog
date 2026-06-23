from __future__ import annotations

from pathlib import Path

from reference_agent.bundle import source_state
from reference_agent.bundle.paths import concept_id_to_path
from reference_agent.sources.code import CodeSource

_A = "package com.acme.lib.utils;\npublic class Alpha { public void a() {} }\n"
_B = "package com.acme.lib.api;\npublic class Beta { public void b() {} }\n"


def _seed_source(root: Path) -> None:
    (root / "src/main/java/com/acme/lib/utils").mkdir(parents=True)
    (root / "src/main/java/com/acme/lib/api").mkdir(parents=True)
    (root / "src/main/java/com/acme/lib/utils/Alpha.java").write_text(_A, encoding="utf-8")
    (root / "src/main/java/com/acme/lib/api/Beta.java").write_text(_B, encoding="utf-8")


def _fake_docs(bundle: Path, src: CodeSource) -> None:
    """Create placeholder docs for every concept so doc-existence checks pass."""
    for ref in src.list_concepts():
        p = concept_id_to_path(bundle, ref.id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("---\ntype: x\ntitle: x\ndescription: x\ntimestamp: t\n---\n\nbody\n")


def test_first_run_marks_everything_changed(tmp_path: Path):
    srcdir = tmp_path / "src"; srcdir.mkdir(); _seed_source(srcdir)
    bundle = tmp_path / "bundle"; bundle.mkdir()
    src = CodeSource(srcdir)
    _fake_docs(bundle, src)
    changed, deleted = source_state.changed_concepts(src, bundle)
    assert set(changed) == {("utils", "Alpha"), ("api", "Beta")}
    assert deleted == []


def test_record_then_no_changes(tmp_path: Path):
    srcdir = tmp_path / "src"; srcdir.mkdir(); _seed_source(srcdir)
    bundle = tmp_path / "bundle"; bundle.mkdir()
    src = CodeSource(srcdir)
    _fake_docs(bundle, src)
    source_state.record_after_run(src, bundle, [("utils", "Alpha"), ("api", "Beta")])
    changed, deleted = source_state.changed_concepts(src, bundle)
    assert changed == [] and deleted == []


def test_modified_source_is_detected(tmp_path: Path):
    srcdir = tmp_path / "src"; srcdir.mkdir(); _seed_source(srcdir)
    bundle = tmp_path / "bundle"; bundle.mkdir()
    src = CodeSource(srcdir)
    _fake_docs(bundle, src)
    source_state.record_after_run(src, bundle, [("utils", "Alpha"), ("api", "Beta")])

    (srcdir / "src/main/java/com/acme/lib/utils/Alpha.java").write_text(
        _A + "// changed\n", encoding="utf-8"
    )
    changed, _ = source_state.changed_concepts(src, bundle)
    assert changed == [("utils", "Alpha")]


def test_missing_doc_is_retried(tmp_path: Path):
    srcdir = tmp_path / "src"; srcdir.mkdir(); _seed_source(srcdir)
    bundle = tmp_path / "bundle"; bundle.mkdir()
    src = CodeSource(srcdir)
    _fake_docs(bundle, src)
    source_state.record_after_run(src, bundle, [("utils", "Alpha"), ("api", "Beta")])

    # Document deleted -> concept must be flagged again even if source is same.
    concept_id_to_path(bundle, ("utils", "Alpha")).unlink()
    changed, _ = source_state.changed_concepts(src, bundle)
    assert ("utils", "Alpha") in changed


def test_untouched_concept_keeps_baseline_on_partial_run(tmp_path: Path):
    srcdir = tmp_path / "src"; srcdir.mkdir(); _seed_source(srcdir)
    bundle = tmp_path / "bundle"; bundle.mkdir()
    src = CodeSource(srcdir)
    _fake_docs(bundle, src)
    source_state.record_after_run(src, bundle, [("utils", "Alpha"), ("api", "Beta")])

    # Both sources change, but only Alpha is reprocessed this run.
    (srcdir / "src/main/java/com/acme/lib/utils/Alpha.java").write_text(_A + "//1\n", encoding="utf-8")
    (srcdir / "src/main/java/com/acme/lib/api/Beta.java").write_text(_B + "//2\n", encoding="utf-8")
    source_state.record_after_run(src, bundle, [("utils", "Alpha")])

    changed, _ = source_state.changed_concepts(src, bundle)
    # Beta still flagged (not reprocessed); Alpha now up to date.
    assert changed == [("api", "Beta")]


def test_deleted_source_reported_and_pruned(tmp_path: Path):
    srcdir = tmp_path / "src"; srcdir.mkdir(); _seed_source(srcdir)
    bundle = tmp_path / "bundle"; bundle.mkdir()
    src = CodeSource(srcdir)
    _fake_docs(bundle, src)
    source_state.record_after_run(src, bundle, [("utils", "Alpha"), ("api", "Beta")])

    # Remove Beta's source; a fresh CodeSource no longer lists it.
    (srcdir / "src/main/java/com/acme/lib/api/Beta.java").unlink()
    src2 = CodeSource(srcdir)
    changed, deleted = source_state.changed_concepts(src2, bundle)
    assert ("api", "Beta") in deleted
    # After recording, the stale entry is pruned from the manifest.
    source_state.record_after_run(src2, bundle, [("utils", "Alpha")])
    assert "api/Beta" not in source_state.load(bundle)
