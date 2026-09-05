#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
CPU 8-Core Unlock (volatile) — via bc250-unlock-cores.py o accesso SMN
diretto (replica esatta della sequenza confermata nel codice sorgente).

  • maschera core: SMN 0x5A870, 0x77 (6c) → 0xFF (8c)
  • messaggio SMU: Queue 3, 0x98 (scrittura SMN ungated privilegiata)
  • volatile: un cold boot ripristina 6 core
  • prerequisiti: root, governor FERMO, maschera 0x77 (o -f a rischio)
"""

import os
import struct
import time
from typing import Any, Dict, Optional

from ..constants import (CORE_MASK_REG, CORE_MASK_STOCK, CORE_MASK_UNLOCKED,
                         PCI_CONFIG_PATH, SMU_DONE_STATES, SMU_MSG_WRITE_FF,
                         SMU_Q3_ARG, SMU_Q3_CMD, SMU_Q3_RSP, SMU_RETURN_OK)
from ..exceptions import SafetyViolation, TimeoutError
from ..utils import smn
from ..utils.logging import LoggerMixin
from .wrappers.bc250_unlock import BC250UnlockWrapper


class CPUUnlock(LoggerMixin):
    """Sblocco dei core CPU (6 → 8)."""

    def __init__(self, mock: bool = False, mock_hardware=None,
                 use_wrapper: bool = True):
        self.mock = mock
        self.mock_hw = mock_hardware
        self.wrapper = BC250UnlockWrapper() if use_wrapper and not mock else None

    # ------------------------------------------------------------------ #

    def read_core_mask(self) -> int:
        """Legge la maschera core corrente (0x77 o 0xFF)."""
        if self.mock and self.mock_hw is not None:
            return self.mock_hw.read_core_mask()

        if not os.path.exists(PCI_CONFIG_PATH):
            return CORE_MASK_STOCK  # hardware non presente: assumi stock

        # Lettura SMN via helper condiviso (buo/utils/smn.py): stessa
        # sequenza PCI config di sempre (pwrite 0xB8 / pread 0xBC).
        return smn.read_core_mask()

    # ------------------------------------------------------------------ #

    def unlock(self, force: bool = False) -> Dict[str, Any]:
        """
        Esegue l'unlock dei core.

        Returns:
            {"mask": ..., "unlocked": bool, "needs_reboot": bool,
             "changed": bool}  # changed=False se i core erano già 8
        """
        before = self.read_core_mask()
        already_unlocked = (before & 0xFF) == CORE_MASK_UNLOCKED

        if self.mock and self.mock_hw is not None:
            ok = self.mock_hw.write_core_mask(CORE_MASK_UNLOCKED)
            return {
                "mask": hex(self.mock_hw.read_core_mask()),
                "unlocked": ok,
                "needs_reboot": not already_unlocked,
                "changed": not already_unlocked,
            }

        # Via script esterno (metodo consigliato, verificato)
        if self.wrapper is not None and self.wrapper.available:
            result = self.wrapper.unlock(force=force)
            parsed = result.get("parsed_output", {})
            after = parsed.get("after_mask")
            changed = (
                not already_unlocked
                and parsed.get("success", False)
                and after == "0x000000FF"
            )
            return {
                "mask": after,
                "unlocked": parsed.get("success", False),
                "needs_reboot": parsed.get("needs_reboot", False) and not already_unlocked,
                "changed": changed,
                "stdout": result.get("stdout", ""),
            }

        # Fallback: accesso SMN diretto (replica del codice sorgente)
        result = self._unlock_direct(force=force)
        result["changed"] = not already_unlocked
        result["needs_reboot"] = (
            result.get("needs_reboot", False) and not already_unlocked
        )
        return result

    def _unlock_direct(self, force: bool = False) -> Dict[str, Any]:
        """Accesso SMN/SMU diretto — identico a bc250-unlock-cores.py."""
        if not os.path.exists(PCI_CONFIG_PATH):
            raise SafetyViolation("PCI config space non trovato — impossibile "
                                  "accedere all'hardware")

        fd = os.open(PCI_CONFIG_PATH, os.O_RDWR)
        try:
            before = self._smn_read(fd, CORE_MASK_REG)
            self.logger.info("core presence mask: 0x%08X", before)

            if before & 0xFF == CORE_MASK_UNLOCKED:
                self.logger.info("Core già sbloccati — reboot per attivarli")
                return {"mask": "0xFF", "unlocked": True, "needs_reboot": True}

            if before & 0xFF != CORE_MASK_STOCK and not force:
                raise SafetyViolation(
                    "Maschera core non è 0x77: alta probabilità di core "
                    "difettosi — fermo (usa force=True a tuo rischio)"
                )

            st = self._smu_send(fd, SMU_MSG_WRITE_FF, CORE_MASK_REG)
            if st != SMU_RETURN_OK:
                raise RuntimeError(
                    f"Q3 0x{SMU_MSG_WRITE_FF:02X} restituito 0x{st:02X} — "
                    "il governor è fermo?"
                )
            time.sleep(0.2)

            after = self._smn_read(fd, CORE_MASK_REG)
            self.logger.info("after write: 0x%08X", after)
            if after & 0xFF != CORE_MASK_UNLOCKED:
                raise RuntimeError("la maschera non è diventata 0xFF")

            self.logger.info("OK. reboot per attivare 8 core (16 thread).")
            return {"mask": "0xFF", "unlocked": True, "needs_reboot": True}
        finally:
            os.close(fd)

    # --------------------- SMN/SMU primitives ------------------------ #

    @staticmethod
    def _smn_read(fd: int, reg: int) -> int:
        os.pwrite(fd, struct.pack("<I", reg), 0xB8)
        return struct.unpack("<I", os.pread(fd, 4, 0xBC))[0]

    @staticmethod
    def _smu_write(fd: int, reg: int, val: int) -> None:
        os.pwrite(fd, struct.pack("<I", reg), 0xB8)
        os.pwrite(fd, struct.pack("<I", val), 0xBC)

    def _smu_send(self, fd: int, msg: int, arg: int, budget: float = 5.0) -> int:
        end = time.monotonic() + budget
        while time.monotonic() < end:
            st = self._smn_read(fd, SMU_Q3_RSP)
            if st in SMU_DONE_STATES:
                break
            time.sleep(0.002)
        else:
            raise TimeoutError("SMU queue 3 timeout — abort, non ritentare")

        self._smu_write(fd, SMU_Q3_RSP, 0)
        self._smu_write(fd, SMU_Q3_ARG, arg)
        self._smu_write(fd, SMU_Q3_ARG + 4, 0)
        self._smu_write(fd, SMU_Q3_CMD, msg)

        end = time.monotonic() + budget
        while time.monotonic() < end:
            st = self._smn_read(fd, SMU_Q3_RSP)
            if st in SMU_DONE_STATES:
                return st
            time.sleep(0.002)
        raise TimeoutError("SMU queue 3 response timeout")

    # ------------------------------------------------------------------ #

    def rollback(self) -> bool:
        """Rollback: la modifica è volatile, basta un reboot."""
        self.logger.info("CPU unlock: volatile — un reboot ripristina 6 core")
        return True

    def revert_to_stock(self) -> Dict[str, Any]:
        """Ripristina la maschera stock 0x77 (revert 8→6 core, D5).

        MECCANICA VERIFICATA sul sorgente pinnato (design D5): il msg SMU
        Q3 0x98 scrive SOLO 0xFF (nell'handler il valore è fisso:
        ``smn_window_write(0, arg, 0xff, 2)``) → NON è utilizzabile per
        scrivere 0x77. Si usa la scrittura SMN diretta (stesso paio PCI
        0xB8/0xBC di ``_smu_write``) con READBACK 0x77 di verifica.

        Attenzione (evidenza community): le scritture host alla core
        presence mask sono SILENZIOSAMENTE DROPPATE (solo l'SMU può
        scriverla, e solo 0xFF) — sul campo questa scrittura fallirà il
        readback e il chiamante NON deve riavviare: verdetto scritto,
        auto-unlock di boot disabilitato, interruzione controllata con
        istruzione power-off (il cold boot ripristina 6 core da solo).
        """
        if self.mock and self.mock_hw is not None:
            before = self.mock_hw.read_core_mask()
            ok = self.mock_hw.write_core_mask(CORE_MASK_STOCK)
            mask = self.mock_hw.read_core_mask()
            return {"reverted": ok and (mask & 0xFF) == CORE_MASK_STOCK,
                    "mask": hex(mask),
                    "already": (before & 0xFF) == CORE_MASK_STOCK}

        if not os.path.exists(PCI_CONFIG_PATH):
            return {"reverted": False,
                    "error": "PCI config space non trovato — il cold "
                             "boot ripristina la maschera stock"}

        fd = os.open(PCI_CONFIG_PATH, os.O_RDWR)
        try:
            before = self._smn_read(fd, CORE_MASK_REG)
            if before & 0xFF == CORE_MASK_STOCK:
                self.logger.info("revert CPU: maschera già 0x77 (stock)")
                return {"reverted": True, "mask": hex(before & 0xFF),
                        "already": True}

            # Scrittura SMN diretta (nessun msg SMU: 0x98 → solo 0xFF).
            self._smu_write(fd, CORE_MASK_REG, CORE_MASK_STOCK)
            time.sleep(0.2)
            after = self._smn_read(fd, CORE_MASK_REG)
            if after & 0xFF == CORE_MASK_STOCK:
                self.logger.info(
                    "revert CPU: maschera 0x77 scritta e verificata — "
                    "reboot per 12 thread")
                return {"reverted": True, "mask": hex(after & 0xFF)}
            self.logger.error(
                "revert CPU IMPOSSIBILE: readback 0x%02X != 0x77 — la "
                "scrittura host alla core mask è stata scartata "
                "(power-off richiesto: il cold boot ripristina 6 core)",
                after & 0xFF)
            return {"reverted": False, "mask": hex(after & 0xFF),
                    "error": "readback != 0x77 dopo la scrittura SMN"}
        finally:
            os.close(fd)
