import os
from typing import Iterable, List, Tuple
from datetime import datetime

from pypdf import PdfReader


def normalize_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def is_pdf_file(path: str) -> bool:
    return os.path.isfile(path) and path.lower().endswith(".pdf")


def unique_pdf_paths(paths: Iterable[str]) -> List[str]:
    seen = set()
    unique = []
    for path in paths:
        norm = normalize_path(path)
        if norm not in seen and is_pdf_file(path):
            seen.add(norm)
            unique.append(path)
    return unique


def get_pdf_page_count(path: str) -> Tuple[bool, int, str]:
    """
    Returns (ok, page_count, error_message).
    Uses pypdf to count pages and gracefully handles corrupted PDFs.
    """
    try:
        reader = PdfReader(path)
        return True, len(reader.pages), ""
    except Exception as exc:  # pypdf throws a variety of exceptions
        return False, 0, str(exc)


def get_file_size_bytes(path: str) -> int:
    return os.path.getsize(path)


def get_file_modified_timestamp(path: str) -> float:
    return os.path.getmtime(path)


def format_bytes(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.0f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


def format_modified(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%d %B %Y %H:%M:%S")
