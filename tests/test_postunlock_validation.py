#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Validazione post-unlock (design research/DESIGN_POSTUNLOCK_VALIDATION.md,
sezione 7): 15 scenari — mock, zero hardware, zero sleep reali (C1).

Copre: validatori CPU/GPU (unità), verdetto store, revert CPU, fase
`unlock_validate` dell'orchestratore (scenari 1-6), hook GPU post-enable
(scenari 7-10), gate verdetto (11), anti-loop (12), dry-run/mock senza
stato persistente (13), config (14), determinismo mock (15).
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from buo.config import BUOConfig
from buo.constants import CORE_MASK_STOCK, CORE_MASK_UNLOCKED
from buo.orchestrator import Orchestrator
from buo.unlock.cpu import CPUUnlock
from buo.unlock.validation import (CpuUnlockValidation, GpuUnlockValidation,
                                   UnlockVerdict, VERDICT_FILE, evidence,
                                   extra_threads, gpu_vkmark_cmd,
                                   gpu_vkmark_env, parse_cpu_list)
from buo.utils.mock import MockHardware

# results.tsv assente (macchina fresca): nessuna evidenza preesistente
TSV_ABSENT = {"stable": [], "defective": [], "total": 0,
              "complete": False, "present": False, "rows": 0}
# results.tsv completo (per-WGP già fatto)
TSV_COMPLETE = {"stable": list(range(40)), "defective": [], "total": 20,
                "complete": True, "present": True, "rows": 20}
# results.tsv parziale (maratona presidiata in corso)
TSV_PARTIAL = {"stable": [], "defective": [], "total": 3,
               "complete": False, "present": True, "rows": 3}


class _FakeHealth:
    """Stub di CUHealthTest: read_results programmabile; run() = la
    maratona per-WGP non deve MAI partire (AssertionError se chiamata)."""

    def __init__(self, payload):
        self.payload = payload

    def read_results(self):
        return self.payload

    def run(self):
        raise AssertionError("health run() avviata: la maratona per-WGP "
                             "non deve partire da una run non presidiata")


