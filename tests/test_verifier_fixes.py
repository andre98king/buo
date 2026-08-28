#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test dei checker FixVerifier per gtt/fan/vram (prima il report mostrava
"nessuna verifica definita").
"""

import unittest
from unittest import mock

from buo.validate.verify import FixVerifier


class TestVerifierNewCheckers(unittest.TestCase):
    def test_checker_keys_present(self):
        """I fix gtt/fan/vram devono avere un checker registrato."""
        v = FixVerifier(mock=True)
        results = v.verify_all(["gtt_tuning", "fan_control", "vram_config"])
        for fix in ("gtt_tuning", "fan_control", "vram_config"):
            self.assertIn(fix, results)
            # in mock i checker risolvono ok (o None per vram manuale)
            self.assertIsNotNone(results[fix]["detail"],
                                 f"{fix}: detail mancante")

    @mock.patch("buo.validate.verify.run_command",
                return_value=(0, "nct6683", ""))
    def test_fan_checks_lsmod(self, _rc):
        v = FixVerifier(mock=False)
        ok, detail = v._check_fan()
        self.assertTrue(ok)
        self.assertIn("nct6683", detail)

    @mock.patch("buo.validate.verify.run_command",
                return_value=(0, "qualcosaltro", ""))
    def test_fan_negative_when_module_missing(self, _rc):
        v = FixVerifier(mock=False)
        ok, _ = v._check_fan()
        self.assertFalse(ok)

    def test_gtt_checks_conf(self):
        v = FixVerifier(mock=False)
        with mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("builtins.open",
                        mock.mock_open(read_data="root=UUID=x rw")):
            ok, detail = v._check_gtt()
            self.assertTrue(ok)
            self.assertIn("buo-gtt.conf", detail)

    def test_vram_not_verifiable_returns_none(self):
        v = FixVerifier(mock=False)
        ok, detail = v._check_vram()
        self.assertIsNone(ok)
        self.assertIn("manuale", detail)


if __name__ == "__main__":
    unittest.main()
