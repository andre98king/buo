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

    def test_validation_stress_duration_default_is_10(self):
        """Default 10 min (design PORTABILITY_DEFAULTS 3.3): con una
        config cattiva l'abort termico scatta comunque in ~17s — 30 min
        non aggiungevano protezione, solo tempo bruciato; 10 = soglia L2
        del motore OC."""
        cfg = BUOConfig()
        self.assertEqual(cfg.validation_stress_duration, 10)

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


class TestCpuTargetVid(unittest.TestCase):
    """phases.undervolt.cpu_target_vid — VID target della ricerca CPU
    (vero undervolt = scale NEGATIVA; scoperta sul campo 30/08).

    Default "auto" (design DESIGN_PORTABILITY_DEFAULTS 3.1): il target
    viene derivato a RUNTIME dalla misura live del VID stock
    (clamp(misura−75, 900, 1250) + ladder + fallback no-UV) — un default
    statico fallisce su silicio con floor UV diverso. Un valore NUMERICO
    esplicito nel file vince e mantiene il comportamento odierno: clamp a
    [vid_min, vid_recommended_max]."""

    def test_cpu_target_vid_default_is_auto(self):
        cfg = BUOConfig()
        self.assertEqual(cfg.undervolt_cpu_target_vid, "auto")

    def test_cpu_target_vid_custom_value_accepted(self):
        cfg = BUOConfig({"phases": {"undervolt": {"cpu_target_vid": 1000}}})
        self.assertEqual(cfg.undervolt_cpu_target_vid, 1000)
        self.assertIsInstance(cfg.undervolt_cpu_target_vid, int)

    def test_cpu_target_vid_explicit_auto_accepted(self):
        cfg = BUOConfig({"phases": {"undervolt": {"cpu_target_vid": "auto"}}})
        self.assertEqual(cfg.undervolt_cpu_target_vid, "auto")

    def test_cpu_target_vid_above_recommended_clamped(self):
        cfg = BUOConfig({"phases": {"undervolt": {"cpu_target_vid": 2000}}})
        self.assertEqual(cfg.undervolt_cpu_target_vid,
                         LIMITS.cpu.vid_recommended_max)

    def test_cpu_target_vid_below_vid_min_clamped(self):
        cfg = BUOConfig({"phases": {"undervolt": {"cpu_target_vid": 500}}})
        self.assertEqual(cfg.undervolt_cpu_target_vid, LIMITS.cpu.vid_min)

    def test_cpu_target_vid_is_known_key(self):
        """Chiave nota dello schema piatto: nessun avviso fail-soft."""
        with self.assertNoLogs("buo.config", level="WARNING"):
            BUOConfig({"phases": {"undervolt": {"cpu_target_vid": 1000}}})

    def test_cpu_target_vid_in_to_dict(self):
        cfg = BUOConfig({"phases": {"undervolt": {"cpu_target_vid": 1000}}})
        d = cfg.to_dict()
        self.assertEqual(d["phases"]["undervolt"]["cpu_target_vid"], 1000)

    def test_cpu_target_vid_auto_in_to_dict_roundtrip(self):
        """Default "auto": to_dict/save/load preservano la sentinella."""
        cfg = BUOConfig()
        d = cfg.to_dict()
        self.assertEqual(d["phases"]["undervolt"]["cpu_target_vid"], "auto")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "buo.yaml"
            cfg.save(path)
            loaded = BUOConfig.load(path)
            self.assertEqual(loaded.undervolt_cpu_target_vid, "auto")


class TestCpuSearchFreq(unittest.TestCase):
    """phases.undervolt.cpu_search_freq (NUOVA chiave, design
    PORTABILITY_DEFAULTS 3.2): la ricerca UV gira a questa frequenza
    (default 3500 stock) — mai parte da cpu_freq_max 4000 (il punto
    trovato da bc250-detect È la frequenza applicata; f-alta + deep-UV
    = zona di wedge/hang misurata). cpu_freq_max resta il soffitto."""

    def test_default_is_stock_freq(self):
        cfg = BUOConfig()
        self.assertEqual(cfg.undervolt_cpu_search_freq, LIMITS.cpu.freq_min)

    def test_explicit_value_accepted(self):
        cfg = BUOConfig({"phases": {"undervolt": {"cpu_search_freq": 3800}}})
        self.assertEqual(cfg.undervolt_cpu_search_freq, 3800)

    def test_above_freq_max_clamped(self):
        cfg = BUOConfig({"phases": {"undervolt": {"cpu_search_freq": 4500}}})
        self.assertEqual(cfg.undervolt_cpu_search_freq, LIMITS.cpu.freq_max)

    def test_is_known_key(self):
        with self.assertNoLogs("buo.config", level="WARNING"):
            BUOConfig({"phases": {"undervolt": {"cpu_search_freq": 3800}}})

    def test_in_to_dict_roundtrip(self):
        cfg = BUOConfig({"phases": {"undervolt": {"cpu_search_freq": 3800}}})
        d = cfg.to_dict()
        self.assertEqual(d["phases"]["undervolt"]["cpu_search_freq"], 3800)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "buo.yaml"
            cfg.save(path)
            loaded = BUOConfig.load(path)
            self.assertEqual(loaded.undervolt_cpu_search_freq, 3800)


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


class TestOstreeConfig(unittest.TestCase):
    """Sezione ostree (design OSTREE_REBOOT, D5): auto_swap_default con
    default ON + schema piatto con avviso sulle chiavi sconosciute."""

    def test_auto_swap_default_is_true(self):
        cfg = BUOConfig()
        self.assertTrue(cfg.ostree_auto_swap_default)

    def test_auto_swap_false_honored(self):
        cfg = BUOConfig({"ostree": {"auto_swap_default": False}})
        self.assertFalse(cfg.ostree_auto_swap_default)

    def test_known_key_no_warning(self):
        with self.assertNoLogs("buo.config", level="WARNING"):
            BUOConfig({"ostree": {"auto_swap_default": False}})

    def test_unknown_key_warns(self):
        """Chiave sconosciuta in ostree: → warning fail-soft (mai
        silenziosa), valore ignorato."""
        with self.assertLogs("buo.config", level="WARNING") as cm:
            cfg = BUOConfig({"ostree": {"foo_bar": 1}})
        self.assertIn("ostree.foo_bar", "\n".join(cm.output))
        self.assertTrue(cfg.ostree_auto_swap_default)

    def test_in_to_dict_roundtrip(self):
        cfg = BUOConfig({"ostree": {"auto_swap_default": False}})
        d = cfg.to_dict()
        self.assertFalse(d["ostree"]["auto_swap_default"])
        # round-trip save/load
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "buo.yaml"
            cfg.save(path)
            loaded = BUOConfig.load(path)
            self.assertFalse(loaded.ostree_auto_swap_default)


if __name__ == "__main__":
    unittest.main()
