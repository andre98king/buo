#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test di ApplyManager: sequenze A (apply volatile/persist), R (rollback),
D (heal), precondizioni (refuse), invarianti I1-I5. Tutti i comandi sono
iniettabili (run_command patchato, smoke/controller fake); mai hardware
reale, mai systemctl/stress reali.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from buo.oc.apply import ApplyManager, ApplyOutcome
from buo.oc.profiles import Profile, ProfileStore, ProfileValidator
from buo.oc.smoke import SmokeResult


class Recorder:
    """Fake di run_command: registra argv e ritorna rc configurabili."""

    def __init__(self):
        self.calls = []
        self.apply_rc = 0          # bc250-apply
        self.install_rc = 0
        self.enable_rc = 0
        self.governor_active = "inactive"   # per is-active governor

    def __call__(self, cmd, timeout=60, sudo=False, capture=True, **kw):
        self.calls.append(list(cmd))
        base = Path(cmd[0]).name if "/" in cmd[0] else cmd[0]
        if base == "bc250-apply":
            return (self.apply_rc, "", "")
        if base == "systemctl":
            if "is-active" in cmd and "cyan-skillfish-governor-smu" in cmd:
                return (0, self.governor_active, "")
            if "is-enabled" in cmd:
                return (0, "enabled", "")
            if "enable" in cmd:
                return (self.enable_rc, "", "")
            if "start" in cmd:
                self.governor_active = "active"   # stateful → retry azzerato
                return (0, "", "")
            if "stop" in cmd:
                self.governor_active = "inactive"
                return (0, "", "")
            return (0, "", "")
        if base == "cp":
            # backup/restore reali su file temporanei (cp esiste)
            import shutil
            try:
                shutil.copy2(cmd[1], cmd[2])
            except OSError as e:
                return (1, "", str(e))
            return (0, "", "")
        return (0, "", "")


class FakeController:
    def __init__(self, process_active=None):
        self._process = process_active

    def process_active(self):
        return self._process


class FakeSmoke:
    def __init__(self, result: SmokeResult):
        self._result = result
        self.runs = []

    def run(self, freq, vid_cap):
        self.runs.append((freq, vid_cap))
        return self._result


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.oc = Path(self.tmp.name)
        self.smu_conf = self.oc / "bc250-smu-oc.conf"
        self.smu_conf.write_text("[overclock]\nfrequency = 3500\n"
                                 "scale = 0\nmax_temperature = 90\n",
                                 encoding="utf-8")
        # fake bc250-apply eseguibile (per il check di presenza)
        self.fake_apply = self.oc / "bc250-apply"
        self.fake_apply.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        self.fake_apply.chmod(0o755)
        self.rec = Recorder()

    def tearDown(self):
        self.tmp.cleanup()

    def mk(self, controller=None, smoke_ok=True, smoke_cause=None,
           **kw):
        ctl = controller or FakeController()
        smoke = FakeSmoke(SmokeResult(ok=smoke_ok, cause=smoke_cause))
        mgr = ApplyManager(
            ctl, store=ProfileStore(self.oc), validator=ProfileValidator(),
            smoke=smoke, reader=None, oc_dir=self.oc,
            bc250_apply_cmd=str(self.fake_apply),
            smu_conf=str(self.smu_conf), mock=False, dry_run=False, **kw)
        mgr._cmd = self.rec     # comandi → Recorder (mai comandi reali)
        mgr._governor_active = lambda: self.rec.governor_active
        return mgr

    def stock(self):
        return Profile(id="stock", name="Stock", freq=3500, scale=0,
                       source="builtin", validated=True)


