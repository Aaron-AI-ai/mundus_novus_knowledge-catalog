"""Source-state manifest for incremental updates.

At generation time we record, per concept, a hash of the source file the
document was produced from. On a later run, `--since-last` compares the current
source hashes against this manifest to find everything that changed — new,
modified, or whose document is missing — independent of git (so uncommitted
edits are caught too).

The manifest lives at ``<bundle>/.okf-source.json`` and travels with the bundle,
so the baseline persists across machines when the bundle is version-controlled.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from reference_agent.bundle.paths import concept_id_to_path

log = logging.getLogger(__name__)

MANIFEST_NAME = ".okf-source.json"


def file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _manifest_path(bundle_root: str | Path) -> Path:
    return Path(bundle_root) / MANIFEST_NAME


def load(bundle_root: str | Path) -> dict[str, dict]:
    """Return the {concept_id: {path, sha256}} map, or {} when absent/unreadable."""
    p = _manifest_path(bundle_root)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    return files if isinstance(files, dict) else {}


def save(bundle_root: str | Path, files: dict[str, dict]) -> None:
    payload = {"version": 1, "files": files}
    _manifest_path(bundle_root).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def changed_concepts(
    source, bundle_root: str | Path
) -> tuple[list[tuple[str, ...]], list[tuple[str, ...]]]:
    """Compare current source against the manifest.

    Returns (changed, deleted):
      - changed: concept ids that are new, whose source hash differs, or whose
        document is missing on disk (so failures get retried).
      - deleted: concept ids present in the manifest but no longer in the source.

    With no manifest yet, every current concept is "changed" (first full run).
    """
    prev = load(bundle_root)
    root = Path(bundle_root)
    changed: list[tuple[str, ...]] = []
    current_ids: set[str] = set()
    for ref in source.list_concepts():
        cid = "/".join(ref.id)
        current_ids.add(cid)
        try:
            cur_hash = file_sha256(source.source_path(ref))
        except OSError:
            continue
        entry = prev.get(cid)
        doc_exists = concept_id_to_path(root, ref.id).exists()
        if entry is None or entry.get("sha256") != cur_hash or not doc_exists:
            changed.append(ref.id)
    deleted = [
        tuple(cid.split("/")) for cid in prev if cid not in current_ids
    ]
    return changed, deleted


def record_after_run(
    source, bundle_root: str | Path, processed_ids: list[tuple[str, ...]]
) -> None:
    """Advance the manifest after an enrich run.

    Only concepts processed this run move their baseline (untouched concepts
    keep their prior hash, so a change that wasn't regenerated stays flagged).
    A processed concept whose document failed to write is dropped from the
    manifest so it is retried next time. Entries for source files that no
    longer exist are pruned.
    """
    files = load(bundle_root)
    root = Path(bundle_root)
    processed = {"/".join(p) for p in processed_ids}

    current: dict[str, object] = {}
    for ref in source.list_concepts():
        cid = "/".join(ref.id)
        current[cid] = ref
        if cid not in processed:
            continue
        if concept_id_to_path(root, ref.id).exists():
            try:
                files[cid] = {
                    "path": ref.resource,
                    "sha256": file_sha256(source.source_path(ref)),
                }
            except OSError:
                files.pop(cid, None)
        else:
            files.pop(cid, None)

    # Prune manifest entries whose source file is gone.
    for cid in list(files):
        if cid not in current:
            files.pop(cid, None)

    save(bundle_root, files)
