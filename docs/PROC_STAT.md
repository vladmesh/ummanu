# Per-process stat readers

`runtime/procfs.py` owns Linux `/proc/<pid>/stat` tokenization and the offsets
relative to field 3. It has no imports, reads, numeric validation, or liveness
decision. Comm ends at its final `)`, including names with spaces and parentheses.
Missing close raises `ValueError`; empty and short tails remain token lists.

Two historical lexical forms remain: the default discards one character after
the close before splitting; `skip_separator=False` splits immediately after it.
They agree on kernel output and whitespace, but differ on malformed `)S ...`.
The argument describes syntax, not a caller's failure or authorization policy.

| Reader and operative seam | Text / separator | Local policy |
| --- | --- | --- |
| `head/identity._proc_starttime_ticks` | UTF-8 strict / discard one | Needs start field; returns raw token, including nonnumeric text. Missing close or short tail raises the original named `ValueError`. `head_process_status` retains its unknown/dead/mismatch classification; boot reading remains separate. |
| `head/children._stat_fields` | UTF-8 replace / discard one; supplied proc root | IO, missing close, or short tail returns `None`. `read_head_children` converts PPID, four CPU counters and start locally; excludes `Z`, `X`, `x`. |
| `head/local_pty/scoped_lifecycle.launch_identity` | Default encoding / immediate | Reads stat before boot. Stat ENOENT/ESRCH returns `None`; boot ENOENT returns `None`; other IO or missing close raises `MemoryScopeError`. Empty/short token indexing retains its existing `IndexError`. `Z`/`X` returns `None`; raw start is accepted otherwise. |
| `scoped_lifecycle.launch_group_present` | Default encoding / immediate | Reads boot first. Wrong boot or reused leader returns false. Missing leader still scans; vanished members are skipped individually. Other IO and malformed evidence raise. Group and state remain raw strings. |
| `head/local_pty/supervisor._live_head` | UTF-8 strict / discard one | Reads record, probes PID, reads boot, then stat. IO, malformed/short stat, `Z`, or identity mismatch returns 0. `X` is deliberately not added to its state rule. `_head_pid` is a supervisor member, not this reader's function name. |
| `po/runner.process_identity` | UTF-8 replace / discard one | Reads stat then boot before parsing. IO/short returns `None`; missing close raises `ValueError`; returns raw boot/start, without rejecting zombie state. |
| `po/runner.still_running` | UTF-8 replace / discard one | Calls the original identity seam, then rereads stat. IO returns false; missing close raises `ValueError`; only `Z`/`X` are rejected, including the historical empty-tail behavior on that second read. |
| `runtime/state._process_start` | UTF-8 replace / discard one | IO/ValueError/IndexError returns `None`; otherwise returns int, including zero and negative values. Global `/proc/stat` btime is a different format. |
| `host_apply._process_start_ticks` | UTF-8 strict / immediate | IO/missing close/short/nonnumeric/nonpositive start returns `None`. Unicode decoding errors still propagate. |
| `runtime/test_guard.reviewer_head`, `authorized` | Default encoding / immediate | PPID only, with no start/CPU validation. Existing OS/Unicode/Value/Index failures end ancestry traversal. Role, bootstrap hash, exact argv/roots, cycle detection, and refusal 125 remain unchanged. |

`scoped_lifecycle._launch_stat_fields` adapts only missing-close exception type
and message to the previous `rsplit` behavior. It owns no tokenization. Existing
public helpers and operative read/parse bindings remain available; there are no
compatibility aliases or broadcast patches. `scope_inventory` already delegates
to identity helpers and has no stat tokenizer. No boot-id helper was consolidated.

The heartbeat writer embedded in `head/command.py` is the explicit inline
exception. It remains byte-identical in this change. `runtime_preflight.py` and
`role_skills.py` are also untouched. Status readers and global `/proc/stat` btime
readers use different formats.

## Standalone guard provenance

The maintained guard imports the pure leaf. Its existing installers call
`standalone_source`, which replaces exactly that import with the canonical leaf's
source bytes. The installed `_ummanu_test_guard.py` therefore has a mechanically
derived deployment copy, with no product import at interpreter startup and no
new installed dependency. Installer-only imports remain inside uncalled functions.
There is no runtime evaluation of downloaded code or renamed product module.
The installers retain their candidate-prefix and claimed-namespace checks.

`tests/test_procfs.py` compares rendered/canonical AST, executable bytecode and
constants, blocks all product imports while executing the artifact, compares
ancestry decisions, and exercises both installers in temporary fixtures. The
original native guard suite adds a cold-start parser hash and zero-preloaded
product-module assertion; its env-i, argv, reviewer and direct-run refusal tests
retain their assertions and CI owner.

## Characterization evidence

The unit module calls real adapters with deterministic file observations. When
the worker's ignored `.ummanu-task-env/procstat/base` archive is present, it also
executes the selected original function definitions from base bytes against the
same fixtures and records complete values, exceptions and observation sequences
in `characterization.json`. This is fixture-on-basebytes evidence, not execution
of a pristine-base suite and not a claimed baseline bug fix. CI always executes
the explicit contract assertions, independent of that optional worker archive.
The ownership regression scans source for maintained close/space tokenization,
tests planted duplicates, and allows the embedded writer through its source
boundary rather than freezing the historical reader bodies.
