from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleaseMetadataTests(unittest.TestCase):
    def test_application_id_and_widget_settings_match_manifest(self) -> None:
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        panel = (ROOT / "Panel.qml").read_text(encoding="utf-8")
        service = (ROOT / "Service.qml").read_text(encoding="utf-8")
        library = (ROOT / "lib/omawix_library.py").read_text(encoding="utf-8")

        app_id = manifest["id"]
        self.assertIn(f'moduleName: "{app_id}"', panel)
        self.assertIn(f'ipcTarget: "{app_id}"', panel)
        self.assertIn(f'APP_ID = "{app_id}"', library)

        defaults = manifest["barWidget"]["defaults"]
        schema = {item["key"]: item for item in manifest["barWidget"]["schema"]}
        for key, fallback in defaults.items():
            item = schema[key]
            pattern = re.compile(
                rf'intSetting\("{re.escape(key)}",\s*{fallback},\s*'
                rf'{item["min"]},\s*{item["max"]}\)'
            )
            self.assertRegex(service, pattern)


if __name__ == "__main__":
    unittest.main()
