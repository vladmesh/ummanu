"""secretary-1474: a mechanical role whose head this product holds under a supervisor of its own.

Since secretary-1720 that supervisor is the only way `runtime/dispatch.py` holds a head of
curator, steward or retro: a tick whose resolution names no `local-pty` head fails closed — it
raises nothing, records why in `runs.jsonl` and exits nonzero — and the pane lifecycle is gone.
These tests are about the bring-up, about what a later tick makes of a head an earlier one raised,
and about each cause of a failed-closed tick.

Nothing about the supervised half is faked, for the reason `test_local_pty_head_runtime` gives:
what the branch has to establish — a head that outlives the tick that raised it, a bring-up over a
live head that is refused before anything is spawned, a record from another boot that fences
nothing out — are facts about processes on this host, and a fake backend would settle none of them.
So every supervised tick here starts a real supervisor over a real pty, and every test gives back
what it started.

That no route to Orca is left in the driver at all is `tests/test_architecture.py`'s to assert,
over the source: there is no pane verb left here to forbid at run time.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import shutil
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ummanu.automations.runtime import dispatch
from ummanu.dispatch.watchdog import head_process_status
from ummanu.head_health import HeadChoice, HeadReadiness
from ummanu.runtime import heads as pipeline_heads
from ummanu.runtime import local_pty_head, role_env
from ummanu.runtime import state as runtime_state
from ummanu.runtime.head import HeadCommand, render_head_command
from ummanu.runtime.head.local_pty import protocol
from ummanu.runtime.head.local_pty.client import SupervisorClient
from ummanu.runtime.head_runtimes import LOCAL_PTY_RUNTIME, ORCA_LEGACY_RUNTIME

REPO = Path(__file__).resolve().parents[1]
#: A head that never exits and says what its own terminal handed it. Both properties are the point:
#: the first is what makes "the head outlived the tick" observable, and the second is the only
#: witness that can say a skill delivered across the boundary actually reached the head.
LINE_READER = REPO / "tests" / "fixtures" / "local_pty_line_reader.py"
HEAD_COMMAND = f"{sys.executable} -u {LINE_READER} --pause 0 --idle 0.2"
#: A head that keeps a composer the way Codex's TUI does: a line typed into it is echoed and kept,
#: and only a carriage return of its own sends it (secretary-1702). This is the head an adapter
#: that takes its prompt after start is, and the only one that can tell a skill that was typed from
#: a skill that was submitted (secretary-1717).
FAKE_TUI = REPO / "tests" / "fixtures" / "local_pty_fake_tui.py"


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    close = stat.rfind(")")
    fields = stat[close + 2 :].split()
    return bool(fields) and fields[0] != "Z"


def _kill(pid: int, number: int = signal.SIGKILL, *, group: bool = False) -> None:
    if pid <= 0:
        return
    try:
        os.killpg(pid, number) if group else os.kill(pid, number)
    except OSError:
        pass


class MechanicalRoleBackendTestCase(unittest.TestCase):
    """One mechanical role, one registry, and a tick that can be run more than once.

    The agent is `retro` rather than `steward` deliberately: the steward's report card is a board
    write, and what these tests are about is which backend holds a head, not what a role reports.
    """

    AGENT = "retro"

    def setUp(self) -> None:
        # /tmp rather than the workspace: a Unix socket address is bounded at about a hundred
        # bytes, and a run root under a workspace path does not fit inside one.
        self.data_dir = Path(tempfile.mkdtemp(prefix="lp-driver-"))
        self.addCleanup(shutil.rmtree, self.data_dir, ignore_errors=True)
        self.root = self.data_dir / "heads"
        self.state_root = Path(tempfile.mkdtemp(prefix="lp-state-"))
        self.addCleanup(shutil.rmtree, self.state_root, ignore_errors=True)
        self.workspace = Path(tempfile.mkdtemp(prefix="lp-ws-"))
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.addCleanup(self._reap_everything)
        self.state = runtime_state.AgentState(self.AGENT, self.state_root / self.AGENT)
        self.prompt_after_start = False
        #: Flags for the composer head a prompt-after-start profile is rendered as.
        self.tui_flags: tuple[str, ...] = ()
        self.submitted_file = self.data_dir / "submitted.jsonl"
        #: Which profile this tick's health resolution lands on. Named per test because a launch
        #: diverted onto another profile is one of the cases the choice has to survive.
        self.resolved = "head"
        #: How many times this tick opened the head registry. One is the contract.
        self.reads = 0

    # -- the registry this tick reads ----------------------------------------------------------

    def _registry(self, **profile: object) -> pipeline_heads.Registry:
        return pipeline_heads.Registry(
            {"acct": {"account": "acct", "probe": "true"}},
            {"head": {"resource": "acct", "adapter": "claude", "fallback": [], **profile}},
            {self.AGENT: "head"},
        )

    def _rendered(
        self, profile, *, prompt=None, workspace="", role="", identity=None, binding=""
    ) -> HeadCommand:
        """The command a head is raised with, substituted for the real renderer.

        What the renderer produces is a provider CLI this test has no business starting, and how it
        produces it is `test_head_command`'s subject. Everything the branch reads — the resolved
        profile and its `runtime` — still comes from the real registry through the real
        `_launch_cmd`; only the program the head runs is this fixture.
        """
        if self.prompt_after_start:
            command = " ".join(
                [sys.executable, "-u", str(FAKE_TUI), str(self.submitted_file), *self.tui_flags]
            )
            return HeadCommand(command, prompt_after_start=True, adapter="codex")
        return HeadCommand(HEAD_COMMAND, prompt_after_start=False, adapter="claude")

    def _reads(self, registry):
        """What each `load_registry()` of this tick answers, and how many it took.

        A registry is one answer for every read. A list is the readings in order, the last one
        standing for any further read: a second reading gets the second answer, which is what an
        ordinary profile publication landing mid-tick used to do to a tick that read twice. A tick
        that takes one reading never sees past the first entry, and `self.reads` is what says so.
        An exception as an answer is a registry that would not load.
        """
        answers = list(registry) if isinstance(registry, list) else [registry]

        def read() -> pipeline_heads.Registry:
            self.reads += 1
            answer = answers.pop(0) if len(answers) > 1 else answers[0]
            if isinstance(answer, Exception):
                raise answer
            return answer

        return read

    @contextlib.contextmanager
    def _tick(self, registry):
        with contextlib.ExitStack() as stack:
            enter = stack.enter_context
            enter(
                mock.patch.dict(
                    os.environ,
                    {
                        "UMMANU_DATA_DIR": str(self.data_dir),
                        # The grant helper is a Ummanu-owned launch boundary.  Keep this
                        # subprocess on the product tree this fixture is testing instead of an
                        # ambient installation's selected checkout.
                        "UMMANU_REPO": str(Path(__file__).resolve().parents[1]),
                    },
                )
            )
            enter(mock.patch.object(runtime_state, "STATE_ROOT", self.state_root))
            enter(mock.patch.object(dispatch, "_workspace", return_value=str(self.workspace)))
            enter(mock.patch.object(dispatch, "_load_spec", return_value={"skill": "/retro"}))
            enter(mock.patch.object(dispatch, "_pipeline_paused", return_value=False))
            enter(mock.patch.object(dispatch, "CLAUDE_JSON", self.data_dir / "claude.json"))
            enter(mock.patch.object(dispatch, "render_head_command", self._rendered))
            enter(mock.patch.object(pipeline_heads, "load_registry", side_effect=self._reads(registry)))
            # The composer head settles in well under a second; the production waits are sized for
            # a real agent starting its MCP servers.
            enter(mock.patch.object(local_pty_head, "PROMPT_QUIET_SECONDS", 0.5))
            enter(mock.patch.object(local_pty_head, "PROMPT_POLL_SECONDS", 0.05))
            enter(mock.patch.object(local_pty_head, "PROMPT_FIRST_OUTPUT_SECONDS", 2.0))
            enter(mock.patch.object(local_pty_head, "SUBMIT_CONFIRM_SECONDS", 3.0))
            enter(
                mock.patch.object(
                    dispatch,
                    "resolve_head_chain",
                    side_effect=lambda preferred, *_: HeadChoice(
                        preferred, self.resolved, HeadReadiness("", "ready", "probe succeeded", 0.0)
                    ),
                )
            )
            yield stack

    def run_tick(self, registry) -> int:
        with self._tick(registry):
            return dispatch.run(self.AGENT, report_board=getattr(self, "board", None))

    # -- what the tick left behind -------------------------------------------------------------

    def actions(self) -> list[str]:
        runs = self.state.dir / "runs.jsonl"
        if not runs.is_file():
            return []
        return [json.loads(line)["action"] for line in runs.read_text(encoding="utf-8").splitlines()]

    def events(self) -> list[dict]:
        runs = self.state.dir / "runs.jsonl"
        if not runs.is_file():
            return []
        return [json.loads(line) for line in runs.read_text(encoding="utf-8").splitlines()]

    def run_dirs(self) -> list[Path]:
        return sorted(path for path in self.root.glob("*") if path.is_dir()) if self.root.is_dir() else []

    def head_pid(self, run_dir: Path) -> int:
        try:
            record = json.loads((run_dir / protocol.PID_FILE_NAME).read_text(encoding="utf-8"))
            return int(record.get("pid") or 0)
        except (OSError, ValueError, TypeError):
            return 0

    def supervisor_pid(self, run_dir: Path) -> int:
        try:
            return int((run_dir / protocol.SUPERVISOR_PID_NAME).read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return 0

    def submitted(self) -> list[str]:
        """What the composer head was asked to do: each prompt a carriage return actually sent."""
        if not self.submitted_file.is_file():
            return []
        lines = self.submitted_file.read_text(encoding="utf-8").splitlines()
        return [json.loads(line)["submitted"].strip() for line in lines if line]

    def head_output(self, run_dir: Path) -> str:
        """What the head has printed, read straight from its own supervisor.

        The head, not a receipt: `local_pty_line_reader` reports the records its own terminal
        handed it, which is the only witness that can say a skill actually reached it.
        """
        socket_path = protocol.socket_path_for(run_dir)
        try:
            with SupervisorClient.connect(socket_path, timeout=5.0) as client:
                return bytes(client.read_output()["bytes_data"]).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - a supervisor that cannot be asked has printed nothing
            return ""

    def _reap_everything(self) -> None:
        for run_dir in self.run_dirs():
            head, supervisor = self.head_pid(run_dir), self.supervisor_pid(run_dir)
            _kill(head, group=True)
            _kill(head)
            _kill(supervisor)
        for run_dir in self.run_dirs():
            self.await_(lambda pid=self.head_pid(run_dir): not _alive(pid), timeout=5.0, soft=True)

    def await_(self, predicate, *, timeout: float = 15.0, message: str = "", soft: bool = False):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        if predicate():
            return True
        if not soft:
            self.fail(message or f"condition never held within {timeout:g}s")
        return False


class BackendChoiceTests(MechanicalRoleBackendTestCase):
    """The resolved profile's own runtime decides, and only `local-pty` raises a head."""

    def test_a_profile_with_no_runtime_key_raises_a_head_under_a_supervisor(self) -> None:
        """secretary-1718: a profile that names no runtime is a `local-pty` head."""
        self.assertEqual(self.run_tick(self._registry()), 0)

        run_dirs = self.run_dirs()
        self.assertEqual(len(run_dirs), 1, "the tick raised no supervised head")
        self.assertTrue(_alive(self.head_pid(run_dirs[0])))
        self.assertEqual(self.actions(), ["supervised-started"])

    def test_a_profile_naming_the_supervisor_raises_a_head_under_one(self) -> None:
        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)

        run_dirs = self.run_dirs()
        self.assertEqual(len(run_dirs), 1, "the tick raised no supervised head")
        self.assertTrue(_alive(self.head_pid(run_dirs[0])))
        self.assertEqual(self.actions(), ["supervised-started"])

    def test_the_backend_is_read_off_the_profile_the_launch_was_rendered_from(self) -> None:
        """Not off a second lookup: the profile the registry routes this agent to is `orca-legacy`,
        and health resolves the launch onto a supervised one, so a driver reading the routed
        profile instead of the resolved one would fail this tick closed."""
        registry = pipeline_heads.Registry(
            {"acct": {"account": "acct", "probe": "true"}},
            {
                "routed": {
                    "resource": "acct",
                    "adapter": "claude",
                    "fallback": ["supervised"],
                    "runtime": ORCA_LEGACY_RUNTIME,
                },
                "supervised": {
                    "resource": "acct",
                    "adapter": "claude",
                    "fallback": [],
                    "runtime": LOCAL_PTY_RUNTIME,
                },
            },
            {self.AGENT: "routed"},
        )

        self.resolved = "supervised"

        self.assertEqual(self.run_tick(registry), 0)

        self.assertEqual(len(self.run_dirs()), 1)
        self.assertEqual(self.state.load_head_profile(), "supervised")


