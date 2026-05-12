import tempfile
import unittest
from pathlib import Path

from src.memory import UserProfileMemory


class UserProfileMemoryTests(unittest.TestCase):
    def test_append_note_creates_profile_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "USER_PROFILE.md"
            memory = UserProfileMemory(profile)

            result = memory.append_note("喜欢用语音和 agent 交流")

            self.assertIn("已追加到用户资料", result)
            self.assertIn("- 喜欢用语音和 agent 交流", profile.read_text(encoding="utf-8"))

    def test_append_note_rejects_empty_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = UserProfileMemory(Path(tmp) / "USER_PROFILE.md")

            result = memory.append_note("  ")

            self.assertEqual(result, "Error: note is empty")


if __name__ == "__main__":
    unittest.main()