class _OrchCase(unittest.TestCase):
    """Base: orchestratore mock isolato (BUO_STATE_DIR in tmp)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def _make(self, dry_run=False, cpu_probe=True, gpu_probe=True):
        hw = MockHardware(seed=7)
        hw.state.is_acpi_fixed = True
        cfg = BUOConfig()
        cfg.validation_stress_duration = 0
        cfg.benchmark_enabled = False
        cfg.probe_cpu_unlock = cpu_probe
        cfg.probe_gpu_unlock = gpu_probe
        orch = Orchestrator(config=cfg, mock=True, dry_run=dry_run,
                            mock_hardware=hw)
        orch.checkpoint.clear()
        return orch, hw

    def _unlocked_cpu_state(self, hw):
        """16T con maschera 0xFF e unlock eseguito da questa run."""
        hw.state.core_mask = CORE_MASK_UNLOCKED
        hw.state.cpu_cores = 8
        hw.state.cpu_stable_cores = [0, 1, 2, 3, 4, 5, 6, 7]

    def _seed_unlock_validate(self, orch, hw):
        self._unlocked_cpu_state(hw)
        orch.checkpoint.set("applied_steps", ["cpu_core_unlock"])

    def _seed_verdict_file(self, unit, verdict):
        """Verdetto pre-esistente nel file (scenario gate)."""
        v = UnlockVerdict()  # path = state_dir()/unlock-verdict.json
        v.set(unit, verdict, evidence(cause="test"))
        return v


# ===================================================================== #
# 1. Validatori — unit (scenario 1, 2, 6 + determinismo 15)
# ===================================================================== #

class TestCpuValidationUnit(unittest.TestCase):
    def _hw(self, **kw):
        hw = MockHardware(seed=1)
        hw.state.cpu_cores = 8
        hw.state.core_mask = CORE_MASK_UNLOCKED
        for k, v in kw.items():
            setattr(hw.state, k, v)
        return hw

    def test_pass_when_healthy(self):
        """Mock sano → pass, nessun verdetto implicito."""
        r = CpuUnlockValidation(mock=True,
                                mock_hardware=self._hw()).run(60)
        self.assertEqual(r["outcome"], "pass")
        self.assertTrue(r.get("simulated"))

    def test_fail_when_extra_cores_defective(self):
        """cpu_unlock_ok=False (core extra difettosi) → fail stress."""
        r = CpuUnlockValidation(mock=True, mock_hardware=self._hw(
            cpu_unlock_ok=False)).run(60)
        self.assertEqual(r["outcome"], "fail")
        self.assertEqual(r["cause"], "stress")

    def test_fail_on_whea_delta(self):
        r = CpuUnlockValidation(mock=True, mock_hardware=self._hw(
            whea_delta=2)).run(60)
        self.assertEqual(r["outcome"], "fail")
        self.assertEqual(r["cause"], "whea")
        self.assertEqual(r["whea_delta"], 2)

    def test_inconclusive_thermal_hard(self):
        """Termico HARD → inconcluso (NON condanna, D4)."""
        from buo.constants import LIMITS
        r = CpuUnlockValidation(mock=True, mock_hardware=self._hw(
            unlock_validate_thermal=True)).run(60)
        self.assertEqual(r["outcome"], "inconclusive")
        self.assertEqual(r["cause"], "thermal")
        self.assertEqual(r["temp_max"], LIMITS.cpu.temp_max)

    def test_sim_without_mock_hw_passes_without_invented_values(self):
        """dry-run senza mock_hw → pass simulato con campi None (C1:
        mai valori inventati)."""
        r = CpuUnlockValidation(mock=True, dry_run=True).run(60)
        self.assertEqual(r["outcome"], "pass")
        self.assertIsNone(r["temp_max"])

    def test_no_real_sleep_in_sim(self):
        """Scenario 15: la validazione mock NON chiama MAI time.sleep."""
        with mock.patch("buo.unlock.validation.time.sleep",
                        side_effect=AssertionError("sleep reale nel mock")):
            r = CpuUnlockValidation(mock=True, mock_hardware=self._hw()).run(60)
        self.assertEqual(r["outcome"], "pass")

    def test_m3_tool_missing_inconclusive(self):
        """MINOR m3: stress-ng/taskset ASSENTI (FileNotFoundError) =
        problema ambientale → inconcluso tool_missing (revert SENZA
        condanna), simmetrico al tool GPU assente."""
        with mock.patch("buo.unlock.validation.extra_threads",
                        return_value=[12, 13, 14, 15]), \
             mock.patch("buo.unlock.validation.subprocess.Popen",
                        side_effect=FileNotFoundError()), \
             mock.patch("buo.unlock.validation.run_command",
                        return_value=(0, "", "")):
            v = CpuUnlockValidation(mock=False, dry_run=False)
            r = v.run(60)
        self.assertEqual(r["outcome"], "inconclusive")
        self.assertEqual(r["cause"], "tool_missing")


class TestGpuValidationUnit(unittest.TestCase):
    def _hw(self, **kw):
        hw = MockHardware(seed=1)
        hw.state.gpu_cu_count = 40
        hw.state.is_40cu_enabled = True
        for k, v in kw.items():
            setattr(hw.state, k, v)
        return hw

    def test_pass_when_healthy(self):
        r = GpuUnlockValidation(mock=True, mock_hardware=self._hw()).run(60)
        self.assertEqual(r["outcome"], "pass")

    def test_fail_on_gpu_fault(self):
        r = GpuUnlockValidation(mock=True, mock_hardware=self._hw(
            gpu_unlock_ok=False)).run(60)
        self.assertEqual(r["outcome"], "fail")
        self.assertEqual(r["cause"], "gpu_fault")

    def test_fail_on_fault_lines(self):
        r = GpuUnlockValidation(mock=True, mock_hardware=self._hw(
            gpu_fault_lines=1)).run(60)
        self.assertEqual(r["outcome"], "fail")
        self.assertEqual(r["cause"], "gpu_fault")

    def test_fail_on_whea(self):
        r = GpuUnlockValidation(mock=True, mock_hardware=self._hw(
            whea_delta=1)).run(60)
        self.assertEqual(r["outcome"], "fail")
        self.assertEqual(r["cause"], "whea")

    def test_inconclusive_thermal(self):
        from buo.constants import LIMITS
        r = GpuUnlockValidation(mock=True, mock_hardware=self._hw(
            unlock_validate_thermal=True)).run(60)
        self.assertEqual(r["outcome"], "inconclusive")
        self.assertEqual(r["cause"], "thermal")
        self.assertEqual(r["temp_max"], LIMITS.gpu.temp_max)

    def test_tool_missing_inconclusive(self):
        """Scenario 10: vkmark assente → inconcluso tool_missing (nessun
        verdetto durevole a valle)."""
        v = GpuUnlockValidation(mock=True, mock_hardware=self._hw())
        with mock.patch.object(v, "tool_available", return_value=False):
            r = v.run(60)
        self.assertEqual(r["outcome"], "inconclusive")
        self.assertEqual(r["cause"], "tool_missing")

    def test_no_real_sleep_in_sim(self):
        with mock.patch("buo.unlock.validation.time.sleep",
                        side_effect=AssertionError("sleep reale nel mock")):
            r = GpuUnlockValidation(mock=True,
                                    mock_hardware=self._hw()).run(60)
        self.assertEqual(r["outcome"], "pass")


# ===================================================================== #
# 2. Verdetto store + helper
# ===================================================================== #

class TestUnlockVerdict(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def _path(self):
        return Path(self._tmp.name) / VERDICT_FILE

    def test_default_empty_no_veto(self):
        v = UnlockVerdict(self._path())
        self.assertIsNone(v.get("cpu"))
        self.assertIsNone(v.get("gpu"))

    def test_set_and_reload(self):
        path = self._path()
        v = UnlockVerdict(path)
        v.set("cpu", "never_unlock", evidence(cause="whea", threads=16))
        v2 = UnlockVerdict(path)  # nuova istanza: legge dal file
        self.assertEqual(v2.get("cpu"), "never_unlock")
        self.assertEqual(v2._data["cpu"]["evidence"]["cause"], "whea")

    def test_gpu_verdicts(self):
        path = self._path()
        v = UnlockVerdict(path)
        v.set("gpu", "stable_short", evidence(seconds=60))
        v2 = UnlockVerdict(path)
        self.assertEqual(v2.get("gpu"), "stable_short")

    def test_corrupt_file_no_veto(self):
        path = self._path()
        path.write_text("{non-json", encoding="utf-8")
        v = UnlockVerdict(path)
        self.assertIsNone(v.get("cpu"))

    def test_sim_mode_never_writes_file(self):
        """M2/scenario 13: in sim il set aggiorna la memoria ma NON
        scrive il file."""
        path = self._path()
        v = UnlockVerdict(path, sim=True)
        v.set("cpu", "never_unlock", evidence(cause="hang"))
        self.assertEqual(v.get("cpu"), "never_unlock")
        self.assertFalse(path.exists())


class TestHelpersValidation(unittest.TestCase):
    def test_parse_cpu_list(self):
        self.assertEqual(parse_cpu_list("0-3,7"), [0, 1, 2, 3, 7])
        self.assertEqual(parse_cpu_list("0-15"), list(range(16)))

    def test_extra_threads_from_online_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "online"
            f.write_text("0-15", encoding="utf-8")
            self.assertEqual(extra_threads(str(f)), [12, 13, 14, 15])
            f.write_text("0-11", encoding="utf-8")
            self.assertEqual(extra_threads(str(f)), [])

    def test_extra_threads_missing_file(self):
        self.assertEqual(extra_threads("/nonexistent/online"), [])

    def test_gpu_vkmark_cmd_and_env(self):
        self.assertEqual(gpu_vkmark_cmd(60),
                         ["vkmark", "-b", "desktop:duration=60",
                          "--size", "1920x1080"])
        env = gpu_vkmark_env()
        # nessun valore inventato: chiavi solo se reali sulla macchina
        for key in ("DISPLAY", "XDG_RUNTIME_DIR", "XAUTHORITY",
                    "VK_ICD_FILENAMES"):
            if key in env:
                self.assertTrue(env[key])


# ===================================================================== #
# 3. Revert CPU (D5, meccanica verificata sul sorgente pinnato)
# ===================================================================== #

class TestCpuRevertToStock(unittest.TestCase):
    def test_mock_revert_writes_0x77_and_readback_ok(self):
        hw = MockHardware(seed=1)
        hw.state.core_mask = CORE_MASK_UNLOCKED
        hw.state.cpu_cores = 8
        cu = CPUUnlock(mock=True, mock_hardware=hw)
        r = cu.revert_to_stock()
        self.assertTrue(r["reverted"])
        self.assertEqual(hw.state.core_mask, CORE_MASK_STOCK)
        self.assertEqual(hw.state.cpu_cores, 6)

    def test_mock_revert_already_stock(self):
        hw = MockHardware(seed=1)  # core_mask stock di default
        cu = CPUUnlock(mock=True, mock_hardware=hw)
        r = cu.revert_to_stock()
        self.assertTrue(r["reverted"])
        self.assertTrue(r.get("already", False))

    def test_mock_revert_failure_propagates(self):
        """Scrittura fallita (mock senza hardware) → reverted=False."""
        cu = CPUUnlock(mock=True, mock_hardware=None)
        r = cu.revert_to_stock()
        self.assertFalse(r["reverted"])

    def test_no_pci_never_reboots(self):
        """Senza PCI config space il revert fallisce in modo pulito
        (il chiamante NON deve riavviare: cold boot = stock)."""
        with mock.patch("buo.unlock.cpu.os.path.exists",
                        return_value=False):
            cu = CPUUnlock(mock=False)
            r = cu.revert_to_stock()
        self.assertFalse(r["reverted"])
        self.assertIn("cold boot", r["error"])


# ===================================================================== #
# 4. Fase unlock_validate — scenari 1-6
# ===================================================================== #

class TestGovernorSmuGate(unittest.TestCase):
    """MINOR m4: accessi SMN/SMU solo con governor FERMO CONFERMATO
    (regola assoluta AGENTS — freeze SoC con accessi concorrenti). Abort
    fail-closed se lo stato non è confermato fermo; letture con
    try/except (None, mai assumere stock)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def _real_orch(self):
        orch = Orchestrator(config=BUOConfig(), mock=False, dry_run=False)
        orch.checkpoint.clear()
        return orch

    def test_revert_aborts_when_governor_not_confirmed_stopped(self):
        """Governor attivo ma stop FALLITO → il revert NON parte (mai
        scrivere SMN a governor attivo)."""
        orch = self._real_orch()
        fake = mock.Mock()
        fake.is_running.return_value = True
        fake.stop.return_value = False  # stop non confermato
        orch.governor = fake
        with mock.patch.object(
                orch.cpu_unlock, "revert_to_stock",
                side_effect=AssertionError(
                    "revert non deve partire a governor non fermo")):
            with self.assertRaises(RuntimeError):
                orch._cpu_revert_and_reboot({"x": 1}, condemn=False)

    def test_revert_stops_and_restarts_governor_when_active(self):
        """Governor attivo + stop confermato → revert eseguito e governor
        riavviato a fine accesso."""
        orch = self._real_orch()
        fake = mock.Mock()
        fake.is_running.return_value = True
        fake.stop.return_value = True
        orch.governor = fake
        with mock.patch.object(orch, "_schedule_reboot") as sched, \
             mock.patch.object(orch.cpu_unlock, "revert_to_stock",
                               return_value={"reverted": True,
                                             "mask": "0x77"}):
            orch._cpu_revert_and_reboot({"x": 1}, condemn=True)
        fake.stop.assert_called_once()
        fake.start.assert_called_once()
        sched.assert_called_once()

    def test_read_mask_aborts_when_governor_state_unknown(self):
        orch = self._real_orch()
        fake = mock.Mock()
        fake.is_running.side_effect = OSError("systemctl boom")
        orch.governor = fake
        with mock.patch.object(orch.cpu_unlock, "read_core_mask",
                               side_effect=AssertionError(
                                   "nessuna lettura senza stato governor")):
            with self.assertRaises(RuntimeError):
                orch._cpu_read_mask()

    def test_read_mask_oserror_returns_none(self):
        """Lettura SMN fallita → None (M4b: mai crashare la fase, mai
        assumere stock da una lettura fallita)."""
        orch = self._real_orch()
        fake = mock.Mock()
        fake.is_running.return_value = False  # governor già fermo
        orch.governor = fake
        with mock.patch.object(orch.cpu_unlock, "read_core_mask",
                               side_effect=OSError("pci error")):
            self.assertIsNone(orch._cpu_read_mask())
        fake.start.assert_not_called()

    def test_read_mask_gated_by_governor_pause(self):
        """Lettura con governor attivo → stop/read/start attorno alla
        lettura SMN."""
        orch = self._real_orch()
        fake = mock.Mock()
        fake.is_running.return_value = True
        fake.stop.return_value = True
        orch.governor = fake
        with mock.patch.object(orch.cpu_unlock, "read_core_mask",
                               return_value=0xFF) as spy:
            m = orch._cpu_read_mask()
        self.assertEqual(m, 0xFF)
        fake.stop.assert_called_once()
        fake.start.assert_called_once()
        spy.assert_called_once()

    def test_governor_not_running_no_stop_needed(self):
        orch = self._real_orch()
        fake = mock.Mock()
        fake.is_running.return_value = False
        orch.governor = fake
        with mock.patch.object(orch, "_schedule_reboot"), \
             mock.patch.object(orch.cpu_unlock, "revert_to_stock",
                               return_value={"reverted": True,
                                             "mask": "0x77"}):
            orch._cpu_revert_and_reboot({"x": 1}, condemn=True)
        fake.stop.assert_not_called()
        fake.start.assert_not_called()


