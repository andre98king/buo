#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test degli hard limits immutabili."""

import unittest

from buo.constants import (CORE_MASK_STOCK, CORE_MASK_UNLOCKED, LIMITS,
                           PHASES, ROLLBACK_ORDER, SMU_MSG_WRITE_FF,
                           CORE_MASK_REG)


class TestHardLimits(unittest.TestCase):
    def test_cpu_vid_hard_limit(self):
        self.assertEqual(LIMITS.cpu.vid_absolute_max, 1325)
        self.assertLessEqual(LIMITS.cpu.vid_recommended_max,
                             LIMITS.cpu.vid_absolute_max)

    def test_gpu_voltage_hard_limit(self):
        self.assertEqual(LIMITS.gpu.voltage_absolute_max, 1100)

    def test_temperature_limits(self):
        # Politica termica a due livelli (03/09): temp_max = HARD abort
        # real-time (CPU 95 / GPU 105, sotto i limiti AMD con margine);
        # il target OPERATIVO applicato (cpu.temp_apply, max_temperature
        # del conf SMU) resta SOTTO l'HARD (mai un criterio di abort).
        self.assertEqual(LIMITS.cpu.temp_max, 95)
        self.assertEqual(LIMITS.gpu.temp_max, 105)
        self.assertEqual(LIMITS.cpu.temp_apply, 90)
        self.assertLess(LIMITS.cpu.temp_apply, LIMITS.cpu.temp_max)

    def test_power_budget(self):
        self.assertEqual(LIMITS.power.power_budget, 300)
        self.assertLessEqual(LIMITS.power.power_budget, LIMITS.power.psu_max)

    def test_core_masks(self):
        self.assertEqual(CORE_MASK_STOCK, 0x77)
        self.assertEqual(CORE_MASK_UNLOCKED, 0xFF)

    def test_smu_constants(self):
        self.assertEqual(SMU_MSG_WRITE_FF, 0x98)
        self.assertEqual(CORE_MASK_REG, 0x5A870)

    def test_rollback_order_is_complete(self):
        expected = ["cpu_overclock", "gpu_governor", "gpu_40cu", "gpu_mask",
                    "cpu_core_unlock", "acpi_fix", "tlb_fix", "ace_fix",
                    "iommu", "vram_config", "gtt_tuning", "fan_control"]
        self.assertEqual(ROLLBACK_ORDER, expected)


class TestPhases(unittest.TestCase):
    """PHASES con la validazione post-unlock (design
    POSTUNLOCK_VALIDATION, D2): unlock_validate tra unlock e fix — il
    segmento reboot-capable la include senza assunzioni su indici fissi
    (usati da _next_phase / _run_can_schedule_reboot)."""

    def test_unlock_validate_between_unlock_and_fix(self):
        i_unlock = PHASES.index("unlock")
        i_validate = PHASES.index("unlock_validate")
        i_fix = PHASES.index("fix")
        self.assertLess(i_unlock, i_validate)
        self.assertLess(i_validate, i_fix)
        self.assertIn("unlock_validate", PHASES)

    def test_phases_contiguous_and_no_duplicates(self):
        self.assertEqual(len(PHASES), len(set(PHASES)))
        self.assertEqual(PHASES[0], "init")
        self.assertEqual(PHASES[-1], "error")


if __name__ == "__main__":
    unittest.main()
