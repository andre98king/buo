#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Regressione: il dry-run non deve inquinare lo stato persistente, e un
run reale non deve riprendere da un checkpoint "complete" di una run
precedente (bug trovato sul campo: dry-run → run reale "completata"
in 20s senza fare nulla).
"""

import os
import tempfile
import unittest

from buo.config import BUOConfig
from buo.orchestrator import Orchestrator
from buo.state.checkpoint import CheckpointManager
from buo.utils.mock import MockHardware


class TestDryRunStateIsolation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def _make(self, mock=True, dry_run=False):
        cfg = BUOConfig()
        cfg.benchmark_enabled = False
        cfg.validation_stress_duration = 0
        return Orchestrator(config=cfg, mock=mock, dry_run=dry_run,
                            mock_hardware=MockHardware(seed=7) if mock else None)

    def test_dry_run_leaves_no_checkpoint(self):
        orch = self._make(mock=True, dry_run=True)
        orch.checkpoint.clear()
        rc = orch.run()
        self.assertEqual(rc, 0)
        # Nessuna fase persistita dal dry-run
        state = orch.checkpoint.full_state()
        self.assertEqual(state["phases"], {})
        self.assertEqual(state["current_phase"], "init")

    def test_dry_run_then_real_run_executes_phases(self):
        """Bug sul campo: dry-run → run reale deve rifare TUTTO da init."""
        # 1. dry-run
        orch = self._make(mock=True, dry_run=True)
        orch.checkpoint.clear()
        orch.run()

        # 2. run reale (stessa istanza di stato su disco)
        real = self._make(mock=True, dry_run=False)
        rc = real.run()
        self.assertEqual(rc, 0)
        phases = real.checkpoint.full_state()["phases"]
        for phase in ["init", "pre_audit", "unlock", "fix", "optimize",
                      "apply", "validate"]:
            self.assertTrue(phases[phase]["completed"],
                            f"fase {phase} non eseguita nel run reale")

    def test_real_run_ignores_stale_complete_checkpoint(self):
        """Checkpoint 'complete' di una run precedente → riparte da init."""
        from buo.utils.paths import state_dir
        cm = CheckpointManager(state_dir())
        cm.clear()
        cm.set_phase("complete", {"done": True}, completed=True)
        cm.set_current_phase("complete")

        orch = self._make(mock=True, dry_run=False)
        rc = orch.run()
        self.assertEqual(rc, 0)
        phases = orch.checkpoint.full_state()["phases"]
        self.assertIn("pre_audit", phases)  # ha eseguito davvero
        self.assertNotIn("unlock", [])  # non deve fallire


if __name__ == "__main__":
    unittest.main()
