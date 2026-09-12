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

    def test_bogo_ops_parser_su_output_reale(self):
        """Regressione 12/09: `bogo_ops` era SEMPRE null.

        La vecchia regex cercava `Bogo ops/s` (maiuscola) che nel corpo
        dell'output non compare mai — solo nell'header, senza numeri. Output
        reale copiato dal campo (stress-ng di Bazzite).
        """
        out = (
            "stress-ng: info:  [15802] dispatching hogs: 16 cpu\n"
            "stress-ng: metrc: [15802] stressor       bogo ops real time  "
            "usr time  sys time   bogo ops/s     bogo ops/s\n"
            "stress-ng: metrc: [15802]                           (secs)    "
            "(secs)    (secs)   (real time) (usr+sys time)\n"
            "stress-ng: metrc: [15802] cpu               37784      2.00     "
            "31.92      0.02     18877.93        1182.92\n"
        )
        # 5ª colonna = bogo ops/s REAL TIME (totale dei worker), non la
        # per-CPU (usr+sys) né il tempo di sistema.
        self.assertEqual(BenchmarkRunner._parse_stress_ng_bogo(out), 18877.93)

    def test_bogo_ops_parser_senza_righe_metrc(self):
        for out in ("", "stress-ng: info: [1] dispatching hogs: 1 cpu",
                    "stress-ng: metrc: [1] stressor bogo ops real time"):
            self.assertIsNone(BenchmarkRunner._parse_stress_ng_bogo(out), out)

    def test_cpu_bench_fallback_stress_ng_senza_sysbench(self):
        """sysbench assente → si riusa la metrica dello stress-ng GIÀ eseguito
        (nessun carico aggiuntivo, metrica dichiarata esplicitamente)."""
        from unittest import mock as m
        runner = BenchmarkRunner(mock=False)
        stress = {"available": True, "tool": "stress-ng", "bogo_ops": 18877.93}
        with m.patch("buo.benchmark.runner.which", return_value=None):
            with m.patch.object(runner, "run_cpu_stress") as fake:
                res = runner.run_cpu_benchmark(30, cpu_stress=stress)
                fake.assert_not_called()   # nessuna seconda esecuzione
        self.assertTrue(res["available"], res)
        self.assertEqual(res["bogo_ops"], 18877.93)
        self.assertEqual(res["metric"], "bogo_ops/s")
        self.assertNotIn("events_per_sec", res)

    def test_cpu_bench_nessuna_metrica_non_inventa_valori(self):
        from unittest import mock as m
        runner = BenchmarkRunner(mock=False)
        with m.patch("buo.benchmark.runner.which", return_value=None):
            res = runner.run_cpu_benchmark(
                30, cpu_stress={"available": True, "bogo_ops": None})
        self.assertFalse(res["available"])
        self.assertIn("note", res)


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
