import tempfile
import unittest
from pathlib import Path

from monitor_config import (
    is_webhook_url_allowed,
    load_config,
    save_local_config_value,
    sanitize_config_for_ui,
    validate_config_update,
)


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

    def test_validate_config_update_allows_dingtalk_webhook(self):
        config = load_config(Path("/Users/hangzhou/openclaw-health-monitor"))
        allowed, message = validate_config_update(
            "DINGTALK_WEBHOOK",
            '"https://oapi.dingtalk.com/robot/send?access_token=test"',
            config,
        )
        self.assertTrue(allowed)
        self.assertEqual(message, "")

    def test_validate_config_update_rejects_non_allowlisted_webhook(self):
        config = load_config(Path("/Users/hangzhou/openclaw-health-monitor"))
        allowed, message = validate_config_update(
            "DINGTALK_WEBHOOK",
            '"https://example.com/robot/send?access_token=test"',
            config,
        )
        self.assertFalse(allowed)
        self.assertIn("白名单", message)

    def test_is_webhook_url_allowed_accepts_feishu_host(self):
        config = {"WEBHOOK_ALLOWED_HOSTS": "oapi.dingtalk.com,api.dingtalk.com,open.feishu.cn"}
        allowed, message = is_webhook_url_allowed("https://open.feishu.cn/open-apis/bot/v2/hook/test", config)
        self.assertTrue(allowed)
        self.assertEqual(message, "")


if __name__ == "__main__":
    unittest.main()
