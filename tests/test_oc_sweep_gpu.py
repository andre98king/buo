#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T4 — `buo oc sweep-gpu` (design research/DESIGN_T4_SWEEP_OC.md §5):
helper buo/oc/gpu_sweep.run() + comando CLI. Mai hardware reale:
FakeOptimizer iniettato (lo sweep interno è dominio di gpu.py, coperto da
test_gpu_sweep.py); crash marker guidato da boot_epoch iniettato; guard
engine da engine_active/_default_engine_active iniettate.
"""

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from buo.cli import cli
from buo.oc import gpu_sweep


def _fake_result(source="per-silicon", safe_points=None, stable=True):
    sp = (safe_points if safe_points is not None
          else [{"freq": 1200, "voltage": 800},
                {"freq": 1500, "voltage": 800}])
    result = {"safe_points": sp,
              "best_efficiency": {"freq": 1500, "voltage": 800},
              "source": source}
    if source == "per-silicon":
        result["sweep"] = {
            "enabled": True, "freqs": [p["freq"] for p in sp],
            "results": [{"freq": p["freq"], "voltage": p["voltage"],
                         "stable": stable} for p in sp],
            "duration_s": 3}
    return result


class FakeOptimizer:
    """Fake di GPUUndervoltOptimizer: registra optimize() e ritorna un
    dict esito controllato (mai hardware)."""

    def __init__(self, result=None):
        self.result = result if result is not None else _fake_result()
        self.calls = []

    def optimize(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class SweepCliCase(unittest.TestCase):
    """Casi CLI (CliRunner). In real-mode patches: engine inattivo,
    ottimizzatore fake, fingerprint fissa (mai hardware)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.oc = Path(self._tmp.name)
        self.runner = CliRunner()
        self.addCleanup(self._tmp.cleanup)

    def invoke(self, *args):
        return self.runner.invoke(cli, ["oc", "sweep-gpu", *args,
                                        "--oc-dir", str(self.oc)])

    def _patches(self, fake):
        stack = ExitStack()
        stack.enter_context(mock.patch("buo.oc.gpu_sweep._make_optimizer",
                                       return_value=fake))
        stack.enter_context(mock.patch(
            "buo.oc.gpu_sweep._default_engine_active", return_value=None))
        stack.enter_context(mock.patch(
            "buo.oc.gpu_sweep.machine_silicon_fingerprint",
            return_value="fp-t4"))
        return stack

    def test_sweep_ok_writes_esito_and_prints_winner(self):
        fake = FakeOptimizer()
        with self._patches(fake):
            res = self.invoke("--freqs", "1200,1500")
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn("Vincitore", res.output)
        self.assertIn("1500", res.output)
        self.assertIn("Safe points", res.output)
        path = self.oc / gpu_sweep.ESITO_FILE
        self.assertTrue(path.exists(), "esito scritto atteso")
        esito = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(esito["schema_version"], gpu_sweep.SWEEP_SCHEMA_VERSION)
        self.assertEqual(esito["winner"], {"freq": 1500, "voltage": 800})
        self.assertEqual(esito["source"], "per-silicon")
        self.assertEqual(esito["fingerprint"], "fp-t4")
        self.assertEqual(esito["floor_mv"], 800)
        self.assertIsNone(esito["in_probe"])
        self.assertTrue(esito["tested"])
        self.assertEqual(fake.calls[0]["sweep"]["freqs"], [1200, 1500])
        self.assertTrue(fake.calls[0]["sweep"]["enabled"])
        self.assertIn("Governor", res.output,
                      "nota governor fermo attesa (sweep per-silicio reale)")
        self.assertTrue(esito["governor_stopped"])

    def test_dry_run_and_mock_write_nothing(self):
        # C1: --mock/--dry-run → flusso simulato, nessuna scrittura reale.
        for extra in (["--dry-run"], ["--mock"], ["--mock", "--dry-run"]):
            with self.subTest(extra=extra):
                with tempfile.TemporaryDirectory() as d:
                    res = self.runner.invoke(
                        cli, ["oc", "sweep-gpu", *extra,
                              "--oc-dir", d, "--freqs", "1500"])
                self.assertEqual(res.exit_code, 0, res.output)
                self.assertFalse((Path(d) / gpu_sweep.ESITO_FILE).exists(),
                                 "nessuna scrittura reale attesa (%s)" % extra)

    def test_no_tools_falls_back_community_with_warning(self):
        # Fallback community (fail-closed di gpu.py): warning, NON errore.
        fake = FakeOptimizer(
            result=_fake_result(source="community_defaults"))
        with self._patches(fake):
            res = self.invoke("--freqs", "1200,1500,2000")
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn("community", res.output)
        path = self.oc / gpu_sweep.ESITO_FILE
        self.assertTrue(path.exists())
        esito = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(esito["source"], "community_defaults")
        self.assertNotIn("Governor", res.output,
                         "il fallback community non tocca il governor")

    def test_engine_active_refuses(self):
        # Guard REFUSE (stesso criterio di apply): engine attivo → exit 1,
        # nessun esito scritto.
        fake = FakeOptimizer()
        with mock.patch("buo.oc.gpu_sweep._default_engine_active",
                        return_value=4321), \
             mock.patch("buo.oc.gpu_sweep._make_optimizer",
                        return_value=fake):
            res = self.invoke("--freqs", "1500")
        self.assertEqual(res.exit_code, 1)
        self.assertIn("REFUSE", res.output)
        self.assertFalse((self.oc / gpu_sweep.ESITO_FILE).exists())
        self.assertEqual(fake.calls, [],
                         "l'ottimizzatore non deve mai girare con engine attiva")

    def test_invalid_freqs_are_rejected(self):
        res = self.invoke("--freqs", "abc,1500")
        self.assertEqual(res.exit_code, 2)  # BadParameter di click

    def test_freqs_outside_gpu_steps_are_rejected(self):
        # Coerenza con GPU_FREQ_STEPS (regola config._sweep_freqs): mai
        # frequenze fuori scala al probe in silenzio.
        res = self.invoke("--freqs", "1150,1200")
        self.assertEqual(res.exit_code, 2)
        self.assertIn("GPU_FREQ_STEPS", res.output)

    def test_freqs_are_sorted_and_deduplicated(self):
        fake = FakeOptimizer()
        with self._patches(fake):
            res = self.invoke("--freqs", "2000,1200,2000,1500")
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertEqual(fake.calls[0]["sweep"]["freqs"],
                         [1200, 1500, 2000])

    def test_community_under_floor_is_clamped(self):
        # mai safe_points < floor in output (difensivo del wrapper).
        fake = FakeOptimizer(result=_fake_result(
            source="community_defaults",
            safe_points=[{"freq": 1200, "voltage": 750}]))
        with self._patches(fake):
            res = self.invoke("--freqs", "1200", "--floor-mv", "800")
        self.assertEqual(res.exit_code, 0, res.output)
        esito = json.loads(
            (self.oc / gpu_sweep.ESITO_FILE).read_text(encoding="utf-8"))
        self.assertTrue(all(p["voltage"] >= 800
                            for p in esito["safe_points"]))
        self.assertTrue(esito["clamped_to_floor"])


