#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test dei gate di sicurezza: fail-closed, preflight e dry-run.

Verifica le garanzie fondamentali richieste dal progetto:
    "prima analizza tutto, testa tutto e solo dopo fa le modifiche"
    1. In modalità reale, se un test non è possibile → RIFIUTO (mai
       valori inventati)
    2. Il preflight blocca con kernel/Mesa non a norma
    3. In dry-run NESSUN modulo può toccare l'hardware
"""

import os
import tempfile
import unittest

from buo.config import BUOConfig
from buo.exceptions import ConfigurationError, SafetyViolation
from buo.optimize.cpu import CPUUndervoltOptimizer
from buo.optimize.gpu import GPUUndervoltOptimizer
from buo.orchestrator import Orchestrator


class TestFailClosed(unittest.TestCase):
    """Niente test = niente modifiche (principio fail-closed)."""

    def test_cpu_undervolt_refuses_without_bc250_detect(self):
        """Senza bc250-detect l'undervolt CPU reale DEVE rifiutarsi."""
        opt = CPUUndervoltOptimizer(mock=False, use_wrapper=False)
        with self.assertRaises(ConfigurationError):
            opt.optimize()

    def test_cpu_undervolt_refuses_when_wrapper_missing(self):
        """Wrapper assente (non installato) → ConfigurationError."""
        opt = CPUUndervoltOptimizer(mock=False, use_wrapper=True)
        if opt.detect_wrapper.available:
            self.skipTest("bc250-detect presente su questa macchina")
        with self.assertRaises(ConfigurationError):
            opt.optimize()

    def test_gpu_undervolt_uses_community_verified_table(self):
        """GPU reale: tabella community-verified, mai oltre i limiti."""
        opt = GPUUndervoltOptimizer(mock=False)
        result = opt.optimize()
        self.assertEqual(result["source"], "community_defaults")
        self.assertTrue(result["safe_points"])
        for point in result["safe_points"]:
            self.assertLessEqual(point["voltage"], 1050)  # recommended max

    def test_community_table_flat_1000mv_top(self):
        """La tabella community NON deve scendere sotto 1000mV a 2000+ MHz
        (bug #17: il vecchio 960mV crashava la GPU sotto stress)."""
        for p in GPUUndervoltOptimizer.COMMUNITY_SAFE_POINTS:
            if p["freq"] >= 2000:
                self.assertGreaterEqual(
                    p["voltage"], 1000,
                    f"2000+ MHz a {p['voltage']}mV è troppo aggressivo")

    def test_gpu_table_clamped_to_max_voltage(self):
        opt = GPUUndervoltOptimizer(mock=False)
        result = opt.optimize(max_voltage=900)
        for point in result["safe_points"]:
            self.assertLessEqual(point["voltage"], 900)


class TestPreflight(unittest.TestCase):
    """Verifica di sanità prima di qualsiasi modifica."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def _make_orchestrator(self):
        return Orchestrator(config=BUOConfig(), mock=False, dry_run=True)

    def test_blocks_on_old_kernel(self):
        orch = self._make_orchestrator()
        orch.audit.run = lambda: {
            "kernel": {"release": "6.6.0", "meets_minimum": False},
            "mesa": {"version": "25.1", "meets_minimum": True},
            "temps": {"cpu_temp": 40.0, "gpu_temp": 35.0},
        }
        with self.assertRaises(SafetyViolation) as ctx:
            orch._preflight_checks()
        self.assertIn("6.11", str(ctx.exception))

    def test_blocks_on_old_mesa(self):
        orch = self._make_orchestrator()
        orch.audit.run = lambda: {
            "kernel": {"release": "6.18.0", "meets_minimum": True},
            "mesa": {"version": "24.3", "meets_minimum": False},
            "temps": {"cpu_temp": 40.0, "gpu_temp": 35.0},
        }
        with self.assertRaises(SafetyViolation) as ctx:
            orch._preflight_checks()
        self.assertIn("25.1", str(ctx.exception))

    def test_blocks_on_hot_cpu(self):
        orch = self._make_orchestrator()
        orch.audit.run = lambda: {
            "kernel": {"release": "6.18.0", "meets_minimum": True},
            "mesa": {"version": "25.2", "meets_minimum": True},
            "temps": {"cpu_temp": 95.0, "gpu_temp": 35.0},
        }
        with self.assertRaises(SafetyViolation):
            orch._preflight_checks()

    def test_passes_with_healthy_system(self):
        orch = self._make_orchestrator()
        orch.audit.run = lambda: {
            "kernel": {"release": "6.18.0", "meets_minimum": True},
            "mesa": {"version": "25.2", "meets_minimum": True},
            "temps": {"cpu_temp": 42.0, "gpu_temp": 38.0},
        }
        orch._preflight_checks()  # non deve sollevare


class TestDryRunNeverTouchesHardware(unittest.TestCase):
    """In dry-run tutti i moduli che scrivono sono in modalità simulata."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def test_dry_run_modules_are_simulated(self):
        orch = Orchestrator(config=BUOConfig(), mock=False, dry_run=True)
        # Moduli che scrivono sull'hardware → devono essere in mock
        self.assertTrue(orch.cpu_unlock.mock)
        self.assertTrue(orch.gpu_unlock.mock)
        self.assertTrue(orch.fix_iommu.mock)
        self.assertTrue(orch.fix_acpi.mock)
        self.assertTrue(orch.uv_cpu.mock)
        self.assertTrue(orch.uv_gpu.mock)
        self.assertTrue(orch.governor.mock)

    def test_mock_run_touches_nothing(self):
        orch = Orchestrator(config=BUOConfig(), mock=False, dry_run=True)
        orch.checkpoint.clear()
        rc = orch.run()
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
