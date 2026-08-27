#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test del GovernorWrapper: la config.toml deve partire dal template
upstream vendored (integrazione fedele) e adattare SOLO min/max,
temperature e safe-points — tutte le altre sezioni restano intatte.
"""

import tempfile
import unittest
from pathlib import Path

from buo.optimize.governor import GovernorWrapper


def _parse_toml(text: str):
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib
    return tomllib.loads(text)


class TestGovernorConfig(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self._tmp.name) / "config.toml"

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, points, **kw):
        w = GovernorWrapper(mock=False, config_path=str(self.cfg))
        return w.write_config(points, **kw)

    def test_config_keeps_upstream_sections(self):
        """Il config generato deve contenere le sezioni di tuning upstream
        (timing, gpu-usage, load-target, frequency-thresholds, dbus)."""
        ok = self._write([{"freq": 2000, "voltage": 960}])
        self.assertTrue(ok)
        data = _parse_toml(self.cfg.read_text())
        for section in ("timing", "gpu-usage", "gpu", "dbus",
                        "frequency-range", "frequency-thresholds",
                        "load-target", "temperature", "safe-points"):
            self.assertIn(section, data, f"sezione {section} mancante")
        # gpu-usage deve mantenere il fix metriche MangoHud dell'upstream
        self.assertTrue(data["gpu-usage"]["fix-metrics"])
        self.assertEqual(data["gpu"]["set-method"], "smu")

    def test_safe_points_replaced(self):
        """I safe-points devono essere quelli passati (non quelli template)."""
        ok = self._write([
            {"freq": 1000, "voltage": 800},
            {"freq": 2200, "voltage": 1000},
        ])
        self.assertTrue(ok)
        data = _parse_toml(self.cfg.read_text())
        pts = data["safe-points"]
        self.assertEqual(len(pts), 2)
        self.assertEqual(pts[0]["frequency"], 1000)
        self.assertEqual(pts[1]["frequency"], 2200)
        self.assertEqual(pts[1]["voltage"], 1000)

    def test_min_max_and_temperature_adapted(self):
        ok = self._write([{"freq": 1500, "voltage": 900}],
                         min_freq=1200, max_freq=2100,
                         throttling=88, recovery=78)
        self.assertTrue(ok)
        data = _parse_toml(self.cfg.read_text())
        self.assertEqual(data["frequency-range"]["min"], 1200)
        self.assertEqual(data["frequency-range"]["max"], 2100)
        self.assertEqual(data["temperature"]["throttling"], 88)
        self.assertEqual(data["temperature"]["throttling_recovery"], 78)


if __name__ == "__main__":
    unittest.main()
