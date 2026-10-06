# -*- coding: utf-8 -*-
"""MCP 接入层测试：工具注册、只读标注、各工具行为与错误路径。

MCP 2.x 里 FastMCP 已更名为 MCPServer；工具标注字段在 Python 侧是
snake_case（read_only_hint），序列化到协议时才转成 readOnlyHint。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from log_ai_compressor.mcp.server import server  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
SAMPLE = BASE / "examples" / "sample_system.log"
GBK = BASE / "examples" / "sample_gbk.log"
V1 = BASE / "examples" / "app_v1.log"
V2 = BASE / "examples" / "app_v2.log"

EXPECTED_TOOLS = {
    "analyze_log_file", "analyze_log_text", "compare_log_files",
    "export_report", "get_cluster_detail", "list_rules", "check_environment",
}


async def call(tool: str, **kwargs):
    """调用工具并把返回的 JSON 文本解成 dict。

    call_tool 返回 CallToolResult，正文在 content[0].text。
    """
    res = await server.call_tool(tool, kwargs)
    if isinstance(res, (list, tuple)):
        first = res[0]
        text = first[0].text if isinstance(first, (list, tuple)) else first.text
    else:
        parts = getattr(res, "content", None) or []
        text = parts[0].text if parts else str(res)
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return {"_raw": str(text)}


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------
class TestRegistration:
    def test_all_tools_registered(self):
        tools = run(server.list_tools())
        assert {t.name for t in tools} == EXPECTED_TOOLS

    def test_all_readonly(self):
        """本工具暴露的是本机文件分析能力，必须全部标记为只读。"""
        for t in run(server.list_tools()):
            assert t.annotations is not None, f"{t.name} 缺 annotations"
            assert t.annotations.read_only_hint is True, f"{t.name} 未标只读"
            assert t.annotations.destructive_hint is False, f"{t.name} 标了破坏性"

    def test_every_tool_has_description(self):
        for t in run(server.list_tools()):
            assert t.description and len(t.description) > 30, \
                f"{t.name} 描述过短，Agent 难以正确调用"

    def test_server_has_instructions(self):
        assert server.instructions
        assert "不出网" in server.instructions or "本机" in server.instructions


# ---------------------------------------------------------------------------
# 自检与规则
# ---------------------------------------------------------------------------
class TestIntrospection:
    def test_check_environment(self):
        r = run(call("check_environment"))
        assert r["ok"] is True
        assert r["app"] == "log-ai-compressor"
        assert "不出网" in r["data_egress"]
        assert len(r["rules"]) >= 3
        assert "ai" in r

    def test_list_rules(self):
        r = run(call("list_rules"))
        assert r["ok"] is True
        names = {x["name"] for x in r["rules"]}
        assert {"generic", "embedded", "jenkins"} <= names


# ---------------------------------------------------------------------------
# 分析
# ---------------------------------------------------------------------------
class TestAnalyze:
    def test_file(self):
        r = run(call("analyze_log_file", paths=[str(SAMPLE)]))
        assert r["ok"] is True
        res = r["result"]
        assert res["stats"]["total_lines"] > 0
        assert res["clusters"]
        assert "root_causes" in res

    def test_payload_is_token_frugal(self):
        """MCP 场景的关键指标：默认返回不含样例与实例，保持小体积。"""
        r = run(call("analyze_log_file", paths=[str(SAMPLE)]))
        c = r["result"]["clusters"][0]
        assert "instances" not in c
        assert c.get("sample") is None

    def test_gbk(self):
        r = run(call("analyze_log_file", paths=[str(GBK)]))
        assert r["ok"] is True
        assert r["result"]["stats"]["encoding"].lower().startswith("gb")

    def test_custom_levels(self):
        r = run(call("analyze_log_file", paths=[str(SAMPLE)],
                     levels=["ERROR", "WARN", "INFO"]))
        assert r["ok"] is True

    def test_text(self):
        r = run(call("analyze_log_text",
                     text="2024-01-01 10:00:00 ERROR [db] boom\n"
                          "2024-01-01 10:00:02 ERROR [db] boom\n"))
        assert r["ok"] is True
        assert r["result"]["clusters"][0]["count"] == 2

    def test_multi_file(self):
        r = run(call("analyze_log_file", paths=[str(V1), str(V2)]))
        assert r["ok"] is True

    def test_compare(self):
        r = run(call("compare_log_files", paths=[str(V1), str(V2)]))
        assert r["ok"] is True
        assert len(r["compare"]) == 1
        assert "新增" in r["markdown"]

    def test_export_markdown(self):
        r = run(call("export_report", paths=[str(SAMPLE)], format="md", top_n=5))
        assert r["ok"] is True
        assert len(r["content"]) > 200
        assert r["length"] == len(r["content"])

    def test_export_redact(self):
        r = run(call("export_report", paths=[str(SAMPLE)], format="md",
                     redact=True))
        assert r["ok"] is True

    def test_cluster_detail(self):
        first = run(call("analyze_log_file", paths=[str(SAMPLE)]))
        cid = first["result"]["clusters"][0]["id"]
        r = run(call("get_cluster_detail", path=str(SAMPLE), cluster_id=cid))
        assert r["ok"] is True
        c = r["cluster"]
        assert c["instances"], "详情应含全部实例"
        assert c["sample"] is not None
        assert "cooccurring" in c


# ---------------------------------------------------------------------------
# 错误路径：必须返回 ok=False 而不是抛异常（Agent 侧更好处理）
# ---------------------------------------------------------------------------
class TestErrorPaths:
    def test_missing_file(self):
        r = run(call("analyze_log_file", paths=["C:/no/such.log"]))
        assert r["ok"] is False and r["error"]

    def test_bad_similarity(self):
        r = run(call("analyze_log_file", paths=[str(SAMPLE)], similarity="bogus"))
        assert r["ok"] is False

    def test_compare_needs_two(self):
        r = run(call("compare_log_files", paths=[str(V1)]))
        assert r["ok"] is False

    def test_unknown_cluster_id(self):
        r = run(call("get_cluster_detail", path=str(SAMPLE), cluster_id=99999))
        assert r["ok"] is False
        assert "不存在" in r["error"]
