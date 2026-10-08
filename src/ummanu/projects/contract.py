"""Whether a registered project's broad-check contract can attest that project.

A card's broad check is the one evidence a worker's round produces about its own code, and the
contract behind it — which suite it runs, which package the run must import and, where it matters,
which interpreter runs it — belongs to the registered project's adapter. Until secretary-1458 that contract was resolved lazily, inside
the workspace, by the worker itself: an adapter that could not name a usable one was discovered
after a workspace had been created, a head brought up and the card put in work, and the round was
already spent by the time anybody could read the refusal.

The rules are here, once, so both sides ask the same question of the same registry:

* the worker's own resolution (``ummanu check broad --module``, in ``check_commands``), and
* the dispatcher's preflight before a card is given to a worker at all.

`decide` is the single decision point, and it answers with **three** named states, never with two
and a silence:

* ``fit`` — the contract is declared usably, and here it is;
* ``refused(shape)`` — one of the enumerated refusal shapes, which are enumerated once, below,
  and neither side may add one of its own;
* ``undecidable(question)`` — the question cannot be answered without something the asker does not
  have. This is a state with a name, an evidence record and tests, not the absence of an answer.

The third one exists because leaving it unnamed is what made this card cost three rounds. The
adapter schema resolves a *relative* interpreter against the candidate workspace
(``schemas/adapter.schema.json``), and at preflight there is no candidate workspace. Round one had
the dispatcher test that interpreter in the registered checkout — an assertion about a different
directory, which approved contracts the worker then refused. Round two had the dispatcher say
nothing about it — and a silence is not a decision, so nobody could see or test what the caller
then did with it. Now the state is returned, and every caller must branch on it by name.

Which suite the check runs may be part of that contract too, since issue:8b39e60e4df361c6138e. Only
the project can say what its broad suite is; without a place to say it, the worker task packet
printed a placeholder and the documentation answered it with repository-wide discovery, which runs
every CI suite in one process. So a declared ``broad_check`` may name a ``module`` (and the exact
``args`` vector that goes with it), and when it does, the caller no longer has to name the suite.

That ``module`` is an *enrichment* of the contract, not a new requirement of it. PR #330 made a
declared block that omitted it `BROAD_CHECK_INCOMPLETE`, and that was wrong: a contract written
before a field existed is not an incomplete contract. Every adapter on the live installation
predates the field and declares ``interpreter`` and ``import_package`` only — a perfectly good
contract under the rules that applied when it was written — so the refusal took three registered
projects out of the pipeline entirely, refused at preflight, no workspace, no head, no round. The
absence of ``module`` means one thing and one thing only: the caller must name the suite itself,
which is what every worker did before the field existed. It is required exactly where it is
actually needed — ``check broad`` / ``check show`` invoked with no ``--module`` — and that path
already refuses by its own name (``no_broad_check_module``), telling the operator to pass
``--module`` or to add ``module:`` to the adapter.

What each state buys a card is settled here and not re-decided by a caller:

* ``refused`` always wins, and always before the card is issued. Its guarantee is the card's:
  no workspace, no head, no round spent.
* ``undecidable`` resolves in favour of **compatibility, not of saving the round**: the card goes
  to work, and the side that holds the tree answers the question there. A relative interpreter is
  the documented and recommended spelling; breaking that published promise to make an internal
  check convenient would move the product contract the wrong way, and absolute paths are
  machine-specific. This branch is reachable in production and is taken on every tick: the
  ``codegen-orchestrator`` and ``service-template`` adapters both declare a relative interpreter,
  and their cards are issued through ``undecidable``, not through ``fit``. (The docstring used to
  say the live cost was zero because no adapter declared ``broad_check`` at all. That stopped being
  true on 2026-08-28, and ``ummanu``'s own adapter declares one now too.)
* ``fit`` goes to work, as it always did.

Declaring ``broad_check`` is mandatory for a project that gets cards. Until this issue an adapter
that declared none fell back to a default nobody wrote down for it — this interpreter, importing
``ummanu`` — which was a true contract for the Ummanu project alone. Every other registered
project got one of two wrong answers: a checkout with no ``ummanu`` package was refused as
``cannot_attest_project``, whose wording blamed a substituted package instead of naming the real
cause, and a checkout that happened to hold one was attested against a contract its owner never
wrote. Silence is now its own named refusal, ``broad_check_not_declared``, which says the one true
thing about it. ``cannot_attest_project`` is left to mean what it says: a *declared* contract that
cannot attest its own checkout. Nothing here judges a checkout by its layout any more — what a
declared run actually imported is caught afterwards, by the receipt's own import provenance.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ummanu.config import ConfigError, load_config, validate

# Enumerated refusal shapes shared by both sides.
ADAPTER_UNAVAILABLE = "adapter_unavailable"
ADAPTER_INVALID = "adapter_invalid"
BROAD_CHECK_INCOMPLETE = "broad_check_incomplete"
# An adapter that declares no `broad_check` at all. Its own shape since
# issue:81a0a1e5c15225fa360e, so that silence is diagnosed as silence rather than through a
# contract substituted for it.
BROAD_CHECK_NOT_DECLARED = "broad_check_not_declared"
INTERPRETER_UNAVAILABLE = "interpreter_unavailable"
# A DECLARED contract that cannot attest its own checkout. It used to double as the diagnosis for
# an adapter that declared nothing, which is what made its wording mislead: it named a package the
# project never asked for. `decide` no longer reaches it — a declared contract is executed as
# declared, and what such a run imported is caught afterwards by the receipt's own provenance.
CANNOT_ATTEST_PROJECT = "cannot_attest_project"
CONTRACT_REFUSALS = (
    ADAPTER_UNAVAILABLE,
    ADAPTER_INVALID,
    BROAD_CHECK_INCOMPLETE,
    BROAD_CHECK_NOT_DECLARED,
    INTERPRETER_UNAVAILABLE,
    CANNOT_ATTEST_PROJECT,
)

# Enumerated answer states.
CONTRACT_FIT = "fit"
CONTRACT_REFUSED = "refused"
CONTRACT_UNDECIDABLE = "undecidable"
CONTRACT_STATES = (CONTRACT_FIT, CONTRACT_REFUSED, CONTRACT_UNDECIDABLE)

# Enumerated open questions: the registry cannot resolve this interpreter yet.
UNDECIDABLE_RELATIVE_INTERPRETER = "relative_interpreter"
# No registered project means no adapter contract to evaluate.
UNDECIDABLE_NO_REGISTERED_PROJECT = "no_registered_project"
# Lookup failure makes no claim about the project binding.
UNDECIDABLE_PROJECT_UNAVAILABLE = "project_unavailable"
UNDECIDABLE_QUESTIONS = (
    UNDECIDABLE_RELATIVE_INTERPRETER,
    UNDECIDABLE_NO_REGISTERED_PROJECT,
    UNDECIDABLE_PROJECT_UNAVAILABLE,
)

# CLI names for refusals; preflight preserves the existing missing-interpreter code.
WORKER_ERROR_CODES = {
    ADAPTER_UNAVAILABLE: "invalid_project_adapter",
    ADAPTER_INVALID: "invalid_project_adapter",
    BROAD_CHECK_INCOMPLETE: "invalid_project_adapter",
    BROAD_CHECK_NOT_DECLARED: "invalid_project_adapter",
    INTERPRETER_UNAVAILABLE: "interpreter_start_failed",
    CANNOT_ATTEST_PROJECT: "invalid_project_adapter",
}


@dataclass(frozen=True)
class ModuleContract:
    """The adapter-owned module-check runtime and suite, plus any CLI-default diagnosis.

    `module` and `args` are the half added for issue:8b39e60e4df361c6138e. Before them the contract
    could say which interpreter runs a broad check and which package it must import, but not WHICH
    SUITE it runs — so the worker task packet printed the literal placeholder
    ``<this project's broad suite module>`` and the documentation answered it with repository-wide
    discovery, a run that costs every CI suite in one process. Only the project can name its own
    broad suite, so the project's adapter is where it is named.

    `module` is empty whenever no suite was named, which a declared ``broad_check`` written before
    the field existed does not. That is not a refusal — an empty `module` only means the caller has
    to name the suite itself, the way every caller did before.

    `interpreter_declared` distinguishes an adapter-owned interpreter from the dispatcher-selected
    candidate default. It is internal routing metadata and does not change the public source record.

    `reason` is set on exactly one contract now, and it is not a project's: the default
    ``check_commands`` uses for a checkout that matches no registered project at all. A registered
    project never reaches here without a declared contract, so no verdict from `decide` carries a
    reason (see `BROAD_CHECK_NOT_DECLARED`).
    """

    interpreter: str
    import_package: str
    reason: str = ""
    module: str = ""
    args: tuple[str, ...] = ()
    interpreter_declared: bool = True
    local: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, str]:
        if self.reason:
            return {"source": "cli_default", "reason": self.reason}
        return {"source": "adapter"}


class ContractUnusable(Exception):
    """One registered project's broad-check contract, and why it cannot attest that project."""

    def __init__(self, shape: str, adapter: str, message: str) -> None:
        super().__init__(message)
        self.shape = shape
        self.adapter = adapter
        self.message = message

    @property
    def code(self) -> str:
        """The worker CLI's error code for this shape."""
        return WORKER_ERROR_CODES[self.shape]

    def evidence(self) -> dict[str, str]:
        """What is missing, in the words an outcome carries: the adapter and the exact shape."""
        return {"shape": self.shape, "adapter": self.adapter, "detail": self.message}

    def detail(self) -> str:
        return f"{self.message} [broad-check contract refusal: {self.shape}]"


