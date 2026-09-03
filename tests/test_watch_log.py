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

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from buo.state.reboot import RebootManager


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
        self.plasma = b / "usr-share" / "wayland-sessions" / "plasma.desktop"
        self.log.parent.mkdir(parents=True)
        self.log.write_text("riga di log\n", encoding="utf-8")
        os.chmod(self.log, 0o600)          # root-only, come in produzione
        os.chmod(self.log.parent, 0o700)

    def tearDown(self):
        self._tmp.cleanup()

    def _patch(self, getent_rc=0, getent_out=None, plasma=True):
        """Punta tutti i percorsi di RebootManager su tmp_path."""
        if getent_out is None:
            getent_out = _getent_line(str(self.home))
        patched = {
            "SERVICE_PATH": self.svc,
            "WATCH_SCRIPT": self.script,
            "WATCH_LOG": self.log,
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
        """buo-resume.service creato → anche script + .desktop + perms."""
        self._patch()
        self.assertTrue(self._create())
        self.assertTrue(self.svc.exists())            # servizio come prima
        self.assertTrue(self.script.exists())         # watch script 755
        self.assertEqual(os.stat(self.script).st_mode & 0o777, 0o755)
        body = self.script.read_text(encoding="utf-8")
        self.assertIn('pgrep -f "[b]uo (resume|unleash)"', body)
        self.assertIn(f"exec konsole --hold -e tail -f {self.log}", body)
        self.assertTrue(self.desktop.exists())        # autostart
        self.assertIn(f"Exec={self.script}",
                      self.desktop.read_text(encoding="utf-8"))
        self.assertEqual(os.stat(self.log).st_mode & 0o777, 0o644)
        self.assertEqual(os.stat(self.log.parent).st_mode & 0o777, 0o755)

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
        """Run attiva (pgrep exit 0) → konsole col tail del log."""
        record = Path(self._tmp.name) / "record.txt"
        r = self._watch_outcome(pgrep_rc=0, record=record)
        self.assertEqual(r.returncode, 0)
        lines = record.read_text().splitlines()
        self.assertEqual(lines[0], "pgrep:-f [b]uo (resume|unleash)")
        self.assertEqual(lines[1], f"konsole:--hold -e tail -f {self.log}")

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


if __name__ == "__main__":
    unittest.main()
