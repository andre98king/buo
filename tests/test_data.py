#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test del data collector (dataset VRAM) e del training ML."""

import os
import tempfile
import unittest

from buo.data.collector import VRAMDataCollector
from buo.models.vram_estimator import VRAMMLModel
from buo.utils.mock import MockHardware


class TestDataCollector(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def test_collect_writes_jsonl(self):
        collector = VRAMDataCollector(mock=True, mock_hardware=MockHardware())
        written = collector.collect(samples=3, interval=0)
        self.assertEqual(written, 3)
        rows = VRAMDataCollector.load_dataset()
        self.assertEqual(len(rows), 3)

    def test_sample_fields(self):
        collector = VRAMDataCollector(mock=True, mock_hardware=MockHardware())
        sample = collector.collect_one()
        self.assertIn("gpu_temp", sample)
        self.assertIn("cpu_temp", sample)
        self.assertTrue(sample["anonymized"])
        self.assertIn("timestamp", sample)

    def test_no_vram_sensor_without_device(self):
        collector = VRAMDataCollector(mock=True, mock_hardware=MockHardware(),
                                      vram_sensor=None)
        sample = collector.collect_one()
        self.assertNotIn("vram_temp_real", sample)

    def test_to_csv(self):
        from buo.utils.paths import state_dir
        collector = VRAMDataCollector(mock=True, mock_hardware=MockHardware())
        collector.collect(samples=2, interval=0)
        rows = VRAMDataCollector.load_dataset()
        path = state_dir() / "dataset" / "export.csv"
        n = VRAMDataCollector.to_csv(rows, path)
        self.assertEqual(n, 2)
        self.assertTrue(path.exists())


class TestMLTrain(unittest.TestCase):
    def test_train_without_sklearn_returns_error(self):
        try:
            import sklearn  # noqa: F401
            self.skipTest("scikit-learn installato")
        except ImportError:
            pass
        model = VRAMMLModel()
        result = model.train([
            {"gpu_temp": 50, "gpu_power": 30, "vram_temp_real": 55},
            {"gpu_temp": 70, "gpu_power": 120, "vram_temp_real": 78},
        ])
        self.assertIn("error", result)

    def test_predict_unavailable_without_model(self):
        model = VRAMMLModel()
        self.assertIsNone(model.predict({"gpu_temp": 70, "gpu_power": 100}))


if __name__ == "__main__":
    unittest.main()
