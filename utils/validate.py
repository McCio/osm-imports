"""osmium validation helpers."""

import subprocess
from pathlib import Path


def _rel(path: str) -> str:
    try:
        return str(Path(path).relative_to(Path.cwd()))
    except ValueError:
        return path


def validate_file(path: str) -> bool:
    rel_path = _rel(path)
    for cmd in (["osmium", "check-refs", rel_path], ["osmium", "fileinfo", "-e", rel_path]):
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.stdout:
            for line in result.stdout.splitlines():
                print(f"    {line}")
        if result.returncode != 0:
            print(f"  [error   ] {rel_path}: {result.stderr.strip()}")
            return False
    return True
