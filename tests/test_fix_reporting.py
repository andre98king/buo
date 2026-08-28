#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test dell'honest fix reporting e del guard servizio 40-CU (BUGS #24).

Copre:
    1. Classificazione onesta dell'esito dei fix (applied/manual/failed)
       in `_phase_fix` → `self.results["fix_summary"]` + campo `status`.
    2. Il guard non bloccante BUGS #24: avviso quando
       `bc250-cu-live-manager.service` è mancante/disabilitato su ostree.
"""

import os
import tempfile
import unittest
from unittest import mock

from buo.config import BUOConfig
from buo.orchestrator import Orchestrator
from buo.utils.mock import MockHardware


class _FakeFixer:
    """Fixer controllabile per una classificazione deterministica."""

    def __init__(self, verify=False, applied=True, error=None, warning=None,
                 needs_reboot=False, raise_on_apply=None):
        self._verify = verify
        self._raise_on_apply = raise_on_apply
        self._result = {"applied": applied, "needs_reboot": needs_reboot}
        if error is not None:
            self._result["error"] = error
        if warning is not None:
            self._result["warning"] = warning

    def verify(self):
        return self._verify

    def apply(self):
        if self._raise_on_apply is not None:
            raise self._raise_on_apply
        return dict(self._result)


class TestFixSummaryClassification(unittest.TestCase):
    """Classificazione applied/manual/failed e campi status/note."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def _make(self):
        hw = MockHardware(seed=1)
        orch = Orchestrator(config=BUOConfig(), mock=True, dry_run=False,
                            mock_hardware=hw)
        orch.checkpoint.clear()
        return orch

    def test_phase_fix_classifies_applied_manual_failed(self):
        orch = self._make()

        # fan_control risulta già eseguito in un run precedente → applied
        orch.checkpoint.set("applied_steps", ["fan_control"])

        orch.fix_iommu = _FakeFixer(applied=False, warning="IOMMU: verifica manuale")
        orch.fix_acpi = _FakeFixer(applied=True)
        orch.fix_tlb = _FakeFixer(applied=False, warning="patch TLB manuale")
        orch.fix_ace = _FakeFixer(applied=False,
                                  raise_on_apply=RuntimeError("repo ACE mancante"))
        orch.fix_vram = _FakeFixer(applied=False,
                                   warning="bc250_memcfg non configurato")
        orch.fix_gtt = _FakeFixer(verify=True)   # già attivo → applied
        orch.fix_fan = _FakeFixer(applied=True)  # in done → skipped_checkpoint

        with self.assertLogs("buo.Orchestrator", level="WARNING") as logs:
            orch._phase_fix()

        summary = orch.results["fix_summary"]
        self.assertCountEqual(summary["applied"],
                              ["acpi_fix", "gtt_tuning", "fan_control"])
        self.assertCountEqual(summary["manual"], ["iommu", "tlb_fix", "vram_config"])
        self.assertCountEqual(summary["failed"], ["ace_fix"])

        results = orch.results["fix_results"]
        self.assertEqual(results["acpi_fix"]["status"], "applied")
        self.assertEqual(results["gtt_tuning"]["status"], "applied")
        self.assertEqual(results["gtt_tuning"]["note"], "già attivo (verificato)")
        self.assertEqual(results["fan_control"]["status"], "applied")
        self.assertEqual(results["fan_control"]["note"], "già eseguito (checkpoint)")
        self.assertEqual(results["iommu"]["status"], "manual")
        self.assertEqual(results["iommu"]["note"], "IOMMU: verifica manuale")
        self.assertEqual(results["tlb_fix"]["status"], "manual")
        self.assertEqual(results["ace_fix"]["status"], "failed")
        self.assertEqual(results["ace_fix"]["note"], "repo ACE mancante")
        self.assertEqual(results["vram_config"]["status"], "manual")
        self.assertEqual(results["vram_config"]["note"],
                         "bc250_memcfg non configurato")

        # Il riepilogo viene loggato chiaramente
        joined = "\n".join(logs.output)
        self.assertIn("Fix NON applicati automaticamente", joined)

    def test_classify_fix_static_rules(self):
        self.assertEqual(Orchestrator._classify_fix({"applied": True}), "applied")
        self.assertEqual(Orchestrator._classify_fix(
            {"applied": True, "skipped_verified": True}), "applied")
        self.assertEqual(Orchestrator._classify_fix(
            {"applied": False, "warning": "manuale"}), "manual")
        self.assertEqual(Orchestrator._classify_fix(
            {"applied": False, "error": "boom"}), "failed")

    def test_fix_note_prefers_specific_markers(self):
        self.assertEqual(Orchestrator._fix_note({"skipped_checkpoint": True}),
                         "già eseguito (checkpoint)")
        self.assertEqual(Orchestrator._fix_note({"skipped_verified": True}),
                         "già attivo (verificato)")
        self.assertEqual(Orchestrator._fix_note({"dry_run": True}),
                         "simulato (dry-run)")
        self.assertEqual(Orchestrator._fix_note({"applied": True, "error": "e"}),
                         "e")

    def test_finalize_appends_manual_attention_note(self):
        orch = self._make()
        orch.results["fix_summary"] = {
            "applied": ["gtt_tuning"],
            "manual": ["iommu", "tlb_fix", "vram_config"],
            "failed": ["ace_fix"],
        }
        orch._finalize()
        joined = " ".join(orch.results["notes"])
        self.assertIn("manuali: iommu, tlb_fix", joined)
        self.assertIn("falliti: ace_fix", joined)

    def test_finalize_no_note_without_manual_or_failed(self):
        orch = self._make()
        orch.results["fix_summary"] = {"applied": ["gtt_tuning"],
                                       "manual": [], "failed": []}
        n_before = len(orch.results["notes"])
        orch._finalize()
        # nessuna nota extra oltre a quelle preesistenti
        self.assertEqual(len(orch.results["notes"]), n_before)


class Test40CUServiceGuard(unittest.TestCase):
    """BUGS #24: avviso quando il servizio 40-CU è assente/disabilitato."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def _make(self):
        return Orchestrator(config=BUOConfig(), mock=False, dry_run=True)

    def test_service_missing_emits_bugs24_warning(self):
        orch = self._make()
        with mock.patch("buo.utils.shell.run_command",
                        return_value=(1, "",
                                      "Unit bc250-cu-live-manager.service "
                                      "could not be found.")) as run:
            with self.assertLogs("buo.Orchestrator", level="WARNING") as logs:
                orch._check_40cu_service_enabled()
        joined = "\n".join(logs.output)
        self.assertIn("BUGS #24", joined)
        self.assertIn("install-service", joined)
        self.assertIn("apply-service", joined)
        run.assert_called_once_with(
            ["systemctl", "is-enabled", "bc250-cu-live-manager"], check=False)

    def test_service_disabled_emits_bugs24_warning(self):
        orch = self._make()
        with mock.patch("buo.utils.shell.run_command",
                        return_value=(1, "disabled", "")):
            with self.assertLogs("buo.Orchestrator", level="WARNING") as logs:
                orch._check_40cu_service_enabled()
        self.assertIn("BUGS #24", "\n".join(logs.output))

    def test_service_enabled_logs_info_no_warning(self):
        orch = self._make()
        with mock.patch("buo.utils.shell.run_command",
                        return_value=(0, "enabled", "")) as run:
            with self.assertLogs("buo.Orchestrator", level="INFO") as logs:
                orch._check_40cu_service_enabled()
        joined = "\n".join(logs.output)
        self.assertIn("abilitato", joined)
        self.assertNotIn("BUGS #24", joined)
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
