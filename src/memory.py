try:
    from .channel import InboundMessage, build_session_key
except ImportError:
    from channel import InboundMessage, build_session_key


class SessionMemory:
    def __init__(self) -> None:
        self._conversations: dict[str, list[dict]] = {}

    def get_messages(self, inbound: InboundMessage) -> list[dict]:
        key = build_session_key(inbound.channel, inbound.account_id, inbound.peer_id)
        if key not in self._conversations:
            self._conversations[key] = []
        return self._conversations[key]

    def stats(self) -> dict[str, int]:
        return {k: len(v) for k, v in self._conversations.items()}