class SweepCrashRecoveryCase(unittest.TestCase):
    """Crash-point T4a via run() diretto (boot_epoch iniettato, mai
    hardware)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.oc = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _seed(self, in_probe):
        (self.oc / gpu_sweep.ESITO_FILE).write_text(
            json.dumps({"in_probe": in_probe}), encoding="utf-8")

    def test_reboot_during_probe_marks_crash_and_skips_point(self):
        # boot_epoch cambiato (reboot durante il probe) → punto marcato
        # 'crash' in tested[], MAI riprovato (freq esclusa), sweep continua.
        self._seed({"freq": 1500, "voltage": 875, "started_epoch": 1000})
        fake = FakeOptimizer()
        esito = gpu_sweep.run(
            oc_dir=self.oc, freqs=[1200, 1500, 2000], optimizer=fake,
            engine_active=lambda: None, boot_epoch=lambda: 5000,
            fingerprint="fp")
        crashes = [t for t in esito["tested"] if t["status"] == "crash"]
        self.assertEqual(len(crashes), 1)
        self.assertEqual(crashes[0]["f"], 1500)
        self.assertEqual(crashes[0]["v"], 875)
        self.assertEqual(crashes[0]["rc"], 1)
        self.assertEqual(fake.calls[0]["sweep"]["freqs"], [1200, 2000],
                         "freq crashatata mai riprovata, sweep continua")
        self.assertIsNone(esito["in_probe"], "marcatore ripulito a fine run")

    def test_same_boot_resumes_point(self):
        # Stesso boot_epoch (Ctrl-C, nessun reboot) → punto RIPRESO: nessun
        # crash, frequenze invariate.
        self._seed({"freq": 1500, "voltage": 875, "started_epoch": 1000})
        fake = FakeOptimizer()
        esito = gpu_sweep.run(
            oc_dir=self.oc, freqs=[1200, 1500, 2000], optimizer=fake,
            engine_active=lambda: None, boot_epoch=lambda: 1000,
            fingerprint="fp")
        self.assertEqual(fake.calls[0]["sweep"]["freqs"],
                         [1200, 1500, 2000])
        self.assertFalse([t for t in esito["tested"]
                          if t["status"] == "crash"])
        self.assertIsNone(esito["in_probe"])

    def test_floor_clamp_defensive(self):
        # Punti sotto il floor (ottimizzatore anomalo) → clamp in uscita.
        fake = FakeOptimizer(result=_fake_result(
            source="per-silicon",
            safe_points=[{"freq": 1200, "voltage": 750}]))
        esito = gpu_sweep.run(
            oc_dir=self.oc, freqs=[1200], floor_mv=800, optimizer=fake,
            engine_active=lambda: None, boot_epoch=lambda: 1,
            fingerprint="fp")
        self.assertEqual(esito["safe_points"][0]["voltage"], 800)
        self.assertEqual(esito["winner"],
                         {"freq": 1200, "voltage": 800})
        self.assertTrue(esito["clamped_to_floor"])

    def test_engine_active_refuses_direct(self):
        with self.assertRaisesRegex(RuntimeError, "REFUSE"):
            gpu_sweep.run(oc_dir=self.oc, freqs=[1500],
                          engine_active=lambda: 4321)

    def test_clamped_flag_from_sweep_meta(self):
        # gpu.py segna clamped_to_floor nel SUO sweep_meta (non top-level):
        # il wrapper lo eredita nell'esito (fix reviewer #1).
        result = _fake_result()
        result["sweep"]["clamped_to_floor"] = True
        fake = FakeOptimizer(result=result)
        esito = gpu_sweep.run(
            oc_dir=self.oc, freqs=[1200, 1500], optimizer=fake,
            engine_active=lambda: None, boot_epoch=lambda: 1,
            fingerprint="fp")
        self.assertTrue(esito["clamped_to_floor"])

    def test_out_of_range_floor_and_step_are_clamped(self):
        # Clamp come config.py: floor [700, 1100] (LIMITS GPU), step
        # [10, 50] multiplo di 5; il raise difensivo resta entro
        # voltage_recommended_max (1050) — mai punti sopra il tetto.
        fake = FakeOptimizer(result=_fake_result(
            source="per-silicon",
            safe_points=[{"freq": 1200, "voltage": 750}]))
        esito = gpu_sweep.run(
            oc_dir=self.oc, freqs=[1200], floor_mv=1500, step_mv=60,
            optimizer=fake, engine_active=lambda: None,
            boot_epoch=lambda: 1, fingerprint="fp")
        self.assertEqual(esito["floor_mv"], 1100)
        self.assertEqual(fake.calls[0]["sweep"]["step_mv"], 50)
        self.assertEqual(esito["safe_points"][0]["voltage"], 1050)

    def test_floor_below_minimum_is_clamped_up(self):
        fake = FakeOptimizer(result=_fake_result(
            source="per-silicon",
            safe_points=[{"freq": 1200, "voltage": 750}]))
        esito = gpu_sweep.run(
            oc_dir=self.oc, freqs=[1200], floor_mv=400, step_mv=3,
            optimizer=fake, engine_active=lambda: None,
            boot_epoch=lambda: 1, fingerprint="fp")
        self.assertEqual(esito["floor_mv"], 700)
        # clamp prima a [10, 50] (3 → 10), poi multiplo di 5 — come config
        self.assertEqual(fake.calls[0]["sweep"]["step_mv"], 10)


if __name__ == "__main__":
    unittest.main()
