import re
import string
import unicodedata
from pathlib import Path


class OperationCancelled(Exception):
    """Raised when the user cancels the operation."""

    pass


class Utils:
    """General utility static methods."""

    @staticmethod
    def sanitize_filename(s: str, max_len: int = 200) -> str:
        if not s:
            return ""
        s = str(s).strip()
        # Remove illegal chars
        s = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "", s)
        s = re.sub(r"\s+", " ", s)
        if len(s) > max_len:
            s = s[:max_len].rstrip()
        return s

    @staticmethod
    def normalize_text(s: str) -> str:
        s = s or ""
        s = unicodedata.normalize("NFKD", s).lower()
        s = s.translate(str.maketrans("", "", string.punctuation))
        return " ".join(s.split())

    @staticmethod
    def unique_path(path: Path) -> Path:
        if not path.exists():
            return path
        i = 1
        while True:
            candidate = path.parent / f"{path.stem} ({i}){path.suffix}"
            if not candidate.exists():
                return candidate
            i += 1
