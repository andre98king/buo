#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
TUI — Cockpit UNIFICATO di BUO (textual), v1.3.

`buo tui` apre UNA sola app tabbed (textual TabbedContent) con:
    • tab Hardware — dashboard hardware live (CPU/GPU/temp/potenza/
      ventole/ambiente) + log delle letture con timestamp, refresh 1s
      (ex cockpit `buo tui`)
    • tab OC — cockpit OC a colonna scrollabile (riga azioni fissa):
      striscia sensori (1s), pannello MOTORE OC, tabella profili,
      GPU (curva + preset), log; azioni apply (conferma), restore
      stock, stop/start run (ex cockpit `buo oc-tui`) + gestione
      OC/UV GPU. Apply/restore girano in un Worker textual (mai UI
      congelata dallo smoke 30 s); preset "esempi" validati su
      un'unità — il silicio varia, vedi buo/oc/gpu.py.
      Revamp UX (research/UX_REVAMP_SPEC): testi italiani, glifi sobri,
      "—" per i valori non rilevabili (C1), conferme per R e s.

`buo oc-tui` è rimasto come ALIAS retro-compatibile: avvia la STESSA app
col tab OC già attivo (vedi buo/oc/tui_app.run_oc_tui). Nessuna logica
duplicata: funzioni pure condivise (dashboard_text qui; sensors_text /
run_text / profiles_table_rows / confirm_text / confirm_stock_text /
confirm_stop_text in buo/oc/tui_app.py) e stesso provider LiveReadings;
la logica del motore OC non è toccata.

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


def dashboard_text(r: Dict[str, Any], mock: bool = False) -> str:
    """Compone il testo della dashboard da un dict di letture (spec §3.2/3.3).

    Regole C1: sensore assente/0 in RESA → '—' (mai '0 MHz'); quando l'INTERO
    gruppo CPU (o GPU) è irrilevabile la riga si compatta con "— (non
    rilevabile)"; dict {} (lettura/eccezione reader) → box di STATO con cosa
    fare, MAI 0°C. Stati termici come PAROLE: ok / CRITICO. mock=True →
    suffisso "· MOCK" nel titolo (mai dimenticabile). Riga "Stato:" con i tre
    flag del dict live (undervolt/OC/40-CU), mai inventare altri campi.
    """
    W = 45  # larghezza interna del riquadro (47 con i bordi)

    def row(content: str) -> str:
        return "│" + content.ljust(W) + "│"

    def sep(start: str, end: str) -> str:
        return start + "─" * W + end

    title = " STATO HARDWARE · LIVE" + (" · MOCK" if mock else "")
    if not r:
        return "\n".join([
            sep("┌", "┐"), row(title), sep("├", "┤"),
            row(" Letture non disponibili"),
            row(" Il reader non risponde (permessi?)"),
            row(" r = riprova · ? = aiuto"),
            sep("└", "┘"),
        ])

    cores = int(r.get("cpu_cores") or 0)
    freq = int(r.get("cpu_freq") or 0)
    ctemp = float(r.get("cpu_temp") or 0)
    vid = int(r.get("cpu_vid") or 0)
    uv = bool(r.get("undervolted"))
    oc = bool(r.get("overclocked"))
    cu40 = bool(r.get("cu40"))
    cu = int(r.get("gpu_cu") or 0)
    gfreq = int(r.get("gpu_freq") or 0)
    gtemp = float(r.get("gpu_temp") or 0)
    gv = int(r.get("gpu_voltage") or 0)
    gp = r.get("gpu_power")
    soc = float(r.get("total_power") or 0)
    fan = int(r.get("fan_speed") or 0)
    amb = float(r.get("ambient_temp") or 0)

    # ---- blocco CPU (2 righe: valori + VID/flags) ----
    c_parts = []
    if cores:
        c_parts.append(f"{cores} core")
    if freq:
        c_parts.append(f"{freq} MHz")
    if ctemp:
        c_parts.append(f"{ctemp:.1f}°C")
    c_word = ""
    if ctemp:
        c_word = "ok" if ctemp < 90 else "CRITICO"
    if c_parts:
        cpu1 = " CPU   " + " · ".join(c_parts)
        if c_word:
            cpu1 += "  " + c_word
        vid_parts = [f"VID {vid} mV" if vid else "VID —"]
        if uv:
            vid_parts.append("undervolt")
        if oc:
            vid_parts.append("OC")
        cpu2 = "       " + " · ".join(vid_parts)
    else:
        cpu1 = " CPU   — (non rilevabile)"
        cpu2 = f"       VID — · undervolt {'sì' if uv else 'no'}"

    # ---- blocco GPU (2 righe: valori + tensione/potenza) ----
    cu_label = ""
    if cu:
        cu_label = f"{cu} CU"
    elif cu40:
        cu_label = "40 CU (attive)"
    g_parts = []
    if cu_label:
        g_parts.append(cu_label)
    if gfreq:
        g_parts.append(f"{gfreq} MHz")
    if gtemp:
        g_parts.append(f"{gtemp:.1f}°C")
    g_word = ""
    if gtemp:
        g_word = "ok" if gtemp < 85 else "CRITICO"
    if g_parts:
        gpu1 = " GPU   " + " · ".join(g_parts)
        if g_word:
            gpu1 += "  " + g_word
        d_parts = []
        if gv:
            d_parts.append(f"{gv} mV")
        if gp:
            d_parts.append(f"{gp:g} W")
        gpu2 = "       " + " · ".join(d_parts) if d_parts else ""
    else:
        gpu1 = " GPU   — (non rilevabile)"
        gpu2 = ""

    # ---- righe basse (potenza/ventola/ambiente + stato ottimizzazioni) ----
    soc_s = f"{soc:.1f} W" if soc else "—"
    fan_s = f"{fan} RPM" if fan else "—"
    amb_s = f"{amb:.1f}°C" if amb else "—"
    stato = (f" Stato: undervolt {'sì' if uv else 'no'} · "
             f"OC {'sì' if oc else 'no'} · 40-CU {'sì' if cu40 else 'no'}")

    lines = [sep("┌", "┐"), row(title), sep("├", "┤"),
             row(cpu1), row(cpu2), row(gpu1)]
    if gpu2:
        lines.append(row(gpu2))
    lines += [sep("├", "┤"),
              row(f" SoC   {soc_s} · ventola {fan_s}"),
              row(f" Amb   {amb_s}"),
              row(stato),
              sep("└", "┘")]
    return "\n".join(lines)


