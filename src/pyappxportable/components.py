"""Install native-backed managed projections for package-local WinRT metadata."""

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path, PureWindowsPath
from typing import List, Optional
from xml.etree import ElementTree

from pyappxportable.constants import REPOSITORY
from pyappxportable.downloads import download_verified, fetch
from pyappxportable.errors import InstallError
from pyappxportable.manifest import local_name
from pyappxportable.pe import pe_info
from pyappxportable.runtime import replace_file

TOOLS_ASSET = "appx-portable-winrt-tools.zip"
INTEROP_ASSEMBLY = "AppxPortable.WinRT.Interop.dll"


class ComponentToolFailure(InstallError):
    """The projection tool rejected an input; reason is its last diagnostic line."""

    def __init__(self, output: str, code: int):
        super().__init__("Component projection failed: " + output[-6000:])
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        self.reason = lines[-1] if lines else f"exit {code}"


def project_each(metadata, output, project) -> set:
    """Project each component; skip, by name, one the tools cannot express."""
    skipped = set()
    for source in metadata:
        try:
            project(source)
        except ComponentToolFailure as error:
            (output / (source.stem + ".dll")).unlink(missing_ok=True)
            skipped.add(source.stem + ".dll")
            print(
                f"Warning: skipped the {source.stem} component projection; "
                f"the game fails only if it uses {source.stem}: {error.reason}"
            )
    return skipped


def package_path(root: Path, parts) -> Optional[Path]:
    """Resolve Windows package spelling on a case-sensitive host, without ambiguity."""
    path = root
    for part in parts:
        if not path.is_dir():
            return None
        matches = [
            entry
            for entry in path.iterdir()
            if entry.name.casefold() == part.casefold()
        ]
        if len(matches) > 1:
            raise InstallError(f"Component path is ambiguous: {path / part}")
        if not matches:
            return None
        path = matches[0]
        if root not in path.resolve().parents:
            raise InstallError(f"Component path escapes package: {path}")
    return path


def discover_components(game: Path) -> List[Path]:
    """Find native servers with matching WinMDs; never replace the server DLL."""
    root = game.resolve()
    try:
        manifest = ElementTree.parse(game / "AppxManifest.xml")
    except (OSError, ElementTree.ParseError) as error:
        raise InstallError(f"Cannot read component manifest: {error}") from error
    found = {}
    for server in manifest.iter():
        if local_name(server) != "InProcessServer":
            continue
        paths = [e.text or "" for e in server if local_name(e) == "Path"]
        if len(paths) != 1:
            raise InstallError("Component server must declare one Path.")
        relative = PureWindowsPath(paths[0])
        if (
            not relative.parts
            or relative.anchor
            or ".." in relative.parts
            or any(":" in part for part in relative.parts)
        ):
            raise InstallError(f"Component path escapes package: {paths[0]}")
        native = package_path(root, relative.parts)
        if native is None or not native.is_file():
            continue
        metadata = package_path(
            root, native.relative_to(root).with_suffix(".winmd").parts
        )
        if metadata is None or not metadata.is_file():
            continue
        if pe_info(native).managed:
            continue
        key = metadata.stem.casefold()
        if key in found and found[key] != metadata:
            raise InstallError(
                f"Component projection name is ambiguous: {metadata.stem}"
            )
        found[key] = metadata
    return sorted(found.values())


def tool_archive(tag: Optional[str], build: Optional[Path]) -> bytes:
    """Read local tools or a SHA-256-verified asset from the chosen release."""
    if build is not None and (build / TOOLS_ASSET).is_file():
        return (build / TOOLS_ASSET).read_bytes()
    if not tag:
        raise InstallError(
            f"Build is missing component projection tools: {TOOLS_ASSET}."
        )
    try:
        release = json.loads(
            fetch(f"https://api.github.com/repos/{REPOSITORY}/releases/tags/{tag}")
        )
        asset = next(
            (a for a in release.get("assets", []) if a["name"] == TOOLS_ASSET), None
        )
        if asset is None:
            raise InstallError(
                f"Release {tag} has no component projection tools ({TOOLS_ASSET})."
            )
        digest = asset.get("digest", "")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise InstallError(
                "Component projection tools have no valid SHA-256 digest."
            )
        return download_verified(asset["browser_download_url"], digest[7:])
    except (ValueError, KeyError, TypeError) as error:
        raise InstallError(
            f"Cannot read component projection tools for {tag}: {error}"
        ) from error


