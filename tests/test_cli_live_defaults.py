from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ummanu.cli import build_parser, main
from ummanu.runtime.paths import resolve_instance_argument
from ummanu.tasks import TaskReader


class CliLiveDefaultsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = mock.patch.dict(
            os.environ, {"HOME": str(self.root), "UMMANU_INSTANCE": "", "UMMANU_DATA_DIR": ""}
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def invoke(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_inventory_every_live_instance_parser_uses_the_resolver(self):
        paths = []

        def inspect(parser, path=()):
            for action in parser._actions:
                if "--instance" in action.option_strings:
                    paths.append(path)
                    self.assertFalse(action.required, path)
                    self.assertIsNone(action.default, path)
                    self.assertTrue(parser.get_default("instance_fallback"), path)
                if isinstance(action, argparse._SubParsersAction):
                    for name, child in action.choices.items():
                        inspect(child, (*path, name))

        inspect(build_parser())
        for path in [
            ("owner-events", "list"),
            ("po", "rename"),
            ("doctor-record",),
            ("restore",),
            ("secret", "init"),
            ("bootstrap",),
        ]:
            self.assertIn(path, paths)

    def test_owner_events_and_po_rename_route_explicit_env_and_home(self):
        home = self.root / "ummanu-data" / "instance"
        home.mkdir(parents=True)
        env = self.root / "environment"
        explicit = self.root / "explicit"
        store = mock.Mock()
        store.events.return_value = []
        store.unread_count.return_value = 0
        with (
            mock.patch(
                "ummanu.board.owner_event_commands.OwnerEventStore.for_instance", return_value=store
            ) as owner,
            mock.patch("ummanu.webproto.po_ops.PoLayer") as po,
        ):
            po.return_value.po_rename.return_value = {"title": "fixture"}
            for source, target in [(None, home), ("env", env), ("explicit", explicit), ("config", explicit)]:
                os.environ["UMMANU_INSTANCE"] = str(env) if source else ""
                flags = (
                    ["--instance", str(explicit / "instance.yaml" if source == "config" else explicit)]
                    if source in {"explicit", "config"}
                    else []
                )
                self.assertEqual(self.invoke(["owner-events", "list", "--json", *flags])[0], 0)
                self.assertEqual(owner.call_args.args[0], target)
                self.assertEqual(
                    self.invoke(["po", "rename", "--session", "fixture", "--title", "fixture", *flags])[0], 0
                )
                self.assertEqual(
                    Path(po.call_args.args[0]), explicit / "instance.yaml" if source == "config" else target
                )

    def test_absent_home_refuses_before_handlers_without_creating_anything(self):
        with (
            mock.patch("ummanu.board.owner_event_commands.OwnerEventStore.for_instance") as owner,
            mock.patch("ummanu.webproto.po_ops.PoLayer") as po,
        ):
            for argv in [
                ["owner-events", "list"],
                ["po", "rename", "--title", "fixture", "--session", "fixture"],
                ["secret", "list"],
                ["restore-board"],
            ]:
                code, _, err = self.invoke(argv)
                self.assertEqual(code, 2)
                self.assertIn("no live root", err)
            owner.assert_not_called()
            po.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_doctor_path_type_and_bootstrap_modes(self):
        os.environ["UMMANU_INSTANCE"] = str(self.root)
        with mock.patch("ummanu.infra.doctor_record.record", return_value=0) as record:
            self.assertEqual(self.invoke(["doctor-record"])[0], 0)
            self.assertEqual(record.call_args.args[0], self.root)
            self.assertIsInstance(record.call_args.args[0], Path)
            self.assertEqual(
                self.invoke(["doctor-record", "--instance", str(self.root / "instance.yaml")])[0], 0
            )
            self.assertEqual(record.call_args.args[0], self.root / "instance.yaml")
        os.environ["UMMANU_INSTANCE"] = ""
        args = build_parser().parse_args(["bootstrap"])
        resolve_instance_argument(args)
        self.assertIsNone(args.instance)
        args = build_parser().parse_args(["bootstrap", "--empty"])
        with self.assertRaisesRegex(RuntimeError, "no live root"):
            resolve_instance_argument(args)
        for verb in ("install", "recover"):
            parser = next(
                a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction)
            ).choices[verb]
            destination = next(a for a in parser._actions if "--instance-dir" in a.option_strings)
            self.assertTrue(destination.required)

    def test_reason_sources_through_actual_task_handlers(self):
        body = self.root / "reason.txt"
        body.write_text("file reason\nКириллица", encoding="utf-8")
        with (
            mock.patch("ummanu.task_commands.card_client"),
            mock.patch("ummanu.task_commands.TaskWriter") as writer,
        ):
            for verb, extra, method, key in [
                ("decide", ["--kind", "release"], "decide", "body"),
                ("move", ["--to", "done"], "move", "reason"),
                ("archive", [], "archive", "reason"),
                ("handover", ["--to", "owner"], "handover", "reason"),
                ("cancel", [], "cancel", "reason"),
            ]:
                operation = getattr(writer.return_value, method)
                operation.return_value = {"ok": True}
                base = [
                    "task",
                    verb,
                    "--ref",
                    "ummanu-1",
                    "--role",
                    "po",
                    "--actor",
                    "po",
                    "--instance",
                    str(self.root),
                    "--data-dir",
                    str(self.root / "data"),
                    *extra,
                ]
                for flag, value, expected in [
                    ("--reason", str(body), str(body)),
                    ("--reason", "  literal\ntext  ", "  literal\ntext  "),
                    ("--reason-file", str(body), body.read_text()),
                ]:
                    self.assertEqual(self.invoke([*base, flag, value])[0], 0)
                    self.assertEqual(operation.call_args.kwargs[key], expected)
                if verb in {"decide", "move", "archive"}:
                    self.assertEqual(self.invoke([*base, "--body-file", str(body)])[0], 0)
                    self.assertEqual(operation.call_args.kwargs[key], body.read_text())
                    pairs = [("--reason", "--body-file"), ("--reason-file", "--body-file")]
                else:
                    pairs = []
                for first, second in [("--reason", "--reason-file"), *pairs]:
                    writer.reset_mock()
                    self.assertEqual(self.invoke([*base, first, str(body), second, str(body)])[0], 2)
                    writer.assert_not_called()
                writer.reset_mock()
                self.assertEqual(self.invoke([*base, "--reas", "text"])[0], 2)
                self.assertEqual(self.invoke([*base, "--reason-file", str(body / "missing")])[0], 2)
                writer.assert_not_called()

    def test_issue_filters_and_edit_content_validation(self):
        rows = [
            {"ref": "issue:open", "closed": False, "product": "ummanu"},
            {"ref": "issue:closed", "closed": True, "product": "ummanu"},
        ]
        with mock.patch("ummanu.product_issue_commands._store") as factory:
            store = factory.return_value
            store.list_issues.side_effect = lambda **kw: rows if kw["include_closed"] else rows[:1]
            base = ["issue", "list", "--instance", str(self.root), "--product", "ummanu"]
            for flags, expected in [([], rows[:1]), (["--closed"], rows[1:]), (["--all"], rows)]:
                code, out, _ = self.invoke([*base, *flags])
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out), expected)
                self.assertEqual(store.list_issues.call_args.kwargs["product"], "ummanu")
            factory.reset_mock()
            self.assertEqual(self.invoke([*base, "--closed", "--all"])[0], 2)
            factory.assert_not_called()
            base = [
                "issue",
                "edit",
                "--instance",
                str(self.root),
                "--ref",
                "issue:open",
                "--role",
                "po",
                "--actor",
                "po",
                "--reason",
                "correct",
            ]
            for flags in [[], ["--description", "a", "--body-file", "b"], ["--body-file", "/absent/fixture"]]:
                self.assertEqual(self.invoke([*base, *flags])[0], 2)
                factory.assert_not_called()
            store.edit_description.return_value = {"description": "  exact\n"}
            self.assertEqual(self.invoke([*base, "--description", "  exact\n"])[0], 0)
            self.assertEqual(store.edit_description.call_args.kwargs["description"], "  exact\n")

    def test_repair_requires_one_reason_source_and_pause_refuses_conflicting_sources(self):
        base = [
            "task",
            "repair-references-apply",
            "--role",
            "po",
            "--actor",
            "po",
            "--plan-id",
            "fixture",
            "--task-id",
            "1",
            "--request-id",
            "fixture",
            "--instance",
            str(self.root),
            "--data-dir",
            str(self.root / "data"),
        ]
        with (
            mock.patch("ummanu.task_commands.card_client"),
            mock.patch("ummanu.task_commands.TaskWriter"),
            mock.patch(
                "ummanu.board.reference_repair.apply_reference_repair", return_value={"ok": True}
            ) as apply,
        ):
            self.assertEqual(self.invoke(base)[0], 2)
            self.assertEqual(self.invoke([*base, "--reason", "literal"])[0], 0)
            self.assertEqual(apply.call_args.kwargs["reason"], "literal")
            apply.reset_mock()
            self.assertEqual(self.invoke([*base, "--reason", "literal", "--reason-file", "/fixture"])[0], 2)
            self.assertEqual(self.invoke([*base, "--rea", "literal"])[0], 2)
            apply.assert_not_called()
        with mock.patch("ummanu.dispatch.commands._pause_operations") as runtime:
            self.assertEqual(
                self.invoke(
                    [
                        "pause",
                        "drain",
                        "--instance",
                        str(self.root),
                        "--reason",
                        "literal",
                        "--reason-file",
                        "/fixture",
                    ]
                )[0],
                2,
            )
            runtime.assert_not_called()


