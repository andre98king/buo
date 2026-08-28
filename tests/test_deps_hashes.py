#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test A7: supply chain — hash SHA-256 dei tool installati."""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


class TestBuildDep(unittest.TestCase):
    """G6: tipo deps 'build' (clone → make → install binario)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def test_build_installs_binary(self):
        mgr = DependencyManager(bin_dir=self._tmp.name)
        checkout = Path(self._tmp.name) / "checkout"
        checkout.mkdir()
        binary = checkout / "bc250_memcfg"
        binary.write_bytes(b"ELFBINARY")
        dep = {"name": "bc250_memcfg", "commit": "abc",
               "binary": "bc250_memcfg"}

        with mock.patch("buo.install.deps.run_command",
                        return_value=(0, "", "")), \
             mock.patch("buo.install.deps.which", return_value="/usr/bin/make"):
            out = mgr._build_and_install(dep, checkout, sudo=False)

        self.assertEqual(out["status"], "ok")
        dest = Path(self._tmp.name) / "bc250_memcfg"
        self.assertTrue(dest.exists())
        self.assertEqual(dest.read_bytes(), b"ELFBINARY")

    def test_build_fails_without_make(self):
        mgr = DependencyManager(bin_dir=self._tmp.name)
        dep = {"name": "bc250_memcfg", "commit": "abc"}
        with mock.patch("buo.install.deps.which", return_value=None):
            out = mgr._build_and_install(dep, Path("/nonexistent"),
                                         sudo=False)
        self.assertEqual(out["status"], "failed")
        self.assertIn("make", out["detail"])

    def test_build_fails_when_make_errors(self):
        mgr = DependencyManager(bin_dir=self._tmp.name)
        checkout = Path(self._tmp.name) / "checkout"
        checkout.mkdir()
        dep = {"name": "bc250_memcfg", "commit": "abc",
               "binary": "bc250_memcfg"}
        with mock.patch("buo.install.deps.run_command",
                        return_value=(1, "", "make: errore")), \
             mock.patch("buo.install.deps.which", return_value="/usr/bin/make"):
            out = mgr._build_and_install(dep, checkout, sudo=False)
        self.assertEqual(out["status"], "failed")
        self.assertIn("make", out["detail"])

    def test_memcfg_in_catalog(self):
        from buo.install.deps import _build_deps
        names = [d["name"] for d in _build_deps()]
        self.assertIn("bc250_memcfg", names)
        dep = next(d for d in _build_deps() if d["name"] == "bc250_memcfg")
        self.assertEqual(dep["type"], "build")
        self.assertTrue(dep["commit"])
