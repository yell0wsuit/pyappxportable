"""Installer helpers for portable Windows Store games."""

import os
import platform
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from pyappxportable.components import install_component_projections
from pyappxportable.dependencies import (
    VCLIBS_DOWNLOADS,
    add_native_frameworks,
    add_vclibs,
    download_vclibs,
    missing_vclibs,
)
from pyappxportable.desktop import make_desktop_copy, needs_desktop_copy
from pyappxportable.downloads import download_runtime, resolve_release
from pyappxportable.errors import InstallError
from pyappxportable.extraction import extract_game, peek_manifest
from pyappxportable.launchers import (
    LAUNCHER,
    MANAGED_UNDER_WINE,
    launch_executable,
    plays_media,
    write_launcher,
)
from pyappxportable.macos import (
    WINE_DEVEL_APP,
    add_macos_dependencies,
    install_managed_wine,
)
from pyappxportable.manifest import Manifest
from pyappxportable.options import InstallOptions
from pyappxportable.patches import KNOWN_PATCHES, apply_known_patch
from pyappxportable.paths import absolute, path_warning, unquote_path
from pyappxportable.pe import (
    architecture_of,
    native_compiled_application,
    native_host_application,
    package_has_xaml,
    pe_info,
    write_process_manifest,
)
from pyappxportable.runtime import stage_runtime
from pyappxportable.settings import read_store, write_settings

STORE_ANSWERS = {"0": "none", "1": "trial", "2": "iap", "3": "both"}


def choose_folders(args, ask: Callable[[str], str]) -> Tuple[Path, Path, Manifest]:
    """The package, the folder to install it in, and the package's manifest."""
    package = args.package
    if not package and not args.yes:
        package = ask("Game package (.appxbundle, .appx or extracted folder): ")
    if not unquote_path(package or ""):
        raise InstallError("No game package given.")
    source = absolute(package)
    manifest = peek_manifest(source)
    default = source.parent / manifest.name
    if default == source:
        default = source.parent / (manifest.name + "-portable")
    destination = args.dest
    if destination is None and not args.yes:
        destination = ask(f"Folder to install the game in [{default}]: ")
    chosen = unquote_path(destination or "")
    return source, absolute(chosen) if chosen else default, manifest


def try_vclibs(
    dependency: str, sources: List[str], game: Path, architecture: str
) -> List[str]:
    """Add dependency from the first source that has it; why the others did not."""
    refused = []
    for source in sources:
        try:
            add_vclibs(dependency, absolute(source), game, architecture)
            return []
        except InstallError as error:
            refused.append(str(error))
    return refused


def add_missing_vclibs(
    args, ask: Callable[[str], str], manifest: Manifest, game: Path, architecture: str
) -> None:
    """Add each VCLibs dependency the game lacks: --dependencies, download, then ask."""
    for dependency in missing_vclibs(manifest, game):
        refused = try_vclibs(dependency, args.dependencies, game, architecture)
        if dependency in missing_vclibs(manifest, game):
            print(f"Downloading {dependency} ({architecture})")
            try:
                download_vclibs(dependency, game, architecture)
            except InstallError as error:
                refused.append(str(error))
        link = VCLIBS_DOWNLOADS[(dependency, architecture)]
        missing = f"Missing {dependency} ({architecture}). Download: {link}."
        if args.yes and dependency in missing_vclibs(manifest, game):
            raise InstallError(
                " ".join(refused + [missing, "Pass it with --dependencies."])
            )
        while dependency in missing_vclibs(manifest, game):
            for message in refused:
                print(message)
            print(missing)
            answer = ask(f"{dependency} .appx or folder: ")
            if not unquote_path(answer):
                raise InstallError(f"Missing {dependency} ({architecture}).")
            refused = try_vclibs(dependency, [answer], game, architecture)


def stage_dlls(
    args, game: Path, architecture: str, managed: bool, native_host: bool = False
) -> Optional[str]:
    """Copy the runtime from --build or the release; returns the release tag used."""
    wine = sys.platform != "win32"
    if args.build:
        stage_runtime(
            absolute(args.build),
            game,
            architecture,
            managed,
            wine=wine,
            native_host=native_host,
        )
        return args.release
    tag = resolve_release(args.release)
    print(f"Downloading Appx Portable {tag} ({architecture})")
    with tempfile.TemporaryDirectory(prefix="installappx-runtime-") as temporary:
        stage_runtime(
            download_runtime(tag, architecture, Path(temporary), wine=wine),
            game,
            architecture,
            managed,
            wine=wine,
            native_host=native_host,
        )
    return tag


