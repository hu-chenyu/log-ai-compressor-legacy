# -*- coding: utf-8 -*-
"""导出层：结构化报告输出（Markdown / JSON / 纯文本）。

Markdown 格式专为「投喂大模型」优化：
- 一、概览统计 —— 让模型快速建立全局认知；
- 二、Top 错误清单 —— 表格化去重后的错误全集；
- 三、典型样例详情 —— 每簇一份样例 + 前后上下文 + 降噪堆栈，
  避免原始日志的重复内容浪费上下文窗口。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import List, Optional

from log_ai_compressor import __version__
from log_ai_compressor.constants import DEFAULT_TOP_N
from log_ai_compressor.core.analysis import simplify_stack
from log_ai_compressor.core.comparator import CompareResult
from log_ai_compressor.core.models import (
    AnalysisResult,
    ErrorCluster,
    format_timestamp,
)

# 优化缺陷R75：新增 周期发作/新型错误（异常检测强化产出）
_ANOMALY_LABELS = {"burst": "集中爆发", "rare": "罕见异常",
                   "periodic": "周期发作", "novel": "新型错误", "": ""}

# 优化缺陷R58：导出内容板块（选项对话框勾选 → 各格式按板块生成）
SECTIONS_ALL = ("overview", "list", "detail", "instances")


def _sections(sections) -> set:
    """板块集合归一化（None = 全量）。"""
    return set(sections) if sections else set(SECTIONS_ALL)


def _instances_line(c: ErrorCluster) -> str:
    """实例行号索引（紧凑单行：L266, L315, …；优化缺陷R58）。"""
    if not c.instances:
        return ""
    refs = ", ".join(f"L{i.line_no}" for i in c.instances)
    return f"实例行号（{len(c.instances)}）：{refs}"


def _iso_ts(t: Optional[float]) -> Optional[str]:
    """epoch 秒 → ISO 8601 可读字符串（UTC，与日志原始 Z 时间一致）。

    修复缺陷R60：JSON 导出时间戳可读化（原始 epoch 浮点人不可读；
    ISO 字符串仍可被 datetime.fromisoformat 机器解析，人/脚本两用）；
    None 原样保留，异常值退化为原字符串。
    """
    if t is None:
        return None
    try:
        return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return str(t)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _md_escape(text: str) -> str:
    """Markdown 表格单元格转义。"""
    return (text or "").replace("|", "\\|").replace("\n", " ").replace("`", "'")


def _ts_range(result: AnalysisResult) -> str:
    s = result.stats
    if s.time_start is None:
        return "-"
    return f"{format_timestamp(s.time_start)} ~ {format_timestamp(s.time_end)}"


_VERDICT_LABEL = {
    "CONFIRMED": "已定位根因",
    "LIKELY": "可能原因（统计推断，非因果证明）",
    "INSUFFICIENT": "无法判定根因",
}


def _root_summary(result: AnalysisResult) -> str:
    """根因摘要 —— **受证据评估约束**。

    v1 无条件输出「初步定位根因：X」，把关键词投票/时间先后这类统计线索
    当成结论，导致「连接池刷了 370 次 → 根因：连接池耗尽」这种自信的错误
    （真正的因只出现 10 次）。现在只有在拿到 Caused-by 因果链
    （verdict=CONFIRMED）时才敢说"根因"。
    """
    roots = [c for c in result.clusters if c.is_root_cause]
    if not roots:
        return "未发现明确根因（无时间连锁 / Caused-by 链 / 强根因特征）"
    top = roots[0]
    others = f"（另有 {len(roots) - 1} 个根因候选）" if len(roots) > 1 else ""
    # 标签由调用方统一加（_VERDICT_LABEL），这里只给内容，避免重复
    return f"{top.summary[:120]}{others}"


def _evidence_gaps_md(result: AnalysisResult, limit: int = 5) -> List[str]:
    """把证据缺口渲染成 Markdown 小节（证据不足时才有内容）。"""
    ev = result.evidence or {}
    if ev.get("verdict") == "CONFIRMED" or not ev.get("gaps"):
        return []
    out: List[str] = ["", "### ⚠ 证据充分性评估", ""]
    out.append(f"**判定：{_VERDICT_LABEL.get(ev.get('verdict'), ev.get('verdict'))}**")
    out.append("")
    out.append("这份日志里**没有**能证明因果方向的证据。以下候选是统计线索，"
               "不是结论 —— 值班时照着错误根因排查会浪费时间。")
    out.append("")
    out.append("补齐下列证据才能定论：")
    out.append("")
    for g in ev["gaps"][:limit]:
        out.append(f"- **{g.get('title')}**")
        out.append(f"  - 为什么重要：{g.get('why')}")
        out.append(f"  - 现在缺：{g.get('missing')}")
    if ev.get("inputs_used"):
        out.append("")
        out.append(f"> 本次实际用到的证据：{'、'.join(ev['inputs_used'])}")
    return out


def _anomaly_label(c: ErrorCluster) -> str:
    return _ANOMALY_LABELS.get(c.anomaly, c.anomaly)


def _rate_text(lps: float) -> str:
    if lps >= 10000:
        return f"{lps / 10000:.1f} 万行/秒"
    return f"{lps:.0f} 行/秒"


# ---------------------------------------------------------------------------
# 优化缺陷R99：Token 估算 + 压缩率展示（贴合「压缩投喂大模型」定位）
# ---------------------------------------------------------------------------
def estimate_tokens(text: str) -> int:
    """粗估文本 token 数：ASCII ≈4 字符/token，CJK ≈1.5 字符/token。

    经验公式（GPT 系 tokenizer 均值），仅用于量级展示，非精确计费。
    """
    if not text:
        return 0
    ascii_n = 0
    cjk_n = 0
    for ch in text:
        if ord(ch) >= 0x2E80:
            cjk_n += 1
        else:
            ascii_n += 1
    return max(1, round(ascii_n / 4 + cjk_n / 1.5))


def estimate_raw_tokens(raw_chars: int) -> int:
    """原始日志侧 token 粗估：逐行流式只留了字符数，日志绝大多数
    为 ASCII，按 4 字符/token 折算。"""
    return max(0, round(raw_chars / 4))


def _fmt_tokens(n: int) -> str:
    """token 数紧凑格式化：3200 → '3.2k'，1_200_000 → '1.2M'。"""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _ratio_text(comp: int, raw: int) -> str:
    """压缩率文案：≥99.5% 时保留一位小数（防 99.7% 四舍五入成
    '100%' 读作 bug；修复缺陷R106）。"""
    ratio = (1 - comp / raw) * 100
    if ratio >= 99.95:
        return "99.9"
    if ratio >= 99.5:
        return f"{ratio:.1f}"
    return str(round(ratio))


def token_status_text(result: AnalysisResult, compressed_text: str) -> str:
    """状态栏紧凑串：'≈3.2k tokens · 压缩 96%'（原始字符数为 0 时
    只显示压缩侧，不出压缩率）。"""
    comp = estimate_tokens(compressed_text)
    raw = estimate_raw_tokens(result.stats.raw_chars)
    if raw <= 0 or comp >= raw:
        return f"≈{_fmt_tokens(comp)} tokens"
    return f"≈{_fmt_tokens(comp)} tokens · 压缩 {_ratio_text(comp, raw)}%"


def token_report_line(result: AnalysisResult, compressed_text: str) -> str:
    """报告/摘要头部行：原始 → 压缩 双向 token 与压缩率。"""
    comp = estimate_tokens(compressed_text)
    raw = estimate_raw_tokens(result.stats.raw_chars)
    if raw <= 0 or comp >= raw:
        return f"压缩后 ≈{_fmt_tokens(comp)} tokens（原始字符数未统计）"
    return (f"原始日志 ≈{_fmt_tokens(raw)} tokens → 压缩后 "
            f"≈{_fmt_tokens(comp)} tokens（压缩 {_ratio_text(comp, raw)}%）")


# ---------------------------------------------------------------------------
# Markdown 报告
# ---------------------------------------------------------------------------
def to_markdown(result: AnalysisResult, top_n: Optional[int] = None,
                title: Optional[str] = None, sections=None) -> str:
    """生成适配大模型输入的 Markdown 结构化报告。"""
    n = top_n or DEFAULT_TOP_N
    clusters = result.clusters[:n]
    s = result.stats
    secs = _sections(sections)
    title = title or f"日志AI压缩报告：{s.source}"

    lines: List[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"> log-ai-compressor v{__version__} | "
                 f"处理 {s.total_lines} 行 | 耗时 {s.duration:.2f}s | "
                 f"{_rate_text(s.lines_per_second)} | 规则 {s.rule_name}")
    lines.append("")
    lines.append(f"**{_VERDICT_LABEL.get((result.evidence or {}).get('verdict'), '初步定位根因')}**：{_md_escape(_root_summary(result))}")
    lines.extend(_evidence_gaps_md(result))
    lines.append("")
    # 优化缺陷R99：token 估算占位行（文末回填，避免自引用长度死循环）
    lines.append("__TOKEN_LINE_R99__")
    lines.append("")

    # 一、概览统计
    if "overview" in secs:
        lines.append("## 一、概览统计")
        lines.append("")
        lines.append("| 指标 | 数值 |")
        lines.append("| --- | --- |")
        lines.append(f"| 日志来源 | {_md_escape(s.source)} |")
        lines.append(f"| 编码 | {s.encoding} |")
        lines.append(f"| 总行数 | {s.total_lines} |")
        lines.append(f"| 错误行数（FATAL/ERROR/FAIL） | {s.error_lines} |")
        lines.append(f"| 错误种类数（去重后） | {len(result.clusters)} |")
        lines.append(f"| 错误总次数（过滤后） | {s.error_entries} |")
        lines.append(f"| 日志时间范围 | {_ts_range(result)} |")
        if s.truncated:
            lines.append(f"| 处理状态 | 用户取消，已处理前 {s.total_lines} 行 |")
        lines.append("")
        level_parts = [f"{k}={v}" for k, v in sorted(s.level_counts.items())]
        lines.append(f"级别分布：{', '.join(level_parts) if level_parts else '-'}")
        lines.append("")

    # 二、Top 错误清单
    if "list" in secs:
        lines.append(f"## 二、Top {len(clusters)} 错误清单（按优先级排序）")
        lines.append("")
        if not clusters:
            lines.append("未发现符合条件的错误。")
            text = "\n".join(lines)
            return text.replace(
                "__TOKEN_LINE_R99__",
                "> " + token_report_line(result, text)) + "\n"
        lines.append("| # | 优先级 | 级别 | 次数 | 模块 | 根因 | 异常 | 错误摘要 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for i, c in enumerate(clusters, 1):
            root = (f"✔ {_md_escape(c.root_cause_reason[:16])}"
                    if c.is_root_cause else "—")
            lines.append(
                f"| {i} | {c.priority_label} | {c.level} | {c.count} | "
                f"{_md_escape(c.module) or '-'} | {root} | "
                f"{_anomaly_label(c) or '—'} | {_md_escape(c.summary)} |")
        lines.append("")

    # 三、典型样例详情
    if "detail" in secs:
        lines.append("## 三、典型样例详情（每错误一份，含上下文与降噪堆栈）")
        lines.append("")
        for i, c in enumerate(clusters, 1):
            lines.extend(_cluster_detail_md(
                i, c, include_instances="instances" in secs))
    # 优化缺陷R99：回填 token 估算行（按最终全文长度估算压缩侧）
    text = "\n".join(lines)
    text = text.replace("__TOKEN_LINE_R99__",
                        "> " + token_report_line(result, text))
    return text + "\n"


def _cluster_detail_md(index: int, c: ErrorCluster,
                       include_instances: bool = False) -> List[str]:
    """单个错误簇的详情段落。"""
    out: List[str] = []
    out.append(f"### {index}. [{c.priority_label}][{c.level}] "
               f"{_md_escape(c.summary)}")
    out.append("")
    meta = [f"出现 {c.count} 次", f"行 {c.first_line}~{c.last_line}"]
    if c.first_seen is not None:
        meta.append(f"首末时间 {format_timestamp(c.first_seen)}"
                    f" ~ {format_timestamp(c.last_seen)}")
    if c.module:
        meta.append(f"模块 {c.module}")
    out.append(f"- {' | '.join(meta)}")
    notes = []
    if c.is_root_cause:
        notes.append(f"根因：{c.root_cause_reason}")
    elif c.root_cause_reason:
        notes.append(c.root_cause_reason)
    if c.anomaly:
        notes.append(f"异常：{_anomaly_label(c)}")
    if notes:
        out.append(f"- 智能分析：{'；'.join(notes)}")
    # 优化缺陷R58：实例行号索引（板块勾选时随详情输出）
    if include_instances:
        inst_line = _instances_line(c)
        if inst_line:
            out.append(f"- {inst_line}")
    out.append("")

    sample = c.sample
    if sample is None:
        out.append("（无典型样例）")
        out.append("")
        return out

    entry = sample.entry
    if sample.before:
        out.append("**前上下文**")
        out.append("```")
        out.extend(sample.before)
        out.append("```")
        out.append("")

    out.append("**典型样例**")
    out.append("```")
    out.append(entry.raw)
    if entry.message_extra:
        out.extend(entry.message_extra)
    out.append("```")
    out.append("")

    if entry.stack:
        simplified = simplify_stack(entry.stack)
        out.append(f"**堆栈（已降噪：业务帧 {simplified.business_count} 行，"
                   f"折叠系统/第三方帧 {simplified.noise_count} 行）**")
        out.append("```")
        out.extend(simplified.lines)
        out.append("```")
        out.append("")

    if sample.after:
        out.append("**后上下文**")
        out.append("```")
        out.extend(sample.after)
        out.append("```")
        out.append("")
    return out


# ---------------------------------------------------------------------------
# 简要摘要（一键复制投喂 AI）
# ---------------------------------------------------------------------------
def brief_summary(result: AnalysisResult, top_n: Optional[int] = None) -> str:
    """生成精简文本摘要（适合直接粘贴给 AI 助手）。"""
    n = top_n or min(DEFAULT_TOP_N, 10)
    s = result.stats
    out: List[str] = []
    out.append(f"【日志分析摘要】{s.source}")
    out.append(f"总行数 {s.total_lines}，错误 {s.error_lines} 行，"
               f"去重后 {len(result.clusters)} 种，时间范围 {_ts_range(result)}。")
    out.append(f"{_VERDICT_LABEL.get((result.evidence or {}).get('verdict'), '初步根因')}：{_root_summary(result)}")
    ev = result.evidence or {}
    if ev.get("verdict") != "CONFIRMED":
        for g in (ev.get("gaps") or [])[:3]:
            out.append(f"  · 缺：{g.get('title')} —— {g.get('missing')}")
    out.append("")
    for i, c in enumerate(result.clusters[:n], 1):
        tags = [c.priority_label, c.level, f"×{c.count}"]
        if c.is_root_cause:
            tags.append("根因")
        if c.anomaly:
            tags.append(_anomaly_label(c))
        # 优化缺陷R62：摘要附行号范围 —— 粘贴投喂 AI 后对方可直接
        # 定位原文位置（用户决策，低成本高价值）
        tags.append(f"行 {c.first_line}~{c.last_line}")
        out.append(f"{i}. [{' | '.join(tags)}] {c.summary}")
        if c.is_root_cause:
            out.append(f"   -> {c.root_cause_reason}")
    # 优化缺陷R99：头部第二行插入 token 估算（粘贴投喂 AI 时自带
    # 压缩率；按最终文本长度估算，忽略本行自身的自引用误差）
    body = "\n".join(out)
    out.insert(1, token_report_line(result, body))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# JSON 导出
# ---------------------------------------------------------------------------
def _cluster_dict(c: ErrorCluster, with_sample: bool = True,
                  full: bool = True) -> dict:
    d = {
        "id": c.cluster_id,
        "level": c.level,
        "module": c.module,
        "summary": c.summary,
        "count": c.count,
        "priority": c.priority,
        "priority_label": c.priority_label,
        "root_cause": c.is_root_cause,
        "root_cause_reason": c.root_cause_reason,
        "anomaly": c.anomaly,
        "first_line": c.first_line,
        "last_line": c.last_line,
        # 修复缺陷R60：时间戳 ISO 可读化（原 epoch 浮点人不可读）
        "first_seen": _iso_ts(c.first_seen),
        "last_seen": _iso_ts(c.last_seen),
    }
    # 修复缺陷R61：精简模式 —— 仅核心字段 + 实例行号列表（不含
    # 上下文/堆栈/样例原文），人可通读；full 模式供脚本二次处理
    if not full:
        d["instance_lines"] = [i.line_no for i in c.instances]
        return d
    if with_sample and c.sample is not None:
        entry = c.sample.entry
        simplified = simplify_stack(entry.stack) if entry.stack else None
        d["sample"] = {
            "line_no": entry.line_no,
            "raw": entry.raw,
            "message": entry.full_message,
            "level": entry.level,
            "module": entry.module,
            "timestamp": _iso_ts(entry.timestamp),
            "stack": entry.stack,
            "stack_simplified": simplified.lines if simplified else [],
            "stack_noise_folded": simplified.noise_count if simplified else 0,
            "context_before": c.sample.before,
            "context_after": c.sample.after,
        }
    # 优化缺陷R58：实例行号索引（JSON 结构化全量携带，便于脚本定位）
    d["instances"] = [
        {"line_no": i.line_no, "last_line_no": i.last_line_no,
         "timestamp": _iso_ts(i.timestamp), "summary": i.summary}
        for i in c.instances
    ]
    return d


def to_json(result: AnalysisResult, top_n: Optional[int] = None,
            full: bool = True) -> str:
    """导出 JSON 格式（结构化，适配脚本二次处理）。

    修复缺陷R60：全部时间戳字段输出 ISO 8601 可读字符串（UTC），
    替代原始 epoch 浮点（人不可读）；None 保持 null。
    修复缺陷R61：full=False 精简输出 —— 簇仅核心字段 + 实例行号
    列表（不含上下文/堆栈/样例原文；261 簇 × 完整上下文 >1MB、
    3 万余行人不可读），GUI 导出默认精简，完整结构为对话框勾选项。
    """
    n = top_n or DEFAULT_TOP_N
    meta = result.stats.as_dict()
    meta["time_start"] = _iso_ts(meta.get("time_start"))
    meta["time_end"] = _iso_ts(meta.get("time_end"))
    payload = {
        "generator": f"log-ai-compressor v{__version__}",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "meta": meta,
        "root_cause_summary": _root_summary(result),
        "error_kinds": len(result.clusters),
        "clusters": [_cluster_dict(c, full=full)
                     for c in result.clusters[:n]],
        "time_series": [[_iso_ts(t), c]
                        for t, c in result.global_hist.series()],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


# ---------------------------------------------------------------------------
# 纯文本导出
# ---------------------------------------------------------------------------
def to_text(result: AnalysisResult, top_n: Optional[int] = None,
            sections=None) -> str:
    """导出纯文本报告（优化缺陷R59：与 Markdown 同内容的无标记版）。

    此前仅为清单级薄报告（无样例/上下文/堆栈，用户对比 MD 后判定
    半成品）；现按板块输出：概览统计 / 错误清单 / 典型样例详情
    （元信息 + 智能分析 + 实例行号索引 + 前上下文 + 样例 + 降噪
    堆栈 + 后上下文），实例索引可独立勾选输出紧凑清单。
    """
    n = top_n or DEFAULT_TOP_N
    clusters = result.clusters[:n]
    s = result.stats
    secs = _sections(sections)
    out: List[str] = []
    out.append("=" * 60)
    out.append(f"日志AI压缩报告：{s.source}")
    out.append("=" * 60)
    out.append(f"生成: log-ai-compressor v{__version__} | 规则 {s.rule_name}")
    out.append(f"{_VERDICT_LABEL.get((result.evidence or {}).get('verdict'), '初步根因')}: {_root_summary(result)}")
    ev = result.evidence or {}
    if ev.get("verdict") != "CONFIRMED":
        for g in (ev.get("gaps") or [])[:3]:
            out.append(f"  ! 缺：{g.get('title')} —— {g.get('missing')}")

    if "overview" in secs:
        out.append("")
        out.append("[概览统计]")
        out.append(f"  总行数: {s.total_lines}  错误行: {s.error_lines}  "
                   f"错误种类: {len(result.clusters)}  "
                   f"错误总次数: {s.error_entries}")
        out.append(f"  编码: {s.encoding}  耗时: {s.duration:.2f}s  "
                   f"速率: {_rate_text(s.lines_per_second)}")
        out.append(f"  时间范围: {_ts_range(result)}")
        level_parts = [f"{k}={v}" for k, v in sorted(s.level_counts.items())]
        out.append(f"  级别分布: {', '.join(level_parts) if level_parts else '-'}")
        if s.truncated:
            out.append(f"  处理状态: 用户取消，已处理前 {s.total_lines} 行")

    if "list" in secs:
        out.append("")
        out.append(f"[错误清单] 共 {len(clusters)} 种（按优先级排序）")
        for i, c in enumerate(clusters, 1):
            root = " [根因]" if c.is_root_cause else ""
            anom = f" [{_anomaly_label(c)}]" if c.anomaly else ""
            out.append(f"  {i:>2}. {c.priority_label} {c.level} ×{c.count}"
                       f"{root}{anom}  {c.summary}")
        if not clusters:
            out.append("  未发现符合条件的错误。")

    if "detail" in secs:
        out.append("")
        out.append("[典型样例详情]（每错误一份，含上下文与降噪堆栈）")
        for i, c in enumerate(clusters, 1):
            out.extend(_cluster_detail_txt(
                i, c, include_instances="instances" in secs))
    elif "instances" in secs:
        # 仅实例索引：紧凑清单（级别 ×次数 + 行号索引）
        out.append("")
        out.append("[实例行号索引]")
        for i, c in enumerate(clusters, 1):
            out.append(f"  [{i}] {c.priority_label} {c.level} ×{c.count}  "
                       f"{c.summary}")
            inst_line = _instances_line(c)
            if inst_line:
                out.append(f"       {inst_line}")
    return "\n".join(out) + "\n"


def _cluster_detail_txt(index: int, c: ErrorCluster,
                        include_instances: bool = False) -> List[str]:
    """单个错误簇的纯文本详情段落（与 _cluster_detail_md 同内容）。"""
    out: List[str] = ["", "-" * 60]
    out.append(f"[{index}] {c.priority_label} {c.level} ×{c.count}  "
               f"{c.summary}")
    meta = [f"出现 {c.count} 次", f"行 {c.first_line}~{c.last_line}"]
    if c.first_seen is not None:
        meta.append(f"首末时间 {format_timestamp(c.first_seen)}"
                    f" ~ {format_timestamp(c.last_seen)}")
    if c.module:
        meta.append(f"模块 {c.module}")
    out.append(f"    {' | '.join(meta)}")
    notes = []
    if c.is_root_cause:
        notes.append(f"根因：{c.root_cause_reason}")
    elif c.root_cause_reason:
        notes.append(c.root_cause_reason)
    if c.anomaly:
        notes.append(f"异常：{_anomaly_label(c)}")
    if notes:
        out.append(f"    智能分析：{'；'.join(notes)}")
    if include_instances:
        inst_line = _instances_line(c)
        if inst_line:
            out.append(f"    {inst_line}")

    sample = c.sample
    if sample is None:
        out.append("    （无典型样例）")
        return out
    entry = sample.entry
    if sample.before:
        out.append("    前上下文:")
        out.extend(f"      {line}" for line in sample.before)
    out.append("    典型样例:")
    out.append(f"      {entry.raw}")
    out.extend(f"      {extra}" for extra in entry.message_extra)
    if entry.stack:
        simplified = simplify_stack(entry.stack)
        out.append(f"    堆栈（已降噪：业务帧 {simplified.business_count} 行，"
                   f"折叠系统/第三方帧 {simplified.noise_count} 行）:")
        out.extend(f"      {line}" for line in simplified.lines)
    if sample.after:
        out.append("    后上下文:")
        out.extend(f"      {line}" for line in sample.after)
    return out


# ---------------------------------------------------------------------------
# HTML 报告（优化缺陷R58：自包含单文件，内联样式，浏览器直接打开）
# ---------------------------------------------------------------------------
_LEVEL_BADGE = {
    "ERROR": "#ef4444", "FAIL": "#f97316", "WARN": "#eab308",
    "INFO": "#3b82f6", "DEBUG": "#6b7280",
}

_HTML_CSS = """
:root{color-scheme:light}
*{box-sizing:border-box}
body{font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif;
margin:0;background:#f1f5f9;color:#1f2937}
.wrap{max-width:1120px;margin:0 auto;padding:24px 20px 60px}
header.band{background:linear-gradient(135deg,#1e3a8a,#3b82f6);color:#fff;
border-radius:14px;padding:22px 26px;box-shadow:0 4px 14px rgba(30,58,138,.25)}
header.band h1{margin:0 0 6px;font-size:22px}
header.band .meta{opacity:.85;font-size:13px}
.root{margin:16px 0;background:#fff7ed;border:1px solid #fdba74;
border-left:6px solid #f97316;border-radius:10px;padding:12px 16px;font-size:14px}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:12px;
padding:16px 20px;margin:16px 0;box-shadow:0 1px 3px rgba(15,23,42,.06)}
h2{font-size:17px;margin:4px 0 12px;color:#1e40af;
border-bottom:2px solid #dbeafe;padding-bottom:6px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border:1px solid #e2e8f0;padding:6px 10px;text-align:left;vertical-align:top}
th{background:#eff6ff;color:#1e3a8a}
tr:nth-child(even) td{background:#f8fafc}
.badge{display:inline-block;color:#fff;border-radius:6px;padding:1px 8px;
font-size:12px;font-weight:600;white-space:nowrap}
.prio{color:#6d28d9;font-weight:600;white-space:nowrap}
pre{background:#0f172a;color:#e2e8f0;border-radius:8px;padding:10px 14px;
overflow-x:auto;font-size:12px;line-height:1.55;
font-family:Consolas,"Courier New",monospace}
pre.ctx{background:#1e293b;color:#94a3b8}
details{margin:6px 0}
summary{cursor:pointer;color:#2563eb;font-weight:600;font-size:13px}
.cmeta{color:#475569;font-size:13px;margin:4px 0}
.notes{color:#7c2d12;font-size:13px}
.insts{font-size:12px;color:#334155;background:#f1f5f9;border-radius:8px;
padding:6px 10px;margin:6px 0;word-break:break-all}
a.anchor{color:#2563eb;text-decoration:none}
a.anchor:hover{text-decoration:underline}
.toc{font-size:13px;line-height:1.9;column-count:2;column-gap:32px}
footer{color:#94a3b8;font-size:12px;text-align:center;margin-top:24px}
h3{font-size:15px;margin:18px 0 6px}
"""


def _badge(level: str) -> str:
    color = _LEVEL_BADGE.get(level, "#6b7280")
    return f'<span class="badge" style="background:{color}">{level}</span>'


def to_html(result: AnalysisResult, top_n: Optional[int] = None,
            title: Optional[str] = None, sections=None) -> str:
    """生成自包含 HTML 结构化报告（优化缺陷R58）。

    单文件内联样式：头部横幅 + 根因横幅 + 概览/清单/详情（锚点目录
    跳转、级别着色徽章、上下文与堆栈 <pre> 横向滚动）；实例行号索
    引随详情板块输出。
    """
    import html as _html

    def esc(t) -> str:
        return _html.escape(str(t), quote=True)

    n = top_n or DEFAULT_TOP_N
    clusters = result.clusters[:n]
    s = result.stats
    secs = _sections(sections)
    title = title or f"日志AI压缩报告：{s.source}"
    level_parts = ", ".join(f"{k}={v}" for k, v in sorted(s.level_counts.items()))

    out: List[str] = []
    out.append("<!DOCTYPE html>")
    out.append('<html lang="zh-CN"><head><meta charset="utf-8">')
    out.append('<meta name="viewport" content="width=device-width,initial-scale=1">')
    out.append(f"<title>{esc(title)}</title>")
    out.append(f"<style>{_HTML_CSS}</style></head><body><div class=\"wrap\">")

    # 头部横幅 + 根因横幅
    out.append('<header class="band">')
    out.append(f"<h1>{esc(title)}</h1>")
    out.append(f'<div class="meta">log-ai-compressor v{__version__} | '
               f"处理 {s.total_lines} 行 | 耗时 {s.duration:.2f}s | "
               f"{esc(_rate_text(s.lines_per_second))} | 规则 {esc(s.rule_name)}"
               f" | 时间范围 {esc(_ts_range(result))}</div>")
    out.append("</header>")
    ev = result.evidence or {}
    out.append(f'<div class="root"><b>'
               f'{esc(_VERDICT_LABEL.get(ev.get("verdict"), "初步定位根因"))}：</b>'
               f"{esc(_root_summary(result))}</div>")
    if ev.get("verdict") != "CONFIRMED" and ev.get("gaps"):
        out.append('<div class="warn">')
        out.append("<b>⚠ 证据不足，这份日志无法证明因果方向</b>")
        out.append("<ul>")
        for g in ev["gaps"][:5]:
            out.append(f'<li><b>{esc(g.get("title"))}</b> —— 缺：{esc(g.get("missing"))}</li>')
        out.append("</ul></div>")

    # 目录锚点
    if "detail" in secs and clusters:
        out.append('<div class="card"><h2>目录</h2><div class="toc">')
        for i, c in enumerate(clusters, 1):
            out.append(f'<div><a class="anchor" href="#c{i}">{i}. '
                       f"{esc(c.priority_label)} {_badge(c.level)} "
                       f"×{c.count} {esc(c.summary[:60])}</a></div>")
        out.append("</div></div>")

    # 一、概览统计
    if "overview" in secs:
        out.append('<div class="card"><h2>一、概览统计</h2><table>')
        out.append("<tr><th>指标</th><th>数值</th></tr>")
        rows = (("日志来源", s.source), ("编码", s.encoding),
                ("总行数", s.total_lines),
                ("错误行数（FATAL/ERROR/FAIL）", s.error_lines),
                ("错误种类数（去重后）", len(result.clusters)),
                ("错误总次数（过滤后）", s.error_entries),
                ("日志时间范围", _ts_range(result)),
                ("级别分布", level_parts or "-"))
        for k, v in rows:
            out.append(f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>")
        if s.truncated:
            out.append(f"<tr><td>处理状态</td><td>用户取消，已处理前 "
                       f"{s.total_lines} 行</td></tr>")
        out.append("</table></div>")

    # 二、错误清单
    if "list" in secs:
        out.append(f'<div class="card"><h2>二、Top {len(clusters)} '
                   "错误清单（按优先级排序）</h2>")
        if not clusters:
            out.append("<p>未发现符合条件的错误。</p>")
        else:
            out.append("<table><tr><th>#</th><th>优先级</th><th>级别</th>"
                       "<th>次数</th><th>模块</th><th>根因</th><th>异常</th>"
                       "<th>错误摘要</th></tr>")
            for i, c in enumerate(clusters, 1):
                root = (f"✔ {esc(c.root_cause_reason[:16])}"
                        if c.is_root_cause else "—")
                out.append(
                    f'<tr><td>{i}</td><td class="prio">{esc(c.priority_label)}'
                    f"</td><td>{_badge(c.level)}</td><td>{c.count}</td>"
                    f"<td>{esc(c.module) or '-'}</td><td>{root}</td>"
                    f"<td>{esc(_anomaly_label(c)) or '—'}</td>"
                    f"<td>{esc(c.summary)}</td></tr>")
            out.append("</table>")
        out.append("</div>")

    # 三、典型样例详情
    if "detail" in secs:
        for i, c in enumerate(clusters, 1):
            out.append(f'<div class="card" id="c{i}">')
            out.append(f"<h3>{i}. [{esc(c.priority_label)}] {_badge(c.level)} "
                       f"{esc(c.summary)}</h3>")
            meta = [f"出现 {c.count} 次", f"行 {c.first_line}~{c.last_line}"]
            if c.first_seen is not None:
                meta.append(f"首末时间 {format_timestamp(c.first_seen)}"
                            f" ~ {format_timestamp(c.last_seen)}")
            if c.module:
                meta.append(f"模块 {c.module}")
            out.append(f'<p class="cmeta">{esc(" | ".join(meta))}</p>')
            notes = []
            if c.is_root_cause:
                notes.append(f"根因：{c.root_cause_reason}")
            elif c.root_cause_reason:
                notes.append(c.root_cause_reason)
            if c.anomaly:
                notes.append(f"异常：{_anomaly_label(c)}")
            if notes:
                out.append(f'<p class="notes"><b>智能分析：</b>'
                           f"{esc('；'.join(notes))}</p>")
            if "instances" in secs and c.instances:
                refs = " ".join(f"L{inst.line_no}" for inst in c.instances)
                out.append(f'<div class="insts"><b>实例行号'
                           f"（{len(c.instances)}）：</b>{esc(refs)}</div>")
            sample = c.sample
            if sample is None:
                out.append("<p>（无典型样例）</p>")
            else:
                entry = sample.entry
                if sample.before:
                    out.append("<details><summary>前上下文"
                               f"（{len(sample.before)} 行）</summary>"
                               f'<pre class="ctx">{esc(chr(10).join(sample.before))}'
                               "</pre></details>")
                body = entry.raw + (
                    "\n" + "\n".join(entry.message_extra)
                    if entry.message_extra else "")
                out.append(f"<p><b>典型样例</b></p><pre>{esc(body)}</pre>")
                if entry.stack:
                    simplified = simplify_stack(entry.stack)
                    out.append(f"<p><b>堆栈（已降噪：业务帧 "
                               f"{simplified.business_count} 行，折叠系统/"
                               f"第三方帧 {simplified.noise_count} 行）</b></p>"
                               f"<pre>{esc(chr(10).join(simplified.lines))}</pre>")
                if sample.after:
                    out.append("<details><summary>后上下文"
                               f"（{len(sample.after)} 行）</summary>"
                               f'<pre class="ctx">{esc(chr(10).join(sample.after))}'
                               "</pre></details>")
            out.append("</div>")

    out.append(f"<footer>由 log-ai-compressor v{__version__} 生成 · "
               f"{esc(datetime.now().isoformat(timespec='seconds'))}</footer>")
    out.append("</div></body></html>")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# 对比报告
# ---------------------------------------------------------------------------
def compare_to_markdown(results: List[CompareResult]) -> str:
    """多文件对比差异报告（Markdown）。"""
    out: List[str] = []
    out.append("# 日志对比分析报告")
    out.append("")
    for cmp in results:
        out.append(f"## 基准 `{cmp.base_name}` vs `{cmp.other_name}`")
        out.append("")
        out.append(f"- 新增错误：**{len(cmp.new_items)}** 种"
                   f"（共 {sum(i.count_b for i in cmp.new_items)} 次）")
        out.append(f"- 消失错误：**{len(cmp.gone_items)}** 种"
                   f"（基准中共 {sum(i.count_a for i in cmp.gone_items)} 次）")
        out.append(f"- 共同错误：**{len(cmp.common_items)}** 种"
                   f"（共 {cmp.total_common} 次）")
        out.append("")

        for name, items, count_col in (("新增错误（基准中不存在）", cmp.new_items, "count_b"),
                                        ("消失错误（对比文件中已不存在）", cmp.gone_items, "count_a")):
            if items:
                out.append(f"### {name}")
                out.append("")
                out.append("| 级别 | 次数 | 模块 | 错误摘要 |")
                out.append("| --- | --- | --- | --- |")
                for i in items:
                    count = i.count_b if count_col == "count_b" else i.count_a
                    out.append(f"| {i.level} | {count} | "
                               f"{_md_escape(i.module) or '-'} | "
                               f"{_md_escape(i.summary)} |")
                out.append("")

        if cmp.common_items:
            out.append("### 共同错误（数量变化）")
            out.append("")
            out.append("| 级别 | 基准次数 | 对比次数 | 变化率 | 模块 | 错误摘要 |")
            out.append("| --- | --- | --- | --- | --- | --- |")
            for i in cmp.common_items:
                out.append(f"| {i.level} | {i.count_a} | {i.count_b} | "
                           f"{i.change_text} | {_md_escape(i.module) or '-'} | "
                           f"{_md_escape(i.summary)} |")
            out.append("")
    return "\n".join(out) + "\n"
