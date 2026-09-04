#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Viewer watch (UX reboot-resume, rev. 3) — funzioni pure + modalità ANSI
+ smoke cockpit textual.

Il file testato è il viewer GENERATO da RebootManager._watch_view() su
tmp_path (importlib.util.spec_from_file_location), come installato in
produzione. Qui NON si testa l'installazione (sta in test_watch_log.py).

- Funzioni pure (parse_line/style_for/status_lines/banner_lines/classify):
  nessuna dipendenza da textual, testabili ovunque.
- E2E modalità ANSI: subprocess deterministico con BUO_WATCH_FORCE_ANSI=1
  + env log/state + fake pgrep in PATH.
- Smoke cockpit: solo se textual è importabile nel python di test
  (skipIf, come gli altri skip per dipendenze opzionali del repo); la
  validazione VISIVA finale avviene sul campo (konsole reale al login).

Mai file reali: tutto su tmp_path.
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from buo.state.reboot import RebootManager

# Prefisso standard di una riga del log buo (asctime | LEVEL | name | msg).
_PREFIX = "2026-09-04 13:25:02,391 | INFO | buo.Orchestrator | "


def _strip_ansi(s):
    """Rimuove le sequenze ANSI SGR (testo visibile puro)."""
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def _write_viewer(path: Path) -> None:
    """Genera il viewer dual-mode su tmp_path (come all'installazione)."""
    path.write_text(RebootManager()._watch_view(), encoding="utf-8")
    os.chmod(path, 0o755)


