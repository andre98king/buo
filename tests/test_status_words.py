#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test delle celle di stato PAROLE di `buo status` (spec §5.2).

Le celle Stato/Valore usano il vocabolario italiano (ok/parziale/ridotte/
critico/attive/stock) al posto delle emoji ✅/⚠️/🔴/💤; Fix e 40-CU sono
le prime righe. Formati '{n}/8' e '{n}/40' INVARIATI. Mai hardware reale.
"""

import os
import tempfile
import unittest
from unittest import mock

from click.testing import CliRunner

from buo.cli import cli


class _FakeReader:
    """Stessa interfaccia di RealHardwareReader (valori controllabili)."""

    def __init__(self, is_40cu_enabled=True):
        self._is_40cu = is_40cu_enabled

    def get_system_info(self):
        return {
            "core_mask": "0xFF",
            "cpu_cores": 8,
            "cpu_freq": 3825,
            "cpu_vid": 1125,
            "cpu_temp": 77.2,
            "gpu_cu": 40,
            "gpu_freq": 1800,
            "gpu_voltage": 800,
            "gpu_temp": 63.1,
            "gpu_power": 95.0,
            "total_power": 118.0,
            "ambient_temp": 46.0,
            "fan_speed": 1800,
            "is_40cu_enabled": self._is_40cu,
        }


class TestStatusWords(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name
        os.environ["BUO_DEPS_DIR"] = self._tmp.name
        self.runner = CliRunner()

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        os.environ.pop("BUO_DEPS_DIR", None)
        self._tmp.cleanup()

    def _invoke(self, args):
        return self.runner.invoke(cli, args)

    def test_status_mock_shows_stock_without_emoji(self):
        """§5.2: 40-CU non attiva → Valore 'stock', Stato '—'; nessuna
        emoji nelle celle (✅/⚠️/🔴/💤 via). Formati numerici invariati."""
        res = self._invoke(["status", "--mock"])
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn("stock", res.output)
        self.assertIn("6/8", res.output)     # formato INVARIATO
        self.assertIn("24/40", res.output)   # formato INVARIATO
        self.assertNotIn("✅", res.output)
        self.assertNotIn("⚠️", res.output)
        self.assertNotIn("🔴", res.output)
        self.assertNotIn("💤", res.output)

    def test_status_40cu_attive_shows_ok(self):
        """40-CU attive → Valore 'attive', Stato 'ok'."""
        with mock.patch("buo.safety.reader.RealHardwareReader",
                        return_value=_FakeReader(is_40cu_enabled=True)):
            res = self._invoke(["status"])
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn("attive", res.output)
        self.assertIn("ok", res.output)
        self.assertNotIn("stock", res.output)

    def test_status_40cu_none_shows_non_rilevabile(self):
        """C1: 40-CU non determinabile → 'non rilevabile' (mai inventato)."""
        with mock.patch("buo.safety.reader.RealHardwareReader",
                        return_value=_FakeReader(is_40cu_enabled=None)):
            res = self._invoke(["status"])
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn("non rilevabile", res.output)

    def test_status_rows_order_fix_and_40cu_first(self):
        """§5.1: stato ottimizzazione in alto → Fix, poi 40-CU, poi i
        sensori (CPU Core...)."""
        with mock.patch("buo.safety.reader.RealHardwareReader",
                        return_value=_FakeReader(is_40cu_enabled=True)):
            res = self._invoke(["status"])
        self.assertEqual(res.exit_code, 0, res.output)
        out = res.output
        self.assertLess(out.index("Fix"), out.index("40-CU"))
        self.assertLess(out.index("40-CU"), out.index("CPU Core"))


if __name__ == "__main__":
    unittest.main()
