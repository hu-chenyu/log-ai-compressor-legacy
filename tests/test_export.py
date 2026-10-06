# -*- coding: utf-8 -*-
"""导出层单元测试：Markdown / JSON / 纯文本 / 简要摘要。"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from log_ai_compressor.core.pipeline import analyze_text
from log_ai_compressor.export.reporters import (
    brief_summary,
    compare_to_markdown,
    estimate_tokens,
    to_html,
    to_json,
    to_markdown,
    to_text,
    token_status_text,
)

from log_ai_compressor.core.comparator import compare_results

SAMPLE = """\
2024-01-01 09:00:00 INFO [auth] service started
2024-01-01 09:00:01 WARN [db] pool nearly exhausted
2024-01-01 09:00:05 ERROR [db] connection refused to db-primary:5432
java.net.ConnectException: Connection refused
\tat com.app.db.Pool.init(Pool.java:42)
\tat java.base/java.net.Socket.connect(Socket.java:1)
\tat com.app.core.Main.start(Main.java:18)
2024-01-01 09:00:06 ERROR [api] request 123 failed
2024-01-01 09:00:07 ERROR [api] request 456 failed
2024-01-01 09:01:00 FATAL [core] out of memory in worker 3
"""


@pytest.fixture(scope="module")
def result():
    return analyze_text(SAMPLE)


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------
class TestMarkdown:
    def test_structure_sections(self, result):
        md = to_markdown(result)
        assert md.startswith("# 日志AI压缩报告")
        assert "## 一、概览统计" in md
        assert "## 二、Top" in md
        assert "## 三、典型样例详情" in md

    def test_overview_stats(self, result):
        md = to_markdown(result)
        assert "| 总行数 | 10 |" in md
        assert "connection refused" in md

    def test_top_table_rows(self, result):
        md = to_markdown(result, top_n=3)
        assert md.count("\n| 1 |") == 1
        assert "P0" in md or "P1" in md

    def test_root_cause_marked(self, result):
        """根因措辞必须跟随证据判定（v2 契约）。

        v1 无论证据强弱都写「初步定位根因」；现在只有拿到 Caused-by 因果链
        （verdict=CONFIRMED）才敢用「根因」措辞，否则改为「可能原因（统计
        推断，非因果证明）」并附缺口清单。
        """
        md = to_markdown(result)
        verdict = result.evidence.get("verdict")
        if verdict == "CONFIRMED":
            assert "已定位根因" in md
            assert "非因果证明" not in md
        else:
            assert "初步定位根因" not in md, "证据不足时不得使用『根因』措辞"
            assert "非因果证明" in md
            assert "证据充分性评估" in md
        assert "✔" in md or "根因" in md or "可能原因" in md

    def test_stack_denoised_in_detail(self, result):
        md = to_markdown(result)
        assert "已折叠" in md
        assert "java.base" in md           # 折叠注释中说明
        assert "com.app.db.Pool.init" in md  # 业务帧保留

    def test_context_fences(self, result):
        md = to_markdown(result)
        assert "**典型样例**" in md
        assert "```" in md

    def test_pipe_in_summary_escaped(self, result):
        md = to_markdown(result)
        # 摘要中若含 | 须转义，保证表格不破
        assert md.count("\\|") >= md.count("|ERROR |")  # 粗略不破坏校验

    def test_empty_clusters_report(self):
        r = analyze_text("2024-01-01 09:00:00 INFO [x] nothing wrong\n")
        md = to_markdown(r)
        assert "未发现符合条件的错误" in md

    def test_title_override(self, result):
        md = to_markdown(result, title="自定义标题")
        assert "# 自定义标题" in md


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------
class TestJson:
    def test_valid_json(self, result):
        payload = json.loads(to_json(result))
        assert payload["generator"].startswith("log-ai-compressor")
        assert payload["meta"]["total_lines"] == 10
        assert isinstance(payload["clusters"], list)

    def test_cluster_fields(self, result):
        payload = json.loads(to_json(result))
        c = payload["clusters"][0]
        for key in ("id", "level", "summary", "count", "priority",
                    "priority_label", "root_cause", "anomaly", "sample"):
            assert key in c

    def test_sample_fields(self, result):
        payload = json.loads(to_json(result))
        pool = next(c for c in payload["clusters"] if "refused" in c["summary"])
        assert pool["sample"]["stack"]
        assert pool["sample"]["stack_noise_folded"] >= 1
        assert pool["sample"]["stack_simplified"]

    def test_time_series(self, result):
        payload = json.loads(to_json(result))
        assert payload["time_series"]
        assert len(payload["time_series"][0]) == 2

    def test_top_n_limit(self, result):
        payload = json.loads(to_json(result, top_n=1))
        assert len(payload["clusters"]) == 1

    def test_instances_index(self, result):
        """优化缺陷R58：JSON 簇携带实例行号索引（结构化全量）。"""
        payload = json.loads(to_json(result))
        first = payload["clusters"][0]
        assert first["instances"], "簇应携带实例行号索引"
        inst = first["instances"][0]
        for key in ("line_no", "last_line_no", "timestamp", "summary"):
            assert key in inst

    def test_lean_mode(self, result):
        """修复缺陷R61：full=False 精简输出 —— 无上下文/堆栈/样例
        原文，仅核心字段 + 实例行号列表，体积大幅缩小。"""
        lean = to_json(result, full=False)
        payload = json.loads(lean)
        first = payload["clusters"][0]
        assert "sample" not in first, "精简模式不得含样例原文"
        assert "instances" not in first, "精简模式以 instance_lines 替代"
        assert first["instance_lines"], "精简模式应含实例行号列表"
        for key in ("id", "level", "summary", "count", "priority_label",
                    "first_line", "last_line", "first_seen"):
            assert key in first
        assert "context_before" not in lean, "精简模式不得含上下文"
        full_text = to_json(result)
        assert len(lean) < len(full_text) / 2, "精简体积应显著小于完整版"
        # 默认（不参）保持完整结构（向后兼容）
        assert "sample" in json.loads(full_text)["clusters"][0]


# ---------------------------------------------------------------------------
# HTML 报告（优化缺陷R58）
# ---------------------------------------------------------------------------
class TestHtmlReport:
    def test_self_contained(self, result):
        html = to_html(result)
        assert html.startswith("<!DOCTYPE html>")
        assert 'charset="utf-8"' in html
        assert "<style>" in html, "应内联样式（自包含单文件）"
        assert "日志AI压缩报告" in html
        assert "connection refused" in html

    def test_badge_and_anchor(self, result):
        html = to_html(result)
        assert 'class="badge"' in html, "级别应有着色徽章"
        assert 'href="#c1"' in html and 'id="c1"' in html, \
            "目录锚点应可跳转详情"

    def test_escapes_content(self):
        r = analyze_text(
            '2024-01-01 09:00:00 ERROR [m] a <b> & "quoted"\n')
        html = to_html(r)
        assert 'a <b> & "quoted"' not in html, "未转义内容会注入报告"
        assert "a &lt;b&gt; &amp; &quot;quoted&quot;" in html

    def test_sections_toggle(self, result):
        only_list = to_html(result, sections={"list"})
        assert "错误清单" in only_list
        assert "概览统计" not in only_list
        assert "典型样例详情" not in only_list

    def test_instances_index_toggle(self, result):
        with_i = to_html(result, sections={"detail", "instances"})
        without_i = to_html(result, sections={"detail"})
        assert "实例行号" in with_i
        assert "实例行号" not in without_i


class TestSections:
    """优化缺陷R58：内容板块勾选 → md/txt 按板块生成。"""

    def test_md_sections_toggle(self, result):
        md = to_markdown(result, sections={"overview"})
        assert "概览统计" in md
        assert "错误清单" not in md
        assert "典型样例详情" not in md

    def test_md_default_has_instances(self, result):
        assert "实例行号" in to_markdown(result), "默认全板块含实例索引"
        md = to_markdown(result, sections={"detail"})
        assert "实例行号" not in md

    def test_txt_sections(self, result):
        txt = to_text(result, sections={"overview", "instances"})
        assert "总行数" in txt
        assert "实例行号" in txt
        txt2 = to_text(result, sections={"overview"})
        assert "实例行号" not in txt2


# ---------------------------------------------------------------------------
# 纯文本与摘要
# ---------------------------------------------------------------------------
class TestTextAndBrief:
    def test_text_report(self, result):
        txt = to_text(result)
        assert "日志AI压缩报告" in txt
        assert "====" in txt
        assert "根因" in txt

    def test_text_full_detail(self, result):
        """优化缺陷R59：纯文本与 MD 同内容（样例/上下文/降噪堆栈）。"""
        txt = to_text(result)
        assert "[概览统计]" in txt
        assert "[错误清单]" in txt
        assert "[典型样例详情]" in txt
        assert "典型样例:" in txt
        assert "前上下文:" in txt
        assert "堆栈（已降噪" in txt
        assert "connection refused" in txt

    def test_brief_summary(self, result):
        brief = brief_summary(result, top_n=5)
        assert brief.startswith("【日志分析摘要】")
        assert "总行数" in brief
        # 根因措辞跟随证据判定，见 test_root_cause_marked
        if result.evidence.get("verdict") == "CONFIRMED":
            assert "已定位根因" in brief
        else:
            assert "可能原因" in brief and "非因果证明" in brief
        # 简要摘要应显著短于完整报告（压缩比）
        assert len(brief) < len(to_markdown(result)) / 2

    def test_brief_has_line_ranges(self, result):
        """优化缺陷R62：摘要每条附行号范围（行 x~y），便于 AI 定位。"""
        brief = brief_summary(result, top_n=5)
        first = result.clusters[0]
        assert f"行 {first.first_line}~{first.last_line}" in brief


# ---------------------------------------------------------------------------
# 优化缺陷R99：Token 估算 + 压缩率展示
# ---------------------------------------------------------------------------
class TestTokenEstimate:
    def test_estimate_tokens_ascii(self):
        """ASCII 文本 ≈4 字符/token。"""
        assert estimate_tokens("a" * 400) == 100

    def test_estimate_tokens_cjk(self):
        """CJK 文本 ≈1.5 字符/token。"""
        assert estimate_tokens("汉" * 150) == 100

    def test_estimate_tokens_empty(self):
        assert estimate_tokens("") == 0

    def test_raw_chars_counted(self, result):
        """pipeline 流式累计原始字符数（压缩率原始侧分母）。"""
        assert result.stats.raw_chars == sum(
            len(l) for l in SAMPLE.splitlines())

    def test_brief_summary_token_line(self, result):
        """摘要头部第二行为 token 估算（原始 → 压缩 + 压缩率）。"""
        lines = brief_summary(result, top_n=5).splitlines()
        assert "tokens" in lines[1]
        assert "压缩" in lines[1]

    def test_markdown_token_line_no_placeholder(self, result):
        """MD 报告含 token 行且占位符已回填（含无簇早退分支）。"""
        md = to_markdown(result)
        assert "tokens" in md
        assert "__TOKEN_LINE_R99__" not in md
        empty = analyze_text("2024-01-01 09:00:00 INFO all good\n")
        md2 = to_markdown(empty)
        assert "__TOKEN_LINE_R99__" not in md2

    def test_status_text_ratio(self, result):
        """状态栏紧凑串：大样本下压缩率显著时应含 '≈' 与 '压缩 xx%'。"""
        big = analyze_text(SAMPLE * 200)
        text = token_status_text(big, brief_summary(big, top_n=5))
        assert text.startswith("≈")
        assert "tokens" in text
        assert "压缩" in text

    def test_status_text_tiny_no_ratio(self, result):
        """极小样本（压缩侧 ≥ 原始侧）时不出压缩率，不误导。"""
        text = token_status_text(result, brief_summary(result, top_n=5))
        assert "压缩" not in text

    def test_ratio_never_shows_100_percent(self):
        """修复缺陷R106：99.7% 不得四舍五入显示为「100%」。"""
        from log_ai_compressor.core.models import (
            AnalysisResult, RunStats)
        # raw=340000 chars → 85000 tokens；comp 222 tokens → 99.74%
        r = AnalysisResult(stats=RunStats(source="x", raw_chars=340000))
        text = token_status_text(r, "a" * 888)   # 888/4 = 222 tokens
        assert "100%" not in text
        assert "99.7%" in text

    def test_status_text_no_raw(self):
        """原始字符数为 0（手工构造结果）时不出压缩率，不报错。"""
        from log_ai_compressor.core.models import (
            AnalysisResult, RunStats)
        r = AnalysisResult(stats=RunStats(source="x"))
        text = token_status_text(r, "abcd")
        assert text == "≈1 tokens"
        assert "压缩" not in text



# ---------------------------------------------------------------------------
# 修复缺陷R59：方括号 ISO 时间戳日志的端到端解析与导出
# ---------------------------------------------------------------------------
class TestBracketIsoEndToEnd:
    _LOG = "\n".join([
        "[2026-09-04T06:12:11.988Z] + git fetch ssh://imv-ci@gerrit.imv.local/x",
        "[2026-09-04T06:12:12.244Z] error: could not apply 84b4dc4 FIX: camera",
        "[2026-09-04T06:12:12.244Z] hint: after resolving the conflicts",
        "[2026-09-04T06:12:16.697Z] Finished: FAILURE",
    ])

    def test_timestamps_parsed(self):
        r = analyze_text(self._LOG, rule="generic")
        assert r.stats.time_start is not None, "方括号 ISO 时间戳应被解析"
        assert r.stats.time_end > r.stats.time_start
        payload = json.loads(to_json(r))
        assert payload["meta"]["time_start"] is not None
        assert payload["time_series"], "时间直方图应有数据（此前为空）"
        for c in payload["clusters"]:
            assert c["sample"]["timestamp"] is not None

    def test_txt_time_range_and_detail(self):
        r = analyze_text(self._LOG, rule="generic")
        txt = to_text(r)
        assert "时间范围: 2026-09-04" in txt, "时间范围不应再为 -"
        assert "[典型样例详情]" in txt
        assert "could not apply 84b4dc4" in txt

    def test_json_timestamps_iso_readable(self):
        """修复缺陷R60：JSON 时间戳全部输出 ISO 可读字符串（UTC）。"""
        r = analyze_text(self._LOG, rule="generic")
        payload = json.loads(to_json(r))
        ts = payload["meta"]["time_start"]
        assert isinstance(ts, str) and ts.startswith("2026-09-04T06:12:11"), \
            f"time_start 应为 ISO 字符串，实际 {ts!r}"
        # ISO 字符串仍可被标准库机器解析（人/脚本两用）
        datetime.fromisoformat(ts)
        assert payload["time_series"][0][0].startswith("2026-09-04")
        for c in payload["clusters"]:
            assert c["sample"]["timestamp"].startswith("2026-09-04")
            assert c["first_seen"].startswith("2026-09-04")
            for inst in c["instances"]:
                assert inst["timestamp"].startswith("2026-09-04")

    def test_json_null_timestamp_stays_null(self):
        """无时间戳日志：timestamp 字段保持 null（而非 'None' 串）。"""
        r = analyze_text("plain unstructured failure line\n")
        payload = json.loads(to_json(r))
        assert payload["meta"]["time_start"] is None
        for c in payload["clusters"]:
            assert c["sample"]["timestamp"] is None
            for inst in c["instances"]:
                assert inst["timestamp"] is None


# ---------------------------------------------------------------------------
# 对比报告
# ---------------------------------------------------------------------------
class TestCompareMarkdown:
    def test_compare_report(self, result):
        other = analyze_text(SAMPLE.replace("db-primary", "db-backup"))
        cmp = compare_results(result, other)
        md = compare_to_markdown([cmp])
        assert "# 日志对比分析报告" in md
        assert "新增错误" in md
        assert "消失错误" in md
        assert "共同错误" in md
        assert "变化率" in md

    def test_compare_report_identical_files(self, result):
        cmp = compare_results(result, result)
        md = compare_to_markdown([cmp])
        assert "新增错误：**0**" in md
        assert "消失错误：**0**" in md
        assert "持平" in md