class TestPhaseUnlockValidate(_OrchCase):
    def test_scenario1_pass_no_verdict_no_second_reboot(self):
        """CPU validata (mock sano): nessun verdetto scritto, nessun
        reboot, fase completata con esito ok."""
        orch, hw = self._make()
        self._seed_unlock_validate(orch, hw)
        data = orch._phase_unlock_validate()
        self.assertEqual(data["cpu"]["outcome"], "pass")
        self.assertEqual(orch.results["unlock_validation"]["cpu"]
                         ["outcome"], "pass")
        self.assertIsNone(orch.unlock_verdict.get("cpu"))
        self.assertEqual(orch.checkpoint.get_reboot_count(), 0)
        self.assertEqual(orch.hardware.state.core_mask, CORE_MASK_UNLOCKED)

    def test_scenario2_defective_cores_condemn_and_revert(self):
        """Core extra difettosi → verdetto never_unlock, revert (mock:
        maschera → 0x77), UN reboot programmato, nessuna ri-esecuzione."""
        orch, hw = self._make()
        self._seed_unlock_validate(orch, hw)
        hw.state.cpu_unlock_ok = False
        with mock.patch.object(orch.cpu_validation, "run",
                               wraps=orch.cpu_validation.run) as spy:
            data = orch._phase_unlock_validate()
        spy.assert_called_once()  # una sola esecuzione
        self.assertEqual(data["cpu"]["cause"], "stress")
        self.assertEqual(orch.unlock_verdict.get("cpu"), "never_unlock")
        self.assertEqual(orch.hardware.state.core_mask, CORE_MASK_STOCK,
                         "revert: maschera tornata a 0x77")
        self.assertEqual(orch.checkpoint.get_reboot_count(), 1)
        self.assertTrue(orch.results["unlock_validation"]["cpu"]
                        .get("reverted"))

    def test_scenario3_resume_after_revert_skips_all(self):
        """Verdetto presente + maschera GIÀ stock (revert riuscito al run
        precedente) → la fase salta tutto (niente ri-valida, niente
        ri-revert, niente reboot)."""
        orch, hw = self._make()
        hw.state.core_mask = CORE_MASK_STOCK  # revert già avvenuto
        hw.state.cpu_cores = 6
        orch.checkpoint.set("applied_steps", ["cpu_core_unlock"])
        self._seed_verdict_file("cpu", "never_unlock")
        orch.unlock_verdict._load()  # ricarica il file appena scritto
        with mock.patch.object(orch.cpu_validation, "run",
                               side_effect=AssertionError(
                                   "nessuna validazione col verdetto")), \
             mock.patch.object(orch, "_schedule_reboot",
                               side_effect=AssertionError(
                                   "nessun reboot col verdetto a stock")):
            data = orch._phase_unlock_validate()
        self.assertTrue(data["cpu"].get("verdict_blocked"))
        self.assertEqual(orch.hardware.state.core_mask, CORE_MASK_STOCK,
                         "nessun revert: la maschera è già stock")
        self.assertEqual(orch.checkpoint.get_reboot_count(), 0)

    def test_scenario3b_m1_verdict_with_mask_0xff_reverts(self):
        """MAJOR M1: verdetto never_unlock presente MA maschera ancora
        0xFF (kill tra verdetto e scrittura 0x77) → la fase REVERTE e
        programma il reboot — mai proseguire con 16T su silicio
        condannato (0xFF sopravvive ai warm reboot)."""
        orch, hw = self._make()
        self._seed_unlock_validate(orch, hw)  # maschera 0xFF, 16T
        self._seed_verdict_file("cpu", "never_unlock")
        orch.unlock_verdict._load()
        with mock.patch.object(orch.cpu_validation, "run",
                               side_effect=AssertionError(
                                   "verdetto: nessuna validazione")):
            data = orch._phase_unlock_validate()
        self.assertTrue(data["cpu"].get("verdict_blocked"))
        self.assertTrue(data["cpu"].get("reverted"))
        self.assertEqual(orch.hardware.state.core_mask, CORE_MASK_STOCK,
                         "revert obbligato: maschera tornata a 0x77")
        self.assertEqual(orch.checkpoint.get_reboot_count(), 1)
        # verdetto NON riscritto (già presente): niente ri-condanna
        self.assertEqual(orch.unlock_verdict.get("cpu"), "never_unlock")

    def test_scenario3c_m1_verdict_mask_0xff_in_unlock_gate(self):
        """MAJOR M1 anche nel gate di _phase_unlock: verdetto + maschera
        0xFF → unlock saltato E revert + reboot."""
        orch, hw = self._make(gpu_probe=False)
        orch.checkpoint.clear()
        hw.state.core_mask = CORE_MASK_UNLOCKED
        hw.state.cpu_cores = 8
        self._seed_verdict_file("cpu", "never_unlock")
        orch.unlock_verdict._load()
        out = orch._phase_unlock()
        self.assertTrue(out["cpu"].get("verdict_blocked"))
        self.assertNotIn("cpu_core_unlock", orch._applied_steps())
        self.assertEqual(orch.hardware.state.core_mask, CORE_MASK_STOCK,
                         "revert nel gate: maschera tornata a 0x77")
        self.assertEqual(orch.checkpoint.get_reboot_count(), 1)

    def test_scenario4_hang_stale_marker(self):
        """Marcatore STALE (epoch < boot) = hang: verdetto + revert +
        reboot SENZA rieseguire lo stress."""
        orch, hw = self._make()
        self._seed_unlock_validate(orch, hw)
        orch.checkpoint.set("unlock_cpu_validate_marker",
                            {"started_epoch": 1})  # ante-boot
        with mock.patch.object(orch.cpu_validation, "run",
                               side_effect=AssertionError(
                                   "hang: lo stress NON va rieseguito")):
            data = orch._phase_unlock_validate()
        self.assertTrue(data["cpu"].get("hang"))
        self.assertEqual(orch.unlock_verdict.get("cpu"), "never_unlock")
        self.assertEqual(orch.hardware.state.core_mask, CORE_MASK_STOCK)
        self.assertEqual(orch.checkpoint.get_reboot_count(), 1)

    def test_scenario5_fresh_marker_reruns_within_attempts(self):
        """Marcatore FRESCO (stesso boot): ri-esecuzione della
        validazione."""
        orch, hw = self._make()
        self._seed_unlock_validate(orch, hw)
        orch.checkpoint.set("unlock_cpu_validate_marker",
                            {"started_epoch": 2 ** 40})  # >= boot
        with mock.patch.object(orch.cpu_validation, "run",
                               wraps=orch.cpu_validation.run) as spy:
            data = orch._phase_unlock_validate()
        spy.assert_called_once()
        self.assertEqual(data["cpu"]["outcome"], "pass")

    def test_scenario5b_attempts_exhausted_inconclusive(self):
        """3° tentativo (attempts >= 2) con marcatore fresco → inconcluso:
        revert SENZA condanna, nessuno stress."""
        orch, hw = self._make()
        self._seed_unlock_validate(orch, hw)
        orch.checkpoint.set("unlock_cpu_validate_marker",
                            {"started_epoch": 2 ** 40})
        orch.checkpoint.set("unlock_cpu_validate_attempts", 2)
        with mock.patch.object(orch.cpu_validation, "run",
                               side_effect=AssertionError(
                                   "attempts esauriti: nessuno stress")):
            data = orch._phase_unlock_validate()
        self.assertTrue(data["cpu"].get("inconclusive"))
        self.assertIsNone(orch.unlock_verdict.get("cpu"),
                          "inconcluso: NESSUN verdetto durevole")
        self.assertEqual(orch.hardware.state.core_mask, CORE_MASK_STOCK)
        self.assertEqual(orch.checkpoint.get_reboot_count(), 1)

    def test_scenario6_thermal_inconclusive_revert_no_verdict(self):
        """Termico HARD → inconcluso: revert, NESSUN verdetto, nota."""
        orch, hw = self._make()
        self._seed_unlock_validate(orch, hw)
        hw.state.unlock_validate_thermal = True
        data = orch._phase_unlock_validate()
        self.assertTrue(data["cpu"].get("inconclusive"))
        self.assertEqual(data["cpu"]["cause"], "thermal")
        self.assertIsNone(orch.unlock_verdict.get("cpu"))
        self.assertEqual(orch.hardware.state.core_mask, CORE_MASK_STOCK)
        self.assertEqual(orch.checkpoint.get_reboot_count(), 1)
        self.assertTrue(any("non conclusa" in n
                            for n in orch.results["notes"]))

    def test_skip_when_unlock_not_in_ledger(self):
        """Niente da validare se l'unlock non è di questa run (es. 8 core
        già da BIOS/DXE)."""
        orch, hw = self._make()
        self._unlocked_cpu_state(hw)  # 16T ma NESSUN unlock nel ledger
        with mock.patch.object(orch.cpu_validation, "run",
                               side_effect=AssertionError(
                                   "niente da validare")):
            data = orch._phase_unlock_validate()
        self.assertEqual(data["cpu"]["skipped"], "no_unlock_this_run")

    def test_skip_when_mask_already_stock(self):
        """Maschera 0x77 (revert già avvenuto) → niente da validare."""
        orch, hw = self._make()
        hw.state.core_mask = CORE_MASK_STOCK
        hw.state.cpu_cores = 6
        orch.checkpoint.set("applied_steps", ["cpu_core_unlock"])
        with mock.patch.object(orch.cpu_validation, "run",
                               side_effect=AssertionError(
                                   "maschera stock: niente da validare")):
            data = orch._phase_unlock_validate()
        self.assertEqual(data["cpu"]["skipped"], "mask_stock")

    def test_revert_write_failure_never_reboots(self):
        """D5: revert impossibile (readback != 0x77) → NIENTE reboot:
        interruzione controllata (RuntimeError), maschera invariata."""
        orch, hw = self._make()
        self._seed_unlock_validate(orch, hw)
        hw.state.cpu_unlock_ok = False
        with mock.patch.object(
                orch.cpu_unlock, "revert_to_stock",
                return_value={"reverted": False, "mask": "0xff",
                              "error": "readback != 0x77"}):
            with self.assertRaises(RuntimeError):
                orch._phase_unlock_validate()
        self.assertEqual(orch.checkpoint.get_reboot_count(), 0,
                         "nessun reboot se la scrittura 0x77 fallisce")
        self.assertEqual(orch.unlock_verdict.get("cpu"), "never_unlock",
                         "verdetto comunque scritto (D5)")
        self.assertTrue(any("POWER-OFF" in n
                            for n in orch.results["notes"]))

    def test_disable_core_unlock_boot_service(self):
        """Touchpoint esterno D6: systemctl disable
        bc250-core-unlock.service (best-effort, run reali)."""
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return 0, "", ""

        orch = Orchestrator(config=BUOConfig(), mock=False, dry_run=False)
        with mock.patch("buo.utils.shell.run_command", side_effect=fake_run):
            orch._disable_core_unlock_boot()
        self.assertTrue(any(cmd == ["systemctl", "disable",
                                    "bc250-core-unlock.service"]
                            for cmd in calls))


