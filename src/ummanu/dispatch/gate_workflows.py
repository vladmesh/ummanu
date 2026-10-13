"""Workflow trigger proofs for PR bases and post-merge branch pushes.

Only readable workflows unanimously refusing a base prove impossibility;
missing, unreadable or admitting workflows leave external checks possible.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

# Workflow trigger analysis: the difference between "checks have not appeared yet" and "no workflow
# can ever post them for this base".
_WORKFLOW_DIR = Path(".github") / "workflows"
_PR_EVENTS = ("pull_request", "pull_request_target")
# The event a release merge raises on the integration base, read by the post-merge CI watch.
_PUSH_EVENTS = ("push",)


def _branch_pattern_matches(pattern: str, branch: str) -> bool:
    """GitHub's branch filter glob, where `*` stops at a `/` and `**` does not."""
    parts = re.split(r"(\*\*|\*|\?)", pattern)
    expression = ""
    for part in parts:
        if part == "**":
            expression += ".*"
        elif part == "*":
            expression += "[^/]*"
        elif part == "?":
            expression += "[^/]"
        else:
            expression += re.escape(part)
    return re.fullmatch(expression, branch) is not None


def _event_triggers_base(spec: object, base: str, *, event: str = "pull_request") -> bool:
    """Would this `on: <event>:` block fire for `base` (the PR's base, or the pushed branch)?"""
    if not isinstance(spec, dict):
        # `pull_request:` or `push:` with no filter at all fires for every base.
        return True
    include = spec.get("branches")
    exclude = spec.get("branches-ignore")
    if event == "push" and include is None and exclude is None and ("tags" in spec or "tags-ignore" in spec):
        # A push filter that names only tags never fires for a branch push.
        return False
    if isinstance(exclude, list) and any(_branch_pattern_matches(str(item), base) for item in exclude):
        return False
    if isinstance(include, list):
        return any(_branch_pattern_matches(str(item), base) for item in include)
    return True


def _workflow_triggers_base(document: object, base: str, events: tuple[str, ...] = _PR_EVENTS) -> bool:
    """Does one parsed workflow declare a trigger among `events` that admits `base`?

    YAML 1.1 reads a bare `on:` key as the boolean true, which is why both spellings are looked up.
    """
    if not isinstance(document, dict):
        return False
    triggers = document.get("on", document.get(True))
    if isinstance(triggers, str):
        return triggers in events
    if isinstance(triggers, list):
        return any(str(item) in events for item in triggers)
    if isinstance(triggers, dict):
        for event in events:
            if event in triggers and _event_triggers_base(triggers[event], base, event=event):
                return True
    return False


def _workflows_missing_base(workspace: str, base: str, events: tuple[str, ...]) -> list[str] | None:
    """The workflow files read when it is established that none of them fires on `events` for
    `base`, or None when that cannot be established (no directory, no files, an unparsable file, or
    one workflow that does fire)."""
    root = Path(workspace) / _WORKFLOW_DIR
    try:
        files = sorted(path for path in root.iterdir() if path.suffix in (".yml", ".yaml") and path.is_file())
    except OSError:
        return None
    if not files:
        return None
    names = []
    for path in files:
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            return None
        if _workflow_triggers_base(document, base, events):
            return None
        names.append(path.name)
    return names


def _listed_workflows(names: list[str]) -> str:
    return ", ".join(names[:6]) + (", …" if len(names) > 6 else "")


def _impossible_trigger_reason(workspace: str, base: str) -> str:
    """Explain when this candidate has no workflow capable of checking a PR to base.

Only conclude impossibility if every workflow parses and none matches.
Missing, invalid or admitting workflows remain pending: external checks
may also supply evidence.
    """
    names = _workflows_missing_base(workspace, base, _PR_EVENTS)
    if names is None:
        return ""
    return (
        f"no workflow in this candidate triggers on a pull request into `{base}` "
        f"({len(names)} workflow file(s) read: {_listed_workflows(names)}), so GitHub will never create a check-run "
        "for it: this rollup is empty because nothing can fill it, not because CI is still starting"
    )


def push_trigger_absent_reason(workspace: str, base: str) -> str:
    """Why a push to `base` starts no workflow of this checkout, or "".

    The same trigger analysis as `_impossible_trigger_reason`, asked about the `push` event a
    release merge raises on the integration base, with the same rule: only a positive answer counts,
    and anything unreadable leaves the post-merge watch waiting for a run.
    """
    names = _workflows_missing_base(workspace, base, _PUSH_EVENTS)
    if names is None:
        return ""
    return (
        f"no workflow triggers on a push to `{base}` ({len(names)} workflow file(s) read: "
        f"{_listed_workflows(names)}), so the merge starts no CI run on the base"
    )
