# -*- coding: utf-8 -*-
"""CLI 命令行入口：支持脚本 / 流水线自动化调用。

用法示例
--------
    # 分析单个日志并导出 Markdown 报告
    log-ai-compressor run test.log --top 20 --level ERROR,FAIL -o report.md

    # JSON 格式导出
    log-ai-compressor run test.log --format json -o report.json

    # 多文件对比（版本对比 / 修复前后对比）
    log-ai-compressor compare v1.log v2.log -o diff.md

    # 查看可用解析规则模板
    log-ai-compressor rules list

    # 启动 GUI
    log-ai-compressor gui
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import List, Optional

from log_ai_compressor import __version__
from log_ai_compressor.constants import (
    DEFAULT_CONTEXT_LINES,
    DEFAULT_TOP_N,
    LEVEL_ORDER,
)
from log_ai_compressor.core.comparator import compare_files
from log_ai_compressor.core.pipeline import analyze_file
from log_ai_compressor.export.reporters import (
    brief_summary,
    compare_to_markdown,
    to_json,
    to_markdown,
    to_text,
)

# 导出格式 -> 生成函数
_FORMAT_TABLE = {"md": to_markdown, "markdown": to_markdown,
                 "json": to_json, "txt": to_text, "text": to_text}
_SUFFIX_FORMAT = {".md": "md", ".markdown": "md", ".json": "json",
                  ".txt": "txt", ".text": "txt"}

EXIT_OK, EXIT_ERROR, EXIT_CANCELLED = 0, 1, 130


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------
def _add_filter_options(p: argparse.ArgumentParser) -> None:
    """公共过滤参数（run / compare 共用）。"""
    # 修复缺陷R40：默认级别 ERROR+FAIL（FATAL 已删除归一 ERROR；
    # --level FATAL 仍兼容 —— 归一映射到 ERROR）
    p.add_argument("--level", "-l", default="ERROR,FAIL",
                   help="级别过滤（逗号分隔，默认 ERROR,FAIL；"
                        "可用值 ERROR/FAIL/WARN/INFO/DEBUG/TRACE）")
    p.add_argument("--include", "-k", default="",
                   help="包含关键字（逗号分隔，任一命中保留）")
    p.add_argument("--exclude", default="",
                   help="排除关键字（逗号分隔，任一命中剔除）")
    p.add_argument("--top", "-t", type=int, default=DEFAULT_TOP_N,
                   help=f"Top N 错误数（默认 {DEFAULT_TOP_N}）")
    p.add_argument("--context", type=int, default=DEFAULT_CONTEXT_LINES,
                   help=f"典型样例上下文行数（默认 {DEFAULT_CONTEXT_LINES}，"
                        "≥0 不限上限）")
    p.add_argument("--rule", "-r", default=None,
                   help="解析规则：模板名(generic/embedded/jenkins)或 YAML 路径")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="log-ai-compressor",
        description="日志AI压缩器：海量日志压缩投喂大模型 / 快速故障排查",
    )
    parser.add_argument("--version", action="version",
                        version=f"log-ai-compressor v{__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # run 子命令
    p_run = sub.add_parser("run", help="分析单个日志文件")
    p_run.add_argument("file", help="日志文件路径")
    _add_filter_options(p_run)
    p_run.add_argument("-o", "--output", default=None,
                       help="输出报告路径（默认 <日志名>_report.md）")
    p_run.add_argument("--format", "-f", default=None,
                       choices=sorted(_FORMAT_TABLE),
                       help="输出格式（默认按输出后缀推断，否则 md）")
    p_run.add_argument("--no-analysis", action="store_true",
                       help="跳过智能分析（根因/异常/优先级）")
    p_run.add_argument("--quiet", "-q", action="store_true",
                       help="抑制进度输出")
    p_run.set_defaults(func=cmd_run)

    # compare 子命令
    p_cmp = sub.add_parser("compare", help="多文件对比分析（2~3 个文件）")
    p_cmp.add_argument("files", nargs="+", help="日志文件（第一个为基准）")
    _add_filter_options(p_cmp)
    p_cmp.add_argument("-o", "--output", default=None,
                       help="输出报告路径（默认 compare_report.md）")
    p_cmp.set_defaults(func=cmd_compare)

    # rules 子命令
    p_rules = sub.add_parser("rules", help="查看解析规则模板")
    p_rules.add_argument("action", nargs="?", default="list",
                         choices=["list", "show"],
                         help="list 列出模板 / show 查看模板内容")
    p_rules.add_argument("name", nargs="?", default=None, help="模板名")
    p_rules.set_defaults(func=cmd_rules)

    # web 子命令（主界面）
    p_web = sub.add_parser("web", help="启动本地 Web 界面（默认，双击启动器用的就是它）")
    p_web.add_argument("--host", default="127.0.0.1",
                       help="绑定地址（默认仅本机；改 0.0.0.0 会把日志分析能力暴露到局域网，不建议）")
    p_web.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")
    p_web.add_argument("--no-browser", action="store_true",
                       help="启动后不自动打开浏览器")
    p_web.add_argument("--log-level", default="warning",
                       choices=["critical", "error", "warning", "info", "debug"])
    p_web.set_defaults(func=cmd_web)

    # mcp 子命令
    p_mcp = sub.add_parser("mcp", help="启动 MCP 服务器（供 Claude Code / Codex 等调用）")
    p_mcp.add_argument("--transport", default="stdio",
                       choices=["stdio", "streamable-http", "sse"],
                       help="stdio=本地 Agent；streamable-http=远程（仅本机）")
    p_mcp.add_argument("--port", type=int, default=8766, help="http 传输端口")
    p_mcp.add_argument("--install", metavar="CLIENT", nargs="?", const="claude-code",
                       help="打印指定客户端的 MCP 配置片段后退出")
    p_mcp.set_defaults(func=cmd_mcp)

    # ai 子命令
    p_ai = sub.add_parser("ai", help="AI 解读（可选功能，不配置也能用其他全部能力）")
    ai_sub = p_ai.add_subparsers(dest="ai_action", required=True)
    p_ai_status = ai_sub.add_parser("status", help="查看 AI 配置状态")
    p_ai_status.set_defaults(func=cmd_ai_status)
    p_ai_set = ai_sub.add_parser("config", help="配置 AI 服务商")
    p_ai_set.add_argument("--provider", required=True,
                          help="none/deepseek/qwen/glm/kimi/openai/ollama/custom")
    p_ai_set.add_argument("--base-url", default="", help="Base URL（默认取服务商预设）")
    p_ai_set.add_argument("--model", default="", help="模型名（默认取服务商预设）")
    p_ai_set.add_argument("--key", default="", help="API Key（可留空，改用环境变量）")
    p_ai_set.set_defaults(func=cmd_ai_config)
    p_ai_test = ai_sub.add_parser("test", help="发一个最小请求验证连通性")
    p_ai_test.set_defaults(func=cmd_ai_test)
    p_ai_explain = ai_sub.add_parser("explain", help="对日志文件生成 AI 解读")
    p_ai_explain.add_argument("file", help="日志文件路径")
    p_ai_explain.add_argument("--cluster", type=int, default=None,
                              help="只解读指定簇号（默认整份结果）")
    p_ai_explain.add_argument("-q", "--question", default="",
                              help="额外追问（可选）")
    # --top / --level / --context / --rule 等由 _add_filter_options 统一提供
    _add_filter_options(p_ai_explain)
    p_ai_explain.set_defaults(func=cmd_ai_explain)
    return parser


# ---------------------------------------------------------------------------
# 进度显示
# ---------------------------------------------------------------------------
class _ConsoleProgress:
    """终端进度显示（仅 TTY 下逐行刷新，避免污染管道输出）。"""

    def __init__(self, enabled: bool):
        self._enabled = enabled and sys.stderr.isatty()
        self._t0 = time.time()

    def __call__(self, data: dict) -> None:
        if not self._enabled:
            return
        if data.get("phase") == "done":
            sys.stderr.write("\n")
            return
        sys.stderr.write(
            f"\r已处理 {data['lines']:>10,} 行 | "
            f"{data['lps']:>8,.0f} 行/秒 | 错误种类 {data['clusters']:>4} | "
            f"耗时 {data['elapsed']:>5.1f}s")
        sys.stderr.flush()


def _parse_keywords(raw: str) -> List[str]:
    return [k.strip() for k in raw.split(",") if k and k.strip()] if raw else []


def _parse_levels(raw: str) -> List[str]:
    levels = [lv.strip().upper() for lv in raw.split(",") if lv.strip()]
    valid = [lv for lv in levels if lv in LEVEL_ORDER]
    return valid if valid else ["ERROR", "FAIL"]


def _resolve_format(args) -> str:
    if args.format:
        return args.format
    if args.output:
        return _SUFFIX_FORMAT.get(Path(args.output).suffix.lower(), "md")
    return "md"


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------
def cmd_run(args) -> int:
    path = Path(args.file)
    if not path.is_file():
        print(f"错误：日志文件不存在 {path}", file=sys.stderr)
        return EXIT_ERROR

    progress = _ConsoleProgress(enabled=not args.quiet)
    try:
        result = analyze_file(
            path,
            levels=_parse_levels(args.level),
            include=_parse_keywords(args.include),
            exclude=_parse_keywords(args.exclude),
            top_n=args.top,
            context_lines=args.context,
            rule=args.rule,
            analyze=not args.no_analysis,
            progress_cb=progress,
        )
    except FileNotFoundError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_ERROR

    # 摘要输出到 stdout（可直接管道组合）
    print(brief_summary(result, top_n=args.top))

    # 报告落盘
    output = Path(args.output) if args.output else \
        path.with_name(f"{path.stem}_report.{_resolve_format(args)}")
    fmt = _resolve_format(args)
    content = _FORMAT_TABLE[fmt](result, top_n=args.top)
    output.write_text(content, encoding="utf-8")

    size_kb = output.stat().st_size / 1024
    print(f"报告已导出: {output}（{fmt.upper()}，{size_kb:.1f} KB）")
    if result.stats.truncated:
        print("注意：处理被取消，报告基于已完成的增量结果。", file=sys.stderr)
        return EXIT_CANCELLED
    return EXIT_OK


def cmd_compare(args) -> int:
    paths = [Path(f) for f in args.files]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        print(f"错误：文件不存在 {' '.join(missing)}", file=sys.stderr)
        return EXIT_ERROR
    if len(paths) < 2:
        print("错误：对比分析至少需要 2 个日志文件", file=sys.stderr)
        return EXIT_ERROR
    if len(paths) > 3:
        print("错误：最多支持 3 个日志文件对比", file=sys.stderr)
        return EXIT_ERROR

    try:
        results = compare_files(
            paths,
            levels=_parse_levels(args.level),
            include=_parse_keywords(args.include),
            exclude=_parse_keywords(args.exclude),
            top_n=args.top,
            context_lines=args.context,
            rule=args.rule,
        )
    except Exception as exc:  # 规则文件错误等用户输入问题
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_ERROR

    content = compare_to_markdown(results)
    output = Path(args.output) if args.output else Path("compare_report.md")
    output.write_text(content, encoding="utf-8")
    print(f"对比报告已导出: {output}")
    return EXIT_OK


def cmd_rules(args) -> int:
    from log_ai_compressor.rules.engine import list_presets, load_ruleset

    if args.action == "list":
        print("可用解析规则模板：")
        for name in list_presets():
            try:
                rs = load_ruleset(name)
                print(f"  {name:<12} {rs.description}")
            except Exception:
                print(f"  {name:<12} (加载失败)")
        print("\n提示：--rule 支持传入自定义 YAML 规则文件路径")
    else:
        name = args.name or "generic"
        try:
            rs = load_ruleset(name)
        except Exception as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return EXIT_ERROR
        print(f"规则集：{rs.name}（来源 {rs.source}）")
        print(f"说明：{rs.description}")
        print(f"模式（{len(rs.patterns)} 条）：")
        for rule in rs.patterns:
            print(f"  - {rule.name}: {rule.pattern_text[:90]}")
        print(f"堆栈特征（{len(rs.stack_indicators)} 条）、"
              f"级别提示（{len(rs.level_hints)} 级）")
    return EXIT_OK


def cmd_web(args) -> int:
    """启动本地 Web 界面。"""
    try:
        from log_ai_compressor.web.server import serve
    except ImportError as exc:
        print(f"错误：Web 组件不可用（{exc}）。"
              f"请执行：pip install fastapi \"uvicorn[standard]\"", file=sys.stderr)
        return EXIT_ERROR
    if args.host not in ("127.0.0.1", "localhost"):
        print(f"警告：绑定 {args.host} 会让局域网内其他设备访问本机日志分析能力，"
              f"服务没有鉴权。确认这是你要的吗？", file=sys.stderr)
    try:
        serve(host=args.host, port=args.port,
              open_browser=not args.no_browser, log_level=args.log_level)
    except OSError as exc:
        print(f"错误：端口 {args.port} 启动失败（{exc}）。"
              f"换一个：--port 8766", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\n已停止")
    return EXIT_OK


# 各 AI 客户端的 MCP 配置片段（--install 打印用）
_MCP_CLIENTS = {
    "claude-code": (
        "Claude Code / Claude Desktop",
        'claude mcp add --scope user log-ai-compressor -- '
        'python -m log_ai_compressor.mcp.server',
    ),
    "codex": (
        "Codex",
        'codex mcp add log-ai-compressor -- '
        'python -m log_ai_compressor.mcp.server',
    ),
    "mavis": (
        "MiniMax Code / 其它支持 JSON 配置的客户端",
        '{\n'
        '  "mcpServers": {\n'
        '    "log-ai-compressor": {\n'
        '      "command": "python",\n'
        '      "args": ["-m", "log_ai_compressor.mcp.server"]\n'
        '    }\n'
        '  }\n'
        '}',
    ),
}


def cmd_mcp(args) -> int:
    """启动 MCP 服务器或打印客户端配置。"""
    if args.install:
        title, snippet = _MCP_CLIENTS.get(
            args.install,
            (args.install, _MCP_CLIENTS["mavis"][1]))
        print(f"# {title}\n{snippet}")
        return EXIT_OK
    try:
        from log_ai_compressor.mcp.server import main as mcp_main
    except ImportError as exc:
        print(f"错误：MCP 组件不可用（{exc}）。"
              f"请执行：pip install mcp", file=sys.stderr)
        return EXIT_ERROR
    if args.transport == "stdio":
        # stdio 传输下 stdout 是协议通道，任何 print 都会污染 JSON-RPC
        print("MCP 服务器以 stdio 模式启动（stdout 专用于协议，"
              "诊断信息走 stderr）", file=sys.stderr)
    else:
        print(f"MCP 服务器以 {args.transport} 启动："
              f"http://127.0.0.1:{args.port}", file=sys.stderr)
    mcp_main(args.transport, args.port)
    return EXIT_OK


def _import_ai():
    """导入 AI 层；缺可选依赖时给出可执行的提示而不是裸 ImportError。

    httpx 被拆成了 [ai] extra（见 pyproject），裸装包的用户会遇到它。
    """
    try:
        from log_ai_compressor import ai as ai_mod
        return ai_mod
    except ImportError as exc:
        print(f"错误：AI 解读需要额外的 httpx 依赖（{exc}）。\n"
              f"      安装：pip install \"log-ai-compressor[ai]\"\n"
              f"      不装也不影响其它功能 —— 聚类 / 根因判定 / 异常检测 / "
              f"报告导出都是本地算法。", file=sys.stderr)
        return None


def cmd_ai_status(args) -> int:
    """打印 AI 配置状态。"""
    ai = _import_ai()
    if ai is None:
        return EXIT_ERROR
    st = ai.describe_config()
    cfg = st.get("config", {})
    print(f"AI 解读：{'已启用' if st.get('available') else '未启用'}")
    if st.get("available"):
        print(f"  服务商：{cfg.get('provider')}   模型：{cfg.get('model')}")
        print(f"  地址：  {cfg.get('base_url')}")
        print(f"  Key：   {cfg.get('masked_key') or '（无需 Key）'}")
    else:
        print(f"  原因：{st.get('reason')}")
    print(f"\n配置文件：{st.get('config_file')}")
    print("\n可用服务商：")
    for p in st.get("providers", []):
        need = f"（环境变量 {p['env_key']}）" if p.get("env_key") else "（无需 Key）"
        print(f"  {p['key']:<10} {p['label']} {need}")
        if p.get("note"):
            print(f"             {p['note']}")
    print("\n提示：AI 解读是可选的。不配置也能正常使用聚类 / 根因判定 / "
          "异常检测 / 导出等全部本地能力。")
    return EXIT_OK


def cmd_ai_config(args) -> int:
    """保存 AI 服务商配置。"""
    ai = _import_ai()
    if ai is None:
        return EXIT_ERROR
    try:
        cfg = ai.load_config({
            "provider": args.provider,
            "base_url": args.base_url,
            "model": args.model,
            "api_key": args.key,
        })
        ai.save_config(cfg)
    except ai.LLMError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:                          # noqa: BLE001
        print(f"错误：保存失败 {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"已保存：{cfg.provider} / {cfg.model} / {cfg.base_url}")
    if not cfg.enabled:
        print("提示：当前配置尚未启用。"
              + ("（该服务商需要 API Key，用 --key 或环境变量提供）"
                 if cfg.provider != "ollama" else "（请确认 Ollama 已启动并已拉取模型）"))
    return EXIT_OK


def cmd_ai_test(args) -> int:
    """发一个最小请求验证连通性。"""
    ai = _import_ai()
    if ai is None:
        return EXIT_ERROR
    cfg = ai.load_config()
    if not cfg.enabled:
        print("AI 未启用，先执行 log-ai-compressor ai config --provider <名字>",
              file=sys.stderr)
        return EXIT_ERROR
    print(f"测试 {cfg.provider} / {cfg.model} @ {cfg.base_url} …")
    try:
        out = ai.chat("只回复两个字：可用", cfg=cfg)
    except ai.LLMError as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"成功，模型返回：{out[:80]}")
    return EXIT_OK


def cmd_ai_explain(args) -> int:
    """对日志文件生成 AI 解读。"""
    ai = _import_ai()
    if ai is None:
        return EXIT_ERROR
    from log_ai_compressor.core.pipeline import analyze_file
    path = Path(args.file)
    if not path.is_file():
        print(f"错误：日志文件不存在 {path}", file=sys.stderr)
        return EXIT_ERROR
    print("本地分析中…", file=sys.stderr)
    try:
        result = analyze_file(
            path,
            levels=_parse_levels(args.level),
            include=_parse_keywords(args.include),
            exclude=_parse_keywords(args.exclude),
            context_lines=args.context,
            rule=args.rule,
        )
    except Exception as exc:                          # noqa: BLE001
        print(f"错误：本地分析失败 {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"调用 AI 解读中…（{path.name}，"
          f"{len(result.clusters)} 个错误簇）", file=sys.stderr)
    try:
        if args.cluster is not None:
            text = ai.explain_cluster(result, args.cluster, question=args.question)
        else:
            text = ai.explain_result(result, top_n=args.top, question=args.question)
    except ai.LLMError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_ERROR
    print("\n" + "=" * 68)
    print(text)
    print("=" * 68)
    return EXIT_OK


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return EXIT_CANCELLED


if __name__ == "__main__":
    sys.exit(main())
