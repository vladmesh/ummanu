"""The one place this layer's error contract is enforced.

What leaves an operation is a typed protocol code, never a source's implementation exception, so a
transport keeps one code table and one `ReadError` catch. A class inheriting
:class:`ProtocolBoundary` has every public method wrapped at class creation, so a new public
operation is guarded with nothing to remember. Only :data:`IMPLEMENTATION_FAILURES` are translated
(to `backend_unavailable`); a `TypeError` or `AttributeError` is a layer defect and travels as
itself. `OSError` also covers a head backend that cannot bring a supervisor up in `run_start`.
See docs/PROTOCOLS.md "Errors" under "Reading the pipeline".
"""

from __future__ import annotations

import functools
import inspect
import json
from typing import Any, Callable, TypeVar

from ummanu.webproto.errors import ReadError, RuntimeUnavailable
from ummanu.webproto.store_io import RunStoreError

#: The durable sources' own vocabularies, each meaning "a source refused": `RunStoreError` (every
#: write goes through :func:`ummanu.webproto.store_io.write_document`, which converts the atomic
#: writer's `RuntimeError`), and `OSError` / `json.JSONDecodeError` for any file under `<data>/`.
#: `RuntimeError` itself is deliberately absent: it would make a layer defect indistinguishable
#: from a full disk.
IMPLEMENTATION_FAILURES: tuple[type[BaseException], ...] = (RunStoreError, OSError, json.JSONDecodeError)

#: Set on a wrapped operation, so a test can tell guarded from unguarded and wrapping twice is a no-op.
GUARDED = "__webproto_guarded__"

_Function = TypeVar("_Function", bound=Callable[..., Any])


def guard(function: _Function) -> _Function:
    """`function`, with this layer's failure contract around it.

    A `ReadError` passes through untouched; an implementation failure becomes `RuntimeUnavailable`,
    chained to what raised it.
    """
    if getattr(function, GUARDED, False):
        return function

    @functools.wraps(function)
    def operation(*args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except ReadError:
            raise
        except IMPLEMENTATION_FAILURES as exc:
            raise RuntimeUnavailable(
                f"{function.__name__!r} could not be answered: {exc}"
            ) from exc

    setattr(operation, GUARDED, True)
    return operation  # type: ignore[return-value]


def operations(cls: type) -> tuple[str, ...]:
    """The public operations of a boundary class, in definition order (the wrapping's predicate)."""
    return tuple(
        name
        for name, attribute in vars(cls).items()
        if not name.startswith("_") and inspect.isfunction(attribute)
    )


class ProtocolBoundary:
    """A layer whose public methods are protocol operations, guarded by being public.

    Private helpers are inside the boundary and untouched, so they may still catch and act on a
    `RunStoreError`, as `RunLifecycle` does.
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for name in operations(cls):
            setattr(cls, name, guard(vars(cls)[name]))
