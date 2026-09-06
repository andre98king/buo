#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test del RIUSO dello stato OC certificato da parte di `buo unleash`
(design DESIGN_UNLEASH_OC_BOUNDARY.md T1, decisioni 06/09):
- fingerprint silicio (mirror fp_capture/fp_hash del motore);
- OCReuseGate: criterio ibrido di certificazione (L2 nel silicon O profilo
  certified validated da un apply ok allineato) + fingerprint coerente +
  fuori zona (incluse le regole anti-hang statiche e il mirror tier-2
  3800/1125). Mai hardware reale: directory temporanee + fingerprint
  esplicita.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from buo.config import BUOConfig
from buo.orchestrator import Orchestrator
from buo.oc.profiles import (
    OCReuseGate,
    Profile,
    ProfileStore,
    ProfileValidator,
    SiliconView,
    machine_silicon_fingerprint,
    silicon_fingerprint,
)
from buo.utils.mock import MockHardware


class TestSiliconFingerprint(unittest.TestCase):
    def test_canonical_and_stable(self):
        a = silicon_fingerprint(cpu_model="AMD BC-250 (Cyan Skillfish)",
                                gpu_pci_id="1002:1640", bios="1.90",
                                smu_support=True)
        b = silicon_fingerprint(bios="1.90", smu_support=True,
                                gpu_pci_id="1002:1640",
                                cpu_model="AMD BC-250 (Cyan Skillfish)")
        self.assertEqual(a, b)
        self.assertRegex(a, r"^[0-9a-f]{64}$")

    def test_empty_fields_excluded_smu_always_included(self):
        # Mirror motore: i campi vuoti non entrano nel JSON canonico,
        # smu_support è SEMPRE presente.
        h1 = silicon_fingerprint(cpu_model="", gpu_pci_id="", bios="",
                                 smu_support=False)
        h2 = silicon_fingerprint(cpu_model="x", gpu_pci_id="", bios="",
                                 smu_support=False)
        self.assertNotEqual(h1, h2)
        # smu_support cambia l'hash
        h3 = silicon_fingerprint(cpu_model="x", smu_support=True)
        self.assertNotEqual(h2, h3)

    def test_machine_sim_deterministic(self):
        a = machine_silicon_fingerprint(sim=True)
        b = machine_silicon_fingerprint(sim=True)
        self.assertEqual(a, b)
        self.assertRegex(a, r"^[0-9a-f]{64}$")


class BaseGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.oc = Path(self.tmp.name)
        self.fp = "abc123" * 8  # fingerprint "corrente" dei test

    def tearDown(self):
        self.tmp.cleanup()

    def write_json(self, name, data):
        (self.oc / name).write_text(json.dumps(data, indent=1),
                                    encoding="utf-8")

    def _write_state(self, *, winner_freq=3825, winner_vid=1125, scale=-26,
                     l2=True, sil_fp=None, sil_upd="2026-09-07T00:00:00Z",
                     cert_validated=False, cert_freq=None,
                     prof_upd="2026-09-07T01:00:00Z", last_apply=None):
        """silicon-profile.json + profiles.json coerenti."""
        self.write_json("silicon-profile.json", {
            "schema_version": 1,
            "hardware_fingerprint": sil_fp or self.fp,
            "updated_at": sil_upd,
            "curve": {str(winner_freq): {
                "vid_cap": winner_vid, "scale": scale,
                "l2_validated": l2, "validated": True}},
            "winner": {"freq": winner_freq, "vid_cap": winner_vid,
                       "scale": scale, "persisted": True},
            "confidence": {"points_tested": 12, "l2_passes": 1 if l2 else 0},
        })
        cf = cert_freq if cert_freq is not None else winner_freq
        self.write_json("profiles.json", {
            "schema_version": 1,
            "updated_at": prof_upd,
            "active": "certified",
            "profiles": [
                {"id": "stock", "name": "Stock", "freq": 3500, "scale": 0,
                 "vid_cap": None, "source": "builtin", "validated": True,
                 "last_applied": None},
                {"id": "certified",
                 "name": "Certificato %d@%d" % (cf, winner_vid),
                 "freq": cf, "scale": scale, "vid_cap": winner_vid,
                 "source": "silicon", "validated": cert_validated,
                 "last_applied": "2026-09-07T00:30:00Z"},
            ],
            "last_apply": (last_apply if last_apply is not None else {
                "profile": "certified", "ts": "2026-09-07T00:30:00Z",
                "result": "ok", "persisted": True, "cause": None}),
        })

    def _gate(self):
        return OCReuseGate(oc_dir=self.oc, current_fingerprint=self.fp)


