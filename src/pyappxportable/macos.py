"""Macos helpers for portable Windows Store games."""

import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Callable, Optional

from pyappxportable.constants import REPOSITORY
from pyappxportable.downloads import download_verified, fetch
from pyappxportable.errors import InstallError

# macOS: what Wine needs

# Gcenx's Wine Devel build; the Wine launcher finds it in ~/Applications.
WINE_DEVEL = (
    "https://github.com/Gcenx/macOS_Wine_builds/releases/download/"
    "11.18/wine-devel-11.18-osx64.tar.xz",
    "aa0ea4c82e636ae7bca2076387cb0a5affa26509ad13f119ecd0d62bd7ba6f82",
)
WINE_DEVEL_APP = Path.home() / "Applications" / "Wine Devel.app"
WINE_CLR_ROOT = (
    Path.home() / "Library" / "Application Support" / "appx-portable" / "wine"
)
WINE_CLR_ASSET = "appx-portable-wine-clr-macos.tar.xz"
# The GStreamer runtime Wine's Media Foundation loads for audio and video.
# Wine 11.18 is built against 1.28; later 1.x releases keep its ABI.
GSTREAMER_RELEASES = "https://gstreamer.freedesktop.org/data/pkg/osx/"
GSTREAMER_FRAMEWORK = Path("/Library/Frameworks/GStreamer.framework")


def wine_installed() -> bool:
    """Whether the Wine launcher finds Wine: WINE, wine on PATH, or Wine Devel."""
    if os.environ.get("WINE"):
        return os.access(os.environ["WINE"], os.X_OK)
    wine = WINE_DEVEL_APP / "Contents" / "Resources" / "wine" / "bin" / "wine"
    return bool(shutil.which("wine")) or os.access(wine, os.X_OK)


def needs_rosetta() -> bool:
    """Whether this Mac lacks Rosetta 2, which runs Wine's Intel code."""
    if platform.machine() != "arm64":
        return False
    result = subprocess.run(
        ["/usr/bin/arch", "-x86_64", "/usr/bin/true"], capture_output=True, check=False
    )
    return result.returncode != 0


def extract_tar_xz(data: bytes, destination: Path) -> None:
    """Unpack a .tar.xz into destination, refusing entries that would land outside it."""
    root = Path(destination).resolve()

    def inside(path: Path) -> bool:
        return path == root or root in path.parents

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:xz") as archive:
        members = archive.getmembers()
        for member in members:
            target = (root / member.name).resolve()
            if member.issym():
                link = (target.parent / member.linkname).resolve()
            elif member.islnk():
                link = (root / member.linkname).resolve()
            else:
                link = target
            if not (
                member.isfile() or member.isdir() or member.issym() or member.islnk()
            ):
                raise InstallError(
                    f"Refusing {member.name}: not a file, folder or link."
                )
            if not inside(target) or not inside(link):
                raise InstallError(f"Refusing {member.name}: it leaves {root}.")
        if hasattr(tarfile, "data_filter"):
            # Checked above; "tar" keeps permissions and the bundle's own links.
            archive.extractall(root, members, filter="tar")
        else:
            archive.extractall(root, members)


def install_wine_devel() -> None:
    """Download Wine Devel and unpack it into ~/Applications."""
    url, sha256 = WINE_DEVEL
    print(f"Downloading Wine Devel ({url.rsplit('/', 1)[-1]}, about 190 MB)")
    data = download_verified(url, sha256)
    WINE_DEVEL_APP.parent.mkdir(parents=True, exist_ok=True)
    # Unpacked beside its destination and moved, so a failure leaves no half app.
    with tempfile.TemporaryDirectory(dir=WINE_DEVEL_APP.parent) as temporary:
        extract_tar_xz(data, Path(temporary))
        app = Path(temporary) / WINE_DEVEL_APP.name
        if not app.is_dir():
            raise InstallError(f"{url} has no {WINE_DEVEL_APP.name}.")
        os.replace(app, WINE_DEVEL_APP)
    print(f"Installed {WINE_DEVEL_APP}")


def check_managed_wine(folder: Path) -> Path:
    """Validate the engine component before reusing an immutable runtime cache."""
    try:
        manifest = json.loads((folder / "wine-clr.json").read_text())
        runtime = folder / "Wine Devel.app" / "Contents" / "Resources" / "wine"
        cpu = runtime / "lib" / "wine" / "x86_64-windows" / "wow64cpu.dll"
        wine = runtime / "bin" / "wine"
        if (
            manifest["schema"] != 1
            or manifest["wine_version"] != "11.18"
            or hashlib.sha256(cpu.read_bytes()).hexdigest() != manifest["cpu_sha256"]
            or not os.access(wine, os.X_OK)
        ):
            raise ValueError("runtime contents do not match the manifest")
        return wine
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise InstallError(
            f"Invalid managed Wine runtime in {folder}: {error}"
        ) from error


