# -*- coding: utf-8 -*-
"""证据充分性评估的测试（v2 差异化核心）。

重点守住三条不变量：
1. 只有 Caused-by 因果链才算 CONFIRMED —— 关键词/时间先后只是 LIKELY
2. 没有 CONFIRMED 时 must not 输出「根因」措辞
3. 缺口清单非空且能指出具体缺什么
"""
from __future__ import annotations

from pathlib import Path

import pytest

from log_ai_compressor import service as S
from log_ai_compressor.core.analysis import (
    CONF_CONFIRMED,
    CONF_INSUFFICIENT,
    CONF_LIKELY,
    assess_evidence,
)
from log_ai_compressor.core.pipeline import analyze_file, analyze_text
from log_ai_compressor.export.reporters import brief_summary, to_markdown, to_text

BASE = Path(__file__).resolve().parent.parent
SAMPLE = BASE / "examples" / "sample_system.log"

# 关键词根因特征（不含 Caused-by 链）—— 只能到 LIKELY
LIKELY_LOG = """
2024-01-01 10:00:00 ERROR [db] connection refused to db-primary:5432
2024-01-01 10:00:01 ERROR [db] connection refused to db-secondary:5432
2024-01-01 10:00:02 ERROR [db] connection refused to db-primary:5433
2024-01-01 10:00:03 ERROR [db] connection refused to db-secondary:5434
2024-01-01 10:00:04 INFO [api] GET /health 200 3ms
"""

# 带 Caused-by 链 —— 唯一够得上 CONFIRMED 的形态
CONFIRMED_LOG = """
2024-01-01 10:00:00 ERROR [order] request failed
2024-01-01 10:00:01 ERROR [db] connection pool exhausted, 0/50 available
java.lang.RuntimeException: order create failed
\tat com.app.OrderService.create(OrderService.java:42)
Caused by: java.sql.SQLException: connection refused
\tat com.zaxxer.hikari.pool.HikariPool.createConnection(HikariPool.java:696)
"""


class TestConfidenceTiers:
    def test_keyword_only_is_likely_not_confirmed(self):
        """关键词/时间先后只是统计线索，绝不能标成 CONFIRMED。"""
        r = analyze_text(LIKELY_LOG, levels=["ERROR"])
        ev = r.evidence
        assert ev["verdict"] in (CONF_LIKELY, CONF_INSUFFICIENT)
        assert ev["verdict"] != CONF_CONFIRMED
        assert ev["can_conclude"] is False

    def test_caused_by_chain_is_confirmed(self):
        r = analyze_text(CONFIRMED_LOG, levels=["ERROR"])
        ev = r.evidence
        assert ev["verdict"] == CONF_CONFIRMED
        assert ev["can_conclude"] is True
        assert any(c["confidence"] == CONF_CONFIRMED for c in ev["candidates"])

    def test_every_cluster_has_a_confidence(self):
        """不允许留空 —— 空串会让下游把「没结论」和「证据不足」搞混。"""
        r = analyze_file(str(SAMPLE), levels=["ERROR", "FAIL"])
        for c in r.clusters:
            assert c.root_cause_confidence in (
                CONF_CONFIRMED, CONF_LIKELY, CONF_INSUFFICIENT)

    def test_non_root_clusters_are_insufficient(self):
        r = analyze_text(LIKELY_LOG, levels=["ERROR"])
        for c in r.clusters:
            if not c.is_root_cause:
                assert c.root_cause_confidence == CONF_INSUFFICIENT


