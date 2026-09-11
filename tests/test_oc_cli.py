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

    def test_cu_live_mock_no_writes_derived_mask(self):
        """`buo oc cu-live` (percorso cumulativo live): in --mock nessuna
        scrittura/nessun comando reale e maschera DERIVATA dalle WGP date."""
        from buo.config import BUOConfig
        cfg = BUOConfig({"phases": {"probe": {"gpu_extra_cu": True}}})
        with mock.patch("buo.config.BUOConfig.load", return_value=cfg):
            res = self.invoke("oc", "cu-live", "0.1.3", "--mock", "--oc-dir",
                              str(self.oc))
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn("saltato", res.output)          # simulazione
        self.assertIn("0x07,0x0f,0x07,0x07", res.output)   # 26 CU
        self.assertIn("26 CU", res.output)

    def test_cu_live_opt_out_reports_reason(self):
        """Opt-out (default): nessuna CU extra, motivo esplicito."""
        from buo.config import BUOConfig
        with mock.patch("buo.config.BUOConfig.load",
                        return_value=BUOConfig()):
            res = self.invoke("oc", "cu-live", "0.1.3", "--mock", "--oc-dir",
                              str(self.oc))
        self.assertIn("extra_cu_disabled", res.output)

    def test_cu_live_real_opt_out_exits_nonzero(self):
        """Run reale senza opt-in: exit 1, nessuna scrittura maschera."""
        from buo.config import BUOConfig
        with mock.patch("buo.config.BUOConfig.load",
                        return_value=BUOConfig()):
            res = self.invoke("oc", "cu-live", "0.1.3", "--oc-dir",
                              str(self.oc))
        self.assertEqual(res.exit_code, 1, res.output)
        self.assertIn("extra_cu_disabled", res.output)

    def test_cu_live_invalid_wgp_refused(self):
        """WGP anomala (o stock) → rifiuto esplicito, mai scrittura."""
        from buo.config import BUOConfig
        cfg = BUOConfig({"phases": {"probe": {"gpu_extra_cu": True}}})
        with mock.patch("buo.config.BUOConfig.load", return_value=cfg):
            res = self.invoke("oc", "cu-live", "0.0.1", "--oc-dir",
                              str(self.oc))
        self.assertEqual(res.exit_code, 1, res.output)
        self.assertIn("non è una delle extra", res.output)

    def test_apply_mock_writes_no_state_files(self):
        res = self.invoke("oc", "apply", "stock", "--mock", "--oc-dir",
                          str(self.oc))
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertFalse((self.oc / "apply.json").exists())
        self.assertFalse(self._profiles_file().exists())
        self.assertFalse(list(self.oc.glob("apply-*.conf")))


class TestCuLiveTracking(Base):
    """Tracciamento del percorso cumulativo (oc_dir/gpu-cu-live.json).

    Il protocollo è human-in-the-loop: senza diario, chi conduce il test non
    sa quali WGP ha già provato. Il tracciamento è fail-soft: NON deve mai
    bloccare un'operazione che ha già scritto i registri.
    """

    def _path(self):
        return self.oc / "gpu-cu-live.json"

    def test_record_and_read_roundtrip(self):
        from buo.oc.cli import read_cu_live, record_cu_live
        self.assertTrue(record_cu_live(self.oc, ["0.1.3"],
                                       {"cu_count": 26,
                                        "mask": "0x07,0x0f,0x07,0x07"}))
        tries = read_cu_live(self.oc)
        self.assertEqual(len(tries), 1)
        self.assertEqual(tries[0]["wgps"], ["0.1.3"])
        self.assertEqual(tries[0]["cu_count"], 26)
        self.assertEqual(tries[0]["esito"], "applicato")
        self.assertTrue(tries[0]["at"])
        schema = json.loads(self._path().read_text(encoding="utf-8"))
        self.assertEqual(schema["schema"], 1)
        self.assertEqual(schema["tries"], tries)

    def test_same_wgps_updates_instead_of_duplicating(self):
        from buo.oc.cli import read_cu_live, record_cu_live
        record_cu_live(self.oc, ["0.1.3"], {"cu_count": 26})
        record_cu_live(self.oc, ["0.1.3"], {"cu_count": 26})
        self.assertEqual(len(read_cu_live(self.oc)), 1)
        record_cu_live(self.oc, ["0.1.3", "0.1.4"], {"cu_count": 28})
        self.assertEqual(len(read_cu_live(self.oc)), 2)

    def test_missing_or_corrupt_file_is_no_history(self):
        from buo.oc.cli import read_cu_live
        self.assertEqual(read_cu_live(self.oc), [])
        self._path().write_text("{non json", encoding="utf-8")
        self.assertEqual(read_cu_live(self.oc), [])
        self._path().write_text(json.dumps({"schema": 99, "tries": [1]}),
                                encoding="utf-8")
        self.assertEqual(read_cu_live(self.oc), [])

    def test_unwritable_tracking_is_fail_soft(self):
        """File di stato non scrivibile → False, nessuna eccezione (le CU
        extra SONO già state abilitate: il diario non blocca nulla)."""
        from buo.oc.cli import record_cu_live
        blocked = Path(self.tmp.name) / "bloccato"
        blocked.write_text("sono un file, non una dir\n", encoding="utf-8")
        self.assertFalse(record_cu_live(blocked, ["0.1.3"], {"cu_count": 26}))

    def test_mock_writes_no_tracking(self):
        """--mock: nessuna scrittura (C1), nemmeno il diario."""
        from buo.config import BUOConfig
        cfg = BUOConfig({"phases": {"probe": {"gpu_extra_cu": True}}})
        with mock.patch("buo.config.BUOConfig.load", return_value=cfg):
            res = self.invoke("oc", "cu-live", "0.1.3", "--mock", "--oc-dir",
                              str(self.oc))
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertFalse(self._path().exists())

    def test_show_lists_recorded_tries(self):
        from buo.oc.cli import record_cu_live
        record_cu_live(self.oc, ["0.1.3", "0.1.4"], {"cu_count": 28,
                                                    "mask": "0x07,0x1f,0x07,0x07"})
        res = self.invoke("oc", "cu-live", "--show", "--oc-dir", str(self.oc))
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn("0.1.3,0.1.4", res.output)
        self.assertIn("28", res.output)

    def test_show_without_history_is_explicit(self):
        res = self.invoke("oc", "cu-live", "--show", "--oc-dir", str(self.oc))
        self.assertEqual(res.exit_code, 0, res.output)
        self.assertIn("Nessuna prova registrata", res.output)

    def test_no_wgp_and_no_show_is_a_usage_error(self):
        res = self.invoke("oc", "cu-live", "--oc-dir", str(self.oc))
        self.assertNotEqual(res.exit_code, 0)
        self.assertIn("WGP", res.output)

    def test_tracking_file_name_is_owned_by_the_tool(self):
        from buo.oc.constants import CU_LIVE_FILE, CU_LIVE_SCHEMA
        self.assertEqual(CU_LIVE_FILE, "gpu-cu-live.json")
        self.assertEqual(CU_LIVE_SCHEMA, 1)


if __name__ == "__main__":
    unittest.main()
