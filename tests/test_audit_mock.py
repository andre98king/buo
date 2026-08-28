#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test dell'audit in modalità mock/dry-run.

In mock l'audit NON deve spawnare subprocess (glxinfo/systemctl/modinfo)
né leggere file reali di /proc, /sys o il file dei risultati health:
deve restituire un risultato completo ma simulato, con la stessa forma
dell'audit reale.
"""

import unittest
from unittest import mock

from buo.audit.hardware import HardwareAudit


class TestAuditMock(unittest.TestCase):
    """HardwareAudit(mock=True) è pura simulazione, mai I/O reale."""

    def test_mock_run_spawns_no_subprocess_and_reads_no_files(self):
        audit = HardwareAudit(mock=True)
        opened = []

        def guarded_open(file, *args, **kwargs):
            opened.append(str(file))
            raise AssertionError(f"mock audit ha letto un file reale: {file}")

        with mock.patch("subprocess.run",
                        side_effect=AssertionError("subprocess spawnato")):
            with mock.patch("builtins.open", side_effect=guarded_open):
                result = audit.run()

        self.assertEqual(opened, [], f"file letti inattesi: {opened}")

    def test_mock_run_returns_complete_shape(self):
        result = HardwareAudit(mock=True).run()

        for key in ["cpu", "gpu", "system", "kernel", "mesa", "iommu",
                    "acpi", "governor", "amdgpu", "health", "temps"]:
            self.assertIn(key, result, f"chiave mancante: {key}")

    def test_mock_run_cpu_gpu_values(self):
        result = HardwareAudit(mock=True).run()
        self.assertEqual(result["cpu"]["cores"], 6)
        self.assertEqual(result["cpu"]["core_mask"], "0x77")
        self.assertFalse(result["cpu"]["unlocked"])
        self.assertEqual(result["gpu"]["cu_count"], 24)
        self.assertEqual(result["gpu"]["stable_cu"], 24)
        self.assertEqual(result["gpu"]["defective_cu"], [])

    def test_mock_run_keeps_downstream_paths_working(self):
        """Valori che non fanno scattare problemi spuri né crash."""
        from buo.audit.problems import ProblemDetector
        result = HardwareAudit(mock=True).run()
        ids = [p["id"] for p in ProblemDetector(mock=True).detect(result)]

        # IOMMU attivo, kernel/Mesa a norma, governor attivo → nessun
        # problema per quei gate; il rilevatore funziona senza crash.
        self.assertNotIn("iommu_disabled", ids)
        self.assertNotIn("kernel_old", ids)
        self.assertNotIn("mesa_old", ids)
        self.assertNotIn("governor_missing", ids)
        self.assertIn("tlb_fault", ids)  # problema sempre presente (stock)

    def test_mock_run_reflects_hardware_state(self):
        """Lo stato MockHardware viene rispecchiato nell'audit."""
        from buo.utils.mock import MockHardware
        hw = MockHardware()
        hw.state.is_acpi_fixed = True
        hw.state.iommu_off = True
        hw.enable_40cu()

        with mock.patch("subprocess.run",
                        side_effect=AssertionError("subprocess spawnato")):
            result = HardwareAudit(mock=True, mock_hardware=hw).run()

        self.assertFalse(result["cpu"]["unlocked"])
        self.assertEqual(result["gpu"]["cu_count"], 40)
        self.assertFalse(result["iommu"]["enabled"])
        self.assertTrue(result["iommu"]["cmdline_has_iommu_off"])
        self.assertTrue(result["acpi"]["cst_present"])
        self.assertTrue(result["acpi"]["pst_present"])
        self.assertTrue(result["amdgpu"]["patched_for_40cu"])


if __name__ == "__main__":
    unittest.main()
