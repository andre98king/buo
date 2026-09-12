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

Bug di campo 12/09/2026 (rollback): la LETTURA dei kargs passava dal runner
delle TRANSAZIONI (`_run_ostree_txn`, unità systemd con stdout su file) →
il client vede sempre `out=''` → `GTTTuning.rollback()` credeva che il karg
non ci fosse, non lo rimuoveva e dichiarava "Rollback completato". Qui la
lettura è patchata su `run_command` (read-only, DIRETTO): i runner finti
che "restituiscono" lo stdout rappresentano un meccanismo che non esiste.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from buo.fix import gtt as gtt_mod
from buo.fix.gtt import GTT_LIMIT_DEFAULT, GTTTuning

STOCK_KARGS = "rhgb quiet root=/dev/x rw"


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


class KargsReadMixin:
    """La lettura dei kargs è un comando read-only DIRETTO (`run_command`)."""

    def setUp(self):
        super().setUp()
        self.read_cmdline = STOCK_KARGS
        self.read_rc = 0
        self.reads = []
        patcher = mock.patch.object(gtt_mod, "run_command", self._read)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _read(self, cmd, **kw):
        self.reads.append(cmd)
        return self.read_rc, self.read_cmdline, ""


class TestGTTApplyKargs(KargsReadMixin, Base):
    def _txn_runner(self, calls, rc=0, err=""):
        def runner(cmd, unit, timeout=600):
            calls.append(cmd)
            return rc, "", err
        return runner

    def test_read_non_passa_dal_runner_delle_transazioni(self):
        """Regressione 12/09: la lettura NON deve usare `_run_ostree_txn`."""
        calls = []
        fix = self._fix(ostree_runner=self._txn_runner(calls))
        with mock.patch.object(gtt_mod, "detect_distro") as dd:
            dd.return_value.initramfs_tool = "ostree"
            fix.apply()
        self.assertEqual(self.reads, [["rpm-ostree", "kargs"]])
        for cmd in calls:
            self.assertNotEqual(len(cmd), 2)   # nessuna lettura via runner

    def test_apply_appends_karg(self):
        calls = []
        fix = self._fix(ostree_runner=self._txn_runner(calls))
        with mock.patch.object(gtt_mod, "detect_distro") as dd:
            dd.return_value.initramfs_tool = "ostree"
            res = fix.apply()
        self.assertTrue(res["applied"], res)
        self.assertTrue(res["needs_reboot"])
        txn = calls[-1]
        self.assertEqual(txn[:2], ["rpm-ostree", "kargs"])
        self.assertIn(f"--append=ttm.pages_limit={GTT_LIMIT_DEFAULT}", txn)
        self.assertNotIn("--delete=ttm.pages_limit", txn)

    def test_apply_replaces_wrong_value(self):
        self.read_cmdline = "rhgb quiet ttm.pages_limit=3959290 rw"
        calls = []
        fix = self._fix(ostree_runner=self._txn_runner(calls))
        with mock.patch.object(gtt_mod, "detect_distro") as dd:
            dd.return_value.initramfs_tool = "ostree"
            res = fix.apply()
        self.assertTrue(res["applied"], res)
        txn = calls[-1]
        self.assertIn("--delete=ttm.pages_limit", txn)
        self.assertIn(f"--append=ttm.pages_limit={GTT_LIMIT_DEFAULT}", txn)

    def test_apply_idempotent_when_already_configured(self):
        self.read_cmdline = f"rhgb quiet ttm.pages_limit={GTT_LIMIT_DEFAULT} rw"
        calls = []
        fix = self._fix(ostree_runner=self._txn_runner(calls))
        with mock.patch.object(gtt_mod, "detect_distro") as dd:
            dd.return_value.initramfs_tool = "ostree"
            res = fix.apply()
        self.assertTrue(res["applied"], res)
        # nessuna transazione: il karg c'e' gia'
        self.assertEqual(calls, [])

    def test_apply_fail_closed_when_lettura_non_attendibile(self):
        """Kargs non leggibili (rc!=0 o output vuoto) → MAI "applicato"."""
        for rc, out in ((1, ""), (0, "")):
            self.read_rc, self.read_cmdline = rc, out
            calls = []
            fix = self._fix(ostree_runner=self._txn_runner(calls))
            with mock.patch.object(gtt_mod, "detect_distro") as dd:
                dd.return_value.initramfs_tool = "ostree"
                res = fix.apply()
            self.assertFalse(res["applied"], (rc, out, res))
            self.assertEqual(calls, [])

    def test_apply_fail_closed_when_txn_fails(self):
        calls = []
        fix = self._fix(ostree_runner=self._txn_runner(
            calls, rc=1, err="errore txn"))
        with mock.patch.object(gtt_mod, "detect_distro") as dd:
            dd.return_value.initramfs_tool = "ostree"
            res = fix.apply()
        self.assertFalse(res["applied"])
        self.assertIn("kargs", res.get("warning", "").lower())

    def test_apply_non_ostree_is_manual(self):
        calls = []
        fix = self._fix(ostree_runner=self._txn_runner(calls))
        with mock.patch.object(gtt_mod, "detect_distro") as dd:
            dd.return_value.initramfs_tool = "dracut"
            res = fix.apply()
        self.assertFalse(res["applied"])
        self.assertIn("ttm.pages_limit", res.get("warning", ""))
        self.assertEqual(calls, [])
        self.assertEqual(self.reads, [])


class TestGTTRollback(KargsReadMixin, Base):
    """Regressioni del bug di campo 12/09/2026 (rollback falso-successo)."""

    def _txn_runner(self, calls, rc=0):
        def runner(cmd, unit, timeout=600):
            calls.append(cmd)
            return rc, "", ""
        return runner

    def test_rollback_rimuove_il_karg(self):
        """Karg PRESENTE: va emessa la transazione di delete (e nessun
        falso "completato" se lo stdout del runner è vuoto)."""
        self.read_cmdline = f"rhgb quiet ttm.pages_limit={GTT_LIMIT_DEFAULT} rw"
        calls = []
        fix = self._fix(ostree_runner=self._txn_runner(calls))
        self.assertTrue(fix.rollback())
        self.assertEqual(calls[-1],
                         ["rpm-ostree", "kargs",
                          "--delete=ttm.pages_limit"])

    def test_rollback_senza_karg_non_tocca_nulla(self):
        calls = []
        fix = self._fix(ostree_runner=self._txn_runner(calls))
        self.assertTrue(fix.rollback())
        self.assertEqual(calls, [])

    def test_rollback_lettura_non_attendibile_non_dichiara_successo(self):
        """rc!=0 o output vuoto = stato IGNOTO → False (mai "fatto")."""
        for rc, out in ((1, ""), (0, "")):
            self.read_rc, self.read_cmdline = rc, out
            calls = []
            fix = self._fix(ostree_runner=self._txn_runner(calls))
            self.assertFalse(fix.rollback(), (rc, out))
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
