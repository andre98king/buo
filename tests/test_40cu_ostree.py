#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test 40-CU su ostree: runtime UMR (bc250-cu-live-manager).

Il kernel patch non funziona su ostree (/usr read-only): su ostree
GPU40CUUnlock deve usare il runtime UMR. Verifica parsing del wrapper
e selezione del metodo per distro.
"""

import os
import tempfile
import unittest
from unittest import mock

from buo.unlock.gpu import GPU40CUUnlock
from buo.unlock.wrappers.bc250_live_manager import BC250LiveManagerWrapper


class TestLiveManagerParse(unittest.TestCase):
    def test_status_40_40_full_die(self):
        """Output status con 40/40 routed → full_die=True."""
        out = (
            "  amdgpu     : bc250_cc_write_mode=not exposed, active_cu_number=24\n"
            "  | SE0.SH0 |  D+  |  D+  |  D+  |  S+  |  S+  | 0x1f | 0xffe00000 |  10/10 |\n"
            "  CUs active & routed  : 40/40\n"
        )
        w = BC250LiveManagerWrapper(script_path="/tmp/nonexistent")
        parsed = w.parse_output(out, "")
        self.assertTrue(parsed["full_die"])
        self.assertEqual(parsed["cu_routed"], 40)
        self.assertEqual(parsed["cu_total"], 40)

    def test_status_24_24_not_full(self):
        """Output status con 24/40 routed → full_die=False."""
        out = (
            "  | SE0.SH0 |  D+  |  D+  |  D+  |  --  |  --  | 0x07 | 0xffe00000 |   6/10 |\n"
            "  CUs active & routed  : 24/40\n"
        )
        w = BC250LiveManagerWrapper(script_path="/tmp/nonexistent")
        parsed = w.parse_output(out, "")
        self.assertFalse(parsed["full_die"])
        self.assertEqual(parsed["cu_routed"], 24)

    def test_enable_all_target_parsed(self):
        """Output 'enable all' → (40/40 CUs target) → cu_target=40."""
        out = "[ OK ] dispatch registers updated (40/40 CUs target)\n"
        w = BC250LiveManagerWrapper(script_path="/tmp/nonexistent")
        parsed = w.parse_output(out, "")
        self.assertEqual(parsed["cu_target"], 40)
        self.assertTrue(parsed["full_die"])


class TestGPUUnlockOstreeSelection(unittest.TestCase):
    def test_ostree_uses_live_manager(self):
        """Su ostree (ostree-booted presente) → wrapper = live manager."""
        with mock.patch("buo.unlock.gpu.os.path.exists", return_value=True):
            g = GPU40CUUnlock(mock=False, use_wrapper=True)
        self.assertTrue(g.is_ostree)
        self.assertIsInstance(g.wrapper, BC250LiveManagerWrapper)

    def test_non_ostree_uses_kernel_patch_wrapper(self):
        """Non-ostree → wrapper = bc250-enable-40cu (kernel patch)."""
        from buo.unlock.wrappers.bc250_40cu import BC25040CUWrapper
        with mock.patch("buo.unlock.gpu.os.path.exists", return_value=False):
            g = GPU40CUUnlock(mock=False, use_wrapper=True)
        self.assertFalse(g.is_ostree)
        self.assertIsInstance(g.wrapper, BC25040CUWrapper)

    def test_mock_has_no_wrapper(self):
        g = GPU40CUUnlock(mock=True, use_wrapper=True)
        self.assertIsNone(g.wrapper)


class _FakeLiveManager:
    """Wrapper finto: available=True, nessun subprocess reale."""

    available = True


class TestGPU40CuPersist(unittest.TestCase):
    """Persistenza 40-CU al boot (bug campo 05/09, root cause).

    Il vecchio flusso (install-service + write-service-table) SNAPSHOTTAVA
    la tabella WGP LIVE dello script: su macchina a 24-CU live (tabella
    0x07 dopo un reboot) il conf di boot veniva regredito a 24 CU. Ora
    persist() scrive il conf full-die (0x1f x4) DIRETTAMENTE e garantisce
    solo che il servizio di boot sia presente+enabled. Mai file reali in
    mock; esito {persisted, error} invariato.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conf = os.path.join(self._tmp.name,
                                 "bc250-cu-live-manager.conf")

    def tearDown(self):
        self._tmp.cleanup()

    def _unlock(self, mock_mode=False):
        g = GPU40CUUnlock(mock=mock_mode, use_wrapper=False)
        g.is_ostree = True
        if not mock_mode:
            g.wrapper = _FakeLiveManager()
        g.boot_conf_path = self.conf
        return g

    @staticmethod
    def _no_snapshot(calls):
        """Nessun comando che SNAPSHOTTA la tabella live né che applica
        la tabella (write-service-table / apply-service)."""
        return all("write-service-table" not in a and "apply-service" not in a
                   for c in calls for a in c)

    def test_persist_writes_full_die_conf_directly(self):
        """Conf 0x1f x4 scritto DIRETTAMENTE (contenuto esatto), nessuna
        chiamata a write-service-table/install-service (servizio già
        enabled → skip)."""
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd[:2] == ["systemctl", "is-enabled"]:
                return 0, "enabled", ""
            return 1, "", ""

        g = self._unlock()
        with mock.patch("buo.utils.shell.run_command", side_effect=fake_run):
            out = g.persist()
        self.assertTrue(out["persisted"])
        with open(self.conf) as fh:
            content = fh.read()
        self.assertEqual(
            content,
            "BC250_WGP_MASKS=0x1f,0x1f,0x1f,0x1f\n"
            "UMR_ASIC=cyan_skillfish.gfx1013\n")
        # unica azione: verifica servizio abilitato (skip) — MAI snapshot
        self.assertEqual(calls,
                         [["systemctl", "is-enabled",
                           "bc250-cu-live-manager"]])

    def test_persist_enables_existing_disabled_unit(self):
        """Unità presente ma disabilitata → systemctl enable (niente
        reinstall né snapshot)."""
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd[:2] == ["systemctl", "is-enabled"]:
                return 1, "disabled", ""
            if cmd[:2] == ["systemctl", "cat"]:
                return 0, "[Unit]", ""
            if cmd[:2] == ["systemctl", "enable"]:
                return 0, "", ""
            return 1, "", ""

        g = self._unlock()
        with mock.patch("buo.utils.shell.run_command", side_effect=fake_run):
            out = g.persist()
        self.assertTrue(out["persisted"])
        self.assertTrue(any(c[:2] == ["systemctl", "enable"] for c in calls))
        self.assertFalse(any("install-service" in a for c in calls for a in c))
        self.assertTrue(self._no_snapshot(calls))

    def test_persist_reinstalls_service_from_tmp_when_unit_missing(self):
        """Unità ASSENTE → install-service dalla COPIA in /tmp (quirk
        'same file' da /usr/local/bin, BUGS #24), mai da /usr/local/bin."""
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd[:2] == ["systemctl", "is-enabled"]:
                return 1, "not-found", ""
            if cmd[:2] == ["systemctl", "cat"]:
                return 1, "", "unit not found"
            if cmd[0].endswith("bc250-cu-live-manager"):
                return 0, "", ""
            return 1, "", ""

        g = self._unlock()
        with mock.patch("buo.utils.shell.run_command",
                        side_effect=fake_run), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch("shutil.copy2"), \
             mock.patch("os.remove"):
            out = g.persist()
        self.assertTrue(out["persisted"])
        installs = [c for c in calls if "install-service" in c]
        self.assertEqual(len(installs), 1)
        self.assertTrue(installs[0][0].startswith("/tmp/"),
                        "install-service dalla copia in /tmp, non da "
                        "/usr/local/bin")
        self.assertTrue(self._no_snapshot(calls))

    def test_persist_reports_error_when_service_install_fails(self):
        """Fallimento dell'abilitazione → persisted False + error (conf
        full-die comunque scritto, MAI snapshot)."""
        def fake_run(cmd, **kw):
            if cmd[:2] == ["systemctl", "is-enabled"]:
                return 1, "disabled", ""
            if cmd[:2] == ["systemctl", "cat"]:
                return 0, "[Unit]", ""
            if cmd[:2] == ["systemctl", "enable"]:
                return 1, "", "cannot enable boom"
            return 1, "", ""

        g = self._unlock()
        with mock.patch("buo.utils.shell.run_command", side_effect=fake_run):
            out = g.persist()
        self.assertFalse(out["persisted"])
        self.assertIn("boom", out["error"])
        with open(self.conf) as fh:
            self.assertIn("0x1f,0x1f,0x1f,0x1f", fh.read())

    def test_mock_persist_writes_no_files_and_no_commands(self):
        """mock → nessun file reale scritto e nessun comando eseguito
        (wrapper assente → errore pulito, mai side effect)."""
        g = self._unlock(mock_mode=True)
        with mock.patch("buo.utils.shell.run_command",
                        side_effect=AssertionError(
                            "persist in mock non deve eseguire comandi")):
            out = g.persist()
        self.assertFalse(out["persisted"])
        self.assertIn("live-manager non installato", out["error"])
        self.assertFalse(os.path.exists(self.conf))

    def test_persist_non_ostree_errors_without_files(self):
        """Non-ostree → errore senza scrivere nulla (path iniettabile mai
        toccato)."""
        g = GPU40CUUnlock(mock=False, use_wrapper=False)
        g.is_ostree = False
        g.boot_conf_path = self.conf
        out = g.persist()
        self.assertFalse(out["persisted"])
        self.assertIn("ostree", out["error"])
        self.assertFalse(os.path.exists(self.conf))


if __name__ == "__main__":
    unittest.main()