class SprintListUnitTests(unittest.TestCase):
    def test_cli_sprint_scope_reads_archived_rows_with_one_metadata_batch(self):
        rows = [
            {
                "id": i,
                "reference": f"ummanu-{i}",
                "column_id": 1 if i == 1 else 2,
                "is_active": 1 if i == 1 else 0,
                "title": "card",
                "position": i,
            }
            for i in (1, 2, 3)
        ]
        client = mock.Mock()
        calls = []

        def call(method, **params):
            calls.append((method, params))
            if method == "getProjectByName":
                return {"id": 1}
            if method == "getColumns":
                return [{"id": 1, "title": "Ready"}, {"id": 2, "title": "Done"}]
            if method == "getActiveSwimlanes":
                return []
            if method == "getAllTasks":
                return [row for row in rows if row["is_active"] == params["status_id"]]
            raise AssertionError(method)

        client.call.side_effect = call
        batches = []

        def batch(requests):
            requests = list(requests)
            batches.append(requests)
            return [
                {
                    "project": "ummanu",
                    "type": "code",
                    "sprint_ref": "sprint:closed" if params["task_id"] != 3 else "sprint:other",
                }
                for _, params in requests
            ]

        client.call_batch.side_effect = batch
        with mock.patch("ummanu.task_commands.card_client", return_value=client):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["task", "list", "--instance", "/fixture", "--sprint", "sprint:closed"])
            self.assertEqual(code, 0)
            cards = json.loads(out.getvalue())
        self.assertEqual({card["ref"] for card in cards}, {"ummanu-1", "ummanu-2"})
        self.assertTrue(next(card for card in cards if card["ref"] == "ummanu-2")["closed"])
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0]), 3)
        self.assertEqual([p["status_id"] for m, p in calls if m == "getAllTasks"], [1, 0])
        reader = TaskReader(client)
        self.assertEqual([c["ref"] for c in reader.list()], ["ummanu-1"])
        self.assertEqual([c["ref"] for c in reader.list(sprint="sprint:closed")], ["ummanu-1"])
        self.assertEqual(reader.list(sprint="sprint:closed", states={"done"}), [])
        self.assertEqual(
            [
                c["ref"]
                for c in reader.list(
                    sprint="sprint:closed",
                    states={"done"},
                    project="ummanu",
                    include_archived=True,
                )
            ],
            ["ummanu-2"],
        )
        self.assertEqual(reader.list(sprint="sprint:closed", project="another", include_archived=True), [])
