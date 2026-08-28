#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test A7: supply chain — hash SHA-256 dei tool installati."""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from buo.install.deps import DependencyManager
from buo.utils.paths import state_dir


class TestDepsHashes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def test_record_hash_creates_file(self):
        mgr = DependencyManager()
        script = Path(self._tmp.name) / "script.sh"
        content = b"#!/bin/sh\necho hi\n"
        script.write_bytes(content)
        dep = {"name": "bc250-test", "commit": "abc123"}
        mgr._record_hash(dep, {"src": "script.sh"}, script)

        path = state_dir() / "deps-hashes.json"
        self.assertTrue(path.exists())
        data = json.loads(path.read_text(encoding="utf-8"))
        entry = data.get(str(script))
        self.assertIsNotNone(entry)
        self.assertEqual(
            entry["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(entry["commit"], "abc123")
        self.assertEqual(entry["dest"], str(script))

    def test_record_hash_updates_existing_file(self):
        mgr = DependencyManager()
        script = Path(self._tmp.name) / "script.sh"
        script.write_bytes(b"v1")
        mgr._record_hash({"name": "x", "commit": "c1"},
                         {"src": "s"}, script)
        # seconda installazione: hash aggiornato
        script.write_bytes(b"v2")
        mgr._record_hash({"name": "x", "commit": "c2"},
                         {"src": "s"}, script)
        data = json.loads(
            (state_dir() / "deps-hashes.json").read_text(encoding="utf-8"))
        self.assertEqual(data[str(script)]["sha256"],
                         hashlib.sha256(b"v2").hexdigest())

    def test_record_hash_unreadable_file_does_not_raise(self):
        mgr = DependencyManager()
        missing = Path(self._tmp.name) / "non-esiste.sh"
        mgr._record_hash({"name": "x", "commit": "c"},
                         {"src": "s"}, missing)  # non deve sollevare


if __name__ == "__main__":
    unittest.main()
