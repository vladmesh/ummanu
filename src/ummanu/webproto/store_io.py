"""The one place `ummanu.webproto` puts a file on disk, and the vocabulary it speaks.

`write_text_atomic` raises `RuntimeError`, which is deliberately absent from
:data:`~ummanu.webproto.boundary.IMPLEMENTATION_FAILURES` (it is how a layer defect travels). So
every write in this package goes through :func:`write_document`, which converts that into
`RunStoreError`. `write_text_atomic` itself is unchanged: other callers catch its `RuntimeError`.
"""

from __future__ import annotations

import os
from pathlib import Path

from ummanu._fsutil import write_text_atomic


class RunStoreError(RuntimeError):
    """A durable store of this layer could not be read or written. Never a statement about a run.

    Re-exported by :mod:`ummanu.webproto.runs`; there is exactly one class.
    """


def write_document(path: Path | str, payload: str) -> None:
    """Write one of this layer's durable documents atomically, or raise `RunStoreError`.

    Any `RuntimeError` or `OSError` from `write_text_atomic` becomes `RunStoreError`, which the
    boundary maps to `backend_unavailable`.
    """
    target = Path(os.fspath(path))
    try:
        write_text_atomic(target, payload)
    except RunStoreError:
        raise
    except (RuntimeError, OSError) as exc:
        raise RunStoreError(f"the record at {target} could not be written: {exc}") from None
