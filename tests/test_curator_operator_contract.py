"""The manual curator-routing boundary is an operator contract, not product policy."""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CuratorOperatorContractTests(unittest.TestCase):
    def test_the_shipped_registry_keeps_installation_policy_out(self) -> None:
        shipped = ROOT / "src" / "ummanu" / "runtime" / "heads.toml"
        canon = tomllib.loads(shipped.read_text(encoding="utf-8"))

        # secretary-1697: the curator is routed by role_defaults onto one of the five pipeline
        # tiers; no curator-specific profile ships. The tiers pin their models (the owner's rule for
        # the portable registry since then), so model pins are no longer asserted absent here.
        self.assertFalse(any("curator" in pid for pid in canon["profiles"]))
        self.assertEqual(
            {
                resource.get("account")
                for resource in canon["resources"].values()
            },
            {"claude-subscription", "openai-subscription"},
        )
