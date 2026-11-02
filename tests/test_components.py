"""Package-native projections keep the original implementation DLLs intact."""

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pyappxportable import components
from pyappxportable.errors import InstallError


class ComponentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.game = Path(temporary.name) / "game"
        self.game.mkdir()

    def manifest(self, paths):
        extensions = "".join(
            '<Extension Category="windows.activatableClass.inProcessServer"><InProcessServer>'
            f'<Path>{p}</Path><ActivatableClass ActivatableClassId="Example.Engine"/>'
            "</InProcessServer></Extension>"
            for p in paths
        )
        (self.game / "AppxManifest.xml").write_text(
            '<Package xmlns="urn:test"><Extensions>'
            + extensions
            + "</Extensions></Package>"
        )

    def component(self, path="Example.dll"):
        native = self.game / path
        native.parent.mkdir(parents=True, exist_ok=True)
        native.write_bytes(b"native unchanged")
        native.with_suffix(".winmd").write_bytes(b"metadata unchanged")
        return native

    def discover(self):
        with mock.patch.object(
            components, "pe_info", return_value=SimpleNamespace(managed=False)
        ):
            return components.discover_components(self.game)

    def test_discovers_namespaced_manifest_components_once(self):
        native = self.component("Engine/Example.dll")
        self.manifest(["Engine\\Example.dll", "Engine\\Example.dll"])
        self.assertEqual(self.discover(), [native.with_suffix(".winmd").resolve()])

    def test_manifest_paths_and_metadata_extensions_ignore_case(self):
        native = self.component("Engine/Example.dll")
        native.with_suffix(".winmd").rename(native.with_suffix(".WinMD"))
        self.manifest(["engine/example.DLL"])
        self.assertEqual(self.discover(), [native.with_suffix(".WinMD").resolve()])

    def test_skips_system_servers_and_native_modules_without_metadata(self):
        self.manifest(["CLRHost.dll", "Plain.dll"])
        (self.game / "Plain.dll").write_bytes(b"native")
        self.assertEqual(self.discover(), [])

    def test_skips_managed_implementation(self):
        self.component()
        self.manifest(["Example.dll"])
        with mock.patch.object(
            components, "pe_info", return_value=SimpleNamespace(managed=True)
        ):
            self.assertEqual(components.discover_components(self.game), [])

    def test_rejects_manifest_paths_outside_package(self):
        for path in (
            "../escape.dll",
            "C:\\outside.dll",
            "\\\\server\\share\\x.dll",
            "folder:stream.dll",
        ):
            with self.subTest(path=path):
                self.manifest([path])
                with self.assertRaises(InstallError):
                    self.discover()

    def test_rejects_duplicate_output_names(self):
        self.component("one/Example.dll")
        self.component("two/Example.dll")
        self.manifest(["one/Example.dll", "two/Example.dll"])
        with self.assertRaisesRegex(InstallError, "ambiguous"):
            self.discover()

    def install(self, schema=3, generate=None, components_found=()):
        with mock.patch.object(
            components, "tool_archive", return_value=self.bundle(schema)
        ), mock.patch.object(
            components, "discover_components", return_value=list(components_found)
        ), mock.patch.object(
            components, "generate_projections", side_effect=generate
        ) as generator:
            components.install_managed_runtime(
                self.game, Path("/wine"), build=Path("/build")
            )
        return generator

    def test_generation_failure_preserves_existing_projection_and_native(self):
        native = self.component()
        self.manifest(["Example.dll"])
        projected = self.game / "appx-projections"
        projected.mkdir()
        existing = projected / "Example.dll"
        existing.write_bytes(b"old projection")

        def fail(*_args):
            raise InstallError("unsupported parameter")

        with self.assertRaisesRegex(InstallError, "unsupported parameter"):
            self.install(generate=fail, components_found=[native.with_suffix(".winmd")])
        self.assertEqual(existing.read_bytes(), b"old projection")
        self.assertEqual(native.read_bytes(), b"native unchanged")
        self.assertFalse((self.game / "Windows.dll").exists())

    def test_success_publishes_prebuilt_and_generated_assemblies(self):
        native = self.component()
        self.manifest(["Example.dll"])

        def generate(_metadata, output, _wine, work, _manifest):
            self.assertEqual((work / "Windows.dll").read_bytes(), b"windows")
            (output / "Example.dll").write_bytes(b"managed projection")
            return set()

        self.install(generate=generate, components_found=[native.with_suffix(".winmd")])
        projected = self.game / "appx-projections"
        self.assertEqual(
            (projected / "Example.dll").read_bytes(), b"managed projection"
        )
        self.assertEqual(
            (projected / "AppxPortable.WinRT.Interop.dll").read_bytes(), b"interop"
        )
        self.assertEqual((self.game / "Windows.dll").read_bytes(), b"windows")
        self.assertEqual(
            (self.game / "System.Runtime.WindowsRuntime.dll").read_bytes(), b"bridge"
        )
        self.assertEqual(native.read_bytes(), b"native unchanged")
        self.assertFalse((self.game / "Mono.Cecil.dll").exists())

    def test_managed_game_without_components_still_gets_windows(self):
        self.manifest([])
        generator = self.install()
        generator.assert_not_called()
        self.assertEqual((self.game / "Windows.dll").read_bytes(), b"windows")
        self.assertEqual(
            (self.game / "System.Runtime.WindowsRuntime.dll").read_bytes(), b"bridge"
        )

    def test_tools_without_prebuilt_assemblies_are_too_old(self):
        self.manifest([])
        with self.assertRaisesRegex(InstallError, "too old"):
            self.install(schema=2)
        self.assertFalse((self.game / "Windows.dll").exists())

    def test_package_file_with_a_staged_name_is_not_overwritten(self):
        self.manifest([])
        shipped = self.game / "Windows.dll"
        shipped.write_bytes(b"package's own")
        with self.assertRaisesRegex(InstallError, "conflicts with package"):
            self.install()
        self.assertEqual(shipped.read_bytes(), b"package's own")

    def test_reinstall_replaces_assemblies_the_installer_staged(self):
        self.manifest([])
        self.install()
        (self.game / "Windows.dll").write_bytes(b"windows")
        with mock.patch.object(
            self, "prebuilt", {**self.prebuilt, "Windows.dll": b"newer windows"}
        ):
            self.install()
        self.assertEqual((self.game / "Windows.dll").read_bytes(), b"newer windows")

    def test_components_project_against_the_shipped_windows(self):
        work = Path("/work")
        generator, compiler = components.component_commands(
            lambda p: "W:" + p.name,
            work,
            Path("/game/Example.winmd"),
            {"runtime": ["runtime.cs"]},
        )
        self.assertEqual(generator[:2], ["W:generator.exe", "--component"])
        self.assertEqual(generator[-2:], ["--reference", "W:Windows.dll"])
        for flag in (
            "/define:APPX_PORTABLE_INTEROP",
            "/r:W:AppxPortable.WinRT.Interop.dll",
            "/r:W:Windows.dll",
        ):
            self.assertIn(flag, compiler)

    def test_unprojectable_component_is_skipped_with_a_named_warning(self):
        staged = Path(self.game) / "staged"
        staged.mkdir()
        sources = [Path("/game/Example.winmd"), Path("/game/Microsoft.Xbox.winmd")]

        def project(source):
            (staged / (source.stem + ".dll")).write_bytes(b"partial")
            if source.stem == "Microsoft.Xbox":
                raise components.ComponentToolFailure(
                    "Generating\nUnsupported factory constructor: ServiceClient(String)\n",
                    1,
                )

        with mock.patch("builtins.print") as printed:
            skipped = components.project_each(sources, staged, project)
        self.assertEqual(skipped, {"Microsoft.Xbox.dll"})
        self.assertEqual([p.name for p in staged.iterdir()], ["Example.dll"])
        message = printed.call_args[0][0]
        self.assertIn("Microsoft.Xbox", message)
        self.assertIn("Unsupported factory constructor: ServiceClient(String)", message)

    def test_tool_command_failure_is_not_skipped(self):
        def project(_source):
            raise InstallError("Component projection command failed: timed out")

        with self.assertRaisesRegex(InstallError, "timed out"):
            components.project_each(
                [Path("/game/Example.winmd")], Path(self.game), project
            )

    def test_skipped_component_is_not_expected_or_published(self):
        native = self.component()
        xbox = self.component("Microsoft.Xbox.dll")
        self.manifest(["Example.dll", "Microsoft.Xbox.dll"])

        def generate(_metadata, output, _wine, _work, _manifest):
            (output / "Example.dll").write_bytes(b"managed projection")
            return {"Microsoft.Xbox.dll"}

        self.install(
            generate=generate,
            components_found=[native.with_suffix(".winmd"), xbox.with_suffix(".winmd")],
        )
        self.assertEqual(
            sorted(p.name for p in (self.game / "appx-projections").iterdir()),
            ["AppxPortable.WinRT.Interop.dll", "Example.dll", components.STAGED_RECORD],
        )

    def test_bad_release_tool_digest_is_refused(self):
        release = {
            "assets": [
                {
                    "name": components.TOOLS_ASSET,
                    "digest": "sha256:bad",
                    "browser_download_url": "https://invalid/asset",
                }
            ]
        }
        with mock.patch.object(
            components, "fetch", return_value=json.dumps(release).encode()
        ):
            with self.assertRaisesRegex(InstallError, "digest"):
                components.tool_archive("v-test", None)

    def test_missing_release_tool_is_actionable(self):
        with mock.patch.object(components, "fetch", return_value=b'{"assets": []}'):
            with self.assertRaisesRegex(InstallError, "component projection tools"):
                components.tool_archive("v-old", None)

    def test_tools_archive_refuses_path_traversal(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("../escaped.cs", "")
        with self.assertRaises(InstallError):
            components.unpack_tools(data.getvalue(), self.game)
        self.assertFalse((self.game.parent / "escaped.cs").exists())

    prebuilt = {
        "AppxPortable.WinRT.Interop.dll": b"interop",
        "System.Runtime.WindowsRuntime.dll": b"bridge",
        "Windows.dll": b"windows",
    }

    def bundle(self, schema, tamper=None):
        files = {
            "generator.cs": b"generator",
            "runtime.cs": b"runtime",
            "LICENSE": b"license",
        }
        manifest = {
            "schema": schema,
            "generator": ["generator.cs"],
            "runtime": ["runtime.cs"],
        }
        if schema >= 2:
            manifest["interop"] = ["interop.cs"]
            files["interop.cs"] = b"shared contract"
        if schema >= 3:
            manifest["prebuilt"] = sorted(self.prebuilt)
            files.update(self.prebuilt)
        manifest["sha256"] = {
            name: hashlib.sha256(data).hexdigest() for name, data in files.items()
        }
        if tamper:
            files[tamper] = b"tampered"
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            for name, data in files.items():
                archive.writestr(name, data)
            archive.writestr("tools.json", json.dumps(manifest))
        return stream.getvalue()

    def test_reads_legacy_tool_bundle(self):
        manifest = components.unpack_tools(self.bundle(1), self.game)
        self.assertEqual(manifest["schema"], 1)
        self.assertFalse((self.game / "interop.cs").exists())

    def test_reads_shared_contract_from_new_bundle(self):
        manifest = components.unpack_tools(self.bundle(2), self.game)
        self.assertEqual(manifest["interop"], ["interop.cs"])
        self.assertEqual((self.game / "interop.cs").read_bytes(), b"shared contract")

    def test_reads_prebuilt_assemblies_from_new_bundle(self):
        manifest = components.unpack_tools(self.bundle(3), self.game)
        self.assertEqual(manifest["prebuilt"], sorted(self.prebuilt))
        self.assertEqual((self.game / "Windows.dll").read_bytes(), b"windows")

    def test_tampered_prebuilt_assembly_is_refused(self):
        with self.assertRaisesRegex(InstallError, "digest mismatch: Windows.dll"):
            components.unpack_tools(self.bundle(3, tamper="Windows.dll"), self.game)
        self.assertFalse((self.game / "Windows.dll").exists())


if __name__ == "__main__":
    unittest.main()