class TestOCReuseGate(BaseGate):
    def test_no_state_blocked(self):
        p, note = self._gate().candidate()
        self.assertIsNone(p)
        self.assertIn("assente", note)

    def test_no_silicon_fingerprint_blocked(self):
        self._write_state(sil_fp=None)  # campo mancante
        data = json.loads((self.oc / "silicon-profile.json").read_text())
        del data["hardware_fingerprint"]
        (self.oc / "silicon-profile.json").write_text(
            json.dumps(data), encoding="utf-8")
        p, note = self._gate().candidate()
        self.assertIsNone(p)
        self.assertIn("fingerprint", note)

    def test_current_fingerprint_missing_blocked(self):
        self._write_state()
        p, note = OCReuseGate(oc_dir=self.oc,
                              current_fingerprint=None).candidate()
        self.assertIsNone(p)
        self.assertIn("non disponibile", note)

    def test_fingerprint_mismatch_blocked(self):
        self._write_state(sil_fp="f" * 64)
        p, note = self._gate().candidate()
        self.assertIsNone(p)
        self.assertIn("diversa", note)

    def test_no_winner_blocked(self):
        self._write_state()
        data = json.loads((self.oc / "silicon-profile.json").read_text())
        del data["winner"]
        (self.oc / "silicon-profile.json").write_text(
            json.dumps(data), encoding="utf-8")
        p, note = self._gate().candidate()
        self.assertIsNone(p)
        self.assertIn("nessun winner", note)

    def test_l2_evidence_ok(self):
        self._write_state()
        p, note = self._gate().candidate()
        self.assertIsNotNone(p)
        self.assertEqual(p.freq, 3825)
        self.assertEqual(p.vid_cap, 1125)
        self.assertEqual(p.scale, -26)
        self.assertTrue(p.validated)
        self.assertIn("evidenza l2", note)

    def test_no_l2_no_validated_profile_blocked(self):
        self._write_state(l2=False, cert_validated=False)
        p, note = self._gate().candidate()
        self.assertIsNone(p)
        self.assertIn("non certificato", note)

    def test_apply_evidence_ok_when_aligned(self):
        # Ibrido (decisione 06/09): niente L2 nel silicon ma il profilo
        # certified è stato validato da un apply ok sullo stesso winner.
        self._write_state(l2=False, cert_validated=True)
        p, note = self._gate().candidate()
        self.assertIsNotNone(p)
        self.assertEqual(p.freq, 3825)
        self.assertIn("evidenza apply", note)

    def test_apply_evidence_mismatched_winner_blocked(self):
        # Il certified validato dall'apply riguarda un clock DIVERSO dal
        # winner corrente del silicon → mai riusare (fail-closed).
        self._write_state(l2=False, cert_validated=True, cert_freq=3775)
        p, note = self._gate().candidate()
        self.assertIsNone(p)
        self.assertIn("non allineato", note)

    def test_winner_in_hang_zone_blocked(self):
        self._write_state(winner_freq=3725, winner_vid=1000, l2=True,
                          cert_validated=True)
        p, note = self._gate().candidate()
        self.assertIsNone(p)
        self.assertIn("zona di hang", note)

    def test_winner_in_tier2_zone_blocked(self):
        # Mirror tier-2 3800/1125 (banda wedge alla scrittura, 02/09): un
        # winner 3850@1100 non è riusabile nemmeno con evidenza L2.
        self._write_state(winner_freq=3850, winner_vid=1100, l2=True,
                          cert_validated=True)
        p, note = self._gate().candidate()
        self.assertIsNone(p)
        self.assertIn("zona di hang", note)

    def test_l2_ok_but_store_irrelevant(self):
        # Con evidenza L2 il profilo certified può anche mancare: il winner
        # del silicon È la verità certificata.
        self._write_state(l2=True, cert_validated=False, last_apply=None)
        (self.oc / "profiles.json").unlink()
        p, note = self._gate().candidate()
        self.assertIsNotNone(p)
        self.assertIn("evidenza l2", note)


