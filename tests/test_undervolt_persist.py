#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test G3: persistenza dell'undervolt (bc250-apply --install).

Dopo il fix, il punto stabile non è più solo volatile: con
persist=True (default di config.undervolt_persist) si esegue anche
`--install` → il profilo viene riapplicato a ogni boot.
"""

import os
import tempfile
import unittest
from unittest import mock

from buo.config import BUOConfig
from buo.orchestrator import Orchestrator


class TestPersistentUndervolt(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def _make(self):
        cfg = BUOConfig()
        cfg.validation_stress_duration = 0
        cfg.benchmark_enabled = False
        return Orchestrator(config=cfg, mock=False, dry_run=False)

    def _fake_wrapper(self):
        w = mock.Mock()
        w.available = True
        w.apply.return_value = {"returncode": 0, "stderr": ""}
        w.install.return_value = {"returncode": 0, "stderr": ""}
        return w

    def test_persist_true_calls_apply_and_install(self):
        orch = self._make()
        w = self._fake_wrapper()
        with mock.patch("buo.unlock.wrappers.bc250_overclock."
                        "BC250ApplyWrapper", return_value=w):
            out = orch._apply_cpu_config(3800, scale=0, vid=1224,
                                         persist=True)
        self.assertTrue(out["applied"])
        self.assertTrue(out.get("persistent"))
        w.apply.assert_called_once()
        w.install.assert_called_once()
        self.assertIn("--install", out["method"])

    def test_persist_false_does_not_install(self):
        orch = self._make()
        w = self._fake_wrapper()
        with mock.patch("buo.unlock.wrappers.bc250_overclock."
                        "BC250ApplyWrapper", return_value=w):
            out = orch._apply_cpu_config(3800, scale=0, vid=1224,
                                         persist=False)
        self.assertTrue(out["applied"])
        self.assertNotIn("persistent", out)
        w.apply.assert_called_once()
        w.install.assert_not_called()

    def test_install_failure_reported_but_apply_kept(self):
        """--install fallisce → avviso esplicito, il volatile resta."""
        orch = self._make()
        w = self._fake_wrapper()
        w.install.return_value = {"returncode": 1, "stderr": "boom"}
        with mock.patch("buo.unlock.wrappers.bc250_overclock."
                        "BC250ApplyWrapper", return_value=w):
            out = orch._apply_cpu_config(3800, scale=0, vid=1224,
                                         persist=True)
        self.assertTrue(out["applied"])
        self.assertFalse(out["persistent"])
        self.assertIn("boom", out["persist_error"])

    def test_dry_run_never_touches_wrapper(self):
        orch = Orchestrator(config=BUOConfig(), mock=False, dry_run=True)
        out = orch._apply_cpu_config(3800, scale=0, vid=1224, persist=True)
        self.assertTrue(out["dry_run"])
        self.assertNotIn("persistent", out)

    def test_config_default_persist_is_true(self):
        self.assertTrue(BUOConfig().undervolt_persist)

    def test_config_persist_false_from_yaml(self):
        cfg = BUOConfig({"phases": {"undervolt": {"persist": False}}})
        self.assertFalse(cfg.undervolt_persist)


if __name__ == "__main__":
    unittest.main()
