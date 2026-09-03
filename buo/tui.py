#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
TUI — Cockpit UNIFICATO di BUO (textual), v1.2.

`buo tui` apre UNA sola app tabbed (textual TabbedContent) con:
    • tab 🖥️ Hardware — dashboard hardware live (CPU/GPU/temp/potenza/
      ventole/ambiente) + log delle letture, refresh 1s
      (ex cockpit `buo tui`)
    • tab ⚡ OC — cockpit OC completa: pannello sensori (1s), tabella
      profili, pannello run (2s), log, con le azioni apply (conferma),
      restore stock, stop/start run (ex cockpit `buo oc-tui`) + gestione
      OC/UV GPU: pannello curva attiva del governor (config.toml) e
      preset GPU validati (apply con conferma, tasto g; preset "esempi"
      validati su un'unità — il silicio varia, vedi buo/oc/gpu.py)

`buo oc-tui` è rimasto come ALIAS retro-compatibile: avvia la STESSA app
col tab OC già attivo (vedi buo/oc/tui_app.run_oc_tui). Nessuna logica
duplicata: funzioni pure condivise (dashboard_text qui; sensors_text /
run_text / profiles_table_rows / confirm_text in buo/oc/tui_app.py) e
stesso provider LiveReadings; la logica del motore OC non è toccata.

`textual` è una dipendenza OPZIONALE: senza di essa `buo tui` mostra un
messaggio chiaro e la CLI classica (rich) resta pienamente funzionante.
"""

from typing import Any, Dict, Optional

from .utils.logging import get_logger

logger = get_logger("tui")


# ============================================================================
# Fonte delle letture live (mock o hardware reale)
# ============================================================================


class LiveReadings:
    """Fornisce letture aggiornate: MockHardware o reader hardware reale."""

    def __init__(self, mock: bool = False, mock_hardware=None):
        self.mock = mock
        self.hardware = mock_hardware
        self.reader = None
        if not mock:
            from .safety.reader import RealHardwareReader
            self.reader = RealHardwareReader()

    def read(self) -> Dict[str, Any]:
        """Lettura corrente (dict piatto per la dashboard)."""
        if self.mock and self.hardware is not None:
            info = self.hardware.get_system_info()
            return {
                "cpu_cores": info.get("cpu_cores", 0),
                "cpu_freq": info.get("cpu_freq", 0),
                "cpu_vid": info.get("cpu_vid", 0),
                "cpu_temp": info.get("cpu_temp", 0),
                "gpu_cu": info.get("gpu_cu", 0),
                "gpu_freq": info.get("gpu_freq", 0),
                "gpu_voltage": info.get("gpu_voltage", 0),
                "gpu_temp": info.get("gpu_temp", 0),
                "gpu_power": info.get("gpu_power", 0),
                "total_power": info.get("total_power", 0),
                "fan_speed": info.get("fan_speed", 0),
                "ambient_temp": info.get("ambient_temp", 0),
                "undervolted": info.get("is_undervolted", False),
                "overclocked": info.get("is_overclocked", False),
                "cu40": info.get("is_40cu_enabled", False),
            }

        # Hardware reale: RealHardwareReader().get_system_info() — stesso
        # pattern di `buo status` (buo/cli.py). Tutti i campi sensore sono
        # reali; fail-soft C1: None → 0/False (come oggi), mai inventare
        # valori. FIX TUI (campo): prima si usava HardwareAudit con
        # freq/volt/power/fan hardcodati a 0 → dashboard tutta zero.
        if self.reader is not None:
            try:
                info = self.reader.get_system_info()
                return {
                    "cpu_cores": info.get("cpu_cores") or 0,
                    "cpu_freq": info.get("cpu_freq") or 0,
                    "cpu_vid": info.get("cpu_vid") or 0,
                    "cpu_temp": info.get("cpu_temp") or 0,
                    "gpu_cu": info.get("gpu_cu") or 0,
                    "gpu_freq": info.get("gpu_freq") or 0,
                    "gpu_voltage": info.get("gpu_voltage") or 0,
                    "gpu_temp": info.get("gpu_temp") or 0,
                    "gpu_power": info.get("gpu_power") or 0,
                    "total_power": info.get("total_power") or 0,
                    "fan_speed": info.get("fan_speed") or 0,
                    "ambient_temp": info.get("ambient_temp") or 0,
                    "undervolted": bool(info.get("is_undervolted")),
                    "overclocked": bool(info.get("is_overclocked")),
                    "cu40": bool(info.get("is_40cu_enabled")),
                }
            except Exception as e:
                logger.debug("Lettura hardware fallita: %s", e)

        return {}


# ============================================================================
# Testo della dashboard (funzione pura, testabile senza terminale)
# ============================================================================


def dashboard_text(r: Dict[str, Any]) -> str:
    """Compone il testo della dashboard da un dict di letture."""
    W = 43  # larghezza interna del riquadro

    def row(content: str) -> str:
        return "│" + content.ljust(W) + "│"

    def sep(start: str, end: str) -> str:
        return start + "─" * W + end

    lines = []
    lines.append(sep("┌", "┐"))
    lines.append(row("🔍 STATO HARDWARE — LIVE"))
    lines.append(sep("├", "┤"))

    cpu_temp = float(r.get("cpu_temp", 0))
    cpu_ok = "✅" if cpu_temp < 90 else "🔴"
    uv = " 🔽 undervolt" if r.get("undervolted") else ""
    oc = " ⬆️ OC" if r.get("overclocked") else ""
    lines.append(row(f"🧠 CPU:  {r.get('cpu_cores', 0)} core  "
                     f"{r.get('cpu_freq', 0)} MHz  {cpu_temp:.1f}°C {cpu_ok}"))
    lines.append(row(f"          VID {r.get('cpu_vid', 0)} mV{uv}{oc}"))

    gpu_temp = float(r.get("gpu_temp", 0))
    gpu_ok = "✅" if gpu_temp < 85 else "🔴"
    cu_label = f"{r.get('gpu_cu', 0)} CU"
    if r.get("cu40"):
        cu_label += " (40)"
    lines.append(row(f"🎮 GPU:  {cu_label:<9} {r.get('gpu_freq', 0)} MHz  "
                     f"{gpu_temp:.1f}°C {gpu_ok}"))
    lines.append(row(f"          {r.get('gpu_voltage', 0)} mV  "
                     f"{r.get('gpu_power', 0)} W"))

    lines.append(sep("├", "┤"))
    lines.append(row(f"⚡ Potenza: {float(r.get('total_power', 0)):.1f} W    "
                     f"💨 {r.get('fan_speed', 0)} RPM"))
    lines.append(row(f"🌡 Ambiente: {float(r.get('ambient_temp', 0)):.1f}°C"))
    lines.append(sep("└", "┘"))
    return "\n".join(lines)


# ============================================================================
# Aiuto/disclaimer del cockpit (funzioni pure, testabili senza textual)
# ============================================================================

# Riga di disclaimer del tab OC (una riga, non invadente). Unica fonte
# della frase: riusata in help_text() e nell'intestazione del tab.
OC_DISCLAIMER = (
    "⚠️ OC/UV sperimentale — preset validati, silicio variabile: "
    "freeze possibili · R ripristina stock"
)


def help_text() -> str:
    """Testo completo della schermata aiuto del cockpit (tasto ?).

    Funzione pura (testata senza terminale). Onestà C1: nessuna garanzia
    inventata — l'OC/UV modifica hardware reale e dipende dal silicio di
    ogni unità; freeze possibili, via d'uscita sempre indicata.
    """
    return f"""\
🛟 AIUTO — Cockpit OC/UV (CPU + GPU)

Cosa è: cockpit OC/UV semi-automatico per-silicio per ASRock BC-250.
CPU: profili del motore oc3600.sh. GPU: preset del governor (curva V/F).
Applica SOLO preset validati con stress reale e chiede conferma prima di
ogni modifica (fail-closed): mai valori a caso.

{OC_DISCLAIMER}
L'OC/UV modifica parametri hardware reali: su questa piattaforma un punto
instabile può causare un FREEZE del SoC (schermo bloccato, nessun errore
a schermo) → serve un power-cycle. Il tool applica solo preset validati e
con conferma, ma il comportamento dipende dal silicio di OGNI unità.

Se qualcosa sembra sbagliato:
  • R = ripristina stock (CPU) — si riparte da una config sicura;
    per la GPU applica il preset Stock-cap 1500;
  • applica un preset più conservativo;
  • leggi il log del tab (ultime righe del motore);
  • se la macchina si congela: power-cycle — al riavvio le config
    persistite (CPU/GPU) vengono riapplicate e lo stato è rilevato.

Nota preset: gli esempi GPU/CPU sono validati su UN'unità: il tuo
silicio può differire — inizia dai preset conservativi.

Tasti: q esci · ? aiuto · space/r refresh · a applica profilo CPU ·
R ripristina stock · g applica preset GPU · s stop run · u start run
"""


def actions_strip_text() -> str:
    """Barra azioni del tab OC (una riga, wrap ok): i flussi primari
    (avvio/stop run CPU, preset GPU, stock, aiuto) sempre visibili —
    non solo nel Footer di textual. Le parentesi quadre sono LETTERALI
    (il widget #actions è markup=False)."""
    return ("⚡ CPU: [u] avvia run motore · [s] stop — "
            "GPU: ↑/↓ scegli preset · [g] applica — "
            "[R] stock · [?] aiuto")


# ============================================================================
# App TUI unificata (definita solo quando textual è disponibile)
# ============================================================================


def run_tui(mock: bool = False, mock_hardware=None, oc_dir=None,
            initial_tab: str = "tab-hw") -> int:
    """
    Avvia il cockpit unificato (tab Hardware + tab OC).

    `buo oc-tui` è un ALIAS: buo/oc/tui_app.run_oc_tui delega qui con
    initial_tab="tab-oc" (inoltrando l'eventuale `oc_dir` di --oc-dir).

    Args:
        mock: hardware simulato (MockHardware) invece del reader reale.
        mock_hardware: istanza MockHardware da usare in modalità mock.
        oc_dir: override OC_DIR per il tab OC (default OC_DIR_DEFAULT).
        initial_tab: id del TabPane attivo all'avvio ("tab-hw" | "tab-oc").

    Raises:
        RuntimeError: se `textual` non è installato
    """
    import importlib.util
    if importlib.util.find_spec("textual") is None:
        raise RuntimeError(
            "TUI non disponibile: installa la dipendenza opzionale "
            "con: pip install textual   (o: pip install -e '.[tui]')"
        )

    from pathlib import Path

    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import ModalScreen
    from textual.widgets import (
        DataTable,
        Footer,
        Header,
        Static,
        TabbedContent,
        TabPane,
    )

    # Riuso (nessuna duplicazione): funzioni pure della cockpit OC e motore
    # OC (solo controllo/lettura — la logica del motore non è toccata).
    from .oc.constants import OC_DIR_DEFAULT
    from .oc.controller import OcController
    from .oc.gpu import (
        DEFAULT_GPU_PRESETS,
        active_gpu_preset,
        apply_gpu_preset,
        gpu_apply_text,
        gpu_panel_text,
        gpu_preset_rows,
        read_active_curve,
    )
    from .oc.profiles import ProfileStore, ProfileValidator
    from .oc.tui_app import (
        confirm_text,
        profiles_table_rows,
        run_empty_hint,
        run_text,
        sensors_text,
    )
    from .optimize.governor import GovernorWrapper

    if mock and mock_hardware is None:
        # Parity con `buo tui --mock` (buo/cli.py inietta MockHardware):
        # `buo oc-tui --mock` vede comunque un hardware simulato live.
        from .utils.mock import MockHardware
        mock_hardware = MockHardware()

    readings = LiveReadings(mock=mock, mock_hardware=mock_hardware)
    oc = Path(oc_dir) if oc_dir else Path(OC_DIR_DEFAULT)
    ctl = OcController(oc_dir=oc, mock=mock)
    store = ProfileStore(oc)
    validator = ProfileValidator()
    # GPU OC/UV: wrapper del governor (apply) — la LETTURA della curva
    # attiva passa da read_active_curve (mock → fixture, mai /etc).
    gpu_gov = GovernorWrapper(mock=mock)

    class ConfirmModal(ModalScreen):
        """Modal di conferma generica (y applica / n annulla): usata sia
        per l'apply dei profili CPU sia per i preset GPU."""

        BINDINGS = [Binding("y", "yes", "Applica"),
                    Binding("n", "no", "Annulla")]

        def __init__(self, text: str, on_yes):
            super().__init__()
            self._text = text
            self._on_yes = on_yes

        def compose(self) -> ComposeResult:
            yield Static(self._text)

        def action_yes(self) -> None:
            self._on_yes()
            self.dismiss()

        def action_no(self) -> None:
            self.dismiss()

    class HelpScreen(ModalScreen):
        """Schermata aiuto/disclaimer del cockpit (tasto ?).

        Testo da help_text() (funzione pura, testata). Scroll verticale
        per terminali corti; chiudi con q / esc / ? (toggle).
        """

        BINDINGS = [
            Binding("q", "close_help", "Chiudi"),
            Binding("escape", "close_help", "Chiudi"),
            Binding("?", "close_help", "Chiudi"),
        ]
        CSS = """
        HelpScreen { align: center middle; }
        #help-box {
            width: 90%;
            height: 90%;
            border: round $accent;
            padding: 1;
        }
        """

        def compose(self) -> ComposeResult:
            with VerticalScroll(id="help-box"):
                yield Static(help_text())

        def action_close_help(self) -> None:
            self.dismiss()

    class CockpitApp(App):
        """Cockpit unificato: tab Hardware (dashboard live) + tab OC."""

        TITLE = "BC-250 Ultimate Orchestrator"
        SUB_TITLE = "Cockpit — MOCK" if mock else "Cockpit — hw reale"
        CSS = """
        TabbedContent { height: 1fr; }
        ContentSwitcher { height: 1fr; }
        TabPane { height: 1fr; }

        #dashboard { width: 1fr; padding: 1; border: round $primary; }
        #logbox { width: 1fr; padding: 1; border: round $secondary;
                  height: 1fr; }
        #log { height: 1fr; }

        #sensors { border: round $primary; padding: 1; }
        #run { border: round $secondary; padding: 1; }
        #profiles { border: round $accent; height: 9; }
        #oclog { border: round $warning; padding: 1; height: 1fr; }

        #gpu { border: round $accent; padding: 1; }
        #gpu-presets { border: round $accent; height: 4; }

        #disclaimer, #actions { text-style: dim; }
        """

        BINDINGS = [
            Binding("q", "quit", "Esci"),
            Binding("space", "refresh_now", "Aggiorna"),
            Binding("r", "refresh_now", "Refresh"),
            Binding("a", "apply_selected", "Applica profilo"),
            Binding("R", "restore_stock", "Ripristina stock"),
            Binding("g", "apply_gpu", "Applica preset GPU"),
            Binding("s", "stop_run", "Stop run"),
            Binding("u", "start_run", "Start run"),
            Binding("?", "show_help", "Aiuto"),
        ]

        def __init__(self):
            super().__init__()
            self._timer_sensors = None
            self._timer_run = None

        def compose(self) -> ComposeResult:
            yield Header()
            with TabbedContent(initial=initial_tab):
                with TabPane("Hardware", id="tab-hw"):
                    with Horizontal():
                        yield Static(dashboard_text(readings.read()),
                                     id="dashboard")
                        with VerticalScroll(id="logbox"):
                            yield Static("📋 Log letture:\n", id="log")
                with TabPane("OC", id="tab-oc"):
                    yield Static(OC_DISCLAIMER, id="disclaimer")
                    yield Static(actions_strip_text(), id="actions",
                                 markup=False)
                    with Horizontal():
                        with Vertical():
                            yield Static(sensors_text({}), id="sensors")
                            yield DataTable(id="profiles")
                        with Vertical():
                            yield Static(run_text({}), id="run",
                                         markup=False)
                            yield Static("log: —", id="oclog")
                    yield Static(gpu_panel_text(None, None, "?"), id="gpu",
                                 markup=False)
                    yield DataTable(id="gpu-presets")
            yield Footer()

        def on_mount(self) -> None:
            table = self.query_one("#profiles", DataTable)
            table.add_columns("nome", "freq@scale", "VID", "valid.", "attivo")
            gpu_table = self.query_one("#gpu-presets", DataTable)
            gpu_table.add_columns("preset", "curva", "stato")
            self._refresh_all()
            self._timer_sensors = self.set_interval(1.0, self._refresh_sensors)
            self._timer_run = self.set_interval(2.0, self._refresh_run)

        # --------------------- refresh pannelli --------------------- #

        def _refresh_hw(self) -> None:
            r = readings.read()
            self.query_one("#dashboard", Static).update(dashboard_text(r))
            self.query_one("#log", Static).update(
                "📋 Log letture:\n\n"
                f"CPU {r.get('cpu_temp', 0):.1f}°C | "
                f"GPU {r.get('gpu_temp', 0):.1f}°C | "
                f"Power {float(r.get('total_power', 0)):.1f}W\n")

        def _read_oc_sensors(self) -> Dict[str, Any]:
            """Lettura RAW per il pannello sensori OC (None conservati:
            il 🔒 gated del VID/SoC resta onesto, come nella cockpit OC)."""
            if mock:
                from .utils.mock import MockHardware
                return MockHardware().get_system_info()
            try:
                from .safety.reader import RealHardwareReader
                return RealHardwareReader().get_system_info()
            except Exception:
                return {}

        def _refresh_sensors(self) -> None:
            self._refresh_hw()
            self.query_one("#sensors", Static).update(
                sensors_text(self._read_oc_sensors()))

        def _refresh_profiles(self) -> None:
            table = self.query_one("#profiles", DataTable)
            table.clear()
            for row in profiles_table_rows(store.load()):
                table.add_row(*row)

        def _refresh_run(self) -> None:
            """Pannello run + log OC e stato GPU (tick 2s)."""
            st = ctl.status()
            text = run_text(st)
            hint = run_empty_hint(st)
            if hint:
                text = f"{text}\n\n{hint}"
            self.query_one("#run", Static).update(text)
            tail = st.get("log_tail") or []
            self.query_one("#oclog", Static).update(
                "\n".join(tail[-6:]) or "log: —")
            self._refresh_gpu()

        def _refresh_gpu(self) -> None:
            """Stato GPU: curva attiva (config.toml) + preset corrispondente
            + righe dei preset. Mock → fixture (mai /etc); fail-soft."""
            curve = read_active_curve(mock=mock)
            preset = active_gpu_preset(curve)
            gov = ("simulato" if mock else
                   ("attivo" if gpu_gov.is_running() else "fermo"))
            self.query_one("#gpu", Static).update(
                gpu_panel_text(curve, preset, gov))
            table = self.query_one("#gpu-presets", DataTable)
            table.clear()
            for row in gpu_preset_rows(
                    DEFAULT_GPU_PRESETS,
                    active=preset.id if preset else None):
                table.add_row(*row)

        def _refresh_all(self) -> None:
            self._refresh_sensors()
            self._refresh_profiles()
            self._refresh_run()

        def action_refresh_now(self) -> None:
            self._refresh_all()

        # --------------------- azioni OC (tab OC) --------------------- #

        def _oc_log(self, msg: str) -> None:
            """Scrive nel log del tab attivo (il messaggio resta visibile)."""
            target = "#oclog"
            if self.query_one(TabbedContent).active == "tab-hw":
                target = "#log"
            self.query_one(target, Static).update(msg)

        def _make_reader(self):
            if mock:
                from .utils.mock import MockHardware
                return MockHardware()
            try:
                from .safety.reader import RealHardwareReader
                return RealHardwareReader()
            except Exception:
                return None

        def action_apply_selected(self) -> None:
            table = self.query_one("#profiles", DataTable)
            row = table.cursor_row
            profiles = store.load()
            if row is None or row >= len(profiles):
                return
            profile = profiles[row]

            def on_yes() -> None:
                from .oc.apply import ApplyManager
                from .oc.smoke import CpuSmoke
                apply_ctl = OcController(oc_dir=oc, mock=mock)
                smoke = CpuSmoke(reader=self._make_reader(), mock=mock,
                                 oc_dir=oc)
                mgr = ApplyManager(apply_ctl, store=store,
                                   validator=validator, smoke=smoke,
                                   reader=self._make_reader(), mock=mock,
                                   oc_dir=oc)
                outcome = mgr.apply(profile, persist=False, yes=True,
                                    on_progress=self._oc_log)
                self._refresh_profiles()
                self._refresh_run()
                self._oc_log(
                    f"apply {profile.name}: {outcome.result}"
                    + (f" — {outcome.cause}" if outcome.cause else ""))

            text = confirm_text(profile, validator.zone_ok(profile))
            self.push_screen(ConfirmModal(text, on_yes))

        def action_apply_gpu(self) -> None:
            """Applica il preset GPU selezionato (#gpu-presets, tasto g)."""
            table = self.query_one("#gpu-presets", DataTable)
            row = table.cursor_row
            if row is None or row >= len(DEFAULT_GPU_PRESETS):
                return
            preset = DEFAULT_GPU_PRESETS[row]

            def on_yes() -> None:
                res = apply_gpu_preset(gpu_gov, preset)
                if res["ok"]:
                    self._oc_log(
                        f"GPU apply {preset.name}: ok — curva riscritta e "
                        "governor riavviato")
                else:
                    self._oc_log(
                        f"GPU apply {preset.name}: ✗ {res.get('reason')}")
                self._refresh_run()

            self.push_screen(ConfirmModal(gpu_apply_text(preset), on_yes))

        def action_restore_stock(self) -> None:
            from .oc.apply import ApplyManager
            from .oc.smoke import CpuSmoke
            mgr = ApplyManager(ctl, store=store, validator=validator,
                               smoke=CpuSmoke(self._make_reader(), mock=mock,
                                              oc_dir=oc),
                               reader=self._make_reader(), mock=mock,
                               oc_dir=oc)
            outcome = mgr.restore_stock(persist=False, yes=True)
            self._oc_log(f"restore-stock: {outcome.result}")
            self._refresh_profiles()

        def action_stop_run(self) -> None:
            ctl.stop()
            self._refresh_run()

        def action_start_run(self) -> None:
            try:
                ctl.start([])
            except RuntimeError as e:
                self._oc_log(f"✗ {e}")
            self._refresh_run()

        def action_show_help(self) -> None:
            self.push_screen(HelpScreen())

        def on_unmount(self) -> None:
            for timer in (self._timer_sensors, self._timer_run):
                if timer is not None:
                    timer.stop()

    app = CockpitApp()
    app.run()
    return 0
