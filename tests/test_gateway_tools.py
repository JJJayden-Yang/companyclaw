import tempfile
import unittest
from pathlib import Path

from src.gateway import AgentGateway


class GatewayToolTests(unittest.TestCase):
    def test_update_user_profile_replaces_profile_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "USER_PROFILE.md").write_text("# 用户资料\n- 用户喜欢长说明\n", encoding="utf-8")
            gateway = AgentGateway(
                client=object(),
                model_id="test-model",
                base_system_prompt="测试系统提示",
                workdir=root,
                skills_dir=root / "skills",
            )

            tool_names = {tool["name"] for tool in gateway.tools}
            result = gateway.process_tool_call(
                "update_user_profile",
                {"old_string": "用户喜欢长说明", "new_string": "用户喜欢简洁说明"},
            )

            self.assertIn("update_user_profile", tool_names)
            self.assertNotIn("append_user_note", tool_names)
            self.assertIn("已更新用户资料", result)
            self.assertIn("- 用户喜欢简洁说明", (root / "USER_PROFILE.md").read_text(encoding="utf-8"))

    def test_update_soul_is_not_registered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gateway = AgentGateway(
                client=object(),
                model_id="test-model",
                base_system_prompt="测试系统提示",
                workdir=root,
                skills_dir=root / "skills",
            )

            result = gateway.process_tool_call("update_soul", {"note": "用户喜欢简洁说明"})

            self.assertEqual("Error: Unknown tool 'update_soul'", result)


if __name__ == "__main__":
    unittest.main()
