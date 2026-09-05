#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Health test CU "smart" (design DESIGN_PORTABILITY_DEFAULTS 3.4):
riuso dei results.tsv COMPLETI; niente maratona per-WGP (~20 reboot)
da una run non presidiata. Verificato sul campo (03/09): `quick` dello
script testa SOLO la config corrente senza isolare le WGP → non
sostituisce il protocollo; il primo-unlock-interattivo resta da
validare (non implementato alla cieca)."""

import os
import tempfile
import unittest

from buo.config import BUOConfig
from buo.orchestrator import Orchestrator
from buo.unlock.wrappers.bc250_health import (BC250HealthWrapper,
                                              HEALTH_WGP_TOTAL)
from buo.utils.mock import MockHardware


def _tsv_rows(n: int) -> str:
    """results.tsv con `n` WGP testate (formato reale dello script:
    target, se, sh, wgp, status, rc, active_cu, started, finished)."""
    lines = ["#idx\tse\tsh\twgp\tstatus\trc\tactive_cu\tstarted\tfinished"]
    for i in range(n):
        se, sh, wgp = i // 10, (i // 5) % 2, i % 5
        lines.append(f"{i}\t{se}\t{sh}\t{wgp}\tPASS\t0\t40\tstart\tend")
    return "\n".join(lines) + "\n"


class TestHealthResultsCompleteness(unittest.TestCase):
    """Il riuso decide sulla COMPLETEZZA di results.tsv: una riga per
    ognuna delle 20 WGP (target 0..19, una per reboot)."""

    def test_complete_file_is_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/results.tsv"
            with open(path, "w") as f:
                f.write(_tsv_rows(HEALTH_WGP_TOTAL))
            res = BC250HealthWrapper().read_results(results_file=path)
        self.assertTrue(res["complete"])
        self.assertEqual(res["total"], HEALTH_WGP_TOTAL)
        self.assertEqual(res["defective"], [])
        # present/rows (design POSTUNLOCK_VALIDATION D8): assente/parziale/
        # completo distinguibili senza toccare la semantica complete
        self.assertTrue(res["present"])
        self.assertEqual(res["rows"], HEALTH_WGP_TOTAL)

    def test_partial_file_not_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/results.tsv"
            with open(path, "w") as f:
                f.write(_tsv_rows(3))
            res = BC250HealthWrapper().read_results(results_file=path)
        self.assertFalse(res["complete"])
        self.assertEqual(res["total"], 3)
        self.assertTrue(res["present"])
        self.assertEqual(res["rows"], 3)

    def test_missing_file_not_complete(self):
        res = BC250HealthWrapper().read_results(
            results_file="/nonexistent/results.tsv")
        self.assertFalse(res["complete"])
        self.assertIn("error", res)
        self.assertFalse(res["present"])
        self.assertEqual(res["rows"], 0)


class _FakeHealth:
    """Stub di CUHealthTest: read_results programmabile; run() = la
    maratona per-WGP non deve MAI partire (AssertionError se chiamata)."""

    def __init__(self, payload):
        self.payload = payload

    def read_results(self):
        return self.payload

    def run(self):
        raise AssertionError("health run() avviata: la maratona per-WGP "
                             "non deve partire da una run non presidiata")


class TestHealthSmartUnlock(unittest.TestCase):
    """Decisione della fase unlock con health_test=true (default)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def _make(self, health_payload, interactive=False):
        hw = MockHardware(seed=42)
        hw.state.is_acpi_fixed = True
        cfg = BUOConfig()
        cfg.probe_cpu_unlock = False   # isola il passo health
        cfg.probe_gpu_unlock = False
        cfg.validation_stress_duration = 0
        orch = Orchestrator(config=cfg, mock=True, dry_run=True,
                            mock_hardware=hw, interactive=interactive)
        orch.health_test = _FakeHealth(health_payload)
        orch.checkpoint.clear()
        return orch

    def test_complete_results_reused_without_run(self):
        """results.tsv completo → riuso: nessun run (maratona), nessuna
        nota di skip, i risultati finiscono nel phase data."""
        orch = self._make({"stable": list(range(40)), "defective": [],
                           "total": HEALTH_WGP_TOTAL, "complete": True})
        out = orch._phase_unlock()
        self.assertTrue(out["health"]["complete"])
        self.assertFalse(any("health test saltato" in n.lower()
                             for n in orch.results["notes"]))

    def test_incomplete_results_skip_non_interactive_with_note(self):
        """results.tsv assente/incompleto in run NON interattiva → skip
        con nota/ricetta (mai la maratona per-WGP da una run non
        presidiata)."""
        orch = self._make({"stable": [], "defective": [],
                           "total": 0, "complete": False})
        out = orch._phase_unlock()
        self.assertFalse(out["health"]["complete"])
        self.assertTrue(any("health test saltato" in n.lower()
                            for n in orch.results["notes"]),
                        "la nota di skip deve essere nel report")


if __name__ == "__main__":
    unittest.main()
