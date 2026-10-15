"""The Codex trust preflight never writes outside the directory the test run owns.

secretary-1173 made every Codex head interactive, and an interactive Codex asks about directory
trust before it takes a prompt. The product answers that question by writing `config.toml` in the
`CODEX_HOME` the head will run with, before the pane exists — so from that card on, *any* test that
reaches a worker, reviewer or service bring-up performs a write, whether or not it thought about
trust. With no `codex_home` in the fixture registry and no `UMMANU_CODEX_HOME` in the environment, that
write lands in the installation's `<data_dir>/codex-home/config.toml` wherever `UMMANU_DATA_DIR`
names one (the legacy `~/.config/orca/...` home until secretary-1723): installation state shared
by every Codex head on the host, where a permanent `trusted` grant for a since-deleted `/tmp`
workspace would accumulate one entry per suite run with nothing to prune it.

`tests/__init__.py` closes that by claiming one throwaway `UMMANU_CODEX_HOME` for the whole run before
any test module is imported. These tests prove the default is actually installed, that an ordinary
worker/reviewer bring-up cannot write outside the tree this run owns, and that a test wanting its
own home can still say so locally.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import _SUITE_CODEX_HOME
from ummanu.automations.agents.pipeline import codex_sessions as pipeline_codex_sessions
from ummanu.dispatch import tui as dispatcher_tui
from ummanu.dispatch.host import InstanceCatalog
from ummanu.runtime import codex_preflight

# A registry in the shape the offending dispatcher tests use: a Codex profile that says nothing
# about `codex_home`, because which home an installation runs its heads with is not something a
# launcher test has an opinion about.
REGISTRY = {
    "resources": {"openai-sub": {"account": "openai-subscription"}},
    "profiles": {
        "codex-hermetic": {"resource": "openai-sub", "adapter": "codex", "model": "gpt-5.6"},
    },
    "role_defaults": {"new_card": "codex-hermetic", "reviewer": "codex-hermetic"},
}


def _installation_homes() -> list[Path]:
    """The CODEX_HOMEs of this host's installation that a bring-up without the suite's seam could
    write into: the data-dir home of an ambient `UMMANU_DATA_DIR`, and the legacy Orca home the
    resolver fell back to before secretary-1723 (still installation state, if nobody writes it now).
    """
    homes = [Path.home() / ".config" / "orca" / "codex-runtime-home" / "home"]
    data_dir = os.environ.get("UMMANU_DATA_DIR")
    if data_dir:
        homes.append(Path(data_dir).expanduser() / codex_preflight.CODEX_HOME_DATA_DIRNAME)
    return homes


def _config_stat(path: Path) -> tuple[int, int] | None:
    """Enough of a file's identity to tell "untouched" from "rewritten", without reading it."""
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_size, info.st_mtime_ns)


