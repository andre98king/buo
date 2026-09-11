#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test fan: persistenza al boot (G7) + EFFETTO reale dei sensori (F4)."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from buo.fix import fan as fan_mod
from buo.fix.fan import FanControl


class TestFanSensorEffect(unittest.TestCase):
    """F4: verify() guarda l'EFFETTO (hwmon nct6686 con ventole/PWM), non
    `lsmod` — un modulo caricato senza `force=true` non espone nulla."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _hwmon(self, name="nct6686", files=()):
        d = self.base / "hwmon2"
        d.mkdir(exist_ok=True)
        (d / "name").write_text(name + "\n")
        for fname, value in files:
            (d / fname).write_text(value + "\n")
        return self.base

    def test_verify_true_when_fan_spins(self):
        base = self._hwmon(files=[("fan1_input", "0"), ("fan2_input", "2090")])
        ok, why = fan_mod.sensor_effect(str(base))
        self.assertTrue(ok)
        self.assertIn("2090", why)

    def test_verify_true_when_pwm_enabled(self):
        base = self._hwmon(files=[("fan1_input", "0"), ("pwm2_enable", "2")])
        ok, why = fan_mod.sensor_effect(str(base))
        self.assertTrue(ok)
        self.assertIn("pwm2_enable", why)

    def test_verify_false_when_module_present_without_effect(self):
        base = self._hwmon(files=[("fan1_input", "0"), ("pwm1_enable", "0")])
        ok, why = fan_mod.sensor_effect(str(base))
        self.assertFalse(ok)
        self.assertIn("nct6686", why)

    def test_verify_false_without_hwmon(self):
        ok, why = fan_mod.sensor_effect(str(self.base))
        self.assertFalse(ok)
        self.assertIn("nessun hwmon", why)

    def test_verify_false_when_base_unreadable(self):
        ok, why = fan_mod.sensor_effect("/nonexistent/hwmon")
        self.assertFalse(ok)
        self.assertIn("non leggibile", why)

    def test_fancontrol_verify_uses_the_effect(self):
        """`lsmod` non conta più: senza hwmon nct6686 verify() è False."""
        with mock.patch.object(fan_mod, "HWMON_BASE", str(self.base)):
            self.assertFalse(FanControl().verify())
        base = self._hwmon(files=[("fan2_input", "1800")])
        with mock.patch.object(fan_mod, "HWMON_BASE", str(base)):
            self.assertTrue(FanControl().verify())


class TestFanPersistence(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.mload = Path(self._tmp.name) / "modules-load.d"
        self.mopt = Path(self._tmp.name) / "modprobe.d"
        self.mload.mkdir()
        self.mopt.mkdir()
        self.mload_c = self.mload / "nct6683.conf"
        self.mopt_c = self.mopt / "nct6683.conf"

    def tearDown(self):
        self._tmp.cleanup()

    def test_persist_writes_both_files(self):
        with mock.patch.object(fan_mod, "MODULES_LOAD", self.mload_c), \
             mock.patch.object(fan_mod, "MODPROBE_OPTS", self.mopt_c):
            fan = FanControl()
            ok = fan._persist()
        self.assertTrue(ok)
        self.assertEqual(self.mload_c.read_text(), "nct6683\n")
        self.assertEqual(self.mopt_c.read_text(),
                         "options nct6683 force=true\n")

    def test_apply_persists_when_modprobe_ok(self):
        with mock.patch.object(fan_mod, "MODULES_LOAD", self.mload_c), \
             mock.patch.object(fan_mod, "MODPROBE_OPTS", self.mopt_c), \
             mock.patch("buo.fix.fan.run_command",
                        return_value=(0, "", "")):
            fan = FanControl()
            out = fan.apply()
        self.assertTrue(out["applied"])
        self.assertTrue(out["persisted"])
        self.assertTrue(self.mload_c.exists())

    def test_apply_not_persist_when_modprobe_fails(self):
        with mock.patch.object(fan_mod, "MODULES_LOAD", self.mload_c), \
             mock.patch.object(fan_mod, "MODPROBE_OPTS", self.mopt_c), \
             mock.patch("buo.fix.fan.run_command",
                        return_value=(1, "", "boom")):
            fan = FanControl()
            out = fan.apply()
        self.assertFalse(out["applied"])
        self.assertFalse(out["persisted"])

    def test_rollback_removes_persistence_files(self):
        self.mload_c.write_text("nct6683\n")
        self.mopt_c.write_text("options nct6683 force=true\n")
        with mock.patch.object(fan_mod, "MODULES_LOAD", self.mload_c), \
             mock.patch.object(fan_mod, "MODPROBE_OPTS", self.mopt_c), \
             mock.patch("buo.fix.fan.run_command", return_value=(0, "", "")):
            fan = FanControl()
            ok = fan.rollback()
        self.assertTrue(ok)
        self.assertFalse(self.mload_c.exists())
        self.assertFalse(self.mopt_c.exists())


if __name__ == "__main__":
    unittest.main()
