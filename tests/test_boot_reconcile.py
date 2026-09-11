#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Test dell'agente di boot (`buo.state.reconcile`) — mai hardware reale.

Copre il caso che ha motivato il modulo (campo 11/09/2026): dopo un cold boot
la macchina resta a 12 thread e senza governor GPU, mentre il ledger di BUO
dichiara tutto applicato. E copre le guardie: tetto tentativi, kill-switch,
gate gioco, fail-closed sul governor in stato transitorio, riavvio del
governor su OGNI percorso di uscita.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from buo.state.reconcile import (BOOT_UNIT, KILL_SWITCH, BootReconciler,
                                 unit_content)


class FakeCPU:
    def __init__(self, result=None, error=None):
        self.result = result or {"unlocked": True, "mask": "0xFF",
                                 "needs_reboot": True}
        self.error = error
        self.calls = 0

    def unlock(self, force=False):
        self.calls += 1
        if self.error:
            raise RuntimeError(self.error)
        return self.result


class FakeGovernor:
    """stop() riuscito ⇒ lo stato systemd diventa `inactive` (holder)."""

    def __init__(self, holder):
        self.holder = holder
        self.stop_calls = 0
        self.start_calls = 0
        self.stop_ok = True

    def stop(self):
        self.stop_calls += 1
        if self.stop_ok:
            self.holder["state"] = "inactive"
        return self.stop_ok

    def start(self):
        self.start_calls += 1
        self.holder["state"] = "active"
        return True


class FakeVerdict:
    """Verdetto durevole del silicio (unlock-verdict.json)."""

    def __init__(self, cpu=None):
        self.cpu = cpu

    def get(self, unit):
        return self.cpu if unit == "cpu" else None


class FakeAcpi:
    def __init__(self, booted=False, apply_result=None):
        self.booted = booted
        self.apply_result = apply_result or {"applied": True,
                                            "needs_reboot": True}
        self.apply_calls = 0

    def verify(self):
        return self.booted

    def apply(self, force=False):
        self.apply_calls += 1
        return self.apply_result


class Responder:
    """Sostituto di `run_command` per systemctl (nessun comando reale)."""

    def __init__(self, holder, enabled=("bc250-smu-oc",
                                        "bc250-cu-live-manager"),
                 ran=("bc250-smu-oc", "bc250-cu-live-manager")):
        self.holder = holder
        self.enabled = set(enabled)
        self.ran = set(ran)
        self.commands = []

    def __call__(self, cmd, timeout=60, sudo=False, capture=True, cwd=None,
                 **kwargs):
        self.commands.append(list(cmd))
        if cmd[:1] == ["pgrep"]:
            return (1, "", "")
        if "show" in cmd and "ActiveState" in cmd:
            state = self.holder["state"]
            if state is None:
                return (1, "", "stato non determinabile")
            return (0, state + "\n", "")
        if "ActiveEnterTimestamp" in cmd:
            name = cmd[-1]
            return (0, ("Fri 2026-09-11 15:54:21 CEST\n"
                        if name in self.ran else "\n"), "")
        if cmd[:1] == ["systemctl"] and "is-enabled" in cmd:
            name = cmd[-1]
            return ((0, "enabled\n", "") if name in self.enabled
                    else (1, "disabled\n", ""))
        if cmd[:1] == ["systemctl"] and "start" in cmd:
            self.ran.add(cmd[-1])
            return (0, "", "")
        if cmd[:1] == ["systemctl"] and "enable" in cmd:
            self.enabled.add(cmd[-1])
            return (0, "", "")
        return (0, "", "")


