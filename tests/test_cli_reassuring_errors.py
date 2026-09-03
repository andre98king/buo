#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test degli errori rassicuranti della CLI (spec UX_REVAMP_CLI_SPEC §4).

`buo unleash` con exit code non-zero deve dire cosa è successo, cosa è
già stato fatto (rollback automatico) e cosa fare (log → doctor →
riprova). Orchestrator finto (mai hardware reale).
"""

import os
import tempfile
import unittest
from unittest import mock

from click.testing import CliRunner

from buo.cli import cli
from buo.constants import EXIT_ERROR, EXIT_SAFETY_VIOLATION, EXIT_SUCCESS


class _FakeOrchestrator:
    """run() restituisce l'exit code deciso dal test."""

    def __init__(self, exit_code, safety_reason=""):
        self._rc = exit_code
        self.safety_reason = safety_reason

    def run(self, start_phase=None, stop_after=None, restore=None):
        return self._rc

    def riepilogo_lines(self):
        return ["Riepilogo finale", "  fix applicati in questa run: 0",
                "  rollback: sudo buo rollback"]


class TestReassuringErrors(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name
        os.environ["BUO_DEPS_DIR"] = self._tmp.name
        self.runner = CliRunner()
        self._patcher = mock.patch("buo.cli._make_orchestrator")
        self._mock_factory = self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        os.environ.pop("BUO_STATE_DIR", None)
        os.environ.pop("BUO_DEPS_DIR", None)
        self._tmp.cleanup()

    def _invoke(self, args, orch):
        self._mock_factory.return_value = orch
        res = self.runner.invoke(cli, args)
        # il Console rich va a capo a 80 colonne: normalizza gli spazi
        # prima di cercare le frasi (il testo è identico)
        res.normalized = " ".join(res.output.split())
        return res

    def test_exit_error_shows_rollback_and_cosa_fare(self):
        res = self._invoke(["unleash"],
                           _FakeOrchestrator(EXIT_ERROR))
        self.assertEqual(res.exit_code, EXIT_ERROR)
        self.assertIn("rollback automatico", res.normalized)
        self.assertIn("Cosa fare", res.normalized)
        self.assertIn("controlla il log: /var/log/buo/buo.log",
                      res.normalized)
        self.assertIn("sudo buo doctor", res.normalized)
        self.assertIn("riprova: sudo buo unleash", res.normalized)

    def test_safety_violation_shows_reason(self):
        res = self._invoke(
            ["unleash"],
            _FakeOrchestrator(EXIT_SAFETY_VIOLATION,
                              safety_reason="CPU Temp 95.0°C > 90°C"))
        self.assertEqual(res.exit_code, EXIT_SAFETY_VIOLATION)
        self.assertIn("SAFETY VIOLATION", res.normalized)
        self.assertIn("Motivo: CPU Temp 95.0°C > 90°C", res.normalized)
        self.assertIn("rollback automatico", res.normalized)
        self.assertIn("Cosa fare", res.normalized)
        self.assertIn("se il motivo è termico: aspetta che la macchina "
                      "si raffreddi", res.normalized)

    def test_success_draws_riepilogo_panel(self):
        """Exit 0 → il pannello mostra le righe di riepilogo_lines() e
        NON la vecchia tripla (riga verde + Report: + Rollback:)."""
        res = self._invoke(["unleash"], _FakeOrchestrator(EXIT_SUCCESS))
        self.assertEqual(res.exit_code, EXIT_SUCCESS)
        self.assertIn("rollback: sudo buo rollback", res.normalized)
        self.assertIn("Riepilogo finale", res.normalized)
        self.assertNotIn("OTTIMIZZAZIONE COMPLETATA!", res.normalized)

    def test_restore_failure_keeps_ripristino_fallito_marker(self):
        """§6.1: il fallimento di `buo restore` mantiene il marcatore
        'ripristino fallito' e mostra il blocco rassicurante (§4)."""
        import json
        from pathlib import Path
        prof = Path(self._tmp.name) / "profilo.json"
        prof.write_text(json.dumps({
            "profile_version": 1,
            "created": "2026-01-01T00:00:00",
            "applied_fixes": ["cpu_core_unlock"],
            "optimize": {"undervolt_cpu": {}},
        }), encoding="utf-8")
        res = self._invoke(["restore", "--profile", str(prof)],
                           _FakeOrchestrator(EXIT_ERROR))
        self.assertEqual(res.exit_code, EXIT_ERROR)
        self.assertIn("Ripristino fallito (codice 1)", res.normalized)
        self.assertIn("rollback automatico", res.normalized)
        self.assertIn("Cosa fare", res.normalized)


if __name__ == "__main__":
    unittest.main()
