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

    def test_apply_cpu_config_conf_keeps_operating_max_temperature(self):
        """Politica termica a due livelli (03/09): il max_temperature del
        conf SMU è il TARGET OPERATIVO applicato (LIMITS.cpu.temp_apply),
        NON segue l'HARD di abort (LIMITS.cpu.temp_max): la soglia
        applicata resta sotto l'abort così il run non viene bocciato dal
        carico sintetico."""
        from unittest import mock
        from buo.constants import LIMITS
        cfg = BUOConfig()
        orch = Orchestrator(config=cfg, mock=False, dry_run=False)
        w = mock.Mock()
        w.available = True
        w.apply.return_value = {"returncode": 0, "stderr": ""}
        with mock.patch("buo.unlock.wrappers.bc250_overclock."
                        "BC250ApplyWrapper", return_value=w):
            out = orch._apply_cpu_config(3500, scale=0)
        self.assertTrue(out["applied"])
        conf_path = w.apply.call_args.args[0]
        with open(conf_path, encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn(f"max_temperature = {LIMITS.cpu.temp_apply}", content)
        self.assertLess(LIMITS.cpu.temp_apply, LIMITS.cpu.temp_max)

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
        """_phase_optimize passa un max_vid NUMERICO esplicito (vince su
        "auto", compat file esistenti) e max_freq = min(cpu_freq_max,
        cpu_search_freq): la ricerca parte da 3500 stock (default), NON
        da cpu_freq_max 4000 — il punto trovato è la frequenza applicata
        e f-alta + deep-UV è zona di wedge/hang (design 3.2)."""
        from unittest import mock
        orch = self._make(dry_run=True)
        orch.config.undervolt_cpu_target_vid = 1000
        orch.config.undervolt_cpu_search_freq = 3500
        result = {
            "v_f_points": [{"freq": 3500, "vid": 999}],
            "best_efficiency": {"freq": 3500, "vid": 999},
            "source": "mock",
        }
        with mock.patch.object(orch.uv_cpu, "optimize",
                               return_value=result) as spy:
            orch._phase_optimize()
        spy.assert_called_once_with(max_freq=3500, max_vid=1000)

    def test_phase_optimize_search_freq_capped_by_cpu_search_freq(self):
        """cpu_search_freq esplicito (3800) → la ricerca parte da
        min(cpu_freq_max 4000, 3800) = 3800, non dal soffitto."""
        from unittest import mock
        orch = self._make(dry_run=True)
        orch.config.undervolt_cpu_target_vid = 1000
        orch.config.undervolt_cpu_search_freq = 3800
        result = {"v_f_points": [{"freq": 3500, "vid": 999}],
                  "best_efficiency": {"freq": 3500, "vid": 999},
                  "source": "mock"}
        with mock.patch.object(orch.uv_cpu, "optimize",
                               return_value=result) as spy:
            orch._phase_optimize()
        spy.assert_called_once_with(max_freq=3800, max_vid=1000)

    def test_auto_target_uses_measured_vid_minus_75(self):
        """cpu_target_vid=auto (default): misura stock 1074 → target
        999 (1074−75), primo tentativo alla ricerca (design 3.1)."""
        from unittest import mock
        orch = self._make(dry_run=True)
        orch.hardware.state.cpu_vid = 1074
        result = {"v_f_points": [{"freq": 3500, "vid": 999}],
                  "best_efficiency": {"freq": 3500, "vid": 999},
                  "source": "mock"}
        with mock.patch.object(orch.uv_cpu, "optimize",
                               return_value=result) as spy:
            uv = orch._phase_optimize()["undervolt_cpu"]
        spy.assert_called_once_with(max_freq=3500, max_vid=999)
        self.assertEqual(uv["source"], "mock")

    def test_auto_target_clamped_at_edges(self):
        """Clamp del target auto a [900, 1250]: misura 850 → 900 (bordo
        basso, mai sotto il minimo sicuro); misura 1400 → 1250 (mai oltre
        il tetto di ricerca)."""
        from unittest import mock
        orch = self._make(dry_run=True)
        result = {"v_f_points": [{"freq": 3500, "vid": 999}],
                  "best_efficiency": {"freq": 3500, "vid": 999},
                  "source": "mock"}
        with mock.patch.object(orch.uv_cpu, "optimize",
                               return_value=result) as spy:
            orch.hardware.state.cpu_vid = 850
            orch._phase_optimize()
            self.assertEqual(spy.call_args.kwargs["max_vid"], 900)
            spy.reset_mock()
            orch.hardware.state.cpu_vid = 1400
            orch._phase_optimize()
            self.assertEqual(spy.call_args.kwargs["max_vid"], 1250)

    def test_auto_ladder_retries_plus_50_on_instability(self):
        """Target auto instabile (ConfigurationError) → retry +50 fino
        alla misura stock: 1074 → 999 fallisce, 1049 riesce."""
        from unittest import mock
        from buo.exceptions import ConfigurationError
        orch = self._make(dry_run=True)
        orch.hardware.state.cpu_vid = 1074
        result = {"v_f_points": [{"freq": 3500, "vid": 1049}],
                  "best_efficiency": {"freq": 3500, "vid": 1049},
                  "source": "mock"}
        with mock.patch.object(
                orch.uv_cpu, "optimize",
                side_effect=[ConfigurationError("instabile"), result]) as spy:
            orch._phase_optimize()
        self.assertEqual(spy.call_count, 2)
        self.assertEqual(spy.call_args_list[0].kwargs["max_vid"], 999)
        self.assertEqual(spy.call_args_list[1].kwargs["max_vid"], 1049)

    def test_auto_fallback_no_uv_when_ladder_exhausted(self):
        """Ladder esaurita (instabile fino alla misura stock) → fallback
        no-UV (curva stock, nessun punto applicato) con nota nel report:
        MAI abortire una run su macchina sana."""
        from unittest import mock
        from buo.exceptions import ConfigurationError
        orch = self._make(dry_run=True)
        orch.hardware.state.cpu_vid = 1074
        with mock.patch.object(
                orch.uv_cpu, "optimize",
                side_effect=ConfigurationError("instabile")) as spy:
            uv = orch._phase_optimize()["undervolt_cpu"]
        self.assertEqual(uv["source"], "no-uv")
        self.assertEqual(uv["v_f_points"], [])
        # tentativi: 999, 1049, 1074 (misura) — mai oltre
        vids = [c.kwargs["max_vid"] for c in spy.call_args_list]
        self.assertEqual(vids, [999, 1049, 1074])
        self.assertTrue(any("Undervolt CPU non trovato" in n
                            for n in orch.results["notes"]))

    def test_auto_static_fallback_when_measure_unavailable(self):
        """Misura VID non disponibile (None) → fallback statico 1000 +
        ladder (stessa robustezza, design §3.1)."""
        from unittest import mock
        orch = self._make(dry_run=True)
        result = {"v_f_points": [{"freq": 3500, "vid": 999}],
                  "best_efficiency": {"freq": 3500, "vid": 999},
                  "source": "mock"}
        with mock.patch.object(orch, "_read_stock_vid",
                               return_value=None), \
             mock.patch.object(orch.uv_cpu, "optimize",
                               return_value=result) as spy:
            orch._phase_optimize()
        spy.assert_called_once_with(max_freq=3500, max_vid=1000)

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


class TestSuggest40cuPersistence(unittest.TestCase):
    """Auto-persistenza 40-CU nei run NON interattivi (gap 04/09) +
    gate D9 (design POSTUNLOCK_VALIDATION): la persistenza gira SOLO su
    silicio validato — (a) validazione short appena passata,
    (b) results.tsv completo (per-WGP), (c) verdetto stable_short.

    Su macchina fresca un `sudo buo unleash` NON interattivo lasciava le
    40 CU volatili (al boot si tornava a 24): il ramo non interattivo si
    limitava all'avviso. Ora il run reale non interattivo AUTO-PERSISTE
    (gpu_unlock.persist()) SOLO se certificato; mock/dry-run → solo nota;
    un fallimento di persistenza è un warning MAI bloccante.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def _gpu(self):
        gpu = {"applied": True, "method": "runtime_umr"}
        return gpu, {"gpu": gpu}

    def test_non_interactive_auto_persists(self):
        """Run reale NON interattivo su silicio CERTIFICATO (results.tsv
        completo) → persist() chiamato + log di successo."""
        from unittest import mock
        orch = Orchestrator(config=BUOConfig(), mock=False, dry_run=False,
                            interactive=False)
        gpu, results = self._gpu()
        fake = mock.Mock()
        fake.persist.return_value = {"persisted": True, "note": "ok"}
        orch.gpu_unlock = fake
        with mock.patch.object(orch, "_gpu_validation_needed",
                               return_value="certified"), \
             self.assertLogs("buo.Orchestrator", level="INFO") as logs:
            orch._suggest_40cu_persistence(results, gpu)
        fake.persist.assert_called_once_with()
        self.assertTrue(results["gpu"]["persistence"]["persisted"])
        self.assertTrue(any("40 CU persistenti al boot" in m
                            for m in logs.output))

    def test_gate_skips_persistence_when_silicon_not_validated(self):
        """Gate D9: results.tsv assente + nessun verdetto + nessuna
        validazione appena passata → persistenza SALTATA con log, mai
        chiamato persist() (persistere 40-CU non certificate renderebbe
        permanente un difetto)."""
        from unittest import mock
        orch = Orchestrator(config=BUOConfig(), mock=False, dry_run=False,
                            interactive=False)
        gpu, results = self._gpu()
        fake = mock.Mock()
        orch.gpu_unlock = fake
        with mock.patch.object(orch, "_gpu_validation_needed",
                               return_value="needed"), \
             self.assertLogs("buo.Orchestrator", level="WARNING") as logs:
            orch._suggest_40cu_persistence(results, gpu)
        fake.persist.assert_not_called()
        self.assertEqual(results["gpu"]["persistence"].get("suggested"),
                         False)
        self.assertTrue(any("persistenza 40-CU SALTATA: silicio non "
                            "validato" in m for m in logs.output))

    def test_gate_skips_persistence_on_partial_tsv(self):
        """Gate D9: results.tsv PARZIALE (maratona per-WGP in corso) →
        persistenza saltata (non interferire)."""
        from unittest import mock
        orch = Orchestrator(config=BUOConfig(), mock=False, dry_run=False,
                            interactive=False)
        gpu, results = self._gpu()
        fake = mock.Mock()
        orch.gpu_unlock = fake
        with mock.patch.object(orch, "_gpu_validation_needed",
                               return_value="partial"):
            orch._suggest_40cu_persistence(results, gpu)
        fake.persist.assert_not_called()
        self.assertEqual(results["gpu"]["persistence"].get("suggested"),
                         False)

    def test_mock_only_notes_no_persist(self):
        """mock → NESSUNA chiamata reale: solo la nota di persistenza
        manuale (il ramo mock/dry-run resta invariato)."""
        from unittest import mock
        hw = MockHardware(seed=42)
        orch = Orchestrator(config=BUOConfig(), mock=True, dry_run=False,
                            mock_hardware=hw)
        gpu, results = self._gpu()
        fake = mock.Mock()
        orch.gpu_unlock = fake
        orch._suggest_40cu_persistence(results, gpu)
        fake.persist.assert_not_called()
        self.assertTrue(results["gpu"]["persistence"]["suggested"])

    def test_persist_failure_warns_never_blocks(self):
        """persist() fallito in un run non interattivo → warning con
        errore, MAI un'eccezione (la run non si blocca)."""
        from unittest import mock
        orch = Orchestrator(config=BUOConfig(), mock=False, dry_run=False,
                            interactive=False)
        gpu, results = self._gpu()
        fake = mock.Mock()
        fake.persist.return_value = {"persisted": False, "error": "boom"}
        orch.gpu_unlock = fake
        with mock.patch.object(orch, "_gpu_validation_needed",
                               return_value="certified"), \
             self.assertLogs("buo.Orchestrator", level="WARNING") as logs:
            orch._suggest_40cu_persistence(results, gpu)  # non deve sollevare
        self.assertFalse(results["gpu"]["persistence"]["persisted"])
        self.assertTrue(any("Persistenza non riuscita" in m and "boom" in m
                            for m in logs.output))


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