# ============================================================================
# Aiuto/disclaimer del cockpit (funzioni pure, testabili senza textual)
# ============================================================================

# Riga di disclaimer del tab OC (una riga fissa, spec §2.6). Unica fonte
# della frase: riusata in help_text() e nell'intestazione del tab.
# markup=False sul widget → le parentesi quadre sono LETTERALI.
OC_DISCLAIMER = (
    "OC/UV sperimentale — preset validati, silicio variabile: freeze "
    "possibili · [R] ripristina stock · [?] aiuto"
)


def help_text() -> str:
    """Testo completo della schermata aiuto del cockpit (tasto ?).

    Funzione pura (testata senza terminale), spec §2.5. Onestà C1: nessuna
    garanzia inventata — l'OC/UV modifica hardware reale e dipende dal
    silicio di ogni unità; freeze possibili, via d'uscita sempre indicata.
    Include OC_DISCLAIMER verbatim (unica fonte del messaggio).
    """
    return f"""\
AIUTO — Cockpit BUO (OC/UV per-silicio su ASRock BC-250)

Cosa è
  Cockpit in due schede: "Hardware" monitora la macchina (letture live
  ogni 1 s), "OC" gestisce l'overclock/undervolt del tuo silicio.
  CPU: profili del motore oc3600.sh. GPU: preset del governor (curva V/F).
  Il tool applica SOLO preset validati con stress reale e chiede conferma
  prima di ogni modifica (fail-closed): mai valori a caso.

{OC_DISCLAIMER}

Sicurezza (leggimi)
  L'OC/UV modifica parametri hardware reali: un punto instabile può
  causare un FREEZE del SoC (schermo bloccato, nessun errore a schermo).
  È un rischio noto di questa piattaforma → serve un power-cycle.
  Al riavvio la config di sistema persistita viene riapplicata da sola:
  non perdi nulla di permanente.
  I preset di esempio sono validati su UN'unità: il tuo silicio può
  differire — inizia dai preset conservativi.

Se qualcosa sembra sbagliato
  • R = ripristina stock (CPU): si riparte da una config sicura.
    Per la GPU applica il preset "Stock-cap 1500".
  • applica un preset più conservativo.
  • leggi il log (ultime righe in fondo alla scheda OC).
  • se la macchina si congela: power-cycle — le config persistite
    (CPU/GPU) vengono riapplicate al riavvio e lo stato è rilevato.

Tasti — navigazione
  q                esci · ? aiuto (chiudi con q/esc)
  ctrl+tab         alterna scheda Hardware/OC
  r / spazio       aggiorna subito

Tasti — scheda OC (azioni)
  u                avvia la run di convergenza (esplora il silicio)
  s                ferma la run (checkpoint salvato: riprendi con u)
  a                applica il profilo CPU selezionato (conferma)
  R                ripristina stock CPU (conferma)
  g                applica il preset GPU selezionato (conferma)
  ↑ / ↓            scelgono la riga nelle tabelle (profili / preset GPU)

Conferme
  y applica · n annulla (esc = annulla)
"""


