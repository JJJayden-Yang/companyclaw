import subprocess
from pathlib import Path
from typing import Any

try:
    from .channel import InboundMessage, ChannelManager, TelegramChannel, print_assistant, print_channel, print_info
    from .memory import SessionMemory
    from .skills_runtime import SkillStore, build_system_prompt
    from .voice_pipeline import VoicePipeline
except ImportError:
    from channel import InboundMessage, ChannelManager, TelegramChannel, print_assistant, print_channel, print_info
    from memory import SessionMemory
    from skills_runtime import SkillStore, build_system_prompt
    from voice_pipeline import VoicePipeline


class AgentGateway:
    def __init__(
        self,
        client: Any,
        model_id: str,
        base_system_prompt: str,
        workdir: Path,
        skills_dir: Path,
        max_tool_output: int = 50000,
    ) -> None:
        self.client = client
        self.model_id = model_id
        self.base_system_prompt = base_system_prompt
        self.workdir = workdir
        self.max_tool_output = max_tool_output
        self.skill_store = SkillStore(skills_dir)
        self.voice_pipeline = VoicePipeline(workdir)
        self.tools = self._build_tools()
        self.tool_handlers = self._build_tool_handlers()

    def print_tool(self, name: str, detail: str) -> None:
        print(f"  \033[2m[tool: {name}] {detail}\033[0m")

    def safe_path(self, raw: str) -> Path:
        target = (self.workdir / raw).resolve()
        if not str(target).startswith(str(self.workdir)):
            raise ValueError(f"Path traversal blocked: {raw} resolves outside WORKDIR")
        return target

    def truncate(self, text: str) -> str:
        if len(text) <= self.max_tool_output:
            return text
        return text[: self.max_tool_output] + f"\n... [truncated, {len(text)} total chars]"

    def tool_bash(self, command: str, timeout: int = 30) -> str:
        dangerous = ["rm -rf /", "mkfs", "> /dev/sd", "dd if="]
        for pattern in dangerous:
            if pattern in command:
                return f"Error: Refused to run dangerous command containing '{pattern}'"
        self.print_tool("bash", command)
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(self.workdir),
            )
            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                output += ("\n--- stderr ---\n" + result.stderr) if output else result.stderr
            if result.returncode != 0:
                output += f"\n[exit code: {result.returncode}]"
            return self.truncate(output) if output else "[no output]"
        except subprocess.TimeoutExpired:
            return f"Error: Command timed out after {timeout}s"
        except Exception as exc:
            return f"Error: {exc}"

    def tool_read_file(self, file_path: str) -> str:
        self.print_tool("read_file", file_path)
        try:
            target = self.safe_path(file_path)
            if not target.exists():
                return f"Error: File not found: {file_path}"
            if not target.is_file():
                return f"Error: Not a file: {file_path}"
            return self.truncate(target.read_text(encoding="utf-8"))
        except Exception as exc:
            return f"Error: {exc}"

    def tool_write_file(self, file_path: str, content: str) -> str:
        self.print_tool("write_file", file_path)
        try:
            target = self.safe_path(file_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return f"Successfully wrote {len(content)} chars to {file_path}"
        except Exception as exc:
            return f"Error: {exc}"

    def tool_edit_file(self, file_path: str, old_string: str, new_string: str) -> str:
        self.print_tool("edit_file", f"{file_path} (replace {len(old_string)} chars)")
        try:
            target = self.safe_path(file_path)
            if not target.exists():
                return f"Error: File not found: {file_path}"
            content = target.read_text(encoding="utf-8")
            count = content.count(old_string)
            if count == 0:
                return "Error: old_string not found in file. Make sure it matches exactly."
            if count > 1:
                return f"Error: old_string found {count} times. It must be unique."
            target.write_text(content.replace(old_string, new_string, 1), encoding="utf-8")
            return f"Successfully edited {file_path}"
        except Exception as exc:
            return f"Error: {exc}"

    def tool_list_skills(self) -> str:
        self.print_tool("list_skills", str(self.skill_store.skills_dir))
        names = self.skill_store.list_names()
        if not names:
            return (
                "No skills found.\n"
                f"Put skill files under: {self.skill_store.skills_dir}\n"
                "Expected path pattern: ~/skills/<skill_name>/SKILL.md"
            )
        lines = [f"Skills dir: {self.skill_store.skills_dir}", "Available skills:"]
        for name in names:
            info = self.skill_store.skills.get(name, {})
            meta = info.get("meta", {})
            desc = str(meta.get("description", "")).strip() if isinstance(meta, dict) else ""
            lines.append(f"- {name}: {desc or 'No description'} ({info.get('path', '')})")
        return "\n".join(lines)

    def tool_reload_skills(self) -> str:
        self.print_tool("reload_skills", str(self.skill_store.skills_dir))
        count = self.skill_store.reload()
        return f"Reloaded {count} skills from {self.skill_store.skills_dir}"

    def tool_load_skill(self, name: str) -> str:
        self.print_tool("load_skill", name)
        return self.skill_store.load(name.strip())

    def _build_tools(self) -> list[dict]:
        return [
            {"name": "bash", "description": "Run a shell command and return its output.",
             "input_schema": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["command"]}},
            {"name": "read_file", "description": "Read the contents of a file.",
             "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}},
            {"name": "write_file", "description": "Write content to a file.",
             "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}}, "required": ["file_path", "content"]}},
            {"name": "edit_file", "description": "Replace an exact string in a file with a new string.",
             "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}}, "required": ["file_path", "old_string", "new_string"]}},
            {"name": "list_skills", "description": "List all persisted skills under ~/skills.",
             "input_schema": {"type": "object", "properties": {}, "required": []}},
            {"name": "reload_skills", "description": "Reload skills from disk (~/skills).",
             "input_schema": {"type": "object", "properties": {}, "required": []}},
            {"name": "load_skill", "description": "Load a skill body by name from ~/skills.",
             "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
        ]

    def _build_tool_handlers(self) -> dict[str, Any]:
        return {
            "bash": self.tool_bash,
            "read_file": self.tool_read_file,
            "write_file": self.tool_write_file,
            "edit_file": self.tool_edit_file,
            "list_skills": lambda **_: self.tool_list_skills(),
            "reload_skills": lambda **_: self.tool_reload_skills(),
            "load_skill": self.tool_load_skill,
        }

    def process_tool_call(self, tool_name: str, tool_input: dict) -> str:
        handler = self.tool_handlers.get(tool_name)
        if handler is None:
            return f"Error: Unknown tool '{tool_name}'"
        try:
            return handler(**tool_input)
        except TypeError as exc:
            return f"Error: Invalid arguments for {tool_name}: {exc}"
        except Exception as exc:
            return f"Error: {tool_name} failed: {exc}"

    def send_reply(self, inbound: InboundMessage, mgr: ChannelManager, text: str) -> None:
        channel = mgr.get(inbound.channel)
        if channel:
            channel.send(inbound.peer_id, text)
        else:
            print_assistant(text)

    def run_agent_turn(self, inbound: InboundMessage, memory: SessionMemory, mgr: ChannelManager) -> None:
        inbound.text = self.voice_pipeline.maybe_transcribe(inbound, mgr)
        messages = memory.get_messages(inbound)
        messages.append({"role": "user", "content": inbound.text})
        if inbound.channel == "telegram":
            tg = mgr.get("telegram")
            if isinstance(tg, TelegramChannel):
                tg.send_typing(inbound.peer_id.split(":topic:")[0])
        while True:
            try:
                response = self.client.messages.create(
                    model=self.model_id,
                    max_tokens=8096,
                    system=build_system_prompt(self.base_system_prompt, self.skill_store),
                    tools=self.tools,
                    messages=messages,
                )
            except Exception as exc:
                print(f"\n\033[33mAPI Error: {exc}\033[0m\n")
                while messages and messages[-1]["role"] != "user":
                    messages.pop()
                if messages:
                    messages.pop()
                return
            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason == "end_turn":
                text = "".join(block.text for block in response.content if hasattr(block, "text"))
                if text:
                    self.send_reply(inbound, mgr, text)
                return
            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    result = self.process_tool_call(block.name, block.input)
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
                messages.append({"role": "user", "content": tool_results})
                continue
            print_info(f"[stop_reason={response.stop_reason}]")
            text = "".join(block.text for block in response.content if hasattr(block, "text"))
            if text:
                self.send_reply(inbound, mgr, text)
            return


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
