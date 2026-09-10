#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test del fixer GTTTuning — tetto VRAM dinamica via KARG.

Fonte: doc ufficiale BC-250 (elektricM/amd-bc250-docs, bios/vram.md):
con split 512MB il max VRAM dinamica e' 8.25 GB e i giochi configurati per
>=8 GB "tip over" -> crash del display driver (indicata come *the primary
reason for games crashing*). Il fix documentato su Bazzite e' il KARG:

    rpm-ostree kargs --delete=ttm.pages_limit --append=ttm.pages_limit=3014656

Bug di campo 10/09/2026: il fixer scriveva /etc/modprobe.d/buo-gtt.conf
(meccanismo sbagliato E inerte su ostree: initramfs non rigenerato) e si
dichiarava "applicato". Ora: kargs + verifica sull'effetto.
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
        self.cmdline = self.root / "cmdline"
        self.cmdline.write_text("rhgb quiet root=/dev/x rw\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _fix(self, **kw):
        kw.setdefault("param_path", str(self.params))
        kw.setdefault("cmdline_path", str(self.cmdline))
        return GTTTuning(mock=False, **kw)

    def _write_param(self, value):
        self.params.write_text(f"{value}\n", encoding="utf-8")


class TestGTTVerify(Base):
    def test_verify_false_when_runtime_is_default(self):
        self._write_param(1944679)
        self.assertFalse(self._fix().verify())

    def test_verify_true_when_runtime_matches(self):
        self._write_param(GTT_LIMIT_DEFAULT)
        self.assertTrue(self._fix().verify())

    def test_verify_true_when_runtime_higher(self):
        self._write_param(GTT_LIMIT_DEFAULT + 1024)
        self.assertTrue(self._fix().verify())

    def test_verify_fallback_to_cmdline_when_param_unreadable(self):
        """Parametro non leggibile (modulo non caricato): vale la presenza
        del karg nel cmdline (unica evidenza disponibile)."""
        self.cmdline.write_text(
            f"rhgb quiet ttm.pages_limit={GTT_LIMIT_DEFAULT} rw\n",
            encoding="utf-8")
        self.assertTrue(self._fix().verify())

    def test_verify_false_when_nothing(self):
        self.assertFalse(self._fix().verify())


class TestGTTApplyKargs(Base):
    def _kargs(self, current="rhgb quiet root=/dev/x rw"):
        return lambda cmd, unit, timeout=600: (
            (0, current, "") if cmd[:2] == ["rpm-ostree", "kargs"]
            and "--append" not in " ".join(cmd) else (0, "", ""))

    def test_apply_appends_karg(self):
        calls = []

        def runner(cmd, unit, timeout=600):
            calls.append(cmd)
            if "kargs" in cmd and len(cmd) == 2:
                return 0, "rhgb quiet root=/dev/x rw", ""
            return 0, "", ""

        fix = self._fix(ostree_runner=runner)
        with mock.patch.object(gtt_mod, "detect_distro") as dd:
            dd.return_value.initramfs_tool = "ostree"
            res = fix.apply()
        self.assertTrue(res["applied"], res)
        self.assertTrue(res["needs_reboot"])
        txn = calls[-1]
        self.assertEqual(txn[:2], ["rpm-ostree", "kargs"])
        self.assertIn(f"--append=ttm.pages_limit={GTT_LIMIT_DEFAULT}", txn)

    def test_apply_replaces_wrong_value(self):
        calls = []

        def runner(cmd, unit, timeout=600):
            calls.append(cmd)
            if len(cmd) == 2:
                return 0, "rhgb quiet ttm.pages_limit=3959290 rw", ""
            return 0, "", ""

        fix = self._fix(ostree_runner=runner)
        with mock.patch.object(gtt_mod, "detect_distro") as dd:
            dd.return_value.initramfs_tool = "ostree"
            res = fix.apply()
        self.assertTrue(res["applied"], res)
        txn = calls[-1]
        self.assertIn("--delete=ttm.pages_limit", txn)
        self.assertIn(f"--append=ttm.pages_limit={GTT_LIMIT_DEFAULT}", txn)

    def test_apply_idempotent_when_already_configured(self):
        calls = []

        def runner(cmd, unit, timeout=600):
            calls.append(cmd)
            return 0, f"rhgb quiet ttm.pages_limit={GTT_LIMIT_DEFAULT} rw", ""

        fix = self._fix(ostree_runner=runner)
        with mock.patch.object(gtt_mod, "detect_distro") as dd:
            dd.return_value.initramfs_tool = "ostree"
            res = fix.apply()
        self.assertTrue(res["applied"], res)
        # nessuna transazione: il karg c'e' gia'
        self.assertEqual(len(calls), 1)

    def test_apply_fail_closed_when_txn_fails(self):
        def runner(cmd, unit, timeout=600):
            if len(cmd) == 2:
                return 0, "rhgb quiet", ""
            return 1, "", "errore txn"

        fix = self._fix(ostree_runner=runner)
        with mock.patch.object(gtt_mod, "detect_distro") as dd:
            dd.return_value.initramfs_tool = "ostree"
            res = fix.apply()
        self.assertFalse(res["applied"])
        self.assertIn("kargs", res.get("warning", "").lower())

    def test_apply_non_ostree_is_manual(self):
        calls = []
        fix = self._fix(ostree_runner=lambda *a, **k: calls.append(a) or (0, "", ""))
        with mock.patch.object(gtt_mod, "detect_distro") as dd:
            dd.return_value.initramfs_tool = "dracut"
            res = fix.apply()
        self.assertFalse(res["applied"])
        self.assertIn("ttm.pages_limit", res.get("warning", ""))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
