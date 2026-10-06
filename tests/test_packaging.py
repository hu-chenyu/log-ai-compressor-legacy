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
        for name in ("web", "mcp", "ai", "all", "dev"):
            assert name in extras, f"缺少 extra: {name}"

    def test_extras_have_no_local_paths(self, pyproject):
        """extras 里的依赖必须是可发布的包名，不能指向本机路径。

        （旧版本这里写的是恒真断言 `A or not A`，等于没测；后来 mcp 那项
        加上 `; python_version >= '3.10'` 环境标记后被误伤，才暴露出来。）
        """
        extras = pyproject["project"]["optional-dependencies"]
        offenders = []
        for name, items in extras.items():
            for item in items:
                low = item.lower()
                if low.startswith(("file:", "./", "../", ".\\", "..\\")) or \
                        (":" in item and "\\" in item) or "://" in low:
                    offenders.append(f"{name}: {item}")
                if item.startswith("log-ai-compressor["):
                    # 自引用 extra 允许，但名字要与 [project].name 一致
                    assert item.startswith("log-ai-compressor[")
        assert not offenders, f"extras 里出现了本机路径：{offenders}"

    def test_mcp_extra_is_version_gated(self, pyproject):
        """mcp SDK 要求 Python >= 3.10，必须带环境标记。

        否则 Python 3.9 的 CI job 会在 pip 阶段直接失败
        （No matching distribution found for mcp>=1.0）。
        """
        mcp_items = pyproject["project"]["optional-dependencies"]["mcp"]
        assert mcp_items, "mcp extra 不能为空"
        for item in mcp_items:
            assert "python_version" in item, (
                f"mcp 依赖缺环境标记，3.9 上会装不上：{item}")
            assert "3.10" in item, f"环境标记版本不对：{item}"

    def test_static_assets_shipped(self, pyproject):
        """前端是零构建静态资源，不打进包的话 pip 安装后界面直接 404。"""
        data = pyproject["tool"]["setuptools"]["package-data"]["log_ai_compressor.web"]
        for ext in ("html", "css", "js"):
            assert any(ext in pattern for pattern in data), \
                f"package-data 漏了 *.{ext}"


# ---------------------------------------------------------------------------
# requirements 文件必须是合法的 pip 文件
# ---------------------------------------------------------------------------
class TestRequirementsFiles:
    """回归：requirements.txt 曾被写成「命令速查表」。

    每行是 `pip install log-ai-compressor` 这种 shell 命令，于是
    `pip install -r requirements.txt`（CI 第一步）报
    `Invalid requirement: 'pip install log-ai-compressor'`，
    三个 job 全挂在 Install dependencies，后面 Lint / Test 全被 skipped。
    这里逐行按 PEP 508 解析，把这个坑钉死。
    """

    #: 会被 pip 执行的 requirements 文件
    EXECUTED = ("requirements.txt", "requirements-dev.txt")

    @staticmethod
    def _spec_lines(name: str):
        """只返回会被 pip 真正解析的行（跳过空行与注释）。"""
        text = (BASE / name).read_text(encoding="utf-8")
        for i, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            yield i, line

    @staticmethod
    def _is_option(line: str) -> bool:
        """`-r file` / `-e .[a,b]` / `--index-url ...` 这类 pip 选项。"""
        return line.startswith("-")

    @pytest.mark.parametrize("name", EXECUTED)
    def test_every_line_is_a_valid_requirement(self, name):
        from packaging.requirements import InvalidRequirement, Requirement
        for lineno, line in self._spec_lines(name):
            if self._is_option(line):
                continue
            try:
                Requirement(line)
            except InvalidRequirement as exc:
                pytest.fail(
                    f"{name} 第 {lineno} 行不是合法的 requirement：{line!r}\n"
                    f"  原因：{exc}\n"
                    f"  requirements*.txt 只能写包名/URL，不能写 shell 命令。\n"
                    f"  安装速查表请放 requirements-optional.txt 或 README。")

    @pytest.mark.parametrize("name", EXECUTED)
    def test_no_shell_commands(self, name):
        for lineno, line in self._spec_lines(name):
            if self._is_option(line):
                continue
            # 单个 requirement 里允许出现空格吗？不允许 —— 合法 specifier
            # 形如 `pkg>=1.0` / `pkg[extra]>=1.0`，都不含空格。
            assert " " not in line, (
                f"{name} 第 {lineno} 行含空格，像 shell 命令：{line!r}\n"
                f"  requirements*.txt 只接受单行 requirement。")
            assert not line.startswith("pip "), (
                f"{name} 第 {lineno} 行是 shell 命令：{line!r}")

    def test_cheatsheet_is_separate(self):
        """安装速查表必须放在不会被执行的文件里。"""
        assert (BASE / "requirements-optional.txt").is_file()
        text = (BASE / "requirements-optional.txt").read_text(encoding="utf-8")
        assert "pip install" in text, "速查表应含 pip install 示例"

    def test_dev_requirements_install_the_package_extras(self):
        """CI 靠这个文件装 web/mcp/ai，否则接入层测试会被 skip 或收集失败。"""
        spec = [line for _, line in self._spec_lines("requirements-dev.txt")]
        assert any("-e .[" in line for line in spec), (
            "requirements-dev.txt 必须装本项目本体（带 extras），"
            "否则 tests/test_web.py 等会被跳过")
        joined = "\n".join(spec).lower()
        for tool in ("pytest", "pytest-cov", "ruff"):
            assert tool in joined, f"缺少开发工具：{tool}"


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
        """MCP 自检工具不能因为 AI 层缺依赖就整个失败。

        需要 mcp SDK 本身可用（Python 3.9 上它装不上，tests/test_mcp.py
        已整体 importorskip，这里也必须同步跳过，否则是在测一个跑不了的
        路径 —— run #159 就因此红过）。
        """
        pytest.importorskip("mcp", reason="mcp SDK 不可用（Python < 3.10）")
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

    def test_ai_status_degrades_without_httpx(self):
        """AI 层缺 httpx 时，健康检查用的 _ai_status() 必须优雅降级。

        这里**不走 TestClient**：starlette 的 TestClient 自己就 import httpx，
        屏蔽掉它等于砸自己的脚（run #159 的第二个失败）。web.server 只在
        懒导入时才碰 ai.client，所以直接调 _ai_status 才是真正的被测面。
        """
        r = _run(GUARD % {"blocked": ["httpx"]} + textwrap.dedent("""
            from log_ai_compressor.web.server import _ai_status
            st = _ai_status()
            assert st["available"] is False
            assert st["reason"]
            print("WEB_OK")
        """))
        assert "WEB_OK" in r.stdout, r.stdout + r.stderr
