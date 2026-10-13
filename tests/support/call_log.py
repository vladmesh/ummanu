"""One ordered log of the collaborators a behaviour calls, for tests that assert call order.

A test patches each collaborator with a recorder from the same log, runs the behaviour, and
compares `log.names` with the order the behaviour promises. The order is then observed while the
code runs, so a rename or a reshuffle of the source text cannot pass or fail it by itself.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest import mock


class CallLog:
    def __init__(self) -> None:
        self.names: list[str] = []

    def recorder(self, name: str, result: Any = None, *, wraps: Callable[..., Any] | None = None) -> Callable[..., Any]:
        """A callable that appends `name` and then answers `wraps(...)`, or `result` without one."""

        def record(*args: Any, **kwargs: Any) -> Any:
            self.names.append(name)
            return wraps(*args, **kwargs) if wraps is not None else result

        return record

    def patch(self, owner: Any, attribute: str, result: Any = None, *, name: str = "", wraps: bool = False) -> Any:
        """`mock.patch.object` of `owner.attribute` with a recorder named `name` (the attribute by default).

        With `wraps=True` the original still runs after the call is recorded.
        """
        original = getattr(owner, attribute) if wraps else None
        return mock.patch.object(owner, attribute, self.recorder(name or attribute, result, wraps=original))
