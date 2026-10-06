# -*- coding: utf-8 -*-
"""LLM 客户端：OpenAI 兼容协议 + Ollama 原生协议，只依赖 httpx。

为什么不用各家官方 SDK
----------------------
各家 SDK 语义大同小异（都是 POST /chat/completions），引进来要多 6~8 个
依赖，且换服务商还要改代码。统一走 OpenAI 兼容协议后，新增一家服务商
只是往 PROVIDERS 里加一条记录。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from log_ai_compressor.ai.config import AIConfig

# Ollama 原生接口（OpenAI 兼容层在部分版本上 /v1 不完整，原生更稳）
_OLLAMA_CHAT = "/api/chat"


class LLMError(RuntimeError):
    """AI 调用失败（配置问题、网络问题、被拒答、格式异常都归这里）。"""


def _chat_url(cfg: AIConfig) -> str:
    if cfg.native_ollama:
        return f"{cfg.base_url}{_OLLAMA_CHAT}"
    return f"{cfg.base_url}/chat/completions"


def _headers(cfg: AIConfig) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if not cfg.native_ollama and cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    return headers


def _payload(prompt: str, system: str, cfg: AIConfig,
             stream: bool = False) -> Dict[str, Any]:
    if cfg.native_ollama:
        return {
            "model": cfg.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}],
            "stream": stream,
            "options": {
                "temperature": cfg.temperature,
                "num_predict": cfg.max_tokens,
            },
        }
    return {
        "model": cfg.model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
        "stream": stream,
    }


def _extract_text(data: Dict[str, Any], native: bool) -> str:
    """两种协议的响应结构不同，统一取出正文。"""
    if native:
        message = data.get("message") or {}
        text = message.get("content")
        if isinstance(text, list):        # 部分版本返回内容块数组
            text = "".join(part.get("text", "") for part in text
                           if isinstance(part, dict))
        return (text or "").strip()
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return (message.get("content") or "").strip()


def chat(prompt: str, *, cfg: Optional[AIConfig] = None,
         system: str = "", history: Optional[List[Dict[str, str]]] = None
         ) -> str:
    """发起一次对话并返回文本。

    Args:
        prompt: 用户提示词
        cfg: 配置；None 时从配置文件/环境变量读
        system: 系统提示词
        history: 可选的历史消息 [{"role","content"}, ...]

    Raises:
        LLMError: 未启用 / 网络失败 / HTTP 报错 / 响应里没有正文
    """
    from log_ai_compressor.ai.config import load_config
    from log_ai_compressor.ai.prompts import SYSTEM_ANALYST

    cfg = cfg or load_config()
    if not cfg.enabled:
        raise LLMError(
            "AI 解读未启用：请在「AI 设置」选择服务商并填 API Key，"
            "或改用本地 Ollama（完全离线）。")

    messages: List[Dict[str, str]] = [{"role": "system",
                                        "content": system or SYSTEM_ANALYST}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": prompt})

    body = _payload(prompt, system or SYSTEM_ANALYST, cfg)
    if history:
        body["messages"] = messages

    url = _chat_url(cfg)
    try:
        with httpx.Client(timeout=cfg.timeout) as client:
            resp = client.post(url, headers=_headers(cfg), json=body)
    except httpx.TimeoutException:
        raise LLMError(f"请求超时（{cfg.timeout:.0f}s）。本地 Ollama 场景请确认 "
                       f"模型已拉取：ollama pull {cfg.model}")
    except httpx.ConnectError:
        raise LLMError(f"连不上 {url}。若用本地 Ollama，请先启动：ollama serve")
    except httpx.HTTPError as exc:
        raise LLMError(f"网络错误：{exc}")

    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.json().get("error", {}).get("message", "")
        except ValueError:
            detail = resp.text[:200]
        hint = ""
        if resp.status_code in (401, 403):
            hint = " —— 疑似 API Key 无效或无权限"
        elif resp.status_code == 404:
            hint = f" —— 疑似模型名不存在（当前 {cfg.model}）或 base_url 配错"
        elif resp.status_code == 429:
            hint = " —— 触发限流或余额不足"
        raise LLMError(f"服务商返回 {resp.status_code}{hint}：{detail or resp.text[:200]}")

    try:
        data = resp.json()
    except ValueError:
        raise LLMError(f"响应不是合法 JSON：{resp.text[:200]}")

    text = _extract_text(data, cfg.native_ollama)
    if not text:
        raise LLMError("服务商返回了空内容（可能被内容安全策略拦截或模型未响应）")
    return text
