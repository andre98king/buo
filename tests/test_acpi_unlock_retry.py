#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BUG F-C: l'unlock CPU bloccato dal gate ACPI viene ritentato al RESUME.

Sequenza osservata sul campo (29/08, restore dopo riavvio a freddo):
1. fase unlock: il gate ACPI è CHIUSO (la boot entry di default non ha
   ancora la fix) → unlock 8-core SALTATO (fail-closed);
2. fase fix: acpi_fix applicata → needs_reboot → la macchina riparte;
3. al resume current = "fix" → la fase unlock NON veniva MAI ritentata →
   la macchina restava a 12 thread fino a un secondo run manuale.

Fix: quando il gate blocca l'unlock (solo run reali) si persiste il
marcatore `unlock_blocked_acpi` nel checkpoint; al resume, se il marcatore
è presente e il gate ora passa (la fix è stata applicata al pre-reboot),
la fase unlock viene RIESEGUITA. Il marcatore viene consumato al retry:
il retry avviene UNA volta per run (anti-loop); se il gate è ancora
chiuso, il marcatore resta per il resume successivo.
"""

import os
import tempfile
import unittest
from unittest import mock

from buo.config import BUOConfig
from buo.orchestrator import Orchestrator
from buo.state.checkpoint import CheckpointManager
from buo.utils.mock import MockHardware


class TestAcpiUnlockRetry(unittest.TestCase):
    """Marcatore + retry al resume dell'unlock CPU bloccato dal gate."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def _make(self, acpi_fixed=False, dry_run=False):
        hw = MockHardware(seed=11)
        hw.state.is_acpi_fixed = acpi_fixed
        cfg = BUOConfig()
        cfg.validation_stress_duration = 0
        cfg.benchmark_enabled = False
        orch = Orchestrator(config=cfg, mock=True, dry_run=dry_run,
                            mock_hardware=hw)
        orch.checkpoint.clear()
        return orch

    def _seed_resume_state(self, marker=True, applied=None):
        """Simula il checkpoint lasciato da un run reale interrotto dal
        reboot: fase unlock completata CON blocco gate, poi fix (acpi_fix)
        applicata e reboot → al resume current_phase = 'fix'."""
        cm = CheckpointManager()
        cm.set_phase("unlock",
                     {"cpu": {"unlocked": False, "acpi_gate_blocked": True}},
                     completed=True)
        cm.set_current_phase("fix")
        cm.set("applied_steps", applied or ["acpi_fix", "gpu_40cu"])
        if marker:
            cm.set("unlock_blocked_acpi", True)
        return cm

    # -------------------- marcatore al blocco del gate ---------------- #

    def test_gate_block_sets_marker_in_real_run(self):
        """Il blocco del gate in un run REALE imposta il marcatore
        unlock_blocked_acpi (per il retry al resume)."""
        orch = self._make(acpi_fixed=False, dry_run=False)
        orch._phase_unlock()
        self.assertTrue(orch.checkpoint.get("unlock_blocked_acpi"))

    def test_no_marker_in_dry_run(self):
        """Il marcatore NON viene scritto in dry-run (nessuna scrittura
        dello stato persistente)."""
        orch = self._make(acpi_fixed=False, dry_run=True)
        orch._phase_unlock()
        self.assertFalse(orch.checkpoint.get("unlock_blocked_acpi"))

    # ------------------- retry al resume (bug sul campo) -------------- #

    def test_resume_retries_unlock_when_gate_now_passes(self):
        """F-C end-to-end: al resume con marcatore presente e gate ora
        APERTO (acpi_fix applicata al pre-reboot), la fase unlock viene
        RIESEGUITA e il run completa senza loop."""
        self._seed_resume_state(marker=True)
        hw = MockHardware(seed=11)
        hw.state.is_acpi_fixed = True
        cfg = BUOConfig()
        cfg.validation_stress_duration = 0
        cfg.benchmark_enabled = False
        orch = Orchestrator(config=cfg, mock=True, dry_run=False,
                            mock_hardware=hw)

        calls = []
        orig = orch._do_cpu_unlock

        def spy():
            calls.append(1)
            return orig()

        with mock.patch.object(orch, "_do_cpu_unlock", side_effect=spy):
            rc = orch.run()

        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1,
                         "l'unlock CPU deve essere RITENTATO al resume")
        self.assertIn("cpu_core_unlock", orch._applied_steps())
        unlock = orch.checkpoint.get_phase("unlock").get("data", {})
        self.assertTrue(unlock.get("cpu", {}).get("unlocked"),
                        "la fase unlock rieseguita deve risultare riuscita")
        self.assertFalse(orch.checkpoint.get("unlock_blocked_acpi"),
                         "il marcatore deve essere consumato dal retry")

    def test_resume_keeps_marker_when_gate_still_closed(self):
        """Se al resume il gate è ANCORA chiuso (acpi_fix non ancora
        applicata — es. reboot dovuto all'unlock GPU), NON si ritenta e il
        marcatore RESTA per il resume successivo."""
        self._seed_resume_state(marker=True, applied=["gpu_40cu"])
        hw = MockHardware(seed=11)
        hw.state.is_acpi_fixed = False
        cfg = BUOConfig()
        cfg.validation_stress_duration = 0
        cfg.benchmark_enabled = False
        orch = Orchestrator(config=cfg, mock=True, dry_run=False,
                            mock_hardware=hw)

        # Il reboot reale esce con sys.exit(EXIT_REBOOT): lo simuliamo per
        # fermare il run al primo reboot (fix ACPI) come sulla macchina.
        with mock.patch.object(orch, "_schedule_reboot",
                               side_effect=SystemExit), \
             mock.patch.object(orch, "_do_cpu_unlock",
                               side_effect=AssertionError(
                                   "unlock NON deve essere ritentato")):
            with self.assertRaises(SystemExit):
                orch.run()

        self.assertTrue(orch.checkpoint.get("unlock_blocked_acpi"),
                        "marcatore deve restare per il resume successivo")

    # ------------------------- anti-loop ------------------------------ #

    def test_retry_happens_once_only(self):
        """Anti-loop: senza marcatore (già consumato dal retry precedente)
        un resume NON ritenta l'unlock."""
        self._seed_resume_state(marker=False)
        hw = MockHardware(seed=11)
        hw.state.is_acpi_fixed = True
        cfg = BUOConfig()
        cfg.validation_stress_duration = 0
        cfg.benchmark_enabled = False
        orch = Orchestrator(config=cfg, mock=True, dry_run=False,
                            mock_hardware=hw)

        with mock.patch.object(orch, "_do_cpu_unlock",
                               side_effect=AssertionError(
                                   "senza marcatore non si ritenta")):
            rc = orch.run()

        self.assertEqual(rc, 0)
        self.assertNotIn("cpu_core_unlock", orch._applied_steps())

    # ------------------------- pulizia marcatore ---------------------- #

    def test_marker_cleared_after_completed_run(self):
        """A run completo il marcatore è pulito: un run successivo non
        eredita il retry (niente loop)."""
        orch = self._make(acpi_fixed=False, dry_run=False)
        rc = orch.run()
        self.assertEqual(rc, 0)
        self.assertFalse(orch.checkpoint.get("unlock_blocked_acpi"))

    def test_marker_cleared_on_fresh_init(self):
        """Un run nuovo da init (checkpoint 'complete') pulisce il
        marcatore anche se era rimasto attivo da un run anomalo."""
        cfg = BUOConfig()
        cfg.validation_stress_duration = 0
        cfg.benchmark_enabled = False
        orch = Orchestrator(config=cfg, mock=True, dry_run=False,
                            mock_hardware=MockHardware(seed=11))
        orch.checkpoint.clear()
        orch.checkpoint.set("unlock_blocked_acpi", True)  # stato anomalo
        orch.checkpoint.set_current_phase("complete")
        with mock.patch.object(orch, "_phase_unlock", return_value={}):
            rc = orch.run()
        self.assertEqual(rc, 0)
        self.assertFalse(orch.checkpoint.get("unlock_blocked_acpi"))


if __name__ == "__main__":
    unittest.main()
