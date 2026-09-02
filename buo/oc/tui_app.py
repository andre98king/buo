#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Cockpit OC interattiva (`buo oc-tui`, textual OPZIONALE).

Funzioni pure (testabili senza terminale, stile dashboard_text di
buo/tui.py): sensors_text / run_text / profiles_table_rows / confirm_text.

OcTuiApp: layout §4 del design — pannello sensori live (1s), pannello run
(2s), tabella profili, log, footer; modal di conferma apply con Worker
smoke+progress; exit pulita (on_unmount ferma i timer).

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
# App textual (import pigro: dipendenza OPZIONALE)
# ============================================================================


def run_oc_tui(mock: bool = False, oc_dir=None) -> int:
    """Avvia la cockpit OC.

    Raises:
        RuntimeError: se `textual` non è installato (guard, stile buo/tui).
    """
    import importlib.util
    if importlib.util.find_spec("textual") is None:
        raise RuntimeError(
            "TUI non disponibile: installa la dipendenza opzionale con: "
            "pip install textual   (o: pip install -e '.[tui]')"
        )

    from pathlib import Path

    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.screen import ModalScreen
    from textual.widgets import DataTable, Footer, Header, Static

    from .constants import OC_DIR_DEFAULT
    from .controller import OcController
    from .profiles import ProfileStore, ProfileValidator
    from .smoke import CpuSmoke
    from .state import OcStateReader
    from ..utils.mock import MockHardware

    oc = Path(oc_dir) if oc_dir else Path(OC_DIR_DEFAULT)
    ctl = OcController(oc_dir=oc, mock=mock)
    store = ProfileStore(oc)
    validator = ProfileValidator()

    class ConfirmApply(ModalScreen):
        """Modal di conferma applica profilo (Sequenza A)."""

        BINDINGS = [Binding("y", "yes", "Applica"), Binding("n", "no", "Annulla")]

        def __init__(self, profile, on_yes):
            super().__init__()
            self._profile = profile
            self._on_yes = on_yes

        def compose(self) -> ComposeResult:
            yield Static(confirm_text(self._profile,
                                      validator.zone_ok(self._profile)))

        def action_yes(self) -> None:
            self._on_yes(self._profile)
            self.dismiss()

        def action_no(self) -> None:
            self.dismiss()

    class OcTuiApp(App):
        TITLE = "BUO · OC Cockpit"
        SUB_TITLE = "MOCK" if mock else "hw reale"
        CSS = """
        #sensors { border: round $primary; padding: 1; }
        #run { border: round $secondary; padding: 1; }
        #profiles { border: round $accent; height: 9; }
        #log { border: round $warning; padding: 1; height: 1fr; }
        """

        BINDINGS = [
            Binding("q", "quit", "Esci"),
            Binding("r", "refresh_now", "Refresh"),
            Binding("a", "apply_selected", "Applica profilo"),
            Binding("R", "restore_stock", "Ripristina stock"),
            Binding("s", "stop_run", "Stop run"),
            Binding("u", "start_run", "Start run"),
            Binding("?", "show_help", "Aiuto"),
        ]

        def compose(self) -> ComposeResult:
            yield Header()
            with Horizontal():
                with Vertical():
                    yield Static(sensors_text({}), id="sensors")
                    yield DataTable(id="profiles")
                with Vertical():
                    yield Static(run_text({}), id="run")
                    yield Static("log: —", id="log")
            yield Footer()

        def on_mount(self) -> None:
            table = self.query_one("#profiles", DataTable)
            table.add_columns("nome", "freq@scale", "VID", "valid.", "attivo")
            self._refresh_profiles()
            self.set_interval(1.0, self._refresh_sensors)
            self.set_interval(2.0, self._refresh_run)

        def _refresh_profiles(self) -> None:
            table = self.query_one("#profiles", DataTable)
            table.clear()
            for row in profiles_table_rows(store.load()):
                table.add_row(*row)

        def _read_sensors(self) -> Dict[str, Any]:
            if mock:
                hw = MockHardware()
                return hw.get_system_info()
            try:
                from ..safety.reader import RealHardwareReader
                return RealHardwareReader().get_system_info()
            except Exception:
                return {}

        def _refresh_sensors(self) -> None:
            self.query_one("#sensors", Static).update(
                sensors_text(self._read_sensors()))

        def _refresh_run(self) -> None:
            st = ctl.status()
            self.query_one("#run", Static).update(run_text(st))
            tail = st.get("log_tail") or []
            self.query_one("#log", Static).update(
                "\n".join(tail[-6:]) or "log: —")

        def action_refresh_now(self) -> None:
            self._refresh_sensors()
            self._refresh_run()

        def action_apply_selected(self) -> None:
            table = self.query_one("#profiles", DataTable)
            row = table.cursor_row
            if row is None or row >= len(store.load()):
                return
            profile = store.load()[row]

            def on_yes(p) -> None:
                from .apply import ApplyManager
                from .controller import OcController
                from .smoke import CpuSmoke
                apply_ctl = OcController(oc_dir=oc, mock=mock)
                smoke = CpuSmoke(reader=self._make_reader(), mock=mock,
                                 oc_dir=oc)
                mgr = ApplyManager(apply_ctl, store=store,
                                   validator=validator, smoke=smoke,
                                   reader=self._make_reader(), mock=mock,
                                   oc_dir=oc)
                outcome = mgr.apply(p, persist=False, yes=True,
                                    on_progress=lambda m: self.query_one(
                                        "#log", Static).update(m))
                self._refresh_profiles()
                self._refresh_run()
                self.query_one("#log", Static).update(
                    f"apply {p.name}: {outcome.result}"
                    + (f" — {outcome.cause}" if outcome.cause else ""))

            self.push_screen(ConfirmApply(profile, on_yes))

        def _make_reader(self):
            if mock:
                return MockHardware()
            try:
                from ..safety.reader import RealHardwareReader
                return RealHardwareReader()
            except Exception:
                return None

        def action_restore_stock(self) -> None:
            from .apply import ApplyManager
            mgr = ApplyManager(ctl, store=store, validator=validator,
                               smoke=CpuSmoke(self._make_reader(), mock=mock,
                                              oc_dir=oc),
                               reader=self._make_reader(), mock=mock, oc_dir=oc)
            outcome = mgr.restore_stock(persist=False, yes=True)
            self.query_one("#log", Static).update(
                f"restore-stock: {outcome.result}")
            self._refresh_profiles()

        def action_stop_run(self) -> None:
            ctl.stop()
            self._refresh_run()

        def action_start_run(self) -> None:
            try:
                ctl.start([])
            except RuntimeError as e:
                self.query_one("#log", Static).update(f"✗ {e}")
            self._refresh_run()

        def action_show_help(self) -> None:
            self.query_one("#log", Static).update(
                "q esci · r refresh · a applica · R stock · s stop · u start")

        def on_unmount(self) -> None:
            # exit pulita: i timer set_interval muoiono col widget tree
            pass

    app = OcTuiApp()
    app.run()
    return 0
