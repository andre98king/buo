#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Watch-log KDE (UX reboot-resume): quando RebootManager crea
buo-resume.service (reboot con ripresa), installa anche
/usr/local/bin/buo-watch-log.sh + autostart ~<desktop>/.config/autostart
per l'utente desktop: al login la konsole si riapre da sola sul log live
della run. No-op su macchine senza utente desktop/KDE (headless).

Mai file reali: percorsi e utente tutti finti su tmp_path.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from buo.state.reboot import RebootManager

# Prefisso standard di una riga del log buo (asctime | LEVEL | name | msg).
_PREFIX = "2026-09-04 13:25:02,391 | INFO | buo.Orchestrator | "


def _getent_line(home: str) -> str:
    """Riga getent passwd finta per l'utente desktop (uid 1000)."""
    return f"buouser:x:1000:1000:BUO User:{home}:/bin/bash"


def _fake_run(getent_rc=0, getent_out=""):
    """Runner finto: getent pilotabile, systemctl sempre ok."""
    def run(cmd, check=False):
        if cmd and cmd[0] == "getent":
            return getent_rc, getent_out, ""
        return 0, "", ""
    return run


class WatchLogInstallTest(unittest.TestCase):
    """Installazione del watch quando viene creato il resume service."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        b = Path(self._tmp.name)
        self.svc = b / "etc-systemd" / "buo-resume.service"
        self.script = b / "usr-local-bin" / "buo-watch-log.sh"
        self.home = b / "home" / "buouser"
        self.autostart = self.home / ".config" / "autostart"
        self.desktop = self.autostart / "buo-watch.desktop"
        self.log = b / "var-log-buo" / "buo.log"
        self.view = b / "usr-local-bin" / "buo-watch.py"
        self.state_dir = b / "var-lib-buo"
        self.state = self.state_dir / "state.json"
        self.plasma = b / "usr-share" / "wayland-sessions" / "plasma.desktop"
        self.log.parent.mkdir(parents=True)
        self.log.write_text("riga di log\n", encoding="utf-8")
        os.chmod(self.log, 0o600)          # root-only, come in produzione
        os.chmod(self.log.parent, 0o700)
        # stato finto root-only: l'install deve renderlo leggibile (644/755)
        self.state_dir.mkdir(parents=True)
        self.state.write_text('{"current_phase": "init"}\n', encoding="utf-8")
        os.chmod(self.state, 0o600)
        os.chmod(self.state_dir, 0o700)

    def tearDown(self):
        self._tmp.cleanup()

    def _patch(self, getent_rc=0, getent_out=None, plasma=True):
        """Punta tutti i percorsi di RebootManager su tmp_path."""
        if getent_out is None:
            getent_out = _getent_line(str(self.home))
        patched = {
            "SERVICE_PATH": self.svc,
            "WATCH_SCRIPT": self.script,
            "WATCH_VIEW": self.view,
            "WATCH_LOG": self.log,
            "WATCH_STATE": self.state,
            "PLASMA_SESSION_FILES": (self.plasma,),
            "_run": staticmethod(_fake_run(getent_rc, getent_out)),
        }
        for attr, value in patched.items():
            p = mock.patch.object(RebootManager, attr, value)
            p.start()
            self.addCleanup(p.stop)
        if plasma:
            self.plasma.parent.mkdir(parents=True, exist_ok=True)
            self.plasma.write_text("[Desktop Entry]\nType=XSession\n",
                                   encoding="utf-8")

    def _create(self):
        return RebootManager()._create_resume_service()

    # --- installazione -------------------------------------------------

    def test_install_watch_when_resume_service_created(self):
        """buo-resume.service creato → script + viewer + .desktop + perms."""
        self._patch()
        self.assertTrue(self._create())
        self.assertTrue(self.svc.exists())            # servizio come prima
        self.assertTrue(self.script.exists())         # watch script 755
        self.assertEqual(os.stat(self.script).st_mode & 0o777, 0o755)
        body = self.script.read_text(encoding="utf-8")
        self.assertIn('pgrep -f "[b]uo (resume|unleash)"', body)
        self.assertIn('exec konsole --hold --title "BUO — ottimizzazione '
                      f'in corso" -e {self.view}', body)
        self.assertTrue(self.view.exists())           # viewer 755
        self.assertEqual(os.stat(self.view).st_mode & 0o777, 0o755)
        vbody = self.view.read_text(encoding="utf-8")
        self.assertIn("def filter_line", vbody)
        self.assertIn('if __name__ == "__main__":', vbody)
        self.assertTrue(self.desktop.exists())        # autostart
        self.assertIn(f"Exec={self.script}",
                      self.desktop.read_text(encoding="utf-8"))
        self.assertEqual(os.stat(self.log).st_mode & 0o777, 0o644)
        self.assertEqual(os.stat(self.log.parent).st_mode & 0o777, 0o755)
        # state.json leggibile dall'utente desktop (dir 755, file 644)
        self.assertEqual(os.stat(self.state).st_mode & 0o777, 0o644)
        self.assertEqual(os.stat(self.state_dir).st_mode & 0o777, 0o755)

    def test_install_idempotent(self):
        """Seconda schedulazione → stessi file, nessun duplicato."""
        self._patch()
        self._create()
        script_first = self.script.read_text(encoding="utf-8")
        desktop_first = self.desktop.read_text(encoding="utf-8")
        self._create()
        self.assertEqual(self.script.read_text(encoding="utf-8"), script_first)
        self.assertEqual(self.desktop.read_text(encoding="utf-8"), desktop_first)
        self.assertEqual(len(list(self.script.parent.glob("buo-watch-log.sh"))), 1)
        self.assertEqual(len(list(self.autostart.glob("buo-watch.desktop"))), 1)

    def test_skip_when_no_desktop_user(self):
        """Nessun utente uid 1000 (getent fallisce) → nessun file."""
        self._patch(getent_rc=2, getent_out="")
        self.assertTrue(self._create())               # servizio comunque ok
        self.assertFalse(self.script.exists())
        self.assertFalse(self.desktop.exists())
        self.assertEqual(os.stat(self.log).st_mode & 0o777, 0o600,
                         "perms del log non toccate senza installazione")

    def test_skip_when_no_plasma(self):
        """uid 1000 esiste ma KDE non c'è → nessun file installato."""
        self._patch(plasma=False)
        self.assertTrue(self._create())
        self.assertFalse(self.script.exists())
        self.assertFalse(self.desktop.exists())

    # --- script watch (decisione pgrep → konsole) ----------------------

    def _watch_outcome(self, pgrep_rc, record):
        """Genera lo script (via install) e lo esegue con pgrep/konsole
        finti in PATH: verifica la decisione reale dello script."""
        self._patch()
        self._create()
        bindir = Path(self._tmp.name) / "bin"
        bindir.mkdir()
        (bindir / "pgrep").write_text(
            '#!/bin/sh\necho "pgrep:$*" >> "$RECORD"\n'
            'exit "${FAKE_PGREP_RC:-0}"\n')
        (bindir / "konsole").write_text(
            '#!/bin/sh\necho "konsole:$*" >> "$RECORD"\n')
        for name in ("pgrep", "konsole"):
            os.chmod(bindir / name, 0o755)
        env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}",
                   RECORD=str(record), FAKE_PGREP_RC=str(pgrep_rc))
        r = subprocess.run([str(self.script)], env=env,
                           capture_output=True, text=True, timeout=30)
        return r

    def test_watch_script_opens_konsole_when_run_active(self):
        """Run attiva (pgrep exit 0) → konsole col viewer del log."""
        record = Path(self._tmp.name) / "record.txt"
        r = self._watch_outcome(pgrep_rc=0, record=record)
        self.assertEqual(r.returncode, 0)
        lines = record.read_text().splitlines()
        self.assertEqual(lines[0], "pgrep:-f [b]uo (resume|unleash)")
        self.assertEqual(lines[1], "konsole:--hold --title BUO — "
                                   f"ottimizzazione in corso -e {self.view}")

    def test_watch_script_silent_when_no_active_run(self):
        """Nessuna run → exit 0 silenzioso, nessuna konsole."""
        record = Path(self._tmp.name) / "record.txt"
        r = self._watch_outcome(pgrep_rc=1, record=record)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        lines = record.read_text().splitlines()
        self.assertEqual(lines, ["pgrep:-f [b]uo (resume|unleash)"])
        self.assertFalse(any(l.startswith("konsole:") for l in lines))

    # --- convivenza col cleanup esistente -------------------------------

    def test_cleanup_resume_keeps_watch_files(self):
        """Il cleanup del servizio a fine ciclo non tocca i file watch."""
        self._patch()
        rm = RebootManager()
        rm._create_resume_service()
        rm.cleanup()
        self.assertFalse(self.svc.exists())
        self.assertTrue(self.script.exists())   # restano, innocui
        self.assertTrue(self.desktop.exists())


    # --- viewer: funzioni pure (file installato su tmp_path) -------------

    def _viewer_module(self):
        """Carica il viewer installato su tmp_path (senza eseguirlo)."""
        spec = importlib.util.spec_from_file_location(
            "buo_watch", str(self.view))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_viewer_filter_line(self):
        """Prefisso 'asctime | LEVEL | name | ' rimosso; il resto verbatim."""
        self._patch()
        self._create()
        mod = self._viewer_module()
        self.assertEqual(
            mod.filter_line(_PREFIX + "Fase: validate"), "Fase: validate")
        self.assertEqual(mod.filter_line("Traceback (most recent call last):"),
                         "Traceback (most recent call last):")
        # messaggio con " | " dentro: mai troncato (maxsplit=3)
        self.assertEqual(mod.filter_line(_PREFIX + "riga | con | separatori"),
                         "riga | con | separatori")

    def test_viewer_classify(self):
        """Classificazione esito dai SOLI marker del segmento."""
        self._patch()
        self._create()
        mod = self._viewer_module()
        self.assertEqual(
            mod.classify([_PREFIX + "OTTIMIZZAZIONE COMPLETATA"]), "completed")
        self.assertEqual(mod.classify([_PREFIX + "SAFETY VIOLATION: caldo"]),
                         "safety")
        self.assertEqual(mod.classify([_PREFIX + "Errore in fase validate: x"]),
                         "error")
        self.assertEqual(mod.classify([_PREFIX + "Errore fatale: x"]), "error")
        self.assertEqual(mod.classify([_PREFIX + "riga qualsiasi"]), "unclear")
        # precedenza: completamento > safety > errore
        self.assertEqual(mod.classify([_PREFIX + "SAFETY VIOLATION: caldo",
                                       _PREFIX + "OTTIMIZZAZIONE COMPLETATA"]),
                         "completed")
        self.assertEqual(mod.classify([_PREFIX + "Errore in fase validate: x",
                                       _PREFIX + "SAFETY VIOLATION: caldo"]),
                         "safety")

    def test_viewer_phase_line(self):
        """Riga fase dallo stato; omessa se illeggibile o fuori tabella."""
        self._patch()
        self._create()
        mod = self._viewer_module()
        self.state.write_text('{"current_phase": "validate"}',
                              encoding="utf-8")
        self.assertEqual(
            mod.phase_line(str(self.state)),
            "Fase 7 di 7: Validazione — stress test e verifica fix")
        self.state.write_text('{"current_phase": "init"}', encoding="utf-8")
        self.assertEqual(mod.phase_line(str(self.state)),
                         "Fase 1 di 7: Inizializzazione")
        self.state.write_text('{"current_phase": "complete"}',
                              encoding="utf-8")
        self.assertEqual(mod.phase_line(str(self.state)), "")
        self.state.write_text('{"current_phase": "ignota"}', encoding="utf-8")
        self.assertEqual(mod.phase_line(str(self.state)), "")
        self.assertEqual(mod.phase_line(str(self.state) + ".missing"), "")

    def test_viewer_banner_for(self):
        """Blocchi terminali: testo esatto, separatore 56, righe < 80."""
        self._patch()
        self._create()
        mod = self._viewer_module()
        cases = {
            "completed": "Run COMPLETATA — esito positivo.",
            "safety": "SAFETY VIOLATION — run interrotta per sicurezza.",
            "error": "Run INTERROTTA — l'ottimizzazione non è stata "
                     "completata.",
            "unclear": "La run si è fermata senza un esito riconoscibile",
        }
        for outcome, expected in cases.items():
            block = mod.banner_for(outcome)
            self.assertTrue(block.startswith(mod.SEP + "\n"), outcome)
            self.assertIn(expected, block)
            for line in block.splitlines():
                self.assertLessEqual(len(line), 79, outcome)
        self.assertIn("Cosa fare:", mod.banner_for("error"))
        self.assertNotIn("Cosa fare:", mod.banner_for("unclear"))

    # --- viewer: end-to-end via subprocess (env override + fake pgrep) ---

    def _run_viewer(self, pgrep_rc, log_lines, phase="validate",
                    timeout=10):
        """Installa il viewer e lo esegue con log/state finti via env."""
        self._patch()
        self._create()
        self.log.write_text("".join(log_lines), encoding="utf-8")
        self.state.write_text(json.dumps({"current_phase": phase}),
                              encoding="utf-8")
        bindir = Path(self._tmp.name) / "bin"
        bindir.mkdir(exist_ok=True)
        (bindir / "pgrep").write_text(
            '#!/bin/sh\nexit "${FAKE_PGREP_RC:-0}"\n')
        os.chmod(bindir / "pgrep", 0o755)
        env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}",
                   FAKE_PGREP_RC=str(pgrep_rc),
                   BUO_WATCH_LOG=str(self.log),
                   BUO_WATCH_STATE=str(self.state))
        return subprocess.run([sys.executable, str(self.view)], env=env,
                              capture_output=True, text=True, timeout=timeout)

    def test_viewer_live_run_shows_header_without_block(self):
        """Run viva: header (ripresa + fase 7), nessun blocco, non esce."""
        lines = [_PREFIX + "Avvio ottimizzazione (BUO v1.3.0)\n",
                 _PREFIX + "Fase: validate\n"]
        with self.assertRaises(subprocess.TimeoutExpired) as ctx:
            self._run_viewer(pgrep_rc=0, log_lines=lines, timeout=3)
        out = ctx.exception.stdout
        if isinstance(out, bytes):      # output parziale non decodificato
            out = out.decode("utf-8", "replace")
        self.assertIn("BUO — BC-250 Ultimate Orchestrator", out)
        self.assertIn("La run è RIPRESA dopo il reboot ed è in corso.", out)
        self.assertIn("Fase 7 di 7: Validazione — stress test e verifica fix",
                      out)
        self.assertIn("Fase: validate", out)
        self.assertNotIn("Run COMPLETATA", out)
        self.assertNotIn("Run INTERROTTA", out)

    def test_viewer_completed_run_exits_with_banner(self):
        """Run terminata con marcatore di completamento → blocco esito ok."""
        lines = [_PREFIX + "Avvio ottimizzazione (BUO v1.3.0)\n",
                 _PREFIX + "Fase: validate\n",
                 _PREFIX + "Stress in corso: 3:00/10:00 — CPU 78°C · GPU 65°C"
                 " (massimi, nessun errore finora)\n",
                 _PREFIX + "OTTIMIZZAZIONE COMPLETATA\n",
                 _PREFIX + "Riepilogo finale\n"]
        r = self._run_viewer(pgrep_rc=1, log_lines=lines, phase="complete")
        self.assertEqual(r.returncode, 0)
        self.assertIn("Run COMPLETATA — esito positivo.", r.stdout)
        self.assertIn("Puoi chiudere questa finestra.", r.stdout)
        self.assertIn("Stress in corso: 3:00/10:00 — CPU 78°C · GPU 65°C",
                      r.stdout)          # messaggio filtrato, prefisso via
        self.assertNotIn("buo.Orchestrator", r.stdout)
        self.assertNotIn("Fase 7 di 7", r.stdout)   # current_phase=complete

    def test_viewer_error_run_exits_with_interrupted_banner(self):
        """Run terminata con Errore in fase → blocco INTERROTTA + Cosa fare."""
        lines = [_PREFIX + "Avvio ottimizzazione (BUO v1.3.0)\n",
                 _PREFIX + "Fase: validate\n",
                 _PREFIX + "Errore in fase validate: stress fallito\n"]
        r = self._run_viewer(pgrep_rc=1, log_lines=lines)
        self.assertEqual(r.returncode, 0)
        self.assertIn("Run INTERROTTA — l'ottimizzazione non è stata "
                      "completata.", r.stdout)
        self.assertIn("Cosa fare:", r.stdout)
        self.assertIn("sudo buo doctor", r.stdout)

    def test_viewer_starts_at_last_run_marker(self):
        """Offset: si parte dall'ultimo 'Avvio ottimizzazione' (la ripresa)."""
        lines = [_PREFIX + "Avvio ottimizzazione (BUO v1.2.0)\n",  # run prima
                 _PREFIX + "Fase: fix\n",
                 _PREFIX + "Avvio ottimizzazione (BUO v1.3.0)\n",  # ripresa
                 _PREFIX + "Fase: validate\n",
                 _PREFIX + "OTTIMIZZAZIONE COMPLETATA\n"]
        r = self._run_viewer(pgrep_rc=1, log_lines=lines)
        self.assertEqual(r.returncode, 0)
        self.assertIn("Fase: validate", r.stdout)
        self.assertNotIn("Fase: fix", r.stdout)   # storia precedente esclusa
        self.assertIn("Run COMPLETATA — esito positivo.", r.stdout)


if __name__ == "__main__":
    unittest.main()
