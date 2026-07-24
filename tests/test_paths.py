from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from contract.entities import ExtractionContext
from contract.graph_contract import normalize_repository_path
from env_loader import load_dotenv
from extractors.package_support.package_writer import configured_files
from graph_package import GraphPackage
from pipeline import _load_config, _validate_source


class CrossPlatformPathTests(unittest.TestCase):
    def test_repository_paths_are_host_independent(self) -> None:
        self.assertEqual(normalize_repository_path(r"src\Api\Order.cs"), "src/Api/Order.cs")
        context = ExtractionContext(source_id="app", relative_source_path=r"src\Api\Order.cs")
        self.assertEqual(context.source_file_qname(), "SourceFile:app:src/Api/Order.cs")

    def test_repository_path_rejects_traversal(self) -> None:
        with self.assertRaises(ValueError):
            normalize_repository_path(r"src\..\secret.cs")

    def test_source_name_is_valid_on_windows_and_macos(self) -> None:
        valid = {"name": "dotnet-api", "type": "dotnet-api", "folders": ["."]}
        self.assertEqual(_validate_source(valid, 0), valid)
        for name in ("CON", "api/name", r"api\name", "bad:name"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                _validate_source({**valid, "name": name}, 0)

    def test_windows_style_folder_selects_file_on_any_host(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "src" / "Api" / "Order.sql"
            source.parent.mkdir(parents=True)
            source.write_text("select 1", encoding="utf-8")
            files = configured_files(
                {"root": str(root), "folders": [r"src\Api"]}, (".sql",)
            )
            self.assertEqual([file.relative for file in files], ["src/Api/Order.sql"])


    def test_config_paths_are_relative_to_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            config_path = directory / "config.json"
            config_path.write_text(
                json.dumps({
                    "root": "source",
                    "output": "../output",
                    "sources": [{"name": "sql", "type": "sql-file", "folders": ["."], "inputData": "catalog"}],
                }),
                encoding="utf-8",
            )
            config = _load_config(config_path)
            self.assertEqual(config["root"], str((directory / "source").resolve()))
            self.assertEqual(config["output"], str((directory / "../output").resolve()))
            self.assertEqual(config["sources"][0]["inputData"], str((directory / "catalog").resolve()))

    def test_manifest_does_not_leak_machine_config_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            GraphPackage().write(output, source_name="sample", config_path="/machine/private/config.json")
            manifest_path = output / "manifest.json"
            manifest_text = manifest_path.read_text(encoding="utf-8")
            manifest = json.loads(manifest_text)
            self.assertEqual(manifest["metadata"]["config"], "config.json")
            self.assertNotIn("/machine/private", manifest_text)

    def test_dotenv_loads_values_without_overriding_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env"
            path.write_text("CODE_TREE_TEST_FROM_ENV=file-value\nCODE_TREE_TEST_PRIORITY=file-value\n", encoding="utf-8")
            previous_from_env = os.environ.pop("CODE_TREE_TEST_FROM_ENV", None)
            previous_priority = os.environ.get("CODE_TREE_TEST_PRIORITY")
            os.environ["CODE_TREE_TEST_PRIORITY"] = "process-value"
            try:
                load_dotenv(path)
                self.assertEqual(os.environ["CODE_TREE_TEST_FROM_ENV"], "file-value")
                self.assertEqual(os.environ["CODE_TREE_TEST_PRIORITY"], "process-value")
            finally:
                if previous_from_env is None:
                    os.environ.pop("CODE_TREE_TEST_FROM_ENV", None)
                else:
                    os.environ["CODE_TREE_TEST_FROM_ENV"] = previous_from_env
                if previous_priority is None:
                    os.environ.pop("CODE_TREE_TEST_PRIORITY", None)
                else:
                    os.environ["CODE_TREE_TEST_PRIORITY"] = previous_priority

if __name__ == "__main__":
    unittest.main()
