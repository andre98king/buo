#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test dell'auto-download: "l'utente avvia buo e buo scarica tutto".

Verifica:
    1. default config: auto_install attivo
    2. mock/dry-run: MAI download
    3. auto_install=false: istruzioni, nessun download
    4. download impossibile (git mancante) → fail-closed (ConfigurationError)
    5. tool già presenti → nessun download
"""

import os
import tempfile
import unittest

from buo.config import BUOConfig
from buo.exceptions import ConfigurationError
from buo.orchestrator import Orchestrator
from buo.install import deps as deps_module


class TestAutoDeps(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        os.environ.pop("BUO_DEPS_DIR", None)
        self._tmp.cleanup()

    def _orchestrator(self, mock=False, dry_run=False, auto_install=True):
        cfg = BUOConfig({"deps": {"auto_install": auto_install}})
        return Orchestrator(config=cfg, mock=mock, dry_run=dry_run)

    # ---------------------------------------------------------------- #

    def test_config_default_auto_install(self):
        self.assertTrue(BUOConfig().deps_auto_install)
        self.assertFalse(BUOConfig().deps_auto_install_governor)
        cfg = BUOConfig({"deps": {"auto_install": False}})
        self.assertFalse(cfg.deps_auto_install)

    def test_no_download_in_mock(self):
        orch = self._orchestrator(mock=True)
        orch._ensure_dependencies()  # non deve né scaricare né sollevare

    def test_no_download_in_dry_run(self):
        orch = self._orchestrator(mock=False, dry_run=True)
        orch._ensure_dependencies()

    def test_disabled_means_no_download(self):
        """auto_install=false: nessun download anche se manca git."""
        orch = self._orchestrator(auto_install=False)
        original = deps_module.which
        deps_module.which = lambda tool: None
        try:
            orch._ensure_dependencies()  # non deve sollevare
        finally:
            deps_module.which = original

    def test_fail_closed_without_git(self):
        """git mancante + tool assenti → ConfigurationError (fail-closed)."""
        orch = self._orchestrator()
        original = deps_module.which
        deps_module.which = lambda tool: None
        try:
            with self.assertRaises(ConfigurationError):
                orch._ensure_dependencies()
        finally:
            deps_module.which = original

    def test_all_present_no_download(self):
        """Tool già presenti → nessun install."""
        orch = self._orchestrator()
        original_check = deps_module.DependencyManager.check
        original_install = deps_module.DependencyManager.install

        def fake_check(self, deps=None):
            return {d["name"]: {"present": True, "type": d["type"]}
                    for d in deps_module.DEPS}

        def fake_install(self, deps=None, sudo=True):
            raise AssertionError("install() non deve essere chiamato")

        deps_module.DependencyManager.check = fake_check
        deps_module.DependencyManager.install = fake_install
        try:
            orch._ensure_dependencies()  # non deve sollevare
        finally:
            deps_module.DependencyManager.check = original_check
            deps_module.DependencyManager.install = original_install


if __name__ == "__main__":
    unittest.main()
