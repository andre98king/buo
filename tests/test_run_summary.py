#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test del riepilogo finale di run (spec UX_REVAMP_CLI_SPEC §3).

`Orchestrator.riepilogo_lines()` è la fonte unica del blocco "Riepilogo
finale" (log in `_finalize` + pannello CLI): funzione pura su
results/checkpoint sintetici — mai hardware reale, mai I/O.
"""

import os
import tempfile
import unittest

from buo.config import BUOConfig
from buo.orchestrator import Orchestrator


def _seed_full_run(orch):
    """results + checkpoint di una run completa (dati sintetici onesti)."""
    orch.results["after"] = {"gpu": {"cu_count": 40}}
    orch.results["fix_summary"] = {
        "applied": ["cpu_core_unlock", "gpu_40cu", "tlb_fix", "fan_control",
                    "iommu", "gtt_tuning"],
        "manual": [], "failed": [],
    }
    orch.checkpoint.set(
        "applied_steps",
        ["cpu_core_unlock", "gpu_40cu", "tlb_fix", "fan_control"])
    orch.checkpoint.set_phase("apply", {
        "cpu_final": {"freq": 3825, "scale": -26, "vid": 1125,
                      "persistent": True},
        "governor_config": True,
    }, completed=True)
    orch.checkpoint.set_phase("optimize", {
        "undervolt_gpu": {"safe_points": [
            {"freq": 1000, "voltage": 800},
            {"freq": 1500, "voltage": 800},
            {"freq": 1800, "voltage": 800}]},
    }, completed=True)
    orch.checkpoint.set_phase("validate", {
        "stress": {"passed": True, "duration_minutes": 3,
                   "cpu_temp_max": 77.0, "gpu_temp_max": 75.0,
                   "power_max": 118.0},
    }, completed=True)


class TestRiepilogoLines(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name
        os.environ["BUO_DEPS_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        os.environ.pop("BUO_DEPS_DIR", None)
        self._tmp.cleanup()

    def _make(self, mock=True, dry_run=False):
        """Orchestratore isolato (stato in tmp). mock=True → nessun
        modulo reale; per il ramo NON simulato si spegne mock a run
        costruita (riepilogo_lines non tocca i moduli hardware)."""
        from buo.utils.mock import MockHardware
        orch = Orchestrator(config=BUOConfig(), mock=mock, dry_run=dry_run,
                            mock_hardware=MockHardware(seed=1))
        orch.checkpoint.clear()
        return orch

    def test_contains_header_rollback_readable_fix_and_persistito(self):
        """§3.3: con results sintetici → 'Riepilogo finale', riga rollback,
        'persistito: sì' e il nome leggibile di un fix."""
        orch = self._make(mock=False, dry_run=False)
        _seed_full_run(orch)
        lines = orch.riepilogo_lines()
        joined = "\n".join(lines)
        self.assertEqual(lines[0], "Riepilogo finale")
        self.assertIn("rollback: sudo buo rollback", joined)
        self.assertIn("persistito: sì", joined)
        self.assertIn("8 core", joined)          # cpu_core_unlock leggibile
        self.assertIn("fix TLB", joined)         # tlb_fix leggibile
        self.assertIn("fix applicati in questa run: 4", joined)
        self.assertIn("già attivi (verificati, nessuna modifica): 2",
                      joined)

    def test_cpu_gpu_40cu_stress_rows(self):
        orch = self._make(mock=False, dry_run=False)
        _seed_full_run(orch)
        joined = "\n".join(orch.riepilogo_lines())
        self.assertIn("CPU: 3825 MHz · scale -26 · VID 1125 mV · "
                      "persistito: sì", joined)
        self.assertIn("GPU: curva 1000-1800 MHz · 3 punti · persistito: sì",
                      joined)
        self.assertIn("40-CU: attive", joined)
        self.assertIn("stress: superato · 3 minuti · picchi CPU 77°C / "
                      "GPU 75°C / 118 W", joined)
        self.assertIn("report: ", joined)

    def test_attenzione_manuale_row(self):
        orch = self._make(mock=False, dry_run=False)
        orch.checkpoint.set("applied_steps", ["cpu_core_unlock"])
        orch.results["after"] = {"gpu": {"cu_count": 40}}
        orch.results["fix_summary"] = {
            "applied": ["cpu_core_unlock"],
            "manual": ["vram_config"], "failed": ["ace_fix"],
        }
        joined = "\n".join(orch.riepilogo_lines())
        self.assertIn("attenzione manuale: 2 — config VRAM (manuale), "
                      "fix ACE (fallito) — dettagli nel report", joined)

    def test_empty_ledger_shows_zero(self):
        """§3.3: ledger vuoto (run reale) → 'fix applicati in questa
        run: 0' e NESSUNA riga 'già attivi'."""
        orch = self._make(mock=False, dry_run=False)
        orch.results["after"] = {}
        joined = "\n".join(orch.riepilogo_lines())
        self.assertIn("fix applicati in questa run: 0", joined)
        self.assertNotIn("già attivi", joined)
        self.assertNotIn("attenzione manuale", joined)
        self.assertIn("rollback: sudo buo rollback", joined)

    def test_mock_shows_simulazione_row(self):
        """§3.2: in mock/dry-run la riga fix applicati è
        '0 — (simulazione)' (C1: nulla di reale è stato applicato)."""
        orch = self._make(mock=True, dry_run=False)
        _seed_full_run(orch)
        joined = "\n".join(orch.riepilogo_lines())
        self.assertIn("fix applicati in questa run: 0 — (simulazione)",
                      joined)
        self.assertNotIn("8 core", joined)

    def test_dry_run_adds_dry_run_row(self):
        orch = self._make(mock=True, dry_run=True)
        orch.results["after"] = {}
        joined = "\n".join(orch.riepilogo_lines())
        self.assertIn("MODALITÀ DRY-RUN: nessuna modifica reale — "
                      "report .dry-run", joined)

    def test_rows_omitted_without_data(self):
        """C1: campi assenti → righe omesse o 'non rilevabile' (mai
        valori inventati)."""
        orch = self._make(mock=False, dry_run=False)
        orch.results["after"] = {}
        joined = "\n".join(orch.riepilogo_lines())
        self.assertNotIn("CPU:", joined)
        self.assertNotIn("GPU:", joined)
        self.assertNotIn("stress:", joined)
        self.assertIn("40-CU: non rilevabile", joined)


class TestFinalizeLogsRiepilogo(unittest.TestCase):
    """§3.2: _finalize logga il blocco dopo OTTIMIZZAZIONE COMPLETATA."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name
        os.environ["BUO_DEPS_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        os.environ.pop("BUO_DEPS_DIR", None)
        self._tmp.cleanup()

    def _make(self):
        from buo.utils.mock import MockHardware
        hw = MockHardware(seed=42)
        hw.state.is_acpi_fixed = True
        cfg = BUOConfig()
        cfg.validation_stress_duration = 0
        cfg.benchmark_enabled = False
        orch = Orchestrator(config=cfg, mock=True, dry_run=False,
                            mock_hardware=hw)
        orch.checkpoint.clear()
        return orch

    def test_full_mock_run_logs_riepilogo_block(self):
        orch = self._make()
        with self.assertLogs("buo.Orchestrator", level="INFO") as logs:
            rc = orch.run()
        self.assertEqual(rc, 0)
        joined = "\n".join(logs.output)
        self.assertIn("OTTIMIZZAZIONE COMPLETATA", joined)
        self.assertIn("Riepilogo finale", joined)
        self.assertIn("rollback: sudo buo rollback", joined)


if __name__ == "__main__":
    unittest.main()
