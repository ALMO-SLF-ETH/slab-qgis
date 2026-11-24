"""Build the installable Slab-QGIS plugin zip, with the slab-sweep engine bundled.

Standard library only, so it runs on any Python 3.11+ (CI, a release job, a developer's machine)::

    python scripts/build_plugin.py                       # engine pinned in engine.toml
    python scripts/build_plugin.py --engine-wheel W.whl  # engine from a local wheel
    python scripts/build_plugin.py --plugins-xml URL     # also write a plugin repository index;
                                                         # URL = the folder the zip is served from

The zip holds one top-level folder, ``slab_qgis/``, as QGIS's plugin installer expects. Inside it:
the plugin code, ``LICENSE``, and ``_vendor/`` with the engine package and its ``.dist-info``
(which carries the engine's own licence). The zip is checked before the script returns.
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import shutil
import sys
import tempfile
import tomllib
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "slab_qgis"
ENGINE_PACKAGE = "slab_sweep"
ENGINE_RELEASES = "https://github.com/ALMO-SLF-ETH/slab-sweep/releases/download"
#: metadata.txt fields the plugin repositories rely on.
REQUIRED_METADATA = (
    "name",
    "qgisMinimumVersion",
    "description",
    "about",
    "version",
    "author",
    "email",
    "repository",
    "tracker",
    "icon",
)


class BuildError(Exception):
    pass


def read_metadata(text: str) -> dict[str, str]:
    meta = configparser.ConfigParser(interpolation=None)
    meta.optionxform = str  # type: ignore[assignment,method-assign]  # keep camelCase keys
    meta.read_string(text)
    return dict(meta["general"])


def read_pin(path: Path) -> dict[str, str]:
    pin = tomllib.loads(path.read_text(encoding="utf-8")).get("slab-sweep", {})
    return {k: str(pin.get(k, "")) for k in ("version", "wheel", "sha256")}


def fetch_pinned_wheel(pin: dict[str, str], dest: Path) -> Path:
    """Download the pinned engine wheel into ``dest`` and verify its SHA-256."""
    if not (pin["version"] and pin["wheel"] and pin["sha256"]):
        raise BuildError(
            "engine.toml pins no slab-sweep release yet; pass --engine-wheel <path to a wheel>"
        )
    url = f"{ENGINE_RELEASES}/v{pin['version']}/{pin['wheel']}"
    target = dest / pin["wheel"]
    print(f"downloading {url}")
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - fixed https URL
        target.write_bytes(response.read())
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if digest != pin["sha256"].lower():
        raise BuildError(f"{pin['wheel']}: sha256 {digest} does not match engine.toml")
    return target


def stage(build_dir: Path, wheel: Path) -> Path:
    """Copy the plugin, its licence and the unpacked engine into ``build_dir/slab_qgis``."""
    staged = build_dir / PACKAGE
    shutil.copytree(
        ROOT / PACKAGE,
        staged,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "_vendor"),
    )
    shutil.copy2(ROOT / "LICENSE", staged / "LICENSE")
    vendor = staged / "_vendor"
    vendor.mkdir()
    with zipfile.ZipFile(wheel) as whl:
        members = [
            n
            for n in whl.namelist()
            if n.startswith(f"{ENGINE_PACKAGE}/") or n.split("/", 1)[0].endswith(".dist-info")
        ]
        if not any(n.startswith(f"{ENGINE_PACKAGE}/") for n in members):
            raise BuildError(f"{wheel.name} contains no {ENGINE_PACKAGE} package")
        for name in members:
            if name.startswith("/") or ".." in Path(name).parts:
                raise BuildError(f"unsafe path in wheel: {name}")
            whl.extract(name, vendor)
    return staged


def write_zip(staged: Path, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(staged.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(staged.parent).as_posix())


def check_zip(path: Path) -> dict[str, str]:
    """Fail unless the zip is an installable, complete plugin; return its metadata."""
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        meta_name = f"{PACKAGE}/metadata.txt"
        meta = read_metadata(zf.read(meta_name).decode("utf-8")) if meta_name in names else {}
    required = (
        f"{PACKAGE}/__init__.py",
        meta_name,
        f"{PACKAGE}/LICENSE",
        f"{PACKAGE}/_vendor/{ENGINE_PACKAGE}/__init__.py",
    )
    problems = [f"missing {n}" for n in required if n not in names]
    problems += [f"outside {PACKAGE}/: {n}" for n in names if not n.startswith(f"{PACKAGE}/")]
    problems += [f"bytecode: {n}" for n in names if n.endswith(".pyc") or "__pycache__" in n]
    if not any(n.endswith(".dist-info/licenses/LICENSE") for n in names):
        problems.append("engine licence (.dist-info/licenses/LICENSE) missing")
    problems += [f"metadata.txt lacks {k}" for k in REQUIRED_METADATA if not meta.get(k)]
    if meta.get("icon") and f"{PACKAGE}/{meta['icon']}" not in names:
        problems.append(f"icon {meta['icon']} not in zip")
    if problems:
        raise BuildError("plugin zip check failed:\n  " + "\n  ".join(sorted(problems)))
    return meta


def plugins_xml(meta: dict[str, str], download_url: str, file_name: str) -> str:
    """A QGIS plugin repository index (``plugins.xml``) listing this one plugin version."""

    def tag(name: str, value: str, cdata: bool = False) -> str:
        body = f"<![CDATA[{value}]]>" if cdata else escape(value)
        return f"    <{name}>{body}</{name}>"

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    version = meta["version"]
    fields = [
        tag("description", meta.get("description", ""), cdata=True),
        tag("about", meta.get("about", ""), cdata=True),
        tag("version", version),
        tag("qgis_minimum_version", meta.get("qgisMinimumVersion", "")),
        tag("qgis_maximum_version", meta.get("qgisMaximumVersion", "")),
        tag("homepage", meta.get("homepage", meta.get("repository", ""))),
        tag("file_name", file_name),
        tag("author_name", meta.get("author", ""), cdata=True),
        tag("download_url", download_url),
        tag("uploaded_by", meta.get("author", ""), cdata=True),
        tag("create_date", now),
        tag("update_date", now),
        tag("experimental", meta.get("experimental", "False")),
        tag("deprecated", meta.get("deprecated", "False")),
        tag("tracker", meta.get("tracker", "")),
        tag("repository", meta.get("repository", "")),
        tag("tags", meta.get("tags", ""), cdata=True),
        tag("server", "False"),
    ]
    name = escape(meta["name"], {'"': "&quot;"})
    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            "<plugins>",
            f'  <pyqgis_plugin name="{name}" version="{escape(version)}">',
            *fields,
            "  </pyqgis_plugin>",
            "</plugins>",
            "",
        ]
    )


def build(engine_wheel: Path | None, out_dir: Path, plugins_xml_base: str | None = None) -> Path:
    meta = read_metadata((ROOT / PACKAGE / "metadata.txt").read_text(encoding="utf-8"))
    version = meta["version"]
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        wheel = engine_wheel or fetch_pinned_wheel(read_pin(ROOT / "engine.toml"), tmpdir)
        staged = stage(tmpdir / "build", wheel)
        out = out_dir / f"{PACKAGE}-{version}.zip"
        write_zip(staged, out)
    meta = check_zip(out)
    print(f"built {out} ({meta['name']} {version}, engine {wheel.name})")
    if plugins_xml_base is not None:
        url = f"{plugins_xml_base.rstrip('/')}/{out.name}"
        (out_dir / "plugins.xml").write_text(plugins_xml(meta, url, out.name), encoding="utf-8")
        print(f"wrote {out_dir / 'plugins.xml'} (download_url {url})")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Slab-QGIS plugin zip.")
    parser.add_argument("--engine-wheel", type=Path, help="bundle this slab-sweep wheel")
    parser.add_argument("--out", type=Path, default=ROOT / "dist", help="output folder")
    parser.add_argument(
        "--plugins-xml",
        metavar="BASE_URL",
        help="also write plugins.xml whose download_url is BASE_URL/<zip name>",
    )
    args = parser.parse_args(argv)
    try:
        build(args.engine_wheel, args.out, args.plugins_xml)
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
