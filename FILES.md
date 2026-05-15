# 文件地图

这个文件给新会话快速了解项目结构用。先读这里，再按需打开具体文件。

## 根目录

- `README.md`：项目介绍、依赖安装、环境变量、启动方式和常见问题。
- `Makefile`：常用命令入口，包含 `make run`、`make test`、`make check`。
- `.gitignore`：忽略本地环境变量、缓存、虚拟环境和实验目录。
- `channel.py`：早期通道功能模板，保留作参考。
- `skills.py`：早期 skills 加载模板，保留作参考。

## `src/`

- `src/loop.py`：主启动入口，负责装配通道、网关、记忆和运行循环。
- `src/channel.py`：通道层，包含 CLI、Telegram、飞书消息解析、发送和轮询。
- `src/gateway.py`：Agent 网关，负责工具定义、工具执行、skills 注入和单轮对话。
- `src/memory.py`：会话记忆和用户资料追加逻辑。
- `src/skills_runtime.py`：扫描 `~/skills`，解析 `SKILL.md`，按需加载 skill 正文。
- `src/voice_pipeline.py`：Telegram/飞书语音文件下载后统一转写，默认使用本机 `faster-whisper`。

## `tests/`

- `tests/test_loop_channels.py`：通道会话键和 CLI 通道基础测试。
- `tests/test_memory.py`：用户资料追加逻辑测试。
- `tests/test_gateway_tools.py`：网关工具兼容性测试。

## 运行时目录

- `~/.companyclaw/SYSTEM.md`：基础系统提示词。
- `~/.companyclaw/USER_PROFILE.md`：用户长期资料，`append_user_note` 会追加到这里；需要更新旧内容时用 `read_file` 加 `edit_file`。
- `~/.companyclaw/.state/telegram/`：Telegram offset 状态，避免重复消费旧消息。
- `~/.companyclaw/media/voice/`：Telegram/飞书语音文件下载缓存。
- `~/skills/`：持久化 skills 目录，每个技能通常是 `~/skills/<name>/SKILL.md`。
