from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from reference_agent.agent import DEFAULT_MODEL
from reference_agent.bundle.paths import parse_concept_id
from reference_agent.runner import ReferenceRunner
from reference_agent.sources.bigquery import BigQuerySource
from reference_agent.sources.code import CodeSource

_SOURCES = ("bq", "code")


def _git_changed_java(root: Path, ref: str) -> list[Path]:
    """Absolute paths of *.java files changed since `ref` (committed or working
    tree) plus untracked new ones, in the git repo containing `root`."""
    root = Path(root)
    top = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    if top.returncode != 0:
        raise SystemExit(f"--changed-since: {root} is not inside a git repo")
    toplevel = Path(top.stdout.strip())

    diff = subprocess.run(
        ["git", "-C", str(root), "diff", "--name-only", ref, "--", "*.java"],
        capture_output=True, text=True,
    )
    if diff.returncode != 0:
        raise SystemExit(
            f"--changed-since: git diff against '{ref}' failed: "
            f"{diff.stderr.strip()}"
        )
    lines = list(diff.stdout.splitlines())

    untracked = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard",
         "--", "*.java"],
        capture_output=True, text=True,
    )
    if untracked.returncode == 0:
        lines += untracked.stdout.splitlines()

    seen: set[str] = set()
    out: list[Path] = []
    for line in lines:
        line = line.strip()
        if line and line not in seen:
            seen.add(line)
            out.append(toplevel / line)
    return out


def _changed_concept_ids(source, root: Path, ref: str) -> list[tuple[str, ...]]:
    """Map git-changed .java files to concept ids, logging files that don't
    correspond to a documented concept (deleted, test-only, etc.)."""
    files = _git_changed_java(root, ref)
    ids: list[tuple[str, ...]] = []
    skipped: list[str] = []
    for f in files:
        cref = source.find_by_path(f)
        if cref is not None:
            ids.append(cref.id)
        else:
            skipped.append(str(f))
    log = logging.getLogger("reference_agent")
    log.info(
        "Changed since %s: %d .java file(s), %d mapped to concepts, %d skipped",
        ref, len(files), len(ids), len(skipped),
    )
    for s in skipped:
        log.info("  skipped (no concept; deleted/test/non-doc): %s", s)
    # De-dup while preserving order.
    seen: set[tuple[str, ...]] = set()
    uniq: list[tuple[str, ...]] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            uniq.append(i)
    return uniq


def _build_source(name: str, args: argparse.Namespace):
    if name == "bq":
        if not args.dataset:
            raise SystemExit("--dataset is required for --source bq")
        return BigQuerySource(
            dataset=args.dataset, billing_project=args.billing_project
        )
    if name == "code":
        if not args.path:
            raise SystemExit("--path is required for --source code")
        return CodeSource(
            root=args.path,
            include_tests=args.include_tests,
            artifact=args.artifact,
        )
    raise SystemExit(f"Unknown source: {name}")


def _parse_seed_file(path: Path) -> list[str]:
    urls: list[str] = []
    text = path.read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            urls.append(line)
    return urls


def _collect_seeds(args: argparse.Namespace) -> list[str]:
    if args.no_web:
        return []
    seeds: list[str] = []
    if args.web_seed:
        seeds.extend(args.web_seed)
    if args.web_seed_file:
        for p in args.web_seed_file:
            seeds.extend(_parse_seed_file(Path(p)))
    return _dedup_preserve_order(seeds)