class TestApplySequence(Base):
    def test_success_volatile_sequence(self):
        mgr = self.mk()
        out = mgr.apply(self.stock())
        self.assertEqual(out.result, "ok")
        # sequenza: backup(cp) → stop governor → apply conf → smoke →
        # start governor → marcatore ok
        bases = [Path(c[0]).name if "/" in c[0] else c[0]
                 for c in self.rec.calls]
        self.assertIn("cp", bases)
        self.assertIn("bc250-apply", bases)
        sys = [c for c in self.rec.calls if c[0] == "systemctl"]
        stops = [c for c in sys if c[1] == "stop"]
        starts = [c for c in sys if c[1] == "start"]
        self.assertTrue(stops)
        self.assertTrue(starts)
        marker = json.loads((self.oc / "apply.json").read_text(
            encoding="utf-8"))
        self.assertEqual(marker["state"], "ok")

    def test_apply_argv_conf(self):
        mgr = self.mk()
        mgr.apply(self.stock())
        apply_calls = [c for c in self.rec.calls
                       if Path(c[0]).name == "bc250-apply" and "--apply" in c]
        # 1 apply: conf operativa con max_temperature = temp_apply (90)
        self.assertEqual(len(apply_calls), 1)
        from buo.constants import LIMITS
        conf = Path(apply_calls[0][-1])
        self.assertTrue(conf.exists())
        content = conf.read_text(encoding="utf-8")
        self.assertIn("frequency = 3500", content)
        self.assertIn("scale = 0", content)
        self.assertIn(f"max_temperature = {LIMITS.cpu.temp_apply}", content)

    def test_success_persist(self):
        mgr = self.mk()
        out = mgr.apply(self.stock(), persist=True, yes=True)
        self.assertEqual(out.result, "ok")
        self.assertTrue(out.persisted)
        sys = [c for c in self.rec.calls if c[0] == "systemctl"]
        self.assertTrue(any(c[1] == "enable" for c in sys))
        install = [c for c in self.rec.calls
                   if Path(c[0]).name == "bc250-apply" and "--install" in c]
        self.assertEqual(len(install), 1)

    def test_persist_without_yes_aborted(self):
        mgr = self.mk()
        out = mgr.apply(self.stock(), persist=True, yes=False)
        self.assertEqual(out.result, "aborted")
        self.assertIn("conferma", out.cause)

    def test_refuse_if_engine_active(self):
        ctl = FakeController(process_active=4242)
        mgr = self.mk(controller=ctl)
        out = mgr.apply(self.stock())
        self.assertEqual(out.result, "aborted")
        self.assertIn("REFUSE", out.cause)
        self.assertEqual(self.rec.calls, [])   # nulla eseguito

    def test_zone_block_before_any_command(self):
        mgr = self.mk()
        bad = Profile(id="bad", name="Bad", freq=3750, scale=-7,
                      vid_cap=950, source="user", validated=False)
        out = mgr.apply(bad)
        self.assertEqual(out.result, "aborted")
        self.assertIn("zona di hang", out.cause)
        self.assertEqual(self.rec.calls, [])

    def test_apply_rc_fail_rollback(self):
        mgr = self.mk()
        self.rec.apply_rc = 1
        out = mgr.apply(self.stock())
        self.assertEqual(out.result, "rolled_back")
        marker = json.loads((self.oc / "apply.json").read_text(
            encoding="utf-8"))
        self.assertEqual(marker["state"], "rolled_back")
        # il conf originale è stato ripristinato (cp backup → conf)
        self.assertEqual(self.smu_conf.read_text(encoding="utf-8"),
                         "[overclock]\nfrequency = 3500\nscale = 0\n"
                         "max_temperature = 90\n")

    def test_smoke_fail_rollback(self):
        mgr = self.mk(smoke_ok=False, smoke_cause="thermal")
        out = mgr.apply(self.stock())
        self.assertEqual(out.result, "rolled_back")
        self.assertEqual(out.cause, "smoke fail: thermal")
        marker = json.loads((self.oc / "apply.json").read_text(
            encoding="utf-8"))
        self.assertEqual(marker["state"], "rolled_back")
        # governor riavviato DOPO il rollback (invariante I2)
        sys = [c for c in self.rec.calls if c[0] == "systemctl"
               and c[1] == "start"]
        self.assertTrue(sys)

    def test_governor_never_left_stopped_on_abort(self):
        mgr = self.mk()
        mgr._governor_stop_verified = lambda d: False   # stop fallisce
        out = mgr.apply(self.stock())
        self.assertEqual(out.result, "aborted")
        self.assertEqual(out.cause, "governor non fermato")
        # il governor è stato RIavviato (never early-return senza restart)
        sys = [c for c in self.rec.calls if c[0] == "systemctl"
               and c[1] == "start"]
        self.assertTrue(sys)