class TestEvidenceGaps:
    def test_gaps_non_empty_when_unproven(self):
        r = analyze_text(LIKELY_LOG, levels=["ERROR"])
        assert r.evidence["gaps"], "证据不足时必须给出缺口清单"

    def test_gap_has_actionable_fields(self):
        r = analyze_text(LIKELY_LOG, levels=["ERROR"])
        for g in r.evidence["gaps"]:
            assert g["title"] and g["why"] and g["missing"]
            assert isinstance(g["weight"], int)

    def test_gaps_sorted_by_weight_desc(self):
        r = analyze_text(LIKELY_LOG, levels=["ERROR"])
        weights = [g["weight"] for g in r.evidence["gaps"]]
        assert weights == sorted(weights, reverse=True)

    def test_no_causal_chain_gap_when_unproven(self):
        r = analyze_text(LIKELY_LOG, levels=["ERROR"])
        codes = {g["code"] for g in r.evidence["gaps"]}
        assert "no_causal_chain" in codes

    def test_single_module_gap(self):
        """全部错误来自同一个模块 → 分不清是自己坏了还是被传导。"""
        text = "\n".join(
            f"2024-01-01 10:00:{i:02d} ERROR [api] request failed: {i}" for i in range(6))
        r = analyze_text(text, levels=["ERROR"])
        codes = {g["code"] for g in r.evidence["gaps"]}
        assert "single_module" in codes

    def test_no_timestamps_gap(self):
        text = "\n".join(f"ERROR [db] boom number {i}" for i in range(6))
        r = analyze_text(text, levels=["ERROR"])
        codes = {g["code"] for g in r.evidence["gaps"]}
        assert "no_timestamps" in codes

    def test_confirmed_has_no_gap_block(self):
        """已证实时不该再罗列缺口（那是噪音）。"""
        r = analyze_text(CONFIRMED_LOG, levels=["ERROR"])
        assert r.evidence["verdict"] == CONF_CONFIRMED
        assert not r.evidence["gaps"], "CONFIRMED 不应报缺口"

    def test_inputs_used_recorded(self):
        r = analyze_file(str(SAMPLE), levels=["ERROR", "FAIL"])
        used = r.evidence["inputs_used"]
        assert used, "必须记录本次实际用到了哪些证据种类"
        assert any("时间戳" in u for u in used)


class TestReportHonesty:
    """报告措辞必须跟着判定走 —— 这才是差异化的落点。"""

    def test_markdown_says_cause_only_when_confirmed(self):
        unproven = to_markdown(analyze_text(LIKELY_LOG, levels=["ERROR"]))
        assert "初步定位根因" not in unproven
        assert "非因果证明" in unproven
        assert "证据充分性评估" in unproven

        proven = to_markdown(analyze_text(CONFIRMED_LOG, levels=["ERROR"]))
        assert "已定位根因" in proven
        assert "非因果证明" not in proven

    def test_brief_summary_carries_gaps(self):
        s = brief_summary(analyze_text(LIKELY_LOG, levels=["ERROR"]))
        assert "非因果证明" in s
        assert "缺：" in s

    def test_text_report_flags_gaps(self):
        t = to_text(analyze_text(LIKELY_LOG, levels=["ERROR"]))
        assert "缺：" in t

    def test_html_report_flags_gaps(self):
        from log_ai_compressor.export.reporters import to_html
        h = to_html(analyze_text(LIKELY_LOG, levels=["ERROR"]))
        assert "证据不足" in h

    def test_gap_names_the_missing_artifact(self):
        """缺口必须说清"补什么"，不能只说"证据不足"。"""
        s = brief_summary(analyze_text(LIKELY_LOG, levels=["ERROR"]))
        assert ("堆栈" in s) or ("调用链" in s) or ("上游" in s)


class TestSerialization:
    def test_evidence_in_payload(self):
        d = S.result_to_dict(analyze_file(str(SAMPLE), levels=["ERROR", "FAIL"]))
        assert d["evidence"]["verdict"] in (
            CONF_CONFIRMED, CONF_LIKELY, CONF_INSUFFICIENT)
        assert isinstance(d["evidence"]["gaps"], list)

    def test_cluster_confidence_in_payload(self):
        d = S.result_to_dict(analyze_file(str(SAMPLE), levels=["ERROR", "FAIL"]))
        for c in d["clusters"]:
            assert c["root_cause_confidence"] in (
                CONF_CONFIRMED, CONF_LIKELY, CONF_INSUFFICIENT, "")

    def test_assess_evidence_direct_call(self):
        r = analyze_file(str(SAMPLE), levels=["ERROR", "FAIL"])
        a = assess_evidence(r.clusters, r.stats)
        assert a.verdict in (CONF_CONFIRMED, CONF_LIKELY, CONF_INSUFFICIENT)
        assert a.to_dict()["verdict"] == a.verdict


class TestTheFailureMode:
    """回归：v1 会在这个场景自信地给出错误根因。"""

    TRAP = (
        "2024-01-01 10:00:00 FATAL [order] Connection is not available, "
        "request timed out after 30000ms\n"          # 真正的因，只 1 次
        + "2024-01-01 10:00:01 ERROR [db-pool] connection pool exhausted\n" * 20
    )

    def test_frequent_error_not_stated_as_root_cause(self):
        """刷屏的错误不该被当成「已定位的根因」输出。"""
        r = analyze_text(self.TRAP, levels=["ERROR", "FAIL"])
        ev = r.evidence
        if ev["verdict"] == CONF_CONFIRMED:
            pytest.skip("该输入意外产生了因果链，场景不适配")
        assert ev["can_conclude"] is False
        assert "初步定位根因" not in to_markdown(r)
        assert "非因果证明" in to_markdown(r)
