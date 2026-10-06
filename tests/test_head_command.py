"""The one head command renderer, and the grep invariant that it is the only one.

Two things are asserted here, and they are the two halves of secretary-1415.

The first is *what* the renderer produces, spelled out literally. Until this card the product had
three renderers — the dispatcher's, the pipeline registry's and the operator shell's — and the
whole risk of collapsing them into one is that some head quietly comes up under a different command
than it did before. So every shape is pinned as an exact string rather than as an `assertIn` of the
interesting flag: a substring check passes just as happily when the rest of the command changed.
The strings below are the ones the three old renderers produced on 2026-08-11 for the same inputs,
compared profile by profile across the shipped registry before they were deleted.

The second is that no fourth renderer, and no second way to reach the session manager, can come
back. `SeamGrepTests` reads every module of both packages and fails on the calls themselves, so a
docstring that quotes `orca terminal` or a comment that spells a `claude` invocation is free to say
so — which matters, because the modules that own those things have to explain them.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import source_trees
from ummanu.runtime.head import (
    CLAUDE_EFFORTS,
    CODEX_EFFORTS,
    HEAD_BINDING,
    STANDING_BINDING,
    HeadCommandError,
    render_head_command,
    with_pid_heartbeat,
    wrap_role_command,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# One installation, written out, so a wrapper assertion is about the wrapper rather than about
# whatever the developer's shell happens to export.
LAUNCH_ENV = {
    "UMMANU_RUNTIME_ENV_FILE": "/opt/inst/runtime.env",
    "UMMANU_INSTANCE": "/opt/inst",
    "UMMANU_REPO": "/opt/checkout",
    "HOME": "/home/nobody",
}
BINDING = (
    "UMMANU_RUNTIME_ENV_FILE=/opt/inst/runtime.env UMMANU_INSTANCE=/opt/inst "
    "UMMANU_REPO=/opt/checkout"
)
CLAUDE_MEMORY_CONFIG = json.dumps(
    {
        "mcpServers": {
            "memory": {
                "type": "http",
                "url": "http://127.0.0.1:8077/mcp",
                "headers": {"Authorization": "Bearer ${UMMANU_MEMORY_ACCESS_TOKEN}"},
            }
        }
    },
    separators=(",", ":"),
)
CLAUDE_BASE = shlex.join(
    [
        "claude",
        "--dangerously-skip-permissions",
        "--strict-mcp-config",
        "--mcp-config",
        CLAUDE_MEMORY_CONFIG,
    ]
)


class ClaudeShapeTests(unittest.TestCase):
    """A `claude` head's command line, with and without the things a profile may pin."""

    def render(self, profile: dict, **kwargs) -> str:
        return render_head_command(profile, **kwargs).command

    def test_a_profile_that_pins_nothing_is_the_bare_invocation(self) -> None:
        self.assertEqual(
            self.render({"adapter": "claude"}),
            CLAUDE_BASE,
        )

    def test_a_pinned_model_is_named_on_the_command_line(self) -> None:
        self.assertEqual(
            self.render({"adapter": "claude", "model": "opus"}),
            f"{CLAUDE_BASE} --model opus",
        )

    def test_every_supported_effort_renders_and_the_default_renders_nothing(self) -> None:
        """`default` is the CLI's own choice and must not become an explicit `--effort default`."""
        for effort in sorted(CLAUDE_EFFORTS):
            with self.subTest(effort=effort):
                expected = f"{CLAUDE_BASE} --model opus"
                if effort != "default":
                    expected += f" --effort {effort}"
                self.assertEqual(
                    self.render({"adapter": "claude", "model": "opus", "effort": effort}), expected
                )

    def test_an_unknown_effort_is_refused_rather_than_passed_through(self) -> None:
        """Fail closed: an effort the CLI does not know is a head that will not start, and finding
        that out from a rendered command beats finding it out from a dead pane."""
        with self.assertRaisesRegex(HeadCommandError, "unknown effort 'unbounded'"):
            self.render({"adapter": "claude", "effort": "unbounded"})

    def test_a_prompt_is_carried_on_the_command_line_when_one_is_given(self) -> None:
        self.assertEqual(
            self.render({"adapter": "claude"}, prompt="/steward --card ummanu-1"),
            f"{CLAUDE_BASE} '/steward --card ummanu-1'",
        )

    def test_no_prompt_means_the_interactive_shape(self) -> None:
        """The dispatcher's shape: nothing of the task on a command line Orca stores, and
        `prompt_after_start` saying the caller still owes this head its prompt."""
        rendered = render_head_command({"adapter": "claude", "model": "opus"}, prompt=None)
        self.assertEqual(rendered.command, f"{CLAUDE_BASE} --model opus")
        self.assertTrue(rendered.prompt_after_start)
        self.assertEqual(rendered.adapter, "claude")

    def test_a_carried_prompt_leaves_nothing_to_deliver(self) -> None:
        self.assertFalse(render_head_command({"adapter": "claude"}, prompt="skill").prompt_after_start)


