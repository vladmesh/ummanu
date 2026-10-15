"""Doctor findings for stored machine identifiers outside the shared grammar; nothing is renamed."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from ummanu.kernel.identifiers import is_machine_id, machine_id_refusal
from ummanu.memory.canon import fact_files
from ummanu.state_repo import memory_facts_dir

MACHINE_ID_INVALID = "machine_id_invalid"


def stored_identifier_findings(instance_dir: Path) -> list[dict[str, Any]]:
    """One finding per adapter file and memory scope directory whose name breaks the grammar."""
    findings = []
    adapters = instance_dir / "adapters"
    for path in sorted(adapters.glob("*.yaml")) if adapters.is_dir() else []:
        if not is_machine_id(path.stem):
            findings.append(_finding("adapter id", path.stem, path, ""))
    facts_dir = memory_facts_dir(instance_dir)
    scopes = Counter(fact_id.split("/", 1)[0] for fact_id, _path in fact_files(facts_dir) if "/" in fact_id)
    for scope, count in sorted(scopes.items()):
        if not is_machine_id(scope):
            hint = f" ({count} facts; move them with `ummanu memory move-scope --from {scope} --to SCOPE`)"
            findings.append(_finding("memory scope", scope, facts_dir / scope, hint))
    return findings


def _finding(field: str, name: str, source: Path, hint: str) -> dict[str, Any]:
    return {
        "code": MACHINE_ID_INVALID,
        "field": field,
        "name": name,
        "source": str(source),
        "message": f"{machine_id_refusal(field, name)} at {source}{hint}",
    }
