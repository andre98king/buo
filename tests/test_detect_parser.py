#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test del parser di bc250-detect (formato reale osservato su hardware)."""

import unittest

from buo.unlock.wrappers.bc250_overclock import BC250DetectWrapper


class TestDetectParser(unittest.TestCase):
    def setUp(self):
        self.wrapper = BC250DetectWrapper()

    def test_final_result_format(self):
        """Formato reale: 'Final Result: 3500 MHz @ 1087 mV using scale 0'."""
        out = (
            "Probing SMU Communication... Test Message OK\n"
            "Detected Active Cores: 01234567\n"
            "Attempting to reach 3500 MHz @ 1300 mV, 90°C\n"
            "Stress Testing 3500 MHz @ 1087 mV\n"
            "\n"
            "Final Result: 3500 MHz @ 1087 mV using scale 0\n"
            "Done, config file was written to overclock.conf\n"
            "Restored Default Parameters\n"
        )
        parsed = self.wrapper.parse_output(out, "")
        self.assertTrue(parsed["success"])
        self.assertEqual(parsed["frequency"], 3500)
        self.assertEqual(parsed["vid"], 1087)
        self.assertEqual(parsed["scale"], 0)

    def test_safe_format(self):
        """Formato storico/documentato: 'Safe: 3600 MHz @ -10'."""
        parsed = self.wrapper.parse_output("Safe: 3600 MHz @ -10", "")
        self.assertTrue(parsed["success"])
        self.assertEqual(parsed["frequency"], 3600)
        self.assertEqual(parsed["vid"], -10)
        self.assertIsNone(parsed["scale"])

    def test_no_result_means_failure(self):
        parsed = self.wrapper.parse_output("crash...", "Traceback")
        self.assertFalse(parsed["success"])

    def test_optimizer_uses_real_vid(self):
        """L'ottimizzatore usa il VID reale (1087) e non la conversione."""
        from buo.optimize.cpu import CPUUndervoltOptimizer
        from buo.unlock.wrappers.bc250_overclock import BC250DetectWrapper

        class FakeDetect(BC250DetectWrapper):
            def __init__(self):
                super().__init__()

            @property
            def available(self):
                return True

            def detect(self, target_freq, max_vid, max_temp, keep=False):
                return {
                    "returncode": 0,
                    "stdout": ("Final Result: 3500 MHz @ 1087 mV "
                               "using scale 0\n"),
                    "stderr": "",
                    "parsed_output": self.parse_output(
                        "Final Result: 3500 MHz @ 1087 mV using scale 0\n", ""),
                }

        opt = CPUUndervoltOptimizer(mock=False, use_wrapper=False)
        opt.detect_wrapper = FakeDetect()
        result = opt.optimize(max_freq=3500)
        self.assertEqual(result["v_f_points"][0]["vid"], 1087)
        self.assertEqual(result["v_f_points"][0]["scale"], 0)

    def test_detect_runs_in_writable_cwd(self):
        """bc250-detect scrive overclock.conf: cwd sempre scrivibile (/tmp),
        altrimenti via systemd la cwd '/' è read-only (bug sul campo)."""
        wrapper = BC250DetectWrapper()
        self.assertEqual(wrapper.WORK_DIR, "/tmp")

        from unittest import mock as umock
        with umock.patch.object(wrapper, "run_with_output") as m:
            wrapper.detect(3500, 1300)
            kwargs = m.call_args.kwargs
            self.assertEqual(kwargs.get("cwd"), "/tmp")


if __name__ == "__main__":
    unittest.main()
