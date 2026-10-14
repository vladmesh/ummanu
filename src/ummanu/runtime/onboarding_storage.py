"""Onboarding generated-state paths, independent of scanning and provisioning."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# The logical homes recorded in `ownership.adapter.storage`: generated drafts in the data
# directory, canonical adapters in the live root.
DRAFT_STORAGE = "<data>/onboarding/adapter-drafts/<project>.yaml"
ADAPTER_STORAGE = "<instance>/adapters/<project>.yaml"


@dataclass(frozen=True)
class OnboardingStorage:
    """Where onboarding, provision and the gate keep their generated state: the data directory.

    Drafts, provision runs, gate runs and compatibility manifests live under `<data>/onboarding/`,
    the per-project locks under `<data>/locks/onboarding/`. All of it is re-creatable by onboarding,
    never exported and never restored (docs/RECOVERY.md), so nothing of it belongs in the live root.
    This is the one place these paths are spelled.
    """

    data_dir: Path

    @classmethod
    def for_instance(cls, instance: Path) -> OnboardingStorage:
        """The storage of the instance at ``instance`` (its directory or `instance.yaml`).

        Raises :class:`ummanu.config.DataDirError` when the instance names no usable data directory.
        """
        from ummanu.config import generated_state_dir

        return cls(generated_state_dir(instance))

    @property
    def root(self) -> Path:
        return self.data_dir / "onboarding"

    @property
    def drafts(self) -> Path:
        return self.root / "adapter-drafts"

    def draft(self, project_id: str) -> Path:
        return self.drafts / f"{project_id}.yaml"

    def provision_runs(self, project_id: str) -> Path:
        return self.root / "provision-runs" / project_id

    def gate_runs(self, project_id: str) -> Path:
        return self.root / "gate-runs" / project_id

    @property
    def compatibility_manifests(self) -> Path:
        return self.root / "compatibility-manifests"

    def lock(self, project_id: str) -> Path:
        """The lock onboarding, provision and the gate serialize their draft writes on."""
        return self.data_dir / "locks" / "onboarding" / f"{project_id}.lock"
