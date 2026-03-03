from __future__ import annotations

import platform
import sys


def _get_glibc_version() -> tuple[int, int] | None:
    if sys.platform != "linux":
        return None
    try:
        libc_ver = platform.libc_ver()
    except OSError:
        return None
    if libc_ver[0] != "glibc":
        return None
    parts = libc_ver[1].split(".")
    return (int(parts[0]), int(parts[1]))


MIN_SUPPORTED_GLIBC = (2, 27)
GLIBC_VERSION = _get_glibc_version()
GLIBC_UNSUPPORTED = GLIBC_VERSION and GLIBC_VERSION < MIN_SUPPORTED_GLIBC