class ContractStateError(Exception):
    """A verdict reached a caller that cannot act on it. Never a default allow, never a silence."""


@dataclass(frozen=True)
class ContractVerdict:
    """The answer `decide` gives: one of three named states, each carrying its own evidence.

    Built only through the three constructors below, so a state can never arrive without the thing
    that makes it readable — a refusal without its shape, or an open question without its name.

    `declared_contract` preserves validated command metadata for the relative-interpreter question.
    Its interpreter is still the adapter's relative spelling, not a runnable resolution. Only a
    fit verdict carries `contract`; the worker must still ask again with its candidate workspace.
    """

    state: str
    adapter: str = ""
    contract: ModuleContract | None = None
    refusal: ContractUnusable | None = None
    question: str = ""
    detail: str = ""
    declared_contract: ModuleContract | None = None

    @classmethod
    def as_fit(cls, contract: ModuleContract, adapter: str) -> ContractVerdict:
        return cls(state=CONTRACT_FIT, adapter=adapter, contract=contract)

    @classmethod
    def as_refused(cls, shape: str, adapter: str, message: str) -> ContractVerdict:
        if shape not in CONTRACT_REFUSALS:
            raise ContractStateError(f"unknown refusal shape {shape!r}")
        return cls(
            state=CONTRACT_REFUSED,
            adapter=adapter,
            refusal=ContractUnusable(shape, adapter, message),
            detail=message,
        )

    @classmethod
    def as_undecidable(
        cls,
        question: str,
        adapter: str,
        detail: str,
        *,
        declared_contract: ModuleContract | None = None,
    ) -> ContractVerdict:
        if question not in UNDECIDABLE_QUESTIONS:
            raise ContractStateError(f"unknown open question {question!r}")
        return cls(
            state=CONTRACT_UNDECIDABLE,
            adapter=adapter,
            question=question,
            detail=detail,
            declared_contract=declared_contract,
        )

    @property
    def fit(self) -> bool:
        return self.state == CONTRACT_FIT

    @property
    def refused(self) -> bool:
        return self.state == CONTRACT_REFUSED

    @property
    def undecidable(self) -> bool:
        return self.state == CONTRACT_UNDECIDABLE

    def evidence(self) -> dict[str, str]:
        """What this verdict says, in the words an outcome or a log carries."""
        if self.refused and self.refusal is not None:
            return self.refusal.evidence()
        record = {"state": self.state, "adapter": self.adapter, "detail": self.detail}
        if self.undecidable:
            record["question"] = self.question
        return record


