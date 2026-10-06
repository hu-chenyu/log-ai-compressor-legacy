# -*- coding: utf-8 -*-
"""可选依赖契约测试。

pyproject 把 web / mcp / ai 拆成了 extras，硬依赖只有 PyYAML
（core / rules / export 三层零三方依赖）。这是 PyPI 上最重要的
分发属性：**裸装包必须能跑核心引擎，且缺可选组件时是友好提示，
不是裸 ImportError 堆栈。**

用子进程跑，模拟真实的「没装 extra」环境。
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent

GUARD = textwrap.dedent("""
    import builtins
    blocked = set(%(blocked)r)
    real = builtins.__import__
    def guard(name, *a, **k):
        if name.split(".")[0] in blocked:
            raise ImportError("No module named %%r" %% name.split(".")[0])
        return real(name, *a, **k)
    builtins.__import__ = guard
""")

CORE_MODULES = ["log_ai_compressor.core.pipeline",
                "log_ai_compressor.core.clustering",
                "log_ai_compressor.core.analysis",
                "log_ai_compressor.export.reporters",
                "log_ai_compressor.rules.engine",
                "log_ai_compressor.service",
                "log_ai_compressor.cli"]


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=str(BASE), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120,
    )


# ---------------------------------------------------------------------------
# 打包元数据
# ---------------------------------------------------------------------------
class TestPackaging:
    @pytest.fixture(scope="class")
    def pyproject(self):
        try:
            import tomllib
        except ModuleNotFoundError:                 # Python 3.10 及以下
            pytest.skip("需要 tomllib（Python 3.11+）")
        return tomllib.loads(
            (BASE / "pyproject.toml").read_text(encoding="utf-8"))

    def test_core_has_single_dependency(self, pyproject):
        """裸装包只应拉 PyYAML —— 30+ 个传递依赖会直接劝退 PyPI 用户。"""
        deps = pyproject["project"]["dependencies"]
        assert deps == ["PyYAML>=6.0"], f"核心硬依赖应只有 PyYAML，实际 {deps}"

    def test_optional_extras_present(self, pyproject):
        extras = pyproject["project"]["optional-dependencies"]
        for name in ("web", "mcp", "ai", "all", "legacy", "dev"):
            assert name in extras, f"缺少 extra: {name}"

    def test_extras_are_not_pinned_to_local_paths(self, pyproject):
        """自引用的 extra（log-ai-compressor[...]）在某些构建器上会解析失败，
        至少要保证不是硬依赖、且拼写正确。"""
        extras = pyproject["project"]["optional-dependencies"]
        for name, items in extras.items():
            for item in items:
                assert "log-ai-compressor[" in item or "log_ai_compressor[" not in item
                assert "\n" not in item and " " not in item.strip() or \
                    item.startswith(("log-ai-compressor[", "customtkinter", "matplotlib",
                                     "tkinterdnd2", "pytest", "ruff"))

    def test_static_assets_shipped(self, pyproject):
        """前端是零构建静态资源，不打进包的话 pip 安装后界面直接 404。"""
        data = pyproject["tool"]["setuptools"]["package-data"]["log_ai_compressor.web"]
        for ext in ("html", "css", "js"):
            assert any(ext in pattern for pattern in data), \
                f"package-data 漏了 *.{ext}"


# ---------------------------------------------------------------------------
# 缺可选依赖时的行为
# ---------------------------------------------------------------------------
class TestGracefulDegradation:
    def test_core_engine_imports_without_extras(self):
        """屏蔽 web/mcp/ai 依赖后，核心引擎必须照常可用。"""
        r = _run(GUARD % {"blocked": ["fastapi", "uvicorn", "mcp", "httpx",
                                     "sse_starlette"]} + textwrap.dedent("""
            import importlib
            for mod in %(mods)r:
                importlib.import_module(mod)
            print("CORE_OK")
        """) % {"mods": CORE_MODULES})
        assert "CORE_OK" in r.stdout, \
            f"核心引擎在无 extras 环境下导入失败:\n{r.stdout}\n{r.stderr}"

    def test_core_engine_analyzes_without_extras(self):
        """无 extras 环境下核心分析必须真能跑出结果（不只是能 import）。"""
        r = _run(GUARD % {"blocked": ["fastapi", "uvicorn", "mcp", "httpx",
                                     "sse_starlette"]} + textwrap.dedent("""
            from log_ai_compressor import service as S
            r = S.analyze(paths=["examples/sample_system.log"])
            assert r.clusters, "没有聚类结果"
            assert len(S.export_text(r, "md")) > 200
            print("ANALYZE_OK", len(r.clusters))
        """))
        assert "ANALYZE_OK" in r.stdout, r.stdout + r.stderr

    @pytest.mark.parametrize("argv", [
        ["web"], ["mcp"], ["ai", "status"], ["ai", "test"],
        ["ai", "explain", "nonexistent.log"],
    ])
    def test_cli_no_traceback(self, argv):
        """缺依赖时 CLI 要给可执行的提示，不能抛裸 ImportError 堆栈。"""
        r = _run(GUARD % {"blocked": ["fastapi", "uvicorn", "mcp", "httpx",
                                     "sse_starlette"]} + textwrap.dedent("""
            from log_ai_compressor.cli import main
            main(%(argv)r)
        """) % {"argv": argv})
        combined = r.stdout + r.stderr
        assert "Traceback" not in combined, f"{argv} 抛了裸堆栈:\n{combined}"
        assert "ImportError" not in combined, \
            f"{argv} 漏出了 ImportError（应转成友好提示）:\n{combined}"

    def test_cli_suggests_the_right_extra(self):
        """提示必须告诉用户装哪个 extra。"""
        r = _run(GUARD % {"blocked": ["httpx"]} + textwrap.dedent("""
            from log_ai_compressor.cli import main
            main(["ai", "status"])
        """))
        combined = r.stdout + r.stderr
        assert "log-ai-compressor[ai]" in combined, \
            f"没提示正确的 extra:\n{combined}"

    def test_mcp_selfcheck_survives_missing_ai(self):
        """MCP 自检工具不能因为 AI 层缺依赖就整个失败。"""
        r = _run(GUARD % {"blocked": ["httpx"]} + textwrap.dedent("""
            import asyncio, json
            from log_ai_compressor.mcp.server import server
            res = asyncio.run(server.call_tool("check_environment", {}))
            parts = getattr(res, "content", None) or []
            d = json.loads(parts[0].text)
            assert d["ok"] is True
            assert "ai" in d and d["ai"]["available"] is False
            print("MCP_OK")
        """))
        assert "MCP_OK" in r.stdout, r.stdout + r.stderr

    def test_web_health_survives_missing_ai(self):
        """Web 健康检查同理：AI 不可用不能让整个服务 500。"""
        r = _run(GUARD % {"blocked": ["httpx"]} + textwrap.dedent("""
            from fastapi.testclient import TestClient
            from log_ai_compressor.web.server import create_app
            c = TestClient(create_app())
            body = c.get("/api/health").json()
            assert body["ok"] is True
            assert body["ai"]["available"] is False
            print("WEB_OK")
        """))
        assert "WEB_OK" in r.stdout, r.stdout + r.stderr
