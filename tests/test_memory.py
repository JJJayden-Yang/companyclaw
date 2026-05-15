import tempfile
import unittest
from pathlib import Path

from src.memory import UserProfileMemory


class UserProfileMemoryTests(unittest.TestCase):
    def test_update_replaces_unique_profile_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "USER_PROFILE.md"
            profile.write_text("# 用户资料\n- 用户喜欢长说明\n", encoding="utf-8")
            memory = UserProfileMemory(profile)

            result = memory.update("用户喜欢长说明", "用户喜欢简洁说明")

            self.assertIn("已更新用户资料", result)
            self.assertIn("- 用户喜欢简洁说明", profile.read_text(encoding="utf-8"))

    def test_update_rejects_missing_old_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "USER_PROFILE.md"
            profile.write_text("# 用户资料\n- 用户喜欢简洁说明\n", encoding="utf-8")
            memory = UserProfileMemory(profile)

            result = memory.update("用户喜欢长说明", "用户喜欢简洁说明")

            self.assertEqual(result, "Error: old_string not found in USER_PROFILE.md. Make sure it matches exactly.")


if __name__ == "__main__":
    unittest.main()
