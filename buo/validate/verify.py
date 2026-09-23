#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Fix Verification — verifica che ogni fix sia attivo e funzionante.

Metodi di verifica (dal design finale, messaggio 100) — sempre l'EFFETTO
reale a runtime, mai la presenza di un file/conf/nome:
    • 8-core CPU      → core FISICI + thread online (sysfs e /proc/cpuinfo)
    • 40-CU GPU       → CU attive dal punto unico dell'audit (sysfs/UMR)
    • TLB fix         → assenza di crash in carichi compute
    • ACE fix         → vkmark con compute (FPS >= baseline)
    • IOMMU           → attivo (iommu=off ASSENTE: è lo stato corretto)
    • ACPI fix        → ACPIFix.verify(): entry del deployment BOOTATO
                        (su ostree i nomi SSDT*CST non sopravvivono)
    • Governor        → systemctl show -p ActiveState
"""

from pathlib import Path
from typing import Any, Dict, List

from ..utils.logging import LoggerMixin


class FixVerifier(LoggerMixin):
    """Verifica lo stato di ogni fix applicato."""

    def __init__(self, mock: bool = False, mock_hardware=None):
        self.mock = mock
        self.mock_hw = mock_hardware

    # ------------------------------------------------------------------ #

    def verify_all(self, applied_fixes: List[str]) -> Dict[str, Dict[str, Any]]:
        """Verifica la lista dei fix applicati."""
        results: Dict[str, Dict[str, Any]] = {}
        checkers = {
            "cpu_core_unlock": self._check_cpu_cores,
            "gpu_40cu": self._check_gpu_cu,
            "iommu": self._check_iommu,
            "acpi_fix": self._check_acpi,
            "governor": self._check_governor,
            "gpu_mask": self._check_gpu_mask,
            "gtt_tuning": self._check_gtt,
            "fan_control": self._check_fan,
            "vram_config": self._check_vram,
        }
        for fix in applied_fixes:
            checker = checkers.get(fix)
            if checker is None:
                results[fix] = {"ok": None, "detail": "nessuna verifica definita"}
                continue
            try:
                ok, detail = checker()
                results[fix] = {"ok": ok, "detail": detail}
            except Exception as e:
                results[fix] = {"ok": False, "detail": str(e)}
        return results

    # --------------------------- checkers ---------------------------- #

    def _check_cpu_cores(self):
        """8 core FISICI / 16 thread reali: l'EFFETTO a runtime.

        Il conteggio delle righe `processor` di /proc/cpuinfo sono i THREAD
        SMT: una macchina 6c/12t superava il check `>= 8` (falso positivo di
        campo). Punti unici riusati: `HardwareAudit._count_cpuinfo` (core
        fisici, coppie physical/core id) e `cpu_online_count` (thread online).
        Fail-closed: conteggio non determinabile → "non verificabile", mai ok.
        """
        if self.mock and self.mock_hw is not None:
            ok = self.mock_hw.read_core_mask() == 0xFF
            return ok, "8 core (mock)" if ok else "6 core (mock)"
        from ..audit.hardware import HardwareAudit
        from ..unlock.validation import cpu_online_count
        cores = HardwareAudit._count_cpuinfo()
        threads = cpu_online_count()
        if not cores:
            return None, "core fisici non leggibili (/proc/cpuinfo) — non verificabile"
        if threads is None:
            return None, "thread online non leggibili (sysfs) — non verificabile"
        return cores >= 8 and threads >= 16, \
            f"{cores} core fisici, {threads} thread online (attesi 8/16)"

    @staticmethod
    def _gpu_effect():
        """(CU, fonte) dal punto unico dell'audit.

        `_audit_gpu` risolve l'ordine giusto (sysfs num_cu → runtime UMR/
        live-manager) e marca il dato che viene dal solo conf: nessuna
        seconda implementazione della lettura CU.
        """
        from ..audit.hardware import HardwareAudit
        gpu = HardwareAudit()._audit_gpu()
        return gpu.get("cu_count"), gpu.get("cu_source") or ""

    def _check_gpu_cu(self):
        """CU attive: lettura reale (sysfs o UMR/live-manager a runtime).

        Il vecchio loop su `card*/num_cu` non trovava nulla su questo path
        ostree (il file non esiste) e l'ordine di `iterdir` non è
        deterministico → falso negativo sistematico.
        """
        if self.mock and self.mock_hw is not None:
            cu = self.mock_hw.get_cu_count()
            return cu >= 38, f"{cu} CU (mock)"
        cu, source = self._gpu_effect()
        if cu is None:
            return None, ("CU non determinabili (num_cu assente e "
                          "live-manager muto) — non verificabile")
        if source.startswith("conf"):
            return None, (f"{cu} CU ma solo dal {source}: è persistenza, "
                          "non un effetto — non verificabile")
        return cu >= 38, f"{cu} CU ({source})"

    def _check_iommu(self):
        if self.mock and self.mock_hw is not None:
            ok = not self.mock_hw.state.iommu_off
            return ok, "IOMMU attivo (mock)" if ok else "iommu=off (mock)"
        try:
            with open("/proc/cmdline") as f:
                cmd = f.read()
            ok = "iommu=off" not in cmd and "iommu=pt" not in cmd
            return ok, ("IOMMU attivo ✓" if ok
                        else "iommu=off presente ⚠️ (rimuoverlo)")
        except Exception as e:
            return False, str(e)

    def _check_acpi(self):
        """Tabelle ACPI: su ostree il NOME non sopravvive.

        Il kernel fonde gli override negli slot SSDT1..N → cercare "CST" nei
        nomi dà un falso negativo col fix attivo. Fonte unica: ACPIFix.verify()
        (tabelle attive: dal firmware moddato o dalla entry del deployment
        BOOTATO), la stessa cosa che guarda il gate.
        """
        if self.mock and self.mock_hw is not None:
            return self.mock_hw.state.is_acpi_fixed, "CST presente (mock)"
        from ..fix.acpi import ACPIFix
        fix = ACPIFix()
        if fix.distro.initramfs_tool == "ostree":
            ok = bool(fix.verify())
            return ok, ("tabelle ACPI attive (firmware o entry bootata)" if ok
                        else "nessuna tabella ACPI (entry bootata o firmware)")
        tables = Path("/sys/firmware/acpi/tables")
        try:
            ssdt = [p.name for p in tables.glob("SSDT*")] if tables.exists() else []
            ok = any("CST" in s for s in ssdt)
            return ok, ", ".join(ssdt) if ssdt else "nessuna tabella SSDT"
        except Exception as e:
            return False, str(e)

    def _check_governor(self):
        if self.mock and self.mock_hw is not None:
            return True, "governor attivo (mock)"
        # Stato da `systemctl show -p ActiveState` (punto unico in
        # optimize/governor.py): "activating" NON è "attivo" e nemmeno
        # "fermo" — il report deve dire lo stato vero, non rc=3.
        from ..optimize.governor import governor_states
        state = governor_states().get("ActiveState") or "sconosciuto"
        return state == "active", state

    def _check_gpu_mask(self):
        """EFFETTO (CU instradate a runtime), non il file di conf.

        /etc/modprobe.d/bc250-40cu-selective-mask.conf è INERTE su ostree
        (lezione GTT: i parametri entrano solo dall'initramfs) e sul campo
        nessuno lo scrive più → verificare il file era scollegato
        dall'hardware. Non determinabile a runtime → "non verificabile".
        """
        if self.mock and self.mock_hw is not None:
            return True, "maschera applicata (mock)"
        cu, source = self._gpu_effect()
        if cu is None or source.startswith("conf"):
            return None, (f"CU instradate non leggibili a runtime "
                          f"(fonte: {source or 'ignota'}) — non verificabile")
        return True, f"{cu} CU instradate ({source})"

    def _check_gtt(self):
        """Tetto VRAM dinamica EFFETTIVO: `ttm.pages_limit` ≥ richiesto.

        Il meccanismo documentato BC-250 è il karg (non modprobe.d): si
        verifica il valore REALE a runtime e, se il parametro non è
        leggibile, la presenza del karg nel cmdline.
        """
        if self.mock and self.mock_hw is not None:
            return True, "gtt tuning (mock)"
        try:
            from ..fix import gtt as gtt_mod
            value = int(Path(gtt_mod.GTT_PARAM_PATH).read_text().strip())
            if value >= gtt_mod.GTT_LIMIT_DEFAULT:
                return True, f"ttm.pages_limit={value} (runtime)"
            return False, (f"ttm.pages_limit={value} (atteso "
                           f"≥{gtt_mod.GTT_LIMIT_DEFAULT}: tetto dinamico "
                           "non alzato)")
        except Exception:
            pass
        try:
            cmdline = Path(gtt_mod.CMDLINE_PATH).read_text()
            if f"{gtt_mod.GTT_KARG}={gtt_mod.GTT_LIMIT_DEFAULT}" in cmdline:
                return False, ("karg configurato ma NON attivo "
                               "(runtime non leggibile: reboot?)")
        except OSError:
            pass
        return False, "gtt tuning non attivo"

    def _check_fan(self):
        """Sensori/PWM SuperIO EFFETTIVI, non `lsmod`.

        Il modulo caricato può non esporre nulla (nct6683 senza `force=true`)
        → il fix risultava "ok" senza alcun sensore. Stesso punto unico di
        FanControl.verify() (`sensor_effect`), motivo incluso nel dettaglio.
        """
        if self.mock and self.mock_hw is not None:
            return True, "sensori nct6686 (mock)"
        from ..fix import fan as fan_mod
        return fan_mod.sensor_effect(fan_mod.HWMON_BASE)

    def _check_vram(self):
        """VRAM config: verificabile solo se bc250_memcfg è stato applicato
        (nessun marker affidabile → non verificabile automaticamente)."""
        if self.mock and self.mock_hw is not None:
            return True, "vram config (mock)"
        # Nessun metodo affidabile: il fix è manuale (bc250_memcfg).
        return None, "vram config manuale (bc250_memcfg) — non verificabile"
