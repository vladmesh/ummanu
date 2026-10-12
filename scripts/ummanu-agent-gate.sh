#!/usr/bin/env bash
# Ummanu automation precheck gate. It is installed only during the explicit cutover.
# It lives as a versioned, unit-testable script rather than the inline `bash -lc '...'` one-liner it
# replaced, so the branch logic is reviewable and covered by tests instead of hiding in a systemd
# unit string (triggered-agents-276).
#
# Exit-code protocol of the role launcher precheck (every agent enters through
# `python3 -P -m ummanu automations`, whose composition root injects the board ports; see
# each agent's cli.py under src/ummanu/automations and ummanu/runtime/state.py PRECHECK_SKIP):
#   0              -> there is work: exec the dispatch, the head wakes up.
#   100            -> deliberate skip (nothing changed / paused): exit successfully, no dispatch.
#                     The gate used to follow with `dispatch --cleanup-only`, a no-op exit 0 for
#                     every agent since the pane lifecycle it tore down was deleted
#                     (secretary-1720), at the cost of one more interpreter per quiet tick
#                     (ummanu-239). The flag is still accepted for manual callers.
#   101            -> the board refused the connection for the client's whole retry window: nothing
#                     was evaluated, so the tick is deferred rather than answered. This gate waits
#                     and re-runs the precheck a bounded number of times before giving up with 101.
#                     A `Persistent=true` timer catches its missed run up seconds after boot, which
#                     is exactly when the docker-hosted board is least likely to be listening, and a
#                     daily unit that crashed there lost the whole day: the timer does not re-fire
#                     (secretary-964). The waiting lives here rather than in the unit because
#                     systemd refuses `RestartForceExitStatus=` on a `Type=oneshot` service, and a
#                     oneshot has no start timeout to run into.
#   102            -> a short role-local settlement transaction is busy. The tick is deliberately
#                     unclaimed: exit successfully without dispatch or cleanup, because either can
#                     race the live transaction holder. Curator uses this for watermark/pending
#                     settlement contention (secretary-1501).
#   any other code -> precheck itself broke (crash, bad env): propagate the code so the unit is
#                     recorded `failed` in systemctl, distinguishable from a skip.
# 100 is deliberately not 1: Python's default uncaught-crash exit code is 1, so a precheck that dies
# before it can return (ImportError, a raise inside its own except handler) lands in the error branch
# below instead of masquerading as a clean skip. That masking was the bug this card fixes.
#
# With a second argument (a variant like the steward's deep-sweep) there is NO gate at all: dispatch
# runs unconditionally (triggered-agents-254). The variant exists to wake the head even when the
# deterministic signals stayed quiet, including the case where the signals themselves went blind).
#
# No login shell (-l): the unit invokes this script directly, not `bash -lc`. Export the per-user
# binary dirs explicitly: systemd's default PATH does not include ~/.local/bin/~/bin, but pipeline
# health probes call user-installed CLIs (`claude`, `codex`). Without this, probes falsely mark
# resources red with FileNotFoundError even though the CLIs are available in the normal dev shell.
set -u

export PATH="$HOME/.local/bin:$HOME/bin:${PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"
# Which checkout the role runs out of: the launcher's explicit TA_RUNTIME_PYTHONPATH, else the
# product checkout this installation is configured with, else the home default. An installation
# materialized from an alternate checkout sets UMMANU_REPO in the unit it renders, and
# skipping that name here would start the role out of ~/ummanu — a checkout the operator may
# never have upgraded, or may not have at all.
product_root="${TA_RUNTIME_PYTHONPATH:-${UMMANU_REPO:-$HOME/ummanu}}"
selected_product_root="$product_root"

# Resolve the root once, before starting a Python process, so the source tree, managed interpreter
# and nested role command cannot come from different checkouts.  The interpreter's resolved binary
# may legitimately be the base Python (normal venvs often use a symlink), so verify its runtime
# prefix instead of requiring the binary's symlink target to sit below .venv.
if [ ! -d "$product_root" ] || ! product_root="$(cd -P -- "$product_root" && pwd)"; then
    echo "[ta-gate] configuration error: selected product checkout '$selected_product_root' is missing or not a directory; repair it through the supported install/upgrade path: ummanu upgrade --no-pull --product-root '$selected_product_root'. Do not use system-wide pip." >&2
    exit 1
