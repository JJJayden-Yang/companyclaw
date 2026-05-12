import tempfile
import unittest
from pathlib import Path

from src.gateway import AgentGateway


class GatewayToolTests(unittest.TestCase):
    def test_update_soul_alias_appends_user_profile_note(self):
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

            self.assertIn("已追加到用户资料", result)
            self.assertIn("- 用户喜欢简洁说明", (root / "USER_PROFILE.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
