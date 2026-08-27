#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test del comando doctor (diagnostica in sola lettura)."""

import os
import tempfile
import unittest

from buo.diagnose import Doctor
from buo.utils.mock import MockHardware


class TestDoctor(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name
        os.environ["BUO_DEPS_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        os.environ.pop("BUO_DEPS_DIR", None)
        self._tmp.cleanup()

    def test_diagnose_structure(self):
        doctor = Doctor(mock=True, mock_hardware=MockHardware())
        report = doctor.diagnose()
        for key in ["environment", "distro", "hardware", "problems",
                    "deps", "config", "data", "log_tail"]:
            self.assertIn(key, report)

    def test_hardware_readings(self):
        doctor = Doctor(mock=True, mock_hardware=MockHardware())
        report = doctor.diagnose()
        hw = report["hardware"]
        self.assertEqual(hw["cpu"]["cores"], 6)
        self.assertEqual(hw["gpu"]["cu_count"], 24)

    def test_problems_detected(self):
        doctor = Doctor(mock=True, mock_hardware=MockHardware())
        report = doctor.diagnose()
        self.assertTrue(report["problems"])

    def test_to_text_contains_sections(self):
        doctor = Doctor(mock=True, mock_hardware=MockHardware())
        text = doctor.to_text(doctor.diagnose())
        for section in ["BUO DOCTOR", "HARDWARE", "PROBLEMI",
                        "TOOL COMMUNITY", "LOG"]:
            self.assertIn(section, text)

    def test_to_json_serializable(self):
        doctor = Doctor(mock=True, mock_hardware=MockHardware())
        import json
        data = json.loads(Doctor.to_json(doctor.diagnose()))
        self.assertIn("environment", data)


if __name__ == "__main__":
    unittest.main()