class CodexShapeTests(unittest.TestCase):
    """A Codex head is one interactive TUI session, and its command says so."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # A directory outside any git repository, so the trust list is the workspace alone and the
        # expected string does not depend on where this checkout happens to sit.
        self.workspace = str(Path(self.tmp.name).resolve())

    def render(self, profile: dict, **kwargs) -> str:
        return render_head_command(
            {"adapter": "codex", "codex_home": "/tmp/codex-home", **profile},
            workspace=self.workspace,
            **kwargs,
        ).command

    def test_the_tui_command_carries_home_model_effort_and_directory_trust(self) -> None:
        self.assertEqual(
            self.render({"model": "gpt-5.5", "effort": "extra"}),
            "CODEX_HOME=/tmp/codex-home codex --dangerously-bypass-approvals-and-sandbox "
            "--enable multi_agent_v2 -c features.multi_agent_v2.wait_agent_enabled=false "
            "-c mcp_servers.po_memory.enabled=false "
            "-c 'mcp_servers.memory.url=\"http://127.0.0.1:8077/mcp\"' "
            "-c 'mcp_servers.memory.bearer_token_env_var=\"UMMANU_MEMORY_ACCESS_TOKEN\"' "
            "-m gpt-5.5 -c 'model_reasoning_effort=\"xhigh\"' "
            f'-c \'projects."{self.workspace}".trust_level="trusted"\'',
        )

    def test_the_tui_command_uses_the_validated_low_fanout_candidate(self) -> None:
        command = self.render({"model": "gpt-5.6-terra"})
        self.assertIn("--enable multi_agent_v2", command)
        self.assertIn("features.multi_agent_v2.wait_agent_enabled=false", command)

    def test_every_supported_effort_maps_to_the_flag_codex_spells(self) -> None:
        for name, flag in sorted(CODEX_EFFORTS.items()):
            with self.subTest(effort=name):
                command = self.render({"effort": name})
                if flag is None:
                    self.assertNotIn("model_reasoning_effort", command)
                else:
                    self.assertIn(f"-c 'model_reasoning_effort=\"{flag}\"'", command)

    def test_an_unknown_effort_is_refused(self) -> None:
        with self.assertRaisesRegex(HeadCommandError, "unknown effort 'turbo'"):
            self.render({"effort": "turbo"})

    def test_a_prompt_never_reaches_a_codex_command_line(self) -> None:
        """Both prompt inputs are for the delivery that follows the launch, whatever the caller
        passed: this adapter has no prompt-carrying shape at all."""
        rendered = render_head_command(
            {"adapter": "codex", "codex_home": "/tmp/codex-home"},
            prompt="read TASK.md first",
            workspace=self.workspace,
        )
        self.assertNotIn("read TASK.md first", rendered.command)
        self.assertNotIn("codex exec", rendered.command)
        self.assertTrue(rendered.prompt_after_start)

    def test_a_codex_head_without_a_workspace_is_refused(self) -> None:
        """The trust override names a directory; there is no honest command without one."""
        with self.assertRaisesRegex(HeadCommandError, "requires workspace"):
            render_head_command({"adapter": "codex"}, workspace="")


class HermesShapeTests(unittest.TestCase):
    def test_a_seeded_session_and_an_empty_repl_differ_only_by_the_seed(self) -> None:
        profile = {"adapter": "hermes", "model": "m1", "provider": "p1"}
        self.assertEqual(
            render_head_command(profile, prompt="skill").command,
            "hermes -z 'skill' -m m1 --provider p1 --yolo --cli",
        )
        self.assertEqual(
            render_head_command(profile, prompt=None).command,
            "hermes -m m1 --provider p1 --yolo --cli",
        )


class UnknownAdapterTests(unittest.TestCase):
    def test_an_adapter_with_no_renderer_is_refused_by_name(self) -> None:
        with self.assertRaisesRegex(HeadCommandError, "unknown adapter 'gemini'"):
            render_head_command({"adapter": "gemini"})

    def test_a_profile_without_an_adapter_is_not_a_head(self) -> None:
        with self.assertRaisesRegex(HeadCommandError, "unknown adapter ''"):
            render_head_command({})


class RoleEnvWrapperTests(unittest.TestCase):
    """The wrapper is part of the rendered command, and which binding renders it is the
    launcher's fact — the two the product has render two different PYTHONPATHs in front of the
    one role-env entry point."""

    def test_every_launched_role_runs_the_one_role_env_entry_point(self) -> None:
        """Worker, reviewer, observer and a standing agent: one module, one `exec --role` CLI."""
        cases = (
            ("worker", HEAD_BINDING, "/worktree", None),
            ("reviewer", HEAD_BINDING, "/worktree", None),
            (
                "observer",
                HEAD_BINDING,
                "",
                {"UMMANU_OBSERVER_SPRINT": "sprint:9", "UMMANU_OBSERVER_GENERATION": "3"},
            ),
            ("curator", STANDING_BINDING, "", None),
        )
        for role, binding, workspace, identity in cases:
            with self.subTest(role=role), mock.patch.dict(os.environ, LAUNCH_ENV, clear=True):
                command = wrap_role_command(
                    role, "true", binding=binding, workspace=workspace, identity=identity
                )
                workspace_arg = f" --workspace {workspace}" if workspace else ""
                self.assertIn(
                    " python3 -P -m ummanu.runtime.role_env exec "
                    f"--role {role}{workspace_arg} -- /bin/sh -lc ",
                    command,
                )
                self.assertEqual(command.count(" -m "), 1)

    def test_the_head_binding_writes_the_installation_into_the_command(self) -> None:
        with mock.patch.dict(os.environ, LAUNCH_ENV, clear=True):
            self.assertEqual(
                render_head_command(
                    {"adapter": "claude"},
                    role="worker",
                    binding=HEAD_BINDING,
                    workspace="/worktree",
                ).command,
                f'{BINDING} PYTHONPATH=/opt/checkout/src"${{PYTHONPATH:+:$PYTHONPATH}}" '
                "python3 -P -m ummanu.runtime.role_env exec --role worker --workspace /worktree -- "
                "/bin/sh -lc "
                + shlex.quote(
                    "test -x /opt/checkout/src/ummanu/runtime/docker-bin/docker || "
                    "{ printf '%s\\n' 'docker-guard: executable unavailable; repair the role launch' >&2; exit 125; }; "
                    "PATH=/opt/checkout/src/ummanu/runtime/docker-bin:/worktree/.ummanu-task-env/venv/bin"
                    "${PATH:+:$PATH}; export PATH; " + CLAUDE_BASE
                ),
            )

    def test_the_standing_binding_is_what_a_background_agent_is_launched_under(self) -> None:
        with mock.patch.dict(os.environ, LAUNCH_ENV, clear=True):
            expected = (
                f"{BINDING} PYTHONPATH=/opt/checkout/src python3 -P -m "
                "ummanu.runtime.role_env exec --role steward -- /bin/sh -lc "
                + shlex.quote(
                    f"PATH=/opt/checkout/.venv/bin${{PATH:+:$PATH}}; export PATH; {CLAUDE_BASE} '/steward'"
                )
            )
            self.assertEqual(
                render_head_command(
                    {"adapter": "claude"},
                    prompt="/steward",
                    role="steward",
                    binding=STANDING_BINDING,
                ).command,
                expected,
            )

    def test_an_identity_is_rendered_beside_the_installation_binding(self) -> None:
        with mock.patch.dict(os.environ, LAUNCH_ENV, clear=True):
            command = render_head_command(
                {"adapter": "claude"},
                role="observer",
                identity={
                    "UMMANU_OBSERVER_SPRINT": "sprint:9",
                    "UMMANU_OBSERVER_GENERATION": "3",
                },
            ).command
        self.assertIn(
            f"{BINDING} UMMANU_OBSERVER_GENERATION=3 UMMANU_OBSERVER_SPRINT=sprint:9 ",
            command,
        )

    def test_a_role_whose_allowlist_does_not_know_a_name_is_refused(self) -> None:
        """Anything outside the role's allowlist would be dropped by `runtime_env` on the way in;
        refusing here is the difference between a head that will not start and one that starts
        without the binding its caller believed it had."""
        with self.assertRaisesRegex(HeadCommandError, "UMMANU_OBSERVER_SPRINT"):
            wrap_role_command("worker", "true", identity={"UMMANU_OBSERVER_SPRINT": "s"})

    def test_the_standing_binding_renders_no_identity_and_says_so(self) -> None:
        with self.assertRaisesRegex(HeadCommandError, "renders no identity"):
            wrap_role_command(
                "observer",
                "true",
                binding=STANDING_BINDING,
                identity={"UMMANU_OBSERVER_SPRINT": "s"},
            )

    def test_an_unwrapped_command_is_what_the_operator_shell_execs(self) -> None:
        """`ummanu shell` runs in a terminal the operator already owns, so there is no role to
        bind — and no identity to render into a command nothing launched."""
        self.assertEqual(
            render_head_command({"adapter": "claude"}).command,
            CLAUDE_BASE,
        )
        with self.assertRaisesRegex(HeadCommandError, "carries no identity"):
            render_head_command({"adapter": "claude"}, identity={"UMMANU_OBSERVER_SPRINT": "s"})

    def test_an_unknown_binding_is_refused_rather_than_defaulted(self) -> None:
        with self.assertRaisesRegex(HeadCommandError, "unknown role env binding"):
            wrap_role_command("worker", "true", binding="ummanu.runtime.role_env")

    def test_local_run_policy_is_an_explicit_argument_only_for_card_heads(self) -> None:
        policy = json.dumps({"argv": ["docker", "run", "owner's two words", ""]})
        for role in ("worker", "reviewer"):
            with self.subTest(role=role), mock.patch.dict(os.environ, LAUNCH_ENV, clear=True):
                vector = shlex.split(render_head_command(
                    {"adapter": "claude"}, role=role, workspace="/worktree", local_run_policy=policy,
                ).command)
                self.assertEqual(vector[vector.index("--local-run-policy") + 1], policy)
                with self.assertRaisesRegex(HeadCommandError, "renders no identity"):
                    wrap_role_command(role, "true", binding=STANDING_BINDING, local_run_policy=policy)
        with self.assertRaisesRegex(HeadCommandError, "carries no identity"):
            render_head_command({"adapter": "claude"}, local_run_policy=policy)
        with self.assertRaisesRegex(HeadCommandError, "carries no local-run policy"):
            wrap_role_command("observer", "true", local_run_policy=policy)


class PidHeartbeatTests(unittest.TestCase):
    def test_the_pid_written_is_the_head_s_own(self) -> None:
        """`$$` then `exec env`: a real shell runs first so `$$` means something, and `env` keeps
        `exec` a single-word invocation despite the leading assignments a wrapped command starts
        with."""
        wrapped = with_pid_heartbeat(
            "PYTHONPATH=/x python3 -m thing",
            "/run/head.pid",
            identity={"run_id": "run-1", "role": "worker", "task": "card:1"},
        )

        self.assertIn("python3 -P -c", wrapped)
        self.assertIn("os.replace", wrapped)
        self.assertTrue(wrapped.endswith("; exec env PYTHONPATH=/x python3 -m thing"))


class EveryCallerRendersThroughThisModuleTests(unittest.TestCase):
    """The three callers that used to have a renderer each, asked for what they ask for."""

    def test_the_dispatcher_brings_a_head_up_on_exactly_the_rendered_command(self) -> None:
        """`CommandHostRuntime.head_launch` is a lookup, a workspace preflight and this renderer —
        asked for the interactive shape, whatever prompt inputs its caller resolved."""
        from ummanu.dispatch.host import InstanceCatalog

        profile = {"adapter": "claude", "model": "opus", "effort": "high"}
        with tempfile.TemporaryDirectory() as tmp:
            catalog = object.__new__(InstanceCatalog)
            catalog._heads = {"profiles": {"claude-opus-high": profile}}  # type: ignore[attr-defined]
            env = {**LAUNCH_ENV, "TA_CLAUDE_JSON": str(Path(tmp) / ".claude.json")}
            with mock.patch.dict(os.environ, env, clear=True):
                launch = catalog.head_launch(  # type: ignore[attr-defined]
                    "claude-opus-high",
                    "TASK.md",
                    workspace=tmp,
                    role="worker",
                    launch_prompt="read TASK.md first",
                )
                expected = render_head_command(profile, prompt=None, workspace=tmp, role="worker")

        self.assertEqual(launch.command, expected.command)
        self.assertTrue(launch.prompt_after_start)
        self.assertEqual(launch.adapter, "claude")
        self.assertNotIn("read TASK.md first", launch.command)
        self.assertNotIn("TASK.md", launch.command)

    def test_the_operator_shell_renders_the_same_command_the_registry_would(self) -> None:
        from ummanu import session
        from ummanu.runtime import heads

        registry = heads.load_registry()
        for pid in registry.known():
            with self.subTest(profile=pid):
                self.assertEqual(
                    session.render_interactive(pid, workspace="/tmp/ws", registry=registry),
                    render_head_command(registry.profile(pid), prompt=None, workspace="/tmp/ws").command,
                )

    def test_a_background_agent_with_no_head_gets_no_command(self) -> None:
        """secretary-1720: an agent a registry routes nowhere is refused, not launched on a bare
        default-model `claude` invocation. A supervisor raises a head from its profile's spec, and
        such a launch has none, so the tick fails closed rather than improvising a command."""
        from ummanu.automations.runtime import dispatch

        with (
            mock.patch.dict(os.environ, LAUNCH_ENV, clear=True),
            mock.patch.object(dispatch, "_load_spec", return_value={"skill": "/curator"}),
            mock.patch.object(dispatch, "_preferred_head", return_value=""),
            self.assertRaisesRegex(dispatch.NoSupervisedHead, "no head profile is routed to curator"),
        ):
            dispatch._launch_cmd("curator", snapshot=dispatch.RegistrySnapshot(registry=object()))


def _module_paths() -> list[Path]:
    """Every product module, the background agents' included, minus the head package.

    The Orca pane host that used to be excused here as the seam was deleted in secretary-1725, so
    no module outside the head package is excused from any check below.
    """
    head_package = REPO_ROOT / "src" / "ummanu" / "runtime" / "head"
    paths = []
    for path in sorted((REPO_ROOT / "src" / "ummanu").rglob("*.py")):
        if head_package in path.parents:
            continue
        paths.append(path)
    return paths


# The subcommands the Orca terminal CLI has. A vector is recognised by this shape rather than by
# the spelling of the binary in front of it, because the binary is exactly the part a module is
# free to hold in a variable: the scheduler used to keep it in an `ORCA` constant and hand its
# runner only the `["terminal", ...]` suffix. A check that recognised the literal
# `["orca", "terminal", ...]` alone would have called that module clean.
_TERMINAL_SUBCOMMANDS = frozenset({"create", "close", "list", "read", "send", "stop", "wait"})

# Modules excused from the two checks below. Empty, and that is the point: the mechanical-role
# scheduler was the last entry (`triggered_agents/runtime/dispatch.py`, sprint:927) and it went
# behind the host in secretary-1416, which is what turned "no terminal driving outside the seam
# except there" into an assertion about the whole tree. The mechanism is kept rather than deleted
# so that excusing a module stays something written down and argued for; the test below fails on
# any entry at all, so the way to make a new violation green is to fix it.
_SEAM_EXCEPTIONS: dict[Path, str] = {}


def _terminal_vectors(tree: ast.AST) -> list[tuple[int, str]]:
    """Every argument vector this module builds for the Orca terminal CLI, as (line, subcommand).

    Both spellings count as one thing: `["orca", "terminal", "send", ...]` (binary written out)
    and `["terminal", "send", ...]` handed to a runner that prepends the binary from a variable.
    So the subcommand word is looked for at index 0 or 1, which is where it lands in either form,
    and the element before it is not required to be a constant at all.
    """
    found: list[tuple[int, str]] = []
    for node in source_trees.walk(tree):
        if not isinstance(node, (ast.List, ast.Tuple)):
            continue
        words = [
            element.value if isinstance(element, ast.Constant) and isinstance(element.value, str) else None
            for element in node.elts
        ]
        for index in (0, 1):
            if (
                index + 1 < len(words)
                and words[index] == "terminal"
                and words[index + 1] in _TERMINAL_SUBCOMMANDS
            ):
                found.append((node.lineno, words[index + 1]))
                break
    return found


def _pane_screen_reads(tree: ast.AST) -> list[int]:
    """Every place this module asks a pane for its rendered screen, by name.

    A rule of its own rather than a consequence of the one above: a module can read a screen
    through a vector built some other way, and the scheduler's own screen read was the live proof
    that assuming `terminal read` is spelled only inside the pane host was wrong. So the words are
    looked for both as a vector and as a non-docstring string constant.
    """
    prose = _docstring_nodes(tree)
    lines = [lineno for lineno, sub in _terminal_vectors(tree) if sub == "read"]
    for node in source_trees.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in prose:
            continue
        if re.search(r"\bterminal read\b", node.value):
            lines.append(node.lineno)
    return sorted(set(lines))


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """The string constants that are documentation rather than data.

    A grep invariant that fails on prose is a grep invariant that gets its modules stripped of the
    explanations they need: a module has to be able to name the CLI it stopped calling, and `heads` has to say what
    it stopped doing. So docstrings are collected here and skipped below, and comments never enter
    an AST at all — what is left is the literals a call is actually built out of.
    """
    out: set[int] = set()
    for node in source_trees.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", [])
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            out.add(id(body[0].value))
    return out


# A head's shell command names its adapter's binary and that binary's own skip-every-question flag
# in one string. A probe that runs the same binary as an argument vector (`["claude", "-p",
# "ping", ...]`, the health check) does not, which is the difference this list is written to see:
# what is forbidden outside the seam is *assembling a shell command*, not naming a CLI.
_HEAD_COMMAND_LITERALS = (
    "claude --dangerously-skip-permissions",
    "codex --dangerously-bypass-approvals-and-sandbox",
    "hermes -z",
    "role_env exec --role",
)


class SeamGrepTests(unittest.TestCase):
    """No second way to reach the session manager, and no second place a head command is built.

    The check is over calls, not mentions: an `orca terminal` argument vector is recognised by the
    subcommand word wherever the binary in front of it came from — written out, or taken from a
    variable and prepended by a runner — and a head command is a string literal carrying a binary
    and its own flag. Both are read out of the AST with docstrings excluded, so the modules that
    own these things stay free to explain them in prose — which several of them have to.

    Reading a pane's screen gets a rule of its own rather than riding on the vector rule. The
    invariant these tests hold is "no terminal driving outside the seam anywhere" — with the
    mechanical-role scheduler moved behind the host (secretary-1416) there is no module left to
    excuse, and `_SEAM_EXCEPTIONS` is empty rather than absent so that re-excusing one is a visible
    edit that fails a test rather than a quiet skip.
    """

    def test_no_module_outside_the_seam_calls_orca_terminal(self) -> None:
        offenders = []
        for path in _module_paths():
            if path.relative_to(REPO_ROOT) in _SEAM_EXCEPTIONS:
                continue
            tree = source_trees.parse(path.read_text(encoding="utf-8"))
            for lineno, sub in _terminal_vectors(tree):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno} (terminal {sub})")
        self.assertEqual(offenders, [], f"orca terminal argument vectors in src/ummanu: {offenders}")

    def test_the_check_sees_a_vector_whose_binary_came_from_a_variable(self) -> None:
        """The check is not vacuous, in both of the forms a live call is written in.

        The second fixture is the shape the scheduler used to have — the one that walked past the
        previous spelling of this test — and the third is the counter-example the rule must NOT
        flag: an argument vector that runs a different CLI entirely.
        """
        literal = "run(['orca', 'terminal', 'send', '--terminal', t])"
        from_variable = (
            "ORCA = shutil.which('orca')\n"
            "def go(args):\n"
            "    subprocess.run([ORCA, *args])\n"
            "def send(t):\n"
            "    go(['terminal', 'send', '--terminal', t])\n"
        )
        other_cli = "run(['claude', '-p', 'ping', '--model', 'haiku'])"

        self.assertEqual([sub for _, sub in _terminal_vectors(source_trees.parse(literal))], ["send"])
        self.assertEqual([sub for _, sub in _terminal_vectors(source_trees.parse(from_variable))], ["send"])
        self.assertEqual(_terminal_vectors(source_trees.parse(other_cli)), [])

    def test_no_module_outside_the_seam_reads_a_pane_screen(self) -> None:
        offenders = []
        for path in _module_paths():
            if path.relative_to(REPO_ROOT) in _SEAM_EXCEPTIONS:
                continue
            tree = source_trees.parse(path.read_text(encoding="utf-8"))
            for lineno in _pane_screen_reads(tree):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
        self.assertEqual(offenders, [], f"pane screen reads in src/ummanu: {offenders}")

    def test_the_pane_read_rule_catches_a_read_however_the_vector_was_built(self) -> None:
        """Named, so a screen read is caught even when the vector around it is assembled
        elsewhere — and prose about `terminal read` still passes, which the modules that own the
        call depend on."""
        self.assertEqual(_pane_screen_reads(source_trees.parse("go(['terminal', 'read', '--terminal', t])")), [1])
        self.assertEqual(_pane_screen_reads(source_trees.parse("cmd = 'orca terminal read --terminal ' + t")), [1])
        self.assertEqual(_pane_screen_reads(source_trees.parse('"""We used to run terminal read."""')), [])
        # The subcommand is a whole word: a message about a terminal readiness probe is prose
        # about a different call, and flagging it would push the dispatcher into rewording errors.
        self.assertEqual(
            _pane_screen_reads(source_trees.parse("raise HostError('terminal readiness unreadable')")), []
        )

    def test_no_module_is_excused_from_the_seam_at_all(self) -> None:
        """The invariant covers the whole tree, and the scheduler is inside it like everything else.

        An entry here — any entry, not just a second one — fails this, so the way to make a new
        violation green is to fix it or to argue for it out loud, never to widen a glob. The last
        module that had one is checked directly as well: it has to be a file the two checks above
        actually walk, with no terminal vector and no pane screen read left in it, or an empty
        exception dict would be saying nothing (secretary-1416).
        """
        self.assertEqual(_SEAM_EXCEPTIONS, {})
        scheduler = REPO_ROOT / "src" / "ummanu" / "automations" / "runtime" / "dispatch.py"
        self.assertIn(scheduler, _module_paths())
        source = scheduler.read_text(encoding="utf-8")
        self.assertEqual(_terminal_vectors(source_trees.parse(source)), [])
        self.assertEqual(_pane_screen_reads(source_trees.parse(source)), [])

    def test_no_module_outside_the_seam_builds_a_head_command(self) -> None:
        offenders = []
        for path in _module_paths():
            tree = source_trees.parse(path.read_text(encoding="utf-8"))
            prose = _docstring_nodes(tree)
            for node in source_trees.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                if id(node) in prose:
                    continue
                for literal in _HEAD_COMMAND_LITERALS:
                    if literal in node.value:
                        offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} ({literal})")
        self.assertEqual(offenders, [], f"head command assembly outside the head package: {offenders}")


def tearDownModule() -> None:
    source_trees.clear()


if __name__ == "__main__":
    unittest.main()