# ===================================================================== #
# 5. Hook GPU post-enable — scenari 7-10
# ===================================================================== #

class TestPhaseUnlockGpuValidation(_OrchCase):
    def _umr_apply(self, applied=True):
        return {"applied": applied, "cu_count": 40,
                "needs_reboot": False, "method": "runtime_umr"}

    def _fresh(self, orch, hw, tsv=TSV_ABSENT):
        """Macchina fresca: 24 CU stock, health con la tsv data."""
        orch.health_test = _FakeHealth(tsv)
        return orch, hw

    def test_scenario7_gpu_pass_persist_gated_ok(self):
        """GPU validata (results.tsv assente, mock sano): validation
        pass, verdetto stable_short, persistenza invocata (D9 ok)."""
        orch, hw = self._make(cpu_probe=False)
        self._fresh(orch, hw)
        with mock.patch.object(orch.gpu_unlock, "apply",
                               return_value=self._umr_apply()) as apply_spy:
            out = orch._phase_unlock()
        self.assertEqual(out["gpu"]["validation"]["outcome"], "pass")
        self.assertEqual(orch.unlock_verdict.get("gpu"), "stable_short")
        persist = out["gpu"].get("persistence", {})
        self.assertTrue(persist.get("suggested", False)
                        or persist.get("persisted", False),
                        "persistenza invocata col gate D9 ok")
        self.assertIn("gpu_40cu", orch._applied_steps())

    def test_scenario8_gpu_fail_stock_and_condemn(self):
        """GPU NON validata (gpu_unlock_ok=False): stock dispatch +
        verdetto never_enable_all + persistenza NON invocata."""
        orch, hw = self._make(cpu_probe=False)
        self._fresh(orch, hw)
        hw.state.gpu_unlock_ok = False
        with mock.patch.object(orch.gpu_unlock, "apply",
                               return_value=self._umr_apply()), \
             mock.patch.object(orch, "_disable_40cu_persistence") as disp:
            out = orch._phase_unlock()
        self.assertEqual(out["gpu"]["validation"]["outcome"], "fail")
        self.assertEqual(out["gpu"]["validation"]["cause"], "gpu_fault")
        self.assertEqual(orch.unlock_verdict.get("gpu"),
                         "never_enable_all")
        self.assertTrue(out["gpu"].get("rollback"))
        self.assertEqual(orch.hardware.state.gpu_cu_count, 24,
                         "stock dispatch: 24 CU")
        self.assertNotIn("persistence", out["gpu"])
        disp.assert_called_once()

    def test_scenario9_tsv_complete_skip_validation_persist_ok(self):
        """results.tsv COMPLETO → validazione saltata (evidenza per-WGP),
        persistenza ok (D9 ramo b)."""
        orch, hw = self._make(cpu_probe=False)
        self._fresh(orch, hw, tsv=TSV_COMPLETE)
        with mock.patch.object(orch.gpu_validation, "run",
                               side_effect=AssertionError(
                                   "results.tsv completo: nessuna "
                                   "validazione short")), \
             mock.patch.object(orch.gpu_unlock, "apply",
                               return_value=self._umr_apply()):
            out = orch._phase_unlock()
        self.assertNotIn("validation", out["gpu"])
        self.assertTrue(out["gpu"]["persistence"]["suggested"])

    def test_scenario10_tool_missing_inconclusive_stock_no_verdict(self):
        """vkmark assente → inconcluso: stock dispatch + ricetta, NESSUN
        verdetto durevole, persistenza non invocata."""
        orch, hw = self._make(cpu_probe=False)
        self._fresh(orch, hw)
        with mock.patch.object(orch.gpu_validation, "tool_available",
                               return_value=False), \
             mock.patch.object(orch.gpu_unlock, "apply",
                               return_value=self._umr_apply()):
            out = orch._phase_unlock()
        self.assertEqual(out["gpu"]["validation"]["outcome"],
                         "inconclusive")
        self.assertEqual(out["gpu"]["validation"]["cause"], "tool_missing")
        self.assertIsNone(orch.unlock_verdict.get("gpu"))
        self.assertTrue(out["gpu"].get("rollback"))
        self.assertTrue(any("vkmark" in n for n in orch.results["notes"]))

    def test_gpu_hang_stale_marker_condemns_without_rerun(self):
        """D7 GPU: marcatore STALE (macchina ripartita durante la
        validazione) → verdetto never_enable_all + stock dispatch, SENZA
        rieseguire la validazione né re-enable."""
        orch, hw = self._make(cpu_probe=False)
        orch.health_test = _FakeHealth(TSV_ABSENT)
        orch.checkpoint.set("applied_steps", ["gpu_40cu"])
        orch.checkpoint.set("unlock_gpu_validate_marker",
                            {"started_epoch": 1})  # ante-boot
        with mock.patch.object(orch.gpu_unlock, "apply",
                               side_effect=AssertionError(
                                   "hang: nessun re-enable")), \
             mock.patch.object(orch.gpu_validation, "run",
                               side_effect=AssertionError(
                                   "hang: nessuna ri-validazione")):
            out = orch._phase_unlock()
        self.assertEqual(out["gpu"]["validation"]["cause"], "hang")
        self.assertEqual(orch.unlock_verdict.get("gpu"),
                         "never_enable_all")
        self.assertTrue(out["gpu"].get("rollback"))

    def test_gpu_already_active_uncertified_validates_current_state(self):
        """4.1: 40-CU già attive (persistenza legacy) ma NON certificate
        e results.tsv assente → validazione sullo stato corrente; fail →
        stock dispatch + verdetto, NESSUN re-enable."""
        orch, hw = self._make(cpu_probe=False)
        hw.state.gpu_cu_count = 40
        hw.state.is_40cu_enabled = True
        orch.health_test = _FakeHealth(TSV_ABSENT)
        hw.state.gpu_unlock_ok = False
        with mock.patch.object(orch.gpu_unlock, "apply",
                               return_value=self._umr_apply()), \
             mock.patch.object(orch, "_disable_40cu_persistence") as disp:
            out = orch._phase_unlock()
        self.assertEqual(out["gpu"]["validation"]["outcome"], "fail")
        self.assertEqual(orch.unlock_verdict.get("gpu"),
                         "never_enable_all")
        self.assertEqual(orch.hardware.state.gpu_cu_count, 24,
                         "stock dispatch sullo stato corrente")
        self.assertNotIn("gpu_40cu", orch._applied_steps())
        disp.assert_called_once()

    def test_m1_fresh_marker_in_ledger_revalidates_current_state(self):
        """MINOR m1: marcatore GPU FRESCO (SIGKILL stesso boot) +
        gpu_40cu in ledger + 40-CU attive non certificate → ri-verifica
        dello stato corrente (D8) invece del solo skip (mai lasciare le
        40-CU attive non certificate per il resto della run)."""
        orch, hw = self._make(cpu_probe=False)
        hw.state.gpu_cu_count = 40
        hw.state.is_40cu_enabled = True
        orch.checkpoint.set("applied_steps", ["gpu_40cu"])
        orch.checkpoint.set("unlock_gpu_validate_marker",
                            {"started_epoch": 2 ** 40})  # fresco
        orch.health_test = _FakeHealth(TSV_ABSENT)
        with mock.patch.object(orch.gpu_unlock, "apply",
                               side_effect=AssertionError(
                                   "già in ledger: nessun enable_all")):
            out = orch._phase_unlock()
        self.assertEqual(out["gpu"]["validation"]["outcome"], "pass")
        self.assertEqual(orch.unlock_verdict.get("gpu"), "stable_short")
        self.assertIn("gpu_40cu", orch._applied_steps())

    def test_m2_gpu_validation_honors_master_switch(self):
        """MINOR m2: probe.unlock_validate=false disabilita la validazione
        post-unlock ANCHE per la GPU (mai girare vkmark)."""
        orch, hw = self._make(cpu_probe=False)
        orch.config.probe_unlock_validate = False
        orch.health_test = _FakeHealth(TSV_ABSENT)
        with mock.patch.object(orch.gpu_validation, "run",
                               side_effect=AssertionError(
                                   "master switch off: nessuna vkmark")), \
             mock.patch.object(orch.gpu_unlock, "apply",
                               return_value=self._umr_apply()):
            out = orch._phase_unlock()
        self.assertNotIn("validation", out["gpu"])

    def test_gpu_fail_also_disables_boot_persistence_service(self):
        """Touchpoint esterno D6 (GPU): systemctl disable --now
        bc250-cu-live-manager.service (run reali)."""
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return 0, "", ""

        orch = Orchestrator(config=BUOConfig(), mock=False, dry_run=False)
        with mock.patch("buo.utils.shell.run_command", side_effect=fake_run):
            orch._disable_40cu_persistence()
        self.assertTrue(any("systemctl" in cmd and "disable" in cmd and
                            any("bc250-cu-live-manager" in c for c in cmd)
                            for cmd in calls))


