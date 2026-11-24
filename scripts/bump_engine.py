"""Point engine.toml at the latest slab-sweep release (used by the bump-engine workflow).

Standard library only::

    python scripts/bump_engine.py            # latest release; prints the new version, if any
    python scripts/bump_engine.py --tag v1.2.0

Finds the release's wheel, downloads it, records its SHA-256 and rewrites the ``[slab-sweep]``
values in ``engine.toml`` (comments are kept). Exits 0 either way; when ``GITHUB_OUTPUT`` is set it
writes ``changed=true|false`` and ``version=...`` there for the workflow.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = "https://api.github.com/repos/ALMO-SLF-ETH/slab-sweep/releases"


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=60) as response:
        data: bytes = response.read()
    return data


def find_release(tag: str | None) -> tuple[str, str, str]:
    """``(version, wheel file name, wheel download URL)`` of a release (latest by default)."""
    url = f"{API}/tags/{tag}" if tag else f"{API}/latest"
    release = json.loads(_get(url))
    version = str(release["tag_name"]).removeprefix("v")
    wheels = [a for a in release.get("assets", []) if str(a["name"]).endswith(".whl")]
    if len(wheels) != 1:
        raise SystemExit(f"release {release['tag_name']} has {len(wheels)} wheels, expected 1")
    return version, str(wheels[0]["name"]), str(wheels[0]["browser_download_url"])


def update_pin(text: str, version: str, wheel: str, sha256: str) -> str:
    """``engine.toml`` text with the three ``[slab-sweep]`` values replaced, comments kept."""
    for key, value in (("version", version), ("wheel", wheel), ("sha256", sha256)):
        text, n = re.subn(rf'(?m)^{key} = "[^"\r\n]*"', f'{key} = "{value}"', text, count=1)
        if n != 1:
            raise SystemExit(f"engine.toml has no '{key} = \"...\"' line")
    pin = tomllib.loads(text)["slab-sweep"]
    assert (pin["version"], pin["wheel"], pin["sha256"]) == (version, wheel, sha256)
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pin the latest slab-sweep release.")
    parser.add_argument("--tag", help="a specific release tag, e.g. v1.2.0 (default: latest)")
    parser.add_argument("--pin", type=Path, default=ROOT / "engine.toml")
    args = parser.parse_args(argv)

    text = args.pin.read_text(encoding="utf-8")
    current = tomllib.loads(text)["slab-sweep"]
    try:
        version, wheel, url = find_release(args.tag)
    except urllib.error.HTTPError as exc:
        if exc.code != 404 or args.tag:
            raise
        print("slab-sweep has no release yet; nothing to pin")
        version, wheel, url = str(current.get("version", "")), "", ""
    changed = version != current.get("version")
    if changed:
        sha256 = hashlib.sha256(_get(url)).hexdigest()
        args.pin.write_text(update_pin(text, version, wheel, sha256), encoding="utf-8")
        print(f"engine.toml: slab-sweep {current.get('version') or '(none)'} -> {version}")
    elif version:
        print(f"engine.toml already pins slab-sweep {version}")
    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"changed={'true' if changed else 'false'}\nversion={version}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
