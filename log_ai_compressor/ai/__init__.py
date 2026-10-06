# -*- coding: utf-8 -*-
"""可选 AI 解读层。

设计原则
--------
1. **可选**：不配置任何 API Key 时，整个工具照常工作（聚类 / 根因 / 异常检测
   / 导出全是本地算法）。AI 只在压缩结果之上再生成一段人话解读。
2. **三家通吃**：所有主流厂商都提供 OpenAI 兼容协议，只需换 base_url；
   本地 Ollama 走同一套协议，另外支持它的原生 /api/chat 接口。
3. **零新增依赖**：只用 httpx（FastAPI 已带），不引入各家 SDK。
4. **不静默外传**：未配置时前端明确显示「未启用」，不会偷偷把日志发出去。
"""
from __future__ import annotations

from typing import Any, Dict

from log_ai_compressor.ai.client import LLMError, chat
from log_ai_compressor.ai.config import (
    AIConfig,
    load_config,
    save_config,
    describe_config,
)
from log_ai_compressor.ai.prompts import (
    SYSTEM_ANALYST,
    build_explain_prompt,
    build_cluster_prompt,
)

__all__ = [
    "AIConfig", "LLMError", "chat", "load_config", "save_config",
    "describe_config", "build_explain_prompt", "build_cluster_prompt",
    "SYSTEM_ANALYST", "explain_result", "explain_cluster", "explain_job",
]


def _require_config() -> AIConfig:
    cfg = load_config()
    if not cfg.enabled:
        raise LLMError(
            "AI 解读未启用。请在「AI 设置」里选择一个服务商并填写 API Key，"
            "或改用本地 Ollama（完全离线、零成本）。")
    return cfg


def explain_result(result, *, cfg: AIConfig | None = None,
                   top_n: int = 5, question: str = "") -> str:
    """对整份分析结果生成 AI 解读。

    Args:
        result: core 层 AnalysisResult
        cfg: 可覆盖默认配置
        top_n: 送进上下文的错误簇数量（控制 token 成本）
        question: 用户额外追问（可选）
    """
    cfg = cfg or _require_config()
    from log_ai_compressor import service as S
    payload = S.result_to_dict(result, top_n=top_n, with_samples=False)
    prompt = build_explain_prompt(payload, question=question)
    return chat(prompt, cfg=cfg)


def explain_cluster(result, cluster_id: int, *,
                    cfg: AIConfig | None = None, question: str = "") -> str:
    """针对单个错误簇生成 AI 解读（带典型样例与降噪堆栈）。"""
    cfg = cfg or _require_config()
    from log_ai_compressor import service as S
    from log_ai_compressor.core.analysis import cooccurring_clusters
    target = next((c for c in result.clusters if c.cluster_id == cluster_id), None)
    if target is None:
        raise LLMError(f"错误簇 {cluster_id} 不存在")
    ctx = {
        "stats": S.result_to_dict(result, with_samples=False)["stats"],
        "cluster": S.cluster_to_dict(target, full=True),
        "cooccurring": [
            {"id": o.cluster_id, "hits": hits, "summary": o.summary}
            for o, hits in cooccurring_clusters(target, result.clusters)
        ],
    }
    prompt = build_cluster_prompt(ctx, question=question)
    return chat(prompt, cfg=cfg)


def explain_job(req: Dict[str, Any]) -> Dict[str, Any]:
    """Web 端点实现：按 job_id 找到结果 → 生成解读。

    Args:
        req: {"job_id": str, "cluster_id": int|None, "question": str, ...}
    """
    from log_ai_compressor.web import jobs as J

    job_id = str(req.get("job_id") or "")
    job = J.REGISTRY.get(job_id)
    if job is None or job.result is None:
        raise LLMError("结果不存在或已被淘汰（请重新分析）")

    cluster_id = req.get("cluster_id")
    question = str(req.get("question") or "")
    try:
        cfg = load_config(req.get("config") or {})
        if cluster_id not in (None, "", 0):
            text = explain_cluster(job.result, int(cluster_id), cfg=cfg,
                                   question=question)
        else:
            top_n = int(req.get("top_n") or 5)
            text = explain_result(job.result, cfg=cfg, top_n=top_n,
                                  question=question)
    except LLMError:
        raise
    except (TypeError, ValueError) as exc:
        raise LLMError(f"参数错误：{exc}")
    return {"text": text, "cluster_id": cluster_id, "config": cfg.to_public()}
