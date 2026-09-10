#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CWD scrivibile per i tool di stress — bug di campo 10/09 (BC-250).

`stress-ng` usa la CWD come temp-path: dentro un'unità systemd la CWD è `/`,
che su ostree è READ-ONLY → `stress-ng` aborta in meno di un secondo con

    aborting: temp-path '.' must be readable and writeable

rc=1 → la validate fallisce SEMPRE e il rollback automatico (T2) disinstalla
una config CPU buona. Le run lunghe sul campo si lanciano proprio come unità
transiente (`systemd-run`), quindi il caso capita sempre sul campo.

Il fix è nel punto CONDIVISO: `utils.shell.stress_cwd()` (usato da validate,
sweep, benchmark e validazione post-unlock).
"""

import os
import subprocess
import unittest
from unittest import mock

from buo.utils.gpu_stress import gpu_stress_cmd
from buo.utils.shell import stress_cwd
from buo.validate.stress import StressTest


class TestStressCwd(unittest.TestCase):
    def test_stress_cwd_is_writable(self):
        d = stress_cwd()
        self.assertTrue(os.path.isdir(d), d)
        self.assertTrue(os.access(d, os.W_OK), f"{d} non scrivibile")

    def test_run_loaded_popen_gets_writable_cwd(self):
        """`_run_loaded` deve passare `cwd=` scrivibile a ogni spawn."""
        st = StressTest(mock=False, mock_hardware=None, reader=_CoolReader())
        st.deadline_grace = 1
        spawned = []

        class _FakeProc:
            returncode = 0
            stderr = None

            def __init__(self, cmd, **kwargs):
                spawned.append((cmd, kwargs))
                self._done = False

            def poll(self):
                self._done = True
                return 0

            def terminate(self):  # pragma: no cover - non usato
                pass

        with mock.patch("buo.validate.stress.subprocess.Popen",
                        side_effect=_FakeProc):
            st._run_loaded(["stress-ng", "--cpu", "0", "--timeout", "1"],
                           1, _CoolReader(), 300)
        self.assertEqual(len(spawned), 1)
        _cmd, kwargs = spawned[0]
        self.assertEqual(kwargs.get("cwd"), stress_cwd(),
                         "stress-ng senza cwd scrivibile → aborta su ostree")

    def test_gpu_command_also_runs_with_writable_cwd(self):
        """Anche il tool GPU (vkmark) usa la cwd scrivibile."""
        st = StressTest(mock=False, mock_hardware=None, reader=_CoolReader())
        with mock.patch("buo.validate.stress.subprocess.Popen") as popen, \
             mock.patch("buo.utils.gpu_stress.which",
                        side_effect=lambda t: "/usr/bin/vkmark"
                        if t == "vkmark" else None):
            popen.return_value.poll.return_value = 0
            popen.return_value.returncode = 0
            st.run(duration_minutes=1, scope="gpu")
        self.assertTrue(popen.called)
        for call in popen.call_args_list:
            self.assertEqual(call.kwargs.get("cwd"), stress_cwd(), call)

    def test_benchmark_runs_with_writable_cwd(self):
        """Il benchmark CPU/GPU usa la stessa cwd scrivibile."""
        from buo.benchmark.runner import BenchmarkRunner
        calls = []

        def fake_run(cmd, timeout=60, sudo=False, check=False,
                     capture=True, cwd=None):
            calls.append((cmd, cwd))
            return 0, "Bogo ops/s 1.0", ""

        def fake_which(tool):
            return f"/usr/bin/{tool}" if tool in ("stress-ng", "vkmark") else None

        br = BenchmarkRunner(mock=False)
        with mock.patch("buo.benchmark.runner.run_command",
                        side_effect=fake_run), \
             mock.patch("buo.benchmark.runner.which", side_effect=fake_which), \
             mock.patch("buo.utils.gpu_stress.which", side_effect=fake_which):
            br.run_cpu_stress(duration=5)
            br.run_gpu_stress(duration=5)
            br.run_compute_benchmark(duration=5)
        self.assertTrue(calls)
        for _cmd, cwd in calls:
            self.assertEqual(cwd, stress_cwd())

    def test_gpu_stress_cmd_still_valid(self):
        """Regressione: la selezione del tool non è cambiata col fix cwd."""
        with mock.patch("buo.utils.gpu_stress.which",
                        side_effect=lambda t: "/usr/bin/vkmark"
                        if t == "vkmark" else None):
            cmd = gpu_stress_cmd(30)
        self.assertEqual(cmd[0], "vkmark")
        self.assertIn("desktop:duration=30", cmd)


class _CoolReader:
    def get_cpu_temp(self):
        return 55.0

    def get_gpu_temp(self):
        return 55.0

    def get_total_power(self):
        return 100.0


if __name__ == "__main__":
    unittest.main()
