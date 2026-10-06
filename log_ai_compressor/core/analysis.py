# -*- coding: utf-8 -*-
"""智能辅助分析：错误因果关联、统计异常检测、优先级排序、堆栈精简降噪。

算法设计
--------
1. **根因判定（因果图）**——保守强证据建边 + 加权评分：
   a. Caused-by 链：带 "Caused by:" 堆栈的错误指向其紧邻的前置错误；
   b. 消息互引用：模板词高包含且含稀有词的后发错误（优化缺陷R76）；
   c. 时间连锁：突发时间窗口内最先出现且含根因特征关键词的错误；
   图源头（出度>0 入度=0）判根因；关键词按 IDF 加权（罕见词证据
   权重大，优化缺陷R76）；入度>0 或含被动失败关键词的簇标记为
   疑似连锁衍生。
2. **统计异常检测（优化缺陷R75 强化）**：
   - 集中爆发（burst）双通道：全局错误直方图中超过 均值+3σ 的桶，
     簇峰值时间落入爆发区间；或簇自持基线（与自身中位数+MAD 比）
     峰值超限 —— 全局平稳但单簇陡增亦可抓；
   - 周期发作（periodic）：实例间隔变异系数 ≤0.10（定时任务指纹）；
   - 新型错误（novel）：罕见且与既有簇模板 Jaccard <0.5；
   - 罕见异常（rare）：总数可观而仅出现 1 次的老错误变体。
3. **优先级综合评分**：级别权重 40% + 频次（对数归一）30% +
   根因 20% + 异常 10%；五级别分档钳制（ERROR 保 P0 / FAIL 钳
   P1 / WARN 钳 P2 / INFO 钳 P3 / DEBUG 封顶 P4，修复缺陷R40）。
4. **堆栈降噪**：折叠系统库/第三方框架帧，保留业务栈帧与
   关键因果行（Caused by / Traceback / 异常摘要）。
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from log_ai_compressor.constants import (
    CASCADE_KEYWORDS,
    LEVEL_WEIGHT,
    ROOT_CAUSE_KEYWORDS,
    is_noise_stack_frame,
)
from log_ai_compressor.core.models import (
    AnalysisResult,
    ErrorCluster,
    RunStats,
    format_timestamp,
)

# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------
BURST_WINDOW_SEC = 60.0        # 时间连锁判定窗口（秒）
BURST_SIGMA = 3.0              # 集中爆发判定阈值（均值 + N 倍标准差）
RARE_MIN_TOTAL = 10            # 触发罕见异常判定的最小错误总量
STRONG_KEYWORD_SCORE = 3       # 强根因关键词命中数阈值

# ---------------------------------------------------------------------------
# 根因置信档位（v2）
# ---------------------------------------------------------------------------
# 为什么要有第三档 INSUFFICIENT
# --------------------------
# 只有 Caused-by 栈是解析器给出的**确定性因果方向**；关键词投票、时间
# 连锁首发、消息互引用全是统计线索，只能说"指向这里"，不能说"就是这里"。
# v1 把这些一视同仁地标成根因，于是会出现：连接池刷了 370 次 → 报告说
# "根因：连接池耗尽"，而真正的因（连接建立失败）只出现 10 次、被当成
# 噪音。**一个自信的错误根因，比没有根因更糟** —— 值班的人会照着它去查。
#
# 所以这里把"证据强度"显式化，拿不出因果链就明说证据不足，并列出还缺什么。
CONF_CONFIRMED = "CONFIRMED"          # Caused-by 因果链直连
CONF_LIKELY = "LIKELY"                # 有指向性但非直连（统计线索）
CONF_INSUFFICIENT = "INSUFFICIENT"    # 只有共现/频率，不足以定因

# 优化缺陷R75：异常检测强化参数（自持基线爆发 / 周期发作 / 新型错误）
OWN_BASELINE_MIN_BUCKETS = 3   # 自持基线：簇内直方图最少桶数
OWN_BASELINE_MIN_PEAK = 5      # 自持基线：峰值桶最小计数（防小样本虚报）
PERIODIC_MIN_SAMPLES = 4       # 周期发作：最少时间戳样本数
PERIODIC_MAX_CV = 0.10         # 周期发作：间隔变异系数上限（越小越规律）
NOVEL_JACCARD_MAX = 0.5        # 新型错误：与既有簇模板相似度上限
# 优化缺陷R102：错误共现分析（时间窗/行距内两簇实例反复同现 → 同根因）
COOC_WINDOW_SEC = 60.0         # 共现判定时间窗（±秒）
COOC_WINDOW_LINES = 100        # 无时间戳时共现判定行距（±行）
COOC_MIN_HITS = 2              # 至少同现次数（1 次可能是巧合）

# 关键因果行（降噪时永不折叠）
_CAUSED_BY_RE = re.compile(r"^\s*Caused by\s*[:：]", re.IGNORECASE)
_TRACEBACK_RE = re.compile(r"^Traceback \(|Backtrace:", re.IGNORECASE)
_EXCEPTION_RE = re.compile(r"^[A-Za-z_][\w.$]*(?:Exception|Error|Fault|Interrupt)\s*[:({]")
_RAISE_RE = re.compile(r"^\s*raise\s+\w")


def _is_key_frame(line: str) -> bool:
    """关键因果行：Caused by / Traceback 头 / 异常摘要 / raise，永不折叠。"""
    return bool(
        _CAUSED_BY_RE.match(line) or _TRACEBACK_RE.match(line)
        or _EXCEPTION_RE.match(line) or _RAISE_RE.match(line)
    )


# ---------------------------------------------------------------------------
# 堆栈精简降噪
# ---------------------------------------------------------------------------
@dataclass
class SimplifiedStack:
    """降噪后的堆栈：展示行（含折叠注释）+ 业务/噪声帧计数。"""
    lines: List[str] = field(default_factory=list)
    business_count: int = 0
    noise_count: int = 0

    @property
    def has_business_frames(self) -> bool:
        return self.business_count > 0


def simplify_stack(stack: Sequence[str]) -> SimplifiedStack:
    """堆栈降噪：系统库/第三方框架帧折叠为注释，业务帧与关键因果行保留。"""
    out: List[str] = []
    noise_total = 0
    run_noise = 0

    def flush_noise() -> None:
        nonlocal run_noise
        if run_noise:
            out.append(f"    ...... 已折叠 {run_noise} 行系统库/第三方栈帧 ......")
            run_noise = 0

    for line in stack:
        if not _is_key_frame(line) and is_noise_stack_frame(line):
            noise_total += 1
            run_noise += 1
        else:
            flush_noise()
            out.append(line)
    flush_noise()
    return SimplifiedStack(out, len(stack) - noise_total, noise_total)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def analyze_clusters(result: AnalysisResult, *,
                     burst_sigma: float = BURST_SIGMA,
                     rare_min_total: int = RARE_MIN_TOTAL,
                     strong_keyword_score: float = STRONG_KEYWORD_SCORE
                     ) -> float:
    """对管线结果执行智能分析（就地填充字段并排序），返回耗时（秒）。

    优化缺陷R79：灵敏度参数化（智能分析模式）——完整分析用默认
    阈值；深度扫描经 ANALYSIS_MODE_DEEP 降阈（宁多报不漏报）。
    """
    t0 = time.perf_counter()
    clusters = result.clusters
    if clusters:
        _mark_anomalies(clusters, result.global_hist, result.stats,
                        burst_sigma=burst_sigma,
                        rare_min_total=rare_min_total)
        out_edges, by_id = _mark_root_causes(
            clusters, strong_keyword_score=strong_keyword_score)
        # 优化缺陷R78：关联叙事（依赖因果图与异常标注，须在优先级前）
        _link_related_clusters(clusters)
        _build_timelines(clusters, out_edges, by_id)
        _compute_priorities(clusters, result.stats)
        _sort_clusters(clusters)
        # v2：证据充分性评估。必须放在 _mark_root_causes 之后（它读
        # root_cause_confidence）、_compute_priorities 之后（它按
        # priority 排候选），否则拿到的是未排序或未标档的中间态。
        result.evidence = assess_evidence(clusters, result.stats).to_dict()
    result.stats.analysis_cost = time.perf_counter() - t0
    return result.stats.analysis_cost


# 优化缺陷R79：深度扫描模式预设（疑难日志：默认啥都没标出时用，
# 宁多报不漏报 —— 爆发 3σ→2σ、罕见判定 10→5、强根因关键词 3→2）
ANALYSIS_MODE_DEEP = {"burst_sigma": 2.0, "rare_min_total": 5,
                      "strong_keyword_score": 2}


# ---------------------------------------------------------------------------
# 异常检测
# ---------------------------------------------------------------------------
def _mark_anomalies(clusters: List[ErrorCluster], global_hist,
                    stats: RunStats, *,
                    burst_sigma: float = BURST_SIGMA,
                    rare_min_total: int = RARE_MIN_TOTAL) -> None:
    """集中爆发 / 周期发作 / 新型错误 / 罕见异常 标注（优先级递降）。

    优化缺陷R75：异常检测强化 ——
    - burst 双通道：全局爆发窗口命中【或】簇自持基线爆发（与自身
      历史比，全局平稳但单簇陡增不再漏报）；
    - 新增 periodic：实例间隔变异系数 ≤0.10（定时任务/心跳失败的
      指纹，此前完全不可见）；
    - 新增 novel：罕见且与既有所有簇模板 Jaccard <0.5（从没见过
      的错，比"老错误的偶发尾巴"含金量高）。
    """
    bursts = global_hist.burst_buckets(k=burst_sigma)
    burst_ranges = [(t, t + global_hist.width) for t, _ in bursts]
    total = stats.error_entries
    token_sets = {id(c): _tokens(c.message_template or c.summary)
                  for c in clusters}

    for c in clusters:
        peak_t: Optional[float] = None
        if c.hist.total:
            series = c.hist.series()
            peak_t = max(series, key=lambda x: x[1])[0]
        global_burst = (peak_t is not None
                        and any(a <= peak_t < b for a, b in burst_ranges))
        if global_burst or _own_baseline_burst(c, burst_sigma):
            c.anomaly = "burst"
        elif _is_periodic(c):
            c.anomaly = "periodic"
        elif total >= rare_min_total and c.count <= 1:
            # 罕见再细分：与既有簇不相似 = 新型错误，相似 = 普通罕见
            c.anomaly = ("novel" if _is_novel(c, clusters, token_sets)
                         else "rare")


def _own_baseline_burst(c: ErrorCluster,
                        sigma: float = BURST_SIGMA) -> bool:
    """簇自持基线爆发：峰值桶超过自身 中位数+3×MAD 且 ≥2 倍基线。

    优化缺陷R75：与全局检测互补 —— 全局直方图被大量其他错误稀释
    时，单簇自身从 2 次/桶陡增到 20 次/桶 的局部爆发照样可抓。
    MAD=0（基线完全平稳）时阈值退化为 2×中位数 + 峰值下限。
    """
    import statistics

    series = c.hist.series()
    if len(series) < OWN_BASELINE_MIN_BUCKETS:
        return False
    counts = [cnt for _, cnt in series]
    peak = max(counts)
    if peak < OWN_BASELINE_MIN_PEAK:
        return False
    median = statistics.median(counts)
    mad = statistics.median(abs(x - median) for x in counts)
    threshold = max(median + sigma * 1.4826 * mad, 2.0 * median)
    return peak > threshold


def _is_periodic(c: ErrorCluster) -> bool:
    """周期发作：实例间隔变异系数（标准差/均值）≤ 阈值。

    优化缺陷R75：定时任务/心跳/看门狗失败的指纹特征 —— 每隔固定
    间隔准时报错；间隔样本取实例时间戳（内存有界：详实实例 +
    元数据实例均带时间戳，与出现总次数无关）。
    """
    ts = sorted(i.timestamp for i in c.instances
                if i.timestamp is not None)
    if len(ts) < PERIODIC_MIN_SAMPLES:
        return False
    deltas = [b - a for a, b in zip(ts, ts[1:]) if b - a > 1e-3]
    if len(deltas) < PERIODIC_MIN_SAMPLES - 1:
        return False
    mean = sum(deltas) / len(deltas)
    var = sum((d - mean) ** 2 for d in deltas) / len(deltas)
    return math.sqrt(var) / mean <= PERIODIC_MAX_CV


_TOKEN_RE = re.compile(r"[a-zA-Z_]{3,}|[一-鿿]{2,}")


def _tokens(text: str) -> set:
    """模板词集（Jaccard 相似度用；英文/数字词 ≥3 字符，中文 ≥2 字）。"""
    return set(_TOKEN_RE.findall((text or "").lower()))


def _is_novel(c: ErrorCluster, clusters: List[ErrorCluster],
              token_sets: dict) -> bool:
    """新型错误：与既有所有簇的模板词集 Jaccard 相似度 < 上限。

    优化缺陷R75：Datadog Content Anomaly 同款思路（内容级相异而
    非量级异常）；仅对罕见簇调用（数量有界，O(罕见簇×总簇)）。
    """
    mine = token_sets.get(id(c)) or set()
    if not mine:
        return False
    for o in clusters:
        if o is c:
            continue
        other = token_sets.get(id(o)) or set()
        if not other:
            continue
        jaccard = len(mine & other) / max(1, len(mine | other))
        if jaccard >= NOVEL_JACCARD_MAX:
            return False
    return True


# ---------------------------------------------------------------------------
# 根因判定
# ---------------------------------------------------------------------------
def _keyword_weights(clusters: List[ErrorCluster]) -> dict:
    """根因/连锁关键词 IDF 权重表（log2 归一）。

    优化缺陷R76：关键词不再一人一票 —— 罕见词（如 deadlock 只在一
    个簇出现）证据权重高于常见词（如 timeout 遍布各簇）：
    w = log2(1 + C/df)，单簇单命中权重恰为 1.0（与旧计数制兼容，
    强关键词阈值 3 语义不变），簇越多、命中越稀有关键词权重越大。
    """
    n = max(1, len(clusters))
    all_kw = tuple(ROOT_CAUSE_KEYWORDS) + tuple(CASCADE_KEYWORDS)
    df = {kw: 0 for kw in all_kw}
    for c in clusters:
        text = f"{c.summary} {c.template}".lower()
        for kw in all_kw:
            if kw in text:
                df[kw] += 1
    # df=0（全语料未出现）权重置 0 —— 该词不参与任何簇的评分
    return {kw: (math.log2(1.0 + n / cnt) if cnt else 0.0)
            for kw, cnt in df.items()}


def _keyword_score(cluster: ErrorCluster, weights: dict) -> float:
    """根因关键词加权得分（根因词权重和 - 连锁词权重和）。"""
    text = f"{cluster.summary} {cluster.template}".lower()
    score = sum(weights.get(kw, 1.0) for kw in ROOT_CAUSE_KEYWORDS
                if kw in text)
    penalty = sum(weights.get(kw, 1.0) for kw in CASCADE_KEYWORDS
                  if kw in text)
    return score - penalty


def _has_cascade_keyword(cluster: ErrorCluster) -> bool:
    text = f"{cluster.summary} {cluster.template}".lower()
    return any(kw in text for kw in CASCADE_KEYWORDS)


def _cluster_sort_key(c: ErrorCluster) -> Tuple[int, float]:
    """排序键：有时间戳者按时间，无时间戳者按行号。"""
    if c.first_seen is not None:
        return (0, c.first_seen)
    return (1, float(c.first_line))


def _add_cross_reference_edges(ordered: List[ErrorCluster],
                               add_edge) -> None:
    """消息互引用边：A 的模板词 60% 出现在后发的 B 中且含稀有词 → A→B。

    优化缺陷R76：衍生错误常在消息中复述上游签名（"auth failed"
    衍生出 "request aborted because auth failed"）。建边条件：
    包含度 ≥0.6 且（包含度 =1.0 —— B 完整复述 A 签名，强证据
    无需稀有词；或含稀有词 df≤2 —— 部分复述时防 "error/failed"
    泛词造成的全连接假边）。
    O(C²)，C 为簇数（有界），仅做集合运算，开销可忽略。
    """
    token_sets = {id(c): _tokens(c.message_template or c.summary)
                  for c in ordered}
    df: dict = {}
    for ts in token_sets.values():
        for t in ts:
            df[t] = df.get(t, 0) + 1
    for i, a in enumerate(ordered):
        ta = token_sets[id(a)]
        if len(ta) < 2:
            continue
        for b in ordered[i + 1:]:
            tb = token_sets[id(b)]
            if not tb:
                continue
            inter = ta & tb
            containment = len(inter) / len(ta)
            if containment < 0.6:
                continue
            if (containment >= 1.0
                    or any(df.get(t, 0) <= 2 for t in inter)):
                add_edge(id(a), id(b))


def _mark_root_causes(clusters: List[ErrorCluster], *,
                      strong_keyword_score: float = STRONG_KEYWORD_SCORE
                      ) -> None:
    """因果图根因判定（优化缺陷R76：从关键词投票升级为因果 DAG）。

    建边（均为保守强证据，宁缺毋错）：
    a. Caused-by 链：带 "Caused by:" 堆栈的错误 → 其紧邻前置错误；
    b. 消息互引用：模板词高包含 + 稀有词的后发错误；
    判定：
    1. 图源头（出度>0 且入度=0）→ 根因（Caused-by 源优先注明）；
    2. 时间连锁：60s 窗口首发 + IDF 加权根因分 >0 → 根因；
    3. 强关键词：加权分 ≥ 阈值 → 根因；
    4. 入度>0 或含连锁关键词 → 疑似连锁衍生（修上游，别修它）。
    """
    ordered = sorted(clusters, key=_cluster_sort_key)
    weights = _keyword_weights(clusters)
    by_id = {id(c): c for c in ordered}
    out_edges: dict = {}
    in_edges: dict = {}
    caused_by_src: set = set()

    def _add(src: int, dst: int) -> None:
        if src == dst:
            return
        out_edges.setdefault(src, set()).add(dst)
        in_edges.setdefault(dst, set()).add(src)

    # a) Caused-by 因果链
    for c in ordered:
        stack = c.sample.entry.stack if c.sample else []
        if any(_CAUSED_BY_RE.match(line) for line in stack):
            prior = _nearest_prior(ordered, c)
            if prior is not None:
                _add(id(prior), id(c))
                caused_by_src.add(id(prior))

    # b) 消息互引用边
    _add_cross_reference_edges(ordered, _add)

    # 1) 图源头 → 根因
    for c in ordered:
        outs = out_edges.get(id(c), set())
        if outs and not in_edges.get(id(c)):
            c.is_root_cause = True
            if id(c) in caused_by_src:
                c.root_cause_reason = "被 Caused-by 因果链指向"
                # 唯一够得上 CONFIRMED 的证据：栈里有 Caused-by 直连，
                # 因果方向由解析器确定，不是靠统计猜出来的
                c.root_cause_confidence = CONF_CONFIRMED
            else:
                c.root_cause_reason = (
                    f"因果链源头（{len(outs)} 个错误由其衍生）")
                # 消息互引用建边是"模板词高包含"这类启发式，属指向非直连
                c.root_cause_confidence = CONF_LIKELY

    # 2) 时间连锁：突发窗口内首发 + 加权根因分 >0
    windows = {}
    for c in ordered:
        if c.first_seen is None:
            continue
        key = int(c.first_seen // BURST_WINDOW_SEC)
        windows.setdefault(key, []).append(c)
    for group in windows.values():
        earliest = group[0]  # ordered 已按时间排序，组内首个即窗口内首发
        if (not earliest.is_root_cause
                and _keyword_score(earliest, weights) > 0):
            earliest.is_root_cause = True
            earliest.root_cause_reason = (
                "时间连锁源头（窗口内首发且含根因特征）")
            earliest.root_cause_confidence = CONF_LIKELY

    # 3) 强关键词 / 4) 连锁衍生标记
    for c in ordered:
        if (not c.is_root_cause
                and _keyword_score(c, weights) >= strong_keyword_score):
            c.is_root_cause = True
            c.root_cause_reason = "高频根因特征关键词"
            # 关键词是统计线索，不构成因果证明
            c.root_cause_confidence = CONF_LIKELY
        elif not c.is_root_cause and not c.root_cause_reason:
            ins = in_edges.get(id(c))
            if ins:
                src = by_id[next(iter(ins))]
                c.root_cause_reason = (
                    f"疑似连锁衍生（上游：{src.summary[:40]}）")
            elif _has_cascade_keyword(c):
                c.root_cause_reason = "疑似连锁衍生错误（被动失败特征）"
            if not c.root_cause_confidence:
                c.root_cause_confidence = CONF_INSUFFICIENT

    # 兜底：任何未被判定过的簇，一律标 INSUFFICIENT（而不是留空）。
    # 空字符串会让下游误以为"没结论"和"证据不足"是两回事。
    for c in ordered:
        if not c.root_cause_confidence:
            c.root_cause_confidence = (
                CONF_CONFIRMED if c.is_root_cause else CONF_INSUFFICIENT)

    return out_edges, by_id


# ---------------------------------------------------------------------------
# 证据缺口评估（v2 的核心差异化）
# ---------------------------------------------------------------------------
@dataclass
class EvidenceGap:
    """一条「还缺什么才能定论」。

    Attributes:
        code: 机器可读标识，前端与 Agent 据此做判断
        title: 一句话标题
        why: 补上它能定论的原因
        missing: 现在具体缺的是什么
        weight: 排序权重，越大越关键
    """
    code: str
    title: str
    why: str
    missing: str
    weight: int = 0


@dataclass
class EvidenceAssessment:
    """整份分析的证据充分性判定。"""
    verdict: str                      # CONFIRMED / LIKELY / INSUFFICIENT
    confidence: str                   # 最高档（与 verdict 同值，保留冗余便于扩展）
    headline: str                     # 一句话结论（含"我不知道"的可能）
    can_conclude: bool                # 能否把某簇当根因输出
    candidate_ids: List[int]
    candidates: List[Dict[str, Any]]
    gaps: List[EvidenceGap]
    inputs_used: List[str]            # 实际用到的证据种类（可审计）

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "confidence": self.confidence,
            "headline": self.headline,
            "can_conclude": self.can_conclude,
            "candidate_ids": self.candidate_ids,
            "candidates": self.candidates,
            "gaps": [
                {"code": g.code, "title": g.title, "why": g.why,
                 "missing": g.missing, "weight": g.weight}
                for g in self.gaps
            ],
            "inputs_used": self.inputs_used,
        }


def assess_evidence(clusters: List[ErrorCluster], stats: RunStats) -> EvidenceAssessment:
    """评估「凭这份日志能不能定论」，并列出证据缺口。

    设计立场：**不猜**。拿不出因果链就说证据不足，并精确说明补什么才
    能定论 —— 这比给一个自信的错误根因有用得多。

    判定完全由确定性规则得出，不含任何模型推断。

    Args:
        clusters: 已完成智能分析的簇列表
        stats: 本次运行的统计信息

    Returns:
        EvidenceAssessment：判定 + 候选 + 缺口清单
    """
    confirmed = [c for c in clusters if c.root_cause_confidence == CONF_CONFIRMED]
    likely = [c for c in clusters
              if c.is_root_cause and c.root_cause_confidence == CONF_LIKELY]

    inputs_used: List[str] = []
    gaps: List[EvidenceGap] = []

    # --- 证据维度盘点：先把"手上有什么"记清楚，缺口才有对照 ---
    has_timestamps = any(
        c.first_seen is not None for c in clusters) or stats.time_start is not None
    if has_timestamps:
        inputs_used.append("时间戳（可做时序推断）")

    has_stacks = any(
        c.sample is not None and c.sample.entry is not None and c.sample.entry.stack
        for c in clusters)
    if has_stacks:
        inputs_used.append("堆栈（可取 Caused-by 因果链）")

    modules = {c.module for c in clusters if c.module}
    multi_module = len(modules) > 1
    if multi_module:
        inputs_used.append(f"多模块覆盖（{len(modules)} 个模块）")

    levels = {c.level for c in clusters}
    multi_level = len(levels) > 1
    if multi_level:
        inputs_used.append(f"多级别分布（{'/'.join(sorted(levels))}）")

    # --- 缺口 1：没有确定性因果链 ---
    if not confirmed:
        gaps.append(EvidenceGap(
            code="no_causal_chain",
            title="缺少确定性因果链",
            why="Caused-by 栈是唯一能证明'谁引发谁'的证据；关键词命中和时间"
                "先后都只是统计线索，无法排除巧合。",
            missing=("带完整异常堆栈的日志（尤其是 Caused by: 链），"
                     "或上游服务的日志"),
            weight=100,
        ))

    # --- 缺口 2：单一模块，无法跨服务定因 ---
    if not multi_module and clusters:
        gaps.append(EvidenceGap(
            code="single_module",
            title="只覆盖单一模块",
            why="根因常在下游（数据库/缓存/中间件），只看一个服务的日志，"
                "分不清是它自己坏了，还是上游把压力传导下来了。",
            missing="上下游服务的日志（同一时间窗），或带 request_id 的调用链",
            weight=80,
        ))

    # --- 缺口 3：无时间戳，无法定先后 ---
    if not has_timestamps:
        gaps.append(EvidenceGap(
            code="no_timestamps",
            title="无时间戳",
            why="没有时间顺序就无法判断谁是先发生的，"
                "而根因必然早于其表现。",
            missing="带时间戳的日志格式",
            weight=70,
        ))

    # --- 缺口 4：无堆栈 ---
    if not has_stacks:
        gaps.append(EvidenceGap(
            code="no_stacks",
            title="无异常堆栈",
            why="堆栈里的 Caused by 链是判断依赖关系最可靠的依据，"
                "缺失时只能靠消息文本猜。",
            missing="未做异常栈裁剪的完整堆栈",
            weight=60,
        ))

    # --- 缺口 5：候选难分伯仲 ---
    if likely:
        top = max(likely, key=lambda c: c.priority)
        rivals = [c for c in likely
                  if c is not top and top.priority - c.priority < 5]
        if rivals:
            gaps.append(EvidenceGap(
                code="ambiguous_candidates",
                title="存在难以区分的并列候选",
                why=("多个错误簇的证据强度接近，靠现有日志无法判断哪个是真因，"
                     "强行选一个会误导排查方向。"),
                missing=("能区分二者的额外信号（调用链先后、指标对比、"
                         "变更时间点）"),
                weight=90,
            ))

    # --- 缺口 6：缺少常态基线 ---
    if stats.error_lines and stats.error_lines >= 10 and not has_timestamps:
        gaps.append(EvidenceGap(
            code="no_baseline",
            title="无历史基线",
            why="没有历史数据就无法区分'一直这么错'和'刚变坏'，"
                "而后者才是故障。",
            missing="同一服务此前的正常期日志",
            weight=40,
        ))

    gaps.sort(key=lambda g: -g.weight)

    candidates = sorted(confirmed or likely,
                        key=lambda c: (-c.priority, -c.count))
    cand_dicts = [{
        "id": c.cluster_id,
        "level": c.level,
        "module": c.module or "",
        "summary": c.summary,
        "count": c.count,
        "priority": c.priority,
        "confidence": c.root_cause_confidence,
        "reason": c.root_cause_reason,
        "evidence_lines": [c.first_line, c.last_line],
    } for c in candidates[:5]]

    if confirmed:
        verdict = CONF_CONFIRMED
        top = confirmed[0]
        headline = (f"已定位根因：{top.summary[:60]}"
                    f"（Caused-by 因果链直连，见第 {top.first_line} 行）")
        can_conclude = True
    elif likely:
        verdict = CONF_LIKELY
        top = candidates[0] if candidates else None
        name = top.summary[:50] if top else "（无）"
        headline = (f"最可能的原因：{name} —— 但这是统计推断，"
                    f"不是因果证明；下面列出了还缺什么才能定论。")
        can_conclude = False
    else:
        verdict = CONF_INSUFFICIENT
        headline = ("无法从这份日志判定根因。错误簇已归并统计，"
                    "但缺少能证明因果方向的证据。")
        can_conclude = False

    if not candidates and verdict != CONF_INSUFFICIENT:
        verdict = CONF_INSUFFICIENT
        can_conclude = False

    return EvidenceAssessment(
        verdict=verdict,
        confidence=verdict,
        headline=headline,
        can_conclude=can_conclude,
        candidate_ids=[c.cluster_id for c in candidates],
        candidates=cand_dicts,
        gaps=gaps,
        inputs_used=inputs_used,
    )


def _nearest_prior(ordered: Sequence[ErrorCluster],
                   target: ErrorCluster) -> Optional[ErrorCluster]:
    """按行号寻找 target 之前最近出现的其他簇。"""
    best: Optional[ErrorCluster] = None
    best_gap = None
    for c in ordered:
        if c is target:
            continue
        if c.first_line < target.first_line:
            gap = target.first_line - c.last_line
            if best_gap is None or gap < best_gap:
                best, best_gap = c, gap
    return best


# ---------------------------------------------------------------------------
# 关联叙事（优化缺陷R78：相似簇关联 + 根因时间线）
# ---------------------------------------------------------------------------
RELATED_JACCARD_MIN = 0.8      # 相似簇模板词集 Jaccard 下限
TIMELINE_MAX_NODES = 5         # 根因时间线最大节点数（可读性）


def _link_related_clusters(clusters: List[ErrorCluster]) -> None:
    """相似簇关联：模板词集 Jaccard ≥0.8 的簇互相登记 related_clusters。

    优化缺陷R78：同一根因常炸出多种错误（变体未被指纹合并时散落
    多簇）——关联后详情面板可见「相关簇」，一眼看穿同源。
    性能：倒排索引（词→簇）只比较共享词的对子，避免 O(C²) 全枚举；
    无共享词的对子 Jaccard 恒 0 无需计算。
    """
    token_sets = {id(c): _tokens(c.message_template or c.summary)
                  for c in clusters}
    index: dict = {}
    for c in clusters:
        for t in token_sets[id(c)]:
            index.setdefault(t, []).append(id(c))
    pair_inter: dict = {}
    for ids in index.values():
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                key = (ids[i], ids[j])
                pair_inter[key] = pair_inter.get(key, 0) + 1
    by_id = {id(c): c for c in clusters}
    for (a, b), inter in pair_inter.items():
        union = len(token_sets[a]) + len(token_sets[b]) - inter
        if union and inter / union >= RELATED_JACCARD_MIN:
            by_id[a].related_clusters.append(by_id[b].cluster_id)
            by_id[b].related_clusters.append(by_id[a].cluster_id)
    for c in clusters:
        c.related_clusters.sort()


# ---------------------------------------------------------------------------
# 错误共现（优化缺陷R102：同窗反复同现的簇 → 直指同一根因）
# ---------------------------------------------------------------------------
def cooccurring_clusters(
        target: ErrorCluster, clusters: List[ErrorCluster],
        window_sec: float = COOC_WINDOW_SEC,
        window_lines: int = COOC_WINDOW_LINES,
        min_hits: int = COOC_MIN_HITS
) -> List[Tuple[ErrorCluster, int]]:
    """与 target 在同一时间窗/行距内反复同现的簇（按同现次数降序）。

    判定：target 的每个实例，在对方实例时间轴（或行号轴，无时间
    戳时）±窗口内存在实例计 1 次同现；累计 ≥ min_hits 判共现。
    与【相关簇】（模板词相似，"长得像"）互补 —— 共现是"一起炸"。

    性能：实例列表按摄入顺序天然有序，bisect 双边计数，
    O((n+m)·log m)；实例记录有界，开销可忽略。
    """
    import bisect

    def _axis(c: ErrorCluster):
        """返回 (时间戳有序数组, 行号有序数组)；时间轴可能不完整。"""
        ts = [i.timestamp for i in c.instances if i.timestamp is not None]
        ts.sort()
        lines = [i.line_no for i in c.instances if i.line_no > 0]
        lines.sort()
        return ts, lines

    def _window_count(sorted_vals: List[float], center: float,
                      half: float) -> int:
        lo = bisect.bisect_left(sorted_vals, center - half)
        hi = bisect.bisect_right(sorted_vals, center + half)
        return hi - lo

    results: List[Tuple[ErrorCluster, int]] = []
    for other in clusters:
        if other is target or not other.instances:
            continue
        o_ts, o_lines = _axis(other)
        hits = 0
        for inst in target.instances:
            if inst.timestamp is not None and o_ts:
                hits += _window_count(o_ts, inst.timestamp, window_sec)
            elif inst.line_no > 0 and o_lines:
                hits += _window_count(o_lines, inst.line_no, window_lines)
        if hits >= min_hits:
            results.append((other, hits))
    results.sort(key=lambda x: x[1], reverse=True)
    return results


# ---------------------------------------------------------------------------
# 故障窗口对比（优化缺陷R104：图表弹窗刷选时段 vs 全量基线显著突增）
# ---------------------------------------------------------------------------
def significant_in_window(
        clusters: List[ErrorCluster],
        win_start: float, win_end: float,
        span_start: float, span_end: float,
        min_count: int = 2, min_z: float = 2.0
) -> List[Tuple[ErrorCluster, int, float]]:
    """窗口内显著突增的簇（Poisson z-score 相对全量基线）。

    expected = 簇带时间戳实例总数 × 窗口时长占比；
    z = (窗口内次数 - expected) / √expected —— z 越大说明该错误
    在窗口内「异常地多」（而非单纯次数多），与 SLS significant_terms
    同款思路的轻量实现。无时间戳实例不参与（无法定位窗口）。

    返回 [(簇, 窗口内次数, z)] 按 z 降序；窗口与全量无交集返回 []。
    """
    span = max(1e-9, span_end - span_start)
    win = max(0.0, min(win_end, span_end) - max(win_start, span_start))
    if win <= 0:
        return []
    frac = win / span
    out: List[Tuple[ErrorCluster, int, float]] = []
    for c in clusters:
        ts = [i.timestamp for i in c.instances if i.timestamp is not None]
        if not ts:
            continue
        w = sum(1 for t in ts if win_start <= t <= win_end)
        expected = len(ts) * frac
        if w < min_count or expected <= 0:
            continue
        z = (w - expected) / math.sqrt(expected)
        if z >= min_z:
            out.append((c, w, z))
    out.sort(key=lambda x: x[2], reverse=True)
    return out


def _build_timelines(clusters: List[ErrorCluster], out_edges: dict,
                     by_id: dict) -> None:
    """根因时间线：沿因果图 BFS，生成传播链叙事（仅根因簇有值）。

    优化缺陷R78：形如
    「14:08:52 首次 connection refused（根因） → +3s request failed
    （衍生） → +40s request failed storm（爆发 ×89）」—— 把因果图
    翻译成人话，详情面板【传播链】展示。节点数上限保证可读；
    无时间戳时退化用行号差。
    """
    for c in clusters:
        if not c.is_root_cause:
            continue
        chain = []                       # (dst_cluster, 增量标签)
        seen = {id(c)}
        frontier = [id(c)]
        while frontier and len(chain) < TIMELINE_MAX_NODES:
            nxt = []
            for src in frontier:
                dsts = sorted(out_edges.get(src, ()),
                              key=lambda d: _cluster_sort_key(by_id[d]))
                for d in dsts:
                    if d in seen or len(chain) >= TIMELINE_MAX_NODES:
                        continue
                    seen.add(d)
                    dst = by_id[d]
                    if (c.first_seen is not None
                            and dst.first_seen is not None):
                        dd = f"+{dst.first_seen - c.first_seen:.0f}s"
                    else:
                        dd = f"+{dst.first_line - c.first_line}行"
                    chain.append((dst, dd))
                    nxt.append(d)
            frontier = nxt
        if not chain:
            continue
        parts = [f"{format_timestamp(c.first_seen)} 首次 "
                 f"{c.summary[:30]}（根因）"]
        for dst, dd in chain:
            tag = "衍生"
            if dst.anomaly == "burst":
                tag = f"爆发 ×{dst.count}"
            parts.append(f"{dd} {dst.summary[:30]}（{tag}）")
        c.root_timeline = " → ".join(parts)


# ---------------------------------------------------------------------------
# 优先级计算
# ---------------------------------------------------------------------------
# 修复缺陷R40：五级别优先级分档表（下界, 上界）—— 级别决定档位
# 区间（P0 错误 / P1 失败 / P2 警告 / P3 信息 / P4 调试），频次/
# 根因/异常加分只在档内拉开差距，不再跨档（原纯公式下 FAIL 高频
# 可冲 P0、INFO 低频掉 P4，级别与档位脱钩）
_LEVEL_BANDS = {
    "ERROR": (80.0, None),     # 保底 P0（≥75）
    "FAIL": (55.0, 75.0),      # 钳 P1
    "WARN": (35.0, 55.0),      # 钳 P2
    "INFO": (15.0, 35.0),      # 钳 P3
    "DEBUG": (None, 15.0),     # 封顶 P4
    "TRACE": (None, 15.0),     # 封顶 P4
}


# 优化缺陷R77：持续性/新生度判定参数
ONGOING_WINDOW_SEC = 60.0      # 末见距日志末尾 ≤60s 视为「持续发生」
NEW_PATTERN_TAIL_RATIO = 0.75  # 首现于日志后 25% 时段视为「新生模式」


def _compute_priorities(clusters: List[ErrorCluster],
                        stats: RunStats) -> None:
    """优先级综合评分（优化缺陷R77：加持续性/新生度，评分构成落库）。

    权重：级别 35% + 频次（对数归一）25% + 根因 20% + 异常 10% +
    持续 5%（末见贴日志末尾，此刻还在炸）+ 新生 5%（后段才出现的
    新模式）；每项贡献与档位钳制说明写入 c.priority_detail，
    供详情面板展示"这分是怎么算的"。
    """
    max_count = max((c.count for c in clusters), default=0)
    denom = math.log10(max_count + 1) if max_count > 1 else 1.0
    duration: Optional[float] = None
    if stats.time_start is not None and stats.time_end is not None:
        duration = stats.time_end - stats.time_start
    for c in clusters:
        level_w = LEVEL_WEIGHT.get(c.level, 0.5)
        freq = math.log10(c.count + 1) / denom if max_count > 1 else 1.0
        root = 1.0 if c.is_root_cause else 0.0
        anomaly = 1.0 if c.anomaly else 0.0
        ongoing = 0.0
        if (stats.time_end is not None and c.last_seen is not None
                and 0 <= stats.time_end - c.last_seen
                <= ONGOING_WINDOW_SEC):
            ongoing = 1.0
        new_pat = 0.0
        if (duration and duration > 0 and c.first_seen is not None
                and c.first_seen >= stats.time_start
                + NEW_PATTERN_TAIL_RATIO * duration):
            new_pat = 1.0
        parts = (("级别", 35.0 * level_w), ("频次", 25.0 * freq),
                 ("根因", 20.0 * root), ("异常", 10.0 * anomaly),
                 ("持续", 5.0 * ongoing), ("新生", 5.0 * new_pat))
        score = sum(v for _, v in parts)
        # 修复缺陷R40：按级别分档钳制（ERROR 保底 80 确保 P0 前置；
        # 原 FATAL 强制 90 随 FATAL 删除移除）
        lo, hi = _LEVEL_BANDS.get(c.level, (None, None))
        clamp_note = ""
        if lo is not None and score < lo:
            score = lo
            clamp_note = f"（{c.level} 档保底 {lo:.0f}）"
        elif hi is not None and score >= hi:
            score = hi - 0.1
            clamp_note = f"（{c.level} 档封顶 {hi - 0.1:.0f}）"
        c.priority = round(score, 1)
        segs = [f"{k}{v:.0f}" for k, v in parts if v > 0.05]
        c.priority_detail = "+".join(segs) + clamp_note


def _sort_clusters(clusters: List[ErrorCluster]) -> None:
    """排序：ERROR 置顶 -> 优先级降序 -> 次数降序（修复缺陷R40）。"""
    clusters.sort(
        key=lambda c: (c.level == "ERROR", c.priority, c.count),
        reverse=True,
    )
