#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T3+T4 — selezione della modalità di ottimizzazione della fase optimize
(design research/DESIGN_UNLEASH_OC_BOUNDARY.md §5 + T4 D1/D2 in
research/DESIGN_T4_SWEEP_OC.md):

- interactive (run reale) → menu a 2 voci (T4 D2, [3] "full" RIMOSSO):
  [1] riuso (default), [2] base sicura; input invalido/vuoto → [1];
- non interactive o dry-run → NESSUN prompt: decisione di stato (T4 D1):
  profilo certificato → riuso, altrimenti BASE SICURA (mai full — lo
  sweep GPU per-silicio è delegato a `buo oc sweep-gpu`).

Sempre mock (mai hardware reale) e nessuna attesa da terminale: input
patchato. I rami chiamano il codice ESISTENTE — i test osservano la
decisione del selettore (mode reuse/safe_base), non l'hardware.
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
        """Esegue il percorso base-sicura (mode safe_base) con le ricerche
        simulate: ritorna (data, spy_gpu, spy_oc_cpu)."""
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

    def _assert_safe_base(self, data, spy_gpu, spy_oc_cpu, orch):
        """Base sicura: UV CPU stock, NESSUN parametro sweep passato
        all'ottimizzatore (community GPU), niente OC."""
        self.assertNotIn("reuse_oc", data)
        self.assertIn("undervolt_cpu", data)
        self.assertNotIn("sweep", spy_gpu.call_args.kwargs,
                         "lo sweep è delegato: unleash non lo passa mai")
        spy_oc_cpu.assert_not_called()  # niente OC
        self.assertNotIn("overclock_cpu", data)
        self.assertTrue(
            any("buo oc sweep-gpu" in n for n in orch.results["notes"]),
            "nota di delega dello sweep attesa in results.notes")


class TestInteractiveMenu(OptimizePromptBase):
    """Menu a 2 voci nei run interattivi (mock, input patchato)."""

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

    def test_menu_has_two_choices_and_delegation_note(self):
        # T4 D2: [3] "full" rimosso; il menu reca la delega dello sweep.
        self._write_certified()
        orch = self._orch()
        menu_text = []
        def fake_input(prompt=""):
            menu_text.append(prompt)
            return "1"
        with mock.patch("builtins.input", side_effect=fake_input):
            orch._phase_optimize()
        text = menu_text[0] if menu_text else ""
        self.assertNotIn("[3]", text)
        self.assertIn("[1]", text)
        self.assertIn("[2]", text)
        self.assertIn("buo oc sweep-gpu", text)

    def test_invalid_or_empty_input_defaults_to_choice_1(self):
        self._write_certified()
        for bad in ("", "x", "0", "4", "si", "\n", "3"):
            orch = self._orch()
            with mock.patch("builtins.input", return_value=bad):
                data = orch._phase_optimize()
            self.assertIn("reuse_oc", data,
                          "input %r deve cadere sul default [1]" % bad)
            self.assertNotIn("undervolt_cpu", data)

    def test_choice_1_without_certificate_uses_safe_base(self):
        # [1] = "applica profilo certificato SE PRESENTE": senza stato
        # certificato coincide con la decisione di stato (T4 D1: base
        # sicura, MAI full/sweep), mai hang.
        orch = self._orch()
        orch.config.overclock_enable = True
        with mock.patch("builtins.input", return_value="1"):
            data, spy_gpu, spy_oc_cpu = self._run_full_patched(orch)
        self._assert_safe_base(data, spy_gpu, spy_oc_cpu, orch)

    def test_choice_1_with_invalid_certified_state_uses_safe_base(self):
        # Stato presente ma NON applicabile (fingerprint diversa dalla
        # macchina) → candidate None → [1] = decisione di stato (base
        # sicura), mai errore né riuso di uno stato non valido.
        self._write_certified(fp="f" * 64)
        orch = self._orch()
        orch.config.overclock_enable = True
        with mock.patch("builtins.input", return_value="1"):
            data, spy_gpu, spy_oc_cpu = self._run_full_patched(orch)
        self._assert_safe_base(data, spy_gpu, spy_oc_cpu, orch)

    def test_choice_2_safe_base_skips_sweep_and_oc(self):
        orch = self._orch()
        orch.config.overclock_enable = True
        with mock.patch("builtins.input", return_value="2"):
            data, spy_gpu, spy_oc_cpu = self._run_full_patched(orch)
        self._assert_safe_base(data, spy_gpu, spy_oc_cpu, orch)

    def test_menu_eof_defaults_to_choice_1_reuse(self):
        # Terminale chiuso sul menu (EOFError, pattern _confirm_phase):
        # nessuna attesa, default [1] → riuso del profilo certificato.
        self._write_certified()
        orch = self._orch()
        with mock.patch("builtins.input", side_effect=EOFError):
            data = orch._phase_optimize()
        self.assertIn("reuse_oc", data)
        self.assertNotIn("undervolt_cpu", data)

    def test_menu_eof_without_certificate_uses_safe_base(self):
        orch = self._orch()  # oc_dir vuoto: nessun profilo certificato
        orch.config.overclock_enable = True
        with mock.patch("builtins.input", side_effect=EOFError):
            data, spy_gpu, spy_oc_cpu = self._run_full_patched(orch)
        self._assert_safe_base(data, spy_gpu, spy_oc_cpu, orch)


class TestNonInteractive(OptimizePromptBase):
    """Nessun prompt: mode = decisione di stato (T4 D1: mai full)."""

    def test_with_certified_profile_reuses_without_prompt(self):
        self._write_certified()
        orch = self._orch(interactive=False)
        with mock.patch("builtins.input") as spy:
            data = orch._phase_optimize()
        spy.assert_not_called()
        self.assertIn("reuse_oc", data)

    def test_without_state_uses_safe_base_without_prompt(self):
        orch = self._orch(interactive=False)
        orch.config.overclock_enable = True
        with mock.patch("builtins.input") as spy:
            data, spy_gpu, spy_oc_cpu = self._run_full_patched(orch)
        spy.assert_not_called()
        self._assert_safe_base(data, spy_gpu, spy_oc_cpu, orch)

    def test_dry_run_never_prompts_even_if_interactive(self):
        # Il menu richiede una run REALE: dry-run/interactive resta sulla
        # decisione di stato (dry-run: mai riuso → base sicura, mai full).
        self._write_certified()
        orch = self._orch(interactive=True, dry_run=True)
        orch.config.overclock_enable = True
        with mock.patch("builtins.input") as spy:
            data, spy_gpu, spy_oc_cpu = self._run_full_patched(orch)
        spy.assert_not_called()
        self._assert_safe_base(data, spy_gpu, spy_oc_cpu, orch)


if __name__ == "__main__":
    unittest.main()