def actions_strip_text() -> str:
    """Barra azioni del tab OC (UNA riga, spec §2.7): i flussi primari
    (avvio/stop run CPU, preset GPU, stock, aiuto) sempre visibili —
    non solo nel Footer di textual. Le parentesi quadre sono LETTERALI
    (il widget #actions è markup=False)."""
    return ("CPU: [u] avvia run motore · [s] stop · [a] applica profilo · "
            "[R] stock  — GPU: [g] applica preset · ↑/↓ scegli riga · "
            "[r] aggiorna · [?] aiuto")


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

    import time
    from collections import deque
    from pathlib import Path

    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, VerticalScroll
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
    from .oc.constants import OC_DIR_DEFAULT, SMOKE_STRESS_S
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
        confirm_stock_text,
        confirm_stop_text,
        confirm_text,
        profiles_table_rows,
        run_empty_hint,
        run_text,
        sensors_text,
    )
    from .optimize.governor import GovernorWrapper

    def _esc(s: str) -> str:
        """Escapa '[' per i widget con markup: testo LETTERALE a schermo."""
        return s.replace("[", "\\[")

    # Stato del pannello MOTORE OC mentre il Worker applica (spec §4.3):
    # la UI resta viva durante lo smoke test.
    BUSY_TEXT = (
        f"APPLICAZIONE IN CORSO — smoke test {SMOKE_STRESS_S} s…\n"
        'non usare i tasti a/g/R/s finché non torna "pronto".\n'
        "Avanzamento: le righe del tool compaiono nel LOG qui sotto."
    )

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
        """Modal di conferma (y applica / n annulla / esc = annulla):
        usata per l'apply dei profili CPU, i preset GPU, il ripristino
        stock (R) e lo stop run (s). Il testo arriva dalle funzioni pure
        confirm_text / gpu_apply_text / confirm_stock_text /
        confirm_stop_text (spec §4.8)."""

        BINDINGS = [Binding("y", "yes", "Applica"),
                    Binding("n", "no", "Annulla"),
                    Binding("escape", "no", "Annulla")]

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
        """Cockpit unificato: tab Hardware (dashboard live) + tab OC.

        Revamp UX (spec UX_REVAMP_SPEC): apply/restore in Worker textual
        (mai il message pump bloccato dallo smoke 30 s), tab OC a colonna
        scrollabile con riga azioni/disclaimer FISSA, selezione visibile
        (cursor_type row) con default riga 0, conferme per R e s,
        log con timestamp, niente emoji decorative.
        """

        TITLE = "BC-250 Ultimate Orchestrator"
        SUB_TITLE = ("Cockpit — MOCK (dati simulati)" if mock
                     else "Cockpit — hardware reale")
        CSS = """
        TabbedContent { height: 1fr; }
        ContentSwitcher { height: 1fr; }
        TabPane { height: 1fr; }

        /* tab Hardware: dashboard a larghezza fissa + log che scrolla */
        #dashboard { width: 47; }
        #logbox { width: 1fr; height: 1fr; border: round $secondary;
                  padding: 0 1; }
        #log { height: auto; }

        /* tab OC: disclaimer + azioni FISSI, contenuto scrollabile */
        #disclaimer, #actions { text-style: dim; }
        #oc-scroll { height: 1fr; }

        #sensors { border: round $primary; padding: 0 1; margin: 0 0 1 0; }
        #run { border: round $secondary; padding: 0 1; margin: 0 0 1 0; }
        #profiles { border: round $accent; height: auto; max-height: 10;
                    margin: 0 0 1 0; }
        #gpu { border: round magenta; padding: 0 1; margin: 0 0 1 0; }
        #gpu-presets { border: round magenta; height: auto; max-height: 6;
                       margin: 0 0 1 0; }
        #oclog { border: round $warning; padding: 0 1; }
        """

        BINDINGS = [
            Binding("q", "quit", "Esci"),
            Binding("?", "show_help", "Aiuto"),
            Binding("space", "refresh_now", "Aggiorna"),
            Binding("r", "refresh_now", "Aggiorna"),
            Binding("ctrl+tab", "switch_tab", "Scheda"),
            Binding("u", "start_run", "Avvia run"),
            Binding("s", "stop_run", "Ferma run"),
            Binding("a", "apply_selected", "Applica profilo"),
            Binding("R", "restore_stock", "Ripristina stock"),
            Binding("g", "apply_gpu", "Applica preset GPU"),
        ]

        def __init__(self):
            super().__init__()
            self._timer_sensors = None
            self._timer_run = None
            # Stato busy del Worker apply/restore (D8): azioni a/g/R/u/s
            # ignorate (con riga nel log) finché è vero.
            self._busy = False
            # Log con timestamp: buffer del tab Hardware (letture 1s +
            # azioni) e del tab OC (motore + azioni, §3.4/§4.7).
            self._hw_log = deque(maxlen=30)
            self._oc_log_lines = deque(maxlen=12)
            self._oc_seen = 0
            # Cache righe tabelle: niente clear+add a ogni tick → la
            # selezione dell'utente non viene azzerata (D3.2).
            self._last_profiles_rows = None
            self._last_preset_rows = None

        def compose(self) -> ComposeResult:
            yield Header()
            with TabbedContent(initial=initial_tab):
                with TabPane("Hardware", id="tab-hw"):
                    with Horizontal():
                        yield Static(dashboard_text(readings.read(),
                                                    mock=mock),
                                     id="dashboard")
                        with VerticalScroll(id="logbox") as logbox:
                            logbox.border_title = (
                                "LOG LETTURE · ultime righe "
                                "(r = aggiorna)")
                            yield Static("", id="log")
                with TabPane("OC", id="tab-oc"):
                    yield Static(OC_DISCLAIMER, id="disclaimer",
                                 markup=False)
                    yield Static(actions_strip_text(), id="actions",
                                 markup=False)
                    with VerticalScroll(id="oc-scroll"):
                        sensors = Static("", id="sensors", markup=False)
                        sensors.border_title = (
                            "SENSORI · 1s" + (" · MOCK" if mock else ""))
                        yield sensors
                        run = Static("", id="run", markup=False)
                        run.border_title = "MOTORE OC"
                        yield run
                        profiles = DataTable(id="profiles")
                        profiles.border_title = _esc(
                            "PROFILI CPU · ↑/↓ scegli · [a] applica "
                            "(conferma)")
                        yield profiles
                        gpu = Static("", id="gpu", markup=False)
                        gpu.border_title = "GPU · curva attiva"
                        yield gpu
                        presets = DataTable(id="gpu-presets")
                        presets.border_title = _esc(
                            "PRESET GPU · ↑/↓ scegli · [g] applica "
                            "(conferma)")
                        yield presets
                        oclog = Static("", id="oclog")
                        oclog.border_title = (
                            "LOG · ultime righe (motore + azioni)")
                        yield oclog
            yield Footer()

        def on_mount(self) -> None:
            table = self.query_one("#profiles", DataTable)
            table.add_columns("nome", "freq@scale", "VID", "valid.", "attivo")
            table.cursor_type = "row"   # selezione VISIBILE (D3.2)
            gpu_table = self.query_one("#gpu-presets", DataTable)
            gpu_table.add_columns("preset", "curva", "stato")
            gpu_table.cursor_type = "row"
            self._refresh_all()
            self._timer_sensors = self.set_interval(1.0, self._refresh_sensors)
            self._timer_run = self.set_interval(2.0, self._refresh_run)

        # --------------------- refresh pannelli --------------------- #

        def _hw_log_line(self, r: Dict[str, Any]) -> str:
            """Riga letture con timestamp (spec §3.4): valori mancanti →
            '—', MAI 0. I campi arrivano da LiveReadings (None → 0)."""
            parts = []
            t = r.get("cpu_temp")
            parts.append(f"CPU {t:.1f}°C" if t else "CPU —")
            g = r.get("gpu_temp")
            parts.append(f"GPU {g:.1f}°C" if g else "GPU —")
            s = r.get("total_power")
            parts.append(f"SoC {s:.0f} W" if s else "SoC —")
            return f"{time.strftime('%H:%M:%S')}  " + " · ".join(parts)

        def _render_hw_log(self) -> None:
            body = "\n".join(self._hw_log)
            if not body:
                body = "[dim](in attesa della prima lettura…)[/dim]"
            self.query_one("#log", Static).update(body)

        def _render_oc_log(self) -> None:
            body = "\n".join(self._oc_log_lines)
            if not body:
                body = ("[dim](log vuoto — nessuna run avviata "
                        "finora)[/dim]")
            self.query_one("#oclog", Static).update(body)

        def _refresh_hw(self) -> None:
            r = readings.read()
            self.query_one("#dashboard", Static).update(
                dashboard_text(r, mock=mock))
            if r:
                self._hw_log.append(_esc(self._hw_log_line(r)))
                self._render_hw_log()

        def _read_oc_sensors(self) -> Dict[str, Any]:
            """Lettura RAW per la striscia sensori OC (None conservati:
            VID/SoC protetti → '—' onesto, mai valori inventati)."""
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

        def _repopulate(self, table: DataTable, rows,
                        cache_attr: str) -> None:
            """Ricarica la tabella SOLO se le righe cambiano (la selezione
            dell'utente sopravvive al tick) e riposiziona il cursore su una
            riga valida (default riga 0, D3.2)."""
            if getattr(self, cache_attr) == rows:
                return
            setattr(self, cache_attr, rows)
            cursor = table.cursor_row
            table.clear()
            for row in rows:
                table.add_row(*row)
            if not rows:
                return
            target = cursor if cursor < len(rows) else 0
            table.move_cursor(row=target, column=0)

        def _refresh_profiles(self) -> None:
            self._repopulate(
                self.query_one("#profiles", DataTable),
                profiles_table_rows(store.load()), "_last_profiles_rows")

        def _append_engine_tail(self, tail) -> None:
            """Accoda al log OC le righe NUOVE del motore (già
            timestampate dal motore; testo escapato: markup=False)."""
            if len(tail) < self._oc_seen:      # log motore troncato
                self._oc_seen = 0
            new = tail[self._oc_seen:]
            self._oc_seen = len(tail)
            for line in new:
                self._oc_log_lines.append(_esc(line))

        def _refresh_run(self) -> None:
            """Pannello MOTORE OC + log + GPU (tick 2s).

            Durante l'apply (Worker) il corpo è lo stato
            "APPLICAZIONE IN CORSO" (§4.3); ctl.status() non viene
            chiamato mentre l'apply scrive (fail-closed)."""
            if self._busy:
                self.query_one("#run", Static).update(BUSY_TEXT)
                return
            st = ctl.status()
            self._append_engine_tail(st.get("log_tail") or [])
            text = run_text(st)
            hint = run_empty_hint(st)
            if hint:
                text = f"{text}\n\n{hint}"
            self.query_one("#run", Static).update(text)
            self._render_oc_log()
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
            self._repopulate(
                self.query_one("#gpu-presets", DataTable),
                gpu_preset_rows(DEFAULT_GPU_PRESETS,
                                active=preset.id if preset else None),
                "_last_preset_rows")

        def _refresh_all(self) -> None:
            self._refresh_sensors()
            self._refresh_profiles()
            self._refresh_run()

        def action_refresh_now(self) -> None:
            self._refresh_all()

        def action_switch_tab(self) -> None:
            """ctrl+tab: alterna scheda Hardware/OC (spec §2.2)."""
            tabs = self.query_one(TabbedContent)
            tabs.active = "tab-hw" if tabs.active == "tab-oc" else "tab-oc"

        # --------------------- log e azioni OC --------------------- #

        def _oc_log(self, msg: str, style: Optional[str] = None) -> None:
            """Scrive una riga di azione nei log (tab Hardware + tab OC):
            timestamp + prefisso '! ' (spec §3.4/§4.7). `msg` è testo PIANO
            (escapato una volta); `style` (es. "green"/"yellow"/"bold red")
            colora la riga SOLO nel log OC — il colore non è mai l'unico
            canale."""
            line = f"{time.strftime('%H:%M:%S')}  ! {_esc(msg)}"
            self._hw_log.append(line)
            self._render_hw_log()
            if style:
                self._oc_log_lines.append(f"[{style}]{line}[/]")
            else:
                self._oc_log_lines.append(line)
            self._render_oc_log()

        def _make_reader(self):
            if mock:
                from .utils.mock import MockHardware
                return MockHardware()
            try:
                from .safety.reader import RealHardwareReader
                return RealHardwareReader()
            except Exception:
                return None

        def _gate_busy(self, what: str) -> bool:
            """True se un apply è in corso: azione ignorata con riga nel
            log (mai fallimento silenzioso, D3.3)."""
            if self._busy:
                self._oc_log(f"{what}: applicazione in corso — azione "
                             "ignorata (attendi il termine)")
                return True
            return False

        def _start_worker(self, work, on_done) -> None:
            """Esegue `work` (apply/restore, bloccante con smoke 30 s) in un
            Worker textual thread: la UI resta viva (D8). on_done gira sul
            thread UI; errore → log + UI sbloccata (mai bloccata)."""
            self._busy = True
            self.query_one("#run", Static).update(BUSY_TEXT)

            def runner() -> None:
                try:
                    result = work()
                except Exception as e:      # fail-safe, mai UI bloccata
                    self.call_from_thread(self._worker_failed, e)
                    return
                self.call_from_thread(on_done, result)

            self.run_worker(runner, thread=True, group="oc-apply",
                            exclusive=True, exit_on_error=False)

        def _worker_failed(self, e: Exception) -> None:
            self._busy = False
            self._oc_log(f"applicazione fallita: {e}", "bold red")
            self._refresh_all()

        def _log_outcome(self, name: str, outcome) -> None:
            """Esito apply nel log: parole + colore (spec §4.8)."""
            result = outcome.result
            cause = outcome.cause or ""
            tag = f"apply «{name}»: "
            if result == "ok":
                self._oc_log(f"{tag}ok", "green")
            elif result == "rolled_back":
                det = f" (smoke fallito: {cause})" if cause else ""
                self._oc_log(f"{tag}ROLLED BACK — ripristinato il "
                             f"backup{det}", "bold red")
            elif result == "aborted":
                det = f" — {cause}" if cause else ""
                self._oc_log(f"{tag}rifiutato{det}", "yellow")
            else:   # stale / altro
                word = {"stale": "interrotto"}.get(result, result)
                det = f" — {cause}" if cause else ""
                self._oc_log(f"{tag}{word}{det}", "bold red")
            # Governor NON ripartito: la riga VERBATIM di apply.py (spec
            # §4.8) va mostrata nel log con il comando di ripristino.
            gov = next((d for d in getattr(outcome, "details", [])
                        if "governor NON ripartito" in d), None)
            if gov:
                self._oc_log(gov.replace("🚨 ", ""), "bold red")

        def _apply_profile(self, profile):
            from .oc.apply import ApplyManager
            from .oc.smoke import CpuSmoke
            apply_ctl = OcController(oc_dir=oc, mock=mock)
            smoke = CpuSmoke(reader=self._make_reader(), mock=mock,
                             oc_dir=oc)
            mgr = ApplyManager(apply_ctl, store=store,
                               validator=validator, smoke=smoke,
                               reader=self._make_reader(), mock=mock,
                               oc_dir=oc)
            return mgr.apply(
                profile, persist=False, yes=True,
                on_progress=lambda m: self.call_from_thread(self._oc_log, m))

        def _apply_done(self, profile, outcome) -> None:
            self._busy = False
            self._log_outcome(profile.name, outcome)
            self._refresh_all()

        def action_apply_selected(self) -> None:
            if self._gate_busy("apply profilo"):
                return
            table = self.query_one("#profiles", DataTable)
            row = table.cursor_row
            profiles = store.load()
            if not profiles or row is None or row >= len(profiles):
                self._oc_log("nessun profilo da applicare")
                return
            profile = profiles[row]
            ok, reason = validator.zone_ok(profile)
            if not ok:
                # fail-closed: modal informativa, nessuna azione applicata
                self.push_screen(ConfirmModal(
                    confirm_text(profile, (False, reason)), lambda: None))
                return
            self.push_screen(ConfirmModal(
                confirm_text(profile, (True, "")),
                lambda: self._start_worker(
                    lambda: self._apply_profile(profile),
                    lambda o: self._apply_done(profile, o))))

        def action_apply_gpu(self) -> None:
            """Applica il preset GPU selezionato (#gpu-presets, tasto g)."""
            if self._gate_busy("apply preset GPU"):
                return
            table = self.query_one("#gpu-presets", DataTable)
            row = table.cursor_row
            if row is None or row >= len(DEFAULT_GPU_PRESETS):
                self._oc_log("nessun preset da applicare")
                return
            preset = DEFAULT_GPU_PRESETS[row]
            self.push_screen(ConfirmModal(
                gpu_apply_text(preset),
                lambda: self._start_worker(
                    lambda: apply_gpu_preset(gpu_gov, preset),
                    self._gpu_done(preset))))

        def _gpu_done(self, preset):
            def on_done(res: Dict[str, Any]) -> None:
                self._busy = False
                if res.get("ok"):
                    self._oc_log(f"GPU apply «{preset.name}»: ok — curva "
                                 "riscritta e governor riavviato", "green")
                else:
                    reason = res.get("reason") or "errore sconosciuto"
                    self._oc_log(f"GPU apply «{preset.name}»: rifiutato — "
                                 f"{reason}", "bold red")
                self._refresh_all()
            return on_done

        def _restore_stock(self):
            from .oc.apply import ApplyManager
            from .oc.smoke import CpuSmoke
            apply_ctl = OcController(oc_dir=oc, mock=mock)
            mgr = ApplyManager(apply_ctl, store=store,
                               validator=validator,
                               smoke=CpuSmoke(self._make_reader(), mock=mock,
                                              oc_dir=oc),
                               reader=self._make_reader(), mock=mock,
                               oc_dir=oc)
            return mgr.restore_stock(persist=False, yes=True)

        def action_restore_stock(self) -> None:
            if self._gate_busy("ripristino stock"):
                return
            profiles = store.load()
            stock = next((p for p in profiles if p.id == "stock"), None)
            if stock is None:
                self._oc_log("nessun profilo Stock da ripristinare")
                return
            self.push_screen(ConfirmModal(
                confirm_stock_text(stock),
                lambda: self._start_worker(
                    lambda: self._restore_stock(),
                    lambda o: self._apply_done(stock, o))))

        def action_stop_run(self) -> None:
            if self._gate_busy("stop run"):
                return
            st = ctl.status()
            proc = st.get("process") if isinstance(st.get("process"),
                                                   dict) else {}
            if not proc.get("active"):
                self._oc_log("nessuna run attiva da fermare")
                return
            self.push_screen(ConfirmModal(
                confirm_stop_text(),
                lambda: self._do_stop_run()))

        def _do_stop_run(self) -> None:
            ctl.stop()
            self._oc_log("run fermata — checkpoint salvato (riprendi "
                         "con u)")
            self._refresh_all()

        def action_start_run(self) -> None:
            if self._gate_busy("start run"):
                return
            try:
                ctl.start([])
                self._oc_log("run avviata — convergenza in corso, segui "
                             "il log")
            except RuntimeError as e:
                self._oc_log(str(e), "yellow")
            self._refresh_all()

        def action_show_help(self) -> None:
            self.push_screen(HelpScreen())

        def on_unmount(self) -> None:
            for timer in (self._timer_sensors, self._timer_run):
                if timer is not None:
                    timer.stop()

    app = CockpitApp()
    app.run()
    return 0