class FailClosedTests(MechanicalRoleBackendTestCase):
    """secretary-1720: a tick with no `local-pty` head to raise starts none, one test per cause.

    Every one of them asserts the same four things: no supervisor was asked to start anything, no
    run directory exists, `runs.jsonl` holds exactly one `no-supervised-head` entry with
    `result="error"` and the cause, and the tick returned nonzero with that cause on stderr. There
    is no pane verb left in the driver for any of them to reach instead; `test_architecture` holds
    that line over the source.
    """

    def run_refused_tick(self, registry, reason: str) -> dict:
        err = io.StringIO()
        with (
            mock.patch.object(
                dispatch,
                "_local_pty_runtime",
                side_effect=AssertionError("a failed-closed tick asked for a supervisor"),
            ),
            contextlib.redirect_stderr(err),
        ):
            self.assertEqual(self.run_tick(registry), dispatch.REFUSED_EXIT)
        self.assertEqual(self.run_dirs(), [], "a failed-closed tick raised a head")
        self.assertIsNone(self.state.load_head_run())
        events = self.events()
        self.assertEqual([event["action"] for event in events], [dispatch.NO_SUPERVISED_HEAD])
        self.assertEqual(events[0]["result"], "error")
        self.assertIn(reason, events[0]["error"])
        self.assertIn(reason, err.getvalue())
        return events[0]

    def test_a_registry_that_will_not_load(self) -> None:
        event = self.run_refused_tick(
            pipeline_heads.HeadRegistryError("heads.yaml: bad table"), "the head registry would not load"
        )

        self.assertIn("heads.yaml: bad table", event["error"])

    def test_no_profile_routed_to_the_role(self) -> None:
        registry = pipeline_heads.Registry(
            {"acct": {"account": "acct", "probe": "true"}},
            {"head": {"resource": "acct", "adapter": "claude", "fallback": []}},
            {},
        )

        self.run_refused_tick(registry, f"no head profile is routed to {self.AGENT}")

    def test_a_profile_that_will_not_make_a_head_spec(self) -> None:
        """The reversed pin: this launch used to stay on a pane as the bare `claude` fallback."""
        self.run_refused_tick(self._registry(adapter="nonsense"), "will not make a head spec")

    def test_a_command_that_will_not_render(self) -> None:
        def unrenderable(*_args, **_kwargs):
            raise ValueError("no binary for this adapter")

        with mock.patch.object(self, "_rendered", side_effect=unrenderable):
            self.run_refused_tick(self._registry(runtime=LOCAL_PTY_RUNTIME), "will not render")

    def test_a_profile_naming_any_other_runtime(self) -> None:
        event = self.run_refused_tick(self._registry(runtime=ORCA_LEGACY_RUNTIME), ORCA_LEGACY_RUNTIME)

        self.assertIn("'head'", event["error"])

    def test_a_launch_diverted_onto_another_runtime_is_refused_too(self) -> None:
        """The registry routes this agent to a supervised head, and this tick's own resolution —
        a red resource is exactly how that happens — lands on a profile naming another runtime.
        What is refused is the resolution, not the routing."""
        registry = pipeline_heads.Registry(
            {"acct": {"account": "acct", "probe": "true"}},
            {
                "routed": {
                    "resource": "acct",
                    "adapter": "claude",
                    "fallback": ["pane"],
                    "runtime": LOCAL_PTY_RUNTIME,
                },
                "pane": {
                    "resource": "acct",
                    "adapter": "claude",
                    "fallback": [],
                    "runtime": ORCA_LEGACY_RUNTIME,
                },
            },
            {self.AGENT: "routed"},
        )
        self.resolved = "pane"

        event = self.run_refused_tick(registry, ORCA_LEGACY_RUNTIME)

        self.assertIn("'pane'", event["error"])

    def test_a_paused_pipeline_is_not_a_refusal(self) -> None:
        with (
            self._tick(self._registry(runtime=ORCA_LEGACY_RUNTIME)),
            mock.patch.object(dispatch, "_pipeline_paused", return_value=True),
        ):
            self.assertEqual(dispatch.run(self.AGENT), 0)

        self.assertEqual(self.actions(), ["paused"])
        self.assertEqual(self.reads, 0, "a paused tick read the head registry")

    def test_a_recorded_pane_refuses_the_bring_up_and_is_left_recorded(self) -> None:
        """A `terminal_handle.json` from the retired pane backend names a head nothing here can
        reach. It is not deleted, and no second head is raised beside it."""
        self.state.save_terminal_handle("term-1", created_at=1.0)
        err = io.StringIO()

        with contextlib.redirect_stderr(err):
            self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), dispatch.REFUSED_EXIT)

        self.assertEqual(self.run_dirs(), [], "a head was raised beside a recorded pane")
        self.assertEqual(self.state.load_terminal_handle(), "term-1", "the pane record was deleted")
        events = self.events()
        self.assertEqual([event["action"] for event in events], [dispatch.SUPERVISED_OWNER_CONFLICT])
        self.assertEqual(events[0]["result"], "error")
        self.assertIn("terminal_handle.json", events[0]["error"])
        self.assertIn("terminal_handle.json", err.getvalue())

    def test_a_pane_record_fences_by_existence_whatever_it_holds(self) -> None:
        """The fence is the file, not its parsed handle: an empty file, unreadable JSON and JSON
        with no `handle` all read as no handle through `AgentState`, and all still refuse."""
        for label, content in (
            ("empty", ""),
            ("unreadable", "{ not json"),
            ("no handle", json.dumps({"created_at": 1.0})),
        ):
            with self.subTest(label):
                self.state.dir.mkdir(parents=True, exist_ok=True)
                self.state.terminal_handle_file.write_text(content, encoding="utf-8")
                self.assertIsNone(self.state.load_terminal_handle(), "the shape parses to a handle")
                runs = self.state.dir / "runs.jsonl"
                runs.unlink(missing_ok=True)
                err = io.StringIO()

                with contextlib.redirect_stderr(err):
                    self.assertEqual(
                        self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), dispatch.REFUSED_EXIT
                    )

                self.assertEqual(self.run_dirs(), [], "a head was raised beside a recorded pane")
                self.assertEqual(
                    self.state.terminal_handle_file.read_text(encoding="utf-8"), content, "the record was changed"
                )
                events = self.events()
                self.assertEqual([event["action"] for event in events], [dispatch.SUPERVISED_OWNER_CONFLICT])
                self.assertEqual(events[0]["result"], "error")
                self.assertIn("terminal_handle.json", err.getvalue())

    def test_cleanup_only_is_a_no_op_for_every_agent(self) -> None:
        """The gate still passes `--cleanup-only` on a precheck skip; it touches nothing at all."""
        for agent in ("curator", "retro", "steward"):
            with (
                self.subTest(agent=agent),
                mock.patch.object(dispatch, "AgentState", side_effect=AssertionError("state was built")),
                mock.patch.object(dispatch, "_pipeline_paused", side_effect=AssertionError("paused read")),
            ):
                self.assertEqual(dispatch.run(agent, cleanup_only=True), 0)