def install_managed_wine(tag: str) -> Optional[Path]:
    """Install this release's patched engine without replacing an existing Wine app."""
    try:
        release = json.loads(
            fetch(f"https://api.github.com/repos/{REPOSITORY}/releases/tags/{tag}")
        )
        asset = next(
            (
                item
                for item in release.get("assets", [])
                if item["name"] == WINE_CLR_ASSET
            ),
            None,
        )
        if asset is None:
            return None
        digest = asset.get("digest", "")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("patched Wine asset has no SHA-256 digest")
        sha256 = digest.split(":", 1)[1]
        url = asset["browser_download_url"]
    except (ValueError, KeyError, TypeError) as error:
        raise InstallError(
            f"Could not read managed Wine runtime for {tag}: {error}"
        ) from error
    destination = WINE_CLR_ROOT / sha256
    if destination.exists():
        return check_managed_wine(destination)
    data = download_verified(url, sha256)
    WINE_CLR_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WINE_CLR_ROOT) as temporary:
        extract_tar_xz(data, Path(temporary))
        check_managed_wine(Path(temporary))
        os.replace(temporary, destination)
    return check_managed_wine(destination)


def latest_gstreamer() -> str:
    """The newest stable GStreamer 1.x for macOS; odd minor versions are development ones."""
    listing = fetch(GSTREAMER_RELEASES, "install it yourself").decode(
        "utf-8", "replace"
    )
    versions = {
        tuple(int(part) for part in match)
        for match in re.findall(r'href="(1)\.(\d+)\.(\d+)/"', listing)
    }
    stable = [version for version in versions if version[1] % 2 == 0]
    if not stable:
        raise InstallError(f"No GStreamer release found at {GSTREAMER_RELEASES}.")
    return ".".join(str(part) for part in max(stable))


def install_gstreamer() -> None:
    """Download the newest stable GStreamer runtime and install it with sudo installer."""
    version = latest_gstreamer()
    name = f"gstreamer-1.0-{version}-universal.pkg"
    url = f"{GSTREAMER_RELEASES}{version}/{name}"
    # The checksum published beside it catches a corrupt or truncated download.
    published = (
        fetch(url + ".sha256sum", "install it yourself")
        .decode("ascii", "replace")
        .split()
    )
    if not published or not re.fullmatch(r"[0-9a-f]{64}", published[0]):
        raise InstallError(f"{url}.sha256sum is not a SHA-256 checksum.")
    print(f"Downloading GStreamer {version} ({name})")
    data = download_verified(url, published[0])
    with tempfile.TemporaryDirectory(prefix="installappx-gstreamer-") as temporary:
        package = Path(temporary) / name
        package.write_bytes(data)
        result = subprocess.run(
            [
                "/usr/bin/sudo",
                "/usr/sbin/installer",
                "-pkg",
                str(package),
                "-target",
                "/",
            ],
            check=False,
        )
    if result.returncode != 0:
        raise InstallError("GStreamer was not installed.")


def install_rosetta() -> None:
    """Install Rosetta 2 with sudo softwareupdate."""
    result = subprocess.run(
        [
            "/usr/bin/sudo",
            "/usr/sbin/softwareupdate",
            "--install-rosetta",
            "--agree-to-license",
        ],
        check=False,
    )
    if result.returncode != 0:
        raise InstallError("Rosetta 2 was not installed.")


def add_macos_dependencies(
    args, ask: Callable[[str], str], wine_ready: bool = False
) -> None:
    """Offer what Wine on macOS needs and lacks: Rosetta 2, Wine Devel and GStreamer.

    Wine Devel needs no administrator; --yes installs it. The others need sudo,
    so --yes only says how to install them."""

    def agreed(question: str) -> bool:
        return ask(question).strip().lower() not in ("n", "no")

    if needs_rosetta():
        if args.yes:
            print("Warning: Wine needs Rosetta 2: softwareupdate --install-rosetta")
        elif agreed(
            "Wine needs Rosetta 2. Install it, accepting Apple's license"
            " (asks for your administrator password)? [Y/n]: "
        ):
            install_rosetta()
    if not wine_ready and not wine_installed():
        if args.yes or agreed(
            f"Wine is not installed. Download Wine Devel into {WINE_DEVEL_APP.parent}? [Y/n]: "
        ):
            install_wine_devel()
        else:
            print(
                "Install Wine, or set WINE to your Wine executable, before starting the game."
            )
    if not GSTREAMER_FRAMEWORK.is_dir():
        hint = (
            "install the macOS runtime from https://gstreamer.freedesktop.org/download/"
        )
        if args.yes:
            print(
                f"Warning: games that play audio or video under Wine need GStreamer; {hint}"
            )
        elif agreed(
            "Games that play audio or video under Wine need GStreamer. Install it"
            " (asks for your administrator password)? [Y/n]: "
        ):
            try:
                install_gstreamer()
            except InstallError as error:
                print(f"Warning: {error} To install it later, {hint}")
