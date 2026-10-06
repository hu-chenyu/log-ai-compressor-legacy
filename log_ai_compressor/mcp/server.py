# -*- coding: utf-8 -*-
"""MCP 服务器：把日志分析能力暴露给 AI Agent。

为什么做这个
------------
2026 年可观测平台几乎全部提供了 MCP Server（阿里云 SLS、Datadog、
Grafana、OpenObserve、IBM Instana、OneUptime…），MCP 把每个平台从
「人要去看的目的地」变成「Agent 可以调用的数据源」。

而这些平台的数据都在别人的机房里；本工具的数据就在用户本机。这是差异
化点：让 Claude Code / Codex / mavis 这类 Agent 直接调用本机日志做聚类、
根因排序和压缩。

安全边界：**全部工具只读**。没有删除、修改、上传、联网的接口。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from log_ai_compressor import __app_name__, __version__, service as S

INSTRUCTIONS = f"""\
{__app_name__} {__version__} —— 本机日志分析取证台（数据不出网）。

典型用法：
1. 用户给你一个日志文件路径 → 调 analyze_log_file，拿聚类后的错误清单与根因排序。
2. 已经有大段日志文本 → 调 analyze_log_text。
3. 想知道某类错误到底怎么回事 → 调 get_cluster_detail 拿典型样例与降噪堆栈。
4. 要把结论整份交给用户/LLM → 调 export_report（Markdown 格式最省 token）。

关键点：分析结果**已经是压缩过的证据摘要**。几十万行的日志经聚类去重后
通常只剩几百到几千 token，直接作为上下文使用即可，不要再去读原始文件。

