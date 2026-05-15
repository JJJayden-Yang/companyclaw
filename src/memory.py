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

    def update(self, old_string: str, new_string: str) -> str:
        if not old_string:
            return "Error: old_string is empty"
        if not self.profile_path.exists():
            return "Error: USER_PROFILE.md not found"
        content = self.profile_path.read_text(encoding="utf-8")
        count = content.count(old_string)
        if count == 0:
            return "Error: old_string not found in USER_PROFILE.md. Make sure it matches exactly."
        if count > 1:
            return f"Error: old_string found {count} times in USER_PROFILE.md. It must be unique."
        self.profile_path.write_text(content.replace(old_string, new_string, 1), encoding="utf-8")
        return "已更新用户资料"
