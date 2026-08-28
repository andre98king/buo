#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test del checkpoint manager."""

import tempfile
import unittest
from pathlib import Path

from buo.exceptions import CheckpointError
from buo.state.checkpoint import CheckpointManager


class TestCheckpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = CheckpointManager(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_initial_state(self):
        self.assertEqual(self.manager.get_current_phase(), "init")
        self.assertEqual(self.manager.get_reboot_count(), 0)

    def test_set_and_get_phase(self):
        self.manager.set_phase("probe_cpu", {"cores": 8}, completed=True)
        phase = self.manager.get_phase("probe_cpu")
        self.assertTrue(phase["completed"])
        self.assertEqual(phase["data"]["cores"], 8)
        self.assertEqual(self.manager.get_current_phase(), "probe_cpu")

    def test_persistence(self):
        self.manager.set("reboot_count", 3)
        self.manager.set_phase("unlock", {"ok": True}, completed=False)

        # Ricarica da disco (nuova istanza)
        reloaded = CheckpointManager(Path(self.tmp.name))
        self.assertEqual(reloaded.get_reboot_count(), 3)
        self.assertEqual(reloaded.get_current_phase(), "unlock")

    def test_rollback_to_phase_removes_later_phases(self):
        self.manager.set_phase("init", {}, completed=True)
        self.manager.set_phase("pre_audit", {}, completed=True)
        self.manager.set_phase("unlock", {}, completed=True)
        self.manager.set_phase("fix", {}, completed=True)

        self.manager.rollback_to_phase("unlock")
        self.assertEqual(self.manager.get_current_phase(), "unlock")
        phases = self.manager.full_state()["phases"]
        self.assertNotIn("fix", phases)
        self.assertIn("unlock", phases)

    def test_backup_created(self):
        self.manager.set("x", 1)
        self.manager.set("x", 2)  # il secondo salvataggio crea un backup
        backups = list((Path(self.tmp.name) / "backups").glob("state_*.json"))
        self.assertGreaterEqual(len(backups), 1)

    def test_corrupt_state_fails_closed(self):
        """A2: JSON corrotto → CheckpointError, NON reset silenzioso."""
        state_file = Path(self.tmp.name) / "state.json"
        state_file.write_text("{ questo non è json !!!")
        with self.assertRaises(CheckpointError):
            CheckpointManager(Path(self.tmp.name))
        # il file corrotto resta intatto (per intervento manuale)
        self.assertTrue(state_file.exists())

    def test_non_dict_state_fails_closed(self):
        state_file = Path(self.tmp.name) / "state.json"
        state_file.write_text("[1, 2, 3]")
        with self.assertRaises(CheckpointError):
            CheckpointManager(Path(self.tmp.name))

    def test_save_is_atomic_no_tmp_leftover(self):
        self.manager.set("x", "valore")
        self.assertFalse((Path(self.tmp.name) / "state.json.tmp").exists())
        self.assertTrue((Path(self.tmp.name) / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
