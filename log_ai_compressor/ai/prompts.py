# -*- coding: utf-8 -*-
"""AI 提示词构造。

为什么要"先压缩再提问"
---------------------
本工具的价值就在这里：把百万行日志压成几百上千 token 的证据摘要，
再交给模型。直接把原始日志丢给模型既超上下文窗口又烧钱。

提示词里显式写死了「只依据给定证据、不得编造」等约束 —— 日志分析场景
里，模型编造一个不存在的根因比不回答的危害大得多。
"""
from __future__ import annotations

from typing import Any, Dict, List

SYSTEM_ANALYST = """你是一名资深 SRE / 故障排查专家，负责根据日志证据定位根因。

硬性要求：
1. 只依据我提供的日志证据作答。证据里没有的根因、文件、行号、时间，一律不要编造。
2. 不确定的地方要明说「证据不足」，并指出还需要什么日志或指标才能判断。
3. 用中文回答，面向工程师，不要写免责套话。
4. 不要复述我已经给过你的统计数字，只讲你的判断和推理。
5. 涉及"先查什么"时，给出具体可执行的排查动作（看哪个服务、开哪个指标、grep 什么关键字），不要写"建议进一步排查"这种废话。"""

_MAX_BUCKETS = 60


def _fmt_time_hist(hist: Dict[str, Any], limit: int = 24) -> str:
    """把时间直方图压成一行文本趋势（供模型判断爆发点）。

    service.result_to_dict 已把直方图转成 [{"t": epoch, "n": 次数}, ...]，
    这里只取 n 画柱状字形，够模型看形态即可。
    """
    series = (hist or {}).get("series") or []
    counts = [int(b.get("n", 0)) for b in series if isinstance(b, dict)]
    if not counts:
        return "（无时间分布）"
    if len(counts) > limit:
        step = len(counts) / float(limit)
        counts = [counts[int(i * step)] for i in range(limit)]
    peak = max(counts) or 1
    bars = "".join("▁▂▃▄▅▆▇█"[min(7, int(v / peak * 7.99))] for v in counts)
    return f"峰值 {peak}，形态 {bars}"


def _fmt_level_counts(counts: Dict[str, int]) -> str:
    if not counts:
        return "（无）"
    return " / ".join(f"{k}:{v:,}" for k, v in
                      sorted(counts.items(), key=lambda kv: -kv[1]))


def _cluster_block(c: Dict[str, Any], idx: int) -> str:
    """单个错误簇的紧凑表示。"""
    tags: List[str] = []
    if c.get("is_root_cause"):
        tags.append("根因")
    if c.get("anomaly"):
        tags.append({"burst": "集中爆发", "periodic": "周期发作",
                     "novel": "新型错误", "rare": "罕见异常"}.get(
                         c["anomaly"], c["anomaly"]))
    if c.get("related_clusters"):
        tags.append(f"相关簇:{c['related_clusters']}")
    lines = [
        f"[{idx}] {c.get('level')} ×{c.get('count')} "
        f"模块={c.get('module') or '-'} "
        f"优先级={c.get('priority')} 行范围={c.get('first_line')}~{c.get('last_line')}"
        + (f" 标记=[{'/'.join(tags)}]" if tags else ""),
        f"    摘要：{c.get('summary')}",
    ]
    if c.get("root_cause_reason"):
        lines.append(f"    根因依据：{c['root_cause_reason']}")
    if c.get("priority_detail"):
        lines.append(f"    评分构成：{c['priority_detail']}")
    if c.get("time_text"):
        lines.append(f"    时间：{c['time_text']}")
    return "\n".join(lines)


def build_explain_prompt(payload: Dict[str, Any], question: str = "") -> str:
    """整份分析结果的解读提示词。"""
    stats = payload.get("stats") or {}
    clusters = payload.get("clusters") or []

    parts: List[str] = [
        "## 日志概况",
        f"- 来源：{stats.get('source')}",
        f"- 编码：{stats.get('encoding')}，解析规则：{stats.get('rule_name')}",
        f"- 规模：{stats.get('total_lines', 0):,} 行 / "
        f"{stats.get('raw_chars', 0):,} 字符",
        f"- 错误：{stats.get('error_lines', 0):,} 行，"
        f"去重后 {payload.get('total_clusters', len(clusters))} 种"
        + (f"（已截断为 Top {payload.get('shown_clusters')}）"
           if payload.get("top_n_truncated") else ""),
        f"- 级别分布：{_fmt_level_counts(stats.get('level_counts') or {})}",
        f"- 时间跨度：{stats.get('time_range_text') or '（日志无时间戳）'}",
        f"- 错误时间形态：{_fmt_time_hist(payload.get('global_hist') or {})}",
    ]

    roots = payload.get("root_causes") or []
    if roots:
        parts.append("\n## 本地算法已判定的根因候选")
        for r in roots:
            parts.append(f"- [{r['id']}] {r['summary']}\n  依据：{r.get('reason')}")

    if clusters:
        parts.append("\n## 错误簇明细（按优先级排序）")
        for i, c in enumerate(clusters, 1):
            parts.append(_cluster_block(c, i))

    cooc = payload.get("cooccurring") or []
    if cooc:
        parts.append("\n## 共现簇（与首个错误同窗反复出现，常见于同一根因）")
        for o in cooc:
            parts.append(f"- [{o['id']}] ×{o['hits']} {o['summary']}")

    parts.append("""
## 请你输出
1. **一句话结论**：这次故障到底是什么。
2. **根因链**：按时间先后说明「起因 → 传导 → 表现」，指明每一步的证据簇编号。
3. **先查哪里**：3~5 条按优先级排序的具体排查动作，每条都要能直接执行。
4. **证据不足的部分**：明确列出还需要哪些日志/指标才能定论。""")

    if question.strip():
        parts.append(f"\n## 用户额外追问\n{question.strip()}")
    return "\n".join(parts)


