import os
import time
from pathlib import Path
from typing import Any

try:
    from .channel import ChannelManager, TelegramChannel, FeishuChannel, InboundMessage
except ImportError:
    from channel import ChannelManager, TelegramChannel, FeishuChannel, InboundMessage


class VoicePipeline:
    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self.media_dir = workdir / "media" / "voice"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.backend = os.getenv("VOICE_STT_BACKEND", "local").strip().lower()
        self.local_model_name = os.getenv("VOICE_LOCAL_MODEL", "small").strip()
        self.local_device = os.getenv("VOICE_LOCAL_DEVICE", "auto").strip()
        self.local_compute_type = os.getenv("VOICE_LOCAL_COMPUTE_TYPE", "int8").strip()
        self.stt_api_key = os.getenv("VOICE_STT_API_KEY", "").strip()
        self.stt_base = os.getenv("VOICE_STT_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.stt_model = os.getenv("VOICE_STT_MODEL", "whisper-1")
        self.stt_language = os.getenv("VOICE_STT_LANGUAGE", "zh")
        self.timeout_seconds = float(os.getenv("VOICE_STT_TIMEOUT", "45"))
        self._local_model: Any | None = None

    def maybe_transcribe(self, inbound: InboundMessage, mgr: ChannelManager) -> str:
        if inbound.text and inbound.text.strip() not in ("[audio]",):
            return inbound.text
        audio_item = self._find_audio_item(inbound.media)
        if not audio_item:
            return inbound.text
        local_file = self._download_audio(audio_item, mgr)
        if not local_file:
            return "[语音消息下载失败，无法转写]"
        text = self._transcribe_file(local_file)
        if not text:
            return "[语音转写失败或结果为空]"
        return text

    def _find_audio_item(self, items: list[Any]) -> dict[str, Any] | None:
        for item in items:
            if isinstance(item, dict) and item.get("type") == "audio":
                return item
        return None

    def _download_audio(self, media: dict[str, Any], mgr: ChannelManager) -> Path | None:
        source = str(media.get("source", "")).strip().lower()
        try:
            if source == "telegram":
                file_id = str(media.get("file_id", "")).strip()
                tg = mgr.get("telegram")
                if not file_id or not isinstance(tg, TelegramChannel):
                    return None
                data, suffix = tg.fetch_file_bytes(file_id)
            elif source == "feishu":
                message_id = str(media.get("message_id", "")).strip()
                file_key = str(media.get("file_key", "")).strip()
                fs = mgr.get("feishu")
                if not message_id or not file_key or not isinstance(fs, FeishuChannel):
                    return None
                data, suffix = fs.fetch_message_resource(message_id, file_key, file_type="audio")
            else:
                return None
            stamp = int(time.time() * 1000)
            path = self.media_dir / f"{source}-{stamp}{suffix or '.bin'}"
            path.write_bytes(data)
            return path
        except Exception:
            return None

    def _transcribe_file(self, local_file: Path) -> str:
        if self.backend == "local":
            return self._transcribe_file_local(local_file)
        if self.backend == "cloud":
            return self._transcribe_file_cloud(local_file)
        if self.backend == "auto":
            local_text = self._transcribe_file_local(local_file)
            if local_text and not local_text.startswith("["):
                return local_text
            return self._transcribe_file_cloud(local_file)
        return f"[未知语音转写后端: {self.backend}]"

    def _transcribe_file_local(self, local_file: Path) -> str:
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            return (
                "[本机语音转写依赖未安装。请运行: "
                "pip install faster-whisper]"
            )

        try:
            if self._local_model is None:
                self._local_model = WhisperModel(
                    self.local_model_name,
                    device=self.local_device,
                    compute_type=self.local_compute_type,
                )
            segments, _ = self._local_model.transcribe(
                str(local_file),
                language=self.stt_language,
                vad_filter=True,
            )
            return "".join(segment.text for segment in segments).strip()
        except Exception as exc:
            return f"[本机语音转写失败: {exc}]"

    def _transcribe_file_cloud(self, local_file: Path) -> str:
        try:
            import httpx
        except ImportError:
            return "[云端语音转写依赖未安装。请运行: pip install httpx]"
        if not self.stt_api_key:
            return "[语音消息已收到，但未配置 VOICE_STT_API_KEY，暂无法云端转写]"
        headers = {"Authorization": f"Bearer {self.stt_api_key}"}
        with local_file.open("rb") as handle:
            files = {"file": (local_file.name, handle, "application/octet-stream")}
            data = {"model": self.stt_model, "language": self.stt_language}
            with httpx.Client(timeout=self.timeout_seconds) as client:
                resp = client.post(f"{self.stt_base}/audio/transcriptions", headers=headers, data=data, files=files)
                resp.raise_for_status()
                payload = resp.json()
        text = str(payload.get("text", "")).strip()
        return text
