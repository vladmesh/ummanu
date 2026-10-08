"""A production-runtime model for real-mode host tests.

Real-mode tests run from the candidate environment, but the production dispatcher does not.  The
host fixtures that cross a production provenance fence therefore need an explicit registered
origin instead of allowing ``CommandHostRuntime`` to infer production from the test interpreter.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ummanu.dispatch.runtime_provenance import RuntimeProvenance


@dataclass(frozen=True)
class RegisteredProductionRuntime:
    """Stable test double for a valid production interpreter and import origin."""

    interpreter: str
    product_root: str
    import_origin: str

    def probe(self, within=None) -> RuntimeProvenance:
        return RuntimeProvenance(
            classification="valid",
            interpreter=self.interpreter,
            product_root=self.product_root,
            import_origin=self.import_origin,
            metadata_targets=(),
        )


def registered_production_runtime(fixture_root: Path) -> RegisteredProductionRuntime:
    """Model production beside, rather than inside, a fixture's candidate workspace."""
    product_root = (fixture_root / "registered-production").resolve(strict=False)
    return RegisteredProductionRuntime(
        interpreter=str(product_root / ".venv" / "bin" / "python3"),
        product_root=str(product_root),
        import_origin=str(product_root / "src" / "ummanu" / "__init__.py"),
    )