def patch_known_game(
    manifest: Manifest, game: Path, tag: Optional[str], patch_dir: Optional[str] = None
) -> None:
    """Apply a known game's patch, from a checkout's patch/ or at the release tag."""
    local = absolute(patch_dir) if patch_dir else None
    if local is not None and not local.is_dir():
        raise InstallError(f"Patch directory does not exist: {local}")
    if (
        local is None
        and tag is None
        and (manifest.name, manifest.version) in KNOWN_PATCHES
    ):
        tag = resolve_release(None)
    output = apply_known_patch(manifest, game, tag, local)
    if output:
        print(output)


def choose_store(args, ask: Callable[[str], str], game: Path) -> Optional[str]:
    """The Store choice from --store or the prompt; None keeps the current one."""
    if args.store is not None or args.yes:
        return args.store
    current = read_store(game)
    while True:
        answer = ask(
            "Store options: 1 unlock trial, 2 stub in-app purchases, 3 both, 0 neither"
            f" [Enter keeps {current}]: "
        ).strip()
        if not answer or answer in STORE_ANSWERS:
            return STORE_ANSWERS.get(answer)


def install_game(  # pylint: disable=too-many-locals,too-many-branches
    args: InstallOptions, ask: Callable[[str], str] = input
) -> int:
    """Install the game the arguments and answers describe."""
    source, destination, manifest = choose_folders(args, ask)
    print(f"Installing {manifest.name} {manifest.version} into {destination}")
    manifest = extract_game(source, destination)
    executable = destination / manifest.executable
    architecture = architecture_of(executable)
    managed = pe_info(executable).managed
    native_compiled = native_compiled_application(executable)
    # .NET Native games and native games that import no proxy (such as C++/CX
    # Unity players) start through the native host.
    native_host = native_compiled or (
        not managed and native_host_application(destination, executable)
    )
    if native_host and sys.platform != "win32":
        raise InstallError("Native hosting requires Windows.")
    add_missing_vclibs(args, ask, manifest, destination, architecture)
    add_native_frameworks(manifest, destination, architecture, args.dependencies)
    tag = stage_dlls(args, destination, architecture, managed, native_host)
    # Only XAML runs in an island, which needs the manifest; CoreWindow games do not.
    if native_host and not native_compiled and package_has_xaml(destination):
        warning = write_process_manifest(destination, manifest.executable)
        if warning:
            print(f"Warning: {warning}")
    if not args.no_patch:
        patch_known_game(manifest, destination, tag, args.patch_dir)
    launch = launch_executable(manifest.executable, managed, native_host)
    if not managed and not native_host and (args.desktop_copy or needs_desktop_copy()):
        launch = make_desktop_copy(destination, manifest.executable)
    write_settings(destination, choose_store(args, ask, destination))
    managed_wine = None
    if (  # pylint: disable=too-many-boolean-expressions
        managed
        and architecture == "x86"
        and sys.platform == "darwin"
        and platform.machine() == "arm64"
        and not os.environ.get("WINE")
        and tag
    ):
        managed_wine = install_managed_wine(tag)
    if sys.platform == "darwin":
        add_macos_dependencies(args, ask, wine_ready=managed_wine is not None)
    if managed and sys.platform != "win32":
        selected_wine = os.environ.get("WINE") or managed_wine or shutil.which("wine")
        if selected_wine is None and sys.platform == "darwin":
            selected_wine = WINE_DEVEL_APP / "Contents/Resources/wine/bin/wine"
        install_component_projections(
            destination,
            Path(selected_wine) if selected_wine else None,
            tag=tag,
            build=absolute(args.build) if args.build else None,
        )
    launcher = args.launcher
    if launcher is None:
        answer = "" if args.yes else ask(f"Create the {LAUNCHER} launcher? [Y/n]: ")
        launcher = answer.strip().lower() not in ("n", "no")
    if launcher:
        launch = str(
            write_launcher(
                destination,
                launch,
                windows=sys.platform == "win32",
                macos=sys.platform == "darwin",
                media=plays_media(destination, executable),
                wine_runtime=managed_wine,
            )
        )
    if managed and sys.platform != "win32":
        message = (
            "managed (.NET) games still need the WinRT projection and XAML engine;"
            " the patched Wine runtime fixes CLR startup only."
            if managed_wine is not None
            else MANAGED_UNDER_WINE
        )
        print(f"Warning: {message}")
    warning = path_warning(
        destination, [str(p) for p in destination.rglob("*") if p.is_file()]
    )
    if warning:
        print(f"Warning: {warning}")
    print(f"Start the game with {destination / launch}")
    print(
        f"Store options: {read_store(destination)}. Settings: {destination / 'appx-portable.ini'}"
    )
    return 0
