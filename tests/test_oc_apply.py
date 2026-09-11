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
    """Fake di run_command: registra argv e ritorna rc configurabili.

    Lo stato del governor NON passa più da qui: `_governor_active()` legge
    ActiveState (`governor_states`), che i test iniettano (seam
    `governor_active`, stringa)."""

    def __init__(self):
        self.calls = []
        self.apply_rc = 0          # bc250-apply
        self.install_rc = 0
        self.enable_rc = 0
        self.governor_active = "inactive"   # ActiveState del governor
        self.stop_works = True   # False = stop "riesce" ma lo stato non cambia

    def __call__(self, cmd, timeout=60, sudo=False, capture=True, **kw):
        self.calls.append(list(cmd))
        base = Path(cmd[0]).name if "/" in cmd[0] else cmd[0]
        if base == "bc250-apply":
            return (self.apply_rc, "", "")
        if base == "systemctl":
            if "is-enabled" in cmd:
                return (0, "enabled", "")
            if "enable" in cmd:
                return (self.enable_rc, "", "")
            if "start" in cmd:
                self.governor_active = "active"   # stateful → retry azzerato
                return (0, "", "")
            if "stop" in cmd:
                if self.stop_works:
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


class TestConfMissingAfterRollback(Base):
    """Campo 10/09: dopo un rollback validate-fail (T2 → `--uninstall`) la
    conf persistita NON esiste più. Il riuso dello stato OC certificato deve
    poterla RICREARE — prima abortiva con "conf assente" e la macchina
    restava a stock senza alcun path di ripristino."""

    def _remove_conf(self):
        self.smu_conf.unlink()

    def test_apply_proceeds_without_previous_conf(self):
        self._remove_conf()
        mgr = self.mk()
        out = mgr.apply(self.stock(), persist=True, yes=True)
        self.assertEqual(out.result, "ok", out.cause)
        self.assertTrue(out.persisted)
        self.assertTrue(any("nessun backup" in d for d in out.details))

    def test_failure_without_backup_restores_stock(self):
        self._remove_conf()
        # store con un profilo stock: è la fonte del fallback
        mgr = self.mk(smoke_ok=False, smoke_cause="stretch")
        mgr.store.save([self.stock()])
        out = mgr.apply(self.stock(), persist=False)
        self.assertEqual(out.result, "rolled_back")
        self.assertTrue(any("stock" in d for d in out.details), out.details)
        # nessun `cp` di backup: la conf persistita non esisteva
        self.assertFalse(any(c and c[0] == "cp" for c in self.rec.calls))

    def test_failure_without_backup_and_without_stock_uninstalls(self):
        self._remove_conf()
        mgr = self.mk(smoke_ok=False, smoke_cause="stretch")
        # store senza profilo stock (caso limite): fallback su --uninstall
        with mock.patch.object(mgr.store, "get", return_value=None):
            out = mgr.apply(self.stock(), persist=False)
        self.assertEqual(out.result, "rolled_back")
        self.assertTrue(any("stock assente" in d for d in out.details),
                        out.details)
        self.assertTrue(any("--uninstall" in " ".join(c)
                            for c in self.rec.calls), self.rec.calls)


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
        self.assertIn("smoke fail: thermal", out.cause)
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


