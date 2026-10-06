"""Hermetic defaults for the unit-test run: nothing the suite reads or writes leaves the
repo/process.

``python -m unittest`` imports this package before it imports any ``test_*``
module, so the defaults below are live before a single test can reach a
host-facing path. Without them, the same checkout is green on one box and red on
another purely from what that host happens to have (secretary-705, secretary-738,
secretary-748).

Board reads need no patch here. A client is built only by
``ummanu.board.backend.board_client(<instance dir>)``, from that
instance's own ``board-store.env``, and nothing in the environment selects
or reaches a board, so a worker/reviewer/operator shell that inherits a live
installation's environment cannot turn the unit suite into a client of that
board (secretary-1026). ``tests/test_hermetic_board.py`` proves the
status read fails closed. A test that needs a board injects it where the
client is built -- ``board_client``/``card_client`` or the reader/writer
constructor the command uses.

Codex runtime state needs one more default, for the same reason and in the
same shape. Since secretary-1173 every Codex head is an interactive TUI, so
every Codex bring-up answers the directory-trust dialog *before* the pane
exists, by appending a ``[projects."<workspace>"]`` table to ``config.toml``
inside the ``CODEX_HOME`` that head will run with. On a developer box that
home is the installation's ``<data_dir>/codex-home`` wherever
``UMMANU_DATA_DIR`` names one (the legacy ``~/.config/orca/...`` home before
secretary-1723) -- installation state shared by every Codex head on the
host -- so any test that reaches a
worker/reviewer/service bring-up without saying otherwise would record a
permanent ``trusted`` grant for its own throwaway workspace there, and
nothing prunes it. ``TA_CODEX_HOME`` is the single seam every one of those
paths reads (``codex_preflight.codex_home``, resolved per launch, and every
sessions reader built on it), so the suite claims one throwaway home of
its own for the whole run, before any test module is imported.
``tests/test_hermetic_codex.py`` proves it.

The live pipeline's state dir needs the same treatment, and needs it early.
``ummanu.automations.agents.pipeline.state`` resolves ``STATE`` at import time,
and ``agents.pipeline.pause`` binds ``PAUSE_FILE`` off it, so by the time any
test body runs the pause path is already fixed. Left at its default that path
is the live ``<workspaces>/ummanu/pipeline/state/pipeline`` of the machine
running the suite: a ``ummanu pause --mode freeze`` held there while the
suite runs makes ``runtime/dispatch._pipeline_paused()`` true, and every
triggered-dispatch test silently takes the "pipeline paused -- no dispatch"
branch instead of the lifecycle branch it was written for. The same binding
also had the suite appending its own ``runs.jsonl`` records to that live
directory. ``TA_PIPELINE_STATE_DIR`` is the single seam both readers go
through (``shared_state.resolve_pipeline_state_dir``, and
``dispatch.pause.legacy_mirror_path`` for the mirror), so the suite claims
one throwaway state dir of its own for the whole run, before any test module
is imported (secretary-1403). ``tests/test_hermetic_pipeline_state.py``
proves it.

A test that needs a real sprint board opts in locally: it passes an explicit
board through ``collect_status(..., sprint_client=sprint_store(test))``.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

from tests.support.shell import shell_tools

# ONNX Runtime can initialize its telemetry uploader while importing fastembed. Disable it
# before any test module imports the memory runtime; children inherit the same opt-out.
# https://github.com/microsoft/onnxruntime/blob/v1.30.0/docs/Privacy.md
os.environ["ORT_DISABLE_TELEMETRY"] = "1"

# The suite's own temporary directory, and the guard that keeps it from leaking (secretary-1663).
# Every `tempfile` call in this process and every child that inherits TMPDIR lands under one root
# the run claims here, before any other default below and before any test module is imported, so
# the run leaves the host's temporary directory as it found it: the root is removed at exit. The
# host's `/tmp` is shared with the live pipeline, which writes its own `ummanu-*` and `orca-*`
# files there concurrently, so a before/after count of `/tmp` cannot tell a test's leak from the
# pipeline's work; a root only this run writes to can. Whatever `ummanu-*` or `orca-*` entry is
# still in it once every other exit handler has run is a test that did not clean up after itself,
# and the run fails naming it. A test that writes to `/tmp` by absolute path bypasses TMPDIR and
# this guard alike, so such a test owns its own cleanup. `tests/test_suite_tmp_guard.py` proves the rest.
_LEAK_PREFIXES = ("ummanu-", "orca-")
# Production state that is persistent by design: the per-terminal prompt lock directory the Orca
# prompt send kept. Nothing creates it since that send went (secretary-1725); the exemption is
# pinned by `test_suite_tmp_guard` and goes with it.
_PERSISTENT_BY_DESIGN = frozenset({"ummanu-agent-prompt-locks"})
# Short on purpose: it lengthens every temporary path in the run, and real-head tests put a Unix
# socket (100-byte address limit) about 70 bytes deep under the temporary directory.
_SUITE_TMP = Path(tempfile.mkdtemp(prefix="ummanu-t"))
_SUITE_PID = os.getpid()
os.environ["TMPDIR"] = str(_SUITE_TMP)
tempfile.tempdir = str(_SUITE_TMP)


def suite_tmp_leaks(root: Path) -> list[str]:
    """The `ummanu-*` and `orca-*` entries a run left in its temporary root."""
    try:
        names = sorted(entry.name for entry in root.iterdir())
    except FileNotFoundError:
        return []
    return [name for name in names if name.startswith(_LEAK_PREFIXES) and name not in _PERSISTENT_BY_DESIGN]


def _guard_suite_tmp() -> None:
    # Registered before every other exit handler of the suite, so it runs after all of them.
    if os.getpid() != _SUITE_PID:
        return
    leaks = suite_tmp_leaks(_SUITE_TMP)
    shutil.rmtree(_SUITE_TMP, ignore_errors=True)
    if not leaks:
        return
    sys.stdout.flush()
    sys.stderr.write(
        "\nFAILED: the test run left temporary entries behind (ummanu-1663); each is a test "
        "that created it and did not remove it:\n" + "".join(f"  {name}\n" for name in leaks)
    )
    sys.stderr.flush()
    os._exit(1)


atexit.register(_guard_suite_tmp)

# The throwaway CODEX_HOME described above. Created here rather than per test so
# that a bring-up reached from anywhere in the suite -- including one whose
# fixture registry names no `codex_home` and which never thought about trust at
# all -- writes into a directory this run owns and removes. Set unconditionally:
# an ambient TA_CODEX_HOME inherited from a worker/reviewer/operator shell names that
# installation's real home, which is exactly what must not be written.
_SUITE_CODEX_HOME = Path(tempfile.mkdtemp(prefix="ummanu-tests-codex-home."))
os.environ["TA_CODEX_HOME"] = str(_SUITE_CODEX_HOME)
atexit.register(shutil.rmtree, _SUITE_CODEX_HOME, ignore_errors=True)

# The throwaway pipeline state dir described above, claimed before any test module
# -- and therefore before `ummanu.automations.agents.pipeline.state` -- is imported,
# because that module binds `STATE` (and through it `pause.PAUSE_FILE`) to whatever
# `resolve_pipeline_state_dir()` answers at import time. Set unconditionally, for
# the same reason as TA_CODEX_HOME above: an ambient TA_PIPELINE_STATE_DIR
# inherited from a worker/reviewer/operator shell names the live installation's
# state dir, which is exactly what the suite must neither read nor write.
# `tests/test_hermetic_pipeline_state.py` proves both halves.
_SUITE_PIPELINE_STATE_DIR = Path(tempfile.mkdtemp(prefix="ummanu-tests-pipeline-state."))
os.environ["TA_PIPELINE_STATE_DIR"] = str(_SUITE_PIPELINE_STATE_DIR)
atexit.register(shutil.rmtree, _SUITE_PIPELINE_STATE_DIR, ignore_errors=True)

# `git fetch` ends with `git maintenance run --auto`, and gc detaches by default,
# so a repository a test built in a temporary directory can still be written to
# after the test body returns. The write lands in the middle of
# TemporaryDirectory cleanup and the run dies with "Directory not empty:
# .../target/.git" against whichever test was running (seen on CI in
# tests.test_secret_recover, secretary-806). The GIT_CONFIG_* trio is honoured by
# every git the suite starts, including those production code spawns.
os.environ.update(
    GIT_CONFIG_COUNT="2",
    GIT_CONFIG_KEY_0="gc.auto",
    GIT_CONFIG_VALUE_0="0",
    GIT_CONFIG_KEY_1="maintenance.auto",
    GIT_CONFIG_VALUE_1="false",
)

# Claim the dispatcher body default before importing test modules. Per-test patches may override it.
_SUITE_BODY_DIR = _SUITE_TMP / "dispatcher-bodies"
_SUITE_BODY_DIR.mkdir()
os.environ["UMMANU_DISPATCHER_BODY_DIR"] = str(_SUITE_BODY_DIR)

# A shell fixture keeps real command execution while removing ambient login/rc side effects.
_SUITE_HOME = _SUITE_TMP / "home"
_SUITE_HOME.mkdir()
os.environ["HOME"] = str(_SUITE_HOME)
os.environ.pop("BASH_ENV", None)
os.environ.pop("ENV", None)
os.environ["PATH"] = (
    str(shell_tools(_SUITE_TMP / "shell-tools")) + os.pathsep + os.environ.get("PATH", os.defpath)
)
