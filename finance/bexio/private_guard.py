"""Unit tests run against a temporary private folder, never the company's real one.

private/ holds the company's data; a test that touches it could overwrite the
problems list of a real dry run. A test module pulls the fixture in with one
line at the end of its import block:

    from private_guard import setUpModule  # unittest calls it before the first test

setUpModule re-points every path under the real private folder that the
imported bexio modules hold as a module constant (PRIVATE, and constants
derived from it) at one shared temp folder, sets BEXIO_PRIVATE for anything
imported later, and wraps every write_private-style helper so a write that
still reaches the real folder fails the test instead of writing through.
Scripts keep their behaviour: BEXIO_PRIVATE (optional) overrides the folder
they use, unset they read the real path, as documented in README.md.
Standard library only.
"""

import os
import shutil
import sys
import tempfile
import unittest

REAL = "/Users/bsaladin/ws_yardr_finance/private"
WRITERS = ("write_private", "write_private_bytes", "_write_private")

_temp = None


def setUpModule():
    """Point the real private folder at one shared temp folder for this test run."""
    global _temp
    if _temp is None:
        _temp = tempfile.mkdtemp(prefix="bexio-test-private-")
        os.environ["BEXIO_PRIVATE"] = _temp
    real = os.path.realpath(REAL)
    here = os.path.dirname(os.path.abspath(__file__))
    for mod in list(sys.modules.values()):
        path = getattr(mod, "__file__", None)
        if not path or mod is sys.modules[__name__]:
            continue
        if os.path.dirname(os.path.abspath(path)) != here:
            continue
        for name, value in list(vars(mod).items()):
            if isinstance(value, str) and (value == real or value.startswith(real + os.sep)):
                setattr(mod, name, _temp + value[len(real):])
            elif callable(value) and getattr(value, "__name__", None) in WRITERS \
                    and not getattr(value, "_private_guarded", False):
                setattr(mod, name, _guarded(value, real))
    unittest.addModuleCleanup(shutil.rmtree, _temp, ignore_errors=True)


def _guarded(write, real):
    def guarded(path, *args, **kwargs):
        target = os.path.abspath(path)
        assert not (target == real or target.startswith(real + os.sep)), \
            "a test wrote into the real private folder: {}".format(path)
        return write(path, *args, **kwargs)
    guarded._private_guarded = True
    return guarded
