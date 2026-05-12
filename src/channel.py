import json
import sys
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"
RESET = "\033[0m"
BOLD = "\033[1m"
BLUE = "\033[34m"


def colored_prompt() -> str:
    return f"{CYAN}{BOLD}You > {RESET}"


def print_assistant(text: str) -> None:
    print(f"\n{GREEN}{BOLD}Assistant:{RESET} {text}\n")


def print_info(text: str) -> None:
    print(f"{DIM}{text}{RESET}")


def print_channel(text: str) -> None:
    print(f"{BLUE}{text}{RESET}")


@dataclass
class InboundMessage:
    text: str
    sender_id: str
    channel: str = ""
    account_id: str = ""
    peer_id: str = ""
    is_group: bool = False
    media: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)


@dataclass
class ChannelAccount:
    channel: str
    account_id: str
    token: str = ""
    config: dict = field(default_factory=dict)


def build_session_key(channel: str, account_id: str, peer_id: str) -> str:
    ch = (channel or "unknown").strip().lower()
    pid = (peer_id or "default").strip().lower()
    return f"agent:main:direct:{ch}:{pid}"


class Channel(ABC):
    name: str = "unknown"

    @abstractmethod
    def receive(self) -> InboundMessage | None:
        pass

    @abstractmethod
    def send(self, to: str, text: str, **kwargs: Any) -> bool:
        pass

    def close(self) -> None:
        pass


class CLIChannel(Channel):
    name = "cli"

    def __init__(self) -> None:
        self.account_id = "cli-local"

    def receive(self) -> InboundMessage | None:
        try:
            text = input(colored_prompt()).strip()
        except (KeyboardInterrupt, EOFError):
            return None
        if not text:
            return None
        return InboundMessage(
            text=text,
            sender_id="cli-user",
            channel="cli",
            account_id=self.account_id,
            peer_id="cli-user",
        )

    def send(self, to: str, text: str, **kwargs: Any) -> bool:
        print_assistant(text)
        return True


def save_offset(path: Path, offset: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(offset), encoding="utf-8")


