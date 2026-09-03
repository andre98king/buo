#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit test dei nuovi getter del lettore hardware REALE (status completo).

Nessun hardware reale: tutte le letture (sysfs, debugfs, hwmon, conf,
systemctl, /proc/cpuinfo) sono iniettate in directory temporanee. Ogni
percorso inesistente o errore deve dare None, mai un'eccezione
(fail-soft C1: mai valori inventati).
"""

import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from buo.safety.reader import RealHardwareReader
from buo.utils import smn


def _cpuinfo_text(n_cores: int) -> str:
    """/proc/cpuinfo finto: `n_cores` core fisici, ognuno con 2 thread."""
    lines = []
    proc = 0
    for core in range(n_cores):
        for _ in range(2):
            lines.append(f"processor : {proc}")
            lines.append("vendor_id : AuthenticAMD")
            lines.append("physical id : 0")
            lines.append(f"core id : {core}")
            lines.append("")
            proc += 1
    return "\n".join(lines)


class _FakeSmuModule:
    """Modulo bc250_smu finto (solo letture, mai scritture SMU)."""

    class Bc250Smu:
        def __init__(self, use_flock=True):
            self.use_flock = use_flock

        def q3_0x36_get_current_cpu_voltage(self):
            return 993


class TestReaderSensors(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        # --- albero sysfs finto ---
        self.sysfs = self.base / "sys"
        cpu = self.sysfs / "devices" / "system" / "cpu"
        cpu.mkdir(parents=True)
        self.online = cpu / "online"
        self.online.write_text("0-11\n")  # 12 thread, SMT2 → 6 core fisici
        cpufreq = cpu / "cpu0" / "cpufreq"
        cpufreq.mkdir(parents=True)
        (cpufreq / "scaling_cur_freq").write_text("3500000\n")  # 3500 MHz
        # --- hwmon finto (amdgpu + nct6686) ---
        self.hwmon = self.base / "hwmon"
        self.hwmon.mkdir()
        amd = self.hwmon / "hwmon0"
        amd.mkdir()
        (amd / "name").write_text("amdgpu\n")
        (amd / "freq1_input").write_text("1500000000\n")  # 1500 MHz
        (amd / "in0_input").write_text("1050\n")          # 1050 mV
        nct = self.hwmon / "hwmon1"
        nct.mkdir()
        (nct / "name").write_text("nct6686\n")
        (nct / "fan1_input").write_text("0\n")
        (nct / "fan2_input").write_text("2090\n")
        (nct / "temp1_input").write_text("30000\n")
        (nct / "temp1_label").write_text("Board\n")
        (nct / "temp2_input").write_text("46000\n")
        (nct / "temp2_label").write_text("System\n")
        # --- debugfs finto (amdgpu_pm_info: VDDGFX + SoC power) ---
        self.debugfs = self.base / "debug"
        dbg = self.debugfs / "dri" / "0000:01:00.0"
        dbg.mkdir(parents=True)
        (dbg / "amdgpu_pm_info").write_text(
            "\t824 mV (VDDGFX)\n"
            "57.82 W (current SoC including CPU)\n")
        # --- conf 40-CU finto ---
        conf_dir = self.base / "etc"
        conf_dir.mkdir()
        self.conf = conf_dir / "bc250-cu-live-manager.conf"
        self.conf.write_text("BC250_WGP_MASKS=0x1f,0x1f,0x1f,0x1f\n")
        # --- /proc/cpuinfo finto ---
        self.cpuinfo = self.base / "cpuinfo"
        self.cpuinfo.write_text(_cpuinfo_text(6))
        # --- systemctl finto di DEFAULT ---
        # governor INATTIVO (rc 3) → gli accessi SMN (VID/core mask) sono
        # permessi; ogni altro servizio → rc 4 (stato sconosciuto → None).
        self.systemctl = self.base / "systemctl"
        self.systemctl.write_text(
            '#!/bin/sh\n'
            'case "$2" in\n'
            '  cyan-skillfish-governor-smu) exit 3 ;;\n'
            '  *) exit 4 ;;\n'
            'esac\n')
        self.systemctl.chmod(0o755)
        self.reader = self._reader()

    def tearDown(self):
        self._tmp.cleanup()

    def _reader(self, **kw):
        kw.setdefault("hwmon_base", str(self.hwmon))
        kw.setdefault("sysfs_base", str(self.sysfs))
        kw.setdefault("debugfs_base", str(self.debugfs))
        kw.setdefault("conf_path", str(self.conf))
        kw.setdefault("online_path", str(self.online))
        kw.setdefault("cpuinfo_path", str(self.cpuinfo))
        kw.setdefault("systemctl_cmd", str(self.systemctl))
        return RealHardwareReader(**kw)

    def _fake_systemctl(self, body):
        """Script systemctl finto (iniettabile come systemctl_cmd)."""
        p = self.base / "fake-systemctl"
        p.write_text(f"#!/bin/sh\n{body}")
        p.chmod(0o755)
        return str(p)

    # ------------------------- CPU ------------------------------- #

    def test_cpu_cores_from_cpuinfo_pairs(self):
        """online '0-11' = 12 thread ma 6 coppie fisiche → 6 core, mai 12."""
        self.assertEqual(self.reader.get_cpu_cores(), 6)

    def test_cpu_cores_fallback_online_smt2(self):
        """cpuinfo illeggibile → fallback online: thread/2 (SMT2)."""
        r = self._reader(cpuinfo_path="/nonexistent/cpuinfo")
        self.assertEqual(r.get_cpu_cores(), 6)

    def test_cpu_cores_none_without_any_source(self):
        """Nessuna fonte (cpuinfo e online assenti) → None."""
        r = self._reader(cpuinfo_path="/nonexistent/cpuinfo",
                         online_path="/nonexistent/online")
        self.assertIsNone(r.get_cpu_cores())

    def test_cpu_freq_khz_to_mhz(self):
        """scaling_cur_freq 3500000 kHz → 3500 MHz (intero)."""
        self.assertEqual(self.reader.get_cpu_freq(), 3500)

    def test_cpu_vid_smu_read(self):
        """VID reale: q3_0x36_get_current_cpu_voltage() → 993 mV."""
        with mock.patch("buo.safety.reader._bc250_smu_import",
                        return_value=_FakeSmuModule):
            self.assertEqual(self.reader.get_cpu_vid(), 993)

    def test_cpu_vid_import_failed_none(self):
        """Import libreria SMU fallito → None, mai un VID inventato."""
        with mock.patch("buo.safety.reader._bc250_smu_import",
                        return_value=None):
            self.assertIsNone(self.reader.get_cpu_vid())

    # ------------------- GATE SMU↔governor (B1) ------------------ #

    def test_cpu_vid_governor_active_returns_none(self):
        """Governor ATTIVO → MAI accesso SMU: VID None (conflitto
        SMU↔governor: accessi concorrenti corrompono il governor)."""
        r = self._reader(systemctl_cmd=self._fake_systemctl("exit 0\n"))
        with mock.patch("buo.safety.reader._bc250_smu_import",
                        return_value=_FakeSmuModule) as imp:
            self.assertIsNone(r.get_cpu_vid())
            imp.assert_not_called()  # l'SMU non viene nemmeno toccata

    def test_cpu_vid_governor_inactive_reads_smu(self):
        """Governor inattivo (rc 3) → si legge il VID reale dall'SMU."""
        r = self._reader(systemctl_cmd=self._fake_systemctl("exit 3\n"))
        with mock.patch("buo.safety.reader._bc250_smu_import",
                        return_value=_FakeSmuModule):
            self.assertEqual(r.get_cpu_vid(), 993)

    def test_cpu_vid_governor_check_error_none(self):
        """systemctl non eseguibile → stato governor sconosciuto → VID
        None (fail-closed: si legge l'SMU solo a governor CONFERMATO
        inattivo)."""
        r = self._reader(systemctl_cmd="/nonexistent/systemctl")
        with mock.patch("buo.safety.reader._bc250_smu_import",
                        return_value=_FakeSmuModule) as imp:
            self.assertIsNone(r.get_cpu_vid())
            imp.assert_not_called()

    def test_governor_ttl_cache(self):
        """Entro il TTL la seconda lettura NON riesegue systemctl
        (cache: il subprocess gira una volta sola)."""
        r = self._reader(governor_ttl=10.0)
        fake_run = mock.Mock(return_value=mock.Mock(returncode=3))
        with mock.patch("buo.safety.reader.subprocess.run", fake_run), \
                mock.patch("buo.safety.reader._bc250_smu_import",
                           return_value=_FakeSmuModule):
            self.assertEqual(r.get_cpu_vid(), 993)
            self.assertEqual(r.get_cpu_vid(), 993)
        self.assertEqual(fake_run.call_count, 1)

    def test_governor_ttl_zero_always_checks(self):
        """TTL=0 → nessuna cache: ogni lettura riesegue systemctl."""
        r = self._reader(governor_ttl=0.0)
        fake_run = mock.Mock(return_value=mock.Mock(returncode=3))
        with mock.patch("buo.safety.reader.subprocess.run", fake_run), \
                mock.patch("buo.safety.reader._bc250_smu_import",
                           return_value=_FakeSmuModule):
            self.assertEqual(r.get_cpu_vid(), 993)
            self.assertEqual(r.get_cpu_vid(), 993)
        self.assertEqual(fake_run.call_count, 2)

    def test_core_mask_governor_active_returns_none(self):
        """Governor ATTIVO → MAI accesso SMN: core mask None (stesso
        gate di get_cpu_vid: il paio PCI config 0xB8/0xBC è condiviso)."""
        r = self._reader(systemctl_cmd=self._fake_systemctl("exit 0\n"))
        with mock.patch("buo.safety.reader.smn.read_core_mask") as m:
            self.assertIsNone(r.get_core_mask())
            m.assert_not_called()

    # ------------------------- CORE MASK ------------------------- #

    def test_core_mask_reads_smn_helper(self):
        """get_core_mask delega a smn.read_core_mask (helper condiviso)."""
        with mock.patch("buo.safety.reader.smn.read_core_mask",
                        return_value=0xFF) as m:
            self.assertEqual(self.reader.get_core_mask(), 0xFF)
            m.assert_called_once_with()

    def test_core_mask_missing_pci_none(self):
        """PCI config space assente → None (mai 0x77/0xFF inventati)."""
        self.assertIsNone(self.reader.get_core_mask())

    def test_smn_read_core_mask(self):
        """smn.read_core_mask: pwrite reg 0x5A870 a 0xB8, pread 0xBC → &0xFF."""
        cfg = self.base / "config"
        cfg.write_bytes(b"")
        with mock.patch("buo.utils.smn.os.pwrite") as pw, \
                mock.patch("buo.utils.smn.os.pread",
                           return_value=struct.pack("<I", 0xFF)):
            self.assertEqual(smn.read_core_mask(str(cfg)), 0xFF)
            pw.assert_called_once_with(mock.ANY,
                                       struct.pack("<I", 0x5A870), 0xB8)

    def test_cpu_unlock_read_core_mask_stock_when_no_pci(self):
        """CPUUnlock senza PCI config → CORE_MASK_STOCK (policy unlock)."""
        from buo.unlock.cpu import CPUUnlock
        with mock.patch("buo.unlock.cpu.os.path.exists", return_value=False):
            self.assertEqual(CPUUnlock(use_wrapper=False).read_core_mask(),
                             0x77)

    def test_cpu_unlock_read_core_mask_delegates_to_helper(self):
        """CPUUnlock delega la lettura SMN all'helper condiviso (0xFF)."""
        from buo.unlock.cpu import CPUUnlock
        with mock.patch("buo.unlock.cpu.os.path.exists", return_value=True), \
                mock.patch("buo.unlock.cpu.smn.read_core_mask",
                           return_value=0xFF) as m:
            self.assertEqual(CPUUnlock(use_wrapper=False).read_core_mask(),
                             0xFF)
            m.assert_called_once_with()

    # ------------------------- GPU ------------------------------- #

    def test_gpu_freq_hz_to_mhz(self):
        """freq1_input 1500000000 Hz → 1500 MHz (hwmon amdgpu)."""
        self.assertEqual(self.reader.get_gpu_freq(), 1500)

    def test_gpu_voltage_vddgfx_fallback(self):
        """hwmon in0 assente + governor INATTIVO → fallback VDDGFX da
        amdgpu_pm_info ('824 mV (VDDGFX)' — regex identica a
        buo/optimize/gpu.py, lettura permessa solo a governor fermo)."""
        (self.hwmon / "hwmon0" / "in0_input").unlink()
        self.assertEqual(self.reader.get_gpu_voltage(), 824)

    def test_gpu_voltage_vddgfx_governor_active_returns_none(self):
        """Governor ATTIVO + in0 assente → VDDGFX MAI letto: None (il
        debugfs interroga l'SMU — mai letture mailbox in concorrenza
        col governor, incidente 30/08)."""
        (self.hwmon / "hwmon0" / "in0_input").unlink()
        r = self._reader(systemctl_cmd=self._fake_systemctl("exit 0\n"))
        with mock.patch("buo.safety.reader.glob.glob") as glob_mock:
            self.assertIsNone(r.get_gpu_voltage())
            glob_mock.assert_not_called()  # amdgpu_pm_info non viene aperto

    def test_gpu_voltage_hwmon_safe_with_governor_active(self):
        """hwmon in0 è SICURO a governor attivo (metrics table cached,
        nessun mailbox): la lettura NON è gated."""
        r = self._reader(systemctl_cmd=self._fake_systemctl("exit 0\n"))
        self.assertEqual(r.get_gpu_voltage(), 1050)

    def test_gpu_voltage_hwmon_wins_over_vddgfx(self):
        """hwmon in0 presente → vince su VDDGFX (ordine: in0 → pm_info)."""
        self.assertEqual(self.reader.get_gpu_voltage(), 1050)

    def test_gpu_cu_from_conf_wgp_bitcount(self):
        """WGP masks 0x1f×4 → popcount(0x1f)=5 → 5×2×4 = 40 CU."""
        self.assertEqual(self.reader.get_gpu_cu(), 40)

    def test_gpu_cu_from_sysfs(self):
        """sysfs num_cu è la fonte preferita quando presente."""
        card = self.sysfs / "class" / "drm" / "card1" / "device"
        card.mkdir(parents=True)
        (card / "num_cu").write_text("40\n")
        self.assertEqual(self.reader.get_gpu_cu(), 40)

    def test_gpu_cu_none_without_sources(self):
        """Nessuna fonte CU (sysfs/conf/wrapper) → None."""
        r = self._reader(conf_path="/nonexistent/conf")
        # isola anche il wrapper live-manager (reale su macchine configurate)
        with mock.patch(
                "buo.unlock.wrappers.bc250_live_manager."
                "BC250LiveManagerWrapper") as wcls:
            wcls.return_value.available = False
            self.assertIsNone(r.get_gpu_cu())

    # ------------------------- POTENZA --------------------------- #

    def test_total_power_from_debugfs(self):
        """Governor INATTIVO → total_power reale da amdgpu_pm_info:
        '57.82 W (current SoC including CPU)' → 57.82 W."""
        self.assertAlmostEqual(self.reader.get_total_power(), 57.82)

    def test_total_power_governor_active_returns_none(self):
        """Governor ATTIVO → pm_info MAI letta: total_power None (il
        debugfs interroga l'SMU via driver — mailbox UNICO — mai in
        concorrenza col governor: incidente 30/08, freeze silenzioso)."""
        r = self._reader(systemctl_cmd=self._fake_systemctl("exit 0\n"))
        with mock.patch("buo.safety.reader.glob.glob") as glob_mock:
            self.assertIsNone(r.get_total_power())
            glob_mock.assert_not_called()  # amdgpu_pm_info non viene aperto

    def test_total_power_governor_check_error_none(self):
        """Stato governor sconosciuto (systemctl non eseguibile) →
        pm_info MAI letta: None (fail-closed: si legge solo a governor
        CONFERMATO inattivo)."""
        r = self._reader(systemctl_cmd="/nonexistent/systemctl")
        with mock.patch("buo.safety.reader.glob.glob") as glob_mock:
            self.assertIsNone(r.get_total_power())
            glob_mock.assert_not_called()

    def test_total_power_none_without_debugfs(self):
        """Governor inattivo ma nessun amdgpu_pm_info → None (mai un
        totale inventato)."""
        r = self._reader(debugfs_base=str(self.base / "empty-debug"))
        self.assertIsNone(r.get_total_power())

    # ------------------------- VENTOLE/TEMP ----------------------- #

    def test_fan_speed_max_nonzero(self):
        """Ventole: fan1=0, fan2=2090 → 2090 (max dei valori non zero)."""
        self.assertEqual(self.reader.get_fan_speed(), 2090)

    def test_fan_speed_none_when_all_zero(self):
        """Tutte le ventole a 0 RPM → None."""
        nct = self.hwmon / "hwmon1"
        (nct / "fan2_input").write_text("0\n")
        self.assertIsNone(self.reader.get_fan_speed())

    def test_ambient_temp_system_label(self):
        """Temp ambiente: temp*_input con label 'System' (46°C)."""
        self.assertAlmostEqual(self.reader.get_ambient_temp(), 46.0)

    def test_ambient_temp_none_without_system_label(self):
        """Nessun label 'System' → None, mai la prima temp qualunque."""
        nct = self.hwmon / "hwmon1"
        (nct / "temp2_label").write_text("Aux\n")
        self.assertIsNone(self.reader.get_ambient_temp())

    # ------------------------- 40-CU ----------------------------- #

    def test_is_40cu_systemctl_active(self):
        """systemctl is-active rc 0 → True."""
        r = self._reader(systemctl_cmd=self._fake_systemctl("exit 0\n"))
        self.assertIs(r.get_is_40cu_enabled(), True)

    def test_is_40cu_systemctl_inactive(self):
        """systemctl is-active rc 3 → False (servizio fermo)."""
        r = self._reader(systemctl_cmd=self._fake_systemctl("exit 3\n"))
        self.assertIs(r.get_is_40cu_enabled(), False)

    def test_is_40cu_systemctl_other_rc_none(self):
        """rc diverso da 0/3 (es. servizio assente, rc 4) → None."""
        r = self._reader(systemctl_cmd=self._fake_systemctl("exit 4\n"))
        self.assertIsNone(r.get_is_40cu_enabled())

    def test_is_40cu_systemctl_missing_none(self):
        """systemctl inesistente → None (fail-soft, mai eccezione)."""
        r = self._reader(systemctl_cmd="/nonexistent/systemctl")
        self.assertIsNone(r.get_is_40cu_enabled())

    # ------------------------- FAIL-SOFT -------------------------- #

    def test_fail_soft_missing_paths_all_none(self):
        """Tutti i percorsi inesistenti → ogni getter None, mai eccezione.

        _bc250_smu_import è patchato a None (M2): anche su una macchina
        con la libreria SMU installata il test resta deterministico."""
        r = RealHardwareReader(hwmon_base="/nonexistent/hwmon",
                               sysfs_base="/nonexistent/sys",
                               debugfs_base="/nonexistent/debug",
                               conf_path="/nonexistent/conf",
                               online_path="/nonexistent/online",
                               cpuinfo_path="/nonexistent/cpuinfo",
                               systemctl_cmd="/nonexistent/systemctl")
        with mock.patch("buo.safety.reader._bc250_smu_import",
                        return_value=None), \
             mock.patch("buo.unlock.wrappers.bc250_live_manager."
                        "BC250LiveManagerWrapper") as wcls:
            wcls.return_value.available = False
            for getter in (r.get_cpu_cores, r.get_cpu_freq, r.get_core_mask,
                           r.get_cpu_vid, r.get_gpu_cu, r.get_gpu_freq,
                           r.get_gpu_voltage, r.get_total_power,
                           r.get_fan_speed, r.get_ambient_temp,
                           r.get_is_40cu_enabled):
                self.assertIsNone(getter(), getter.__name__)

    def test_get_system_info_wires_all_new_fields(self):
        """get_system_info espone TUTTI i campi sensore con valori reali."""
        # M2: import SMU forzato a None — mai accesso SMU reale nei test,
        # anche su macchine dove la libreria bc250_smu è installata.
        with mock.patch("buo.safety.reader._bc250_smu_import",
                        return_value=None):
            info = self.reader.get_system_info()
        self.assertEqual(info["core_mask"], None)   # PCI assente nei test
        self.assertEqual(info["cpu_cores"], 6)
        self.assertEqual(info["cpu_freq"], 3500)
        self.assertEqual(info["cpu_vid"], None)     # import SMU forzato
        self.assertEqual(info["gpu_cu"], 40)
        self.assertEqual(info["gpu_freq"], 1500)
        self.assertAlmostEqual(info["total_power"], 57.82)
        self.assertAlmostEqual(info["ambient_temp"], 46.0)
        self.assertEqual(info["fan_speed"], 2090)
        self.assertIsNone(info["is_40cu_enabled"])  # servizio sconosciuto
        # campi non sensore: invariati (None)
        for key in ("is_undervolted", "is_overclocked", "is_acpi_fixed",
                    "is_tlb_fixed", "is_ace_fixed", "iommu_off",
                    "reboot_count"):
            self.assertIsNone(info[key])

    def test_get_system_info_core_mask_format(self):
        """core_mask nel formato audit '0x%02X' (maiuscolo, 2 cifre),
        anche quando la lettura SMN restituisce un int minuscolo."""
        with mock.patch("buo.safety.reader.smn.read_core_mask",
                        return_value=0xff):
            info = self.reader.get_system_info()
        self.assertEqual(info["core_mask"], "0xFF")


if __name__ == "__main__":
    unittest.main()
