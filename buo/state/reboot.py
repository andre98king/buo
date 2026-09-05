#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Reboot Manager — reboot automatici con ripresa.

Crea il servizio systemd buo-resume.service (che riesegue `buo resume`
al boot) e poi esegue il reboot. Il checkpoint viene salvato
dall'orchestratore PRIMA di chiamare schedule(). Alla creazione del
servizio installa anche il watch KDE (konsole col viewer al login),
così l'utente vede la run riprendere dopo il reboot.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

from ..constants import EXIT_REBOOT
from ..utils.logging import LoggerMixin


# Template del viewer DUAL-MODE (installato come WATCH_VIEW da
# _watch_view()): cockpit textual se disponibile nel python del venv,
# altrimenti viewer ANSI a flusso (fallback, rev. 2). Stringa PIANA
# (non f-string): @PYTHON@/@LOG@/@STATE@ interpolati all'installazione;
# env BUO_WATCH_LOG/BUO_WATCH_STATE/BUO_WATCH_FORCE_ANSI solo per i test.
_WATCH_VIEW_SRC = '''#!@PYTHON@
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""Vista live della run buo (resume/unleash) dopo il reboot — DUAL-MODE.

Generato da RebootManager._watch_view() e installato come buo-watch.py;
la konsole lo lancia al login SOLO se una run è attiva (gate del wrapper).
Textual nel python della shebang (il venv di buo) → cockpit a pannelli
read-only; altrimenti → viewer ANSI a flusso (fallback, testi identici).
textual è importato SOLO dentro _watch_app(); le funzioni pure
(parse_line / style_for / status_lines / banner_lines / classify) non
hanno dipendenze. Nessun valore aggiunto: il colore avvolge il RIGO, il
testo resta identico (C1); l'esito è classificato SOLO dai marker del
segmento letto (mai da state.json).
"""
import importlib.util
import json
import os
import subprocess
import sys
import time

# Default interpolati all'installazione; override via env per i test.
LOG = "@LOG@"
STATE = "@STATE@"

SEP = "─" * 56

# ANSI SGR (mappati 1:1 sui colori rich della CLI: cli.py/tui.py). Usati
# SOLO dal fallback ANSI; ogni riga stilizzata termina con RST.
RST = "\\x1b[0m"
BOLD = "\\x1b[1m"
DIM = "\\x1b[2m"
CYAN = "\\x1b[36m"
BOLD_CYAN = "\\x1b[1;36m"
GREEN = "\\x1b[32m"
BOLD_GREEN = "\\x1b[1;32m"
RED = "\\x1b[31m"
BOLD_RED = "\\x1b[1;31m"
YELLOW = "\\x1b[33m"
BOLD_YELLOW = "\\x1b[1;33m"
BOLD_WHITE = "\\x1b[1;37m"

# Mappa token di stile (mappa semantica §4.6) → sequenza SGR.
_SGR = {
    "bold cyan": BOLD_CYAN,
    "cyan": CYAN,
    "bold green": BOLD_GREEN,
    "green": GREEN,
    "bold red": BOLD_RED,
    "red": RED,
    "yellow": YELLOW,
    "bold yellow": BOLD_YELLOW,
    "bold white": BOLD_WHITE,
    "bold": BOLD,
    "dim": DIM,
}

# Mappa fase → (numero, etichetta) — tenere allineato con
# UX_REVAMP_CLI_SPEC §2.1/2.2 (ordine = PHASES in buo/constants.py).
PHASE_LABELS = {
    "init": (1, "Inizializzazione"),
    "pre_audit": (2, "Pre-audit — analisi dello stato attuale"),
    "unlock": (3, "Sblocchi — CPU 8-core e GPU 40-CU"),
    "unlock_validate": (4, "Validazione post-unlock — CPU 8-core"),
    "fix": (5, "Fix di sistema"),
    "optimize": (6, "Ottimizzazione — undervolt e overclock"),
    "apply": (7, "Applicazione della configurazione finale"),
    "validate": (8, "Validazione — stress test e verifica fix"),
}


# --- funzioni PURE (testabili senza textual) -------------------------


def wrap_ansi(text, token):
    """Avvolge il rigo (token → SGR + RST); testo mai alterato (C1)."""
    code = _SGR.get(token)
    if not code:
        return text
    return code + text + RST


def parse_line(line):
    """Riga formattata → (level, msg); None se non ha il formato del file
    (traceback/continuazioni → verbatim, senza colore)."""
    parts = line.split(" | ", 3)
    if len(parts) == 4:
        return parts[1].strip(), parts[3]
    return None


def phase_line(state):
    """Riga 'Fase N di 8: etichetta' da state.json; '' se il file non è
    leggibile o current_phase non è tra le 8 fasi (mai testo inventato)."""
    try:
        with open(state, encoding="utf-8") as f:
            current = json.load(f).get("current_phase")
    except (OSError, ValueError):
        return ""
    num, label = PHASE_LABELS.get(current, (None, None))
    if num is None:
        return ""
    return "Fase %d di 8: %s" % (num, label)


def status_lines(state, resumed):
    """Righe di stato (§5.1): [riga modo, riga fase?] — la fase è OMESSA
    se il file non è leggibile o current_phase è fuori tabella (C1)."""
    lines = ["La run è RIPRESA dopo il reboot ed è in corso." if resumed
             else "Ottimizzazione in corso."]
    pl = phase_line(state)
    if pl:
        lines.append(pl)
    return lines


def style_for(level, msg, summary=False):
    """NOME di stile del rigo di log (mappa §4.6, con stato summary);
    primo match vince; default = nessuno stile (mai inventare un colore).
    Ritorna (token, summary_aggiornato)."""
    if "SAFETY VIOLATION" in msg:
        return "bold red", summary
    if msg.startswith("OTTIMIZZAZIONE COMPLETATA"):
        return "bold green", summary
    if msg.startswith("Fase: "):
        return "bold cyan", summary
    if msg.startswith("Riepilogo finale"):
        return "bold white", True
    if summary:  # righe del riepilogo finale (dim; rollback in giallo)
        if "rollback:" in msg:
            return "bold yellow", True
        return "dim", True
    if level in ("ERROR", "CRITICAL"):
        return "red", summary
    if level == "WARNING":
        return "yellow", summary
    return "", summary


def classify(segment):
    """Esito dal SOLO segmento di log letto (primo match vince)."""
    text = "".join(segment)
    if "OTTIMIZZAZIONE COMPLETATA" in text:
        return "completed"
    if "SAFETY VIOLATION" in text:
        return "safety"
    if "Errore in fase" in text or "Errore fatale" in text:
        return "error"
    return "unclear"


def banner_lines(outcome):
    """Righe del blocco d'esito (§5.3, VERBATIM, senza separatore)."""
    if outcome == "completed":
        return ["Run COMPLETATA — esito positivo.",
                "Riepilogo finale qui sopra. Puoi chiudere questa finestra."]
    if outcome == "error":
        return ["Run INTERROTTA — l'ottimizzazione non è stata completata.",
                "Le modifiche applicate in questa run sono state annullate",
                "(rollback automatico): la macchina resta nella configurazione",
                "precedente.",
                "",
                "Cosa fare:",
                " 1. controlla le ultime righe qui sopra (o /var/log/buo/buo.log)",
                " 2. diagnostica: sudo buo doctor",
                " 3. riprova: sudo buo unleash"]
    if outcome == "safety":
        return ["SAFETY VIOLATION — run interrotta per sicurezza.",
                "Dettagli e motivo nelle righe qui sopra.",
                "",
                "Le modifiche applicate in questa run sono state annullate",
                "(rollback automatico). La macchina riparte normalmente.",
                "",
                "Cosa fare:",
                " 1. se il motivo è termico: aspetta che la macchina si raffreddi",
                " 2. diagnostica: sudo buo doctor",
                " 3. riprova: sudo buo unleash"]
    return ["La run si è fermata senza un esito riconoscibile nel log",
            "(possibile riavvio in corso o interruzione improvvisa).",
            "Se la macchina NON si sta riavviando: controlla il log",
            "/var/log/buo/buo.log e riprova con: sudo buo unleash"]


# --- fallback ANSI (rev. 2, testi e SGR invariati) -------------------


def banner_ansi(outcome):
    """Blocco esito del fallback: separatore dim + righe stilizzate."""
    lines = [wrap_ansi(SEP, "dim")]
    for i, line in enumerate(banner_lines(outcome)):
        if not line:
            lines.append("")
        elif outcome == "unclear":
            lines.append(wrap_ansi(line, "dim"))
        elif i == 0:
            token = "bold green" if outcome == "completed" else "bold red"
            lines.append(wrap_ansi(line, token))
        elif outcome == "completed":
            lines.append(wrap_ansi(line, "dim"))  # hint
        elif line == "Cosa fare:":
            lines.append(wrap_ansi(line, "bold"))
        else:
            lines.append(line)  # corpo e passi normali
    return "\\n".join(lines) + "\\n"


def _emit_line(out, line, summary):
    """Stampa una riga del log (filtro + stile ANSI); ritorna lo stato
    summary. Riga non formattata → verbatim, senza colore."""
    parsed = parse_line(line)
    if parsed is None:
        out.write(line + "\\n")
        return summary
    level, msg = parsed
    token, summary = style_for(level, msg, summary)
    out.write(wrap_ansi(msg, token) + "\\n")
    return summary


def pgrep_active(pattern):
    """True se un processo buo attivo matcha il pattern (pgrep)."""
    try:
        rc = subprocess.run(["pgrep", "-f", pattern],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL).returncode
    except OSError:
        return True  # pgrep assente: resta in attesa, nessun finto esito
    return rc == 0


def run_offset(path):
    """Offset (byte) dell'ultima riga 'Avvio ottimizzazione' (inizio del
    processo corrente); 0 se il marker è assente o il file è illeggibile."""
    marker = b"Avvio ottimizzazione"
    try:
        with open(path, "rb") as f:
            offset = 0
            for raw in f:
                if marker in raw:
                    offset = f.tell() - len(raw)
            return offset
    except OSError:
        return 0


def run_ansi():
    """Fallback ANSI (rev. 2): header, flusso live, blocco esito, exit 0."""
    log = os.environ.get("BUO_WATCH_LOG") or LOG
    state = os.environ.get("BUO_WATCH_STATE") or STATE
    out = sys.stdout
    resumed = pgrep_active("[b]uo resume")
    out.write(wrap_ansi("BUO — BC-250 Ultimate Orchestrator", "bold cyan")
              + "\\n\\n")
    lines = status_lines(state, resumed)
    out.write(wrap_ansi(lines[0], "bold green") + "\\n")
    if len(lines) > 1:
        out.write(wrap_ansi(lines[1], "cyan") + "\\n")
    out.write(wrap_ansi("Log live della run:", "dim") + "\\n")
    out.write(wrap_ansi(SEP, "dim") + "\\n")
    out.flush()

    segment = []
    summary = False
    try:
        f = open(log, "rb")
    except OSError:
        f = None
    else:
        f.seek(run_offset(log))
    try:
        while True:
            raw = f.readline() if f else b""
            if raw:
                line = raw.decode("utf-8", "replace").rstrip("\\n")
                segment.append(line)
                summary = _emit_line(out, line, summary)
                out.flush()
                continue
            if not pgrep_active("[b]uo (resume|unleash)"):
                time.sleep(0.5)  # settle: righe residue del processo morto
                while f:
                    raw = f.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", "replace").rstrip("\\n")
                    segment.append(line)
                    summary = _emit_line(out, line, summary)
                break
            time.sleep(1)
    finally:
        if f:
            f.close()
    out.write(banner_ansi(classify(segment)))
    out.flush()
    return 0


# --- modalità cockpit (textual, importato SOLO qui) -------------------


def _watch_app(log, state):
    """Classe App cockpit (read-only); None se textual non c'è.

    Pannelli: LOG in alto (1fr) e STATO/ESITO in basso (auto) — così i
    riferimenti spaziali dei blocchi esito ("qui sopra") restano veri.
    """
    import importlib.util
    if importlib.util.find_spec("textual") is None:
        return None
    from collections import deque

    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import VerticalScroll
    from textual.widgets import Footer, Header, Static

    def _esc(text):
        """Escapa '[' per i widget con markup: testo LETTERALE a schermo."""
        return text.replace("[", "\\\\[")

    resumed = pgrep_active("[b]uo resume")

    class WatchApp(App):
        """Cockpit read-only della run: log live sopra, stato/esito sotto."""

        TITLE = "BUO — BC-250 Ultimate Orchestrator"
        CSS = """
        #logbox { border: round $secondary; height: 1fr; padding: 0 1; }
        #log    { width: 1fr; }
        #stato  { border: round $accent; height: auto; padding: 0 1;
                  margin: 1 0 0 0; }
        """
        BINDINGS = [
            Binding("q", "quit", "Chiudi"),
            Binding("escape", "quit", "Chiudi"),
        ]

        def __init__(self):
            super().__init__()
            self.sub_title = (
                "watch run · ripresa dopo il reboot" if resumed
                else "watch run · ottimizzazione in corso")
            self._log_path = log
            self._state_path = state
            self._fh = None
            self._offset = run_offset(log)
            # ponytail: buffer log e segmento limitati a 500 righe; i marker
            # di esito sono terminali (fine run) → sempre nel buffer.
            self._lines = deque(maxlen=500)   # (token|None, testo) mostrati
            self._segment = deque(maxlen=500)  # righe grezze per classify
            self._summary = False
            self._done = False
            self._outcome = None
            self._resumed = resumed
            self._stato_plain = ""            # testo del pannello STATO
            self._log_plain = ""              # testo del pannello LOG

        def compose(self) -> ComposeResult:
            yield Header()
            with VerticalScroll(id="logbox") as logbox:
                logbox.border_title = (
                    "LOG LIVE · righe della run (↑/↓ scorri)")
                yield Static("", id="log")
            stato = Static("", id="stato")
            stato.border_title = "STATO RUN"
            yield stato
            yield Footer()

        def on_mount(self) -> None:
            try:
                self._fh = open(self._log_path, "rb")
                self._fh.seek(self._offset)
            except OSError:
                self._fh = None
            logbox = self.query_one("#logbox")
            if logbox.can_focus:
                logbox.focus()
            self.set_interval(1.0, self._tick)
            self._tick()

        def on_unmount(self) -> None:
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None

        def _read_log(self) -> None:
            """Legge le righe nuove (fino a EOF) e aggiorna il pannello."""
            if self._fh is None:
                return
            changed = False
            while True:
                raw = self._fh.readline()
                if not raw:
                    break
                changed = True
                line = raw.decode("utf-8", "replace").rstrip("\\n")
                self._segment.append(line)
                parsed = parse_line(line)
                if parsed is None:
                    self._lines.append((None, line))
                else:
                    level, msg = parsed
                    token, self._summary = style_for(
                        level, msg, self._summary)
                    self._lines.append((token, msg))
            if changed:
                self._render_log()

        def _render_log(self) -> None:
            body = "\\n".join(
                "[%s]%s[/]" % (tok, _esc(txt)) if tok else _esc(txt)
                for tok, txt in self._lines)
            self._log_plain = "\\n".join(txt for _, txt in self._lines)
            self.query_one("#log", Static).update(body)
            self.query_one("#logbox", VerticalScroll).scroll_end(
                animate=False)

        def _set_stato(self, plain, markup) -> None:
            self._stato_plain = plain
            self.query_one("#stato", Static).update(markup)

        def _stato_viva(self) -> None:
            lines = status_lines(self._state_path, self._resumed)
            markup = "[bold green]%s[/]" % _esc(lines[0])
            if len(lines) > 1:
                markup += "\\n[cyan]%s[/]" % _esc(lines[1])
            self._set_stato("\\n".join(lines), markup)

        def _stato_esito(self, outcome) -> None:
            block = banner_lines(outcome)
            markup = []
            for i, line in enumerate(block):
                if not line:
                    markup.append("")
                elif outcome == "unclear":
                    markup.append("[dim]%s[/]" % _esc(line))
                elif i == 0:
                    head = ("[bold green]" if outcome == "completed"
                            else "[bold red]")
                    markup.append(head + _esc(line) + "[/]")
                elif outcome == "completed":
                    markup.append("[dim]%s[/]" % _esc(line))  # hint
                elif line == "Cosa fare:":
                    markup.append("[bold]%s[/]" % _esc(line))
                else:
                    markup.append(_esc(line))
            self._set_stato("\\n".join(block), "\\n".join(markup))

        def _tick(self) -> None:
            if self._done:
                return
            if pgrep_active("[b]uo (resume|unleash)"):
                self._read_log()
                self._stato_viva()
            else:
                time.sleep(0.5)  # settle: righe residue del processo morto
                self._read_log()
                self._outcome = classify(list(self._segment))
                self._done = True
                self._stato_esito(self._outcome)

    return WatchApp


def run_cockpit():
    """Modalità cockpit; senza textual ricade sul fallback ANSI."""
    cls = _watch_app(LOG, STATE)
    if cls is None:
        return run_ansi()
    cls().run()
    return 0


def main():
    """Scelta modalità: ANSI forzata (test) o textual assente → fallback."""
    if os.environ.get("BUO_WATCH_FORCE_ANSI"):
        return run_ansi()
    if importlib.util.find_spec("textual") is None:
        return run_ansi()
    return run_cockpit()


if __name__ == "__main__":
    sys.exit(main())
'''


