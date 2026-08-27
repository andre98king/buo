#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test del cockpit TUI (guardia senza textual + dashboard pura)."""

import unittest
from unittest import mock as umock

from buo.tui import LiveReadings, dashboard_text, run_tui
from buo.utils.mock import MockHardware


class TestDashboardText(unittest.TestCase):
    def test_contains_key_metrics(self):
        r = {
            "cpu_cores": 8, "cpu_freq": 3700, "cpu_vid": 1012,
            "cpu_temp": 72.0, "gpu_cu": 38, "gpu_freq": 1500,
            "gpu_voltage": 900, "gpu_temp": 67.0, "gpu_power": 100.0,
            "total_power": 175.0, "fan_speed": 1800, "ambient_temp": 22.0,
            "undervolted": True, "overclocked": False, "cu40": True,
        }
        text = dashboard_text(r)
        self.assertIn("8 core", text)
        self.assertIn("38 CU", text)
        self.assertIn("72.0", text)
        self.assertIn("67.0", text)
        self.assertIn("175.0", text)
        self.assertIn("1800", text)
        self.assertIn("undervolt", text)

    def test_tolerates_empty_dict(self):
        text = dashboard_text({})
        self.assertIn("CPU", text)  # nessun crash

    def test_cold_status(self):
        text = dashboard_text({"cpu_temp": 45.0, "gpu_temp": 40.0})
        self.assertIn("✅", text)


class TestLiveReadings(unittest.TestCase):
    def test_mock_provider(self):
        provider = LiveReadings(mock=True, mock_hardware=MockHardware())
        r = provider.read()
        self.assertEqual(r["cpu_cores"], 6)
        self.assertEqual(r["gpu_cu"], 24)
        self.assertIn("cpu_temp", r)

    def test_mock_after_unlock(self):
        hw = MockHardware()
        hw.enable_40cu()
        provider = LiveReadings(mock=True, mock_hardware=hw)
        r = provider.read()
        self.assertTrue(r["cu40"])

    def test_empty_without_hardware(self):
        provider = LiveReadings(mock=True, mock_hardware=None)
        r = provider.read()
        self.assertEqual(r, {})  # lettura vuota, nessun crash


class TestTUIGuard(unittest.TestCase):
    @umock.patch("importlib.util.find_spec", return_value=None)
    def test_raises_without_textual(self, _find_spec):
        with self.assertRaises(RuntimeError) as ctx:
            run_tui(mock=True)
        self.assertIn("textual", str(ctx.exception))

    def test_module_importable_without_textual(self):
        """Il modulo si importa anche senza textual (import pigro)."""
        import buo.tui  # noqa: F401  (già importato sopra)


if __name__ == "__main__":
    unittest.main()
