#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test del ROLLBACK AUTOMATICO su validate-fail (design
DESIGN_UNLEASH_OC_BOUNDARY.md §6(b), backlog T2, decisione utente #5):
stress di validate fallito (WHEA/stretch — NON termico, quello abortisce
già) con config applicata in questa run → ripristino della config pre-run:
CPU → uninstall (mai un voltaggio non validato applicato), GPU governor →
bytes config.toml pre-run (o delete se prima non esisteva). Escluso in
restore/dry-run. Mai hardware reale: mock + config governor su tmp.
"""

import os
import tempfile
import unittest
from pathlib import Path

from buo.config import BUOConfig
from buo.orchestrator import Orchestrator
from buo.utils.mock import MockHardware


class _FakeStress:
    """Sostituto di StressTest.run: esito controllato dal test."""

    def __init__(self, passed: bool):
        self._passed = passed

    def run(self, **kwargs):
        return {
            "passed": self._passed,
            "skipped": False,
            "duration_minutes": kwargs.get("duration_minutes", 0),
            "cpu_temp_max": None, "gpu_temp_max": None,
            "power_max": None, "errors": 0 if self._passed else 1,
        }


class BaseValidate(unittest.TestCase):
    def setUp(self):
        self._state_tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._state_tmp.name
        self.gov_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._state_tmp.cleanup()

    def _orch(self, dry_run=False, gov_path=None):
        hw = MockHardware(seed=42)
        hw.state.is_acpi_fixed = True
        cfg = BUOConfig()
        cfg.validation_stress_duration = 0
        cfg.benchmark_enabled = False
        orch = Orchestrator(config=cfg, mock=True, dry_run=dry_run,
                            mock_hardware=hw,
                            governor_config_path=gov_path)
        orch.checkpoint.clear()
        return orch

    def _seed_cpu_applied(self, orch):
        """Simula la config CPU applicata in questa run (ledger)."""
        orch._mark_step("cpu_overclock")

    def _seed_apply_gpu(self, orch):
        orch.checkpoint.set_phase(
            "apply", {"governor_config": True, "applied": True},
            completed=True)

    def _run_validate(self, orch, passed):
        orch.stress = _FakeStress(passed)
        return orch._phase_validate()


class TestValidateRollback(BaseValidate):
    def test_no_rollback_when_stress_passed(self):
        orch = self._orch()
        self._seed_cpu_applied(orch)
        data = self._run_validate(orch, passed=True)
        self.assertNotIn("config_rollback", data)
        self.assertIn("cpu_overclock", orch._applied_steps())

    def test_cpu_rolled_back_and_ledger_cleaned(self):
        orch = self._orch()
        self._seed_cpu_applied(orch)
        data = self._run_validate(orch, passed=False)
        rb = data.get("config_rollback")
        self.assertIsNotNone(rb)
        self.assertTrue(rb["cpu"])
        self.assertFalse(rb.get("gpu"))
        # ledger ripulito: il fix risulta ripristinato, non più applicato
        self.assertNotIn("cpu_overclock", orch._applied_steps())
        joined = "\n".join(orch.results["notes"])
        self.assertIn("ripristinata", joined)

    def test_gpu_config_restored_to_pre_run_bytes(self):
        conf = self.gov_dir / "config.toml"
        original = b"# config pre-run\n[[safe-points]]\nfrequency = 1500\n"
        conf.write_bytes(original)
        orch = self._orch(gov_path=conf)
        self._seed_apply_gpu(orch)
        orch._capture_pre_validate_config()
        # l'apply ha scritto la config dello sweep (contenuto DIVERSO)
        conf.write_bytes(b"[[safe-points]]\nfrequency = 2000\nvoltage = 900\n")
        data = self._run_validate(orch, passed=False)
        rb = data.get("config_rollback")
        self.assertIsNotNone(rb)
        self.assertTrue(rb["gpu"])
        self.assertEqual(conf.read_bytes(), original)

    def test_gpu_config_deleted_when_no_pre_run_config(self):
        conf = self.gov_dir / "config.toml"
        orch = self._orch(gov_path=conf)
        self._seed_apply_gpu(orch)
        orch._capture_pre_validate_config()   # file assente pre-run
        conf.write_bytes(b"[[safe-points]]\nfrequency = 1500\n")  # creato
        data = self._run_validate(orch, passed=False)
        rb = data.get("config_rollback")
        self.assertIsNotNone(rb)
        self.assertTrue(rb["gpu"])
        self.assertFalse(conf.exists())

    def test_no_rollback_when_nothing_applied(self):
        orch = self._orch()
        n_notes = len(orch.results["notes"])
        data = self._run_validate(orch, passed=False)
        self.assertNotIn("config_rollback", data)
        self.assertEqual(len(orch.results["notes"]), n_notes)

    def test_no_rollback_in_restore_mode(self):
        orch = self._orch()
        orch.checkpoint.set("restore_active", True)
        self._seed_cpu_applied(orch)
        data = self._run_validate(orch, passed=False)
        self.assertNotIn("config_rollback", data)
        # in restore il ledger non va toccato (nessuna config applicata qui)
        self.assertIn("cpu_overclock", orch._applied_steps())

    def test_benchmark_after_skipped_after_rollback(self):
        orch = self._orch()
        orch.config.benchmark_enabled = True
        self._seed_cpu_applied(orch)
        orch.benchmark.run_all = lambda **kw: {"gpu": "x"}
        data = self._run_validate(orch, passed=False)
        self.assertTrue(data["config_rollback"]["cpu"])
        self.assertNotIn("after", orch.results["benchmarks"])

    def test_riepilogo_reports_rollback(self):
        orch = self._orch()
        self._seed_cpu_applied(orch)
        data = self._run_validate(orch, passed=False)
        # run() persiste la fase nel checkpoint: riepilogo_lines legge QUI.
        orch.checkpoint.set_phase("validate", data, completed=True)
        joined = "\n".join(orch.riepilogo_lines())
        self.assertIn("stress: fallito", joined)
        self.assertIn("ripristinata", joined)


class TestValidateRollbackEndToEnd(BaseValidate):
    def test_full_run_validate_fail_rolls_back_cpu(self):
        # Run completa simulata con stress fallito: l'uninstall della CPU
        # avviene in fase validate e il run chiude comunque (exit 0).
        hw = MockHardware(seed=42)
        hw.state.is_acpi_fixed = True
        cfg = BUOConfig()
        cfg.validation_stress_duration = 0
        cfg.benchmark_enabled = False
        orch = Orchestrator(config=cfg, mock=True, dry_run=False,
                            mock_hardware=hw)
        orch.checkpoint.clear()
        orch.stress = _FakeStress(passed=False)
        rc = orch.run()
        self.assertEqual(rc, 0)
        phases = orch.checkpoint.full_state()["phases"]
        vd = phases["validate"]["data"]
        self.assertFalse(vd["stress"]["passed"])
        rb = vd.get("config_rollback")
        self.assertIsNotNone(rb)
        self.assertTrue(rb["cpu"])
        self.assertNotIn("cpu_overclock", orch._applied_steps())
        self.assertTrue(any("ripristinata" in n
                            for n in orch.results["notes"]))


if __name__ == "__main__":
    unittest.main()