def decide(
    binding: dict[str, Any],
    *,
    instance: Path,
    workspace: Path | None,
    default_interpreter: str = "",
) -> ContractVerdict:
    """The one decision point about one registered project's broad-check contract.

    `workspace` is the tree the check will actually run in, or ``None`` when no candidate workspace
    exists yet. The workspace-independent questions are asked first and a refusal among them always
    wins; only once they are all answered can what is left be `undecidable`.

    It takes no project checkout any more. The only thing that ever looked at one was the legacy
    default's layout heuristic, and a declared contract is executed as declared rather than checked
    against a layout — what such a run imported is caught afterwards by the receipt's provenance.

    Cheap and offline by construction: the binding it was handed and the adapter beside it. Nothing
    here starts a process, creates a workspace or brings up a head, which is what lets the
    dispatcher ask it before a claim.
    """
    adapter_name = binding.get("adapter")
    if not isinstance(adapter_name, str) or not adapter_name:
        return ContractVerdict.as_refused(ADAPTER_UNAVAILABLE, "", "registered project has no adapter")
    try:
        adapter = load_config(Path(instance) / "adapters" / f"{adapter_name}.yaml")
    except ConfigError:
        return ContractVerdict.as_refused(
            ADAPTER_UNAVAILABLE, adapter_name, f"adapter {adapter_name!r} is unavailable"
        )
    if not isinstance(adapter, dict) or validate(adapter, "adapter", f"{adapter_name}.yaml"):
        return ContractVerdict.as_refused(
            ADAPTER_INVALID, adapter_name, f"adapter {adapter_name!r} is invalid"
        )
    configured = adapter.get("broad_check")
    if configured is None:
        # Silence is a refusal with its own name, not somebody else's contract substituted for it.
        # A project that gets cards declares its own broad check; until this issue an adapter that
        # declared none inherited Ummanu's default, which either refused the project under a
        # diagnosis about a package it never named or attested a contract its owner never wrote.
        project = str(binding.get("id") or "")
        return ContractVerdict.as_refused(
            BROAD_CHECK_NOT_DECLARED,
            adapter_name,
            f"project {project!r} declares no broad-check contract: adapter {adapter_name!r} has "
            "no `broad_check` block",
        )
    return _declared_contract(
        configured,
        adapter_name,
        workspace,
        default_interpreter=default_interpreter,
    )


