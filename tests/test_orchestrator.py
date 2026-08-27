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
        # IOMMU attivo è lo stato CORRETTO (docs/BUGS.md #2): non deve
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

    def test_recovery_plan(self):
        orch = self._make()
        plan = orch.recovery_plan()
        self.assertIn("interrupted_phase", plan)


if __name__ == "__main__":
    unittest.main()
