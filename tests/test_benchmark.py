#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test del BenchmarkRunner e della cattura before/after nell'orchestratore."""

import os
import tempfile
import unittest

from buo.benchmark.runner import BenchmarkRunner
from buo.config import BUOConfig
from buo.orchestrator import Orchestrator
from buo.utils.mock import MockHardware


class TestBenchmarkRunner(unittest.TestCase):
    """Il runner in mock deve restituire dati simulati, mai un dict vuoto."""

    def test_mock_returns_fake_data_for_all_benchmarks(self):
        runner = BenchmarkRunner(mock=True, mock_hardware=MockHardware(seed=1))
        results = runner.run_all()

        for name in ("gpu_stress", "cpu_stress", "cpu_bench", "compute_bench",
                     "ai_inference"):
            self.assertIn(name, results, f"benchmark {name} mancante")
            self.assertTrue(results[name].get("available"),
                            f"benchmark {name} non disponibile in mock")
        self.assertIn("timestamp", results)

    def test_parse_float(self):
        self.assertEqual(BenchmarkRunner._parse_float(r"([\d.]+)", "FPS 42.5"), 42.5)
        self.assertIsNone(BenchmarkRunner._parse_float(r"([\d.]+)", "niente"))


class TestBenchmarkCapture(unittest.TestCase):
    """L'orchestratore deve catturare sia before sia after."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def _make(self, dry_run):
        hw = MockHardware(seed=42)
        hw.state.is_acpi_fixed = True
        cfg = BUOConfig()
        cfg.benchmark_enabled = True
        cfg.validation_stress_duration = 0
        orch = Orchestrator(config=cfg, mock=True, dry_run=dry_run,
                            mock_hardware=hw)
        orch.checkpoint.clear()
        return orch

    def test_dry_run_captures_both_before_and_after(self):
        orch = self._make(dry_run=True)
        orch._phase_pre_audit()
        orch._phase_validate()

        benchmarks = orch.results["benchmarks"]
        self.assertIn("before", benchmarks)
        self.assertIn("after", benchmarks)
        self.assertTrue(benchmarks["before"].get("gpu_stress", {}).get("available"))
        self.assertTrue(benchmarks["after"].get("gpu_stress", {}).get("available"))


if __name__ == "__main__":
    unittest.main()
