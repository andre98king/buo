#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test della dashboard HTML del report."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from buo.report.dashboard import generate_html_dashboard


class TestDashboard(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def _write_report(self, path: Path):
        data = {
            "generated_at": "2026-08-27T00:00:00",
            "performance_gain": {"gpu_fps": "+50%"},
            "benchmarks": {
                "before": {"gpu_stress": {"fps": 45.0, "temperature": 80.0}},
                "after": {"gpu_stress": {"fps": 72.0, "temperature": 67.0}},
            },
            "fixes_verification": {"gpu_40cu": {"ok": True, "detail": "40 CU"}},
            "problems_found": [{"severity": "alta", "title": "TLB fault"}],
        }
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_generates_self_contained_html(self):
        report = Path(self._tmp.name) / "report.json"
        self._write_report(report)
        out = generate_html_dashboard(report)
        self.assertTrue(out.exists())
        html = out.read_text(encoding="utf-8")
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("BUO", html)
        self.assertIn("+50%", html)          # performance gain incorporato
        self.assertIn("72.0", html)          # benchmark after
        self.assertIn("<script>", html)      # JS vanilla (nessuna dipendenza)
        self.assertNotIn("plotly", html)

    def test_missing_report_raises(self):
        with self.assertRaises(FileNotFoundError):
            generate_html_dashboard(Path(self._tmp.name) / "nope.json")

    def test_output_path_is_sibling(self):
        report = Path(self._tmp.name) / "report.json"
        self._write_report(report)
        out = generate_html_dashboard(report)
        self.assertEqual(out, report.with_suffix(".html"))


if __name__ == "__main__":
    unittest.main()
