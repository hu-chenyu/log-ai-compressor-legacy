# -*- coding: utf-8 -*-
"""核心数据模型：日志条目、错误簇、自适应时间直方图、运行统计与结果。

设计说明
--------
- 数据模型与处理逻辑分离：parser 产出 LogEntry，clustering 聚合为 ErrorCluster，
  analysis 填充智能分析字段，export/GUI 只读模型渲染，层间单向依赖；
- TimeHistogram 是内存控制的核心：桶数上限固定、超出自动扩宽桶宽合并旧桶，
  内存占用 O(max_buckets)，与日志总行数无关。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from log_ai_compressor.constants import (
    GLOBAL_HIST_MAX_BUCKETS,
    HIST_MAX_BUCKETS,
    LEVEL_WEIGHT,
)


def format_timestamp(t: Optional[float]) -> str:
    """时间戳展示格式化：epoch 秒 -> 日期时间；相对秒 -> 秒计数。"""
    if t is None:
        return "-"
    if t < 1e9:                      # 相对计时（嵌入式秒计数等）
        return f"{t:.3f}s"
    from datetime import datetime
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 自适应时间直方图
# ---------------------------------------------------------------------------
class TimeHistogram:
    """时间分布直方图（自适应分桶，内存 O(max_buckets)）。

    - 首个样本确定起点，桶宽从 1 秒起步；
    - 桶数超过上限时桶宽 ×8 并合并旧桶（向前兼容历史数据）；
    - 时间回跳（无序日志）时向前平移起点。
    """

    __slots__ = ("_max", "_width", "_start", "_counts")

    def __init__(self, max_buckets: int = HIST_MAX_BUCKETS,
                 initial_width: float = 1.0):
        self._max = max_buckets
        self._width = initial_width
        self._start: Optional[float] = None
        self._counts: Dict[int, int] = {}

    # -- 写入 ------------------------------------------------------------
    def add(self, t: float) -> None:
        """记录一个时间点。"""
        if self._start is None:
            self._start = t
            self._counts = {0: 1}
            return
        if t < self._start:
            # 时间回跳：向前平移起点并整体重定位旧桶
            shift = int((self._start - t) / self._width) + 1
            self._start -= shift * self._width
            self._counts = {k + shift: v for k, v in self._counts.items()}
        idx = int((t - self._start) // self._width)
        while idx >= self._max:
            self._widen()
            idx = int((t - self._start) // self._width)
        self._counts[idx] = self._counts.get(idx, 0) + 1

    def _widen(self) -> None:
        """桶宽 ×8，旧桶按 8:1 合并。"""
        merged: Dict[int, int] = {}
        for idx, cnt in self._counts.items():
            merged[idx // 8] = merged.get(idx // 8, 0) + cnt
        self._counts = merged
        self._width *= 8

    # -- 读取 ------------------------------------------------------------
    @property
    def width(self) -> float:
        """当前桶宽（秒）。"""
        return self._width

    @property
    def total(self) -> int:
        """样本总数。"""
        return sum(self._counts.values())

    @property
    def start(self) -> Optional[float]:
        return self._start

    def series(self) -> List[Tuple[float, int]]:
        """返回 (桶起始时间, 计数) 升序序列，用于趋势图。"""
        if self._start is None:
            return []
        return [
            (self._start + i * self._width, c)
            for i, c in sorted(self._counts.items())
        ]

    def burst_buckets(self, k: float = 3.0) -> List[Tuple[float, int]]:
        """识别集中爆发桶。

        稳健统计：中位数 + k×MAD×1.4826（抗尖峰膨胀：普通 mean+σ 会被
        尖峰自身推高阈值导致漏检）；同时要求至少 2 倍于基线中位数。
        """
        import statistics

        if not self._counts or self._start is None:
            return []
        items = sorted(self._counts.items())
        counts = [c for _, c in items]
        if len(counts) < 2:
            return []
        median = statistics.median(counts)
        mad = statistics.median(abs(c - median) for c in counts)
        robust_threshold = median + k * 1.4826 * mad
        threshold = max(robust_threshold, 2.0 * median)
        return [
            (self._start + i * self._width, c)
            for i, c in items
            if c > threshold
        ]


# ---------------------------------------------------------------------------
# 日志条目
# ---------------------------------------------------------------------------
@dataclass
class LogEntry:
    """单条解析完成的日志条目（含消息折行与堆栈聚合）。"""

    line_no: int                        # 起始行号（1-based）
    raw: str = ""                       # 首行原始文本
    timestamp: Optional[float] = None   # epoch 秒或相对秒
    level: str = "INFO"                 # 规范化级别
    module: str = ""                    # 模块名（可为空）
    message: str = ""                   # 日志内容首行
    message_extra: List[str] = field(default_factory=list)  # 折行续接内容
    stack: List[str] = field(default_factory=list)          # 堆栈/回溯行
    last_line_no: int = 0               # 条目结束行号（含堆栈与续行）

    @property
    def has_stack(self) -> bool:
        return bool(self.stack)

    @property
    def full_message(self) -> str:
        """含折行的完整消息。"""
        return " ".join([self.message] + self.message_extra).strip()


# ---------------------------------------------------------------------------
# 错误簇（聚类结果）
# ---------------------------------------------------------------------------
@dataclass
class ClusterSample:
    """错误簇的典型样例：完整条目 + 前后上下文原始行。"""
    entry: LogEntry
    before: List[str] = field(default_factory=list)   # 前上下文（原始行）
    after: List[str] = field(default_factory=list)    # 后上下文（原始行）


@dataclass
class ClusterInstance:
    """簇内单个错误实例（修复缺陷R4：全屏簇展开查看全部实例）。

    内存有界设计：
    - 前 MAX_CLUSTER_INSTANCES_DETAILED 个实例保留完整条目与
      前后上下文（可查看原始日志、堆栈与上下文）；
    - 后续实例仅记录时间戳/行号/摘要（元数据）；
    - 超出全局上限后不再记录（instances_truncated 标记）。
    """
    timestamp: Optional[float] = None
    line_no: int = 0
    last_line_no: int = 0
    summary: str = ""                        # 展示用摘要（截断 160）
    entry: Optional[LogEntry] = None         # 完整条目（详情用，可为 None）
    before: List[str] = field(default_factory=list)   # 前上下文（详情用）
    # 修复缺陷R44：实例后上下文（此前仅典型样例收集，点击实例
    # 时详情面板无后上下文可显示）
    after: List[str] = field(default_factory=list)    # 后上下文（详情用）


@dataclass
class ErrorCluster:
    """同类错误簇：模糊指纹聚合后的错误类别。"""

    cluster_id: int
    template: str                        # 归一化完整指纹模板（级别+消息+堆栈特征）
    message_template: str = ""           # 消息指纹模板（跨堆栈差异的同类合并键）
    level: str = "ERROR"
    module: str = ""
    summary: str = ""                    # 首条消息（展示用）
    count: int = 0
    first_line: int = 0
    last_line: int = 0
    first_seen: Optional[float] = None
    last_seen: Optional[float] = None
    sample: Optional[ClusterSample] = None
    hist: TimeHistogram = field(default_factory=TimeHistogram)
    # 修复缺陷R4：全部实例记录（展开查看；count 与 len(instances)
    # 在超出保留上限时不一致，以 count 为准）
    instances: List[ClusterInstance] = field(default_factory=list)
    instances_truncated: bool = False    # 实例记录超出上限（未全量保留）

    # ---- 智能分析结果（analysis 模块填充）----
    is_root_cause: bool = False
    root_cause_reason: str = ""
    # 置信档位（v2 新增）：把"是/不是根因"的二元判断升级为三档，
    # 让工具在证据不足时**敢于不下结论** —— 见 analysis.assess_evidence。
    #   CONFIRMED   确定性证据链直连（Caused-by 链 / 强关键词 ≥3）
    #   LIKELY      有指向性但非直连（时间连锁首发 / 关键词 1~2）
    #   INSUFFICIENT 只有时间共现或频率，没有因果链 —— 不应作为根因输出
    root_cause_confidence: str = ""
    anomaly: str = ""                    # 'burst'/'periodic'/'novel'/'rare'/''
    priority: float = 0.0
    # 优化缺陷R77：优先级评分构成（详情面板判断依据，
    # 形如 "级别35+频次25+根因20（ERROR 档保底 80）"）
    priority_detail: str = ""
    # 优化缺陷R78：相似簇关联（模板 Jaccard ≥0.8 的簇 id 列表）
    related_clusters: List[int] = field(default_factory=list)
    # 优化缺陷R78：根因时间线叙事（因果图传播链文本，仅根因簇有值）
    root_timeline: str = ""

    @property
    def level_weight(self) -> float:
        return LEVEL_WEIGHT.get(self.level, 0.0)

    @property
    def priority_label(self) -> str:
        """优先级档位标签（P0 错误 / P1 失败 / P2 警告 / P3 信息 /
        P4 调试；修复缺陷R40：FATAL 删除后五档重划）。"""
        if self.priority >= 75:
            return "P0"
        if self.priority >= 55:
            return "P1"
        if self.priority >= 35:
            return "P2"
        if self.priority >= 15:
            return "P3"
        return "P4"


# ---------------------------------------------------------------------------
# 运行统计与分析结果
# ---------------------------------------------------------------------------
@dataclass
class RunStats:
    """一次管线运行的元统计。"""
    source: str = ""                     # 文件路径或 '<粘贴文本>'
    encoding: str = "utf-8"
    rule_name: str = "generic"
    total_lines: int = 0                 # 总行数
    # 优化缺陷R99：原始摄入字符数（Token 估算 + 压缩率展示的原始侧
    # 分母；流式逐行累计，不额外占内存）
    raw_chars: int = 0
    entry_lines: int = 0                 # 结构化条目数
    error_lines: int = 0                 # 错误级行数（FATAL/ERROR/FAIL）
    error_entries: int = 0               # 通过过滤并参与聚类的错误条目数
    # 优化缺陷R71：带时间戳的条目数（规则体检的时间戳识别率分子）
    ts_entries: int = 0
    level_counts: Dict[str, int] = field(default_factory=dict)
    duration: float = 0.0                # 处理耗时（秒）
    lines_per_second: float = 0.0
    time_start: Optional[float] = None   # 日志时间范围
    time_end: Optional[float] = None
    truncated: bool = False              # 是否因取消中断
    # 优化缺陷R85：行数上限采样命中标记（与取消区分：非中断而是
    # 达上限收束 —— 状态栏显示「已达行数上限」而非「已取消」）
    limit_hit: bool = False
    analysis_cost: float = 0.0           # 智能分析耗时（秒，用于开销核算）

    def as_dict(self) -> dict:
        """导出 JSON 友好的字典。"""
        return {
            "source": self.source, "encoding": self.encoding,
            "rule": self.rule_name, "total_lines": self.total_lines,
            "raw_chars": self.raw_chars,
            "entry_lines": self.entry_lines, "error_lines": self.error_lines,
            "error_entries": self.error_entries,
            "level_counts": dict(self.level_counts),
            "duration_sec": round(self.duration, 3),
            "lines_per_second": round(self.lines_per_second, 1),
            "time_start": self.time_start, "time_end": self.time_end,
            "truncated": self.truncated,
        }


@dataclass
class AnalysisResult:
    """管线完整输出：错误簇（按优先级降序）+ 运行统计 + 全局错误直方图。"""

    stats: RunStats
    clusters: List[ErrorCluster] = field(default_factory=list)
    global_hist: TimeHistogram = field(
        default_factory=lambda: TimeHistogram(max_buckets=GLOBAL_HIST_MAX_BUCKETS)
    )
    keywords: List[str] = field(default_factory=list)   # 高亮关键字
    # v2：证据充分性评估（analysis.assess_evidence 填充）。
    # 存 dict 而非 dataclass，避免 models ↔ analysis 循环 import；
    # 结构见 analysis.EvidenceAssessment.to_dict。
    evidence: Optional[Dict[str, Any]] = None

    @property
    def top_clusters(self) -> List[ErrorCluster]:
        """默认 Top N（DEFAULT_TOP_N）簇。"""
        from log_ai_compressor.constants import DEFAULT_TOP_N
        return self.clusters[:DEFAULT_TOP_N]

    def clusters_of_module(self, module: str) -> List[ErrorCluster]:
        return [c for c in self.clusters if c.module == module]

    def clusters_of_level(self, level: str) -> List[ErrorCluster]:
        return [c for c in self.clusters if c.level == level]
