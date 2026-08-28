#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test A5 (rollback ACPI completo) e A6 (lock anti-concorrenza)."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from buo.config import BUOConfig
from buo.fix.acpi import ACPIFix
from buo.orchestrator import Orchestrator


class TestAcpiRollbackCompleteness(unittest.TestCase):
    """A5: il rollback rimuove anche la conf dracut scritta da apply() e
    ripristina HOOKS mkinitcpio."""

    def _fix(self, tool):
        fix = ACPIFix(mock=False)
        fix.distro.initramfs_tool = tool
        return fix

    def test_dracut_override_conf_removed(self):
        fix = self._fix("dracut")
        removed = []

        def fake_exists(self):
            return str(self) in (
                "/etc/dracut.conf.d/acpi",
                "/etc/dracut.conf.d/buo-acpi-override.conf",
                "/boot/SSDT_ACPI.cpio",
            )

        def fake_unlink(self):
            removed.append(str(self))

        with mock.patch.object(fix.distro, "rebuild_initramfs"), \
             mock.patch("buo.fix.acpi.Path.exists", fake_exists), \
             mock.patch("buo.fix.acpi.Path.is_dir", return_value=False), \
             mock.patch("buo.fix.acpi.Path.unlink", fake_unlink):
            ok = fix.rollback()
        self.assertTrue(ok)
        self.assertIn("/etc/dracut.conf.d/buo-acpi-override.conf", removed)

    def test_mkinitcpio_hooks_restored(self):
        fix = self._fix("mkinitcpio")
        written = {}

        def fake_read(self_, *a, **k):
            if str(self_) == "/etc/mkinitcpio.conf":
                return "HOOKS=(acpi_override base udev)\n"
            return ""

        def fake_write(self_, content):
            written[str(self_)] = content

        with mock.patch.object(fix.distro, "rebuild_initramfs"), \
             mock.patch("buo.fix.acpi.Path.exists", return_value=True), \
             mock.patch("buo.fix.acpi.Path.is_dir", return_value=False), \
             mock.patch("buo.fix.acpi.Path.unlink"), \
             mock.patch("buo.fix.acpi.Path.read_text",
                        mock.Mock(side_effect=fake_read)), \
             mock.patch("buo.fix.acpi.Path.write_text",
                        mock.Mock(side_effect=fake_write)):
            fix.rollback()
        content = written.get("/etc/mkinitcpio.conf", "")
        self.assertIn("HOOKS=(base udev)", content)
        self.assertNotIn("acpi_override", content)


class TestConcurrencyLock(unittest.TestCase):
    """A6: una seconda istanza reale di buo viene rifiutata."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def test_second_instance_refused(self):
        import fcntl
        from buo.utils.paths import state_dir
        state_dir().mkdir(parents=True, exist_ok=True)
        fd = open(state_dir() / "buo.lock", "w", encoding="utf-8")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            cfg = BUOConfig()
            cfg.validation_stress_duration = 0
            cfg.benchmark_enabled = False
            orch = Orchestrator(config=cfg, mock=False, dry_run=False)
            with self.assertRaises(RuntimeError) as ctx:
                orch.run()
            self.assertIn("istanza", str(ctx.exception))
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()

    def test_mock_run_does_not_lock(self):
        from buo.utils.mock import MockHardware
        cfg = BUOConfig()
        cfg.validation_stress_duration = 0
        cfg.benchmark_enabled = False
        orch = Orchestrator(config=cfg, mock=True, dry_run=True,
                            mock_hardware=MockHardware(seed=11))
        orch.checkpoint.clear()
        # non deve sollevare RuntimeError (niente lock in mock/dry-run)
        rc = orch.run()
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
