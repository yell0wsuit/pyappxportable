"""Launchers helpers for portable Windows Store games."""

import hashlib
import shlex
from pathlib import Path, PureWindowsPath
from typing import Optional

from pyappxportable.pe import pe_imports

# Launchers

LAUNCHER = "00_click2startme"
BAT_LAUNCHER = """\
@echo off
rem Starts the game. Written by pyappxportable; options are in appx-portable.ini.
cd /d "%~dp0"
start "" "%~dp0{exe}" %*
"""
SH_LAUNCHER = """\
#!/usr/bin/env bash
# Starts the game under Wine. Written by pyappxportable; options are in
# appx-portable.ini. Set WINE to choose the Wine executable.
set -euo pipefail
cd "$(dirname "$0")"
# --log always: the relaunch check below reads the log.
flags=(--log "$@")
export WINEDEBUG=${{WINEDEBUG:--all}}
export MVK_CONFIG_LOG_LEVEL=${{MVK_CONFIG_LOG_LEVEL:-0}}
# The proxies load the shim; the VC runtimes are the package's own (VCLibs 110, 120, 140).
# The patched graphics stack from scripts/build-wined3d.sh replaces Wine's
# when installed; delete its three DLLs to use Wine's own.
graphics=
[[ -f d3d11.dll && -f dxgi.dll && -f wined3d.dll ]] && graphics=';d3d11,dxgi,wined3d=n'
{renderer}export WINEDLLOVERRIDES="d3dcompiler_47,xaudio2_8,xaudio2_9,dwrite=n,b;msvcr110,msvcp110,vccorlib110,msvcr120_app,msvcp120_app,vccorlib120_app,vcruntime140_app,msvcp140_app,vccorlib140_app=n$graphics${{WINEDLLOVERRIDES:+;$WINEDLLOVERRIDES}}"
{wine_setup}
[[ -x "$wine" ]] || {{ echo 'Install Wine, or set WINE to your Wine executable.' >&2; exit 1; }}
{media}# A crash otherwise opens Wine's crash dialog, which keeps the process alive
# until closed and stalls the relaunch below; print the backtrace and exit.
"$wine" reg add 'HKCU\\Software\\Wine\\WineDbg' /v ShowCrashDialog /t REG_DWORD /d 0 /f > /dev/null 2>&1 || true
# Wine 11 WoW64 sometimes kills the process inside wined3d's first OpenGL
# setup ("Exception frame is not in stack limits"). That startup death leaves
# "CreateDXGIFactory1 begin" without a result in the log; retry only that case.
status=0
for attempt in 1 2 3; do
  "$wine" {exe} "${{flags[@]}}" && exit 0 || status=$?
  # Wine launches always log, and each logging run starts a new appx-portable.log.
  run=$(cat appx-portable.log 2>/dev/null || true)
  if grep -q 'CreateDXGIFactory1 begin' <<< "$run" && ! grep -q 'CreateDXGIFactory1 hr=' <<< "$run"; then
    echo "Wine graphics startup crashed (attempt $attempt/3); relaunching." >&2
    continue
  fi
  exit "$status"
done
exit "$status"
"""
# macOS: wined3d's Vulkan renderer, on Metal through MoltenVK. Its OpenGL one
# stops at OpenGL 4.1 and often crashes in its first setup under WoW64.
MACOS_RENDERER = """\
# With the patched stack, Vulkan through MoltenVK, on Metal; Wine's own wined3d
# cannot run 32-bit games there. Set WINE_D3D_CONFIG=renderer=gl for OpenGL.
[[ -n $graphics ]] && export WINE_D3D_CONFIG=${WINE_D3D_CONFIG:-renderer=vulkan}
"""


# macOS, for a game that plays media: GStreamer scans its plugins again after
# it is installed or updated, about a minute while Rosetta translates them,
# which otherwise stalls the game the first time it plays audio or video.
MACOS_GSTREAMER = """\
# This game plays audio or video through GStreamer, which rescans its plugins
# after it is installed or updated. Scan here rather than mid-game; once its
# cache is current, the check takes a moment.
inspect=/Library/Frameworks/GStreamer.framework/Versions/1.0/bin/gst-inspect-1.0
if [[ -x $inspect ]]; then
  { sleep 2; echo 'Preparing GStreamer for audio and video, about a minute after it is installed or updated.' >&2; } &
  notice=$!
  arch -x86_64 "$inspect" > /dev/null 2>&1 || true
  kill "$notice" 2> /dev/null || true
fi
"""


