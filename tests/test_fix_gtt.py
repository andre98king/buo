#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test del fixer GTTTuning (ttm.pages_limit via modprobe.d).

Bug di campo 10/09/2026 (BC-250 dopo cambio SSD): il fix scriveva
/etc/modprobe.d/buo-gtt.conf e si dichiarava "applicato", ma su ostree la
rigenerazione dell'initramfs è DISABILITATA → il conf non entra
nell'initramfs → `ttm.pages_limit` restava il default (1944679 vs 3959290
configurato). Verifica e apply devono basarsi sull'EFFETTO (parametro
runtime / rigenerazione initramfs), mai sulla presenza del file.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from buo.fix import gtt as gtt_mod
from buo.fix.gtt import GTT_LIMIT_DEFAULT, GTTTuning


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.params = self.root / "pages_limit"
        self.conf_dir = self.root / "modprobe.d"
        self.conf_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _fix(self, **kw):
        kw.setdefault("param_path", str(self.params))
        return GTTTuning(mock=False, **kw)

    def _write_param(self, value):
        self.params.write_text(f"{value}\n", encoding="utf-8")


class TestGTTVerify(Base):
    def test_verify_false_when_runtime_is_default(self):
        """Il file di conf NON basta: se il parametro runtime è il default,
        il fix è INERTE → verify() False."""
        self._write_param(1944679)
        conf = self.conf_dir / "buo-gtt.conf"
        conf.write_text("options ttm pages_limit=3959290\n")
        with mock.patch.object(gtt_mod, "GTT_CONF", str(conf)):
            self.assertFalse(self._fix().verify())

    def test_verify_true_when_runtime_matches(self):
        self._write_param(GTT_LIMIT_DEFAULT)
        self.assertTrue(self._fix().verify())

    def test_verify_true_when_runtime_higher(self):
        """Un valore più alto del richiesto soddisfa comunque l'obiettivo."""
        self._write_param(GTT_LIMIT_DEFAULT + 1000)
        self.assertTrue(self._fix().verify())

    def test_verify_false_when_param_unreadable(self):
        self.assertFalse(self._fix().verify())


class TestGTTApplyInitramfs(Base):
    def test_apply_enables_initramfs_regeneration_on_ostree(self):
        """Su ostree il conf non entra nell'initramfs senza rigenerazione:
        apply() deve abilitarla (txn staccata) e riportare l'esito."""
        calls = []

        def fake_txn(cmd, unit, timeout=600):
            calls.append((cmd, unit))
            return 0, "", ""

        fix = self._fix(ostree_runner=fake_txn)
        with mock.patch.object(gtt_mod, "GTT_CONF",
                               str(self.conf_dir / "buo-gtt.conf")), \
             mock.patch.object(gtt_mod, "detect_distro") as dd:
            dd.return_value.initramfs_tool = "ostree"
            res = fix.apply()
        self.assertTrue(res["applied"], res)
        self.assertTrue(res["needs_reboot"])
        self.assertEqual(calls[0][0], ["rpm-ostree", "initramfs", "--enable"])

    def test_apply_fail_closed_when_initramfs_not_regenerated(self):
        """Se la rigenerazione fallisce il fix resta inerte: MAI
        'applicato' (fail-honest, non fail-silent)."""
        fix = self._fix(ostree_runner=lambda cmd, unit, timeout=600:
                        (1, "", "errore txn"))
        with mock.patch.object(gtt_mod, "GTT_CONF",
                               str(self.conf_dir / "buo-gtt.conf")), \
             mock.patch.object(gtt_mod, "detect_distro") as dd:
            dd.return_value.initramfs_tool = "ostree"
            res = fix.apply()
        self.assertFalse(res["applied"])
        self.assertIn("initramfs", res.get("warning", ""))

    def test_apply_no_initramfs_on_non_ostree(self):
        calls = []
        fix = self._fix(ostree_runner=lambda cmd, unit, timeout=600:
                        calls.append(cmd) or (0, "", ""))
        with mock.patch.object(gtt_mod, "GTT_CONF",
                               str(self.conf_dir / "buo-gtt.conf")), \
             mock.patch.object(gtt_mod, "detect_distro") as dd:
            dd.return_value.initramfs_tool = "dracut"
            res = fix.apply()
        self.assertTrue(res["applied"], res)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