class OneRegistryReadingTests(MechanicalRoleBackendTestCase):
    """A tick opens the registry once, and acts on that one reading from its first verb to its last.

    A tick asks the registry which profile this agent is routed to and which profile its launch
    resolved onto. An ordinary profile publication fits between two readings, so a tick that read
    twice could route on one registry and resolve on the other. Both answers come out of one reading.
    """

    def _published_between_the_old_readings(self) -> list[pipeline_heads.Registry]:
        """The registry a publication lands in the middle of: the first two readings answer
        `orca-legacy` for this agent's profile and the last two answer `local-pty` for the very
        same one. A tick that reads once never reaches the third entry at all."""
        before = self._registry(runtime=ORCA_LEGACY_RUNTIME)
        after = self._registry(runtime=LOCAL_PTY_RUNTIME)
        return [before, before, after, after]

    def test_a_failed_closed_tick_opens_the_registry_once(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                self.run_tick(self._registry(runtime=ORCA_LEGACY_RUNTIME)), dispatch.REFUSED_EXIT
            )

        self.assertEqual(self.reads, 1, "the tick opened the head registry more than once")

    def test_a_supervised_tick_opens_the_registry_once(self) -> None:
        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)

        self.assertEqual(self.reads, 1, "the supervised tick opened the head registry more than once")
        self.assertEqual(len(self.run_dirs()), 1)

    def test_a_publication_mid_tick_is_not_read_by_the_tick_it_lands_in(self) -> None:
        """The one reading is the tick's whole answer: it said `orca-legacy`, so the tick fails
        closed, and the publication is the next tick's business."""
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                self.run_tick(self._published_between_the_old_readings()), dispatch.REFUSED_EXIT
            )

        self.assertEqual(self.run_dirs(), [], "the tick acted on a registry it never read first")
        self.assertEqual(self.reads, 1, "the tick read the registry the publication changed")
        self.assertEqual(self.actions(), [dispatch.NO_SUPERVISED_HEAD])

    def test_a_publication_before_the_tick_is_the_whole_tick(self) -> None:
        after = self._registry(runtime=LOCAL_PTY_RUNTIME)
        stale = self._registry(runtime=ORCA_LEGACY_RUNTIME)

        self.assertEqual(self.run_tick([after, stale, stale, stale]), 0)

        self.assertEqual(self.reads, 1)
        self.assertEqual(len(self.run_dirs()), 1, "the tick raised no supervised head")
        self.assertEqual(self.actions(), ["supervised-started"])


