#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test della configurazione: gli hard limits non sono sovrascrivibili."""

import tempfile
import unittest
from pathlib import Path

from buo.config import BUOConfig
from buo.constants import LIMITS


class TestConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = BUOConfig()
        self.assertEqual(cfg.psu_wattage, 350)
        self.assertEqual(cfg.power_budget, LIMITS.power.power_budget)
        self.assertTrue(cfg.fix_tlb)

    def test_hard_limits_not_overridable(self):
        """Il file YAML non può alzare gli hard limits."""
        cfg = BUOConfig({"safety": {"cpu_vid_recommended_max": 1400}})
        self.assertLessEqual(cfg.cpu_vid_recommended_max,
                             LIMITS.cpu.vid_absolute_max)
        self.assertEqual(cfg.cpu_vid_absolute_max,
                         LIMITS.cpu.vid_absolute_max)

    def test_cpu_vid_lower_bound_clamped(self):
        """Il file YAML non può scendere sotto il VID minimo sicuro."""
        cfg = BUOConfig({"safety": {"cpu_vid_recommended_max": 100}})
        self.assertEqual(cfg.cpu_vid_recommended_max, LIMITS.cpu.vid_min)

    def test_gpu_voltage_clamped(self):
        cfg = BUOConfig({"safety": {"gpu_voltage_recommended_max": 1200}})
        self.assertLessEqual(cfg.gpu_voltage_recommended_max,
                             LIMITS.gpu.voltage_absolute_max)

    def test_power_budget_clamped(self):
        cfg = BUOConfig({"safety": {"power_budget": 500}})
        self.assertLessEqual(cfg.power_budget, LIMITS.power.psu_max)

    def test_load_missing_file_returns_defaults(self):
        cfg = BUOConfig.load(Path("/nonexistent/buo.yaml"))
        self.assertEqual(cfg.psu_wattage, 350)

    def test_roundtrip_dict(self):
        cfg = BUOConfig({"hardware": {"psu_wattage": 400}})
        d = cfg.to_dict()
        self.assertEqual(d["hardware"]["psu_wattage"], 400)
        # gli hard limits restano immutabili anche nella serializzazione
        self.assertEqual(d["safety"]["cpu"]["vid_absolute_max"], 1325)

    def test_nested_hardware_config(self):
        cfg = BUOConfig({"hardware": {"psu_wattage": 400}})
        self.assertEqual(cfg.psu_wattage, 400)
        self.assertEqual(cfg.cooling_type, "push-pull")

    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "buo.yaml"
            cfg = BUOConfig({"hardware": {"cooling_type": "custom"}})
            cfg.save(path)
            loaded = BUOConfig.load(path)
            self.assertEqual(loaded.cooling_type, "custom")


if __name__ == "__main__":
    unittest.main()
