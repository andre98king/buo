#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Reboot Manager — reboot automatici con ripresa.

Crea il servizio systemd buo-resume.service (che riesegue `buo resume`
al boot) e poi esegue il reboot. Il checkpoint viene salvato
dall'orchestratore PRIMA di chiamare schedule(). Alla creazione del
servizio installa anche il watch-log KDE (konsole sul log live al login),
così l'utente vede la run riprendere dopo il reboot.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

from ..constants import EXIT_REBOOT
from ..utils.logging import LoggerMixin


# Template del viewer (installato come WATCH_VIEW da _watch_view()).
# Stringa PIANA (non f-string): i path @LOG@/@STATE@ vengono interpolati
# all'installazione; l'override env BUO_WATCH_LOG/BUO_WATCH_STATE serve
# SOLO ai test.
_WATCH_VIEW_SRC = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""Vista live della run buo (resume/unleash) dopo il reboot.

Generato da RebootManager._watch_view() e installato come buo-watch.py;
la konsole lo lancia al login SOLO se una run è attiva (gate del wrapper).
Solo stdlib. Nessun valore aggiunto: mostra le righe del log (prefisso
rimosso) dall'ultimo 'Avvio ottimizzazione' e, a run finita, classifica
l'esito sui SOLI marker presenti nel segmento letto. Veste ANSI SGR a
mano (§3.10): il colore avvolge il RIGO stampato, il testo resta identico.
"""
import json
import os
import subprocess
import sys
import time

# Default interpolati all'installazione; override via env per i test.
LOG = "@LOG@"
STATE = "@STATE@"

SEP = "─" * 56

# ANSI SGR (mappati 1:1 sui colori rich della CLI: cli.py/tui.py).
# Costanti modulo: MAI interpolate nei testi, avvolgono SOLO il rigo
# stampato; ogni riga stilizzata termina con RST.
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

# Mappa fase → (numero, etichetta) — tenere allineato con
# UX_REVAMP_CLI_SPEC §2.1/2.2 (ordine = PHASES in buo/constants.py).
PHASE_LABELS = {
    "init": (1, "Inizializzazione"),
    "pre_audit": (2, "Pre-audit — analisi dello stato attuale"),
    "unlock": (3, "Sblocchi — CPU 8-core e GPU 40-CU"),
    "fix": (4, "Fix di sistema"),
    "optimize": (5, "Ottimizzazione — undervolt e overclock"),
    "apply": (6, "Applicazione della configurazione finale"),
    "validate": (7, "Validazione — stress test e verifica fix"),
}


def colorize(text, code):
    """Avvolge il rigo con code+RST; il testo non viene mai alterato."""
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


def filter_line(line):
    """Rimuove il prefisso 'asctime | LEVEL | name | ' se presente."""
    parsed = parse_line(line)
    if parsed is None:
        return line
    return parsed[1]


def phase_line(state):
    """Riga 'Fase N di 7: etichetta' da state.json; '' se il file non è
    leggibile o current_phase non è tra le 7 fasi (mai testo inventato)."""
    try:
        with open(state, encoding="utf-8") as f:
            current = json.load(f).get("current_phase")
    except (OSError, ValueError):
        return ""
    num, label = PHASE_LABELS.get(current, (None, None))
    if num is None:
        return ""
    return "Fase %d di 7: %s" % (num, label)


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


def line_style(level, msg, summary=False):
    """Stile del rigo di log (codice ANSI, stato summary); primo match
    vince; default = nessun colore (mai inventare uno stile)."""
    if "SAFETY VIOLATION" in msg:
        return BOLD_RED, summary
    if msg.startswith("OTTIMIZZAZIONE COMPLETATA"):
        return BOLD_GREEN, summary
    if msg.startswith("Fase: "):
        return BOLD_CYAN, summary
    if msg.startswith("Riepilogo finale"):
        return BOLD_WHITE, True
    if summary:  # righe del riepilogo finale (dim; rollback in giallo)
        if "rollback:" in msg:
            return BOLD_YELLOW, True
        return DIM, True
    if level in ("ERROR", "CRITICAL"):
        return RED, summary
    if level == "WARNING":
        return YELLOW, summary
    return "", summary


def banner_for(outcome):
    """Blocco terminale per l'esito (testi esatti, righe < 80)."""
    if outcome == "completed":
        return (SEP + "\\n"
                "Run COMPLETATA — esito positivo.\\n"
                "Riepilogo finale qui sopra. Puoi chiudere questa finestra.\\n")
    if outcome == "safety":
        return (SEP + "\\n"
                "SAFETY VIOLATION — run interrotta per sicurezza.\\n"
                "Dettagli e motivo nelle righe qui sopra.\\n"
                "\\n"
                "Le modifiche applicate in questa run sono state annullate\\n"
                "(rollback automatico). La macchina riparte normalmente.\\n"
                "\\n"
                "Cosa fare:\\n"
                " 1. se il motivo è termico: aspetta che la macchina si raffreddi\\n"
                " 2. diagnostica: sudo buo doctor\\n"
                " 3. riprova: sudo buo unleash\\n")
    if outcome == "error":
        return (SEP + "\\n"
                "Run INTERROTTA — l'ottimizzazione non è stata completata.\\n"
                "Le modifiche applicate in questa run sono state annullate\\n"
                "(rollback automatico): la macchina resta nella configurazione\\n"
                "precedente.\\n"
                "\\n"
                "Cosa fare:\\n"
                " 1. controlla le ultime righe qui sopra (o /var/log/buo/buo.log)\\n"
                " 2. diagnostica: sudo buo doctor\\n"
                " 3. riprova: sudo buo unleash\\n")
    return (SEP + "\\n"
            "La run si è fermata senza un esito riconoscibile nel log\\n"
            "(possibile riavvio in corso o interruzione improvvisa).\\n"
            "Se la macchina NON si sta riavviando: controlla il log\\n"
            "/var/log/buo/buo.log e riprova con: sudo buo unleash\\n")


