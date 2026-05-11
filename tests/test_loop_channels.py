import unittest
import sys
import types
from unittest.mock import patch

anthropic_stub = types.ModuleType("anthropic")
anthropic_stub.Anthropic = lambda *args, **kwargs: object()
sys.modules.setdefault("anthropic", anthropic_stub)

from src import loop


class LoopChannelTests(unittest.TestCase):
    def test_build_session_key_includes_channel_and_peer(self):
        self.assertEqual(
            loop.build_session_key("telegram", "tg-primary", "12345"),
            "agent:main:direct:telegram:12345",
        )

    def test_cli_channel_receive_returns_inbound_message(self):
        channel = loop.CLIChannel()
        with patch("builtins.input", return_value="你好"):
            msg = channel.receive()

        self.assertIsNotNone(msg)
        self.assertEqual(msg.text, "你好")
        self.assertEqual(msg.channel, "cli")
        self.assertEqual(msg.account_id, "cli-local")
        self.assertEqual(msg.peer_id, "cli-user")


if __name__ == "__main__":
    unittest.main()
