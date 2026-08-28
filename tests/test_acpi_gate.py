#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gate ACPI per l'unlock CPU 8-core + avvisi semi-automatici.

Fail-closed: senza le fix ACPI (SSDT-CST/PST, repo
e-tho/bc250-acpi-fix) l'unlock 8-core manda la BC-250 in boot loop.
BUO quindi BLOCCA l'unlock CPU in automatico e avvisa; la conferma
esplicita è possibile solo in modalità interattiva.
"""

import os
import tempfile
import unittest
from unittest import mock

from buo.config import BUOConfig
from buo.orchestrator import Orchestrator
from buo.utils.mock import MockHardware


class TestAcpiGate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def _make(self, acpi_fixed=False):
        hw = MockHardware(seed=11)
        hw.state.is_acpi_fixed = acpi_fixed
        cfg = BUOConfig()
        cfg.validation_stress_duration = 0
        cfg.benchmark_enabled = False
        orch = Orchestrator(config=cfg, mock=True, dry_run=False,
                            mock_hardware=hw)
        orch.checkpoint.clear()
        return orch

    def test_gate_blocks_cpu_unlock_without_acpi(self):
        """Senza fix ACPI l'unlock CPU è BLOCCATO (fail-closed)."""
        orch = self._make(acpi_fixed=False)
        out = orch._phase_unlock()
        cpu = out.get("cpu", {})
        self.assertTrue(cpu.get("acpi_gate_blocked"),
                        f"unlock CPU non bloccato: {cpu}")
        self.assertNotIn("cpu_core_unlock", orch._applied_steps())

    def test_gate_allows_cpu_unlock_with_acpi(self):
        """Con le fix ACPI presenti l'unlock CPU procede."""
        orch = self._make(acpi_fixed=True)
        orch._phase_unlock()
        self.assertIn("cpu_core_unlock", orch._applied_steps())

    def test_gate_ostree_uses_boot_blob_verify(self):
        """Su ostree il gate usa verify() (boot entry → blob), non /sys.

        Bug G5: i nomi delle tabelle in /sys su ostree sono SSDT1-N
        (override fusi dal kernel), quindi il check cst/pst darebbe un
        falso blocco anche a fix applicata.
        """
        orch = self._make(acpi_fixed=False)
        orch.mock = False          # forza il ramo "reale"
        orch.dry_run = False
        orch.fix_acpi.distro.initramfs_tool = "ostree"
        with mock.patch.object(orch.fix_acpi, "verify",
                               return_value=True) as verify:
            self.assertTrue(orch._acpi_gate_ok())
            verify.assert_called_once()
        with mock.patch.object(orch.fix_acpi, "verify",
                               return_value=False):
            self.assertFalse(orch._acpi_gate_ok())

    def test_gate_non_ostree_accepts_blob_or_cst_pst(self):
        """Non-ostree: gate aperto con cst+pst OPPURE blob concatenato."""
        orch = self._make(acpi_fixed=False)
        orch.mock = False
        orch.dry_run = False
        # il ramo non-ostree passa dall'audit: forziamo audit con blob
        with mock.patch.object(orch.audit, "run", return_value={
                "acpi": {"cst_present": False, "pst_present": False,
                         "boot_fix_present": True}}):
            self.assertTrue(orch._acpi_gate_ok())
        with mock.patch.object(orch.audit, "run", return_value={
                "acpi": {"cst_present": True, "pst_present": True,
                         "boot_fix_present": False}}):
            self.assertTrue(orch._acpi_gate_ok())
        with mock.patch.object(orch.audit, "run", return_value={
                "acpi": {"cst_present": False, "pst_present": False,
                         "boot_fix_present": False}}):
            self.assertFalse(orch._acpi_gate_ok())

    def test_gate_does_not_block_gpu(self):
        """Il gate ACPI riguarda SOLO la CPU: la GPU procede comunque."""
        orch = self._make(acpi_fixed=False)
        orch._phase_unlock()
        self.assertIn("gpu_40cu", orch._applied_steps())

    def test_interactive_can_override_gate(self):
        """In modalità interattiva l'utente può confermare il rischio."""
        orch = self._make(acpi_fixed=False)
        orch.interactive = True
        import unittest.mock as mock
        with mock.patch("builtins.input", return_value="y"):
            orch._phase_unlock()
        self.assertIn("cpu_core_unlock", orch._applied_steps())

    def test_interactive_default_no(self):
        """In interattivo il default (invio) NON procede (fail-closed)."""
        orch = self._make(acpi_fixed=False)
        orch.interactive = True
        import unittest.mock as mock
        with mock.patch("builtins.input", return_value=""):
            orch._phase_unlock()
        self.assertNotIn("cpu_core_unlock", orch._applied_steps())


class TestPowerBudgetWarnings(unittest.TestCase):
    """Avvisi non bloccanti su potenza e toolchain 40-CU."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def _make(self, psu=350):
        hw = MockHardware(seed=11)
        hw.state.is_acpi_fixed = True
        cfg = BUOConfig({"psu_wattage": psu})
        cfg.benchmark_enabled = False
        cfg.validation_stress_duration = 0
        return Orchestrator(config=cfg, mock=True, dry_run=True,
                            mock_hardware=hw)

    def test_power_warning_when_both_unlocks_and_small_psu(self):
        """8 core + 40 CU con PSU < 350W → warning (non eccezione)."""
        orch = self._make(psu=300)
        with self.assertLogs(orch.logger, level="WARNING") as logs:
            orch._check_power_budget()
        joined = "\n".join(logs.output)
        self.assertIn("POTENZA", joined)

    def test_no_power_warning_with_adequate_psu(self):
        """PSU sufficiente → nessun warning di potenza."""
        orch = self._make(psu=600)
        with self.assertLogs(orch.logger, level="INFO") as logs:
            orch._check_power_budget()
        joined = "\n".join(logs.output)
        self.assertNotIn("POTENZA", joined)

    def test_toolchain_check_is_noop_in_mock(self):
        """In mock il check toolchain non fa nulla (nessun accesso a shutil)."""
        orch = self._make()
        orch._check_40cu_toolchain({"governor": {"active": True}})  # no raise


if __name__ == "__main__":
    unittest.main()
