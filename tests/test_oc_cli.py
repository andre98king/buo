#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test CLI del gruppo `buo oc` e del comando `buo oc-tui` (CliRunner, mock,
directory temporanee — mai hardware reale).
"""

import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from buo.cli import cli


class Base(unittest.TestCase):
    def setUp(self):
        # il logger BUO (RichHandler → stdout) inquinerebbe l'output JSON dei
        # comandi --json/--mock: silenziato SOLO per questi test (ripristino
        # in tearDown via addCleanup)
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.tmp = tempfile.TemporaryDirectory()
        self.oc = Path(self.tmp.name)
        (self.oc / "oc3600.sh").write_text("#!/bin/bash\nexit 0\n",
                                           encoding="utf-8")
        (self.oc / "oc3600.sh").chmod(0o755)
        (self.oc / "state.json").write_text(json.dumps({
            "schema_version": 3, "phase": "P1b",
            "testing": {"freq": 3725, "vid_cap": 1025, "kind": "point",
                        "started_epoch": 1788197000},
            "persisted": False,
        }), encoding="utf-8")
        self.runner = CliRunner()

    def tearDown(self):
        self.tmp.cleanup()

    def invoke(self, *args):
        return self.runner.invoke(cli, list(args))


class TestStatus(Base):
    def test_status_json(self):
        res = self.invoke("oc", "status", "--json", "--mock", "--oc-dir",
                          str(self.oc))
        self.assertEqual(res.exit_code, 0, res.output)
        data = json.loads(res.output)
        self.assertEqual(data["state"]["phase"], "P1b")
        self.assertEqual(data["state"]["testing"]["freq"], 3725)
        self.assertTrue(data["engine"]["present"])

    def test_status_human(self):
        res = self.invoke("oc", "status", "--mock", "--oc-dir", str(self.oc))
        self.assertEqual(res.exit_code, 0)
        self.assertIn("P1b", res.output)


class TestProfiles(Base):
    def test_profiles_list_has_stock(self):
        res = self.invoke("oc", "profiles", "list", "--mock", "--oc-dir",
                          str(self.oc))
        self.assertEqual(res.exit_code, 0)
        self.assertIn("Stock", res.output)

    def test_profiles_add_and_list(self):
        res = self.invoke("oc", "profiles", "add", "Custom3600", "--freq",
                          "3600", "--scale", "-10", "--vid", "975",
                          "--mock", "--oc-dir", str(self.oc))
        self.assertEqual(res.exit_code, 0, res.output)
        res = self.invoke("oc", "profiles", "list", "--mock", "--oc-dir",
                          str(self.oc))
        self.assertIn("Custom3600", res.output)

    def test_profiles_add_zone_block(self):
        res = self.invoke("oc", "profiles", "add", "Zona", "--freq", "3750",
                          "--scale", "-10", "--vid", "950",
                          "--mock", "--oc-dir", str(self.oc))
        self.assertEqual(res.exit_code, 1)
        self.assertIn("zona di hang", res.output)

    def test_profiles_add_zone_vid_none_block(self):
        res = self.invoke("oc", "profiles", "add", "Zona2", "--freq",
                          "3750", "--scale", "-10",
                          "--mock", "--oc-dir", str(self.oc))
        self.assertEqual(res.exit_code, 1)
        self.assertIn("VID non verificabile", res.output)

    def test_profiles_rm_builtin_blocked(self):
        res = self.invoke("oc", "profiles", "rm", "stock",
                          "--mock", "--oc-dir", str(self.oc))
        self.assertEqual(res.exit_code, 1)
        self.assertIn("builtin", res.output)

    def test_profiles_rm_unknown(self):
        res = self.invoke("oc", "profiles", "rm", "inesistente",
                          "--mock", "--oc-dir", str(self.oc))
        self.assertEqual(res.exit_code, 1)
        self.assertIn("non trovato", res.output)


class TestApply(Base):
    def test_apply_stock_mock(self):
        res = self.invoke("oc", "apply", "stock", "--mock", "--oc-dir",
                          str(self.oc))
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn("result=ok", res.output)

    def test_apply_unknown_profile(self):
        res = self.invoke("oc", "apply", "inesistente", "--mock",
                          "--oc-dir", str(self.oc))
        self.assertEqual(res.exit_code, 1)
        self.assertIn("non trovato", res.output)

    def test_restore_stock(self):
        res = self.invoke("oc", "restore-stock", "--mock", "--oc-dir",
                          str(self.oc))
        self.assertEqual(res.exit_code, 0, res.output)

    def test_heal(self):
        res = self.invoke("oc", "heal", "--mock", "--oc-dir", str(self.oc))
        self.assertEqual(res.exit_code, 0, res.output)


class TestRun(Base):
    def test_run_dry_run_no_real_commands(self):
        res = self.invoke("oc", "run", "--dry-run", "--oc-dir", str(self.oc))
        self.assertEqual(res.exit_code, 0, res.output)

    def test_run_engine_missing(self):
        (self.oc / "oc3600.sh").unlink()
        res = self.invoke("oc", "run", "--dry-run", "--oc-dir", str(self.oc))
        self.assertEqual(res.exit_code, 1)


class TestTuiCli(Base):
    @mock.patch("importlib.util.find_spec", return_value=None)
    def test_oc_tui_without_textual_message(self, _find_spec):
        res = self.invoke("oc-tui", "--mock")
        self.assertEqual(res.exit_code, 1)
        self.assertIn("textual", res.output)


if __name__ == "__main__":
    unittest.main()
