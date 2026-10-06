# -*- coding: utf-8 -*-
"""MCP 接入层：把本机日志分析能力暴露给 AI Agent。

全部工具只读。没有删除、修改、上传、联网接口。
"""
from __future__ import annotations

from log_ai_compressor.mcp.server import main, server

__all__ = ["main", "server"]
