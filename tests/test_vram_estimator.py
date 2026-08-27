#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test dello stimatore VRAM (modello empirico + calibrazione)."""

import unittest

from buo.models.vram_estimator import VRAMTemperatureEstimator


class TestVRAMEstimator(unittest.TestCase):
    def setUp(self):
        self.est = VRAMTemperatureEstimator()

    def test_defaults(self):
        self.assertEqual(self.est.alpha, 0.45)
        self.assertEqual(self.est.beta, 0.04)

    def test_idle_estimate(self):
        result = self.est.estimate(gpu_temp=45.0, gpu_power=20.0)
        # T_vram = 22 + 0.45*(45-22) + 0.04*20 = 22 + 10.35 + 0.8 = 33.15
        self.assertAlmostEqual(result.raw_temperature, 33.15, places=2)
        self.assertAlmostEqual(result.temperature, 33.15, places=2)

    def test_heavy_load_estimate(self):
        result = self.est.estimate(gpu_temp=80.0, gpu_power=200.0)
        # 22 + 0.45*58 + 8 = 22 + 26.1 + 8 = 56.1
        self.assertAlmostEqual(result.raw_temperature, 56.1, places=2)

    def test_clamping(self):
        result = self.est.estimate(gpu_temp=200.0, gpu_power=5000.0)
        self.assertLessEqual(result.raw_temperature, 120.0)

    def test_smoothing_converges(self):
        """Con letture costanti, la stima converge al valore grezzo."""
        est = VRAMTemperatureEstimator(tau=0.2)
        for _ in range(200):
            result = est.estimate(gpu_temp=70.0, gpu_power=150.0)
        self.assertLess(abs(result.temperature - result.raw_temperature), 0.5)

    def test_confidence_range(self):
        for _ in range(60):
            self.est.estimate(gpu_temp=70.0, gpu_power=150.0)
        result = self.est.estimate(gpu_temp=70.0, gpu_power=150.0)
        self.assertTrue(0.0 <= result.confidence <= 1.0)

    def test_warning_counting(self):
        est = VRAMTemperatureEstimator()
        for _ in range(5):
            est.estimate(gpu_temp=140.0, gpu_power=400.0)
        stats = est.get_stats()
        self.assertGreaterEqual(stats["warnings"], 1)

    def test_calibrate_requires_3_points(self):
        result = self.est.calibrate([
            {"gpu_temp": 45, "gpu_power": 20, "vram_temp_real": 48},
        ])
        self.assertEqual(result["alpha"], 0.45)  # non calibrato

    def test_calibrate_with_numpy(self):
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("numpy non installato")
        points = [
            {"gpu_temp": 45, "gpu_power": 20, "vram_temp_real": 48},
            {"gpu_temp": 60, "gpu_power": 80, "vram_temp_real": 65},
            {"gpu_temp": 75, "gpu_power": 150, "vram_temp_real": 82},
            {"gpu_temp": 80, "gpu_power": 200, "vram_temp_real": 90},
        ]
        result = self.est.calibrate(points)
        self.assertTrue(0.1 <= result["alpha"] <= 0.9)
        self.assertTrue(0.01 <= result["beta"] <= 0.1)

    def test_ml_model_unavailable_without_sklearn(self):
        """Senza sklearn il modello ML non è disponibile ma non rompe nulla."""
        model = VRAMTemperatureEstimator()
        self.assertIsNotNone(model.estimate(70, 150))


if __name__ == "__main__":
    unittest.main()
