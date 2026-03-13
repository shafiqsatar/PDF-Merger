import os
import sys


def resource_path(relative_path: str) -> str:
    """Return absolute path to resource, works for dev and PyInstaller builds."""
    if hasattr(sys, "_MEIPASS"):
        base_path = getattr(sys, "_MEIPASS")
    else:
        base_path = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    return os.path.join(base_path, relative_path)
