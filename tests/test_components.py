"""Package-native projections keep the original implementation DLLs intact."""

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

    def test_generation_failure_preserves_existing_projection_and_native(self):
        native = self.component()
        self.manifest(["Example.dll"])
        projected = self.game / "appx-projections"
        projected.mkdir()
        existing = projected / "Example.dll"
        existing.write_bytes(b"old projection")
        with mock.patch.object(
            components,
            "discover_components",
            return_value=[native.with_suffix(".winmd")],
        ), mock.patch.object(
            components,
            "generate_projections",
            side_effect=InstallError("unsupported parameter"),
        ):
            with self.assertRaisesRegex(InstallError, "unsupported parameter"):
                components.install_component_projections(
                    self.game, Path("/wine"), build=Path("/build")
                )
        self.assertEqual(existing.read_bytes(), b"old projection")
        self.assertEqual(native.read_bytes(), b"native unchanged")

    def test_success_publishes_only_generated_files_in_separate_directory(self):
        native = self.component()
        self.manifest(["Example.dll"])

        def generate(_metadata, output, _wine, _tag, _build):
            (output / "Example.dll").write_bytes(b"managed projection")

        with mock.patch.object(
            components,
            "discover_components",
            return_value=[native.with_suffix(".winmd")],
        ), mock.patch.object(components, "generate_projections", side_effect=generate):
            components.install_component_projections(
                self.game, Path("/wine"), build=Path("/build")
            )
        self.assertEqual(
            (self.game / "appx-projections/Example.dll").read_bytes(),
            b"managed projection",
        )
        self.assertEqual(native.read_bytes(), b"native unchanged")
        self.assertFalse((self.game / "Mono.Cecil.dll").exists())

    def test_without_components_needs_no_tools_or_wine(self):
        self.manifest([])
        with mock.patch.object(components, "generate_projections") as generate:
            components.install_component_projections(self.game, None)
        generate.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
