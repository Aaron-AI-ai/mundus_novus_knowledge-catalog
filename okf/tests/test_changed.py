from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from reference_agent.cli import _changed_concept_ids
from reference_agent.sources.code import CodeSource

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available"
)

_A = """\
package com.acme.lib.utils;
public class Alpha {
    public void a() {}
}
"""
_B = """\
package com.acme.lib.api;
public class Beta {
    public void b() {}
}
"""


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True, capture_output=True, text=True,
    )


def _seed_repo(root: Path) -> None:
    # Two distinct packages so the shared package prefix (com.acme.lib) is
    # stripped, leaving subpackage-qualified ids like utils/Alpha.
    (root / "src/main/java/com/acme/lib/utils").mkdir(parents=True)
    (root / "src/main/java/com/acme/lib/api").mkdir(parents=True)
    (root / "src/main/java/com/acme/lib/utils/Alpha.java").write_text(_A, encoding="utf-8")
    (root / "src/main/java/com/acme/lib/api/Beta.java").write_text(_B, encoding="utf-8")
    (root / "settings.gradle").write_text("rootProject.name = 'acme'\n", encoding="utf-8")
    (root / "build.gradle").write_text("group = 'com.acme'\nversion = '1.0'\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")


def test_changed_since_maps_only_modified_files(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _seed_repo(root)

    # Modify only Alpha after the baseline commit.
    (root / "src/main/java/com/acme/lib/utils/Alpha.java").write_text(
        _A + "// touched\n", encoding="utf-8"
    )
    src = CodeSource(root)
    ids = _changed_concept_ids(src, root, "HEAD")
    assert ids == [("utils", "Alpha")]


def test_changed_since_includes_untracked_new_file(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _seed_repo(root)

    (root / "src/main/java/com/acme/lib/utils/Gamma.java").write_text(
        "package com.acme.lib.utils;\npublic class Gamma {}\n", encoding="utf-8"
    )
    src = CodeSource(root)
    ids = _changed_concept_ids(src, root, "HEAD")
    assert ("utils", "Gamma") in ids


def test_changed_since_empty_when_nothing_changed(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _seed_repo(root)
    src = CodeSource(root)
    assert _changed_concept_ids(src, root, "HEAD") == []
