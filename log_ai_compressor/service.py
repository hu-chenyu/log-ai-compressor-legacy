# -*- coding: utf-8 -*-
"""共享服务层：Web / MCP / CLI 共用的门面。

设计意图
--------
`core` 层是纯算法、零 UI 依赖，直接返回 dataclass。但接入层（HTTP 响应、
MCP 工具返回值、JSON 报告）都需要 **可 JSON 序列化的结构**。把这段转换
逻辑抽到这里，Web 和 MCP 就不会各写一份、也不会各写错一份。

分层：`core`（算法）→ `service`（序列化门面）→ `web` / `mcp`（接入）
本模块不引入任何 Web / MCP 依赖，可被纯脚本直接 import。
"""
from __future__ import annotations

import math
import os
import re
from typing import Any, Dict, List, Optional, Sequence

from log_ai_compressor.core.analysis import cooccurring_clusters, simplify_stack
from log_ai_compressor.core.clustering import extract_variable_distribution
from log_ai_compressor.core.comparator import CompareResult, compare_files
from log_ai_compressor.core.models import AnalysisResult, ErrorCluster
from log_ai_compressor.core.pipeline import analyze_file, analyze_files, analyze_text
from log_ai_compressor.core.redact import redact_text
from log_ai_compressor.export.reporters import (
    SECTIONS_ALL,
    brief_summary,
    to_html,
    to_json,
    to_markdown,
    to_text,
)
# compare_to_markdown 在本模块内部没有调用点，但 web/jobs.py 与 mcp 都通过
# `S.compare_to_markdown` 取它 —— 属于**跨模块再导出**，删掉会静默炸掉对比
# 功能。显式列入 __all__ 让 ruff 知道这是有意为之。
from log_ai_compressor.export.reporters import compare_to_markdown as compare_to_markdown

# 与 GUI 旧版同源的档位常量（接入层共用，避免各处硬编码字符串）
SIMILARITY_PRESETS = {"strict": 0.95, "standard": 0.85, "lenient": 0.70}
ANALYZE_MODES = ("full", "deep", "fast")
MAXLINES_PRESETS = {"all": None, "100k": 100_000, "500k": 500_000, "1m": 1_000_000}
ENCODING_PRESETS = {
    "auto": None, "utf-8": "utf-8", "gb18030": "gb18030", "utf-16": "utf-16",
}
DEFAULT_LEVELS = ("ERROR", "FAIL")

# 单个簇详情返回的最大实例数（防止十万行日志把响应体撑爆；
# 完整实例仍保留在内存结果里，导出报告不受此限制）
MAX_INSTANCES_IN_PAYLOAD = 200


class ServiceError(ValueError):
    """参数校验失败（接入层应转成 4xx 而非 5xx）。"""


# ---------------------------------------------------------------------------
# 参数规范化
# ---------------------------------------------------------------------------
# 关键词分隔符：中文用户常打全角标点，必须一并归一，
# 否则 "超时；拒绝" 会变成一个字面量含「；」的关键词（静默不过滤）。
_SEP_TRANSLATION = str.maketrans({
    "，": ",",   # 全角逗号
    "、": ",",   # 顿号
    "；": ",",   # 全角分号
    ";": ",",    # 半角分号
    "　": " ",   # 全角空格
})


def _as_list(value: Any) -> Optional[List[str]]:
    """把逗号/空格分隔的关键词串或列表统一成 list[str]。"""
    if value is None:
        return None
    if isinstance(value, str):
        raw = value.translate(_SEP_TRANSLATION)
        items = [k.strip() for k in re.split(r"[,\s]+", raw) if k.strip()]
        return items or None
    items = [str(k).strip() for k in value if str(k).strip()]
    return items or None


