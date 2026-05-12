# companyclaw

一个支持多通道（CLI、Telegram、飞书）的 Agent 循环项目，内置工具调用与技能（skills）按需加载能力。

## 功能概览

- 多通道消息输入与回复：
  - `CLI`（终端输入）
  - `Telegram`（Bot API 长轮询）
  - `Feishu`（当前包含发送与事件解析基础能力）
- 工具调用：
  - `bash`
  - `read_file`
  - `write_file`
  - `edit_file`
- skills 机制：
  - 从 `~/skills` 持久化读取
  - 支持 `list_skills` / `reload_skills` / `load_skill`
  - 技能元信息会自动注入系统提示词

## 目录结构

- `src/loop.py`：主程序入口与核心循环
- `channel.py`：通道示例/模板代码
- `skills.py`：skills 模板代码
- `tests/test_loop_channels.py`：基础测试

## 环境准备

1. Python 版本建议：`3.10+`
2. 安装依赖：

```bash
pip install anthropic python-dotenv
pip install httpx        # 如果要接 Telegram / 飞书
pip install pyyaml       # 如果 skills frontmatter 需要完整 YAML 解析
pip install faster-whisper  # 如果要本机语音转文字
pip install fastapi uvicorn # 如果要启动 HTTP 语音网关
```

## 环境变量

在项目根目录准备 `.env`（可参考下面内容）：

```bash
COMPANYCLAW_API_KEY=你的key
COMPANYCLAW_BASE_URL=你的网关地址   # 可选
MODEL_ID=deepseek-v4-flash

# Telegram 可选
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_CHATS=

# 飞书可选
FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_ENCRYPT_KEY=
FEISHU_BOT_OPEN_ID=
FEISHU_IS_LARK=false

# 语音转文字可选，默认走本机 faster-whisper
VOICE_STT_BACKEND=local
VOICE_LOCAL_MODEL=small
VOICE_LOCAL_DEVICE=auto
VOICE_LOCAL_COMPUTE_TYPE=int8
VOICE_STT_LANGUAGE=zh
```

## 启动方式

```bash
python3 src/loop.py
```

启动后：
- 直接在终端输入就是 CLI 通道对话
- 若配置了 `TELEGRAM_BOT_TOKEN`，会自动启动 Telegram 轮询线程

## HTTP 语音网关

如果已有前端能把语音转成文本，可以把文本发到这个 HTTP 网关。它会复用真正的 `AgentGateway`，因此支持 `bash`、文件工具和 skills。

```bash
python3 src/voice_server.py 8765
```

接口：
- `POST /chat`：请求体 `{"message": "你好", "session_id": "voice-user"}`
- `POST /reset`：清空语音网关会话记忆
- `GET /health`：健康检查

## skills 使用方式

skills 默认目录：`~/skills`

建议结构：

```text
~/skills/
  my-skill/
    SKILL.md
```

`SKILL.md` 示例：

```md
---
name: my-skill
description: 这是一个示例技能
---

这里写技能正文内容。
```

可用工具：
- `list_skills`：查看当前已加载技能
- `reload_skills`：从磁盘重新扫描 `~/skills`
- `load_skill`：按技能名加载完整技能正文

## 测试与检查

```bash
python3 -m py_compile src/loop.py
python3 -m unittest tests.test_loop_channels -v
```

## 常见问题

1. Telegram 消息延迟明显：
   - 已在 `src/loop.py` 调整为短轮询与低延迟文本缓冲
2. 启动时报缺少依赖：
   - 按“环境准备”重新安装依赖
3. `load_skill` 找不到技能：
   - 检查路径是否为 `~/skills/<name>/SKILL.md`
   - 执行 `reload_skills`
