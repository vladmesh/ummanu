# systemd assets

Templates for the ummanu runtime units: production dispatcher ticks, the memory service, the PO
service, the web transport and its front, curator, steward (including the deep sweep), retro, the
periodic `doctor` record, independent checkpoint and daily instance maintenance. There is no scheduled backup unit: the
git checkpoint is the recovery contract ([Recovery](../../docs/RECOVERY.md)), and `backup create` is
a manual, optional cold archive.

These files are templates, not host-ready files or a starting point to copy by hand. `ummanu
reconcile apply` compiles them with the installation user, home, product checkout, instance and
data layout, then installs those bytes and records ownership in `host-managed.json`. `ummanu
upgrade` does the same on every release. The rendered bytes and their digest are the desired state,
so editing a template or changing the installation layout makes the next apply update the unit.

The manifest is private state for the installation account. A root-run reconcile publishes it as that
account with mode `0600`, using the instance checkout owner as the source of the installation user;
an unreadable manifest is reported as a host-state error, not treated as an empty managed set.

Every unit name here must fall under the instance's `host.unit_prefix`, and its component name (the
file name minus that prefix and the suffix) is what `host.components` opts out of. Paths in
committed templates contain placeholders for that layout. Run `systemd-analyze verify` on anything
you change.
A unit already on the host is never overwritten until it is adopted; apply refuses to write over a
name it cannot prove it owns.

`ummanu-dispatcher-production.timer` launches a one-shot `production-tick` through the configured
venv's isolated `runtime_preflight.py`. The preflight runs by pathname before the `ummanu` entry
point can import an editable package, refuses foreign or task-workspace provenance with the existing
production-state diagnostic, and execs the tick only after a valid observation. It is materialized
with the unit through `reconcile apply` or `ummanu upgrade`; do not copy or edit it on the host.
Every one-shot unit that can launch a local-pty head (the production tick and the curator, retro,
steward and deep-sweep ticks) sets `KillMode=process`: the head's supervisor outlives the tick, and
systemd's default control-group kill would SIGTERM it the moment the tick exits. A new one-shot unit
either sets it too or is listed in `tests/test_board_boot_race.py` as one that launches no head.
Profile-backed heads enter a unique transient system scope through noninteractive sudo, then return
to the unit's runtime UID before the supervisor starts. The supervisor stays as the scope's
synchronous command after the dispatcher tick exits, so the memory ceiling remains active for the
whole head run. `MemoryMax` is 8192 MiB by default; `MemorySwapMax=0` keeps swapping from
evading that per-head ceiling. A
profile can set `memory_limit_mib`, and the shipped high tiers use 12288 MiB. A scope registration
failure refuses the launch. The supervisor records `head_loss_reason=memory_limit` only from a
kernel OOM kill record naming its reserved head PID, together with SIGKILL and no operator stop.
The durable owner must prove recursive scope membership empty before stop or settlement succeeds.
See [Head scopes](../../docs/HEAD_SCOPES.md) for launch, cleanup, causal evidence and upgrade boundaries.
`ummanu-instance-maintenance.timer` fires daily (`Persistent=true`) a one-shot
`ummanu instance-maintenance` at idle CPU and I/O priority: Git's own `gc --auto` heuristic run
outside any tick, because the lifecycle sets `gc.auto=0` in the instance repository so that no
checkpoint commit packs. The packing step takes no state-repo lock; only the short reflog expiry
after it does ([Recovery](../../docs/RECOVERY.md#local-git-packing-controls)).
`ummanu-memory.service` serves MCP on the configured local endpoint and loads the instance
embedding model. `ummanu-web.service` runs the web transport on `127.0.0.1:8787` — that host is
not a default the unit may relax — and `ummanu-web-front.service` runs the distribution's Caddy that
terminates TLS, checks the owner's password and proxies to it. The front is `PartOf` the transport,
so the pair starts, stops and restarts together, and its configuration is not a template here: it
carries a bcrypt hash and is rendered from the secret store by `ummanu web-front render` into
`<data-dir>/webfront/Caddyfile` with mode 0600. The distribution's own `caddy.service` is masked on
this installation so that installing the package can never start an unconfigured public listener;
see [Operations](../../docs/OPERATIONS.md#the-published-web-front).
`ummanu-po.service` runs `ummanu po-serve`, the one owner of PO head turns: every turn process is
a child in its control group, it takes messages from the durable queue `<data-dir>/po-queue/` and
listens on the Unix socket `<data-dir>/po-service/po.sock` (mode 0600) for the web and, later, the
dispatcher. It has no `PartOf=`/`BindsTo=` coupling to the web, so a web restart touches no turn.
`ummanu upgrade` never restarts it while a turn runs: it asks, and the service exits by itself
(`Restart=always` brings it back on the new code) once no turn runs; see
[Operations](../../docs/OPERATIONS.md#the-po-service). Scheduler-backed roles must have exactly one
owner: the systemd timer here.

`ummanu-checkpoint.timer` starts `ummanu checkpoint-run` 30 seconds after boot and every minute
from the previous activation, including during freeze and drain. The coordinator retains its
five-minute preparation and thirty-minute push windows. A minute divides the preparation window;
a four-minute trigger would skip the second activation and stretch cuts to eight minutes. With
roughly 50-second runs, steady-state cuts finish every five minutes. Accuracy is one second and
there is no randomized delay. The monotonic timer resumes promptly on boot without requiring a
persisted calendar trigger. CPU batch scheduling, Nice=10 and best-effort I/O priority 7 yield to
the dispatcher. The service's 3600-second timeout exceeds the existing per-command Git and push
bounds; hung work still becomes a failed unit observable by doctor.

The checkpoint owns `dispatcher/checkpoint.lock` and atomically replaces
`dispatcher/checkpoint-state.json`. It takes neither the tick lock nor `cleanup.lock`. Readers
and the first run use legacy production-state checkpoint records only while the new file is
absent. The board projection holds `board-bulk.lock` shared during its SQL read-only REPEATABLE
READ cut, including metadata, comments, audit and sprints; publication and Git run after release.
The instance repository writer lock continues to protect commit publication. The component is
enabled when omitted, so upgrade installs both units for existing instances.
