"""Advancing the Ummanu production checkout: the target's board schema first, then the code.

Both release paths of `CommandHostRuntime.complete_green` end by fast-forwarding a project's
checkout: the push path to the integration base it pushed, the GitHub path to the default branch
after `gh pr merge`. When that checkout is the production checkout the dispatcher itself runs from
(`production_runtime.product_root`), moving it activates the merged code for every later tick, so
both paths reach :func:`advance`, which keeps one order:

1. pin the target: the ref just fetched is resolved once to a full commit id, and everything after
   names that id, never the ref (which another merge may move);
2. refuse a checkout that cannot fast-forward to it before touching the board, as the plain
   ``merge --ff-only`` it replaces would have;
3. refuse a target that does not keep the entrypoint the live units execute
   (`dispatch.entrypoint_guard`, reason ``entrypoint_moved``): it lands on the remote, and only the
   installation operation guidance (`docs/OPERATIONS.md` §Entrypoint activation) activates it;
4. bring the board store to the target's schema (`board.release_migrations.prepare`: the target's
   own migration bundle, additive revisions only, under the migration advisory lock, verified at
   the target's head);
5. only then ``merge --ff-only <target>``, and read ``HEAD`` back to confirm it is the target.

A refused entrypoint or schema raises `ProductionActivationRefused` and the checkout stays where it was; the
release turns it into a Blocked card, a typed reason and one operation card for the sprint's PO
(`dispatch.release_lifecycle`). Every other failure is the `HostError` the plain fast-forward
raised before, so each path keeps its own handling of it. A replay is safe at every step: the
owed revisions are read under the lock, so a revision applied before an interrupted tick is not
applied again, and the checkout still moves only through this function.

Checkouts other than the production one (other projects, a checkout of another product root) keep
their plain fast-forward (`CommandHostRuntime._advance_checkout`).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ummanu.board import release_migrations
from ummanu.board.release_migrations import ReleaseSchemaRefused
from ummanu.dispatch.entrypoint_guard import EntrypointMoved, require_entrypoint
from ummanu.dispatch.types import HostError, MergeLanding


class ProductionActivationRefused(HostError):
    """The production checkout was not advanced: the target's entrypoint or board schema was refused.

    `refusal` is the `EntrypointMoved` or `ReleaseSchemaRefused` behind it; its `facts()` carry the
    `code`, `reason`, `target`, `revision` and `message` every consumer reads. `landing` is what the release had already delivered to the remote before this (the pushed or
    merged commit), set by `complete_green`: the remote merge happened, the production activation
    did not, and the two are reported apart.
    """

    def __init__(self, refusal: ReleaseSchemaRefused | EntrypointMoved, *, checkout: Path | str, old: str) -> None:
        self.refusal = refusal
        self.checkout = str(checkout)
        self.old = old
        self.target = refusal.target
        self.landing: MergeLanding | None = None
        super().__init__(
            f"production checkout {self.checkout} stays at {old[:12]}, not {refusal.target[:12]}: "
            f"{refusal.code} ({refusal.reason}): {refusal}"
        )

    def facts(self) -> dict[str, Any]:
        landing = self.landing
        return {
            **self.refusal.facts(),
            "checkout": self.checkout,
            "old": self.old,
            "remote_merge": (
                {"sha": landing.sha, "base": landing.base, "path": landing.path, "branch": landing.branch}
                if landing is not None
                else None
            ),
        }


def advance(
    run: Callable[..., Any],
    repo: Path,
    ref: str,
    *,
    instance_dir: Path | str,
) -> str:
    """Move the production checkout `repo` to `ref`'s commit, its board schema first. Returns the commit.

    `run` is the host's command runner (`CommandHostRuntime._run`): it raises `HostError` on failure.
    """
    target = run(
        ["git", "-C", str(repo), "rev-parse", "--verify", f"{ref}^{{commit}}"], "post-merge target"
    ).stdout.strip()
    old = run(["git", "-C", str(repo), "rev-parse", "HEAD"], "post-merge checkout head").stdout.strip()
    if old != target:
        try:
            run(["git", "-C", str(repo), "merge-base", "--is-ancestor", old, target], "post-merge ancestry")
        except HostError:
            raise HostError(
                f"post-merge fast-forward failed: the checkout at {old[:12]} is not an ancestor of "
                f"{ref} at {target[:12]}"
            ) from None

        def probe(args: list[str]) -> str | None:
            try:
                return str(run(["git", "-C", str(repo), *args], "post-merge entrypoint guard").stdout)
            except HostError:
                return None

        try:
            require_entrypoint(probe, target)
        except EntrypointMoved as exc:
            raise ProductionActivationRefused(exc, checkout=repo, old=old) from exc
    try:
        release_migrations.prepare(repo, target, instance_dir)
    except ReleaseSchemaRefused as exc:
        raise ProductionActivationRefused(exc, checkout=repo, old=old) from exc
    run(["git", "-C", str(repo), "merge", "--ff-only", target], "post-merge fast-forward")
    moved = run(["git", "-C", str(repo), "rev-parse", "HEAD"], "post-merge checkout head").stdout.strip()
    if moved != target:
        raise HostError(f"post-merge fast-forward left the checkout at {moved[:12]}, not {target[:12]}")
    return target


__all__ = ["ProductionActivationRefused", "advance"]
