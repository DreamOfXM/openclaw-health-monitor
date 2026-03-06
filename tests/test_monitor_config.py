import tempfile
import unittest
from pathlib import Path

from monitor_config import load_config, save_local_config_value, sanitize_config_for_ui


class MonitorConfigTests(unittest.TestCase):
    def test_load_config_layers_defaults_and_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "config.conf").write_text("AUTO_UPDATE=false\nCHECK_INTERVAL=30\n", encoding="utf-8")
            (base / "config.local.conf").write_text("AUTO_UPDATE=true\nDINGTALK_WEBHOOK=\"https://example.invalid\"\n", encoding="utf-8")

            config = load_config(base)

            self.assertTrue(config["AUTO_UPDATE"])
            self.assertEqual(config["CHECK_INTERVAL"], 30)
            self.assertEqual(config["DINGTALK_WEBHOOK"], "https://example.invalid")

    def test_save_local_config_value_updates_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.assertTrue(save_local_config_value(base, "AUTO_UPDATE", "true"))
            self.assertTrue(save_local_config_value(base, "AUTO_UPDATE", "false"))

            content = (base / "config.local.conf").read_text(encoding="utf-8")
            self.assertIn("AUTO_UPDATE=false", content)
            self.assertEqual(content.count("AUTO_UPDATE="), 1)

    def test_sanitize_config_masks_secret_values(self):
        safe = sanitize_config_for_ui(
            {
                "DINGTALK_WEBHOOK": "https://secret",
                "FEISHU_WEBHOOK": "",
                "AUTO_UPDATE": True,
            }
        )
        self.assertTrue(safe["DINGTALK_WEBHOOK"])
        self.assertFalse(safe["FEISHU_WEBHOOK"])
        self.assertTrue(safe["AUTO_UPDATE"])


if __name__ == "__main__":
    unittest.main()
