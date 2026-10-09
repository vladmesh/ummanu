"""The declared local profile and selectors share broad-check runtime and import provenance.

Only a complete profile writes or reuses a workspace-local broad receipt. Legacy broad/show
commands retain the adapter's full-profile argv; a selector runs without a broad receipt.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from ummanu.broad_check import (
    BroadCheckError,
    CheckSpec,
    candidate_import_refusal,
    receipt_path,
    recorded_result,
    run_broad_check,
    summarize,
    usable_receipt,
)
from ummanu.config import ConfigError, load_config
from ummanu.projects.contract import (
    ContractUnusable,
    ModuleContract,
    module_contract,
)
from ummanu.projects.local_check import LocalProfile, PytestSelection
from ummanu.runtime.paths import add_instance_argument

_GIT_TIMEOUT = 60

# The import package this command falls back to for a checkout that matches NO registered project.
# This is the CLI's own default, not a project contract, and it lives here rather than in
# `projects.contract` for exactly that reason: `decide` now refuses a registered project that
# declares no `broad_check` by name (`broad_check_not_declared`) instead of lending it Ummanu's
# default, and nothing in the registry may hand one project another project's contract again.
#
# An unregistered checkout is a different case and keeps working. There is no adapter there to have
# declared anything and no card, no workspace and no round at stake — it is somebody running
# `ummanu check broad --module unittest` in a clone by hand, and refusing that would break
# direct interactive use of a documented command to fix a problem it does not have. The response
# still says so out loud: `module_contract.source` is `cli_default` with the reason
# (`no_project_binding` / `project_binding_disabled`) that named the fallback before.
CLI_DEFAULT_IMPORT_PACKAGE = "ummanu"


class ResolvedCheck:
    """The executable check and the contract selection the caller should be able to see."""

    def __init__(
        self, spec: CheckSpec, module_contract: dict[str, str] | None = None, selector: str = ""
    ) -> None:
        self.pytest_marker: str | None = None
        self.spec = spec
        self.module_contract = module_contract
        self.selector = selector


def add_check_subcommands(subparsers) -> None:
    check = subparsers.add_parser(
        "check", help="run the declared local profile, a module or a test; show its broad receipt"
    )
    _common(check)
    check.add_argument("check_command", nargs="?", default="", metavar="SELECTOR|broad|show")
    check.add_argument("selectors", nargs="*", metavar="SELECTOR")
    check.add_argument("--timeout-seconds", type=float, default=0.0)
    check.add_argument("--reuse", action="store_true", help="reuse a usable full-profile receipt")
    check.set_defaults(handler=run_check)


def run_check(args: argparse.Namespace) -> int:
    if args.check_command == "show":
        return run_check_show(args)
    if args.check_command == "broad":
        return run_check_broad(args)
    # The new entrypoint always uses the adapter declaration. Legacy shape flags belong only
    # to broad/show and cannot be used to replace this profile or smuggle runner arguments.
    if args.module or args.command or args.module_arg:
        return _fail(
            BroadCheckError(
                "local_check_override",
                "use a selector with ummanu check; shape flags belong to the legacy broad/show commands",
            )
        )
    args.reuse = True
    return run_check_broad(args)


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=".", help="workspace root; the receipt lives under it")
    add_instance_argument(
        parser, help="registered project adapters (default: UMMANU_INSTANCE or the default instance)"
    )
    # Not `required=True` any more. Since issue:8b39e60e4df361c6138e the registered project's
    # adapter can name its own broad suite, and when it does, the whole point is that a worker (or
    # the prompt that tells a worker what to run) does not have to know the module name to run the
    # project's broad check. The group stays mutually exclusive — `--module` and `--command` are
    # still two different promises about import provenance — and neither flag falls back silently:
    # a project that declares no module and is given none fails with `no_broad_check_module`.
    shape = parser.add_mutually_exclusive_group()
    shape.add_argument(
        "--module",
        help="run `python -m MODULE` in this workspace; the standard shape, which attests the "
        "project the check process imported. Omitted, the registered project's declared broad "
        "suite is used",
    )
    shape.add_argument(
        "--command",
        help="run an arbitrary command through bash -lc; its receipt attests no import provenance "
        "and is never reused in place of a run",
    )
    parser.add_argument(
        "--module-arg", action="append", default=[], help="argument passed to --module, repeatable"
    )
    parser.add_argument(
        "--default-interpreter",
        default="",
        help="candidate interpreter used when the registered adapter omits broad_check.interpreter; "
        "dispatcher task packets set this to their workspace-owned environment",
    )


def _fail(exc: BroadCheckError) -> int:
    print(json.dumps({"error": {"code": exc.code, "message": exc.message}}), file=sys.stderr)
    return 2


def _spec(args: argparse.Namespace) -> ResolvedCheck:
    """Resolve runtime once and validate the complete profile or one declared selector.

    Registered profiles accept their declared full argv or a validated selector. Legacy manual
    checks in unregistered clones keep the existing CLI default and shell provenance rules.
    """
    if args.module_arg and not args.module:
        raise BroadCheckError("module_arg_without_module", "--module-arg needs --module")
    contract = _module_contract(
        Path(args.root),
        Path(args.instance),
        default_interpreter=args.default_interpreter,
    )
    new_form = args.check_command not in {"broad", "show"}
    selector = args.check_command if new_form else ""
    selectors = (selector, *args.selectors) if selector else ()
    if args.selectors and not new_form:
        raise BroadCheckError("local_check_override", "broad/show selectors use --module-arg")
    profile = LocalProfile.load(Path(args.root), contract.local) if contract.local is not None else None
    if new_form and profile is None:
        raise BroadCheckError(
            "local_check_not_declared",
            "adapter is missing broad_check.local; declare manifest or runner-owned membership "
            "before using ummanu check or a selector",
        )
    if profile is not None and not profile.runner_owned:
        # Legacy shape overrides cannot hide the manifest's CI ownership diagnostic.
        if len(args.module_arg) == 1 and tuple(args.module_arg) != contract.args:
            profile.select(args.module_arg[0])
        if args.module and args.module != contract.module:
            try:
                profile.select(args.module)
            except BroadCheckError as exc:
                if exc.code == "ci_only_module":
                    raise
    if not contract.reason:
        if args.command:
            raise BroadCheckError(
                "local_check_override", "a registered profile cannot be replaced by --command"
            )
        if args.module and contract.module and args.module != contract.module:
            raise BroadCheckError(
                "local_check_override", "--module must equal the adapter's broad_check.module"
            )
        if args.module_arg:
            if tuple(args.module_arg) == contract.args:
                pass  # Dispatcher-produced argv for the declared full profile.
            elif profile is not None:
                supplied = tuple(args.module_arg)
                prefix = contract.args + profile.selector_args
                if len(supplied) > len(prefix) and supplied[:len(prefix)] == prefix:
                    selectors = supplied[len(prefix):]
                else:
                    selectors = supplied
                if len(selectors) != 1 and contract.module != "pytest":
                    raise BroadCheckError(
                        "local_check_override", "subset arguments must name one selector"
                    )
                selector = selectors[0]
            else:
                raise BroadCheckError(
                    "local_check_not_declared" if profile is None else "local_check_override",
                    "subset arguments require broad_check.local and exactly one permitted selector",
                )
    elif args.command:
        if args.module_arg:
            raise BroadCheckError("module_arg_without_module", "--module-arg needs --module")
        return ResolvedCheck(CheckSpec.for_shell(args.command))

    module = args.module or contract.module
    if not module:
        raise BroadCheckError(
            "no_broad_check_module", "pass --module or declare broad_check.module in the adapter"
        )
    pytest_selection = None
    if profile is not None:
        if selectors:
            if profile.runner_owned and module == "pytest":
                pytest_selection = PytestSelection.resolve(contract.args, contract.collection_roots)
                module_args = list(pytest_selection.select(Path(args.root), selectors))
            else:
                if len(selectors) != 1:
                    raise BroadCheckError("local_check_override", "subset arguments must name one selector")
                _path, target = profile.select(selectors[0])
                module_args = [*contract.args, *profile.selector_args, target]
            if args.check_command == "show":
                raise BroadCheckError("subset_has_no_receipt", "a subset has no full-round receipt to show")
        else:
            module_args = list(contract.args)
    elif args.module:
        module_args = list(args.module_arg) if args.module_arg else list(contract.args)
    else:
        if args.module_arg:
            raise BroadCheckError("module_arg_without_module", "--module-arg needs --module")
        module_args = list(contract.args)
    resolved = ResolvedCheck(
        CheckSpec.for_module(
            module, module_args, interpreter=contract.interpreter, import_package=contract.import_package
        ),
        contract.as_dict(),
        selector,
    )
    if pytest_selection is not None:
        resolved.pytest_marker = pytest_selection.marker()
    return resolved


def _module_contract(
    root: Path,
    instance: Path,
    *,
    default_interpreter: str = "",
) -> ModuleContract:
    """Return the registered project's contract, or the CLI default for an unregistered checkout.

    A worker's checkout is normally a git worktree, not the registered checkout itself. Comparing
    git common directories identifies the registered repository without guessing from its files;
    an ordinary unregistered checkout keeps `CLI_DEFAULT_IMPORT_PACKAGE` for direct use, which is
    a deliberate choice and not a leftover — see the note on that constant.

    A registered project's contract is judged by `projects.contract`, the one implementation of
    those rules, and the dispatcher's preflight asks it the same question before a card is ever
    given to a worker (secretary-1458). This side maps its refusal onto the CLI error contract and
    never re-decides what a usable contract is — including the refusal a registered project earns
    by declaring no contract at all, which is `decide`'s to name and not this side's to paper over.
    """
    binding, fallback_reason = _binding_for_workspace(root, instance)
    if binding is None:
        return ModuleContract(sys.executable, CLI_DEFAULT_IMPORT_PACKAGE, fallback_reason)
    try:
        return module_contract(
            binding,
            instance=instance,
            project_root=root,
            default_interpreter=default_interpreter,
        )
    except ContractUnusable as exc:
        raise BroadCheckError(exc.code, exc.message) from exc


def _binding_for_workspace(root: Path, instance: Path) -> tuple[dict[str, object] | None, str]:
    projects = instance / "projects"
    if not projects.is_dir():
        return None, "no_project_binding"
    enabled: list[dict[str, object]] = []
    disabled_match = False
    root_common = _git_common_dir(root)
    for path in sorted(projects.glob("*.yaml")):
        try:
            binding = load_config(path)
        except ConfigError:
            continue
        if not isinstance(binding, dict):
            continue
        repo = binding.get("repo")
        if not isinstance(repo, str) or not repo:
            continue
        if not _same_repository(
            root, Path(repo).expanduser(), first_common=root_common, first_common_known=True
        ):
            continue
        if binding.get("enabled") is True:
            enabled.append(binding)
        else:
            disabled_match = True
    if len(enabled) > 1:
        raise BroadCheckError("ambiguous_project", "workspace matches more than one registered project")
    if enabled:
        return enabled[0], ""
    return None, "project_binding_disabled" if disabled_match else "no_project_binding"


def _same_repository(
    first: Path,
    second: Path,
    *,
    first_common: Path | None = None,
    first_common_known: bool = False,
) -> bool:
    try:
        if first.resolve() == second.resolve():
            return True
    except OSError:
        return False
    if not first_common_known:
        first_common = _git_common_dir(first)
    second_common = _git_common_dir(second)
    return first_common is not None and first_common == second_common


def _git_common_dir(root: Path) -> Path | None:
    try:
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
        common = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-common-dir"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if top.returncode != 0 or common.returncode != 0:
        return None
    top_path = Path(top.stdout.strip())
    common_path = Path(common.stdout.strip())
    if not top_path.is_dir() or not str(common_path):
        return None
    return (common_path if common_path.is_absolute() else top_path / common_path).resolve()


def run_check_broad(args: argparse.Namespace) -> int:
    """Run the check and hand back its own exit status, never a status of our own invention."""
    root = Path(args.root)
    try:
        resolved = _spec(args)
        spec = resolved.spec
        if resolved.selector:
            return _run_subset(args, resolved)
        if args.reuse:
            # The one authorization question, asked the one way `check show` asks it.
            lookup = usable_receipt(root, spec)
            authorized = lookup.authorized()
            if authorized is not None:
                payload = {
                    "reused": True,
                    "path": str(lookup.path),
                    "receipt": authorized,
                    "summary": summarize(authorized),
                }
                if resolved.module_contract is not None:
                    payload["module_contract"] = resolved.module_contract
                print(json.dumps(payload, sort_keys=True, indent=2))
                # A receipt that stands in for the run hands back the result that run had, taken
                # from the canonical model the load boundary reconstructed — never from a raw
                # field this command read for itself.
                return lookup.authorized_result().shell_status
        # The check's combined output goes to stderr so it stays visible live while stdout keeps
        # carrying exactly one JSON document, as every other command here does.
        _exit_code, receipt = run_broad_check(
            spec,
            root=root,
            stream=sys.stderr,
            timeout_seconds=args.timeout_seconds or None,
        )
    except BroadCheckError as exc:
        return _fail(exc)
    result = recorded_result(receipt)
    if result is None:  # unreachable: the writer records the model it just derived
        return _fail(BroadCheckError("unrepresentable_result", "the check result could not be recorded"))
    # `run_broad_check` derives both values from one RunResult. Refuse loudly if that internal
    # invariant ever regresses instead of returning a receipt status softer than the subprocess.
    if _exit_code != result.exit_code:
        return _fail(
            BroadCheckError(
                "receipt_status_mismatch",
                f"check exit code {_exit_code} disagrees with recorded exit code {result.exit_code}",
            )
        )
    payload = {
        "reused": False,
        "path": str(receipt_path(root, spec)),
        "receipt": receipt,
        "summary": summarize(receipt),
    }
    if resolved.module_contract is not None:
        payload["module_contract"] = resolved.module_contract
    print(json.dumps(payload, sort_keys=True, indent=2))
    return result.shell_status


def _run_subset(args: argparse.Namespace, resolved: ResolvedCheck) -> int:
    _code, observation = run_broad_check(
        resolved.spec,
        root=Path(args.root),
        stream=sys.stderr,
        timeout_seconds=args.timeout_seconds or None,
        record_receipt=False,
    )
    refusal = candidate_import_refusal(
        observation, Path(args.root), expected_package=resolved.spec.import_package
    )
    if refusal:
        raise BroadCheckError("candidate_import_refused", refusal)
    result = recorded_result(observation)
    if result is None:
        raise BroadCheckError("unrepresentable_result", "the subset returned an unrepresentable result")
    if resolved.pytest_marker is not None and result.exit_code == 5:
        marker = f"; declared marker {resolved.pytest_marker!r}" if resolved.pytest_marker else ""
        raise BroadCheckError(
            "pytest_selection_empty",
            f"pytest selector deselected or collected no tests{marker}; execution only in CI",
        )
    print(
        json.dumps(
            {
                "selector": resolved.selector,
                "argv": resolved.spec.displayed_argv(),
                **result.as_fields(),
                "project_provenance": observation["project_provenance"],
                "timing": observation["parsed"].get("timing"),
            },
            sort_keys=True,
            indent=2,
        )
    )
    return result.shell_status


def run_check_show(args: argparse.Namespace) -> int:
    try:
        resolved = _spec(args)
        lookup = usable_receipt(Path(args.root), resolved.spec)
    except BroadCheckError as exc:
        return _fail(exc)
    payload = lookup.as_dict()
    if resolved.module_contract is not None:
        payload["module_contract"] = resolved.module_contract
    if lookup.receipt is not None:
        payload["summary"] = summarize(lookup.receipt)
    print(json.dumps(payload, sort_keys=True, indent=2))
    # `show` answers whether a run may be skipped, not what the check decided: 0 when a receipt is
    # authorized, 1 when it is not. The check's own status is in the receipt it prints.
    return 0 if lookup.authorized() is not None else 1