class ViewerPureTest(unittest.TestCase):
    """Funzioni pure del viewer generato (nessuna dipendenza)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.view = Path(self._tmp.name) / "buo-watch.py"
        _write_viewer(self.view)

    def tearDown(self):
        self._tmp.cleanup()

    def _module(self):
        spec = importlib.util.spec_from_file_location(
            "buo_watch_view", str(self.view))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    # --- parse ---------------------------------------------------------

    def test_parse_line(self):
        """Riga formattata → (level, msg); senza formato → None."""
        mod = self._module()
        self.assertEqual(mod.parse_line(_PREFIX + "Fase: validate"),
                         ("INFO", "Fase: validate"))
        self.assertEqual(
            mod.parse_line("2026-09-04 13:25:02,391 | ERROR | "
                           "buo.Orchestrator | Errore in fase validate: x"),
            ("ERROR", "Errore in fase validate: x"))
        # il messaggio può contenere " | ": mai troncato (maxsplit=3)
        self.assertEqual(mod.parse_line(_PREFIX + "riga | con | separatori"),
                         ("INFO", "riga | con | separatori"))
        self.assertIsNone(mod.parse_line("Traceback (most recent call last):"))
        self.assertIsNone(mod.parse_line(""))

    # --- style_for (mappa semantica §4.6) ------------------------------

    def test_style_for_table(self):
        """style_for: primo match vince, default nessuno stile."""
        mod = self._module()
        self.assertEqual(mod.style_for("ERROR", "Errore in fase validate: x"),
                         ("red", False))
        self.assertEqual(mod.style_for("CRITICAL", "x"), ("red", False))
        self.assertEqual(mod.style_for("WARNING", "ATTENZIONE: temp alta"),
                         ("yellow", False))
        self.assertEqual(mod.style_for("INFO", "SAFETY VIOLATION: caldo"),
                         ("bold red", False))
        self.assertEqual(mod.style_for("INFO", "OTTIMIZZAZIONE COMPLETATA"),
                         ("bold green", False))
        self.assertEqual(mod.style_for("INFO", "Fase: validate"),
                         ("bold cyan", False))
        # stato riepilogo: da "Riepilogo finale" dim; rollback in giallo
        self.assertEqual(mod.style_for("INFO", "Riepilogo finale"),
                         ("bold white", True))
        self.assertEqual(mod.style_for(
            "INFO", "  fix applicati in questa run: 5", True),
            ("dim", True))
        self.assertEqual(mod.style_for(
            "INFO", "  rollback: sudo buo rollback", True),
            ("bold yellow", True))
        # default: nessun colore (ticker/altre righe)
        self.assertEqual(mod.style_for("INFO", "Stress in corso: 3:00/10:00"),
                         ("", False))
        self.assertEqual(mod.style_for("DEBUG", "dettaglio"), ("", False))

    # --- classify ------------------------------------------------------

    def test_classify(self):
        """Classificazione esito dai SOLI marker del segmento."""
        mod = self._module()
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

    # --- stato: riga fase / status_lines -------------------------------

    def _state(self, payload):
        st = Path(self._tmp.name) / "state.json"
        st.write_text(json.dumps(payload), encoding="utf-8")
        return str(st)

    def test_phase_line(self):
        """Riga fase dallo stato; omessa se illeggibile o fuori tabella."""
        mod = self._module()
        self.assertEqual(
            mod.phase_line(self._state({"current_phase": "validate"})),
            "Fase 7 di 7: Validazione — stress test e verifica fix")
        self.assertEqual(mod.phase_line(self._state({"current_phase": "init"})),
                         "Fase 1 di 7: Inizializzazione")
        self.assertEqual(mod.phase_line(self._state({"current_phase": "complete"})),
                         "")
        self.assertEqual(mod.phase_line(self._state({"current_phase": "ignota"})),
                         "")
        self.assertEqual(mod.phase_line(self._state({}) + ".missing"), "")

    def test_status_lines(self):
        """Righe di stato §5.1: modo + fase solo se leggibile (C1)."""
        mod = self._module()
        st = self._state({"current_phase": "validate"})
        self.assertEqual(
            mod.status_lines(st, True),
            ["La run è RIPRESA dopo il reboot ed è in corso.",
             "Fase 7 di 7: Validazione — stress test e verifica fix"])
        self.assertEqual(
            mod.status_lines(st, False), ["Ottimizzazione in corso.",
                                          "Fase 7 di 7: Validazione — "
                                          "stress test e verifica fix"])
        # stato illeggibile o current_phase fuori tabella → fase OMESSA
        self.assertEqual(mod.status_lines(self._state({}) + ".missing", True),
                         ["La run è RIPRESA dopo il reboot ed è in corso."])
        self.assertEqual(
            mod.status_lines(self._state({"current_phase": "complete"}), True),
            ["La run è RIPRESA dopo il reboot ed è in corso."])

    # --- banner (verbatim §5.3) e veste ANSI ---------------------------

    def test_banner_lines_verbatim(self):
        """Blocchi d'esito §5.3 verbatim, senza separatore, righe < 80."""
        mod = self._module()
        comp = mod.banner_lines("completed")
        self.assertEqual(comp, ["Run COMPLETATA — esito positivo.",
                                "Riepilogo finale qui sopra. Puoi chiudere "
                                "questa finestra."])
        err = mod.banner_lines("error")
        self.assertEqual(err[0],
                         "Run INTERROTTA — l'ottimizzazione non è stata "
                         "completata.")
        self.assertIn("Cosa fare:", err)
        self.assertNotIn("Cosa fare:", mod.banner_lines("unclear"))
        for outcome in ("completed", "error", "safety", "unclear"):
            for line in mod.banner_lines(outcome):
                self.assertLessEqual(len(line), 79, outcome)
        self.assertEqual(mod.banner_lines("safety")[0],
                         "SAFETY VIOLATION — run interrotta per sicurezza.")

    def test_wrap_ansi_and_banner(self):
        """wrap_ansi: token → SGR+RST, testo invariato (C1); banner SGR."""
        mod = self._module()
        self.assertEqual(mod.wrap_ansi("testo", "bold red"),
                         "\x1b[1;31mtesto\x1b[0m")
        self.assertEqual(mod.wrap_ansi("testo", ""), "testo")   # plain
        self.assertEqual(_strip_ansi(mod.wrap_ansi("testo", "cyan")), "testo")
        # blocco ANSI: strip → separatore + righe verbatim
        for outcome in ("completed", "error", "safety", "unclear"):
            self.assertEqual(
                _strip_ansi(mod.banner_ansi(outcome)),
                mod.SEP + "\n" + "\n".join(mod.banner_lines(outcome)) + "\n",
                outcome)
        comp = mod.banner_ansi("completed")
        self.assertIn("\x1b[1;32mRun COMPLETATA — esito positivo.\x1b[0m",
                      comp)
        self.assertIn("\x1b[2mRiepilogo finale qui sopra. Puoi chiudere "
                      "questa finestra.\x1b[0m", comp)
        err = mod.banner_ansi("error")
        self.assertIn("\x1b[1;31mRun INTERROTTA — l'ottimizzazione non è "
                      "stata completata.\x1b[0m", err)
        self.assertIn("\x1b[1mCosa fare:\x1b[0m", err)
        self.assertEqual(err.count("\x1b[1;31m"), 1)   # solo la headline
        unc = mod.banner_ansi("unclear")
        for line in unc.splitlines():
            self.assertTrue(line.startswith("\x1b[2m") or line == "", line)


