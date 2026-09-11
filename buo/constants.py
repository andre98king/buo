#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Costanti e hard limits per la BC-250 — BUO (BC-250 Ultimate Orchestrator).

Tutti i valori sono stati CONFERMATI dalla community (README ufficiali + codice
sorgente letto durante lo studio tecnico). I limiti marcati "HARD LIMIT" NON
devono mai essere superati: superarli può brickare la scheda in modo
permanente. Per questo sono in un modulo separato, importato ovunque, e NON
sono sovrascrivibili da file di configurazione utente.
"""

from dataclasses import dataclass, field
from typing import Any, Iterable, List, Tuple


# ============================================================================
# HARD LIMITS — codice immutabile (NON modificabili dall'utente)
# ============================================================================


# Griglia delle frequenze di sweep GPU (sottoinsiemi configurabili via
# `phases.undervolt.gpu_sweep_freqs`). Fonte unica per config e ottimizzatore.
GPU_FREQ_STEPS = [1200, 1300, 1400, 1500, 1600, 1700, 1800, 1900, 2000, 2100, 2200]


@dataclass(frozen=True)
class CPULimits:
    """Limiti CPU confermati dallo studio (README + codice)."""
    vid_absolute_max: int = 1325      # mV — HARD LIMIT (brick permanente sopra)
    vid_recommended_max: int = 1300   # mV — limite consigliato
    vid_min: int = 800                # mV — minimo sicuro per binary search
    freq_max: int = 4000              # MHz — OC max documentato
    freq_min: int = 3500              # MHz — stock
    temp_max: int = 95                # °C — HARD abort real-time (livello 1,
                                      #      mai superato, sotto i limiti AMD)
    temp_apply: int = 90              # °C — target OPERATIVO applicato (livello
                                      #      2: max_temperature del conf SMU)
    temp_critical: int = 100          # °C — throttle hardware


@dataclass(frozen=True)
class GPULimits:
    """Limiti GPU confermati dallo studio."""
    voltage_absolute_max: int = 1100  # mV — HARD LIMIT (degrado/danni sopra)
    voltage_recommended_max: int = 1050  # mV — limite consigliato
    voltage_min: int = 700            # mV — minimo sicuro per binary search
    freq_max: int = 2200              # MHz — instabile per molti chip
    freq_min: int = 500               # MHz
    temp_max: int = 105               # °C — HARD abort real-time (livello 1,
                                      #      mai superato; sotto l'edge AMD 110)
    temp_critical: int = 100          # °C — base del gate pre-operativo preflight


@dataclass(frozen=True)
class PowerLimits:
    """Limiti di potenza (PSU 350W dichiarato dall'utente)."""
    psu_max: int = 350                # W
    power_budget: int = 300           # W — margine di sicurezza
    cpu_max_power: int = 95           # W — stima max CPU
    gpu_max_power: int = 165          # W — stima max GPU (40 CU)


@dataclass(frozen=True)
class VRAMLimits:
    """Soglie per la stima VRAM (modello empirico + ML)."""
    warning_threshold: float = 82.0   # °C
    critical_threshold: float = 92.0  # °C
    alpha_default: float = 0.45       # accoppiamento termico GPU→VRAM
    beta_default: float = 0.04        # °C per watt GPU
    tau_default: float = 5.0          # costante di tempo smoothing (s)


@dataclass(frozen=True)
class HardwareLimits:
    """Aggregatore di tutti i limiti hardware."""
    cpu: CPULimits = CPULimits()
    gpu: GPULimits = GPULimits()
    power: PowerLimits = PowerLimits()
    vram: VRAMLimits = VRAMLimits()


# Istanza globale — importa LIMITS da qualsiasi modulo
LIMITS = HardwareLimits()


# ============================================================================
# HARDWARE — registri SMN/SMU e maschere (confermati nel codice)
# ============================================================================

# Maschera core presence (SMN 0x5A870): 0x77 = 6 core, 0xFF = 8 core
CORE_MASK_STOCK = 0x77
CORE_MASK_UNLOCKED = 0xFF
CORE_MASK_REG = 0x5A870

# Messaggi SMU (Queue 3)
SMU_MSG_WRITE_FF = 0x98        # scrittura SMN ungated e privilegiata
SMU_MSG_SET_SCALE = 0x50       # set V/F curve scale
SMU_MSG_SET_BOOST = 0x8F       # set max CPU boost clock
SMU_MSG_SET_VID = 0x0F         # set CPU/GPU VID (kind=0 CPU, kind=1 GFX)
SMU_MSG_SET_TEMP = 0x20        # set max temperature CPU/GPU

# Indirizzi code SMU (da Ghidra, confermati nello studio)
SMU_Q3_CMD = 0x03B10A20
SMU_Q3_RSP = 0x03B10A80
SMU_Q3_ARG = 0x03B10A88

# Status code mailbox
SMU_RETURN_OK = 0x01
SMU_RETURN_FAILED = 0xFF
SMU_RETURN_UNKNOWN_CMD = 0xFE
SMU_RETURN_REJECTED_PREREQ = 0xFD
SMU_RETURN_REJECTED_BUSY = 0xFC
SMU_DONE_STATES = {0x01, 0xFF, 0xFE, 0xFD, 0xFC}

# Percorso PCI config space per accesso SMN/SMU
PCI_CONFIG_PATH = "/sys/bus/pci/devices/0000:00:00.0/config"

# Registri GPU per l'unlock 40-CU (patch amdgpu di duggasco)
REG_CC_GC_SHADER_ARRAY_CONFIG = "mmCC_GC_SHADER_ARRAY_CONFIG"
REG_SPI_PG_ENABLE_STATIC_WGP_MASK = "mmSPI_PG_ENABLE_STATIC_WGP_MASK"
REG_RLC_PG_ALWAYS_ON_WGP_MASK = "mmRLC_PG_ALWAYS_ON_WGP_MASK"
BC250_CC_WRITE_MODE = 3          # modalità consigliata (clear tutti i SE/SH)

# ---------------------------------------------------------------------------
# Maschera WGP (dispatch CU) — PUNTO UNICO di codifica/decodifica.
#
# La maschera è un campo da 5 bit per shader array (SA): 4 SA, un bit = una
# WGP = 2 CU. Stock = 3 WGP per SA (0x07 ×4 = 24 CU), tutte = 5 WGP per SA
# (0x1f ×4 = 40 CU). La granularità è quindi 2 CU: 24/26/…/38/40 CU sono gli
# unici valori esprimibili. L'APU ha 40 CU fisiche di cui il firmware ne
# abilita 24: le 16 extra sono un unlock di natura COMPUTE (glmark2 +4,4%,
# compute LLM +1,5-1,6×, ma +30 W/+4 °C e, a 2 GHz con 40 CU, 181 W e 96 °C)
# e su ~26% delle board sono CU realmente difettose (pattern non contiguo):
# per questo la maschera va SEMPRE derivata dalle WGP validate, mai scritta
# "tutte" a mano. Qui vivono le due sole conversioni, con validazione
# fail-closed (una maschera anomala instrada CU guaste).
# ---------------------------------------------------------------------------
WGP_MASK_STOCK = 0x07            # per SA: WGP 0-2 abilitate (6 CU = 24 totali)
WGP_MASK_ALL = 0x1f              # per SA: WGP 0-4 (10 CU = 40 totali)
WGP_SA_COUNT = 4                 # SE0.SH0, SE0.SH1, SE1.SH0, SE1.SH1
WGP_PER_SA = 5
EXTRA_WGP_PER_SA = (3, 4)        # WGP disabilitate di fabbrica = le 16 CU extra
MASK_STOCK_24 = "0x07,0x07,0x07,0x07"
MASK_ALL_40 = "0x1f,0x1f,0x1f,0x1f"


def wgp_name(sa: int, wgp: int) -> str:
    """(SA, WGP) → 'SE.SH.WGP' (formato accettato dal live-manager)."""
    return "%d.%d.%d" % (sa // 2, sa % 2, wgp)


def parse_wgp(wgp: Any) -> Tuple[int, int]:
    """'SE.SH.WGP' → (SA, WGP). ValueError se anomalo (fail-closed)."""
    parts = str(wgp).strip().split(".")
    if len(parts) != 3:
        raise ValueError("WGP '%s' non valida (atteso SE.SH.WGP)" % (wgp,))
    try:
        se, sh, idx = (int(p) for p in parts)
    except ValueError:
        raise ValueError("WGP '%s' non numerica (atteso SE.SH.WGP)" % (wgp,))
    if se not in (0, 1) or sh not in (0, 1) or not 0 <= idx < WGP_PER_SA:
        raise ValueError("WGP '%s' fuori range (SE/SH in 0-1, WGP in 0-%d)"
                         % (wgp, WGP_PER_SA - 1))
    return se * 2 + sh, idx


def all_wgps() -> List[str]:
    """Tutte le 20 WGP del die (40 CU)."""
    return [wgp_name(sa, w) for sa in range(WGP_SA_COUNT)
            for w in range(WGP_PER_SA)]


def extra_wgps() -> List[str]:
    """Le 8 WGP extra (16 CU) non abilitate dal firmware stock."""
    return [wgp_name(sa, w) for sa in range(WGP_SA_COUNT)
            for w in EXTRA_WGP_PER_SA]


def stock_wgps() -> List[str]:
    """Le 12 WGP abilitate di fabbrica (24 CU)."""
    return [w for w in all_wgps() if w not in extra_wgps()]


def mask_from_wgps(wgps: Iterable[str]) -> str:
    """Elenco WGP → maschera conf ('0x07,0x07,0x07,0x07').

    Non solleva mai per un elenco VUOTO (maschera 0x00 ×4 = 0 CU): è il
    chiamante a rifiutarla. ValueError su WGP anomala.
    """
    rows = [0] * WGP_SA_COUNT
    for w in wgps:
        sa, idx = parse_wgp(w)
        rows[sa] |= 1 << idx
    return ",".join("0x%02x" % r for r in rows)


def wgps_from_mask(mask: str) -> List[str]:
    """Maschera conf → elenco delle WGP accese (inverse di
    mask_from_wgps). ValueError se i campi non sono 4 o escono dai 5 bit:
    una maschera anomala non va MAI interpretata a occhio."""
    fields = str(mask).strip().split(",")
    if len(fields) != WGP_SA_COUNT:
        raise ValueError("maschera WGP '%s' non ha %d campi SA"
                         % (mask, WGP_SA_COUNT))
    out: List[str] = []
    for sa, field in enumerate(fields):
        try:
            value = int(field.strip(), 0)
        except ValueError:
            raise ValueError("campo maschera '%s' non numerico" % (field,))
        if not 0 <= value <= WGP_MASK_ALL:
            raise ValueError("campo maschera '%s' fuori dai 5 bit (max 0x1f)"
                             % (field,))
        out.extend(wgp_name(sa, w) for w in range(WGP_PER_SA)
                   if value >> w & 1)
    return out


def cu_count_from_mask(mask: str) -> int:
    """CU totali della maschera: 2 per WGP accesa (usato per verificare
    che la maschera scritta sia davvero quella attesa)."""
    return 2 * len(wgps_from_mask(mask))

# Versioni minime richieste (confermate nello studio)
KERNEL_MIN = (6, 11)
MESA_MIN = (25, 1)


# ============================================================================
# PATH DI SISTEMA
# ============================================================================

STATE_DIR = "/var/lib/buo"
LOG_DIR = "/var/log/buo"
CONFIG_DIR = "/etc/buo"
STATE_FILE = STATE_DIR + "/state.json"
CONFIG_FILE = CONFIG_DIR + "/buo.yaml"
REPORT_FILE = STATE_DIR + "/report.md"
REPORT_JSON = STATE_DIR + "/report.json"
LOG_FILE = LOG_DIR + "/buo.log"
BACKUP_DIR = STATE_DIR + "/backups"

# Percorsi degli script esterni della community (default)
SCRIPT_APPLY = "/usr/local/bin/bc250-apply"
GOVERNOR_SERVICE = "cyan-skillfish-governor-smu"
GOVERNOR_CONFIG = "/etc/cyan-skillfish-governor-smu/config.toml"

# Unità systemd creata da `bc250-apply --install` (upstream): riapplica
# l'undervolt a ogni boot. L'upstream la crea ma NON la abilita (BUG F-D)
# → BUO esegue `systemctl enable` esplicito dopo l'install.
SMU_OC_SERVICE = "bc250-smu-oc"

# File di stato degli script esterni
HEALTH_RESULTS_FILE = "/var/lib/bc250-cu-health-test/results.tsv"


# ============================================================================
# FASI DELL'ORCHESTRATORE
# ============================================================================

PHASES = [
    "init",
    "pre_audit",
    "unlock",
    "unlock_validate",
    "fix",
    "optimize",
    "apply",
    "validate",
    "complete",
    "error",
]

# Ordine del rollback a cascata: dal più recente al più vecchio
# (l'ordine di applicazione è l'inverso)
ROLLBACK_ORDER = [
    "cpu_overclock",
    "gpu_governor",
    "gpu_40cu",
    "gpu_mask",
    "cpu_core_unlock",
    "acpi_fix",
    "tlb_fix",
    "ace_fix",
    "iommu",
    "vram_config",
    "gtt_tuning",
    "fan_control",
]


# ============================================================================
# EXIT CODES
# ============================================================================

EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_CONFIG_ERROR = 10
EXIT_SAFETY_VIOLATION = 20
EXIT_HARDWARE_ERROR = 30
EXIT_TIMEOUT = 40
EXIT_REBOOT = 50
