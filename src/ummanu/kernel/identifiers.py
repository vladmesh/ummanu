"""The one grammar for a machine identifier that becomes part of a path, branch, unit or ref.

Project, product and adapter ids, card slugs, role and profile names and memory project scopes
all use it. Titles and descriptions are free text and never pass through here.
"""

from __future__ import annotations

import re

MACHINE_ID_GRAMMAR = "[a-z0-9][a-z0-9-]*"
_MACHINE_ID = re.compile(MACHINE_ID_GRAMMAR + r"\Z")


class MachineIdError(ValueError):
    """A machine identifier does not match `MACHINE_ID_GRAMMAR`; the message names the field."""


def is_machine_id(value: object) -> bool:
    return isinstance(value, str) and _MACHINE_ID.match(value) is not None


def machine_id_refusal(field: str, value: object) -> str:
    """The refusal text for `value` as `field`: the field, the grammar and an ASCII example."""
    return (
        f"{field} {value!r} must match {MACHINE_ID_GRAMMAR} "
        "(lowercase ASCII letters, digits and hyphens, e.g. 'codegen-orchestrator')"
    )


def require_machine_id(field: str, value: object) -> str:
    """`value` unchanged when it matches the grammar, otherwise MachineIdError; never normalized."""
    if isinstance(value, str) and _MACHINE_ID.match(value):
        return value
    raise MachineIdError(machine_id_refusal(field, value))
