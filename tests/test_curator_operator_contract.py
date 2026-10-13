"""The manual curator-routing boundary is an operator contract, not product policy."""

from __future__ import annotations

import unittest

from ummanu.runtime.heads import HEADS_TOML, load_registry, role_head


class CuratorOperatorContractTests(unittest.TestCase):
    def test_the_shipped_registry_keeps_installation_policy_out(self) -> None:
        # secretary-1697: the curator is routed by role_defaults onto one of the pipeline tiers the
        # shipped registry declares; the installation repoints it in its own canon.
        registry = load_registry(HEADS_TOML)
        curator = role_head("curator", registry)
        self.assertEqual(registry.resolve(curator), curator)
        self.assertIn("adapter", registry.profile(curator))

