#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test del safety monitor con hardware mock (usa il thread reale)."""

import time
import unittest

from buo.safety.monitor import SafetyMonitor
from buo.utils.mock import MockHardware


class TestSafetyMonitor(unittest.TestCase):
    @staticmethod
    def _run_monitor(hw, violations, samples=0.2):
        monitor = SafetyMonitor(abort_callback=violations.append,
                                hardware=hw, vram_estimation=False)
        monitor.start()
        time.sleep(samples)
        monitor.stop()
        monitor.join(timeout=2)
        return monitor

    def test_no_violation_at_stock(self):
        hw = MockHardware()
        violations = []
        monitor = self._run_monitor(hw, violations)
        self.assertEqual(violations, [])

    def test_cpu_vid_over_limit_triggers_abort(self):
        hw = MockHardware()
        hw.state.cpu_vid = 1400  # oltre l'hard limit di 1325mV
        violations = []
        self._run_monitor(hw, violations)
        self.assertEqual(len(violations), 1)
        self.assertIn("1325", violations[0])

    def test_gpu_voltage_over_limit_triggers_abort(self):
        hw = MockHardware()
        hw.state.gpu_voltage = 1150  # oltre 1100mV
        violations = []
        self._run_monitor(hw, violations)
        self.assertEqual(len(violations), 1)
        self.assertIn("1100", violations[0])

    def test_cpu_temp_over_limit_triggers_abort(self):
        hw = MockHardware()
        hw.state.cpu_temp = 95.0  # oltre 90°C
        violations = []
        self._run_monitor(hw, violations)
        self.assertEqual(len(violations), 1)

    def test_power_over_budget_triggers_abort(self):
        hw = MockHardware()
        hw.state.gpu_power = 400.0  # budget 300W (mock aggiunge ~15W)
        violations = []
        self._run_monitor(hw, violations)
        self.assertEqual(len(violations), 1)

    def test_readings_recorded(self):
        hw = MockHardware()
        monitor = self._run_monitor(hw, [])
        readings = monitor.get_last_readings()
        self.assertIsNotNone(readings)
        self.assertGreater(readings.cpu_temp, 0)

    def test_stop_is_clean(self):
        hw = MockHardware()
        monitor = SafetyMonitor(abort_callback=lambda r: None,
                                hardware=hw, vram_estimation=False)
        monitor.start()
        time.sleep(0.1)
        monitor.stop()
        monitor.join(timeout=2)
        self.assertFalse(monitor.is_alive())

    # --------------------- C1: letture None/real reader ----------------- #

    class _BlindReader:
        """Nessun sensore leggibile → tutto None (fail-visible)."""

        def get_cpu_temp(self):
            return None

        def get_cpu_vid(self):
            return None

        def get_gpu_temp(self):
            return None

        def get_gpu_voltage(self):
            return None

        def get_gpu_power(self):
            return None

        def get_total_power(self):
            return None

    def test_none_readings_no_crash_no_violation(self):
        violations = []
        monitor = self._run_monitor(self._BlindReader(), violations)
        self.assertEqual(violations, [])
        readings = monitor.get_last_readings()
        self.assertIsNotNone(readings)
        self.assertIsNone(readings.cpu_temp)

    def test_reader_temp_over_limit_triggers(self):
        class HotReader(self._BlindReader):
            def get_cpu_temp(self):
                return 99.0

        violations = []
        self._run_monitor(HotReader(), violations)
        self.assertEqual(len(violations), 1)
        self.assertIn("CPU Temp", violations[0])

    def test_reader_vid_over_hard_limit_triggers(self):
        class HighVidReader(self._BlindReader):
            def get_cpu_vid(self):
                return 1400

        violations = []
        self._run_monitor(HighVidReader(), violations)
        self.assertEqual(len(violations), 1)
        self.assertIn("HARD", violations[0])

    def test_config_limits_are_only_stricter(self):
        """m1: le soglie config safety.cpu_temp_max/gpu_temp_max (passate
        via `limits`) sono SOLO stringimenti — mai oltre gli hard limits."""
        from buo.constants import LIMITS

        class Hot86(self._BlindReader):
            def get_cpu_temp(self):
                return 86.0     # sotto l'hard 90 ma sopra la config 85

        violations = []
        monitor = SafetyMonitor(
            abort_callback=violations.append,
            hardware=Hot86(), vram_estimation=False,
            limits={"cpu_temp_max": 85, "gpu_temp_max": 80,
                    "power_budget": 250})
        monitor.start()
        time.sleep(0.3)
        monitor.stop()
        monitor.join(timeout=2)
        self.assertEqual(len(violations), 1)
        self.assertIn("CPU Temp", violations[0])
        # clamp: chiedere PIÙ dell'hard limit non allenta mai la soglia
        monitor2 = SafetyMonitor(
            abort_callback=lambda r: None, hardware=Hot86(),
            vram_estimation=False,
            limits={"cpu_temp_max": LIMITS.cpu.temp_max + 50})
        self.assertEqual(monitor2.limits.cpu_temp_max, LIMITS.cpu.temp_max)


if __name__ == "__main__":
    unittest.main()
