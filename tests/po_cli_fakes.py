"""Fake `claude` and `codex` executables for PO head turns, shared by the runner and `/po` tests.

Each fake logs its argv, cwd, stdin, `$UMMANU_PO_SESSION` and `$UMMANU_PO_REQUEST` to
`$FAKE_LOG`, prints an event stream that carries reasoning and a tool call beside the final answer, and
changes behaviour on words in the owner's message:
`SLEEP` keeps the turn running with a child in its process group, `GATE` keeps it running until the
file `$FAKE_LOG.gate` exists, `FAIL` exits non-zero, `SILENT` exits zero without a final answer,
`NOPERSIST` makes Claude save no conversation. While the file `$FAKE_LOG.quota-<cli>` exists, that
CLI refuses every turn with its provider's own spent-usage-limit message, as the real one does.

Each also says which model it ran the way the real CLI does: Claude's result object keys `modelUsage`
by the full id (`FAKE_CLAUDE_RESOLVED` of the alias it was given, the session's own model first and a
subagent's after it), and Codex appends a `turn_context` naming `-m` and the
`model_reasoning_effort` override to a rollout under `$FAKE_CODEX_HOME/sessions`, only when that is
set, so a test never writes into a real Codex home.
"""

from __future__ import annotations

import time
import subprocess


def unscoped_test_launch(session, seq, argv, files, environment, spec):
    """Keep the existing fake CLI process tests independent of a CI systemd scope."""
    del seq, spec
    with files.prompt.open("rb") as stdin, files.stdout.open("ab") as stdout, files.stderr.open("ab") as stderr:
        return subprocess.Popen(
            argv, cwd=session.cwd, stdin=stdin, stdout=stdout, stderr=stderr,
            env=environment, start_new_session=True,
        )

SETTLE_SECONDS = 30
# The full id the fake Claude reports for each alias it may be given.
FAKE_CLAUDE_RESOLVED = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5", "fable": "claude-fable-5-1"}

FAKE_CLAUDE = r"""#!/usr/bin/env python3
import json, os, subprocess, sys, time
prompt = sys.stdin.read()
argv = sys.argv[1:]
log = os.environ["FAKE_LOG"]
with open(log, "a") as handle:
    handle.write(json.dumps({"cli": "claude", "argv": argv, "cwd": os.getcwd(), "prompt": prompt,
                             "po_session": os.environ.get("UMMANU_PO_SESSION"),
                             "po_request": os.environ.get("UMMANU_PO_REQUEST")}) + "\n")
flag = "--resume" if "--resume" in argv else "--session-id"
session = argv[argv.index(flag) + 1]
# Claude 2.1.270's own refusals: a saved conversation cannot be created again, a missing one resumed.
saved_path = log + ".saved"
saved = set(open(saved_path).read().split()) if os.path.exists(saved_path) else set()
if flag == "--resume" and session not in saved:
    print(f"No conversation found with session ID: {session}", file=sys.stderr)
    sys.exit(1)
if flag == "--session-id" and session in saved:
    print(f"Error: Session ID {session} is already in use.", file=sys.stderr)
    sys.exit(1)
if os.path.exists(log + ".quota-claude"):
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "session_id": session,
                      "result": "You've hit your weekly limit \u00b7 resets 1am (UTC)"}))
    sys.exit(1)
if "NOPERSIST" not in prompt:
    with open(saved_path, "a") as handle:
        handle.write(session + "\n")
print(json.dumps({"type": "system", "subtype": "init", "session_id": session}), flush=True)
print(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "thinking", "thinking": "THINKING-SECRET"},
    {"type": "tool_use", "name": "Bash", "input": {"command": "TOOL-CALL-SECRET"}}]}}), flush=True)
if "GATE" in prompt:
    gate, deadline = log + ".gate", time.time() + 60
    while not os.path.exists(gate) and time.time() < deadline:
        time.sleep(0.02)
if "SLEEP" in prompt:
    child = subprocess.Popen(["sleep", "300"])
    with open(log + ".pids", "a") as handle:
        handle.write(f"{os.getpid()} {child.pid}\n")
    time.sleep(300)
if "FAIL" in prompt:
    print("boom from fake claude", file=sys.stderr)
    sys.exit(3)
if "SILENT" in prompt:
    sys.exit(0)
model = argv[argv.index("--model") + 1]
resolved = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5", "fable": "claude-fable-5-1"}.get(model, model)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "session_id": session, "result": f"claude {flag} {session}: {prompt}",
                  "modelUsage": {resolved: {"outputTokens": 7}, "claude-haiku-4-5": {"outputTokens": 1}}}))
"""

FAKE_CODEX = r"""#!/usr/bin/env python3
import json, os, subprocess, sys, time
prompt = sys.stdin.read()
argv = sys.argv[1:]
log = os.environ["FAKE_LOG"]
with open(log, "a") as handle:
    handle.write(json.dumps({"cli": "codex", "argv": argv, "cwd": os.getcwd(), "prompt": prompt,
                             "po_session": os.environ.get("UMMANU_PO_SESSION"),
                             "po_request": os.environ.get("UMMANU_PO_REQUEST")}) + "\n")
resume = argv[:2] == ["exec", "resume"]
thread = argv[-2] if resume else "019a-fake-thread"
out = argv[argv.index("-o") + 1]
print(json.dumps({"type": "thread.started", "thread_id": thread}), flush=True)
if os.path.exists(log + ".quota-codex"):
    message = ("You\u2019ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase "
               "more credits or try again at Oct 9th, 2026 9:11 PM.")
    print(json.dumps({"type": "error", "message": message}), flush=True)
    print(json.dumps({"type": "turn.failed", "error": {"message": message}}), flush=True)
    sys.exit(1)
home = os.environ.get("FAKE_CODEX_HOME")
if home:
    effort = next((value.split("=", 1)[1] for value in argv if value.startswith("model_reasoning_effort=")), "medium")
    rollout = os.path.join(home, "sessions", "2026", "09", "22", f"rollout-2026-09-22T00-00-00-{thread}.jsonl")
    os.makedirs(os.path.dirname(rollout), exist_ok=True)
    with open(rollout, "a") as handle:
        context = {"model": argv[argv.index("-m") + 1], "effort": effort}
        handle.write(json.dumps({"type": "turn_context", "payload": context}) + "\n")
print(json.dumps({"type": "item.completed", "item": {"type": "reasoning", "text": "THINKING-SECRET"}}), flush=True)
print(json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": "TOOL-CALL-SECRET"}}), flush=True)
if "GATE" in prompt:
    gate, deadline = log + ".gate", time.time() + 60
    while not os.path.exists(gate) and time.time() < deadline:
        time.sleep(0.02)
if "SLEEP" in prompt:
    child = subprocess.Popen(["sleep", "300"])
    with open(log + ".pids", "a") as handle:
        handle.write(f"{os.getpid()} {child.pid}\n")
    time.sleep(300)
if "FAIL" in prompt:
    print("boom from fake codex", file=sys.stderr)
    sys.exit(4)
if "SILENT" in prompt:
    sys.exit(0)
with open(out, "w") as handle:
    handle.write(f"codex {'resume' if resume else 'new'} {thread}: {prompt}\n")
print(json.dumps({"type": "turn.completed"}))
"""


def eventually(predicate, message: str, timeout: float = SETTLE_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(message)
