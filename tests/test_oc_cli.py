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
        # NOTA M2: senza --mock/--dry-run (le modalità simulate NON
        # scrivono: vedi TestSimulatedNoWrites)
        res = self.invoke("oc", "profiles", "add", "Custom3600", "--freq",
                          "3600", "--scale", "-10", "--vid", "975",
                          "--oc-dir", str(self.oc))
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


class TestSimulatedNoWrites(Base):
    """M2: --mock/--dry-run NON scrive MAI profili/stato/apply (le scritture
    sono skip esplicito con messaggio, mai file toccati)."""

    def _profiles_file(self):
        return self.oc / "profiles.json"

    def test_profiles_add_mock_does_not_write(self):
        res = self.invoke("oc", "profiles", "add", "X", "--freq", "3600",
                          "--scale", "-10", "--vid", "975", "--mock",
                          "--oc-dir", str(self.oc))
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn("saltato", res.output)
        self.assertFalse(self._profiles_file().exists())

    def test_profiles_add_dry_run_does_not_write(self):
        res = self.invoke("oc", "profiles", "add", "X", "--freq", "3600",
                          "--scale", "-10", "--vid", "975", "--dry-run",
                          "--oc-dir", str(self.oc))
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn("saltato", res.output)
        self.assertFalse(self._profiles_file().exists())

    def test_profiles_rm_mock_keeps_store(self):
        from buo.oc.profiles import Profile, ProfileStore
        store = ProfileStore(self.oc)
        store.save([Profile(id="custom-x", name="X", freq=3600, scale=-10,
                            vid_cap=975, source="user", validated=False)])
        res = self.invoke("oc", "profiles", "rm", "custom-x", "--mock",
                          "--oc-dir", str(self.oc))
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn("saltato", res.output)
        self.assertIsNotNone(store.get("custom-x"))   # profilo ancora lì

    def test_reset_mock_keeps_checkpoint(self):
        state = self.oc / "state.json"
        self.assertTrue(state.exists())
        res = self.invoke("oc", "reset", "--mock", "--yes", "--oc-dir",
                          str(self.oc))
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn("saltato", res.output)
        self.assertTrue(state.exists())   # checkpoint NON cancellato

    def test_reset_dry_run_keeps_checkpoint(self):
        state = self.oc / "state.json"
        res = self.invoke("oc", "reset", "--dry-run", "--yes", "--oc-dir",
                          str(self.oc))
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn("saltato", res.output)
        self.assertTrue(state.exists())

    def test_apply_mock_writes_no_state_files(self):
        res = self.invoke("oc", "apply", "stock", "--mock", "--oc-dir",
                          str(self.oc))
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertFalse((self.oc / "apply.json").exists())
        self.assertFalse(self._profiles_file().exists())
        self.assertFalse(list(self.oc.glob("apply-*.conf")))


if __name__ == "__main__":
    unittest.main()
