#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Selezione del tool di stress GPU — unica fonte di verità (bug di campo 10/09).

Contesto: la validate costruiva il comando GPU per conto proprio
(`glmark2 --run-forever --seconds N`, opzione inesistente) e non provava mai
vkmark. Su una macchina con il SOLO vkmark `gpu_rc` restava 1 → la validate
falliva SEMPRE con `scope="both"` → il rollback automatico (T2) disinstallava
una config CPU buona a ogni run.

Regole verificate:
  • vkmark è il tool PRIMARIO (carico realistico, durata controllata);
  • furmark solo ultima risorsa (sintetico/aggressivo: non rappresentativo);
  • glmark2 mai (nessun controllo durata);
  • nessun tool ⇒ componente NON verificabile, NON un fallimento del test.
"""

import unittest
from unittest import mock

from buo.utils.gpu_stress import gpu_stress_cmd, gpu_stress_tool
from buo.validate.stress import StressTest


def _which_only(*tools):
    def fake(tool):
        return f"/usr/bin/{tool}" if tool in tools else None
    return fake


class TestToolSelection(unittest.TestCase):
    def test_vkmark_primary_when_both_present(self):
        with mock.patch("buo.utils.gpu_stress.which",
                        side_effect=_which_only("vkmark", "furmark")):
            self.assertEqual(gpu_stress_tool(), "vkmark")
            cmd = gpu_stress_cmd(300)
        self.assertEqual(cmd[0], "vkmark")
        self.assertIn("desktop:duration=300", cmd)

    def test_furmark_only_as_last_resort(self):
        with mock.patch("buo.utils.gpu_stress.which",
                        side_effect=_which_only("furmark")):
            self.assertEqual(gpu_stress_tool(), "furmark")
            cmd = gpu_stress_cmd(300)
        self.assertEqual(cmd[0], "furmark")
        self.assertIn("--max-time", cmd)
        self.assertIn("300", cmd)
        self.assertNotIn("--seconds", cmd)
        self.assertNotIn("--duration", cmd)

    def test_glmark2_never_selected(self):
        """glmark2 non ha durata controllabile (--seconds inesistente)."""
        with mock.patch("buo.utils.gpu_stress.which",
                        side_effect=_which_only("glmark2")):
            self.assertIsNone(gpu_stress_tool())
            self.assertIsNone(gpu_stress_cmd(30))

    def test_no_tool_returns_none(self):
        with mock.patch("buo.utils.gpu_stress.which", return_value=None):
            self.assertIsNone(gpu_stress_cmd(30))


class _Reader:
    def get_cpu_temp(self):
        return 60.0

    def get_gpu_temp(self):
        return 60.0

    def get_total_power(self):
        return 100.0


class TestValidateUsesSharedSelection(unittest.TestCase):
    """La validate DEVE usare la selezione condivisa (mai un tool rotto)."""

    def _stress(self):
        return StressTest(mock=False, mock_hardware=None, reader=_Reader())

    def test_scope_cpu_skips_gpu_entirely(self):
        st = self._stress()
        with mock.patch.object(st, "_run_loaded",
                               return_value=(0, 60.0, 60.0, 100.0)) as run, \
             mock.patch("buo.validate.stress.which", return_value="/usr/bin/stress-ng"):
            out = st.run(duration_minutes=1, scope="cpu")
        self.assertTrue(out["passed"], out)
        self.assertEqual(run.call_count, 1)          # solo stress-ng
        self.assertEqual(run.call_args[0][0][0], "stress-ng")

    def test_scope_both_uses_vkmark(self):
        st = self._stress()
        with mock.patch.object(st, "_run_loaded",
                               return_value=(0, 60.0, 60.0, 100.0)) as run, \
             mock.patch("buo.validate.stress.which", return_value="/usr/bin/stress-ng"), \
             mock.patch("buo.utils.gpu_stress.which",
                        side_effect=_which_only("vkmark")):
            out = st.run(duration_minutes=1, scope="both")
        self.assertTrue(out["passed"], out)
        cmds = [c[0][0][0] for c in run.call_args_list]
        self.assertEqual(cmds, ["stress-ng", "vkmark"])
        self.assertEqual(out["gpu_rc"], 0)
        self.assertFalse(out["gpu_skipped"])

    def test_no_gpu_tool_is_not_a_failure(self):
        """BUG 10/09: senza tool GPU la validate deve passare (componente
        non verificabile) — NON far scattare il rollback che disinstalla
        una config CPU buona."""
        st = self._stress()
        with mock.patch.object(st, "_run_loaded",
                               return_value=(0, 60.0, 60.0, 100.0)), \
             mock.patch("buo.validate.stress.which", return_value="/usr/bin/stress-ng"), \
             mock.patch("buo.utils.gpu_stress.which", return_value=None):
            with self.assertLogs("buo.StressTest", level="WARNING") as logs:
                out = st.run(duration_minutes=1, scope="both")
        self.assertTrue(out["passed"], out)
        self.assertTrue(out["gpu_skipped"])
        self.assertIn("NON verificato", "\n".join(logs.output))

    def test_failure_reports_which_component(self):
        """Il fallimento deve dire QUALE componente (diagnosi 10/09)."""
        st = self._stress()
        with mock.patch.object(st, "_run_loaded",
                               return_value=(1, 60.0, 60.0, 100.0)), \
             mock.patch("buo.validate.stress.which", return_value="/usr/bin/stress-ng"), \
             mock.patch("buo.utils.gpu_stress.which",
                        side_effect=_which_only("vkmark")):
            with self.assertLogs("buo.StressTest", level="ERROR") as logs:
                out = st.run(duration_minutes=1, scope="both")
        self.assertFalse(out["passed"])
        self.assertIn("cpu_rc=1", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
