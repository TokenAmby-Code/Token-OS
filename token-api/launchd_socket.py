"""launchd socket-activation helper (macOS).

Unlike systemd, launchd does **not** export activated sockets through
``LISTEN_FDS``; it hands them to the job via ``launch_activate_socket(3)`` in
libSystem. This module wraps that call with ctypes so token-api can serve on a
launchd-owned listening socket.

Why this matters: when token-api restarts (every deploy does
``launchctl kickstart``/SIGTERM + KeepAlive respawn), the old uvicorn process
dies and a new one binds the port. If uvicorn owns the socket, there is a
~1-3s connection-refused window in which incoming hooks — most criticially the
SessionStart registration POST — are dropped fire-and-forget. If launchd owns
the socket, it (and its kernel accept backlog) outlives each uvicorn instance,
so new connections **stall briefly in the kernel** instead of being refused.

``activated_fd`` returns ``None`` whenever no activated socket is present
(non-macOS, dev runs, ``token-restart --from`` local runs, the WSL satellite),
so callers fall back to an ordinary host/port bind.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import sys

logger = logging.getLogger("token_api")


def activated_fd(name: str = "Listeners") -> int | None:
    """Return the first launchd-activated listening fd for ``name``, or ``None``.

    ``None`` means "not launched by launchd with a matching ``Sockets`` entry";
    the caller should fall back to a host/port bind. Any unexpected libSystem
    failure also degrades to ``None`` rather than crashing startup.
    """
    if sys.platform != "darwin":
        return None
    try:
        libsystem = ctypes.CDLL(ctypes.util.find_library("System") or "/usr/lib/libSystem.dylib")
        fn = libsystem.launch_activate_socket
    except (OSError, AttributeError):
        # launch_activate_socket appeared in macOS 10.10; absence => fall back.
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
        # rc is typically ESRCH (3) when not running under launchd, or ENOENT
        # (2) when the Sockets dict has no entry for ``name``.
        logger.debug("launch_activate_socket(%s) rc=%d count=%d", name, rc, count.value)
        return None

    try:
        fd = int(fds_ptr[0])
    finally:
        # launch_activate_socket malloc()s the fd array; the caller must free it.
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "/usr/lib/libc.dylib")
        libc.free(fds_ptr)
    return fd
