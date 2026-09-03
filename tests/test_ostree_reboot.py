#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ostree deployment-aware reboot (design research/DESIGN_OSTREE_REBOOT.md).

BUO esegue run che possono programmare reboot (unlock/fix) mentre la
macchina è avviata su un deployment ostree NON-default. Su image-mode
`systemctl reboot` boota SEMPRE il default (indice 0): la run si orfana.
La feature imposta il default sul deployment bootato a inizio run
(`rpm-ostree rollback`, runner iniettabile) e lo ripristina a fine run
(marker-guarded, fail-closed, mai rollback alla cieca).

Regole: MAI rpm-ostree/subprocess reali (runner finti); inerte in
mock/dry-run/non-ostree/default-booted.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from buo.config import BUOConfig
from buo.constants import (EXIT_ERROR, EXIT_SAFETY_VIOLATION, EXIT_SUCCESS)
from buo.exceptions import SafetyViolation, TimeoutError
from buo.orchestrator import Orchestrator
from buo.state.ostree import (DeploymentInfo, OstreeBootState,
                              OstreeDeploymentManager, _run_ostree_txn)

# Checksum finti (64 hex) — MAI valori fittizi in percorsi reali: qui
# sono solo payload di runner finti iniettati (regola C1).
COMMIT_A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
COMMIT_B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
COMMIT_X = "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"

VER = "7.2.0-ogc6.1.fc44.x86_64"

NON_OSTREE_CMDLINE = (
    "BOOT_IMAGE=/vmlinuz-6.11 root=UUID=1234 ro quiet"
)


def cmdline_booted(commit: str, index: int) -> str:
    """Cmdline reale con token ostree=/ostree/boot.0/<os>/<commit>/<idx>."""
    return (f"BOOT_IMAGE=/ostree/bazzite-{VER}/vmlinuz root=UUID=1234 "
            f"ostree=/ostree/boot.0/bazzite/{commit}/{index} rhgb quiet")


def cmdline_image_mode(base_key: str, index: int) -> str:
    """Cmdline IMAGE-MODE reale (finding di campo 03/09): il hash nel path
    è la boot-dir/base key dell'immagine, CONDIVISA tra i deployment — NON
    è mai un checksum di deployment (i checksum reali sono i nomi delle
    deploy dir, visibili solo in `rpm-ostree status`)."""
    return (f"BOOT_IMAGE=/ostree/default-{base_key}/vmlinuz root=UUID=1234 "
            f"ostree=/ostree/boot.1/default/{base_key}/{index} rhgb quiet")


def dep(checksum: str, booted: bool = False) -> dict:
    return {"id": f"bazzite/{checksum}", "checksum": checksum,
            "booted": booted}


def status_ok(deployments):
    return (0, json.dumps({"deployments": deployments}), "")


def two_deps(booted_index: int, first: str = COMMIT_A,
             second: str = COMMIT_B) -> list:
    """2 deployment attivi in ordine di boot; il booted è `booted_index`."""
    deps = [dep(first), dep(second)]
    deps[booted_index]["booted"] = True
    return deps


class _RecordingTxn:
    """txn_runner finto: registra le chiamate, rc configurabile."""

    def __init__(self, rc: int = 0):
        self.rc = rc
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        return (self.rc, "", "")


class _FakeWorld:
    """Mini-mondo ostree finto: lo status riflette il rollback (come
    rpm-ostree reale, che inverte l'ordine di boot a ogni rollback)."""

    def __init__(self, deployments):
        self.deployments = [dict(d) for d in deployments]
        self.calls = []
        self.rc = 0
        self.fail_status = False

    def status(self, cmd):
        if self.fail_status:
            return (1, "", "rpm-ostree: errore simulato")
        return status_ok(self.deployments)

    def txn(self, cmd):
        self.calls.append(list(cmd))
        if self.rc != 0:
            return (self.rc, "", "rollback simulato fallito")
        if len(self.deployments) == 2:
            self.deployments.reverse()
        return (0, "", "")


def _raising(*args, **kwargs):
    raise AssertionError("il runner non doveva essere chiamato")


# ====================================================================== #
# 1. Rilevamento — parse_cmdline (puro, senza file)
# ====================================================================== #