class TestGovernorStateFailClosed(Base):
    """FIX fail-open (regola SMU): lo stato del governor si legge da
    ActiveState REALE (`governor_states()`), mai `systemctl is-active` —
    esce rc=3 anche per gli stati TRANSITORI (activating/deactivating) e
    mappare rc≠0 su "inactive" faceva partire l'apply SMU mentre il
    governor scriveva sull'SMU (freeze del SoC, incidente 30/08)."""

    def _mk_real(self, states):
        """ApplyManager con il METODO VERO `_governor_active` (gli altri
        test lo sostituiscono col Recorder) e `governor_states` iniettato:
        mai systemctl/subprocess reali."""
        mgr = ApplyManager(
            FakeController(), store=ProfileStore(self.oc),
            validator=ProfileValidator(),
            smoke=FakeSmoke(SmokeResult(ok=True)), reader=None,
            oc_dir=self.oc, bc250_apply_cmd=str(self.fake_apply),
            smu_conf=str(self.smu_conf), mock=False, dry_run=False)
        mgr._cmd = self.rec
        patcher = mock.patch("buo.oc.apply.governor_states",
                             return_value=states)
        self.gov_states = patcher.start()
        self.addCleanup(patcher.stop)
        return mgr

    def test_reads_active_state_not_is_active(self):
        """`activating` è uno stato REALE (prima l'output veniva scartato):
        la lettura passa dal punto unico `governor_states()`, e `activating`
        NON vale come "fermo confermato"."""
        mgr = self._mk_real({"ActiveState": "activating",
                             "LoadState": "loaded"})
        self.assertEqual(mgr._governor_active(), "activating")
        self.assertIsNone(mgr._governor_confirmed_inactive())
        self.assertTrue(self.gov_states.called)

    def test_unknown_state_is_not_confirmed(self):
        """Nessuno stato leggibile → "unknown" e NON confermato fermo."""
        mgr = self._mk_real({})
        self.assertEqual(mgr._governor_active(), "unknown")
        self.assertIsNone(mgr._governor_confirmed_inactive())

    def test_apply_aborts_when_governor_transient(self):
        """Activating/deactivating ⇒ ABORT: nessun apply SMU, marcatore
        aborted, governor comunque riavviato (invariante I2)."""
        for state in ("activating", "deactivating"):
            with self.subTest(state=state):
                self.rec.calls.clear()
                self.rec.stop_works = False   # stop "ok" ma stato invariato
                self.rec.governor_active = state
                out = self.mk().apply(self.stock())
                self.assertEqual(out.result, "aborted")
                self.assertEqual(out.cause, "governor non fermato")
                self.assertFalse([c for c in self.rec.calls
                                  if Path(c[0]).name == "bc250-apply"])
                self.assertTrue([c for c in self.rec.calls
                                 if c[0] == "systemctl" and "start" in c])

    def test_apply_aborts_when_state_unknown(self):
        """Stato non determinabile ⇒ ABORT (fail-closed): nessun comando
        bc250-apply, nemmeno quello di re-apply."""
        mgr = self._mk_real({})
        out = mgr.apply(self.stock())
        self.assertEqual(out.result, "aborted")
        self.assertEqual(out.cause, "governor non fermato")
        self.assertFalse([c for c in self.rec.calls
                          if Path(c[0]).name == "bc250-apply"])

    def test_apply_proceeds_when_governor_failed(self):
        """`failed` è "fermo CONFERMATO" quanto `inactive`: si procede."""
        self.rec.stop_works = False
        self.rec.governor_active = "failed"
        out = self.mk().apply(self.stock())
        self.assertEqual(out.result, "ok", out.cause)

    def test_stop_retries_when_still_active(self):
        """Stop inefficace (ancora active) → ri-verifica: secondo stop e
        procedi solo quando lo stato è confermato fermo."""
        mgr = self._mk_real({})
        seq = iter([{"ActiveState": "active"}, {"ActiveState": "inactive"}])
        with mock.patch("buo.oc.apply.governor_states",
                        side_effect=lambda *a, **k: next(seq)), \
                mock.patch("buo.oc.apply.time.sleep"):
            details = []
            self.assertTrue(mgr._governor_stop_verified(details))
        stops = [c for c in self.rec.calls
                 if c[0] == "systemctl" and "stop" in c]
        self.assertEqual(len(stops), 2)
        self.assertTrue(any("retry" in d for d in details), details)

    def test_stop_aborts_after_two_ineffective_stops(self):
        """Due stop senza effetto (governor sempre active) → abort."""
        mgr = self._mk_real({})
        with mock.patch("buo.oc.apply.governor_states",
                        return_value={"ActiveState": "active"}), \
                mock.patch("buo.oc.apply.time.sleep"):
            details = []
            self.assertFalse(mgr._governor_stop_verified(details))
        self.assertTrue(any("NON fermabile" in d for d in details), details)

    def test_start_path_unknown_state_alerts_without_abort(self):
        """Sul percorso di START lo stato ignoto NON è un abort: il
        governor va comunque (ri)avviato (invariante I2) — esito = alert."""
        mgr = self._mk_real({})
        with mock.patch("buo.oc.apply.time.sleep"):
            details = []
            self.assertFalse(mgr._governor_start_verified(details))
        starts = [c for c in self.rec.calls
                  if c[0] == "systemctl" and "start" in c]
        self.assertEqual(len(starts), 2)   # tentativo + retry
        self.assertTrue(any("NON ripartito" in d for d in details), details)

    def test_start_path_activating_is_not_verified(self):
        """`activating` non è `active`: mai dichiarare avviato ciò che non
        lo è (dopo il retry resta l'alert esplicito)."""
        mgr = self._mk_real({"ActiveState": "activating"})
        with mock.patch("buo.oc.apply.time.sleep"):
            details = []
            self.assertFalse(mgr._governor_start_verified(details))
        self.assertTrue(any("NON ripartito" in d for d in details), details)

    def test_start_path_active_verified(self):
        """Caso felice: ActiveState=active ⇒ verifica positiva."""
        mgr = self._mk_real({"ActiveState": "active"})
        self.assertTrue(mgr._governor_start_verified([]))


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