class ManagedInterpreterLaunchTests(MechanicalRoleBackendTestCase):
    """secretary-1708: the head's command puts the product's venv first.

    The command a tick renders is recorded from the real renderer, and the head the supervisor
    then raises is still this module's fixture: a provider CLI is not something these tests start.
    The helper is answered with a sentinel so the rendered command can only carry it through that
    helper.
    """

    SENTINEL = Path("/sentinel/product/.venv/bin")

    def setUp(self) -> None:
        super().setUp()
        self.rendered: list[str] = []

    def _rendered(
        self, profile, *, prompt=None, workspace="", role="", identity=None, binding=""
    ) -> HeadCommand:
        real = render_head_command(
            profile, prompt=prompt, workspace=workspace, role=role, identity=identity, binding=binding
        )
        self.rendered.append(real.command)
        return super()._rendered(
            profile, prompt=prompt, workspace=workspace, role=role, identity=identity, binding=binding
        )

    def assert_rendered_under_the_helper(self) -> None:
        prefix = f"PATH={self.SENTINEL}${{PATH:+:$PATH}}; export PATH; "
        self.assertTrue(self.rendered, "the tick rendered no command")
        for command in self.rendered:
            self.assertIn(f" -- /bin/sh -lc {shlex.quote(prefix)[:-1]}", command)

    def test_a_supervised_head_is_launched_under_the_product_venv(self) -> None:
        with mock.patch.object(role_env, "managed_venv_bin", return_value=self.SENTINEL):
            self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)

        self.assertEqual(len(self.run_dirs()), 1)
        self.assert_rendered_under_the_helper()


