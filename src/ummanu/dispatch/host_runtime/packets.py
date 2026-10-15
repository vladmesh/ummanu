"""Production host packets implementation."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from stat import S_IMODE, S_ISDIR
from typing import Any

from ummanu._fsutil import write_text_atomic
from ummanu.board.completion_evidence import (
    RESEARCH_REPORT_DIR,
    RESEARCH_REPORT_FILE,
    has_candidate,
    no_candidate_report_contract,
    research_report_path,
)
from ummanu.dispatch.cleanup import CleanupJournal
from ummanu.dispatch.gate_receipt import (
    accepted_receipt as _accepted_gate_receipt,
    is_exact_sha as _is_exact_sha,
    render_receipt,
)
from ummanu.dispatch.helpers import (
    _decision_record_line,
    _last_gate_red_body,
    _legacy_worker_branch,
    _protocol_prerequisites_record_line,
    _round_record_line,
    _round_report_ids,
    _round_worker_check,
    safe_one_line as _safe_one_line,
)
from ummanu.dispatch.launch import REVIEW_ROLE, WORKER_ROLE
from ummanu.dispatch.observer import OBSERVER_PROMPT_FILE
from ummanu.dispatch.review_packet import (
    ReviewEvidence,
    data_block,
    render_review_evidence,
    resolve_review_evidence,
    style_checklist,
)
from ummanu.dispatch.state import (
    REVIEW_REJECTION_REASON,
    DispatcherRecord,
    attempt_request_id as _attempt_request_id,
    request_token as _request_token,
)
from ummanu.dispatch.types import DispatcherError, HostError
from ummanu.dispatch.worker_comments import (
    WORKER_COMMENT_ROLES,
    WorkerComment,
    select_worker_comments,
    worker_comments_record_line,
    worker_comments_section,
)
from ummanu.projects.contract import UNDECIDABLE_RELATIVE_INTERPRETER
from ummanu.runtime.head import PYTHON_SAFE_PATH_FLAG as _PYTHON_SAFE_PATH_FLAG
from ummanu.runtime.launch_prefix import pythonpath_prefix
from ummanu.runtime.prompt_document import (
    PromptDocumentError,
    nudge_for as _nudge_for,
    worker_style,
    write_prompt_document as _write_prompt_document,
)
from ummanu.runtime.role_env import WORKSPACE_ENV_DIR

_PYTHONPATH_PREFIX = pythonpath_prefix()

def _interrupted_command_section(record: DispatcherRecord | None) -> list[str]:
    """The one factual line a respawned head gets about the command its predecessor was running.

    Only ephemeral respawn context; never persist it.
    """
    line = _safe_one_line(getattr(record, "respawn_interrupted_command", "") or "", limit=600)
    if not line:
        return []
    return ["## Interrupted command", "", line, ""]

def _gate_attestation_for_prompt(
    record: DispatcherRecord | None,
    current_sha: str = "",
    candidate: dict[str, object] | None = None,
) -> dict[str, object]:
    """Return a complete persisted receipt, never a guessed substitute.

    A record carrying only the old boolean ``gate_state`` is unavailable evidence rather than an
    invented SHA: the exact binding is the safety property.
    """
    if isinstance(candidate, dict):
        return _accepted_gate_receipt(candidate, current_sha)
    source = getattr(record, "gate_attestation", {})
    receipt = getattr(source, "receipt", None)
    if receipt is not None:
        return receipt.as_dict() if receipt.validated_sha == current_sha else {}
    return _accepted_gate_receipt(source, current_sha)

def _body_file_path(data_dir: Path, kind: str, reference: str, review_round: int) -> str:
    """Where a head writes its report/verdict body: a directory private to this user, never the
    shared `/tmp`, where another account could plant the predictable name first."""
    override = os.environ.get("UMMANU_DISPATCHER_BODY_DIR", "").rstrip("/")
    root = override or str(Path(data_dir) / "artifacts" / "bodies")
    return f"{root}/ummanu-{kind}-{_request_token(reference)}-{_request_token(str(review_round))}.md"

def _private_body_dir(directory: Path) -> None:
    """Create the body directory 0700, or take an existing one only when this user owns it.

    The head writes the body file itself (its editing tool refuses to overwrite a file it has not
    read, which is why the dispatcher clears it before launch), so the privacy lives on the
    directory: nobody else can create, replace or read a name inside it.
    """
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if not S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise HostError(f"report body directory {directory} is not a directory this user owns")
        if S_IMODE(info.st_mode) & 0o077:
            os.chmod(directory, 0o700)
    except OSError as exc:
        raise HostError(f"report body directory {directory} could not be made private: {exc}") from None

def _no_candidate_review_subject(task: dict[str, Any]) -> list[str]:
    """What a reviewer of a research/infra card reviews, in place of a branch and its commits."""
    if str(task.get("type") or "") == "research":
        return [
            "This research card has no candidate: there is no branch, diff or pull request to review.",
            f"Review the report directory: `{RESEARCH_REPORT_DIR}/` in the worker's workspace (the",
            f"report is `{RESEARCH_REPORT_DIR}/{RESEARCH_REPORT_FILE}`, with its artifacts beside it). Once",
            f"transferred it lives in the instance repository at `{research_report_path(str(task['ref']))}`.",
            "",
        ]
    return [
        "This infra card has no candidate: there is no branch, diff or pull request to review.",
        "Review the worker's `report:done` body on the card: its `## What was done` section and its",
        "`## How to verify` section, a command or observation you can repeat.",
        "",
    ]

def _body_file_instructions(body_file: str) -> list[str]:
    """Spell out the delivery path: file first with a normal editing tool, then one plain command.
    The codex runtime refuses an inline mktemp/rm assembly.
    """
    return [
        f"Write the body to {body_file} with your file-writing tool,",
        "then run the command below verbatim. Do not assemble the body inside the shell command",
        "(no heredoc, no mktemp, no echo pipeline) and do not add `rm`: the codex runtime refuses",
        "rm-style commands, and quotes or backticks in the body break the call. Leave the file in",
        "place afterwards; the dispatcher does not read it.",
    ]


class PromptPacketMixin:

    def _write_prompt(self, path: Path, body: str) -> None:
        write_text_atomic(path, body)
        if self.mode == "real" and path.name in {"TASK.md", OBSERVER_PROMPT_FILE}:
            from ummanu.dispatch.cleanup import CleanupJournal
            CleanupJournal(self.data_dir).generated(path, body)

    def _require_cleanup_admission(self, reference: str) -> None:
        if self.mode == "real":
            refusal = CleanupJournal(self.data_dir).admission_refusal(reference)
            if refusal:
                raise HostError(refusal)

    def _review_document(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        *,
        local_run_policy: tuple[dict[str, Any] | None, bool] | None = None,
    ) -> tuple[Path, str]:
        """This round's review task, on disk, and the one line that points a reviewer at it."""
        document = self._prompt_document_path(REVIEW_ROLE, task["ref"], record.review_baseline)
        prompt = self._review_prompt(
            task, record.attempt_id, record.review_baseline, record=record,
            local_run_policy=local_run_policy,
        )
        try:
            _write_prompt_document(document, prompt, outside=Path(record.workspace))
            nudge = _nudge_for(document)
        except PromptDocumentError as exc:
            raise HostError(f"the reviewer task document could not be prepared: {exc}") from None
        return document, nudge

    def _prompt_document_path(self, role: str, reference: str, round_number: int) -> Path:
        """Where a head's task document lives: outside every worktree, with the run's artifacts."""
        root = os.environ.get("UMMANU_DISPATCHER_PROMPT_DIR")
        base = Path(root).expanduser() if root else self.data_dir / "artifacts" / "prompts"
        name = f"{_request_token(role)}-{_request_token(str(round_number))}.md"
        return (base / _request_token(reference) / name).resolve()

    def _clear_body_file(self, kind: str, reference: str, review_round: int) -> None:
        """Drop the body file before launching the head that is supposed to write it."""
        try:
            Path(_body_file_path(self.data_dir, kind, reference, review_round)).unlink(missing_ok=True)
        except OSError:
            pass

    def _clear_report_bodies(self, reference: str) -> None:
        """Drop every report body file this card has, the round about to start included."""
        sample = _body_file_path(self.data_dir, "report", reference, 0)
        directory, name = os.path.split(sample)
        prefix = name[: -len("0.md")]
        try:
            entries = os.listdir(directory)
        except OSError:
            return
        for entry in entries:
            if not entry.startswith(prefix) or not entry.endswith(".md"):
                continue
            if not entry[len(prefix) : -len(".md")].isdigit():
                continue
            try:
                os.unlink(os.path.join(directory, entry))
            except OSError:
                pass

    def _worker_launch_prompt(self) -> str:
        """Short pointer delivered to the worker head at launch."""
        return (
            "The full task is in TASK.md at the workspace root. Read it first and follow it. "
            "Do not spawn, create, delegate to, or manage subagents; perform the work in this "
            "head only. Report done or blocked with the command given in TASK.md. Do not commit "
            "TASK.md."
        )

    def _broad_check_invocation(self, project: str) -> tuple[str, str]:
        """The exact `check broad`/`check show` commands this project's contract resolves to.

        Resolve commands through the declared project contract, including missing-suite refusals.
        """
        return self._broad_check_commands(self._packet_check_contract(project))

    def _broad_check_commands(self, contract: Any) -> tuple[str, str]:
        """Render commands from the same resolved declaration as the packet header."""
        if contract is None or not contract.module:
            return "", ""
        broad_arguments = ["check", "broad", "--reuse", "--module", contract.module]
        show_arguments = ["check", "show", "--module", contract.module]
        for argument in contract.args:
            # '=' keeps option-looking suite arguments from being parsed as wrapper options.
            broad_arguments.append(f"--module-arg={argument}")
            show_arguments.append(f"--module-arg={argument}")
        if not contract.interpreter_declared:
            candidate = str(Path(WORKSPACE_ENV_DIR) / "bin" / "python3")
            broad_arguments.extend(("--default-interpreter", candidate))
            show_arguments.extend(("--default-interpreter", candidate))
        return (
            self._control_plane_command(*broad_arguments),
            self._control_plane_command(*show_arguments),
        )

    def _packet_check_contract(self, project: str) -> Any:
        """Share the adapter verdict with all packet command renderers."""
        if not project:
            return None
        try:
            verdict = self.catalog.broad_check_verdict(project)
        except (HostError, DispatcherError):
            # Rendering a task document never fails over a registry question. A project whose
            # binding or adapter cannot be read reaches the worker with the honest wording below,
            # and the refusal itself is the preflight's to report, not this packet's.
            return None
        contract = verdict.contract if verdict.fit else None
        if verdict.undecidable and verdict.question == UNDECIDABLE_RELATIVE_INTERPRETER:
            contract = verdict.declared_contract
        return contract

    def _check_header(self, project: str) -> list[str]:
        """First lines of both packets, before any user-authored task text."""
        return self._render_check_header(self._packet_check_contract(project))

    def _review_check_header(self, project: str) -> list[str]:
        contract = self._packet_check_contract(project)
        return [
            "## Declared local checks and CI evidence boundary", "",
            "Reviewer heads must not run tests or any ummanu check, including show or --reuse (refusal125).",
            "Read the diff, commits, acceptance criteria, worker receipt artifacts and exact-SHA CI evidence.",
            "Name missing evidence in the verdict; request validation from the worker or CI.",
            f"Worker declared check: {contract.module if contract else '(configuration unavailable)'}",
            "Worker-local receipts attest content and imports; only machinery-owned gate receipts attest exact-SHA CI.",
            "",
        ]

    def _worker_check_packet(self, task: dict[str, Any], record: DispatcherRecord | None, sha: str) -> list[str]:
        from ummanu.broad_check import load_receipt
        from ummanu.dispatch.review_packet import data_block

        lines = ["## Worker completion receipt", ""]
        if record is None:
            return lines + ["Missing evidence: no worker round is bound to this review.", ""]
        from ummanu.broad_check import admitted_check

        evidence = admitted_check(_round_worker_check(
            self._card_audit(), task["ref"],
            _round_report_ids(record.workspace, record.attempt_id, task["ref"], record.report_generation),
        ))
        if evidence is None:
            return lines + ["Missing evidence: accepted worker report carries no full-profile admission receipt.", ""]
        lines += ["Immutable worker report evidence (not a mechanical gate attestation):",
                  *data_block(json.dumps(evidence, sort_keys=True, indent=2))]
        if evidence.get("candidate_sha") != sha:
            lines += ["Worker receipt covers the admitted SHA; the mechanical gate must attest the current candidate after any base refresh."]
        path = evidence.get("path")
        artifact = load_receipt(Path(path)) if isinstance(path, str) else None
        if artifact is None or artifact.get("receipt_digest") != evidence.get("receipt_digest"):
            lines += ["Worker receipt artifact is missing or changed; the immutable admitted snapshot above remains audit evidence."]
        return lines + [""]

    def _render_check_header(self, contract: Any) -> list[str]:
        from ummanu.broad_check import BroadCheckError
        from ummanu.projects.local_check import PytestSelection

        lines = ["## Declared local checks and CI evidence boundary", ""]
        if contract is None or not contract.module:
            return lines + [
                "Configuration gap: no usable adapter-declared local profile. Obtain CI evidence",
                "and report the gap; do not use repository discovery or direct pytest/unittest.",
                "",
            ]
        default = (
            []
            if contract.interpreter_declared
            else ["--default-interpreter", str(Path(WORKSPACE_ENV_DIR) / "bin" / "python3")]
        )
        broad, show = self._broad_check_commands(contract)
        lines += [
            f"Matching explicit full-profile wrapper: {broad}",
            f"Matching full receipt readback: {show}",
            f"Candidate check interpreter: {contract.interpreter if contract.interpreter_declared else default[-1]}",
        ]
        roots = ()
        gap = ""
        if contract.local is None:
            gap = "broad_check.local is missing"
        elif contract.module == "pytest":
            try:
                roots = PytestSelection.resolve(contract.args, contract.collection_roots).roots
            except BroadCheckError as exc:
                gap = exc.message
        if gap:
            lines += [
                f"Configuration gap: {gap}; bare check and module/node selector forms are unavailable.",
                "Use the declared legacy broad/show commands above; do not guess collection roots",
                "or use repository discovery or direct pytest/unittest.",
            ]
        else:
            lines += [
                f"Full declared profile (reuse): {self._control_plane_command('check', '--reuse', *default)}"
            ]
            selectors = (
                ("<declared-collection-path>", "<declared-collection-path>::<test-node>")
                if roots
                else (
                    ("<runner-owned-module-selector>", "<runner-owned-node-selector>")
                    if contract.local.get("membership") == "runner"
                    else (
                        "<declared-unit-or-component-module>",
                        "<declared-unit-or-component-file.py>::<class>::<test>",
                    )
                )
            )
            lines += [
                f"Subset form (placeholder, replace with an allowed selector): {self._control_plane_command('check', *default, '--', selector)}"
                for selector in selectors
            ]
            if roots:
                lines += [
                    f"Declared pytest collection roots: {list(roots)!r}; wrapper preserves declared options/markers."
                ]
            elif contract.local.get("membership") == "runner":
                lines += [
                    "Runner owns selector forwarding through -- and its declared selector_args; do not bypass it."
                ]
            else:
                lines += ["Manifest membership is unit + component; other shards are CI-only."]
        return lines + [
            "Every AC needs declared local-profile evidence or its corresponding CI shard and report.",
            "Respect native runner budgets, including codegen's 0.5s/test and 240s CPU limits.",
            "Ummanu local tests warn at >5s; CI unit/component fails at >5s/test or >90s/module.",
            "Real integration/packaging/runtime/backend/network/container evidence is CI-only here.",
            "Never run direct pytest/unittest or expand the local profile to satisfy an AC.",
            "Fresh code report:done requires an intact complete green full declared receipt for committed HEAD tree.",
            "Subsets are permitted during work; reuse the full receipt for an unchanged validated tree.",
            "A valid executed dispatcher-owned exact-SHA gate suppresses routine broad reruns;",
            "none/noop/missing receipts attest no suite. Subsets and refusal125 do not alter full receipts.",
            "",
        ]

    def _control_plane_command(self, *arguments: str) -> str:
        """Render one head-visible Ummanu command on the production control-plane boundary."""
        interpreter = shlex.quote(str(self.production_runtime.interpreter))
        suffix = "".join(f" {shlex.quote(argument)}" for argument in arguments)
        return f"{_PYTHONPATH_PREFIX} {interpreter} {_PYTHON_SAFE_PATH_FLAG} -m ummanu{suffix}"

    def _local_run_policy(self, task: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
        """The same creation-only authority read for packets and ordinary head launches."""
        from ummanu.board.local_run import parse_local_run_exceptions, parse_local_run_policy

        reference, project = task.get("sprint"), task.get("project")
        if not reference:
            return None, False
        try:
            policy = {"card": task.get("ref"), "sprint": reference, "project": project, "exceptions": []}
            parse_local_run_policy(policy)
            if self.sprint_reader is None:
                raise ValueError("no sprint reader")
            sprint = self.sprint_reader.show(reference, include_cards=False)
            if not isinstance(sprint, dict) or sprint.get("ref") != reference:
                raise ValueError("sprint identity mismatch")
            projects = sprint.get("reservations", [])
            if not isinstance(projects, list) or any(
                not isinstance(item, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", item) for item in projects
            ):
                raise ValueError("malformed sprint scope")
            if project not in projects:
                raise ValueError("card project is not reserved")
            policy["exceptions"] = [
                entry.to_document()
                for entry in parse_local_run_exceptions(
                    sprint.get("local_run_exceptions", []), projects=projects
                )
                if entry.project == project
            ]
            parse_local_run_policy(policy)
            return policy, False
        except Exception:  # noqa: BLE001 - any failed authority read grants no exceptions
            return None, True

    def local_run_snapshot_for_round(
        self,
        task: dict[str, Any],
        role: str,
        round_number: int,
        retained: dict[str, Any],
    ) -> dict[str, Any]:
        """Capture once for a fresh round; keep the same result on retries and recovery."""
        identity = {
            "card": task.get("ref"),
            "sprint": task.get("sprint") or "",
            "project": task.get("project"),
            "role": role,
            "round": round_number,
        }
        if retained.get("role") == role and retained.get("round") == round_number:
            # A corrupt or changed identity in the current round must not trigger a new read that
            # could grant authority to a guard already bound with a different result.
            return dict(retained)
        policy, unavailable = self._local_run_policy(task)
        return {**identity, "policy": policy, "unavailable": unavailable}

    def _frozen_local_run_policy(
        self,
        task: dict[str, Any],
        role: str,
        round_number: int,
        snapshot: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, bool]:
        from ummanu.board.local_run import parse_local_run_policy

        identity = {
            "card": task.get("ref"),
            "sprint": task.get("sprint") or "",
            "project": task.get("project"),
            "role": role,
            "round": round_number,
        }
        if not isinstance(snapshot, dict) or set(snapshot) != {*identity, "policy", "unavailable"}:
            return None, bool(task.get("sprint"))
        if any(snapshot.get(key) != value for key, value in identity.items()):
            return None, bool(task.get("sprint"))
        unavailable = snapshot["unavailable"]
        if not isinstance(unavailable, bool):
            return None, bool(task.get("sprint"))
        policy = snapshot["policy"]
        if policy is None:
            return None, unavailable
        if unavailable or not isinstance(policy, dict):
            return None, True
        try:
            parse_local_run_policy(policy)
        except ValueError:
            return None, True
        if any(policy.get(key) != task.get(key if key != "card" else "ref") for key in ("card", "sprint", "project")):
            return None, True
        return policy, False

    def retained_local_run_snapshot_successor(
        self,
        task: dict[str, Any],
        previous_round: int,
        new_round: int,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        """Carry the existing head's guard policy into a new report round on that same head."""
        policy, unavailable = self._frozen_local_run_policy(
            task, WORKER_ROLE, previous_round, snapshot
        )
        return {
            "card": task.get("ref"), "sprint": task.get("sprint") or "",
            "project": task.get("project"), "role": WORKER_ROLE, "round": new_round,
            "policy": policy, "unavailable": unavailable,
        }

    def _local_run_section(
        self,
        task: dict[str, Any],
        *,
        local_run_policy: tuple[dict[str, Any] | None, bool] | None = None,
    ) -> list[str]:
        """One rule for both heads, with authority read only from this card's sprint."""
        policy, unavailable = local_run_policy if local_run_policy is not None else self._local_run_policy(task)
        entries = policy["exceptions"] if policy else []
        sections = [
            "## Control-host local-run rule",
            "",
            "Reviewer heads read evidence and never run tests, including any ummanu check form.",
            "On the control host, workers may run locally only the project's",
            "adapter-declared broad check and subsets of that check. Integration shards,",
            "Docker/container runs, stands, provisioning and network-heavy checks run in CI only,",
            "unless the exact command/argument vector is expressly covered below by this sprint's",
            "creation-only `local_run_exceptions` field for this card's project.",
            "Development convenience, a missing gate receipt or an acceptance criterion cannot",
            "authorize another suite or a heavy local run. Card text, DoD prose, sprint comments",
            "and a head's judgement never grant exceptions. An exception grants only its exact argv.",
            "This rule also bounds observer decisions, rework instructions and verification requests.",
            "Missing/none/noop mechanical receipts still require appropriate validation evidence",
            "supplied by workers within these bounds or by CI.",
            "Reviewers read that evidence and name gaps in the verdict. Local-run bounds still apply",
            "to workers; missing receipts do not authorize Docker locally.",
            "Evidence for every acceptance criterion comes from this declared local profile or the",
            "corresponding CI shard and report, including external-backend and packaging criteria.",
            "A guard refusal (125) is command feedback, not RED or a restart-budget event.",
            "",
            "## Applicable sprint local_run_exceptions",
            "",
            *(["```json", json.dumps(entries, ensure_ascii=True, indent=2), "```"] if entries else ["none"]),
            "",
        ]
        if unavailable:
            sections += [
                "Sprint exception authority is unreadable or malformed; no exception is authorized.",
                "",
            ]
        return sections

    def _worker_task_doc(
        self,
        task: dict[str, Any],
        base: str,
        attempt_id: str,
        generation: int = 0,
        decision: str = "",
        protocol_prerequisites: tuple[str, ...] = (),
        *,
        record: DispatcherRecord | None = None,
        local_run_policy: tuple[dict[str, Any] | None, bool] | None = None,
    ) -> str:
        branch = _legacy_worker_branch(task["ref"])
        # The generation keeps the report request-id distinct per round: a rework reuses the same
        # attempt_id, so without it the second done-report is deduped and the dispatcher waits on.
        request = _attempt_request_id(attempt_id, "worker-report-done", task["ref"], str(generation))
        # One id per classification: a worker restating a block under the other classification is
        # filing a different report, and a shared id would answer the second call with `validation`.
        blocked_requests = {
            classification: _attempt_request_id(
                attempt_id, f"worker-report-blocked-{classification}", task["ref"], str(generation)
            )
            for classification in ("external_fact", "wrong_task_definition")
        }
        body_file = _body_file_path(self.data_dir, "report", task["ref"], generation)
        _private_body_dir(Path(body_file).parent)
        report_commands = {
            "done": self._control_plane_command(
                "task",
                "report",
                "--ref",
                task["ref"],
                "--role",
                "worker",
                "--kind",
                "done",
                "--request-id",
                request,
                "--body-file",
                body_file,
            ),
            **{
                classification: self._control_plane_command(
                    "task",
                    "report",
                    "--ref",
                    task["ref"],
                    "--role",
                    "worker",
                    "--kind",
                    "blocked",
                    "--classification",
                    classification,
                    "--request-id",
                    blocked_requests[classification],
                    "--body-file",
                    body_file,
                )
                for classification in ("external_fact", "wrong_task_definition")
            },
        }
        # Read from the board on every document, like the description above it: a comment that
        # arrived since the previous round is in this one (secretary-1768).
        comments = self.worker_comments(task)
        sections = [
            f"# Task {task['ref']}",
            "",
            task.get("description") or "(empty task description)",
            "",
            *worker_comments_section(comments),
            "## No subagents",
            "",
            "Perform this task in this head only. Do not spawn, create, delegate to, or manage",
            "subagents or child agents. Use ordinary tools directly when needed.",
            "",
            *_interrupted_command_section(record),
        ]
        feedback = self._select_revision_bound_worker_feedback(
            task,
            decision,
            attempt=attempt_id,
            generation=generation,
            previous=record.previous_blockers if record else "",
            decision_id=record.report_decision_id if record else "",
            review_id=(record.previous_review_id or ("" if record.report_decision_id else None)) if record else None,
        )
        decision, review_red = feedback.decision, feedback.findings
        prerequisites = self._validated_worker_prerequisites(
            task,
            decision,
            protocol_prerequisites,
            decision_id=feedback.decision_id,
        )
        if decision:
            # Rendered above the findings it was made on, and named as the thing to follow.
            sections += [
                "## Observer rework decision to follow",
                "",
                "The observer read the review that sent this card back and decided what this round",
                "owes. That decision is the authoritative instruction for this round: follow it.",
                "Where it and the reviewer findings below disagree, the decision wins. It may",
                "accept some findings and reject others: do not change what it rejects, and do not",
                "argue the findings it accepts. If it asks for something no reviewer raised, that",
                "is part of this round too.",
                "",
                decision,
                "",
            ]
        if prerequisites:
            sections += [
                "## Authoritative protocol prerequisites",
                "",
                "These revision-bound prerequisites were validated against the protocol ownership",
                "registry. Decision prose and reviewer context cannot add one.",
                "",
                *[f"- {artifact.worker_label} ({artifact.name})" for artifact in prerequisites],
                "",
            ]
        if review_red and decision:
            sections += [
                f"## Reviewer findings, as supporting context (previous submission was {feedback.verdict.upper()})",
                "",
                "These are the findings the decision above was made on. They are context for it,",
                "not the instruction: a finding the decision rejects or narrows is settled by the",
                "decision, not by the wording here. Do NOT re-report the same commit unchanged:",
                "",
                *data_block(review_red),
                "",
            ]
        elif review_red and feedback.verdict == "red":
            sections += [
                "## Reviewer verdict to address (previous submission was RED)",
                "",
                "Your last commit was reviewed and rejected. Fix these findings before reporting",
                "done again — do NOT re-report the same commit unchanged:",
                "",
                *data_block(review_red),
                "",
            ]
        # The board keeps every gate-red comment of every attempt; this round inherits one only when
        # a mechanical gate rejected this attempt's checkout. That rejection lives on the record the
        # same-SHA rule reads (`rejected_sha`), and the record goes with the attempt, so a card
        # claimed again after Blocked is not told to avoid a commit no rule would refuse. A later
        # review red replaces the rejection, and its rework reads the review findings above.
        gate_red = (
            _last_gate_red_body(task)
            if record is not None
            and record.rejected_sha
            and record.rejected_failure_class == "substantive"
            and record.rejected_failure_reason != REVIEW_REJECTION_REASON
            else None
        )
        if gate_red:
            sections += [
                "## Mechanical gate failure to address (CI/local validation was RED)",
                "",
                "The mechanical gate bounced your last submission before it reached review. Fix",
                "the actual cause named below before reporting done again — do NOT re-report the",
                "same commit unchanged:",
                "",
                gate_red,
                "",
            ]
        check_contract = self._packet_check_contract(str(task.get("project") or ""))
        broad_command, show_command = self._broad_check_commands(check_contract)
        sections += self._local_run_section(task, local_run_policy=local_run_policy)
        sections += ["## Code style", "", worker_style(), ""]
        sections += [
            "## Prior blocker dispositions in the worker report",
            "",
            "List every applicable prior stable ID on its own line using these report forms:",
            "BLOCKER-<id>: fixed; commit: <repair SHA>",
            "BLOCKER-<id>: observer-rejected; observer quote: <exact applicable decision quotation>",
            "BLOCKER-<id>: deferred; issue: issue:<ref>; observer quote: <exact applicable decision quotation>",
            "These are placeholders. Quote the observer verbatim on a single line, include its",
            "source event and the same blocker ID (and issue ref for deferral) in the quotation.",
            "Never derive rejection from reviewer prose. Fixed is a reported",
            "claim requiring independent review. Missing, conflicting or unbound evidence stays",
            "unknown/unresolved; preserve all IDs and explain that gap. Give exact candidate SHA,",
            "actual role environment/hook, artifact paths, command/status/count/import provenance,",
            "full receipt hashes and relevant CI links; distinguish fixtures, projections and delivered packets.",
            "",
            *(render_review_evidence(feedback) if feedback.findings or feedback.historical or feedback.decision else []),
        ]
        if broad_command:
            broad_invocation = [f"    {broad_command}", ""]
            show_invocation = f"`{show_command}` and quote its summary"
        else:
            broad_invocation = [
                "Configuration gap: this project's adapter supplies no usable declared local profile/module.",
                "No exact local broad command can be named. Do not select a module yourself, invent",
                "a placeholder invocation or use repository-wide discovery. Report the configuration",
                "gap and the validation evidence available through CI or the declared exceptions.",
                "Do not claim a broad run or receipt for a suite that cannot be named.",
                "",
            ]
            show_invocation = "When the adapter declares a suite, use its matching `check show` command and quote its summary"
        sections += [
            "## Check-cost contract",
            "",
            "Before fresh code report:done, obtain a complete green full declared profile receipt",
            "for the committed HEAD tree. Missing, RED, incomplete or mismatched receipts refuse",
            "admission without running tests. Subsets never satisfy completion. Reuse unchanged content.",
            "",
            "During development, run the smallest relevant checks first. Run at most one local broad",
            "suite for this report generation and unchanged content when it is actually useful; name any",
            "additional broad rerun and its reason in the report. A later executed local/GitHub gate is",
            "reusable downstream only if it produces a valid dispatcher-owned exact-SHA gate receipt. A",
            "none/noop gate or a missing dispatcher-owned exact-SHA gate receipt attests no broad suite;",
            "do not call it authoritative, and run the appropriate validation before reporting when this",
            "card's acceptance criteria require it.",
            "",
            "Run that broad suite through the receipt wrapper, so its worker-local broad receipt outlives",
            "the pane:",
            "",
            *broad_invocation,
            "`--reuse` is the default way to invoke it: with a usable worker-local broad receipt it prints",
            "that worker-local broad receipt",
            "and returns the result the run had, and otherwise it runs the suite. So the answer to a",
            "pane that scrolled away is this same command, not a rerun; asking for a rerun over content the",
            "worker-local broad receipt already covers is prohibited. Drop `--reuse` only to force a fresh run you can",
            "name a reason for.",
            "",
            "It streams the combined output while the suite runs and returns the check's own exit",
            "status, and it writes `.ummanu-task-env/checks/broad-<digest>.json` in this workspace: command and",
            "digest, cwd and imported project, start/end/duration, exit code, parsed verdict and",
            "counts where the runner prints them, and a bounded diagnostic tail. Read it back with",
            show_invocation,
            "in the report. While that worker-local broad receipt is usable, you already have the answer. An edit to the",
            "content, or a concrete red result you are fixing, opens a new justified run — name which",
            "one in the report. Committing content a receipt already covers is not one of them: the",
            "identity is the tree, so a commit that changes no byte reuses the worker-local broad receipt.",
            "The worker-local broad receipt is workspace-local and ignored by git; never commit it.",
            "It is never presented as a dispatcher-owned exact-SHA gate receipt.",
            "",
            "A worker-local broad receipt stands in for a run only while it describes this content and the check",
            "process imported the project from this workspace; an import resolved elsewhere is",
            "recorded truthfully and still refused. `check show` and `--reuse` answer that with",
            "one predicate, so they cannot disagree.",
            "",
            "A check that needs a shell runs as `--command '<shell>'` instead, and buys that",
            "generality by attesting less: a shell may change directory or import environment",
            "before any interpreter starts, so its receipt records no import provenance and is",
            "never reused in place of a run. Prefer `--module` for the suite you report on.",
            "A shell receipt does not authorize a command outside the local-run rule.",
            "",
        ]
        sections += [
            "## Scope of a rework",
            "",
            "Address a reviewer finding when its repair is local to this card. Use `report:blocked`",
            "instead only for an obvious wrong cut: the requested fix contradicts this card, crosses",
            "its explicit Out of scope, or requires a new durable protocol, product contract, or trust",
            "boundary. Difficulty or size alone is not a reason to stop. In a blocked report, name the",
            "conflict and the observer decision needed. Do not silently expand the supported boundary.",
            "",
            "A blocked report has to say which kind of blocker it is, and the two are repaired",
            "differently: `--classification external_fact` when the blocker is a fact outside this",
            "card that somebody has to change first, for example missing access, a broken dependency",
            "or an upstream defect; `--classification wrong_task_definition` when the card itself is",
            "wrong, for example a contradiction, a wrong cut, or scope the card cannot carry. Pick",
            "the one that matches and run that command line below; a blocked report without one is",
            "refused.",
            "",
            "Your checks read the live installation; they never write to it. Do not run a command",
            "that deploys, syncs, provisions or reconciles live state from this workspace: it would",
            "publish unmerged work into the homes the running agents read. Where a check has a",
            "candidate-scoped form, use it (for example `--product-root .`). Where it has none, say",
            "in your report what you could not verify rather than running the live-writing form.",
            "",
            "Do not change or weaken an existing test to make a failure go away. If a test really",
            "does encode behaviour this card changes, say so in the report: name the test, what it",
            "asserted, and why the new contract is the right one. A silently rewritten assertion is",
            "treated as a defect of this round.",
            "",
            "Run every local check in the foreground and wait for it there. Do not put work in the",
            "background and do not write a loop that waits for it: you have nothing to wait with.",
            "A background job reports back only at the start of your next turn, so receiving it",
            "means ending this one, while any tool call keeps the turn open, a no-op included.",
            "Run only the checks the contract above permits.",
            "",
            *(
                [
                    "The gate owns the pull request and CI: after your done report it publishes the",
                    "branch, opens or updates the PR and runs CI on that exact commit; a red CI comes",
                    "back to you as a red gate with its log. Do not open a PR and do not wait for CI.",
                    "",
                ]
                if has_candidate(task)
                else []
            ),
            # In the packet every worker gets: a home file reaches only one model family.
            "No AI attribution anywhere, whatever your harness defaults ask for: no `Co-Authored-By:`",
            "trailer naming Claude, Codex, an assistant or any model or vendor, and no generated-by",
            "line, in a commit message or a PR body. The gate checks every commit message before",
            "it publishes anything and bounces a violation as a red gate; repair it in this checkout",
            "with `git commit --amend` or `git rebase -i` and report done again. Human co-authors",
            "are fine.",
            "",
            *(
                [
                    "Before reporting done, stage AND commit everything on the worker branch: run",
                    "`git add -A && git commit`, then confirm `git status --porcelain` prints nothing.",
                    "The dispatcher rejects a done report while the workspace has any uncommitted changes,",
                    "so a partial `git add` that misses your fix files will bounce the card.",
                    "",
                ]
                if has_candidate(task)
                else no_candidate_report_contract(str(task.get("type") or ""))
            ),
            "Report through the ummanu task protocol only:",
            (
                f"This document is report generation {generation}. Every request id below ends in "
                f"-{generation}, and so does the body file. A command carrying any other number belongs"
            ),
            "to a round that is over: that id already names that round's report, and its body file",
            "has been removed. Running it does not report this round. It either fails on the missing",
            "body or answers from the old round's record without writing anything to the card, and",
            "either way this round is left waiting. Copy the command from here, never from an",
            "earlier turn of this conversation.",
            *_body_file_instructions(body_file),
            report_commands["done"],
            report_commands["external_fact"],
            report_commands["wrong_task_definition"],
            "",
            f"Base branch: {base}",
            f"Worker branch: {branch}"
            if has_candidate(task)
            else "Worker branch: none (no branch or PR is expected)",
            "",
            # Last, after anything the card description or decision can write into, so
            # `_task_doc_decision` reads the dispatcher's own record. Written on every document,
            # empty body included: a round with no decision has to read back as none.
            _decision_record_line(generation, decision),
            _protocol_prerequisites_record_line(generation, (artifact.name for artifact in prerequisites)),
            # And the round's own ids, on the same terms: the report commands above are prose in a
            # document that also renders the card description, so they cannot be the authority.
            _round_record_line(generation, [request, *blocked_requests.values()]),
            # Which comments this document carries, so the mid-round arm points the live worker
            # only at the ones it has not been handed.
            worker_comments_record_line(comments),
            "",
        ]
        return "\n".join([*self._render_check_header(check_contract), *sections])

    def worker_comments(self, task: dict[str, Any]) -> tuple[WorkerComment, ...]:
        """The PO, owner and observer comments this card's worker is handed, oldest first."""
        if not any(
            isinstance(comment, dict) and comment.get("marker") in WORKER_COMMENT_ROLES
            for comment in task.get("comments") or []
        ):
            return ()
        return select_worker_comments(task, self._card_audit().events(str(task.get("ref") or "")))

    def _card_audit(self) -> Any:
        """The card audit this host was handed, or a refusal that names the missing wiring."""
        if self.audit is None:
            raise HostError("this dispatcher host was built without the card audit its TASK.md reads")
        return self.audit

    def _select_revision_bound_worker_feedback(
        self, task: dict[str, Any], decision: str, *, attempt: str,
        generation: int, previous: str = "",
        decision_id: str = "", review_id: str | None = None,
    ) -> ReviewEvidence:
        """One round/spec resolution for both worker instructions and reviewer evidence."""
        return resolve_review_evidence(
            task, self._card_audit().events(task["ref"]), attempt=attempt,
            generation=generation, decision=decision, previous=previous,
            decision_id=decision_id, review_id=review_id,
        )

    def _review_prompt(
        self,
        task: dict[str, Any],
        attempt_id: str,
        review_round: int,
        *,
        record: DispatcherRecord | None = None,
        local_run_policy: tuple[dict[str, Any] | None, bool] | None = None,
    ) -> str:
        # The round belongs in the key like it does in the worker report id: a card that goes red
        # twice in one attempt reuses attempt_id, and a round-less id replays the first verdict.
        green_request = _attempt_request_id(attempt_id, "review-green", task["ref"], str(review_round))
        red_request = _attempt_request_id(attempt_id, "review-red", task["ref"], str(review_round))
        body_file = _body_file_path(self.data_dir, "verdict", task["ref"], review_round)
        _private_body_dir(Path(body_file).parent)
        verdict_commands = {
            kind: self._control_plane_command(
                "task",
                "verdict",
                "--ref",
                task["ref"],
                "--role",
                "reviewer",
                "--kind",
                kind,
                "--request-id",
                request,
                "--body-file",
                body_file,
            )
            for kind, request in (("green", green_request), ("red", red_request))
        }
        current_sha = self.head_commit(record) if record else ""
        attestation = _gate_attestation_for_prompt(record, current_sha)
        sections = [
            f"# Review {task['ref']}",
            "",
            task.get("description") or "(empty task description)",
            "",
            *self._local_run_section(task, local_run_policy=local_run_policy),
            "An observed excessive local heavy run is a non-blocking observation, never grounds",
            "for RED, even if its tests passed. Exclude its results from validation evidence; CI",
            "or an allowed local check supplies that evidence. Judge the code and valid evidence.",
            "The observer does not order rework or charge the budget for such a run alone.",
            "Preserve historical verdicts in the audit; do not reopen them under this rule.",
            "Workers or CI perform required validation within these bounds. Reviewers read the",
            "evidence and report missing required valid evidence or code defects in the verdict.",
            "Apply the same local-run bounds to worker checks. Reviewers request evidence from workers or CI.",
            "Missing required valid evidence or a code defect can still block release.",
            "",
            "## No subagents",
            "",
            "Perform this review in this head only. Do not spawn, create, delegate to, or manage",
            "subagents or child agents. Use ordinary tools directly when needed.",
            "",
            *_interrupted_command_section(record),
            "A red verdict must list every blocker you have found in this round. Prefix each with a",
            "stable `BLOCKER-<short-slug>` id so a re-review can close it without rediscovering it.",
            "Do not hold blockers back for a later round and do not widen the scope on the next one.",
            "An observer-rejected prior blocker may appear only as a non-blocking observation.",
            "On re-review, a new blocker requires changed delta, new external behavior, or a",
            "security/data-loss finding. State the concrete violated invariant and stable ID now.",
            "A new test in a local-profile module that starts a container, accesses the network,",
            "sleeps or exceeds the native local budget blocks with this exact repair instruction:",
            "перенести в интеграционный шард. Distinguish that code defect from an earlier head's",
            "excessive local run, which remains a non-blocking observation with evidence excluded.",
            "",
            "For every RED blocker other than a style blocker S1-S4, state the concrete reachable scenario,",
            "the violated acceptance criterion or operational invariant, material assumptions, whether this",
            "branch introduced the defect or it was pre-existing, and whether the repair appears local or",
            "would change architecture, a compatibility promise, a product contract, or a trust boundary.",
            "Report evidence; do not silently widen the supported boundary or decide sprint scope.",
            "",
            *(style_checklist() if has_candidate(task) else []),
            *(
                [
                    # Deliberately duplicates the gate's own deterministic preflight: a check that only
                    # ever runs in one place has no second opinion.
                    "Read the commit messages on this branch, not only the diff. AI co-authorship is",
                    "forbidden: a `Co-Authored-By:` trailer naming a model or vendor, or a generated-by",
                    "attribution line, is a RED blocker. Ordinary human co-authors are not. Say what you",
                    "found; do not rewrite history yourself.",
                    "",
                ]
                if has_candidate(task)
                else _no_candidate_review_subject(task)
            ),
            "When a change depends on how an external backend behaves, a passing fixture is not",
            "evidence: it can encode the same wrong assumption as the code under review. Say which",
            "real behaviour you verified and how. If no end-to-end check against the real backend",
            "was possible, write plainly that it was not done and which assumption stays unverified.",
            "In the verdict give the exact candidate SHA, actual REVIEW.md and role environment/hook",
            "paths and hashes, worker wrapper commands/status/count/import provenance, full receipt",
            "hashes and CI links. Name each pending evidence item. A fixture or offline projection",
            "does not prove delivery of this candidate's packet to a head that started before it.",
            "",
            "Post exactly one review verdict through the ummanu task protocol:",
            *_body_file_instructions(body_file),
            verdict_commands["green"],
            verdict_commands["red"],
            "",
        ]
        if not has_candidate(task):
            # No candidate: no branch, diff or gate to point at. A re-review keeps only the blockers.
            if record and (record.previous_blockers or record.report_decision):
                sections[4:4] = [
                    "## Re-review packet",
                    "",
                    "Previous blockers (close or explicitly retain these stable IDs):",
                    *self._prior_review_evidence(task, record),
                    "",
                ]
            return "\n".join([*self._review_check_header(str(task.get("project") or "")), *sections])
        if attestation:
            sections[4:4] = [
                "## Mechanical gate attestation",
                "",
                render_receipt(attestation),
                "",
                "Independently inspect the diff, commits, acceptance criteria and invariants using this evidence.",
                "Do not run broad, focused, pytest, unittest or any ummanu check, including receipt readback.",
                "Request necessary additional validation from the worker or CI and name the gap in the verdict.",
                "Mandatory CI and the exact-SHA pre-merge gate remain machinery-owned and are not waived.",
                "",
            ]
        else:
            sections[4:4] = [
                "## Mechanical gate evidence",
                "",
                "No valid SHA-bound mechanical-gate receipt is available. Independently inspect the diff,",
                "acceptance criteria and invariants; mandatory CI and exact-SHA pre-merge checks remain",
                "machinery-owned. Do not claim that a broad suite was attested. This includes none/noop",
                "gates. Name missing evidence in the verdict; request validation from the worker or CI.",
                "Do not run broad, focused, pytest, unittest or any ummanu check, including show or reuse.",
                "",
            ]
        if record and record.preferred_head:
            sections[4:4] = [
                "## Head failover",
                "",
                (
                    f"This branch was written by `{_safe_one_line(record.head)}`, not by the head this "
                    f"card asks for (`{_safe_one_line(record.preferred_head)}`): that head's resource "
                    "was red or spent when the card was claimed, so the claim walked the registry's "
                    "fallback chain onto another family."
                ),
                "Review the work on its merits. This is here because who wrote it is a fact you are",
                "entitled to have, not an invitation to grade the head.",
                "",
            ]
        if record and (record.previous_reviewed_sha or record.report_decision or record.previous_blockers):
            sections[4:4] = [
                "## Re-review packet",
                "",
                f"previous_reviewed_sha: {_safe_one_line(record.previous_reviewed_sha) or 'unknown/unresolved'}",
                f"current_sha: {_safe_one_line(current_sha) or '(unavailable)'}",
                "Changed paths / delta from the prior review:",
                self._review_delta(record, record.previous_reviewed_sha, current_sha),
                "Previous blockers (close or explicitly retain these stable IDs):",
                *self._prior_review_evidence(task, record),
                "Review this delta, the closure of prior blockers and collateral impact; do not restart",
                "from the original base unless a concrete suspicion requires the historical diff.",
                "",
            ]
        sections[4:4] = self._worker_check_packet(task, record, current_sha)
        return "\n".join([*self._review_check_header(str(task.get("project") or "")), *sections])

    def _prior_review_evidence(self, task: dict[str, Any], record: DispatcherRecord) -> list[str]:
        evidence = self._select_revision_bound_worker_feedback(
            task, record.report_decision, attempt=record.attempt_id,
            generation=record.report_generation,
            previous=record.previous_blockers,
            decision_id=record.report_decision_id,
            review_id=record.previous_review_id or ("" if record.report_decision_id else None),
        )
        return render_review_evidence(evidence)

    def _review_delta(self, record: DispatcherRecord, previous: str, current: str) -> str:
        """A small re-review packet; failure to read it is evidence, never a broad test fallback."""
        if (
            self.mode == "noop"
            or not record.workspace
            or not _is_exact_sha(previous)
            or not _is_exact_sha(current)
        ):
            return "(delta unavailable; inspect only the necessary history)"
        try:
            paths = self.run_capture(
                ["git", "-C", record.workspace, "diff", "--name-only", f"{previous}..{current}"],
                "review delta paths",
            )
            stat = self.run_capture(
                ["git", "-C", record.workspace, "diff", "--stat", f"{previous}..{current}"],
                "review delta stat",
            )
        except (HostError, OSError, subprocess.TimeoutExpired):
            return "(delta unavailable; inspect only the necessary history)"
        if paths.returncode or stat.returncode:
            return "(delta unavailable; inspect only the necessary history)"
        names = (paths.stdout or "").strip()
        summary = (stat.stdout or "").strip()
        return (
            "\n".join(_safe_one_line(part, limit=4000) for part in (names, summary) if part)
            or "(no changed paths)"
        )