class HermeticCodexHomeTests(unittest.TestCase):
    def test_the_suite_owns_the_codex_home_every_bring_up_writes_into(self) -> None:
        suite_home = Path(os.environ["UMMANU_CODEX_HOME"])

        self.assertEqual(suite_home, _SUITE_CODEX_HOME)
        self.assertTrue(suite_home.is_dir())
        # Not an installation's own home, and not anywhere underneath one.
        for installation_home in _installation_homes():
            self.assertNotEqual(suite_home, installation_home)
            self.assertFalse(suite_home.is_relative_to(installation_home))
        # Every reader resolves the seam at the call, so all of them put the suite's home first.
        # (Readers also scan the installation homes that exist, read-only, for live heads.)
        self.assertEqual(codex_preflight.codex_home({}), str(suite_home))
        with mock.patch.dict(os.environ):
            os.environ.pop("UMMANU_CODEX_SESSIONS", None)
            os.environ.pop("TA_CODEX_SESSIONS", None)
            self.assertEqual(dispatcher_tui._sessions_roots()[0], suite_home / "sessions")
            self.assertEqual(pipeline_codex_sessions.sessions_roots()[0], suite_home / "sessions")

    def test_a_worker_and_a_reviewer_bring_up_write_only_where_the_run_owns(self) -> None:
        """The regression proper: the two roles whose launch tests wrote into the live config.

        Nothing here patches `UMMANU_CODEX_HOME`. That is the point — these calls are exactly what an
        ordinary launcher test does, and they have to be safe without the test knowing that a Codex
        bring-up writes anything at all.
        """
        installation_configs = [home / codex_preflight.CODEX_CONFIG_FILE for home in _installation_homes()]
        before = [_config_stat(config) for config in installation_configs]
        written: list[Path] = []
        real_save = codex_preflight._save_codex_config

        def recording_save(config: Path, text: str) -> None:
            written.append(Path(config))
            real_save(config, text)

        with tempfile.TemporaryDirectory() as tmp:
            owned = Path(tmp)
            workspace = owned / "workspace"
            workspace.mkdir()
            catalog = object.__new__(InstanceCatalog)
            catalog._heads = REGISTRY  # type: ignore[attr-defined]

            with mock.patch.object(codex_preflight, "_save_codex_config", recording_save):
                for role in ("worker", "reviewer"):
                    with self.subTest(role=role):
                        self.assertTrue(catalog.head_launch(
                            "codex-hermetic",
                            "TASK.md",
                            workspace=str(workspace),
                            role=role,
                        ).command)

            # Not vacuous: these bring-ups really did answer the trust question.
            self.assertTrue(written)
            for config in written:
                with self.subTest(config=str(config)):
                    self.assertTrue(
                        config.is_relative_to(_SUITE_CODEX_HOME) or config.is_relative_to(owned),
                        f"codex bring-up wrote {config}, outside the directories this run owns",
                    )
            trusted = (_SUITE_CODEX_HOME / codex_preflight.CODEX_CONFIG_FILE).read_text(encoding="utf-8")
            self.assertIn(str(workspace.resolve()), trusted)
            self.assertIn('trust_level = "trusted"', trusted)

        self.assertEqual([_config_stat(config) for config in installation_configs], before)

    def test_a_test_can_still_own_its_codex_home_locally(self) -> None:
        """The escape hatch the rest of the suite already uses: a local patch shadows the default
        for the duration of the block, and a profile that pins a home wins over both."""
        with tempfile.TemporaryDirectory() as tmp:
            owned = Path(tmp)
            workspace = owned / "workspace"
            workspace.mkdir()
            local_home = owned / "local-home"
            pinned_home = owned / "pinned-home"
            catalog = object.__new__(InstanceCatalog)
            catalog._heads = {  # type: ignore[attr-defined]
                "profiles": {
                    "codex-hermetic": dict(REGISTRY["profiles"]["codex-hermetic"]),
                    "codex-pinned": {
                        **REGISTRY["profiles"]["codex-hermetic"],
                        "codex_home": str(pinned_home),
                    },
                },
            }

            with mock.patch.dict(os.environ, {"UMMANU_CODEX_HOME": str(local_home)}):
                catalog.prepare_head_workspace(  # type: ignore[attr-defined]
                    "codex-hermetic", str(workspace), role="worker"
                )
                catalog.prepare_head_workspace(  # type: ignore[attr-defined]
                    "codex-pinned", str(workspace), role="reviewer"
                )

            for home in (local_home, pinned_home):
                with self.subTest(home=home.name):
                    trusted = (home / codex_preflight.CODEX_CONFIG_FILE).read_text(encoding="utf-8")
                    self.assertIn(str(workspace.resolve()), trusted)
            # The suite default is back, undisturbed, for whatever runs next.
            self.assertEqual(os.environ["UMMANU_CODEX_HOME"], str(_SUITE_CODEX_HOME))


if __name__ == "__main__":
    unittest.main()
