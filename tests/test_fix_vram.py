#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test del fixer VRAMConfig (split VRAM via bc250_memcfg).

La configurazione VRAM è un fix MANUALE: BUO non esegue mai `bc250_memcfg`
da solo. Su hardware reale `apply()` restituisce `applied=False` con un
`warning` (mai un `error`), quindi `_classify_fix` lo classifica come
"manual" e NON come "failed". Questo test blocca il comportamento reale,
contro il falso "failed" riportato in un vecchio run report.
"""

import unittest

from buo.fix.vram import VRAMConfig
from buo.orchestrator import Orchestrator
from buo.utils.mock import MockHardware


class TestVRAMConfig(unittest.TestCase):
    def test_real_apply_without_memcfg_is_manual_not_failed(self):
        fix = VRAMConfig(mock=False)
        result = fix.apply()
        self.assertFalse(result["applied"])
        self.assertNotIn("error", result)
        self.assertEqual(Orchestrator._classify_fix(result), "manual")

    def test_real_apply_formats_gpu_memory_value(self):
        fix = VRAMConfig(mock=False)
        result = fix.apply()
        self.assertIn("bc250_memcfg --set-vram 8G", result["warning"])
        self.assertNotIn("{gpu_memory_gb}", result["warning"])

    def test_out_of_range_returns_error(self):
        fix = VRAMConfig(mock=False)
        result = fix.apply(gpu_memory_gb=15)
        self.assertIn("error", result)
        self.assertEqual(Orchestrator._classify_fix(result), "failed")

    def test_mock_apply_returns_applied(self):
        fix = VRAMConfig(mock=True, mock_hardware=MockHardware())
        result = fix.apply()
        self.assertTrue(result["applied"])

    def test_verify_is_false(self):
        self.assertFalse(VRAMConfig(mock=False).verify())


if __name__ == "__main__":
    unittest.main()
