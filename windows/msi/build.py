"""
Build the Windows MSI: the installer, Python with cryptography, and the setup
wizard's images, packaged by WiX.

Usage, on Windows with WiX on PATH (dotnet tool install --global wix) and the
WixToolset.UI and WixToolset.Util extensions added:

    python windows/msi/build.py --version 1.0.3 --out dist

The embeddable Python is the official zip from python.org. pip downloads
cryptography's Windows wheels, which are unpacked into the embedded
Lib\\site-packages and compiled with unchecked-hash .pyc files, so Python never
writes files the MSI does not know about.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

PYTHON_VERSION = "3.12.10"
HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def run(*argv: str, cwd: Path | None = None) -> None:
    print("$", subprocess.list2cmdline(argv), flush=True)
    subprocess.run(argv, check=True, cwd=cwd)


def embedded_python(cache: Path, dest: Path) -> None:
    archive = cache / f"python-{PYTHON_VERSION}-embed-amd64.zip"
    if not archive.exists():
        url = (f"https://www.python.org/ftp/python/{PYTHON_VERSION}/"
               f"python-{PYTHON_VERSION}-embed-amd64.zip")
        urllib.request.urlretrieve(url, archive)
    with zipfile.ZipFile(archive) as z:
        z.extractall(dest)
    # The ._pth file is the embedded interpreter's whole sys.path.
    pth = next(dest.glob("python3*._pth"))
    pth.write_text(pth.read_text() + "Lib\\site-packages\n")

    wheels = cache / "wheels"
    run(sys.executable, "-m", "pip", "download", "--disable-pip-version-check",
        "--only-binary=:all:", "--platform", "win_amd64",
        "--python-version", PYTHON_VERSION.rsplit(".", 1)[0], "--implementation", "cp",
        "--dest", str(wheels), "cryptography")
    site = dest / "Lib" / "site-packages"
    site.mkdir(parents=True)
    for wheel in sorted(wheels.glob("*.whl")):
        with zipfile.ZipFile(wheel) as z:
            z.extractall(site)
    run(str(dest / "python.exe"), "-m", "compileall", "-q",
        "--invalidation-mode", "unchecked-hash", str(site))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True, help="MSI ProductVersion, a.b.c")
    parser.add_argument("--out", type=Path, default=REPO / "dist")
    parser.add_argument("--work", type=Path, default=REPO / "build" / "msi")
    args = parser.parse_args()

    work, cache = args.work, args.work.parent / "msi-cache"
    if work.exists():
        shutil.rmtree(work)
    payload = work / "payload"
    payload.mkdir(parents=True)
    cache.mkdir(parents=True, exist_ok=True)

    shutil.copy2(REPO / "ubo-lpa.py", payload)
    shutil.copytree(REPO / "assets", payload / "assets")
    shutil.copy2(REPO / "LICENSE", payload / "LICENSE.txt")
    shutil.copy2(HERE / "update-now.cmd", payload)
    embedded_python(cache, payload / "python")

    run(sys.executable, str(HERE / "art.py"), str(work / "art"))
    for name in ("package.wxs", "ui.wxs", "strings.wxl"):
        shutil.copy2(HERE / name, work)

    args.out.mkdir(parents=True, exist_ok=True)
    msi = args.out.resolve() / f"ubo-lpa-{args.version}.msi"
    run("wix", "build", "package.wxs", "ui.wxs", "-loc", "strings.wxl",
        "-ext", "WixToolset.UI.wixext", "-ext", "WixToolset.Util.wixext",
        "-arch", "x64", "-d", f"Version={args.version}", "-o", str(msi), cwd=work)
    print(f"built {msi}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
