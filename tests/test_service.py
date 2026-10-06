# -*- coding: utf-8 -*-
"""service 层测试：参数归一化、序列化、导出门面。

service 是 core 与接入层（Web / MCP）之间唯一的转换点，两边都靠它，
所以这里既要测「对不对」，也要测「非法输入会不会漏到后面炸」。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from log_ai_compressor import service as S

BASE = Path(__file__).resolve().parent.parent
SAMPLE = BASE / "examples" / "sample_system.log"
GBK = BASE / "examples" / "sample_gbk.log"
V1 = BASE / "examples" / "app_v1.log"
V2 = BASE / "examples" / "app_v2.log"


@pytest.fixture(scope="module")
def result():
    return S.analyze(paths=[str(SAMPLE)], params={"levels": ["ERROR", "FAIL"]})


# ---------------------------------------------------------------------------
# 参数归一化
# ---------------------------------------------------------------------------
class TestNormalizeParams:
    def test_defaults(self):
        cfg = S.normalize_params()
        assert cfg["levels"] == ["ERROR", "FAIL"]
        assert cfg["similarity"] == "standard"
        assert cfg["analysis_mode"] == "full"
        assert cfg["context_lines"] == 50
        assert cfg["encoding"] is None          # None = 交给 core 自动探测
        assert cfg["max_lines"] is None

    def test_levels_accepts_comma_string(self):
        assert S.normalize_params({"levels": "error, fail ,warn"})["levels"] == \
            ["ERROR", "FAIL", "WARN"]

    def test_keywords_split_by_comma_and_space(self):
        cfg = S.normalize_params({"include": "timeout, refused ；disk  full"})
        assert cfg["include"] == ["timeout", "refused", "disk", "full"]

    def test_chinese_separators_normalized(self):
        cfg = S.normalize_params({"include": "超时，拒绝"})
        assert cfg["include"] == ["超时", "拒绝"]

    def test_context_lines_negative_becomes_zero(self):
        assert S.normalize_params({"context_lines": -5})["context_lines"] == 0

    def test_context_lines_invalid_falls_back(self):
        assert S.normalize_params({"context_lines": "abc"})["context_lines"] == 50

    def test_context_lines_unbounded(self):
        # 修复缺陷R44：无上限（旧的 200 上限已移除）
        assert S.normalize_params({"context_lines": 9999})["context_lines"] == 9999

    def test_maxlines_preset(self):
        assert S.normalize_params({"maxlines": "100k"})["max_lines"] == 100_000
        assert S.normalize_params({"maxlines": "all"})["max_lines"] is None

    @pytest.mark.parametrize("raw,expected", [
        ("auto", None), ("", None), ("自动探测", None), ("自动识别", None),
        ("utf-8", "utf-8"), ("UTF-8", "utf-8"),
        ("gbk", "gb18030"),          # 用户口语，gb18030 是超集
        ("gb18030", "gb18030"), ("utf-16", "utf-16"),
    ])
    def test_encoding_normalization(self, raw, expected):
        assert S.normalize_params({"encoding": raw})["encoding"] == expected

    def test_invalid_encoding_rejected(self):
        with pytest.raises(S.ServiceError, match="不支持的编码"):
            S.normalize_params({"encoding": "ebcdic"})

    @pytest.mark.parametrize("raw", ["bogus", "xx", "unknown"])
    def test_invalid_similarity_rejected(self, raw):
        with pytest.raises(S.ServiceError, match="similarity"):
            S.normalize_params({"similarity": raw})

    def test_invalid_analysis_mode_rejected(self):
        with pytest.raises(S.ServiceError, match="analysis_mode"):
            S.normalize_params({"analysis_mode": "turbo"})

    def test_invalid_maxlines_rejected(self):
        with pytest.raises(S.ServiceError, match="maxlines"):
            S.normalize_params({"maxlines": "lots"})

    def test_invalid_regex_rejected_early(self):
        # 正则必须在入口拦：core 里编译失败会退化成静默不过滤
        with pytest.raises(S.ServiceError, match="正则"):
            S.normalize_params({"use_regex": True, "include": "[unclosed"})

    def test_valid_regex_passes(self):
        cfg = S.normalize_params({"use_regex": True, "include": r"ERR-\d{4}"})
        assert cfg["include"] == [r"ERR-\d{4}"]

    def test_invalid_time_rejected(self):
        with pytest.raises(S.ServiceError, match="时间戳"):
            S.normalize_params({"time_start": "not-a-number"})


# ---------------------------------------------------------------------------
# 分析入口
# ---------------------------------------------------------------------------
class TestAnalyze:
    def test_file(self, result):
        assert result.stats.total_lines > 0
        assert result.clusters

    def test_text(self):
        r = S.analyze(text="2024-01-01 10:00:00 ERROR [db] boom\n"
                           "2024-01-01 10:00:01 ERROR [db] boom\n")
        assert len(r.clusters) == 1
        assert r.clusters[0].count == 2

    def test_text_strips_bom(self):
        r = S.analyze(text="\ufeff2024-01-01 10:00:00 ERROR [db] boom\n")
        assert r.clusters

    def test_multiple_files_merged(self):
        r = S.analyze(paths=[str(V1), str(V2)])
        assert r.stats.total_lines > 0

    def test_gbk_auto_detected(self):
        r = S.analyze(paths=[str(GBK)])
        assert r.stats.encoding.lower().startswith("gb")

    def test_no_input_rejected(self):
        with pytest.raises(S.ServiceError, match="没有可分析"):
            S.analyze()

    def test_missing_file_rejected(self):
        with pytest.raises(S.ServiceError, match="文件不存在"):
            S.analyze(paths=["C:/definitely/not/here.log"])

    def test_similarity_affects_clustering(self):
        """三档相似度必须真的影响聚类激进度。"""
        msgs = [
            "failed to connect to database host server",
            "failed to connect to database node server",
            "failed to connect to database hosts server",
            "disk quota exceeded on volume /data",
        ]
        counts = {}
        for key in ("strict", "standard", "lenient"):
            text = "".join(
                f"2024-01-01 10:00:0{i} ERROR [db] {m}\n"
                for i, m in enumerate(msgs))
            counts[key] = len(S.analyze(text=text, params={"similarity": key}).clusters)
        assert counts["strict"] >= counts["standard"] >= counts["lenient"]
        assert counts["strict"] > counts["lenient"], \
            f"相似度档位没起作用：{counts}"


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------
class TestSerialization:
    def test_result_is_json_serializable(self, result):
        payload = S.result_to_dict(result)
        json.dumps(payload, ensure_ascii=False)      # 不抛异常即通过

    def test_result_shape(self, result):
        d = S.result_to_dict(result)
        assert d["stats"]["total_lines"] > 0
        assert d["total_clusters"] == len(result.clusters)
        assert isinstance(d["clusters"], list)
        assert "root_causes" in d and "cooccurring" in d

    def test_cluster_fields(self, result):
        c = S.result_to_dict(result)["clusters"][0]
        for key in ("id", "level", "summary", "count", "priority",
                    "first_line", "last_line", "hist", "sample"):
            assert key in c, f"簇缺少字段 {key}"

    def test_histogram_uses_methods_not_attributes(self, result):
        """回归：core 的 TimeHistogram.start/width/total 是 property，
        series()/burst_buckets() 才是方法。用错会静默返回空图。"""
        hist = S.result_to_dict(result)["global_hist"]
        assert hist["series"], "直方图序列为空 —— 检查 _hist 是否误用属性"
        assert all("t" in b and "n" in b for b in hist["series"])
        assert isinstance(hist["burst"], list)

    def test_sample_has_simplified_stack(self):
        text = (
            "2024-01-01 10:00:00 ERROR [db] connect failed\n"
            "java.net.ConnectException: refused\n"
            "\tat com.app.db.Pool.init(Pool.java:42)\n"
            "\tat java.base/java.lang.Thread.run(Thread.java:840)\n"
        )
        c = S.result_to_dict(S.analyze(text=text))["clusters"][0]
        ss = c["sample"]["stack_simplified"]
        assert ss is not None
        assert ss["business_count"] + ss["noise_count"] >= 2
        assert any("已折叠" in line for line in ss["lines"]), \
            "系统库帧应被折叠成占位行"

    def test_full_cluster_has_instances_and_variables(self, result):
        c = S.cluster_to_dict(result.clusters[0], full=True)
        assert "instances" in c
        assert c["count"] == len(c["instances"])
        assert "variables" in c

    def test_top_n_truncates(self, result):
        full = S.result_to_dict(result)
        if full["total_clusters"] <= 2:
            pytest.skip("样例簇数不足，跳过截断断言")
        cut = S.result_to_dict(result, top_n=2)
        assert cut["shown_clusters"] == 2
        assert cut["top_n_truncated"] is True
        assert cut["total_clusters"] == full["total_clusters"]

    def test_without_samples(self, result):
        c = S.cluster_to_dict(result.clusters[0], include_sample=False)
        assert c["sample"] is None

    def test_compare_to_dicts(self):
        payload = S.compare_to_dicts(S.compare([str(V1), str(V2)]))
        assert len(payload) == 1
        pair = payload[0]
        for key in ("base_name", "other_name", "new_items", "gone_items",
                    "common_items", "new_count"):
            assert key in pair
        json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
class TestExport:
    @pytest.mark.parametrize("fmt,must", [
        ("md", "日志AI压缩报告"),
        ("json", '"error_kinds"'),
        ("json_full", '"clusters"'),
        ("txt", "日志"),
        ("html", "<html"),
        ("summary", "日志分析摘要"),
    ])
    def test_formats(self, result, fmt, must):
        text = S.export_text(result, fmt)
        assert must in text
        assert len(text) > 50

    def test_unknown_format_rejected(self, result):
        with pytest.raises(S.ServiceError, match="不支持的导出格式"):
            S.export_text(result, "pdf")

    def test_redact_removes_sensitive(self):
        r = S.analyze(text="2024-01-01 10:00:00 ERROR [db] "
                           "login alice@corp.com password=hunter2 boom")
        plain = S.export_text(r, "md")
        assert "alice@corp.com" in plain
        assert "alice@corp.com" not in S.export_text(r, "md", redact=True)

    def test_custom_redact_rules(self, result):
        out = S.export_text(result, "md", redact=True,
                            custom_rules=[r"\d{4}-\d{2}-\d{2}"])
        assert "2024-03-15" not in out

    def test_media_types(self):
        assert "json" in S.media_type("json")
        assert "markdown" in S.media_type("md")

    def test_summary_much_smaller_than_source(self, result):
        """压缩是这个工具的存在意义：摘要必须远小于原文。"""
        raw = SAMPLE.read_text(encoding="utf-8")
        assert len(S.export_text(result, "summary")) < len(raw) / 2
