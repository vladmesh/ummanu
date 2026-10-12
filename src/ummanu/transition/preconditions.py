"""Step 1: refuse unless the installation is ready to move (`docs/RENAME.md` §T3).

Every check reads; none writes. `--plan` runs them as they are, `--apply` fetches `origin` first.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .context import Context, TransitionError
from .names import BASELINE_MARKER, COUNTS_MARKER, NEW, OLD

PAUSE_MODES_ALLOWING_FREEZE = ("drain", "freeze")
_REVISION = re.compile(r"^src/[^/]+/board/migrations/versions/([^/]+\.py)$")


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str

    def line(self) -> str:
        return f"  [{'ok' if self.ok else 'FAIL'}] {self.name}: {self.detail}"


class BoardReads(Protocol):
    def prepared_sprints(self) -> list[str]: ...

    def markers(self, sprint: str) -> dict[str, int]: ...


def preflight_path(package: str) -> str:
    return f"src/{package}/dispatch/runtime_preflight.py"


def _has(ctx: Context, root: Path, commit: str, path: str) -> bool:
    return ctx.runner.git_probe(root, "cat-file", "-e", f"{commit}:{path}") is not None


def _scripts(ctx: Context, root: Path, commit: str) -> dict[str, Any]:
    text = ctx.runner.git_probe(root, "show", f"{commit}:pyproject.toml")
    try:
        scripts = tomllib.loads(text or "").get("project", {}).get("scripts", {})
    except tomllib.TOMLDecodeError:
        return {}
    return scripts if isinstance(scripts, dict) else {}


def _revisions(ctx: Context, root: Path, commit: str) -> set[str]:
    listing = ctx.runner.git_probe(root, "ls-tree", "-r", "--name-only", commit, "--", "src") or ""
    return {match.group(1) for line in listing.splitlines() if (match := _REVISION.match(line))}


def git_checks(ctx: Context, root: Path) -> tuple[list[Check], dict[str, Any]]:
    """The checkout, the rename commit, the revisions and the merges between them."""
    facts: dict[str, Any] = {"code_root": str(root)}
    head = ctx.runner.git_probe(root, "rev-parse", "HEAD")
    branch = ctx.runner.git_probe(root, "symbolic-ref", "--short", "HEAD")
    target = ctx.runner.git_probe(root, "rev-parse", "--verify", "--quiet", "origin/main^{commit}")
    facts.update(checkout_sha=head, checkout_branch=branch, origin_main=target)
    if not head or not target:
        detail = f"cannot read HEAD or origin/main in {root}"
        return [Check("rename commit", False, detail), Check("no new Alembic revision", False, detail),
                Check("only the rename merge", False, detail)], facts
    problems = []
    if branch != "main":
        problems.append(f"the checkout is on {branch or 'a detached HEAD'}, not main")
    if head == target:
        problems.append("the checkout is already at origin/main")
    elif ctx.runner.git_probe(root, "merge-base", "--is-ancestor", head, target) is None:
        problems.append(f"the checkout {head[:12]} is not an ancestor of origin/main {target[:12]}")
    if not _has(ctx, root, head, preflight_path(OLD.package)):
        problems.append(f"the checkout lacks {preflight_path(OLD.package)}")
    rename = ""
    if not problems:
        added = ctx.runner.git_probe(
            root, "log", "--reverse", "--format=%H", f"{head}..{target}", "--", preflight_path(NEW.package)
        )
        rename = (added or "").split("\n")[0].strip()
        if not rename:
            problems.append(f"no commit between the checkout and origin/main adds {preflight_path(NEW.package)}")
        elif _has(ctx, root, target, preflight_path(OLD.package)):
            problems.append(f"origin/main still carries {preflight_path(OLD.package)}")
        elif NEW.package not in _scripts(ctx, root, target):
            problems.append(f"origin/main's pyproject.toml has no {NEW.package!r} console script")
    facts["rename_commit"] = rename
    behind = ctx.runner.git_probe(root, "rev-list", "--count", f"{head}..{target}") or "?"
    checks = [
        Check(
            "rename commit",
            not problems,
            "; ".join(problems)
            or f"{rename[:12]} on origin/main {target[:12]}; checkout {head[:12]} on main is {behind} commit(s) behind",
        )
    ]

    # The rename moves every revision file, so a new revision is a file *name* the checkout lacks.
    before, after = _revisions(ctx, root, head), _revisions(ctx, root, target)
    new_revisions = sorted(after - before)
    facts["new_revisions"] = new_revisions
    checks.append(
        Check(
            "no new Alembic revision",
            not new_revisions and bool(before),
            f"added between the checkout and origin/main: {', '.join(new_revisions)}"
            if new_revisions
            else (f"{len(after)} revision files, the same names on both sides" if before
                  else "no revision files found in the checkout"),
        )
    )

    merges = (ctx.runner.git_probe(root, "rev-list", "--merges", "--first-parent", "--reverse", f"{head}..{target}")
              or "").split()
    expected = next(
        (merge for merge in merges
         if rename and ctx.runner.git_probe(root, "merge-base", "--is-ancestor", rename, merge) is not None),
        "",
    )
    extra = [merge for merge in merges if merge != expected]
    unexplained = [merge for merge in extra if not any(merge.startswith(allowed) for allowed in ctx.allow_extra_merges
                                                       if allowed)]
    facts.update(rename_merge=expected, extra_merges=extra)
    checks.append(
        Check(
            "only the rename merge",
            not unexplained,
            f"merges besides the rename's: {', '.join(merge[:12] for merge in unexplained)} "
            "(name each with --allow-extra-merge after checking it adds nothing the transition misses)"
            if unexplained
            else f"rename merge {expected[:12] or '(none, squash)'}"
            + (f"; allowed extra: {', '.join(merge[:12] for merge in extra)}" if extra else ""),
        )
    )
    return checks, facts


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"__unreadable__": True}


def pipeline_checks(data_dir: Path) -> tuple[list[Check], dict[str, Any]]:
    """Pause, in-flight dispatcher records, live worker and reviewer heads."""
    checks = []
    pause = _json(data_dir / "dispatcher" / "pause.json")
    mode = str(pause.get("mode") or "") if isinstance(pause, dict) and not pause.get("__unreadable__") else ""
    if isinstance(pause, dict) and pause.get("__unreadable__"):
        checks.append(Check("pause allows a freeze", False, "pause.json is unreadable"))
    else:
        checks.append(
            Check(
                "pause allows a freeze",
                mode in PAUSE_MODES_ALLOWING_FREEZE,
                f"pause is {mode or 'not set'}"
                + ("" if mode in PAUSE_MODES_ALLOWING_FREEZE else "; the observer drains the pipeline first"),
            )
        )
    state = _json(data_dir / "dispatcher" / "production-state.json")
    if not isinstance(state, dict) or state.get("__unreadable__"):
        detail = "production-state.json is missing or unreadable"
        checks += [Check("no in-flight records", False, detail), Check("no live worker or reviewer head", False, detail)]
        return checks, {"pause_mode": mode}
    records = state.get("records") if isinstance(state.get("records"), dict) else {}
    in_flight = sorted(records)
    releases = sorted(
        ref for ref, record in records.items()
        if isinstance(record, dict) and record.get("activation_recovery")
    )
    watches = sorted(state.get("post_merge_watches") or {}) + sorted(state.get("e2e_after_merge") or {})
    problems = []
    if releases:
        problems.append(f"release records awaiting activation recovery: {', '.join(releases)}")
    if in_flight:
        problems.append(f"dispatcher records: {', '.join(in_flight)}")
    if watches:
        problems.append(f"post-merge watches: {', '.join(watches)}")
    checks.append(Check("no in-flight records", not problems, "; ".join(problems) or "records, watches empty"))
    live = []
    for ref, record in sorted(records.items()):
        if not isinstance(record, dict):
            continue
        for role, key in (("worker", "handle"), ("reviewer", "review_handle")):
            handle = str(record.get(key) or "")
            if handle and Path(handle).exists():
                live.append(f"{role} of {ref} ({handle})")
    checks.append(
        Check(
            "no live worker or reviewer head",
            not live,
            "live: " + "; ".join(live) if live else "none (observer heads are stopped by step 2)",
        )
    )
    return checks, {"pause_mode": mode, "records": in_flight}


def sprint_checks(ctx: Context, reads: BoardReads | None) -> tuple[list[Check], dict[str, Any]]:
    if reads is None:
        return [Check("baseline and counts comments", False, "the board store is not readable")], {}
    try:
        sprint = ctx.sprint
        if not sprint:
            prepared = reads.prepared_sprints()
            if len(prepared) != 1:
                return [Check(
                    "baseline and counts comments", False,
                    f"open sprints carrying {BASELINE_MARKER}: {', '.join(prepared) or 'none'}; "
                    "name the sprint with --sprint" if prepared else
                    f"no open sprint carries a {BASELINE_MARKER} comment yet",
                )], {}
            sprint = prepared[0]
        found = reads.markers(sprint)
    except TransitionError as exc:
        return [Check("baseline and counts comments", False, str(exc))], {}
    missing = [marker for marker in (BASELINE_MARKER, COUNTS_MARKER) if marker not in found]
    return [
        Check(
            "baseline and counts comments",
            not missing,
            f"{sprint} lacks {' and '.join(missing)}" if missing
            else f"{sprint}: {', '.join(f'{marker} #{found[marker]}' for marker in (BASELINE_MARKER, COUNTS_MARKER))}",
        )
    ], {"sprint": sprint, "markers": found}


def run(ctx: Context, reads: BoardReads | None, *, fetch: bool) -> tuple[list[Check], dict[str, Any]]:
    root = ctx.layout.code_root()
    if fetch:
        ctx.runner.git(root, "fetch", "--quiet", "origin")
    checks, facts = git_checks(ctx, root)
    more, pipeline = pipeline_checks(ctx.layout.live_data_dir())
    sprint, sprint_facts = sprint_checks(ctx, reads)
    return checks + more + sprint, {**facts, **pipeline, **sprint_facts}


__all__ = ["BoardReads", "Check", "git_checks", "pipeline_checks", "preflight_path", "run", "sprint_checks"]
