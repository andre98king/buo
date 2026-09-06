#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T3 — menu interattivo a 3 opzioni della fase optimize (design
research/DESIGN_UNLEASH_OC_BOUNDARY.md §5):

- interactive (run reale) → prompt a 3 opzioni: [1] riuso (default),
  [2] base sicura senza sweep, [3] ottimizzazione completa; input
  invalido/vuoto → [1]; [3] con profilo certificato → conferma esplicita
  di sovrascrittura, rifiutata → [1];
- non interactive o dry-run → NESSUN prompt: decisione di stato INVARIATA
  (T1): profilo certificato → riuso, altrimenti percorso completo.

Sempre mock (mai hardware reale) e nessuna attesa da terminale: input
patchato. I rami chiamano il codice ESISTENTE — i test osservano la
decisione del selettore (mode reuse/safe_base/full), non l'hardware.
"""

import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from buo.config import BUOConfig
from buo.orchestrator import Orchestrator
from buo.oc.profiles import machine_silicon_fingerprint
from buo.utils.mock import MockHardware


def _uv_cpu_result():
    return {"v_f_points": [{"freq": 3500, "vid": 999}],
            "best_efficiency": {"freq": 3500, "vid": 999},
            "source": "mock"}


def _uv_gpu_result():
    return {"safe_points": [{"freq": 1500, "voltage": 900}],
            "best_efficiency": {"freq": 1500, "voltage": 900},
            "source": "mock"}


class OptimizePromptBase(unittest.TestCase):
    def setUp(self):
        self._oc_tmp = tempfile.TemporaryDirectory()
        self._state_tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._state_tmp.name
        self.oc = Path(self._oc_tmp.name)

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._oc_tmp.cleanup()
        self._state_tmp.cleanup()

    def _write_certified(self, winner_freq=3825, winner_vid=1125, scale=-26,
                         fp=None):
        """silicon-profile.json coerente con la fingerprint simulata
        (fp esplicita per simulare uno stato INVALIDO)."""
        fp = fp or machine_silicon_fingerprint(sim=True)
        (self.oc / "silicon-profile.json").write_text(json.dumps({
            "schema_version": 1,
            "hardware_fingerprint": fp,
            "updated_at": "2026-09-07T00:00:00Z",
            "curve": {str(winner_freq): {
                "vid_cap": winner_vid, "scale": scale,
                "l2_validated": True, "validated": True}},
            "winner": {"freq": winner_freq, "vid_cap": winner_vid,
                       "scale": scale, "persisted": True},
        }), encoding="utf-8")

    def _orch(self, interactive=True, dry_run=False):
        hw = MockHardware(seed=42)
        hw.state.is_acpi_fixed = True
        cfg = BUOConfig()
        cfg.validation_stress_duration = 0
        cfg.benchmark_enabled = False
        orch = Orchestrator(config=cfg, mock=True, dry_run=dry_run,
                            interactive=interactive, mock_hardware=hw,
                            oc_dir=self.oc)
        orch.checkpoint.clear()
        return orch

    def _run_full_patched(self, orch):
        """Esegue il percorso sweep+ricerca (mode full/safe_base) con le
        ricerche simulate: ritorna (data, spy_gpu, spy_oc_cpu)."""
        p_cpu = mock.patch.object(orch.uv_cpu, "optimize",
                                  return_value=_uv_cpu_result())
        p_gpu = mock.patch.object(orch.uv_gpu, "optimize",
                                  return_value=_uv_gpu_result())
        p_oc_cpu = mock.patch.object(orch.oc, "optimize_cpu",
                                     return_value={})
        p_oc_gpu = mock.patch.object(orch.oc, "optimize_gpu",
                                     return_value={})
        with p_cpu, p_gpu as spy_gpu, p_oc_cpu as spy_oc_cpu, p_oc_gpu:
            data = orch._phase_optimize()
        return data, spy_gpu, spy_oc_cpu


class TestInteractiveMenu(OptimizePromptBase):
    """Prompt a 3 opzioni nei run interattivi (mock, input patchato)."""

    def test_choice_1_reuses_certified_profile(self):
        self._write_certified()
        orch = self._orch()
        with mock.patch("builtins.input", return_value="1") as spy:
            data = orch._phase_optimize()
        self.assertIn("reuse_oc", data)
        self.assertEqual(data["reuse_oc"]["freq"], 3825)
        self.assertNotIn("undervolt_cpu", data)  # ricerca saltata
        self.assertNotIn("undervolt_gpu", data)
        spy.assert_called_once()  # solo il menu, nessuna conferma

    def test_invalid_or_empty_input_defaults_to_choice_1(self):
        self._write_certified()
        for bad in ("", "x", "0", "4", "si", "\n"):
            orch = self._orch()
            with mock.patch("builtins.input", return_value=bad):
                data = orch._phase_optimize()
            self.assertIn("reuse_oc", data,
                          "input %r deve cadere sul default [1]" % bad)
            self.assertNotIn("undervolt_cpu", data)

    def test_choice_2_safe_base_skips_sweep_and_oc(self):
        orch = self._orch()
        orch.config.undervolt_gpu_sweep_enabled = True
        orch.config.overclock_enable = True
        with mock.patch("builtins.input", return_value="2"):
            data, spy_gpu, spy_oc_cpu = self._run_full_patched(orch)
        self.assertNotIn("reuse_oc", data)
        self.assertIn("undervolt_cpu", data)  # UV CPU stock resta
        sweep = spy_gpu.call_args.kwargs["sweep"]
        self.assertFalse(sweep["enabled"], "base sicura = sweep disabilitato")
        spy_oc_cpu.assert_not_called()  # niente OC
        self.assertNotIn("overclock_cpu", data)

    def test_choice_3_full_without_certificate(self):
        orch = self._orch()  # oc_dir vuoto: nessun profilo certificato
        orch.config.undervolt_gpu_sweep_enabled = True
        orch.config.overclock_enable = True
        with mock.patch("builtins.input", return_value="3"):
            data, spy_gpu, spy_oc_cpu = self._run_full_patched(orch)
        self.assertNotIn("reuse_oc", data)
        self.assertIn("undervolt_cpu", data)
        sweep = spy_gpu.call_args.kwargs["sweep"]
        self.assertTrue(sweep["enabled"])
        spy_oc_cpu.assert_called_once()

    def test_choice_3_with_certified_asks_confirm_and_refusal_reuses(self):
        self._write_certified()
        orch = self._orch()
        with mock.patch("builtins.input", side_effect=["3", "n"]) as spy:
            data = orch._phase_optimize()
        self.assertIn("reuse_oc", data)
        self.assertNotIn("undervolt_cpu", data)
        self.assertEqual(spy.call_count, 2)  # menu + conferma sovrascrittura

    def test_choice_3_with_certified_confirm_accepted_runs_full(self):
        self._write_certified()
        orch = self._orch()
        orch.config.undervolt_gpu_sweep_enabled = True
        orch.config.overclock_enable = True
        with mock.patch("builtins.input", side_effect=["3", "y"]):
            data, spy_gpu, spy_oc_cpu = self._run_full_patched(orch)
        self.assertNotIn("reuse_oc", data)
        self.assertIn("undervolt_cpu", data)
        self.assertTrue(spy_gpu.call_args.kwargs["sweep"]["enabled"])
        spy_oc_cpu.assert_called_once()

    def test_choice_1_without_certificate_falls_back_to_state_decision(self):
        # [1] = "applica profilo certificato SE PRESENTE": senza stato
        # certificato coincide con la decisione di stato (full), mai hang.
        orch = self._orch()
        orch.config.undervolt_gpu_sweep_enabled = True
        orch.config.overclock_enable = True
        with mock.patch("builtins.input", return_value="1"):
            data, spy_gpu, _ = self._run_full_patched(orch)
        self.assertNotIn("reuse_oc", data)
        self.assertIn("undervolt_cpu", data)
        self.assertTrue(spy_gpu.call_args.kwargs["sweep"]["enabled"])

    def test_menu_eof_defaults_to_choice_1(self):
        # Terminale chiuso sul menu (EOFError, pattern _confirm_phase):
        # nessuna attesa, default [1] → riuso del profilo certificato.
        self._write_certified()
        orch = self._orch()
        with mock.patch("builtins.input", side_effect=EOFError):
            data = orch._phase_optimize()
        self.assertIn("reuse_oc", data)
        self.assertNotIn("undervolt_cpu", data)

    def test_choice_3_confirm_eof_refuses_and_reuses(self):
        # EOFError sulla conferma di sovrascrittura → rifiuto → [1] riuso.
        self._write_certified()
        orch = self._orch()
        with mock.patch("builtins.input", side_effect=["3", EOFError]):
            data = orch._phase_optimize()
        self.assertIn("reuse_oc", data)
        self.assertNotIn("undervolt_cpu", data)

    def test_choice_1_with_invalid_certified_state_falls_back_full(self):
        # Stato presente ma NON applicabile (fingerprint diversa dalla
        # macchina) → candidate None → [1] = decisione di stato (full),
        # mai errore né riuso di uno stato non valido.
        self._write_certified(fp="f" * 64)
        orch = self._orch()
        orch.config.undervolt_gpu_sweep_enabled = True
        orch.config.overclock_enable = True
        with mock.patch("builtins.input", return_value="1"):
            data, spy_gpu, _ = self._run_full_patched(orch)
        self.assertNotIn("reuse_oc", data)
        self.assertIn("undervolt_cpu", data)
        self.assertTrue(spy_gpu.call_args.kwargs["sweep"]["enabled"])


class TestNonInteractive(OptimizePromptBase):
    """Nessun prompt: mode = decisione di stato (identica a prima di T3)."""

    def test_with_certified_profile_reuses_without_prompt(self):
        self._write_certified()
        orch = self._orch(interactive=False)
        with mock.patch("builtins.input") as spy:
            data = orch._phase_optimize()
        spy.assert_not_called()
        self.assertIn("reuse_oc", data)

    def test_without_state_runs_full_without_prompt(self):
        orch = self._orch(interactive=False)
        orch.config.undervolt_gpu_sweep_enabled = True
        orch.config.overclock_enable = True
        with mock.patch("builtins.input") as spy:
            data, spy_gpu, spy_oc_cpu = self._run_full_patched(orch)
        spy.assert_not_called()
        self.assertNotIn("reuse_oc", data)
        self.assertIn("undervolt_cpu", data)
        self.assertTrue(spy_gpu.call_args.kwargs["sweep"]["enabled"])
        spy_oc_cpu.assert_called_once()

    def test_dry_run_never_prompts_even_if_interactive(self):
        # Il menu richiede una run REALE: dry-run/interactive resta sulla
        # decisione di stato (dry-run: mai riuso → percorso completo).
        self._write_certified()
        orch = self._orch(interactive=True, dry_run=True)
        orch.config.undervolt_gpu_sweep_enabled = True
        orch.config.overclock_enable = True
        with mock.patch("builtins.input") as spy:
            data, _, _ = self._run_full_patched(orch)
        spy.assert_not_called()
        self.assertNotIn("reuse_oc", data)
        self.assertIn("undervolt_cpu", data)


if __name__ == "__main__":
    unittest.main()