# ===================================================================== #
# 6. Gate verdetto (11), anti-loop (12), dry-run/mock (13)
# ===================================================================== #

class TestVerdictGates(_OrchCase):
    def test_scenario11_verdict_blocks_unlock_from_init(self):
        """Run nuova da init con verdetto never_*: CPU e GPU saltati con
        nota, nessun unlock, nessun reboot."""
        self._seed_verdict_file("cpu", "never_unlock")
        self._seed_verdict_file("gpu", "never_enable_all")
        orch, hw = self._make()
        orch.checkpoint.clear()
        out = orch._phase_unlock()
        self.assertTrue(out["cpu"].get("verdict_blocked"))
        self.assertTrue(out["gpu"].get("verdict_blocked"))
        self.assertNotIn("cpu_core_unlock", orch._applied_steps())
        self.assertNotIn("gpu_40cu", orch._applied_steps())
        self.assertEqual(orch.checkpoint.get_reboot_count(), 0)
        self.assertTrue(any("marcato" in n.lower()
                            for n in orch.results["notes"]))

    def test_scenario11b_cpu_never_blocks_only_cpu(self):
        """Verdetto solo CPU → GPU procede normalmente."""
        self._seed_verdict_file("cpu", "never_unlock")
        orch, hw = self._make(gpu_probe=False)
        orch.checkpoint.clear()
        out = orch._phase_unlock()
        self.assertTrue(out["cpu"].get("verdict_blocked"))
        self.assertNotIn("cpu_core_unlock", orch._applied_steps())


