"""Portable subprocess environments for external campaign runtimes."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def foundry_environment() -> dict[str, str]:
    """Return an environment that can load Homebrew libusb on macOS hosts."""
    environment = dict(os.environ)
    if sys.platform != "darwin" or environment.get("DYLD_LIBRARY_PATH"):
        return environment
    candidates = (
        Path.home() / ".homebrew/opt/libusb/lib",
        Path("/usr/local/opt/libusb/lib"),
        Path("/opt/homebrew/opt/libusb/lib"),
    )
    library_path = next((path for path in candidates if path.is_dir()), None)
    if library_path is not None:
        environment["DYLD_LIBRARY_PATH"] = str(library_path)
    return environment
