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

    def test_level_ok_below_warning(self):
        est = VRAMTemperatureEstimator(warning_threshold=82.0,
                                       critical_threshold=92.0)
        result = est.estimate(gpu_temp=60.0, gpu_power=100.0)
        self.assertEqual(result.level, "ok")

    def test_level_warning_between_thresholds(self):
        est = VRAMTemperatureEstimator(warning_threshold=82.0,
                                       critical_threshold=92.0)
        result = est.estimate(gpu_temp=160.0, gpu_power=0.0)
        self.assertEqual(result.level, "warning")

    def test_level_critical_above_critical(self):
        est = VRAMTemperatureEstimator(warning_threshold=82.0,
                                       critical_threshold=92.0)
        result = est.estimate(gpu_temp=200.0, gpu_power=5000.0)
        self.assertEqual(result.level, "critical")

    def test_level_defaults_to_ok(self):
        est = VRAMTemperatureEstimator()
        result = est.estimate(gpu_temp=45.0, gpu_power=20.0)
        self.assertEqual(result.level, "ok")

    def test_reset_clears_state(self):
        est = VRAMTemperatureEstimator()
        est.estimate(gpu_temp=200.0, gpu_power=5000.0)  # critical
        est.estimate(gpu_temp=70.0, gpu_power=150.0)
        self.assertGreater(len(est._history), 0)
        self.assertGreater(est._estimates_count, 0)
        self.assertGreater(est._critical_count, 0)

        est.reset()

        self.assertIsNone(est._filtered_temp)
        self.assertIsNone(est._last_time)
        self.assertEqual(len(est._history), 0)
        self.assertEqual(est._estimates_count, 0)
        self.assertEqual(est._warning_count, 0)
        self.assertEqual(est._critical_count, 0)

    def test_reset_next_estimate_starts_fresh(self):
        est = VRAMTemperatureEstimator(tau=0.2)
        est.estimate(gpu_temp=70.0, gpu_power=150.0)
        est.reset()
        result = est.estimate(gpu_temp=70.0, gpu_power=150.0)
        self.assertAlmostEqual(result.temperature, result.raw_temperature,
                               places=6)

    def test_str_emoji_per_level(self):
        ok_est = VRAMTemperatureEstimator(warning_threshold=82.0,
                                          critical_threshold=92.0)
        ok_result = ok_est.estimate(gpu_temp=60.0, gpu_power=100.0)
        self.assertEqual(str(ok_result), "✅ 43.1°C (conf: 50%)")

        warn_est = VRAMTemperatureEstimator(warning_threshold=82.0,
                                            critical_threshold=92.0)
        warn_result = warn_est.estimate(gpu_temp=160.0, gpu_power=0.0)
        self.assertEqual(str(warn_result), "⚠️ 84.1°C (conf: 50%)")

        crit_est = VRAMTemperatureEstimator(warning_threshold=82.0,
                                            critical_threshold=92.0)
        crit_result = crit_est.estimate(gpu_temp=200.0, gpu_power=5000.0)
        self.assertEqual(str(crit_result), "🔴 120.0°C (conf: 50%)")


if __name__ == "__main__":
    unittest.main()