class TestRestoreStock(Base):
    def test_restore_stock_volatile(self):
        mgr = self.mk()
        out = mgr.restore_stock(persist=False, yes=False)
        self.assertEqual(out.result, "ok")
        # nessun disable del servizio senza --persist
        sys = [c for c in self.rec.calls if c[0] == "systemctl"]
        self.assertFalse(any(c[1] == "disable" for c in sys))

    def test_restore_stock_persist_disable(self):
        mgr = self.mk()
        out = mgr.restore_stock(persist=True, yes=True)
        self.assertEqual(out.result, "ok")
        sys = [c for c in self.rec.calls if c[0] == "systemctl"]
        self.assertTrue(any(c[1] == "disable" for c in sys))

    def test_restore_stock_persist_without_yes(self):
        mgr = self.mk()
        out = mgr.restore_stock(persist=True, yes=False)
        self.assertEqual(out.result, "aborted")


class TestHeal(Base):
    def test_no_stale_noop(self):
        mgr = self.mk()
        out = mgr.heal()
        self.assertEqual(out.result, "ok")

    def test_stale_apply_rolls_back_and_starts_governor(self):
        (self.oc / "apply.json").write_text(json.dumps({
            "state": "applying", "profile": "custom-x",
            "started_epoch": 1, "pid": 99999999,   # processo morto
        }), encoding="utf-8")
        backup = self.oc / "bc250-smu-oc.conf.buo-rollback-20260901"
        backup.write_text("[overclock]\nfrequency = 3500\nscale = 0\n"
                          "max_temperature = 90\n", encoding="utf-8")
        mgr = self.mk()
        out = mgr.heal()
        self.assertEqual(out.result, "rolled_back")
        marker = json.loads((self.oc / "apply.json").read_text(
            encoding="utf-8"))
        self.assertEqual(marker["state"], "rolled_back")
        sys = [c for c in self.rec.calls if c[0] == "systemctl"
               and c[1] == "start"]
        self.assertTrue(sys)

    def test_apply_log_appended(self):
        mgr = self.mk()
        mgr.apply(self.stock())
        log = (self.oc / "apply.log").read_text(encoding="utf-8")
        self.assertIn("result=ok", log)


class TestSimulatedNoWrite(Base):
    """M2: apply/restore-stock/heal in --mock/--dry-run NON scrive MAI
    apply.json, apply-*.conf, apply.log né profiles.json (simulazione =
    nessuna scrittura di stato reale)."""

    def _written(self):
        return [p.name for p in self.oc.iterdir()
                if p.name in ("apply.json", "apply.log", "profiles.json")
                or p.name.startswith("apply-")]

    def _mk_sim(self, mock=True, dry_run=False):
        ctl = FakeController()
        smoke = FakeSmoke(SmokeResult(ok=True))
        return ApplyManager(
            ctl, store=ProfileStore(self.oc), validator=ProfileValidator(),
            smoke=smoke, reader=None, oc_dir=self.oc,
            bc250_apply_cmd=str(self.fake_apply),
            smu_conf=str(self.smu_conf), mock=mock, dry_run=dry_run)

    def test_apply_mock_writes_nothing(self):
        out = self._mk_sim(mock=True).apply(self.stock())
        self.assertEqual(out.result, "ok")
        self.assertEqual(self._written(), [])

    def test_apply_dry_run_writes_nothing(self):
        out = self._mk_sim(mock=False, dry_run=True).apply(self.stock())
        self.assertEqual(out.result, "ok")
        self.assertEqual(self._written(), [])

    def test_restore_stock_mock_writes_nothing(self):
        out = self._mk_sim(mock=True).restore_stock()
        self.assertEqual(out.result, "ok")
        self.assertEqual(self._written(), [])

    def test_heal_mock_writes_nothing(self):
        out = self._mk_sim(mock=True).heal()
        self.assertEqual(out.result, "ok")
        self.assertEqual(self._written(), [])


if __name__ == "__main__":
    unittest.main()
