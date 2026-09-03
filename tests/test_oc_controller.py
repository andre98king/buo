#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test di OcController: argv ESATTO di systemd-run (unità transient),
precondizioni (engine assente / run attiva), stop/reset/watch, dry-run
(nessun comando reale), status su fixture. Mai hardware reale.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from buo.oc.constants import (
    CONSOLE_LOG,
    ENGINE_SCRIPT,
    LAUNCH_PID,
    RUN_PID,
    STATE_FILE,
    UNIT_NAME,
)
from buo.oc.controller import OcController
from buo.oc.state import OcStateReader


class SysRecorder:
    """Fake di run_command: registra argv; systemctl/unità simulati."""

    def __init__(self):
        self.calls = []
        self.unit_active = "inactive"
        self.main_pid = "4242"

    def __call__(self, cmd, timeout=60, sudo=False, capture=True, **kw):
        self.calls.append(list(cmd))
        base = cmd[0]
        if base == "systemctl":
            if cmd[1:3] == ["is-active", UNIT_NAME]:
                return (0, self.unit_active, "")
            if cmd[1:3] == ["show", "-p"]:
                return (0, self.main_pid, "")   # systemctl --value → solo PID
            if cmd[1] == "stop":
                self.unit_active = "inactive"
                return (0, "", "")
            return (0, "", "")
        return (0, "", "")


class FakeStateReader:
    """OcStateReader con pgrep mockato (mai subprocess reali)."""

    def __init__(self, oc_dir, active_pid=None, state=None):
        self.oc_dir = Path(oc_dir)
        self._active = active_pid
        self._state = state

    def read_state(self, state_path=None):
        return self._state

    def log_tail(self, n=14, path=None):
        return ["riga di log"]

    def run_pid(self, pid_path=None):
        return None

    def engine_process_active(self, pattern="[o]c3600[.]sh"):
        return self._active


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.oc = Path(self.tmp.name)
        self.engine = self.oc / ENGINE_SCRIPT
        self.engine.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        self.engine.chmod(0o755)
        self.rec = SysRecorder()

    def tearDown(self):
        self.tmp.cleanup()

    def ctl(self, **kw):
        # state_reader con pgrep FAKE di default: mai pgrep reale nei test
        # (suite parallele con processi oc3600.sh vivi avvelenerebbero il
        # check → "GIÀ attivo" falso)
        kw.setdefault("state_reader", FakeStateReader(self.oc))
        c = OcController(oc_dir=self.oc, mock=False, dry_run=False, **kw)
        c._cmd = self.rec
        return c


class TestStart(Base):
    def test_start_exact_systemd_run_argv(self):
        c = self.ctl()
        c.start(["--cap-freq", "3850", "--no-fine"])
        run = [x for x in self.rec.calls if x[0] == "systemd-run"]
        self.assertEqual(len(run), 1)
        argv = run[0]
        self.assertIn("--unit", argv)
        self.assertEqual(argv[argv.index("--unit") + 1], UNIT_NAME)
        self.assertIn("--collect", argv)
        self.assertIn("--working-directory", argv)
        self.assertEqual(argv[argv.index("--working-directory") + 1],
                         str(self.oc))
        self.assertIn("--setenv", argv)
        self.assertIn(f"OC_DIR={self.oc}", argv)
        self.assertTrue(any(x.startswith("StandardOutput=append:")
                            for x in argv))
        self.assertTrue(any(x.startswith("StandardError=append:")
                            for x in argv))
        # flags verbatim in coda (--no-fine dopo --cap-freq)
        self.assertEqual(argv[-5:-1],
                         ["bash", str(self.engine), "--cap-freq", "3850"])
        self.assertEqual(argv[-1], "--no-fine")
        # launch.pid = MainPID dell'unità
        self.assertEqual((self.oc / LAUNCH_PID).read_text().strip(),
                         "4242")

    def test_start_engine_missing(self):
        self.engine.unlink()
        with self.assertRaises(RuntimeError) as ctx:
            self.ctl().start([])
        self.assertIn("engine non presente", str(ctx.exception))
        self.assertEqual(self.rec.calls, [])

    def test_start_refuses_if_active(self):
        reader = FakeStateReader(self.oc, active_pid=777)
        with self.assertRaises(RuntimeError) as ctx:
            self.ctl(state_reader=reader).start([])
        self.assertIn("GIÀ attivo", str(ctx.exception))
        self.assertEqual(self.rec.calls, [])

    def test_start_dry_run_no_real_commands(self):
        c = OcController(oc_dir=self.oc, mock=True, dry_run=True)
        c.start(["--no-fine"])   # nessuna eccezione, nessun comando
        self.assertEqual(self.rec.calls, [])
        self.assertFalse((self.oc / LAUNCH_PID).exists())


