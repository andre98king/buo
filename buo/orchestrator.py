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
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import __version__
from .audit.hardware import HardwareAudit
from .audit.problems import ProblemDetector
from .benchmark.runner import BenchmarkRunner
from .config import BUOConfig
from .constants import (CORE_MASK_STOCK, EXIT_ERROR, EXIT_REBOOT,
                        EXIT_SAFETY_VIOLATION, EXIT_SUCCESS, LIMITS, PHASES,
                        SMU_OC_SERVICE)
from .exceptions import ConfigurationError, SafetyViolation
from .fix.ace import ACEComputeFix
from .fix.acpi import ACPIFix
from .fix.fan import FanControl
from .fix.gtt import GTTTuning
from .fix.iommu import IOMMUFix
from .fix.tlb import TLBKernelFix
from .fix.vram import VRAMConfig
from .optimize.cpu import (CPUUndervoltOptimizer,
                           resolve_cpu_target_vid)
from .optimize.governor import GovernorWrapper
from .optimize.gpu import GPUUndervoltOptimizer
from .optimize.overclock import OverclockOptimizer
from .report.generator import ReportGenerator
from .safety.monitor import SafetyMonitor
from .state.checkpoint import CheckpointManager
from .state.ostree import OstreeDeploymentManager
from .state.recovery import RecoveryManager
from .state.rollback import RollbackManager
from .unlock.cpu import CPUUnlock
from .unlock.dxe import DXECoreUnlock
from .unlock.gpu import GPU40CUUnlock
from .unlock.health import CUHealthTest
from .unlock.mask import CUMask
from .unlock.validation import (CpuUnlockValidation, GpuUnlockValidation,
                                UnlockVerdict, evidence)
from .utils.logging import LoggerMixin, setup_logging
from .utils.mock import MockHardware
from .validate.stress import StressTest
from .validate.verify import FixVerifier

# Istruzioni esatte per il fallback offline quando il download dei tool
# fallisce senza rete/bundle (design: DESIGN_OFFLINE_DEPS.md sez. 2).
OFFLINE_HINT = (
    " Controlla la connessione e `git`, oppure usa il bundle offline:\n"
    "  1) su una macchina CON rete:   sudo buo install-deps "
    "--export-bundle bundle.tar.gz\n"
    "  2) copia il file su USB e importalo qui:\n"
    "       sudo buo install-deps --offline /percorso/bundle.tar.gz\n"
    "  3) oppure imposta deps.offline_bundle in /etc/buo/buo.yaml "
    "e riprova: sudo buo unleash\n"
)

# Nomi leggibili dei fix per il riepilogo finale (§3 spec UX_REVAMP_CLI).
FIX_READABLE = {
    "cpu_core_unlock": "8 core",
    "gpu_40cu": "40 CU",
    "gpu_mask": "maschera CU",
    "acpi_fix": "fix ACPI",
    "tlb_fix": "fix TLB",
    "ace_fix": "fix ACE",
    "iommu": "fix IOMMU",
    "vram_config": "config VRAM",
    "gtt_tuning": "tuning GTT",
    "fan_control": "ventole",
    "cpu_overclock": "undervolt/OC CPU",
}


