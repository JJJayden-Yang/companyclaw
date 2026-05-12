import os
import sys
import threading
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

try:
    from .channel import (
        HAS_HTTPX,
        CLIChannel,
        ChannelAccount,
        ChannelManager,
        FeishuChannel,
        InboundMessage,
        TelegramChannel,
        build_session_key,
        print_info,
        telegram_poll_loop,
    )
    from .gateway import AgentGateway, handle_repl_command
    from .memory import SessionMemory
except ImportError:
    from channel import (
        HAS_HTTPX,
        CLIChannel,
        ChannelAccount,
        ChannelManager,
        FeishuChannel,
        InboundMessage,
        TelegramChannel,
        build_session_key,
        print_info,
        telegram_poll_loop,
    )
    from gateway import AgentGateway, handle_repl_command
    from memory import SessionMemory

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env", override=True)

MODEL_ID = os.getenv("MODEL_ID", "deepseek-v4-flash")
WORKDIR = Path.home() / ".companyclaw"
WORKDIR.mkdir(parents=True, exist_ok=True)
SKILLS_DIR = Path.home() / "skills"
SKILLS_DIR.mkdir(parents=True, exist_ok=True)

BASE_SYSTEM_PROMPT = (WORKDIR / "SYSTEM.md").read_text(encoding="utf-8")
client = Anthropic(
    api_key=os.getenv("COMPANYCLAW_API_KEY"),
    base_url=os.getenv("COMPANYCLAW_BASE_URL") or None,
)


def make_cli_message(text: str) -> InboundMessage:
    return InboundMessage(
        text=text,
        sender_id="cli-user",
        channel="cli",
        account_id="cli-local",
        peer_id="cli-user",
    )


def agent_loop() -> None:
    mgr = ChannelManager()
    cli = CLIChannel()
    mgr.register(cli)

    gateway = AgentGateway(
        client=client,
        model_id=MODEL_ID,
        base_system_prompt=BASE_SYSTEM_PROMPT,
        workdir=WORKDIR,
        skills_dir=SKILLS_DIR,
    )
    memory = SessionMemory()

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
        tg_channel = TelegramChannel(tg_acc, WORKDIR)
        mgr.register(tg_channel)
        tg_thread = threading.Thread(
            target=telegram_poll_loop,
            daemon=True,
            args=(tg_channel, msg_queue, q_lock, stop_event),
        )
        tg_thread.start()
    elif tg_token and not HAS_HTTPX:
        print("\033[33m  [telegram] httpx 未安装, 已跳过 Telegram 通道.\033[0m")

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
        print("\033[33m  [feishu] httpx 未安装, 已跳过 Feishu 通道.\033[0m")

    print_info("=" * 60)
    print_info("  companyclaw  |  Modular Loop")
    print_info(f"  Model: {MODEL_ID}")
    print_info(f"  Workdir: {WORKDIR}")
    print_info(f"  Skills dir: {SKILLS_DIR}")
    print_info(f"  Channels: {', '.join(mgr.list_channels())}")
    print_info(f"  Tools: {', '.join(gateway.tool_handlers.keys())}")
    print_info("  Commands: /channels /accounts /help  |  quit/exit")
    print_info("=" * 60)
    print()

    try:
        while True:
            with q_lock:
                tg_msgs = msg_queue[:]
                msg_queue.clear()
            for msg in tg_msgs:
                print(f"\033[34m\n  [telegram] {msg.sender_id}: {msg.text[:80]}\033[0m")
                gateway.run_agent_turn(msg, memory, mgr)

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

            gateway.run_agent_turn(make_cli_message(user_input), memory, mgr)
    finally:
        print("\033[2m再见.\033[0m")
        stop_event.set()
        if tg_thread and tg_thread.is_alive():
            tg_thread.join(timeout=3.0)
        mgr.close_all()


def main() -> None:
    if not os.getenv("COMPANYCLAW_API_KEY"):
        print("\033[33mError: COMPANYCLAW_API_KEY 未设置.\033[0m")
        print("\033[2m将 .env.example 复制为 .env 并填入你的 key.\033[0m")
        raise SystemExit(1)
    agent_loop()


if __name__ == "__main__":
    main()
