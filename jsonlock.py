"""Inter-process read-modify-write lock for the shared JSON state files.

Multiple processes write the same JSON state — each bot's CLI, the Flask server,
the slash-command bot, and the Claude Code hooks. An unsynchronized
load->mutate->save loses whichever write committed first (last-writer-wins).
store.py already guards its own files with this flock pattern; this factors the
same primitive out so the sibling state files (facts, the veto/choice card maps,
…) can share it instead of each re-discovering the race.

Usage — hold the lock across the WHOLE cycle, and read fresh under it:

    with rmw_lock(PATH):
        data = _load()          # read the latest on-disk state, under the lock
        data[k] = v
        _save(data)             # write atomically (tmp + os.replace) inside it

The lock file is ``<path>.lock``; flock is advisory but every cooperating writer
takes it. Atomic writes alone (tmp + rename) stop a reader seeing a half-written
file, but they do NOT stop the lost-update race — that needs this lock.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

try:
    import fcntl  # POSIX only
    _HAVE_FCNTL = True
except ImportError:  # non-POSIX (bare Windows) — degrade to a no-op
    _HAVE_FCNTL = False


@contextmanager
def rmw_lock(path: str):
    """Exclusive inter-process lock spanning a read-modify-write cycle on `path`.

    Yields WITHOUT locking where fcntl is unavailable, so callers still work on
    platforms that lack it (they just don't get cross-process serialization
    there — acceptable, since those hosts run a single writer)."""
    if not _HAVE_FCNTL:
        yield
        return
    lock_path = str(path) + ".lock"
    parent = os.path.dirname(os.path.abspath(lock_path))
    os.makedirs(parent, exist_ok=True)
    lf = open(lock_path, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(lf, fcntl.LOCK_UN)
        finally:
            lf.close()
