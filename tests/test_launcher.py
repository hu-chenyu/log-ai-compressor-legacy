# -*- coding: utf-8 -*-
"""双击启动脚本测试：编码、行尾、关键逻辑。

唯一入口是 ``start.bat`` —— 起本地 Web 服务并自动开浏览器。

**为什么强制纯 ASCII + CRLF**（这条被测试反复咬住，别改回去）
----------------------------------------------------------
cmd.exe 逐字节按控制台代码页解析批处理文件。踩中任一条都会静默失败：
1. 用了裸 LF 行尾 → cmd 解析错位，每行开头被吞（``%PY% start.py`` 变
   ``art.py``，报一堆 ``'xxx' is not recognized``，程序永远起不来）；
2. 含与控制台代码页不匹配的非 ASCII 字节 → 同样错位，且中文提示变乱码。
把中文放进 Web 界面层，bat 里只留 ASCII，才能在任何系统代码页下工作。
"""
from __future__ import annotations

from pathlib import Path

import pytest

START_BAT = Path(__file__).resolve().parent.parent / "start.bat"


def _read_bat(path: Path) -> str:
    """读取 bat 内容并校验编码/行尾硬约束。"""
    if not path.exists():
        pytest.skip(f"{path.name} 不存在")
    raw = path.read_bytes()
    # cmd 逐行解析要求 CRLF：不允许存在裸 LF
    lf = raw.count(b"\n")
    crlf = raw.count(b"\r\n")
    assert lf == crlf, (
        f"{path.name} 必须使用 CRLF 行尾（当前 {lf} 个 LF / {crlf} 个 CRLF）")
    # 纯 ASCII：任何非 ASCII 字节都可能与控制台代码页错位
    non_ascii = [i for i, b in enumerate(raw) if b > 127]
    assert not non_ascii, (
        f"{path.name} 含 {len(non_ascii)} 个非 ASCII 字节（首个在偏移 "
        f"{non_ascii[0]}）—— cmd 会与控制台代码页错位解析")
    return raw.decode("ascii")     # 纯 ASCII 必然也能被 gbk 解码


@pytest.fixture(scope="module")
def start_content() -> str:
    """v2 主启动器内容。"""
    return _read_bat(START_BAT)


# ---------------------------------------------------------------------------
# v2 主启动器 start.bat
# ---------------------------------------------------------------------------
class TestStartBat:
    def test_echo_off_and_no_chcp_utf8(self, start_content):
        assert start_content.splitlines()[0].strip().lower() == "@echo off"
        # chcp 65001 会让 cmd 逐行解析更糟，不能加
        assert "chcp 65001" not in start_content

    def test_cd_to_script_dir(self, start_content):
        assert 'cd /d "%~dp0"' in start_content

    def test_python_fallback_detection(self, start_content):
        assert 'set "PY=python"' in start_content
        assert "py -3" in start_content
        assert 'python -c "import sys"' in start_content

    def test_dependencies_checked_and_installed(self, start_content):
        assert "import fastapi" in start_content
        assert "pip install fastapi" in start_content

    def test_launches_web_subcommand(self, start_content):
        assert "-m log_ai_compressor web" in start_content

    def test_failure_shows_pause(self, start_content):
        assert start_content.count("pause") >= 2


# ---------------------------------------------------------------------------
# CLI 参数默认值与常量一致性
# ---------------------------------------------------------------------------
class TestCliDefaults:
    def test_run_context_default_50(self):
        """CLI --context 默认值必须与全局常量（50）一致。"""
        from log_ai_compressor.cli import build_parser
        args = build_parser().parse_args(["run", "app.log"])
        assert args.context == 50

    def test_context_max_clamped_by_pipeline(self, tmp_path):
        """修复缺陷R20：--context 无上限（9999 原样保留，不再钳到 200）。"""
        from log_ai_compressor.core.filters import FilterConfig
        cfg = FilterConfig.from_dict({"context_lines": 9999})
        assert cfg.context_lines == 9999

    def test_cli_context_matches_constant(self):
        """CLI 的 --context 默认值必须同源 DEFAULT_CONTEXT_LINES，不能各写一份。"""
        from log_ai_compressor.cli import build_parser
        from log_ai_compressor.constants import DEFAULT_CONTEXT_LINES
        args = build_parser().parse_args(["run", "app.log"])
        assert args.context == DEFAULT_CONTEXT_LINES
