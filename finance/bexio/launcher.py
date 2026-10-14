"""Load a launcher's python body as a module.

The launchers are bash files (their first line is an av launcher for /bin/bash):
av runs a Blessed Script as /dev/fd/N, and the mini's Python 3.9 reads a /dev/fd
script from where its zip check left the shared offset, so a script of more than
a few bytes runs empty and exits 0 (IT, 2026-10-14). The python therefore travels
on stdin, between the heredoc markers inside the one blessed file, and the file
itself is not importable python. load() takes what stands between the markers and
runs it as a module: login_helper loads oauth this way, and the tests load every
launcher this way. Standard library only.
"""

import os
import sys
import types


def load(filename):
    """Run the python body of the launcher `filename` next to this file, as a module."""
    name = filename[: -len(".py")] if filename.endswith(".py") else filename
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    with open(path) as f:
        lines = f.read().split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith("exec ") and "<<" in line)
    tag = lines[start].split("<<", 1)[1].strip().strip("'")
    end = lines.index(tag, start + 1)
    module = sys.modules[name] = types.ModuleType(name)
    module.__file__ = path
    exec(compile("\n".join(lines[start + 1:end]) + "\n", path, "exec"), module.__dict__)
    return module