class TestUnlockValidateAntiloop(_OrchCase):
    def test_scenario12_reboot_cap_aborts_no_extra_reboot(self):
        """Tetto max_reboots raggiunto durante il flusso → abort
        (safety_violation), NESSUN reboot extra (il contatore non
        incrementa)."""
        orch, hw = self._make()
        self._seed_unlock_validate(orch, hw)
        hw.state.cpu_unlock_ok = False
        orch.config.max_reboots = 3
        orch.checkpoint.set("reboot_count", 3)
        with mock.patch.object(orch, "_cpu_online_threads",
                               return_value=16), \
             mock.patch.object(orch, "_disable_core_unlock_boot"):
            orch.mock = False  # ramo reale di _schedule_reboot (tetto)
            data = orch._phase_unlock_validate()
        self.assertTrue(orch.safety_violation)
        self.assertEqual(orch.checkpoint.get_reboot_count(), 3,
                         "il contatore non deve incrementare oltre il tetto")
        self.assertTrue(data["cpu"].get("reverted"))
        # il verdetto resta comunque salvato (in-memory: sim dell'init)
        self.assertEqual(orch.unlock_verdict.get("cpu"), "never_unlock")


class TestSimNoPersistentState(_OrchCase):
    def test_scenario13_dry_run_no_verdict_no_marker_files(self):
        """Dry-run: validazioni simulate — nessun unlock-verdict.json,
        nessun marcatore nel checkpoint, nessun disable di servizi."""
        orch, hw = self._make(dry_run=True)
        self._seed_unlock_validate(orch, hw)
        # GPU fail simulato su macchina fresca (tsv assente)
        hw.state.gpu_unlock_ok = False
        orch.health_test = _FakeHealth(TSV_ABSENT)
        with mock.patch.object(orch, "_disable_40cu_persistence",
                               side_effect=AssertionError(
                                   "dry-run: nessun systemctl")):
            out = orch._phase_unlock()
        # GPU fail simulato → verdetto solo in memoria (mai su file)
        self.assertEqual(orch.unlock_verdict.get("gpu"),
                         "never_enable_all")
        state_dir = Path(os.environ["BUO_STATE_DIR"])
        self.assertFalse((state_dir / VERDICT_FILE).exists(),
                         "dry-run: nessun unlock-verdict.json scritto")

    def test_scenario13_mock_fail_writes_no_verdict_file(self):
        """mock (dry_run=False): stessa regola M2 — verdetto in memoria,
        nessun file nello state dir."""
        orch, hw = self._make()
        self._seed_unlock_validate(orch, hw)
        hw.state.cpu_unlock_ok = False
        orch._phase_unlock_validate()
        state_dir = Path(os.environ["BUO_STATE_DIR"])
        self.assertFalse((state_dir / VERDICT_FILE).exists())


