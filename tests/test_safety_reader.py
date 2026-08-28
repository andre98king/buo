#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test del lettore hardware REALE per il SafetyMonitor (fix C1).

hwmon finto (k10temp + amdgpu) in una dir temporanea; ogni sensore
assente deve dare None, mai valori inventati.
"""

import tempfile
import unittest
from pathlib import Path

from buo.safety.reader import RealHardwareReader


class TestRealHardwareReader(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.k10 = self.base / "hwmon0"
        self.k10.mkdir()
        (self.k10 / "name").write_text("k10temp\n")
        (self.k10 / "temp1_input").write_text("45000\n")  # 45 °C
        self.amd = self.base / "hwmon1"
        self.amd.mkdir()
        (self.amd / "name").write_text("amdgpu\n")
        (self.amd / "temp1_input").write_text("50000\n")       # 50 °C
        (self.amd / "in0_input").write_text("1050\n")          # 1050 mV
        (self.amd / "power1_average").write_text("85000000\n")  # 85 W
        self.reader = RealHardwareReader(hwmon_base=str(self.base))

    def tearDown(self):
        self._tmp.cleanup()

    def test_cpu_temp(self):
        self.assertEqual(self.reader.get_cpu_temp(), 45.0)

    def test_gpu_temp(self):
        self.assertEqual(self.reader.get_gpu_temp(), 50.0)

    def test_gpu_voltage(self):
        self.assertEqual(self.reader.get_gpu_voltage(), 1050)

    def test_gpu_power(self):
        self.assertAlmostEqual(self.reader.get_gpu_power(), 85.0)

    def test_cpu_vid_not_readable_returns_none(self):
        """VID via SMN non implementato → None (limite non verificabile)."""
        self.assertIsNone(self.reader.get_cpu_vid())

    def test_total_power_not_readable_returns_none(self):
        """Potenza CPU non misurabile → None, MAI sottostima del totale."""
        self.assertIsNone(self.reader.get_total_power())

    def test_missing_sensor_returns_none(self):
        (self.k10 / "temp1_input").unlink()
        self.assertIsNone(self.reader.get_cpu_temp())

    def test_get_system_info_real_values_or_none(self):
        """get_system_info: valori reali dove leggibili, None MAI fittizi."""
        info = self.reader.get_system_info()
        self.assertEqual(info["cpu_temp"], 45.0)
        self.assertEqual(info["gpu_temp"], 50.0)
        self.assertEqual(info["gpu_voltage"], 1050)
        self.assertAlmostEqual(info["gpu_power"], 85.0)
        # Non esposti da hwmon → None (mai valori inventati)
        self.assertIsNone(info["cpu_cores"])
        self.assertIsNone(info["gpu_cu"])
        self.assertIsNone(info["cpu_freq"])
        self.assertIsNone(info["total_power"])
        self.assertIsNone(info["is_40cu_enabled"])

    def test_empty_hwmon_dir_returns_none(self):
        reader = RealHardwareReader(hwmon_base="/nonexistent")
        self.assertIsNone(reader.get_gpu_temp())


if __name__ == "__main__":
    unittest.main()
