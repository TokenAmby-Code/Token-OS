"""launchd socket-activation helper (macOS).

launchd does not export activated sockets through LISTEN_FDS; it hands them to
the job via launch_activate_socket(3) in libSystem. tmuxctld uses this helper to
serve on a launchd-owned listener when the LaunchAgent has a Sockets entry, so
connections to :7778 stall in the kernel across daemon restarts instead of
getting ECONNREFUSED.

``activated_fd`` returns ``None`` whenever no activated socket is present
(non-macOS, dev runs, tests, or a plist without the named Sockets entry), so the
caller falls back to an ordinary host/port bind.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import sys

logger = logging.getLogger("tmuxctld")


def activated_fd(name: str = "Listeners") -> int | None:
    """Return the first launchd-activated listening fd for ``name``, or ``None``."""
    if sys.platform != "darwin":
        return None
    try:
        libsystem = ctypes.CDLL(ctypes.util.find_library("System") or "/usr/lib/libSystem.dylib")
        fn = libsystem.launch_activate_socket
    except (OSError, AttributeError):
        return None

    fn.argtypes = [
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_int)),
        ctypes.POINTER(ctypes.c_size_t),
    ]
    fn.restype = ctypes.c_int

    fds_ptr = ctypes.POINTER(ctypes.c_int)()
    count = ctypes.c_size_t(0)
    rc = fn(name.encode(), ctypes.byref(fds_ptr), ctypes.byref(count))
    if rc != 0 or count.value == 0:
        logger.debug("launch_activate_socket(%s) rc=%d count=%d", name, rc, count.value)
        return None

    try:
        fd = int(fds_ptr[0])
    finally:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "/usr/lib/libc.dylib")
        libc.free(fds_ptr)
    return fd
