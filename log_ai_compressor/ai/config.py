# -*- coding: utf-8 -*-
"""AI 服务商配置：环境变量 + 用户配置文件双来源。

优先级按字段区分（详见 load_config 的 docstring）：服务商/模型看配置文件，
Key 优先看环境变量。简单记法：**在界面上选好的东西别被环境变量改掉，
但 Key 仍可安全地只放在环境变量里。**

为什么配置文件放用户主目录而不是项目目录
--------------------------------------
项目目录可能被 clone 到只读位置、或多人共用的 CI 上；而 API Key 属于
用户个人凭据，必须跟着用户走，不该进版本库。
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

CONFIG_DIR = Path(os.path.expanduser("~")) / ".log-ai-compressor"
CONFIG_FILE = CONFIG_DIR / "ai.json"

# 内置服务商：全部走 OpenAI 兼容协议，只有 Ollama 额外支持原生接口
PROVIDERS: Dict[str, Dict[str, Any]] = {
    "none": {
        "label": "不启用（仅本地分析）",
        "base_url": "", "model": "", "env_key": "",
        "note": "不配置任何外部服务，零出网、零成本。AI 解读不可用。",
    },
    "deepseek": {
        "label": "DeepSeek（国内直连，最便宜）",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "env_key": "DEEPSEEK_API_KEY",
        "note": "输入约 $0.14 / 百万 tokens，中文日志理解好，推荐首选。",
    },
    "qwen": {
        "label": "阿里百炼 Qwen（通义千问）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "env_key": "DASHSCOPE_API_KEY",
        "note": "国内节点多，企业合规友好。",
    },
    "glm": {
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "env_key": "ZHIPU_API_KEY",
        "note": "GLM-4-Flash 近乎免费，中文场景性价比高。",
    },
    "kimi": {
        "label": "月之暗面 Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-32k",
        "env_key": "MOONSHOT_API_KEY",
        "note": "长上下文（128K+），适合超大日志。",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "env_key": "OPENAI_API_KEY",
        "note": "国内访问通常需要代理。",
    },
    "ollama": {
        "label": "本地 Ollama（完全离线，零成本）",
        "base_url": "http://127.0.0.1:11434",
        "model": "qwen2.5:7b",
        "env_key": "",
        "note": "数据完全不出本机，无需 API Key。需先 ollama pull 模型。",
    },
    "custom": {
        "label": "自定义 OpenAI 兼容端点",
        "base_url": "", "model": "", "env_key": "OPENAI_API_KEY",
        "note": "任何暴露 /chat/completions 的服务都可以填进来。",
    },
}


@dataclass
class AIConfig:
    """一份可直接发起对话的配置。"""
    provider: str = "none"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    temperature: float = 0.2
    max_tokens: int = 2000
    timeout: float = 120.0
    # 由 provider 推导，custom 时可强制指定
    native_ollama: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        if self.provider == "none" or self.provider == "":
            return False
        if self.provider == "ollama":
            # 本地 Ollama 不需要 key，但要有 base_url
            return bool(self.base_url and self.model)
        return bool(self.base_url and self.model and self.api_key)

    def masked_key(self) -> str:
        if not self.api_key:
            return ""
        if len(self.api_key) <= 8:
            return "*" * len(self.api_key)
        return f"{self.api_key[:4]}{'*' * 6}{self.api_key[-4:]}"

    def to_public(self) -> Dict[str, Any]:
        """给前端的表示：绝不回传明文 Key。"""
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "has_key": bool(self.api_key),
            "masked_key": self.masked_key(),
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }


def load_config(overrides: Optional[Dict[str, Any]] = None) -> AIConfig:
    """合并三层来源得到最终配置。

    优先级**分字段**决定，不是统一一套：

    - provider / base_url / model：显式入参 > **配置文件** > 环境变量 > 默认
      理由：Web 界面与 CLI config 是用户最明确的操作，环境变量只是逃生舱；
      若让环境变量压过配置文件，用户在界面上选好的服务商会被一个残留的
      环境变量悄悄改掉。
    - api_key：显式入参 > **环境变量** > 配置文件
      理由：Key 是机密，最常见的用法是「界面上选好服务商与模型，Key 走
      环境变量注入」，不该为了换个 Key 去改配置文件。
    """
    stored: Dict[str, Any] = {}
    try:
        if CONFIG_FILE.is_file():
            stored = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # 配置文件损坏不该让程序起不来，退回默认即可
        stored = {}

    ov = overrides or {}

    def pick(key: str, env_name: str, default: str = "") -> str:
        value = ov.get(key)
        if value not in (None, ""):
            return str(value)
        value = stored.get(key)
        if value not in (None, ""):
            return str(value)
        return os.environ.get(env_name, default) or default

    provider = pick("provider", "LOG_AI_PROVIDER", "none")
    preset = PROVIDERS.get(provider, PROVIDERS["none"])

    base_url = (pick("base_url", "LOG_AI_BASE_URL")
                or preset.get("base_url") or "").rstrip("/")
    model = pick("model", "LOG_AI_MODEL") or preset.get("model") or ""

    env_key = preset.get("env_key") or ""
    api_key = (str(ov.get("api_key") or "").strip()
               or (os.environ.get(env_key, "").strip() if env_key else "")
               or str(stored.get("api_key") or "").strip())

    def _num(key: str, default: float) -> float:
        raw = ov.get(key, stored.get(key, default))
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    return AIConfig(
        provider=provider,
        base_url=base_url,
        model=model,
        api_key=api_key,
        temperature=_num("temperature", 0.2),
        max_tokens=int(_num("max_tokens", 2000)),
        timeout=_num("timeout", 120.0),
        native_ollama=(provider == "ollama"),
    )


def save_config(cfg: AIConfig) -> None:
    """落盘到用户配置目录（不写入项目目录，避免误提交）。"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = asdict(cfg)
    data.pop("native_ollama", None)
    CONFIG_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def describe_config() -> Dict[str, Any]:
    """给 /api/health 与前端设置面板用。"""
    cfg = load_config()
    return {
        "available": cfg.enabled,
        "config": cfg.to_public(),
        "config_file": str(CONFIG_FILE),
        "providers": [
            {"key": key,
             "label": meta.get("label", key),
             "note": meta.get("note", ""),
             "env_key": meta.get("env_key", ""),
             "needs_key": bool(meta.get("env_key")) and key != "ollama"}
            for key, meta in PROVIDERS.items()
        ],
        "reason": "" if cfg.enabled else (
            "未配置服务商 —— 聚类/根因/异常检测等本地能力不受影响，仅 AI 解读不可用。"
        ),
    }