class TestParseCmdline(unittest.TestCase):
    def test_parse_non_ostree(self):
        st = OstreeDeploymentManager.parse_cmdline(NON_OSTREE_CMDLINE)
        self.assertFalse(st.is_ostree)
        self.assertIsNone(st.booted_index)
        self.assertTrue(st.is_default_booted)  # niente da fare → inerte

    def test_parse_index_0(self):
        st = OstreeDeploymentManager.parse_cmdline(
            cmdline_booted(COMMIT_A, 0))
        self.assertTrue(st.is_ostree)
        self.assertEqual(st.booted_index, 0)
        self.assertTrue(st.is_default_booted)

    def test_parse_index_1(self):
        st = OstreeDeploymentManager.parse_cmdline(
            cmdline_booted(COMMIT_B, 1))
        self.assertTrue(st.is_ostree)
        self.assertEqual(st.booted_index, 1)
        self.assertFalse(st.is_default_booted)  # non-default → attivazione

    def test_parse_malformed_index(self):
        text = ("ostree=/ostree/boot.0/bazzite/%s/abc rhgb quiet"
                % COMMIT_B)
        st = OstreeDeploymentManager.parse_cmdline(text)
        self.assertTrue(st.is_ostree)
        self.assertIsNone(st.booted_index)      # ignoto → guard via status
        self.assertFalse(st.is_default_booted)  # ambiguità ≠ default

    def test_parse_hash_is_not_deployment_checksum(self):
        """Finding di campo (image-mode): il penultimo componente del path
        cmdline è la boot-dir/base key, MAI un checksum di deployment →
        booted_checksum resta None (mai derivato dal cmdline)."""
        st = OstreeDeploymentManager.parse_cmdline(
            cmdline_image_mode(COMMIT_X, 1))
        self.assertTrue(st.is_ostree)
        self.assertEqual(st.booted_index, 1)
        self.assertIsNone(st.booted_checksum)

    def test_parse_only_first_line(self):
        text = cmdline_booted(COMMIT_B, 1) + "\nseconda riga ostree=/x/0"
        st = OstreeDeploymentManager.parse_cmdline(text)
        self.assertEqual(st.booted_index, 1)

    def test_parse_ostree_token_without_path(self):
        st = OstreeDeploymentManager.parse_cmdline("BOOT_IMAGE=x ostree= rhgb")
        self.assertTrue(st.is_ostree)
        self.assertIsNone(st.booted_index)
        self.assertFalse(st.is_default_booted)


# ====================================================================== #
# 2. Inerzia del manager in mock/dry-run + detect_boot
# ====================================================================== #

class TestDetectBoot(unittest.TestCase):
    def test_detect_resolves_booted_from_status(self):
        """Finding di campo: hash cmdline (base key) ≠ checksum status → il
        booted (checksum + default-ness) viene da status `booted: true`."""
        deps = two_deps(booted_index=1)
        mgr = OstreeDeploymentManager(
            cmdline_reader=lambda: cmdline_image_mode(COMMIT_X, 1),
            status_runner=lambda cmd: status_ok(deps))
        st = mgr.detect_boot()
        self.assertIsInstance(st, OstreeBootState)
        self.assertTrue(st.is_ostree)
        self.assertEqual(st.booted_index, 1)          # index cmdline (sanity)
        self.assertEqual(st.booted_checksum, COMMIT_B)  # da status booted:true
        self.assertFalse(st.is_default_booted)         # posizione 1

    def test_detect_default_from_status_position(self):
        """is_default = posizione del booted:true == 0 (status), non index
        cmdline."""
        deps = two_deps(booted_index=0)
        mgr = OstreeDeploymentManager(
            cmdline_reader=lambda: cmdline_image_mode(COMMIT_X, 0),
            status_runner=lambda cmd: status_ok(deps))
        st = mgr.detect_boot()
        self.assertTrue(st.is_default_booted)
        self.assertEqual(st.booted_checksum, COMMIT_A)

    def test_detect_status_unreadable_fail_closed(self):
        """Status illeggibile → booted ignoto (checksum None, non-default):
        fail-closed, il guard/verify abortirà con messaggio chiaro."""
        mgr = OstreeDeploymentManager(
            cmdline_reader=lambda: cmdline_image_mode(COMMIT_X, 0),
            status_runner=lambda cmd: (1, "", "rpm-ostree: errore"))
        st = mgr.detect_boot()
        self.assertTrue(st.is_ostree)
        self.assertIsNone(st.booted_checksum)
        self.assertFalse(st.is_default_booted)

    def test_detect_and_verify_ignore_cmdline_hash(self):
        """Fixture REALE (finding): cmdline .../aaaa.../1, status
        [{bbbb, booted False}, {cccc, booted True}] → detect identifica
        cccc come booted, is_default corretto, verify passa: NESSUN
        confronto col hash cmdline."""
        deps = [dep(COMMIT_A), dep(COMMIT_B)]          # bbbb / cccc
        deps[1]["booted"] = True                       # cccc booted
        mgr = OstreeDeploymentManager(
            cmdline_reader=lambda: cmdline_image_mode(COMMIT_X, 1),
            status_runner=lambda cmd: status_ok(deps))
        st = mgr.detect_boot()
        self.assertEqual(st.booted_checksum, COMMIT_B)
        self.assertFalse(st.is_default_booted)
        ok, reason = mgr.verify_swap_preconditions(st)
        self.assertTrue(ok, reason)

    def test_detect_mock_inert(self):
        """mock=True → detect_boot è un no-op: il reader non viene mai
        chiamato (nemmeno per errore) e lo stato è 'non-ostree/inerte'."""
        mgr = OstreeDeploymentManager(mock=True, cmdline_reader=_raising)
        st = mgr.detect_boot()
        self.assertFalse(st.is_ostree)
        self.assertTrue(st.is_default_booted)

    def test_detect_dry_run_inert(self):
        mgr = OstreeDeploymentManager(dry_run=True, cmdline_reader=_raising)
        st = mgr.detect_boot()
        self.assertFalse(st.is_ostree)
        self.assertTrue(st.is_default_booted)

    def test_detect_reader_failure_is_inert(self):
        """Cmdline illeggibile (es. non-Linux) → trattato come non-ostree
        (inerte), mai fail-closed: un sistema senza /proc non è ostree."""
        def boom():
            raise OSError("no /proc")
        mgr = OstreeDeploymentManager(cmdline_reader=boom)
        st = mgr.detect_boot()
        self.assertFalse(st.is_ostree)
        self.assertTrue(st.is_default_booted)

    def test_inert_manager_never_calls_runners(self):
        """Mock/dry-run: nessun metodo tocca status/txn runner (no-op)."""
        mgr = OstreeDeploymentManager(mock=True, cmdline_reader=_raising,
                                      status_runner=_raising,
                                      txn_runner=_raising)
        self.assertIsNone(mgr.read_deployments())
        rc, _, _ = mgr.swap_default()
        self.assertEqual(rc, 0)
        rc, _, _ = mgr.restore_default()
        self.assertEqual(rc, 0)
        self.assertIsNone(mgr.current_default_checksum())
        ok, _ = mgr.verify_swap_preconditions(
            OstreeBootState(True, 1, COMMIT_B, False))
        self.assertFalse(ok)


