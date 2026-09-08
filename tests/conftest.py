"""Cross-platform test resource ownership helpers.

Windows does not permit deleting an SQLite database while a connection still
owns a file handle. A number of legacy unittest-style tests register N0TE
memory/store ``close`` callbacks with ``addCleanup`` but delete their temporary
profile tree in ``tearDown``. unittest normally runs registered cleanups after
``tearDown``, which POSIX tolerated and Windows correctly rejects.

Keep production connection semantics unchanged. On Windows only, run the
registered N0TE SQLite-owner close callbacks immediately before unittest calls
``tearDown``. All unrelated cleanup callbacks remain registered and preserve
normal unittest ordering.
"""

from __future__ import annotations

import os
import unittest

from n0te import HeadquartersMemory, LineageStore


if os.name == "nt":
    _original_call_teardown = unittest.TestCase._callTearDown
    _SQLITE_OWNERS = (HeadquartersMemory, LineageStore)

    def _call_teardown_after_n0te_resource_close(self: unittest.TestCase) -> None:
        remaining = []
        for cleanup in reversed(self._cleanups):
            function, args, kwargs = cleanup
            owner = getattr(function, "__self__", None)
            if isinstance(owner, _SQLITE_OWNERS) and getattr(function, "__name__", "") == "close":
                function(*args, **kwargs)
            else:
                remaining.append(cleanup)
        self._cleanups[:] = reversed(remaining)
        _original_call_teardown(self)

    unittest.TestCase._callTearDown = _call_teardown_after_n0te_resource_close
