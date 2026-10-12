# Old-name policy: secretary → ummanu

## Rename history

The product became `ummanu` on 2026-10-02 in
[ad286ae0](https://github.com/vladmesh/ummanu/commit/ad286ae008bc88f7d27941d55cb547b5e1af898e)
(secretary-1932, sprint:1475). The installation transition is complete; there is no second host.
Package/CLI `ummanu`, `UMMANU_*`, units `ummanu-*` and the new paths have no old-name aliases.

### T1. Entrypoint move

An entrypoint move removes `src/<PACKAGE>/dispatch/runtime_preflight.py` or the running package's
`[project.scripts]` console script from a target commit. `dispatch/entrypoint_guard.py` checks the
target Git objects for both; `dispatch/production_checkout.advance` calls it after the fast-forward
check and before board migrations, and `upgrade.fast_forward` uses the same guard before moving the
checkout. An `entrypoint_moved` refusal leaves the live checkout on its current tree even if the PR
has merged. Release recovery blocks activation and opens the operation referring to §T3; retrying
release or ordinary upgrade cannot activate a tree whose entrypoint has moved. The guard remains
applicable to the running package after the rename (secretary-1929).

### T3. Installation transition

An entrypoint move requires an installation operation that moves code, environment, data and unit
entrypoints together, after stopping the callers that execute the old tree. The completed rename's
implementation remains in `src/ummanu/transition/`, with `scripts/transition-from-secretary.sh` and
`ummanu transition from-secretary --instance … (--plan | --apply | --rollback)`; its literal name
table, preconditions, journalled steps and rollback describe the supported historical operation.
`--plan` reads without writing, apply resumes journalled steps, and rollback uses only the recorded
effects and rollback copies. It is retained until a separate removal card, not a recurring upgrade
procedure. Guard diagnostics, release recovery and archives spanning the rename cite this section
to distinguish installation activation from an ordinary checkout advance. Detailed steps are in
the implementation and this file's git history.

### T5. Guard

The old name stays only in four classes: H (the Hermes agent), I (the instance repository),
R (historical records) and T (the retained transition and guard). A class does not grant a blanket
exception: only the implemented allowlist rows cover a match.

- **Product repo:** `tests/test_old_name_guard.py`, in the unit suite (so in `tests.broad` and CI), listed
  in `tests/ci-shards.txt`. It walks `git ls-files`, checks every tracked
  path and every text file (binary skipped) for `(?i)secretary`, and fails on any match that is not
  allowlisted:
  - **H:** `hermes-secretary`, `hermes-secretary-roles`, `~/.hermes/skills/secretary`, `~/.hermes/skills/secretary-roles` (only in `skills/manifest.toml`).
  - **I:** `secretary-instance` (any file).
  - **R:** card refs `\bsecretary-\d+\b`; the fixture path `tests/fixtures/local_pty_journals/secretary_1727_*.jsonl.gz`.
  - **T:** `src/ummanu/transition/**`, `scripts/transition-from-secretary.sh`, `tests/test_transition_from_secretary.py`, `docs/RENAME.md`, the guard test itself and its shared matcher `src/ummanu/infra/old_name_guard.py`.

  Nothing else, and no per-file exemptions outside these four classes. The exact table also
  retains `scripts/rename_to_ummanu.py` under T and the immutable quoted record
  `tests/fixtures/gate_attestation_1883.json` under R. References to
  `transition-from-secretary.sh`, `test_transition_from_secretary` and `from-secretary` are T
  spellings allowed in any file. The I matcher excludes `secretary-instance-maintenance`;
  the R card matcher excludes version spellings such as `secretary-0.1.0.dist-info`.
  Journal fixture spellings match `\bsecretary_1727_[0-9a-f]+\.jsonl\.gz\b`.
- **Env:** `tests/test_env_prefix.py` sets `SECRETARY_DATA_DIR`, `SECRETARY_INSTANCE` and
  `TA_SECRETARY_REPO` to bogus values and asserts that resolution ignores them, then that the `UMMANU_*`
  equivalents take effect. A second assertion checks that `import secretary` raises `ModuleNotFoundError`
  in a clean interpreter on the installed tree.
- **Instance repo:** the retired `tests/test_old_name_guard.py` in `secretary-instance` used the same
  classes. R there covers `state/board/**`, `state/knowledge/**`, `state/runs/**`,
  `gate-runs/**`, `provision-runs/**` and `source:` lines of memory facts. Historical external
  adapters/projects (`secretary-supervisor*`) were outside that transition's scope. Since ummanu-32
  that suite is retired: its allowlist is ported into `src/ummanu/infra/old_name_guard.py`
  (`LIVE_ROOT_ALLOWLIST`, verbatim but for the receipt rows, whose paths are no longer exported;
  the matcher the product guard shares, listed under T), and
  `ummanu config check --instance LIVE_ROOT` runs it over the live root's exported files,
  without Git ([Operations](OPERATIONS.md#changing-installation-config)).

The live-root table has H spellings and I/R card spellings in any path, R references matching
`\bprojects/secretary/` only in `state/knowledge/*`, and R `^(?:source|supersedes):.*$` lines only
in `state/memory/facts/*`. Whole-file R rows are exactly `state/board/*`, `state/knowledge/*` and
`state/runs/*`. They do not cover live instructions: `LIVE_KNOWLEDGE` is
`("state/knowledge/runbooks/[!0-9]*", "state/knowledge/plans/current-*")`. Receipt directories
`gate-runs/**` and `provision-runs/**` are historical instance paths, not exported live-root rows.
`fnmatch` uses case-sensitive paths and `*` crosses `/`; a whole-file row covers path and text,
otherwise every case-insensitive old-name occurrence must lie inside an allowed pattern match.
Failures identify the path, line and matched text. Product source outside `src/ummanu/transition/*`
also cannot spell the old instance root literally: use the transition's name table.

The standalone `opus_review.md` remains a historical audit (R), not operational instructions.
PR #668 recorded this classification verbatim:

> The standalone `opus_review.md` audit is also a class R record: it preserves quoted historical
> source and production unit names. Its exact path is allowlisted; this does not permit the same
> names in operational documentation or product source.

That quotation records the audit PR's allowance. `opus_review.md` and its proposed whole-file
row are absent from the current `origin/main` baseline; the two implemented tables above remain
the authority. This policy neither adds an exception nor reclassifies the archived audit.
