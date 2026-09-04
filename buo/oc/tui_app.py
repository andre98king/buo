#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Formattatori puri della cockpit OC + alias `buo oc-tui` (v1.3).

Funzioni pure (testabili senza terminale, stile dashboard_text di
buo/tui.py): sensors_text / run_text / RUN_EMPTY_HINT /
profiles_table_rows / confirm_text / confirm_stock_text /
confirm_stop_text. La cockpit OC NON vive più qui: è confluita nella
TUI UNIFICATA di `buo tui` (tab OC, textual TabbedContent — vedi
buo/tui.py), che importa queste funzioni e i moduli del motore OC
(controller/profiles/apply: mai toccati). `buo oc-tui` resta come ALIAS
retro-compatibile: run_oc_tui() avvia la STESSA app unificata con il tab
OC già attivo.

Sicurezza (C1): le letture protette (VID/SoC con governor attivo)
arrivano None dal reader → resa "—" (mai valori inventati); apply passa
da CpuSmoke + ApplyManager (sequenza A/R); mai SMU a governor attivo.
"""

from typing import Any, Dict, List, Optional, Tuple

from ..utils.logging import get_logger

logger = get_logger("buo.oc.tui")

# ============================================================================
# Funzioni PURE (testabili senza textual)
# ============================================================================


def _gov_word(gov: Optional[str]) -> str:
    """Normalizza lo stato governor per il pannello run (D5): active →
    attivo; inactive → FERMO (atteso durante apply); simulato → simulato;
    altro → '—' (mai inventare)."""
    return {"active": "attivo", "inactive": "FERMO (atteso durante apply)",
            "simulato": "simulato"}.get(gov, "—") if gov else "—"


def sensors_text(r: Dict[str, Any]) -> str:
    """Striscia sensori COMPATTA del tab OC (3 righe fisse).

    C1: sensore assente/0 → '—' (mai '0 MHz', mai 'n/d', mai 🔒). La lettura
    protetta (VID/SoC con governor attivo) arriva già None dal reader: il
    PERCHÉ sta nel pannello run/governor.
    """
    f = r.get("cpu_freq") or 0
    t = r.get("cpu_temp")
    v = r.get("cpu_vid")
    uv = r.get("undervolted", r.get("is_undervolted")) if r else None
    uv_word = "undervolt sì" if uv else "undervolt no"

    if not (f or t or v):
        line1 = "CPU — (non rilevabile) · — · VID —"
    else:
        f_s = f"{f} MHz" if f else "—"
        t_s = f"{t:.1f}°C" if t else "—"
        v_s = f"VID {v} mV" if v else "VID —"
        line1 = f"CPU {f_s} · {t_s} · {v_s} · {uv_word}"

    g = r.get("gpu_freq") or 0
    g_t = r.get("gpu_temp")
    g_v = r.get("gpu_voltage")
    g_p = r.get("gpu_power")
    soc = r.get("total_power")
    g_s = f"{g} MHz" if g else "—"
    gt_s = f"{g_t:.1f}°C" if g_t else "—"
    gv_s = f"{g_v} mV" if g_v else "—"
    gp_s = f"{g_p:g} W" if g_p else "—"
    soc_s = f"SoC {soc:g} W" if soc else "SoC —"
    line2 = f"GPU {g_s} · {gt_s} · {gv_s} · {gp_s} · {soc_s}"

    fan = r.get("fan_speed")
    amb = r.get("ambient_temp")
    fan_s = f"{fan} RPM" if fan else "—"
    amb_s = f"{amb:.1f}°C" if amb else "—"
    line3 = f"ventola {fan_s} · amb {amb_s}"
    return "\n".join((line1, line2, line3))


def _fresh_state(state: Dict[str, Any]) -> bool:
    """Fase assente/fresca (nessun checkpoint): corpo pannello ridotto."""
    return state.get("phase") in (None, "", "none")


def run_text(st: Dict[str, Any]) -> str:
    """Testo del pannello MOTORE OC (dict status → righe opzionali).

    Una riga esiste solo se il dato c'è (spec §4.3): fase SEMPRE; testing/L2/
    winner/best_known_good solo se presenti; persistito solo su fase di
    lavoro; governor/processo sempre; motore/nota solo se i campi ci sono.
    """
    state = st.get("state", {}) if isinstance(st, dict) else {}
    fresh = _fresh_state(state)
    lines: List[str] = []
    lines.append(f"fase:        {state.get('phase_label', '?')}")
    testing = state.get("testing")
    if testing and testing.get("freq"):
        vid = testing.get("vid_cap")
        t = f"{testing.get('freq')}@{vid}" if vid else f"{testing.get('freq')}"
        kind = testing.get("kind")
        if kind:
            t = f"{t} ({kind})"
        lines.append(f"testing:     {t}")
    l2 = state.get("l2") or {}
    if l2:
        lines.append(f"L2:          {l2.get('status', '-')} · "
                     f"run {l2.get('runs', '-')}")
    winner = state.get("winner") or {}
    if winner and winner.get("freq"):
        v = winner.get("vid_cap")
        w = f"{winner.get('freq')}@{v}" if v else f"{winner.get('freq')}"
        lines.append(f"winner:      {w}")
    bkg = state.get("best_known_good") or {}
    if bkg and bkg.get("freq"):
        v = bkg.get("vid_cap")
        b = f"{bkg.get('freq')}@{v}" if v else f"{bkg.get('freq')}"
        lines.append(f"migliore noto: {b}")
    if not fresh and "persisted" in state:
        lines.append(f"persistito:  "
                     f"{'sì' if state.get('persisted') else 'no'}")
    lines.append(f"governor:    {_gov_word(st.get('governor'))}")
    proc = st.get("process") if isinstance(st.get("process"), dict) else {}
    if proc.get("active"):
        lines.append(f"processo:    attivo (pid {proc.get('pid')})")
    else:
        lines.append("processo:    fermo")
    engine = st.get("engine") if isinstance(st.get("engine"), dict) else {}
    if "engine" in st:
        if engine.get("present"):
            lines.append("motore:      presente")
        else:
            lines.append("motore:      NON PRESENTE — il motore oc3600.sh "
                         "non è in OC_DIR")
            lines.append("Cosa fare: deploy del motore prima di avviare una "
                         "run (vedi ? = aiuto).")
    apply_state = st.get("apply") if isinstance(st.get("apply"), dict) else {}
    if apply_state.get("state") in ("rolled_back", "aborted", "stale"):
        # Rassicurazione: rolled_back = apply NON riuscito ma config
        # precedente RIPRISTINATA (macchina sicura) — niente toni da guasto.
        label = {"rolled_back": "non applicato (config precedente "
                                "ripristinata)",
                 "aborted": "RIFIUTATO",
                 "stale": "INTERROTTO"}.get(apply_state.get("state"))
        lines.append(f"nota:        ultimo apply {label} — controlla il log")
    return "\n".join(lines)


# CTA del pannello run quando NON c'è un run attivo (testo ESATTO §4.3; le
# parentesi quadre sono LETTERALI — il widget #run è markup=False).
RUN_EMPTY_HINT = (
    "nessun run attivo. Premi [u] per avviare la convergenza CPU — il "
    "motore esplora il tuo silicio in automatico."
)


def run_empty_hint(st: Dict[str, Any]) -> str:
    """Riga CTA "nessun run attivo" se NON c'è un processo engine e la
    fase è finale o assente (done/none/fresco); "" altrimenti — run
    attivo o fase di lavoro utile: lo stato del pannello parla da solo.
    Motore assente NON è "nessun run": niente CTA start (§4.4).
    Non sostituisce run_text(): il chiamante accoda la riga al pannello.
    """
    proc = st.get("process") if isinstance(st.get("process"), dict) else {}
    if proc.get("active"):
        return ""
    engine = st.get("engine") if isinstance(st.get("engine"), dict) else {}
    if engine.get("present") is False:
        return ""
    state = st.get("state") if isinstance(st.get("state"), dict) else {}
    if state.get("phase") in (None, "", "none", "done"):
        return RUN_EMPTY_HINT
    return ""


def profiles_table_rows(profiles: List[Any],
                        active: Optional[str] = None) -> List[Tuple[str, ...]]:
    """Righe DataTable: (nome, freq@scale, VID, valid., attivo).

    D5: validazione come PAROLE 'sì'/'no' (mai ✅); VID mancante → '—'.
    """
    rows: List[Tuple[str, ...]] = []
    for p in profiles:
        rows.append((
            p.name,
            f"{p.freq}@{p.scale}",
            str(p.vid_cap) if p.vid_cap is not None else "—",
            "sì" if p.validated else "no",
            "●" if p.id == active else "",
        ))
    return rows


def confirm_text(profile: Any, zone_ok: Tuple[bool, str]) -> str:
    """Testo della modal di conferma apply profilo (spec §4.8).

    Head INVARIATA "Applicare {name} ({freq}@{scale})?"; profilo rifiutato
    → fail-closed con motivo e via d'uscita (mai silenzioso).
    """
    head = f"Applicare {profile.name} ({profile.freq}@{profile.scale})?"
    ok, reason = zone_ok
    if not ok:
        return "\n".join([
            head,
            "",
            "[bold red]✕ RIFIUTATO — regola di sicurezza[/]",
            f"  motivo: {reason}",
            "",
            "Cosa fare: scegli un profilo più conservativo o il Certificato.",
            "R = ripristina stock (stato sicuro) · ? = aiuto",
        ])
    vid = f"VID: {profile.vid_cap} mV" if profile.vid_cap is not None \
        else "VID: —"
    valid = "sì" if profile.validated else "no"
    return "\n".join([
        head,
        "",
        f"  {vid} · validato: {valid} · persistito: no",
        "  Al reboot torna la config di sistema persistita (apply volatile).",
        "",
        "Nota: modifica hardware reale — il comportamento dipende dal "
        "silicio di questa unità. Freeze possibile (rischio noto) → "
        "power-cycle; la config persistita torna da sola al riavvio.",
        "",
        "y applica · n annulla (esc = annulla)",
    ])


def confirm_stock_text(stock: Any) -> str:
    """Conferma ripristino STOCK (R) — spec §4.8, via d'uscita sicura."""
    return "\n".join([
        "Ripristinare STOCK (CPU)?",
        "",
        f"  profilo {stock.name} · {stock.freq} MHz · scala {stock.scale} "
        "(curva di fabbrica)",
        "  volatile: al reboot torna la config di sistema persistita",
        "",
        "È la via d'uscita sicura: si riparte da una config di fabbrica.",
        "",
        "y ripristina · n annulla (esc = annulla)",
    ])


def confirm_stop_text() -> str:
    """Conferma stop run (s) — spec §4.8, checkpoint riprendibile."""
    return "\n".join([
        "Fermare la run in corso?",
        "",
        "  la fase corrente è salvata nel checkpoint: riprendi quando vuoi "
        "premendo u (la run riparte da dov'era).",
        "",
        "y ferma · n continua (esc = continua)",
    ])


# ============================================================================
# `buo oc-tui` (ALIAS della cockpit unificata — textual opzionale)
# ============================================================================


def run_oc_tui(mock: bool = False, oc_dir=None) -> int:
    """Avvia la cockpit OC (v1.2: ALIAS della cockpit unificata).

    La cockpit OC è confluita nella TUI unificata di `buo tui` (tab OC,
    stesse funzioni pure e stessi pannelli/azioni): `buo oc-tui` resta per
    retro-compatibilità e avvia la STESSA app col tab OC già attivo.
    `oc_dir` (opzione --oc-dir) viene inoltrato com'era.

    Raises:
        RuntimeError: se `textual` non è installato (guard, stile buo/tui).
    """
    from ..tui import run_tui
    return run_tui(mock=mock, mock_hardware=None, oc_dir=oc_dir,
                   initial_tab="tab-oc")
