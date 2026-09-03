#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test end-to-end dell'orchestratore in modalità mock/dry-run."""

import os
import tempfile
import unittest

from buo.config import BUOConfig
from buo.orchestrator import Orchestrator
from buo.utils.mock import MockHardware


class TestOrchestrator(unittest.TestCase):
    def setUp(self):
        # Stato isolato per ogni test (niente checkpoint condiviso su disco)
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def _make(self, dry_run=True):
        hw = MockHardware(seed=42)
        # Scheda simulata "pronta": fix ACPI già presenti (prerequisito
        # per l'unlock 8-core, vedi test_acpi_gate.py)
        hw.state.is_acpi_fixed = True
        cfg = BUOConfig()
        cfg.validation_stress_duration = 0
        cfg.benchmark_enabled = True
        orch = Orchestrator(config=cfg, mock=True, dry_run=dry_run,
                            mock_hardware=hw)
        orch.checkpoint.clear()  # parte da zero
        return orch

    def test_full_run_success(self):
        # run reale simulato (mock): il checkpoint viene scritto,
        # i reboot sono simulati, tutte le fasi vengono eseguite
        orch = self._make(dry_run=False)
        rc = orch.run()
        self.assertEqual(rc, 0)

        phases = orch.checkpoint.full_state()["phases"]
        for phase in ["init", "pre_audit", "unlock", "fix", "optimize",
                      "apply", "validate", "complete"]:
            self.assertTrue(phases[phase]["completed"], f"fase {phase} non completata")

    def test_apply_fixes_recorded(self):
        orch = self._make()
        orch.run()
        self.assertIn("cpu_core_unlock", orch.results["applied_fixes"])
        self.assertIn("gpu_40cu", orch.results["applied_fixes"])

    def test_before_after_collected(self):
        orch = self._make()
        orch.run()
        self.assertEqual(orch.results["before"]["cpu"]["cores"], 6)
        self.assertEqual(orch.results["after"]["gpu"]["cu_count"], 40)

    def test_problems_detected(self):
        orch = self._make()
        orch.run()
        ids = [p["id"] for p in orch.results["problems"]]
        # IOMMU attivo è lo stato CORRETTO: non deve
        # comparire come problema; il problema è iommu_disabled (kernel)
        self.assertNotIn("iommu_disabled", ids)
        self.assertIn("tlb_fault", ids)

    def test_report_generated(self):
        orch = self._make(dry_run=False)
        orch.run()
        from buo.utils.paths import report_file_json, report_file_md
        self.assertTrue(report_file_md().exists())
        self.assertTrue(report_file_json().exists())

    def test_report_dry_run_never_overwrites_real(self):
        """m2: il dry-run scrive report.*.dry-run, MAI sopra l'ultimo
        report reale (un --dry-run non deve distruggere i dati veri)."""
        from buo.utils.paths import report_file_json, report_file_md
        real_md = report_file_md()
        real_md.parent.mkdir(parents=True, exist_ok=True)
        real_md.write_text("# REPORT REALE\n", encoding="utf-8")
        orch = self._make(dry_run=True)
        orch.run()
        self.assertEqual(real_md.read_text(encoding="utf-8"),
                         "# REPORT REALE\n")
        self.assertTrue(report_file_md().with_name(
            report_file_md().name + ".dry-run").exists())
        self.assertTrue(report_file_json().with_name(
            report_file_json().name + ".dry-run").exists())

    def test_status(self):
        orch = self._make()
        status = orch.status()
        self.assertEqual(status["current_phase"], "init")
        self.assertIsNotNone(status["hardware"])

    def test_status_applied_fixes_from_checkpoint(self):
        """status() legge i fix dal checkpoint (applied_steps), MAI da
        results['applied_fixes'] che fuori da run() è sempre vuoto."""
        orch = self._make()
        orch.checkpoint.set("applied_steps", ["gpu_40cu", "cpu_core_unlock"])
        self.assertEqual(orch.results["applied_fixes"], [])
        status = orch.status()
        self.assertEqual(status["applied_fixes"],
                         ["cpu_core_unlock", "gpu_40cu"])

    def test_recovery_plan(self):
        orch = self._make()
        plan = orch.recovery_plan()
        self.assertIn("interrupted_phase", plan)

    def test_apply_cpu_config_dry_run(self):
        """Applicazione CPU in dry-run: simulata, nessun effetto."""
        orch = self._make(dry_run=True)
        r = orch._apply_cpu_config(3500, scale=0)
        self.assertTrue(r["applied"])
        self.assertTrue(r["dry_run"])

    def test_apply_cpu_config_mock(self):
        orch = self._make(dry_run=False)
        r = orch._apply_cpu_config(3500, scale=0)
        self.assertTrue(r["applied"])
        self.assertTrue(r["mock"])

    def test_apply_cpu_config_clamps_frequency(self):
        """La frequenza è clampata ai limiti immutabili (mai oltre)."""
        orch = self._make(dry_run=True)
        f, s = orch._clamp_cpu(99999)  # oltre il limite
        self.assertEqual(f, 4000)  # LIMITS.cpu.freq_max
        f2, _ = orch._clamp_cpu(100)  # sotto il limite
        self.assertEqual(f2, 3500)  # LIMITS.cpu.freq_min

    def test_apply_cpu_config_scale_from_vid(self):
        """Senza scale, conversione da VID: la formula community
        (1206-vid)/8 produce valori POSITIVI per l'undervolt, incoerenti coi
        bounds reali della scale (bc250_limits.py: -50..0) → clampata a 0
        (curva stock, MAI overvolt)."""
        orch = self._make(dry_run=True)
        _, s = orch._clamp_cpu(3500, vid=1030)
        self.assertEqual(s, 0)  # (1206-1030)/8 = 22 → clamp [−50, 0] → 0

    def test_apply_cpu_config_scale_bounds_never_positive(self):
        """Bounds scale VERIFICATI nel sorgente community (scale_min=-50,
        scale_max=0; bc250_detect.smu_apply RIFIUTA scale>0): una scale
        positiva chiederebbe un overvolt → mai ammessa; i valori validi
        negativi passano; sotto -50 → clamp a -50."""
        orch = self._make(dry_run=True)
        # positiva → 0 (mai overvolt)
        _, s = orch._clamp_cpu(3500, scale=50)
        self.assertEqual(s, 0)
        _, s2 = orch._clamp_cpu(3500, scale=10)
        self.assertEqual(s2, 0)
        # negativa valida → preservata
        _, s3 = orch._clamp_cpu(3500, scale=-20)
        self.assertEqual(s3, -20)
        # sotto il floor community → clamp a -50
        _, s4 = orch._clamp_cpu(3500, scale=-80)
        self.assertEqual(s4, -50)

    def test_phase_optimize_passes_cpu_target_vid(self):
        """_phase_optimize passa max_vid=undervolt_cpu_target_vid alla
        ricerca CPU (es. 1000): senza un target sotto la misura stock la
        scale non va mai negativa e l'"undervolt" è solo downclock."""
        from unittest import mock
        orch = self._make(dry_run=True)
        orch.config.undervolt_cpu_target_vid = 1000
        result = {
            "v_f_points": [{"freq": 3500, "vid": 999}],
            "best_efficiency": {"freq": 3500, "vid": 999},
            "source": "mock",
        }
        with mock.patch.object(orch.uv_cpu, "optimize",
                               return_value=result) as spy:
            orch._phase_optimize()
        spy.assert_called_once_with(
            max_freq=orch.config.cpu_freq_max, max_vid=1000)

    def test_phase_apply_passes_gpu_freq_max_to_write_config(self):
        """_phase_apply deve passare a write_config il cap della config
        (safety.gpu_freq_max): senza, a ogni apply il range tornerebbe a
        2230 e il punto 2000@900 (110°C sotto FurMark su questa macchina)
        verrebbe riscritto."""
        from unittest import mock
        orch = self._make(dry_run=False)
        orch.config = BUOConfig({"safety": {"gpu_freq_max": 1500}})
        points = [
            {"freq": 1200, "voltage": 800},
            {"freq": 1500, "voltage": 900},
            {"freq": 2000, "voltage": 1000},
        ]
        orch.checkpoint.set_phase("optimize", {
            "undervolt_gpu": {"safe_points": points},
            "undervolt_cpu": {},
            "overclock_cpu": {},
        })
        with mock.patch.object(orch.governor, "write_config",
                               return_value=True) as spy:
            r = orch._phase_apply()
        spy.assert_called_once_with(points, max_freq=1500)
        self.assertTrue(r["governor_config"])


