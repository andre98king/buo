#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test T5 (G2): stato OC nell'export del profilo + riapplicazione in
`buo restore` (design research/DESIGN_T5_EXPORT_G2.md, spec §5 t1-t6).

Mai hardware: export su path iniettati (tmp), restore in modalità mock
con oc_dir e governor_config_path espliciti su tmp.
"""

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from buo.config import BUOConfig
from buo.constants import EXIT_ERROR, EXIT_SAFETY_VIOLATION
from buo.exceptions import SafetyViolation
from buo.orchestrator import Orchestrator
from buo.profile import PROFILE_VERSION, export_profile, load_profile
from buo.state.checkpoint import CheckpointManager
from buo.utils.mock import MockHardware

from buo.oc import profiles as oc_profiles
from buo.oc.profiles import (OCReuseGate, ProfileStore, export_oc_state,
                             machine_silicon_fingerprint,
                             silicon_fingerprint)

OPTIMIZE_DATA = {
    "undervolt_cpu": {"best_efficiency": {"freq": 3800, "scale": 0,
                                           "vid": 1224}},
    "undervolt_gpu": {"safe_points": [{"freq": 1200, "voltage": 1000}]},
    "overclock_cpu": {"recommended_freq": 3800},
}

CPU_CONF = (b"[overclock]\nfrequency = 3825\nscale = -26\n"
            b"max_temperature = 90\n")
GPU_CONF = (b"[frequency-range]\nmin = 1000\nmax = 1800\n\n"
            b"[[safe-points]]\nfrequency = 1800\nvoltage = 800\n")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _silicon_raw(fp: str) -> dict:
    return {
        "hardware_fingerprint": fp,
        "updated_at": "2026-01-01T00:00:00Z",
        "curve": {"3825": {"vid_cap": 1125, "scale": -26,
                           "l2_validated": True}},
        "winner": {"freq": 3825, "vid_cap": 1125, "scale": -26},
    }


def _profiles_raw() -> dict:
    return {
        "schema_version": 1,
        "updated_at": "2026-01-01T00:00:00Z",
        "active": "certified",
        "profiles": [
            {"id": "stock", "name": "Stock", "freq": 3500, "scale": 0,
             "vid_cap": None, "source": "builtin", "validated": True,
             "last_applied": None},
            {"id": "certified", "name": "Certificato 3825@1125",
             "freq": 3825, "scale": -26, "vid_cap": 1125,
             "source": "silicon", "validated": True,
             "last_applied": "2026-01-01T00:00:00Z"},
        ],
        "last_apply": {"profile": "certified", "persisted": True},
    }


def _oc_block(fp: str = None) -> dict:
    fp = fp if fp is not None else machine_silicon_fingerprint(sim=True)
    return {
        "schema_version": 1,
        "exported_at": "2026-01-01T00:00:00Z",
        "hardware_fingerprint": fp,
        "silicon_profile": _silicon_raw(fp),
        "profiles": _profiles_raw(),
        "cpu_conf_b64": _b64(CPU_CONF),
        "gpu_conf_b64": _b64(GPU_CONF),
    }


class TestProfileV2Compat(unittest.TestCase):
    """t1: un profilo v1 esiste e carica identico (nessun oc_state)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def test_v1_file_loads_identical(self):
        p = Path(self._tmp.name) / "v1.json"
        p.write_text(json.dumps({
            "profile_version": 1,
            "created": "2026-01-01T00:00:00",
            "applied_fixes": ["acpi_fix", "gpu_40cu"],
            "optimize": OPTIMIZE_DATA,
        }), encoding="utf-8")
        loaded = load_profile(p)
        self.assertEqual(loaded["profile_version"], 1)
        self.assertEqual(loaded["optimize"]["undervolt_cpu"]
                         ["best_efficiency"]["freq"], 3800)
        self.assertNotIn("oc_state", loaded,
                         "v1 senza blocco: nessun oc_state aggiunto")

    def test_unsupported_version_still_raises(self):
        for bad in (0, 3):
            p = Path(self._tmp.name) / f"bad{bad}.json"
            p.write_text(json.dumps({"profile_version": bad,
                                     "optimize": {}}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_profile(p)

    def test_current_version_is_2(self):
        self.assertEqual(PROFILE_VERSION, 2)


class TestOcStateExport(unittest.TestCase):
    """t2/t3: export del blocco oc_state (campi attesi, round-trip
    base64 byte-identico) e omissione fail-soft senza accesso."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name
        self.oc_dir = Path(self._tmp.name) / "oc"
        self.oc_dir.mkdir()
        self.sil_path = self.oc_dir / "silicon-profile.json"
        self.prof_path = self.oc_dir / "profiles.json"
        self.cpu_conf = Path(self._tmp.name) / "bc250-smu-oc.conf"
        self.gpu_conf = Path(self._tmp.name) / "config.toml"
        self.fp = machine_silicon_fingerprint(sim=True)
        self.sil_path.write_text(
            json.dumps(_silicon_raw(self.fp)), encoding="utf-8")
        self.prof_path.write_text(
            json.dumps(_profiles_raw()), encoding="utf-8")
        self.cpu_conf.write_bytes(CPU_CONF)
        self.gpu_conf.write_bytes(GPU_CONF)

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def test_export_oc_state_fields_and_b64_roundtrip(self):
        state = export_oc_state(self.oc_dir, smu_conf=str(self.cpu_conf),
                                governor_config=str(self.gpu_conf))
        self.assertIsNotNone(state)
        self.assertEqual(state["schema_version"], 1)
        self.assertEqual(state["hardware_fingerprint"], self.fp)
        self.assertEqual(state["silicon_profile"],
                         json.loads(self.sil_path.read_text()))
        self.assertEqual(state["profiles"],
                         json.loads(self.prof_path.read_text()))
        self.assertEqual(base64.b64decode(state["cpu_conf_b64"]), CPU_CONF)
        self.assertEqual(base64.b64decode(state["gpu_conf_b64"]), GPU_CONF)

    def test_export_profile_embeds_oc_state_top_level(self):
        cm = CheckpointManager()
        cm.seed_phase("optimize", OPTIMIZE_DATA)
        out = Path(self._tmp.name) / "profilo.json"
        prof = export_profile(out, oc_dir=self.oc_dir,
                              smu_conf=str(self.cpu_conf),
                              governor_config=str(self.gpu_conf))
        self.assertEqual(prof["profile_version"], PROFILE_VERSION)
        block = prof["oc_state"]
        self.assertEqual(block["hardware_fingerprint"], self.fp)
        self.assertEqual(base64.b64decode(block["cpu_conf_b64"]), CPU_CONF)
        self.assertEqual(base64.b64decode(block["gpu_conf_b64"]), GPU_CONF)
        self.assertNotIn("oc_state", prof["optimize"],
                         "il blocco è top-level, NON dentro optimize")

    def test_export_without_oc_access_omits_block(self):
        """t3: export senza accesso all'OC_DIR → blocco assente, nessun
        crash."""
        out = Path(self._tmp.name) / "profilo.json"
        prof = export_profile(out, oc_dir=Path(self._tmp.name) / "assente")
        self.assertNotIn("oc_state", prof)
        loaded = load_profile(out)
        self.assertNotIn("oc_state", loaded)

    def test_export_oc_state_none_when_source_missing(self):
        self.assertIsNone(export_oc_state(
            Path(self._tmp.name) / "assente",
            smu_conf=str(self.cpu_conf),
            governor_config=str(self.gpu_conf)))

    def test_export_warns_root_only_when_dir_unreadable(self):
        """Finding 4: il warning 'serve root' scatta solo per PERMESSI
        (PermissionError); stato OC semplicemente assente → silenzio."""
        out = Path(self._tmp.name) / "profilo.json"
        missing = Path(self._tmp.name) / "assente"
        with mock.patch("buo.profile.logger.warning") as warn:
            export_profile(out, oc_dir=missing)
        warn.assert_not_called()
        with mock.patch("buo.profile.os.listdir",
                        side_effect=PermissionError("denied")), \
             mock.patch("buo.profile.logger.warning") as warn2:
            export_profile(Path(self._tmp.name) / "p2.json", oc_dir=missing)
        warn2.assert_called_once()


class TestFingerprintGate(unittest.TestCase):
    """Finding 1: il gate del restore confronta il SILICIO, non la
    presenza dei tool (smu_support) — su post-format senza toolchain la
    fingerprint non deve divergere."""

    def test_machine_fp_override_ignores_tool_presence(self):
        with mock.patch("buo.oc.profiles._smu_tools_present",
                        return_value=False), \
             mock.patch("buo.oc.profiles._cpu_model_text",
                        return_value="CPU X"), \
             mock.patch("buo.oc.profiles._gpu_pci_id",
                        return_value="1002:1640"), \
             mock.patch("buo.oc.profiles._bios_version",
                        return_value="1.90"):
            detected = machine_silicon_fingerprint(sim=False)
            forced = machine_silicon_fingerprint(sim=False,
                                                 smu_support=True)
        # senza tool la fp "rilevata" diverge (smu_support 0)…
        self.assertNotEqual(detected, forced)
        # …con l'override la fp è quella di una macchina COL tool
        self.assertEqual(
            forced,
            silicon_fingerprint(cpu_model="CPU X", gpu_pci_id="1002:1640",
                                bios="1.90", smu_support=True))


class TestRestoreOcState(unittest.TestCase):
    """t4/t5/t6: riapplicazione pre-fasi dello stato OC nel restore
    (mock: mai hardware)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name
        self.oc_dir = Path(self._tmp.name) / "oc"
        self.gpu_toml = Path(self._tmp.name) / "config.toml"

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def _orch(self):
        cfg = BUOConfig()
        cfg.validation_stress_duration = 0
        cfg.benchmark_enabled = False
        return Orchestrator(config=cfg, mock=True, dry_run=False,
                            mock_hardware=MockHardware(seed=11),
                            oc_dir=self.oc_dir,
                            governor_config_path=self.gpu_toml)

    def _profile(self, block=None):
        prof = {
            "profile_version": PROFILE_VERSION,
            "created": "2026-01-01T00:00:00",
            "applied_fixes": ["acpi_fix", "gpu_40cu"],
            "optimize": OPTIMIZE_DATA,
        }
        if block is not None:
            prof["oc_state"] = block
        return prof

    @staticmethod
    def _apply_ok(reuse):
        return {"outcome": "ok", "profile": "certified",
                "persisted": True, "cause": None}

    def test_restore_materializes_and_applies_certified_once(self):
        """t4: blocco valido → oc_dir materializzato, bytes GPU scritti su
        config.toml, ApplyManager (reuse T1) chiamato su `certified` UNA
        sola volta (la fase apply NON riapplica: niente doppio apply)."""
        block = _oc_block()
        orch = self._orch()
        orch.checkpoint.clear()
        applied = []

        def fake_apply(reuse):
            applied.append(reuse)
            return self._apply_ok(reuse)

        with mock.patch.object(orch, "_apply_reused_oc",
                               side_effect=fake_apply), \
             mock.patch.object(orch.governor, "is_running",
                               return_value=True), \
             mock.patch.object(oc_profiles, "machine_silicon_fingerprint",
                               wraps=oc_profiles.machine_silicon_fingerprint
                               ) as fp_spy:
            rc = orch.run(restore=self._profile(block))

        self.assertEqual(rc, 0)
        # il gate confronta il silicio, non la presenza dei tool (Finding 1)
        fp_spy.assert_called_with(sim=True, smu_support=True)
        # oc_dir materializzato (silicon + profiles)
        self.assertTrue((self.oc_dir / "silicon-profile.json").exists())
        self.assertTrue((self.oc_dir / "profiles.json").exists())
        # GPU: bytes salvati scritti su config.toml
        self.assertEqual(self.gpu_toml.read_bytes(), GPU_CONF)
        # CPU: certified applicato una volta sola (hook pre-fasi)
        self.assertEqual(len(applied), 1, "doppio apply del certified!")
        self.assertEqual(applied[0]["profile"], "certified")
        self.assertEqual(applied[0]["freq"], 3825)
        self.assertEqual(applied[0]["scale"], -26)
        self.assertEqual(applied[0]["vid_cap"], 1125)
        # la fase apply è saltata (marcatore) — risultati registrati
        data = orch.checkpoint.get_phase("apply").get("data", {})
        self.assertTrue(data.get("oc_state_restored"))

    @staticmethod
    def _block_without_certified() -> dict:
        block = _oc_block()
        raw = _profiles_raw()
        raw["profiles"] = [p for p in raw["profiles"]
                           if p.get("id") != "certified"]
        block["profiles"] = raw
        return block

    @staticmethod
    def _block_with_certified_freq(freq: int) -> dict:
        block = _oc_block()
        for p in block["profiles"]["profiles"]:
            if p.get("id") == "certified":
                p["freq"] = freq
                p["vid_cap"] = 1125
        return block

    def test_restore_corrupt_or_fingerprint_mismatch_skips_oc(self):
        """t5: blocco corrotto / fingerprint diversa / certified
        mancante o fuori zona → skip fail-closed, OC_DIR NON toccato,
        nota nel report; restore base non-OC."""
        cases = (
            ("fingerprint diversa",
             _oc_block(fp="0" * 64)),
            ("blocco corrotto (gpu_conf_b64 non-base64)",
             dict(_oc_block(), gpu_conf_b64="@@@ non-base64 @@@")),
            ("blocco senza silicon_profile",
             {k: v for k, v in _oc_block().items()
              if k != "silicon_profile"}),
            ("schema_version non supportata",
             dict(_oc_block(), schema_version=99)),
            ("certified mancante nel blocco",
             self._block_without_certified()),
            ("certified fuori zona (freq oltre il muro)",
             self._block_with_certified_freq(5000)),
        )
        for label, block in cases:
            with self.subTest(label):
                orch = self._orch()
                orch.checkpoint.clear()
                with mock.patch.object(
                        orch, "_apply_reused_oc",
                        side_effect=AssertionError(
                            "nessun apply in skip fail-closed (T5)")):
                    rc = orch.run(restore=self._profile(block))
                self.assertEqual(rc, 0)
                if self.oc_dir.exists():
                    self.assertEqual(list(self.oc_dir.iterdir()), [],
                                     "OC_DIR non deve essere toccato")
                self.assertTrue(
                    any("NON riapplicato" in n
                        for n in orch.results["notes"]),
                    "nota di skip nel report: %s"
                    % orch.results["notes"])

    def test_restore_rebuilt_oc_dir_serves_certified(self):
        """t6: dopo il restore, ProfileStore.load sull'OC_DIR ricostruito
        restituisce il profilo certificato (riuso T1 al run successivo)."""
        block = _oc_block()
        orch = self._orch()
        orch.checkpoint.clear()
        with mock.patch.object(orch, "_apply_reused_oc",
                               side_effect=self._apply_ok), \
             mock.patch.object(orch.governor, "is_running",
                               return_value=True):
            rc = orch.run(restore=self._profile(block))
        self.assertEqual(rc, 0)

        store = ProfileStore(self.oc_dir)
        cert = store.get("certified")
        self.assertIsNotNone(cert)
        self.assertEqual(cert.freq, 3825)
        self.assertEqual(cert.scale, -26)
        self.assertEqual(cert.vid_cap, 1125)
        self.assertTrue(cert.validated)

        # il prossimo unleash può riusare via T1 (gate del riuso)
        gate = OCReuseGate(
            oc_dir=self.oc_dir,
            current_fingerprint=machine_silicon_fingerprint(sim=True))
        candidate, note = gate.candidate()
        self.assertIsNotNone(candidate, note)
        self.assertEqual(candidate.freq, 3825)

    def test_cpu_pre_phase_fail_is_retried_in_apply_phase(self):
        """Finding 2a: apply CPU pre-fasi fallito (es. smoke) → reuse
        comunque seedato, marcatore ASSENTE → _phase_apply RITENTA il
        certified (2 chiamate totali, run ok)."""
        block = _oc_block()
        orch = self._orch()
        orch.checkpoint.clear()
        calls = []
        outcomes = iter([
            {"outcome": "aborted", "profile": "certified",
             "persisted": False, "cause": "smoke fail (test)"},
            {"outcome": "ok", "profile": "certified",
             "persisted": True, "cause": None},
        ])

        def fake_apply(reuse):
            calls.append(reuse)
            # contratto di _apply_reused_oc: freq/scale/vid_cap nel ritorno
            return {**next(outcomes), "freq": reuse["freq"],
                    "scale": reuse.get("scale"),
                    "vid_cap": reuse.get("vid_cap")}

        with mock.patch.object(orch, "_apply_reused_oc",
                               side_effect=fake_apply), \
             mock.patch.object(orch.governor, "is_running",
                               return_value=True):
            rc = orch.run(restore=self._profile(block))
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 2,
                         "retry in fase apply dopo il fail pre-fasi")
        self.assertEqual(calls[0]["freq"], 3825)
        data = orch.checkpoint.get_phase("apply").get("data", {})
        self.assertEqual((data.get("cpu_final") or {}).get("freq"), 3825)

    def test_fresh_init_without_restore_clears_oc_restored_marker(self):
        """Finding 2b: un run nuovo da init SENZA restore pulisce il
        marcatore residuo (l'apply del run successivo NON è saltato)."""
        orch = self._orch()
        orch.checkpoint.clear()
        orch.checkpoint.set("oc_state_restored", True)   # residuo anomalo
        with mock.patch.object(orch, "_phase_optimize",
                               side_effect=lambda: {}):
            rc = orch.run()
        self.assertEqual(rc, 0)
        self.assertFalse(orch.checkpoint.get("oc_state_restored"))

    def test_completed_restore_clears_oc_restored_marker(self):
        """Finding 2b: a ciclo completato il marcatore è pulito (un
        unleash successivo applica la config nuova)."""
        block = _oc_block()
        orch = self._orch()
        orch.checkpoint.clear()
        with mock.patch.object(orch, "_apply_reused_oc",
                               side_effect=self._apply_ok), \
             mock.patch.object(orch.governor, "is_running",
                               return_value=True):
            rc = orch.run(restore=self._profile(block))
        self.assertEqual(rc, 0)
        self.assertFalse(orch.checkpoint.get("oc_state_restored"),
                         "finalize deve pulire il marcatore")

    def test_abort_clears_oc_restored_marker(self):
        """Finding 2b: abort di safety ED errore di fase puliscono il
        marcatore — i run successivi non ereditano lo skip dell'apply."""

        def _boom_safety():
            raise SafetyViolation("test abort")

        def _boom_error():
            raise RuntimeError("test errore fase")

        for label, boom, expected_rc in (
            ("safety", _boom_safety, EXIT_SAFETY_VIOLATION),
            ("errore", _boom_error, EXIT_ERROR),
        ):
            with self.subTest(label):
                orch = self._orch()
                orch.checkpoint.clear()
                orch.checkpoint.set("oc_state_restored", True)
                orch.checkpoint.set_current_phase("fix")
                with mock.patch.object(orch, "_phase_fix",
                                       side_effect=boom), \
                     mock.patch.object(orch.rollback, "rollback"):
                    rc = orch.run()
                self.assertEqual(rc, expected_rc)
                self.assertFalse(orch.checkpoint.get("oc_state_restored"))

    def test_restore_gpu_fallback_template_when_service_not_active(self):
        """Finding 7c: config salvata che NON riparte (is-active fail) →
        riscritto il default dal template (write_default_config) con nota
        (D1: niente re-smoke, mai curva non verificata)."""
        block = _oc_block()
        orch = self._orch()
        orch.checkpoint.clear()
        with mock.patch.object(orch, "_apply_reused_oc",
                               side_effect=self._apply_ok), \
             mock.patch.object(orch.governor, "is_running",
                               return_value=False), \
             mock.patch.object(orch.governor, "write_default_config",
                               return_value=True) as fallback:
            reuse = orch._restore_oc_state(block)
        self.assertIsNotNone(reuse)
        self.assertEqual(self.gpu_toml.read_bytes(), GPU_CONF,
                         "bytes scritti prima del fallback")
        fallback.assert_called_once()
        self.assertTrue(any("NON attiva" in n
                            for n in orch.results["notes"]))


if __name__ == "__main__":
    unittest.main()