# ====================================================================== #
# 3. rpm-ostree status --json (read-only) e guardie fail-closed
# ====================================================================== #

class TestReadDeployments(unittest.TestCase):
    def _mgr(self, status_runner):
        return OstreeDeploymentManager(status_runner=status_runner)

    def test_read_deployments_parses(self):
        deps = two_deps(booted_index=1)
        mgr = self._mgr(lambda cmd: status_ok(deps))
        out = mgr.read_deployments()
        self.assertIsNotNone(out)
        self.assertEqual(len(out), 2)
        self.assertIsInstance(out[0], DeploymentInfo)
        self.assertEqual(out[0].index, 0)
        self.assertEqual(out[0].checksum, COMMIT_A)
        self.assertFalse(out[0].booted)
        self.assertEqual(out[1].index, 1)
        self.assertEqual(out[1].checksum, COMMIT_B)
        self.assertTrue(out[1].booted)

    def test_read_deployments_error_none(self):
        mgr = self._mgr(lambda cmd: (1, "", "rpm-ostree: errore"))
        self.assertIsNone(mgr.read_deployments())

    def test_read_deployments_invalid_json_none(self):
        mgr = self._mgr(lambda cmd: (0, "non-json{{{", ""))
        self.assertIsNone(mgr.read_deployments())

    def test_read_deployments_empty_none(self):
        mgr = self._mgr(lambda cmd: status_ok([]))
        self.assertIsNone(mgr.read_deployments())

    def test_current_default_checksum(self):
        deps = two_deps(booted_index=1)  # default = primo (A)
        mgr = self._mgr(lambda cmd: status_ok(deps))
        self.assertEqual(mgr.current_default_checksum(), COMMIT_A)

    def test_current_default_checksum_none_on_error(self):
        mgr = self._mgr(lambda cmd: (1, "", "errore"))
        self.assertIsNone(mgr.current_default_checksum())


class TestSwapPreconditions(unittest.TestCase):
    """Guardia fail-closed: (ok, reason); nessun effetto collaterale."""

    def _mgr(self, deps):
        return OstreeDeploymentManager(
            status_runner=lambda cmd: status_ok(deps))

    def _state(self, index=1):
        return OstreeBootState(is_ostree=True, booted_index=index,
                               booted_checksum=None,
                               is_default_booted=(index == 0))

    def test_preconditions_ok_2_deployments(self):
        ok, reason = self._mgr(two_deps(booted_index=1)).verify_swap_preconditions(
            self._state())
        self.assertTrue(ok, reason)

    def test_preconditions_ok_with_unknown_index(self):
        """Index cmdline ignoto → nessuna sanity possibile, decide lo status
        (booted:true)."""
        st = OstreeBootState(True, None, COMMIT_B, False)
        ok, reason = self._mgr(two_deps(booted_index=1)).verify_swap_preconditions(st)
        self.assertTrue(ok, reason)

    def test_preconditions_fail_3_deployments(self):
        deps = two_deps(booted_index=1) + [dep(COMMIT_X)]
        ok, reason = self._mgr(deps).verify_swap_preconditions(self._state())
        self.assertFalse(ok)
        self.assertIn("3", reason)

    def test_preconditions_fail_single_deployment(self):
        ok, reason = self._mgr([dep(COMMIT_A, booted=True)]).verify_swap_preconditions(
            self._state())
        self.assertFalse(ok)
        self.assertIn("1", reason)

    def test_preconditions_fail_two_booted(self):
        deps = two_deps(booted_index=1)
        deps[0]["booted"] = True
        ok, reason = self._mgr(deps).verify_swap_preconditions(self._state())
        self.assertFalse(ok)
        self.assertIn("booted", reason)

    def test_preconditions_ok_vacuously_not_ostree(self):
        """Non-ostree → ok SENZA mai chiamare lo status runner (niente da
        verificare)."""
        st = OstreeBootState(False, None, None, True)
        mgr = OstreeDeploymentManager(status_runner=_raising)
        ok, _ = mgr.verify_swap_preconditions(st)
        self.assertTrue(ok)


