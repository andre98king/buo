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
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .audit.hardware import HardwareAudit
from .audit.problems import ProblemDetector
from .benchmark.runner import BenchmarkRunner
from .config import BUOConfig
from .constants import (EXIT_ERROR, EXIT_REBOOT, EXIT_SAFETY_VIOLATION,
                        EXIT_SUCCESS, LIMITS, PHASES)
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
from .utils.logging import LoggerMixin, setup_logging
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

        # In dry-run anche i moduli READ-ONLY usano il mock: nessuna lettura
        # reale di /proc//sys né spawn di glxinfo/systemctl/modinfo.
        self.audit = HardwareAudit(mock=eff_mock, mock_hardware=hw)
        self.detector = ProblemDetector(mock=eff_mock, mock_hardware=hw)

        self.cpu_unlock = CPUUnlock(mock=eff_mock, mock_hardware=hw,
                                    use_wrapper=not eff_mock)
        self.dxe_unlock = DXECoreUnlock(mock=eff_mock, mock_hardware=hw)
        self.gpu_unlock = GPU40CUUnlock(mock=eff_mock, mock_hardware=hw,
                                        use_wrapper=not eff_mock)
        self.health_test = CUHealthTest(mock=eff_mock, mock_hardware=hw,
                                        max_reboots=self.config.probe_health_reboot_max)
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

        self.benchmark = BenchmarkRunner(mock=eff_mock, mock_hardware=hw)
        self.stress = StressTest(mock=eff_mock, mock_hardware=hw)
        self.verifier = FixVerifier(mock=eff_mock, mock_hardware=hw)
        self.report = ReportGenerator()

    def _register_rollback_handlers(self) -> None:
        """Registra i rollback di ogni livello (ordine dal design)."""
        handlers: Dict[str, Callable[[], bool]] = {
            "cpu_overclock": lambda: self._rollback_cpu_overclock(),
            "gpu_governor": lambda: self.governor.stop(),
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

        # Il dry-run è pura simulazione: NON tocca lo stato persistente.
        # Un run reale con checkpoint "complete" (run precedente finita)
        # riparte da init: la ripresa serve solo per esecuzioni interrotte.
        if self.dry_run:
            current = start_phase or "init"
        else:
            current = start_phase or self.checkpoint.get_current_phase()
        if current not in PHASES or current == "complete":
            current = "init"

        # Un run completo NUOVO (da init) azzera il ledger delle modifiche
        # e il contatore dei reboot (per-run): i fix ripartono da zero.
        # In caso di RIPRESA (fase intermedia) restano e impediscono loop.
        if current == "init" and not self.dry_run:
            self.checkpoint.set("applied_steps", [])
            self.checkpoint.set("reboot_count", 0)

        # RIPRESA (fase > init): ricarica i dati delle fasi già completate
        # dal checkpoint (before/problemi/fix applicati), altrimenti il
        # report finale risulta vuoto (bug #12).
        if not self.dry_run and current != "init":
            self._restore_results_from_checkpoint()

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
                    # Il checkpoint viene scritto SOLO nei run reali:
                    # il dry-run non deve inquinare lo stato persistente.
                    if not self.dry_run:
                        self.checkpoint.set_phase(current, data, completed=True)
                    if stop_after and current == stop_after:
                        current = "complete"
                    else:
                        current = self._next_phase(current)
                        if not self.dry_run:
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
                vram_alpha=self.config.vram_alpha,
                vram_beta=self.config.vram_beta,
                vram_tau=self.config.vram_tau,
                vram_warning_threshold=self.config.vram_warning_threshold,
                vram_critical_threshold=self.config.vram_critical_threshold,
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
        if cpu_t is None:
            self.logger.warning(
                "⚠️ Temperatura CPU non leggibile: il gate termico "
                "pre-operativo non può verificare il surriscaldamento "
                "(fail-soft — si procede)")
        elif cpu_t > LIMITS.cpu.temp_critical - 10:  # > 90°C pre-operativo
            raise SafetyViolation(
                f"Temperatura CPU attuale {cpu_t:.1f}°C troppo alta per iniziare"
            )
        gpu_t = temps.get("gpu_temp")
        if gpu_t is None:
            self.logger.warning(
                "⚠️ Temperatura GPU non leggibile: il gate termico "
                "pre-operativo non può verificare il surriscaldamento "
                "(fail-soft — si procede)")
        elif gpu_t > LIMITS.gpu.temp_critical - 15:  # > 85°C
            raise SafetyViolation(
                f"Temperatura GPU attuale {gpu_t:.1f}°C troppo alta per iniziare"
            )

        if cpu_t and cpu_t > 60:
            self.logger.warning("⚠️ CPU a %.1f°C: verifica il raffreddamento",
                                cpu_t)

        # Budget di potenza: la combo 8 core + 40 CU ha un picco noto.
        # Avviso NON bloccante: la decisione
        # finale resta al governor/limiti immutabili.
        self._check_power_budget()

        # Toolchain 40-CU: su ostree serve bc250-cu-live-manager + umr
        # (il kernel patch non funziona: /usr read-only). Avviso non
        # bloccante: l'unlock fallirà in modo pulito se mancano.
        self._check_40cu_toolchain(audit)

        self.logger.info("✅ Verifica di sanità superata")

    def _check_power_budget(self) -> None:
        """Avvisa se la combo 8 core + 40 CU può superare il PSU dichiarato.

        Numeri misurati sul campo:
            • gaming 200-220W (undervolt + cap 1500 MHz)
            • FurMark 250-320W senza cap
            • GPU idle 48W, PSU Metalfish 300W
        Se psu_wattage < 350 e sono abilitati entrambi gli unlock, l'utente
        deve sapere che serve il cap GPU 1500 MHz + undervolt.
        """
        if self.config.probe_cpu_unlock and self.config.probe_gpu_unlock:
            psu = self.config.psu_wattage
            if psu < 350:
                self.logger.warning(
                    "⚠️ POTENZA: PSU dichiarato %dW con 8 core + 40 CU "
                    "abilitati. Picco misurato: FurMark 250-320W SENZA cap. "
                    "Per restare sotto i %dW: undervolt + cap GPU 1500 MHz "
                    "(≈125-220W).", psu, psu)
            else:
                self.logger.info(
                    "✅ Potenza: PSU %dW sufficiente per 8 core + 40 CU "
                    "(comunque consigliato il cap GPU 1500 MHz per "
                    "l'efficienza).", psu)
        elif self.config.probe_gpu_unlock and self.config.psu_wattage < 300:
            self.logger.warning(
                "⚠️ POTENZA: PSU %dW con 40 CU: picco FurMark 250-320W "
                "senza cap — usare undervolt + cap GPU 1500 MHz.",
                self.config.psu_wattage)

    def _check_40cu_toolchain(self, audit: Dict[str, Any]) -> None:
        """Verifica i tool necessari per il percorso 40-CU della distro.

        Su ostree il kernel patch non funziona (/usr read-only): serve
        bc250-cu-live-manager + umr (runtime UMR). Su non-ostree serve
        bc250-enable-40cu.sh. Avviso non bloccante: se mancano, l'unlock
        fallirà in modo pulito (fail-closed) con istruzioni.
        """
        if not self.config.probe_gpu_unlock or self.mock:
            return
        if self.gpu_unlock is None or self.gpu_unlock.wrapper is None:
            self.logger.warning(
                "⚠️ Toolchain 40-CU non inizializzata: esegui "
                "`sudo buo install-deps` prima dell'unlock GPU.")
            return
        if not self.gpu_unlock.wrapper.available:
            self.logger.warning(
                "⚠️ Script 40-CU mancante (%s): esegui "
                "`sudo buo install-deps` (o installa manualmente il tool "
                "della community).", self.gpu_unlock.wrapper.script_path)
            return
        if self.gpu_unlock.is_ostree:
            import shutil
            if shutil.which("umr") is None:
                self.logger.warning(
                    "⚠️ `umr` non trovato: necessario per il runtime UMR "
                    "delle 40 CU su ostree. Installare con: "
                    "rpm-ostree install umr (poi reboot).")
            else:
                self.logger.info("✅ Toolchain 40-CU pronta (umr + live-manager)")
            # BUGS #24: l'unità systemd può sparire dopo un cambio deployment
            # (binario+config intatti) → la GPU torna silenziosamente a 24 CU.
            self._check_40cu_service_enabled()

    def _check_40cu_service_enabled(self) -> None:
        """BUGS #24: verifica che il servizio 40-CU sia ENABLED su ostree.

        Il servizio `bc250-cu-live-manager.service` può sparire dopo un cambio
        deployment ostree pur con binario `/usr/local/bin/bc250-cu-live-manager`
        e config intatti: la GPU torna silenziosamente a 24 CU. Avviso NON
        bloccante (fail-closed è compito dell'unlock, non di questo check):
        logga chiaramente il problema e la ricetta di recovery.
        """
        from .utils.shell import run_command
        unit = "bc250-cu-live-manager"
        try:
            rc, out, _ = run_command(
                ["systemctl", "is-enabled", unit], check=False)
        except Exception as e:  # mai bloccare l'unlock per questo check
            self.logger.warning(
                "⚠️ BUGS #24: verifica servizio 40-CU non riuscita (%s) — "
                "controlla manualmente `systemctl is-enabled %s`.", e, unit)
            return
        if rc == 0 and out.strip() == "enabled":
            self.logger.info("✅ Servizio 40-CU: %s.service abilitato", unit)
            return
        self.logger.warning(
            "⚠️ BUGS #24: %s.service mancante/disabilitato — le 40 CU "
            "torneranno a 24 CU al prossimo riavvio. Recovery (quirk: "
            "`install-service` da /usr/local/bin fallisce con 'same file' "
            "perché /usr/local è un symlink; eseguirlo da una copia in "
            "path NON-symlink):\n"
            "  sudo cp /usr/local/bin/bc250-cu-live-manager /tmp/\n"
            "  sudo /tmp/bc250-cu-live-manager --yes install-service\n"
            "  rm /tmp/bc250-cu-live-manager\n"
            "  sudo /usr/local/bin/bc250-cu-live-manager --yes apply-service",
            unit)

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
        # Il governor è un pacchetto distro-specifico: entra nel giro
        # automatico solo se autorizzato (default: sì — tutto automatico,
        # installato dal COPR/AUR ufficiali, niente installer di terze parti).
        if not self.config.deps_auto_install_governor:
            missing = [n for n in missing if n != "cyan-skillfish-governor"]
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

        # Installa SOLO ciò che manca (e che è abilitato): senza il filtro,
        # il governor disabilitato verrebbe installato comunque.
        result = manager.install(deps=missing)
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

        # Governor appena installato (COPR/AUR): scrivi la config di default
        # sicura (flat 1000mV, template vendored) e avvisa se serve un
        # reboot per l'attivazione (rpm-ostree layering su Bazzite).
        gov = result.get("cyan-skillfish-governor", {})
        if gov.get("status") == "ok":
            self._configure_installed_governor(gov)
        umr = result.get("umr", {})
        if umr.get("status") == "ok" and umr.get("needs_reboot"):
            self.logger.warning(
                "💾 umr installato: ATTIVO al prossimo reboot "
                "(necessario per le 40 CU via runtime UMR)")

    def _configure_installed_governor(self, gov: Dict[str, Any]) -> None:
        """Configura il governor appena installato (config di default).

        NON sovrascrive una config.toml esistente (rispetta le scelte
        dell'utente); su ostree il servizio parte al prossimo reboot.
        """
        from .constants import GOVERNOR_CONFIG
        cfg = Path(GOVERNOR_CONFIG)
        if cfg.exists():
            self.logger.info(
                "Governor: config.toml esistente — non sovrascritta")
        else:
            ok = self.governor.write_default_config()
            if ok:
                self.logger.info(
                    "Governor: config di default scritta (flat 1000mV)")
            else:
                self.logger.warning(
                    "Governor: impossibile scrivere la config di default")
        if gov.get("needs_reboot"):
            self.logger.warning(
                "♻️ Governor installato: sarà ATTIVO al prossimo reboot "
                "(rpm-ostree layering)")

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
        done = self._applied_steps()

        # 1. CPU 8-core (volatile) — con GATE ACPI fail-closed
        if self.config.probe_cpu_unlock and "cpu_core_unlock" not in done:
            if not self._acpi_gate_ok():
                self.logger.warning(
                    "⚠️ GATE ACPI: fix SSDT-PST/CST mancanti — senza di esse "
                    "l'unlock 8-core porta la BC-250 in BOOT LOOP.")
                proceed = False
                if self.interactive:
                    try:
                        resp = input(
                            "   Procedere comunque con l'unlock 8-core SENZA "
                            "fix ACPI? [y/N] ").strip().lower()
                        proceed = resp in ("y", "yes")
                    except EOFError:
                        proceed = False
                if not proceed:
                    self.logger.warning(
                        "⛔ CPU unlock SALTATO (fail-closed): applicare prima "
                        "la fix ACPI (e-tho/bc250-acpi-fix), poi rieseguire")
                    results["cpu"] = {
                        "unlocked": False,
                        "acpi_gate_blocked": True,
                    }
                    self.results["notes"].append(
                        "CPU unlock bloccato dal gate ACPI: fix SSDT-CST/PST "
                        "mancanti (e-tho/bc250-acpi-fix) — necessario prima "
                        "di sbloccare gli 8 core (boot loop)"
                    )
                else:
                    results["cpu"] = self._do_cpu_unlock()
            else:
                results["cpu"] = self._do_cpu_unlock()
        elif "cpu_core_unlock" in done:
            self.logger.info("CPU: unlock già eseguito (checkpoint) — salto")

        # 2. GPU 40-CU
        if self.config.probe_gpu_unlock and "gpu_40cu" not in done:
            try:
                was_enabled = self.gpu_unlock.is_enabled()
                gpu = self.gpu_unlock.apply()
                results["gpu"] = gpu
                if not was_enabled and gpu.get("applied"):
                    self.results["applied_fixes"].append("gpu_40cu")
                    self._mark_step("gpu_40cu")  # prima del reboot
                    if gpu.get("needs_reboot"):
                        self._schedule_reboot("GPU 40-CU — reboot richiesto")
                    # Runtime UMR = VOLATILE: suggerisci la persistenza
                    # (semi-automatico: avviso + conferma interattiva).
                    self._suggest_40cu_persistence(results, gpu)
                elif was_enabled:
                    self.logger.info("GPU: 40-CU già attive — salto")
            except Exception as e:
                self.logger.warning("Unlock GPU non eseguito: %s", e)
                results["gpu"] = {"applied": False, "error": str(e)}
        elif "gpu_40cu" in done:
            self.logger.info("GPU: unlock 40-CU già eseguito (checkpoint) — salto")

        # 3. Health test CU (se abilitato)
        if self.config.probe_health_test:
            try:
                health = self.health_test.run()
                results["health"] = health
                defective = health.get("defective", [])
                if defective and "gpu_mask" not in self._applied_steps():
                    results["mask"] = self.cu_mask.apply(defective_cu=defective)
                    self.results["applied_fixes"].append("gpu_mask")
                    self._mark_step("gpu_mask")
            except Exception as e:
                self.logger.warning("Health test non eseguito: %s", e)
                results["health"] = {"error": str(e)}

        return results

    def _do_cpu_unlock(self) -> Dict[str, Any]:
        """Esegue l'unlock CPU 8-core (volatile, richiede reboot)."""
        try:
            cpu = self.cpu_unlock.unlock()
            if cpu.get("changed", True):
                self.results["applied_fixes"].append("cpu_core_unlock")
                # MARCATO PRIMA del reboot: al resume non si ripete
                self._mark_step("cpu_core_unlock")
                if cpu.get("needs_reboot"):
                    self._schedule_reboot("CPU unlock — reboot richiesto")
            else:
                self.logger.info("CPU: core già sbloccati (BIOS/DXE) — salto")
            return cpu
        except Exception as e:
            self.logger.warning("Unlock CPU non eseguito: %s", e)
            return {"unlocked": False, "error": str(e)}

    def _acpi_gate_ok(self) -> bool:
        """True se le fix ACPI per gli 8 core risultano applicate.

        Fail-closed: senza le fix ACPI l'unlock 8-core manda la scheda in
        boot loop. Su ostree il metodo reale è l'initramfs concatenato e
        il segnale affidabile è la boot entry che punta al blob
        (fix_acpi.verify); sulle altre distro si leggono i nomi delle
        tabelle da /sys (SSDT-CST/PST) o la presenza del blob.
        """
        if self.mock and self.hardware is not None:
            return bool(self.hardware.state.is_acpi_fixed)
        if self.dry_run:
            return True  # simulazione: nessun blocco in dry-run
        if self.fix_acpi.distro.initramfs_tool == "ostree":
            return bool(self.fix_acpi.verify())
        acpi = self.audit.run().get("acpi", {})
        return bool((acpi.get("cst_present") and acpi.get("pst_present"))
                    or acpi.get("boot_fix_present"))

    def _suggest_40cu_persistence(self, results: Dict[str, Any],
                                  gpu: Dict[str, Any]) -> None:
        """Suggerisce (e opzionalmente esegue) la persistenza 40-CU.

        Il runtime UMR è VOLATILE: al reboot le 40 CU tornano a 24. La
        persistenza (install-service + write-service-table) è validata
        sul campo e stabile, ma richiede un reboot per l'attivazione.
        Semi-automatico: in modalità interattiva BUO chiede conferma,
        altrimenti si limita ad avvisare con le istruzioni.
        """
        if gpu.get("method") != "runtime_umr":
            return  # kernel patch: la persistenza è nel modulo, non serve
        self.logger.warning(
            "💾 40 CU attive ma VOLATILI: al prossimo reboot tornano a 24. "
            "Persistenza validata (install-service + write-service-table).")
        if not self.interactive or self.mock or self.dry_run:
            results["gpu"]["persistence"] = {
                "suggested": True,
                "note": "Per rendere persistenti le 40 CU al boot: esegui "
                        "la persistenza manuale (install-service + "
                        "write-service-table)",
            }
            self.results["notes"].append(
                "40 CU attive ma VOLATILI (runtime UMR): al reboot tornano "
                "a 24. Persistenza manuale disponibile (install-service + "
                "write-service-table)"
            )
            return
        try:
            resp = input(
                "   Rendere persistenti le 40 CU al boot? [y/N] "
            ).strip().lower()
        except EOFError:
            resp = "n"
        if resp not in ("y", "yes"):
            self.logger.info("Persistenza 40-CU annullata (resterà volatile)")
            results["gpu"]["persistence"] = {"suggested": True, "applied": False}
            return
        p = self.gpu_unlock.persist()
        results["gpu"]["persistence"] = p
        if p.get("persisted"):
            self.logger.info(
                "✅ 40 CU persistenti al boot (attive al prossimo reboot)")
        else:
            self.logger.warning("Persistenza non riuscita: %s",
                                p.get("error") or "errore sconosciuto")

    def _phase_fix(self) -> Dict[str, Any]:
        """FASE 1b — FIX: TLB, ACE, IOMMU, ACPI, VRAM, GTT, ventole.

        ANTI-LOOP: ogni fix è registrato nel ledger `applied_steps` PRIMA
        del reboot; al resume i fix già eseguiti (o già attivi via verify)
        vengono saltati, e per ogni rientro di fase scatta AL MASSIMO UN
        reboot. Senza questo, un fix che richiede reboot faceva rientrare
        la fase all'infinito (bug trovato sul campo: loop di riavvii).
        """
        self.logger.info("🔧 FIX di sistema")
        results: Dict[str, Any] = {}
        done = self._applied_steps()

        fixers = [
            # I nomi coincidono con i livelli del rollback (ROLLBACK_ORDER)
            ("iommu", self.fix_iommu, self.config.fix_iommu),
            ("acpi_fix", self.fix_acpi, self.config.fix_acpi),
            ("tlb_fix", self.fix_tlb, self.config.fix_tlb),
            ("ace_fix", self.fix_ace, self.config.fix_ace),
            ("vram_config", self.fix_vram, self.config.fix_vram),
            ("gtt_tuning", self.fix_gtt, self.config.fix_gtt),
            ("fan_control", self.fix_fan, self.config.fix_fan),
        ]

        for name, fixer, enabled in fixers:
            if not enabled:
                continue
            if name in done:
                self.logger.info("Fix %s: già eseguito (checkpoint) — salto",
                                 name)
                results[name] = {"applied": True, "skipped_checkpoint": True}
                continue
            try:
                if self.dry_run:
                    results[name] = {"applied": True, "dry_run": True}
                    continue
                if fixer.verify():
                    self.logger.info("Fix %s: già attivo — salto", name)
                    self._mark_step(name)
                    results[name] = {"applied": True, "skipped_verified": True}
                    continue
                result = fixer.apply()
                results[name] = result
                if result.get("applied"):
                    self.results["applied_fixes"].append(name)
                    # Registra PRIMA del reboot: il resume non deve ripeterlo
                    self._mark_step(name)
                    if result.get("needs_reboot"):
                        self._schedule_reboot(f"{name} — reboot richiesto")
                        break  # al massimo UN reboot per rientro di fase
            except Exception as e:
                self.logger.warning("Fix %s non applicato: %s", name, e)
                results[name] = {"applied": False, "error": str(e)}

        # Classificazione ONESTA dell'esito (applied/manual/failed): così
        # l'utente vede dal report quali fix sono attivi, quali richiedono
        # attenzione manuale e quali sono falliti. I no-op/manuali NON sono
        # errori (il contratto EXIT_SUCCESS resta invariato): sono solo resi
        # espliciti, non più nascosti dietro un generico WARNING.
        summary = {"applied": [], "manual": [], "failed": []}
        for name, entry in results.items():
            status = self._classify_fix(entry)
            note = self._fix_note(entry)
            entry["status"] = status
            if note:
                entry["note"] = note
            summary[status].append(name)
        self.results["fix_summary"] = summary
        self.results["fix_results"] = results

        manual_label = ", ".join(f"{n} (manuale)" for n in summary["manual"])
        failed_label = ", ".join(f"{n} (fallito)" for n in summary["failed"])
        labels = [x for x in (manual_label, failed_label) if x]
        if labels:
            self.logger.warning(
                "⚠️ Fix NON applicati automaticamente: %s", ", ".join(labels))

        return results

    @staticmethod
    def _classify_fix(result: Dict[str, Any]) -> str:
        """Classifica l'esito di un fix in 'applied'/'manual'/'failed'.

        - applied: fix applicato o già attivo (applied=True, skipped_*)
        - manual:  fix manuale/no-op (applied=False SENZA errore)
        - failed:  eccezione o applied=False CON errore
        """
        if result.get("applied"):
            return "applied"
        if result.get("error"):
            return "failed"
        return "manual"

    @staticmethod
    def _fix_note(result: Dict[str, Any]) -> str:
        """Estrae un dettaglio leggibile dall'esito di un fix."""
        if result.get("skipped_checkpoint"):
            return "già eseguito (checkpoint)"
        if result.get("skipped_verified"):
            return "già attivo (verificato)"
        if result.get("dry_run"):
            return "simulato (dry-run)"
        for key in ("error", "warning", "note", "detail"):
            value = result.get(key)
            if value:
                return str(value)
        return ""

    def _phase_optimize(self) -> Dict[str, Any]:
        """FASE 2 — OTTIMIZZAZIONE: undervolt + overclock power-limited."""
        self.logger.info("⚡ OTTIMIZZAZIONE (undervolt → overclock)")
        results: Dict[str, Any] = {}

        # Il governor va fermato durante i test
        if not self.mock:
            self.governor.stop()

        # CPU undervolt
        uv_cpu = self.uv_cpu.optimize(
            max_freq=self.config.cpu_freq_max,
        )
        results["undervolt_cpu"] = uv_cpu

        # GPU undervolt
        uv_gpu = self.uv_gpu.optimize(
            start_freq=self.config.undervolt_gpu_start_freq,
        )
        results["undervolt_gpu"] = uv_gpu

        # Log dedicato dell'undervolt (leggibile anche con sudo)
        self._write_undervolt_log(uv_cpu, uv_gpu)

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

    def _write_undervolt_log(self, cpu: Dict[str, Any],
                             gpu: Dict[str, Any]) -> None:
        """Scrive l'esito dell'undervolt in un file JSON dedicato (in home),
        così il risultato è leggibile anche quando BUO gira con sudo."""
        import json
        from datetime import datetime
        from .utils.paths import undervolt_log_file
        payload = {
            "timestamp": datetime.now().isoformat(),
            "cpu": cpu,
            "gpu": gpu,
        }
        try:
            path = undervolt_log_file()
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False,
                                       default=str), encoding="utf-8")
            self.logger.info("📝 Undervolt log scritto: %s", path)
        except Exception as e:
            self.logger.warning("Scrittura undervolt log fallita: %s", e)

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

        # Applica la config CPU: il punto UNDERVOLT validato (bc250-detect
        # lo ha testato stabile), VOLATILE via bc250-apply --apply. L'overclock
        # aggressivo (freq > punto stabile) NON viene applicato: è informativo.
        uv_cpu = optimize_data.get("undervolt_cpu", {})
        best = (uv_cpu.get("best_efficiency")
                or (uv_cpu.get("v_f_points") or [{}])[0])
        cpu_freq = best.get("freq") or oc_cpu.get("recommended_freq")
        if cpu_freq:
            results["cpu_final"] = self._apply_cpu_config(
                cpu_freq,
                scale=best.get("scale"),
                vid=best.get("vid"),
            )

        return results

    def _apply_cpu_config(self, freq: int, scale: Optional[int] = None,
                          vid: Optional[int] = None) -> Dict[str, Any]:
        """Applica il punto CPU (undervolt validato) — VOLATILE.

        Scrive overclock.conf e lo applica con `bc250-apply --apply`
        (NON --install: niente persistenza, nessun servizio). Il punto
        deve essere già stato validato da bc250-detect. Fail-closed ma
        NON bloccante: se lo script manca o fallisce, logga e continua
        (l'undervolt è un guadagno, non un requisito di sicurezza).
        """
        if self.dry_run:
            self.logger.info("CPU config: [DRY-RUN] simulata")
            return {"applied": True, "dry_run": True, "freq": freq}
        if self.mock:
            return {"applied": True, "mock": True, "freq": freq}
        try:
            from pathlib import Path as _P
            import os as _os
            from .unlock.wrappers.bc250_overclock import BC250ApplyWrapper
            f, s = self._clamp_cpu(freq, scale, vid)
            # A1: scrittura symlink-safe in /tmp (O_NOFOLLOW: se il path
            # è un symlink pre-creato da un altro utente → ELOOP → fail)
            conf = _P("/tmp/buo-overclock.conf")
            try:
                fd = _os.open(conf, _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC
                              | _os.O_NOFOLLOW, 0o600)
            except OSError:
                return {"applied": False,
                        "error": "overclock.conf non scrivibile "
                                 "(path occupato o symlink)"}
            with _os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(
                    f"[overclock]\nfrequency = {f}\n"
                    f"scale = {s}\nmax_temperature = {LIMITS.cpu.temp_max}\n",
                )
            w = BC250ApplyWrapper()
            if not w.available:
                return {"applied": False,
                        "warning": "bc250-apply non installato "
                                   "(esegui: sudo buo install-deps)"}
            result = w.apply(str(conf))
            if result["returncode"] != 0:
                return {"applied": False,
                        "error": (result.get("stderr") or "apply fallito")[:200]}
            self.logger.info("✅ CPU config applicata: %d MHz, scale %d "
                             "(volatile)", f, s)
            return {"applied": True, "freq": f, "scale": s,
                    "method": "bc250-apply (volatile)"}
        except Exception as e:
            self.logger.warning("CPU config non applicata: %s", e)
            return {"applied": False, "error": str(e)[:200]}

    @staticmethod
    def _clamp_cpu(freq: int, scale: Optional[int] = None,
                   vid: Optional[int] = None):
        """Clamp della coppia frequenza/scale ai limiti immutabili.
        Se scale manca ma c'è vid, conversione community scale=(1206-vid)/8."""
        f = max(LIMITS.cpu.freq_min, min(LIMITS.cpu.freq_max, int(freq)))
        s = 0
        if scale is not None:
            s = max(-8, min(50, int(scale)))
        elif vid is not None:
            s = max(-8, min(50, round((1206 - int(vid)) / 8)))
        return f, s

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

        # Benchmark after (in dry-run il runner è in modalità mock,
        # quindi viene simulato come il benchmark BEFORE e lo stress test)
        if self.config.benchmark_enabled:
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

        # Rendere espliciti i fix che richiedono ancora attenzione manuale
        # (no-op/manuali) o che sono falliti: il report deve dirlo chiaramente
        # invece di lasciare l'utente nell'incertezza.
        summary = self.results.get("fix_summary", {})
        manual = summary.get("manual") or []
        failed = summary.get("failed") or []
        if manual or failed:
            parts = []
            if manual:
                parts.append("manuali: " + ", ".join(manual))
            if failed:
                parts.append("falliti: " + ", ".join(failed))
            self.results["notes"].append(
                "Attenzione manuale richiesta — fix non applicati "
                "automaticamente (" + "; ".join(parts) + "). Consulta la "
                "sezione 'Esito Fix' del report."
            )

        self.report.generate(
            before=self.results["before"],
            after=self.results["after"],
            problems=self.results["problems"],
            fixes=self.results["fixes"],
            benchmarks=self.results["benchmarks"],
            applied_fixes=self.results["applied_fixes"],
            notes=self.results["notes"],
            fix_summary=self.results.get("fix_summary"),
            fix_results=self.results.get("fix_results"),
        )
        if getattr(self, "_partial_run", False):
            self.logger.info("✅ Fase/i richiesta/e completata/e")
        else:
            self.logger.info("✅ OTTIMIZZAZIONE COMPLETATA!")
        if not self.dry_run:
            self.checkpoint.set_phase("complete", {"done": True}, completed=True)
            # Cleanup anti-loop: a ciclo completato il servizio di ripresa
            # va rimosso, altrimenti al prossimo boot `buo resume` vede
            # "complete" → riparte da init → riesegue tutto → reboot → loop
            # (bug trovato sul campo: riavvii ripetuti a ogni accensione).
            from .state.reboot import RebootManager
            RebootManager().cleanup()

    def _handle_safety_violation(self) -> None:
        self.logger.error("🛑 Esecuzione interrotta per safety violation")
        if self.safety_monitor is not None:
            self.safety_monitor.stop()
        if not self.dry_run:
            self.rollback.rollback(reason=self.safety_reason,
                                   applied=self._applied_steps())
            from .state.reboot import RebootManager
            RebootManager().cleanup()
        self.results["notes"].append(
            f"Safety violation: {self.safety_reason} — rollback eseguito")

    def _handle_error(self, phase: str, error: str) -> None:
        self.logger.error("❌ Errore in fase %s: %s", phase, error)
        if self.safety_monitor is not None:
            self.safety_monitor.stop()
        if not self.dry_run:
            self.rollback.rollback(from_phase=None,
                                   reason=f"errore in {phase}: {error}",
                                   applied=self._applied_steps())
            from .state.reboot import RebootManager
            RebootManager().cleanup()
        self.results["notes"].append(f"Errore in {phase}: {error}")

    def _schedule_reboot(self, reason: str) -> None:
        """Salva checkpoint e programma il reboot (auto-ripresa)."""
        if self.dry_run:
            self.logger.info("♻️ [DRY-RUN] reboot richiesto: %s", reason)
            return
        if self.mock:
            self.checkpoint.increment_reboot_count()
            self.logger.info("♻️ [MOCK] reboot simulato: %s", reason)
            return
        # Tetto globale anti-boot-loop (difesa in profondità): oltre il
        # limite il pipeline si FERMA invece di riavviare ancora, evitando
        # loop infiniti causati da bug futuri.
        count = self.checkpoint.get_reboot_count()
        if count >= self.config.max_reboots:
            msg = (f"Tetto globale reboot raggiunto ({count}/"
                   f"{self.config.max_reboots}) — interruzione per evitare "
                   f"boot loop (ultimo reboot richiesto da: {reason})")
            self.logger.error("🚨 %s", msg)
            self._safety_abort(msg)
            return
        self.checkpoint.increment_reboot_count()
        self.logger.info("♻️ Reboot programmato: %s", reason)
        # In produzione: crea buo-resume.service e reboot
        from .state.reboot import RebootManager
        RebootManager().schedule(reason=reason, delay=5)
        sys.exit(EXIT_REBOOT)

    def _applied_steps(self) -> set:
        """Ledger delle modifiche già eseguite (persistito nel checkpoint)."""
        return set(self.checkpoint.get("applied_steps", []) or [])

    def _restore_results_from_checkpoint(self) -> None:
        """Ripresa dopo reboot: ricarica i dati in-memory delle fasi già
        completate dal checkpoint. Senza questo, il report finale perde
        "before", "problems" e "applied_fixes"."""
        pa = self.checkpoint.get_phase("pre_audit")
        if pa.get("completed"):
            data = pa.get("data", {}) or {}
            if data.get("audit"):
                self.results["before"] = data["audit"]
            if data.get("problems"):
                self.results["problems"] = data["problems"]
        steps = self._applied_steps()
        if steps:
            self.results["applied_fixes"] = sorted(steps)

    def _mark_step(self, name: str) -> None:
        """Registra una modifica come eseguita (PRIMA di eventuali reboot)."""
        steps = self._applied_steps()
        steps.add(name)
        if not self.dry_run:
            self.checkpoint.set("applied_steps", sorted(steps))

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