class Orchestrator(LoggerMixin):
    """Macchina a stati principale di BUO."""

    def __init__(self, config: Optional[BUOConfig] = None,
                 mock: bool = False,
                 dry_run: bool = False,
                 interactive: bool = False,
                 mock_hardware: Optional[MockHardware] = None,
                 log_level: str = "INFO",
                 offline_bundle: Optional[str] = None,
                 ostree: Optional[OstreeDeploymentManager] = None):
        setup_logging(level=log_level)

        self.config = config or BUOConfig.load()
        self.mock = mock
        self.dry_run = dry_run
        self.interactive = interactive
        # Bundle offline dei checkout (flag CLI; ha precedenza sulla config
        # deps.offline_bundle). Mai importato in mock/dry-run.
        self.offline_bundle = offline_bundle
        # Segmento di fasi target (impostato da run(); usato da
        # _run_can_schedule_reboot anche fuori da run() nei test).
        self._stop_after: Optional[str] = None

        # Hardware (mock o reale)
        self.hardware = mock_hardware or (MockHardware() if mock else None)

        # Stato e checkpoint
        self.checkpoint = CheckpointManager()
        self.state = self.checkpoint
        self.rollback = RollbackManager(mock=mock, hardware=self.hardware)
        # Ostree (deployment-aware reboot, design OSTREE_REBOOT): iniettato
        # nei test (come mock_hardware); inerte per costruzione in
        # mock/dry-run → i run simulati NON cambiano comportamento.
        self.ostree = ostree or OstreeDeploymentManager(mock=mock,
                                                        dry_run=dry_run)

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

        # Verdetto durevole del silicio (unlock-verdict.json, D6): letto
        # SEMPRE (il gate si applica anche in mock ai file pre-esistenti);
        # in mock/dry-run set() aggiorna SOLO la memoria (nessun file).
        self.unlock_verdict = UnlockVerdict(sim=mock or dry_run)

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
        # Validazione post-unlock (design POSTUNLOCK_VALIDATION): in
        # mock/dry-run nessun subprocess (esiti SOLO dal mock_hardware).
        self.cpu_validation = CpuUnlockValidation(
            mock=eff_mock, dry_run=self.dry_run, mock_hardware=hw)
        self.gpu_validation = GpuUnlockValidation(
            mock=eff_mock, dry_run=self.dry_run, mock_hardware=hw)

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
            stop_after: Optional[str] = None,
            restore: Optional[Dict[str, Any]] = None) -> int:
        """
        Esegue il workflow (o un sottoinsieme di fasi).

        Args:
            start_phase: fase da cui partire (default: dal checkpoint)
            stop_after: fermarsi DOPO questa fase (es. comandi fase
                        standalone: probe, undervolt, apply)
            restore: profilo macchina (G2). Se presente, la fase
                     'optimize' NON viene rilanciata (auto-tuning):
                     si riapplicano i punti salvati nel profilo.
        """
        self.logger.info("Avvio ottimizzazione (BUO v%s)", __version__)

        # A6: lock anti-esecuzione-concorrente (solo run reali). Due
        # istanze simultanee corromperebbero stato e ledger. Il flock
        # viene rilasciato automaticamente dal kernel alla chiusura del
        # processo (anche su crash).
        if not self.dry_run and not self.mock:
            self._acquire_lock()

        # Modalità RESTORE (G2): riapplica il profilo senza auto-tuning
        self._restore_mode = restore is not None
        if restore is not None:
            optimize_data = restore.get("optimize", {})
            self.checkpoint.seed_phase("optimize", optimize_data)
            # F-A: marcatore PERSISTENTE — la modalità restore deve
            # sopravvivere al reboot (buo-resume/recovery riparte con un
            # NUOVO processo SENZA il parametro restore). Solo nei run
            # reali: il dry-run non deve toccare lo stato persistente.
            # set() salva l'intero stato, incluse le fasi seedate.
            if not self.dry_run:
                self.checkpoint.set("restore_active", True)
                # FIX (30/08): il restore con stress saltato (CLI senza
                # --validate imposta validation_stress_duration=0 SOLO nel
                # processo CLI) deve restare saltato anche al resume: il
                # nuovo processo ricarica la config con lo stress_duration
                # reale e rifaceva lo stress completo. Marcatore persistente
                # (stesso pattern di restore_active).
                if self.config.validation_stress_duration <= 0:
                    self.checkpoint.set("validation_stress_skip", True)
            self.logger.info(
                "RESTORE: profilo applicato (%d fix, %s)",
                len(restore.get("applied_fixes", []) or []),
                restore.get("created", "data sconosciuta"))

        # Esecuzione parziale (comando fase standalone)? `unleash` passa
        # start_phase="init" per partire SEMPRE da init: è un run COMPLETO
        # da init, non un'esecuzione parziale (messaggio finale di run).
        self._partial_run = start_phase is not None and start_phase != "init"
        self._stop_after = stop_after

        # Il dry-run è pura simulazione: NON tocca lo stato persistente.
        # Un run reale con checkpoint "complete" (run precedente finita)
        # riparte da init: la ripresa serve solo per esecuzioni interrotte.
        if self.dry_run:
            current = start_phase or "init"
        else:
            current = start_phase or self.checkpoint.get_current_phase()
        if current not in PHASES or current == "complete":
            current = "init"

        # F-C (bug sul campo 29/08): se al PRE-reboot il gate ACPI ha
        # bloccato l'unlock CPU (marcatore `unlock_blocked_acpi`) e la fix
        # è stata applicata (il gate ora passa), al RESUME si ritenta la
        # fase unlock PRIMA di proseguire — altrimenti la macchina resta
        # a 12 thread fino a un secondo run manuale. Il marcatore viene
        # consumato al retry: l'unlock viene ritentato UNA volta (anti-loop);
        # se il gate è ancora chiuso, il marcatore resta per il resume
        # successivo. Solo riprese automatiche (start_phase None): i comandi
        # fase standalone non devono essere dirottati verso l'unlock.
        if (not self.dry_run and start_phase is None
                and current != "init"
                and self.checkpoint.get("unlock_blocked_acpi")):
            if self._acpi_gate_ok():
                self.checkpoint.set("unlock_blocked_acpi", False)
                self.logger.info(
                    "F-C: fix ACPI applicata — RETRY della fase unlock "
                    "CPU (bloccata dal gate al run precedente)")
                current = "unlock"
            else:
                self.logger.warning(
                    "F-C: unlock CPU bloccato dal gate ACPI al run "
                    "precedente ma la fix NON risulta ancora applicata — "
                    "si prosegue; il retry avverrà a un prossimo resume")

        # Un run completo NUOVO (da init) azzera il ledger delle modifiche
        # e il contatore dei reboot (per-run): i fix ripartono da zero.
        # In caso di RIPRESA (fase intermedia) restano e impediscono loop.
        # F-A: un run nuovo SENZA restore deve anche pulire il marcatore
        # restore_active (se un restore è abortito/fallito e l'utente
        # rilancia `buo unleash`, NON deve ereditare la modalità restore:
        # il tuning deve ripartire). Il blocco restore sopra (che imposta
        # il marcatore) viene eseguito PRIMA di questo, quindi il guard
        # `restore is None` evita di cancellarlo nei run di restore.
        # F-C: anche il marcatore di retry unlock va azzerato nei run nuovi
        # da init, per non inquinare run successivi.
        if current == "init" and not self.dry_run:
            self.checkpoint.set("applied_steps", [])
            self.checkpoint.set("reboot_count", 0)
            self.checkpoint.set("unlock_blocked_acpi", False)
            # Validazione post-unlock: un run nuovo azzera marker/attempts
            # residui (pattern unlock_blocked_acpi). Il verdetto durevole
            # in unlock-verdict.json resta — è la memoria del silicio.
            self.checkpoint.set("unlock_cpu_validate_marker", None)
            self.checkpoint.set("unlock_gpu_validate_marker", None)
            self.checkpoint.set("unlock_cpu_validate_attempts", 0)
            if restore is None:
                self.checkpoint.set("restore_active", False)
                # FIX (30/08): stesso pattern — un run nuovo SENZA restore
                # pulisce anche il marcatore di stress saltato residuo (se
                # un restore è abortito e l'utente rilancia `buo unleash`,
                # la validate deve girare con lo stress normale).
                self.checkpoint.set("validation_stress_skip", False)

        # RIPRESA (fase > init): ricarica i dati delle fasi già completate
        # dal checkpoint (before/problemi/fix applicati), altrimenti il
        # report finale risulta vuoto (bug #12).
        if not self.dry_run and current != "init":
            self._restore_results_from_checkpoint()

        try:
            # OSTREE (design OSTREE_REBOOT §7.2): se la run PUÒ programmare
            # reboot (unlock/fix nel segmento di fasi) ed è partita da un
            # deployment NON-default, il default di boot viene impostato
            # SUBITO sul deployment corrente (swap EAGER, D1): ogni reboot
            # — pianificato, della CU health test o imprevisto — atterra
            # qui, dove vivono /etc, buo-resume.service e lo stato della
            # run. Fail-closed: False = abort PRIMA di toccare l'hardware.
            if not self._ensure_ostree_default(current):
                return EXIT_ERROR
            while current != "complete":
                if self.safety_violation:
                    self._handle_safety_violation()
                    return EXIT_SAFETY_VIOLATION

                if self.interactive and not self._confirm_phase(current):
                    self.logger.info("Interrotto dall'utente")
                    self._exit_ostree_cleanup()
                    return EXIT_SUCCESS

                self.logger.info("Fase: %s", current)
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
                    self.logger.error("SAFETY VIOLATION: %s", e)
                    self.safety_reason = str(e)
                    self._handle_safety_violation()
                    return EXIT_SAFETY_VIOLATION
                except Exception as e:
                    self.logger.error("Errore in fase %s: %s", current, e)
                    self._handle_error(current, str(e))
                    return EXIT_ERROR

            # Completato
            self._finalize()
            return EXIT_SUCCESS

        except KeyboardInterrupt:
            self.logger.info("Interrotto dall'utente")
            self._exit_ostree_cleanup()
            return EXIT_SUCCESS
        except Exception as e:
            self.logger.error("Errore fatale: %s", e)
            import traceback
            self.logger.debug(traceback.format_exc())
            self._exit_ostree_cleanup()
            return EXIT_ERROR

    def _acquire_lock(self) -> None:
        """A6: flock non bloccante sulla dir di stato (anti-concorrenza).

        Se un'altra istanza è in esecuzione → RuntimeError immediato
        (niente doppie scritture hardware / stato corrotto).
        """
        import fcntl
        from .utils.paths import state_dir
        state_dir().mkdir(parents=True, exist_ok=True)
        self._lock_fd = open(state_dir() / "buo.lock", "w", encoding="utf-8")
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError(
                "Un'altra istanza di buo è già in esecuzione "
                "(stato bloccato: attendere o chiudere l'altra istanza).")

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
        if phase == "optimize" and (self._restore_mode
                                    or self.checkpoint.get("restore_active")):
            # G2 + F-A: in restore NON si rilancia l'auto-tuning; si
            # riapplicano i punti salvati nel profilo (già seedati nel
            # checkpoint). Il marcatore persistente `restore_active`
            # estende la modalità al resume dopo reboot (il nuovo processo
            # non ha il parametro restore).
            self._restore_mode = True
            return self.checkpoint.get_phase("optimize").get("data", {})
        handlers = {
            "init": self._phase_init,
            "pre_audit": self._phase_pre_audit,
            "unlock": self._phase_unlock,
            "unlock_validate": self._phase_unlock_validate,
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
        self.logger.info("Inizializzazione…")

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
            from .safety.reader import RealHardwareReader
            self.safety_monitor = SafetyMonitor(
                # m1: le soglie config safety.* sono SOLO STRINGIMENTI —
                # SafetyMonitor le clampa ai hard limits (min)
                limits={
                    "cpu_temp_max": self.config.cpu_temp_max,
                    "gpu_temp_max": self.config.gpu_temp_max,
                    "power_budget": self.config.power_budget,
                },
                hardware=RealHardwareReader(),  # C1: letture REALI, mai fittizie
                abort_callback=self._safety_abort,
                vram_estimation=self.config.vram_estimation_enabled,
                vram_alpha=self.config.vram_alpha,
                vram_beta=self.config.vram_beta,
                vram_tau=self.config.vram_tau,
                vram_warning_threshold=self.config.vram_warning_threshold,
                vram_critical_threshold=self.config.vram_critical_threshold,
            )
            self.safety_monitor.start()
            self.logger.info(
                "Safety monitor avviato (campionamento ogni 0,5 s)")

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
        self.logger.info("Verifica di sanità pre-operativa…")
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
                "Temperatura CPU non leggibile: il gate termico "
                "pre-operativo non può verificare il surriscaldamento "
                "(fail-soft — si procede)")
        elif cpu_t > LIMITS.cpu.temp_critical - 10:  # > 90°C pre-operativo
            raise SafetyViolation(
                f"Temperatura CPU attuale {cpu_t:.1f}°C troppo alta per iniziare"
            )
        gpu_t = temps.get("gpu_temp")
        if gpu_t is None:
            self.logger.warning(
                "Temperatura GPU non leggibile: il gate termico "
                "pre-operativo non può verificare il surriscaldamento "
                "(fail-soft — si procede)")
        elif gpu_t > LIMITS.gpu.temp_critical - 15:  # > 85°C
            raise SafetyViolation(
                f"Temperatura GPU attuale {gpu_t:.1f}°C troppo alta per iniziare"
            )

        if cpu_t and cpu_t > 60:
            self.logger.warning(
                "ATTENZIONE: CPU a %.1f°C — verifica il raffreddamento",
                cpu_t)

        # Budget di potenza: la combo 8 core + 40 CU ha un picco noto.
        # Avviso NON bloccante: la decisione
        # finale resta al governor/limiti immutabili.
        self._check_power_budget()

        # Toolchain 40-CU: su ostree serve bc250-cu-live-manager + umr
        # (il kernel patch non funziona: /usr read-only). Avviso non
        # bloccante: l'unlock fallirà in modo pulito se mancano.
        self._check_40cu_toolchain(audit)

        self.logger.info("Verifica di sanità superata")

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
                    "POTENZA: PSU dichiarato %dW con 8 core + 40 CU "
                    "abilitati. Picco misurato: FurMark 250-320W SENZA cap. "
                    "Per restare sotto i %dW: undervolt + cap GPU 1500 MHz "
                    "(≈125-220W).", psu, psu)
            else:
                self.logger.info(
                    "Potenza: PSU %dW sufficiente per 8 core + 40 CU "
                    "(comunque consigliato il cap GPU 1500 MHz per "
                    "l'efficienza).", psu)
        elif self.config.probe_gpu_unlock and self.config.psu_wattage < 300:
            self.logger.warning(
                "POTENZA: PSU %dW con 40 CU: picco FurMark 250-320W "
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
                "Toolchain 40-CU non inizializzata: esegui "
                "`sudo buo install-deps` prima dell'unlock GPU.")
            return
        if not self.gpu_unlock.wrapper.available:
            self.logger.warning(
                "Script 40-CU mancante (%s): esegui "
                "`sudo buo install-deps` (o installa manualmente il tool "
                "della community).", self.gpu_unlock.wrapper.script_path)
            return
        if self.gpu_unlock.is_ostree:
            import shutil
            if shutil.which("umr") is None:
                self.logger.warning(
                    "`umr` non trovato: necessario per il runtime UMR "
                    "delle 40 CU su ostree. Installare con: "
                    "rpm-ostree install umr (poi reboot).")
            else:
                self.logger.info(
                    "Toolchain 40-CU pronta (umr + live-manager)")
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
                "BUGS #24: verifica servizio 40-CU non riuscita (%s) — "
                "controlla manualmente `systemctl is-enabled %s`.", e, unit)
            return
        if rc == 0 and out.strip() == "enabled":
            self.logger.info("Servizio 40-CU: %s.service abilitato", unit)
            return
        # G4: auto-riparazione (solo run reali) — "BUO si occupa di tutto"
        if not self.mock and not self.dry_run:
            if self._repair_40cu_service(unit):
                self.logger.info(
                    "Servizio 40-CU riparato automaticamente")
                return
        self.logger.warning(
            "BUGS #24: %s.service mancante/disabilitato — le 40 CU "
            "torneranno a 24 CU al prossimo riavvio. Recovery (quirk: "
            "`install-service` da /usr/local/bin fallisce con 'same file' "
            "perché /usr/local è un symlink; eseguirlo da una copia in "
            "path NON-symlink):\n"
            "  sudo cp /usr/local/bin/bc250-cu-live-manager /tmp/\n"
            "  sudo /tmp/bc250-cu-live-manager --yes install-service\n"
            "  rm /tmp/bc250-cu-live-manager\n"
            "  sudo /usr/local/bin/bc250-cu-live-manager --yes apply-service",
            unit)

    def _repair_40cu_service(self, unit: str) -> bool:
        """G4: ripara l'unità systemd 40-CU (BUGS #24).

        1) unità esistente ma disabilitata → `systemctl enable`;
        2) unità ASSENTE → reinstall tramite copia in /tmp (quirk:
           /usr/local è un symlink su ostree) + install-service +
           apply-service, poi pulizia del temporaneo.
        """
        import os as _os
        import shutil as _shutil
        from .utils.shell import run_command

        rc2, _, _ = run_command(["systemctl", "cat", unit], check=False)
        if rc2 == 0:  # unità presente ma non abilitata
            rc3, _, err = run_command(["systemctl", "enable", unit],
                                      sudo=True, check=False)
            if rc3 == 0:
                return True
            self.logger.warning("enable %s fallito: %s", unit, err)
            return False
        # unità assente → reinstall (quirk symlink /usr/local)
        lm = "/usr/local/bin/bc250-cu-live-manager"
        if not _os.path.exists(lm):
            self.logger.warning("live-manager assente: %s", lm)
            return False
        tmp = "/tmp/bc250-cu-live-manager"
        try:
            _shutil.copy2(lm, tmp)
        except Exception as e:
            self.logger.warning("copia live-manager fallita: %s", e)
            return False
        try:
            r1 = run_command([tmp, "--yes", "install-service"],
                             sudo=True, check=False)
            r2 = run_command([tmp, "--yes", "apply-service"],
                             sudo=True, check=False)
            return r1[0] == 0 and r2[0] == 0
        finally:
            try:
                _os.remove(tmp)
            except Exception:
                pass

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
            self.logger.info("Tool della community presenti")
            return

        self.logger.info(
            "Tool della community mancanti (%s) — download automatico...",
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
        # il governor disabilitato verrebbe installato comunque. Il bundle
        # offline (flag CLI o config deps.offline_bundle) viene importato da
        # install() PRIMA del giro normale, se servono tool git-based.
        bundle = self.offline_bundle or self.config.deps_offline_bundle or None
        result = manager.install(deps=missing, offline_bundle=bundle)
        if "_error" in result:
            raise ConfigurationError(
                f"Download automatico non possibile: {result['_error']}"
                + ("" if bundle else OFFLINE_HINT)
            )
        failed = [n for n, s in result.items() if s.get("status") == "failed"]
        if failed:
            # UX (bug sul campo): riporta il DETTAGLIO di ogni dep
            # fallito (es. "binario non prodotto: ...", "make fallito:
            # ..."), non solo il nome — altrimenti il problema reale
            # resta invisibile e va riprodotto a mano.
            pieces = []
            for n in failed:
                detail = (result[n].get("detail") or "").strip()
                if not detail:
                    pieces.append(n)
                elif detail.startswith(f"{n}:"):
                    pieces.append(detail)  # il dettaglio include già il nome
                else:
                    pieces.append(f"{n}: {detail}")
            raise ConfigurationError(
                "Impossibile scaricare i tool necessari: "
                f"{', '.join(pieces)}."
                + ("" if bundle else OFFLINE_HINT)
            )
        self.logger.info("Tool scaricati e installati automaticamente")

        # Governor appena installato (COPR/AUR): scrivi la config di default
        # sicura (flat 1000mV, template vendored) e avvisa se serve un
        # reboot per l'attivazione (rpm-ostree layering su Bazzite).
        gov = result.get("cyan-skillfish-governor", {})
        if gov.get("status") == "ok":
            self._configure_installed_governor(gov)
        umr = result.get("umr", {})
        if umr.get("status") == "ok" and umr.get("needs_reboot"):
            self.logger.warning(
                "umr installato: ATTIVO al prossimo reboot "
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
                "Governor installato: sarà ATTIVO al prossimo reboot "
                "(rpm-ostree layering)")

    def _phase_pre_audit(self) -> Dict[str, Any]:
        """FASE 0 — PRE-AUDIT: discovery, problemi, benchmark before."""
        self.logger.info("Pre-audit — analisi dello stato attuale")

        audit = self.audit.run()
        problems = self.detector.detect(audit)
        self.results["before"] = audit
        self.results["problems"] = problems

        # "Nessun problema noto rilevato" è uno stato positivo → INFO;
        # le righe dei problemi restano WARNING (spec UX_REVAMP_CLI §2.2).
        for line in self.detector.summary(problems).splitlines():
            if problems:
                self.logger.warning(line)
            else:
                self.logger.info(line)

        if self.config.benchmark_enabled:
            self.logger.info("Benchmark prima (stato attuale)…")
            self.results["benchmarks"]["before"] = self.benchmark.run_all(
                gpu_duration=self.config.benchmark_gpu_duration,
                cpu_duration=self.config.benchmark_cpu_duration,
                compute_duration=self.config.benchmark_compute_duration,
            )

        return {"audit": audit, "problems": problems}

    def _phase_unlock(self) -> Dict[str, Any]:
        """FASE 1 — SBLOCCHI: CPU 8-core, GPU 40-CU, health test, maschera."""
        self.logger.info("Sblocchi — CPU 8-core e GPU 40-CU")
        results: Dict[str, Any] = {}
        done = self._applied_steps()

        # 1. CPU 8-core (volatile) — con GATE ACPI fail-closed e gate
        # verdetto durevole (D6: silicio condannato → mai più sbloccare)
        if self.config.probe_cpu_unlock and "cpu_core_unlock" not in done:
            if self.unlock_verdict.get("cpu") == "never_unlock":
                self.logger.warning(
                    "CPU: silicio marcato never_unlock — unlock 8-core "
                    "SALTATO (vedi research/DESIGN_POSTUNLOCK_VALIDATION.md)")
                results["cpu"] = {"unlocked": False,
                                  "verdict_blocked": True}
                self.results["notes"].append(
                    "Unlock CPU saltato: silicio marcato instabile "
                    "(verdetto never_unlock) — si prosegue a 6 core")
                # M1: verdetto presente MA maschera ancora 0xFF (kill tra
                # verdetto e scrittura 0x77, o revert fallito ignorato):
                # la maschera sopravvive ai WARM reboot → revert OBBLIGATO
                # prima di proseguire (D5: mai 16T su silicio condannato).
                self._cpu_revert_if_condemned(results["cpu"])
            elif not self._acpi_gate_ok():
                self.logger.warning(
                    "GATE ACPI: fix SSDT-PST/CST mancanti — senza di esse "
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
                        "CPU unlock SALTATO (fail-closed): applicare prima "
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
                    # F-C (bug sul campo 29/08): il blocco va RICORDATO nel
                    # checkpoint (SOLO run reali) — al resume dopo il reboot
                    # in cui acpi_fix viene applicata, la fase unlock verrà
                    # RITENTATA (vedi run()). Il dry-run non tocca lo stato
                    # persistente.
                    if not self.dry_run:
                        self.checkpoint.set("unlock_blocked_acpi", True)
                else:
                    results["cpu"] = self._do_cpu_unlock()
            else:
                results["cpu"] = self._do_cpu_unlock()
        elif "cpu_core_unlock" in done:
            self.logger.info("CPU: unlock già eseguito (checkpoint) — salto")

        # 2. GPU 40-CU — con gate verdetto (D6), validazione short dopo
        # l'attivazione (D8) e persistenza solo su silicio validato (D9)
        gpu_marker_handled = False
        gpu_marker_fresh = False
        if self.config.probe_gpu_unlock:
            marker = self.checkpoint.get("unlock_gpu_validate_marker")
            if marker and self._marker_stale(marker):
                # D7: marcatore STALE = la macchina è ripartita durante la
                # validazione GPU (hang): NIENTE ri-esecuzione — verdetto
                # never_enable_all + stock dispatch + disable persistenza.
                self.logger.error(
                    "unlock: HANG durante la validazione GPU (marcatore "
                    "stale) — verdetto never_enable_all, stock dispatch")
                self.unlock_verdict.set(
                    "gpu", "never_enable_all",
                    evidence(cause="hang", tool="vkmark"))
                # interno no-op in mock/dry-run (mai systemctl reali)
                self._disable_40cu_persistence()
                results["gpu"] = {
                    "applied": False,
                    "validation": {"outcome": "fail", "cause": "hang"},
                }
                self.results["notes"].append(
                    "40-CU NON validata (hang durante la validazione): "
                    "tornate a 24 CU — verdetto never_enable_all scritto")
                self._gpu_stock_dispatch(results)
                if not self.mock and not self.dry_run:
                    self.checkpoint.set("unlock_gpu_validate_marker", None)
                gpu_marker_handled = True
            elif marker:
                # fresco (stesso boot, processo ucciso): pulisci; lo
                # stato attivo NON certificato va ri-verificato (m1)
                gpu_marker_fresh = True
                if not self.mock and not self.dry_run:
                    self.checkpoint.set("unlock_gpu_validate_marker", None)
        if (self.config.probe_gpu_unlock and "gpu_40cu" not in done
                and not gpu_marker_handled):  # n5: hang già gestito sopra
            try:
                was_enabled = self.gpu_unlock.is_enabled()
                if self.unlock_verdict.get("gpu") == "never_enable_all":
                    self.logger.warning(
                        "GPU: silicio marcato never_enable_all — enable_all "
                        "SALTATO (vedi research/DESIGN_POSTUNLOCK_"
                        "VALIDATION.md)")
                    if was_enabled:
                        # già attiva da persistenza legacy: torna a stock
                        # e disattiva la persistenza (mai 40-CU su
                        # silicio condannato a ogni boot)
                        self.gpu_unlock.rollback()
                        self._disable_40cu_persistence()
                    results["gpu"] = {"applied": False,
                                      "verdict_blocked": True}
                    self.results["notes"].append(
                        "enable_all GPU saltato: silicio marcato instabile "
                        "(verdetto never_enable_all) — GPU a 24 CU")
                else:
                    gpu = self.gpu_unlock.apply()
                    results["gpu"] = gpu
                    if not was_enabled and gpu.get("applied"):
                        self.results["applied_fixes"].append("gpu_40cu")
                        self._mark_step("gpu_40cu")  # prima del reboot
                        if gpu.get("needs_reboot"):
                            self._schedule_reboot("GPU 40-CU — reboot richiesto")
                        self._gpu_post_enable(results, gpu)
                    elif was_enabled:
                        self._gpu_already_active(results, gpu)
            except Exception as e:
                self.logger.warning("Unlock GPU non eseguito: %s", e)
                results["gpu"] = {"applied": False, "error": str(e)}
        elif "gpu_40cu" in done and not gpu_marker_handled:
            if gpu_marker_fresh:
                # m1: validazione interrotta (SIGKILL, stesso boot) — le
                # 40-CU attive NON certificate non vanno lasciate per il
                # resto della run: ri-verifica dello stato corrente (D8)
                if self.gpu_unlock.is_enabled():
                    self.logger.warning(
                        "GPU: validazione interrotta (marcatore fresco) — "
                        "ri-verifica dello stato corrente")
                    results["gpu"] = {"applied": False,
                                      "already_active": True,
                                      "method": "runtime_umr"}
                    self._gpu_already_active(results, results["gpu"])
                else:
                    self.logger.info(
                        "GPU: validazione interrotta ma 40-CU non attive "
                        "(stock) — salto")
            else:
                self.logger.info(
                    "GPU: unlock 40-CU già eseguito (checkpoint) — salto")

        # 3. Health test CU (se abilitato) — "smart" (design
        # DESIGN_PORTABILITY_DEFAULTS 3.4): si RIUSANO i results.tsv
        # COMPLETI (macchina già testata → nessun reboot); assenti o
        # incompleti → SKIP con ricetta — la maratona per-WGP
        # (bc250-cu-health-test.sh start = ~20 reboot) NON parte MAI da
        # una run non presidiata. Verificato sul campo (03/09): `quick`
        # testa SOLO la config corrente senza isolare le WGP → non
        # sostituisce il protocollo; il primo unlock 40-CU in run
        # interattiva resta da validare (non implementato alla cieca).
        if self.config.probe_health_test:
            try:
                health = self.health_test.read_results()
                results["health"] = health
                defective = health.get("defective", [])
                if health.get("complete"):
                    self.logger.info(
                        "CU health: results.tsv completo (%d righe) — "
                        "riuso, nessun reboot", health.get("total", 0))
                else:
                    self.logger.warning(
                        "CU health test SALTATO: results.tsv "
                        "assente/incompleto — il protocollo per-WGP "
                        "richiede ~20 reboot (eseguirlo a parte: "
                        "bc250-cu-health-test.sh start, o un run "
                        "interattivo sul primo unlock 40-CU)")
                    self.results["notes"].append(
                        "CU health test saltato: results.tsv assente/"
                        "incompleto — eseguire bc250-cu-health-test.sh "
                        "start (per-WGP, ~20 reboot) o un run interattivo "
                        "sul primo unlock 40-CU")
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

    # ================================================================== #
    # VALIDAZIONE POST-UNLOCK (design POSTUNLOCK_VALIDATION, 4.1/4.2)
    # ================================================================== #

    def _phase_unlock_validate(self) -> Dict[str, Any]:
        """FASE — VALIDAZIONE post-unlock CPU (8 core, 16 thread reali).

        Gira tra `unlock` e `fix` (D1/D2): solo quando i thread extra
        sono attivi dopo il warm reboot di attivazione. Ingresso
        idempotente e fail-closed (design 4.2); esito tri-state; il
        revert costa UN solo reboot (il verdetto salta la fase al
        resume). MAI SafetyViolation (D10) — unica eccezione: revert CPU
        impossibile in software → interruzione controllata (D5).
        """
        self.logger.info(
            "Validazione post-unlock CPU (thread extra, %ds, 0 WHEA "
            "attesi)", self.config.validation_unlock_cpu_seconds)
        data: Dict[str, Any] = {"cpu": {}}
        cpu_out = data["cpu"]

        # 1. Verdetto durevole → silicio condannato: si prosegue a stock
        # (MAJOR M1: verdetto presente MA maschera ancora 0xFF — kill tra
        # verdetto e scrittura 0x77, o revert fallito ignorato — la
        # maschera sopravvive ai WARM reboot → revert OBBLIGATO prima di
        # proseguire; D5: mai 16T su silicio condannato)
        if self.unlock_verdict.get("cpu") == "never_unlock":
            self.logger.warning(
                "CPU: silicio marcato never_unlock — si prosegue a stock "
                "(vedi research/DESIGN_POSTUNLOCK_VALIDATION.md)")
            cpu_out["verdict_blocked"] = True
            self._cpu_revert_if_condemned(cpu_out, phase_data=data)
            self.results["unlock_validation"] = data
            return data

        # 2. Niente da validare (skip idempotenti)
        if "cpu_core_unlock" not in self._applied_steps():
            self.logger.info(
                "CPU: unlock non eseguito da questa run — niente da "
                "validare (8 core già da BIOS/DXE)")
            cpu_out["skipped"] = "no_unlock_this_run"
            self.results["unlock_validation"] = data
            return data
        if not self.config.probe_unlock_validate:
            self.logger.info(
                "Validazione post-unlock DISABILITATA (probe."
                "unlock_validate=false)")
            cpu_out["skipped"] = "disabled"
            self.results["unlock_validation"] = data
            return data
        if self.config.validation_unlock_cpu_seconds <= 0:
            self.logger.info(
                "Validazione CPU saltata (0 secondi, config)")
            cpu_out["skipped"] = "duration_zero"
            self.results["unlock_validation"] = data
            return data
        mask = self._cpu_read_mask()  # M4: try/except + governor FERMO
        if mask is not None and (mask & 0xFF) == CORE_MASK_STOCK:
            self.logger.info(
                "CPU: maschera 0x77 (stock) — revert già avvenuto, niente "
                "da validare")
            cpu_out["skipped"] = "mask_stock"
            self.results["unlock_validation"] = data
            return data
        threads = self._cpu_online_threads()
        if threads is not None and threads <= 12:
            self.logger.info(
                "CPU: %d thread attivi — niente da validare", threads)
            cpu_out["skipped"] = "threads_le_12"
            self.results["unlock_validation"] = data
            return data

        # 3. Marcatore STALE (epoch < boot) = HANG durante la validazione
        # precedente: verdetto never_unlock + revert SENZA rieseguire lo
        # stress (D7). 4. Marcatore FRESCO (stesso boot, processo ucciso):
        # ri-esecuzione, max 2 attempts, poi inconcluso.
        marker = self.checkpoint.get("unlock_cpu_validate_marker")
        if marker and self._marker_stale(marker):
            self.logger.error(
                "unlock: HANG durante la validazione CPU (marcatore "
                "stale) — verdetto never_unlock, revert a 6 core")
            cpu_out["hang"] = True
            self._cpu_condemn_revert(cpu_out, cause="hang", phase_data=data)
            self.results["unlock_validation"] = data
            return data
        if marker:
            attempts = int(self.checkpoint.get(
                "unlock_cpu_validate_attempts", 0) or 0)
            if attempts >= 2:
                self.logger.warning(
                    "Validazione CPU interrotta %d volte (stesso boot) — "
                    "inconcluso: revert senza condanna", attempts)
                cpu_out["inconclusive"] = True
                cpu_out["cause"] = "retry_exhausted"
                self._cpu_clear_marker()
                self._cpu_revert_and_reboot(cpu_out, condemn=False,
                                            phase_data=data)
                self.results["unlock_validation"] = data
                return data
            if not self.mock and not self.dry_run:
                self.checkpoint.set("unlock_cpu_validate_attempts",
                                    attempts + 1)
            self._cpu_clear_marker()
            self.logger.warning(
                "Marcatore CPU fresco (processo interrotto, stesso boot) "
                "— ri-esecuzione della validazione")

        # 5. Esegui la validazione (marcatore scritto PRIMA, pattern D7)
        self._cpu_write_marker()
        val = self.cpu_validation.run(
            duration_s=self.config.validation_unlock_cpu_seconds)
        outcome = val.get("outcome")
        cause = val.get("cause")
        cpu_out.update({k: v for k, v in val.items()
                        if k in ("outcome", "cause", "temp_max",
                                 "whea_delta", "threads")})
        self._cpu_clear_marker()
        if not self.mock and not self.dry_run:
            self.checkpoint.set("unlock_cpu_validate_attempts", 0)

        if outcome == "pass":
            self.logger.info(
                "unlock: CPU validata (%ds: temp_max=%s whea=%s) — 8 core "
                "tenuti", self.config.validation_unlock_cpu_seconds,
                cpu_out.get("temp_max"), cpu_out.get("whea_delta", 0))
            self.results["unlock_validation"] = data
            return data
        if outcome == "fail":
            self.logger.warning(
                "unlock: CPU NON validata (cause=%s) — revert a 6 core + "
                "verdetto never_unlock", cause)
            self._cpu_condemn_revert(cpu_out, cause=cause or "stress",
                                     phase_data=data)
            self.results["unlock_validation"] = data
            return data
        # inconcluso (termico HARD / tool assente): revert SENZA condanna
        self.logger.warning(
            "unlock: validazione CPU non conclusa (%s) — unlock annullato "
            "senza condanna, riprovare a macchina fredda", cause)
        cpu_out["inconclusive"] = True
        cpu_out["cause"] = cause
        self.results["notes"].append(
            f"Validazione post-unlock CPU non conclusa ({cause}): unlock "
            "annullato (6 core) — " + (
                "installare stress-ng/taskset e riprovare"
                if cause == "tool_missing"
                else "riprovare a macchina fredda"))
        self._cpu_revert_and_reboot(cpu_out, condemn=False, phase_data=data)
        self.results["unlock_validation"] = data
        return data

    # -------------------- CPU: marker / revert / gate ---------------- #

    def _marker_stale(self, marker: Dict[str, Any]) -> bool:
        """True se il marcatore è di un boot precedente (hang)."""
        started = marker.get("started_epoch")
        try:
            from .oc.smoke import boot_epoch
            boot = boot_epoch()
        except Exception:
            boot = None
        if not started or not boot:
            return False
        return int(started) < boot

    def _cpu_write_marker(self) -> None:
        if not self.mock and not self.dry_run:
            self.checkpoint.set("unlock_cpu_validate_marker",
                                {"started_epoch": int(time.time())})

    def _cpu_clear_marker(self) -> None:
        if not self.mock and not self.dry_run:
            self.checkpoint.set("unlock_cpu_validate_marker", None)

    def _cpu_online_threads(self) -> Optional[int]:
        """Thread online reali (mock: cores*2; None = illeggibile)."""
        if self.mock and self.hardware is not None:
            return int(self.hardware.state.cpu_cores or 0) * 2
        if self.mock:
            return None
        from .unlock.validation import cpu_online_count
        return cpu_online_count()

    def _cpu_read_mask(self) -> Optional[int]:
        """Lettura maschera core (SMN) con governor FERMO (M4: regola
        assoluta AGENTS — MAI SMU con governor attivo) e try/except:
        None se illeggibile (MAI assumere stock da una lettura fallita).
        """
        if self.mock:
            try:
                return self.cpu_unlock.read_core_mask()
            except Exception:
                return None
        try:
            with self._governor_paused():
                return self.cpu_unlock.read_core_mask()
        except RuntimeError:
            raise  # governor non confermato fermo: abort, mai SMN
        except Exception:
            return None

    @contextmanager
    def _governor_paused(self):
        """Governor FERMO durante un accesso SMU/SMN (regola assoluta
        AGENTS: accessi concorrenti = freeze SoC silenzioso). Fail-closed:
        se lo stato FERMO non è CONFERMATO → RuntimeError (abort
        dell'accesso, mai procedere). In mock è un no-op."""
        if self.mock:
            yield
            return
        was_active: Optional[bool] = None
        try:
            was_active = bool(self.governor.is_running())
        except Exception:
            was_active = None
        if was_active is None:
            raise RuntimeError(
                "Stato del governor non determinabile — accesso SMU "
                "annullato (mai SMU con governor attivo: freeze SoC). "
                "Verificare cyan-skillfish-governor-smu e riprovare.")
        if was_active:
            try:
                stopped = self.governor.stop()
            except Exception:
                stopped = False
            if not stopped:
                raise RuntimeError(
                    "Governor non confermato FERMO — accesso SMU annullato "
                    "(mai SMU con governor attivo: freeze SoC). Fermare "
                    "cyan-skillfish-governor-smu e riprovare.")
        try:
            yield
        finally:
            if was_active:
                try:
                    self.governor.start()
                except Exception:
                    self.logger.warning(
                        "Riavvio del governor fallito dopo l'accesso SMU")

    def _cpu_revert_if_condemned(self, cpu_out: Dict[str, Any],
                                 phase_data: Optional[Dict[str, Any]] = None
                                 ) -> None:
        """M1: verdetto never_unlock presente MA maschera non stock (kill
        tra verdetto e scrittura 0x77, o revert fallito ignorato — la
        maschera sopravvive ai WARM reboot) → revert OBBLIGATO prima di
        proseguire (D5: mai 16T su silicio condannato). Verdetto GIÀ
        scritto → niente ri-condanna né ri-disable. Scrittura impossibile
        → interruzione controllata (power-off)."""
        mask = self._cpu_read_mask()
        if mask is not None and (mask & 0xFF) == CORE_MASK_STOCK:
            return  # già a stock: niente da fare
        self.logger.warning(
            "CPU: silicio condannato ma maschera %s (non stock) — revert "
            "a 6 core prima di proseguire",
            hex(mask & 0xFF) if mask is not None else "illeggibile")
        cpu_out["reverted"] = True
        self._cpu_revert_and_reboot(cpu_out, condemn=False,
                                    phase_data=phase_data)

    def _cpu_condemn_revert(self, cpu_out: Dict[str, Any],
                            cause: str,
                            phase_data: Optional[Dict[str, Any]] = None
                            ) -> None:
        """Condanna + revert (D5/D6): verdetto never_unlock scritto PRIMA
        (durevole), disable auto-unlock di boot, poi revert maschera 0x77;
        scrittura fallita → interruzione controllata (power-off)."""
        cpu_out["outcome"] = "fail"
        cpu_out["reverted"] = True
        cpu_out["cause"] = cause
        threads = self._cpu_online_threads() or 16
        mask_at = self._cpu_read_mask()  # M4: governor FERMO + try/except
        mask_hex = hex(mask_at & 0xFF) if mask_at is not None else "?"
        self.unlock_verdict.set(
            "cpu", "never_unlock",
            evidence(cause=cause, threads=threads,
                     mask_at_test=mask_hex))
        # interno no-op in mock/dry-run (mai systemctl reali)
        self._disable_core_unlock_boot()
        self._cpu_revert_and_reboot(cpu_out, condemn=True,
                                    phase_data=phase_data)

    def _cpu_revert_and_reboot(self, cpu_out: Dict[str, Any],
                               condemn: bool,
                               phase_data: Optional[Dict[str, Any]] = None
                               ) -> None:
        """Revert a 0x77 (governor FERMO confermato) + warm reboot (12T).

        Regola assoluta SMU (AGENTS): MAI accessi SMU/SMN con il governor
        attivo → se risulta attivo viene fermato e lo stato FERMO va
        CONFERMATO; stop non confermato/indeterminabile → abort del
        revert (mai scrivere SMN a governor attivo: freeze SoC, M4).
        Scrittura/readback falliti (sul campo le scritture host alla
        core mask sono DROPPATE) → NIENTE reboot automatico: interruzione
        controllata con istruzione power-off (D5) — mai riavviare con la
        maschera ancora 0xFF su core sospetti (boot-loop manuale).
        Se phase_data è dato (fase unlock_validate), la fase viene
        persistita PRIMA del reboot (sys.exit): al resume il riepilogo
        mostra l'esito reale e il run riparte da fix (NIT n4).
        """
        with self._governor_paused():
            rev = self.cpu_unlock.revert_to_stock()
        if not rev.get("reverted"):
            self.logger.error(
                "unlock: revert CPU IMPOSSIBILE (%s) — NIENTE reboot. "
                "Eseguire il POWER-OFF della macchina (il cold boot "
                "ripristina 6 core da solo), poi rilanciare `buo "
                "unleash`.", rev.get("error") or "scrittura SMN fallita")
            cpu_out["revert_failed"] = True
            self.results["notes"].append(
                "Revert CPU impossibile in software (scrittura maschera "
                "0x77 non confermata): POWER-OFF richiesto — il cold boot "
                "ripristina 6 core.")
            raise RuntimeError(
                "Revert CPU impossibile in software — POWER-OFF richiesto "
                "(il cold boot ripristina 6 core da solo). "
                + ("Verdetto salvato; auto-unlock di boot disabilitato."
                   if condemn else "Nessun verdetto (esito non concluso)."))
        cpu_out["mask"] = rev.get("mask")
        self.logger.info(
            "unlock: revert CPU: maschera 0x77 scritta e verificata — "
            "reboot per 12 thread")
        if phase_data is not None and not self.mock and not self.dry_run:
            self.checkpoint.set_phase("unlock_validate", phase_data,
                                      completed=True)
            self.checkpoint.set_current_phase("fix")
        self._schedule_reboot("CPU revert a 6 core — reboot richiesto")

    def _disable_core_unlock_boot(self) -> None:
        """Disabilita l'auto-unlock al boot (bc250-core-unlock.service,
        touchpoint esterno D6). Best-effort: solo run reali, fail-soft —
        il verdetto è la protezione principale."""
        if self.mock or self.dry_run:
            return
        try:
            from .utils.shell import run_command
            rc, _, err = run_command(
                ["systemctl", "disable", "bc250-core-unlock.service"],
                sudo=True, check=False)
        except Exception as e:
            rc, err = 1, str(e)
        if rc != 0:
            self.logger.warning(
                "Disabilitazione bc250-core-unlock.service fallita (%s) — "
                "rimuoverla manualmente per evitare il re-unlock al boot "
                "del silicio condannato", err or rc)

    # --------------------- GPU: D8/D9 + validazione ------------------ #

    def _gpu_validation_needed(self) -> str:
        """D8: 'certified' (evidenza preesistente: results.tsv completo o
        verdetto stable_short) | 'partial' (maratona per-WGP in corso:
        non interferire) | 'needed' (results.tsv assente, nessun
        verdetto → validazione short)."""
        if self.unlock_verdict.get("gpu") == "stable_short":
            return "certified"
        try:
            health = self.health_test.read_results()
        except Exception:
            health = {}
        if health.get("complete"):
            return "certified"
        if health.get("present"):
            return "partial"
        return "needed"

    def _gpu_post_enable(self, results: Dict[str, Any],
                         gpu: Dict[str, Any]) -> None:
        """Dopo un enable_all appena eseguito: validazione short (D8) +
        persistenza solo su silicio validato (D9)."""
        decision = self._gpu_validation_needed()
        if decision == "partial":
            self.logger.warning(
                "persistenza 40-CU SALTATA: results.tsv parziale "
                "(maratona per-WGP in corso) — la validazione short non "
                "interferisce (evidenza definitiva = protocollo per-WGP)")
            self.results["notes"].append(
                "Persistenza 40-CU saltata: results.tsv parziale "
                "(maratona per-WGP in corso)")
            return
        if decision == "certified":
            # Evidenza preesistente (results.tsv completo / stable_short)
            self._suggest_40cu_persistence(results, gpu)
            return
        self._gpu_validate(results, gpu, already_active=False)

    def _gpu_already_active(self, results: Dict[str, Any],
                            gpu: Dict[str, Any]) -> None:
        """40-CU già attive (persistenza legacy/boot service): certificate
        (D8) → salto; NON certificate e results.tsv assente → validazione
        sullo stato corrente; fail → stock dispatch + disable persistenza.
        """
        decision = self._gpu_validation_needed()
        if decision in ("certified", "partial"):
            self.logger.info("GPU: 40-CU già attive — salto")
            return
        self.logger.warning(
            "GPU: 40 CU attive ma non certificate — validazione short "
            "(vkmark) sullo stato corrente")
        self._gpu_validate(results, gpu, already_active=True)

    def _gpu_validate(self, results: Dict[str, Any],
                      gpu: Dict[str, Any],
                      already_active: bool = False) -> None:
        """Validazione short GPU (D8): vkmark duration=unlock_gpu_seconds
        con sampling temp 1s + dmesg WHEA/fault. Tri-state (D4); MAI
        SafetyViolation (D10). Pass → verdetto stable_short + persistenza;
        fail → stock dispatch + never_enable_all + disable persistenza;
        inconcluso (termico/display) → stock dispatch + NESSUN verdetto +
        ricetta; inconcluso tool_missing (vkmark assente) → NESSUN stock
        dispatch: le 40-CU restano ATTIVE VOLATILI (decisione utente
        05/09: il volatile torna da solo a 24 al reboot, il fail-closed
        peggiorerebbe l'esito di un silicio sano)."""
        duration = self.config.validation_unlock_gpu_seconds
        if not self.config.probe_unlock_validate:
            # m2: probe.unlock_validate è l'interruttore master della
            # validazione post-unlock (CPU E GPU) — mai girare vkmark
            self.logger.info(
                "   validazione GPU DISABILITATA (probe."
                "unlock_validate=false)")
            return
        if duration <= 0:
            self.logger.info("   validazione GPU saltata (0 secondi, config)")
            return
        marker_key = "unlock_gpu_validate_marker"
        if not self.mock and not self.dry_run:
            self.checkpoint.set(marker_key,
                                {"started_epoch": int(time.time())})
        try:
            val = self.gpu_validation.run(duration_s=duration)
        finally:
            if not self.mock and not self.dry_run:
                self.checkpoint.set(marker_key, None)
        results["gpu"]["validation"] = val
        outcome = val.get("outcome")
        cause = val.get("cause")

        if outcome == "pass":
            self.logger.info(
                "unlock: GPU validata (vkmark %ds: temp_max=%s whea=%s "
                "fault=%s) — verdetto stable_short", duration,
                val.get("temp_max"), val.get("whea_delta", 0),
                val.get("gpu_faults", 0))
            self.unlock_verdict.set(
                "gpu", "stable_short",
                evidence(cause=None, tool=val.get("tool") or "vkmark",
                         seconds=duration, temp_max=val.get("temp_max")))
            if not already_active:
                self._suggest_40cu_persistence(results, gpu)
            return

        if outcome == "fail":
            self.logger.warning(
                "unlock: GPU NON validata (cause=%s) — stock dispatch + "
                "persistenza disattivata", cause)
            # Verdetto durevole PRIMA (se il processo muore a metà
            # rollback, il gate del prossimo run copre lo stato)
            self.unlock_verdict.set(
                "gpu", "never_enable_all",
                evidence(cause=cause, tool=val.get("tool") or "vkmark",
                         seconds=duration, temp_max=val.get("temp_max")))
            # interno no-op in mock/dry-run (mai systemctl reali)
            self._disable_40cu_persistence()
            self.results["notes"].append(
                f"40-CU NON validata (cause={cause}): tornate a 24 CU "
                "(stock dispatch) — verdetto never_enable_all scritto")
            self._gpu_stock_dispatch(results)
            return

        # Inconcluso — NESSUN verdetto durevole. Decisione utente
        # (05/09): la causa tool_missing (vkmark non installato) è un
        # problema AMBIENTALE, non evidenza di CU difettose → NIENTE
        # stock dispatch: le 40-CU restano ATTIVE VOLATILI (tornano da
        # sole a 24 al reboot), nessuna certificazione/persistenza. Il
        # fail-closed totale (stock a 24 CU) peggiorerebbe l'esito di un
        # silicio sano senza beneficio di sicurezza.
        if cause == "tool_missing":
            self.logger.warning(
                "unlock: validazione GPU non possibile (vkmark assente) "
                "— 40 CU lasciate ATTIVE VOLATILI (nessuna "
                "certificazione/persistenza), nessun verdetto")
            self.results["notes"].append(
                "Validazione GPU non possibile (vkmark assente): 40 CU "
                "lasciate ATTIVE VOLATILI (nessuna certificazione/"
                "persistenza) — installa vkmark per la validazione")
            return

        # Altro inconcluso (termico HARD / tool fallito a runtime):
        # stock dispatch come prima (D4: torna a stock senza condanna).
        self.logger.warning(
            "unlock: validazione GPU non conclusa (%s) — stock "
            "dispatch, nessun verdetto", cause)
        self.results["notes"].append(
            f"Validazione GPU non conclusa ({cause}): 40-CU tornate a "
            "24 CU — " + (
                "riprovare a macchina fredda"
                if cause == "thermal"
                else "installare vkmark (radv) e riprovare"))
        self._gpu_stock_dispatch(results)

    def _gpu_stock_dispatch(self, results: Dict[str, Any]) -> None:
        """Revert GPU a stock (stock_dispatch, UMR volatile): best-effort."""
        try:
            ok = bool(self.gpu_unlock.rollback())
        except Exception as e:
            self.logger.warning("Stock dispatch GPU fallito: %s", e)
            ok = False
        results["gpu"]["rollback"] = ok
        if not ok:
            self.results["notes"].append(
                "Stock dispatch GPU non riuscito — 40-CU ancora attive, "
                "verificare manualmente")

    def _disable_40cu_persistence(self) -> None:
        """Disattiva la persistenza 40-CU al boot (bc250-cu-live-manager,
        D5 GPU). Best-effort/fail-soft: su fallimento → nota per la
        rimozione manuale della conf."""
        if self.mock or self.dry_run:
            return
        try:
            from .utils.shell import run_command
            rc, _, err = run_command(
                ["systemctl", "disable", "--now",
                 "bc250-cu-live-manager.service"],
                sudo=True, check=False)
        except Exception as e:
            rc, err = 1, str(e)
        if rc != 0:
            self.logger.warning(
                "Disabilitazione bc250-cu-live-manager fallita (%s) — "
                "rimuovere manualmente la conf "
                "/etc/bc250-cu-live-manager.conf", err or rc)

    def _suggest_40cu_persistence(self, results: Dict[str, Any],
                                  gpu: Dict[str, Any]) -> None:
        """Persistenza 40-CU al boot (auto nei run NON interattivi).

        Il runtime UMR è VOLATILE: al reboot le 40 CU tornano a 24. La
        persistenza (install-service + write-service-table) è validata
        sul campo e stabile, ma richiede un reboot per l'attivazione.
        • GATE D9 (design POSTUNLOCK_VALIDATION): si persiste SOLO su
          silicio validato — (a) validazione short appena passata
          (results.gpu.validation), (b) results.tsv completo (per-WGP),
          (c) verdetto GPU stable_short preesistente. Altrimenti skip
          con log+nota: persistere al boot 40-CU non certificate
          renderebbe PERMANENTE un difetto.
        • run reale NON interattivo: persistenza AUTOMATICA — su
          fallimento resta un warning, la run NON si blocca;
        • interattivo: BUO chiede conferma;
        • mock/dry-run: nessuna chiamata reale, solo la nota.
        """
        if gpu.get("method") != "runtime_umr":
            return  # kernel patch: la persistenza è nel modulo, non serve
        # D9: gate silicio validato (una sola verifica per tutti i rami)
        validation = (results.get("gpu") or {}).get("validation") or {}
        certified = (
            validation.get("outcome") == "pass"
            or self._gpu_validation_needed() == "certified"
        )
        if not certified:
            self.logger.warning(
                "persistenza 40-CU SALTATA: silicio non validato "
                "(results.tsv assente, nessun verdetto stable_short)")
            self.results["notes"].append(
                "Persistenza 40-CU saltata: silicio non validato — "
                "eseguire il protocollo per-WGP (bc250-cu-health-test.sh "
                "start) o la validazione short")
            results["gpu"]["persistence"] = {
                "suggested": False, "reason": "silicon_not_validated"}
            return
        auto = not self.mock and not self.dry_run and not self.interactive
        if auto:
            self.logger.info(
                "40 CU attive (runtime UMR): persistenza automatica al boot")
        else:
            self.logger.warning(
                "40 CU attive ma VOLATILI: al prossimo reboot tornano a 24. "
                "Persistenza validata (install-service + "
                "write-service-table).")
        if self.mock or self.dry_run:
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
        if self.interactive:
            try:
                resp = input(
                    "   Rendere persistenti le 40 CU al boot? [y/N] "
                ).strip().lower()
            except EOFError:
                resp = "n"
            if resp not in ("y", "yes"):
                self.logger.info("Persistenza 40-CU annullata (resterà volatile)")
                results["gpu"]["persistence"] = {"suggested": True,
                                                 "applied": False}
                return
        p = self.gpu_unlock.persist()
        results["gpu"]["persistence"] = p
        if p.get("persisted"):
            self.logger.info(
                "40 CU persistenti al boot (attive al prossimo reboot)")
        else:
            self.logger.warning("Persistenza non riuscita: %s",
                                p.get("error") or "errore sconosciuto")

    def _phase_fix(self) -> Dict[str, Any]:
        """FASE 1b — FIX: TLB, ACE, IOMMU, ACPI, VRAM, GTT, ventole.

        ANTI-LOOP: ogni fix APPLICATO è registrato nel ledger
        `applied_steps` PRIMA del reboot; al resume i fix già eseguiti
        vengono saltati e per ogni rientro di fase scatta AL MASSIMO UN
        reboot. Senza questo, un fix che richiede reboot faceva rientrare
        la fase all'infinito (bug trovato sul campo: loop di riavvii).
        NOTA F-B: un fix SOLO VERIFICATO (già attivo, NON applicato da
        questo run) NON entra nel ledger: al resume viene ri-verificato e
        saltato di nuovo, e il rollback non deve annullare modifiche
        pre-esistenti che il run non ha mai fatto.
        """
        self.logger.info("Fix di sistema")
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
                    # F-B: fix già attivo → NON nel ledger (non è una
                    # modifica di QUESTO run): il rollback non deve
                    # annullarlo. Al resume viene ri-verificato (verify è
                    # idempotente e senza effetti collaterali) e saltato.
                    self.logger.info("Fix %s: già attivo — salto", name)
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
                "Fix NON applicati automaticamente: %s", ", ".join(labels))

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

    def _gpu_sweep_params(self) -> Dict[str, Any]:
        """Opzioni della ricerca per-silicio GPU (design GPU_UV §6) da
        passare a uv_gpu.optimize(sweep=...)."""
        return {
            "enabled": self.config.undervolt_gpu_sweep_enabled,
            "freqs": list(self.config.undervolt_gpu_sweep_freqs),
            "step_mv": self.config.undervolt_gpu_sweep_step_mv,
            "floor_mv": self.config.undervolt_gpu_sweep_floor_mv,
            "max_steps": self.config.undervolt_gpu_sweep_max_steps,
            "test_seconds": self.config.undervolt_gpu_sweep_test_seconds,
            "confirm_seconds": self.config.undervolt_gpu_sweep_confirm_seconds,
            "max_minutes": self.config.undervolt_gpu_sweep_max_minutes,
        }
        # N.B. nessun "temp_gate": il gate termico dei probe È l'HARD
        # (politica a due livelli 03/09, LIMITS.gpu.temp_max in gpu.py).

    def _read_stock_vid(self) -> Optional[int]:
        """Misura del VID stock (mV) per cpu_target_vid=auto.

        Mock/dry-run: stato hardware simulato. Reale: reader SMU
        (bc250_smu, Q3/0x36) — in _phase_optimize il governor è GIÀ
        fermo, quindi il gate SMU↔governor del reader è superato.
        Non leggibile → None (fallback statico nel consumatore); C1:
        mai un VID inventato.
        """
        if self.hardware is not None:
            return self.hardware.get_cpu_vid()
        from .safety.reader import RealHardwareReader
        return RealHardwareReader().get_cpu_vid()

    def _optimize_cpu_uv(self) -> Dict[str, Any]:
        """Ricerca UV CPU (design DESIGN_PORTABILITY_DEFAULTS §3.1-3.2).

        max_freq = min(cpu_freq_max, cpu_search_freq): la ricerca parte
        dalla frequenza STOCK (default 3500), non dal soffitto 4000.

        cpu_target_vid NUMERICO (file esplicito) → comportamento odierno
        (ConfigurationError se bc250-detect non trova nulla). "auto"
        (default): target = clamp(misura VID stock − 75, 900, 1250),
        ladder di retry +50 fino alla misura stock; ladder esaurita →
        fallback no-UV (curva stock: nessun punto applicato) con WARNING
        — MAI un abort di run su macchina sana. Misura non disponibile →
        fallback statico 1000 mV + ladder (stessa robustezza).
        """
        search_freq = min(self.config.cpu_freq_max,
                          self.config.undervolt_cpu_search_freq)
        target_vid = self.config.undervolt_cpu_target_vid
        if target_vid != "auto":
            return self.uv_cpu.optimize(max_freq=search_freq,
                                        max_vid=target_vid)

        measure = self._read_stock_vid()
        attempt = resolve_cpu_target_vid(measure)
        ceiling = (measure if measure is not None
                   else LIMITS.cpu.vid_recommended_max)
        last_error = ""
        while True:
            try:
                return self.uv_cpu.optimize(max_freq=search_freq,
                                            max_vid=attempt)
            except ConfigurationError as e:
                last_error = str(e)
                if attempt >= ceiling:
                    break
                attempt = min(attempt + 50, ceiling)
        # Fallback no-UV: il run continua a curva stock (apply con punti
        # vuoti = no-op) — degradazione con nota, mai fail-opaco.
        self.logger.warning(
            "Undervolt CPU non trovato (ultimo tentativo %d mV: %s) — "
            "curva STOCK, nessuna modifica applicata", attempt, last_error)
        self.results["notes"].append(
            "Undervolt CPU non trovato: curva stock (nessuna modifica) — "
            f"ultimo errore: {last_error[:200]}")
        return {"v_f_points": [], "best_efficiency": None,
                "source": "no-uv", "reason": last_error[:300]}

    def _phase_optimize(self) -> Dict[str, Any]:
        """FASE 2 — OTTIMIZZAZIONE: undervolt + overclock power-limited."""
        self.logger.info("Ottimizzazione — undervolt e overclock")
        results: Dict[str, Any] = {}

        # Il governor va fermato durante i test
        if not self.mock:
            self.governor.stop()

        # CPU undervolt: ricerca a `cpu_search_freq` (default 3500 stock)
        # — mai parte da cpu_freq_max 4000 (il punto trovato È la
        # frequenza applicata; f-alta + deep-UV = zona wedge/hang);
        # target numerico esplicito o "auto" con ladder/fallback no-UV
        # (design DESIGN_PORTABILITY_DEFAULTS 3.1-3.2).
        uv_cpu = self._optimize_cpu_uv()
        results["undervolt_cpu"] = uv_cpu

        # GPU undervolt
        sweep = self._gpu_sweep_params()
        if sweep["enabled"] and not self.mock:
            # Budget comunicato PRIMA dello sweep (design §8)
            n_freq = len([f for f in sweep["freqs"]
                          if f >= self.config.undervolt_gpu_start_freq
                          and f <= self.config.gpu_freq_max])
            if n_freq > 0:
                est_s = (n_freq
                         * (sweep["max_steps"]
                            * (sweep["test_seconds"] + 5)
                            + sweep["confirm_seconds"]))
                self.logger.info(
                    "Sweep GPU per-silicio: %d freq × fino a %d candidati × "
                    "%ds (+ conferma %ds per freq) + ciclo governor ~5s/"
                    "candidato → stimato ~%d min (tetto wall-clock %d min)",
                    n_freq, sweep["max_steps"], sweep["test_seconds"],
                    sweep["confirm_seconds"], (est_s + 59) // 60,
                    sweep["max_minutes"])
        uv_gpu = self.uv_gpu.optimize(
            start_freq=self.config.undervolt_gpu_start_freq,
            max_voltage=self.config.gpu_voltage_recommended_max,
            sweep=sweep,
            power_budget=self.config.power_budget,
            monitor=self.safety_monitor,
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
            self.logger.info("Log undervolt scritto: %s", path)
        except Exception as e:
            self.logger.warning("Scrittura undervolt log fallita: %s", e)

    def _phase_apply(self) -> Dict[str, Any]:
        """Applica la configurazione finale (governor + overclock)."""
        self.logger.info("Applicazione della configurazione finale")
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
                ok = self.governor.write_config(
                    safe_points, max_freq=self.config.gpu_freq_max)
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
                persist=self.config.undervolt_persist,
            )

        return results

    def _apply_cpu_config(self, freq: int, scale: Optional[int] = None,
                          vid: Optional[int] = None,
                          persist: bool = False) -> Dict[str, Any]:
        """Applica il punto CPU (undervolt validato).

        Scrive overclock.conf e lo applica con `bc250-apply --apply`
        (VOLATILE). Con persist=True esegue anche `bc250-apply --install`
        (G3): il profilo viene applicato automaticamente a ogni boot —
        è ciò che fa sopravvivere l'undervolt a un riavvio/format.
        L'upstream crea l'unità SENZA abilitarla (BUG F-D): BUO esegue
        `systemctl enable` esplicito, altrimenti l'undervolt non torna
        al boot.
        Fail-closed ma NON bloccante: se lo script manca o fallisce
        (install o enable), logga e continua (l'undervolt è un guadagno,
        non un requisito di sicurezza).
        """
        if self.dry_run:
            self.logger.info("CPU config: [DRY-RUN] simulata")
            return {"applied": True, "dry_run": True, "freq": freq}
        if self.mock:
            self._mark_step("cpu_overclock")
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
                    # Politica a due livelli (03/09): il max_temperature
                    # del conf SMU è il TARGET OPERATIVO applicato
                    # (temp_apply, sotto l'HARD di abort temp_max): lo
                    # SMU throttla al livello 2, BUO aborta solo se il
                    # livello 1 (HARD) viene superato.
                    f"scale = {s}\n"
                    f"max_temperature = {LIMITS.cpu.temp_apply}\n",
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
            # Ledger (bug di sicurezza 03/09): la config CPU è stata
            # APPLICATA allo SMU volatile — va tracciata come cpu_overclock
            # (livello di rollback registrato → bc250-apply --uninstall).
            # Senza, il rollback post-abort (filtrato sul ledger) la
            # saltava e la macchina restava con un OC/UV non validato
            # applicato (es. 91°C sostenuti fino al reboot manuale).
            self._mark_step("cpu_overclock")
            out: Dict[str, Any] = {
                "applied": True, "freq": f, "scale": s,
                "method": "bc250-apply (volatile)",
            }
            if persist:
                from .utils.shell import run_command
                inst = w.install(str(conf))
                if inst.get("returncode") == 0:
                    # BUG F-D: `--install` crea l'unità systemd ma NON la
                    # abilita → senza `systemctl enable` l'undervolt non
                    # viene riapplicato al boot (verificato sul campo).
                    en_rc, _, en_err = run_command(
                        ["systemctl", "enable", SMU_OC_SERVICE],
                        sudo=True, check=False)
                    if en_rc == 0:
                        out["persistent"] = True
                        out["method"] = "bc250-apply --apply + --install"
                        self.logger.info(
                            "Undervolt persistente installato: %d MHz, "
                            "scale %d — riapplicato a ogni boot", f, s)
                    else:
                        out["persistent"] = False
                        out["persist_error"] = (
                            f"systemctl enable {SMU_OC_SERVICE} fallito: "
                            + (en_err or "errore sconosciuto"))[:200]
                        self.logger.warning(
                            "Persistenza undervolt NON riuscita "
                            "(unità non abilitata al boot): %s",
                            out["persist_error"])
                else:
                    out["persistent"] = False
                    out["persist_error"] = (
                        (inst.get("stderr") or "install fallito")[:200])
                    self.logger.warning(
                        "Persistenza undervolt NON riuscita: %s",
                        out["persist_error"])
            self.logger.info("CPU config applicata: %d MHz, scale %d", f, s)
            return out
        except Exception as e:
            self.logger.warning("CPU config non applicata: %s", e)
            return {"applied": False, "error": str(e)[:200]}

    @staticmethod
    def _clamp_cpu(freq: int, scale: Optional[int] = None,
                   vid: Optional[int] = None):
        """Clamp della coppia frequenza/scale ai limiti immutabili.

        Bounds scale VERIFICATI nel sorgente community (bc250_limits.py:
        scale_min=-50, scale_max=0; bc250_detect.smu_apply RIFIUTA scale>0).
        Una scale POSITIVA chiederebbe un overvolt a un'SMU "with minimal
        validity checking" → mai ammessa. Scale 0 = curva stock; negativa =
        undervolt vero. Il fallback vid→scale della community produce valori
        positivi per l'undervolt: incoerente coi bounds → clampato a 0
        (curva stock, MAI overvolt); il path reale passa scale da bc250-detect.
        """
        f = max(LIMITS.cpu.freq_min, min(LIMITS.cpu.freq_max, int(freq)))
        s = 0
        if scale is not None:
            s = max(-50, min(0, int(scale)))
        elif vid is not None:
            s = max(-50, min(0, round((1206 - int(vid)) / 8)))
        return f, s

    def _phase_validate(self) -> Dict[str, Any]:
        """FASE 3 — VALIDAZIONE: stress test, verifica fix, benchmark after."""
        self.logger.info("Validazione — stress test e verifica fix")
        results: Dict[str, Any] = {}

        # FIX (30/08): il restore con stress saltato resta saltato anche al
        # resume — il marcatore persistente (scritto dal restore quando la
        # CLI ha impostato durata 0) prevale sulla config ricaricata dal
        # nuovo processo: durata 0 = skip VERO (fix 5ff85f3, nessuno
        # spawn). Il marcatore viene pulito a finalize / nei run nuovi.
        stress_duration = self.config.validation_stress_duration
        if self.checkpoint.get("validation_stress_skip"):
            stress_duration = 0
            self.logger.info("   Stress test saltato (restore)")

        # Stress test (in dry-run viene solo simulato: niente 30 min reali)
        if self.dry_run:
            stress = {
                "passed": True, "simulated": True,
                "duration_minutes": stress_duration,
                "cpu_temp_max": None, "gpu_temp_max": None,
                "power_max": None, "errors": 0,
            }
            self.logger.info("   [DRY-RUN] stress test simulato")
        else:
            stress = self.stress.run(
                duration_minutes=stress_duration,
                power_budget=self.config.power_budget,
                scope=self.config.validation_stress_scope,
            )
        results["stress"] = stress

        # Verifica dei fix applicati
        verification = self.verifier.verify_all(self.results["applied_fixes"])
        results["fix_verification"] = verification
        self.results["fixes"] = verification

        # Benchmark after (in dry-run il runner è in modalità mock,
        # quindi viene simulato come il benchmark BEFORE e lo stress test)
        if self.config.benchmark_enabled:
            self.logger.info("Benchmark dopo (config applicata)…")
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

        # G2: dopo un run reale completo, auto-esporta il profilo macchina
        # (così `buo restore` dopo un format trova lo stato più recente).
        # La riga di log è PULITA (il percorso del file): il dump del dict
        # intero su una riga INFO rendeva il log/viewer illeggibile
        # (feedback utente 04/09); i dati strutturati stanno nel file e il
        # riepilogo umano è già stampato da _finalize.
        if not self.dry_run and not self.mock:
            try:
                from .profile import export_profile, default_profile_path
                export_profile()
                self.logger.info("Profilo macchina salvato: %s",
                                 default_profile_path())
            except Exception as e:
                self.logger.warning("Export profilo fallito: %s", e)

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

        if self.dry_run:
            # m2: MAI sovrascrivere l'ultimo report REALE con un dry-run:
            # si scrive con suffisso .dry-run (report.md.dry-run / .json)
            from .utils.paths import report_file_json, report_file_md
            self.report.output_md = report_file_md().with_name(
                report_file_md().name + ".dry-run")
            self.report.output_json = report_file_json().with_name(
                report_file_json().name + ".dry-run")

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
            self.logger.info("Fase richiesta completata")
        else:
            self.logger.info("OTTIMIZZAZIONE COMPLETATA")
            # Riepilogo finale (§3 spec UX_REVAMP_CLI): le stesse righe
            # del pannello CLI — una sola fonte (riepilogo_lines).
            for line in self.riepilogo_lines():
                self.logger.info(line)
        if not self.dry_run:
            self.checkpoint.set_phase("complete", {"done": True}, completed=True)
            # F-A: a ciclo completato il marcatore restore va RIMOSSO,
            # altrimenti un unleash successivo erediterebbe la modalità
            # restore (optimize restituirebbe i dati seedati senza girare
            # l'auto-tuning).
            self.checkpoint.set("restore_active", False)
            # FIX (30/08): a ciclo completato anche il marcatore di stress
            # saltato va rimosso, altrimenti un unleash successivo
            # erediterebbe lo skip della validate.
            self.checkpoint.set("validation_stress_skip", False)
            # F-C: a ciclo completato anche il marcatore di retry unlock va
            # pulito: l'unlock è stato ritentato (o saltato definitivamente)
            # e un run successivo non deve ereditare il retry.
            self.checkpoint.set("unlock_blocked_acpi", False)
            # Validazione post-unlock: marker/attempts puliti a ciclo
            # completato (stesso pattern degli altri marcatori transitori).
            self.checkpoint.set("unlock_cpu_validate_marker", None)
            self.checkpoint.set("unlock_gpu_validate_marker", None)
            self.checkpoint.set("unlock_cpu_validate_attempts", 0)
            # Cleanup anti-loop: a ciclo completato il servizio di ripresa
            # va rimosso, altrimenti al prossimo boot `buo resume` vede
            # "complete" → riparte da init → riesegue tutto → reboot → loop
            # (bug trovato sul campo: riavvii ripetuti a ogni accensione).
            from .state.reboot import RebootManager
            RebootManager().cleanup()
            # OSTREE: a ciclo completato il default di boot va ripristinato
            # se la run lo aveva cambiato (marker-guarded, no-op altrimenti).
            self._exit_ostree_cleanup()

    def _handle_safety_violation(self) -> None:
        self.logger.error("Esecuzione interrotta per safety violation")
        if self.safety_monitor is not None:
            self.safety_monitor.stop()
        if not self.dry_run:
            # F-A: un abort NON crea servizi di resume (a differenza del
            # reboot programmato): il marcatore restore deve essere pulito
            # qui, altrimenti un `buo unleash` successivo che riprende
            # dalla fase interrotta erediterebbe la modalità restore e
            # saltarebbe l'auto-tuning. Stesso pattern per lo stress
            # saltato: un restore abortito non deve lasciare la validate
            # saltata ai run successivi.
            self.checkpoint.set("restore_active", False)
            self.checkpoint.set("validation_stress_skip", False)
            self.rollback.rollback(reason=self.safety_reason,
                                   applied=self._applied_steps())
            from .state.reboot import RebootManager
            RebootManager().cleanup()
            self._exit_ostree_cleanup()
            # ABORT TERMINALE (bug 03/09): dopo rollback+cleanup lo stato
            # di run viene azzerato (stesso pattern del reset init di un
            # run nuovo) — né `buo unleash` né `buo resume` proseguono la
            # run appena fallita. La run interrotta da REBOOT (processo
            # morto, nessun handler) NON passa da qui e resta riprendibile.
            self.checkpoint.set_current_phase("init")
            self.checkpoint.set("applied_steps", [])
            self.checkpoint.set("reboot_count", 0)
            self.checkpoint.set("unlock_blocked_acpi", False)
            self.checkpoint.set("unlock_cpu_validate_marker", None)
            self.checkpoint.set("unlock_gpu_validate_marker", None)
            self.checkpoint.set("unlock_cpu_validate_attempts", 0)
        self.results["notes"].append(
            f"Safety violation: {self.safety_reason} — rollback eseguito")

    def _handle_error(self, phase: str, error: str) -> None:
        self.logger.error("Errore in fase %s: %s", phase, error)
        if self.safety_monitor is not None:
            self.safety_monitor.stop()
        if not self.dry_run:
            # F-A: come per l'abort di sicurezza — niente resume service,
            # il marcatore restore va pulito (vedi _handle_safety_violation).
            # Anche lo stress saltato: il run è fallito, non deve ereditare
            # lo skip della validate.
            self.checkpoint.set("restore_active", False)
            self.checkpoint.set("validation_stress_skip", False)
            self.rollback.rollback(from_phase=None,
                                   reason=f"errore in {phase}: {error}",
                                   applied=self._applied_steps())
            from .state.reboot import RebootManager
            RebootManager().cleanup()
            self._exit_ostree_cleanup()
            # ABORT TERMINALE: come per l'abort di sicurezza (vedi
            # _handle_safety_violation): la run fallita NON è riprendibile.
            self.checkpoint.set_current_phase("init")
            self.checkpoint.set("applied_steps", [])
            self.checkpoint.set("reboot_count", 0)
            self.checkpoint.set("unlock_blocked_acpi", False)
            self.checkpoint.set("unlock_cpu_validate_marker", None)
            self.checkpoint.set("unlock_gpu_validate_marker", None)
            self.checkpoint.set("unlock_cpu_validate_attempts", 0)
        self.results["notes"].append(f"Errore in {phase}: {error}")

    def _run_can_schedule_reboot(self, current: str) -> bool:
        """True se il segmento di fasi [current..stop_after/complete]
        include unlock, unlock_validate o fix (le uniche fasi che chiamano
        _schedule_reboot o la CU health test — la validazione post-unlock
        programma il reboot del revert CPU). In mock/dry-run sempre False:
        la run non può programmare reboot reali → niente attivazione
        ostree."""
        if self.dry_run or self.mock:
            return False
        end = self._stop_after or "complete"
        try:
            seg = PHASES[PHASES.index(current): PHASES.index(end) + 1]
        except ValueError:
            seg = [current]
        return any(p in seg for p in ("unlock", "unlock_validate", "fix"))

    def _ensure_ostree_default(self, current: str) -> bool:
        """Attivazione EAGER (D1) + fail-closed (design OSTREE_REBOOT).

        Se la run può programmare reboot ed è partita da un deployment
        ostree NON-default: imposta il default di boot sul deployment
        corrente (`rpm-ostree rollback`) PRIMA di qualunque modifica, così
        ogni reboot atterra qui. Ritorna False per ABORTIRE prima di
        toccare l'hardware (nessun marcatore residuo in caso di swap
        fallito). Inerte nei casi comuni: non-ostree, default booted,
        mock/dry-run, flag auto_swap_default=false (kill-switch D5)."""
        if not self._run_can_schedule_reboot(current):
            return True                      # run non reboot-capable: inerte
        state = self.ostree.detect_boot()    # no-op in mock/dry-run
        if not state.is_ostree or state.is_default_booted:
            return True                      # non-ostree / default: inerte
        if not self.config.ostree_auto_swap_default:
            self.logger.warning(
                "OSTREE: run da deployment NON-default con auto-swap "
                "disabilitato (ostree.auto_swap_default=false): i riavvii "
                "torneranno sul default e la run può restare orfana. "
                "Esegui buo dal deployment di default o abilita "
                "l'auto-swap.")
            return True                      # kill-switch esplicito: legacy
        ok, reason = self.ostree.verify_swap_preconditions(state)
        if not ok:
            # Vero caso a rischio (stato incoerente): abort fail-closed, mai
            # procedere con lo swap. Con MAJOR-1 non si arriva più qui per un
            # falso mismatch serial-cmdline vs posizione-status.
            self.logger.error(
                "OSTREE: %s — esegui buo dal deployment di default, "
                "oppure rpm-ostree rollback manuale (servono ESATTAMENTE "
                "2 deployment attivi).", reason)
            return False                     # abort fail-closed
        # NESSUNA sanity sull'index cmdline: dopo un rollback nello stesso
        # boot (swap o restore di un run precedente) il cmdline conserva il
        # seriale di boot-time e può contraddire la posizione status (es.
        # booted non-default con entry 0) — stato LEGITTIMO, mai abortire
        # (finding di campo 03/09: la sanity causava falsi abort). La
        # sicurezza è tutta in verify_swap_preconditions + guard checksum.
        # target = checksum del deployment booted (full-64 DA STATUS): il
        # restore è lecito solo se il default corrente tornerà a coincidere
        # con lui (guard D3).
        target = state.booted_checksum
        original = self.ostree.current_default_checksum()
        if not target or not original:
            self.logger.error(
                "OSTREE: stato deployment non determinabile — esegui "
                "buo dal deployment di default, oppure rpm-ostree "
                "rollback manuale.")
            return False
        if target == original:
            # il default È già il deployment bootato (serial cmdline
            # ≠ 0 fuorviante, risk-3): niente da fare, la run prosegue
            # inerte
            return True
        # D8: marcatore scritto PRIMA del rollback → crash tra i due = il
        # run successivo ritenta lo swap (self-healing), restore unico.
        self.checkpoint.set("ostree_swap_target_checksum", target)
        self.checkpoint.set("ostree_default_swapped", True)
        rc, _, err = self.ostree.swap_default()
        if rc != 0:
            self.checkpoint.set("ostree_default_swapped", False)
            self.logger.error(
                "OSTREE: swap default fallito (%s) — nessuna modifica "
                "applicata, run interrotta.", err or rc)
            return False
        self.logger.info(
            "OSTREE: default impostato sul deployment corrente "
            "(%.12s…) — i prossimi riavvii atterrano qui; a fine run "
            "verrà ripristinato il default originale.", target)
        return True

    def _exit_ostree_cleanup(self) -> None:
        """Choke point del restore (D4): ripristina il default originale se
        e solo se la run lo aveva cambiato (marcatore) e il default
        corrente è ancora il nostro target (verifica checksum via status,
        D3). Idempotente; MAI rollback alla cieca. Inerte in mock/dry-run
        (nessuna chiamata, nessun warning)."""
        if self.mock or self.dry_run:
            return
        if not self.checkpoint.get("ostree_default_swapped", False):
            return
        target = self.checkpoint.get("ostree_swap_target_checksum")
        deps = self.ostree.read_deployments()
        if deps is None:
            self.logger.error(
                "OSTREE: restore rimandato — stato deployment "
                "illeggibile; il default resta sul deployment di questa "
                "run. Verifica: rpm-ostree status.")
            return                            # marcatore tenuto: retry dopo
        if len(deps) != 2:
            self.logger.error(
                "OSTREE: restore rimandato — ora i deployment attivi "
                "sono %d (servono 2). Default manuale: rpm-ostree "
                "rollback.", len(deps))
            return
        if deps[0].checksum != target:
            self.checkpoint.set("ostree_default_swapped", False)
            self.logger.warning(
                "OSTREE: default cambiato esternamente (ora %.12s…) — "
                "nessun rollback automatico.", deps[0].checksum)
            return
        rc, _, err = self.ostree.restore_default()
        if rc == 0:
            self.checkpoint.set("ostree_default_swapped", False)
            self.logger.info("OSTREE: default originale ripristinato.")
        else:
            self.logger.error(
                "OSTREE: ripristino default FALLITO (%s) — il default "
                "resta sul deployment di questa run. Manuale: rpm-ostree "
                "rollback.", err or rc)
            # marcatore tenuto: il prossimo run ritenta (self-healing)

    def _schedule_reboot(self, reason: str) -> None:
        """Salva checkpoint e programma il reboot (auto-ripresa)."""
        if self.dry_run:
            self.logger.info("[DRY-RUN] reboot richiesto: %s", reason)
            return
        if self.mock:
            self.checkpoint.increment_reboot_count()
            self.logger.info("[MOCK] reboot simulato: %s", reason)
            return
        # Tetto globale anti-boot-loop (difesa in profondità): oltre il
        # limite il pipeline si FERMA invece di riavviare ancora, evitando
        # loop infiniti causati da bug futuri.
        count = self.checkpoint.get_reboot_count()
        if count >= self.config.max_reboots:
            msg = (f"Tetto globale reboot raggiunto ({count}/"
                   f"{self.config.max_reboots}) — interruzione per evitare "
                   f"boot loop (ultimo reboot richiesto da: {reason})")
            self.logger.error("%s", msg)
            self._safety_abort(msg)
            return
        self.checkpoint.increment_reboot_count()
        self.logger.info("Reboot programmato: %s", reason)
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
        """Stato corrente (per `buo status`).

        `applied_fixes` viene dal checkpoint (applied_steps): `results`
        è popolato solo durante `run()`, quindi in `buo status` (nuovo
        Orchestrator) sarebbe sempre vuoto.
        """
        info = self.hardware.get_system_info() if self.hardware else None
        return {
            "current_phase": self.checkpoint.get_current_phase(),
            "reboot_count": self.checkpoint.get_reboot_count(),
            "hardware": info,
            "applied_fixes": sorted(self._applied_steps()),
        }

    def recovery_plan(self) -> Dict[str, Any]:
        """Piano di ripresa (per `buo recover`)."""
        manager = RecoveryManager(checkpoint=self.checkpoint,
                                  verify_callback=None)
        return manager.get_recovery_plan()

    # ================================================================== #
    # RIEPILOGO FINALE (§3 UX_REVAMP_CLI_SPEC)
    # ================================================================== #

    def riepilogo_lines(self) -> List[str]:
        """Righe del riepilogo finale di run (fonte unica per log e CLI).

        Regole §3.2: una riga per voce; solo campi REALI (results/
        checkpoint) — campo assente → riga omessa o `non rilevabile`,
        MAI valori inventati (C1). In mock/dry-run nulla è reale: riga
        fix `0 — (simulazione)`; in dry-run una riga MODALITÀ DRY-RUN.
        """
        lines = ["Riepilogo finale"]
        ledger = self._applied_steps()

        def _names(ids):
            return [FIX_READABLE.get(s, s) for s in ids]

        if self.mock or self.dry_run:
            lines.append("  fix applicati in questa run: 0 — (simulazione)")
        elif ledger:
            names = _names(sorted(ledger))
            lines.append("  fix applicati in questa run: %d — %s"
                         % (len(names), ", ".join(names)))
        else:
            lines.append("  fix applicati in questa run: 0")
        if self.dry_run:
            lines.append("  MODALITÀ DRY-RUN: nessuna modifica reale — "
                         "report .dry-run")

        summary = self.results.get("fix_summary") or {}
        gia_attivi = sorted(set(summary.get("applied") or []) - ledger)
        if gia_attivi:
            lines.append(
                "  già attivi (verificati, nessuna modifica): %d — %s"
                % (len(gia_attivi), ", ".join(_names(gia_attivi))))
        manual = summary.get("manual") or []
        failed = summary.get("failed") or []
        if manual or failed:
            pieces = ([f"{n} (manuale)" for n in _names(manual)]
                      + [f"{n} (fallito)" for n in _names(failed)])
            lines.append("  attenzione manuale: %d — %s — dettagli nel "
                         "report" % (len(pieces), ", ".join(pieces)))

        apply_data = (self.checkpoint.get_phase("apply")
                      .get("data", {}) or {})
        cpu_final = apply_data.get("cpu_final") or {}
        if cpu_final.get("freq"):
            cpu = "  CPU: %d MHz" % cpu_final["freq"]
            if cpu_final.get("scale") is not None:
                cpu += " · scale %d" % cpu_final["scale"]
            if cpu_final.get("vid") is not None:
                cpu += " · VID %d mV" % cpu_final["vid"]
            cpu += " · persistito: %s" % (
                "sì" if cpu_final.get("persistent") else "no")
            lines.append(cpu)

        optimize_data = (self.checkpoint.get_phase("optimize")
                         .get("data", {}) or {})
        safe_points = ((optimize_data.get("undervolt_gpu") or {})
                       .get("safe_points") or [])
        freqs = [p.get("freq") for p in safe_points if p.get("freq")]
        if freqs:
            n = len(freqs)
            lines.append(
                "  GPU: curva %d-%d MHz · %d %s · persistito: %s"
                % (min(freqs), max(freqs), n,
                   "punto" if n == 1 else "punti",
                   "sì" if apply_data.get("governor_config") else "no"))

        after_gpu = (self.results.get("after") or {}).get("gpu") or {}
        cu = after_gpu.get("cu_count")
        if cu == 40:
            stato_40cu = "attive"
        elif "gpu_40cu" in ledger:
            stato_40cu = "attive (volatili, al boot tornano 24)"
        elif cu is not None:
            stato_40cu = "stock"
        else:
            stato_40cu = "non rilevabile"
        lines.append("  40-CU: %s" % stato_40cu)

        # Validazione post-unlock (design POSTUNLOCK_VALIDATION §8):
        # righe sintetiche SOLO se la fase ha prodotto un esito reale.
        uv_data = (self.checkpoint.get_phase("unlock_validate")
                   .get("data", {}) or {})
        cpu_uv = uv_data.get("cpu") or {}
        if cpu_uv.get("verdict_blocked"):
            lines.append("  unlock CPU: saltato (silicio marcato "
                         "never_unlock) — 6 core")
        elif cpu_uv.get("outcome") == "pass":
            lines.append("  unlock CPU: validato (thread extra) — 8 core "
                         "tenuti")
        elif cpu_uv.get("outcome") == "fail":
            lines.append("  unlock CPU: REVERTITO (cause=%s) — si "
                         "prosegue a 6 core" % cpu_uv.get("cause"))
        elif cpu_uv.get("inconclusive"):
            lines.append("  unlock CPU: non concluso (%s) — 6 core" %
                         cpu_uv.get("cause"))

        unlock_data = (self.checkpoint.get_phase("unlock")
                       .get("data", {}) or {})
        gpu_val = (unlock_data.get("gpu") or {}).get("validation") or {}
        if gpu_val.get("outcome") == "pass":
            lines.append("  unlock GPU: validato (vkmark) — 40 CU tenute")
        elif gpu_val.get("outcome") == "fail":
            lines.append("  unlock GPU: REVERTITO (cause=%s) — 24 CU + "
                         "persistenza disattivata" % gpu_val.get("cause"))
        elif gpu_val.get("outcome") == "inconclusive":
            lines.append("  unlock GPU: non concluso (%s) — 24 CU" %
                         gpu_val.get("cause"))

        validate_data = (self.checkpoint.get_phase("validate")
                         .get("data", {}) or {})
        stress = validate_data.get("stress") or {}
        if stress:
            if stress.get("skipped"):
                lines.append("  stress: saltato (--skip-validation o "
                             "restore)")
            elif stress.get("passed"):
                d = stress.get("duration_minutes")
                dur = ("%d minuto" % d if d == 1 else "%d minuti" % d) \
                    if d is not None else ""
                cpu_pk = stress.get("cpu_temp_max")
                gpu_pk = stress.get("gpu_temp_max")
                pow_pk = stress.get("power_max")

                def _metric(v, unit):
                    if v is None:
                        return "non rilevabile"
                    s = f"{v:.1f}".replace(".", ",")
                    if s.endswith(",0"):
                        s = s[:-2]
                    return f"{s}{unit}"

                lines.append("  stress: superato%s · picchi CPU %s / "
                             "GPU %s / %s"
                             % ((" · " + dur) if dur else "",
                                _metric(cpu_pk, "°C"),
                                _metric(gpu_pk, "°C"),
                                _metric(pow_pk, " W")))
            else:
                lines.append("  stress: fallito")

        lines.append("  report: %s" % self.report.output_md)
        lines.append("  rollback: sudo buo rollback")
        return lines