class SupervisedDeliveryTests(MechanicalRoleBackendTestCase):
    """Criterion 3: the skill crosses the backend's own boundary."""

    def test_the_skill_reaches_the_head_across_the_boundary(self) -> None:
        self.prompt_after_start = True

        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)

        self.assertEqual(self.submitted(), ["$retro"], "the head was never asked to run its skill")

    def test_a_head_whose_command_carries_its_prompt_is_not_typed_at(self) -> None:
        """The claude shape: the prompt is on the command line.

        There is nothing to deliver, so the receipt is a bring-up and the head is left working.
        """
        self.prompt_after_start = False

        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)

        run_dir = self.run_dirs()[0]
        self.await_(lambda: "UP" in self.head_output(run_dir), message="the head never started")
        self.assertNotIn("$retro", self.head_output(run_dir))


class StandingPromptSubmitTests(MechanicalRoleBackendTestCase):
    """secretary-1717: a prompt-after-start skill is submitted and its turn confirmed, or the tick fails.

    The curator on a Codex local-pty profile had `$curate` typed into its composer and never sent,
    and was then recorded as the role's working head, so every later tick was a
    `supervised-busy-skip head_already_up` over a head that had never been asked to do anything.
    """

    def setUp(self) -> None:
        super().setUp()
        self.prompt_after_start = True

    def test_the_skill_is_submitted_on_its_own_and_a_turn_is_confirmed(self) -> None:
        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)

        self.assertEqual(self.submitted(), ["$retro"], "the skill was typed into the composer and never sent")
        self.assertEqual(self.actions(), ["supervised-started"])
        record = self.state.load_head_run()
        self.assertIsNotNone(record)
        self.assertEqual(record["run_id"], self.run_dirs()[0].name)

    def test_a_skill_the_head_never_takes_fails_the_tick_and_records_no_head(self) -> None:
        self.tui_flags = ("--deaf-enter",)

        with self.assertRaises(dispatch.LocalPtyDispatchError) as caught:
            self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME))

        self.assertIn(local_pty_head.DELIVER_NOT_SUBMITTED, str(caught.exception))
        self.assertEqual(self.submitted(), [])
        self.assertIsNone(self.state.load_head_run(), "a head that never took its skill was recorded as up")
        self.assertEqual(self.actions(), ["supervised-start-failed"])
        (run_dir,) = self.run_dirs()
        self.await_(
            lambda: head_process_status(str(run_dir / protocol.PID_FILE_NAME)).get("state") == "dead",
            message="the head that never took its skill was left running",
        )

        # The next tick is a fresh bring-up, not a busy skip over the head that was never used.
        self.tui_flags = ()
        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)

        self.assertEqual(self.actions(), ["supervised-start-failed", "supervised-started"])
        self.assertEqual(self.submitted(), ["$retro"])