def _dedup_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reference-agent")
    sub = p.add_subparsers(dest="command", required=True)

    enrich = sub.add_parser(
        "enrich", help="Enrich concepts from a source into an OKF bundle."
    )
    enrich.add_argument("--source", choices=_SOURCES, required=True)
    enrich.add_argument(
        "--dataset",
        help="Source-specific identifier (for --source bq: 'project.dataset').",
    )
    enrich.add_argument(
        "--path",
        type=Path,
        help="Root directory of the source tree (required for --source code).",
    )
    enrich.add_argument(
        "--include-tests",
        action="store_true",
        help="For --source code: also document files under test/ directories "
        "(default: skip tests).",
    )
    enrich.add_argument(
        "--artifact",
        help="For --source code: dependency coordinate written into each doc "
        "as 'artifact' (e.g. 'group:name:version'). Auto-detected from "
        "build.gradle/settings.gradle when omitted.",
    )
    enrich.add_argument(
        "--embed-source",
        choices=("hybrid", "full", "signatures", "none"),
        default="hybrid",
        help="For --source code: how much verbatim code to embed in each doc "
        "so it is self-contained without repo access. 'hybrid' (default) = "
        "full public API signatures + full source for small files / truncated "
        "for large; 'full' = whole source; 'signatures' = API only; 'none' = "
        "no embedding.",
    )
    enrich.add_argument(
        "--billing-project",
        help="Google Cloud project to bill for queries; "
        "defaults to ADC default.",
    )
    enrich.add_argument(
        "--out", required=True, type=Path, help="Bundle root directory."
    )
    enrich.add_argument(
        "--concept",
        action="append",
        default=None,
        help="Enrich only this concept id (e.g. 'tables/events_'). "
        "Repeatable.",
    )
    enrich.add_argument(
        "--changed-since",
        metavar="GIT_REF",
        default=None,
        help="For --source code: enrich only concepts whose .java file changed "
        "since GIT_REF (git diff in the --path repo, including uncommitted and "
        "untracked files). Combine with --concept to add more. Examples: "
        "'HEAD' (uncommitted), 'HEAD~1', 'main', a tag.",
    )
    enrich.add_argument(
        "--since-last",
        action="store_true",
        help="For --source code: enrich only concepts whose source changed "
        "since the last run, using the bundle's recorded source-state manifest "
        "(<out>/.okf-source.json) — git-independent (catches uncommitted edits) "
        "and self-updating. On the first run (no manifest) this enriches "
        "everything.",
    )
    enrich.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="Model id (default: %(default)s). Bare Gemini ids are served "
        "natively; provider-prefixed ids route through LiteLLM, e.g. a local "
        "Ollama model: 'ollama_chat/qwen3-coder-next:q8_0' (override the "
        "endpoint with OLLAMA_API_BASE; defaults to http://localhost:11434). "
        "The OKF_MODEL env var sets the default.",
    )
    enrich.add_argument(
        "--web-seed",
        action="append",
        default=None,
        help="Seed URL for the web pass. Repeatable.",
    )
    enrich.add_argument(
        "--web-seed-file",
        action="append",
        default=None,
        help="Path to a file with one seed URL per line (# comments allowed). "
        "Repeatable.",
    )
    enrich.add_argument(
        "--web-max-pages",
        type=int,
        default=100,
        help="Hard cap on pages the web agent may fetch in one run (default 100).",
    )
    enrich.add_argument(
        "--web-allowed-host",
        action="append",
        default=None,
        help="Extra hostname the web agent may fetch beyond seed hostnames. "
        "Repeatable. Default: only seed hosts.",
    )
    enrich.add_argument(
        "--web-allowed-path-prefix",
        action="append",
        default=None,
        help="Only fetch URLs whose path starts with one of these prefixes "
        "(e.g. '/docs/'). Repeatable. Default: no path restriction.",
    )
    enrich.add_argument(
        "--web-denied-path-substring",
        action="append",
        default=None,
        help="Reject URLs whose path contains any of these substrings "
        "(e.g. '/login', '/pricing'). Repeatable.",
    )
    enrich.add_argument(
        "--web-max-depth",
        type=int,
        default=2,
        help="Hard cap on hop distance from any seed URL (default 2). "
        "Seeds are depth 0; their outbound links are depth 1; etc.",
    )
    enrich.add_argument(
        "--max-repair",
        type=int,
        default=2,
        help="Self-heal attempts per concept: if the agent finishes without "
        "calling a required tool (e.g. write_concept_doc), re-prompt in the "
        "same session up to this many times (default 2; 0 disables).",
    )
    enrich.add_argument(
        "--language",
        default="English",
        help="Natural language for generated prose — the description field, "
        "document body, section headings, and index.md summaries (default: "
        "%(default)s). Code identifiers, type names, and tags stay as-is. "
        "Example: --language Korean.",
    )
    enrich.add_argument(
        "--no-web",
        action="store_true",
        help="Skip the web pass entirely.",
    )
    enrich.add_argument("-v", "--verbose", action="store_true")

    viz = sub.add_parser(
        "visualize",
        help="Generate a self-contained HTML graph view of an OKF bundle.",
    )
    viz.add_argument(
        "--bundle", required=True, type=Path,
        help="Path to the bundle root directory.",
    )
    viz.add_argument(
        "--out", type=Path, default=None,
        help="Output HTML path (default: <bundle>/viz.html).",
    )
    viz.add_argument(
        "--name", default=None,
        help="Display name for the bundle (default: bundle directory name).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
    )
    if getattr(args, "verbose", False):
        logging.getLogger("reference_agent").setLevel(logging.DEBUG)
    # Quiet chatty third-party loggers regardless of mode.
    for noisy in ("google", "google_genai", "google_adk", "urllib3", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.command == "visualize":
        from reference_agent.viewer import generate_visualization
        out = args.out or (args.bundle / "viz.html")
        stats = generate_visualization(args.bundle, out, bundle_name=args.name)
        print(
            f"Wrote {stats['concepts']} concept(s), "
            f"{stats['edges']} edge(s), "
            f"{stats['bytes']} bytes → {out}",
            file=sys.stderr,
        )
        return 0

    if args.command == "enrich":
        source = _build_source(args.source, args)
        seeds = _collect_seeds(args)
        allowed_hosts: set[str] | None = None
        if seeds:
            allowed_hosts = {urlparse(s).netloc for s in seeds if urlparse(s).netloc}
            if args.web_allowed_host:
                allowed_hosts |= set(args.web_allowed_host)
        runner = ReferenceRunner(
            source=source,
            bundle_root=args.out,
            model=args.model,
            web_seeds=seeds or None,
            web_max_pages=args.web_max_pages,
            web_allowed_hosts=allowed_hosts,
            web_allowed_path_prefixes=args.web_allowed_path_prefix,
            web_denied_path_substrings=args.web_denied_path_substring,
            web_max_depth=args.web_max_depth,
            language=args.language,
            embed_mode=args.embed_source,
            max_repair=args.max_repair,
            verbose=args.verbose,
        )
        explicit = [parse_concept_id(c) for c in args.concept] if args.concept else []
        selective = bool(args.changed_since or args.since_last or explicit)
        only: list[tuple[str, ...]] | None
        if selective:
            if (args.changed_since or args.since_last) and args.source != "code":
                raise SystemExit(
                    "--changed-since/--since-last are only valid for --source code"
                )
            picked: list[tuple[str, ...]] = list(explicit)
            if args.changed_since:
                picked += _changed_concept_ids(source, Path(args.path), args.changed_since)
            if args.since_last:
                from reference_agent.bundle import source_state
                changed, deleted = source_state.changed_concepts(source, args.out)
                picked += changed
                logging.getLogger("reference_agent").info(
                    "Since last run: %d changed concept(s)%s",
                    len(changed),
                    f", {len(deleted)} source file(s) deleted" if deleted else "",
                )
                for d in deleted:
                    logging.getLogger("reference_agent").info(
                        "  deleted source (doc kept): %s", "/".join(d)
                    )
            # De-dup, preserving order.
            seen: set[tuple[str, ...]] = set()
            only = []
            for cid in picked:
                if cid not in seen:
                    seen.add(cid)
                    only.append(cid)
            if not only:
                print(
                    "No concepts to update (nothing changed).", file=sys.stderr
                )
                return 0
        else:
            only = None
        n = runner.enrich_all(only=only)
        web_note = f"; web pass used {len(seeds)} seed(s)" if seeds else "; web pass skipped"
        print(f"Enriched {n} concept(s) into {args.out}{web_note}", file=sys.stderr)
        return 0
    return 1
