#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Gestione della configurazione YAML di BUO.

La configurazione utente può personalizzare SOLO i parametri "safe" (PSU,
raffreddamento, soglie consigliate, durate dei test...). Gli HARD LIMIT
(vid_absolute_max, gpu_voltage_absolute_max, ecc.) vivono in constants.py
e NON possono essere sovrascritti dal file di configurazione: se il file
li contiene con valori diversi, vengono ignorati a favore dei limiti
immutabili (sicurezza assoluta).
"""

import logging
from pathlib import Path
from typing import Dict, Any, Optional

from .constants import CONFIG_FILE, LIMITS


def _yaml():
    """Import pigro di pyyaml (il core si importa anche senza)."""
    import yaml
    return yaml


class BUOConfig:
    """Configurazione dell'orchestratore con limiti immutabili."""

    def __init__(self, data: Optional[Dict[str, Any]] = None):
        data = data or {}

        # Modalità: adaptive | efficiency | performance | custom
        self.mode: str = str(data.get("mode", "adaptive"))

        # Hardware dichiarato dall'utente
        hardware = data.get("hardware") or {}
        self.psu_wattage: int = int(
            hardware.get("psu_wattage", data.get("psu_wattage", 350))
        )
        self.cooling_type: str = str(
            hardware.get("cooling_type", data.get("cooling_type", "push-pull"))
        )

        # ----- Safety: partono dagli hard limits immutabili -----
        # I valori del file YAML NON possono alzare gli hard limits.
        safety = data.get("safety", {})
        self.cpu_vid_absolute_max: int = LIMITS.cpu.vid_absolute_max
        self.cpu_vid_recommended_max: int = int(
            safety.get("cpu_vid_recommended_max", LIMITS.cpu.vid_recommended_max)
        )
        self.cpu_temp_max: int = int(safety.get("cpu_temp_max", LIMITS.cpu.temp_max))
        self.cpu_freq_max: int = int(safety.get("cpu_freq_max", LIMITS.cpu.freq_max))
        self.gpu_voltage_absolute_max: int = LIMITS.gpu.voltage_absolute_max
        self.gpu_voltage_recommended_max: int = int(
            safety.get("gpu_voltage_recommended_max", LIMITS.gpu.voltage_recommended_max)
        )
        self.gpu_temp_max: int = int(safety.get("gpu_temp_max", LIMITS.gpu.temp_max))
        self.gpu_freq_max: int = int(safety.get("gpu_freq_max", LIMITS.gpu.freq_max))
        self.power_budget: int = int(
            safety.get("power_budget", LIMITS.power.power_budget)
        )
        # Tetto globale ai reboot per run (difesa in profondità contro i
        # boot loop; vedi docs/BUGS.md #14). Un ciclo completo legittimo
        # richiede al massimo ~4 reboot (CPU, GPU, ACPI, IOMMU non-ostree).
        self.max_reboots: int = max(1, int(safety.get("max_reboots", 5)))

        # Vincoli di sicurezza: mai oltre gli hard limits
        self.cpu_vid_recommended_max = min(self.cpu_vid_recommended_max,
                                           self.cpu_vid_absolute_max)
        self.gpu_voltage_recommended_max = min(self.gpu_voltage_recommended_max,
                                               self.gpu_voltage_absolute_max)
        self.power_budget = min(self.power_budget, LIMITS.power.psu_max)

        # ----- Stima VRAM -----
        vram = data.get("vram_estimation", {})
        self.vram_estimation_enabled: bool = bool(vram.get("enabled", True))
        self.vram_alpha: float = float(vram.get("alpha", LIMITS.vram.alpha_default))
        self.vram_beta: float = float(vram.get("beta", LIMITS.vram.beta_default))
        self.vram_tau: float = float(vram.get("tau", LIMITS.vram.tau_default))
        self.vram_warning_threshold: float = float(
            vram.get("warning_threshold", LIMITS.vram.warning_threshold)
        )
        self.vram_critical_threshold: float = float(
            vram.get("critical_threshold", LIMITS.vram.critical_threshold)
        )

        # ----- Fasi -----
        phases = data.get("phases", {})
        probe = phases.get("probe", {})
        self.probe_cpu_unlock: bool = bool(probe.get("cpu_unlock", True))
        self.probe_gpu_unlock: bool = bool(probe.get("gpu_unlock", True))
        self.probe_health_test: bool = bool(probe.get("health_test", True))
        self.probe_health_reboot_max: int = int(probe.get("health_reboot_max", 25))

        fix = phases.get("fix", {})
        self.fix_tlb: bool = bool(fix.get("tlb", True))
        self.fix_ace: bool = bool(fix.get("ace", True))
        self.fix_iommu: bool = bool(fix.get("iommu", True))
        self.fix_acpi: bool = bool(fix.get("acpi", True))
        self.fix_vram: bool = bool(fix.get("vram", True))
        self.fix_gtt: bool = bool(fix.get("gtt", True))
        self.fix_fan: bool = bool(fix.get("fan", True))

        undervolt = phases.get("undervolt", {})
        self.undervolt_cpu_start_freq: int = int(undervolt.get("cpu_start_freq", 3500))
        self.undervolt_cpu_step: int = int(undervolt.get("cpu_step", 100))
        self.undervolt_cpu_test_duration: int = int(
            undervolt.get("cpu_test_duration", 30)
        )
        self.undervolt_gpu_start_freq: int = int(undervolt.get("gpu_start_freq", 1200))
        self.undervolt_gpu_step: int = int(undervolt.get("gpu_step", 50))
        self.undervolt_gpu_test_duration: int = int(
            undervolt.get("gpu_test_duration", 30)
        )

        overclock = phases.get("overclock", {})
        self.overclock_enable: bool = bool(overclock.get("enable", True))
        self.overclock_power_budget: int = int(
            overclock.get("power_budget", self.power_budget)
        )

        validation = phases.get("validation", {})
        self.validation_stress_duration: int = int(
            validation.get("stress_duration", 30)
        )

        benchmark = data.get("benchmark", {})
        self.benchmark_enabled: bool = bool(benchmark.get("enabled", True))
        self.benchmark_gpu_duration: int = int(benchmark.get("gpu_duration", 60))
        self.benchmark_cpu_duration: int = int(benchmark.get("cpu_duration", 60))
        self.benchmark_compute_duration: int = int(
            benchmark.get("compute_duration", 30)
        )

        # ----- Dipendenze (download automatico dei tool della community) -----
        deps = data.get("deps", {})
        self.deps_auto_install: bool = bool(deps.get("auto_install", True))
        self.deps_auto_install_governor: bool = bool(
            deps.get("auto_install_governor", True)
        )

        # ----- Logging / Report -----
        logging_cfg = data.get("logging", {})
        self.log_level: str = str(logging_cfg.get("level", "info"))
        self.log_file: str = str(logging_cfg.get("file", "/var/log/buo/buo.log"))



    # ------------------------------------------------------------------ #

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "BUOConfig":
        """Carica la configurazione da file YAML (default se assente)."""
        path = Path(path) if path else Path(CONFIG_FILE)
        if not path.exists():
            return cls()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = _yaml().safe_load(f) or {}
            return cls(data)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Caricamento configurazione fallito da %s (%s): uso i default",
                path, exc,
            )
            return cls()

    def to_dict(self) -> Dict[str, Any]:
        """Serializza la configurazione (per checkpoint e report)."""
        return {
            "mode": self.mode,
            "hardware": {
                "psu_wattage": self.psu_wattage,
                "cooling_type": self.cooling_type,
            },
            "safety": {
                "cpu": {
                    "vid_absolute_max": self.cpu_vid_absolute_max,
                    "vid_recommended_max": self.cpu_vid_recommended_max,
                    "temp_max": self.cpu_temp_max,
                    "freq_max": self.cpu_freq_max,
                },
                "gpu": {
                    "voltage_absolute_max": self.gpu_voltage_absolute_max,
                    "voltage_recommended_max": self.gpu_voltage_recommended_max,
                    "temp_max": self.gpu_temp_max,
                    "freq_max": self.gpu_freq_max,
                },
                "power_budget": self.power_budget,
                "max_reboots": self.max_reboots,
            },
            "vram_estimation": {
                "enabled": self.vram_estimation_enabled,
                "alpha": self.vram_alpha,
                "beta": self.vram_beta,
                "tau": self.vram_tau,
                "warning_threshold": self.vram_warning_threshold,
                "critical_threshold": self.vram_critical_threshold,
            },
            "phases": {
                "probe": {
                    "cpu_unlock": self.probe_cpu_unlock,
                    "gpu_unlock": self.probe_gpu_unlock,
                    "health_test": self.probe_health_test,
                    "health_reboot_max": self.probe_health_reboot_max,
                },
                "fix": {
                    "tlb": self.fix_tlb,
                    "ace": self.fix_ace,
                    "iommu": self.fix_iommu,
                    "acpi": self.fix_acpi,
                    "vram": self.fix_vram,
                    "gtt": self.fix_gtt,
                    "fan": self.fix_fan,
                },
                "undervolt": {
                    "cpu_start_freq": self.undervolt_cpu_start_freq,
                    "cpu_step": self.undervolt_cpu_step,
                    "cpu_test_duration": self.undervolt_cpu_test_duration,
                    "gpu_start_freq": self.undervolt_gpu_start_freq,
                    "gpu_step": self.undervolt_gpu_step,
                    "gpu_test_duration": self.undervolt_gpu_test_duration,
                },
                "overclock": {
                    "enable": self.overclock_enable,
                    "power_budget": self.overclock_power_budget,
                },
                "validation": {
                    "stress_duration": self.validation_stress_duration,
                },
            },
            "benchmark": {
                "enabled": self.benchmark_enabled,
                "gpu_duration": self.benchmark_gpu_duration,
                "cpu_duration": self.benchmark_cpu_duration,
                "compute_duration": self.benchmark_compute_duration,
            },
            "deps": {
                "auto_install": self.deps_auto_install,
                "auto_install_governor": self.deps_auto_install_governor,
            },
            "logging": {"level": self.log_level, "file": self.log_file},
        }

    def save(self, path: Optional[Path] = None) -> None:
        """Salva la configurazione su file YAML."""
        path = Path(path) if path else Path(CONFIG_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            _yaml().dump(self.to_dict(), f, default_flow_style=False, indent=2,
                         allow_unicode=True)
