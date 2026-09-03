#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke test della CLI (exit code) e dei comandi fase."""

import os
import tempfile
import unittest
from unittest import mock

from click.testing import CliRunner

from buo.cli import cli


class TestCLISmoke(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name
        os.environ["BUO_DEPS_DIR"] = self._tmp.name
        self.runner = CliRunner()

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        os.environ.pop("BUO_DEPS_DIR", None)
        self._tmp.cleanup()

    def _invoke(self, args):
        return self.runner.invoke(cli, args)

    def test_version(self):
        result = self._invoke(["--version"])
        self.assertEqual(result.exit_code, 0)

    def test_status_mock(self):
        result = self._invoke(["status", "--mock"])
        self.assertEqual(result.exit_code, 0)

    def test_probe_mock(self):
        result = self._invoke(["probe", "--mock"])
        self.assertEqual(result.exit_code, 0)

    def test_undervolt_mock(self):
        result = self._invoke(["undervolt", "--mock", "--dry-run"])
        self.assertEqual(result.exit_code, 0)

    def test_overclock_mock(self):
        result = self._invoke(["overclock", "--mock", "--dry-run"])
        self.assertEqual(result.exit_code, 0)

    def test_apply_mock(self):
        result = self._invoke(["apply", "--mock", "--dry-run"])
        self.assertEqual(result.exit_code, 0)

    def test_benchmark_mock(self):
        result = self._invoke(["benchmark", "--mock"])
        self.assertEqual(result.exit_code, 0)

    def test_safety_test_mock(self):
        result = self._invoke(["safety-test", "--mock"])
        self.assertEqual(result.exit_code, 0)

    def test_config(self):
        result = self._invoke(["config"])
        self.assertEqual(result.exit_code, 0)

    def test_install_deps_check(self):
        result = self._invoke(["install-deps", "--check"])
        # 0 = tutto presente, 1 = manca qualcosa: entrambi validi
        self.assertIn(result.exit_code, (0, 1))

    def test_install_deps_offline_missing_file(self):
        result = self._invoke(["install-deps", "--offline",
                               "/nonexistent/bundle.tar.gz"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("bundle", result.output)

    def test_install_deps_export_and_offline_mutually_exclusive(self):
        result = self._invoke(["install-deps",
                               "--export-bundle", "/tmp/x.tar.gz",
                               "--offline", "/tmp/y.tar.gz"])
        self.assertEqual(result.exit_code, 2)  # UsageError
        self.assertIn("uno alla volta", result.output)

    def test_install_deps_check_and_offline_conflict(self):
        result = self._invoke(["install-deps", "--check",
                               "--offline", "/tmp/y.tar.gz"])
        self.assertEqual(result.exit_code, 2)  # UsageError

    def test_unleash_offline_bundle_mock_no_import(self):
        # in --mock mai import: il path inesistente non deve mai essere
        # toccato (se lo fosse, l'import fallirebbe con exit != 0)
        result = self._invoke(["unleash", "--mock",
                               "--offline-bundle", "/nonexistent/bundle.tar.gz"])
        self.assertEqual(result.exit_code, 0)

    def test_tui_without_textual(self):
        """Senza textual, `buo tui` esce con codice 1 e messaggio chiaro."""
        import importlib.util
        if importlib.util.find_spec("textual") is not None:
            self.skipTest("textual installato")
        result = self._invoke(["tui"])
        self.assertEqual(result.exit_code, 1)

    def test_unknown_command(self):
        result = self._invoke(["non-esiste"])
        self.assertNotEqual(result.exit_code, 0)


class TestPhaseCommandsWithOrchestrator(unittest.TestCase):
    """`run(start_phase, stop_after)` esegue solo le fasi richieste."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def test_stop_after_pre_audit(self):
        from buo.config import BUOConfig
        from buo.orchestrator import Orchestrator
        cfg = BUOConfig()
        cfg.benchmark_enabled = False
        orch = Orchestrator(config=cfg, mock=True, dry_run=False)
        orch.checkpoint.clear()
        rc = orch.run(start_phase="pre_audit", stop_after="pre_audit")
        self.assertEqual(rc, 0)
        phases = orch.checkpoint.full_state()["phases"]
        self.assertIn("pre_audit", phases)
        self.assertNotIn("unlock", phases)  # fermato dopo pre_audit

    def test_stop_after_apply(self):
        from buo.config import BUOConfig
        from buo.orchestrator import Orchestrator
        cfg = BUOConfig()
        cfg.benchmark_enabled = False
        orch = Orchestrator(config=cfg, mock=True, dry_run=False)
        orch.checkpoint.clear()
        rc = orch.run(start_phase="optimize", stop_after="apply")
        self.assertEqual(rc, 0)
        phases = orch.checkpoint.full_state()["phases"]
        self.assertIn("optimize", phases)
        self.assertIn("apply", phases)
        self.assertNotIn("validate", phases)


class _FakeOrchestrator:
    """Stub per M1: audit/detect/benchmark SENZA hardware reale (mai
    valori finti presentati come reali nei comandi probe/safety-test)."""

    class _Detector:
        def detect(self, audit):
            return [{"severity": "alta", "title": "Problema fittizio",
                     "detail": "solo per il test"},
                    {"severity": "media", "title": "Altro fittizio",
                     "detail": "x"}]

    class _Audit:
        def run(self):
            return {"fake": True}

    class _Bench:
        def run_all(self, **kw):
            return {"cpu_bench": {"available": True, "score": 42},
                    "timestamp": 0.0}

    def __init__(self):
        self.audit = self._Audit()
        self.detector = self._Detector()
        self.benchmark = self._Bench()


class TestHonestReadOnlyCommands(unittest.TestCase):
    """M1 (classe C1): probe/safety-test/benchmark SENZA --mock devono
    fare letture reali (mai valori simulati spacciati per reali) e CON
    --mock devono marcare esplicitamente l'output come SIMULATO."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name
        self.runner = CliRunner()
        self._patcher = mock.patch("buo.cli._make_orchestrator",
                                   return_value=_FakeOrchestrator())
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def _invoke(self, args):
        return self.runner.invoke(cli, args)

    def test_probe_without_mock_no_simulated_output(self):
        res = self._invoke(["probe"])
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn("Problema fittizio", res.output)
        self.assertNotIn("SIMULATO", res.output)

    def test_probe_mock_is_labeled_simulated(self):
        res = self._invoke(["probe", "--mock"])
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn("SIMULATO", res.output)

    def test_safety_test_without_mock_no_simulated_output(self):
        res = self._invoke(["safety-test"])
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn("Problema fittizio", res.output)
        self.assertNotIn("SIMULATO", res.output)

    def test_safety_test_mock_is_labeled_simulated(self):
        res = self._invoke(["safety-test", "--mock"])
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn("SIMULATO", res.output)

    def test_benchmark_without_mock_no_simulated_output(self):
        res = self._invoke(["benchmark"])
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertNotIn("SIMULATO", res.output)

    def test_benchmark_mock_is_labeled_simulated(self):
        res = self._invoke(["benchmark", "--mock"])
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn("SIMULATO", res.output)


if __name__ == "__main__":
    unittest.main()
