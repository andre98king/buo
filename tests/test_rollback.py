#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test del rollback a cascata: ordine e gestione errori."""

import unittest

from buo.state.rollback import RollbackManager
from buo.constants import ROLLBACK_ORDER


class TestRollback(unittest.TestCase):
    def test_cascade_order(self):
        """I livelli vengono eseguiti in ordine inverso di applicazione."""
        manager = RollbackManager(mock=True)
        executed = []

        for level in ROLLBACK_ORDER:
            manager.register(level, lambda l=level: executed.append(l) or True)

        ok = manager.rollback()
        self.assertTrue(ok)
        self.assertEqual(executed, ROLLBACK_ORDER)  # dal più recente al più vecchio

    def test_rollback_from_phase(self):
        manager = RollbackManager(mock=True)
        executed = []

        for level in ROLLBACK_ORDER:
            manager.register(level, lambda l=level: executed.append(l) or True)

        manager.rollback(from_phase="gpu_40cu")
        idx = ROLLBACK_ORDER.index("gpu_40cu")
        self.assertEqual(executed, ROLLBACK_ORDER[:idx + 1])

    def test_false_handler_is_not_needed_not_failure(self):
        """False da un handler = livello 'non necessario' (niente da
        ripristinare, es. modulo mai caricato) — NON fallisce l'insieme:
        `buo rollback` su una macchina senza modifiche deve uscire 0."""
        manager = RollbackManager(mock=True)

        def not_needed():
            return False   # es. governor già fermo / file mai scritto

        def ok():
            return True

        manager.register("cpu_overclock", not_needed)
        manager.register("gpu_governor", ok)

        success = manager.rollback()
        self.assertTrue(success)  # non necessario ≠ errore

    def test_exception_is_failure_and_continues(self):
        """Un errore VERO (eccezione) fallisce l'insieme ma non blocca i
        livelli successivi."""
        manager = RollbackManager(mock=True)

        def boom():
            raise RuntimeError("rollback fallito")

        def ok():
            return True

        manager.register("cpu_overclock", boom)
        manager.register("gpu_governor", ok)

        success = manager.rollback()
        self.assertFalse(success)  # eccezione = fallimento reale

    def test_no_handlers_is_graceful(self):
        manager = RollbackManager(mock=True)
        ok = manager.rollback()
        self.assertTrue(ok)  # niente da fare = successo


if __name__ == "__main__":
    unittest.main()
