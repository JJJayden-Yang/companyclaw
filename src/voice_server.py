import os
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

try:
    from .channel import Channel, ChannelManager, InboundMessage
    from .gateway import AgentGateway
    from .memory import SessionMemory
except ImportError:
    from channel import Channel, ChannelManager, InboundMessage
    from gateway import AgentGateway
    from memory import SessionMemory

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env", override=True)

MODEL_ID = os.getenv("MODEL_ID", "deepseek-v4-flash")
WORKDIR = Path.home() / ".companyclaw"
WORKDIR.mkdir(parents=True, exist_ok=True)
SKILLS_DIR = Path.home() / "skills"
SKILLS_DIR.mkdir(parents=True, exist_ok=True)
BASE_SYSTEM_PROMPT = (WORKDIR / "SYSTEM.md").read_text(encoding="utf-8")


class ChatRequest(BaseModel):
    message: str
    session_id: str = "voice-user"


class ChatResponse(BaseModel):
    reply: str


class VoiceApiChannel(Channel):
    name = "voice"

    def __init__(self) -> None:
        self._replies: dict[str, str] = {}

    def receive(self) -> InboundMessage | None:
        return None

    def send(self, to: str, text: str, **kwargs) -> bool:
        self._replies[to] = text
        return True

    def pop_reply(self, peer_id: str) -> str:
        return self._replies.pop(peer_id, "")


client = Anthropic(
    api_key=os.getenv("COMPANYCLAW_API_KEY"),
    base_url=os.getenv("COMPANYCLAW_BASE_URL") or None,
)
memory = SessionMemory()
channel = VoiceApiChannel()
mgr = ChannelManager()
mgr.register(channel)
gateway = AgentGateway(
    client=client,
    model_id=MODEL_ID,
    base_system_prompt=BASE_SYSTEM_PROMPT,
    workdir=WORKDIR,
    skills_dir=SKILLS_DIR,
)

app = FastAPI(title="companyclaw voice gateway")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    peer_id = req.session_id.strip() or "voice-user"
    inbound = InboundMessage(
        text=req.message,
        sender_id=peer_id,
        channel="voice",
        account_id="voice-local",
        peer_id=peer_id,
    )
    gateway.run_agent_turn(inbound, memory, mgr)
    reply = channel.pop_reply(peer_id)
    return ChatResponse(reply=reply or "[no reply]")


@app.post("/reset")
def reset() -> dict[str, str]:
    memory.clear()
    return {"status": "ok"}


def main() -> None:
    import sys
    import uvicorn

    if not os.getenv("COMPANYCLAW_API_KEY"):
        raise SystemExit("COMPANYCLAW_API_KEY 未设置")
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