def build_cluster_prompt(ctx: Dict[str, Any], question: str = "") -> str:
    """单个错误簇的深度解读提示词（含样例与降噪堆栈）。"""
    c = ctx.get("cluster") or {}
    sample = c.get("sample") or {}
    entry = sample.get("entry") or {}
    stats = ctx.get("stats") or {}

    parts: List[str] = [
        "## 全局背景",
        f"- 来源 {stats.get('source')}，{stats.get('total_lines', 0):,} 行，"
        f"错误 {stats.get('error_lines', 0):,} 行，编码 {stats.get('encoding')}",
        f"- 级别分布：{_fmt_level_counts(stats.get('level_counts') or {})}",
        "\n## 待分析错误簇",
        f"- 簇号：[{c.get('id')}] 级别 {c.get('level')} 模块 "
        f"{c.get('module') or '-'}",
        f"- 出现次数：{c.get('count')}，行范围 {c.get('first_line')}~{c.get('last_line')}",
        f"- 时间：{c.get('first_seen_text') or '-'} ~ "
        f"{c.get('last_seen_text') or '-'}",
        f"- 优先级：{c.get('priority')}（{c.get('priority_detail')}）",
        f"- 摘要：{c.get('summary')}",
    ]
    if c.get("is_root_cause"):
        parts.append(f"- 根因判定：是（{c.get('root_cause_reason')}）")
    if c.get("anomaly"):
        parts.append(f"- 异常标记：{c.get('anomaly')}")

    if entry:
        parts.append("\n## 典型样例原始行")
        parts.append(f"行号 {entry.get('line_no')}~{entry.get('last_line_no')}：")
        parts.append("```")
        parts.append(entry.get("raw", ""))
        for extra in entry.get("message_extra") or []:
            parts.append(extra)
        parts.append("```")

    ss = sample.get("stack_simplified")
    if ss and ss.get("lines"):
        parts.append(
            f"\n## 降噪堆栈（业务帧 {ss.get('business_count')} 行，"
            f"已折叠噪声帧 {ss.get('noise_count')} 行）")
        parts.append("```")
        parts.extend(ss["lines"])
        parts.append("```")

    if sample.get("before"):
        parts.append(f"\n## 前置上下文（{len(sample['before'])} 行）")
        parts.append("```")
        parts.extend(sample["before"][-15:])
        parts.append("```")
    if sample.get("after"):
        parts.append(f"\n## 后置上下文（{len(sample['after'])} 行）")
        parts.append("```")
        parts.extend(sample["after"][:15])
        parts.append("```")

    variables = c.get("variables") or []
    if variables:
        parts.append("\n## 变量取值分布（同类错误的差异来源）")
        for var in variables:
            values = "、".join(f"{v}×{n}" for v, n in var.get("values") or [])
            parts.append(f"- {var.get('name')}：{values}")

    cooc = ctx.get("cooccurring") or []
    if cooc:
        parts.append("\n## 共现错误簇")
        for o in cooc:
            parts.append(f"- [{o['id']}] ×{o['hits']} {o['summary']}")

    parts.append("""
## 请你输出
1. **这个错误在说什么**（把技术黑话翻译成人话）。
2. **最可能的根因**，按可能性排序，每条注明依据。
3. **具体怎么修**（改配置 / 改代码 / 改容量，给出可操作建议）。
4. **如何确认修好了**（看什么指标回升、什么日志不再出现）。""")

    if question.strip():
        parts.append(f"\n## 用户额外追问\n{question.strip()}")
    return "\n".join(parts)


def build_raw_chat_prompt(summary_text: str, question: str) -> str:
    """兜底：用户直接贴报告文本时的通用问答。"""
    return (
        "以下是一份日志分析报告（已压缩）。请基于它回答问题，"
        "证据不足时明说，不要编造。\n\n"
        f"```\n{summary_text}\n```\n\n问题：{question}"
    )
