#!/usr/bin/env python3
"""Build a deterministic standalone ZIP with Unix executable modes."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import zipfile


EXECUTABLE_NAMES = {"setuptelemt.sh", "cleantelemt.sh"}
EXCLUDED_NAMES = {"AGENTS.md", "__pycache__", ".git", ".agents", "tests", "venv", ".venv"}


def excluded(path: Path) -> bool:
    if any(part in EXCLUDED_NAMES for part in path.parts) or path.suffix in {".pyc", ".pcap", ".pcapng"}:
        return True
    return path.name.startswith("config") and path.suffix == ".yaml" and not path.name.endswith(".example.yaml")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    root_name = source.name
    files = sorted(path for path in source.rglob("*") if path.is_file() and not excluded(path.relative_to(source)))
    with zipfile.ZipFile(args.output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            relative = path.relative_to(source)
            name = (Path(root_name) / relative).as_posix()
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.create_system = 3
            executable = path.name in EXECUTABLE_NAMES or "steps" in relative.parts or ("tools" in relative.parts and path.suffix == ".py")
            info.external_attr = ((0o755 if executable else 0o644) & 0xFFFF) << 16
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    digest = hashlib.sha256(args.output.read_bytes()).hexdigest()
    print(f"{args.output} sha256={digest} files={len(files)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
