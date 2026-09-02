#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test di profili OC: ProfileStore (load/save atomico, corrotto → .bak +
default, ri-semina Certificato da SiliconView), ProfileValidator anti-zona,
suggest_vid. Mai hardware reale: directory temporanee.
"""

import json
import tempfile
import unittest
from pathlib import Path

from buo.oc.constants import (
    HANG_ZONE_MIN_FREQ,
    HANG_ZONE_MIN_VID,
    WALL_FREQ,
)
from buo.oc.profiles import (
    Profile,
    ProfileStore,
    ProfileValidator,
    SiliconView,
)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.oc = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data):
        p = self.oc / name
        p.write_text(json.dumps(data) if isinstance(data, dict) else data,
                     encoding="utf-8")
        return p


def _silicon():
    return {
        "schema_version": 1,
        "hardware_fingerprint": "fp1",
        "updated_at": "2026-09-02T09:00:00Z",
        "curve": {"3725": {"vid_cap": 1125, "scale": -7},
                  "3750": {"vid_cap": 1162, "scale": -10}},
        "winner": {"freq": 3775, "vid_cap": 1206, "scale": -7},
        "thermal": {"max_temperature_smu": 85},
        "confidence": {"points_tested": 71},
    }


class TestSiliconView(Base):
    def test_missing_returns_none(self):
        self.assertIsNone(SiliconView(self.oc).load())

    def test_expected_vid(self):
        self.write("silicon-profile.json", _silicon())
        v = SiliconView(self.oc)
        self.assertEqual(v.expected_vid(3725), 1125)
        self.assertIsNone(v.expected_vid(9999))

    def test_winner(self):
        self.write("silicon-profile.json", _silicon())
        v = SiliconView(self.oc)
        self.assertEqual(v.winner(), (3775, 1206))

    def test_thermal_max(self):
        self.write("silicon-profile.json", _silicon())
        self.assertEqual(SiliconView(self.oc).thermal_max_temperature(), 85)


class TestProfileStore(Base):
    def test_defaults_without_file(self):
        store = ProfileStore(self.oc)
        profiles = store.load()
        ids = [p.id for p in profiles]
        self.assertEqual(ids[:2], ["stock", "certified"])
        stock = profiles[0]
        self.assertEqual(stock.freq, 3500)
        self.assertTrue(stock.validated)

    def test_certified_seeded_from_silicon(self):
        self.write("silicon-profile.json", _silicon())
        profiles = ProfileStore(self.oc).load()
        cert = next(p for p in profiles if p.id == "certified")
        self.assertEqual(cert.freq, 3775)
        self.assertEqual(cert.vid_cap, 1206)
        self.assertEqual(cert.scale, -7)   # winner.scale
        self.assertTrue(cert.validated)
        self.assertEqual(cert.source, "silicon")

    def test_certified_seed_scale_from_curve(self):
        sil = _silicon()
        sil["winner"] = {"freq": 3725, "vid_cap": 1125, "scale": 0}
        self.write("silicon-profile.json", sil)
        cert = next(p for p in ProfileStore(self.oc).load()
                    if p.id == "certified")
        self.assertEqual(cert.scale, -7)   # curve[3725].scale

    def test_save_load_roundtrip(self):
        store = ProfileStore(self.oc)
        profiles = store.load()
        profiles.append(Profile(id="custom-x", name="X", freq=3600,
                                scale=-10, vid_cap=975, source="user",
                                validated=False))
        store.save(profiles, active="stock",
                   last_apply={"profile": "stock", "ts": "t", "result": "ok",
                               "persisted": False, "cause": None})
        store2 = ProfileStore(self.oc)
        loaded = store2.load()
        ids = [p.id for p in loaded]
        self.assertIn("custom-x", ids)
        cx = next(p for p in loaded if p.id == "custom-x")
        self.assertEqual(cx.freq, 3600)
        self.assertEqual(cx.scale, -10)
        self.assertEqual(cx.vid_cap, 975)

    def test_corrupt_file_backup_and_defaults(self):
        self.write("profiles.json", "{ rotto")
        store = ProfileStore(self.oc)
        profiles = store.load()
        self.assertEqual([p.id for p in profiles][:2],
                         ["stock", "certified"])
        self.assertTrue((self.oc / "profiles.json.bak").exists())

    def test_get_by_id_and_name(self):
        self.write("silicon-profile.json", _silicon())
        store = ProfileStore(self.oc)
        self.assertEqual(store.get("stock").id, "stock")
        self.assertEqual(store.get("Stock").id, "stock")   # case-insensitive
        self.assertIsNone(store.get("inesistente"))

    def test_save_is_atomic_no_tmp_left(self):
        store = ProfileStore(self.oc)
        store.save(store.load())
        self.assertFalse((self.oc / "profiles.json.tmp").exists())


class TestProfileValidator(Base):
    def setUp(self):
        super().setUp()
        self.v = ProfileValidator()
        self.sil = SiliconView(self.oc)

    def p(self, freq, scale, vid=None):
        return Profile(id="x", name="x", freq=freq, scale=scale,
                       vid_cap=vid, source="user", validated=False)

    def assert_block(self, p, frag=None):
        ok, reason = self.v.zone_ok(p)
        self.assertFalse(ok)
        if frag:
            self.assertIn(frag, reason)

    def test_zone_hang_vid_below_1000_block(self):
        self.assert_block(self.p(HANG_ZONE_MIN_FREQ, -7, 950), "zona di hang")
        self.assert_block(self.p(3750, -7, 999), "zona di hang")

    def test_zone_hang_vid_1000_ok(self):
        ok, _ = self.v.zone_ok(self.p(3725, -7, 1000))
        self.assertTrue(ok)

    def test_zone_vid_none_block(self):
        self.assert_block(self.p(3750, -7, None), "VID non verificabile")

    def test_certified_3775_1206_ok(self):
        ok, _ = self.v.zone_ok(self.p(3775, -7, 1206))
        self.assertTrue(ok)

    def test_3850_1000_ok(self):
        ok, _ = self.v.zone_ok(self.p(3850, -7, 1000))
        self.assertTrue(ok)

    def test_wall_freq_block(self):
        self.assert_block(self.p(WALL_FREQ, 0, 1200), "muro")

    def test_downclock_allowed(self):
        ok, _ = self.v.zone_ok(self.p(3300, 0, None))
        self.assertTrue(ok)

    def test_scale_out_of_range_block(self):
        self.assert_block(self.p(3600, -60, 975), "scale")
        self.assert_block(self.p(3600, 5, 975), "scale")

    def test_vid_over_hard_block(self):
        self.assert_block(self.p(3600, -7, 1400), "hard limit")

    def test_suggest_vid(self):
        self.write("silicon-profile.json", _silicon())
        self.assertEqual(self.v.suggest_vid(3725, self.sil), 1125)
        self.assertIsNone(self.v.suggest_vid(9999, self.sil))


if __name__ == "__main__":
    unittest.main()
