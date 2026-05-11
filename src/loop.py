
# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------
import os
import sys
import json
import time
import threading
import subprocess
import re
from abc import ABC, abstractmethod
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv
from anthropic import Anthropic
try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env", override=True)

MODEL_ID = os.getenv("MODEL_ID", "deepseek-v4-flash")
client = Anthropic(
    api_key=os.getenv("COMPANYCLAW_API_KEY"),
    base_url=os.getenv("COMPANYCLAW_BASE_URL") or None,
)


# 工具输出最大字符数 -- 防止超大输出撑爆上下文
MAX_TOOL_OUTPUT = 50000

# 工作目录 -- 所有文件操作相对于此目录, 防止路径穿越
WORKDIR = Path.home() / ".companyclaw"
WORKDIR.mkdir(parents=True, exist_ok=True)
SKILLS_DIR = Path.home() / "skills"
SKILLS_DIR.mkdir(parents=True, exist_ok=True)

SYSTEM_PROMPT = (Path.home() / ".companyclaw" / "SYSTEM.md").read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# ANSI 颜色
# ---------------------------------------------------------------------------
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


def print_tool(name: str, detail: str) -> None:
    """打印工具调用信息."""
    print(f"  {DIM}[tool: {name}] {detail}{RESET}")


def print_info(text: str) -> None:
    print(f"{DIM}{text}{RESET}")


def print_channel(text: str) -> None:
    print(f"{BLUE}{text}{RESET}")


# ---------------------------------------------------------------------------
# 通道数据结构
# ---------------------------------------------------------------------------


@dataclass
class InboundMessage:
    """所有通道都规范化为这个结构, agent 循环只处理它."""
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
    """单个通道账号配置, 例如一个 Telegram bot 或一个飞书应用."""
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

    def __init__(self, account: ChannelAccount) -> None:
        if not HAS_HTTPX:
            raise RuntimeError("TelegramChannel requires httpx: pip install httpx")
        self.account_id = account.account_id
        self.base_url = f"https://api.telegram.org/bot{account.token}"
        self._http = httpx.Client(timeout=35.0)
        raw = account.config.get("allowed_chats", "")
        self.allowed_chats = {c.strip() for c in raw.split(",") if c.strip()} if raw else set()
        self._offset_path = WORKDIR / ".state" / "telegram" / f"offset-{self.account_id}.txt"
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

    def send_typing(self, chat_id: str) -> None:
        self._api("sendChatAction", chat_id=chat_id, action="typing")

    def poll(self) -> list[InboundMessage]:
        # 先释放已到期缓冲, 避免被后续网络等待阻塞
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
        expired = [
            k for k, g in self._media_groups.items()
            if (now - g["ts"]) >= self.MEDIA_BUFFER_SECONDS
        ]
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
        expired = [
            k for k, b in self._text_buf.items()
            if (now - b["ts"]) >= self.TEXT_BUFFER_SECONDS
        ]
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
        if not text:
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
        self.api_base = (
            "https://open.larksuite.com/open-apis"
            if is_lark else "https://open.feishu.cn/open-apis"
        )
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
        if not text:
            return None

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


# ---------------------------------------------------------------------------
# Skills: 持久化技能目录 + 按需加载
# ---------------------------------------------------------------------------


class SkillStore:
    def __init__(self, skills_dir: Path) -> None:
        self.skills_dir = skills_dir
        self.skills: dict[str, dict[str, str | dict]] = {}
        self.reload()

    def reload(self) -> int:
        self.skills = {}
        if not self.skills_dir.exists():
            return 0
        for path in sorted(self.skills_dir.rglob("SKILL.md")):
            text = path.read_text(encoding="utf-8")
            meta, body = self._parse_frontmatter(text)
            name = str(meta.get("name", path.parent.name)).strip() or path.parent.name
            self.skills[name] = {"meta": meta, "body": body, "path": str(path)}
        return len(self.skills)

    def _parse_frontmatter(self, text: str) -> tuple[dict, str]:
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            return {}, text.strip()
        raw_meta = match.group(1)
        body = match.group(2).strip()
        if HAS_YAML:
            try:
                meta = yaml.safe_load(raw_meta) or {}
                if isinstance(meta, dict):
                    return meta, body
            except Exception:
                pass
        meta: dict[str, str] = {}
        for line in raw_meta.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip().strip("'\"")
            if key:
                meta[key] = value
        return meta, body

    def descriptions(self) -> str:
        if not self.skills:
            return "(no skills available)"
        lines: list[str] = []
        for name, item in self.skills.items():
            meta = item.get("meta", {})
            desc = ""
            if isinstance(meta, dict):
                desc = str(meta.get("description", "")).strip()
            if not desc:
                desc = "No description"
            lines.append(f"- {name}: {desc}")
        return "\n".join(lines)

    def list_names(self) -> list[str]:
        return sorted(self.skills.keys())

    def load(self, name: str) -> str:
        skill = self.skills.get(name)
        if not skill:
            names = ", ".join(self.list_names()) or "(none)"
            return f"Error: Unknown skill '{name}'. Available: {names}"
        body = str(skill.get("body", ""))
        return f"<skill name=\"{name}\">\n{body}\n</skill>"


