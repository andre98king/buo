#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test 40-CU su ostree: runtime UMR (bc250-cu-live-manager).

Il kernel patch non funziona su ostree (/usr read-only): su ostree
GPU40CUUnlock deve usare il runtime UMR. Verifica parsing del wrapper
e selezione del metodo per distro.
"""

import unittest
from unittest import mock

from buo.unlock.gpu import GPU40CUUnlock
from buo.unlock.wrappers.bc250_live_manager import BC250LiveManagerWrapper


class TestLiveManagerParse(unittest.TestCase):
    def test_status_40_40_full_die(self):
        """Output status con 40/40 routed → full_die=True."""
        out = (
            "  amdgpu     : bc250_cc_write_mode=not exposed, active_cu_number=24\n"
            "  | SE0.SH0 |  D+  |  D+  |  D+  |  S+  |  S+  | 0x1f | 0xffe00000 |  10/10 |\n"
            "  CUs active & routed  : 40/40\n"
        )
        w = BC250LiveManagerWrapper(script_path="/tmp/nonexistent")
        parsed = w.parse_output(out, "")
        self.assertTrue(parsed["full_die"])
        self.assertEqual(parsed["cu_routed"], 40)
        self.assertEqual(parsed["cu_total"], 40)

    def test_status_24_24_not_full(self):
        """Output status con 24/40 routed → full_die=False."""
        out = (
            "  | SE0.SH0 |  D+  |  D+  |  D+  |  --  |  --  | 0x07 | 0xffe00000 |   6/10 |\n"
            "  CUs active & routed  : 24/40\n"
        )
        w = BC250LiveManagerWrapper(script_path="/tmp/nonexistent")
        parsed = w.parse_output(out, "")
        self.assertFalse(parsed["full_die"])
        self.assertEqual(parsed["cu_routed"], 24)

    def test_enable_all_target_parsed(self):
        """Output 'enable all' → (40/40 CUs target) → cu_target=40."""
        out = "[ OK ] dispatch registers updated (40/40 CUs target)\n"
        w = BC250LiveManagerWrapper(script_path="/tmp/nonexistent")
        parsed = w.parse_output(out, "")
        self.assertEqual(parsed["cu_target"], 40)
        self.assertTrue(parsed["full_die"])


class TestGPUUnlockOstreeSelection(unittest.TestCase):
    def test_ostree_uses_live_manager(self):
        """Su ostree (ostree-booted presente) → wrapper = live manager."""
        with mock.patch("buo.unlock.gpu.os.path.exists", return_value=True):
            g = GPU40CUUnlock(mock=False, use_wrapper=True)
        self.assertTrue(g.is_ostree)
        self.assertIsInstance(g.wrapper, BC250LiveManagerWrapper)

    def test_non_ostree_uses_kernel_patch_wrapper(self):
        """Non-ostree → wrapper = bc250-enable-40cu (kernel patch)."""
        from buo.unlock.wrappers.bc250_40cu import BC25040CUWrapper
        with mock.patch("buo.unlock.gpu.os.path.exists", return_value=False):
            g = GPU40CUUnlock(mock=False, use_wrapper=True)
        self.assertFalse(g.is_ostree)
        self.assertIsInstance(g.wrapper, BC25040CUWrapper)

    def test_mock_has_no_wrapper(self):
        g = GPU40CUUnlock(mock=True, use_wrapper=True)
        self.assertIsNone(g.wrapper)


if __name__ == "__main__":
    unittest.main()