class ViewerAnsiE2ETest(unittest.TestCase):
    """Modalità ANSI end-to-end (BUO_WATCH_FORCE_ANSI=1 + fake pgrep)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        b = Path(self._tmp.name)
        self.view = b / "buo-watch.py"
        _write_viewer(self.view)
        self.log = b / "buo.log"
        self.state = b / "state.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _run_ansi(self, pgrep_rc, log_lines, phase="validate", timeout=10):
        """Esegue il viewer in modalità ANSI forzata con log/state finti."""
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
                   BUO_WATCH_FORCE_ANSI="1",
                   BUO_WATCH_LOG=str(self.log),
                   BUO_WATCH_STATE=str(self.state))
        return subprocess.run([sys.executable, str(self.view)], env=env,
                              capture_output=True, text=True, timeout=timeout)

    def test_live_run_shows_header_without_block(self):
        """Run viva: header (ripresa + fase 7), nessun blocco, non esce."""
        lines = [_PREFIX + "Avvio ottimizzazione (BUO v1.3.0)\n",
                 _PREFIX + "Fase: validate\n"]
        with self.assertRaises(subprocess.TimeoutExpired) as ctx:
            self._run_ansi(pgrep_rc=0, log_lines=lines, timeout=3)
        out = ctx.exception.stdout
        if isinstance(out, bytes):      # output parziale non decodificato
            out = out.decode("utf-8", "replace")
        plain = _strip_ansi(out)
        self.assertIn("BUO — BC-250 Ultimate Orchestrator", plain)
        self.assertIn("La run è RIPRESA dopo il reboot ed è in corso.", plain)
        self.assertIn("Fase 7 di 7: Validazione — stress test e verifica fix",
                      plain)
        self.assertIn("Fase: validate", plain)
        self.assertNotIn("Run COMPLETATA", plain)
        self.assertNotIn("Run INTERROTTA", plain)
        # veste ANSI: titolo ciano bold, stato verde, fase ciano, label dim
        self.assertIn("\x1b[1;36mBUO — BC-250 Ultimate Orchestrator\x1b[0m",
                      out)
        self.assertIn("\x1b[1;32mLa run è RIPRESA dopo il reboot ed è in "
                      "corso.\x1b[0m", out)
        self.assertIn("\x1b[36mFase 7 di 7: Validazione — stress test e "
                      "verifica fix\x1b[0m", out)
        self.assertIn("\x1b[2mLog live della run:\x1b[0m", out)
        self.assertIn("\x1b[1;36mFase: validate\x1b[0m", out)

    def test_completed_run_exits_with_banner(self):
        """Run terminata con completamento → blocco esito ok (exit 0)."""
        lines = [_PREFIX + "Avvio ottimizzazione (BUO v1.3.0)\n",
                 _PREFIX + "Fase: validate\n",
                 _PREFIX + "Stress in corso: 3:00/10:00 — CPU 78°C · GPU 65°C"
                 " (massimi, nessun errore finora)\n",
                 _PREFIX + "OTTIMIZZAZIONE COMPLETATA\n",
                 _PREFIX + "Riepilogo finale\n",
                 _PREFIX + "  fix applicati in questa run: 2 — 8 core, 40 CU\n",
                 _PREFIX + "  stress: superato · 10 minuti\n",
                 _PREFIX + "  rollback: sudo buo rollback\n"]
        r = self._run_ansi(pgrep_rc=1, log_lines=lines, phase="complete")
        self.assertEqual(r.returncode, 0)
        plain = _strip_ansi(r.stdout)
        self.assertIn("Run COMPLETATA — esito positivo.", plain)
        self.assertIn("Puoi chiudere questa finestra.", plain)
        self.assertIn("Stress in corso: 3:00/10:00 — CPU 78°C · GPU 65°C",
                      plain)          # messaggio filtrato, prefisso via
        self.assertIn("  rollback: sudo buo rollback", plain)
        self.assertNotIn("buo.Orchestrator", plain)
        self.assertNotIn("Fase 7 di 7", plain)   # current_phase=complete
        # veste ANSI: completamento verde, riepilogo dim, rollback giallo
        self.assertIn("\x1b[1;32mOTTIMIZZAZIONE COMPLETATA\x1b[0m", r.stdout)
        self.assertIn("\x1b[1;37mRiepilogo finale\x1b[0m", r.stdout)
        self.assertIn("\x1b[2m  fix applicati in questa run: 2 — 8 core, "
                      "40 CU\x1b[0m", r.stdout)
        self.assertIn("\x1b[1;33m  rollback: sudo buo rollback\x1b[0m",
                      r.stdout)
        self.assertIn("\x1b[1;32mRun COMPLETATA — esito positivo.\x1b[0m",
                      r.stdout)
        self.assertIn("\x1b[2mRiepilogo finale qui sopra. Puoi chiudere "
                      "questa finestra.\x1b[0m", r.stdout)

    def test_error_run_exits_with_interrupted_banner(self):
        """Run terminata con Errore in fase → blocco INTERROTTA + Cosa fare."""
        error_line = ("2026-09-04 13:25:02,391 | ERROR | buo.Orchestrator | "
                      "Errore in fase validate: stress fallito\n")
        lines = [_PREFIX + "Avvio ottimizzazione (BUO v1.3.0)\n",
                 _PREFIX + "Fase: validate\n",
                 error_line]
        r = self._run_ansi(pgrep_rc=1, log_lines=lines)
        self.assertEqual(r.returncode, 0)
        plain = _strip_ansi(r.stdout)
        self.assertIn("Run INTERROTTA — l'ottimizzazione non è stata "
                      "completata.", plain)
        self.assertIn("Cosa fare:", plain)
        self.assertIn("sudo buo doctor", plain)
        # veste ANSI: riga ERROR rossa, headline rossa bold, "Cosa fare:" bold
        self.assertIn("\x1b[31mErrore in fase validate: stress fallito"
                      "\x1b[0m", r.stdout)
        self.assertIn("\x1b[1;31mRun INTERROTTA — l'ottimizzazione non è "
                      "stata completata.\x1b[0m", r.stdout)
        self.assertIn("\x1b[1mCosa fare:\x1b[0m", r.stdout)

    def test_starts_at_last_run_marker(self):
        """Offset: si parte dall'ultimo 'Avvio ottimizzazione' (la ripresa)."""
        lines = [_PREFIX + "Avvio ottimizzazione (BUO v1.2.0)\n",  # run prima
                 _PREFIX + "Fase: fix\n",
                 _PREFIX + "Avvio ottimizzazione (BUO v1.3.0)\n",  # ripresa
                 _PREFIX + "Fase: validate\n",
                 _PREFIX + "OTTIMIZZAZIONE COMPLETATA\n"]
        r = self._run_ansi(pgrep_rc=1, log_lines=lines)
        self.assertEqual(r.returncode, 0)
        plain = _strip_ansi(r.stdout)
        self.assertIn("Fase: validate", plain)
        self.assertNotIn("Fase: fix", plain)   # storia precedente esclusa
        self.assertIn("Run COMPLETATA — esito positivo.", plain)


