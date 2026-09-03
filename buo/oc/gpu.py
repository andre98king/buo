#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Gestione OC/UV GPU (governor cyan-skillfish-governor-smu) per il tab OC
della cockpit unificata (buo/tui.py).

La GPU (GFX1013) è controllata dal governor via config.toml (costanti
GOVERNOR_CONFIG / GOVERNOR_SERVICE in buo/constants.py):
    • [frequency-range]  min/max
    • [temperature]      throttling / recovery
    • [[safe-points]]    frequency/voltage — curva V/F; voltage = target
                         VDDGFX mV ASSOLUTO; il tetto duro è l'ULTIMO
                         safe-point (per alzare il cap serve estendere la
                         curva, non solo il range).

PRESET — ESEMPI VALIDATI su UN'UNITÀ (02-03/09, CPU 3825@1125 / 16T; il
silicio VARIA: C1, mai valori inventati — su altri chip servono nuove
validazioni):
    • "UV 1800 (daily)" = config ATTUALE della macchina (FurMark 91°C di
      picco, gaming 75°C);
    • "Stock-cap 1500"  = config precedente (1500 = clock stock del lock
      mining). NON offrire preset non validati su questo silicio (es.
      2000@1000 = muro termico QUI).
Il floor GPU reale è ~800 mV (sotto, l'SMU non segue il target): nessun
preset scende sotto.

Qui c'è SOLO logica di lettura/controllo: l'APPLY riusa GovernorWrapper
(buo/optimize/governor.py — write_config dal template + restart), mai il
basso livello. mock → nessun subprocess e nessuna lettura di /etc
(fixture interna).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..constants import GOVERNOR_CONFIG

# Floor GPU REALE misurato (30/08/2026): sotto ~800 mV l'SMU non segue il
# target (una config con punti < floor fa partire il governor in hang).
GPU_VOLTAGE_FLOOR_MV = 800

# Fixture MOCK della config attiva (nessuna lettura di /etc in mock):
# stessi valori della config "UV 1800 (daily)" validata sul campo.
_MOCK_CONFIG_TOML = """\
# fixture mock — UV 1800 (daily)
[frequency-range]
min = 1000
max = 1800

[temperature]
throttling = 85
throttling_recovery = 75

[[safe-points]]
frequency = 1000
voltage = 800

[[safe-points]]
frequency = 1400
voltage = 800

[[safe-points]]
frequency = 1800
voltage = 800
"""


@dataclass(frozen=True)
class GpuPoint:
    """Punto della curva V/F: freq (MHz) → voltage (target VDDGFX mV)."""
    freq: int
    voltage: int


@dataclass(frozen=True)
class GpuCurve:
    """Curva attiva letta da config.toml (parsata, fail-soft)."""
    min_freq: int
    max_freq: int
    throttling: int
    recovery: int
    points: Tuple[GpuPoint, ...]


@dataclass(frozen=True)
class GpuPreset:
    """Preset GPU validato (silicio di un'unità — la curva da applicare)."""
    id: str
    name: str
    min_freq: int
    max_freq: int
    throttling: int
    recovery: int
    points: Tuple[GpuPoint, ...]
    note: str = ""

    def to_safe_points(self) -> List[Dict[str, int]]:
        """Formato di GovernorWrapper.write_config."""
        return [{"freq": p.freq, "voltage": p.voltage} for p in self.points]


# Preset ESEMPI validati sul campo (vedi docstring del modulo): la scelta
# è in codice (nessuno store: dati read-only di collaudo, non profili
# utente modificabili). Ordine = ordine di visualizzazione nel tab OC.
DEFAULT_GPU_PRESETS: Tuple[GpuPreset, ...] = (
    GpuPreset(
        id="uv-1800",
        name="UV 1800 (daily)",
        min_freq=1000, max_freq=1800, throttling=85, recovery=75,
        points=(GpuPoint(1000, 800), GpuPoint(1400, 800),
                GpuPoint(1800, 800)),
        note="config attuale — FurMark 91°C picco, gaming 75°C",
    ),
    GpuPreset(
        id="stock-1500",
        name="Stock-cap 1500",
        min_freq=1000, max_freq=1500, throttling=85, recovery=75,
        points=(GpuPoint(1500, 800),),
        note="config precedente (1500 = clock stock del lock mining)",
    ),
)


# ============================================================================
# Lettura/parse della config attiva (fail-soft C1: mai eccezione)
# ============================================================================


def read_config_text(config_path: Optional[str] = None) -> Optional[str]:
    """Testo di config.toml; assente/illeggibile → None (mai crash)."""
    path = Path(config_path) if config_path else Path(GOVERNOR_CONFIG)
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _loads_toml(text: str):
    """tomllib (stdlib ≥3.11) con fallback tomli; errore → None."""
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover — Python < 3.11
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ModuleNotFoundError:
            return None
    try:
        return tomllib.loads(text)
    except Exception:
        return None


def parse_gpu_config(text: Optional[str]) -> Optional[GpuCurve]:
    """Parsa il testo di config.toml → GpuCurve.

    Fail-soft: testo assente/corrotto o schema inatteso → None ("config
    non leggibile"), mai eccezione, mai valori inventati.
    """
    if not text:
        return None
    data = _loads_toml(text)
    if not isinstance(data, dict):
        return None
    try:
        fr = data.get("frequency-range") or {}
        temp = data.get("temperature") or {}
        raw_points = data.get("safe-points") or []
        points = tuple(
            GpuPoint(freq=int(p["frequency"]), voltage=int(p["voltage"]))
            for p in raw_points
            if isinstance(p, dict) and "frequency" in p and "voltage" in p
        )
        if not points:
            return None
        return GpuCurve(
            min_freq=int(fr.get("min", 0)),
            max_freq=int(fr.get("max", 0)),
            throttling=int(temp.get("throttling", 0)),
            recovery=int(temp.get("throttling_recovery", 0)),
            points=points,
        )
    except (KeyError, TypeError, ValueError):
        return None


def read_active_curve(mock: bool = False,
                      config_path: Optional[str] = None) -> Optional[GpuCurve]:
    """Curva attiva del governor per il pannello:
    mock → fixture interna (mai /etc); reale → config.toml (fail-soft)."""
    if mock:
        return parse_gpu_config(_MOCK_CONFIG_TOML)
    return parse_gpu_config(read_config_text(config_path))


# ============================================================================
# Validazione preset (fail-closed PRIMA della scrittura)
# ============================================================================


def validate_gpu_preset(preset: GpuPreset) -> Tuple[bool, str]:
    """(ok, motivo). Regole:
      • ogni punto ≥ floor 800 mV (sotto, l'SMU non segue → governor hang);
      • punti dentro [min, max], frequenze crescenti, almeno un punto;
      • tetto duro = l'ULTIMO safe-point: max_freq oltre l'ultimo punto
        creerebbe un cap senza curva (interpolazioni fuori range).
    """
    if not preset.points:
        return False, "nessun safe-point"
    prev_freq: Optional[int] = None
    for p in preset.points:
        if p.voltage < GPU_VOLTAGE_FLOOR_MV:
            return False, (
                f"voltage {p.voltage} mV < floor {GPU_VOLTAGE_FLOOR_MV} mV "
                "(sotto, l'SMU non segue)")
        if p.freq < preset.min_freq or p.freq > preset.max_freq:
            return False, (
                f"punto {p.freq} MHz fuori dal range "
                f"[{preset.min_freq}, {preset.max_freq}] MHz")
        if prev_freq is not None and p.freq <= prev_freq:
            return False, "frequenze non crescenti"
        prev_freq = p.freq
    if preset.points[-1].freq != preset.max_freq:
        return False, (
            f"tetto {preset.max_freq} MHz oltre l'ultimo safe-point "
            f"{preset.points[-1].freq} MHz: estendi la curva, non il range")
    return True, ""


# ============================================================================
# Corrispondenza preset ↔ curva attiva (confronto punti + range)
# ============================================================================


def curve_matches_preset(curve: GpuCurve, preset: GpuPreset) -> bool:
    """Confronto su punti + range (le soglie termiche possono differire
    senza cambiare l'identità della curva applicata)."""
    if (curve.min_freq, curve.max_freq) != (preset.min_freq,
                                            preset.max_freq):
        return False
    return [(p.freq, p.voltage) for p in curve.points] == [
        (p.freq, p.voltage) for p in preset.points]


def active_gpu_preset(
        curve: Optional[GpuCurve],
        presets: Tuple[GpuPreset, ...] = DEFAULT_GPU_PRESETS
) -> Optional[GpuPreset]:
    """Preset corrispondente alla curva attiva, o None (curva
    personalizzata / non leggibile)."""
    if curve is None:
        return None
    for preset in presets:
        if curve_matches_preset(curve, preset):
            return preset
    return None


# ============================================================================
# Apply (riuso di GovernorWrapper — mai il basso livello)
# ============================================================================


def apply_gpu_preset(wrapper, preset: GpuPreset) -> Dict[str, Any]:
    """Applica un preset GPU: valida PRIMA di scrivere, poi
    write_config (dal template upstream) + restart del governor.

    Args:
        wrapper: GovernorWrapper (mock iniettabile nei test).
        preset: preset validato.

    Returns:
        {"ok", "written", "restarted", "reason"} — ok=False se la
        validazione fallisce, se la scrittura fallisce, o se il governor
        NON riparte dopo il write (config scritta ma inattiva: reason con
        suggerimento di ripristino).
    """
    ok, reason = validate_gpu_preset(preset)
    if not ok:
        return {"ok": False, "written": False, "restarted": False,
                "reason": reason}
    written = wrapper.write_config(
        preset.to_safe_points(),
        min_freq=preset.min_freq,
        max_freq=preset.max_freq,
        throttling=preset.throttling,
        recovery=preset.recovery,
    )
    if not written:
        return {"ok": False, "written": False, "restarted": False,
                "reason": "write_config fallita (template o file non "
                          "scrivibile — esegui come root?)"}
    restarted = wrapper.restart()
    if not restarted:
        return {"ok": False, "written": True, "restarted": False,
                "reason": ("config scritta ma governor non riavviato — "
                           "riavvialo (systemctl restart "
                           "cyan-skillfish-governor-smu) o riapplica il "
                           "preset Stock-cap 1500")}
    return {"ok": True, "written": True, "restarted": True, "reason": ""}


# ============================================================================
# Formattatori puri (testabili senza terminale)
# ============================================================================


def _gov_normalize(governor: str) -> str:
    """Normalizza lo stato governor per i pannelli GPU (spec §4.6):
    active → attivo; inactive → FERMO; simulato → simulato; valori non
    noti → verbatim (mai inventare)."""
    return {"active": "attivo", "inactive": "FERMO",
            "simulato": "simulato"}.get(governor, governor)


def gpu_panel_text(curve: Optional[GpuCurve],
                   preset: Optional[GpuPreset],
                   governor: str) -> str:
    """Testo del pannello GPU (#gpu): curva attiva + preset corrispondente.

    Stati espliciti (spec §4.6, mai schermate vuote): curva attiva con
    preset riconosciuto / "curva personalizzata" (hint di selezione) /
    "config assente o non leggibile" (con la via d'uscita).
    """
    if curve is None:
        return "\n".join([
            "GPU · config assente o non leggibile — la curva attiva NON è "
            "verificabile",
            "Cosa fare: seleziona un preset qui sotto e premi g per "
            "riscriverla,",
            "oppure avvia il governor (systemctl start "
            "cyan-skillfish-governor-smu).",
        ])
    gov = _gov_normalize(governor)
    punti = " ".join(f"{p.freq}@{p.voltage}" for p in curve.points)
    if preset is not None:
        head = f"GPU · preset {preset.name}"
        if preset.note:
            head = f"{head} — {preset.note}"
    else:
        head = "GPU · curva personalizzata (nessun preset corrisponde)"
    lines = [
        head,
        f"curva: range {curve.min_freq}-{curve.max_freq} MHz · {punti}",
        f"soglie: throttle {curve.throttling}°C / recovery "
        f"{curve.recovery}°C · governor: {gov}",
    ]
    if preset is None:
        # Nessun preset corrisponde alla curva attiva: hint di selezione
        # (parentesi quadre LETTERALI — il widget #gpu è markup=False).
        lines.append("↑/↓ scegli un preset sotto · [g] applica (conferma)")
    return "\n".join(lines)


def gpu_preset_rows(presets: Tuple[GpuPreset, ...],
                    active: Optional[str] = None) -> List[Tuple[str, ...]]:
    """Righe DataTable #gpu-presets: (nome, curva, stato)."""
    rows: List[Tuple[str, ...]] = []
    for p in presets:
        curva = " ".join(f"{pt.freq}@{pt.voltage}" for pt in p.points)
        rows.append((
            p.name,
            f"{p.min_freq}-{p.max_freq} MHz · {curva}",
            "●" if p.id == active else "",
        ))
    return rows


def gpu_apply_text(preset: GpuPreset) -> str:
    """Testo della modal di conferma apply preset GPU (spec §4.8)."""
    punti = ", ".join(f"{p.freq}@{p.voltage}" for p in preset.points)
    lines = [
        f"Applicare il preset GPU «{preset.name}»?",
        "",
        f"  range {preset.min_freq}-{preset.max_freq} MHz",
        f"  punti: {punti}",
        f"  soglie throttle {preset.throttling}°C / recovery "
        f"{preset.recovery}°C",
    ]
    if preset.note:
        lines.append(f"  ({preset.note})")
    lines += [
        "",
        "La curva viene riscritta e il governor riavviato. Freeze possibile "
        "→ power-cycle; la config persistita torna al riavvio.",
        "",
        "y applica · n annulla (esc = annulla)",
    ]
    return "\n".join(lines)
