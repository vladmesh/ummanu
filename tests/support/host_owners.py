"""Bounded dispatcher host ownership guards and deterministic packet inputs."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests import config_dependencies, source_trees
from tests.fakes.review_packet import marker_event
from ummanu.dispatch.host import CommandHostRuntime
from ummanu.dispatch.state import DispatcherRecord, attempt_request_id
from ummanu.projects.contract import ContractVerdict, ModuleContract

HOST_CLASSES = {
    "CommandHostRuntime", "ObserverHostMixin", "WorkspaceEnvironmentMixin",
    "HeadLifecycleMixin", "PromptPacketMixin",
}


def sources(root: Path) -> dict[str, str]:
    paths = [root / "src/ummanu/dispatch/host.py"]
    paths.extend(sorted((root / "src/ummanu/dispatch/host_runtime").rglob("*.py")))
    return {str(p.relative_to(root)): p.read_text() for p in paths}


def inventory(sources: dict[str, str]) -> dict[str, tuple[str, ast.AST]]:
    result = {}
    def bind(name, path, node):
        if name in result:
            raise AssertionError(f"duplicate host implementation: {name}: {result[name][0]}, {path}")
        result[name] = (path, node)
    for path, source in sources.items():
        for node in source_trees.parse(source, filename=path).body:
            if isinstance(node, ast.ClassDef) and node.name in HOST_CLASSES | {"InstanceCatalog"}:
                prefix = "InstanceCatalog" if node.name == "InstanceCatalog" else "CommandHostRuntime"
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        bind(f"{prefix}.{member.name}", path, member)
                    elif isinstance(member, ast.AnnAssign):
                        bind(f"{prefix}.{ast.unparse(member.target)}", path, member)
            elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                bind(node.name, path, node)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for name in source_trees.walk(target):
                        if isinstance(name, ast.Name):
                            bind(name.id, path, node)
    return result


def file_limits(sources: dict[str, str]) -> list[str]:
    return [f"{p}: {s.count(chr(10))} > 3000" for p, s in sources.items() if s.count("\n") > 3000]


def reverse_imports(path: str, source: str) -> set[str]:
    name = path.removeprefix("src/").removesuffix(".py").replace("/", ".")
    targets = config_dependencies.dependencies(name, Path(path), source, strict_dynamic=False)
    return {t for t in targets if t == "ummanu.dispatch.host" or t.startswith("ummanu.dispatch.host.")}


def packet_fixture(root: Path):
    """Real packet renderers over fixed board shapes; no Git, provider or backend effects."""
    task = {"ref": "ummanu-262", "project": "ummanu", "type": "code", "sprint": "sprint:1490",
            "description": "# Goal\nPreserve dispatcher host contracts.\n", "comments": []}
    digest = hashlib.sha256(task["description"].encode()).hexdigest()
    events = [{"event_id": "spec-1", "kind": "created", "payload": {"description_sha256": digest}}]
    decision = "BLOCKER-owner: preserve operative patches. BLOCKER-later: defer to issue:abc123."
    for marker, body, request, extra in (
        ("review:red", "BLOCKER-owner: patch ignored\nBLOCKER-later: future",
         attempt_request_id("fixture-attempt", "review-red", task["ref"], "2"), {}),
        ("decision:rework", decision, "decision-1", {"assessment_visit": "visit-1"}),
        ("report:done", "BLOCKER-owner: fixed; commit: " + "a" * 40 +
         "\nBLOCKER-later: deferred; issue: issue:abc123; observer quote: BLOCKER-later: defer to issue:abc123.",
         attempt_request_id("fixture-attempt", "worker-report-done", task["ref"], "3"), {}),
    ):
        if marker == "decision:rework":
            events.append({"event_id": "visit-1", "kind": "card.moved", "record_type": "board.protocol_event",
                           "transition": {"source": "validate", "target": "assessment"}, "data": {}})
        task["comments"].append({"marker": marker, "body": f"[{marker}]\n{body}"})
        events.append(marker_event(marker, body, request, ref=task["ref"], description=task["description"],
                                   revision="spec-1", occurrence=1, **extra))
    contract = ModuleContract(".ummanu-task-env/venv/bin/python3", "ummanu", module="tests.broad",
                              interpreter_declared=False,
                              local={"membership": "manifest", "runner": "unittest",
                                     "ci_manifest": "tests/ci-shards.txt", "shards": ["unit", "component"]})
    catalog = SimpleNamespace(broad_check_verdict=lambda project: ContractVerdict.as_fit(contract, project))
    sprint = {"ref": task["sprint"], "reservations": ["ummanu"], "local_run_exceptions": [
        {"project": "ummanu", "argv": ["fixture", "two words", ""], "rationale": "exact fixture only"},
    ]}
    host = CommandHostRuntime(catalog, root, mode="noop", audit=mock.Mock(events=mock.Mock(return_value=events)),
                              sprint_reader=mock.Mock(show=mock.Mock(return_value=sprint)),
                              production_runtime=SimpleNamespace(interpreter="/fixture/control/bin/python3"))
    record = DispatcherRecord(worker="worker", handle="", workspace=str(root / "workspace"), head="head",
                              review_head="reviewer", attempt_id="fixture-attempt", comment_baseline=0,
                              review_baseline=0, state="claimed", claimed_at=0, report_generation=3)
    record.report_decision = decision
    record.report_decision_id = "decision-1"
    record.previous_blockers = "BLOCKER-owner: patch ignored\nBLOCKER-later: future"
    record.previous_review_id = events[1]["event_id"]
    record.previous_reviewed_sha = "b" * 40
    record.respawn_interrupted_command = "fixture check --owner packets"
    record.local_run_snapshot = host.local_run_snapshot_for_round(task, "worker", 3, {})
    record.review_local_run_snapshot = host.local_run_snapshot_for_round(task, "reviewer", 2, {})
    return host, task, record


def render_packets(root: Path) -> dict[str, str]:
    host, task, record = packet_fixture(root)
    with (mock.patch.dict("os.environ", {"UMMANU_DISPATCHER_BODY_DIR": str(root / "bodies")}),
          mock.patch.object(host, "head_commit", return_value="a" * 40)):
        documents = {
            "worker": host._worker_task_doc(task, "main", record.attempt_id, 3, record.report_decision, record=record),
            "reviewer": host._review_prompt(task, record.attempt_id, 2, record=record),
        }
        host.sprint_reader.show.return_value["local_run_exceptions"] = []
        for kind in ("infra", "research"):
            task["type"] = kind
            documents[f"{kind}-worker"] = host._worker_task_doc(
                task, "main", record.attempt_id, 3, record.report_decision, record=record
            )
            documents[f"{kind}-reviewer"] = host._review_prompt(task, record.attempt_id, 2, record=record)
    # Only the temporary fixture root varies; command, decision, revision and hash bytes remain.
    return {name: doc.replace(str(root), "<fixture>") for name, doc in documents.items()}