@unittest.skipIf(importlib.util.find_spec("textual") is None,
                 "textual opzionale (extra [tui])")
class ViewerCockpitSmokeTest(unittest.TestCase):
    """Smoke della cockpit textual (run_test) — solo dove textual c'è.

    La validazione VISIVA finale avviene sul campo (konsole al login):
    qui si verifica che l'app monti, legga log/state e aggiorni i pannelli.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        b = Path(self._tmp.name)
        self.view = b / "buo-watch.py"
        _write_viewer(self.view)
        self.log = b / "buo.log"
        self.state = b / "state.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _module(self):
        spec = importlib.util.spec_from_file_location(
            "buo_watch_cockpit", str(self.view))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _scenario(self, pgrep_rc, log_lines, phase, checks):
        """Avvia l'app con pgrep pilotato e log/state finti su tmp."""
        import asyncio
        self.log.write_text("".join(log_lines), encoding="utf-8")
        self.state.write_text(json.dumps({"current_phase": phase}),
                              encoding="utf-8")
        module = self._module()
        cls = module._watch_app(str(self.log), str(self.state))
        self.assertIsNotNone(cls)
        app = cls()

        async def run():
            async with app.run_test() as pilot:
                await pilot.pause(0.8)
                checks(app)
                await pilot.press("q")
                await pilot.pause(0.2)

        with mock.patch.object(module, "pgrep_active",
                               return_value=pgrep_rc):
            asyncio.run(run())

    def test_dead_run_shows_esito_panel(self):
        """Run morta con marcatore → pannello STATO col blocco esito."""
        lines = [_PREFIX + "Avvio ottimizzazione (BUO v1.3.0)\n",
                 _PREFIX + "Fase: validate\n",
                 _PREFIX + "OTTIMIZZAZIONE COMPLETATA\n"]

        def checks(app):
            self.assertTrue(app._done)
            self.assertEqual(app._outcome, "completed")
            self.assertIn("Run COMPLETATA — esito positivo.",
                          app._stato_plain)
            self.assertIn("Fase: validate", app._log_plain)

        self._scenario(pgrep_rc=False, log_lines=lines, phase="validate",
                       checks=checks)

    def test_live_run_shows_phase_and_log(self):
        """Run viva: pannello stato con modo+fase, log con le righe."""
        lines = [_PREFIX + "Avvio ottimizzazione (BUO v1.3.0)\n",
                 _PREFIX + "Fase: validate\n"]

        def checks(app):
            self.assertFalse(app._done)
            self.assertIn("La run è RIPRESA dopo il reboot ed è in corso.",
                          app._stato_plain)
            self.assertIn("Fase 7 di 7: Validazione — stress test e "
                          "verifica fix", app._stato_plain)
            self.assertIn("Fase: validate", app._log_plain)

        self._scenario(pgrep_rc=True, log_lines=lines, phase="validate",
                       checks=checks)


if __name__ == "__main__":
    unittest.main()