class SupervisedHeadLifetimeTests(MechanicalRoleBackendTestCase):
    """Criteria 4, 5 and 6: the head outlives the tick, and what a later tick makes of it."""

    def raise_one(self) -> Path:
        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)
        run_dirs = self.run_dirs()
        self.assertEqual(len(run_dirs), 1)
        return run_dirs[0]

    def test_the_head_and_its_supervisor_outlive_the_tick_that_raised_them(self) -> None:
        run_dir = self.raise_one()

        self.assertTrue(_alive(self.head_pid(run_dir)), "the head died with its tick")
        self.assertTrue(_alive(self.supervisor_pid(run_dir)), "the supervisor died with its tick")
        record = self.state.load_head_run()
        self.assertEqual(record["run_id"], run_dir.name)
        self.assertEqual(record["workspace"], str(self.workspace))
        self.assertEqual(record["role"], self.AGENT)

    def test_the_record_is_written_the_way_its_neighbours_are(self) -> None:
        """One small file in this agent's own state directory, replaced rather than rewritten."""
        self.raise_one()

        self.assertEqual(self.state.head_run_file, self.state.dir / "head_run.json")
        self.assertEqual(list(self.state.dir.glob("*.tmp")), [], "a temporary file was left behind")
        self.state.head_run_file.write_text("{ not json", encoding="utf-8")
        self.assertIsNone(self.state.load_head_run(), "a corrupt record must not fence the role out")

    def test_a_tick_over_a_working_head_raises_no_second_one_and_sends_no_second_skill(self) -> None:
        """Criterion 5, on the precondition secretary-1468 put in front of the spawn."""
        self.prompt_after_start = True
        run_dir = self.raise_one()
        self.assertEqual(self.submitted(), ["$retro"])
        head, supervisor = self.head_pid(run_dir), self.supervisor_pid(run_dir)

        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)

        self.assertEqual(self.run_dirs(), [run_dir], "a second run was started beside a live head")
        self.assertEqual(
            (self.head_pid(run_dir), self.supervisor_pid(run_dir)),
            (head, supervisor),
            "the live head was replaced",
        )
        self.assertEqual(self.submitted(), ["$retro"], "the busy head was sent a second skill")
        self.assertEqual(self.actions(), ["supervised-started", "supervised-busy-skip"])
        # The tick looks at the recorded head before the bring-up (is its turn over?), which
        # adopts the open turn; either refusal is made before anything is spawned.
        self.assertIn(
            self.events()[-1]["error"],
            ("head_already_up", "turn_in_flight"),
            "the refusal is made before anything was spawned",
        )

    def test_a_dead_head_is_an_ordinary_bring_up(self) -> None:
        """Criterion 6: nothing about a finished run fences its role out of the next tick."""
        run_dir = self.raise_one()
        dead = self.head_pid(run_dir)
        _kill(self.supervisor_pid(run_dir))
        _kill(dead, group=True)
        _kill(dead)
        self.await_(
            lambda: head_process_status(str(run_dir / protocol.PID_FILE_NAME)).get("state") == "dead",
            message="the head never actually died",
        )

        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)

        self.assertEqual(self.run_dirs(), [run_dir], "the dead run was abandoned rather than reused")
        self.await_(
            lambda: _alive(self.head_pid(run_dir)) and self.head_pid(run_dir) != dead,
            message="the tick raised no head over the dead one",
        )
        self.assertEqual(self.actions(), ["supervised-started", "supervised-started"])

    def test_a_record_left_by_a_previous_boot_does_not_fence_the_role_out(self) -> None:
        """The reboot case: a run directory full of files and a pid that means nothing.

        Refusing here would leave a mechanical role off duty for good, since nothing rewrites that
        record except the bring-up the refusal is preventing.
        """
        run_dir = self.raise_one()
        pid_file = run_dir / protocol.PID_FILE_NAME
        stale = self.head_pid(run_dir)
        _kill(self.supervisor_pid(run_dir))
        _kill(stale, group=True)
        _kill(stale)
        self.await_(lambda: not _alive(stale), message="the head never actually died")
        record = json.loads(pid_file.read_text(encoding="utf-8"))
        record["boot_id"] = "00000000-0000-0000-0000-000000000000"
        pid_file.write_text(json.dumps(record), encoding="utf-8")

        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)

        self.await_(
            lambda: _alive(self.head_pid(run_dir)),
            message="a record from another boot became a permanent refusal to go on duty",
        )
        self.assertEqual(self.actions(), ["supervised-started", "supervised-started"])


class IdleHeadRetirementTests(MechanicalRoleBackendTestCase):
    """A head whose turn is over and that has sat idle since does not hold its role off duty.

    Codex's TUI does not exit after its turn: the curator's head wrote its memory, advanced, and
    then sat at its composer, and every later hourly tick was a `supervised-busy-skip
    head_already_up` over a head with nothing left to do, until someone stopped it by hand.
    """

    def setUp(self) -> None:
        super().setUp()
        self.prompt_after_start = True

    def finished_head(self) -> Path:
        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)
        (run_dir,) = self.run_dirs()
        self.assertEqual(self.submitted(), ["$retro"])
        journal = run_dir / "journal.jsonl"

        def settled() -> bool:
            kinds = [json.loads(line).get("kind") for line in journal.read_text(encoding="utf-8").splitlines()]
            turns = [kind for kind in kinds if kind in ("turn.started", "turn.finished")]
            return bool(turns) and turns[-1] == "turn.finished"

        self.await_(settled, message="the head's turn never finished")
        return run_dir

    def test_an_idle_head_is_retired_and_the_tick_runs_on_a_fresh_one(self) -> None:
        run_dir = self.finished_head()
        first = self.head_pid(run_dir)

        with mock.patch.object(dispatch, "IDLE_HEAD_GRACE_SECONDS", 0.5):
            time.sleep(0.6)
            self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)

        self.assertEqual(
            self.actions(),
            ["supervised-started", dispatch.SUPERVISED_IDLE_STOP, "supervised-started"],
            "the tick was busy-skipped over a head whose turn had long finished",
        )
        self.assertEqual(self.submitted(), ["$retro", "$retro"], "the tick never ran its skill")
        self.assertFalse(_alive(first), "the idle head was left running beside its successor")
        self.await_(
            lambda: _alive(self.head_pid(run_dir)) and self.head_pid(run_dir) != first,
            message="no fresh head was raised",
        )

    def test_a_head_inside_the_grace_is_still_busy_skipped(self) -> None:
        run_dir = self.finished_head()
        head = self.head_pid(run_dir)

        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)

        self.assertEqual(self.actions(), ["supervised-started", "supervised-busy-skip"])
        self.assertEqual(self.head_pid(run_dir), head)
        self.assertTrue(_alive(head))


