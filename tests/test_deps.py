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
                         "bc250-acpi-fix", "cyan-skillfish-governor", "umr"]:
            self.assertIn(expected, names)

    def test_governor_is_package_type(self):
        """Il governor è un pacchetto distro (COPR/AUR), non più solo
        istruzioni: BUO lo installa da solo col package manager."""
        gov = next(d for d in DEPS if d["name"] == "cyan-skillfish-governor")
        self.assertEqual(gov["type"], "package")
        self.assertIn("fedora", gov["pkg_map"])
        self.assertIn("bazzite", gov["pkg_map"])
        self.assertEqual(gov["copr"], "filippor/bazzite")
        self.assertTrue(gov["aur"])

    def test_umr_is_package_only_ostree(self):
        """umr è richiesto solo per il runtime UMR (ostree)."""
        umr = next(d for d in DEPS if d["name"] == "umr")
        self.assertEqual(umr["type"], "package")
        self.assertTrue(umr["only_ostree"])
        self.assertIn("umr", umr["check_bins"])

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

    # ---------------------- package (governor/umr) ---------------------- #

    def test_check_package_present_when_binary_found(self):
        """Package presente se il binario è nel PATH."""
        dep = {"name": "umr", "type": "package", "check_bins": ["umr"],
               "required_for": "runtime UMR"}
        original = deps_module.which
        deps_module.which = lambda tool: "/usr/bin/umr"
        try:
            st = self.manager._check_one(dep)
            self.assertTrue(st["present"])
        finally:
            deps_module.which = original

    def test_check_package_missing_when_binary_absent(self):
        dep = {"name": "umr", "type": "package", "check_bins": ["umr"],
               "required_for": "runtime UMR"}
        original = deps_module.which
        deps_module.which = lambda tool: None
        try:
            st = self.manager._check_one(dep)
            self.assertFalse(st["present"])
            self.assertIn("umr", st["missing"])
        finally:
            deps_module.which = original

    def test_check_package_skipped_on_non_ostree_when_only_ostree(self):
        """only_ostree su un sistema non-ostree → presente (non serve)."""
        import unittest.mock as mock
        dep = {"name": "umr", "type": "package", "check_bins": ["umr"],
               "only_ostree": True, "required_for": "runtime UMR"}
        with mock.patch("os.path.exists", return_value=False):
            st = self.manager._check_one(dep)
        self.assertTrue(st["present"])
        self.assertEqual(st["missing"], [])
    def test_install_package_fedora_governor_uses_copr(self):
        """Fedora: abilita il COPR filippor/bazzite poi dnf install."""
        import unittest.mock as mock
        dep = {"name": "cyan-skillfish-governor", "type": "package",
               "pkg_map": {"fedora": "cyan-skillfish-governor-smu"},
               "copr": "filippor/bazzite"}
        calls = []

        class FakeDistro:
            id = "fedora"
            pkg_manager = "dnf"

            def install_package(self, pkg, sudo=True):
                calls.append(("install", pkg))
                return (0, "", "")

        def fake_run(cmd, **kw):
            calls.append(("cmd", cmd[0], cmd[1]))
            return (0, "", "")

        with mock.patch("buo.utils.distro.detect_distro",
                        return_value=FakeDistro()), \
             mock.patch("buo.install.deps.run_command", side_effect=fake_run):
            res = self.manager._install_package(dep, sudo=False)

        self.assertEqual(res["status"], "ok")
        self.assertFalse(res.get("needs_reboot"))
        self.assertIn(("cmd", "dnf", "copr"), calls)
        self.assertIn(("install", "cyan-skillfish-governor-smu"), calls)

    def test_install_package_ostree_needs_reboot(self):
        """Bazzite/ostree: rpm-ostree install → attivo al prossimo reboot."""
        import unittest.mock as mock
        dep = {"name": "umr", "type": "package",
               "pkg_map": {"bazzite": "umr"}}
        calls = []

        class FakeDistro:
            id = "bazzite"
            pkg_manager = "rpm-ostree"

            def install_package(self, pkg, sudo=True):
                calls.append(("install", pkg))
                return (0, "", "")

        with mock.patch("buo.utils.distro.detect_distro",
                        return_value=FakeDistro()):
            res = self.manager._install_package(dep, sudo=False)

        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["needs_reboot"],
                        "rpm-ostree install richiede un reboot")
        self.assertIn(("install", "umr"), calls)

    def test_install_governor_bazzite_enables_copr_before_ostree(self):
        """Bazzite: COPR enable PRIMA di rpm-ostree install (il pacchetto
        vive nel COPR filippor/bazzite, serve il repo per risolverlo)."""
        import unittest.mock as mock
        dep = {"name": "cyan-skillfish-governor", "type": "package",
               "pkg_map": {"bazzite": "cyan-skillfish-governor-smu"},
               "copr": "filippor/bazzite"}
        calls = []

        class FakeDistro:
            id = "bazzite"
            pkg_manager = "rpm-ostree"

            def install_package(self, pkg, sudo=True):
                calls.append(("install", pkg))
                return (0, "", "")

        def fake_run(cmd, **kw):
            calls.append(("cmd", cmd[0], cmd[1]))
            return (0, "", "")

        with mock.patch("buo.utils.distro.detect_distro",
                        return_value=FakeDistro()), \
             mock.patch("buo.install.deps.run_command", side_effect=fake_run):
            res = self.manager._install_package(dep, sudo=False)

        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["needs_reboot"])
        # COPR enable prima di rpm-ostree install
        copr_idx = calls.index(("cmd", "dnf", "copr"))
        install_idx = calls.index(("install", "cyan-skillfish-governor-smu"))
        self.assertLess(copr_idx, install_idx)

    def test_install_package_arch_requires_aur_helper(self):
        """Arch: senza yay/paru → istruzioni chiare (failed, non crash)."""
        import unittest.mock as mock
        dep = {"name": "cyan-skillfish-governor", "type": "package",
               "pkg_map": {"arch": "cyan-skillfish-governor-smu"},
               "aur": True}

        class FakeDistro:
            id = "arch"
            pkg_manager = "pacman"

        with mock.patch("buo.utils.distro.detect_distro",
                        return_value=FakeDistro()), \
             mock.patch("buo.install.deps.which", return_value=None):
            res = self.manager._install_package(dep, sudo=False)

        self.assertEqual(res["status"], "failed")
        self.assertIn("yay", res["detail"])


if __name__ == "__main__":
    unittest.main()
