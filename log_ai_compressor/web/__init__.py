# -*- coding: utf-8 -*-
"""本地 Web 接入层：FastAPI 服务 + 浏览器界面。

定位：服务只监听 127.0.0.1，日志不出网。
"""
from __future__ import annotations

from log_ai_compressor.web.server import create_app

__all__ = ["create_app"]