class BackendHandoverTests(MechanicalRoleBackendTestCase):
    """A supervised head whose role's resolution stops naming a supervisor is left alone.

    Publishing a profile that names another runtime for a role whose head is already up used to
    hand the role to a pane by stopping that head. There is no pane any more, so a stop would hand
    the role to nothing and could end a working head mid-turn over a momentarily unreadable
    registry. A failed-closed tick changes nothing: the head finishes under its own supervisor, its
    record stands, and the next tick with a usable profile finds it through that record.
    """

    def refused_tick(self, registry) -> int:
        with contextlib.redirect_stderr(io.StringIO()):
            return self.run_tick(registry)

    def test_a_live_supervised_head_is_untouched_when_its_role_stops_naming_a_supervisor(self) -> None:
        """`local-pty -> orca-legacy`: nothing is raised and nothing is stopped."""
        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)
        run_dir = self.run_dirs()[0]
        head, supervisor = self.head_pid(run_dir), self.supervisor_pid(run_dir)
        record = self.state.load_head_run()
        self.assertTrue(_alive(head), "the first tick raised no live head")

        with mock.patch.object(
            dispatch, "_local_pty_runtime", side_effect=AssertionError("a failed-closed tick asked for a supervisor")
        ):
            self.assertEqual(
                self.refused_tick(self._registry(runtime=ORCA_LEGACY_RUNTIME)), dispatch.REFUSED_EXIT
            )

        self.assertTrue(_alive(head), "a failed-closed tick stopped the role's live head")
        self.assertTrue(_alive(supervisor), "a failed-closed tick stopped the head's supervisor")
        self.assertEqual(self.state.load_head_run(), record, "a failed-closed tick changed the head's record")
        self.assertEqual(self.run_dirs(), [run_dir], "a failed-closed tick raised another head")
        self.assertEqual(self.actions(), ["supervised-started", dispatch.NO_SUPERVISED_HEAD])

        # A later tick with a usable profile finds the head through its record, as it always has.
        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)

        self.assertEqual(self.run_dirs(), [run_dir])
        self.assertEqual(self.actions()[-1], "supervised-busy-skip")

    def test_a_supervised_head_that_has_already_ended_is_left_recorded(self) -> None:
        """A failed-closed tick does not tidy up either: the record of a dead head stays, and the
        next usable tick's bring-up over it is the ordinary one."""
        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)
        run_dir = self.run_dirs()[0]
        record = self.state.load_head_run()
        dead = self.head_pid(run_dir)
        _kill(self.supervisor_pid(run_dir))
        _kill(dead, group=True)
        _kill(dead)
        self.await_(
            lambda: head_process_status(str(run_dir / protocol.PID_FILE_NAME)).get("state") == "dead",
            message="the head never actually died",
        )

        self.assertEqual(self.refused_tick(self._registry(runtime=ORCA_LEGACY_RUNTIME)), dispatch.REFUSED_EXIT)

        self.assertEqual(self.state.load_head_run(), record)
        self.assertEqual(self.actions(), ["supervised-started", dispatch.NO_SUPERVISED_HEAD])


class StewardBoard:
    """The pipeline board a steward tick writes to, as much of it as a tick can tell apart.

    A report card is created In progress and moved out of it, and the whole question these tests
    ask is which cards exist and which column they end in — so this is the board reduced to
    exactly that, with no transport under it.
    """

    def __init__(self) -> None:
        self.cards: list[dict] = []
        self.moves: list[tuple[str, str, str]] = []

    def create_report(self, *, project: str, title: str, slug: str) -> str:
        card = {
            "reference": f"ummanu-report-{len(self.cards) + 1}",
            "column": "In progress",
            "steward_report": "1",
            "date_moved": time.time(),
            "title": title,
            "slug": slug,
            "project": project,
        }
        self.cards.append(card)
        return str(card["reference"])

    def in_progress_reports(self, *, project: str) -> list[dict]:
        return [card for card in self.cards if card["column"] == "In progress" and card["project"] == project]

    def move_report(self, *, reference: str, target: str, reason: str) -> None:
        column = {"done": "Done", "blocked": "Blocked"}[target]
        for card in self.cards:
            if card["reference"] == reference:
                card["column"] = column
                card["date_moved"] = time.time()
                self.moves.append(("steward", reference, column))
                return
        raise AssertionError(f"a tick moved a card that was never created: {reference}")

    def in_progress(self) -> list[str]:
        return [card["reference"] for card in self.cards if card["column"] == "In progress"]