def load_offset(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


class TelegramChannel(Channel):
    name = "telegram"
    MAX_MSG_LEN = 4096
    POLL_TIMEOUT_SECONDS = 2
    MEDIA_BUFFER_SECONDS = 0.5
    TEXT_BUFFER_SECONDS = 0.25

    def __init__(self, account: ChannelAccount, workdir: Path) -> None:
        if not HAS_HTTPX:
            raise RuntimeError("TelegramChannel requires httpx: pip install httpx")
        self.account_id = account.account_id
        self.base_url = f"https://api.telegram.org/bot{account.token}"
        self._http = httpx.Client(timeout=35.0)
        raw = account.config.get("allowed_chats", "")
        self.allowed_chats = {c.strip() for c in raw.split(",") if c.strip()} if raw else set()
        self._offset_path = workdir / ".state" / "telegram" / f"offset-{self.account_id}.txt"
        self._offset = load_offset(self._offset_path)
        self._seen: set[int] = set()
        self._media_groups: dict[str, dict] = {}
        self._text_buf: dict[tuple[str, str], dict] = {}

    def _api(self, method: str, **params: Any) -> Any:
        filtered = {k: v for k, v in params.items() if v is not None}
        try:
            resp = self._http.post(f"{self.base_url}/{method}", json=filtered)
            data = resp.json()
            if not data.get("ok"):
                print(f"  {RED}[telegram] {method}: {data.get('description', '?')}{RESET}")
                return {}
            return data.get("result", {})
        except Exception as exc:
            print(f"  {RED}[telegram] {method}: {exc}{RESET}")
            return {}

    def fetch_file_bytes(self, file_id: str) -> tuple[bytes, str]:
        info = self._api("getFile", file_id=file_id)
        if not isinstance(info, dict):
            raise RuntimeError("getFile failed")
        file_path = info.get("file_path", "")
        if not file_path:
            raise RuntimeError("file_path missing in getFile response")
        token = self.base_url.rsplit("/bot", 1)[-1]
        url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        resp = self._http.get(url)
        resp.raise_for_status()
        suffix = Path(file_path).suffix or ".bin"
        return resp.content, suffix

    def send_typing(self, chat_id: str) -> None:
        self._api("sendChatAction", chat_id=chat_id, action="typing")

    def poll(self) -> list[InboundMessage]:
        ready = self._flush_all()
        if ready:
            return ready
        result = self._api(
            "getUpdates",
            offset=self._offset,
            timeout=self.POLL_TIMEOUT_SECONDS,
            allowed_updates=["message"],
        )
        if not result or not isinstance(result, list):
            return self._flush_all()
        for update in result:
            uid = update.get("update_id", 0)
            if uid >= self._offset:
                self._offset = uid + 1
                save_offset(self._offset_path, self._offset)
            if uid in self._seen:
                continue
            self._seen.add(uid)
            if len(self._seen) > 5000:
                self._seen.clear()
            msg = update.get("message")
            if not msg:
                continue
            if msg.get("media_group_id"):
                self._buf_media(msg, update)
                continue
            inbound = self._parse(msg, update)
            if not inbound:
                continue
            if self.allowed_chats and inbound.peer_id not in self.allowed_chats:
                continue
            self._buf_text(inbound)
        return self._flush_all()

    def _flush_all(self) -> list[InboundMessage]:
        ready = self._flush_media()
        ready.extend(self._flush_text())
        return ready

    def _buf_media(self, msg: dict, update: dict) -> None:
        mgid = msg["media_group_id"]
        if mgid not in self._media_groups:
            self._media_groups[mgid] = {"ts": time.monotonic(), "entries": []}
        self._media_groups[mgid]["entries"].append((msg, update))

    def _flush_media(self) -> list[InboundMessage]:
        now = time.monotonic()
        ready: list[InboundMessage] = []
        expired = [k for k, g in self._media_groups.items() if (now - g["ts"]) >= self.MEDIA_BUFFER_SECONDS]
        for mgid in expired:
            entries = self._media_groups.pop(mgid)["entries"]
            captions: list[str] = []
            media_items: list[dict] = []
            for msg, _ in entries:
                if msg.get("caption"):
                    captions.append(msg["caption"])
                for media_type in ("photo", "video", "document", "audio"):
                    if media_type not in msg:
                        continue
                    raw_media = msg[media_type]
                    if isinstance(raw_media, list) and raw_media:
                        file_id = raw_media[-1].get("file_id", "")
                    elif isinstance(raw_media, dict):
                        file_id = raw_media.get("file_id", "")
                    else:
                        file_id = ""
                    media_items.append({"type": media_type, "file_id": file_id})
            inbound = self._parse(entries[0][0], entries[0][1])
            if inbound:
                inbound.text = "\n".join(captions) if captions else "[media group]"
                inbound.media = media_items
                if not self.allowed_chats or inbound.peer_id in self.allowed_chats:
                    ready.append(inbound)
        return ready

    def _buf_text(self, inbound: InboundMessage) -> None:
        key = (inbound.peer_id, inbound.sender_id)
        now = time.monotonic()
        if key in self._text_buf:
            self._text_buf[key]["text"] += "\n" + inbound.text
            self._text_buf[key]["ts"] = now
        else:
            self._text_buf[key] = {"text": inbound.text, "msg": inbound, "ts": now}

    def _flush_text(self) -> list[InboundMessage]:
        now = time.monotonic()
        ready: list[InboundMessage] = []
        expired = [k for k, b in self._text_buf.items() if (now - b["ts"]) >= self.TEXT_BUFFER_SECONDS]
        for key in expired:
            buf = self._text_buf.pop(key)
            buf["msg"].text = buf["text"]
            ready.append(buf["msg"])
        return ready

    def _parse(self, msg: dict, raw_update: dict) -> InboundMessage | None:
        chat = msg.get("chat", {})
        chat_type = chat.get("type", "")
        chat_id = str(chat.get("id", ""))
        user_id = str(msg.get("from", {}).get("id", ""))
        text = msg.get("text", "") or msg.get("caption", "")
        media: list[dict] = []
        if "voice" in msg and isinstance(msg["voice"], dict):
            media.append({
                "type": "audio",
                "source": "telegram",
                "file_id": msg["voice"].get("file_id", ""),
                "mime": msg["voice"].get("mime_type", ""),
                "duration": msg["voice"].get("duration", 0),
            })
        if "audio" in msg and isinstance(msg["audio"], dict):
            media.append({
                "type": "audio",
                "source": "telegram",
                "file_id": msg["audio"].get("file_id", ""),
                "mime": msg["audio"].get("mime_type", ""),
                "duration": msg["audio"].get("duration", 0),
            })
        if not text and not media:
            return None
        thread_id = msg.get("message_thread_id")
        is_forum = chat.get("is_forum", False)
        is_group = chat_type in ("group", "supergroup")
        if chat_type == "private":
            peer_id = user_id
        elif is_group and is_forum and thread_id is not None:
            peer_id = f"{chat_id}:topic:{thread_id}"
        else:
            peer_id = chat_id
        return InboundMessage(
            text=text,
            sender_id=user_id,
            channel="telegram",
            account_id=self.account_id,
            peer_id=peer_id,
            is_group=is_group,
            media=media,
            raw=raw_update,
        )

    def receive(self) -> InboundMessage | None:
        msgs = self.poll()
        return msgs[0] if msgs else None

    def send(self, to: str, text: str, **kwargs: Any) -> bool:
        chat_id, thread_id = to, None
        if ":topic:" in to:
            parts = to.split(":topic:")
            chat_id = parts[0]
            thread_id = int(parts[1]) if len(parts) > 1 else None
        ok = True
        for chunk in self._chunk(text):
            if not self._api("sendMessage", chat_id=chat_id, text=chunk, message_thread_id=thread_id):
                ok = False
        return ok

    def _chunk(self, text: str) -> list[str]:
        if len(text) <= self.MAX_MSG_LEN:
            return [text]
        chunks = []
        while text:
            if len(text) <= self.MAX_MSG_LEN:
                chunks.append(text)
                break
            split_at = text.rfind("\n", 0, self.MAX_MSG_LEN)
            if split_at <= 0:
                split_at = self.MAX_MSG_LEN
            chunks.append(text[:split_at])
            text = text[split_at:].lstrip("\n")
        return chunks

    def close(self) -> None:
        self._http.close()


class FeishuChannel(Channel):
    name = "feishu"

    def __init__(self, account: ChannelAccount) -> None:
        if not HAS_HTTPX:
            raise RuntimeError("FeishuChannel requires httpx: pip install httpx")
        self.account_id = account.account_id
        self.app_id = account.config.get("app_id", "")
        self.app_secret = account.config.get("app_secret", "")
        self._encrypt_key = account.config.get("encrypt_key", "")
        self._bot_open_id = account.config.get("bot_open_id", "")
        is_lark = account.config.get("is_lark", False)
        self.api_base = "https://open.larksuite.com/open-apis" if is_lark else "https://open.feishu.cn/open-apis"
        self._tenant_token = ""
        self._token_expires_at = 0.0
        self._http = httpx.Client(timeout=15.0)

    def _refresh_token(self) -> str:
        if self._tenant_token and time.time() < self._token_expires_at:
            return self._tenant_token
        try:
            resp = self._http.post(
                f"{self.api_base}/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            data = resp.json()
            if data.get("code") != 0:
                print(f"  {RED}[feishu] Token error: {data.get('msg', '?')}{RESET}")
                return ""
            self._tenant_token = data.get("tenant_access_token", "")
            self._token_expires_at = time.time() + data.get("expire", 7200) - 300
            return self._tenant_token
        except Exception as exc:
            print(f"  {RED}[feishu] Token error: {exc}{RESET}")
            return ""

    def _bot_mentioned(self, event: dict) -> bool:
        for mention in event.get("message", {}).get("mentions", []):
            mention_id = mention.get("id", {})
            if isinstance(mention_id, dict) and mention_id.get("open_id") == self._bot_open_id:
                return True
            if isinstance(mention_id, str) and mention_id == self._bot_open_id:
                return True
            if mention.get("key") == self._bot_open_id:
                return True
        return False

    def _parse_content(self, message: dict) -> tuple[str, list]:
        msg_type = message.get("msg_type", "text")
        raw = message.get("content", "{}")
        try:
            content = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            return "", []
        media: list[dict] = []
        if msg_type == "text":
            return content.get("text", ""), media
        if msg_type == "post":
            texts: list[str] = []
            for locale_content in content.values():
                if not isinstance(locale_content, dict):
                    continue
                title = locale_content.get("title", "")
                if title:
                    texts.append(title)
                for para in locale_content.get("content", []):
                    for node in para:
                        tag = node.get("tag")
                        if tag == "text":
                            texts.append(node.get("text", ""))
                        elif tag == "a":
                            texts.append(node.get("text", "") + " " + node.get("href", ""))
            return "\n".join(texts), media
        if msg_type == "image":
            key = content.get("image_key", "")
            if key:
                media.append({"type": "image", "key": key})
            return "[image]", media
        if msg_type == "audio":
            file_key = content.get("file_key", "") or content.get("audio_key", "")
            duration = content.get("duration", 0)
            if file_key:
                media.append({
                    "type": "audio",
                    "source": "feishu",
                    "file_key": file_key,
                    "duration": duration,
                })
            return "[audio]", media
        return "", media

    def parse_event(self, payload: dict, token: str = "") -> InboundMessage | None:
        if self._encrypt_key and token and token != self._encrypt_key:
            print(f"  {RED}[feishu] Token verification failed{RESET}")
            return None
        if "challenge" in payload:
            print_info(f"[feishu] Challenge: {payload['challenge']}")
            return None
        event = payload.get("event", {})
        message = event.get("message", {})
        sender = event.get("sender", {}).get("sender_id", {})
        user_id = sender.get("open_id", sender.get("user_id", ""))
        chat_id = message.get("chat_id", "")
        chat_type = message.get("chat_type", "")
        is_group = chat_type == "group"
        if is_group and self._bot_open_id and not self._bot_mentioned(event):
            return None
        text, media = self._parse_content(message)
        if not text and not media:
            return None
        message_id = message.get("message_id", "")
        for item in media:
            if isinstance(item, dict) and item.get("type") == "audio":
                item["message_id"] = message_id
        return InboundMessage(
            text=text,
            sender_id=user_id,
            channel="feishu",
            account_id=self.account_id,
            peer_id=user_id if chat_type == "p2p" else chat_id,
            media=media,
            is_group=is_group,
            raw=payload,
        )

    def fetch_message_resource(self, message_id: str, file_key: str, file_type: str = "audio") -> tuple[bytes, str]:
        token = self._refresh_token()
        if not token:
            raise RuntimeError("Feishu tenant token unavailable")
        url = f"{self.api_base}/im/v1/messages/{message_id}/resources/{file_key}"
        resp = self._http.get(
            url,
            params={"type": file_type},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        suffix = ".bin"
        if "ogg" in content_type:
            suffix = ".ogg"
        elif "mpeg" in content_type or "mp3" in content_type:
            suffix = ".mp3"
        elif "wav" in content_type:
            suffix = ".wav"
        elif "mp4" in content_type or "m4a" in content_type:
            suffix = ".m4a"
        return resp.content, suffix

    def receive(self) -> InboundMessage | None:
        return None

    def send(self, to: str, text: str, **kwargs: Any) -> bool:
        token = self._refresh_token()
        if not token:
            return False
        try:
            resp = self._http.post(
                f"{self.api_base}/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "receive_id": to,
                    "msg_type": "text",
                    "content": json.dumps({"text": text}),
                },
            )
            data = resp.json()
            if data.get("code") != 0:
                print(f"  {RED}[feishu] Send: {data.get('msg', '?')}{RESET}")
                return False
            return True
        except Exception as exc:
            print(f"  {RED}[feishu] Send: {exc}{RESET}")
            return False

    def close(self) -> None:
        self._http.close()


class ChannelManager:
    def __init__(self) -> None:
        self.channels: dict[str, Channel] = {}
        self.accounts: list[ChannelAccount] = []

    def register(self, channel: Channel) -> None:
        self.channels[channel.name] = channel
        print_channel(f"  [+] Channel registered: {channel.name}")

    def list_channels(self) -> list[str]:
        return list(self.channels.keys())

    def get(self, name: str) -> Channel | None:
        return self.channels.get(name)

    def close_all(self) -> None:
        for channel in self.channels.values():
            channel.close()


def telegram_poll_loop(
    tg: TelegramChannel,
    queue: list[InboundMessage],
    lock: threading.Lock,
    stop: threading.Event,
) -> None:
    print_channel(f"  [telegram] Polling started for {tg.account_id}")
    while not stop.is_set():
        try:
            msgs = tg.poll()
            if msgs:
                with lock:
                    queue.extend(msgs)
        except Exception as exc:
            print(f"  {RED}[telegram] Poll error: {exc}{RESET}")
            stop.wait(5.0)


def read_cli_line_nonblocking(has_telegram: bool) -> str | None:
    if has_telegram:
        import select
        if not select.select([sys.stdin], [], [], 0.5)[0]:
            return None
        try:
            return sys.stdin.readline().strip()
        except (KeyboardInterrupt, EOFError):
            return ""
    return None
