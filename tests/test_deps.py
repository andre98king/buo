#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test del DependencyManager (download delle repo della community)."""

import os
import tempfile
import unittest
from pathlib import Path

from buo.install import deps as deps_module
from buo.install.deps import DEPS, DependencyManager
from buo.utils.shell import which


class TestDeps(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._deps_tmp = tempfile.TemporaryDirectory()
        self._bin_tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_DEPS_DIR"] = self._deps_tmp.name
        self.manager = DependencyManager(bin_dir=self._bin_tmp.name)

    def tearDown(self):
        os.environ.pop("BUO_DEPS_DIR", None)
        self._tmp.cleanup()
        self._deps_tmp.cleanup()
        self._bin_tmp.cleanup()

    def test_catalog_is_complete(self):
        """Le repo note devono essere tutte nel catalogo."""
        names = [d["name"] for d in DEPS]
        for expected in ["bc250_smu_oc", "bc250-40cu-unlock",
                         "bc250-acpi-fix", "cyan-skillfish-governor"]:
            self.assertIn(expected, names)

    def test_check_structure(self):
        status = self.manager.check()
        self.assertEqual(set(status.keys()), {d["name"] for d in DEPS})
        for name, st in status.items():
            self.assertIn("present", st)
            self.assertIn("required_for", st)

    def test_check_honors_filter(self):
        status = self.manager.check(deps=["bc250_smu_oc"])
        self.assertEqual(list(status.keys()), ["bc250_smu_oc"])

    def test_install_graceful_without_git(self):
        original = deps_module.which
        deps_module.which = lambda tool: None
        try:
            result = self.manager.install()
            self.assertIn("_error", result)
        finally:
            deps_module.which = original

    def test_real_install_into_temp(self):
        """Integrazione: clone reale in cartelle temporanee."""
        if which("git") is None:
            self.skipTest("git non installato")

        result = self.manager.install(deps=["bc250_smu_oc"])
        entry = result.get("bc250_smu_oc", {})
        if entry.get("status") == "failed":
            self.skipTest(f"nessuna rete: {entry.get('detail', '')}")

        self.assertEqual(entry["status"], "ok")
        bin_dir = Path(self._bin_tmp.name)
        self.assertTrue((bin_dir / "bc250-detect").exists())
        self.assertTrue((bin_dir / "bc250-apply").exists())
        # libreria e helper necessari a bc250-detect (bug trovato sul campo)
        self.assertTrue((bin_dir / "bc250_smu" / "api.py").exists())
        self.assertTrue((bin_dir / "stress_helper.py").exists())

    def test_stress_wrapper_created_when_missing(self):
        """Se manca `stress` ma c'è `stress-ng`, viene creato il wrapper."""
        original_stress = deps_module.which
        deps_module.which = lambda tool: (
            "/usr/bin/stress-ng" if tool == "stress-ng" else None)
        try:
            status = self.manager._ensure_stress(sudo=False)
            self.assertEqual(status["status"], "ok")
            wrapper = Path(self._bin_tmp.name) / "stress"
            self.assertTrue(wrapper.exists())
            self.assertIn("stress-ng", wrapper.read_text())
        finally:
            deps_module.which = original_stress

    def test_stress_error_when_neither_available(self):
        original_stress = deps_module.which
        deps_module.which = lambda tool: None
        try:
            status = self.manager._ensure_stress(sudo=False)
            self.assertEqual(status["status"], "failed")
        finally:
            deps_module.which = original_stress

    def test_check_after_install(self):
        """Dopo l'installazione, check() rileva la dipendenza presente."""
        if which("git") is None:
            self.skipTest("git non installato")
        result = self.manager.install(deps=["bc250_smu_oc"])
        if result.get("bc250_smu_oc", {}).get("status") == "failed":
            self.skipTest("nessuna rete")

        status = self.manager.check(deps=["bc250_smu_oc"])
        self.assertTrue(status["bc250_smu_oc"]["present"])


if __name__ == "__main__":
    unittest.main()