class StewardBackendHandoverTests(MechanicalRoleBackendTestCase):
    """The one mechanical role that reports on itself.

    `retro` has no reporting contract, so the tests above can say nothing about it: a steward
    dispatch creates a report card as it renders the skill naming it, hands it to the head it
    launches, and records in `active_report.json` which head is writing which card. Everything
    below is about what a tick owes that card.

    A tick that raises nothing creates no card of its own, because the refusal is decided before a
    command is built. It also closes none it finds: the card a live head is writing is that head's.
    """

    AGENT = "steward"

    def setUp(self) -> None:
        super().setUp()
        self.board = StewardBoard()

    @contextlib.contextmanager
    def _tick(self, registry):
        with (
            super()._tick(registry) as running,
            mock.patch.object(dispatch, "_load_spec", return_value={"skill": "/steward"}),
        ):
            yield running

    def refused_tick(self, registry) -> int:
        with contextlib.redirect_stderr(io.StringIO()):
            return self.run_tick(registry)

    def _standing_report(self) -> str:
        """The card the head this role currently has is writing, as the record names it."""
        record = self.state.load_active_report() or {}
        reference = record.get("reference")
        self.assertTrue(reference, "the tick that raised this role's head recorded no report card")
        self.assertEqual(self.board.in_progress(), [reference])
        return reference

    def test_a_failed_closed_tick_creates_no_report_card(self) -> None:
        for registry in (
            self._registry(runtime=ORCA_LEGACY_RUNTIME),
            self._registry(adapter="nonsense"),
            pipeline_heads.HeadRegistryError("heads.yaml: bad table"),
        ):
            with self.subTest(registry=registry):
                self.assertEqual(self.refused_tick(registry), dispatch.REFUSED_EXIT)
                self.assertEqual(self.board.cards, [], "a tick that raised nothing filed a report card")
        self.assertEqual(self.actions(), [dispatch.NO_SUPERVISED_HEAD] * 3)

    def test_a_card_whose_command_will_not_render_after_all_is_closed_undispatched(self) -> None:
        """The resolution renders once without a card; should the render with the card refuse
        after all, the card it was made for is closed through `_TickReports` before the refusal."""
        rendered = self._rendered

        def refuse_with_a_card(profile, *, prompt=None, **kwargs):
            if prompt and "--card" in prompt:
                raise ValueError("the card argument broke the renderer")
            return rendered(profile, prompt=prompt, **kwargs)

        with mock.patch.object(self, "_rendered", side_effect=refuse_with_a_card):
            self.assertEqual(
                self.refused_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), dispatch.REFUSED_EXIT
            )

        self.assertEqual(self.run_dirs(), [])
        self.assertEqual(len(self.board.cards), 1)
        self.assertEqual(self.board.in_progress(), [], "the card of a tick that raised nothing is open")
        self.assertIsNone(self.state.load_active_report())
        self.assertEqual(self.actions(), ["dispatch-release", dispatch.NO_SUPERVISED_HEAD])

    def test_a_live_supervised_head_keeps_its_report_on_a_failed_closed_tick(self) -> None:
        """`local-pty -> orca-legacy`: the head, its record and the card it is writing all stay."""
        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)
        run_dir = self.run_dirs()[0]
        head = self.head_pid(run_dir)
        self.assertTrue(_alive(head), "the first tick raised no live head")
        standing = self._standing_report()
        active = self.state.load_active_report()
        record = self.state.load_head_run()

        self.assertEqual(self.refused_tick(self._registry(runtime=ORCA_LEGACY_RUNTIME)), dispatch.REFUSED_EXIT)

        self.assertEqual(len(self.board.cards), 1, "the failed-closed tick created a report card of its own")
        self.assertEqual(self.board.in_progress(), [standing], "the live head's report was closed")
        self.assertEqual(self.board.moves, [])
        self.assertEqual(self.state.load_active_report(), active)
        self.assertEqual(self.state.load_head_run(), record)
        self.assertTrue(_alive(head), "the failed-closed tick stopped the live head")
        self.assertEqual(self.actions()[-1], dispatch.NO_SUPERVISED_HEAD)
        self.assertNotIn("owner-report-release", self.actions())

    def test_a_failed_closed_tick_leaves_even_an_ownerless_report_alone(self) -> None:
        """A refused tick holds still: a report whose writer is not recorded anywhere is the next
        ordinary tick's to close, not this one's."""
        self.state.save_active_report("ummanu-report-9", "run-9")

        self.assertEqual(self.refused_tick(self._registry(runtime=ORCA_LEGACY_RUNTIME)), dispatch.REFUSED_EXIT)

        self.assertEqual(self.state.load_active_report()["reference"], "ummanu-report-9")
        self.assertEqual(self.actions(), [dispatch.NO_SUPERVISED_HEAD])

    def test_a_working_supervised_head_keeps_the_report_it_is_writing(self) -> None:
        """The busy-skip dispatches nothing and stops nothing: the head that is up is the one
        writing the standing card, so that card is untouched and the card this tick made is the one
        that is closed."""
        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)
        standing = self._standing_report()

        self.assertEqual(self.run_tick(self._registry(runtime=LOCAL_PTY_RUNTIME)), 0)

        self.assertEqual(len(self.board.cards), 2, "the busy tick filed no card of its own")
        self.assertEqual(
            self.board.in_progress(), [standing], "the busy head's own report was closed under it"
        )
        self.assertIn((self.AGENT, self.board.cards[-1]["reference"], "Done"), self.board.moves)
        self.assertEqual(
            (self.state.load_active_report() or {}).get("reference"),
            standing,
            "the record stopped naming the card the live head is writing",
        )
        self.assertEqual(self.actions()[-1], "supervised-busy-skip")


if __name__ == "__main__":
    unittest.main()
