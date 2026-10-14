"""Deterministic presentation documents, shared by source and installed-package checks."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)
SOURCE = {"state": "available", "reason": None, "observed_at": "2026-09-20T12:00:00Z", "data_age_seconds": 0}
REFUSED = {**SOURCE, "state": "unavailable", "reason": "denied <source>"}
ASSETS = ("STYLE", "_ACTIONS_SCRIPT", "_REFRESH_SCRIPT", "_RESET_SCRIPT", "_TASK_SCRIPT",
          "_SPRINT_FORM_SCRIPT", "_PO_FORM_SCRIPT", "_PO_SESSION_SCRIPT")


def packets():
    """Each tuple carries a label, renderer, positional documents and keyword choices."""
    snapshot = {"observed_at": SOURCE["observed_at"], "installation": {"health": {"source": SOURCE}},
                "projects": {"source": SOURCE, "items": [{"id": "ummanu", "enabled": True, "repo": "/repo"}]}}
    item = {"ref": "sprint:7", "status": "open", "product": "ummanu", "projects": ["ummanu"],
            "goal": "Keep <bytes> & links", "current_task": {"ref": "ummanu-9", "live": True},
            "current_card_state": {"state": "in_progress", "age_seconds": 3600, "transition": "recorded"},
            "waiting": {"state": "waiting", "reason": "wait <review>"},
            "checks": {"gate": {"state": "green"}}, "budget": {"total": 3},
            "head_profiles": {"worker": {"model": "gpt-6-sol", "effort": "high"}},
            "observer": {"launch": {"state": "running"}},
            "decision": {"entry": {"selected_step": "review", "selected_why": "safe"}},
            "cards": {"states": {"assessment": ["ummanu-9"]}}}
    listing = {"observed_at": SOURCE["observed_at"], "sprints": {"source": SOURCE, "items": [item]}}
    for label, side in (("missing", None), ("available", {"available": True, "document": listing}),
                        ("refused", {"available": False, "reason": "no <read>"}),
                        ("malformed", {"available": True, "document": []})):
        yield "dashboard-" + label, "dashboard", [snapshot], {"sprints": side, "pause": side, "po": side}
        yield "project-" + label, "project_page", [snapshot], {"project_id": "ummanu", "sprints": side}
        yield "doctor-" + label, "doctor", [side], {}
    yield "doctor-findings", "doctor", [{"available": True, "document": {
        "readable": True, "recorded": {"state": "red"}, "problems": [
            {"severity": "red", "code": "R", "message": "<red>"},
            {"severity": "yellow", "code": "Y", "message": "<yellow>"}]}}], {}
    yield "projects", "projects_page", [snapshot], {}
    yield "project-absent", "project_page", [snapshot], {"project_id": "absent", "sprints": None}
    yield "sprints", "sprints_page", [listing], {}
    yield "sprints-filter", "sprints_page", [listing], {"view": "archive", "search": "bytes", "project": "ummanu"}
    yield "sprint-detail", "sprint", [{"ref": "sprint:7", "sprint": {"source": SOURCE, "value": item}}], {}
    yield "sprint-refused", "sprint", [{"ref": "sprint:7", "sprint": {"source": REFUSED}}], {}
    history = {"commands": {"source": SOURCE, "has_more": True, "next_cursor": "cursor<&>", "items": [
        {"actor": {"id": "worker", "role": "worker"}, "entity": {"kind": "card", "ref": "ummanu-9"},
         "result": {"outcome": "refused", "reason": "no <write>"}, "action": "task_move"}]}}
    yield "history", "commands", [history], {}
    yield "owner-events", "owner_events", [{"state": "available", "items": [
        {"event_id": "evt-1", "class": "needs_owner", "subject": "card:ummanu-9", "text": "<owner>",
         "unread": True, "pinned": True}]}], {}
    task = {"ref": "ummanu-9", "card": {"source": SOURCE, "value": {
        "title": "<card>", "description": "# Task\n[good](https://example.invalid) [bad](javascript:bad)\n<script>",
        "state": "in_progress", "type": "code", "routing": {"sprint": "sprint:7"},
        "origin": {"po_session": "session-1"}, "waiting_owner": {"reason": "owner <answer>"}}},
        "events": {"source": SOURCE, "items": []}}
    yield "task", "task", [task], {"runs": {"items": []}, "sessions": {"session-1": "A <session>"}}
    for kind in ("wait", "e2e", "decision", "operation", "research", "infra"):
        typed = deepcopy(task)
        typed["card"]["value"]["type"] = kind
        typed["card"]["value"]["wait"] = {"target": {"kind": "time", "at": SOURCE["observed_at"]}, "state": "waiting"}
        yield "task-type-" + kind, "task", [typed], {"runs": {"items": []}}
    for value in (None, [], "bad", {}):
        malformed = deepcopy(task)
        malformed["card"]["value"] = value
        yield "task-value-" + repr(value), "task", [malformed], {"runs": {}}
    head = {"ref": "ummanu-9", "run_id": "head-1", "head": {"role": "worker", "state": "running"},
            "journal": {"answered": True, "tail": [{"seq": 1, "kind": "turn.finished", "at": 0, "reason": "<tail>"}]}}
    yield "head", "head_view", [head], {}
    for name in ("head", "journal"):
        for value in (None, [], "bad", {"answered": False, "reason": "denied <tail>"}):
            yield "head-" + name + "-" + repr(value), "head_view", [{**head, name: value}], {}
    options = {"products": {"source": SOURCE, "items": [{"id": "ummanu"}]},
               "projects": snapshot["projects"], "issues": {"source": SOURCE, "items": []},
               "heads": {"source": SOURCE, "items": [{"id": "observer", "observer": True, "role": "observer"},
                   {"id": "worker", "role": "worker"}, {"id": "reviewer", "role": "reviewer"}]}}
    submitted = {"request_id": "request<&>", "product": "ummanu", "projects": ["ummanu"],
                 "observer": "observer", "worker": "worker", "reviewer": "reviewer", "goal": "<goal>"}
    yield "sprint-new", "sprint_form", [options], {"submitted": submitted, "errors": {}}
    yield "sprint-new-refused", "sprint_form", [None], {"submitted": submitted, "errors": {"goal": "required"},
        "catalogue": "no <catalogue>", "refusal": {"message": "denied"}, "reissued": True}
    yield "sprint-new-part-done", "sprint_form", [options], {"submitted": submitted, "errors": {},
        "refusal": {"message": "part done", "data": {"action": {"repeat_request": True, "reference": "sprint:7"}}}}
    models = {"claude": ["claude-opus-5-5", "sonnet"], "codex": ["gpt-6-sol", "gpt-6.1-sol"]}
    efforts = {"claude": ["default", "high", "max"], "codex": ["none", "low", "xhigh"]}
    session = {"session_id": "session-1", "title": "<title>", "cli": "codex", "model": "gpt-6-sol",
               "effort": "xhigh", "state": "open", "created_at": "2026-09-20T11:00:00Z"}
    for closed in (False, True):
        overview = {"models": models, "efforts": efforts, "sessions": [session], "closed": closed}
        for cli, model, effort in (("claude", "sonnet", "max"), ("codex", "gpt-6.1-sol", "xhigh"),
                                  ("lost", "lost", "default")):
            yield f"po-overview-{closed}-{cli}", "po_page", [overview], {"request_id": "request<&>", "now": NOW,
                "submitted": {"cli": cli, "model": model, "effort": effort}, "refusal": {"message": "<refused>"}}
    yield "po-no-models", "po_page", [{}], {"request_id": "r", "now": NOW}
    for state, running, queued in (("open", False, 0), ("open", True, 2), ("closed", False, 0)):
        document = {"session": {**session, "state": state}, "turns": [{"seq": 1, "state": "running" if running else "completed"}],
                    "running": running, "running_seq": 1, "efforts": efforts,
                    "feed": [{"turn_seq": 1, "role": role, "text": "**text** <tag>", "metadata": {
                        "source": "web", "transition": {"kind": "successor", "from": "old", "to": "new"}}}
                        for role in ("owner", "agent")],
                    "queued": [{"text": "queued <text>"} for _ in range(queued)],
                    "delegated": {"items": [{"ref": "ummanu-9", "type": "decision", "state": "done", "relation": "assigned"}]}}
        for refused in ("send", "close", "title"):
            yield f"po-session-{state}-{running}-{refused}", "po_session", [document], {"request_id": "r", "draft": "<draft>",
                "title_draft": "<rename>", "refused": refused, "refusal": {"message": "<refused>"}}
    yield "error", "error", [404, "missing", "<message>"], {}
    yield "redirect", "redirect", ["/po?x=<&>"], {"what": "<redirect>"}


def helper_packets():
    """Total dict-normalization callers, sentinel policies and presentation choices."""
    for name, key in (("_current_card_box", "current_task"), ("_heads_of", "observer"),
                      ("_waiting_chip", "waiting"), ("_budget_line", "thresholds"),
                      ("_card_standing", "current_card_state"), ("_recorded_at", "decision"),
                      ("_observer_call", "decision"), ("_origin_panel", "origin")):
        for value in (None, [], "bad", False, {}, {"state": "running", "entry": {"selected_step": "review"}}):
            args = [value, None] if name == "_origin_panel" else [{key: value}]
            yield name + "-" + repr(value), name, args, {}
    for role in ("worker", "reviewer", "observer", "PO"):
        for model in ("gpt-6-sol", "claude-opus-5-5", "<unknown>"):
            for effort in (None, "default", "extra", "high", "max", "turbo"):
                yield f"head-choice-{role}-{model}-{effort}", "_head", [role, {"model": model, "effort": effort}], {}
    for metadata in (None, [], "bad", {"source": "web"}, {"source": "sprint_transition", "from": "old", "to": "new"}):
        for role in ("owner", "agent"):
            yield f"entry-{role}-{metadata}", "_po_entry", [{"role": role, "text": "**text** <tag>", "metadata": metadata}], {}
