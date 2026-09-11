#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test dei checker FixVerifier: ogni check guarda l'EFFETTO reale a runtime,
non la presenza di un file/conf/nome (principio "applicato ≠ verificato").
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from buo.audit.hardware import HardwareAudit
from buo.fix import fan as fan_mod
from buo.validate.verify import FixVerifier


def _hwmon(tmp, name="nct6686", files=()):
    """hwmon finto: dir hwmonN con `name` + file passati."""
    base = Path(tmp)
    d = base / "hwmon3"
    d.mkdir()
    (d / "name").write_text(name + "\n")
    for fname, value in files:
        (d / fname).write_text(value + "\n")
    return base


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


class TestFanEffect(unittest.TestCase):
    """F4: l'effetto sono i sensori/PWM, non `lsmod`."""

    def test_fan_ok_with_spinning_fan(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = _hwmon(tmp, files=[("fan1_input", "0"),
                                      ("fan2_input", "2090")])
            v = FixVerifier(mock=False)
            with mock.patch.object(fan_mod, "HWMON_BASE", str(base)):
                ok, detail = v._check_fan()
        self.assertTrue(ok)
        self.assertIn("2090", detail)

    def test_fan_ok_with_enabled_pwm(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = _hwmon(tmp, files=[("fan1_input", "0"),
                                      ("pwm2_enable", "2")])
            v = FixVerifier(mock=False)
            with mock.patch.object(fan_mod, "HWMON_BASE", str(base)):
                ok, detail = v._check_fan()
        self.assertTrue(ok)
        self.assertIn("pwm2_enable", detail)

    def test_fan_false_when_module_loaded_but_no_sensor_effect(self):
        """Modulo caricato ma nessuna ventola/PWM: `lsmod` direbbe sì."""
        with tempfile.TemporaryDirectory() as tmp:
            base = _hwmon(tmp, files=[("fan1_input", "0"),
                                      ("pwm1_enable", "0")])
            v = FixVerifier(mock=False)
            with mock.patch.object(fan_mod, "HWMON_BASE", str(base)):
                ok, detail = v._check_fan()
        self.assertFalse(ok)
        self.assertIn("nct6686", detail)

    def test_fan_false_and_explains_when_no_hwmon(self):
        with tempfile.TemporaryDirectory() as tmp:
            v = FixVerifier(mock=False)
            with mock.patch.object(fan_mod, "HWMON_BASE", tmp):
                ok, detail = v._check_fan()
        self.assertFalse(ok)
        self.assertIn("nessun hwmon", detail)

    def test_fan_false_when_hwmon_unreadable(self):
        v = FixVerifier(mock=False)
        with mock.patch.object(fan_mod, "HWMON_BASE", "/nonexistent/hwmon"):
            ok, detail = v._check_fan()
        self.assertFalse(ok)
        self.assertIn("non leggibile", detail)


class TestCpuCoresEffect(unittest.TestCase):
    """F1: 8 core FISICI / 16 thread, non le righe `processor` (thread)."""

    def _check(self, cores, threads):
        v = FixVerifier(mock=False)
        with mock.patch.object(HardwareAudit, "_count_cpuinfo",
                               return_value=cores), \
             mock.patch("buo.unlock.validation.cpu_online_count",
                        return_value=threads):
            return v._check_cpu_cores()

    def test_6c12t_does_not_pass(self):
        ok, detail = self._check(6, 12)
        self.assertIs(ok, False)
        self.assertIn("6 core fisici, 12 thread", detail)

    def test_8c16t_passes(self):
        ok, detail = self._check(8, 16)
        self.assertIs(ok, True)
        self.assertIn("attesi 8/16", detail)

    def test_not_verifiable_when_threads_unreadable(self):
        ok, detail = self._check(8, None)
        self.assertIsNone(ok)
        self.assertIn("non verificabile", detail)

    def test_not_verifiable_when_core_count_unreadable(self):
        ok, detail = self._check(0, 16)
        self.assertIsNone(ok)
        self.assertIn("non verificabile", detail)


class TestGpuCuEffect(unittest.TestCase):
    """F2/F3: CU dal punto unico dell'audit; il conf stale non è un effetto."""

    def _gpu(self, cu, source):
        return mock.patch.object(HardwareAudit, "_audit_gpu",
                                 return_value={"cu_count": cu,
                                               "cu_source": source})

    def test_40cu_from_runtime_passes(self):
        v = FixVerifier(mock=False)
        with self._gpu(40, "runtime"):
            self.assertIs(v._check_gpu_cu()[0], True)
        with self._gpu(40, "sysfs"):
            self.assertIs(v._check_gpu_cu()[0], True)

    def test_24cu_from_runtime_fails(self):
        v = FixVerifier(mock=False)
        with self._gpu(24, "runtime"):
            ok, detail = v._check_gpu_cu()
        self.assertIs(ok, False)
        self.assertIn("24", detail)

    def test_conf_only_is_not_verifiable(self):
        """Un conf stale dichiara 40 CU: non è un effetto → non verificabile."""
        v = FixVerifier(mock=False)
        with self._gpu(40, "conf (stale?)"):
            ok, detail = v._check_gpu_cu()
        self.assertIsNone(ok)
        self.assertIn("non verificabile", detail)

    def test_undeterminable_is_not_verifiable(self):
        v = FixVerifier(mock=False)
        with self._gpu(None, "non determinabile"):
            self.assertIsNone(v._check_gpu_cu()[0])
            self.assertIsNone(v._check_gpu_mask()[0])

    def test_gpu_mask_checks_routing_not_the_conf_file(self):
        """Il conf /etc/modprobe.d è inerte su ostree: conta il routing."""
        v = FixVerifier(mock=False)
        with self._gpu(24, "runtime"):
            ok, detail = v._check_gpu_mask()
        self.assertIs(ok, True)
        self.assertIn("24", detail)
        with self._gpu(40, "conf (stale?)"):
            ok, detail = v._check_gpu_mask()
        self.assertIsNone(ok)
        self.assertIn("non verificabile", detail)


class TestAcpiCheckOstree(unittest.TestCase):
    """F7: su ostree il nome delle tabelle non sopravvive → verify()."""

    def test_ostree_uses_booted_entry_verify(self):
        v = FixVerifier(mock=False)
        with mock.patch("buo.fix.acpi.detect_distro",
                        return_value=mock.Mock(initramfs_tool="ostree")), \
             mock.patch("buo.fix.acpi.ACPIFix.verify", return_value=True):
            ok, detail = v._check_acpi()
        self.assertTrue(ok)
        self.assertIn("entry bootata", detail)

    def test_ostree_reports_missing_when_verify_false(self):
        v = FixVerifier(mock=False)
        with mock.patch("buo.fix.acpi.detect_distro",
                        return_value=mock.Mock(initramfs_tool="ostree")), \
             mock.patch("buo.fix.acpi.ACPIFix.verify", return_value=False):
            ok, detail = v._check_acpi()
        self.assertFalse(ok)
        self.assertIn("entry bootata", detail)

    def test_gtt_checks_runtime_param(self):
        """Il verifier deve leggere l'EFFETTO (parametro runtime), non la
        presenza del conf: campo 10/09, conf presente ma parametro al
        default (initramfs non rigenerato) → fix INERTE, ok=False."""
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
