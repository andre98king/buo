#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test di OcStateReader: parsing di state.json (schema 3 + chiavi additive),
file assente/corrotto → stato fresco, log tail, run.pid, pgrep mockato.

Mai hardware reale: ogni path e comando è iniettato in directory temporanee.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from buo.oc.constants import PHASE_LABELS
from buo.oc.state import OcState, OcStateReader


def _write(dirpath: Path, name: str, data) -> Path:
    p = dirpath / name
    p.write_text(json.dumps(data) if isinstance(data, dict) else data,
                 encoding="utf-8")
    return p


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.oc = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def reader(self):
        return OcStateReader(oc_dir=self.oc)


def _p1b_state():
    return {
        "schema_version": 3,
        "phase": "P1b",
        "updated_at": "2026-09-02T10:00:00Z",
        "testing": {"freq": 3725, "vid_cap": 1025, "kind": "point",
                    "started_epoch": 1788197000},
        "l2": {"status": "pending", "target_freq": None, "target_vid": None,
               "runs": 0},
        "winner_clock": None,
        "winner_vmin": None,
        "best_known_good": {"freq": 3675, "vid_cap": 1050, "scale": -7},
        "next_point": {"freq": 3725, "vid": 1025},
        "vmin_3500": {"cap": 950, "vid_measured": 949, "scale": -21},
        "ceiling": None,
        "coarse_winner": None,
        "persisted": False,
        "governor_stopped": True,
        "applied": {"freq": None, "vid_cap": None, "scale": None,
                    "source": None, "at": None},
        "points": {"3725@1000": {"status": "passed", "scale": -17,
                                 "vid_measured": 999, "temp_max": 60.0,
                                 "whea_delta": 0, "source": "run",
                                 "attempts": 1, "cause": None}},
    }


class TestReadState(Base):
    def test_missing_file_is_fresh(self):
        st = self.reader().read_state()
        self.assertIsNone(st.phase)
        self.assertIn("fresco", st.phase_label)
        self.assertEqual(st.points, {})

    def test_corrupt_file_is_fresh(self):
        _write(self.oc, "state.json", "{ non-json")
        st = self.reader().read_state()
        self.assertIsNone(st.phase)

    def test_p1b_parsing(self):
        _write(self.oc, "state.json", _p1b_state())
        st = self.reader().read_state()
        self.assertEqual(st.phase, "P1b")
        self.assertEqual(st.phase_label, PHASE_LABELS["P1b"])
        self.assertEqual(st.testing.freq, 3725)
        self.assertEqual(st.testing.vid_cap, 1025)
        self.assertEqual(st.testing.kind, "point")
        self.assertEqual(st.testing.started_epoch, 1788197000)
        self.assertEqual(st.l2.status, "pending")
        self.assertEqual(st.l2.runs, 0)
        self.assertEqual(st.best_known_good.freq, 3675)
        self.assertEqual(st.best_known_good.scale, -7)
        self.assertEqual(st.next_point.freq, 3725)
        self.assertEqual(st.vmin_3500.cap, 950)
        self.assertTrue(st.governor_stopped)
        self.assertFalse(st.persisted)
        rec = st.points["3725@1000"]
        self.assertEqual(rec.status, "passed")
        self.assertEqual(rec.scale, -17)
        self.assertEqual(rec.temp, 60.0)
        self.assertEqual(rec.attempts, 1)

    def test_done_state(self):
        data = _p1b_state()
        data["phase"] = "done"
        data["winner_clock"] = 3775
        data["winner_vmin"] = {"cap": 1206, "vid_measured": 1200, "scale": -7}
        data["persisted"] = True
        data["applied"] = {"freq": 3775, "vid_cap": 1206, "scale": -7,
                           "source": "persist_apply",
                           "at": "2026-09-02T09:00:00Z"}
        _write(self.oc, "state.json", data)
        st = self.reader().read_state()
        self.assertEqual(st.phase, "done")
        self.assertEqual(st.winner_clock, 3775)
        self.assertEqual(st.winner_vmin.cap, 1206)
        self.assertEqual(st.winner_vmin.scale, -7)
        self.assertTrue(st.persisted)
        self.assertEqual(st.applied.freq, 3775)
        self.assertEqual(st.applied.source, "persist_apply")

    def test_unknown_keys_ignored(self):
        data = _p1b_state()
        data["chiave_futura"] = {"a": 1}   # chiave additiva sconosciuta
        _write(self.oc, "state.json", data)
        st = self.reader().read_state()    # nessuna eccezione
        self.assertEqual(st.phase, "P1b")

    def test_non_numeric_fields_to_none(self):
        data = _p1b_state()
        data["winner_clock"] = "abc"
        data["vmin_3500"] = {"cap": None, "vid_measured": "x", "scale": ""}
        _write(self.oc, "state.json", data)
        st = self.reader().read_state()
        self.assertIsNone(st.winner_clock)
        self.assertIsNone(st.vmin_3500.cap)
        self.assertIsNone(st.vmin_3500.vid_measured)
        self.assertIsNone(st.vmin_3500.scale)

    def test_points_non_dict_skipped(self):
        data = _p1b_state()
        data["points"] = {"x": "non-dict"}
        _write(self.oc, "state.json", data)
        st = self.reader().read_state()
        self.assertEqual(st.points, {})


class TestLogAndPid(Base):
    def test_log_tail(self):
        lines = [f"riga {i}" for i in range(20)]
        _write(self.oc, "oc.log", "\n".join(lines) + "\n")
        tail = self.reader().log_tail(n=5)
        self.assertEqual(tail, lines[-5:])

    def test_log_tail_missing_file(self):
        self.assertEqual(self.reader().log_tail(), [])

    def test_run_pid(self):
        _write(self.oc, "run.pid", "12345\n")
        self.assertEqual(self.reader().run_pid(), 12345)

    def test_run_pid_missing_or_invalid(self):
        self.assertIsNone(self.reader().run_pid())
        _write(self.oc, "run.pid", "abc")
        self.assertIsNone(self.reader().run_pid())


class TestEngineProcess(Base):
    @mock.patch("subprocess.run")
    def test_active_returns_pid(self, run):
        run.return_value = mock.Mock(stdout="4242\n4243\n", returncode=0)
        self.assertEqual(self.reader().engine_process_active(), 4242)

    @mock.patch("subprocess.run")
    def test_inactive_returns_none(self, run):
        run.return_value = mock.Mock(stdout="", returncode=1)
        self.assertIsNone(self.reader().engine_process_active())

    @mock.patch("subprocess.run")
    def test_error_returns_none(self, run):
        run.side_effect = OSError("pgrep assente")
        self.assertIsNone(self.reader().engine_process_active())

    def test_pattern_is_bracket_form(self):
        # il pattern di default usa la doppia parentesi (self-match evitato)
        self.assertEqual(OcStateReader.engine_process_active.__defaults__[0],
                         "[o]c3600[.]sh")


class TestOcStateDataclass(Base):
    def test_fresh_defaults(self):
        st = OcState()
        self.assertIsNone(st.phase)
        self.assertEqual(st.points, {})


if __name__ == "__main__":
    unittest.main()
