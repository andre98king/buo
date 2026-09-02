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
    confirm_text,
    profiles_table_rows,
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

    def test_gated_vid_shows_lock(self):
        # VID None (gated: governor attivo) → 🔒 mostrato onestamente
        text = sensors_text({"cpu_freq": 3700, "cpu_vid": None,
                             "cpu_temp": 60.0})
        self.assertIn("🔒", text)

    def test_gated_soc(self):
        text = sensors_text({"cpu_freq": 3700, "cpu_temp": 60.0,
                             "total_power": None})
        self.assertIn("🔒", text)


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
        self.assertEqual(rows[1][3], "✅")

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


if __name__ == "__main__":
    unittest.main()
