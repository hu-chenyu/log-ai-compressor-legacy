# -*- coding: utf-8 -*-
"""双击启动脚本测试：编码、行尾、关键逻辑。

两个脚本：
- ``start.bat``   —— v2 主入口，起本地 Web 服务并自动开浏览器
- ``run_gui.bat`` —— 旧版 Tkinter 桌面界面（已归档，回退用）

**为什么强制纯 ASCII + CRLF**（这条被测试反复咬住，别改回去）
----------------------------------------------------------
cmd.exe 逐字节按控制台代码页解析批处理文件。踩中任一条都会静默失败：
1. 用了裸 LF 行尾 → cmd 解析错位，每行开头被吞（``%PY% run_gui.py`` 变
   ``ui.py``，报一堆 ``'xxx' is not recognized``，GUI 永远起不来）；
2. 含与控制台代码页不匹配的非 ASCII 字节 → 同样错位，且中文提示变乱码。
把中文放进 GUI/script 层，bat 里只留 ASCII，才能在任何系统代码页下工作。
"""
from __future__ import annotations

from pathlib import Path

import pytest

BAT_PATH = Path(__file__).resolve().parent.parent / "run_gui.bat"
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
def bat_content() -> str:
    """旧版桌面启动器内容。"""
    return _read_bat(BAT_PATH)


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
# 旧版 run_gui.bat
# ---------------------------------------------------------------------------
class TestRunGuiBat:
    def test_file_exists(self):
        assert BAT_PATH.is_file()

    def test_echo_off_and_no_chcp_utf8(self, bat_content):
        # @echo off 首行；不使用 chcp 65001（UTF-8 模式会破坏 cmd 逐行解析）
        assert bat_content.splitlines()[0].strip().lower() == "@echo off"
        assert "chcp 65001" not in bat_content

    def test_cd_to_script_dir(self, bat_content):
        # 切换到脚本目录，保证任何工作目录下双击都能找到 run_gui.py
        assert 'cd /d "%~dp0"' in bat_content

    def test_python_fallback_detection(self, bat_content):
        # python 优先、py -3 回退（覆盖仅装 py 启动器的环境）
        assert 'set "PY=python"' in bat_content
        assert "py -3" in bat_content
        # 用真实执行校验排除 Windows 商店占位 python
        assert 'python -c "import sys"' in bat_content

    def test_dependency_auto_install(self, bat_content):
        # 依赖缺失时自动安装（tkinterdnd2 为拖拽所需）
        assert "pip install customtkinter matplotlib PyYAML tkinterdnd2" in bat_content

    def test_launches_run_gui(self, bat_content):
        assert "%PY% run_gui.py" in bat_content

    def test_failure_shows_pause(self, bat_content):
        # 失败分支必须 pause，避免双击后窗口闪退看不到错误
        assert bat_content.count("pause") >= 3


# ---------------------------------------------------------------------------
# CLI 参数默认值与 GUI / 常量一致性（修复缺陷#5 收尾）
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

    def test_gui_and_cli_context_same_default(self):
        """GUI 与 CLI 的上下文默认值必须同源（DEFAULT_CONTEXT_LINES）。"""
        from log_ai_compressor.cli import build_parser
        from log_ai_compressor.constants import DEFAULT_CONTEXT_LINES
        from log_ai_compressor.gui_legacy.config_store import DEFAULT_CONFIG
        args = build_parser().parse_args(["run", "app.log"])
        assert args.context == DEFAULT_CONTEXT_LINES == DEFAULT_CONFIG["context_lines"]