class TestStop(Base):
    def test_stop_active_unit(self):
        self.rec.unit_active = "active"
        self.ctl().stop()
        sys_calls = [c for c in self.rec.calls if c[0] == "systemctl"]
        self.assertTrue(any(c[1] == "stop" and UNIT_NAME in c
                            for c in sys_calls))

    def test_stop_no_unit_noop(self):
        self.ctl().stop()   # unità assente → niente da fermare
        sys_calls = [c for c in self.rec.calls if c[0] == "systemctl"]
        self.assertFalse(any(c[1] == "stop" for c in sys_calls))


class TestReset(Base):
    def test_reset_requires_confirm(self):
        (self.oc / STATE_FILE).write_text("{}", encoding="utf-8")
        with self.assertRaises(RuntimeError) as ctx:
            self.ctl().reset(confirm=False)
        self.assertIn("conferma", str(ctx.exception))
        self.assertTrue((self.oc / STATE_FILE).exists())

    def test_reset_removes_checkpoint_only(self):
        (self.oc / STATE_FILE).write_text("{}", encoding="utf-8")
        (self.oc / RUN_PID).write_text("1\n", encoding="utf-8")
        log = self.oc / "oc.log"
        log.write_text("x\n", encoding="utf-8")
        self.ctl().reset(confirm=True)
        self.assertFalse((self.oc / STATE_FILE).exists())
        self.assertFalse((self.oc / RUN_PID).exists())
        self.assertTrue(log.exists())   # MAI i log

    def test_reset_refuses_if_active(self):
        reader = FakeStateReader(self.oc, active_pid=777)
        with self.assertRaises(RuntimeError):
            self.ctl(state_reader=reader).reset(confirm=True)

    def test_reset_dry_run_or_mock_is_noop(self):
        """M2: reset con --mock/--dry-run NON cancella MAI i file reali
        (state.json/run.pid): modalità simulate = nessuna scrittura."""
        for kw in ({"mock": True}, {"dry_run": True}):
            with self.subTest(kw=kw):
                (self.oc / STATE_FILE).write_text("{}", encoding="utf-8")
                (self.oc / RUN_PID).write_text("1\n", encoding="utf-8")
                ctl = OcController(oc_dir=self.oc,
                                   state_reader=FakeStateReader(self.oc),
                                   **kw)
                ctl.reset(confirm=True)   # nessun RuntimeError, nessun rm
                self.assertTrue((self.oc / STATE_FILE).exists())
                self.assertTrue((self.oc / RUN_PID).exists())
                (self.oc / STATE_FILE).unlink()
                (self.oc / RUN_PID).unlink()


class TestStatus(Base):
    def test_status_fixture(self):
        from buo.oc.state import OcState
        (self.oc / STATE_FILE).write_text(json.dumps({
            "schema_version": 3, "phase": "P2", "persisted": False,
            "l2": {"status": "pending", "runs": 0},
        }), encoding="utf-8")
        reader = FakeStateReader(self.oc, state=OcStateReader(self.oc)
                                 .read_state())
        c = self.ctl(state_reader=reader)
        st = c.status()
        self.assertTrue(st["engine"]["present"])
        self.assertEqual(st["state"]["phase"], "P2")
        self.assertIn("L2", st["state"]["phase_label"])
        self.assertEqual(st["log_tail"], ["riga di log"])

    def test_status_governor_explicit_check(self):
        # is-active è letto ESPLICITAMENTE (systemctl), mai dal reader
        from buo.oc.state import OcState
        reader = FakeStateReader(self.oc, state=OcState())
        c = self.ctl(state_reader=reader)
        c.status()
        sys_calls = [c for c in self.rec.calls if c[0] == "systemctl"]
        self.assertTrue(any(c[1:3] == ["is-active",
                                       "cyan-skillfish-governor-smu"]
                            for c in sys_calls))


class TestHealDelegation(Base):
    @mock.patch("buo.oc.apply.ApplyManager")
    def test_heal_delegates(self, mgr_cls):
        mgr_cls.return_value.heal.return_value = mock.Mock(
            result="rolled_back", cause="stale", details=[])
        out = self.ctl().heal()
        self.assertEqual(out["result"], "rolled_back")
        mgr_cls.return_value.heal.assert_called_once()


if __name__ == "__main__":
    unittest.main()
