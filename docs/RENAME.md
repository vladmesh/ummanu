# Old-name policy: secretary → ummanu

The product became `ummanu` on 2026-10-02 in
[ad286ae0](https://github.com/vladmesh/ummanu/commit/ad286ae008bc88f7d27941d55cb547b5e1af898e)
(secretary-1932, sprint:1475). The installation transition is complete; there is no second host.
Package/CLI `ummanu`, `UMMANU_*`, units `ummanu-*` and current paths have no old-name aliases.

## Guard

The old name remains only under explicit H (Hermes agent), I (instance repository), R (historical
records) and G (guard/policy source and negative fixtures) rows. A class grants no blanket
exemption. `tests/test_old_name_guard.py` checks every tracked path and text file for
`(?i)secretary`; binary files still have their paths checked. It runs in unit, declared local broad
and CI. Its product allowlist is exactly:

| Class | Pattern/scope | Paths |
|---|---|---|
| H | `hermes-secretary`, `hermes-secretary-roles`, `~/.hermes/skills/secretary`, `~/.hermes/skills/secretary-roles` | `skills/manifest.toml` only |
| I | `\bsecretary-instance\b(?!-maintenance)` | Any tracked path |
| R | `\bsecretary-\d+\b(?!\.\d)` card references | Any tracked path |
| R | `\bsecretary_1727_[0-9a-f]+\.jsonl\.gz\b` journal fixture names | Any tracked path |
| R | Whole immutable historical record | `tests/fixtures/gate_attestation_1883.json` |
| R | Whole source-captured cwd/key/prefix golden for the released sprint:1475 move | `tests/fixtures/rebind_move_sprint1475.json` |
| R | Only exact lines `_OLD_PRODUCT = "secretary"` and `_OLD_DATA = "secretary-data"` | `src/ummanu/automations/agents/curator/move_paths.py` |
| G | Exact quoted old env/package negative values in the implemented regex | `tests/test_env_prefix.py` |
| G | Exact quoted removed-verb/script/test inventory negative values in the implemented regex | `tests/test_installation_retirement.py` |
| G | Whole policy/guard source | This document, `tests/test_old_name_guard.py`, `src/ummanu/infra/old_name_guard.py` |

The I matcher excludes the product maintenance unit; the R card matcher excludes version names
such as `secretary-0.1.0.dist-info`. Historical instance repository and remote/card references are
allowed, but an operational source literal naming the old live root is forbidden.
`INSTANCE_PROJECT = "secretary-instance"` has one exact definition in `infra.old_name_guard`;
consumers import that policy. Doctor's separate path matcher refuses the historical root in
installed product units, runtime env and role env bound to this installation
([Recovery](RECOVERY.md#names-on-the-host)).

The env regression's allowed quoted values are `SECRETARY_DATA_DIR`, `SECRETARY_INSTANCE`,
`TA_SECRETARY_REPO`, `SECRETARY_RUNTIME_ENV_FILE`, `SECRETARY_`, `TA_SECRETARY_`,
`import secretary` and `ModuleNotFoundError: No module named 'secretary'`. They check ignored old
keys, effective current keys and installed-package refusal. The retirement regression's allowance
is limited to its exact quoted negative inventory; the same references planted in source or
operational docs fail. No command or script spelling has an anywhere allowance.

Curator rebind consumes the frozen historical cwd map for persisted pending identities, watermark
paths and Claude transcript keys from the released sprint:1475 move. Pair order, whole-component
matching and nonalphanumeric key encoding remain recovery policy
([Operations](OPERATIONS.md#background-role-telemetry)); other automation/infra source has no new
exemption. No live records or historical fixture bytes are translated by this policy.

## Live-root scope

`ummanu config check --instance LIVE_ROOT` uses `infra.old_name_guard.LIVE_ROOT_ALLOWLIST` over
the live root's exported files, without Git. The product guard shares its matcher. The live-root
table has H spellings and I/R card spellings in any path, R references matching
`\bprojects/secretary/` only in `state/knowledge/*`, and R `^(?:source|supersedes):.*$` front-matter
lines only in `state/memory/facts/*`. Whole-file R rows are exactly `state/board/*`,
`state/knowledge/*` and `state/runs/*`. They exclude live instructions:
`LIVE_KNOWLEDGE = ("state/knowledge/runbooks/[!0-9]*", "state/knowledge/plans/current-*")`.
Receipt directories `gate-runs/**` and `provision-runs/**` are historical instance paths, not
exported live-root rows. The retired instance guard's receipt rows are therefore not ported.

Paths use case-sensitive `fnmatch` globs, where `*` crosses `/`. Whole-file rows cover path and
content; otherwise every case-insensitive old-name occurrence must lie inside an allowed pattern
match. Failures identify the path, line and matched text. Operational runbooks/current plans do
not inherit whole-file history coverage. The standalone PR #668 audit is historical evidence;
it is absent from the main product tree and adds no allowlist row here.
