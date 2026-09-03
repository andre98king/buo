#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test della cockpit OC: funzioni PURE (sensors_text / run_text /
profiles_table_rows / confirm_text) con dict fissi + guardia senza textual.
Nessun terminale, nessun hardware reale.
"""

import unittest
from unittest import mock

from buo.oc.profiles import Profile
from buo.oc.tui_app import (
    confirm_stock_text,
    confirm_stop_text,
    confirm_text,
    profiles_table_rows,
    run_empty_hint,
    run_oc_tui,
    run_text,
    sensors_text,
)


class TestSensorsText(unittest.TestCase):
    def test_full_dict(self):
        r = {"cpu_freq": 3700, "cpu_temp": 72.0, "cpu_vid": 1012,
             "gpu_freq": 1500, "gpu_temp": 67.0, "gpu_power": 100.0,
             "total_power": 175.0, "fan_speed": 1800, "ambient_temp": 22.0}
        text = sensors_text(r)
        self.assertIn("3700", text)
        self.assertIn("72.0", text)
        self.assertIn("1012", text)
        self.assertIn("1800", text)

    def test_empty_dict_no_crash(self):
        text = sensors_text({})
        self.assertIn("CPU", text)

    def test_gated_vid_shows_dash(self):
        # VID None (gated: governor attivo) → "—" (mai 🔒, mai 0)
        text = sensors_text({"cpu_freq": 3700, "cpu_vid": None,
                             "cpu_temp": 60.0})
        self.assertNotIn("🔒", text)
        self.assertIn("VID —", text)

    def test_gated_soc(self):
        text = sensors_text({"cpu_freq": 3700, "cpu_temp": 60.0,
                             "total_power": None})
        self.assertNotIn("🔒", text)
        self.assertIn("SoC —", text)

    def test_compact_missing_no_zero(self):
        """C1: sensori assenti → '—', MAI '0 MHz' (striscia compatta)."""
        text = sensors_text({})
        self.assertNotIn("0 MHz", text)
        self.assertIn("—", text)
        self.assertIn("CPU —", text)
        text2 = sensors_text({"cpu_freq": 0, "cpu_temp": None})
        self.assertIn("CPU —", text2)


class TestRunText(unittest.TestCase):
    def test_full_status(self):
        st = {
            "state": {"phase_label": "P1b ascesa",
                      "testing": {"freq": 3725, "vid_cap": 1025,
                                  "kind": "point"},
                      "l2": {"status": "pending", "runs": 3},
                      "winner": {"freq": 3675, "vid_cap": 1050},
                      "best_known_good": {"freq": 3675, "vid_cap": 1050},
                      "persisted": False},
            "process": {"active": True, "pid": 12345},
            "governor": "inactive",
        }
        text = run_text(st)
        self.assertIn("P1b ascesa", text)
        self.assertIn("3725@1025", text)
        self.assertIn("3675@1050", text)
        self.assertIn("12345", text)
        self.assertIn("FERMO", text)

    def test_empty_no_crash(self):
        text = run_text({})
        self.assertIn("fase", text)

    def test_governor_active_no_warning(self):
        st = {"state": {"phase_label": "done", "persisted": True},
              "process": {"active": False, "pid": None},
              "governor": "active"}
        text = run_text(st)
        self.assertNotIn("FERMO", text)
        self.assertIn("attivo", text)

    def test_engine_missing_shows_block(self):
        """Motore assente (fail-closed): blocco NON PRESENTE nel pannello."""
        st = {"state": {"phase_label": "done", "persisted": False},
              "process": {"active": False, "pid": None},
              "governor": "active",
              "engine": {"present": False, "executable": False}}
        text = run_text(st)
        self.assertIn("NON PRESENTE", text)
        self.assertIn("Cosa fare", text)

    def test_engine_present_row(self):
        st = {"state": {"phase_label": "done"},
              "process": {"active": False},
              "governor": "active",
              "engine": {"present": True}}
        text = run_text(st)
        self.assertIn("motore:", text)
        self.assertIn("presente", text)
        self.assertNotIn("NON PRESENTE", text)

    def test_apply_rolled_back_nota(self):
        """Stato apply rolled_back → riga nota con ROLLED BACK."""
        st = {"state": {"phase_label": "done"},
              "process": {"active": False},
              "governor": "active",
              "engine": {"present": True},
              "apply": {"state": "rolled_back", "profile": "certified"}}
        text = run_text(st)
        self.assertIn("ROLLED BACK", text)
        self.assertIn("controlla il log", text)

    def test_apply_ok_no_nota(self):
        st = {"state": {"phase_label": "done"},
              "process": {"active": False},
              "governor": "active",
              "engine": {"present": True},
              "apply": {"state": "ok"}}
        self.assertNotIn("nota", run_text(st))


class TestProfilesRows(unittest.TestCase):
    def test_rows(self):
        profiles = [
            Profile(id="stock", name="Stock", freq=3500, scale=0,
                    vid_cap=None, source="builtin", validated=True),
            Profile(id="certified", name="Certificato 3775@1206", freq=3775,
                    scale=-7, vid_cap=1206, source="silicon",
                    validated=True),
        ]
        rows = profiles_table_rows(profiles, active="stock")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1], "3500@0")
        self.assertEqual(rows[0][4], "●")       # active
        self.assertEqual(rows[1][2], "1206")
        self.assertEqual(rows[0][2], "—")       # VID mancante → "—" (D5)
        self.assertEqual(rows[0][3], "sì")      # validato → "sì" (non ✅)
        self.assertEqual(rows[1][3], "sì")

    def test_empty(self):
        self.assertEqual(profiles_table_rows([]), [])


class TestConfirmText(unittest.TestCase):
    def test_ok(self):
        p = Profile(id="x", name="X", freq=3775, scale=-7, vid_cap=1206)
        text = confirm_text(p, (True, ""))
        self.assertIn("Applicare X", text)
        self.assertNotIn("RIFIUTATO", text)

    def test_refused(self):
        p = Profile(id="x", name="X", freq=3750, scale=-7, vid_cap=950)
        text = confirm_text(p, (False, "zona di hang"))
        self.assertIn("RIFIUTATO", text)
        self.assertIn("zona di hang", text)


class TestTuiGuard(unittest.TestCase):
    @mock.patch("importlib.util.find_spec", return_value=None)
    def test_raises_without_textual(self, _find_spec):
        with self.assertRaises(RuntimeError) as ctx:
            run_oc_tui(mock=True)
        self.assertIn("textual", str(ctx.exception))

    def test_module_importable_without_textual(self):
        import buo.oc.tui_app  # noqa: F401


class TestOcTuiAlias(unittest.TestCase):
    """`buo oc-tui` è un ALIAS della cockpit unificata (v1.2): run_oc_tui
    delega a buo.tui.run_tui con il tab OC già attivo, inoltrando --oc-dir.
    Nessun terminale: run_tui è patchato (mai import textual)."""

    @mock.patch("buo.tui.run_tui")
    def test_delegates_to_unified_app_on_oc_tab(self, run_tui):
        run_oc_tui(mock=True, oc_dir="/tmp/oc-x")
        run_tui.assert_called_once_with(
            mock=True, mock_hardware=None, oc_dir="/tmp/oc-x",
            initial_tab="tab-oc")

    @mock.patch("buo.tui.run_tui")
    def test_forwards_default_oc_dir(self, run_tui):
        run_oc_tui()
        run_tui.assert_called_once_with(
            mock=False, mock_hardware=None, oc_dir=None,
            initial_tab="tab-oc")


class TestRunEmptyHint(unittest.TestCase):
    """run_empty_hint(): CTA "nessun run attivo" solo quando non c'è un
    processo e la fase è finale/assente (mai durante un run o una fase
    di lavoro utile)."""

    def test_no_hint_when_run_active(self):
        st = {"state": {"phase": "P1b"}, "process": {"active": True,
                                                     "pid": 123}}
        self.assertEqual(run_empty_hint(st), "")

    def test_no_hint_when_phase_in_progress(self):
        # run fermo ma fase di lavoro (es. dopo uno stop temporaneo):
        # lo stato del pannello parla da solo
        st = {"state": {"phase": "P1b"}, "process": {"active": False}}
        self.assertEqual(run_empty_hint(st), "")

    def test_hint_when_fresh_no_state(self):
        self.assertIn("nessun run attivo", run_empty_hint({}))

    def test_hint_when_phase_done(self):
        st = {"state": {"phase": "done", "persisted": True},
              "process": {"active": False, "pid": None},
              "governor": "active"}
        self.assertIn("nessun run attivo", run_empty_hint(st))

    def test_hint_when_phase_none(self):
        st = {"state": {"phase": None}, "process": {"active": False}}
        self.assertEqual(run_empty_hint(st),
                         run_empty_hint({"state": {}, "process": {}}))

    def test_hint_text_guides_to_start_run(self):
        hint = run_empty_hint({"state": {}, "process": {}})
        self.assertIn("[u]", hint)
        self.assertIn("convergenza CPU", hint)
        self.assertIn("silicio", hint)

    def test_no_hint_when_engine_missing(self):
        """Motore assente NON è 'nessun run': niente CTA start."""
        st = {"state": {"phase": "done"}, "process": {"active": False},
              "engine": {"present": False}}
        self.assertEqual(run_empty_hint(st), "")


class TestConfirmStockStop(unittest.TestCase):
    """NUOVE conferme pure per R e s (testi §4.8, parole + via d'uscita)."""

    def test_confirm_stock_text(self):
        stock = Profile(id="stock", name="Stock", freq=3500, scale=0,
                        vid_cap=None, source="builtin", validated=True)
        text = confirm_stock_text(stock)
        self.assertIn("STOCK", text)
        self.assertIn("Ripristinare", text)
        self.assertIn("3500", text)
        self.assertIn("y ripristina", text)
        self.assertIn("n annulla", text)

    def test_confirm_stop_text(self):
        text = confirm_stop_text()
        self.assertIn("Fermare la run", text)
        self.assertIn("checkpoint", text)
        self.assertIn("y ferma", text)
        self.assertIn("n continua", text)


if __name__ == "__main__":
    unittest.main()