def render_banner(outcome):
    """Blocco terminale con la veste ANSI dell'esito — testo invariato:
    _strip_ansi(render_banner(o)) == banner_for(o)."""
    lines = banner_for(outcome).rstrip("\\n").split("\\n")
    styled = []
    for i, line in enumerate(lines):
        if not line:
            styled.append("")
        elif outcome == "unclear":
            styled.append(colorize(line, DIM))
        elif i == 0:  # separatore (header e blocchi)
            styled.append(colorize(line, DIM))
        elif outcome == "completed":
            code = BOLD_GREEN if i == 1 else DIM
            styled.append(colorize(line, code))
        elif i == 1:  # headline error/safety
            styled.append(colorize(line, BOLD_RED))
        elif line == "Cosa fare:":
            styled.append(colorize(line, BOLD))
        else:
            styled.append(line)  # corpo e passi normali
    return "\\n".join(styled) + "\\n"


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


def write_line(out, line, summary):
    """Stampa una riga del log (filtrata e stilizzata); ritorna lo stato
    summary aggiornato. Riga non formattata → verbatim, senza colore."""
    parsed = parse_line(line)
    if parsed is None:
        out.write(line + "\\n")
        return summary
    level, msg = parsed
    code, summary = line_style(level, msg, summary)
    out.write(colorize(msg, code) + "\\n")
    return summary


def main():
    log = os.environ.get("BUO_WATCH_LOG") or LOG
    state = os.environ.get("BUO_WATCH_STATE") or STATE
    out = sys.stdout
    out.write(colorize("BUO — BC-250 Ultimate Orchestrator", BOLD_CYAN)
              + "\\n\\n")
    if pgrep_active("[b]uo resume"):
        out.write(colorize("La run è RIPRESA dopo il reboot ed è in corso.",
                           BOLD_GREEN) + "\\n")
    else:
        out.write(colorize("Ottimizzazione in corso.", BOLD_GREEN) + "\\n")
    pl = phase_line(state)
    if pl:
        out.write(colorize(pl, CYAN) + "\\n")
    out.write(colorize("Log live della run:", DIM) + "\\n")
    out.write(colorize(SEP, DIM) + "\\n")
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
                summary = write_line(out, line, summary)
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
                    summary = write_line(out, line, summary)
                break
            time.sleep(1)
    finally:
        if f:
            f.close()
    out.write(render_banner(classify(segment)))
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


class RebootManager(LoggerMixin):
    """Gestisce i reboot automatici con ripresa."""

    SERVICE_NAME = "buo-resume.service"
    SERVICE_PATH = Path("/etc/systemd/system") / SERVICE_NAME
    # UX watch-log: al login successivo la konsole apre il VIEWER del log
    # live (solo reboot con ripresa). Percorsi/utente come attributi di
    # classe per i test.
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

    def _watch_script(self) -> str:
        """Script autostart: konsole col viewer se una run è attiva."""
        return f"""#!/bin/sh
# BUO watch — se una run buo (resume/unleash) è attiva al login, riapre
# la konsole col viewer del log; altrimenti esce in silenzio.
pgrep -f "[b]uo (resume|unleash)" >/dev/null 2>&1 || exit 0
exec konsole --hold --title "BUO — ottimizzazione in corso" -e {self.WATCH_VIEW}
"""

    def _watch_view(self) -> str:
        """Template del viewer python, con i path di sistema interpolati."""
        return (_WATCH_VIEW_SRC
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