# ====================================================================== #
# 4. swap/restore primitivi (rpm-ostree rollback via txn_runner finto)
# ====================================================================== #

class TestSwapRestore(unittest.TestCase):
    def _mgr(self, txn: _RecordingTxn):
        return OstreeDeploymentManager(txn_runner=txn)

    def test_swap_issues_rollback_once(self):
        txn = _RecordingTxn()
        rc, _, _ = self._mgr(txn).swap_default()
        self.assertEqual(rc, 0)
        self.assertEqual(txn.calls, [["rpm-ostree", "rollback"]])

    def test_restore_issues_rollback_once(self):
        txn = _RecordingTxn()
        rc, _, _ = self._mgr(txn).restore_default()
        self.assertEqual(rc, 0)
        self.assertEqual(txn.calls, [["rpm-ostree", "rollback"]])

    def test_swap_rc_failure_propagates(self):
        txn = _RecordingTxn(rc=1)
        rc, _, err = self._mgr(txn).swap_default()
        self.assertEqual(rc, 1)
        self.assertEqual(txn.calls, [["rpm-ostree", "rollback"]])


class _FakeTxnRunner:
    """Fake di run_command per _run_ostree_txn: risponde al singolo launch
    `systemd-run --wait` (nessun poll) o solleva (timeout del runner)."""

    def __init__(self, result=None, exc=None):
        self.result = result          # (rc, out, err) restituiti dal launch
        self.exc = exc                # eccezione da sollevare (es. TimeoutError)
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs))
        if self.exc is not None:
            raise self.exc
        return self.result


class TestOstreeTxnWait(unittest.TestCase):
    """MAJOR-2 — `_run_ostree_txn` delega l'attesa a `systemd-run --wait`:
    l'exit code dell'unità È quello del client (niente poll da sbagliare →
    un rollback fallito VELOCE non può più diventare un falso 0). Uccidere
    il client su timeout NON uccide la txn (mai cancellare, AGENTS.md).
    Runner finto: MAI systemd-run reali (regola C1)."""

    def _run(self, fake, timeout=30):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("buo.state.ostree.run_command", new=fake), \
                mock.patch("buo.state.ostree.log_dir", return_value=Path(tmp)):
            return _run_ostree_txn(
                ["rpm-ostree", "rollback"], "buo-ostree-test",
                timeout=timeout)

    def test_wait_collect_propagates_unit_exit_code(self):
        """Rollback fallito (rc=1) propagato com'è; launch con --wait e
        --collect e timeout del runner come deadline."""
        fake = _FakeTxnRunner(result=(1, "", "rpm-ostree: errore"))
        rc, _, err = self._run(fake, timeout=600)
        self.assertEqual(rc, 1)
        self.assertEqual(len(fake.calls), 1)   # nessun poll successivo
        cmd, kw = fake.calls[0]
        self.assertEqual(cmd[0], "systemd-run")
        self.assertIn("--wait", cmd)
        self.assertIn("--collect", cmd)
        self.assertIn("--unit=buo-ostree-test", cmd)
        self.assertEqual(cmd[-2:], ["rpm-ostree", "rollback"])
        self.assertEqual(kw["timeout"], 600)

    def test_wait_clean_exit_rc_zero(self):
        fake = _FakeTxnRunner(result=(0, "", ""))
        rc, _, _ = self._run(fake)
        self.assertEqual(rc, 0)

    def test_wait_timeout_returns_124_never_kills_unit(self):
        """Timeout del runner (il client systemd-run viene ucciso, l'unità
        NO) → rc=124 col messaggio di non uccidere la txn."""
        fake = _FakeTxnRunner(exc=TimeoutError("timeout dopo 600s"))
        rc, _, err = self._run(fake, timeout=600)
        self.assertEqual(rc, 124)
        self.assertIn("non ucciderla", err)
        self.assertEqual(len(fake.calls), 1)


