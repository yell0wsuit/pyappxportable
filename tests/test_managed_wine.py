"""The patched Wine runtime that starts managed (.NET) x86 games on Apple Silicon."""

import contextlib
import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pyappxportable import downloads, installer, launchers, macos
from pyappxportable.errors import InstallError
from pyappxportable.manifest import Manifest
from pyappxportable.options import InstallOptions


def tar_xz(files):
    """A .tar.xz holding (name, bytes) files; bin/wine is executable."""
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:xz") as archive:
        for name, content in files:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o755 if name.endswith("/bin/wine") else 0o644
            archive.addfile(info, io.BytesIO(content))
    return data.getvalue()


class ManagedWineRuntimeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.root = self.base / "owned runtime"
        self.cpu = b"wine cpu fixture"
        manifest = {
            "schema": 1,
            "wine_version": "11.18",
            "cpu_sha256": hashlib.sha256(self.cpu).hexdigest(),
        }
        self.archive = tar_xz(
            [
                ("wine-clr.json", json.dumps(manifest).encode()),
                ("Wine Devel.app/Contents/Resources/wine/bin/wine", b"#!/bin/sh\n"),
                (
                    "Wine Devel.app/Contents/Resources/wine/lib/wine/x86_64-windows/wow64cpu.dll",
                    self.cpu,
                ),
            ]
        )
        self.asset = {
            "name": "appx-portable-wine-clr-macos.tar.xz",
            "browser_download_url": "https://example.invalid/managed-wine.tar.xz",
            "digest": "sha256:" + hashlib.sha256(self.archive).hexdigest(),
        }
        patch = mock.patch.object(macos, "WINE_CLR_ROOT", self.root, create=True)
        patch.start()
        self.addCleanup(patch.stop)

    def release(self, asset=None):
        return json.dumps({"assets": [self.asset if asset is None else asset]}).encode()

    @contextlib.contextmanager
    def serve(self, asset=None, archive=None):
        fetch = mock.Mock(
            side_effect=[
                self.release(asset),
                self.archive if archive is None else archive,
            ]
        )
        with mock.patch.object(macos, "fetch", fetch), mock.patch.object(
            downloads, "fetch", fetch
        ):
            yield fetch

    def test_installs_verified_release_in_owned_directory(self):
        with self.serve():
            wine = macos.install_managed_wine("v-test")
        self.assertTrue(wine.is_file())
        self.assertIn(self.root, wine.parents)
        self.assertTrue(os.access(wine, os.X_OK))
        self.assertFalse((self.base / "Applications").exists())

    def test_refuses_bad_archive_digest(self):
        with self.serve(archive=b"wrong archive"):
            with self.assertRaises(InstallError):
                macos.install_managed_wine("v-test")
        self.assertFalse(any(self.root.glob("*/Wine Devel.app")))

    def test_refuses_asset_without_digest(self):
        asset = dict(self.asset)
        asset.pop("digest")
        with self.serve(asset=asset):
            with self.assertRaises(InstallError):
                macos.install_managed_wine("v-test")

    def test_reuses_verified_cache_without_downloading_archive(self):
        with self.serve():
            wine = macos.install_managed_wine("v-test")
        with mock.patch.object(macos, "fetch", return_value=self.release()) as fetch:
            self.assertEqual(macos.install_managed_wine("v-test"), wine)
            self.assertEqual(fetch.call_count, 1)

    def test_refuses_corrupt_cached_cpu(self):
        with self.serve():
            wine = macos.install_managed_wine("v-test")
        cpu = wine.parent.parent / "lib/wine/x86_64-windows/wow64cpu.dll"
        cpu.write_bytes(b"corrupt")
        with mock.patch.object(macos, "fetch", return_value=self.release()):
            with self.assertRaises(InstallError):
                macos.install_managed_wine("v-test")

    def test_older_release_does_not_claim_patched_runtime(self):
        with mock.patch.object(macos, "fetch", return_value=b'{"assets": []}'):
            self.assertIsNone(macos.install_managed_wine("v-old"))


class ManagedWineLauncherTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.game = self.base / "game"
        self.game.mkdir()

    def test_launcher_pins_engine_and_isolates_prefix(self):
        selected = self.base / "engine with space" / "wine"
        launcher = launchers.write_launcher(
            self.game, "appx-host.exe", windows=False, macos=True, wine_runtime=selected
        )
        text = launcher.read_text()
        self.assertIn("wine=${WINE:-" + shlex.quote(str(selected)) + "}", text)
        self.assertIn('WINEPREFIX=${WINEPREFIX:-"$PWD/.wine-prefix/', text)
        self.assertNotIn("command -v wine", text)

    def test_launcher_without_runtime_keeps_wine_lookup(self):
        launcher = launchers.write_launcher(
            self.game, "Game.exe", windows=False, macos=True
        )
        self.assertIn("command -v wine", launcher.read_text())

    @unittest.skipIf(
        os.name == "nt" or not shutil.which("bash"), "needs bash on a POSIX host"
    )
    def test_launcher_rejects_a_retained_cpu_from_another_engine(self):
        runtime = self.base / "runtime"
        wine = runtime / "bin/wine"
        wine.parent.mkdir(parents=True)
        wine.write_text("#!/bin/sh\ntouch must-not-run\n")
        wine.chmod(0o755)
        cpu = runtime / "lib/wine/x86_64-windows/wow64cpu.dll"
        cpu.parent.mkdir(parents=True)
        cpu.write_bytes(b"patched")
        prefix = self.base / "old-prefix"
        retained = prefix / "drive_c/windows/system32/wow64cpu.dll"
        retained.parent.mkdir(parents=True)
        retained.write_bytes(b"old")
        launcher = launchers.write_launcher(
            self.game, "appx-host.exe", windows=False, macos=True, wine_runtime=wine
        )
        env = dict(os.environ, WINEPREFIX=str(prefix))
        env.pop("WINE", None)
        result = subprocess.run(
            ["bash", str(launcher)],
            env=env,
            cwd=self.game,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("different WoW64", result.stderr)
        self.assertFalse((self.game / "must-not-run").exists())
        self.assertEqual(retained.read_bytes(), b"old")

    @unittest.skipIf(
        os.name == "nt" or not shutil.which("bash"), "needs bash on a POSIX host"
    )
    def test_first_owned_prefix_has_a_parent_before_wine_starts(self):
        wine = self.base / "runtime/bin/wine"
        wine.parent.mkdir(parents=True)
        wine.write_text(
            '#!/bin/sh\ntest -d "$(dirname "$WINEPREFIX")" || exit 21\nexit 0\n'
        )
        wine.chmod(0o755)
        launcher = launchers.write_launcher(
            self.game, "appx-host.exe", windows=False, macos=True, wine_runtime=wine
        )
        env = dict(os.environ)
        env.pop("WINE", None)
        env.pop("WINEPREFIX", None)
        result = subprocess.run(
            ["bash", str(launcher)],
            env=env,
            cwd=self.game,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class ManagedWineInstallTests(unittest.TestCase):
    """install_game selects the patched runtime only for managed x86 games on Apple Silicon."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.game = Path(folder.name) / "game"
        self.game.mkdir()
        self.selected = Path(folder.name) / "owned wine" / "wine"

    def install(
        self,
        managed=True,
        architecture="x86",
        machine="arm64",
        wine="",
        tag="v-test",
        system="darwin",
        available=True,
    ):
        manifest = Manifest("Game", "1.0.0.0", architecture, "Game.exe", [])
        stubs = {
            "choose_folders": mock.DEFAULT,
            "install_managed_runtime": mock.DEFAULT,
            "add_missing_vclibs": mock.DEFAULT,
            "add_native_frameworks": mock.DEFAULT,
            "write_settings": mock.DEFAULT,
            "plays_media": mock.DEFAULT,
            "add_macos_dependencies": mock.DEFAULT,
            "path_warning": mock.DEFAULT,
            "read_store": mock.DEFAULT,
        }
        output = io.StringIO()
        with mock.patch.multiple(installer, **stubs) as patched, mock.patch.object(
            installer, "extract_game", return_value=manifest
        ), mock.patch.object(
            installer, "architecture_of", return_value=architecture
        ), mock.patch.object(
            installer, "pe_info", return_value=SimpleNamespace(managed=managed)
        ), mock.patch.object(
            installer, "native_compiled_application", return_value=False
        ), mock.patch.object(
            installer, "native_host_application", return_value=False
        ), mock.patch.object(
            installer, "stage_dlls", return_value=tag
        ), mock.patch.object(
            installer.sys, "platform", system
        ), mock.patch.object(
            macos.platform, "machine", return_value=machine
        ), mock.patch.dict(
            os.environ, {"WINE": wine}
        ), mock.patch.object(
            installer,
            "install_managed_wine",
            return_value=self.selected if available else None,
        ) as runtime, mock.patch(
            "sys.stdout", output
        ):
            patched["choose_folders"].return_value = (None, self.game, manifest)
            patched["plays_media"].return_value = False
            patched["path_warning"].return_value = None
            patched["read_store"].return_value = "none"
            self.projections = patched["install_managed_runtime"]
            code = installer.install_game(InstallOptions(no_patch=True, store="0"))
        return code, runtime, patched["add_macos_dependencies"], output.getvalue()

    def test_managed_apple_silicon_release_uses_owned_wine(self):
        code, runtime, dependencies, output = self.install()
        self.assertEqual(code, 0)
        runtime.assert_called_once_with("v-test")
        self.projections.assert_called_once_with(
            self.game, self.selected, tag="v-test", build=None
        )
        text = (self.game / "00_click2startme.command").read_text()
        self.assertIn(str(self.selected), text)
        self.assertIn(".wine-prefix", text)
        self.assertTrue(dependencies.call_args.kwargs["wine_ready"])
        self.assertIn("fixes CLR startup only", output)

    def test_native_game_keeps_user_wine(self):
        code, runtime, dependencies, output = self.install(managed=False)
        self.assertEqual(code, 0)
        runtime.assert_not_called()
        self.projections.assert_not_called()
        self.assertFalse(dependencies.call_args.kwargs.get("wine_ready", False))
        self.assertNotIn("fixes CLR startup only", output)

    def test_explicit_wine_is_respected(self):
        _, runtime, _, _ = self.install(wine="/opt/wine/bin/wine")
        runtime.assert_not_called()

    def test_intel_mac_and_x64_keep_user_wine(self):
        _, intel, _, _ = self.install(machine="x86_64")
        _, wide, _, _ = self.install(architecture="x64")
        intel.assert_not_called()
        wide.assert_not_called()

    def test_local_build_without_release_keeps_user_wine(self):
        _, runtime, dependencies, output = self.install(tag=None)
        runtime.assert_not_called()
        self.assertFalse(dependencies.call_args.kwargs.get("wine_ready", False))
        self.assertNotIn("fixes CLR startup only", output)

    def test_release_without_runtime_keeps_dependency_setup(self):
        _, runtime, dependencies, output = self.install(available=False)
        runtime.assert_called_once_with("v-test")
        self.assertFalse(dependencies.call_args.kwargs.get("wine_ready", False))
        self.assertIn(
            "command -v wine", (self.game / "00_click2startme.command").read_text()
        )
        self.assertNotIn("fixes CLR startup only", output)

    def test_other_platforms_do_not_download_managed_wine(self):
        for system in ("win32", "linux"):
            with self.subTest(system=system):
                _, runtime, dependencies, _ = self.install(system=system)
                runtime.assert_not_called()
                dependencies.assert_not_called()


class ManagedWineDependencyTests(unittest.TestCase):
    def test_owned_runtime_skips_standard_wine_but_checks_other_dependencies(self):
        output = io.StringIO()
        with mock.patch.object(
            macos, "needs_rosetta", return_value=True
        ), mock.patch.object(
            macos, "wine_installed", return_value=False
        ), mock.patch.object(
            macos, "install_wine_devel"
        ) as wine, mock.patch.object(
            macos, "GSTREAMER_FRAMEWORK"
        ) as framework, mock.patch(
            "sys.stdout", output
        ):
            framework.is_dir.return_value = False
            macos.add_macos_dependencies(InstallOptions(), input, wine_ready=True)
        wine.assert_not_called()
        self.assertIn("Rosetta", output.getvalue())
        self.assertIn("GStreamer", output.getvalue())


if __name__ == "__main__":
    unittest.main()