class RebootManager(LoggerMixin):
    """Gestisce i reboot automatici con ripresa."""

    SERVICE_NAME = "buo-resume.service"
    SERVICE_PATH = Path("/etc/systemd/system") / SERVICE_NAME
    # UX watch-log: al login successivo la konsole apre il VIEWER del log
    # (cockpit textual o fallback ANSI — solo reboot con ripresa).
    # Percorsi/utente come attributi di classe per i test.
    WATCH_SCRIPT = Path("/usr/local/bin/buo-watch-log.sh")
    WATCH_VIEW = Path("/usr/local/bin/buo-watch.py")
    WATCH_DESKTOP = "buo-watch.desktop"
    WATCH_LOG = Path("/var/log/buo/buo.log")
    WATCH_STATE = Path("/var/lib/buo/state.json")
    DESKTOP_UID = 1000
    PLASMA_SESSION_FILES = (
        Path("/usr/share/wayland-sessions/plasma.desktop"),
        Path("/usr/share/xsessions/plasma.desktop"),
    )

    def __init__(self, resume_command: str = "buo resume"):
        # Fail-closed: niente newline/control char (iniezione unit systemd).
        if any(c in resume_command for c in "\r\n\x00"):
            raise ValueError("resume_command non valido (newline non ammesso)")
        self.resume_command = resume_command

    def schedule(self, reason: str = "reboot required",
                 delay: int = 5) -> None:
        """Crea il servizio di ripresa e riavvia (exit code 50)."""
        self.logger.info("Reboot programmato: %s", reason)
        self._create_resume_service()

        self.logger.info("Riavvio in %d secondi… (Ctrl+C per annullare)",
                         delay)
        self.logger.info("La run riprende da sola al riavvio.")
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            self.logger.info("Reboot annullato dall'utente")
            self.cleanup()
            return

        # reboot effettivo se possibile, altrimenti exit code dedicato
        rc, _, _ = self._run(["systemctl", "reboot"], check=False)
        if rc != 0:
            self.logger.warning("systemctl reboot non disponibile — "
                                "chiusura con exit code %d", EXIT_REBOOT)
        sys.exit(EXIT_REBOOT)

    def _create_resume_service(self) -> bool:
        """Scrive e abilita buo-resume.service."""
        content = f"""# BUO Resume Service — generato automaticamente
[Unit]
Description=BUO Resume Service (BC-250 Ultimate Orchestrator)
After=multi-user.target

[Service]
Type=oneshot
# La cwd di un servizio systemd è "/" (read-only su ostree):
# gli script che scrivono file relativi (bc250-detect → overclock.conf)
# devono girare da una directory scrivibile.
WorkingDirectory=/tmp
ExecStart=/usr/local/bin/{self.resume_command}
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
"""
        try:
            self.SERVICE_PATH.parent.mkdir(parents=True, exist_ok=True)
            self.SERVICE_PATH.write_text(content, encoding="utf-8")
            self._run(["systemctl", "enable", self.SERVICE_NAME])
        except Exception as e:
            self.logger.error("Creazione servizio di ripresa fallita: %s", e)
            return False
        self.logger.info("Servizio di ripresa creato: %s", self.SERVICE_NAME)
        # UX: al login la konsole si riapre da sola sul log della run
        # (no-op senza utente desktop KDE; mai bloccante per il reboot).
        self._install_watch_log()
        return True

    def _install_watch_log(self) -> None:
        """Installa script + viewer + autostart KDE per il watch del log.

        Idempotente (sovrascrive gli stessi file). Best effort: un errore
        qui non deve mai bloccare la schedulazione del reboot.
        """
        try:
            home = self._desktop_user_home()
            if not home:
                self.logger.info("Watch-log: nessun utente desktop "
                                 "(uid %d) — skip", self.DESKTOP_UID)
                return
            if not any(p.exists() for p in self.PLASMA_SESSION_FILES):
                self.logger.info("Watch-log: nessuna sessione KDE — skip")
                return
            self.WATCH_SCRIPT.parent.mkdir(parents=True, exist_ok=True)
            self.WATCH_SCRIPT.write_text(self._watch_script(),
                                         encoding="utf-8")
            os.chmod(self.WATCH_SCRIPT, 0o755)
            self.WATCH_VIEW.write_text(self._watch_view(), encoding="utf-8")
            os.chmod(self.WATCH_VIEW, 0o755)
            autostart = Path(home) / ".config" / "autostart"
            autostart.mkdir(parents=True, exist_ok=True)
            (autostart / self.WATCH_DESKTOP).write_text(
                self._watch_desktop(), encoding="utf-8")
            self._make_log_readable()
            self.logger.info("Watch-log installato: la run si vedrà nel "
                             "log al prossimo login")
        except Exception as e:
            self.logger.warning("Watch-log: installazione fallita (%s) — "
                                "continuo", e)

    def _desktop_user_home(self):
        """Home dell'utente desktop (uid DESKTOP_UID) o None (headless)."""
        rc, out, _ = self._run(["getent", "passwd", str(self.DESKTOP_UID)])
        if rc != 0:
            return None
        fields = out.strip().split(":")
        if len(fields) < 7 or fields[2] != str(self.DESKTOP_UID):
            return None
        return fields[5] or None

    def _has_textual(self) -> bool:
        """Probe: il python che genera (il venv di buo) ha textual?

        Deciso alla GENERAZIONE (i file sono rigenerati a ogni reboot
        schedulato e usati entro lo stesso boot): cockpit → wrapper senza
        --hold (q chiude la finestra); fallback ANSI → --hold (schermata
        finale statica, rev. 2). Mai hardcodare il path del venv.
        """
        try:
            import importlib.util
            return importlib.util.find_spec("textual") is not None
        except Exception:
            return False

    def _watch_script(self) -> str:
        """Script autostart: konsole col viewer se una run è attiva.

        --hold SOLO nel fallback ANSI (la schermata finale resta per la
        lettura); con la cockpit niente --hold (q chiude la finestra).
        """
        hold = "" if self._has_textual() else "--hold "
        return f"""#!/bin/sh
# BUO watch — se una run buo (resume/unleash) è attiva al login, riapre
# la konsole col viewer del log; altrimenti esce in silenzio.
pgrep -f "[b]uo (resume|unleash)" >/dev/null 2>&1 || exit 0
exec konsole {hold}--title "BUO — ottimizzazione in corso" -e {self.WATCH_VIEW}
"""

    def _watch_view(self) -> str:
        """Template del viewer (dual-mode), path e shebang interpolati.

        Shebang = sys.executable del processo che genera (il python del
        venv di buo, quello che ha textual); mai hardcodato.
        """
        return (_WATCH_VIEW_SRC
                .replace("@PYTHON@", sys.executable)
                .replace("@LOG@", str(self.WATCH_LOG))
                .replace("@STATE@", str(self.WATCH_STATE)))

    def _watch_desktop(self) -> str:
        """Entry autostart KDE che lancia lo script a ogni login."""
        return f"""[Desktop Entry]
Type=Application
Name=BUO Watch Log
Exec={self.WATCH_SCRIPT}
"""

    def _make_log_readable(self) -> None:
        """Watch da utente: log e stato leggibili (best effort)."""
        for d in (self.WATCH_LOG.parent, self.WATCH_STATE.parent):
            try:
                os.chmod(d, 0o755)
            except OSError:
                pass
        for p in (self.WATCH_LOG, self.WATCH_STATE):
            try:
                if p.exists():
                    os.chmod(p, 0o644)
            except OSError:
                pass

    def cleanup(self) -> None:
        """Rimuove il servizio di ripresa."""
        if self.SERVICE_PATH.exists():
            self._run(["systemctl", "disable", self.SERVICE_NAME])
            try:
                self.SERVICE_PATH.unlink()
            except Exception:
                pass
            self.logger.info("Servizio di ripresa rimosso")

    @staticmethod
    def _run(cmd, check: bool = False):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return r.returncode, r.stdout, r.stderr
        except Exception as e:
            return 127, "", str(e)
