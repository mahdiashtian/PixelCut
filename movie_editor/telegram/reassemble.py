"""Standalone, standard-library-only recovery for PixelCut byte parts."""

import argparse
import hashlib
import json
import re
from pathlib import Path


def filename(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"pixelcut-[a-f0-9]+\.[a-z0-9.]+", value):
        raise ValueError("Invalid export filename")
    return value


def restore(manifest_path: Path) -> Path:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    folder = manifest_path.resolve().parent
    output = folder / filename(manifest["name"])
    parts = manifest["parts"]
    if not parts or len({part["name"] for part in parts}) != len(parts):
        raise ValueError("Missing or duplicate parts")
    for part in parts:
        path = folder / filename(part["name"])
        if not path.is_file() or path.stat().st_size != part["size"]:
            raise ValueError(f"Missing or wrong-size part: {path.name}")
    digest = hashlib.sha256()
    # Never overwrite an existing export. Delete an incomplete recovery on any error.
    with output.open("xb") as target:
        try:
            for part in parts:
                part_hash = hashlib.sha256()
                with (folder / part["name"]).open("rb") as source:
                    while block := source.read(4 * 1024**2):
                        target.write(block)
                        digest.update(block)
                        part_hash.update(block)
                if part_hash.hexdigest() != part["sha256"]:
                    raise ValueError(f"Corrupted part: {part['name']}")
            if target.tell() != manifest["size"] or digest.hexdigest() != manifest["sha256"]:
                raise ValueError("Export checksum mismatch")
        except BaseException:
            target.close()
            output.unlink(missing_ok=True)
            raise
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore a PixelCut export without transcoding")
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    print(f"Restored and SHA-256 verified: {restore(args.manifest)}")


if __name__ == "__main__":
    main()
