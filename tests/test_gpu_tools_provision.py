#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Auto-provvigionamento del tool GPU (design
research/DESIGN_AUTOPROVISION_GPU_TOOLS.md, sezione 7 — scenari 1-7):
unit del servizio `ensure_vkmark` con distro e runner finti — nessun
subprocess, nessun hardware reale (C1).
"""

import unittest
from unittest import mock

from buo.install.gpu_tools import ensure_vkmark


class _FakeDistro:
    """Distro finta: pkg_manager + install_package programmabili."""

    def __init__(self, pkg_manager="dnf", rc=0, out="", err=""):
        self.pkg_manager = pkg_manager
        self.rc = rc
        self.out = out
        self.err = err
        self.calls = []   # [(pkg, sudo)]

    def install_package(self, pkg, sudo=True):
        self.calls.append((pkg, sudo))
        return self.rc, self.out, self.err


def _vkmark_present():
    return mock.patch("buo.install.gpu_tools.shutil.which",
                      return_value="/usr/bin/vkmark")


def _vkmark_absent():
    return mock.patch("buo.install.gpu_tools.shutil.which",
                      return_value=None)


class TestEnsureVkmark(unittest.TestCase):
    """Scenari 1-7 del design §7 + robustezza (mai eccezioni non gestite)."""

    def test_1_already_present_no_runner_no_distro(self):
        """#1: vkmark già presente → ok, installed False, NESSUN runner
        chiamato (idempotenza: nessuna installazione)."""
        distro = _FakeDistro()
        txn = mock.Mock()
        install = mock.Mock()
        with _vkmark_present():
            res = ensure_vkmark(distro=distro, txn_runner=txn,
                                install_runner=install)
        self.assertEqual(res["status"], "ok")
        self.assertFalse(res["installed"])
        self.assertFalse(res["needs_reboot"])
        txn.assert_not_called()
        install.assert_not_called()
        self.assertEqual(distro.calls, [])

    def test_2_ostree_txn_ok_staged(self):
        """#2: ostree (rpm-ostree) + txn ok → ok, needs_reboot True
        (layering staged, attivo al prossimo reboot)."""
        calls = []

        def txn(cmd, unit):
            calls.append((cmd, unit))
            return 0, "", ""

        distro = _FakeDistro(pkg_manager="rpm-ostree")
        with _vkmark_absent():
            res = ensure_vkmark(distro=distro, txn_runner=txn)
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["installed"])
        self.assertTrue(res["needs_reboot"])
        self.assertEqual(calls,
                         [(["rpm-ostree", "install", "vkmark"],
                           "buo-install-vkmark")])
        self.assertEqual(distro.calls, [], "ostree: mai dnf/apt/pacman")

    def test_3_txn_timeout_never_kill(self):
        """#3: timeout del CLIENT (rc 124) → failed con messaggio "non
        uccidere, il daemon completerà" (txn staccata MAI killata)."""
        def txn(cmd, unit):
            return 124, "", ""

        distro = _FakeDistro(pkg_manager="rpm-ostree")
        with _vkmark_absent():
            res = ensure_vkmark(distro=distro, txn_runner=txn)
        self.assertEqual(res["status"], "failed")
        self.assertFalse(res["needs_reboot"])
        self.assertIn("uccider", res["detail"])
        self.assertIn("completerà", res["detail"])

    def test_4_dnf_ok_active_immediately(self):
        """#4: non-ostree dnf ok → ok, needs_reboot False (attivo subito),
        install_package chiamato col pacchetto giusto."""
        distro = _FakeDistro(pkg_manager="dnf")
        with _vkmark_absent():
            res = ensure_vkmark(distro=distro)
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["installed"])
        self.assertFalse(res["needs_reboot"])
        self.assertEqual(distro.calls, [("vkmark", True)])

    def test_5_install_failed_detail_truncated(self):
        """#5: offline (install rc != 0) → failed + detail = stderr
        troncato (~200 char, mai stderr intero)."""
        distro = _FakeDistro(pkg_manager="apt", rc=100,
                             err="E: Failed to fetch " + "x" * 500)
        with _vkmark_absent():
            res = ensure_vkmark(distro=distro)
        self.assertEqual(res["status"], "failed")
        self.assertFalse(res["needs_reboot"])
        self.assertIn("Failed to fetch", res["detail"])
        self.assertLessEqual(len(res["detail"]), 200)

    def test_6_already_staged_no_double_staging(self):
        """#6: install già staged (stderr "already ... requested/layered")
        → ok, needs_reboot True, niente doppio staging (una sola txn)."""
        calls = []

        def txn(cmd, unit):
            calls.append(cmd)
            return 1, "", ("error: Package 'vkmark' is already requested "
                           "to be layered")

        distro = _FakeDistro(pkg_manager="rpm-ostree")
        with _vkmark_absent():
            res = ensure_vkmark(distro=distro, txn_runner=txn)
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["installed"])
        self.assertTrue(res["needs_reboot"])
        self.assertEqual(len(calls), 1)

    def test_7_package_not_found(self):
        """#7: pacchetto assente dal repo (dnf rc != 0) → failed + detail
        con l'errore del package manager."""
        distro = _FakeDistro(
            pkg_manager="dnf", rc=1,
            err="Error: Unable to find a match: vkmark")
        with _vkmark_absent():
            res = ensure_vkmark(distro=distro)
        self.assertEqual(res["status"], "failed")
        self.assertFalse(res["installed"])
        self.assertIn("find a match", res["detail"])

    def test_runner_exception_never_propagates(self):
        """Mai eccezioni non gestite: runner/distro rotti → failed pulito
        (un tool opzionale non deve MAI far crashare il chiamante)."""
        def boom_install(pkg, sudo=True):
            raise OSError("dnf crashato")

        distro = _FakeDistro(pkg_manager="dnf")
        with _vkmark_absent():
            res = ensure_vkmark(distro=distro, install_runner=boom_install)
        self.assertEqual(res["status"], "failed")
        self.assertIn("dnf crashato", res["detail"])

    def test_unknown_pkg_manager_fails_cleanly(self):
        """Package manager sconosciuto (install_package → rc 127) →
        failed pulito con il messaggio della distro."""
        distro = _FakeDistro(pkg_manager="unknown", rc=127,
                             err="nessun package manager per la distro")
        with _vkmark_absent():
            res = ensure_vkmark(distro=distro)
        self.assertEqual(res["status"], "failed")
        self.assertIn("nessun package manager", res["detail"])


if __name__ == "__main__":
    unittest.main()
