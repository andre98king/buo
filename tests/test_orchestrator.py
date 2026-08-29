#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test end-to-end dell'orchestratore in modalità mock/dry-run."""

import os
import tempfile
import unittest

from buo.config import BUOConfig
from buo.orchestrator import Orchestrator
from buo.utils.mock import MockHardware


class TestOrchestrator(unittest.TestCase):
    def setUp(self):
        # Stato isolato per ogni test (niente checkpoint condiviso su disco)
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def _make(self, dry_run=True):
        hw = MockHardware(seed=42)
        # Scheda simulata "pronta": fix ACPI già presenti (prerequisito
        # per l'unlock 8-core, vedi test_acpi_gate.py)
        hw.state.is_acpi_fixed = True
        cfg = BUOConfig()
        cfg.validation_stress_duration = 0
        cfg.benchmark_enabled = True
        orch = Orchestrator(config=cfg, mock=True, dry_run=dry_run,
                            mock_hardware=hw)
        orch.checkpoint.clear()  # parte da zero
        return orch

    def test_full_run_success(self):
        # run reale simulato (mock): il checkpoint viene scritto,
        # i reboot sono simulati, tutte le fasi vengono eseguite
        orch = self._make(dry_run=False)
        rc = orch.run()
        self.assertEqual(rc, 0)

        phases = orch.checkpoint.full_state()["phases"]
        for phase in ["init", "pre_audit", "unlock", "fix", "optimize",
                      "apply", "validate", "complete"]:
            self.assertTrue(phases[phase]["completed"], f"fase {phase} non completata")

    def test_apply_fixes_recorded(self):
        orch = self._make()
        orch.run()
        self.assertIn("cpu_core_unlock", orch.results["applied_fixes"])
        self.assertIn("gpu_40cu", orch.results["applied_fixes"])

    def test_before_after_collected(self):
        orch = self._make()
        orch.run()
        self.assertEqual(orch.results["before"]["cpu"]["cores"], 6)
        self.assertEqual(orch.results["after"]["gpu"]["cu_count"], 40)

    def test_problems_detected(self):
        orch = self._make()
        orch.run()
        ids = [p["id"] for p in orch.results["problems"]]
        # IOMMU attivo è lo stato CORRETTO: non deve
        # comparire come problema; il problema è iommu_disabled (kernel)
        self.assertNotIn("iommu_disabled", ids)
        self.assertIn("tlb_fault", ids)

    def test_report_generated(self):
        orch = self._make()
        orch.run()
        from buo.utils.paths import report_file_json, report_file_md
        self.assertTrue(report_file_md().exists())
        self.assertTrue(report_file_json().exists())

    def test_status(self):
        orch = self._make()
        status = orch.status()
        self.assertEqual(status["current_phase"], "init")
        self.assertIsNotNone(status["hardware"])

    def test_status_applied_fixes_from_checkpoint(self):
        """status() legge i fix dal checkpoint (applied_steps), MAI da
        results['applied_fixes'] che fuori da run() è sempre vuoto."""
        orch = self._make()
        orch.checkpoint.set("applied_steps", ["gpu_40cu", "cpu_core_unlock"])
        self.assertEqual(orch.results["applied_fixes"], [])
        status = orch.status()
        self.assertEqual(status["applied_fixes"],
                         ["cpu_core_unlock", "gpu_40cu"])

    def test_recovery_plan(self):
        orch = self._make()
        plan = orch.recovery_plan()
        self.assertIn("interrupted_phase", plan)

    def test_apply_cpu_config_dry_run(self):
        """Applicazione CPU in dry-run: simulata, nessun effetto."""
        orch = self._make(dry_run=True)
        r = orch._apply_cpu_config(3500, scale=0)
        self.assertTrue(r["applied"])
        self.assertTrue(r["dry_run"])

    def test_apply_cpu_config_mock(self):
        orch = self._make(dry_run=False)
        r = orch._apply_cpu_config(3500, scale=0)
        self.assertTrue(r["applied"])
        self.assertTrue(r["mock"])

    def test_apply_cpu_config_clamps_frequency(self):
        """La frequenza è clampata ai limiti immutabili (mai oltre)."""
        orch = self._make(dry_run=True)
        f, s = orch._clamp_cpu(99999)  # oltre il limite
        self.assertEqual(f, 4000)  # LIMITS.cpu.freq_max
        f2, _ = orch._clamp_cpu(100)  # sotto il limite
        self.assertEqual(f2, 3500)  # LIMITS.cpu.freq_min

    def test_apply_cpu_config_scale_from_vid(self):
        """Senza scale, conversione da VID: la formula community
        (1206-vid)/8 produce valori POSITIVI per l'undervolt, incoerenti coi
        bounds reali della scale (bc250_limits.py: -50..0) → clampata a 0
        (curva stock, MAI overvolt)."""
        orch = self._make(dry_run=True)
        _, s = orch._clamp_cpu(3500, vid=1030)
        self.assertEqual(s, 0)  # (1206-1030)/8 = 22 → clamp [−50, 0] → 0

    def test_apply_cpu_config_scale_bounds_never_positive(self):
        """Bounds scale VERIFICATI nel sorgente community (scale_min=-50,
        scale_max=0; bc250_detect.smu_apply RIFIUTA scale>0): una scale
        positiva chiederebbe un overvolt → mai ammessa; i valori validi
        negativi passano; sotto -50 → clamp a -50."""
        orch = self._make(dry_run=True)
        # positiva → 0 (mai overvolt)
        _, s = orch._clamp_cpu(3500, scale=50)
        self.assertEqual(s, 0)
        _, s2 = orch._clamp_cpu(3500, scale=10)
        self.assertEqual(s2, 0)
        # negativa valida → preservata
        _, s3 = orch._clamp_cpu(3500, scale=-20)
        self.assertEqual(s3, -20)
        # sotto il floor community → clamp a -50
        _, s4 = orch._clamp_cpu(3500, scale=-80)
        self.assertEqual(s4, -50)

    def test_phase_apply_passes_gpu_freq_max_to_write_config(self):
        """_phase_apply deve passare a write_config il cap della config
        (safety.gpu_freq_max): senza, a ogni apply il range tornerebbe a
        2230 e il punto 2000@900 (110°C sotto FurMark su questa macchina)
        verrebbe riscritto."""
        from unittest import mock
        orch = self._make(dry_run=False)
        orch.config = BUOConfig({"safety": {"gpu_freq_max": 1500}})
        points = [
            {"freq": 1200, "voltage": 800},
            {"freq": 1500, "voltage": 900},
            {"freq": 2000, "voltage": 1000},
        ]
        orch.checkpoint.set_phase("optimize", {
            "undervolt_gpu": {"safe_points": points},
            "undervolt_cpu": {},
            "overclock_cpu": {},
        })
        with mock.patch.object(orch.governor, "write_config",
                               return_value=True) as spy:
            r = orch._phase_apply()
        spy.assert_called_once_with(points, max_freq=1500)
        self.assertTrue(r["governor_config"])


if __name__ == "__main__":
    unittest.main()