class ReconcilerTestCase(unittest.TestCase):
    """Base: filesystem finto per /proc e /sys, dipendenze finte."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.present = self.root / "present"
        self.cmdline = self.root / "cmdline"
        self.mode = self.root / "reboot-mode"
        self.attempts = self.root / "attempts"
        self.present.write_text("0-15\n")
        self.cmdline.write_text("rhgb quiet mitigations=off "
                                "ttm.pages_limit=3014656\n")
        self.mode.write_text("cold\n")
        self.holder = {"state": "active"}
        self.cpu = FakeCPU()
        self.gov = FakeGovernor(self.holder)
        self.acpi = FakeAcpi(booted=True)
        self.reboot_calls = []
        self.game = [False]

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, dry_run=False, **kwargs):
        return BootReconciler(
            cpu=kwargs.pop("cpu", self.cpu),
            governor=kwargs.pop("governor", self.gov),
            acpi=kwargs.pop("acpi", self.acpi),
            dry_run=dry_run,
            attempts_path=self.attempts,
            present_path=self.present,
            cmdline_path=self.cmdline,
            reboot_mode_path=self.mode,
            verdict=kwargs.pop("verdict", FakeVerdict()),
            reboot_fn=kwargs.pop("reboot_fn", self.fake_reboot),
            game_check_fn=kwargs.pop("game_check_fn",
                                     lambda: self.game[0]),
            **kwargs)

    def fake_reboot(self):
        self.reboot_calls.append(True)
        return {"rebooted": True}

    def responder(self, gov_state="active", **kwargs):
        self.holder["state"] = gov_state
        return Responder(self.holder, **kwargs)

    # ------------------------------------------------------------------ #

    def test_threads_present_parsing(self):
        rec = self.make()
        self.assertEqual(rec.threads_present(), 16)
        self.present.write_text("0-11\n")
        self.assertEqual(rec.threads_present(), 12)
        self.present.write_text("0-7,16-19\n")
        self.assertEqual(rec.threads_present(), 12)
        self.present.write_text("0\n")
        self.assertEqual(rec.threads_present(), 1)

    def test_threads_present_missing_file_is_none(self):
        self.present.unlink()
        self.assertIsNone(self.make().threads_present())

    def test_check_reports_real_state(self):
        with patch("buo.state.reconcile.run_command", self.responder()):
            state = self.make().check()
        self.assertTrue(state["threads_ok"])
        self.assertTrue(state["governor_ok"])
        self.assertTrue(state["acpi_ok"])
        self.assertTrue(state["services_ok"])
        self.assertTrue(state["kargs"]["mitigations_off"])
        self.assertEqual(state["kargs"]["pages_limit"], "3014656")

    def test_healthy_run_does_nothing_and_resets_attempts(self):
        self.attempts.write_text("2\n")
        with patch("buo.state.reconcile.run_command", self.responder()):
            report = self.make().reconcile()
        self.assertTrue(report["healthy"])
        self.assertEqual(self.cpu.calls, 0)
        self.assertEqual(self.reboot_calls, [])
        self.assertEqual(self.attempts.read_text().strip(), "0")

    def test_cold_boot_12_threads_reunlocks_and_reboots_warm(self):
        self.present.write_text("0-11\n")
        with patch("buo.state.reconcile.run_command", self.responder()):
            report = self.make().reconcile()
        self.assertEqual(self.cpu.calls, 1)
        self.assertEqual(self.gov.stop_calls, 1, "governor fermato per lo SMU")
        self.assertTrue(report["unlock"]["unlocked"])
        self.assertEqual(self.attempts.read_text().strip(), "1")
        self.assertEqual(self.mode.read_text().strip(), "warm")
        self.assertEqual(self.reboot_calls, [True])

    def test_governor_restarted_after_unlock(self):
        """Lezione campo 11/09: lo stop cancella lo start job del boot."""
        self.present.write_text("0-11\n")
        rec = self.make()
        with patch("buo.state.reconcile.run_command",
                   self.responder(gov_state="inactive")):
            rec.reconcile()
            self.assertGreaterEqual(self.gov.start_calls, 1)
            # a fine riconciliazione la GPU ha di nuovo la sua curva
            self.assertEqual(rec.governor_state(), "active")

    def test_governor_restarted_even_if_unlock_raises(self):
        self.present.write_text("0-11\n")
        cpu = FakeCPU(error="SMU non risponde")
        with patch("buo.state.reconcile.run_command",
                   self.responder(gov_state="inactive")):
            report = self.make(cpu=cpu).reconcile()
        self.assertFalse(report["unlock"]["unlocked"])
        self.assertIn("SMU non risponde", report["unlock"]["error"])
        self.assertGreaterEqual(self.gov.start_calls, 1)
        self.assertEqual(self.reboot_calls, [], "nessun reboot senza unlock")

    def test_transient_governor_state_blocks_smu_access(self):
        """`activating` NON è 'fermo': fail-closed (mai SMU col governor)."""
        self.present.write_text("0-11\n")
        self.gov.stop_ok = False
        with patch("buo.state.reconcile.run_command",
                   self.responder(gov_state="activating")):
            report = self.make().reconcile()
        self.assertEqual(self.cpu.calls, 0, "nessun accesso SMU")
        self.assertFalse(report["unlock"]["unlocked"])
        self.assertEqual(self.reboot_calls, [])

    def test_unknown_governor_state_blocks_smu_access(self):
        self.present.write_text("0-11\n")
        with patch("buo.state.reconcile.run_command",
                   self.responder(gov_state=None)):
            report = self.make().reconcile()
        self.assertEqual(self.cpu.calls, 0)
        self.assertIn("non determinabile", report["unlock"]["error"])

    def test_reboot_blocked_when_game_running(self):
        self.present.write_text("0-11\n")
        self.game[0] = True
        with patch("buo.state.reconcile.run_command", self.responder()):
            report = self.make().reconcile()
        self.assertEqual(report["reboot"]["blocked"], "sessione_gioco_attiva")
        self.assertEqual(self.reboot_calls, [], "mai reboot col gioco attivo")
        # il budget di tentativi NON si consuma quando il reboot è rinviato
        self.assertFalse(self.attempts.exists())

    def test_reboot_blocked_when_attempts_exhausted(self):
        self.present.write_text("0-11\n")
        self.attempts.write_text("2\n")
        with patch("buo.state.reconcile.run_command", self.responder()):
            report = self.make().reconcile()
        self.assertEqual(report["reboot"]["blocked"], "tentativi_esauriti")
        self.assertEqual(self.reboot_calls, [])
        self.assertEqual(self.attempts.read_text().strip(), "2")

    def test_kill_switch_stops_everything(self):
        self.present.write_text("0-11\n")
        self.cmdline.write_text(f"rhgb {KILL_SWITCH}\n")
        with patch("buo.state.reconcile.run_command", self.responder()):
            report = self.make().reconcile()
        self.assertTrue(report["skipped"])
        self.assertEqual(self.cpu.calls, 0)
        self.assertEqual(self.reboot_calls, [])

    def test_acpi_reapplied_on_booted_entry(self):
        self.acpi.booted = False
        with patch("buo.state.reconcile.run_command", self.responder()):
            report = self.make().reconcile()
        self.assertEqual(self.acpi.apply_calls, 1)
        self.assertTrue(report["acpi"]["applied"])
        self.assertTrue(report["rebooted"])

    def test_acpi_ok_does_not_touch_the_entry(self):
        with patch("buo.state.reconcile.run_command", self.responder()):
            self.make().reconcile()
        self.assertEqual(self.acpi.apply_calls, 0)

    def test_services_enabled_and_oneshot_started(self):
        resp = self.responder(enabled=(), ran=())
        with patch("buo.state.reconcile.run_command", resp):
            report = self.make().reconcile()
        enable_cmds = [c for c in resp.commands
                       if "enable" in c and c[:1] == ["systemctl"]]
        self.assertEqual(len(enable_cmds), 2)
        self.assertTrue(report["services"])

    def test_services_ok_but_oneshot_never_ran_is_started(self):
        """Enablement perso/avvio fallito: OC CPU e 40 CU non applicati."""
        resp = self.responder(ran=())
        with patch("buo.state.reconcile.run_command", resp):
            report = self.make().reconcile()
        start_cmds = [c for c in resp.commands
                      if "start" in c and c[:1] == ["systemctl"]]
        self.assertGreaterEqual(len(start_cmds), 2)
        self.assertNotIn("healthy", report)
        self.assertTrue(report["services"])

    def test_condemned_silicon_is_never_reunlocked(self):
        """Silicio harvest difettoso: MAI riattivare i core extra."""
        self.present.write_text("0-11\n")
        rec = self.make(verdict=FakeVerdict(cpu="never_unlock"))
        with patch("buo.state.reconcile.run_command", self.responder()):
            report = rec.reconcile()
            self.assertEqual(self.cpu.calls, 0, "nessun accesso SMU")
            self.assertEqual(self.reboot_calls, [])
            self.assertTrue(any("never_unlock" in s
                                for s in report["skipped"]))
            # 12 thread È lo stato corretto: nessun problema segnalato
            self.assertEqual(rec.degraded(), [])

    def test_boot_run_ignores_steam_session_but_not_games(self):
        """Al boot Steam È la sessione: non deve bloccare il recupero."""
        self.present.write_text("0-11\n")
        rec = self.make(boot_run=True)
        rec._game_check_fn = lambda: False          # niente gioco
        with patch("buo.state.reconcile.run_command", self.responder()):
            report = rec.reconcile()
        self.assertEqual(report["reboot"]["rebooted"], True)

    def test_manual_run_still_blocks_on_steam_session(self):
        """Una run manuale mid-session resta prudente (regola di campo)."""
        self.present.write_text("0-11\n")
        self.game[0] = True
        rec = self.make()                            # boot_run=False
        rec._game_check_fn = lambda: True
        with patch("buo.state.reconcile.run_command", self.responder()):
            report = rec.reconcile()
        self.assertEqual(report["reboot"]["blocked"], "sessione_gioco_attiva")

    def test_game_check_real_splits_game_from_session(self):
        """`pgrep` che trova 'steam' blocca la run manuale, non quella di boot."""
        class SteamOnly:
            def __init__(self):
                self.seen = []

            def __call__(self, cmd, **kwargs):
                self.seen.append(cmd[-1])
                return (0, "1234 steam\n", "") if cmd[-1] == "steam" \
                    else (1, "", "")

        manual = self.make()
        manual._game_check_fn = None
        with patch("buo.state.reconcile.run_command", SteamOnly()):
            self.assertTrue(manual._game_active_real(), "run manuale: blocca")
        boot = self.make(boot_run=True)
        boot._game_check_fn = None
        with patch("buo.state.reconcile.run_command", SteamOnly()):
            self.assertFalse(boot._game_active_real(),
                             "run di boot: la sessione Steam non blocca")

    def test_dry_run_never_writes_nor_reboots(self):
        self.present.write_text("0-11\n")
        self.acpi.booted = False
        with patch("buo.state.reconcile.run_command", self.responder()):
            report = self.make(dry_run=True).reconcile()
        self.assertEqual(self.cpu.calls, 0)
        self.assertEqual(self.acpi.apply_calls, 0)
        self.assertEqual(self.reboot_calls, [])
        self.assertFalse(self.attempts.exists())
        self.assertEqual(self.mode.read_text().strip(), "cold")

    def test_degraded_lists_only_problems(self):
        rec = self.make()
        with patch("buo.state.reconcile.run_command", self.responder()):
            self.assertEqual(rec.degraded(), [])
        self.present.write_text("0-11\n")
        self.acpi.booted = False
        with patch("buo.state.reconcile.run_command",
                   self.responder(gov_state="inactive")):
            problems = rec.degraded()
        self.assertEqual(len(problems), 3)
        self.assertTrue(any("thread" in p for p in problems))
        self.assertTrue(any("governor" in p for p in problems))
        self.assertTrue(any("ACPI" in p for p in problems))


class UnitTestCase(unittest.TestCase):
    """Contenuto dell'unità di boot (nessun systemd reale)."""

    def test_unit_uses_python_module_entrypoint(self):
        text = unit_content("/var/opt/buo-venv/bin/python")
        self.assertIn("ExecStart=/var/opt/buo-venv/bin/python -m buo "
                      "boot-reconcile", text)
        self.assertIn("WantedBy=graphical.target", text)
        self.assertIn("Before=graphical.target", text)
        self.assertIn("boot-reconcile --boot", text)
        self.assertIn("WorkingDirectory=/tmp", text)

    def test_unit_name_and_no_boot_block_protection(self):
        self.assertEqual(BOOT_UNIT, "buo-boot-reconcile.service")
        # Un fallimento dell'agente non deve bloccare il boot
        # Il reboot di riparazione termina l'agente con SIGTERM: non deve
        # risultare "failed" nel journal (era cosi' nel primo test sul campo).
        self.assertIn("SuccessExitStatus=0 1 SIGTERM",
                      unit_content("/usr/bin/python3"))


if __name__ == "__main__":
    unittest.main()
