# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Paliverse
"""Write-path link guard: user links are refused, root-owned POSIX system links are layout."""
from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.support import scratch  # noqa: F401  (puts src/ on sys.path)

from workboard import core

POSIX = os.name != "nt"


class LinkGuardTest(unittest.TestCase):
    @unittest.skipUnless(POSIX, "creating symlinks on Windows needs extra privileges")
    def test_user_owned_link_in_the_path_is_refused(self):
        with scratch() as base:
            (base / "real").mkdir()
            os.symlink(base / "real", base / "link")
            with self.assertRaises(core.WorkflowError) as caught:
                core.require_write_scope(base / "link" / "board" / "board.json")
            self.assertEqual(caught.exception.status, 403)

    @unittest.skipUnless(POSIX, "POSIX ownership rule")
    def test_root_owned_link_is_system_layout_but_a_user_link_is_not(self):
        link = Path("/fake/system-link")
        for uid, refused in ((0, False), (os.getuid() or 1000, True)):
            fake = SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=uid)
            with mock.patch.object(core.os, "lstat", return_value=fake):
                self.assertIs(core._has_reparse_point(link), refused, uid)

    def test_unresolved_system_temp_dir_is_writable(self):
        # On macOS the temp dir lives under /var -> /private/var, a root-owned link.
        base = Path(tempfile.mkdtemp(prefix="wb-paths-"))
        try:
            self.assertTrue(core.require_write_scope(base / "board" / "board.json").is_absolute())
        finally:
            os.rmdir(base)


if __name__ == "__main__":
    unittest.main()
