#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Formattatori puri della cockpit OC + alias `buo oc-tui` (v1.2).

Funzioni pure (testabili senza terminale, stile dashboard_text di
buo/tui.py): sensors_text / run_text / profiles_table_rows / confirm_text.
La cockpit OC NON vive più qui: è confluita nella TUI UNIFICATA di
`buo tui` (tab ⚡ OC, textual TabbedContent — vedi buo/tui.py), che importa
queste funzioni e i moduli del motore OC (controller/profiles/apply: mai
toccati). `buo oc-tui` resta come ALIAS retro-compatibile: run_oc_tui()
avvia la STESSA app unificata con il tab OC già attivo.

Sicurezza: VID/SoC gated dal reader (🔒 se governor attivo); apply passa da
CpuSmoke + ApplyManager (sequenza A/R); mai SMU a governor attivo.
"""

from typing import Any, Dict, List, Optional, Tuple

from ..utils.logging import get_logger

logger = get_logger("buo.oc.tui")

# ============================================================================
# Funzioni PURE (testabili senza textual)
# ============================================================================


def sensors_text(r: Dict[str, Any]) -> str:
    """Testo del pannello sensori (dict flat → righe). Mai crash su {}."""
    cpu = r.get("cpu_freq", 0) or 0
    temp = r.get("cpu_temp")
    vid = r.get("cpu_vid")
    gpu = r.get("gpu_freq", 0) or 0
    gpu_t = r.get("gpu_temp")
    gpu_p = r.get("gpu_power")
    soc = r.get("total_power")
    fan = r.get("fan_speed")
    amb = r.get("ambient_temp")
    lines = [
        f"CPU  {cpu} MHz · {temp}°C" if temp else f"CPU  {cpu} MHz · temp n/d",
        f"VID  {vid} mV 🔒 (gated)" if vid is None else f"VID  {vid} mV",
        f"GPU  {gpu} MHz · {gpu_t}°C · {gpu_p} W"
        if gpu_t else f"GPU  {gpu} MHz",
        f"SoC  {soc} W 🔒 (gated)" if soc is None else f"SoC  {soc} W",
        f"Fan  {fan} RPM · amb {amb}°C"
        if amb else f"Fan  {fan if fan else 'n/d'} RPM",
    ]
    return "\n".join(lines)


def run_text(st: Dict[str, Any]) -> str:
    """Testo del pannello run (dict status → righe)."""
    state = st.get("state", {}) if isinstance(st, dict) else {}
    phase = state.get("phase_label", "?")
    testing = state.get("testing")
    t = ""
    if testing and testing.get("freq"):
        t = (f"{testing.get('freq')}@{testing.get('vid_cap')} "
             f"({testing.get('kind')})")
    l2 = state.get("l2") or {}
    winner = state.get("winner") or {}
    bkg = state.get("best_known_good") or {}
    proc = st.get("process", {})
    proc_txt = (f"attivo (pid {proc.get('pid')})"
                if proc.get("active") else "fermo")
    gov = st.get("governor", "?")
    lines = [
        f"fase: {phase}",
        f"testing: {t}" if t else "testing: —",
        f"L2: {l2.get('status', '-')} · run {l2.get('runs', '-')}",
        f"winner: {winner.get('freq')}@{winner.get('vid_cap')}",
        f"bkg: {bkg.get('freq')}@{bkg.get('vid_cap')}",
        f"persistito: {'sì' if state.get('persisted') else 'no'}",
        f"governor: {gov} {'(FERMO — atteso durante apply)' if gov == 'inactive' else ''}",
        f"processo: {proc_txt}",
    ]
    return "\n".join(lines)


# CTA del pannello run quando NON c'è un run attivo (testo ESATTO; le
# parentesi quadre sono LETTERALI — il widget #run è markup=False).
RUN_EMPTY_HINT = (
    "— nessun run attivo. Premi [u] per avviare la convergenza CPU "
    "(il motore esplora il tuo silicio in automatico)."
)


def run_empty_hint(st: Dict[str, Any]) -> str:
    """Riga CTA "nessun run attivo" se NON c'è un processo engine e la
    fase è finale o assente (done/none/fresco); "" altrimenti — run
    attivo o fase di lavoro utile: lo stato del pannello parla da solo.
    Non sostituisce run_text(): il chiamante accoda la riga al pannello.
    """
    proc = st.get("process") if isinstance(st.get("process"), dict) else {}
    if proc.get("active"):
        return ""
    state = st.get("state") if isinstance(st.get("state"), dict) else {}
    if state.get("phase") in (None, "", "none", "done"):
        return RUN_EMPTY_HINT
    return ""


def profiles_table_rows(profiles: List[Any],
                        active: Optional[str] = None) -> List[Tuple[str, ...]]:
    """Righe DataTable: (nome, freq@scale, VID, validated, attivo)."""
    rows: List[Tuple[str, ...]] = []
    for p in profiles:
        rows.append((
            p.name,
            f"{p.freq}@{p.scale}",
            str(p.vid_cap) if p.vid_cap is not None else "-",
            "✅" if p.validated else "—",
            "●" if p.id == active else "",
        ))
    return rows


def confirm_text(profile: Any, zone_ok: Tuple[bool, str]) -> str:
    """Testo della modal di conferma apply."""
    ok, reason = zone_ok
    head = f"Applicare {profile.name} ({profile.freq}@{profile.scale})?"
    if not ok:
        return f"{head}\n\n[red]❌ RIFIUTATO: {reason}[/]"
    return f"{head}\n\nVID: {profile.vid_cap or 'n/d'}\n" \
           f"validated: {'sì' if profile.validated else 'no'}"


# ============================================================================
# `buo oc-tui` (ALIAS della cockpit unificata — textual opzionale)
# ============================================================================


def run_oc_tui(mock: bool = False, oc_dir=None) -> int:
    """Avvia la cockpit OC (v1.2: ALIAS della cockpit unificata).

    La cockpit OC è confluita nella TUI unificata di `buo tui` (tab ⚡ OC,
    stesse funzioni pure e stessi pannelli/azioni): `buo oc-tui` resta per
    retro-compatibilità e avvia la STESSA app col tab OC già attivo.
    `oc_dir` (opzione --oc-dir) viene inoltrato com'era.

    Raises:
        RuntimeError: se `textual` non è installato (guard, stile buo/tui).
    """
    from ..tui import run_tui
    return run_tui(mock=mock, mock_hardware=None, oc_dir=oc_dir,
                   initial_tab="tab-oc")
