# Dispatcher host implementation owners

`dispatch.host` composes `CommandHostRuntime` and retains its constructor, provider
ingress/preflight, worker/reviewer launches, Git admission/gate/merge, continuation
delivery and bounded command execution. `LaunchedHead` and `DispatcherHeadTransport`
retain their original definitions there. Public imports of `InstanceCatalog` and
`CommandHostRuntime` keep the released API and object identities.

The implementation package `dispatch.host_runtime` has a docstring-only namespace:

| Owner | Capability |
| --- | --- |
| `catalog` | Instance configuration, project availability, integration base/seed and head choice; repository comparison and live-root refusal |
| `observer_io` | Observer worktree/launch/stop/delivery and exact-run provider I/O |
| `workspace` | Seed validation, setup, reserved Python environment ownership, test guards and bytecode exclusions |
| `lifecycle` | Workspace/head stop, heartbeat/process confirmation, signal/wait, freeze and retained continuation |
| `packets` | Worker/reviewer documents, declared checks, frozen local-run policy, revision feedback and private body files |
| `contracts` | Shared durable-run parsing, launch-writer capability and delivery evidence helpers |

The four mixins have no constructors. Every moved method still calls collaborators
through `self`, preserving instance/subclass patches. Three revision binding methods
remain in the core because they explicitly call
`CommandHostRuntime._canonical_decision_binding`; their class lookup is unchanged.
Lower owners never import the facade, even for annotations. The existing observer
tick/state machine, prompt renderers, review evidence resolver, workspace manager,
cleanup journal, launcher, watchdog and head providers retain their responsibilities.

Patch a global where the operation executes. Lifecycle process reads and stop timing
now belong to `host_runtime.lifecycle`; review document writes belong to
`host_runtime.packets`; observer Git creation belongs to `host_runtime.observer_io`.
Worker/reviewer `_launch`, gate functions, provider preparation/turn detection,
continuation heartbeat reads and command timeouts still execute in `dispatch.host`.
The dynamic provider seam in `test_card_role_boundary` still targets that core.
Shared module objects such as `memory_access` retain identity across imports, so
patching `memory_access.issue_grant` continues to affect both launch paths. A facade
alias alone is never evidence that a patch affects a moved operation.

`tests.test_host_owners` bounds source parsing to these files and checks unique
implementations, the 3000-newline limit, reverse imports (including planted nested,
relative, alias and string cases), descriptors/MRO and operative faults. Existing
catalog-call source guards traverse all concrete MRO owners. The declared manifest
retains the existing local/CI suite assignments. The existing task-protocol graph
workflow also covers these owners and their changed consumers; the frozen original
scanner and all-four kernel guard remain unchanged, with expensive graph checks in CI.

`tests/fixtures/host-packets.json` contains complete deterministic worker/reviewer
documents, including rework feedback, round/request identities, declared checks and
an exact-vector local-run grant. Only the temporary fixture root is normalized.
These bytes were captured through the declared local runner from the extracted
implementation after source equivalence verification. They are fixture evidence,
not an independently executed pre-cut baseline or a live delivery receipt.
For an intentional packet change, review the entire golden diff and regenerate with:

```sh
UMMANU_REGENERATE_HOST_GOLDENS=1 PYTHONPATH="${UMMANU_REPO:-$HOME/ummanu}/src${PYTHONPATH:+:$PYTHONPATH}" /home/dev/ummanu/.venv/bin/python -P -m ummanu check --default-interpreter .ummanu-task-env/venv/bin/python3 -- tests/test_host_owners.py::HostOwnerBehaviorTests::test_complete_worker_and_reviewer_documents_match_captured_bytes
```

The full baseline-to-candidate AST/global/caller inventory is card-scoped evidence
under the worker's `artifacts/ummanu-262`, not a permanent historical AST pin.
Future legitimate edits retain the useful ownership and behavior guards without
regenerating a second task-protocol inventory. This closes only DEC-02's host file
portion; no runtime, board, persistence, deployment or broader architecture policy
changes are claimed.
