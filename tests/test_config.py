#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test della configurazione: gli hard limits non sono sovrascrivibili."""

import tempfile
import unittest
from pathlib import Path

from buo.config import BUOConfig
from buo.constants import LIMITS


class TestConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = BUOConfig()
        self.assertEqual(cfg.psu_wattage, 350)
        self.assertEqual(cfg.power_budget, LIMITS.power.power_budget)
        self.assertTrue(cfg.fix_tlb)

    def test_hard_limits_not_overridable(self):
        """Il file YAML non può alzare gli hard limits."""
        cfg = BUOConfig({"safety": {"cpu_vid_recommended_max": 1400}})
        self.assertLessEqual(cfg.cpu_vid_recommended_max,
                             LIMITS.cpu.vid_absolute_max)
        self.assertEqual(cfg.cpu_vid_absolute_max,
                         LIMITS.cpu.vid_absolute_max)

    def test_cpu_vid_lower_bound_clamped(self):
        """Il file YAML non può scendere sotto il VID minimo sicuro."""
        cfg = BUOConfig({"safety": {"cpu_vid_recommended_max": 100}})
        self.assertEqual(cfg.cpu_vid_recommended_max, LIMITS.cpu.vid_min)

    def test_gpu_voltage_clamped(self):
        cfg = BUOConfig({"safety": {"gpu_voltage_recommended_max": 1200}})
        self.assertLessEqual(cfg.gpu_voltage_recommended_max,
                             LIMITS.gpu.voltage_absolute_max)

    def test_power_budget_clamped(self):
        cfg = BUOConfig({"safety": {"power_budget": 500}})
        self.assertLessEqual(cfg.power_budget, LIMITS.power.psu_max)

    def test_load_missing_file_returns_defaults(self):
        cfg = BUOConfig.load(Path("/nonexistent/buo.yaml"))
        self.assertEqual(cfg.psu_wattage, 350)

    def test_roundtrip_dict(self):
        cfg = BUOConfig({"hardware": {"psu_wattage": 400}})
        d = cfg.to_dict()
        self.assertEqual(d["hardware"]["psu_wattage"], 400)
        # safety è serializzato PIATTO (schema di config/buo.yaml): il
        # parser legge chiavi piatte, non la vecchia forma annidata
        # safety.cpu.{...} (fix 30/08 — i valori annidati andavano persi)
        self.assertEqual(d["safety"]["cpu_freq_max"], LIMITS.cpu.freq_max)
        self.assertEqual(d["safety"]["gpu_voltage_recommended_max"],
                         LIMITS.gpu.voltage_recommended_max)
        self.assertEqual(d["safety"]["power_budget"],
                         LIMITS.power.power_budget)

    def test_nested_hardware_config(self):
        cfg = BUOConfig({"hardware": {"psu_wattage": 400}})
        self.assertEqual(cfg.psu_wattage, 400)
        self.assertEqual(cfg.cooling_type, "push-pull")

    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "buo.yaml"
            cfg = BUOConfig({"hardware": {"cooling_type": "custom"}})
            cfg.save(path)
            loaded = BUOConfig.load(path)
            self.assertEqual(loaded.cooling_type, "custom")


class TestUnknownConfigKeys(unittest.TestCase):
    """Avviso fail-soft (MAI bloccante) per chiavi config sconosciute o
    strutture annidate (fix 30/08): BUO legge chiavi PIATTE sotto safety:
    (es. cpu_freq_max) ma un file con safety.cpu.freq_max annidato veniva
    ignorato SILENZIOSAMENTE — l'utente crede di aver alzato/abbassato un
    limite che invece non viene applicato (pericoloso)."""

    def test_warns_on_nested_safety_keys(self):
        """safety.cpu: {freq_max: 3000} → warning esplicito e valore NON
        applicato (default invariato)."""
        with self.assertLogs("buo.config", level="WARNING") as cm:
            cfg = BUOConfig({"safety": {"cpu": {"freq_max": 3000}}})
        joined = "\n".join(cm.output)
        self.assertIn("safety.cpu.freq_max", joined)
        self.assertIn("safety.cpu_freq_max", joined)
        self.assertIn("config/buo.yaml", joined)
        self.assertEqual(cfg.cpu_freq_max, LIMITS.cpu.freq_max)

    def test_warns_on_unknown_flat_safety_key(self):
        """Chiave piatta sconosciuta (safety.foo_bar) → warning."""
        with self.assertLogs("buo.config", level="WARNING") as cm:
            BUOConfig({"safety": {"foo_bar": 123}})
        self.assertIn("safety.foo_bar", "\n".join(cm.output))

    def test_no_warning_on_flat_correct_config(self):
        """Config piatta con chiavi note → nessun warning."""
        with self.assertNoLogs("buo.config", level="WARNING"):
            BUOConfig({"safety": {"cpu_freq_max": 3000,
                                  "power_budget": 200}})

    def test_no_warning_on_known_phases(self):
        """phases.* con chiavi note (schema piatto per sezione) →
        nessun warning."""
        with self.assertNoLogs("buo.config", level="WARNING"):
            BUOConfig({"phases": {"probe": {"cpu_unlock": True},
                                  "undervolt": {"persist": True}}})


if __name__ == "__main__":
    unittest.main()
