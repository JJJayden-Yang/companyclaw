import tempfile
import unittest
from pathlib import Path

from src.gateway import AgentGateway


class GatewayToolTests(unittest.TestCase):
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

            tool_names = {tool["name"] for tool in gateway.tools}
            result = gateway.process_tool_call("update_soul", {"note": "用户喜欢简洁说明"})

            self.assertNotIn("update_soul", tool_names)
            self.assertEqual("Error: Unknown tool 'update_soul'", result)
            self.assertFalse((root / "USER_PROFILE.md").exists())


if __name__ == "__main__":
    unittest.main()