SKILL_STORE = SkillStore(SKILLS_DIR)


def build_system_prompt() -> str:
    return (
        f"{SYSTEM_PROMPT}\n\n"
        "You can use skills for specialized knowledge.\n"
        "If a task needs domain-specific process, call `load_skill` first.\n"
        "Available skills:\n"
        f"{SKILL_STORE.descriptions()}"
    )


# ---------------------------------------------------------------------------
# 安全辅助函数
# ---------------------------------------------------------------------------


def safe_path(raw: str) -> Path:
    """
    将用户/模型传入的路径解析为安全的绝对路径.
    防止路径穿越: 最终路径必须在 WORKDIR 之下.
    """
    target = (WORKDIR / raw).resolve()
    if not str(target).startswith(str(WORKDIR)):
        raise ValueError(f"Path traversal blocked: {raw} resolves outside WORKDIR")
    return target


def truncate(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    """截断过长的输出, 并附上提示."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text)} total chars]"


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------
# 每个工具函数接收关键字参数 (和 schema 中的 properties 对应),
# 返回字符串结果. 错误通过返回 "Error: ..." 传递给模型.
# ---------------------------------------------------------------------------


def tool_bash(command: str, timeout: int = 30) -> str:
    """执行 shell 命令并返回输出."""
    # 基础安全检查: 拒绝明显危险的命令
    dangerous = ["rm -rf /", "mkfs", "> /dev/sd", "dd if="]
    for pattern in dangerous:
        if pattern in command:
            return f"Error: Refused to run dangerous command containing '{pattern}'"

    print_tool("bash", command)
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(WORKDIR),
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += ("\n--- stderr ---\n" + result.stderr) if output else result.stderr
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"
        return truncate(output) if output else "[no output]"
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out after {timeout}s"
    except Exception as exc:
        return f"Error: {exc}"


def tool_read_file(file_path: str) -> str:
    """读取文件内容."""
    print_tool("read_file", file_path)
    try:
        target = safe_path(file_path)
        if not target.exists():
            return f"Error: File not found: {file_path}"
        if not target.is_file():
            return f"Error: Not a file: {file_path}"
        content = target.read_text(encoding="utf-8")
        return truncate(content)
    except ValueError as exc:
        return str(exc)
    except Exception as exc:
        return f"Error: {exc}"


def tool_write_file(file_path: str, content: str) -> str:
    """写入内容到文件. 父目录不存在时自动创建."""
    print_tool("write_file", file_path)
    try:
        target = safe_path(file_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Successfully wrote {len(content)} chars to {file_path}"
    except ValueError as exc:
        return str(exc)
    except Exception as exc:
        return f"Error: {exc}"


def tool_edit_file(file_path: str, old_string: str, new_string: str) -> str:
    """
    精确替换文件中的文本.
    old_string 必须在文件中恰好出现一次, 否则报错.
    这和 OpenClaw 的 edit 工具逻辑一致.
    """
    print_tool("edit_file", f"{file_path} (replace {len(old_string)} chars)")
    try:
        target = safe_path(file_path)
        if not target.exists():
            return f"Error: File not found: {file_path}"

        content = target.read_text(encoding="utf-8")
        count = content.count(old_string)

        if count == 0:
            return "Error: old_string not found in file. Make sure it matches exactly."
        if count > 1:
            return (
                f"Error: old_string found {count} times. "
                "It must be unique. Provide more surrounding context."
            )

        new_content = content.replace(old_string, new_string, 1)
        target.write_text(new_content, encoding="utf-8")
        return f"Successfully edited {file_path}"
    except ValueError as exc:
        return str(exc)
    except Exception as exc:
        return f"Error: {exc}"


def tool_list_skills() -> str:
    print_tool("list_skills", str(SKILLS_DIR))
    names = SKILL_STORE.list_names()
    if not names:
        return (
            "No skills found.\n"
            f"Put skill files under: {SKILLS_DIR}\n"
            "Expected path pattern: ~/skills/<skill_name>/SKILL.md"
        )
    lines = [f"Skills dir: {SKILLS_DIR}", "Available skills:"]
    for name in names:
        info = SKILL_STORE.skills.get(name, {})
        meta = info.get("meta", {})
        desc = ""
        if isinstance(meta, dict):
            desc = str(meta.get("description", "")).strip()
        path = str(info.get("path", ""))
        lines.append(f"- {name}: {desc or 'No description'} ({path})")
    return "\n".join(lines)


def tool_reload_skills() -> str:
    print_tool("reload_skills", str(SKILLS_DIR))
    count = SKILL_STORE.reload()
    return f"Reloaded {count} skills from {SKILLS_DIR}"


def tool_load_skill(name: str) -> str:
    print_tool("load_skill", name)
    return SKILL_STORE.load(name.strip())


# ---------------------------------------------------------------------------
# 工具定义: Schema (传给 API) + Handler 调度表
# ---------------------------------------------------------------------------
# 关键认知:
#   TOOLS 数组 = 告诉模型 "你有哪些工具可用"
#   TOOL_HANDLERS 字典 = 告诉我们的代码 "收到工具调用时执行什么函数"
#   两者通过 name 字段关联. 就这么简单.
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "bash",
        "description": (
            "Run a shell command and return its output. "
            "Use for system commands, git, package managers, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds. Default 30.",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read the contents of a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file (relative to working directory).",
                },
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write content to a file. Creates parent directories if needed. "
            "Overwrites existing content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file (relative to working directory).",
                },
                "content": {
                    "type": "string",
                    "description": "The content to write.",
                },
            },
            "required": ["file_path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Replace an exact string in a file with a new string. "
            "The old_string must appear exactly once in the file. "
            "Always read the file first to get the exact text to replace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file (relative to working directory).",
                },
                "old_string": {
                    "type": "string",
                    "description": "The exact text to find and replace. Must be unique.",
                },
                "new_string": {
                    "type": "string",
                    "description": "The replacement text.",
                },
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    },
    {
        "name": "list_skills",
        "description": "List all persisted skills under ~/skills.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "reload_skills",
        "description": "Reload skills from disk (~/skills).",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "load_skill",
        "description": "Load a skill body by name from ~/skills.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill name. Use list_skills first if unknown.",
                },
            },
            "required": ["name"],
        },
    },
]

# 调度表: 工具名 -> 处理函数
TOOL_HANDLERS: dict[str, Any] = {
    "bash": tool_bash,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
    "list_skills": lambda: tool_list_skills(),
    "reload_skills": lambda: tool_reload_skills(),
    "load_skill": tool_load_skill,
}


# ---------------------------------------------------------------------------
# 工具调用处理
# ---------------------------------------------------------------------------


def process_tool_call(tool_name: str, tool_input: dict) -> str:
    """
    根据工具名分发到对应的处理函数.
    这就是整个 "agent" 的核心调度逻辑.
    """
    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return f"Error: Unknown tool '{tool_name}'"
    try:
        return handler(**tool_input)
    except TypeError as exc:
        return f"Error: Invalid arguments for {tool_name}: {exc}"
    except Exception as exc:
        return f"Error: {tool_name} failed: {exc}"


# ---------------------------------------------------------------------------
# 核心: 多通道 Agent 循环
# ---------------------------------------------------------------------------


def handle_repl_command(cmd: str, mgr: ChannelManager) -> bool:
    cmd = cmd.strip().lower()
    if cmd == "/channels":
        for name in mgr.list_channels():
            print_channel(f"  - {name}")
        return True
    if cmd == "/accounts":
        for acc in mgr.accounts:
            masked = acc.token[:8] + "..." if len(acc.token) > 8 else "(none)"
            print_channel(f"  - {acc.channel}/{acc.account_id}  token={masked}")
        return True
    if cmd in ("/help", "/h"):
        print_info("  /channels  /accounts  /help  quit/exit")
        return True
    return False


def send_reply(inbound: InboundMessage, mgr: ChannelManager, text: str) -> None:
    channel = mgr.get(inbound.channel)
    if channel:
        channel.send(inbound.peer_id, text)
    else:
        print_assistant(text)


def run_agent_turn(
    inbound: InboundMessage,
    conversations: dict[str, list[dict]],
    mgr: ChannelManager,
) -> None:
    session_key = build_session_key(inbound.channel, inbound.account_id, inbound.peer_id)
    if session_key not in conversations:
        conversations[session_key] = []
    messages = conversations[session_key]
    messages.append({"role": "user", "content": inbound.text})

    if inbound.channel == "telegram":
        tg = mgr.get("telegram")
        if isinstance(tg, TelegramChannel):
            tg.send_typing(inbound.peer_id.split(":topic:")[0])

    while True:
        try:
                response = client.messages.create(
                    model=MODEL_ID,
                    max_tokens=8096,
                    system=build_system_prompt(),
                    tools=TOOLS,
                    messages=messages,
                )
        except Exception as exc:
            print(f"\n{YELLOW}API Error: {exc}{RESET}\n")
            while messages and messages[-1]["role"] != "user":
                messages.pop()
            if messages:
                messages.pop()
            return

        messages.append({
            "role": "assistant",
            "content": response.content,
        })

        if response.stop_reason == "end_turn":
            assistant_text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    assistant_text += block.text
            if assistant_text:
                send_reply(inbound, mgr, assistant_text)
            break

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                result = process_tool_call(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
            messages.append({
                "role": "user",
                "content": tool_results,
            })
            continue

        print_info(f"[stop_reason={response.stop_reason}]")
        assistant_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                assistant_text += block.text
        if assistant_text:
            send_reply(inbound, mgr, assistant_text)
        break


def make_cli_message(text: str) -> InboundMessage:
    return InboundMessage(
        text=text,
        sender_id="cli-user",
        channel="cli",
        account_id="cli-local",
        peer_id="cli-user",
    )


def agent_loop() -> None:
    """主 agent 循环 -- 支持 CLI、Telegram 和 Feishu 通道."""

    mgr = ChannelManager()
    cli = CLIChannel()
    mgr.register(cli)

    tg_channel: TelegramChannel | None = None
    stop_event = threading.Event()
    msg_queue: list[InboundMessage] = []
    q_lock = threading.Lock()
    tg_thread: threading.Thread | None = None

    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if tg_token and HAS_HTTPX:
        tg_acc = ChannelAccount(
            channel="telegram",
            account_id="tg-primary",
            token=tg_token,
            config={"allowed_chats": os.getenv("TELEGRAM_ALLOWED_CHATS", "")},
        )
        mgr.accounts.append(tg_acc)
        tg_channel = TelegramChannel(tg_acc)
        mgr.register(tg_channel)
        tg_thread = threading.Thread(
            target=telegram_poll_loop,
            daemon=True,
            args=(tg_channel, msg_queue, q_lock, stop_event),
        )
        tg_thread.start()
    elif tg_token and not HAS_HTTPX:
        print(f"  {YELLOW}[telegram] httpx 未安装, 已跳过 Telegram 通道.{RESET}")

    fs_id = os.getenv("FEISHU_APP_ID", "").strip()
    fs_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    if fs_id and fs_secret and HAS_HTTPX:
        fs_acc = ChannelAccount(
            channel="feishu",
            account_id="feishu-primary",
            config={
                "app_id": fs_id,
                "app_secret": fs_secret,
                "encrypt_key": os.getenv("FEISHU_ENCRYPT_KEY", ""),
                "bot_open_id": os.getenv("FEISHU_BOT_OPEN_ID", ""),
                "is_lark": os.getenv("FEISHU_IS_LARK", "").lower() in ("1", "true"),
            },
        )
        mgr.accounts.append(fs_acc)
        mgr.register(FeishuChannel(fs_acc))
    elif (fs_id or fs_secret) and not HAS_HTTPX:
        print(f"  {YELLOW}[feishu] httpx 未安装, 已跳过 Feishu 通道.{RESET}")

    print_info("=" * 60)
    print_info("  companyclaw  |  Channels + Agent Loop")
    print_info(f"  Model: {MODEL_ID}")
    print_info(f"  Workdir: {WORKDIR}")
    print_info(f"  Channels: {', '.join(mgr.list_channels())}")
    print_info(f"  Tools: {', '.join(TOOL_HANDLERS.keys())}")
    print_info("  Commands: /channels /accounts /help  |  quit/exit")
    print_info("=" * 60)
    print()

    conversations: dict[str, list[dict]] = {}

    try:
        while True:
            with q_lock:
                tg_msgs = msg_queue[:]
                msg_queue.clear()
            for msg in tg_msgs:
                print_channel(f"\n  [telegram] {msg.sender_id}: {msg.text[:80]}")
                run_agent_turn(msg, conversations, mgr)

            if tg_channel:
                import select
                if not select.select([sys.stdin], [], [], 0.5)[0]:
                    continue
                try:
                    user_input = sys.stdin.readline().strip()
                except (KeyboardInterrupt, EOFError):
                    break
                if not user_input:
                    continue
            else:
                msg = cli.receive()
                if msg is None:
                    break
                user_input = msg.text

            if user_input.lower() in ("quit", "exit"):
                break
            if user_input.startswith("/") and handle_repl_command(user_input, mgr):
                continue

            run_agent_turn(make_cli_message(user_input), conversations, mgr)
    finally:
        print(f"{DIM}再见.{RESET}")
        stop_event.set()
        if tg_thread and tg_thread.is_alive():
            tg_thread.join(timeout=3.0)
        mgr.close_all()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> None:
    if not os.getenv("COMPANYCLAW_API_KEY"):
        print(f"{YELLOW}Error: COMPANYCLAW_API_KEY 未设置.{RESET}")
        print(f"{DIM}将 .env.example 复制为 .env 并填入你的 key.{RESET}")
        sys.exit(1)

    agent_loop()


if __name__ == "__main__":
    main()
