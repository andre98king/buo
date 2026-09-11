#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DIFETTO 2 e 3: il governor è VERIFICATO in validate e lo stato si legge da
`systemctl show -p ActiveState` (mai con `is-active`, che esce con rc=3
anche per gli stati transitori `activating`/`deactivating`).

Zero hardware reale: la lettura dello stato è il punto unico
`buo/optimize/governor.py` e viene sempre mockata (C1).
"""

import os
import tempfile
import unittest
from unittest import mock

from buo.config import BUOConfig
from buo.optimize.governor import (GovernorWrapper,
                                   governor_confirmed_inactive,
                                   governor_states)
from buo.orchestrator import Orchestrator
from buo.utils.mock import MockHardware
from buo.validate.verify import FixVerifier


def _states(active, load="loaded"):
    """Output di `systemctl show -p ActiveState -p LoadState`."""
    return mock.Mock(returncode=0,
                     stdout=f"ActiveState={active}\nLoadState={load}\n")


class TestGovernorStateRead(unittest.TestCase):
    """Punto UNICO di lettura dello stato (sostituisce `is-active`)."""

    def _read(self, **kw):
        with mock.patch("buo.optimize.governor.subprocess.run",
                        **kw) as run:
            return governor_states(), run

    def test_active_state_parsed(self):
        st, run = self._read(return_value=_states("activating"))
        self.assertEqual(st, {"ActiveState": "activating",
                              "LoadState": "loaded"})
        # `show` con le DUE proprietà: `is-active` non distingue i
        # transitori (rc=3) → sarebbe "fermo" per errore.
        args = run.call_args[0][0]
        self.assertEqual(args[1:3], ["show", "-p"])
        self.assertIn("ActiveState", args)

    def test_unreadable_returns_empty(self):
        for kw in ({"return_value": mock.Mock(returncode=1, stdout="")},
                   {"side_effect": OSError("no systemctl")}):
            st, _ = self._read(**kw)
            self.assertEqual(st, {})

    def test_confirmed_inactive_only_for_stopped_states(self):
        for state, expected in (("inactive", True), ("failed", True),
                                ("active", False), ("activating", None),
                                ("deactivating", None), ("reloading", None),
                                ("", None)):
            with mock.patch("buo.optimize.governor.subprocess.run",
                            return_value=_states(state)):
                self.assertIs(governor_confirmed_inactive(), expected,
                              f"state={state!r}")


class TestGovernorWrapperState(unittest.TestCase):
    def test_is_running_only_active(self):
        for state, expected in (("active", True), ("activating", False),
                                ("inactive", False), ("failed", False)):
            w = GovernorWrapper(mock=False)
            with mock.patch("buo.optimize.governor.subprocess.run",
                            return_value=_states(state)):
                self.assertIs(w.is_running(), expected, f"state={state!r}")

    def test_is_installed_from_load_state(self):
        """Not-found = governor NON installato (check non applicabile)."""
        w = GovernorWrapper(mock=False)
        with mock.patch("buo.optimize.governor.subprocess.run",
                        return_value=_states("inactive", load="loaded")):
            self.assertTrue(w.is_installed())
        with mock.patch("buo.optimize.governor.subprocess.run",
                        return_value=_states("inactive", load="not-found")):
            self.assertFalse(w.is_installed())

    def test_mock_never_touches_systemctl(self):
        w = GovernorWrapper(mock=True)
        with mock.patch("buo.optimize.governor.subprocess.run") as run:
            self.assertFalse(w.is_running())
            self.assertFalse(w.is_installed())
        run.assert_not_called()


class TestGovernorVerifiedInValidate(unittest.TestCase):
    """DIFETTO 2: governor INSTALLATO ma non attivo → esito negativo
    VISIBILE (log ERROR + nota nel report + voce fix verification), senza
    falso fallimento su macchine senza governor."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def _orch(self):
        cfg = BUOConfig()
        cfg.validation_stress_duration = 0
        orch = Orchestrator(config=cfg, mock=True, dry_run=False,
                            mock_hardware=MockHardware(seed=3))
        orch.checkpoint.clear()
        # Verificatore NON mock: in mock `_check_governor` risponde
        # "attivo (mock)" e non eserciterebbe la lettura reale.
        orch.verifier = FixVerifier(mock=False)
        return orch

    def test_governor_inactive_is_visible_negative(self):
        orch = self._orch()
        verification = {}
        with mock.patch.object(orch.governor, "is_installed",
                               return_value=True), \
             mock.patch("buo.optimize.governor.subprocess.run",
                        return_value=_states("inactive")), \
             self.assertLogs("buo.Orchestrator", level="ERROR") as logs:
            orch._verify_governor(verification)
        self.assertIs(verification["governor"]["ok"], False)
        self.assertEqual(verification["governor"]["detail"], "inactive")
        self.assertTrue(any("Governor NON attivo" in m for m in logs.output))
        self.assertTrue(any("Governor NON attivo" in n
                            for n in orch.results["notes"]))

    def test_governor_active_ok_and_no_notes(self):
        orch = self._orch()
        verification = {}
        with mock.patch.object(orch.governor, "is_installed",
                               return_value=True), \
             mock.patch("buo.optimize.governor.subprocess.run",
                        return_value=_states("active")):
            orch._verify_governor(verification)
        self.assertIs(verification["governor"]["ok"], True)
        self.assertEqual(orch.results["notes"], [])

    def test_not_installed_no_check_no_false_failure(self):
        """Macchina senza governor: nessun check, nessun fallimento."""
        orch = self._orch()
        verification = {}
        with mock.patch.object(orch.governor, "is_installed",
                               return_value=False):
            orch._verify_governor(verification)
        self.assertEqual(verification, {})
        self.assertEqual(orch.results["notes"], [])
