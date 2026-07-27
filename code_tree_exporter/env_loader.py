from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(*paths: Path) -> None:
    """Load simple KEY=VALUE files without overriding the process environment."""
    for path in paths:
        if not path.is_file():
            continue
        for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                raise ValueError(f"Invalid .env entry at {path}:{line_number}")
            key, value = line.split("=", 1)
            key = key.strip()
            if not key or not key.replace("_", "A").isalnum() or key[0].isdigit():
                raise ValueError(f"Invalid .env key at {path}:{line_number}")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key, value)
