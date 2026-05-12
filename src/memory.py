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

    def clear(self) -> None:
        self._conversations.clear()


class UserProfileMemory:
    def __init__(self, profile_path) -> None:
        self.profile_path = profile_path

    def append_note(self, note: str) -> str:
        cleaned = note.strip()
        if not cleaned:
            return "Error: note is empty"
        self.profile_path.parent.mkdir(parents=True, exist_ok=True)
        if self.profile_path.exists():
            current = self.profile_path.read_text(encoding="utf-8")
        else:
            current = "# 用户资料\n"
        prefix = "" if current.endswith("\n") else "\n"
        entry = f"{prefix}- {cleaned}\n"
        self.profile_path.write_text(current + entry, encoding="utf-8")
        return f"已追加到用户资料: {cleaned[:80]}"
