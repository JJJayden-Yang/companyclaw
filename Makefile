.PHONY: help run voice test check

PYTHON ?= python3
VOICE_PORT ?= 8765

help:
	@echo "可用命令:"
	@echo "  make run       启动 CLI/TG agent"
	@echo "  make voice     启动 HTTP 语音网关"
	@echo "  make test      运行单元测试"
	@echo "  make check     运行编译检查和单元测试"

run:
	$(PYTHON) src/loop.py

voice:
	$(PYTHON) src/voice_server.py $(VOICE_PORT)

test:
	$(PYTHON) -m unittest discover -s tests -v

check:
	$(PYTHON) -m py_compile src/loop.py src/channel.py src/gateway.py src/memory.py src/skills_runtime.py src/voice_pipeline.py src/voice_server.py
	$(PYTHON) -m unittest discover -s tests -v