def unpack_tools(data: bytes, destination: Path) -> dict:
    """Validate the complete tool manifest before writing any source files."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            manifest = json.loads(archive.read("tools.json"))
            schema = manifest["schema"]
            interop_sources = manifest.get("interop", [])
            if schema == 2 and not interop_sources:
                raise ValueError("tool manifest is missing its shared interop contract")
            if schema == 1 and interop_sources:
                raise ValueError(
                    "legacy tool manifest cannot declare an interop contract"
                )
            names = (
                manifest["generator"]
                + manifest["runtime"]
                + interop_sources
                + ["LICENSE"]
            )
            if (  # pylint: disable=too-many-boolean-expressions
                schema not in (1, 2)
                or len(names) != len(set(names))
                or not manifest["generator"]
                or not manifest["runtime"]
                or set(archive.namelist()) != set(names + ["tools.json"])
                or len(archive.namelist()) != len(names) + 1
            ):
                raise ValueError("unsupported or incomplete tool manifest")
            files = {}
            for name in names:
                if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9.]*", name):
                    raise ValueError(f"unsafe source filename: {name}")
                files[name] = archive.read(name)
                if hashlib.sha256(files[name]).hexdigest() != manifest["sha256"][name]:
                    raise ValueError(f"source digest mismatch: {name}")
            for name, content in files.items():
                (destination / name).write_bytes(content)
            return manifest
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
        raise InstallError(f"Invalid component projection tools: {error}") from error


def mono_directory(wine: Path) -> Path:
    """Detect the compiler, reference assemblies and Cecil belonging to this Wine."""
    engine = wine.resolve().parent.parent
    roots = [engine / "share/wine/mono", engine / "lib/wine/mono"]
    candidates = [
        path
        for root in roots
        for path in root.glob("*/lib/mono")
        if (path / "4.5/mcs.exe").is_file()
        and (path / "4.8-api/mscorlib.dll").is_file()
    ]
    if len(candidates) != 1:
        raise InstallError(
            f"Cannot identify one Wine Mono compiler and .NET 4.8 references for {wine}."
        )
    return candidates[0]


def component_references(game: Path, build: Optional[Path]) -> List[Path]:
    """Explicit build-time projections; never overwrite a package's DLL to stage them."""
    if build is None:
        return []
    directory = build / "projection-references"
    if not directory.is_dir():
        return []
    references = []
    names = set()
    for source in sorted(directory.iterdir()):
        if (
            source.suffix.casefold() != ".dll"
            or source.name.casefold() == INTEROP_ASSEMBLY.casefold()
        ):
            continue
        key = source.name.casefold()
        if key in names:
            raise InstallError(f"Ambiguous projection reference: {source.name}")
        names.add(key)
        existing = package_path(game.resolve(), (source.name,))
        if existing is not None and existing.read_bytes() != source.read_bytes():
            raise InstallError(
                f"Projection reference conflicts with package DLL: {source.name}"
            )
        references.append(source)
    return references