fi
if [ ! -d "$product_root/src" ]; then
    echo "[ta-gate] configuration error: selected product checkout '$product_root' has no source tree; repair it through the supported install/upgrade path: ummanu upgrade --no-pull --product-root '$product_root'. Do not use system-wide pip." >&2
    exit 1
fi

managed_venv="$product_root/.venv"
managed_python="$managed_venv/bin/python3"
if [ ! -f "$managed_python" ] || [ ! -x "$managed_python" ]; then
    echo "[ta-gate] configuration error: selected product checkout '$product_root' has no executable managed interpreter at '$managed_python'; repair it through the supported install/upgrade path: ummanu upgrade --no-pull --product-root '$product_root'. Do not use system-wide pip." >&2
    exit 1
fi
if ! "$managed_python" -I -c '
import os
import sys

expected = os.path.normcase(os.path.realpath(sys.argv[1]))
actual = os.path.normcase(os.path.realpath(sys.prefix))
exec_prefix = os.path.normcase(os.path.realpath(sys.exec_prefix))
expected_executable = os.path.normcase(os.path.realpath(sys.argv[2]))
actual_executable = os.path.normcase(os.path.realpath(sys.executable))
raise SystemExit(0 if actual == expected and exec_prefix == expected and actual_executable == expected_executable else 1)
' "$managed_venv" "$managed_python"; then
    echo "[ta-gate] configuration error: selected product checkout '$product_root' has an interpreter at '$managed_python' that is not its managed venv; repair it through the supported install/upgrade path: ummanu upgrade --no-pull --product-root '$product_root'. Do not use system-wide pip." >&2
    exit 1
fi

export PYTHONPATH="$product_root/src${PYTHONPATH:+:$PYTHONPATH}"

agent="${1:?usage: ta-gate.sh <agent> [variant]}"
variant="${2:-}"

# One entry for every agent: `ummanu automations` wires Ummanu's canonical
# TaskReader/TaskWriter ports into the board-owning steward and retro itself.
role_command=(-m ummanu automations)

run_role_env() {
    "$managed_python" -P -m ummanu.runtime.role_env exec --role "$agent" -- "$@"
}

exec_role_env() {
    exec "$managed_python" -P -m ummanu.runtime.role_env exec --role "$agent" -- "$@"
}

if [ -n "$variant" ]; then
    exec_role_env "$managed_python" -P "${role_command[@]}" "$agent" dispatch "$variant"
fi

# How long the gate keeps re-attempting a precheck that could not reach the board: attempts spaced
# by TA_GATE_BOARD_WAIT seconds, on top of the retry window the client itself already spends inside
# each attempt. The total has to comfortably cover a board coming up a minute or two after a reboot,
# and has to stay bounded so a board that is really gone still ends the unit `failed` and visible.
board_attempts="${TA_GATE_BOARD_ATTEMPTS:-5}"
board_wait="${TA_GATE_BOARD_WAIT:-120}"

attempt=1
while : ; do
    run_role_env "$managed_python" -P "${role_command[@]}" "$agent" precheck
    rc=$?
    if [ "$rc" -ne 101 ] || [ "$attempt" -ge "$board_attempts" ]; then
        break
    fi
    echo "[ta-$agent] precheck: board unreachable (attempt $attempt/$board_attempts); retrying in ${board_wait}s" >&2
    sleep "$board_wait"
    attempt=$((attempt + 1))
done

if [ "$rc" -eq 0 ]; then
    exec_role_env "$managed_python" -P "${role_command[@]}" "$agent" dispatch
elif [ "$rc" -eq 100 ]; then
    echo "[ta-$agent] precheck: no change, skill dispatch skipped"
    exit 0
elif [ "$rc" -eq 101 ]; then
    # No dispatch and no cleanup: both talk to the same board none of the attempts could reach.
    echo "[ta-$agent] precheck: board unreachable after $board_attempts attempts; run not taken" >&2
    exit "$rc"
elif [ "$rc" -eq 102 ]; then
    echo "[ta-$agent] precheck: settlement busy, tick deferred" >&2
    exit 0
else
    echo "[ta-$agent] precheck: ERROR (rc=$rc); see runs.jsonl" >&2
    exit "$rc"
fi