# ===================================================================== #
# 7. Config (scenario 14)
# ===================================================================== #

class TestUnlockValidationConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = BUOConfig()
        self.assertTrue(cfg.probe_unlock_validate)
        self.assertEqual(cfg.validation_unlock_cpu_seconds, 60)
        self.assertEqual(cfg.validation_unlock_gpu_seconds, 60)

    def test_zero_means_skip(self):
        cfg = BUOConfig({"phases": {"validation": {
            "unlock_cpu_seconds": 0, "unlock_gpu_seconds": 0}}})
        self.assertEqual(cfg.validation_unlock_cpu_seconds, 0)
        self.assertEqual(cfg.validation_unlock_gpu_seconds, 0)

    def test_n3_negative_means_skip(self):
        """NIT n3: durate negative → skip (0), MAI far girare la
        validazione per un valore negativo mal configurato."""
        cfg = BUOConfig({"phases": {"validation": {
            "unlock_cpu_seconds": -5, "unlock_gpu_seconds": -1}}})
        self.assertEqual(cfg.validation_unlock_cpu_seconds, 0)
        self.assertEqual(cfg.validation_unlock_gpu_seconds, 0)

    def test_clamped_to_range(self):
        cfg = BUOConfig({"phases": {"validation": {
            "unlock_cpu_seconds": 5, "unlock_gpu_seconds": 999}}})
        self.assertEqual(cfg.validation_unlock_cpu_seconds, 10)
        self.assertEqual(cfg.validation_unlock_gpu_seconds, 300)

    def test_known_keys_no_warning(self):
        with self.assertNoLogs("buo.config", level="WARNING"):
            BUOConfig({"phases": {"probe": {"unlock_validate": True},
                                  "validation": {"unlock_cpu_seconds": 30,
                                                 "unlock_gpu_seconds": 30}}})

    def test_unknown_key_warns_fail_soft(self):
        with self.assertLogs("buo.config", level="WARNING") as cm:
            BUOConfig({"phases": {"validation": {"unlock_vga_seconds": 5}}})
        self.assertIn("validation.unlock_vga_seconds",
                      "\n".join(cm.output))

    def test_in_to_dict_roundtrip(self):
        cfg = BUOConfig({"phases": {"validation": {
            "unlock_cpu_seconds": 0, "unlock_gpu_seconds": 120},
            "probe": {"unlock_validate": False}}})
        d = cfg.to_dict()
        self.assertFalse(d["phases"]["probe"]["unlock_validate"])
        self.assertEqual(d["phases"]["validation"]["unlock_cpu_seconds"], 0)
        self.assertEqual(d["phases"]["validation"]["unlock_gpu_seconds"],
                         120)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "buo.yaml"
            cfg.save(path)
            loaded = BUOConfig.load(path)
            self.assertEqual(loaded.validation_unlock_gpu_seconds, 120)
            self.assertFalse(loaded.probe_unlock_validate)

    def test_master_switch_disables_phase(self):
        """probe.unlock_validate=false → la fase salta la validazione."""
        orch, hw = None, None
        tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = tmp.name
        try:
            hw = MockHardware(seed=7)
            hw.state.is_acpi_fixed = True
            cfg = BUOConfig({"phases": {"probe": {"unlock_validate": False}}})
            cfg.validation_stress_duration = 0
            cfg.benchmark_enabled = False
            orch = Orchestrator(config=cfg, mock=True, dry_run=False,
                                mock_hardware=hw)
            orch.checkpoint.clear()
            orch.checkpoint.set("applied_steps", ["cpu_core_unlock"])
            hw.state.core_mask = CORE_MASK_UNLOCKED
            hw.state.cpu_cores = 8
            data = orch._phase_unlock_validate()
            self.assertEqual(data["cpu"]["skipped"], "disabled")
        finally:
            os.environ.pop("BUO_STATE_DIR", None)
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
