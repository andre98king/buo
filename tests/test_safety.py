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


if __name__ == "__main__":
    unittest.main()