def normalize_params(raw: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把任意来源（HTTP body / MCP args / CLI）的参数收敛成管线认识的形状。

    非法值在这里就抛 ServiceError，接入层无需各自做校验。
    """
    p = dict(raw or {})

    similarity = str(p.get("similarity") or "standard")
    if similarity not in SIMILARITY_PRESETS:
        raise ServiceError(
            f"similarity 必须是 {tuple(SIMILARITY_PRESETS)} 之一，收到 {similarity!r}")

    mode = str(p.get("analysis_mode") or "full")
    if mode not in ANALYZE_MODES:
        raise ServiceError(f"analysis_mode 必须是 {ANALYZE_MODES} 之一，收到 {mode!r}")

    maxlines_key = str(p.get("maxlines") or "all")
    if maxlines_key not in MAXLINES_PRESETS:
        raise ServiceError(
            f"maxlines 必须是 {tuple(MAXLINES_PRESETS)} 之一，收到 {maxlines_key!r}")

    # 编码：空值与 "auto" 一律归一为 None（交给 core 自动探测）；
    # "gbk" 是用户口语，实际按 gb18030 解码（超集，兼容 GB2312）
    encoding_raw = p.get("encoding")
    if isinstance(encoding_raw, str):
        encoding_raw = encoding_raw.strip()
    if not encoding_raw or encoding_raw in ("auto", "自动探测", "自动识别"):
        encoding = None
    elif isinstance(encoding_raw, str) and encoding_raw.lower() in ENCODING_PRESETS:
        encoding = ENCODING_PRESETS[encoding_raw.lower()]
    elif isinstance(encoding_raw, str) and encoding_raw.lower() == "gbk":
        encoding = "gb18030"
    else:
        raise ServiceError(
            f"不支持的编码：{p.get('encoding')!r}，可用："
            f"{tuple(k for k, v in ENCODING_PRESETS.items() if v)} 或 auto")

    max_lines = p.get("max_lines")
    if max_lines in (None, "", 0):
        max_lines = MAXLINES_PRESETS[maxlines_key]
    else:
        try:
            max_lines = int(max_lines)
        except (TypeError, ValueError):
            raise ServiceError(f"max_lines 必须是整数，收到 {p.get('max_lines')!r}")
        if max_lines <= 0:
            max_lines = None

    # 上下文行数：≥0 任意整数有效（负数按 0），非法回退 50
    try:
        context_lines = int(p.get("context_lines", 50))
    except (TypeError, ValueError):
        context_lines = 50
    context_lines = max(0, context_lines)

    top_n = p.get("top_n")
    if top_n not in (None, "", 0):
        try:
            top_n = int(top_n)
        except (TypeError, ValueError):
            raise ServiceError(f"top_n 必须是整数，收到 {p.get('top_n')!r}")

    levels = p.get("levels") or list(DEFAULT_LEVELS)
    if isinstance(levels, str):
        levels = _as_list(levels) or list(DEFAULT_LEVELS)
    levels = [str(lv).upper() for lv in levels] or list(DEFAULT_LEVELS)

    use_regex = bool(p.get("use_regex"))
    include = _as_list(p.get("include"))
    exclude = _as_list(p.get("exclude"))
    if use_regex:
        # 正则在管线内编译，非法正则必须在这里拦住，否则会退化成静默不过滤
        for item in (include or []) + (exclude or []):
            try:
                re.compile(item)
            except re.error as exc:
                raise ServiceError(f"关键词正则非法：{item!r} —— {exc}")

    def _float(key: str) -> Optional[float]:
        val = p.get(key)
        if val in (None, ""):
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            raise ServiceError(f"{key} 必须是时间戳，收到 {val!r}")

    return {
        "levels": levels,
        "top_n": top_n,
        "context_lines": context_lines,
        "rule": p.get("rule") or "auto",
        "analyze": bool(p.get("analyze", True)),
        "analysis_mode": mode,
        "similarity": similarity,
        "include": include,
        "exclude": exclude,
        "use_regex": use_regex,
        "encoding": encoding,
        "max_lines": max_lines,
        "time_start": _float("time_start"),
        "time_end": _float("time_end"),
        "tod_start": _float("tod_start"),
        "tod_end": _float("tod_end"),
        "progress_cb": p.get("progress_cb"),
        "cancel_event": p.get("cancel_event"),
    }


# ---------------------------------------------------------------------------
# 分析入口
# ---------------------------------------------------------------------------
def analyze(paths: Optional[Sequence[str]] = None,
            text: Optional[str] = None,
            params: Optional[Dict[str, Any]] = None) -> AnalysisResult:
    """统一分析入口：单文件 / 多文件（轮转合并）/ 粘贴文本。

    Raises:
        ServiceError: 参数非法，或没有提供任何输入。
    """
    cfg = normalize_params(params)
    if text is not None and str(text).strip():
        payload = text.lstrip("\ufeff")
        return analyze_text(payload, source=params.get("source", "<粘贴文本>")
                            if params else "<粘贴文本>", **_core_kwargs(cfg))
    cleaned = [str(p).strip() for p in (paths or []) if str(p).strip()]
    if not cleaned:
        raise ServiceError("没有可分析的内容：请提供日志文件路径或粘贴日志文本")
    missing = [p for p in cleaned if not os.path.isfile(p)]
    if missing:
        raise ServiceError(f"文件不存在：{', '.join(missing[:3])}"
                           + ("…" if len(missing) > 3 else ""))
    if len(cleaned) == 1:
        return analyze_file(cleaned[0], **_core_kwargs(cfg))
    return analyze_files(cleaned, **_core_kwargs(cfg))


def compare(paths: Sequence[str], params: Optional[Dict[str, Any]] = None
            ) -> List[CompareResult]:
    """多文件对比（第一个为基准）。对比模式不跑智能标记，只做差异。"""
    cleaned = [str(p).strip() for p in (paths or []) if str(p).strip()]
    if len(cleaned) < 2:
        raise ServiceError("对比分析至少需要 2 个日志文件")
    missing = [p for p in cleaned if not os.path.isfile(p)]
    if missing:
        raise ServiceError(f"文件不存在：{', '.join(missing[:3])}")
    cfg = normalize_params(params)
    # compare_files 不接受 top_n / analysis_mode / similarity / 时间与行数上限
    return compare_files(
        cleaned,
        levels=cfg["levels"],
        include=cfg["include"],
        exclude=cfg["exclude"],
        top_n=cfg["top_n"],
        context_lines=cfg["context_lines"],
        rule=cfg["rule"],
        use_regex=cfg["use_regex"],
        encoding=cfg["encoding"],
    )


def _core_kwargs(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """只保留 analyze_file/text 认识的参数（compare 用不到的一律剔除）。"""
    keys = ("levels", "top_n", "context_lines", "rule", "analyze", "analysis_mode",
            "similarity", "include", "exclude", "use_regex", "encoding", "max_lines",
            "time_start", "time_end", "tod_start", "tod_end",
            "progress_cb", "cancel_event")
    return {k: cfg[k] for k in keys if cfg.get(k) is not None}


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------
def _fnum(value: Optional[float]) -> Optional[float]:
    """float → JSON 安全值（NaN/Inf 不是合法 JSON，统一转 None）。"""
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _ts(value: Optional[float]) -> Optional[str]:
    """epoch → 本地时间字符串（无时间戳则 None）。"""
    if value is None or value <= 0:
        return None
    try:
        from datetime import datetime
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return None


def _hist(hist) -> Dict[str, Any]:
    """TimeHistogram → 前端可直接画图的序列。

    注意 core 层的接口形状：start / width / total 是 **property**，
    series() / burst_buckets() 是**方法**，且返回 [(时间, 计数), ...]，
    不是纯计数数组。取错会静默得到空图，这里逐项显式处理。
    """
    try:
        series = list(hist.series())          # [(epoch, count), ...]
        burst = list(hist.burst_buckets())    # [(epoch, count), ...]
        return {
            "start": _fnum(hist.start),
            "width": _fnum(hist.width),
            "total": int(hist.total),
            "series": [{"t": _fnum(t), "n": int(n)} for t, n in series],
            "burst": [{"t": _fnum(t), "n": int(n)} for t, n in burst],
        }
    except Exception:          # 直方图为纯辅助信息，失败不应拖垮整个响应
        return {"start": None, "width": None, "total": 0,
                "series": [], "burst": []}


def _entry(entry) -> Optional[Dict[str, Any]]:
    if entry is None:
        return None
    return {
        "line_no": entry.line_no,
        "last_line_no": entry.last_line_no,
        "raw": entry.raw,
        "level": entry.level,
        "module": entry.module,
        "message": entry.message,
        "message_extra": list(entry.message_extra),
        "stack": list(entry.stack),
        "timestamp": _fnum(entry.timestamp),
        "time_text": _ts(entry.timestamp),
    }


def cluster_to_dict(cluster: ErrorCluster, *,
                    include_sample: bool = True,
                    full: bool = False) -> Dict[str, Any]:
    """单个错误簇 → dict。

    Args:
        include_sample: 是否带典型样例（原始行 + 前后上下文 + 降噪堆栈）
        full: True 时带全部实例与变量分布（MCP「深度分析」用）
    """
    data: Dict[str, Any] = {
        "id": cluster.cluster_id,
        "level": cluster.level,
        "module": cluster.module,
        "summary": cluster.summary,
        "template": cluster.template,
        "count": cluster.count,
        "priority": cluster.priority,
        "priority_detail": cluster.priority_detail,
        # v2：置信档位 CONFIRMED / LIKELY / INSUFFICIENT
        "root_cause_confidence": cluster.root_cause_confidence or "",
        "first_line": cluster.first_line,
        "last_line": cluster.last_line,
        "first_seen": _fnum(cluster.first_seen),
        "last_seen": _fnum(cluster.last_seen),
        "first_seen_text": _ts(cluster.first_seen),
        "last_seen_text": _ts(cluster.last_seen),
        "is_root_cause": cluster.is_root_cause,
        "root_cause_reason": cluster.root_cause_reason,
        "root_timeline": cluster.root_timeline,
        "anomaly": cluster.anomaly,
        "related_clusters": list(cluster.related_clusters),
        "hist": _hist(cluster.hist),
        "instances_truncated": cluster.instances_truncated,
    }

    if include_sample and cluster.sample is not None:
        smp = cluster.sample
        entry = smp.entry
        # simplify_stack 已把系统库/第三方帧折叠成一行 "...... 已折叠 N 行 ......"
        # 占位，前端据此把该行与真实业务帧分别着色
        simplified = simplify_stack(entry.stack) if entry and entry.stack else None
        data["sample"] = {
            "entry": _entry(entry),
            "before": list(smp.before),
            "after": list(smp.after),
            "stack_simplified": (
                {
                    "lines": list(simplified.lines),
                    "business_count": simplified.business_count,
                    "noise_count": simplified.noise_count,
                } if simplified else None
            ),
        }
    else:
        data["sample"] = None

    if full:
        data["instances"] = [
            {
                "line_no": inst.line_no,
                "last_line_no": inst.last_line_no,
                "summary": inst.summary,
                "timestamp": _fnum(inst.timestamp),
                "time_text": _ts(inst.timestamp),
                "before": list(inst.before),
                "after": list(inst.after),
                "entry": _entry(inst.entry),
            }
            for inst in cluster.instances
        ]
        try:
            _, slots = extract_variable_distribution(
                cluster.template, [i.summary for i in cluster.instances])
            data["variables"] = [
                {"name": name, "values": [[v, n] for v, n in values]}
                for name, values in slots
            ]
        except Exception:
            data["variables"] = []
    return data


def result_to_dict(result: AnalysisResult, *, top_n: Optional[int] = None,
                   with_samples: bool = True) -> Dict[str, Any]:
    """AnalysisResult → 前端 / Agent 用的完整 dict。"""
    stats = result.stats
    clusters = result.clusters
    truncated = False
    if top_n is not None and top_n > 0 and len(clusters) > top_n:
        clusters = clusters[:top_n]
        truncated = True

    roots = [c for c in result.clusters if c.is_root_cause]
    cooc: List[Dict[str, Any]] = []
    if result.clusters:
        for other, hits in cooccurring_clusters(result.clusters[0], result.clusters):
            cooc.append({"id": other.cluster_id, "hits": hits,
                         "summary": other.summary})

    return {
        "stats": {
            "source": stats.source,
            "encoding": stats.encoding,
            "rule_name": stats.rule_name,
            "total_lines": stats.total_lines,
            "raw_chars": stats.raw_chars,
            "entry_lines": stats.entry_lines,
            "error_lines": stats.error_lines,
            "error_entries": stats.error_entries,
            "ts_entries": stats.ts_entries,
            "level_counts": dict(stats.level_counts),
            "duration": round(stats.duration, 3),
            "lines_per_second": round(stats.lines_per_second, 1),
            "time_start": _fnum(stats.time_start),
            "time_end": _fnum(stats.time_end),
            "time_range_text": (
                f"{_ts(stats.time_start)} ~ {_ts(stats.time_end)}"
                if stats.time_start and stats.time_end else ""
            ),
            "truncated": stats.truncated,
            "limit_hit": stats.limit_hit,
        },
        # v2：证据充分性评估（analysis.assess_evidence 产出）
        "evidence": result.evidence or {},
        "keywords": list(result.keywords),
        "global_hist": _hist(result.global_hist),
        "root_causes": [
            {"id": c.cluster_id, "summary": c.summary, "reason": c.root_cause_reason}
            for c in roots
        ],
        "cooccurring": cooc,
        "clusters": [cluster_to_dict(c, include_sample=with_samples)
                     for c in clusters],
        "total_clusters": len(result.clusters),
        "shown_clusters": len(clusters),
        "top_n_truncated": truncated,
    }


def compare_to_dicts(results: List[CompareResult]) -> List[Dict[str, Any]]:
    """对比结果 → dict 列表。

    差异项只保留「摘要 + 次数 + 级别」，明细靠调用方按需再分析
    （原始对象里是完整条目，序列化后体积会翻十几倍）。
    """
    def items(seq):
        return [
            {
                "summary": getattr(it, "summary", ""),
                "level": getattr(it, "level", ""),
                "count": getattr(it, "count", 0),
            }
            for it in seq
        ]
    return [
        {
            "base_name": r.base_name,
            "other_name": r.other_name,
            "new_items": items(r.new_items),
            "gone_items": items(r.gone_items),
            "common_items": items(r.common_items),
            "new_count": len(r.new_items),
            "gone_count": len(r.gone_items),
            "common_count": len(r.common_items),
        }
        for r in results
    ]


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
EXPORT_FORMATS = {
    "md": to_markdown, "markdown": to_markdown,
    "json": to_json, "json_full": to_json,
    "txt": to_text, "text": to_text,
    "html": to_html,
    "summary": brief_summary,
}

MEDIA_TYPES = {
    "md": "text/markdown; charset=utf-8", "markdown": "text/markdown; charset=utf-8",
    "json": "application/json; charset=utf-8", "json_full": "application/json; charset=utf-8",
    "txt": "text/plain; charset=utf-8", "text": "text/plain; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "summary": "text/plain; charset=utf-8",
}


def export_text(result: AnalysisResult, fmt: str, *,
                top_n: Optional[int] = None,
                sections: Optional[Sequence[str]] = None,
                redact: bool = False,
                custom_rules: Optional[Sequence[str]] = None) -> str:
    """按格式导出报告文本。fmt 见 EXPORT_FORMATS。

    `json_full` 走完整序列化（含全部实例与变量分布），供机器消费。
    """
    key = (fmt or "md").lower()
    if key not in EXPORT_FORMATS:
        raise ServiceError(
            f"不支持的导出格式 {fmt!r}，可用：{sorted(set(EXPORT_FORMATS))}")
    if key == "json_full":
        text = to_json(result, top_n=top_n, full=True)
    elif key == "json":
        text = to_json(result, top_n=top_n, full=False)
    elif key in ("md", "markdown", "html"):
        text = EXPORT_FORMATS[key](result, top_n=top_n,
                                    sections=sections or list(SECTIONS_ALL))
    else:
        text = EXPORT_FORMATS[key](result, top_n=top_n)
    if redact:
        text = redact_text(text, list(custom_rules or []) or None)
    return text


def media_type(fmt: str) -> str:
    return MEDIA_TYPES.get((fmt or "md").lower(), "text/plain; charset=utf-8")