class TestAbortTerminal(unittest.TestCase):
    """Bug sul campo 03/09: gli ABORT (safety/errore) sono TERMINALI.

    Un abort azzera lo stato di run del checkpoint (current_phase → init),
    così né `buo unleash` né `buo resume` proseguono una run appena
    fallita (ri-eseguivano la fase abortita → circolare). La run interrotta
    da REBOOT (il processo muore senza passare dagli handler di abort)
    NON viene azzerata e resta riprendibile da `buo resume`.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def _make(self):
        hw = MockHardware(seed=42)
        hw.state.is_acpi_fixed = True
        cfg = BUOConfig()
        cfg.validation_stress_duration = 0
        cfg.benchmark_enabled = False
        return Orchestrator(config=cfg, mock=True, dry_run=False,
                            mock_hardware=hw)

    def _seed_interrupted_run(self, orch, phase="validate"):
        """Checkpoint di un run interrotto a metà SENZA abort (es. reboot
        dopo apply): fasi fino a `phase` completate, corrente = `phase`."""
        from buo.constants import PHASES
        for p in PHASES[:PHASES.index(phase)]:
            orch.checkpoint.set_phase(p, {}, completed=True)
        orch.checkpoint.set_current_phase(phase)

    def _run_recording_phases(self, orch):
        """orch.run() registrando le fasi eseguite in ordine."""
        from unittest import mock
        seen = []
        orig = orch._execute_phase

        def spy(phase):
            seen.append(phase)
            return orig(phase)

        with mock.patch.object(orch, "_execute_phase", side_effect=spy):
            rc = orch.run()
        return rc, seen

    def _abort_at_validate(self, exc, expected_rc):
        """Run completa (mock) che abortisce a validate."""
        from unittest import mock
        orch = self._make()
        orch.checkpoint.clear()
        with mock.patch.object(orch, "_phase_validate", side_effect=exc), \
             mock.patch.object(orch.rollback, "rollback") as rb:
            rc = orch.run()
        self.assertEqual(rc, expected_rc)
        rb.assert_called_once()
        return orch

    def test_safety_abort_marks_run_terminal(self):
        """Abort di safety → stato di run azzerato (current_phase init,
        ledger e contatore reboot vuoti): il run successivo riparte da
        init, NON dalla fase abortita."""
        from buo.constants import EXIT_SAFETY_VIOLATION
        from buo.exceptions import SafetyViolation
        orch = self._abort_at_validate(SafetyViolation("CPU 90°C"),
                                       EXIT_SAFETY_VIOLATION)
        self.assertEqual(orch.checkpoint.get_current_phase(), "init",
                         "l'abort deve azzerare current_phase")
        self.assertEqual(orch.checkpoint.get("applied_steps", []), [],
                         "l'abort deve azzerare il ledger")
        self.assertEqual(orch.checkpoint.get_reboot_count(), 0,
                         "l'abort deve azzerare il contatore reboot")

        orch2 = self._make()  # nuovo processo: stesso stato su disco
        rc, seen = self._run_recording_phases(orch2)
        self.assertEqual(rc, 0)
        self.assertEqual(seen[0], "init",
                         "il run post-abort deve partire da init")

    def test_phase_error_marks_run_terminal(self):
        """Errore di fase → come l'abort di safety: terminale, il run
        successivo riparte da init."""
        from buo.constants import EXIT_ERROR
        orch = self._abort_at_validate(RuntimeError("fix fallito"),
                                       EXIT_ERROR)
        self.assertEqual(orch.checkpoint.get_current_phase(), "init")
        orch2 = self._make()
        rc, seen = self._run_recording_phases(orch2)
        self.assertEqual(rc, 0)
        self.assertEqual(seen[0], "init",
                         "il run post-errore deve partire da init")

    def test_resume_after_abort_does_not_resume_failed_run(self):
        """`buo resume` (run() senza argomenti) dopo un abort NON riprende
        la run fallita: lo stato è terminale → parte da init."""
        from buo.constants import EXIT_SAFETY_VIOLATION
        from buo.exceptions import SafetyViolation
        self._abort_at_validate(SafetyViolation("CPU 90°C"),
                                EXIT_SAFETY_VIOLATION)
        orch2 = self._make()
        rc, seen = self._run_recording_phases(orch2)
        self.assertEqual(rc, 0)
        self.assertEqual(seen[0], "init",
                         "il resume post-abort deve partire da init")

    def test_resume_after_reboot_interruption_resumes_from_phase(self):
        """REGRESSIONE da non rompere: una run interrotta da REBOOT
        (checkpoint con fase intermedia, NESSUN abort) resta riprendibile
        da `buo resume` dalla fase interrotta — init NON viene rieseguito."""
        orch = self._make()
        orch.checkpoint.clear()
        self._seed_interrupted_run(orch, "validate")
        rc, seen = self._run_recording_phases(orch)
        self.assertEqual(rc, 0)
        self.assertEqual(seen[0], "validate",
                         "il resume deve ripartire dalla fase interrotta")
        self.assertNotIn("init", seen)

    def test_abort_restores_applied_cpu_config_to_stock(self):
        """Bug di sicurezza 03/09: la config CPU applicata allo SMU
        volatile durante il run (qui: mock optimize applica 3700 +
        is_overclocked) deve essere RIPRISTINATA a stock dall'abort —
        la macchina non resta MAI con un OC/UV non validato applicato
        (il rollback filtra sul ledger: cpu_overclock deve esserci)."""
        from unittest import mock
        from buo.constants import EXIT_SAFETY_VIOLATION
        from buo.exceptions import SafetyViolation

        orch = self._make()
        orch.checkpoint.clear()
        # abort di safety a validate: apply/optimize hanno GIÀ applicato
        # la config CPU (mock: freq 3700, is_overclocked True)
        with mock.patch.object(orch, "_phase_validate",
                               side_effect=SafetyViolation("CPU 90°C")):
            rc = orch.run()
        self.assertEqual(rc, EXIT_SAFETY_VIOLATION)
        # rollback (filtrato sul ledger) deve aver incluso cpu_overclock
        # (config tracciata all'apply) → SMU tornato a stock
        self.assertEqual(orch.hardware.state.cpu_freq, 3500,
                         "l'abort deve riportare la CPU a stock (3500)")
        self.assertFalse(orch.hardware.state.is_overclocked,
                         "l'abort non deve lasciare un OC non validato "
                         "applicato allo SMU")


if __name__ == "__main__":
    unittest.main()
