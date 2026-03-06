import unittest

from dashboard import stringify_config_value


class DashboardHelperTests(unittest.TestCase):
    def test_stringify_masks_secret_values(self):
        self.assertEqual(stringify_config_value("DINGTALK_WEBHOOK", "https://secret"), "已配置")
        self.assertEqual(stringify_config_value("DINGTALK_WEBHOOK", ""), "未配置")

    def test_stringify_formats_regular_values(self):
        self.assertEqual(stringify_config_value("AUTO_UPDATE", True), "true")
        self.assertEqual(stringify_config_value("AUTO_UPDATE", False), "false")
        self.assertEqual(stringify_config_value("CHECK_INTERVAL", 30), "30")


if __name__ == "__main__":
    unittest.main()