def generate_projections(
    metadata, output, wine, tag, build
):  # pylint: disable=too-many-locals
    """Compile trusted tooling, generate each metadata surface, then compile wrappers."""
    if wine is None:
        raise InstallError(
            "Install Wine Mono or set WINE before generating component projections."
        )
    external = component_references(output.parent, build)
    if {p.stem.casefold() for p in external} & {p.stem.casefold() for p in metadata}:
        raise InstallError(
            "A projection reference duplicates a component being generated."
        )
    mono = mono_directory(wine)
    cecils = list((mono / "gac/Mono.Cecil").glob("0.11.*__*/Mono.Cecil.dll"))
    if len(cecils) != 1:
        raise InstallError(f"Cannot identify Mono.Cecil 0.11 in {mono}.")
    with tempfile.TemporaryDirectory(prefix="appx-component-tools-") as temporary:
        work = Path(temporary)
        manifest = unpack_tools(tool_archive(tag, build), work)
        if external and not manifest.get("interop"):
            raise InstallError(
                "These component projection tools do not support external references."
            )
        shutil.copyfile(cecils[0], work / "Mono.Cecil.dll")
        env = dict(os.environ, WINEDEBUG="-all", MVK_CONFIG_LOG_LEVEL="0")
        env.setdefault("WINEPREFIX", str(work / "wine-prefix"))
        for name in (
            "MONO_PATH",
            "MONO_ENV_OPTIONS",
            "WINE_MONO_AOT",
            "WINEDLLOVERRIDES",
        ):
            env.pop(name, None)

        def run(arguments):
            try:
                # A Wine service can inherit a stdout pipe past the tool's exit.
                # A file lets us wait for the tool itself and retain its diagnostics.
                with tempfile.TemporaryFile(
                    mode="w+", encoding="utf-8", errors="replace"
                ) as log, tempfile.TemporaryFile(
                    mode="w+", encoding="utf-8", errors="replace"
                ) as diagnostics:
                    result = subprocess.run(
                        [str(wine), *arguments],
                        cwd=work,
                        env=env,
                        stdout=log,
                        stderr=diagnostics,
                        timeout=180,
                        check=False,
                    )
                    log.seek(0)
                    text = log.read()
                    diagnostics.seek(0)
                    errors = diagnostics.read()
            except (OSError, subprocess.TimeoutExpired) as error:
                raise InstallError(
                    f"Component projection command failed: {error}"
                ) from error
            if result.returncode:
                raise ComponentToolFailure(text + errors, result.returncode)
            return text.strip()

        def windows(path):
            return run(["winepath", "-w", str(path)])

        compiler = windows(mono / "4.5/mcs.exe")
        references = [
            "/r:" + windows(mono / "4.8-api" / name)
            for name in (
                "mscorlib.dll",
                "System.dll",
                "System.Core.dll",
                "System.Xml.dll",
            )
        ]

        def compile_file(target, sources, extra):
            run(
                [
                    compiler,
                    "/nologo",
                    "/noconfig",
                    "/nostdlib+",
                    "/warnaserror+",
                    "/langversion:5",
                    "/platform:x86",
                    "/out:" + windows(target),
                ]
                + references
                + extra
                + [windows(p) for p in sources]
            )

        generator = work / "generator.exe"
        compile_file(
            generator,
            [work / n for n in manifest["generator"]],
            ["/r:" + windows(work / "Mono.Cecil.dll")],
        )
        reference_flags = []
        generator_flags = []
        extras = set()
        if external:
            bridge = output / INTEROP_ASSEMBLY
            compile_file(
                bridge,
                [work / name for name in manifest["interop"]],
                ["/target:library"],
            )
            reference_flags = ["/define:APPX_PORTABLE_INTEROP", "/r:" + windows(bridge)]
            for reference in external:
                generator_flags += ["--reference", windows(reference)]
                reference_flags += ["/r:" + windows(reference)]
            extras = {p.name for p in external} | {INTEROP_ASSEMBLY}

        def project(source):
            generated = work / (source.stem + ".generated.cs")
            run(
                [windows(generator), "--component", windows(source), windows(generated)]
                + generator_flags
            )
            compile_file(
                output / (source.stem + ".dll"),
                [work / n for n in manifest["runtime"]] + [generated],
                ["/target:library"] + reference_flags,
            )

        skipped = project_each(metadata, output, project)
        for reference in external:
            shutil.copyfile(reference, output / reference.name)
        return extras, skipped


def install_component_projections(
    game: Path,
    wine: Optional[Path],
    tag: Optional[str] = None,
    build: Optional[Path] = None,
) -> None:
    """Generate everything before publishing; leave package-native DLLs untouched."""
    metadata = discover_components(game)
    if not metadata:
        return
    destination = game / "appx-projections"
    if destination.is_symlink():
        raise InstallError(
            f"Component projection directory must not be a symlink: {destination}"
        )
    with tempfile.TemporaryDirectory(prefix="appx-projections-", dir=game) as temporary:
        staged = Path(temporary)
        extras, skipped = generate_projections(metadata, staged, wine, tag, build)
        expected = ({source.stem + ".dll" for source in metadata} - skipped) | extras
        if {p.name for p in staged.iterdir()} != expected:
            raise InstallError(
                "Component projection tool did not produce the expected assemblies."
            )
        destination.mkdir(exist_ok=True)
        for name in sorted(expected):
            replace_file(staged / name, destination / name)
