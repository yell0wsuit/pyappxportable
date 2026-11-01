# pyappxportable

Install Windows Store games as ordinary portable desktop games using [Appx Portable](https://github.com/yell0wsuit/appx-portable).

Give the installer an `.appxbundle`, `.appx`, or extracted package folder. It extracts the game, downloads the matching Appx Portable runtime and required Microsoft framework packages, applies supported game patches, and writes a `00_click2startme` launcher. Updates preserve saves, settings, and logs.

This package contains no games or Microsoft runtime files. Supply your own game packages. Runtime DLLs and game patches come from `yell0wsuit/appx-portable`; framework packages come from `yell0wsuit/vclibs-appx` with pinned SHA-256 checks.

## Install

Requires **Python 3.8 or later**, with pip. Use a Python release compatible with your Windows version; managed XAML games need Windows 10 1903 or later and .NET Framework 4.x. macOS/Wine support is experimental; managed games and games that require the native host need Windows.

```sh
python -m pip install pyappxportable
pyappxportable "C:\Downloads\Game.appxbundle"
```

On Windows, `py -m pip` and `py -m pyappxportable` also work. On macOS/Linux, use `python3` if that is your Python command. With a Python installation managed by your OS, install into a virtual environment or use `pipx install pyappxportable`.

Run `pyappxportable` without arguments for prompts. If the command is not on your PATH, use `python -m pyappxportable`. After installation, double-click `00_click2startme.bat` on Windows or `00_click2startme.command` on macOS. Linux uses `00_click2startme.sh` with Wine.

## Command-line options

```sh
pyappxportable Game.appx --dest Games/Game --store none --launcher --yes
python -m pyappxportable --help
```

| Option                           | Purpose                                                         |
| -------------------------------- | --------------------------------------------------------------- |
| `--version` | Show the installed pyappxportable version |
| `--no-update-check` | Skip the background PyPI update check |
| `--dest FOLDER`                  | Destination; defaults beside the source, using its package name |
| `--dependencies PATH`            | VCLibs or .NET Native package/folder; repeat for several        |
| `--build FOLDER`                 | Use locally built runtime DLLs                                  |
| `--release TAG`                  | Select a runtime release instead of the latest                  |
| `--store none\|trial\|iap\|both` | Store overrides; omitted with `--yes` preserves settings        |
| `--launcher`, `--no-launcher`    | Create or skip the launcher; defaults to creating it            |
| `--no-patch`                     | Skip known-game patches                                         |
| `--patch-dir FOLDER`             | Use a local Appx Portable `patch/` folder                       |
| `--desktop-copy`                 | Make a Windows 8/8.1 desktop executable copy on any Windows     |
| `--yes`                          | Disable prompts; fail if a required input is missing            |

For a local runtime checkout:

```sh
pyappxportable Game.appx --build ../appx-portable/build/windows-x86 --patch-dir ../appx-portable/patch
```

Choose an x64 build for an x64 game. Without `--patch-dir`, known-game patches are downloaded at the selected runtime release tag, including with `--build`.

Upgrade the installer with `python -m pip install --upgrade pyappxportable`. Run it again with the original package and the same `--dest` to update a game. The Python package version and the runtime release version are independent.

The CLI checks PyPI in the background at most once a day and prints an upgrade command when a newer compatible stable release is available. Checks never install updates or delay startup; offline failures stay silent. Use `--no-update-check` or set `PYAPPXPORTABLE_NO_UPDATE_CHECK=1` to disable them. `--help`, `--version`, and Python API calls do not check for updates. Use `pyappxportable --version` to show the installed package version.

For managed x86 games on Apple Silicon, runtime releases that include
`appx-portable-wine-clr-macos.tar.xz` use a patched Wine runtime. The installer
checks the release asset SHA-256 and the bundled CPU module, then caches it in
`~/Library/Application Support/appx-portable/wine/<archive-sha256>/`. The launcher
selects that Wine executable and a game-local `.wine-prefix/<runtime-identity>`.
Set `WINE` to select another engine or `WINEPREFIX` to use an existing prefix;
the pinned engine refuses a prefix that retains a different WoW64 CPU module.
Older releases without this asset and local `--build` installs without a
`--release` tag keep the usual Wine lookup. This patch fixes CLR startup only;
managed games still need WinRT projection and XAML support.

On macOS, the installer can set up Wine Devel, Rosetta 2, and GStreamer. Rosetta and GStreamer require administrator authorization; `--yes` prints instructions for those steps. See the [installer guide](https://github.com/yell0wsuit/appx-portable/blob/main/docs/installer.md) and [game compatibility](https://github.com/yell0wsuit/appx-portable/blob/main/docs/compatibility.md).

Managed Wine installs also discover native WinRT components registered in the
package manifest and generate their managed projections at install time. Runtime
releases must include `appx-portable-winrt-tools.zip`; with `--build`, place that
ZIP in the build folder (create it with Appx Portable's
`scripts/package-winrt-tools.py --output BUILD/appx-portable-winrt-tools.zip`).
The selected Wine installation must provide one Wine Mono compiler, .NET 4.8
reference set, and Mono.Cecil 0.11. Generated assemblies live in
`appx-projections/`; the original native component DLLs remain in place.

This currently supports default activation, scalar/string methods, input byte
arrays and custom callbacks. Prepared external class/enum projections are read from the local build
folder's `projection-references/` directory. They must be generated with Appx
Portable's `--interop` mode; the installer compiles the shared handle contract and
stages those dependencies with the components. Reference DLL filenames and
assembly identities must match. References conflicting with package DLLs are
refused. External interfaces/structs and other unsupported signatures fail with
a named diagnostic. SDK-based reference generation remains a build step. Windows installations keep the CLR's native
WinRT projection path. Neither SDK metadata nor compiler/Cecil tools are copied
to the game directory.

## Python API

```python
from pyappxportable import InstallError, InstallOptions, install_game

try:
    install_game(InstallOptions(
        package="Game.appx",
        dest="Games/Game",
        store="none",
    ))
except InstallError as error:
    print(error)
```

The API runs without prompts by default and returns `0` on success. It raises `InstallError` for actionable installation problems; filesystem errors can also raise `OSError`. Set `yes=False` to enable prompts, optionally supplying an `ask` callback to `install_game`. `Manifest` is also exported for package metadata.

## Development

This project uses Hatchling and a `src/pyappxportable` package with a console entry point. Modules separate manifests/extraction, PE inspection, downloads, framework dependencies, runtime staging, patches, settings, desktop copies, launchers, macOS setup, and installation orchestration.

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pylint src/pyappxportable
python -m black --check src/pyappxportable
python -m build
python -m twine check dist/*
```

CI lints, runs the update/release tests, builds wheel/source distributions, checks their metadata, and installs the wheel on Windows and Linux with Python 3.8, and on Windows, macOS, and Linux with the current Python.

Pushes to `main` automatically publish to PyPI and create a GitHub release when any pushed commit starts with `feat:`, `fix:`, `perf:`, or `refactor:` (including scopes such as `fix(cli):`). Any conventional commit with `!` or a `BREAKING CHANGE:` footer also releases. Documentation, test, and chore commits only validate unless they declare a breaking change. A manual workflow run on `main` always releases. Publication requires the existing PyPI Trusted Publisher for workflow `build.yaml` and GitHub environment `pypi`.

Versions use the UTC date, such as `2026.11.1`, with a fourth segment for repeat releases that day (`2026.11.1.1`, `2026.11.1.2`). Existing GitHub tags, releases, and PyPI versions are checked to avoid reuse. The workflow chooses and builds the version, publishes the validated distributions, and creates the matching `v` tag and GitHub release; no manual version bump or tag push is needed. Hatch VCS derives local checkout versions from Git tags, and the published source distribution retains its release version.

## License

[MIT](LICENSE). Extracted from `installappx.py` in Appx Portable by yell0wsuit. Games and Microsoft runtime files retain their own licenses.
