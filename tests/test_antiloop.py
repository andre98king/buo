#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Anti-loop: le modifiche che richiedono reboot non devono causare
riavvii infiniti (bug trovato sul campo: ACPI fix → reboot → resume →
fase fix di nuovo → reboot → loop).
"""

import os
import tempfile
import unittest

from buo.config import BUOConfig
from buo.orchestrator import Orchestrator
from buo.utils.mock import MockHardware


class TestAntiLoop(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def _make(self, dry_run=False):
        cfg = BUOConfig()
        cfg.benchmark_enabled = False
        cfg.validation_stress_duration = 0
        orch = Orchestrator(config=cfg, mock=True, dry_run=dry_run,
                            mock_hardware=MockHardware(seed=3))
        orch.checkpoint.clear()
        return orch

    def _count_reboots(self, orch, fn):
        """Esegue fn contando le chiamate a _schedule_reboot."""
        from buo.orchestrator import Orchestrator
        calls = []
        original = Orchestrator._schedule_reboot

        def fake(self, reason):
            calls.append(reason)
            self.checkpoint.increment_reboot_count()

        Orchestrator._schedule_reboot = fake
        try:
            fn()
        finally:
            Orchestrator._schedule_reboot = original
        return calls

    def test_fix_phase_at_most_one_reboot(self):
        """Più fix che richiedono reboot → UN solo reboot per rientro."""
        orch = self._make()
        calls = self._count_reboots(orch, orch._phase_fix)
        self.assertEqual(len(calls), 1, f"reboot schedulati: {calls}")
        # IOMMU è un no-op (verify già attivo): il primo fix che richiede
        # reboot è acpi_fix (docs/BUGS.md #2)
        self.assertIn("acpi_fix", calls[0])

    def test_fix_phase_skips_applied_steps_on_resume(self):
        """Resume: i fix già nel ledger vengono saltati (niente loop)."""
        orch = self._make()
        orch.checkpoint.set("applied_steps", ["iommu"])
        orch.checkpoint.set_current_phase("fix")

        calls = self._count_reboots(orch, orch._phase_fix)
        # iommu NON deve essere ri-applicato: il primo reboot è per acpi_fix
        self.assertEqual(len(calls), 1)
        self.assertIn("acpi_fix", calls[0])

        # Verifica che il ledger ora contenga iommu E acpi_fix
        steps = orch._applied_steps()
        self.assertIn("iommu", steps)
        self.assertIn("acpi_fix", steps)

    def test_ledger_names_match_rollback_levels(self):
        """I nomi del ledger devono coincidere con ROLLBACK_ORDER
        (altrimenti il rollback automatico non ripristina nulla)."""
        from buo.constants import ROLLBACK_ORDER
        orch = self._make()
        orch._phase_fix()
        steps = orch._applied_steps()
        for s in steps:
            self.assertIn(s, ROLLBACK_ORDER,
                          f"{s!r} non è un livello di rollback")

    def test_unlock_skips_cpu_already_done(self):
        """Se cpu_core_unlock è nel ledger, l'unlock non si ripete."""
        orch = self._make()
        orch.checkpoint.set("applied_steps", ["cpu_core_unlock"])
        orch.checkpoint.set_current_phase("unlock")

        calls = self._count_reboots(orch, orch._phase_unlock)
        # nessun reboot per cpu; al massimo per gpu_40cu (un solo reboot)
        for c in calls:
            self.assertNotIn("CPU", c)

    def test_fresh_run_resets_ledger(self):
        """Un nuovo run completo da init azzera il ledger (i vecchi
        passi spariscono; restano solo quelli applicati nel run nuovo)."""
        orch = self._make()
        orch.checkpoint.set("applied_steps", ["acpi", "iommu"])
        orch.checkpoint.set_current_phase("complete")
        rc = orch.run()
        self.assertEqual(rc, 0)
        steps = orch._applied_steps()
        self.assertNotIn("acpi", steps)          # vecchio passo azzerato
        self.assertIn("cpu_core_unlock", steps)  # applicato nel run nuovo


if __name__ == "__main__":
    unittest.main()
