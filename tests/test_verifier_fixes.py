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

    def test_gtt_checks_runtime_param(self):
        """Il verifier deve leggere l'EFFETTO (parametro runtime), non la
        presenza del conf: campo 10/09, conf presente ma parametro al
        default (initramfs non rigenerato) → fix INERTE, ok=False."""
        import tempfile
        from pathlib import Path
        from buo.fix import gtt as gtt_mod
        from buo.fix.gtt import GTT_LIMIT_DEFAULT

        v = FixVerifier(mock=False)
        with tempfile.TemporaryDirectory() as tmp:
            params = Path(tmp) / "pages_limit"
            with mock.patch.object(gtt_mod, "GTT_PARAM_PATH", str(params)):
                params.write_text("1944679\n", encoding="utf-8")
                ok, detail = v._check_gtt()
                self.assertFalse(ok)
                self.assertIn("1944679", detail)

                params.write_text(f"{GTT_LIMIT_DEFAULT}\n", encoding="utf-8")
                ok, detail = v._check_gtt()
                self.assertTrue(ok)
                self.assertIn(str(GTT_LIMIT_DEFAULT), detail)

    def test_vram_not_verifiable_returns_none(self):
        v = FixVerifier(mock=False)
        ok, detail = v._check_vram()
        self.assertIsNone(ok)
        self.assertIn("manuale", detail)


if __name__ == "__main__":
    unittest.main()