所有工具只读，不会修改或上传任何日志。
"""

server = MCPServer(
    name=__app_name__,
    title="日志AI压缩器 · 本地日志取证",
    version=__version__,
    instructions=INSTRUCTIONS,
)

READONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                           idempotentHint=True, openWorldHint=False)


def _err(exc: Exception) -> str:
    return f"分析失败：{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
@server.tool(
    name="analyze_log_file",
    title="分析本机日志文件",
    description=(
        "分析一个或多个本机日志文件（按 mtime 从旧到新合并，适用于轮转日志），"
        "返回：统计概览 + 按优先级排序的错误簇 + 根因判定 + 异常标记。"
        "输出已经过聚类去重，token 消耗比读原始日志低几个数量级。"
    ),
    annotations=READONLY,
)
def analyze_log_file(
    paths: List[str],
    levels: Optional[List[str]] = None,
    include: Optional[List[str]] = None,
    exclude: Optional[List[str]] = None,
    context_lines: int = 50,
    similarity: str = "standard",
    analysis_mode: str = "full",
    rule: str = "auto",
    top_n: int = 20,
    encoding: str = "auto",
    max_lines: Optional[int] = None,
) -> Dict[str, Any]:
    """聚类 + 根因 + 异常检测。

    Args:
        paths: 日志文件绝对路径；多个文件按修改时间从旧到新合并分析
        levels: 只统计这些级别，默认 ["ERROR", "FAIL"]
        include: 包含关键词（子串）
        exclude: 排除关键词（子串）
        context_lines: 每个错误保留的上下文行数，默认 50
        similarity: 聚类并簇激进度 strict(0.95) / standard(0.85) / lenient(0.70)
        analysis_mode: full 完整 / deep 深度扫描（门槛低）/ fast 快速聚类
        rule: 解析规则 auto / generic / embedded / jenkins
        top_n: 返回前 N 个错误簇
        encoding: auto / utf-8 / gb18030 / utf-16
        max_lines: 只分析前 N 行（超大文件提速）
    """
    params = {
        "levels": levels, "include": include, "exclude": exclude,
        "context_lines": context_lines, "similarity": similarity,
        "analysis_mode": analysis_mode, "rule": rule, "top_n": top_n,
        "encoding": encoding, "max_lines": max_lines,
    }
    try:
        result = S.analyze(paths=paths, params=params)
    except S.ServiceError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:                          # noqa: BLE001
        return {"ok": False, "error": _err(exc)}
    return {"ok": True, "result": S.result_to_dict(result, top_n=top_n,
                                                    with_samples=False)}


@server.tool(
    name="analyze_log_text",
    title="分析日志文本",
    description="直接分析一段日志文本（用户粘贴的内容、命令输出、Agent 读到的片段）。",
    annotations=READONLY,
)
def analyze_log_text(
    text: str,
    source: str = "<mcp-text>",
    levels: Optional[List[str]] = None,
    context_lines: int = 50,
    similarity: str = "standard",
    analysis_mode: str = "full",
    rule: str = "auto",
    top_n: int = 20,
) -> Dict[str, Any]:
    """对粘贴文本做与文件相同的分析。

    Args:
        text: 日志文本
        source: 来源标记，会出现在报告标题里
        levels: 只统计这些级别
        context_lines: 上下文行数
        similarity: 聚类激进度
        analysis_mode: 完整 / 深度 / 快速
        rule: 解析规则
        top_n: 返回前 N 个错误簇
    """
    params = {
        "levels": levels, "context_lines": context_lines, "similarity": similarity,
        "analysis_mode": analysis_mode, "rule": rule, "top_n": top_n,
    }
    try:
        result = S.analyze(text=text, params={**params, "source": source})
    except S.ServiceError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:                          # noqa: BLE001
        return {"ok": False, "error": _err(exc)}
    return {"ok": True, "result": S.result_to_dict(result, top_n=top_n,
                                                    with_samples=False)}


@server.tool(
    name="compare_log_files",
    title="多文件对比",
    description=(
        "对比 2~5 个日志文件，列出相对基准文件的「新增 / 消失 / 共同」错误，"
        "适配版本回归验证与修复前后对比。"
    ),
    annotations=READONLY,
)
def compare_log_files(paths: List[str], levels: Optional[List[str]] = None,
                      include: Optional[List[str]] = None,
                      exclude: Optional[List[str]] = None,
                      context_lines: int = 50,
                      rule: str = "auto") -> Dict[str, Any]:
    """对比多个日志文件。

    Args:
        paths: 日志文件路径，**第一个为基准**
        levels: 只统计这些级别
        include: 包含关键词
        exclude: 排除关键词
        context_lines: 上下文行数
        rule: 解析规则
    """
    params = {"levels": levels, "include": include, "exclude": exclude,
              "context_lines": context_lines, "rule": rule}
    try:
        results = S.compare(paths, params)
    except S.ServiceError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:                          # noqa: BLE001
        return {"ok": False, "error": _err(exc)}
    return {
        "ok": True,
        "compare": S.compare_to_dicts(results),
        "markdown": S.compare_to_markdown(results),
    }


@server.tool(
    name="export_report",
    title="导出分析报告",
    description=(
        "把指定日志文件导出为压缩报告。Markdown 格式专为投喂 LLM 优化"
        "（概览 + Top N 清单 + 典型样例详情），通常比原始日志小 50~500 倍。"
    ),
    annotations=READONLY,
)
def export_report(
    paths: List[str],
    format: str = "md",
    levels: Optional[List[str]] = None,
    top_n: int = 20,
    context_lines: int = 20,
    similarity: str = "standard",
    analysis_mode: str = "full",
    rule: str = "auto",
    redact: bool = False,
) -> Dict[str, Any]:
    """导出报告文本。

    Args:
        paths: 日志文件路径
        format: md / json / txt / html / summary / json_full
        levels: 只统计这些级别
        top_n: Top N 错误
        context_lines: 上下文行数（MCP 场景建议 10~20，控制 token）
        similarity: 聚类激进度
        analysis_mode: 完整 / 深度 / 快速
        rule: 解析规则
        redact: 是否脱敏（邮箱/手机号/身份证/密钥）
    """
    params = {
        "levels": levels, "top_n": top_n, "context_lines": context_lines,
        "similarity": similarity, "analysis_mode": analysis_mode, "rule": rule,
    }
    try:
        result = S.analyze(paths=paths, params=params)
        text = S.export_text(result, format, top_n=top_n, redact=redact)
    except S.ServiceError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:                          # noqa: BLE001
        return {"ok": False, "error": _err(exc)}
    return {"ok": True, "format": format, "length": len(text), "content": text}


@server.tool(
    name="get_cluster_detail",
    title="查看错误簇详情",
    description=(
        "对某个日志文件取单个错误簇的完整证据：典型样例原始行、前后上下文、"
        "降噪堆栈（业务帧高亮、系统库帧已折叠）、变量取值分布、全部实例。"
    ),
    annotations=READONLY,
)
def get_cluster_detail(
    path: str,
    cluster_id: int,
    levels: Optional[List[str]] = None,
    context_lines: int = 50,
    similarity: str = "standard",
    analysis_mode: str = "full",
    rule: str = "auto",
) -> Dict[str, Any]:
    """取单个错误簇的深度证据。

    Args:
        path: 日志文件路径
        cluster_id: 簇号（先调 analyze_log_file 拿到）
        levels: 只统计这些级别
        context_lines: 上下文行数
        similarity: 聚类激进度
        analysis_mode: 完整 / 深度 / 快速
        rule: 解析规则
    """
    params = {"levels": levels, "context_lines": context_lines,
              "similarity": similarity, "analysis_mode": analysis_mode,
              "rule": rule}
    try:
        result = S.analyze(paths=[path], params=params)
        target = next((c for c in result.clusters
                       if c.cluster_id == cluster_id), None)
        if target is None:
            return {"ok": False,
                    "error": f"簇 {cluster_id} 不存在；当前文件的簇号有："
                             f"{[c.cluster_id for c in result.clusters][:20]}"}
        from log_ai_compressor.core.analysis import cooccurring_clusters
        detail = S.cluster_to_dict(target, full=True)
        detail["cooccurring"] = [
            {"id": o.cluster_id, "hits": h, "summary": o.summary}
            for o, h in cooccurring_clusters(target, result.clusters)
        ]
        return {"ok": True, "cluster": detail}
    except S.ServiceError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:                          # noqa: BLE001
        return {"ok": False, "error": _err(exc)}


@server.tool(
    name="list_rules",
    title="列出内置解析规则",
    description="列出内置的日志解析规则模板及其适用场景。日志格式没被正确解析时先看这里。",
    annotations=READONLY,
)
def list_rules() -> Dict[str, Any]:
    """列出内置解析规则。"""
    from log_ai_compressor.rules.engine import list_presets, load_ruleset
    out = []
    for name in list_presets():
        try:
            rs = load_ruleset(name)
            out.append({
                "name": name,
                "description": rs.description,
                "patterns": len(rs.patterns),
                "stack_indicators": len(rs.stack_indicators),
            })
        except Exception as exc:                      # noqa: BLE001
            out.append({"name": name, "error": str(exc)})
    return {"ok": True, "rules": out,
            "note": "日志格式特殊时可在项目里加自己的 YAML 规则，"
                    "用 rule 参数传文件路径即可。"}


@server.tool(
    name="check_environment",
    title="检查运行环境",
    description="确认工具是否可用、内置规则、AI 解读是否已配置。不传参数即可自检。",
    annotations=READONLY,
)
def check_environment() -> Dict[str, Any]:
    """自检：环境、版本、规则、AI 状态。

    httpx 是可选依赖（pyproject 的 [ai] extra），没装时如实报告
    「未安装」而不是让整个自检工具失败 —— 其余字段仍然有用。
    """
    try:
        from log_ai_compressor.ai import describe_config
        ai_status = describe_config()
    except ImportError as exc:
        ai_status = {"available": False, "reason": f"未安装可选依赖 httpx（{exc}）"}
    return {
        "ok": True,
        "app": __app_name__,
        "version": __version__,
        "python": os.sys.version.split()[0],
        "data_egress": "无 —— 所有分析在本机完成，日志不出网",
        "rules": list_rules()["rules"],
        "ai": ai_status,
    }


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main(transport: str = "stdio", port: int = 8766) -> None:
    """启动 MCP 服务器。

    Args:
        transport: stdio（Claude Code / Codex 等本地客户端用）
                   或 streamable-http（远程客户端用，默认只监听 127.0.0.1）
        port: http 传输时的端口
    """
    if transport == "stdio":
        server.run("stdio")
    else:
        # 只绑回环：MCP 暴露的是本机文件分析能力，不该暴露到局域网
        server.settings.host = "127.0.0.1"
        server.settings.port = port
        server.run(transport)


if __name__ == "__main__":                            # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="日志AI压缩器 MCP 服务器")
    ap.add_argument("--transport", default="stdio",
                    choices=["stdio", "streamable-http", "sse"])
    ap.add_argument("--port", type=int, default=8766)
    args = ap.parse_args()
    main(args.transport, args.port)
