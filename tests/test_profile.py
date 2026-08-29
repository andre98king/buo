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

    def test_restore_marker_cleared_on_safety_abort(self):
        """F-A: su abort di sicurezza il marcatore restore viene pulito —
        un unleash successivo che riprende dalla fase interrotta NON
        eredita la modalità restore (l'abort non crea resume service)."""
        from buo.exceptions import SafetyViolation
        from buo.constants import EXIT_SAFETY_VIOLATION

        cfg = BUOConfig()
        cfg.validation_stress_duration = 0
        cfg.benchmark_enabled = False
        orch = Orchestrator(config=cfg, mock=True, dry_run=False,
                            mock_hardware=MockHardware(seed=11))
        orch.checkpoint.clear()
        orch.checkpoint.set("restore_active", True)
        orch.checkpoint.set_current_phase("fix")

        def _boom():
            raise SafetyViolation("test abort")

        with mock.patch.object(orch, "_phase_fix", side_effect=_boom), \
             mock.patch.object(orch.rollback, "rollback") as rb:
            rc = orch.run()
        self.assertEqual(rc, EXIT_SAFETY_VIOLATION)
        self.assertFalse(orch.checkpoint.get("restore_active"),
                         "l'abort deve pulire il marcatore restore")
        rb.assert_called_once()

        # unleash successivo: resume da fix, optimize RILANCIA il tuning
        orch2 = Orchestrator(config=cfg, mock=True, dry_run=False,
                             mock_hardware=MockHardware(seed=11))
        called = []
        with mock.patch.object(orch2, "_phase_optimize",
                               side_effect=lambda: called.append(1) or {}):
            rc2 = orch2.run()
        self.assertEqual(rc2, 0)
        self.assertEqual(len(called), 1,
                         "l'unleash post-abort deve rilanciare il tuning")

    def test_restore_marker_cleared_on_fresh_init_without_restore(self):
        """F-A: un run nuovo da init (checkpoint 'complete') SENZA restore
        pulisce il marcatore anche se era rimasto attivo."""
        cfg = BUOConfig()
        cfg.validation_stress_duration = 0
        cfg.benchmark_enabled = False
        orch = Orchestrator(config=cfg, mock=True, dry_run=False,
                            mock_hardware=MockHardware(seed=11))
        orch.checkpoint.clear()
        orch.checkpoint.set("restore_active", True)  # stato anomalo
        called = []
        with mock.patch.object(orch, "_phase_optimize",
                               side_effect=lambda: called.append(1) or {}):
            rc = orch.run()
        self.assertEqual(rc, 0)
        self.assertEqual(len(called), 1, "il tuning deve girare")
        self.assertFalse(orch.checkpoint.get("restore_active"))

    # ---------------- FIX: "stress saltato" persistente al resume -------- #
    # `buo restore` (senza --validate) imposta validation_stress_duration=0
    # nel processo CLI; dopo un reboot il resume (NUOVO processo) ricarica
    # la config con lo stress_duration REALE (3) e rifaceva lo stress
    # completo. Il marcatore persistente estende lo skip al resume.

    def _recording_stress(self, orch, durations):
        """Sostituisce orch.stress.run con una spia che registra le durate
        (stessa firma di StressTest.run, nessuno spawn)."""
        def fake_run(duration_minutes=30, power_budget=300, scope="both"):
            durations.append(duration_minutes)
            return {
                "passed": True, "skipped": duration_minutes == 0,
                "duration_minutes": duration_minutes, "scope": scope,
                "cpu_temp_max": None, "gpu_temp_max": None,
                "power_max": None, "errors": 0,
            }
        orch.stress.run = fake_run

    def test_restore_with_stress_zero_writes_marker(self):
        """restore con stress 0 (CLI senza --validate) → marcatore
        validation_stress_skip scritto nel checkpoint (solo run reali);
        con stress 3 (--validate) il marcatore NON viene scritto."""
        for stress_duration, expected in ((0, True), (3, False)):
            cfg = BUOConfig()
            cfg.validation_stress_duration = stress_duration
            cfg.benchmark_enabled = False
            orch = Orchestrator(config=cfg, mock=True, dry_run=False,
                                mock_hardware=MockHardware(seed=11))
            orch.checkpoint.clear()
            with mock.patch.object(orch, "_finalize"):
                rc = orch.run(restore=self._profile(), stop_after="optimize")
            self.assertEqual(rc, 0)
            self.assertEqual(
                bool(orch.checkpoint.get("validation_stress_skip")),
                expected)

    def test_restore_with_stress_zero_skips_stress_and_clears_marker(self):
        """Run restore completo (stress 0): _phase_validate usa durata 0
        (skip vero) e a finalize il marcatore è pulito."""
        cfg = BUOConfig()
        cfg.validation_stress_duration = 0
        cfg.benchmark_enabled = False
        orch = Orchestrator(config=cfg, mock=True, dry_run=False,
                            mock_hardware=MockHardware(seed=11))
        orch.checkpoint.clear()
        durations = []
        self._recording_stress(orch, durations)
        rc = orch.run(restore=self._profile())
        self.assertEqual(rc, 0)
        self.assertEqual(durations, [0],
                         "lo stress deve essere chiamato con durata 0")
        self.assertFalse(orch.checkpoint.get("validation_stress_skip"),
                         "marcatore pulito a finalize")

    def test_resume_after_restore_keeps_stress_skipped(self):
        """Dopo reboot il resume (NUOVO Orchestrator SENZA restore, config
        con stress_duration=3 reale) NON esegue lo stress: il marcatore
        prevale → durata 0 (mai chiamato con 3)."""
        cm = CheckpointManager()
        cm.seed_phase("optimize", OPTIMIZE_DATA)
        cm.set("validation_stress_skip", True)
        cm.set_current_phase("validate")

        cfg = BUOConfig()
        cfg.validation_stress_duration = 3   # valore REALE della macchina
        cfg.benchmark_enabled = False
        orch = Orchestrator(config=cfg, mock=True, dry_run=False,
                            mock_hardware=MockHardware(seed=11))
        durations = []
        self._recording_stress(orch, durations)
        rc = orch.run()                       # resume, niente restore
        self.assertEqual(rc, 0)
        self.assertEqual(durations, [0],
                         "stress saltato al resume (mai chiamato con 3)")
        self.assertFalse(orch.checkpoint.get("validation_stress_skip"))

    def test_fresh_unleash_clears_marker_and_runs_stress(self):
        """Unleash nuovo da init senza restore: il marcatore residuo viene
        pulito e lo stress gira con la durata normale (3)."""
        cfg = BUOConfig()
        cfg.validation_stress_duration = 3
        cfg.benchmark_enabled = False
        orch = Orchestrator(config=cfg, mock=True, dry_run=False,
                            mock_hardware=MockHardware(seed=11))
        orch.checkpoint.clear()
        orch.checkpoint.set("validation_stress_skip", True)  # residuo
        durations = []
        self._recording_stress(orch, durations)
        rc = orch.run()
        self.assertEqual(rc, 0)
        self.assertEqual(durations, [3], "stress normale al nuovo unleash")
        self.assertFalse(orch.checkpoint.get("validation_stress_skip"))

    def test_restore_stress_zero_marker_not_written_in_dry_run(self):
        """F-A: come restore_active, il marcatore stress-skip è scritto
        SOLO nei run reali (mai in dry-run)."""
        cfg = BUOConfig()
        cfg.validation_stress_duration = 0
        cfg.benchmark_enabled = False
        orch = Orchestrator(config=cfg, mock=True, dry_run=True,
                            mock_hardware=MockHardware(seed=11))
        orch.checkpoint.clear()
        with mock.patch.object(orch, "_finalize"):
            rc = orch.run(restore=self._profile(), stop_after="optimize")
        self.assertEqual(rc, 0)
        self.assertFalse(orch.checkpoint.get("validation_stress_skip"))


if __name__ == "__main__":
    unittest.main()