# Why a managed game installed for Wine will not start; see docs/islands.md.
MANAGED_UNDER_WINE = (
    "managed (.NET) games do not run under Wine yet. They use XAML Islands, and"
    " Wine has no Windows.UI.Xaml; on macOS, Wine's .NET also hangs in 32-bit"
    " programs such as appx-host.exe."
)


# Wine plays Media Foundation and DirectShow media through GStreamer.
MEDIA_DLLS = {
    "mf.dll",
    "mfplat.dll",
    "mfplay.dll",
    "mfreadwrite.dll",
    "mfmediaengine.dll",
    "quartz.dll",
}


def plays_media(game: Path, executable: Path) -> bool:
    """Whether the game likely plays audio or video through Wine's GStreamer.

    Its own executable importing Media Foundation or DirectShow counts, and so
    does a XAML MediaElement; middleware such as FMOD imports Media Foundation
    without playing through it."""
    if MEDIA_DLLS.intersection(pe_imports(executable)):
        return True
    for page in game.rglob("*"):
        if (
            page.suffix.lower() in (".xaml", ".xbf")
            or page.name.lower() == "resources.pri"
        ):
            data = page.read_bytes()
            if b"MediaElement" in data or "MediaElement".encode("utf-16-le") in data:
                return True
    return False


def launch_executable(executable: str, managed: bool, native_host: bool = False) -> str:
    """Select the desktop CLR host, native host, or game's executable."""
    if native_host:
        return "appx-native-host.exe"
    return "appx-host.exe" if managed else executable


def managed_wine_setup(wine_runtime: Path) -> str:
    """Keep a managed prefix tied to the engine component it was tested with."""
    wine = shlex.quote(str(wine_runtime))
    identity = hashlib.sha256(str(wine_runtime).encode()).hexdigest()[:16]
    cpu = shlex.quote(
        str(wine_runtime.parent.parent / "lib/wine/x86_64-windows/wow64cpu.dll")
    )
    return f"""wine=${{WINE:-{wine}}}
if [[ -z ${{WINEPREFIX:-}} ]]; then
  mkdir -p "$PWD/.wine-prefix"
fi
export WINEPREFIX=${{WINEPREFIX:-"$PWD/.wine-prefix/{identity}"}}
expected_cpu={cpu}
retained_cpu="$WINEPREFIX/drive_c/windows/system32/wow64cpu.dll"
if [[ "$wine" == {wine} && -f "$retained_cpu" ]] && ! cmp -s "$expected_cpu" "$retained_cpu"; then
  echo 'This prefix retains a different WoW64 component. Use a new WINEPREFIX or unset it for the game runtime prefix.' >&2
  exit 1
fi"""


def write_launcher(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    game: Path,
    executable: str,
    windows: bool,
    macos: bool = False,
    media: bool = False,
    wine_runtime: Optional[Path] = None,
) -> Path:
    """Write 00_click2startme.bat (CRLF), or the executable Wine launcher into game.

    Finder opens a .sh in an editor even when it is executable, but runs a
    .command in Terminal, so macOS gets 00_click2startme.command."""
    if windows:
        path = Path(game) / (LAUNCHER + ".bat")
        text = BAT_LAUNCHER.format(
            exe=str(PureWindowsPath(executable)).replace("%", "%%")
        )
        path.write_bytes(text.replace("\n", "\r\n").encode())
    else:
        path = Path(game) / (LAUNCHER + (".command" if macos else ".sh"))
        text = SH_LAUNCHER.format(
            exe=shlex.quote(executable),
            renderer=MACOS_RENDERER if macos else "",
            media=MACOS_GSTREAMER if macos and media else "",
            wine_setup=(
                managed_wine_setup(wine_runtime)
                if wine_runtime is not None
                else (
                    'wine=${WINE:-$(command -v wine || echo "$HOME/Applications/'
                    'Wine Devel.app/Contents/Resources/wine/bin/wine")}'
                )
            ),
        )
        path.write_bytes(text.encode())
        path.chmod(0o755)
    return path
