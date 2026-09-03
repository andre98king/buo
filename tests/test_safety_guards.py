#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Test degli invarianti di sicurezza e dei guard (senza hardware reale).

Copre:
    1. safety/limits.py + safety/monitor.py — clamp degli hard limit
    2. unlock/dxe.py — DXE core unlock (rifiuto senza verifica)
    3. state/recovery.py — piano di ripresa (resume vs rollback)
    4. unlock/mask.py + wrappers/bc250_mask.py — math CU→WGP e parser

Nessuna rete, nessun hardware, nessun flash: tutto via unittest.mock.
"""

import tempfile
import unittest
from unittest import mock

from buo.constants import LIMITS
from buo.exceptions import SafetyViolation
from buo.safety.limits import SAFETY_LIMITS
from buo.safety.monitor import SafetyMonitor
from buo.state.reboot import RebootManager
from buo.state.recovery import RecoveryManager
from buo.unlock.dxe import DXECoreUnlock
from buo.unlock.mask import CUMask
from buo.unlock.wrappers.bc250_mask import BC250MaskWrapper
from buo.utils.mock import MockHardware


class TestHardLimitClamp(unittest.TestCase):
    """I limiti custom non possono MAI superare gli hard limit congelati."""

    def test_safety_limits_equal_frozen_hard_limits(self):
        self.assertEqual(SAFETY_LIMITS.cpu_vid_absolute_max,
                         LIMITS.cpu.vid_absolute_max)
        self.assertEqual(SAFETY_LIMITS.cpu_temp_max, LIMITS.cpu.temp_max)
        self.assertEqual(SAFETY_LIMITS.gpu_voltage_absolute_max,
                         LIMITS.gpu.voltage_absolute_max)
        self.assertEqual(SAFETY_LIMITS.gpu_temp_max, LIMITS.gpu.temp_max)
        self.assertEqual(SAFETY_LIMITS.power_budget, LIMITS.power.power_budget)

    def test_custom_limits_above_hard_limit_clamp_down(self):
        m = SafetyMonitor(
            limits={"cpu_temp_max": 200,
                    "gpu_temp_max": 999,
                    "power_budget": 10000},
            vram_estimation=False,
        )
        self.assertEqual(m.limits.cpu_temp_max, LIMITS.cpu.temp_max)       # 95
        self.assertEqual(m.limits.gpu_temp_max, LIMITS.gpu.temp_max)       # 105
        self.assertEqual(m.limits.power_budget, LIMITS.power.power_budget)  # 300

    def test_custom_limits_below_hard_limit_are_kept(self):
        # min(base, custom) deve preservare i valori più restrittivi
        m = SafetyMonitor(
            limits={"cpu_temp_max": 50,
                    "gpu_temp_max": 40,
                    "power_budget": 200},
            vram_estimation=False,
        )
        self.assertEqual(m.limits.cpu_temp_max, 50)
        self.assertEqual(m.limits.gpu_temp_max, 40)
        self.assertEqual(m.limits.power_budget, 200)

    def test_cpu_vid_and_gpu_voltage_always_hard_limits(self):
        # Questi campi NON sono clampabili dall'input: restano hard limit
        m = SafetyMonitor(
            limits={"cpu_vid_absolute_max": 9999,
                    "gpu_voltage_absolute_max": 9999,
                    "cpu_temp_max": 200,
                    "gpu_temp_max": 200,
                    "power_budget": 9999},
            vram_estimation=False,
        )
        self.assertEqual(m.limits.cpu_vid_absolute_max,
                         LIMITS.cpu.vid_absolute_max)  # 1325
        self.assertEqual(m.limits.gpu_voltage_absolute_max,
                         LIMITS.gpu.voltage_absolute_max)  # 1100

    def test_default_monitor_uses_hard_limits(self):
        m = SafetyMonitor(vram_estimation=False)
        self.assertEqual(m.limits.cpu_temp_max, LIMITS.cpu.temp_max)
        self.assertEqual(m.limits.gpu_temp_max, LIMITS.gpu.temp_max)
        self.assertEqual(m.limits.power_budget, LIMITS.power.power_budget)


class TestDXECoreUnlockGuard(unittest.TestCase):
    """Il DXE unlock rifiuta senza verifica core e non tocca il firmware."""

    def test_prerequisites_require_verified_cores(self):
        u = DXECoreUnlock()
        self.assertFalse(u.prerequisites_ok(False))
        self.assertTrue(u.prerequisites_ok(True))

    def test_apply_refuses_when_cores_not_verified(self):
        u = DXECoreUnlock(mock=True)
        with self.assertRaises(SafetyViolation):
            u.apply(cores_verified=False)

    def test_apply_mock_returns_applied_without_firmware(self):
        u = DXECoreUnlock(mock=True)
        # Guard: se qualcosa tentasse di eseguire un processo, fallisce
        with mock.patch("subprocess.run",
                        side_effect=AssertionError("firmware touched")):
            result = u.apply(cores_verified=True)
        self.assertTrue(result["applied"])
        self.assertTrue(result["permanent"])
        self.assertEqual(result["method"], "mock")
        self.assertIn("nessuna modifica reale", result["note"])

    def test_apply_real_mode_never_auto_flashes(self):
        u = DXECoreUnlock(mock=False)
        result = u.apply(cores_verified=True)
        self.assertFalse(result["applied"])
        self.assertTrue(result["permanent"])
        self.assertEqual(result["method"], "manual")
        self.assertIn("warning", result)

    def test_rollback_is_manual_only(self):
        u = DXECoreUnlock(mock=True)
        self.assertFalse(u.rollback())


class TestRecoveryPlan(unittest.TestCase):
    """get_recovery_plan: resume vs rollback secondo la logica reale."""

    @staticmethod
    def _checkpoint(state):
        cp = mock.MagicMock()
        cp.full_state.return_value = state
        return cp

    def _interrupted_state(self):
        return {
            "current_phase": "unlock",
            "reboot_count": 1,
            "phases": {
                "unlock": {"completed": False},
            },
        }

    def test_interrupted_phase_resumes(self):
        rm = RecoveryManager(checkpoint=self._checkpoint(
            self._interrupted_state()))
        plan = rm.get_recovery_plan()
        self.assertEqual(plan["interrupted_phase"], "unlock")
        self.assertIsNone(plan["verification"])
        self.assertEqual(plan["action"], "resume")

    def test_verified_phase_resumes(self):
        rm = RecoveryManager(checkpoint=self._checkpoint(
            self._interrupted_state()), verify_callback=lambda p: True)
        plan = rm.get_recovery_plan()
        self.assertEqual(plan["verification"], True)
        self.assertEqual(plan["action"], "resume")

    def test_not_verified_phase_rolls_back(self):
        rm = RecoveryManager(checkpoint=self._checkpoint(
            self._interrupted_state()), verify_callback=lambda p: False)
        plan = rm.get_recovery_plan()
        self.assertEqual(plan["verification"], False)
        self.assertEqual(plan["action"], "rollback")

    def test_finds_first_incomplete_phase_after_current(self):
        state = {
            "current_phase": "unlock",
            "reboot_count": 2,
            "phases": {
                "unlock": {"completed": True},
                "fix": {"completed": False},
                "optimize": {"completed": False},
            },
        }
        rm = RecoveryManager(checkpoint=self._checkpoint(state))
        plan = rm.get_recovery_plan()
        self.assertEqual(plan["interrupted_phase"], "fix")
        self.assertEqual(plan["action"], "resume")
        self.assertEqual(plan["phases_completed"], ["unlock"])
        self.assertIn("fix", plan["phases_pending"])

    def test_verify_callback_error_fails_closed_to_rollback(self):
        # Fail-closed: un errore di verifica NON riprende, ma avvia il rollback
        def boom(phase):
            raise RuntimeError("probe fallito")

        rm = RecoveryManager(checkpoint=self._checkpoint(
            self._interrupted_state()), verify_callback=boom)
        plan = rm.get_recovery_plan()
        self.assertFalse(plan["verification"])
        self.assertEqual(plan["action"], "rollback")

    def test_recommend_rollback_text(self):
        rm = RecoveryManager(checkpoint=self._checkpoint(
            self._interrupted_state()), verify_callback=lambda p: False)
        text = rm.recommend()
        self.assertIn("rollback", text)


class TestCUMaskMath(unittest.TestCase):
    """Conversione CU→WGP e parsing dell'output dello script di maschera."""

    @staticmethod
    def _bad_arg_for(cus):
        with tempfile.NamedTemporaryFile() as f:
            wrapper = BC250MaskWrapper(script_path=f.name)
            with mock.patch("buo.unlock.wrappers.base.run_command",
                            return_value=(0, "", "")) as rc:
                wrapper.generate(bad_cu=cus)
                cmd = rc.call_args[0][0]
        idx = cmd.index("--bad")
        return cmd[idx + 1]

    def test_each_wgp_masks_two_cus(self):
        # wgp = cu // 2; stringa = f"{wgp//4}.{(wgp%4)//2}.{wgp%2}"
        cases = {
            (0, 1): "0.0.0",
            (2, 3): "0.0.1",
            (4, 5): "0.1.0",
            (6, 7): "0.1.1",
            (8, 9): "1.0.0",
        }
        for cus, expected in cases.items():
            with self.subTest(cus=cus):
                self.assertEqual(self._bad_arg_for(list(cus)), expected)

    def test_bad_cu_0_1_2_3_maps_to_two_wgps(self):
        self.assertEqual(self._bad_arg_for([0, 1, 2, 3]), "0.0.0,0.0.1")

    def test_parse_output_full_fixture(self):
        wrapper = BC250MaskWrapper()
        out = wrapper.parse_output(
            "options amdgpu bc250_cc_write_mode=3 disable_cu=0.0.0,0.0.1\n"
            "Usable after mask: 38/40 CUs\n"
            "Installed /etc/modprobe.d/bc250-40cu-selective-mask.conf",
            "",
        )
        self.assertEqual(out["mask"], "0.0.0,0.0.1")
        self.assertEqual(out["usable_cus"], 38)
        self.assertEqual(out["total_cus"], 40)
        self.assertTrue(out["installed"])

    def test_parse_output_malformed_disable_cu_yields_no_mask(self):
        # Il parser NON valida i valori: un disable_cu non numerico
        # semplicemente non produce alcuna maschera (fail-safe a None).
        wrapper = BC250MaskWrapper()
        out = wrapper.parse_output("disable_cu=not-a-valid-mask", "")
        self.assertIsNone(out["mask"])

    def test_parse_output_empty(self):
        wrapper = BC250MaskWrapper()
        out = wrapper.parse_output("", "")
        self.assertIsNone(out["mask"])
        self.assertIsNone(out["usable_cus"])


class TestCUMaskApply(unittest.TestCase):
    """apply() del CUMask: no-op e path mock senza toccare lo script."""

    def test_apply_no_defective_is_noop(self):
        cm = CUMask(mock=True)
        result = cm.apply()
        self.assertTrue(result["applied"])
        self.assertIsNone(result["mask"])

    def test_apply_mock_uses_mock_hw_mask(self):
        hw = MockHardware()
        hw.state.wgp_mask = "0x000FFFFF"
        cm = CUMask(mock=True, mock_hardware=hw)
        result = cm.apply(defective_cu=[2, 3])
        self.assertTrue(result["applied"])
        self.assertEqual(result["mask"], "0x000FFFFF")


class TestRebootManagerGuard(unittest.TestCase):
    """Il comando di ripresa non può iniettare direttive nell'unit systemd."""

    def test_default_command_is_accepted(self):
        self.assertEqual(RebootManager().resume_command, "buo resume")

    def test_newline_in_command_is_rejected(self):
        with self.assertRaises(ValueError):
            RebootManager("buo resume\nExecStart=/tmp/evil")


if __name__ == "__main__":
    unittest.main()
