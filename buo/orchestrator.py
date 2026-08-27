#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
BC-250 Ultimate Orchestrator — motore principale (macchina a stati).

Esegue la sequenza completa `unleash`:

    init → pre_audit → unlock → fix → optimize → apply → validate → complete

con checkpoint dopo ogni fase, safety monitor sempre attivo, rollback a
cascata su fallimento e ripresa dopo reboot (buo recover).

Il design completo (studio + progettazione dalla chat) è implementato
nei moduli: audit, unlock, fix, optimize, validate, safety, state,
benchmark, report, models.
"""

import sys
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from .audit.hardware import HardwareAudit
from .audit.problems import ProblemDetector
from .benchmark.runner import BenchmarkRunner
from .config import BUOConfig
from .constants import (EXIT_ERROR, EXIT_REBOOT, EXIT_SAFETY_VIOLATION,
                        EXIT_SUCCESS, LIMITS, PHASES, ROLLBACK_ORDER,
                        STATE_DIR)
from .exceptions import ConfigurationError, SafetyViolation
from .fix.ace import ACEComputeFix
from .fix.acpi import ACPIFix
from .fix.fan import FanControl
from .fix.gtt import GTTTuning
from .fix.iommu import IOMMUFix
from .fix.tlb import TLBKernelFix
from .fix.vram import VRAMConfig
from .optimize.cpu import CPUUndervoltOptimizer
from .optimize.governor import GovernorWrapper
from .optimize.gpu import GPUUndervoltOptimizer
from .optimize.overclock import OverclockOptimizer
from .report.generator import ReportGenerator
from .safety.monitor import SafetyMonitor
from .state.checkpoint import CheckpointManager
from .state.recovery import RecoveryManager
from .state.rollback import RollbackManager
from .unlock.cpu import CPUUnlock
from .unlock.dxe import DXECoreUnlock
from .unlock.gpu import GPU40CUUnlock
from .unlock.health import CUHealthTest
from .unlock.mask import CUMask
from .utils.logging import LoggerMixin, get_logger, setup_logging
from .utils.mock import MockHardware
from .validate.stress import StressTest
from .validate.verify import FixVerifier


class Orchestrator(LoggerMixin):
    """Macchina a stati principale di BUO."""

    def __init__(self, config: Optional[BUOConfig] = None,
                 mock: bool = False,
                 dry_run: bool = False,
                 interactive: bool = False,
                 mock_hardware: Optional[MockHardware] = None,
                 log_level: str = "INFO"):
        setup_logging(level=log_level)

        self.config = config or BUOConfig.load()
        self.mock = mock
        self.dry_run = dry_run
        self.interactive = interactive

        # Hardware (mock o reale)
        self.hardware = mock_hardware or (MockHardware() if mock else None)

        # Stato e checkpoint
        self.checkpoint = CheckpointManager()
        self.state = self.checkpoint
        self.rollback = RollbackManager(mock=mock, hardware=self.hardware)

        # Safety
        self.safety_monitor: Optional[SafetyMonitor] = None
        self.safety_violation = False
        self.safety_reason = ""

        # Risultati (popolati durante l'esecuzione)
        self.results: Dict[str, Any] = {
            "before": {}, "after": {}, "problems": [],
            "fixes": {}, "benchmarks": {}, "applied_fixes": [], "notes": [],
        }

        # Moduli di business
        self._init_modules()
        self._register_rollback_handlers()

        self.logger.info(
            "Orchestrator inizializzato (mock=%s, dry_run=%s, interactive=%s)",
            mock, dry_run, interactive)

    # ================================================================== #
    # INIT
    # ================================================================== #

    def _init_modules(self) -> None:
        # PRINCIPIO: in dry-run NESSUN modulo può toccare l'hardware reale.
        # Tutti i moduli che scrivono usano la modalità simulata se
        # mock=True OPPURE dry_run=True, con un MockHardware di default
        # (così nessun ramo mock accede mai a PCI/SMU reali).
        eff_mock = self.mock or self.dry_run
        if eff_mock and self.hardware is None:
            self.hardware = MockHardware(seed=1)
        hw = self.hardware if eff_mock else None

        # L'audit resta REALE anche in dry-run: è sola lettura e dà dati veri
        self.audit = HardwareAudit(mock=self.mock, mock_hardware=self.hardware)
        self.detector = ProblemDetector(mock=self.mock, mock_hardware=self.hardware)

        self.cpu_unlock = CPUUnlock(mock=eff_mock, mock_hardware=hw,
                                    use_wrapper=not eff_mock)
        self.dxe_unlock = DXECoreUnlock(mock=eff_mock, mock_hardware=hw)
        self.gpu_unlock = GPU40CUUnlock(mock=eff_mock, mock_hardware=hw,
                                        use_wrapper=not eff_mock)
        self.health_test = CUHealthTest(mock=eff_mock, mock_hardware=hw)
        self.cu_mask = CUMask(mock=eff_mock, mock_hardware=hw)

        self.fix_tlb = TLBKernelFix(mock=eff_mock, mock_hardware=hw)
        self.fix_ace = ACEComputeFix(mock=eff_mock, mock_hardware=hw)
        self.fix_iommu = IOMMUFix(mock=eff_mock, mock_hardware=hw)
        self.fix_acpi = ACPIFix(mock=eff_mock, mock_hardware=hw)
        self.fix_vram = VRAMConfig(mock=eff_mock, mock_hardware=hw)
        self.fix_gtt = GTTTuning(mock=eff_mock, mock_hardware=hw)
        self.fix_fan = FanControl(mock=eff_mock, mock_hardware=hw)

        self.uv_cpu = CPUUndervoltOptimizer(mock=eff_mock, mock_hardware=hw,
                                            use_wrapper=not eff_mock)
        self.uv_gpu = GPUUndervoltOptimizer(mock=eff_mock, mock_hardware=hw)
        self.oc = OverclockOptimizer(mock=eff_mock, mock_hardware=hw)
        self.governor = GovernorWrapper(mock=eff_mock, mock_hardware=hw)

        self.benchmark = BenchmarkRunner(mock=self.mock, mock_hardware=self.hardware)
        self.stress = StressTest(mock=self.mock, mock_hardware=self.hardware)
        self.verifier = FixVerifier(mock=self.mock, mock_hardware=self.hardware)
        self.report = ReportGenerator()

    def _register_rollback_handlers(self) -> None:
        """Registra i rollback di ogni livello (ordine dal design)."""
        handlers: Dict[str, Callable[[], bool]] = {
            "cpu_overclock": lambda: self._rollback_cpu_overclock(),
            "gpu_governor": lambda: self.governor.stop() or True,
            "gpu_40cu": lambda: self.gpu_unlock.rollback(),
            "gpu_mask": lambda: self.cu_mask.rollback(),
            "cpu_core_unlock": lambda: self.cpu_unlock.rollback(),
            "acpi_fix": lambda: self.fix_acpi.rollback(),
            "tlb_fix": lambda: self.fix_tlb.rollback(),
            "ace_fix": lambda: self.fix_ace.rollback(),
            "iommu": lambda: self.fix_iommu.rollback(),
            "vram_config": lambda: self.fix_vram.rollback(),
            "gtt_tuning": lambda: self.fix_gtt.rollback(),
            "fan_control": lambda: self.fix_fan.rollback(),
        }
        for name, handler in handlers.items():
            self.rollback.register(name, handler)

    def _rollback_cpu_overclock(self) -> bool:
        """Rimuove l'overclock CPU (servizio systemd / valori stock)."""
        if self.mock and self.hardware is not None:
            self.hardware.set_cpu_freq(3500)
            self.hardware.set_cpu_vid(1206)
            self.hardware.state.is_overclocked = False
            return True
        from .constants import SCRIPT_APPLY
        from .utils.shell import run_command
        if SCRIPT_APPLY:
            rc, _, _ = run_command([SCRIPT_APPLY, "--uninstall"],
                                   sudo=True, check=False)
            return rc == 0
        return True

    # ================================================================== #
    # RUN — macchina a stati
    # ================================================================== #

    def run(self, start_phase: Optional[str] = None,
            stop_after: Optional[str] = None) -> int:
        """
        Esegue il workflow (o un sottoinsieme di fasi).

        Args:
            start_phase: fase da cui partire (default: dal checkpoint)
            stop_after: fermarsi DOPO questa fase (es. comandi fase
                        standalone: probe, undervolt, apply)
        """
        self.logger.info("🚀 Avvio ottimizzazione (BUO v1.0.0)")

        # Esecuzione parziale (comando fase standalone)?
        self._partial_run = start_phase is not None

        current = start_phase or self.checkpoint.get_current_phase()
        if current not in PHASES:
            current = "init"

        try:
            while current != "complete":
                if self.safety_violation:
                    self._handle_safety_violation()
                    return EXIT_SAFETY_VIOLATION

                if self.interactive and not self._confirm_phase(current):
                    self.logger.info("⏹️ Interrotto dall'utente")
                    return EXIT_SUCCESS

                self.logger.info("📍 Fase: %s", current)
                if self.dry_run:
                    self.logger.info("   [DRY-RUN] nessuna modifica reale")

                try:
                    data = self._execute_phase(current)
                    self.checkpoint.set_phase(current, data, completed=True)
                    if stop_after and current == stop_after:
                        current = "complete"
                    else:
                        current = self._next_phase(current)
                        self.checkpoint.set_current_phase(current)
                except SafetyViolation as e:
                    self.logger.error("🚨 SAFETY VIOLATION: %s", e)
                    self.safety_reason = str(e)
                    self._handle_safety_violation()
                    return EXIT_SAFETY_VIOLATION
                except Exception as e:
                    self.logger.error("❌ Errore in fase %s: %s", current, e)
                    self._handle_error(current, str(e))
                    return EXIT_ERROR

            # Completato
            self._finalize()
            return EXIT_SUCCESS

        except KeyboardInterrupt:
            self.logger.info("⏹️ Interrotto dall'utente")
            return EXIT_SUCCESS
        except Exception as e:
            self.logger.error("❌ Errore fatale: %s", e)
            import traceback
            self.logger.debug(traceback.format_exc())
            return EXIT_ERROR

    def _next_phase(self, current: str) -> str:
        idx = PHASES.index(current)
        return PHASES[idx + 1] if idx + 1 < len(PHASES) else "complete"

    def _confirm_phase(self, phase: str) -> bool:
        try:
            resp = input(f"   Eseguire fase '{phase}'? [Y/n] ").strip().lower()
            return resp in ("", "y", "yes")
        except EOFError:
            return True

    # ================================================================== #
    # PHASES
    # ================================================================== #

    def _execute_phase(self, phase: str) -> Dict[str, Any]:
        handlers = {
            "init": self._phase_init,
            "pre_audit": self._phase_pre_audit,
            "unlock": self._phase_unlock,
            "fix": self._phase_fix,
            "optimize": self._phase_optimize,
            "apply": self._phase_apply,
            "validate": self._phase_validate,
        }
        handler = handlers.get(phase)
        if handler is None:
            raise ValueError(f"Fase sconosciuta: {phase}")
        return handler()

    def _phase_init(self) -> Dict[str, Any]:
        """Inizializzazione: preflight + auto-download deps + safety monitor."""
        self.logger.info("🔧 Inizializzazione...")

        if not self.mock and sys.platform != "linux":
            raise SafetyViolation("BUO funziona solo su Linux")

        # PRIMA di qualsiasi modifica: verifica di sanità (solo hardware reale)
        if not self.mock:
            self._preflight_checks()

        # Scarica automaticamente i tool della community mancanti
        # (solo hardware reale; in mock/dry-run non si scarica nulla)
        self._ensure_dependencies()

        # Avvia il safety monitor (sempre attivo durante l'esecuzione)
        if not self.dry_run and not self.mock:
            self.safety_monitor = SafetyMonitor(
                hardware=self.hardware,
                abort_callback=self._safety_abort,
                vram_estimation=self.config.vram_estimation_enabled,
            )
            self.safety_monitor.start()
            self.logger.info("🛡️ Safety monitor avviato (sampling 0.5s)")

        return {"initialized": True, "mock": self.mock, "dry_run": self.dry_run}

    def _preflight_checks(self) -> None:
        """
        VERIFICA DI SANITÀ (design: "mai toccare l'hardware senza
        verifiche preliminari"). Solo lettura — nessuna modifica.

        Blocca l'esecuzione se:
            • kernel < 6.11
            • Mesa < 25.1
            • temperature attuali pericolosamente vicine ai limiti
        """
        self.logger.info("🔎 Verifica di sanità pre-operativa...")
        audit = self.audit.run()

        kernel = audit.get("kernel", {})
        if not kernel.get("meets_minimum", True):
            raise SafetyViolation(
                f"Kernel {kernel.get('release')} < 6.11: la BC-250 richiede "
                "kernel ≥ 6.11 — aggiorna il sistema prima di procedere"
            )

        mesa = audit.get("mesa", {})
        if mesa.get("version") and not mesa.get("meets_minimum", True):
            raise SafetyViolation(
                f"Mesa {mesa.get('version')} < 25.1: aggiorna Mesa prima di "
                "procedere (la BC-250 non funziona sotto 25.1)"
            )

        temps = audit.get("temps", {})
        cpu_t = temps.get("cpu_temp")
        if cpu_t and cpu_t > LIMITS.cpu.temp_critical - 10:  # > 90°C pre-operativo
            raise SafetyViolation(
                f"Temperatura CPU attuale {cpu_t:.1f}°C troppo alta per iniziare"
            )
        gpu_t = temps.get("gpu_temp")
        if gpu_t and gpu_t > LIMITS.gpu.temp_critical - 15:  # > 85°C
            raise SafetyViolation(
                f"Temperatura GPU attuale {gpu_t:.1f}°C troppo alta per iniziare"
            )

        if cpu_t and cpu_t > 60:
            self.logger.warning("⚠️ CPU a %.1f°C: verifica il raffreddamento",
                                cpu_t)

        self.logger.info("✅ Verifica di sanità superata")

    def _ensure_dependencies(self) -> None:
        """
        Scarica automaticamente i tool della community mancanti.

        "Un solo comando, tutto automatico": se i tool esterni
        (bc250_smu_oc, bc250-40cu-unlock, bc250-acpi-fix) non sono
        installati, BUO li clona e li installa da solo PRIMA di iniziare.

        - mock/dry-run: nessun download (mai)
        - config deps.auto_install=false: istruzioni manuali
        - governor: mai installato in automatico (servizio distro-specifico)
        - download fallito → ConfigurationError (fail-closed)
        """
        if self.mock or self.dry_run:
            self.logger.info(
                "Dependencies: [%s] nessun download automatico",
                "MOCK" if self.mock else "DRY-RUN")
            return

        if not self.config.deps_auto_install:
            self.logger.info(
                "Auto-install deps disabilitato — se mancano i tool, "
                "esegui: sudo buo install-deps")
            return

        from .install.deps import DependencyManager
        manager = DependencyManager()

        missing = [n for n, s in manager.check().items()
                   if not s.get("present") and s.get("type") != "instruct"]
        if not missing:
            self.logger.info("✅ Tool della community presenti")
            return

        self.logger.info(
            "📥 Tool della community mancanti (%s) — download automatico...",
            ", ".join(missing))

        if self.interactive:
            try:
                resp = input("   Procedere con il download? [Y/n] ").strip().lower()
                if resp not in ("", "y", "yes"):
                    self.logger.info("Download annullato dall'utente")
                    return
            except EOFError:
                pass

        result = manager.install()
        if "_error" in result:
            raise ConfigurationError(
                f"Download automatico non possibile: {result['_error']}"
            )
        failed = [n for n, s in result.items() if s.get("status") == "failed"]
        if failed:
            raise ConfigurationError(
                "Impossibile scaricare i tool necessari: "
                f"{', '.join(failed)}. Controlla la connessione/`git` "
                "oppure esegui manualmente: sudo buo install-deps"
            )
        self.logger.info("✅ Tool scaricati e installati automaticamente")

    def _phase_pre_audit(self) -> Dict[str, Any]:
        """FASE 0 — PRE-AUDIT: discovery, problemi, benchmark before."""
        self.logger.info("🔍 PRE-AUDIT: analisi dello stato attuale")

        audit = self.audit.run()
        problems = self.detector.detect(audit)
        self.results["before"] = audit
        self.results["problems"] = problems

        for line in self.detector.summary(problems).splitlines():
            self.logger.warning(line)

        if self.config.benchmark_enabled:
            self.logger.info("📊 Benchmark BEFORE...")
            self.results["benchmarks"]["before"] = self.benchmark.run_all(
                gpu_duration=self.config.benchmark_gpu_duration,
                cpu_duration=self.config.benchmark_cpu_duration,
                compute_duration=self.config.benchmark_compute_duration,
            )

        return {"audit": audit, "problems": problems}

    def _phase_unlock(self) -> Dict[str, Any]:
        """FASE 1 — SBLOCCHI: CPU 8-core, GPU 40-CU, health test, maschera."""
        self.logger.info("🔓 SBLOCCHI: CPU + GPU")
        results: Dict[str, Any] = {}

        # 1. CPU 8-core (volatile)
        if self.config.probe_cpu_unlock:
            try:
                cpu = self.cpu_unlock.unlock()
                results["cpu"] = cpu
                if cpu.get("changed", True):
                    self.results["applied_fixes"].append("cpu_core_unlock")
                    if cpu.get("needs_reboot"):
                        self._schedule_reboot("CPU unlock — reboot richiesto")
                else:
                    self.logger.info("CPU: core già sbloccati (BIOS/DXE) — salto")
            except Exception as e:
                self.logger.warning("Unlock CPU non eseguito: %s", e)
                results["cpu"] = {"unlocked": False, "error": str(e)}

        # 2. GPU 40-CU
        if self.config.probe_gpu_unlock:
            try:
                was_enabled = self.gpu_unlock.is_enabled()
                gpu = self.gpu_unlock.apply()
                results["gpu"] = gpu
                if not was_enabled and gpu.get("applied"):
                    self.results["applied_fixes"].append("gpu_40cu")
                    if gpu.get("needs_reboot"):
                        self._schedule_reboot("GPU 40-CU — reboot richiesto")
                elif was_enabled:
                    self.logger.info("GPU: 40-CU già attive — salto")
            except Exception as e:
                self.logger.warning("Unlock GPU non eseguito: %s", e)
                results["gpu"] = {"applied": False, "error": str(e)}

        # 3. Health test CU (se abilitato)
        if self.config.probe_health_test:
            try:
                health = self.health_test.run()
                results["health"] = health
                defective = health.get("defective", [])
                if defective:
                    results["mask"] = self.cu_mask.apply(defective_cu=defective)
                    self.results["applied_fixes"].append("gpu_mask")
            except Exception as e:
                self.logger.warning("Health test non eseguito: %s", e)
                results["health"] = {"error": str(e)}

        return results

    def _phase_fix(self) -> Dict[str, Any]:
        """FASE 1b — FIX: TLB, ACE, IOMMU, ACPI, VRAM, GTT, ventole."""
        self.logger.info("🔧 FIX di sistema")
        results: Dict[str, Any] = {}

        fixers = [
            ("iommu", self.fix_iommu, self.config.fix_iommu),
            ("acpi", self.fix_acpi, self.config.fix_acpi),
            ("tlb", self.fix_tlb, self.config.fix_tlb),
            ("ace", self.fix_ace, self.config.fix_ace),
            ("vram", self.fix_vram, self.config.fix_vram),
            ("gtt", self.fix_gtt, self.config.fix_gtt),
            ("fan", self.fix_fan, self.config.fix_fan),
        ]

        for name, fixer, enabled in fixers:
            if not enabled:
                continue
            try:
                if self.dry_run:
                    results[name] = {"applied": True, "dry_run": True}
                else:
                    result = fixer.apply()
                    results[name] = result
                    if result.get("applied"):
                        self.results["applied_fixes"].append(name)
                        if result.get("needs_reboot"):
                            self._schedule_reboot(f"{name} — reboot richiesto")
            except Exception as e:
                self.logger.warning("Fix %s non applicato: %s", name, e)
                results[name] = {"applied": False, "error": str(e)}

        return results

    def _phase_optimize(self) -> Dict[str, Any]:
        """FASE 2 — OTTIMIZZAZIONE: undervolt + overclock power-limited."""
        self.logger.info("⚡ OTTIMIZZAZIONE (undervolt → overclock)")
        results: Dict[str, Any] = {}

        # Il governor va fermato durante i test
        if not self.mock:
            self.governor.stop()

        # CPU undervolt
        uv_cpu = self.uv_cpu.optimize(
            start_freq=self.config.undervolt_cpu_start_freq,
            step=self.config.undervolt_cpu_step,
            max_freq=self.config.cpu_freq_max,
            test_duration=self.config.undervolt_cpu_test_duration,
        )
        results["undervolt_cpu"] = uv_cpu

        # GPU undervolt
        uv_gpu = self.uv_gpu.optimize(
            start_freq=self.config.undervolt_gpu_start_freq,
            test_duration=self.config.undervolt_gpu_test_duration,
        )
        results["undervolt_gpu"] = uv_gpu

        # Overclock power-limited
        if self.config.overclock_enable:
            oc_cpu = self.oc.optimize_cpu(
                uv_cpu.get("v_f_points", []),
                power_budget=self.config.overclock_power_budget,
            )
            oc_gpu = self.oc.optimize_gpu(
                uv_gpu.get("safe_points", []),
                power_budget=self.config.overclock_power_budget,
            )
            results["overclock_cpu"] = oc_cpu
            results["overclock_gpu"] = oc_gpu

        return results

    def _phase_apply(self) -> Dict[str, Any]:
        """Applica la configurazione finale (governor + overclock)."""
        self.logger.info("⚙️ Applicazione configurazione finale")
        results: Dict[str, Any] = {"applied": True}

        optimize_data = self.checkpoint.get_phase("optimize").get("data", {})
        safe_points = (optimize_data.get("undervolt_gpu", {})
                       .get("safe_points", []))
        oc_cpu = optimize_data.get("overclock_cpu", {})

        # Configura il governor con i safe-points trovati
        if safe_points:
            if self.dry_run:
                self.logger.info("Governor config: [DRY-RUN] simulata")
                results["governor_config"] = True
            else:
                ok = self.governor.write_config(safe_points)
                results["governor_config"] = ok
                if ok and not self.mock:
                    self.governor.restart()

        # Applica la config CPU (in produzione: bc250-apply --install)
        if oc_cpu.get("recommended_freq"):
            results["cpu_final"] = oc_cpu

        return results

    def _phase_validate(self) -> Dict[str, Any]:
        """FASE 3 — VALIDAZIONE: stress test, verifica fix, benchmark after."""
        self.logger.info("🔥 VALIDAZIONE")
        results: Dict[str, Any] = {}

        # Stress test (in dry-run viene solo simulato: niente 30 min reali)
        if self.dry_run:
            stress = {
                "passed": True, "simulated": True,
                "duration_minutes": self.config.validation_stress_duration,
                "cpu_temp_max": None, "gpu_temp_max": None,
                "power_max": None, "errors": 0,
            }
            self.logger.info("   [DRY-RUN] stress test simulato")
        else:
            stress = self.stress.run(
                duration_minutes=self.config.validation_stress_duration,
                power_budget=self.config.power_budget,
            )
        results["stress"] = stress

        # Verifica dei fix applicati
        verification = self.verifier.verify_all(self.results["applied_fixes"])
        results["fix_verification"] = verification
        self.results["fixes"] = verification

        # Benchmark after
        if self.config.benchmark_enabled and not self.dry_run:
            self.logger.info("📊 Benchmark AFTER...")
            self.results["benchmarks"]["after"] = self.benchmark.run_all(
                gpu_duration=self.config.benchmark_gpu_duration,
                cpu_duration=self.config.benchmark_cpu_duration,
                compute_duration=self.config.benchmark_compute_duration,
            )

        # Aggiorna l'audit "after"
        self.results["after"] = self.audit.run()

        return results

    # ================================================================== #
    # FINALIZE / ERROR / REBOOT
    # ================================================================== #

    def _finalize(self) -> None:
        """Genera il report finale e ferma il safety monitor."""
        if self.safety_monitor is not None:
            self.safety_monitor.stop()

        if self.dry_run:
            self.results["notes"].append(
                "MODALITÀ DRY-RUN: nessuna modifica reale è stata applicata. "
                "Questo report descrive cosa sarebbe successo."
            )

        self.report.generate(
            before=self.results["before"],
            after=self.results["after"],
            problems=self.results["problems"],
            fixes=self.results["fixes"],
            benchmarks=self.results["benchmarks"],
            applied_fixes=self.results["applied_fixes"],
            notes=self.results["notes"],
        )
        if getattr(self, "_partial_run", False):
            self.logger.info("✅ Fase/i richiesta/e completata/e")
        else:
            self.logger.info("✅ OTTIMIZZAZIONE COMPLETATA!")
        self.checkpoint.set_phase("complete", {"done": True}, completed=True)

    def _handle_safety_violation(self) -> None:
        self.logger.error("🛑 Esecuzione interrotta per safety violation")
        if self.safety_monitor is not None:
            self.safety_monitor.stop()
        if not self.dry_run:
            self.rollback.rollback(reason=self.safety_reason)
        self.results["notes"].append(
            f"Safety violation: {self.safety_reason} — rollback eseguito")

    def _handle_error(self, phase: str, error: str) -> None:
        self.logger.error("❌ Errore in fase %s: %s", phase, error)
        if self.safety_monitor is not None:
            self.safety_monitor.stop()
        if not self.dry_run:
            self.rollback.rollback(from_phase=None, reason=f"errore in {phase}: {error}")
        self.results["notes"].append(f"Errore in {phase}: {error}")

    def _schedule_reboot(self, reason: str) -> None:
        """Salva checkpoint e programma il reboot (auto-ripresa)."""
        self.checkpoint.increment_reboot_count()
        self.logger.info("♻️ Reboot programmato: %s", reason)
        if self.dry_run:
            return
        # In produzione: crea buo-resume.service e reboot
        from .state.reboot import RebootManager
        RebootManager().schedule(reason=reason, delay=5)
        sys.exit(EXIT_REBOOT)

    def _safety_abort(self, reason: str) -> None:
        self.safety_violation = True
        self.safety_reason = reason

    # ================================================================== #
    # COMANDI AUSILIARI
    # ================================================================== #

    def status(self) -> Dict[str, Any]:
        """Stato corrente (per `buo status`)."""
        info = self.hardware.get_system_info() if self.hardware else None
        return {
            "current_phase": self.checkpoint.get_current_phase(),
            "reboot_count": self.checkpoint.get_reboot_count(),
            "hardware": info,
            "applied_fixes": self.results["applied_fixes"],
        }

    def recovery_plan(self) -> Dict[str, Any]:
        """Piano di ripresa (per `buo recover`)."""
        manager = RecoveryManager(checkpoint=self.checkpoint,
                                  verify_callback=None)
        return manager.get_recovery_plan()