def _declared_contract(
    configured: Any,
    adapter_name: str,
    workspace: Path | None,
    *,
    default_interpreter: str = "",
) -> ContractVerdict:
    """The contract an adapter declares for itself: complete, runnable, or an open question."""
    if not isinstance(configured, dict):  # schema validation above normally catches this.
        return ContractVerdict.as_refused(
            BROAD_CHECK_INCOMPLETE,
            adapter_name,
            f"adapter {adapter_name!r} has no broad-check contract",
        )
    import_package = str(configured.get("import_package") or "").strip()
    # An omitted `module` is not an incomplete contract either. It is the shape every adapter on the
    # installation was written in, before issue:8b39e60e4df361c6138e gave a project anywhere to name
    # its own broad suite, and a contract written before a field existed is not an incomplete
    # contract. Refusing it here cost three registered projects their cards: the refusal lands at
    # preflight, so no workspace was created, no head brought up and no round run. What the absence
    # actually means is narrow — the caller has to name the suite itself — so it is enforced where
    # it bites and nowhere else: `check broad`/`check show` with no `--module` fails there by its
    # own name, `no_broad_check_module`, which says to pass the flag or to declare the key.
    module = str(configured.get("module") or "").strip()
    if not import_package:
        return ContractVerdict.as_refused(
            BROAD_CHECK_INCOMPLETE,
            adapter_name,
            f"adapter {adapter_name!r} has no broad-check contract",
        )
    raw_args = configured.get("args")
    if raw_args is None:
        args: tuple[str, ...] = ()
    elif isinstance(raw_args, list) and all(isinstance(entry, str) for entry in raw_args):
        args = tuple(raw_args)
    else:  # schema validation above normally catches this.
        return ContractVerdict.as_refused(
            BROAD_CHECK_INCOMPLETE,
            adapter_name,
            f"adapter {adapter_name!r} declares broad-check arguments that are not a list of strings",
        )
    local = configured.get("local")
    if local is not None:
        from ummanu.broad_check import BroadCheckError
        from ummanu.projects.local_check import validate_declaration

        try:
            validate_declaration(local, module, args)
        except BroadCheckError as exc:
            return ContractVerdict.as_refused(BROAD_CHECK_INCOMPLETE, adapter_name, exc.message)
    # An omitted interpreter is not an incomplete contract, it is the common case. PR #329 made the
    # check subprocess prepends the candidate workspace's own import roots to `sys.path` before it
    # imports the project. The dispatcher may supply its reserved candidate interpreter for the
    # inner suite while its outer wrapper stays on the production runtime; direct CLI callers use
    # the interpreter they deliberately invoked. A declared interpreter still means exactly what
    # it always did.
    if "interpreter" not in configured:
        interpreter = default_interpreter or sys.executable
        interpreter_path = Path(interpreter)
        if not interpreter_path.is_absolute() and workspace is not None:
            interpreter = str(Path(workspace).resolve() / interpreter_path)
        if not _executable(interpreter):
            return ContractVerdict.as_refused(
                INTERPRETER_UNAVAILABLE,
                adapter_name,
                f"could not start dispatcher-selected candidate interpreter {interpreter!r}: "
                "it is not an executable file",
            )
        return ContractVerdict.as_fit(
            ModuleContract(
                interpreter,
                import_package,
                module=module,
                args=args,
                interpreter_declared=False,
                local=local,
            ),
            adapter_name,
        )
    interpreter = str(configured.get("interpreter") or "").strip()
    if not interpreter:
        # Present but blank is a typo, not an omission: it names nothing runnable, and answering it
        # with the wrapper's interpreter would silently run a contract nobody wrote down.
        return ContractVerdict.as_refused(
            BROAD_CHECK_INCOMPLETE,
            adapter_name,
            f"adapter {adapter_name!r} has no broad-check contract",
        )
    interpreter_path = Path(interpreter)
    if not interpreter_path.is_absolute():
        if workspace is None:
            return ContractVerdict.as_undecidable(
                UNDECIDABLE_RELATIVE_INTERPRETER,
                adapter_name,
                f"adapter {adapter_name!r} names interpreter {interpreter!r}, which the adapter "
                "schema resolves from the candidate workspace; no candidate workspace exists yet, "
                "so the tree that will run the check is the only side that can answer this",
                declared_contract=ModuleContract(
                    interpreter, import_package, module=module, args=args, local=local
                ),
            )
        # Preserve a venv symlink: resolving it loses its site paths.
        interpreter = str(Path(workspace).resolve() / interpreter_path)
    if not _executable(interpreter):
        return ContractVerdict.as_refused(
            INTERPRETER_UNAVAILABLE,
            adapter_name,
            f"could not start configured interpreter {interpreter!r}: it is not an executable file",
        )
    return ContractVerdict.as_fit(
        ModuleContract(interpreter, import_package, module=module, args=args, local=local), adapter_name
    )


