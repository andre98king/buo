#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test degli hard limits immutabili."""

import unittest

from buo.constants import (CORE_MASK_STOCK, CORE_MASK_UNLOCKED, LIMITS,
                           ROLLBACK_ORDER, SMU_MSG_WRITE_FF, CORE_MASK_REG)


class TestHardLimits(unittest.TestCase):
    def test_cpu_vid_hard_limit(self):
        self.assertEqual(LIMITS.cpu.vid_absolute_max, 1325)
        self.assertLessEqual(LIMITS.cpu.vid_recommended_max,
                             LIMITS.cpu.vid_absolute_max)

    def test_gpu_voltage_hard_limit(self):
        self.assertEqual(LIMITS.gpu.voltage_absolute_max, 1100)

    def test_temperature_limits(self):
        self.assertEqual(LIMITS.cpu.temp_max, 90)
        self.assertEqual(LIMITS.gpu.temp_max, 85)

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


if __name__ == "__main__":
    unittest.main()
