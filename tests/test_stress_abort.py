#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test C2: lo stress test campiona LIVE e ABORTA il processo reale."""

import subprocess
import unittest

from buo.exceptions import SafetyViolation
from buo.validate.stress import StressTest


class _HotReader:
    def get_cpu_temp(self):
        return 99.0

    def get_gpu_temp(self):
        return 40.0

    def get_total_power(self):
        return 85.0


class _CoolReader:
    def get_cpu_temp(self):
        return 50.0

    def get_gpu_temp(self):
        return 45.0

    def get_total_power(self):
        return 120.0


class _BlindReader:
    def get_cpu_temp(self):
        return None

    def get_gpu_temp(self):
        return None

    def get_total_power(self):
        return None


class TestStressAbort(unittest.TestCase):
    def test_hot_cpu_terminates_process_and_raises(self):
        """Temp CPU oltre il limite → processo terminato + SafetyViolation."""
        stress = StressTest(reader=_HotReader())
        t0 = __import__("time").monotonic()
        with self.assertRaises(SafetyViolation) as ctx:
            stress._run_loaded(["sleep", "30"], 30, _HotReader(), 300)
        self.assertIn("CPU", str(ctx.exception))
        # abort entro pochi secondi, non dopo 30s
        self.assertLess(__import__("time").monotonic() - t0, 10)

    def test_cool_reader_runs_to_completion(self):
        stress = StressTest(reader=_CoolReader())
        rc, cpu_max, gpu_max, power_max = stress._run_loaded(
            ["sleep", "2"], 2, _CoolReader(), 300)
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(cpu_max, 50.0)
        self.assertGreaterEqual(gpu_max, 45.0)
        self.assertGreaterEqual(power_max, 120.0)

    def test_blind_reader_no_crash(self):
        """Sensori non leggibili → nessun crash, nessuna violazione."""
        stress = StressTest(reader=_BlindReader())
        rc, cpu_max, gpu_max, power_max = stress._run_loaded(
            ["sleep", "1"], 1, _BlindReader(), 300)
        self.assertEqual(rc, 0)

    def test_monitor_violation_terminates(self):
        """Violazione segnalata dal safety monitor → abort immediato."""
        class FakeMonitor:
            def is_violation(self):
                return True

            def get_violation_reason(self):
                return "test violation"

        stress = StressTest(reader=_CoolReader(), safety_monitor=FakeMonitor())
        with self.assertRaises(SafetyViolation) as ctx:
            stress._run_loaded(["sleep", "30"], 30, _CoolReader(), 300)
        self.assertIn("test violation", str(ctx.exception))

    def test_deadline_enforced(self):
        """Timeout globale: il processo non può superare la deadline."""
        stress = StressTest(reader=_CoolReader())
        stress.deadline_grace = 1
        t0 = __import__("time").monotonic()
        rc, *_ = stress._run_loaded(["sleep", "30"], 1, _CoolReader(), 300)
        self.assertLess(rc, 0)  # terminato con segnale (SIGTERM)
        self.assertLess(__import__("time").monotonic() - t0, 10)

    def test_zero_duration_skips_without_spawning(self):
        """BUG di campo: durata 0 deve saltare SENZA spawnare alcun
        processo (stress-ng --timeout 0 = stress infinito)."""
        import unittest.mock as mock
        with mock.patch("buo.validate.stress.subprocess.Popen",
                        side_effect=AssertionError(
                            "durata 0 NON deve spawnare processi")):
            stress = StressTest(reader=_CoolReader())
            result = stress.run(duration_minutes=0, power_budget=300)
        self.assertTrue(result["passed"])
        self.assertTrue(result["skipped"])
        self.assertIsNone(result["cpu_temp_max"])
        self.assertIsNone(result["gpu_temp_max"])

    def test_positive_duration_still_spawns(self):
        """Durata > 0: il percorso normale resta invariato (spawn reale)."""
        import unittest.mock as mock
        spawned = []
        real_popen = __import__("subprocess").Popen

        def _fake_popen(cmd, **kwargs):
            spawned.append(cmd)
            return real_popen(["sleep", "0.2"], **kwargs)

        with mock.patch("buo.validate.stress.subprocess.Popen",
                        side_effect=_fake_popen), \
             mock.patch("buo.validate.stress.which",
                        side_effect=lambda name: f"/usr/bin/{name}"):
            stress = StressTest(reader=_CoolReader())
            result = stress.run(duration_minutes=1, power_budget=300)
        self.assertTrue(spawned, "durata > 0 deve spawnare lo stress")
        self.assertTrue(result["passed"])


if __name__ == "__main__":
    unittest.main()
