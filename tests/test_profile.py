#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test G2: profilo macchina (export/import) e modalità restore."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from buo.config import BUOConfig
from buo.orchestrator import Orchestrator
from buo.profile import (PROFILE_VERSION, default_profile_path, export_profile,
                         load_profile)
from buo.state.checkpoint import CheckpointManager
from buo.utils.mock import MockHardware

OPTIMIZE_DATA = {
    "undervolt_cpu": {"best_efficiency": {"freq": 3800, "scale": 0,
                                           "vid": 1224}},
    "undervolt_gpu": {"safe_points": [{"freq": 1200, "voltage": 1000}]},
    "overclock_cpu": {"recommended_freq": 3800},
}


class TestProfile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def _seed_checkpoint(self):
        cm = CheckpointManager()
        cm.seed_phase("optimize", OPTIMIZE_DATA)
        cm.set("applied_steps", ["acpi_fix", "gpu_40cu"])
        return cm

    def test_export_import_roundtrip(self):
        self._seed_checkpoint()
        out = Path(self._tmp.name) / "profilo.json"
        prof = export_profile(out)
        self.assertEqual(prof["profile_version"], PROFILE_VERSION)
        self.assertEqual(prof["applied_fixes"],
                         ["acpi_fix", "gpu_40cu"])
        self.assertEqual(prof["optimize"]["undervolt_cpu"]
                         ["best_efficiency"]["freq"], 3800)

        loaded = load_profile(out)
        self.assertEqual(loaded["optimize"]["undervolt_gpu"]
                         ["safe_points"][0]["voltage"], 1000)

    def test_default_profile_path_in_state_dir(self):
        self.assertEqual(default_profile_path().parent.name,
                         Path(self._tmp.name).name)

    def test_load_missing_raises(self):
        with self.assertRaises(ValueError):
            load_profile(Path(self._tmp.name) / "assente.json")

    def test_load_invalid_json_raises(self):
        p = Path(self._tmp.name) / "bad.json"
        p.write_text("{ nope")
        with self.assertRaises(ValueError):
            load_profile(p)

    def test_load_wrong_version_raises(self):
        p = Path(self._tmp.name) / "old.json"
        p.write_text(json.dumps({"profile_version": 0, "optimize": {}}))
        with self.assertRaises(ValueError):
            load_profile(p)

    def test_load_missing_optimize_raises(self):
        p = Path(self._tmp.name) / "noopt.json"
        p.write_text(json.dumps({"profile_version": PROFILE_VERSION}))
        with self.assertRaises(ValueError):
            load_profile(p)


class TestRestoreMode(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def _profile(self):
        return {
            "profile_version": PROFILE_VERSION,
            "created": "2026-01-01T00:00:00",
            "applied_fixes": ["acpi_fix", "gpu_40cu"],
            "optimize": OPTIMIZE_DATA,
        }

    def test_restore_skips_optimize_and_seeds_data(self):
        hw = MockHardware(seed=11)
        cfg = BUOConfig()
        cfg.validation_stress_duration = 0
        cfg.benchmark_enabled = False
        orch = Orchestrator(config=cfg, mock=True, dry_run=True,
                            mock_hardware=hw)
        orch.checkpoint.clear()

        with mock.patch.object(orch, "_phase_optimize",
                               side_effect=AssertionError(
                                   "optimize NON deve girare in restore")):
            rc = orch.run(restore=self._profile())

        self.assertEqual(rc, 0)
        data = orch.checkpoint.get_phase("optimize").get("data", {})
        self.assertEqual(data["undervolt_cpu"]["best_efficiency"]["freq"],
                         3800)

    def test_run_without_restore_runs_optimize(self):
        hw = MockHardware(seed=11)
        cfg = BUOConfig()
        cfg.validation_stress_duration = 0
        cfg.benchmark_enabled = False
        orch = Orchestrator(config=cfg, mock=True, dry_run=True,
                            mock_hardware=hw)
        orch.checkpoint.clear()
        called = []
        with mock.patch.object(orch, "_phase_optimize",
                               side_effect=lambda: called.append(1) or {}):
            orch.run()
        self.assertEqual(len(called), 1)

    # ------------------------- BUG F-A (restore + resume) ------------- #

    def test_restore_marker_not_persisted_in_dry_run(self):
        """F-A: il marcatore restore_active viene scritto SOLO nei run
        reali (mai in dry-run, coerente con le altre scritture
        checkpoint)."""
        hw = MockHardware(seed=11)
        cfg = BUOConfig()
        cfg.validation_stress_duration = 0
        cfg.benchmark_enabled = False
        orch = Orchestrator(config=cfg, mock=True, dry_run=True,
                            mock_hardware=hw)
        orch.checkpoint.clear()
        with mock.patch.object(orch, "_phase_optimize",
                               side_effect=AssertionError(
                                   "optimize NON deve girare in restore")):
            rc = orch.run(restore=self._profile())
        self.assertEqual(rc, 0)
        self.assertFalse(orch.checkpoint.get("restore_active"))

    def test_restore_mode_survives_resume_via_checkpoint(self):
        """F-A: dopo reboot il resume (NUOVO Orchestrator SENZA restore)
        rileva il marcatore restore_active e NON rilancia l'auto-tuning.

        Simula il resume reale: checkpoint con fase a metà (fix),
        marcatore restore_active e dati optimize seedati → il run riparte
        da fix e optimize restituisce i dati del profilo (3800) invece di
        girare il tuning (bc250-detect).
        """
        cm = CheckpointManager()
        cm.seed_phase("optimize", OPTIMIZE_DATA)
        cm.set("restore_active", True)
        cm.set_current_phase("fix")

        cfg = BUOConfig()
        cfg.validation_stress_duration = 0
        cfg.benchmark_enabled = False
        orch = Orchestrator(config=cfg, mock=True, dry_run=False,
                            mock_hardware=MockHardware(seed=11))
        # il resume NON passa restore → run() senza argomenti
        with mock.patch.object(
                orch, "_phase_optimize",
                side_effect=AssertionError(
                    "auto-tuning NON deve girare al resume di un restore")):
            rc = orch.run()

        self.assertEqual(rc, 0)
        data = orch.checkpoint.get_phase("optimize").get("data", {})
        self.assertEqual(data["undervolt_cpu"]["best_efficiency"]["freq"],
                         3800)

    def test_restore_marker_cleared_after_completed_run(self):
        """F-A: a run completo il marcatore è pulito: un unleash successivo
        NON eredita la modalità restore e rilancia il tuning."""
        cfg = BUOConfig()
        cfg.validation_stress_duration = 0
        cfg.benchmark_enabled = False
        orch = Orchestrator(config=cfg, mock=True, dry_run=False,
                            mock_hardware=MockHardware(seed=11))
        orch.checkpoint.clear()
        rc = orch.run(restore=self._profile())
        self.assertEqual(rc, 0)
        self.assertFalse(orch.checkpoint.get("restore_active"))

        # unleash nuovo (processo nuovo, senza restore): il tuning gira
        orch2 = Orchestrator(config=cfg, mock=True, dry_run=False,
                             mock_hardware=MockHardware(seed=11))
        called = []
        with mock.patch.object(orch2, "_phase_optimize",
                               side_effect=lambda: called.append(1) or {}):
            rc2 = orch2.run()
        self.assertEqual(rc2, 0)
        self.assertEqual(len(called), 1,
                         "l'unleash successivo deve rilanciare il tuning")


if __name__ == "__main__":
    unittest.main()