# ====================================================================== #
# Helper orchestratore: manager reale con runner finti su stato isolato
# ====================================================================== #

class _OrchCase(unittest.TestCase):
    """Orchestratore reale (mock=False, dry_run=False) con manager ostree
    a runner finti. Lo stato BUO vive in una dir temporanea per test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def make_manager(self, cmdline_text, deployments, txn_rc=0):
        """Manager attivo su un 'mondo' finto condiviso (cmdline/status/
        txn): lo status muta col rollback come nel mondo reale."""
        world = _FakeWorld(deployments)
        world.rc = txn_rc
        mgr = OstreeDeploymentManager(
            mock=False, dry_run=False,
            cmdline_reader=lambda: cmdline_text,
            status_runner=world.status,
            txn_runner=world.txn,
        )
        mgr.world = world
        return mgr

    def make_orch(self, manager, interactive=False, cfg_data=None,
                  clear=True):
        cfg = BUOConfig(cfg_data or {})
        cfg.benchmark_enabled = False
        cfg.validation_stress_duration = 0
        orch = Orchestrator(config=cfg, mock=False, dry_run=False,
                            interactive=interactive, ostree=manager)
        if clear:
            orch.checkpoint.clear()
        return orch


# ====================================================================== #
# 5. Attivazione eager dell'orchestratore (_ensure_ostree_default)
# ====================================================================== #

class TestEnsureOstreeDefault(_OrchCase):
    def _non_default_env(self):
        return (cmdline_booted(COMMIT_B, 1), two_deps(booted_index=1))

    def test_activation_inert_when_run_cannot_reboot(self):
        """current=optimize (nessuna fase unlock/fix nel segmento) →
        nessuna chiamata, nessun marcatore, True."""
        text, deps = self._non_default_env()
        mgr = self.make_manager(text, deps)
        orch = self.make_orch(mgr)
        self.assertTrue(orch._ensure_ostree_default("optimize"))
        self.assertEqual(mgr.world.calls, [])
        self.assertFalse(orch.checkpoint.get("ostree_default_swapped", False))

    def test_activation_inert_on_default_boot(self):
        text = cmdline_booted(COMMIT_A, 0)
        mgr = self.make_manager(text, two_deps(booted_index=0))
        orch = self.make_orch(mgr)
        self.assertTrue(orch._ensure_ostree_default("unlock"))
        self.assertEqual(mgr.world.calls, [])
        self.assertFalse(orch.checkpoint.get("ostree_default_swapped", False))

    def test_activation_inert_non_ostree_no_warning(self):
        mgr = self.make_manager(NON_OSTREE_CMDLINE, [])
        orch = self.make_orch(mgr)
        with self.assertNoLogs("buo", level="WARNING"):
            self.assertTrue(orch._ensure_ostree_default("unlock"))
        self.assertEqual(mgr.world.calls, [])
        self.assertFalse(orch.checkpoint.get("ostree_default_swapped", False))

    def test_activation_swaps_once_on_non_default(self):
        text, deps = self._non_default_env()
        mgr = self.make_manager(text, deps)
        orch = self.make_orch(mgr)
        self.assertTrue(orch._ensure_ostree_default("unlock"))
        self.assertEqual(mgr.world.calls, [["rpm-ostree", "rollback"]])
        cp = orch.checkpoint
        self.assertTrue(cp.get("ostree_default_swapped"))
        self.assertEqual(cp.get("ostree_swap_target_checksum"), COMMIT_B)

    def test_activation_from_non_default_image_mode(self):
        """END-TO-END sulla forma REALE (finding 03/09): hash cmdline (base
        key) ≠ checksum status, booted:true non-default in posizione 1,
        index cmdline 1 → sanity ok → swap sul checksum del booted preso
        dallo status (MAI dal hash cmdline)."""
        text = cmdline_image_mode(COMMIT_X, 1)   # hash cmdline ≠ checksum
        mgr = self.make_manager(text, two_deps(booted_index=1))
        orch = self.make_orch(mgr)
        self.assertTrue(orch._ensure_ostree_default("unlock"))
        self.assertEqual(mgr.world.calls, [["rpm-ostree", "rollback"]])
        cp = orch.checkpoint
        self.assertTrue(cp.get("ostree_default_swapped"))
        self.assertEqual(cp.get("ostree_swap_target_checksum"), COMMIT_B)

    def test_activation_aborts_when_cmdline_index_contradicts_status(self):
        """Sanity pre-swap: cmdline indica la entry 0 (default) ma lo status
        segna come booted un deployment NON-default, senza swap in questo
        boot → incoerenza → abort fail-closed."""
        text = cmdline_booted(COMMIT_A, 0)
        mgr = self.make_manager(text, two_deps(booted_index=1))
        orch = self.make_orch(mgr)
        self.assertFalse(orch._ensure_ostree_default("unlock"))
        self.assertEqual(mgr.world.calls, [])   # nessuno swap

    def test_activation_skips_sanity_after_swap_marker(self):
        """Post-swap nello stesso boot (marcatore attivo): la posizione del
        booted è cambiata rispetto all'index cmdline → sanity SKIPPATA,
        nessun abort; il flag booted:true resta autorevole (ri-swap
        self-healing D8)."""
        text = cmdline_booted(COMMIT_A, 0)      # index cmdline di boot-time
        mgr = self.make_manager(text, two_deps(booted_index=1))
        orch = self.make_orch(mgr)
        orch.checkpoint.set("ostree_default_swapped", True)
        self.assertTrue(orch._ensure_ostree_default("unlock"))
        self.assertEqual(mgr.world.calls, [["rpm-ostree", "rollback"]])

    def test_activation_aborts_on_3_deployments(self):
        text, deps = self._non_default_env()
        deps = deps + [dep(COMMIT_X)]
        mgr = self.make_manager(text, deps)
        orch = self.make_orch(mgr)
        self.assertFalse(orch._ensure_ostree_default("unlock"))
        self.assertEqual(mgr.world.calls, [])
        self.assertFalse(orch.checkpoint.get("ostree_default_swapped", False))

    def test_activation_aborts_on_status_unreadable(self):
        text = cmdline_booted(COMMIT_B, 1)
        mgr = self.make_manager(text, [])
        mgr.world.fail_status = True
        orch = self.make_orch(mgr)
        self.assertFalse(orch._ensure_ostree_default("unlock"))
        self.assertEqual(mgr.world.calls, [])
        self.assertFalse(orch.checkpoint.get("ostree_default_swapped", False))

    def test_activation_aborts_on_rollback_failure(self):
        text, deps = self._non_default_env()
        mgr = self.make_manager(text, deps, txn_rc=1)
        orch = self.make_orch(mgr)
        self.assertFalse(orch._ensure_ostree_default("unlock"))
        self.assertEqual(len(mgr.world.calls), 1)
        # D8: marcatore scritto PRIMA del rollback → azzerato su fallimento
        self.assertFalse(orch.checkpoint.get("ostree_default_swapped", False))

    def test_activation_warns_when_flag_disabled(self):
        text, deps = self._non_default_env()
        mgr = self.make_manager(text, deps)
        orch = self.make_orch(mgr, cfg_data={"ostree": {"auto_swap_default": False}})
        with self.assertLogs("buo", level="WARNING") as cm:
            self.assertTrue(orch._ensure_ostree_default("unlock"))
        self.assertIn("auto-swap", "\n".join(cm.output))
        self.assertEqual(mgr.world.calls, [])  # kill-switch: nessuna chiamata
        self.assertFalse(orch.checkpoint.get("ostree_default_swapped", False))

    def test_no_double_swap_across_processes(self):
        """Processo 1 da deployment non-default → swap. Resume (NUOVO
        processo, cmdline ora /0 perché il default È il booted) → nessun
        secondo rollback; il marcatore sopravvive via state.json."""
        text1, deps1 = self._non_default_env()
        mgr1 = self.make_manager(text1, deps1)
        orch1 = self.make_orch(mgr1)
        self.assertTrue(orch1._ensure_ostree_default("unlock"))
        self.assertEqual(len(mgr1.world.calls), 1)

        # "Resume": altro processo, stesso stato su disco, booted = default
        text2 = cmdline_booted(COMMIT_B, 0)
        deps2 = two_deps(booted_index=0, first=COMMIT_B, second=COMMIT_A)
        mgr2 = self.make_manager(text2, deps2)
        orch2 = self.make_orch(mgr2, clear=False)  # stesso state.json su disco
        self.assertTrue(orch2.checkpoint.get("ostree_default_swapped"))
        self.assertTrue(orch2._ensure_ostree_default("unlock"))
        self.assertEqual(mgr2.world.calls, [])   # nessun secondo swap
        # il restore resta dovuto (marcatore intatto per _exit_ostree_cleanup)
        self.assertTrue(orch2.checkpoint.get("ostree_default_swapped"))

    def test_default_boot_with_cmdline_index_nonzero_is_inert(self):
        """MAJOR-1 regressione: l'index cmdline può non corrispondere alla
        posizione (booted sul DEFAULT con index cmdline ≠ 0). NESSUN abort
        né swap: is_default viene dallo STATUS (booted:true in posizione 0,
        checksum identici) → la run prosegue inerte."""
        text = cmdline_booted(COMMIT_A, 2)   # index cmdline 2, default pos. 0
        mgr = self.make_manager(text, two_deps(booted_index=0))
        orch = self.make_orch(mgr)
        self.assertTrue(orch._ensure_ostree_default("unlock"))
        self.assertEqual(mgr.world.calls, [])          # nessun rollback
        self.assertFalse(orch.checkpoint.get("ostree_default_swapped", False))

    def test_activation_aborts_when_runner_rc_none(self):
        """Runner rotto che ritorna rc=None → NON è un successo (nessuna
        coercizione a 0): abort fail-closed, marcatore azzerato (D8)."""
        text, deps = self._non_default_env()
        mgr = self.make_manager(text, deps, txn_rc=None)
        orch = self.make_orch(mgr)
        self.assertFalse(orch._ensure_ostree_default("unlock"))
        self.assertEqual(len(mgr.world.calls), 1)
        self.assertFalse(orch.checkpoint.get("ostree_default_swapped", False))


# ====================================================================== #
# 6. Restore nei path di uscita (_exit_ostree_cleanup + wiring in run())
# ====================================================================== #

class TestExitCleanup(_OrchCase):
    """Marcatore pre-impostato (run che ha swap-pato) → il cleanup ripristina
    il default originale se e solo se il default corrente è ancora il target."""

    # Stato post-swap: il default (deps[0]) è il deployment bootato B
    _SWAPPED_DEPS = staticmethod(lambda: two_deps(
        booted_index=0, first=COMMIT_B, second=COMMIT_A))

    def _swapped_orch(self, txn_rc=0):
        mgr = self.make_manager(cmdline_booted(COMMIT_B, 0),
                                self._SWAPPED_DEPS(), txn_rc=txn_rc)
        orch = self.make_orch(mgr)
        orch.checkpoint.set("ostree_default_swapped", True)
        orch.checkpoint.set("ostree_swap_target_checksum", COMMIT_B)
        return orch, mgr

    def test_restore_skipped_without_marker(self):
        mgr = self.make_manager(cmdline_booted(COMMIT_A, 0),
                                two_deps(booted_index=0))
        orch = self.make_orch(mgr)
        orch._exit_ostree_cleanup()
        self.assertEqual(mgr.world.calls, [])

    def test_restore_on_success(self):
        """Run completa (finalize) con marcatore → rollback di restore 1×,
        marcatore azzerato. E2E: attivazione (swap) + finalize (restore)."""
        text, deps = cmdline_booted(COMMIT_B, 1), two_deps(booted_index=1)
        mgr = self.make_manager(text, deps)
        orch = self.make_orch(mgr)
        orch._execute_phase = lambda phase: {}       # niente hardware reale
        orch.report.generate = lambda **kw: None     # report non necessario
        rc = orch.run(start_phase="unlock")
        self.assertEqual(rc, EXIT_SUCCESS)
        # swap (attivazione) + restore (finalize)
        self.assertEqual(mgr.world.calls,
                         [["rpm-ostree", "rollback"], ["rpm-ostree", "rollback"]])
        self.assertFalse(orch.checkpoint.get("ostree_default_swapped", False))

    def test_restore_on_safety_abort(self):
        text, deps = cmdline_booted(COMMIT_B, 1), two_deps(booted_index=1)
        mgr = self.make_manager(text, deps)
        orch = self.make_orch(mgr)

        def boom(phase):
            raise SafetyViolation("temp critica")
        orch._execute_phase = boom
        rc = orch.run(start_phase="unlock")
        self.assertEqual(rc, EXIT_SAFETY_VIOLATION)
        self.assertEqual(len(mgr.world.calls), 2)  # swap + restore
        self.assertFalse(orch.checkpoint.get("ostree_default_swapped", False))

    def test_restore_on_phase_error(self):
        text, deps = cmdline_booted(COMMIT_B, 1), two_deps(booted_index=1)
        mgr = self.make_manager(text, deps)
        orch = self.make_orch(mgr)

        def boom(phase):
            raise RuntimeError("fix fallito")
        orch._execute_phase = boom
        rc = orch.run(start_phase="unlock")
        self.assertEqual(rc, EXIT_ERROR)
        self.assertEqual(len(mgr.world.calls), 2)
        self.assertFalse(orch.checkpoint.get("ostree_default_swapped", False))

    def test_restore_on_keyboard_interrupt(self):
        text, deps = cmdline_booted(COMMIT_B, 1), two_deps(booted_index=1)
        mgr = self.make_manager(text, deps)
        orch = self.make_orch(mgr)

        def boom(phase):
            raise KeyboardInterrupt()
        orch._execute_phase = boom
        rc = orch.run(start_phase="unlock")
        self.assertEqual(rc, EXIT_SUCCESS)
        self.assertEqual(len(mgr.world.calls), 2)
        self.assertFalse(orch.checkpoint.get("ostree_default_swapped", False))

    def test_restore_on_fatal_exception(self):
        """Eccezione FUORI dal try interno (qui: report in _finalize) →
        except Exception fatale → restore."""
        text, deps = cmdline_booted(COMMIT_B, 1), two_deps(booted_index=1)
        mgr = self.make_manager(text, deps)
        orch = self.make_orch(mgr)
        orch._execute_phase = lambda phase: {}

        def boom(**kw):
            raise RuntimeError("report fallito")
        orch.report.generate = boom
        rc = orch.run(start_phase="unlock")
        self.assertEqual(rc, EXIT_ERROR)
        self.assertEqual(len(mgr.world.calls), 2)
        self.assertFalse(orch.checkpoint.get("ostree_default_swapped", False))

    def test_restore_on_interactive_decline(self):
        text, deps = cmdline_booted(COMMIT_B, 1), two_deps(booted_index=1)
        mgr = self.make_manager(text, deps)
        orch = self.make_orch(mgr, interactive=True)
        with unittest.mock.patch("builtins.input", return_value="n"):
            rc = orch.run(start_phase="unlock")
        self.assertEqual(rc, EXIT_SUCCESS)
        self.assertEqual(len(mgr.world.calls), 2)  # swap + restore
        self.assertFalse(orch.checkpoint.get("ostree_default_swapped", False))

    def test_restore_skipped_when_default_changed_externally(self):
        """Default cambiato a mano durante la run → nessun rollback alla
        cieca; marcatore azzerato + warning (il default resta com'è)."""
        orch, mgr = self._swapped_orch()
        mgr.world.deployments = [dep(COMMIT_X, booted=True), dep(COMMIT_B)]
        with self.assertLogs("buo", level="WARNING") as cm:
            orch._exit_ostree_cleanup()
        self.assertEqual(mgr.world.calls, [])
        self.assertFalse(orch.checkpoint.get("ostree_default_swapped", False))
        self.assertIn("esternamente", "\n".join(cm.output))

    def test_restore_retries_on_failure(self):
        """Restore fallito a fine run → errore chiaro, marcatore TENUTO
        (self-healing: il run successivo ritenta)."""
        orch, mgr = self._swapped_orch(txn_rc=1)
        orch._exit_ostree_cleanup()
        self.assertEqual(len(mgr.world.calls), 1)
        self.assertTrue(orch.checkpoint.get("ostree_default_swapped", False))

    def test_restore_deferred_when_status_unreadable(self):
        orch, mgr = self._swapped_orch()
        mgr.world.fail_status = True
        orch._exit_ostree_cleanup()
        self.assertEqual(mgr.world.calls, [])
        self.assertTrue(orch.checkpoint.get("ostree_default_swapped", False))

    def test_restore_deferred_on_3_deployments(self):
        orch, mgr = self._swapped_orch()
        mgr.world.deployments = [
            dep(COMMIT_B, booted=True), dep(COMMIT_A), dep(COMMIT_X)]
        orch._exit_ostree_cleanup()
        self.assertEqual(mgr.world.calls, [])
        self.assertTrue(orch.checkpoint.get("ostree_default_swapped", False))

    def test_exit_cleanup_idempotent(self):
        """Doppia chiamata al cleanup → un SOLO rollback (la prima azzera
        il marcatore)."""
        orch, mgr = self._swapped_orch()
        orch._exit_ostree_cleanup()
        orch._exit_ostree_cleanup()
        self.assertEqual(mgr.world.calls, [["rpm-ostree", "rollback"]])
        self.assertFalse(orch.checkpoint.get("ostree_default_swapped", False))


# ====================================================================== #
# 7. Inerzia orchestratore: mock/dry-run non toccano MAI i runner
# ====================================================================== #

class TestOrchestratorOstreeInertness(_OrchCase):
    def _active_raising_manager(self):
        """Manager ATTIVO con runner che esplodono se chiamati: la prova
        che l'inerzia sta nell'orchestratore, non nel manager."""
        return OstreeDeploymentManager(
            mock=False, dry_run=False,
            cmdline_reader=_raising, status_runner=_raising,
            txn_runner=_raising)

    def test_dry_run_run_no_ostree_activity(self):
        orch = Orchestrator(config=BUOConfig(), mock=True, dry_run=True,
                            ostree=self._active_raising_manager())
        orch.checkpoint.clear()
        rc = orch.run()
        self.assertEqual(rc, EXIT_SUCCESS)
        self.assertFalse(orch.checkpoint.get("ostree_default_swapped", False))

    def test_mock_run_no_ostree_activity(self):
        from buo.utils.mock import MockHardware
        cfg = BUOConfig()
        cfg.benchmark_enabled = False
        cfg.validation_stress_duration = 0
        orch = Orchestrator(config=cfg, mock=True, dry_run=False,
                            mock_hardware=MockHardware(seed=3),
                            ostree=self._active_raising_manager())
        orch.checkpoint.clear()
        rc = orch.run()
        self.assertEqual(rc, EXIT_SUCCESS)
        self.assertFalse(orch.checkpoint.get("ostree_default_swapped", False))


if __name__ == "__main__":
    unittest.main()