class TestOrchestratorReuse(unittest.TestCase):
    """Integrazione: il riuso dello stato OC in `unleash` (mock)."""

    def setUp(self):
        self._oc_tmp = tempfile.TemporaryDirectory()
        self._state_tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._state_tmp.name
        self.oc = Path(self._oc_tmp.name)

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._oc_tmp.cleanup()
        self._state_tmp.cleanup()

    def _write_state(self, *, winner_freq=3825, winner_vid=1125, scale=-26,
                     l2=True, fp=None, sil_upd="2026-09-07T00:00:00Z",
                     cert_validated=False, prof_upd="2026-09-07T01:00:00Z"):
        fp = fp or machine_silicon_fingerprint(sim=True)
        (self.oc / "silicon-profile.json").write_text(json.dumps({
            "schema_version": 1,
            "hardware_fingerprint": fp,
            "updated_at": sil_upd,
            "curve": {str(winner_freq): {
                "vid_cap": winner_vid, "scale": scale,
                "l2_validated": l2, "validated": True}},
            "winner": {"freq": winner_freq, "vid_cap": winner_vid,
                       "scale": scale, "persisted": True},
        }), encoding="utf-8")
        (self.oc / "profiles.json").write_text(json.dumps({
            "schema_version": 1,
            "updated_at": prof_upd,
            "active": "certified",
            "profiles": [
                {"id": "stock", "name": "Stock", "freq": 3500, "scale": 0,
                 "vid_cap": None, "source": "builtin", "validated": True,
                 "last_applied": None},
                {"id": "certified",
                 "name": "Certificato %d@%d" % (winner_freq, winner_vid),
                 "freq": winner_freq, "scale": scale, "vid_cap": winner_vid,
                 "source": "silicon", "validated": cert_validated,
                 "last_applied": None},
            ],
        }), encoding="utf-8")

    def _orch(self, dry_run=False):
        hw = MockHardware(seed=42)
        hw.state.is_acpi_fixed = True
        cfg = BUOConfig()
        cfg.validation_stress_duration = 0
        cfg.benchmark_enabled = False
        orch = Orchestrator(config=cfg, mock=True, dry_run=dry_run,
                            mock_hardware=hw, oc_dir=self.oc)
        orch.checkpoint.clear()
        return orch

    def _run(self, orch):
        rc = orch.run()
        self.assertEqual(rc, 0)
        phases = orch.checkpoint.full_state()["phases"]
        return phases["optimize"]["data"], phases["apply"]["data"]

    def test_reuse_skips_research_and_applies_certified(self):
        self._write_state(l2=True)
        orch = self._orch()
        opt, ap = self._run(orch)
        self.assertIn("reuse_oc", opt)
        self.assertEqual(opt["reuse_oc"]["freq"], 3825)
        # ricerca UV/sweep SALTATA
        self.assertNotIn("undervolt_cpu", opt)
        self.assertNotIn("undervolt_gpu", opt)
        # apply del certificato via ApplyManager
        self.assertNotIn("governor_config", ap)
        ra = ap["reuse_apply"]
        self.assertEqual(ra["outcome"], "ok")
        self.assertEqual(ra["freq"], 3825)
        self.assertEqual(ra["scale"], -26)
        self.assertEqual(ap["cpu_final"]["freq"], 3825)
        self.assertEqual(ap["cpu_final"]["scale"], -26)
        self.assertFalse(ap["cpu_final"]["persistent"])  # sim: mai persist
        self.assertTrue(any("RIUSATA" in n for n in orch.results["notes"]))

    def test_reuse_apply_evidence_accepted(self):
        # Ibrido: niente L2 nel silicon ma profilo certified validato da un
        # apply ok (profiles.json più recente del silicon).
        self._write_state(l2=False, cert_validated=True)
        orch = self._orch()
        opt, ap = self._run(orch)
        self.assertIn("reuse_oc", opt)
        self.assertNotIn("undervolt_cpu", opt)
        self.assertEqual(ap["reuse_apply"]["outcome"], "ok")

    def test_no_state_falls_back_to_research(self):
        orch = self._orch()  # oc_dir vuoto
        opt, ap = self._run(orch)
        self.assertNotIn("reuse_oc", opt)
        self.assertIn("undervolt_cpu", opt)   # ricerca normale
        self.assertNotIn("reuse_apply", ap)

    def test_fingerprint_mismatch_falls_back(self):
        self._write_state(l2=True, fp="f" * 64)
        orch = self._orch()
        opt, _ = self._run(orch)
        self.assertNotIn("reuse_oc", opt)
        self.assertIn("undervolt_cpu", opt)

    def test_winner_in_zone_falls_back(self):
        self._write_state(winner_freq=3725, winner_vid=1000, l2=True)
        orch = self._orch()
        opt, _ = self._run(orch)
        self.assertNotIn("reuse_oc", opt)
        self.assertIn("undervolt_cpu", opt)

    def test_winner_in_tier2_zone_falls_back(self):
        self._write_state(winner_freq=3850, winner_vid=1100, l2=True)
        orch = self._orch()
        opt, _ = self._run(orch)
        self.assertNotIn("reuse_oc", opt)
        self.assertIn("undervolt_cpu", opt)

    def test_reuse_apply_rolled_back_reports_note(self):
        # Apply del certificato fallito (smoke/rollback): la run NON aborta,
        # la config precedente resta e la nota va nel report.
        from unittest import mock as _mock
        self._write_state(l2=True)
        orch = self._orch()
        orch.checkpoint.set_phase("optimize", {
            "reuse_oc": {"profile": "certified", "freq": 3825, "scale": -26,
                         "vid_cap": 1125, "note": "x"}}, completed=True)
        with _mock.patch.object(
                Orchestrator, "_apply_reused_oc",
                return_value={"outcome": "rolled_back", "cause": "smoke fail",
                              "freq": 3825, "scale": -26, "vid_cap": 1125}):
            ap = orch._phase_apply()
        self.assertEqual(ap["reuse_apply"]["outcome"], "rolled_back")
        self.assertNotIn("cpu_final", ap)
        self.assertTrue(any("NON applicato" in n
                            for n in orch.results["notes"]))

    def test_dry_run_never_reuses(self):
        # In dry-run non si valutano né si applicano stati OC (nessuna
        # lettura hardware reale; la simulazione descrive la base sicura).
        # Il checkpoint non viene scritto in dry-run → si ispeziona la
        # fase direttamente.
        self._write_state(l2=True)
        orch = self._orch(dry_run=True)
        data = orch._phase_optimize()
        self.assertNotIn("reuse_oc", data)
        self.assertIn("undervolt_cpu", data)


if __name__ == "__main__":
    unittest.main()