def contract_of(verdict: ContractVerdict) -> ModuleContract:
    """The contract to run, for the side that holds the tree. Exhaustive over the three states.

    There is no branch here that turns an unanswered question into permission: a verdict this side
    cannot act on raises rather than falling through. `decide` leaves no question open once it is
    given a workspace, so `undecidable` reaching here is a programming error and is reported as one.
    """
    if verdict.refused and verdict.refusal is not None:
        raise verdict.refusal
    if verdict.fit and verdict.contract is not None:
        return verdict.contract
    if verdict.undecidable:
        raise ContractStateError(
            f"the broad-check contract of adapter {verdict.adapter!r} is undecidable "
            f"({verdict.question}) on a side that holds the tree: {verdict.detail}"
        )
    raise ContractStateError(f"unreadable contract verdict {verdict.state!r}")


def module_contract(
    binding: dict[str, Any],
    *,
    instance: Path,
    project_root: Path,
    default_interpreter: str = "",
) -> ModuleContract:
    """The worker's own resolution: the contract to run in THIS tree, or ``ContractUnusable``.

    `project_root` is the workspace the check will run in, which is what the adapter schema means
    when it resolves a relative interpreter. Having that tree, this side leaves no question open —
    and it does not re-decide anything either: the state it acts on is the one `decide` returned.
    """
    return contract_of(
        decide(
            binding,
            instance=instance,
            workspace=project_root,
            default_interpreter=default_interpreter,
        )
    )


def _executable(interpreter: str) -> bool:
    try:
        return os.access(interpreter, os.X_OK) and Path(interpreter).is_file()
    except OSError:
        return False
