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
from typing import Dict, Any, List, Optional

from .constants import CONFIG_FILE, GPU_FREQ_STEPS, LIMITS


def _yaml():
    """Import pigro di pyyaml (il core si importa anche senza)."""
    import yaml
    return yaml


# --------------------------------------------------------------------- #
# Helper delle opzioni gpu_sweep_* (design GPU_UV §6): cast + clamp con
# logger.warning, MAI errore bloccante. Nessun valore può violare gli
# hard limits immutabili di constants.py.
# --------------------------------------------------------------------- #

def _sweep_int(value, default: int, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        logging.getLogger(__name__).warning(
            "Config: %s non numerico (%r) → default %d", name, value, default)
        return default


def _sweep_clamp(value, lo: int, hi: int, default: int,
                 name: str) -> int:
    v = _sweep_int(value, default, name)
    if v < lo or v > hi:
        logging.getLogger(__name__).warning(
            "Config: %s=%d fuori range [%d, %d] → clampato",
            name, v, lo, hi)
        return max(lo, min(v, hi))
    return v


def _sweep_at_most(value, cap: int, default: int, name: str) -> int:
    v = _sweep_int(value, default, name)
    if v > cap:
        logging.getLogger(__name__).warning(
            "Config: %s=%d > %d → clampato a %d", name, v, cap, cap)
        return cap
    return v


def _sweep_step(value) -> int:
    """step 10–50 mV, multiplo di 5 (design §6)."""
    v = _sweep_clamp(value, 10, 50, 25, "gpu_sweep_step_mv")
    return max(10, min(50, int(round(v / 5.0) * 5)))


def _sweep_bool(value, default: bool) -> bool:
    """gpu_sweep_enabled accetta SOLO bool/1/0 (design §6)."""
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    logging.getLogger(__name__).warning(
        "Config: gpu_sweep_enabled=%r non valido (solo bool/1/0) → default %s",
        value, default)
    return default


def _sweep_freqs(value, default: Optional[List[int]] = None) -> List[int]:
    """Sottoinsieme di GPU_FREQ_STEPS, strettamente crescente; altrimenti
    default (design §6: "freqs non numeriche/non crescente → default")."""
    default = list(default) if default is not None else [1200, 1500, 2000]
    if not isinstance(value, (list, tuple)):
        logging.getLogger(__name__).warning(
            "Config: gpu_sweep_freqs non è una lista → default %s", default)
        return default
    try:
        freqs = [int(v) for v in value]
    except (TypeError, ValueError):
        logging.getLogger(__name__).warning(
            "Config: gpu_sweep_freqs non numeriche → default %s", default)
        return default
    valid = [f for f in freqs if f in GPU_FREQ_STEPS]
    if not valid or any(a >= b for a, b in zip(freqs, freqs[1:])):
        logging.getLogger(__name__).warning(
            "Config: gpu_sweep_freqs=%s non valide (fuori da FREQ_STEPS o "
            "non crescenti) → default %s", freqs, default)
        return default
    return valid


def _stress_scope(value, default: str = "both") -> str:
    """validation.stress_scope: both|cpu|gpu (default both). Stress test
    separabile (richiesta utente 30/08): validare un solo componente non
    deve caricare l'altro. Valori sconosciuti → warning + default
    (fail-soft, mai bloccante)."""
    if isinstance(value, str) and value in ("both", "cpu", "gpu"):
        return value
    logging.getLogger(__name__).warning(
        "Config: validation.stress_scope=%r non valido (both|cpu|gpu) → "
        "default %s", value, default)
    return default


# --------------------------------------------------------------------- #
# Chiavi note dello schema PIATTO (config/buo.yaml). Avviso fail-soft
# (MAI bloccante) per chiavi sconosciute o strutture annidate: un valore
# ignorato SILENZIOSAMENTE è pericoloso — l'utente crede di aver
# alzato/abbassato un limite che invece non viene applicato (verificato
# sul campo 30/08: safety.cpu.freq_max annidato ignorato senza avvisi).
# --------------------------------------------------------------------- #

_KNOWN_SAFETY_KEYS = frozenset({
    "cpu_vid_recommended_max", "cpu_temp_max", "cpu_freq_max",
    "gpu_voltage_recommended_max", "gpu_temp_max", "gpu_freq_max",
    "power_budget", "max_reboots",
})

# Sezione ostree (design OSTREE_REBOOT, D5): schema piatto + avviso chiavi
# sconosciute, come safety/phases.*. auto_swap_default è il kill-switch
# dello swap del default di boot sui run da deployment ostree NON-default.
_KNOWN_OSTREE_KEYS = frozenset({"auto_swap_default"})

_KNOWN_PHASE_KEYS: Dict[str, frozenset] = {
    "probe": frozenset({"cpu_unlock", "gpu_unlock", "health_test",
                        "health_reboot_max"}),
    "fix": frozenset({"tlb", "ace", "iommu", "acpi", "vram", "gtt",
                      "fan"}),
    "undervolt": frozenset({
        "cpu_target_vid", "gpu_start_freq", "persist", "gpu_sweep_enabled",
        "gpu_sweep_freqs", "gpu_sweep_step_mv", "gpu_sweep_floor_mv",
        "gpu_sweep_max_steps", "gpu_sweep_test_seconds",
        "gpu_sweep_confirm_seconds", "gpu_sweep_max_minutes",
        "gpu_sweep_temp_gate",
    }),
    "overclock": frozenset({"enable", "power_budget"}),
    "validation": frozenset({"stress_duration", "stress_scope"}),
}


def _warn_unknown(section: str, data: Any, known_keys: frozenset) -> None:
    """Segnala con logger.warning le chiavi sconosciute o i sottodizionari
    inattesi in una sezione della config (schema PIATTO, vedi
    config/buo.yaml). Fail-soft: solo avviso, MAI bloccante."""
    if not isinstance(data, dict):
        return
    for key, value in data.items():
        if key in known_keys:
            continue
        if isinstance(value, dict):
            # sottodizionario inatteso (es. safety.cpu: {...}): elenca i
            # figli con la chiave PIATTA suggerita
            for sub in value:
                logging.getLogger(__name__).warning(
                    "Config: chiave di configurazione IGNORATA: "
                    "%s.%s.%s — usare %s.%s_%s (schema piatto), "
                    "vedi config/buo.yaml",
                    section, key, sub, section, key, sub)
        else:
            logging.getLogger(__name__).warning(
                "Config: chiave di configurazione IGNORATA: %s.%s — "
                "chiave sconosciuta (schema piatto, vedi config/buo.yaml)",
                section, key)


class BUOConfig:
    """Configurazione dell'orchestratore con limiti immutabili."""

    def __init__(self, data: Optional[Dict[str, Any]] = None):
        data = data or {}

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
        # Avviso fail-soft per chiavi sconosciute/annidate (schema
        # PIATTO): mai ignorare silenziosamente un limite configurato.
        _warn_unknown("safety", safety, _KNOWN_SAFETY_KEYS)
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
        # boot loop). Un ciclo completo legittimo
        # richiede al massimo ~4 reboot (CPU, GPU, ACPI, IOMMU non-ostree).
        self.max_reboots: int = max(1, int(safety.get("max_reboots", 5)))

        # Vincoli di sicurezza: mai oltre gli hard limits e mai sotto il
        # minimo sicuro (un VID troppo basso farebbe testare a bc250-detect
        # una tensione instabile).
        self.cpu_vid_recommended_max = max(
            LIMITS.cpu.vid_min,
            min(self.cpu_vid_recommended_max, self.cpu_vid_absolute_max),
        )
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
        # Avviso fail-soft anche per le sezioni delle fasi (stesso
        # principio dello schema piatto per sezione).
        for _section, _known in _KNOWN_PHASE_KEYS.items():
            _warn_unknown(_section, phases.get(_section), _known)
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
        # ---- VID target della ricerca CPU (vero undervolt = scale
        # NEGATIVA, scoperta sul campo 30/08): col default
        # vid_recommended_max la misura stock resta sotto il target e la
        # scale non va mai negativa (solo downclock). Target più basso
        # (es. 1000) spinge bc250-detect nel negativo. Clamp a
        # [vid_min, vid_recommended_max] (mai sotto il minimo sicuro, mai
        # oltre il consigliato), default conservativo.
        self.undervolt_cpu_target_vid: int = _sweep_clamp(
            undervolt.get("cpu_target_vid", LIMITS.cpu.vid_recommended_max),
            LIMITS.cpu.vid_min, LIMITS.cpu.vid_recommended_max,
            LIMITS.cpu.vid_recommended_max, "cpu_target_vid")
        self.undervolt_gpu_start_freq: int = int(undervolt.get("gpu_start_freq", 1200))
        # G3: rende PERSISTENTE l'undervolt al boot (bc250-apply --install).
        # Default ON: "BUO si occupa di tutto" — il profilo deve sopravvivere
        # al reboot, altrimenti dopo un riavvio la CPU torna a tensione stock.
        self.undervolt_persist: bool = bool(undervolt.get("persist", True))
        # ---- Ricerca per-silicio dell'undervolt GPU (design GPU_UV §6) ----
        self.undervolt_gpu_sweep_enabled: bool = _sweep_bool(
            undervolt.get("gpu_sweep_enabled", True), True)
        self.undervolt_gpu_sweep_freqs: List[int] = _sweep_freqs(
            undervolt.get("gpu_sweep_freqs", [1200, 1500, 2000]))
        self.undervolt_gpu_sweep_step_mv: int = _sweep_step(
            undervolt.get("gpu_sweep_step_mv", 25))
        # ---- FLOOR di SICUREZZA dei safe_points GPU (fail-closed,
        # 30/08): il floor SMU DIPENDE DAL CARICO — sotto vkmark (probe
        # dello sweep) l'SMU segue il target fino a 700 mV, ma sotto
        # FurMark la VDDGFX non scende sotto ~790-800 mV (misurato su
        # Cyan Skillfish). Default 800 = floor FurMark della macchina:
        # una config con punti < floor fa partire il governor in hang.
        # Clamp a [700, 1100] (minimo sicuro / hard limit assoluto;
        # mai oltre).
        self.undervolt_gpu_sweep_floor_mv: int = _sweep_clamp(
            undervolt.get("gpu_sweep_floor_mv", 800),
            LIMITS.gpu.voltage_min, LIMITS.gpu.voltage_absolute_max,
            800, "gpu_sweep_floor_mv")
        self.undervolt_gpu_sweep_max_steps: int = _sweep_clamp(
            undervolt.get("gpu_sweep_max_steps", 5), 2, 10, 5,
            "gpu_sweep_max_steps")
        self.undervolt_gpu_sweep_test_seconds: int = _sweep_clamp(
            undervolt.get("gpu_sweep_test_seconds", 30), 15, 120, 30,
            "gpu_sweep_test_seconds")
        _confirm = _sweep_clamp(
            undervolt.get("gpu_sweep_confirm_seconds", 60), 0, 300, 60,
            "gpu_sweep_confirm_seconds")
        if 0 < _confirm < 15:   # 0 = skip conferma; altrimenti 15–300
            _confirm = 15
        self.undervolt_gpu_sweep_confirm_seconds: int = _confirm
        self.undervolt_gpu_sweep_max_minutes: int = _sweep_clamp(
            undervolt.get("gpu_sweep_max_minutes", 15), 1, 60, 15,
            "gpu_sweep_max_minutes")
        self.undervolt_gpu_sweep_temp_gate: int = _sweep_at_most(
            undervolt.get("gpu_sweep_temp_gate", 85), LIMITS.gpu.temp_max, 85,
            "gpu_sweep_temp_gate")

        overclock = phases.get("overclock", {})
        self.overclock_enable: bool = bool(overclock.get("enable", True))
        self.overclock_power_budget: int = int(
            overclock.get("power_budget", self.power_budget)
        )

        validation = phases.get("validation", {})
        self.validation_stress_duration: int = int(
            validation.get("stress_duration", 30)
        )
        # Stress test separabile CPU/GPU (richiesta utente 30/08):
        # "both" (default) | "cpu" | "gpu" — un valore sconosciuto viene
        # segnalato e ricade su "both" (fail-soft).
        self.validation_stress_scope: str = _stress_scope(
            validation.get("stress_scope", "both")
        )

        benchmark = data.get("benchmark", {})
        self.benchmark_enabled: bool = bool(benchmark.get("enabled", True))
        self.benchmark_gpu_duration: int = int(benchmark.get("gpu_duration", 60))
        self.benchmark_cpu_duration: int = int(benchmark.get("cpu_duration", 60))
        self.benchmark_compute_duration: int = int(
            benchmark.get("compute_duration", 30)
        )

        # ----- Ostree (deployment-aware reboot, design OSTREE_REBOOT D5) -----
        # auto_swap_default (default ON): la run da un deployment ostree
        # NON-default imposta il default di boot sul deployment corrente
        # prima di programmare reboot (altrimenti `systemctl reboot` torna
        # sul default e la run si orfana). false = kill-switch esplicito
        # (comportamento legacy, con warning in run quando il caso a
        # rischio è presente — mai silenzioso).
        ostree = data.get("ostree", {})
        _warn_unknown("ostree", ostree, _KNOWN_OSTREE_KEYS)
        self.ostree_auto_swap_default: bool = bool(
            ostree.get("auto_swap_default", True)
        )

        # ----- Dipendenze (download automatico dei tool della community) -----
        deps = data.get("deps", {})
        self.deps_auto_install: bool = bool(deps.get("auto_install", True))
        self.deps_auto_install_governor: bool = bool(
            deps.get("auto_install_governor", True)
        )
        # Bundle offline dei checkout (fallback senza rete): vuoto = nessuno.
        self.deps_offline_bundle: str = str(deps.get("offline_bundle") or "")

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
            "hardware": {
                "psu_wattage": self.psu_wattage,
                "cooling_type": self.cooling_type,
            },
            "safety": {
                # Schema PIATTO (config/buo.yaml): il parser legge chiavi
                # piatte. La vecchia forma annidata (safety.cpu.{...},
                # safety.gpu.{...}) non veniva letta e i valori andavano
                # persi nel round-trip save/load (fix 30/08: avviso
                # _warn_unknown per chi la usa ancora).
                "cpu_vid_recommended_max": self.cpu_vid_recommended_max,
                "cpu_temp_max": self.cpu_temp_max,
                "cpu_freq_max": self.cpu_freq_max,
                "gpu_voltage_recommended_max": self.gpu_voltage_recommended_max,
                "gpu_temp_max": self.gpu_temp_max,
                "gpu_freq_max": self.gpu_freq_max,
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
                    "cpu_target_vid": self.undervolt_cpu_target_vid,
                    "gpu_start_freq": self.undervolt_gpu_start_freq,
                    "gpu_sweep_enabled": self.undervolt_gpu_sweep_enabled,
                    "gpu_sweep_freqs": self.undervolt_gpu_sweep_freqs,
                    "gpu_sweep_step_mv": self.undervolt_gpu_sweep_step_mv,
                    "gpu_sweep_floor_mv": self.undervolt_gpu_sweep_floor_mv,
                    "gpu_sweep_max_steps": self.undervolt_gpu_sweep_max_steps,
                    "gpu_sweep_test_seconds": self.undervolt_gpu_sweep_test_seconds,
                    "gpu_sweep_confirm_seconds": self.undervolt_gpu_sweep_confirm_seconds,
                    "gpu_sweep_max_minutes": self.undervolt_gpu_sweep_max_minutes,
                    "gpu_sweep_temp_gate": self.undervolt_gpu_sweep_temp_gate,
                },
                "overclock": {
                    "enable": self.overclock_enable,
                    "power_budget": self.overclock_power_budget,
                },
                "validation": {
                    "stress_duration": self.validation_stress_duration,
                    "stress_scope": self.validation_stress_scope,
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
                "offline_bundle": self.deps_offline_bundle,
            },
            "ostree": {
                "auto_swap_default": self.ostree_auto_swap_default,
            },
        }

    def save(self, path: Optional[Path] = None) -> None:
        """Salva la configurazione su file YAML."""
        path = Path(path) if path else Path(CONFIG_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            _yaml().dump(self.to_dict(), f, default_flow_style=False, indent=2,
                         allow_unicode=True)
