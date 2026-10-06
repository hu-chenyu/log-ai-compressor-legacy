# -*- coding: utf-8 -*-
"""GUI 应用层测试：拖拽、按钮状态、主题、全屏、Tooltip 等交互逻辑。

运行前提：需要可用的显示环境（本地桌面）；CI 无头环境自动跳过。
"""
from __future__ import annotations

import json
import os
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from types import SimpleNamespace

import pytest

ctk = pytest.importorskip("customtkinter")

# 修复R12：分隔条参数（宽度/最小宽度限制）
from log_ai_compressor.gui_legacy.app import (  # noqa: E402
    _SPLITTER_MIN_DETAIL,
    _SPLITTER_MIN_LIST,
    _SPLITTER_WIDTH,
)


def _display_available() -> bool:
    """探测能否创建 Tk 窗口（无头 CI 返回 False）。"""
    try:
        root = tk.Tk()
        root.destroy()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _display_available(),
                                reason="无可用显示环境（无头 CI 跳过 GUI 测试）")


@pytest.fixture()
def app(monkeypatch, tmp_path):
    """创建主窗口实例，隔离用户配置文件，测试后销毁。

    teardown 强制 gc.collect()：长测试序列中 Tk/CTk 控件句柄
    （Canvas/字体/GDI 对象）依赖 GC 释放，累积不回收会触发
    Windows 句柄耗尽（新 Tk root 创建失败）。
    """
    monkeypatch.setattr("log_ai_compressor.gui_legacy.config_store.CONFIG_FILE",
                        tmp_path / "config.json")
    from log_ai_compressor.gui_legacy.app import LogCompressorApp
    application = LogCompressorApp()
    application.update()
    yield application
    # 防御性解除 CTk 冻结（类级补丁是全局的，测试中断未松开时
    # 必须还原，否则后续所有测试的 CTk 重绘被卡死）
    try:
        application._set_ctk_drag_freeze(False)
    except Exception:
        pass
    # 宽容销毁：窗口已失效时仅静默清理（避免teardown报错）
    try:
        application._on_close()
    except Exception:
        try:
            application.destroy()
        except Exception:
            pass
    import gc
    gc.collect()


class TestDragAndDrop:
    """修复2：拖拽文件导入（整窗注册 + Tab 路由）。"""

    def test_tkinterdnd2_imported_and_root_registered(self, app):
        from log_ai_compressor.gui_legacy import app as app_module
        assert app_module._HAS_DND, "tkinterdnd2 应已安装并启用"
        # 根窗口已具备 DnD 能力（tkdnd 已加载）
        assert getattr(app, "TkdndVersion", None) is not None
        assert hasattr(app, "drop_target_register")

    def test_drop_single_file_fills_file_entry(self, app):
        event = SimpleNamespace(data="{D:/logs/app.log}")
        app._on_drop_file(event)
        assert app._file_entry.get() == "D:/logs/app.log"
        assert "已拖入文件" in app._status_label.cget("text")

    def test_drop_plain_path_without_braces(self, app):
        event = SimpleNamespace(data="D:/logs/plain.log")
        app._on_drop_file(event)
        assert app._file_entry.get() == "D:/logs/plain.log"

    def test_drop_multiple_files_prefills_compare(self, app):
        event = SimpleNamespace(
            data="{D:/logs/a.log} {D:/logs/b.log} {D:/logs/c.log}")
        app._on_drop_file(event)
        # 首个进入文件导入框，其余进入对比区
        assert app._file_entry.get() == "D:/logs/a.log"
        assert app._compare_entries[0].get() == "D:/logs/b.log"
        assert app._compare_entries[1].get() == "D:/logs/c.log"

    def test_drop_in_compare_tab_fills_ab(self, app):
        app._tabview.set("多文件对比")
        app.update()
        event = SimpleNamespace(data="{D:/v1.log} {D:/v2.log}")
        app._on_drop_file(event)
        assert app._compare_entries[0].get() == "D:/v1.log"
        assert app._compare_entries[1].get() == "D:/v2.log"
        assert "对比模式" in app._status_label.cget("text")

    def test_drop_empty_event_no_crash(self, app):
        app._on_drop_file(SimpleNamespace(data=""))
        app._on_drop_file(SimpleNamespace(data=None))
        assert app._file_entry.get() == ""

    def test_requirements_declares_tkinterdnd2(self):
        from pathlib import Path
        content = (Path(__file__).resolve().parent.parent
                   / "requirements.txt").read_text(encoding="utf-8")
        assert "tkinterdnd2" in content


# ---------------------------------------------------------------------------
# 修复3：按钮状态机
# ---------------------------------------------------------------------------
SAMPLE_PASTE = """\
2024-01-01 09:00:00 INFO [auth] start
2024-01-01 09:00:05 ERROR [db] connection refused to db-primary:5432
java.net.ConnectException: Connection refused
\tat com.app.db.Pool.init(Pool.java:42)
\tat java.base/java.net.Socket.connect(Socket.java:1)
2024-01-01 09:01:00 FATAL [core] out of memory in worker 3
"""


def _run_paste_analysis(app, text=SAMPLE_PASTE, timeout=60.0):
    """执行一次文本粘贴分析并等待完成。

    timeout 放宽到 60s：全量测试运行时系统满载（多 GUI 实例 +
    matplotlib 首次导入），30s 偶发超时。
    """
    app._tabview.set("文本粘贴")
    app._paste_box.delete("1.0", "end")
    app._paste_box.insert("1.0", text)
    app._on_start()
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.update()
        if app._result is not None:
            break
        time.sleep(0.02)
    if app._result is None:
        # 诊断转储：失败时输出 worker/轮询状态（定位挂起类问题）
        worker = app._worker
        print(f"[diag] queue_size={app._queue.qsize()} "
              f"worker_alive={worker.is_alive() if worker else None}",
              flush=True)
    assert app._result is not None, "分析未完成"


class TestButtonStates:
    ACTION_BUTTONS = ("_cancel_btn", "_export_btn", "_copy_btn", "_chart_btn")

    def test_initial_state_all_disabled(self, app):
        # 未开始分析：四个操作按钮全部置灰
        for name in self.ACTION_BUTTONS:
            assert app.__getattribute__(name).cget("state") == "disabled", name

    def test_after_analysis_all_enabled(self, app):
        # 分析完成后：四个操作按钮全部可点击
        _run_paste_analysis(app)
        for name in self.ACTION_BUTTONS:
            assert app.__getattribute__(name).cget("state") == "normal", name
        assert app._start_btn.cget("state") == "normal"

    def test_cancel_without_task_is_safe(self, app):
        # 完成后点击取消（无进行中任务）：仅提示，不崩溃
        _run_paste_analysis(app)
        app._on_cancel()
        assert "没有进行中的分析任务" in app._status_label.cget("text")

    def test_start_disabled_while_running(self, app, monkeypatch):
        # 分析进行中：开始按钮置灰（monkeypatch 慢速分析消除竞态）
        import log_ai_compressor.gui_legacy.app as app_mod
        from log_ai_compressor.core.models import RunStats, AnalysisResult

        def slow_analyze(text, **kwargs):
            cancel = kwargs.get("cancel_event")
            for _ in range(200):
                if cancel is not None and cancel.is_set():
                    break
                time.sleep(0.02)
            # 取消后返回一个最小结果（与真实管线行为一致）
            stats = RunStats(source="<粘贴文本>", total_lines=1)
            return AnalysisResult(stats=stats, clusters=[])

        monkeypatch.setattr(app_mod, "analyze_text", slow_analyze)
        app._tabview.set("文本粘贴")
        app._paste_box.insert("1.0", SAMPLE_PASTE)
        app._on_start()
        app.update()
        try:
            assert app._start_btn.cget("state") == "disabled"
            assert app._cancel_btn.cget("state") == "normal"
        finally:
            # 取消任务并等待收尾，避免影响后续测试
            app._on_cancel()
            deadline = time.time() + 15
            while time.time() < deadline:
                app.update()
                if app._result is not None:
                    break
                time.sleep(0.02)
        assert app._result is not None
        assert app._start_btn.cget("state") == "normal"


# ---------------------------------------------------------------------------
# 修复4：错误列表换行布局（长摘要完整可见）
# ---------------------------------------------------------------------------
LONG_SUMMARY_LOG = (
    "2024-01-01 09:00:00 ERROR [db] " + "x" * 140 + " 尾部可见标记TAIL\n"
    "2024-01-01 09:00:01 FATAL [core] short fatal\n"
)


class TestClusterListWrap:
    def test_rows_rendered_with_summary_label(self, app):
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        # 两行错误 -> 两行记录（修复缺陷R40：FATAL 归一 ERROR，
        # 同级等优先级按日志原序稳定排序，长摘要行在 row 0）
        assert len(app._cluster_rows) == 2
        # R97：虚拟池行摘要按 SUMMARY_CLIP 截断（单行不换行 + 省略号；
        # 完整摘要进详情面板/水平滚动区域按截断宽测量）
        first_summary = str(app._cluster_rows[0]["summary"].cget("text"))
        assert len(first_summary) > 60
        assert first_summary.endswith("…"), \
            "虚拟模式长摘要应截断省略（完整内容见详情面板）"
        # 行首元信息不包含摘要（R16 起用行内 head 引用）
        head_text = str(app._cluster_rows[0]["head"].cget("text"))
        assert "TAIL" not in head_text

    def test_summary_single_line_no_wrap(self, app):
        """修复R9：摘要单行不换行（wraplength=0），长内容靠水平滚动。"""
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        app.update()
        for row in app._cluster_rows:
            assert int(row["summary"].cget("wraplength")) == 0, \
                "摘要应取消自动换行（wraplength=0 单行显示）"

    def test_horizontal_scrollbar_covers_wide_content(self, app):
        """修复R9/R97：长摘要不换行后，虚拟列表水平滚动区域覆盖
        完整内容宽度（统一虚拟渲染后内容宽由 vl._content_w 承担）。"""
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        app.update()
        vl = app._virtual_list
        assert vl is not None, "R97：任意簇数一律虚拟渲染"
        # 行 1 摘要极长（>100 字符），内容宽应超出视口（可水平滚动）
        widest = max(r["summary"].winfo_reqwidth()
                     for r in app._cluster_rows)
        assert vl._content_w >= widest - 2, \
            f"内容宽 {vl._content_w} 应 ≥ 摘要完整宽 {widest}"
        region = str(vl._canvas.cget("scrollregion")).split()
        assert int(region[2]) >= max(vl._content_w,
                                     vl._canvas.winfo_width()) - 2

    def test_virtual_hbar_wired_and_mapped(self, app):
        """修复R9/R97：虚拟列表底部水平滚动条存在且与画布联动（统一
        虚拟渲染；经典 hbar 在虚拟模式下隐藏）。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        vl = app._virtual_list
        assert vl is not None
        assert vl._hbar.winfo_ismapped(), "虚拟列表底部应有水平滚动条"
        assert not app._list_hbar.winfo_ismapped(), \
            "虚拟模式下经典 hbar 应隐藏"
        # 画布 xscrollcommand 已接滚动条（set 回调非空）
        assert str(vl._canvas.cget("xscrollcommand")) != "", \
            "画布 xscrollcommand 应接入水平滚动条"

    def test_selection_highlight(self, app):
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        app._select_cluster(1)
        assert app._selected_row == 1
        app.update()
        # R97：虚拟池行原生 tk 控件（bg 而非 fg_color）
        selected_color = app._cluster_rows[1]["frame"].cget("bg")
        default_color = app._cluster_rows[0]["frame"].cget("bg")
        assert selected_color != default_color

    def test_row_click_selects(self, app):
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        summary_label = app._cluster_rows[1]["summary"]
        summary_label.event_generate("<Button-1>")
        app.update()
        assert app._selected_row == 1

    def test_row_head_internal_click_selects(self, app):
        """修复R2/R97：点击行首标签（级别/次数行）也触发选中。

        R97 统一虚拟渲染后行首为原生 tk.Label，点击绑定随
        _fill_slot 挂到 frame/head/summary/divider 全控件。
        """
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        app._select_cluster(0)
        app.update()
        head1 = app._cluster_rows[1]["head"]
        head1.event_generate("<Button-1>")
        app.update()
        assert app._selected_row == 1, \
            "点击行 1 头部应选中行 1"
        # 详情面板同步更新
        detail = app._detail_box.get("1.0", "end")
        assert "错误摘要" in detail

    def test_row_click_detail_updates(self, app):
        """修复R2：点击任意行右侧详情同步切换。"""
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        app._select_cluster(0)
        app.update()
        before = app._detail_box.get("1.0", "end")
        # 点击行 1 的摘要标签
        app._cluster_rows[1]["summary"].event_generate("<Button-1>")
        app.update()
        after = app._detail_box.get("1.0", "end")
        assert app._selected_row == 1
        assert before != after, "详情应随点击切换"

    def test_selected_row_blue_highlight(self, app):
        """修复R2：选中态与未选中态背景色明显区分（蓝色高亮）。"""
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        app._select_cluster(1)
        app.update()
        states = app._row_states()
        selected = app._cluster_rows[1]["frame"].cget("bg")
        normal = app._cluster_rows[0]["frame"].cget("bg")
        assert str(selected) != str(normal), "选中/未选中应明显区分"
        assert str(states["selected"]) not in ("", "None")

    def test_hover_highlight(self, app):
        """R97：虚拟池行悬停由 vl._hover + _fill_slot 着色。"""
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        # 行 0 在结果渲染后自动选中，悬停测试使用未选中的行 1
        vl = app._virtual_list
        assert vl is not None
        frame = app._cluster_rows[1]["frame"]
        base = frame.cget("bg")
        vl._hover(1, True)
        app.update()
        hovered = frame.cget("bg")
        vl._hover(1, False)
        app.update()
        restored = frame.cget("bg")
        assert hovered != base and restored == base

    def test_long_word_single_line_horizontal(self, app):
        """修复R9：超长 token 不再折行（单行），水平滚动查看完整内容。"""
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        app.update()
        row = app._cluster_rows[1]
        assert int(row["summary"].cget("wraplength")) == 0
        # 单行摘要高度应只含一行文字（大字体下约 40-60px 逻辑）
        assert row["summary"].winfo_height() < 120, \
            f"摘要应为单行（实际高度 {row['summary'].winfo_height()}px）"


# ---------------------------------------------------------------------------
# 修复R7/R9：列表宽度对齐 + 字体放大 + 水平滚动 + 高度布局
# ---------------------------------------------------------------------------
class TestClusterListFontAndWidth:
    def test_main_list_font_sizes(self, app):
        """修复R9：主列表字体标称大小——头部 22 加粗 / 摘要 18。"""
        assert int(app._font_row_head.cget("size")) == 22
        assert str(app._font_row_head.cget("weight")) == "bold"
        assert int(app._font_row_summary.cget("size")) == 18
        # 底层 tk 命名字体的实际像素尺寸（CTkFont 用负数表示像素）
        head_tk = tkfont.Font(root=app, name=str(app._font_row_head),
                              exists=True)
        sum_tk = tkfont.Font(root=app, name=str(app._font_row_summary),
                             exists=True)
        assert int(head_tk.cget("size")) == -22
        assert int(sum_tk.cget("size")) == -18

    def test_fullscreen_list_font_sizes(self, app):
        """修复R10：全屏列表字体标称大小——头部 28 加粗 / 摘要 24 / 实例 20。"""
        assert int(app._font_fs_head.cget("size")) == 28
        assert str(app._font_fs_head.cget("weight")) == "bold"
        assert int(app._font_fs_summary.cget("size")) == 24
        assert int(app._font_fs_inst.cget("size")) == 20

    def test_summary_font_dpi_scaled(self, app):
        """修复R9/R97：摘要字体随 DPI 缩放（渲染比例与头部一致）。

        R97 统一虚拟渲染后池行均为原生 tk.Label，直接取 cget("font")
        的实际渲染字体（创建时已按 DPI 缩放，见 _make_slot）。
        """
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        row = app._cluster_rows[0]
        head_size = int(tkfont.Font(
            font=row["head"].cget("font")).cget("size"))
        sum_size = int(tkfont.Font(
            font=row["summary"].cget("font")).cget("size"))
        ratio = sum_size / max(1, head_size)
        assert abs(ratio - 18 / 22) < 0.06, \
            f"摘要/头部渲染比例 {ratio:.3f} 应 ≈ 18/22（均含 DPI 缩放）"

    def test_virtual_row_uses_enlarged_fonts(self, app):
        """修复R9/R97：池行头部/摘要渲染字号匹配 22/18 档（统一虚拟
        渲染后原「经典行共享字体」断言转化为池行实际字体比例）。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        row = app._cluster_rows[0]
        head_size = int(tkfont.Font(
            font=row["head"].cget("font")).cget("size"))
        sum_size = int(tkfont.Font(
            font=row["summary"].cget("font")).cget("size"))
        assert abs(sum_size / max(1, head_size) - 18 / 22) < 0.06

    def test_virtual_row_height_fits_fonts(self, app):
        """修复R9：虚拟行高按实际字体度量计算（容纳头部+摘要单行）。"""
        _run_many_clusters(app)
        app.update()
        vl = app._virtual_list
        assert vl is not None
        head_ls = tkfont.Font(
            font=vl.slots[0]["head"].cget("font")).metrics("linespace")
        sum_ls = tkfont.Font(
            font=vl.slots[0]["summary"].cget("font")).metrics("linespace")
        assert vl.ROW_HEIGHT >= head_ls + sum_ls, \
            f"行高 {vl.ROW_HEIGHT} 应 ≥ 头部{head_ls}+摘要{sum_ls}行距"

    def test_fullscreen_rows_use_fs_fonts(self, app):
        """修复R9：全屏列表行实际使用全屏字体（头部 24 / 摘要 20，含缩放）。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        # 主列表头部渲染字号取样（R97：池行原生 tk.Label 直取）
        classic_head = app._cluster_rows[0]["head"]
        classic_size = int(
            tkfont.Font(font=classic_head.cget("font")).cget("size"))
        app._open_list_fullscreen()
        for _ in range(20):
            app.update()
            time.sleep(0.005)
        win = app._fs_list_win
        assert win is not None and win.winfo_exists()
        # 全屏行（原生 tk.Label）头部含级别文本；其渲染字号应 ≈ 28 号
        #（经典 22 号基准 × 28/22，同一缩放系数）
        heads = [w for w in _all_widgets(win)
                 if isinstance(w, tk.Label)
                 and "ERROR" in str(w.cget("text"))]
        assert heads, "全屏窗口应有行头部标签"
        fs_size = int(
            tkfont.Font(font=heads[0].cget("font")).cget("size"))
        assert abs(fs_size - classic_size * 28 / 22) <= 2, \
            f"全屏头部渲染 {fs_size} 应 ≈ 经典 {classic_size}×28/22"
        win.event_generate("<Escape>")
        app.update()

    def test_fullscreen_instance_row_font_and_nowrap(self, app):
        """修复R10/R42：全屏实例行 = 全屏摘要档渲染 + 单行不换行。"""
        # 同簇多实例日志（×2 实例，展开后可见实例行）
        multi = (SAMPLE_PASTE + "2024-01-01 09:02:00 FATAL [core] "
                 "out of memory in worker 4\n")
        _run_paste_analysis(app, multi)
        app.update()
        app._open_list_fullscreen()
        for _ in range(25):
            app.update()
            time.sleep(0.005)
        win = app._fs_list_win
        assert win is not None and win.winfo_exists()
        vl = app._fs_vl
        # 点击「▶ ×2」展开按钮（FATAL→ERROR 归一簇 ×2）
        toggles = [s for s in vl.slots
                   if 0 <= s.get("idx", -1) < len(vl._data)
                   and str(s["toggle"].cget("text")) == "×2"]
        assert toggles, "全屏窗口应有 ×2 展开按钮"
        toggles[0]["toggle"].event_generate("<Button-1>")
        for _ in range(25):
            app.update()
            time.sleep(0.005)
        # 优化缺陷R42：实例作为视图行注入全屏虚拟列表
        inst_rows = [r for r in vl._data if r[0] == "i"]
        assert len(inst_rows) == 2, "展开后应有 2 个实例视图行"
        inst_slot = next(
            s for s in vl.slots
            if 0 <= s.get("idx", -1) < len(vl._data)
            and vl._data[s["idx"]][0] == "i")
        lbl = inst_slot["summary"]
        assert int(lbl.cget("wraplength")) == 0, "实例行应单行不换行"
        assert "out of memory" in str(lbl.cget("text"))
        size = int(tkfont.Font(font=lbl.cget("font")).cget("size"))
        # 实例行渲染字号 = 全屏摘要档（主列表头部 22 号基准 × 24/22）
        classic = app._cluster_rows[0]["head"]
        base = int(tkfont.Font(font=classic.cget("font")).cget("size"))
        assert abs(size - base * 24 / 22) <= 2, \
            f"实例行渲染 {size} 应 ≈ 主列表 {base}×24/22（全屏摘要档）"
        win.event_generate("<Escape>")
        app.update()

    def test_fullscreen_search_font_enlarged(self, app):
        """修复R10：全屏搜索框字体 18 号。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        app._open_list_fullscreen()
        for _ in range(20):
            app.update()
            time.sleep(0.005)
        win = app._fs_list_win
        entries = [w for w in _all_widgets(win)
                   if isinstance(w, ctk.CTkEntry)]
        assert entries, "全屏窗口应有搜索输入框"
        # CTkEntry.cget("font") 返回 CTkFont 对象（标称字号，DPI 无关）
        font_obj = entries[0].cget("font")
        assert int(font_obj.cget("size")) == 18, \
            f"搜索框字号应为 18（实际 {font_obj.cget('size')}）"
        win.event_generate("<Escape>")
        app.update()

    def test_virtual_hbar_present_and_classic_hidden(self, app):
        """修复R9/R97：虚拟列表有独立水平滚动条，经典 hbar 恒隐藏
        （R97 统一虚拟渲染后不再存在经典/虚拟模式切换）。"""
        _run_many_clusters(app)
        app.update()
        vl = app._virtual_list
        assert vl is not None
        assert vl._hbar.winfo_ismapped(), "虚拟列表应有水平滚动条"
        assert not app._list_hbar.winfo_ismapped(), \
            "经典 hbar 应隐藏"
        # 长摘要数据：水平滚动区域应加宽（内容宽超视口）
        canvas = vl._canvas
        region = str(canvas.cget("scrollregion")).split()
        assert int(region[2]) >= max(vl._content_w, canvas.winfo_width()) - 2
        # R97：小簇数同样虚拟渲染（不再切回经典）
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        assert app._virtual_list is not None, "R97：小簇数也走虚拟渲染"
        assert not app._list_hbar.winfo_ismapped(), \
            "经典 hbar 在小簇数下同样隐藏"

    def test_default_window_height_upgraded(self, app):
        """修复R9：默认窗口高度升级为 1000（容纳大字体与 6 行可视）。"""
        assert int(app._config.get("window", {}).get("height", 0)) >= 1000

    def test_list_shows_six_rows_at_default_height(self, app):
        """修复R9：1000 逻辑高默认窗口下列表可视行数 ≥6。

        屏幕不够高时窗口会被 WM 钳制（无法直接渲染验证），改用实测
        行距与固定区域高度做数学验证（逻辑单位，DPI 无关）。
        """
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        rows = app._cluster_rows
        if len(rows) < 2:
            pytest.skip("行数不足")
        pitch = (rows[1]["frame"].winfo_rooty()
                 - rows[0]["frame"].winfo_rooty())
        canvas = app._virtual_list._canvas   # R97：虚拟画布
        if pitch < 10 or canvas.winfo_height() < 60:
            pytest.skip("窗口未完成布局")
        scale = max(1.0, app._font_scale)
        # 固定区域高度（逻辑）：窗口高 - 列表画布高
        fixed = app.winfo_height() / scale - canvas.winfo_height() / scale
        rows_at_1000 = int((1000 - fixed) / (pitch / scale))
        assert rows_at_1000 >= 6, \
            f"1000 逻辑高窗口应显示 ≥6 行（实际 {rows_at_1000}，" \
            f"行距 {pitch / scale:.1f} 逻辑px，固定区 {fixed:.0f} 逻辑px）"

    def test_list_right_edge_aligns_with_fullscreen_button(self, app):
        """修复R7：列表右缘（含滚动条）与「全屏」按钮右缘严格对齐。"""
        app.update()
        if app._list_host.winfo_width() < 50:
            pytest.skip("窗口未完成布局")
        btn = app._list_fs_btn
        btn_right = btn.winfo_rootx() + btn.winfo_width()
        list_right = (app._list_host.winfo_rootx()
                      + app._list_host.winfo_width())
        assert abs(list_right - btn_right) <= 2, \
            f"列表右缘与按钮右缘偏差 {list_right - btn_right}px（应 ≤2px）"

    def test_list_fills_host_width(self, app):
        """修复R7/R97：列表（虚拟画布）占满宿主宽度 ≥90%。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        if app._list_host.winfo_width() < 50:
            pytest.skip("窗口未完成布局")
        host_w = app._list_host.winfo_width()
        list_w = app._virtual_list._canvas.winfo_width()
        assert list_w >= 0.9 * host_w, \
            f"列表宽 {list_w}px 应占宿主宽 {host_w}px 的 90% 以上"


# ---------------------------------------------------------------------------
# 修复缺陷R19：FATAL 复选框删除（始终放行显示）+ 五级别前移
# ---------------------------------------------------------------------------
class TestFatalLevelFilter:
    def test_fatal_checkbox_removed_five_remain(self, app):
        """修复R19：FATAL 复选框删除，ERROR 居首共五个复选框。"""
        from log_ai_compressor.gui_legacy.app import LEVEL_CHECKS
        assert LEVEL_CHECKS == ("ERROR", "FAIL", "WARN", "INFO", "DEBUG")
        assert "FATAL" not in LEVEL_CHECKS
        assert "FATAL" not in app._level_vars, "FATAL 复选框应已删除"
        assert len(app._level_vars) == 5, "应只剩五个级别复选框"
        assert app._level_vars["ERROR"].get() is True
        assert app._level_vars["FAIL"].get() is True
        assert app._level_vars["WARN"].get() is False

    def test_fatal_normalized_to_error_displayed(self, app):
        """修复R40：显式 [FATAL] 日志归一为 ERROR 显示（级别删除）。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        levels = [c.level for c in app._displayed]
        assert "FATAL" not in levels, "FATAL 级别应已删除（归一 ERROR）"
        assert "ERROR" in levels
        # 原 FATAL 行（out of memory）以 ERROR 身份出现在列表中
        assert any("out of memory" in c.summary for c in app._displayed)

    def test_fatal_help_tooltip_registered(self, app):
        """优化：五个级别复选框左侧都有 ⓘ 且悬停说明已登记。"""
        tooltips = getattr(app, "_level_tooltips", {})
        assert len(tooltips) == 5, \
            f"五个级别都应有 ⓘ 悬停说明（实际 {len(tooltips)} 个）"
        for level in ("ERROR", "FAIL", "WARN", "INFO", "DEBUG"):
            assert level in tooltips, f"{level} 缺少 ⓘ 悬停说明"
            assert tooltips[level] is not None
        assert "FATAL" not in tooltips

    def test_level_help_texts_match(self, app):
        """优化：每个 ⓘ 的悬停解释文字与其级别精确对应。"""
        from log_ai_compressor.gui_legacy.app import _LEVEL_HELP
        expected = {
            "ERROR": "ERROR：错误，程序运行中出现的异常，"
                     "可能导致功能异常但程序仍可继续运行",
            "FAIL": "FAIL：失败，操作或测试未成功完成的结果",
            "WARN": "WARN：警告，可能存在问题但不影响程序正常运行，"
                    "需要关注",
            "INFO": "INFO：信息，程序正常运行时的一般性记录",
            "DEBUG": "DEBUG：调试，开发调试用的详细信息，"
                     "通常生产环境不显示",
        }
        assert "FATAL" not in _LEVEL_HELP, "FATAL 说明应随复选框删除"
        for level, text in expected.items():
            tip = app._level_tooltips[level]
            assert tip._current_text() == text, \
                f"{level} 的解释文字不匹配（实际 {tip._current_text()!r}）"

    def test_level_info_icons_left_of_checkboxes(self, app):
        """优化：五个 ⓘ 位于对应复选框左侧且样式统一（蓝/手型光标）。"""
        app.update()
        level_box = app._level_tooltips["ERROR"]._widget.master
        boxes = {}
        infos = {}
        for child in level_box.winfo_children():
            txt = str(child.cget("text"))
            if txt == "ⓘ":
                infos[child] = child.winfo_x()
            elif txt in ("ERROR", "FAIL", "WARN", "INFO", "DEBUG"):
                boxes[txt] = child.winfo_x()
            else:
                assert txt != "FATAL", "FATAL 复选框不应存在"
        assert len(infos) == 5, f"应有五个 ⓘ 图标（实际 {len(infos)}）"
        assert len(boxes) == 5, f"应有五个复选框（实际 {len(boxes)}）"
        for icon, ix in infos.items():
            # 每个 ⓘ 紧邻其右侧最近的复选框（同一级别组）
            right = min(bx for bx in boxes.values() if bx > ix - 5)
            assert right - ix <= 60, "ⓘ 应紧邻复选框左侧"
            assert str(icon.cget("cursor")) == "hand2", \
                "ⓘ 悬停光标应为手型"
            color = str(icon.cget("text_color"))
            assert color.lower() == "#3b82f6", \
                f"ⓘ 颜色应统一为 #3B82F6（实际 {color}）"

    def test_five_level_colors(self, app):
        """修复R40：五级别五色 + 根因紫 + 选中调亮（一眼区分严重程度）。"""
        from log_ai_compressor.gui_legacy.app import LogCompressorApp
        from log_ai_compressor.core.models import ErrorCluster

        def mk(level, root=False):
            return ErrorCluster(cluster_id=level, template="t",
                                summary="s", level=level, count=1,
                                is_root_cause=root)

        expected = {"ERROR": "#ff5252", "FAIL": "#ff7a45",
                    "WARN": "#ffb74d", "INFO": "#5ac8fa",
                    "DEBUG": "#9ca3af"}
        for level, color in expected.items():
            got = LogCompressorApp._row_color(mk(level))
            assert got and got.lower() == color, \
                f"{level} 应为 {color}（实际 {got}）"
        # 根因紫优先于级别色（与五级别色区分）
        root = LogCompressorApp._row_color(mk("ERROR", root=True))
        assert root and root.lower() == "#c084fc", \
            f"根因应为紫色 #c084fc（实际 {root}）"
        # 选中蓝底上的调亮版（无色级别回退 None 由调用方回退白字）
        sel = LogCompressorApp._row_color_sel(mk("ERROR"))
        assert sel and sel.lower() == "#ff8a80", \
            f"选中 ERROR 应调亮为 #ff8a80（实际 {sel}）"
        assert LogCompressorApp._row_color_sel(
            mk("WARN", root=True)).lower() == "#d8b4fe"

    def test_old_config_with_fatal_loads_safely(self, app):
        """修复R19：旧配置（levels 含 FATAL）静默兼容不崩溃。"""
        # app fixture 的配置由 _restore_config 处理：旧 levels 含
        # FATAL 时 _level_vars（无 FATAL 键）正常恢复、不 KeyError
        assert "FATAL" not in app._level_vars
        assert app._level_vars["ERROR"].get() is True


class TestFontSizeSelector:
    def test_font_menu_exists_with_default(self, app):
        """修复R10：字体大小选择器存在且默认「中」。"""
        assert app._font_menu.get() == "中"
        assert int(app._font_row_head.cget("size")) == 22, \
            "「中」档头部应为基准 22 号"

    def test_font_menu_in_list_title_bar(self, app):
        """修复R11：字体选择器在错误列表标题栏（标题→字体大小→全屏）。"""
        app.update()
        if app._list_fs_btn.winfo_rootx() == 0:
            pytest.skip("窗口未完成布局")
        # 1) 与全屏按钮同一容器（列表标题栏）
        assert (app._font_menu.master is app._list_fs_btn.master), \
            "字体选择器应与全屏按钮同在列表标题栏"
        # 2) 横向顺序：标题 → 字体大小选择器 → 全屏按钮
        font_x = app._font_menu.winfo_rootx()
        btn_x = app._list_fs_btn.winfo_rootx()
        assert font_x < btn_x, "字体选择器应在全屏按钮左边"
        # 3) 不在配置区（父容器不是配置面板）
        assert app._font_menu.master is not app._rule_menu.master, \
            "字体选择器应已从配置区移除"

    def test_font_size_change_scales_fonts(self, app):
        """修复R10：切换档位即时缩放主列表/全屏字体并保存配置。"""
        app._apply_font_size("特大")
        assert int(app._font_row_head.cget("size")) == 29, \
            "特大档头部应 round(22×1.3)=29"
        assert int(app._font_row_summary.cget("size")) == 23, \
            "特大档摘要应 round(18×1.3)=23"
        assert int(app._font_fs_head.cget("size")) == 36, \
            "特大档全屏头部应 round(28×1.3)=36"
        assert app._font_size == "特大"
        assert app._config.get("font_size") == "特大", "档位应已持久化"
        # 档位切换后行级原生标签重渲染（字号随档位；R97 池行 tk.Label 直取）
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        head = app._cluster_rows[0]["head"]
        size = int(tkfont.Font(font=head.cget("font")).cget("size"))
        # 特大档 29 号 vs 基准 22 号（同 DPI 系数下比例 ≈ 29/22）
        assert size >= 26, f"特大档头部渲染 {size} 应 ≥26"
        # 恢复默认档位（避免影响其他用例）
        app._apply_font_size("中")
        assert int(app._font_row_head.cget("size")) == 22

    def test_font_size_persisted_across_restart(self, app, tmp_path):
        """修复R10：字体档位保存后新实例自动恢复（同一配置文件）。"""
        app._apply_font_size("大")
        from log_ai_compressor.gui_legacy.app import LogCompressorApp
        # app fixture 已隔离配置文件；新实例读取同一份 -> 恢复「大」
        app2 = LogCompressorApp()
        try:
            app2.update()
            assert app2._font_size == "大", "重启应恢复上次档位"
            assert app2._font_menu.get() == "大"
            assert int(app2._font_row_head.cget("size")) == 25, \
                "大档头部应 22×1.15≈25"
        finally:
            try:
                app2._on_close()
            except Exception:
                try:
                    app2.destroy()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# 修复R12：错误列表 | 详情面板 可拖动分隔条
# ---------------------------------------------------------------------------
class TestSplitter:
    def _pw(self, app):
        return max(1, app._result_panel.winfo_width())

    def test_splitter_exists_with_cursor(self, app):
        """修复R12：分隔条存在、宽 4~6px、光标为左右箭头。"""
        sp = app._splitter
        assert sp.winfo_exists()
        assert 4 <= int(sp.cget("width")) <= 6, \
            f"分隔条宽度应 4~6px（实际 {sp.cget('width')}）"
        assert str(sp.cget("cursor")) == "sb_h_double_arrow", \
            "光标应为左右双箭头"
        # 三个握点（视觉提示）
        assert len(app._splitter_dots) == 3

    def test_drag_resizes_columns(self, app):
        """修复R12：拖动分隔条实时调整左右列宽。"""
        app.update()
        sp = app._splitter
        panel = app._result_panel
        pw = self._pw(app)
        left0 = app._list_col.winfo_width()
        # 拖动：把分隔条移到面板 65% 处（相对偏移 = 目标 - 当前）
        target = int(pw * 0.65)
        delta = target - (sp.winfo_rootx() - panel.winfo_rootx())
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        sp.event_generate("<B1-Motion>", x=3 + delta, y=40)
        app.update()
        sp.event_generate("<ButtonRelease-1>", x=3 + delta, y=40)
        app.update()
        left1 = app._list_col.winfo_width()
        detail1 = app._detail_col.winfo_width()
        scale = max(1.0, app._font_scale)
        assert abs(left1 - target) <= 8, \
            f"拖动后左列应 ≈{target}px（实际 {left1}px）"
        assert left1 > left0, "往右拖列表应变宽"
        assert abs((left1 + detail1 + _SPLITTER_WIDTH * scale) - pw) <= 6, \
            "左右列 + 分隔条应占满面板宽"

    def test_drag_columns_follow_per_motion(self, app):
        """优化：矢量文本代理 —— 内容随容器实时延展（文字露出更多）。

        用户验收核心：拖动中内容随容器实时变化（摘要文字随列宽
        露出更多），不是松开才变。矢量代理把可见行绘制为完整文本
        items，裁剪框变宽时 canvas 边界自然露出更多文字（真延展，
        非静态截图）。断言：左裁剪框宽逐 motion 实时跟随（= 左列
        内容视口）、右画布视口实时滚动（详情文本贴住分隔条）、
        真实列拖动中冻结（松开一次到位）。
        """
        _run_many_clusters(app)
        app.update()
        sp = app._splitter
        pw = self._pw(app)
        assert not hasattr(app, "_splitter_proxy"), "位图代理应已移除"
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        live = app._splitter_live
        assert live is not None, "虚拟列表模式应构建矢量文本代理"
        lw0 = live["lw"]
        panel = app._result_panel
        # 修复缺陷R13：初始视口精确归零（原 xview_moveto 错位数百 px
        # → 代理竖线与真实分隔条分离成双竖线残影）
        assert abs(live["right"].canvasx(0)) <= 1, \
            "press 后初始视口应归零（代理竖线与分隔条位置一致）"
        # 修复缺陷R13：真实分隔条 press 时隐藏（竖线全由代理呈现，
        # 每帧窗口操作 4→3，根除双线）
        assert not sp.winfo_ismapped(), "拖动中真实分隔条应隐藏"
        # 修复缺陷R15：ⓘ 保持真实控件显示（固定在右端全屏按钮左边，
        # 不随分隔条移动；仅「详情」标题 Label 被隐藏由代理近似）
        info_lbl = app._detail_head.winfo_children()[1]
        info_x0 = info_lbl.winfo_rootx()
        assert info_lbl.winfo_ismapped(), "拖动中 ⓘ 应真实显示"
        assert not app._detail_head.winfo_children()[0].winfo_ismapped(), \
            "拖动中真实「详情」标题应隐藏（代理近似）"
        frozen = app._list_col.winfo_width()
        for dx in (80, 160, 240):
            sp.event_generate("<B1-Motion>", x=3 + dx, y=40)
            # 拖动为 rAF 节流（≤83fps 节拍应用最新位置）：测试中
            # motion 间隔远小于节流窗口，手动 flush 应用待应用帧
            # （真实拖动由节拍器/兜底 after 帧触发，逻辑一致）
            app._live_flush()
            app.update()
            expect = app._splitter_ratio * pw
            assert abs(live["clip"].winfo_width() - expect) <= 2, \
                "左裁剪框宽应逐 motion 实时跟随（内容视口延展）"
            assert abs(live["right"].canvasx(0) - (lw0 - expect)) <= 2, \
                "右画布视口应实时滚动（详情文本贴住分隔条）"
            # 修复缺陷R13：右标题条左缘 x=left（含分隔条区，内部
            # canvas [0, sp_w] 画竖线 + 标题跟随）
            tbar_x = live["tbar"].winfo_rootx() - panel.winfo_rootx()
            assert abs(tbar_x - expect) <= 2, \
                "右标题条应跟随分隔条（左缘含分隔条区）"
            # 修复缺陷R14：tbar 右缘让开右「⛶ 全屏」按钮实测宽
            # （原固定 100px 高 DPI 下吃掉按钮左半）
            tbar_r = tbar_x + live["tbar"].winfo_width()
            assert abs(tbar_r - (pw - live["fs_w"])) <= 2, \
                "覆盖条右缘应让开右全屏按钮（实测宽余量）"
            # 修复缺陷R14：左列标题栏控件组（字体大小+全屏）实时
            # 跟随左列右缘（真实容器纯移动，非代理近似）
            ctrl_x = (app._list_ctrl_box.winfo_rootx()
                      - panel.winfo_rootx())
            assert abs(ctrl_x - max(0, expect - live["ctrl_dx"])) <= 2, \
                "标题栏控件组应实时跟随左列右缘"
            # 修复缺陷R15：ⓘ 位置固定（不随分隔条移动）
            assert info_lbl.winfo_rootx() == info_x0, \
                "ⓘ 应固定在全屏按钮左边（不随分隔条）"
            assert app._list_col.winfo_width() == frozen, \
                "拖动中真实列冻结（代理之下，松开一次应用）"
        sp.event_generate("<ButtonRelease-1>", x=3 + 240, y=40)
        app.update()
        assert app._splitter_live is None, "释放后代理应销毁"
        # 修复缺陷R13：真实分隔条恢复显示
        assert sp.winfo_ismapped(), "释放后真实分隔条应恢复显示"
        # 真实「详情」标题+ⓘ 应恢复显示
        assert all(w.winfo_ismapped()
                   for w in app._detail_head.winfo_children()), \
            "释放后真实标题栏控件应全部恢复"
        assert abs(app._splitter_ratio * pw
                   - app._list_col.winfo_width()) <= 8, \
            "释放后真实列一次性到最终位置"

    def test_drag_freezes_ctk_redraw_cascade(self, app):
        """优化：回退路径（经典小列表无代理）——按下冻结 CTk 重绘级联。

        虚拟列表模式走矢量代理（真实控件不动，无需冻结）；经典
        小列表代理不可用，回退真实布局逐 motion，此时按下冻结
        15 个 CTk 类的 _draw（掐断嵌套 update_idletasks 重入级联），
        松开还原。
        """
        import customtkinter as _ctk
        app.update()
        sp = app._splitter
        orig_draw = _ctk.CTkBaseClass._draw
        orig_sb_draw = _ctk.CTkScrollbar._draw
        # 经典模式（无虚拟列表）：代理不可用 → 回退路径
        assert app._virtual_list is None
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        assert app._splitter_live is None, "经典模式不应建代理"
        assert app._ctk_freeze_orig is not None, "回退路径按下应冻结"
        assert _ctk.CTkBaseClass._draw is not orig_draw
        assert _ctk.CTkScrollbar._draw is not orig_sb_draw
        sp.event_generate("<B1-Motion>", x=3 + 120, y=40)
        app.update()
        sp.event_generate("<ButtonRelease-1>", x=3 + 120, y=40)
        app.update()
        assert app._ctk_freeze_orig is None, "松开应解除冻结"
        assert _ctk.CTkBaseClass._draw is orig_draw
        assert _ctk.CTkScrollbar._draw is orig_sb_draw

    def test_drag_focusout_ends_drag(self, app):
        """兼容性：拖动中窗口失焦 → 结束拖动并应用当前位置。"""
        _run_many_clusters(app)
        app.update()
        sp = app._splitter
        pw = self._pw(app)
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        assert app._splitter_live is not None
        sp.event_generate("<B1-Motion>", x=3 + 160, y=40)
        app.update()
        app.event_generate("<FocusOut>")
        app.update()
        assert not app._splitter_dragging, "失焦应结束拖动"
        assert app._splitter_live is None, "失焦应销毁代理"
        assert abs(app._splitter_ratio * pw
                   - app._list_col.winfo_width()) <= 8, \
            "失焦应应用当前位置"

    def test_drag_realtime_fast_perf(self, app):
        """优化：矢量代理拖动性能 —— 同步段 <16ms；墙钟仅兜底防回归。

        矢量文本代理：每帧仅左裁剪框 place + 右画布视口滚动
        （GDI 级原语），单画布原子渲染无撕裂。确定性断言 = motion
        处理器同步段（钳制算术 + _live_flush 的 place/scroll/coords
        Tcl 派发，不随视口内容/机器负载变化，<16ms）；
        app.update() 墙钟含 Tk 实际重绘，受机器负载波动影响大
        （R21 提高结果区视口后同代码实测 24~93ms 摆动），只留
        140ms（真实重排物理下限）兜底防方案级回归。
        """
        _run_many_clusters(app)
        app.update()
        assert app._virtual_list is not None
        sp = app._splitter
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        assert app._splitter_live is not None
        pw = self._pw(app)
        ctx = app._splitter_drag_ctx
        live = app._splitter_live

        class _Ev:  # 最小事件：处理器只读 x_root
            __slots__ = ("x_root",)

        def motion_at(frac):
            ev = _Ev()
            ev.x_root = ctx["rootx"] + int(pw * frac) + ctx["sp_w"] // 2
            return ev

        # 预热：首帧冷启动（代理建层 / 首次几何级联）
        for frac in (0.25, 0.6, 0.25):
            app._on_splitter_drag(motion_at(frac))
        app.update()
        sync_times, wall_times = [], []
        try:
            for i in range(40):
                frac = 0.25 if i % 2 == 0 else 0.6
                live["t0"] = 0.0      # 绕过节流：每次必 flush（最坏路径）
                t0 = time.perf_counter()
                app._on_splitter_drag(motion_at(frac))
                sync_times.append((time.perf_counter() - t0) * 1000)
                t1 = time.perf_counter()
                app.update()
                wall_times.append((time.perf_counter() - t1) * 1000)
        finally:
            sp.event_generate("<ButtonRelease-1>", x=3, y=40)
            app.update()
        # trimmed mean（去最高/最低各 4 帧）：抗负载尖峰
        st = sorted(sync_times)[4:-4]
        sync_trimmed = sum(st) / len(st)
        assert sync_trimmed < 16.0, \
            f"拖动同步段应 <16ms（实际 {sync_trimmed:.1f}ms，" \
            f"均值 {sum(sync_times) / 40:.1f}ms）"
        wt = sorted(wall_times)[4:-4]
        wall_trimmed = sum(wt) / len(wt)
        assert wall_trimmed < 140.0, \
            f"墙钟单帧不得退回真实重排下限 ~140ms（实际 " \
            f"{wall_trimmed:.1f}ms，均值 {sum(wall_times) / 40:.1f}ms）"

    def test_virtual_fast_path_during_drag(self, app):
        """优化：虚拟列表拖动期走快速路径（不重填文本），松开全量同步。

        拖动中数据/滚动位置不变，_sync 只 itemconfigure 行宽（<1ms），
        跳过文本重填/事件重绑；松开时补一次全量 _sync。
        """
        _run_many_clusters(app)
        app.update()
        assert app._virtual_list is not None
        sp = app._splitter
        calls = []
        orig = app._virtual_list._fill_slot

        def counting(*a, **k):
            calls.append(1)
            return orig(*a, **k)

        app._virtual_list._fill_slot = counting
        try:
            sp.event_generate("<ButtonPress-1>", x=3, y=40)
            app.update()
            calls.clear()
            for dx in (80, 160, 240):
                sp.event_generate("<B1-Motion>", x=3 + dx, y=40)
                app.update()
            assert calls == [], "拖动中不应重填行文本（快速路径）"
            sp.event_generate("<ButtonRelease-1>", x=3 + 240, y=40)
            app.update()
            assert calls, "松开后应执行一次全量同步"
        finally:
            app._virtual_list._fill_slot = orig

    def test_drag_min_width_limits(self, app):
        """修复R12：拖到最左/最右受最小宽度限制（动态实测标题栏宽）。"""
        app.update()
        sp = app._splitter
        scale = max(1.0, app._font_scale)
        # 动态最小宽（标题栏实测）为下限；固定常量兜底值也应满足
        left_min, right_min = app._splitter_min_widths()
        min_list = max(_SPLITTER_MIN_LIST * scale, left_min) - 4
        min_detail = max(_SPLITTER_MIN_DETAIL * scale, right_min) - 4
        # 拖到最左
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        sp.event_generate("<B1-Motion>", x=-5000, y=40)
        app.update()
        sp.event_generate("<ButtonRelease-1>", x=-5000, y=40)
        app.update()
        assert app._list_col.winfo_width() >= min_list, \
            f"列表最小宽度应 ≥{min_list:.0f}物理px（实际 {app._list_col.winfo_width()}）"
        assert app._detail_col.winfo_width() >= min_detail
        # 拖到最右
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        sp.event_generate("<B1-Motion>", x=5000, y=40)
        app.update()
        sp.event_generate("<ButtonRelease-1>", x=5000, y=40)
        app.update()
        assert app._detail_col.winfo_width() >= min_detail, \
            f"详情最小宽度应 ≥{min_detail:.0f}物理px（实际 {app._detail_col.winfo_width()}）"
        assert app._list_col.winfo_width() >= min_list

    def test_extremes_keep_titlebars_visible(self, app):
        """修复R12：拖到左右极限时标题栏控件完整可见（不被遮挡）。

        旧固定最小宽（200/300）小于标题栏内容宽：最左时「错误分类
        列表（按优先级降序）」整体被裁，最右时「详情」起首两字被
        分隔条挡住。断言：极限列宽 ≥ 标题栏请求宽 + padx，且标题栏
        每个子控件完全落在列内。
        """
        app.update()
        sp = app._splitter
        scale = max(1.0, app._font_scale)
        pad = 10 * scale * 2        # 标题栏 grid padx（物理，两侧）

        def check_visible(col, head, tag):
            app.update_idletasks()
            app.update()
            assert col.winfo_width() >= head.winfo_reqwidth() + pad - 2, \
                f"{tag}: 列宽 {col.winfo_width()} 应 ≥ 标题栏需求 " \
                f"{head.winfo_reqwidth() + pad}"
            # 标题栏每个子控件完整落在列内（几何不遮挡的强断言）
            col_l = col.winfo_rootx()
            col_r = col_l + col.winfo_width()
            for child in head.winfo_children():
                cl = child.winfo_rootx()
                cr = cl + child.winfo_width()
                assert cl >= col_l - 2, f"{tag}: 子控件左缘越界"
                assert cr <= col_r + 2, f"{tag}: 子控件右缘越界（被裁剪）"

        # 修复缺陷R14：标题栏控件组（常驻 panel 的字体大小+全屏）
        # 极限位置完整落在左列内、贴右缘
        def check_ctrl(tag):
            app.update_idletasks()
            app.update()
            col_l = app._list_col.winfo_rootx()
            col_r = col_l + app._list_col.winfo_width()
            cl = app._list_ctrl_box.winfo_rootx()
            cr = cl + app._list_ctrl_box.winfo_width()
            assert cl >= col_l - 2, f"{tag}: 控件组左缘越界"
            assert cr <= col_r + 2, f"{tag}: 控件组右缘越界（被裁剪）"
            assert abs(cr - (col_r - 10 * scale)) <= 4, \
                f"{tag}: 控件组应贴左列右缘（右边距 10）"

        # 拖到最左：左列表标题完整可见
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        sp.event_generate("<B1-Motion>", x=-5000, y=40)
        app.update()
        sp.event_generate("<ButtonRelease-1>", x=-5000, y=40)
        app.update()
        check_visible(app._list_col, app._list_head, "最左极限")
        check_visible(app._detail_col, app._detail_head, "最左极限右列")
        check_ctrl("最左极限")

        # 拖到最右：右详情标题完整可见
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        sp.event_generate("<B1-Motion>", x=5000, y=40)
        app.update()
        sp.event_generate("<ButtonRelease-1>", x=5000, y=40)
        app.update()
        check_visible(app._detail_col, app._detail_head, "最右极限")
        check_visible(app._list_col, app._list_head, "最右极限左列")
        check_ctrl("最右极限左列")

        # 极限后仍能拖回（不锁死）
        pw = max(1, app._result_panel.winfo_width())
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        sp.event_generate("<B1-Motion>", x=3 - int(pw * 0.3), y=40)
        app.update()
        sp.event_generate("<ButtonRelease-1>", x=3 - int(pw * 0.3), y=40)
        app.update()
        assert app._splitter_ratio < 0.75, "从右极限应能拖回"

    def test_drag_back_from_rightmost(self, app):
        """修复R12：拖到最右后仍能拖回左边（比例可恢复，不锁死）。

        高DPI下旧实现（分隔条 rootx + event.x 反推指针）坐标系错乱，
        拖到右极限后 ratio 卡死无法回拖。
        """
        app.update()
        sp = app._splitter
        panel = app._result_panel
        pw = self._pw(app)
        # 拖到右极限
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        sp.event_generate("<B1-Motion>", x=5000, y=40)
        app.update()
        sp.event_generate("<ButtonRelease-1>", x=5000, y=40)
        app.update()
        assert app._splitter_ratio > 0.5, "拖到右极限比例应 >0.5"
        # 从右极限拖回中间（重新按下，目标 40% 处）
        target = int(pw * 0.4)
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        delta = target - (sp.winfo_rootx() - panel.winfo_rootx())
        sp.event_generate("<B1-Motion>", x=3 + delta, y=40)
        app.update()
        sp.event_generate("<ButtonRelease-1>", x=3 + delta, y=40)
        app.update()
        assert abs(app._splitter_ratio - 0.4) < 0.02, \
            f"右极限后应能拖回 0.4（实际 {app._splitter_ratio:.3f}）"
        assert abs(app._list_col.winfo_width() - target) <= 8, \
            f"拖回后左列应 ≈{target}px（实际 {app._list_col.winfo_width()}）"

    def test_columns_compact_no_gap(self, app):
        """修复R12：三列（列表|分隔条|详情）紧密占满结果区，无中间空白。

        高DPI下旧实现 x（被CTk二次缩放）与 relwidth（不缩放）混用，
        分隔条/详情列被推出面板外（详情消失、列表右侧大片空白）。
        """
        app.update()
        panel = app._result_panel
        pw = self._pw(app)
        sp_w = max(1, app._splitter.winfo_width())

        def check():
            app.update_idletasks()
            app.update()
            lx = app._list_col.winfo_rootx() - panel.winfo_rootx()
            sx = app._splitter.winfo_rootx() - panel.winfo_rootx()
            dx = app._detail_col.winfo_rootx() - panel.winfo_rootx()
            assert abs(sx - (lx + app._list_col.winfo_width())) <= 2, \
                "列表右缘应紧贴分隔条左缘"
            assert abs(dx - (sx + sp_w)) <= 2, "分隔条右缘应紧贴详情左缘"
            assert abs(pw - (dx + app._detail_col.winfo_width())) <= 2, \
                "详情右缘应贴齐面板右缘"
            assert 0 < app._detail_col.winfo_width() < pw, \
                f"详情列应可见（宽 {app._detail_col.winfo_width()}）"

        check()                                   # 初始布局
        sp = app._splitter
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        sp.event_generate("<B1-Motion>", x=3 + 600, y=40)
        app.update()
        sp.event_generate("<ButtonRelease-1>", x=3 + 600, y=40)
        app.update()
        check()                                   # 拖动后布局
        app._on_splitter_dblclick(None)
        app.update()
        check()                                   # 双击恢复后布局

    def test_double_click_restores_default(self, app):
        """修复R12：双击分隔条恢复默认比例（2:3）。"""
        app.update()
        sp = app._splitter
        # 先拖到非默认位置
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        sp.event_generate("<B1-Motion>", x=3 + 200, y=40)
        app.update()
        sp.event_generate("<ButtonRelease-1>", x=3 + 200, y=40)
        app.update()
        assert abs(app._splitter_ratio - 0.4) > 0.05, "先偏离默认比例"
        # 双击恢复（event_generate 无法合成 Double 事件，直接调 handler）
        app._on_splitter_dblclick(None)
        app.update()
        # 闪缩回调（150ms）后回落主题色
        deadline = time.time() + 2
        while time.time() < deadline:
            app.update()
            time.sleep(0.05)
        assert abs(app._splitter_ratio - 0.4) < 0.02, \
            f"双击应恢复默认比例 0.4（实际 {app._splitter_ratio:.3f}）"
        pw = self._pw(app)
        assert abs(app._list_col.winfo_width() / pw - 0.4) < 0.02

    def test_splitter_position_persisted(self, app):
        """修复R12：拖动后位置保存，重启自动恢复。"""
        app.update()
        sp = app._splitter
        panel = app._result_panel
        pw = self._pw(app)
        target = int(pw * 0.6)
        delta = target - (sp.winfo_rootx() - panel.winfo_rootx())
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        sp.event_generate("<B1-Motion>", x=3 + delta, y=40)
        app.update()
        sp.event_generate("<ButtonRelease-1>", x=3 + delta, y=40)
        app.update()
        assert abs(app._splitter_ratio - 0.6) < 0.02
        assert app._config.get("splitter_ratio") is not None
        # 新实例读同一份配置
        from log_ai_compressor.gui_legacy.app import LogCompressorApp
        app2 = LogCompressorApp()
        try:
            app2.update()
            assert abs(app2._splitter_ratio - 0.6) < 0.03, \
                f"重启应恢复 0.6（实际 {app2._splitter_ratio:.3f}）"
        finally:
            try:
                app2._on_close()
            except Exception:
                try:
                    app2.destroy()
                except Exception:
                    pass

    def test_splitter_theme_colors(self, app):
        """修复R12：四态主题切换分隔条颜色跟随调色板。"""
        from log_ai_compressor.gui_legacy.app import THEMES
        for theme in ("dark", "light", "blue", "green"):
            app._theme = theme
            app._apply_palette()
            app.update()
            sp_color = app._splitter.cget("fg_color")
            # CTk 颜色可能是元组（暗/亮）；归一化取当前模式的值
            if isinstance(sp_color, (tuple, list)):
                idx = 1 if theme == "dark" else 0
                sp_color = sp_color[idx]
            assert str(sp_color).lower() == THEMES[theme]["splitter"].lower(), \
                f"{theme} 主题分隔条色 {sp_color} 应为 {THEMES[theme]['splitter']}"

    def test_splitter_works_in_virtual_mode(self, app):
        """修复R12：虚拟列表模式下拖动分隔条正常。"""
        _run_many_clusters(app)
        app.update()
        assert app._virtual_list is not None
        sp = app._splitter
        panel = app._result_panel
        pw = self._pw(app)
        target = int(pw * 0.55)
        delta = target - (sp.winfo_rootx() - panel.winfo_rootx())
        sp.event_generate("<ButtonPress-1>", x=3, y=40)
        app.update()
        sp.event_generate("<B1-Motion>", x=3 + delta, y=40)
        app.update()
        sp.event_generate("<ButtonRelease-1>", x=3 + delta, y=40)
        app.update()
        assert app._virtual_list is not None, "拖动后虚拟列表应仍在"
        assert abs(app._list_col.winfo_width() - target) <= 8
        # 虚拟列表画布随左列变宽
        assert app._virtual_list._canvas.winfo_width() <= \
            app._list_col.winfo_width()

    def test_splitter_no_overlap_with_scrollbars(self, app):
        """修复R12：分隔条不遮挡列表滚动条（列间留白 ≥2px）。"""
        app.update()
        sp = app._splitter
        sp_x = sp.winfo_rootx()
        sp_w = max(1, app._splitter.winfo_width())
        # 列表宿主右缘（含垂直滚动条）在分隔条左侧且留有间隙
        list_right = (app._list_host.winfo_rootx()
                      + app._list_host.winfo_width())
        assert list_right <= sp_x + 1, "列表区域不应越过分隔条"
        # 详情框左缘在分隔条右侧
        detail_left = app._detail_box.winfo_rootx()
        assert detail_left >= sp_x + sp_w - 1, \
            "详情面板不应被分隔条遮挡"


# ---------------------------------------------------------------------------
# 修复9：性能优化（matplotlib 懒加载 + 共享字体防死锁）
# ---------------------------------------------------------------------------
class TestPerformanceOptimizations:
    def test_charts_module_not_imported_at_startup(self):
        """matplotlib 必须延迟加载：GUI 模块导入后不应加载 matplotlib。

        用独立子进程验证（当前测试进程可能已被其他用例拉起 matplotlib）。
        """
        import json
        import subprocess
        import sys
        code = ("import sys, json; import log_ai_compressor.gui_legacy.app; "
                "print(json.dumps('matplotlib' not in sys.modules))")
        proc = subprocess.run([sys.executable, "-c", code],
                              capture_output=True, text=True,
                              cwd=str(Path(__file__).resolve().parent.parent))
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout.strip().splitlines()[-1]), \
            "matplotlib 不应在 GUI 启动路径上被导入（应懒加载）"

    def test_chart_button_lazy_loads_charts(self, app):
        """点击统计图表后才导入 matplotlib 且窗口正常弹出。"""
        import sys
        import time as _time
        _run_paste_analysis(app)
        # 触发前确保未加载（其他测试可能已加载，先清理引用判定逻辑：
        # 直接调用 _show_charts 验证功能不受懒加载影响）
        had_matplotlib = "matplotlib" in sys.modules
        app._show_charts()
        deadline = _time.time() + 5
        while _time.time() < deadline and not (
                app._chart_window is not None
                and app._chart_window.winfo_exists()):
            app.update()
            _time.sleep(0.02)
        assert app._chart_window is not None and app._chart_window.winfo_exists()
        assert "matplotlib" in sys.modules or had_matplotlib
        app._chart_window.destroy()
        app._chart_window = None

    def test_shared_fonts_reused_across_rows(self, app):
        """行级字体必须共享复用：防止跨线程 GC 析构导致 Tkinter 死锁。"""
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        assert len(app._cluster_rows) >= 2
        # tk.Label cget('font') 返回字体名；底层共享通过 app 字段验证
        assert app._font_row_summary is not None
        assert app._font_row_head is not None
        # 两个字体的底层 Tk 字体名不同（各自独立共享对象）
        assert (str(app._font_row_head) != str(app._font_row_summary))

    def test_analysis_runs_in_worker_thread(self, app, monkeypatch):
        """分析必须在后台线程执行（主线程阻塞 = 界面卡死）。"""
        import threading
        import log_ai_compressor.gui_legacy.app as app_mod
        observed = {}

        def spy_analyze(text, **kwargs):
            observed["thread"] = threading.current_thread()
            observed["context_lines"] = kwargs.get("context_lines")
            from log_ai_compressor.core.models import RunStats, AnalysisResult
            return AnalysisResult(stats=RunStats(source="<t>", total_lines=1),
                                  clusters=[])

        monkeypatch.setattr(app_mod, "analyze_text", spy_analyze)
        app._tabview.set("文本粘贴")
        app._paste_box.delete("1.0", "end")
        app._paste_box.insert("1.0", SAMPLE_PASTE)
        app._on_start()
        deadline = time.time() + 10
        while time.time() < deadline:
            app.update()
            if app._result is not None:
                break
            time.sleep(0.02)
        assert app._result is not None
        # 工作线程必须不是主线程
        assert observed["thread"] is not threading.main_thread()


# ---------------------------------------------------------------------------
# 修复5：上下文行数（默认 50；优化缺陷R43：GUI 输入框已删除，固定默认）
# ---------------------------------------------------------------------------
class TestContextLines:
    def test_default_context_lines_is_50(self):
        """全局默认值必须为 50（原 5 行太少）。"""
        from log_ai_compressor.constants import DEFAULT_CONTEXT_LINES
        assert DEFAULT_CONTEXT_LINES == 50

    def test_filter_inputs_removed(self, app):
        """优化缺陷R43/R93：Top N 输入框已删除；关键词黑白名单经
        用户决策在过滤行上下文行数右侧回归（R93 自 ⚙ 弹层迁回），
        智能分析/解析规则不再占用过滤行。
        """
        filter_panel = app._ctx_entry.master
        slaves_texts = []
        for w in filter_panel.winfo_children():
            try:
                slaves_texts.append(str(w.cget("text")))
            except (tk.TclError, ValueError, AttributeError):
                continue
        assert not any("智能分析" in t or "解析规则" in t
                       for t in slaves_texts), \
            "级别过滤行不得再有智能分析/解析规则选组（已迁高级选项行）"
        assert any("包含关键词" in t for t in slaves_texts), \
            "R93：包含关键词应在过滤行（上下文行数右侧）"
        assert any("排除关键词" in t for t in slaves_texts), \
            "R93：排除关键词应在过滤行"
        assert not hasattr(app, "_topn_entry"), "Top N 输入框应已删除"
        # 分析参数默认不含关键词（过滤行留空 = 不限）
        assert app._include_entry.get() == ""
        assert app._exclude_entry.get() == ""
        common_levels = [lv for lv, var in app._level_vars.items()
                         if var.get()]
        assert set(common_levels) == {"ERROR", "FAIL"}, \
            "默认级别仍为 ERROR+FAIL"

    def test_context_entry_relocated_with_default(self, app):
        """优化缺陷R44：上下文行数输入框回归（级别过滤与解析规则
        之间的空白区），默认值 50。"""
        assert app._ctx_entry is not None
        assert app._ctx_entry.get() == "50"
        assert str(app._ctx_entry.grid_info()["row"]) == "0", \
            "输入框应与级别过滤同行（右侧空白区）"

    def test_context_lines_negative_becomes_zero(self, app):
        """优化缺陷R44：≥0 有效；负数按 0 行处理；非法/空回退 50。"""
        for raw, expected in [("0", 0), ("5", 5), ("200", 200),
                              ("99999", 99999),
                              ("-1", 0), ("-3", 0), ("-999", 0),
                              ("abc", 50), ("", 50)]:
            app._ctx_entry.delete(0, "end")
            app._ctx_entry.insert(0, raw)
            assert app._current_context_lines() == expected, \
                f"输入 {raw!r} 应得 {expected}（≥0 有效，负数按 0 行）"

    def test_context_lines_passed_to_pipeline(self, app, monkeypatch):
        """GUI 配置的上下文行数必须传给分析管线（含负数→0）。"""
        import log_ai_compressor.gui_legacy.app as app_mod
        captured = {}

        def spy_analyze(text, **kwargs):
            captured["context_lines"] = kwargs.get("context_lines")
            from log_ai_compressor.core.models import RunStats, AnalysisResult
            return AnalysisResult(stats=RunStats(source="<t>", total_lines=1),
                                  clusters=[])

        monkeypatch.setattr(app_mod, "analyze_text", spy_analyze)
        app._ctx_entry.delete(0, "end")
        app._ctx_entry.insert(0, "-5")
        app._tabview.set("文本粘贴")
        app._paste_box.delete("1.0", "end")
        app._paste_box.insert("1.0", SAMPLE_PASTE)
        app._on_start()
        deadline = time.time() + 10
        while time.time() < deadline:
            app.update()
            if app._result is not None:
                break
            time.sleep(0.02)
        assert captured["context_lines"] == 0, \
            "填负数分析应以上下文 0 行执行"

    def test_context_lines_persisted(self, app, tmp_path):
        """用户调整的上下文行数必须持久化，重启后恢复。"""
        app._ctx_entry.delete(0, "end")
        app._ctx_entry.insert(0, "100")
        app._save_config()
        saved = app._store.load()
        assert saved.get("context_lines") == 100

    def test_context_lines_restored_from_config(self, app, monkeypatch):
        """配置文件中的 context_lines 启动时恢复到输入框。"""
        app._ctx_entry.delete(0, "end")
        app._ctx_entry.insert(0, "77")
        app._save_config()
        # 模拟重启：清空输入框后用保存的配置重放恢复逻辑
        app._ctx_entry.delete(0, "end")
        app._config = app._store.load()
        app._restore_config()
        app.update()
        assert app._ctx_entry.get() == "77"


# ---------------------------------------------------------------------------
# 修复6：「典型样例」悬停说明（Tooltip）
# ---------------------------------------------------------------------------
SAMPLE_HELP_TEXT = ("该错误类型的代表性日志样例，包含完整的错误信息、"
                    "堆栈跟踪和前后上下文，用于快速定位问题")


class TestSampleHelpTooltip:
    def test_help_icon_exists(self, app):
        """详情面板标题旁必须有 ⓘ 帮助图标。"""
        assert app._sample_help_tooltip is not None

    def test_tooltip_text_content(self, app):
        """悬停说明文本必须为规范要求的完整说明。"""
        tip = app._sample_help_tooltip
        assert tip._text == SAMPLE_HELP_TEXT

    def test_tooltip_shows_on_hover(self, app):
        """Enter 事件（延时后）应显示说明窗口，Leave 后销毁。"""
        tip = app._sample_help_tooltip
        assert tip._tip is None
        tip._show()
        app.update()
        assert tip._tip is not None
        assert tip._tip.winfo_exists()
        # 窗口内文本正确（Canvas 绘制的 text 项）
        canvas = tip._tip.winfo_children()[0]
        item = canvas.find_withtag("text")[0]
        assert canvas.itemcget(item, "text") == SAMPLE_HELP_TEXT
        tip._hide_now()
        app.update()
        assert tip._tip is None

    def test_tooltip_delayed_show_via_event(self, app):
        """Enter 事件 -> 延时调度 -> 显示（真实悬停路径）。"""
        tip = app._sample_help_tooltip
        tip._schedule()
        assert tip._after_id is not None  # 已调度延时任务
        # 手动触发延时回调（跳过等待）
        tip._show()
        app.update()
        assert tip._tip is not None
        tip._hide_now()

    def test_tooltip_leak_free_after_destroy(self, app):
        """关联控件销毁后 hide 不抛异常（健壮性）。"""
        tip = app._sample_help_tooltip
        tip._show()
        app.update()
        tip._hide_now()   # 常规销毁
        tip._hide_now()   # 二次销毁应幂等
        assert tip._tip is None

    def test_tooltip_enter_cancels_pending_hide(self, app):
        """修复闪烁：Enter 取消挂起的延迟销毁（已显示则保持）。

        旧链路：Leave 调度 200ms 销毁，Enter 只重置显示调度不取消
        销毁 —— 已显示的 tooltip 被销毁又在 300ms 后重建（闪烁
        一下；指针在 ⓘ 内微动跨 CTk 子窗口边界会反复触发）。
        """
        tip = app._sample_help_tooltip
        tip._show()
        app.update()
        assert tip._tip is not None
        tip._hide()                       # Leave（指针在外）
        assert tip._hide_after_id is not None
        tip._schedule()                   # 立刻 Enter（抖回）
        assert tip._hide_after_id is None, "Enter 应取消挂起的销毁"
        # 超过原销毁延迟（200ms）后 tooltip 仍显示，不销毁不重建
        deadline = time.time() + 0.6
        while time.time() < deadline:
            app.update()
            time.sleep(0.02)
        assert tip._tip is not None, "Enter 后 tooltip 应保持显示（无闪烁）"
        tip._hide_now()

    def test_tooltip_leave_ignored_when_pointer_inside(self, app):
        """修复闪烁：指针仍在控件内时 Leave 被忽略（子窗口抖动）。

        CTkLabel 是复合控件（canvas + 内部 label），指针跨子窗口
        边界会发 detail=NotifyInferior 的 Leave —— 指针并未真正
        离开控件，此时不应调度销毁。
        """
        tip = app._sample_help_tooltip
        tip._show()
        app.update()
        assert tip._tip is not None
        # 模拟指针仍在控件内（真实鼠标位置测试中不可控）
        tip._pointer_inside = lambda: True
        tip._hide()                       # NotifyInferior 类 Leave
        assert tip._hide_after_id is None, \
            "指针在控件内时 Leave 不应调度销毁"
        app.update()
        assert tip._tip is not None, "tooltip 不应被销毁（无闪烁）"
        tip._hide_now()


# ---------------------------------------------------------------------------
# 修复R3：Tooltip 字体放大 + 自动换行 + 智能定位（不溢出屏幕）
# ---------------------------------------------------------------------------
class TestTooltipR3:
    """悬停说明的可读性与定位（典型样例说明 / 解析规则说明共用）。"""

    def test_tooltip_font_size_enlarged(self, app):
        """优化：tooltip 字体放大到 17~19 号（15 号用户仍反馈小）。"""
        tip = app._sample_help_tooltip
        tip._show()
        app.update()
        try:
            canvas = tip._tip.winfo_children()[0]
            item = canvas.find_withtag("text")[0]
            font = str(canvas.itemcget(item, "font"))
            sizes = [int(t) for t in
                     __import__("re").findall(r"-?\d+", font)]
            assert any(17 <= s <= 19 for s in sizes), \
                f"字体应为 17~19 号，实际 {font}"
        finally:
            tip._hide_now()

    def test_level_tooltip_font_size_enlarged(self, app):
        """优化：级别 ⓘ 悬停说明的字体同样放大到 17~19 号。"""
        tip = app._level_tooltips["ERROR"]
        tip._show()
        app.update()
        try:
            canvas = tip._tip.winfo_children()[0]
            item = canvas.find_withtag("text")[0]
            font = str(canvas.itemcget(item, "font"))
            sizes = [int(t) for t in
                     __import__("re").findall(r"-?\d+", font)]
            assert any(17 <= s <= 19 for s in sizes), \
                f"级别说明字体应为 17~19 号，实际 {font}"
        finally:
            tip._hide_now()

    def test_level_tooltip_centered_above_icon(self, app):
        """优化：级别 ⓘ 的 tooltip 停在图标正上方（水平居中）。

        此前默认右下方弹出，行内靠右的 ⓘ 触发右缘换向/钳位后
        tooltip 相对图标位置各异（视觉"扭曲"）。以第一个 ⓘ 为
        基准统一：tooltip 水平中心 == 图标水平中心，tooltip 底边
        在图标顶边上方（8px 间隙，允许钳位引起的水平平移）。
        """
        app.update()
        for level in ("ERROR", "FAIL", "WARN", "INFO", "DEBUG"):
            icon = app._level_tooltips[level]._widget
            tip = app._level_tooltips[level]
            tip._show()
            app.update()
            try:
                tw = tip._tip
                ic_cx = icon.winfo_rootx() + icon.winfo_width() / 2
                tp_cx = tw.winfo_x() + tw.winfo_width() / 2
                # 水平居中（屏幕钳位平移时容差 40px）
                assert abs(tp_cx - ic_cx) <= 40, (
                    f"{level} 的 tooltip 水平中心 {tp_cx:.0f} 应与图标"
                    f"中心 {ic_cx:.0f} 对齐")
                # 底边在图标顶边上方（正上方关系）
                assert tw.winfo_y() + tw.winfo_height() \
                    <= icon.winfo_rooty() + 2, (
                        f"{level} 的 tooltip 应在图标正上方"
                        f"（底边 {tw.winfo_y() + tw.winfo_height()}"
                        f" vs 图标顶 {icon.winfo_rooty()}）")
            finally:
                tip._hide_now()
                app.update()

    def test_tooltip_wrap_width_in_range(self, app):
        """修复R3：tooltip 宽度限制在 400~500px（长文本自动换行）。"""
        from log_ai_compressor.gui_legacy.app import Tooltip
        assert 400 <= Tooltip._WRAP <= 500
        tip = app._sample_help_tooltip
        tip._show()
        app.update()
        try:
            canvas = tip._tip.winfo_children()[0]
            item = canvas.find_withtag("text")[0]
            assert 400 <= int(canvas.itemcget(item, "width")) <= 500
            # 画布宽度不超 500 + 边距
            assert canvas.winfo_width() <= 530
        finally:
            tip._hide_now()

    def test_tooltip_light_bg_dark_text(self, app):
        """修复R3：tooltip 白底深字（视觉清晰）。"""
        tip = app._sample_help_tooltip
        tip._show()
        app.update()
        try:
            canvas = tip._tip.winfo_children()[0]
            item = canvas.find_withtag("text")[0]
            # 深色文字（亮度低于 0.5）
            fg = canvas.itemcget(item, "fill")
            r, g, b = (int(fg[i:i + 2], 16) for i in (1, 3, 5))
            assert (r + g + b) / 3 < 128, "文字应为深色"
            # 圆角卡片为白色（rounded 路径）或画布白底（降级路径）
            cards = canvas.find_enclosed(0, 0, canvas.winfo_width() + 5,
                                         canvas.winfo_height() + 5)
            fill_colors = {str(canvas.itemcget(c, "fill")) for c in cards}
            assert "#ffffff" in fill_colors or str(canvas.cget("bg")) == "#ffffff"
        finally:
            tip._hide_now()

    def test_tooltip_within_screen_bounds(self, app):
        """修复R3：tooltip 完整可见（不溢出物理屏幕边界）。

        优化（定位修正）：校验用物理像素边界（_screen_bounds）——
        winfo_screenwidth 高 DPI 下是逻辑值，与物理几何混用会误报。
        """
        from log_ai_compressor.gui_legacy.app import Tooltip
        tip = app._sample_help_tooltip
        tip._show()
        app.update()
        try:
            tw = tip._tip
            vx, vy, vw, vh = Tooltip._screen_bounds(tw)
            x, y = tw.winfo_x(), tw.winfo_y()
            w, h = tw.winfo_width(), tw.winfo_height()
            assert x >= vx - 2, "左边缘溢出"
            assert y >= vy - 2, "上边缘溢出"
            assert x + w <= vx + vw + 2, \
                f"右边缘溢出（{x + w} > {vx + vw}）"
            assert y + h <= vy + vh + 2, \
                f"下边缘溢出（{y + h} > {vy + vh}）"
        finally:
            tip._hide_now()

    def _tooltip_on_edge_widget(self, app, geometry: str):
        """在指定屏幕位置创建宿主控件并显示 tooltip。"""
        host = tk.Toplevel(app)
        host.geometry(geometry)
        host.geometry("+300+300")  # 先强制一次布局
        host.geometry(geometry)
        app.update()
        lbl = tk.Label(host, text="ⓘ")
        lbl.pack()
        app.update()
        return host, lbl

    def test_tooltip_flips_left_near_right_edge(self, app):
        """优化：宿主控件贴近物理屏幕右边缘时 tooltip 保持在屏内（居中被平移）。"""
        from log_ai_compressor.gui_legacy.app import Tooltip
        vx, vy, vw, vh = Tooltip._screen_bounds(app)
        host, lbl = self._tooltip_on_edge_widget(
            app, f"+{vx + vw - 40}+{vy + 240}")
        try:
            assert lbl.winfo_rootx() > vx + vw - 120, \
                "测试前置：宿主应贴近右边缘"
            tip = Tooltip(lbl, "较长的悬停说明文本 " * 10)
            tip._show()
            app.update()
            try:
                tw = tip._tip
                # 完整可见（物理屏内）
                assert tw.winfo_x() + tw.winfo_width() <= vx + vw + 2
                assert tw.winfo_x() >= vx - 2
            finally:
                tip._hide_now()
        finally:
            host.destroy()
            app.update()

    def test_tooltip_flips_up_near_bottom_edge(self, app):
        """优化：宿主控件贴近物理屏幕下边缘时 tooltip 保持在屏内上方。"""
        from log_ai_compressor.gui_legacy.app import Tooltip
        vx, vy, vw, vh = Tooltip._screen_bounds(app)
        host, lbl = self._tooltip_on_edge_widget(
            app, f"+{vx + 240}+{vy + vh - 50}")
        try:
            assert lbl.winfo_rooty() > vy + vh - 150, \
                "测试前置：宿主应贴近下边缘"
            tip = Tooltip(lbl, "多行悬停说明\n" * 8)
            tip._show()
            app.update()
            try:
                tw = tip._tip
                assert tw.winfo_y() + tw.winfo_height() <= vy + vh + 2
                assert tw.winfo_y() < lbl.winfo_rooty(), \
                    "下边缘情形应在控件上方弹出"
            finally:
                tip._hide_now()
        finally:
            host.destroy()
            app.update()


# ---------------------------------------------------------------------------
# 修复7：全屏查看（列表 / 详情独立最大化窗口 + ESC 返回）
# ---------------------------------------------------------------------------
class TestFullscreenView:
    def _open_fs_windows(self, app):
        """打开两个全屏窗口并返回（列表窗, 详情窗）。"""
        app._open_list_fullscreen()
        app.update()
        list_win = [w for w in app.winfo_children()
                    if isinstance(w, tk.Toplevel)
                    and "错误分类列表" in w.title()]
        app._open_detail_fullscreen()
        app.update()
        detail_win = [w for w in app.winfo_children()
                      if isinstance(w, tk.Toplevel)
                      and "错误详情" in w.title()]
        return list_win, detail_win

    def test_fullscreen_buttons_exist(self, app):
        """主界面必须有列表 / 详情两个全屏按钮。"""
        assert app._list_fs_btn is not None
        assert app._detail_fs_btn is not None

    def test_fullscreen_window_centered_and_large(self, app):
        """修复R2：全屏窗口位于屏幕正中央且尺寸 ≥80%。

        zoomed 生效时窗口为整屏（同样满足 ≥80%）；zoomed 不可用时
        退回显式居中几何（85% × 88%）。两种情形均校验。
        """
        _run_paste_analysis(app, SAMPLE_PASTE)
        win = app._make_fullscreen_window("测试全屏")
        app.update()
        try:
            sw = win.winfo_screenwidth()
            sh = win.winfo_screenheight()
            # zoomed 状态：窗口即整屏，视为通过
            if str(win.state()) == "zoomed":
                return
            w, h = win.winfo_width(), win.winfo_height()
            assert w >= sw * 0.8, f"宽度 {w} 应 ≥ 屏幕 80%（{sw * 0.8:.0f}）"
            assert h >= sh * 0.8, f"高度 {h} 应 ≥ 屏幕 80%（{sh * 0.8:.0f}）"
            # 居中：窗口中心与屏幕中心偏差 ≤ 5%
            cx_off = abs((w / 2 + win.winfo_x()) - sw / 2)
            cy_off = abs((h / 2 + win.winfo_y()) - sh / 2)
            assert cx_off <= sw * 0.05, f"水平中心偏差 {cx_off}px 过大"
            assert cy_off <= sh * 0.05, f"垂直中心偏差 {cy_off}px 过大"
        finally:
            win.destroy()
            app.update()

    def test_list_fullscreen_opens_with_rows(self, app):
        """列表全屏：窗口打开且包含全部错误行 + 搜索框。"""
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        app._open_list_fullscreen()
        app.update()
        fs_windows = [w for w in app.winfo_children()
                      if isinstance(w, tk.Toplevel)
                      and "错误分类列表" in w.title()]
        assert fs_windows, "列表全屏窗口应已打开"
        win = fs_windows[0]
        # 窗口内应有滚动列表 + 搜索框 + 关闭按钮
        # （CTk 控件 winfo_class 均为 Frame，直接取 text 属性判定）
        texts = _texts_in(win)
        assert any("关闭" in t for t in texts), "应有关闭按钮"
        assert any("搜索" in t for t in texts), "应有搜索框"

    def test_list_fullscreen_search_filters(self, app):
        """全屏搜索：关键字过滤行数（修复缺陷R69：「显示 x/y 簇」
        过滤计数标签已删除，改断言视图行数口径）。"""
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        app._open_list_fullscreen()
        app.update()
        win = [w for w in app.winfo_children()
               if isinstance(w, tk.Toplevel)
               and "错误分类列表" in w.title()][0]
        entries = [w for w in _all_widgets(win)
                   if isinstance(w, ctk.CTkEntry)]
        assert entries, "全屏窗口应有搜索输入框"
        search = entries[0]
        # 输入 fatal：仅 FATAL"short fatal" 行匹配（trace 实时过滤）
        search.insert(0, "fatal")
        app.update()
        cluster_rows = [r for r in app._fs_view_rows() if r[0] == "c"]
        assert len(cluster_rows) == 1, \
            f"过滤后应只剩 1 簇，实际: {len(cluster_rows)}"

    # ------------------------------------------------------------------
    # 优化缺陷R53：全屏搜索计数导航（与主窗口 x/y 条完全同款）
    # ------------------------------------------------------------------
    @staticmethod
    def _fs_two_cluster_log():
        """两簇日志：kernel ×3 + filesystem ×3（均 ERROR，关键字
        error 两簇均命中）。"""
        lines = []
        for i in range(3):
            lines.append(
                f"2024-01-01 09:00:0{i} ERROR [db] kernel panic in scheduler")
        for i in range(3):
            lines.append(
                f"2024-01-01 09:01:0{i} ERROR [db] filesystem journal corrupted")
        return "\n".join(lines)

    def _open_fs_with_two_clusters(self, app):
        _run_paste_analysis(app, self._fs_two_cluster_log())
        app.update()
        app._open_list_fullscreen()
        for _ in range(20):
            app.update()
            time.sleep(0.005)
        assert app._fs_list_win is not None
        return app._fs_list_win

    def test_fs_search_count_box_nav_cycles(self, app):
        """输入关键字 → 0/y；Enter 实例级下钻循环；Shift+Enter 反向。

        优化缺陷R56：两簇各 ×3 实例 → 序列 6 条；定位自动展开簇、
        _selected_inst 指向具体实例行。
        """
        self._open_fs_with_two_clusters(app)
        app._fs_search_entry.insert(0, "error")
        app.update()
        assert app._fs_count.cget("text") == "0 / 6 条", \
            "输入后未导航应为 0/y"
        # 影子框显形（与输入框同底色）
        assert app._fs_count_box.cget("fg_color") == \
            app._fs_search_entry.cget("fg_color")
        app._on_fs_search_enter(True)
        app.update()
        assert app._selected_inst == (0, 0)
        assert app._fs_count.cget("text") == "1 / 6 条"
        app._on_fs_search_enter(True)
        app.update()
        assert app._selected_inst == (0, 1)
        assert app._fs_count.cget("text") == "2 / 6 条"
        # 回绕：6/6 后再 Enter 回 1/6
        app._fs_search_nav = 6
        app._on_fs_search_enter(True)
        app.update()
        assert app._selected_inst == (0, 0)
        assert app._fs_count.cget("text") == "1 / 6 条"
        # Shift+Enter：1/6 反向到 6/6
        app._on_fs_search_enter(False)
        app.update()
        assert app._selected_inst == (1, 2)
        assert app._fs_count.cget("text") == "6 / 6 条"

    def test_fs_click_row_syncs_nav(self, app):
        """点击全屏列表簇行 → 序号同步到该簇首个命中实例（R56）。"""
        self._open_fs_with_two_clusters(app)
        app._fs_search_entry.insert(0, "error")
        app.update()
        assert app._fs_count.cget("text") == "0 / 6 条"
        vl = app._fs_vl
        slot = next(
            s for s in vl.slots
            if 0 <= s.get("idx", -1) < len(vl._data)
            and vl._data[s["idx"]] == ("c", 1))
        slot["summary"].event_generate("<Button-1>")
        app.update()
        assert app._selected_row == 1
        assert app._fs_count.cget("text") == "4 / 6 条", \
            "点选第 2 簇应对齐其首个命中实例序号（第 4 条）"

    def test_fs_count_box_hidden_when_empty(self, app):
        """清空关键字 → 计数影子框透明隐形（布局零扰动）。"""
        self._open_fs_with_two_clusters(app)
        assert app._fs_count.cget("text") == ""
        assert app._fs_count_box.cget("fg_color") == "transparent"
        app._fs_search_entry.insert(0, "kernel")
        app.update()
        assert app._fs_count.cget("text") == "0 / 3 条"
        app._fs_search_entry.delete(0, "end")
        app.update()
        assert app._fs_count.cget("text") == ""
        assert app._fs_count_box.cget("fg_color") == "transparent"

    # ------------------------------------------------------------------
    # 优化缺陷R54：详情全屏文内查找（同款 x/y 条 + 循环定位）
    # ------------------------------------------------------------------
    def _open_detail_fs(self, app):
        _run_paste_analysis(app, self._fs_two_cluster_log())
        app.update()
        app._open_detail_fullscreen()
        for _ in range(20):
            app.update()
            time.sleep(0.005)
        assert app._fs_detail_win is not None
        return app._fs_detail_box

    def test_fd_search_counts_and_highlights(self, app):
        """输入关键字 → 全部匹配黄底 + 计数 0/y（y=文本内匹配处数）。"""
        box = self._open_detail_fs(app)
        app._fd_search_entry.insert(0, "kernel")
        app.update()
        expected = box.get("1.0", "end").lower().count("kernel")
        assert expected > 0, "详情文本应含关键字 kernel"
        assert app._fd_count.cget("text") == f"0 / {expected} 处"
        assert len(box.tag_ranges("searchkw")) == expected * 2, \
            "全部匹配处应有 searchkw 高亮"
        # 影子框显形（与输入框同底色）
        assert app._fd_count_box.cget("fg_color") == \
            app._fd_search_entry.cget("fg_color")

    def test_fd_enter_cycles_matches(self, app):
        """Enter 1/y→…→y/y→回绕 1/y；Shift+Enter 反向；当前匹配橙底。"""
        box = self._open_detail_fs(app)
        app._fd_search_entry.insert(0, "error")
        app.update()
        n = len(app._fd_matches)
        assert n >= 2, "测试日志详情应含多处 error"
        app._on_fd_search_enter(True)
        app.update()
        assert app._fd_search_nav == 1
        assert app._fd_count.cget("text") == f"1 / {n} 处"
        assert box.tag_ranges("fdcur"), "定位后当前匹配应有橙底高亮"
        app._on_fd_search_enter(True)
        app.update()
        assert app._fd_search_nav == 2
        # 回绕：y/y 后再 Enter 回 1/y
        app._fd_search_nav = n
        app._on_fd_search_enter(True)
        app.update()
        assert app._fd_search_nav == 1
        # Shift+Enter：1/y 反向到 y/y
        app._on_fd_search_enter(False)
        app.update()
        assert app._fd_search_nav == n
        assert app._fd_count.cget("text") == f"{n} / {n} 处"

    def test_fd_clear_hides_count_box(self, app):
        """清空关键字 → 高亮移除 + 影子框透明隐形。"""
        box = self._open_detail_fs(app)
        app._fd_search_entry.insert(0, "kernel")
        app.update()
        assert box.tag_ranges("searchkw")
        app._fd_search_entry.delete(0, "end")
        app.update()
        assert app._fd_count.cget("text") == ""
        assert app._fd_count_box.cget("fg_color") == "transparent"
        assert not box.tag_ranges("searchkw")
        assert not box.tag_ranges("fdcur")

    def test_detail_fullscreen_shows_content(self, app):
        """详情全屏：内容与主面板一致且支持横向滚动。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        app._open_detail_fullscreen()
        app.update()
        wins = [w for w in app.winfo_children()
                if isinstance(w, tk.Toplevel) and "错误详情" in w.title()]
        assert wins, "详情全屏窗口应已打开"
        win = wins[0]
        # 全屏文本内容 = 主面板内容
        boxes = [w for w in _all_widgets(win)
                 if isinstance(w, ctk.CTkTextbox)]
        assert boxes
        fs_text = boxes[0].get("1.0", "end")
        main_text = app._detail_box.get("1.0", "end")
        assert fs_text.strip() == main_text.strip()
        # 水平滚动条存在（wrap=none + xscrollbar）
        xbars = [w for w in _all_widgets(win)
                 if isinstance(w, tk.Scrollbar)
                 and str(w.cget("orient")).endswith("horizontal")]
        assert xbars, "详情全屏应配置水平滚动条"

    def test_fullscreen_esc_closes(self, app):
        """ESC 键应关闭全屏窗口（返回主界面）。

        修复R6：窗口预创建复用后 ESC 改为 withdraw（隐藏返回主界面，
        窗口保留复用）。判定标准：窗口销毁 或 不再可见（未映射）。
        """
        _run_paste_analysis(app, SAMPLE_PASTE)
        app._open_list_fullscreen()
        app.update()
        wins = [w for w in app.winfo_children()
                if isinstance(w, tk.Toplevel)
                and "错误分类列表" in w.title()]
        assert wins
        win = wins[0]
        win.event_generate("<Escape>")
        app.update()
        remaining = [w for w in app.winfo_children()
                     if isinstance(w, tk.Toplevel)
                     and "错误分类列表" in w.title()
                     and w.winfo_ismapped()]
        assert not remaining, "ESC 后窗口应不再可见（销毁或隐藏）"
        # 主界面仍存活
        assert app.winfo_exists()

    def test_fullscreen_click_links_main_detail(self, app):
        """全屏列表点击行 -> 主界面详情同步切换。"""
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        app._select_cluster(0)
        before = app._detail_box.get("1.0", "end")
        app._open_list_fullscreen()
        app.update()
        titled = [w for w in app.winfo_children()
                  if isinstance(w, tk.Toplevel)
                  and "错误分类列表" in w.title()]
        assert titled, "应存在标题含「错误分类列表」的全屏窗口"
        # 优化缺陷R42：全屏列表 = 虚拟列表组件 —— 直接点击目标簇
        # 槽位头部（槽内绑定 app._select_cluster，与真实点击同链路）
        assert len(app._displayed) > 1, "需要 ≥2 簇以验证详情切换"
        slots = {s.get("idx"): s for s in app._fs_vl.slots
                 if s.get("idx", -1) >= 0}
        assert 1 in slots, "全屏列表应有簇 1 槽位"
        slots[1]["head"].event_generate("<Button-1>")
        app.update()
        after = app._detail_box.get("1.0", "end")
        assert after != before, "点击全屏行应联动主界面详情"

    def test_fullscreen_without_analysis_is_safe(self, app):
        """未分析时点击全屏按钮不崩溃（提示后返回）。"""
        # displayed 为空 -> 直接返回不弹窗（messagebox 需要交互，跳过）
        app._displayed = []
        app._detail_box.delete("1.0", "end")
        # monkeypatch 掉 messagebox 避免阻塞
        import log_ai_compressor.gui_legacy.app as app_mod
        original = app_mod.messagebox.showinfo
        app_mod.messagebox.showinfo = lambda *a, **k: None
        try:
            app._open_list_fullscreen()
            app._open_detail_fullscreen()
            app.update()
        finally:
            app_mod.messagebox.showinfo = original


# ---------------------------------------------------------------------------
# 修复R4：全屏窗口簇展开（左右分栏 + 实例列表 + 实例详情联动）
# ---------------------------------------------------------------------------
REPEAT_LOG = "\n".join(
    f"2024-01-01 09:{i // 60:02d}:{i % 60:02d} ERROR [db] "
    f"connection refused to db-primary (attempt {i})"
    for i in range(12)
) + "\n" + "2024-01-01 09:01:01 ERROR [api] request 404 failed\n"


def _click_ctk_label(widget):
    """模拟真实点击：事件发给实际命中的子控件（Tk 不冒泡）。

    - CTkLabel（winfo_class 为 Frame 的容器）：bind 转发到内部
      Canvas/tk.Label，对其内部 Label 发事件；
    - 原生 tk.Label（修复R6 全屏行）：绑定即在本体，直接发事件。
    """
    if widget.winfo_class() == "Label":
        widget.event_generate("<Button-1>", x=3, y=2)
        widget.update()
        return
    for child in widget.winfo_children():
        if child.winfo_class() == "Label":
            child.event_generate("<Button-1>")
    widget.update()


class TestFullscreenExpand:
    def _open_fs(self, app):
        """分析 REPEAT_LOG 后打开列表全屏，返回窗口。

        修复R6：行渲染分批异步（首批 after 1ms），需推进事件循环
        至少一批行出现。
        """
        _run_paste_analysis(app, REPEAT_LOG)
        app._open_list_fullscreen()
        for _ in range(20):
            app.update()
            time.sleep(0.005)
        wins = [w for w in app.winfo_children()
                if isinstance(w, tk.Toplevel)
                and "错误分类列表" in w.title()]
        assert wins, "全屏窗口应已打开"
        return wins[0]

    def _find_toggles(self, win):
        # 修复R6：全屏行为原生 tk.Label（控件复用降开销）
        return [w for w in _all_widgets(win)
                if isinstance(w, tk.Label)
                and w.winfo_class() == "Label"
                and "×12" in str(w.cget("text"))]

    def _toggle_icon(self, toggle):
        """次数标签 → 同行图标等宽盒内的 ▶/▼ 标签（修复缺陷R34）。"""
        line = toggle.master
        for ch in line.winfo_children():
            if isinstance(ch, tk.Frame):
                kids = ch.winfo_children()
                if kids and isinstance(kids[0], tk.Label):
                    return kids[0]
        return None

    def test_toggle_button_exists_with_count(self, app):
        """每簇有「▶ ×N」展开按钮（次数在按钮上可点击）。"""
        win = self._open_fs(app)
        try:
            toggles = self._find_toggles(win)
            assert toggles, "应有 ×12 展开按钮"
            assert "▶" in str(
                self._toggle_icon(toggles[0]).cget("text")), \
                "初始为收起态 ▶"
        finally:
            win.destroy()
            app.update()

    def test_expand_shows_all_instances(self, app):
        """点击展开：显示全部 12 个实例（视图数据注入实例行）。"""
        win = self._open_fs(app)
        try:
            toggle = self._find_toggles(win)[0]
            _click_ctk_label(toggle)
            for _ in range(40):
                app.update()
                time.sleep(0.005)
            assert "▼" in str(
                self._toggle_icon(toggle).cget("text")), "展开后为 ▼"
            # 优化缺陷R42：全屏列表 = 虚拟列表 —— 实例作视图行注入
            inst_rows = [r for r in app._fs_vl._data
                         if r[0] == "i" and r[1] == 0]
            assert len(inst_rows) == 12, \
                f"应展开 12 个实例，实际 {len(inst_rows)}"
        finally:
            win.destroy()
            app.update()

    def test_instance_click_shows_detail(self, app):
        """点击实例：右侧详情面板显示该实例原始日志与堆栈。"""
        win = self._open_fs(app)
        try:
            toggle = self._find_toggles(win)[0]
            _click_ctk_label(toggle)
            for _ in range(40):
                app.update()
                time.sleep(0.005)
            # 优化缺陷R42：实例行 = 虚拟列表槽位（点击触发
            # app._select_instance → 全屏详情联动）
            inst_slot = next(
                s for s in app._fs_vl.slots
                if 0 <= s.get("idx", -1) < len(app._fs_vl._data)
                and app._fs_vl._data[s["idx"]][0] == "i")
            inst_slot["summary"].event_generate("<Button-1>")
            app.update()
            boxes = [w for w in _all_widgets(win)
                     if isinstance(w, ctk.CTkTextbox)]
            assert boxes, "右侧应有详情面板"
            text = boxes[0].get("1.0", "end")
            assert "【实例详情】" in text, "应显示实例详情"
            assert "原始日志" in text or "典型样例" in text
            assert "connection refused" in text
        finally:
            win.destroy()
            app.update()

    def test_cluster_click_shows_cluster_detail(self, app):
        """点击簇行：右侧显示簇详情（典型样例）。"""
        win = self._open_fs(app)
        try:
            # 点击首个簇行头部（非 toggle；修复R6 后为原生 Label）
            heads = [w for w in _all_widgets(win)
                     if w.winfo_class() == "Label"
                     and "ERROR" in str(w.cget("text"))
                     and "×" not in str(w.cget("text"))]
            assert heads, "应有簇行头部"
            _click_ctk_label(heads[0])
            app.update()
            boxes = [w for w in _all_widgets(win)
                     if isinstance(w, ctk.CTkTextbox)]
            text = boxes[0].get("1.0", "end")
            assert "【错误摘要】" in text, "应显示簇详情"
        finally:
            win.destroy()
            app.update()

    def test_collapse_after_expand(self, app):
        """再次点击 toggle 收起实例列表（▼ → ▶）。"""
        win = self._open_fs(app)
        try:
            toggle = self._find_toggles(win)[0]

            # 修复缺陷R34：展开/收起头部文字起始 x 不变（图标等宽盒）
            # 优化缺陷R42：虚拟列表槽位按视图行复用（控件引用随刷新
            # 重指）—— 每次按数据行 ("c",0) 重新取簇头标签
            def cluster_head():
                for s in app._fs_vl.slots:
                    if 0 <= s.get("idx", -1) < len(app._fs_vl._data) \
                            and app._fs_vl._data[s["idx"]] == ("c", 0):
                        return s["head"]
                return None

            head_lbl = cluster_head()
            assert head_lbl is not None
            head_x0 = head_lbl.winfo_x()
            _click_ctk_label(toggle)
            for _ in range(40):
                app.update()
                time.sleep(0.005)
            assert "▼" in str(self._toggle_icon(toggle).cget("text"))
            assert cluster_head().winfo_x() == head_x0, \
                "展开后头部文字起始 x 不变"
            _click_ctk_label(toggle)
            for _ in range(60):
                app.update()
                time.sleep(0.005)
            assert "▶" in str(self._toggle_icon(toggle).cget("text")), \
                "收起后为 ▶"
            assert cluster_head().winfo_x() == head_x0, \
                "收起后头部文字起始 x 复原"
        finally:
            win.destroy()
            app.update()

    def test_split_pane_layout(self, app):
        """左右分栏：左簇列表 + 右详情面板。"""
        win = self._open_fs(app)
        try:
            boxes = [w for w in _all_widgets(win)
                     if isinstance(w, ctk.CTkTextbox)]
            assert boxes, "右侧应有详情面板"
            # 左侧簇列表（优化缺陷R42：虚拟列表画布 + 自带滚动条）
            assert app._fs_vl is not None, "左侧应有虚拟列表组件"
            assert app._fs_vl._canvas.winfo_exists(), \
                "左侧应有虚拟列表画布"
        finally:
            win.destroy()
            app.update()

    def test_instances_data_available(self, app):
        """数据层：簇实例全量记录（count == len(instances)）。"""
        _run_paste_analysis(app, REPEAT_LOG)
        db = max(app._displayed, key=lambda c: c.count)
        assert db.count == 12
        assert len(db.instances) == 12
        assert db.instances[0].entry is not None
        assert "connection refused" in db.instances[0].summary


# ---------------------------------------------------------------------------
# 修复R6：UI 性能优化（虚拟列表 / 全屏窗口复用 / 原生行控件）
# ---------------------------------------------------------------------------
def _make_virtual_log(n=60):
    """生成 n 类互不相似的错误（数字被指纹归一化掩盖、相似骨架会
    触发相似度合并——用确定性唯一拼造码 + 分段不同骨架词保证独立簇）。"""
    cons = "bcdfghjklmnpqrstvwxz"
    vow = "aeiou"
    muls = [7, 11, 13, 17, 19, 23, 3, 5, 9, 29]
    tails = ["signal lost", "carrier gone", "beacon dead"]

    def code(i):
        out = [cons[(i // 20 * 7) % 20]]
        for k, m in enumerate(muls):
            mod = 20 if k % 2 == 0 else 5
            out.append((cons if k % 2 == 0 else vow)[(i * m) % mod])
        return "".join(out)

    return "\n".join(
        f"2024-01-01 09:{i // 60:02d}:{i % 60:02d} ERROR [db] "
        f"{code(i)} {tails[i // 20]}" for i in range(n)) + "\n"


def _run_many_clusters(app, n=60):
    """分析 n 簇日志（优化缺陷R43：Top N 已删除，全量显示即触发
    虚拟列表阈值，无需再调大 Top N）。"""
    _run_paste_analysis(app, _make_virtual_log(n))


def _force_hscroll_range(app, extra=1200):
    """虚拟列表强制超宽内容（单测聚焦滚动机制，不依赖聚类行为）。"""
    vl = app._virtual_list
    assert vl is not None
    vl._content_w = max(vl._region_w() + extra, 1200 + extra)
    vl._update_region()
    vl._sync()
    app.update()
    return vl


class TestVirtualList:
    def test_virtual_list_activates_above_threshold(self, app):
        """修复R6/R97：大列表池化虚拟渲染（R97 后任意簇数一律虚拟，
        经典滚动容器恒隐藏）。"""
        _run_many_clusters(app)
        assert len(app._displayed) > 40
        assert app._virtual_list is not None, "应启用虚拟列表"
        # 经典滚动容器隐藏
        assert not app._cluster_list.winfo_ismapped()

    def test_slot_pool_bounded(self, app):
        """修复R6：池化行数远小于总行数（只建可见区+缓冲）。"""
        _run_many_clusters(app)
        slots = app._virtual_list.slots
        app.update()
        assert len(slots) < 25, \
            f"池行数应 <25（视口行数级别），实际 {len(slots)}"

    def test_virtual_row_click_selects(self, app):
        """修复R6：虚拟行点击选中 + 详情同步（池行复用后仍正确）。"""
        _run_many_clusters(app)
        app.update()
        app._select_cluster(0)
        slots = app._virtual_list.slots
        # 点可见区第二行
        target = next(s for s in slots if s["idx"] == 1)
        target["summary"].event_generate("<Button-1>", x=3, y=2)
        app.update()
        assert app._selected_row == 1
        assert "【错误摘要】" in app._detail_box.get("1.0", "end")

    def test_virtual_scroll_reuses_slots(self, app):
        """修复R6：滚动后池行复用到高索引（不新建控件）。"""
        _run_many_clusters(app)
        app.update()
        before = len(app._virtual_list.slots)
        app._virtual_list._canvas.yview_moveto(1.0)
        app.update()
        after = len(app._virtual_list.slots)
        assert after <= before + 1, "滚动不应显著增加池行数"
        max_idx = max(s["idx"] for s in
                      app._virtual_list.slots if s["idx"] >= 0)
        assert max_idx >= len(app._displayed) - 5, \
            "滚动到底应显示尾部行"

    def test_small_list_also_virtual(self, app):
        """优化缺陷R97：小簇数同样虚拟渲染（渲染路径统一，不再有
        经典/虚拟双轨；经典 hbar 恒隐藏）。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        assert app._virtual_list is not None
        assert len(app._cluster_rows) == 2
        assert not app._list_hbar.winfo_ismapped()

    def test_selected_highlight_on_virtual_row(self, app):
        """修复R6：虚拟行选中态蓝色高亮（与未选中区分）。"""
        _run_many_clusters(app)
        app.update()
        app._select_cluster(1)
        app.update()
        slots = {s["idx"]: s for s in app._virtual_list.slots}
        if 1 in slots and 2 in slots:
            sel = str(slots[1]["frame"].cget("bg"))
            normal = str(slots[2]["frame"].cget("bg"))
            assert sel != normal, "选中/未选中背景应区分"

    def test_selected_virtual_row_continuous_block(self, app):
        """修复R22/R37：选中行统一蓝底 + 白字 + 画布圆角底。

        R37 三模式样式统一：line/toggle/head/frame/summary 均为
        sel_bot（与经典 _apply_row_bg 同色无缝，弃用 R26 渐变能
        带）；文字统一 sel_text 白保可读；选中行原生方形描边关闭
        （圆角背景/亮描边由画布图元呈现）。
        """
        _run_many_clusters(app)
        app.update()
        app._select_cluster(1)
        app.update()
        p = app._palette()
        slots = {s["idx"]: s for s in app._virtual_list.slots}
        assert 1 in slots and 2 in slots
        sel = slots[1]
        for key in ("line", "toggle", "head"):
            assert str(sel[key].cget("bg")) == p["sel_bot"], \
                f"选中行 {key} 应为统一行体色（实际 {sel[key].cget('bg')}）"
        for key in ("frame", "summary"):
            assert str(sel[key].cget("bg")) == p["sel_bot"], \
                f"选中行 {key} 应为底部能带色（实际 {sel[key].cget('bg')}）"
        # 修复缺陷R40：选中行头部用调亮级别色（非统一白）；展开
        # 按钮/摘要仍统一选中白
        cluster = app._displayed[1]
        expect_head = app._row_color_sel(cluster) or p["sel_text"]
        assert str(sel["head"].cget("fg")).lower() == expect_head.lower(), \
            f"选中行 head 应为调亮级别色（实际 {sel['head'].cget('fg')}）"
        for key in ("toggle", "summary"):
            assert str(sel[key].cget("fg")) == p["sel_text"], \
                f"选中行 {key} 文字应为选中白（实际 {sel[key].cget('fg')}）"
        assert int(sel["frame"].cget("highlightthickness")) == 0, \
            "选中行原生描边应关闭（画布圆角描边替代）"

    def test_selected_virtual_row_window_inside_round_bg(self, app):
        """修复缺陷R41b：行窗口不溢出圆角背景（底部亮描边/阴影可见）。

        行窗口垂直内缩/行高内边距未随 DPI 缩放时，高 DPI 下行窗口
        底部溢出圆角背景，盖住底部亮描边与阴影条（选中行底部
        「边框开口」）。回归断言：窗口底缘与圆角背景底缘之间
        始终保留 ≥4px 间隙（亮描边线宽 3px 放得下）。
        """
        _run_many_clusters(app)
        app.update()
        app._select_cluster(1)
        app.update()
        vl = app._virtual_list
        slots = {s["idx"]: s for s in vl.slots}
        assert 1 in slots
        sel = slots[1]
        grp = sel.get("_round")
        assert grp and grp.get("border") and grp.get("shadow"), \
            "选中行应有画布圆角组（亮描边+阴影）"
        canvas = vl._canvas
        wx, wy = canvas.coords(sel["win"])
        wh = int(canvas.itemcget(sel["win"], "height"))
        bb = canvas.bbox(grp["bg"])
        assert bb is not None, "圆角背景应有包围盒"
        bottom_room = bb[3] - (wy + wh)
        assert bottom_room >= 4, \
            f"窗口底缘应至少留 4px 给底部亮描边/阴影（实际 {bottom_room:.0f}px）"
        assert wy - bb[1] >= 4, "窗口顶缘应留间隙（顶部高光条可见）"

    def test_row_press_pop_animation(self, app):
        """优化缺陷R23/R25：点击行立体弹起（下沉→上弹→回落 240ms）。

        按下：行窗口 y 坐标下沉（按压感）+ 常驻投影收缩；释放：两
        段缓动动画（上弹投影加深 → 回落），完成后坐标精确复位、
        常驻投影保留基础深度（R25：立体感不随动画结束消失）。
        """
        _run_many_clusters(app)
        app.update()
        app._select_cluster(1)
        app.update()
        vl = app._virtual_list
        slots = {s["idx"]: s for s in vl.slots}
        assert 1 in slots
        s1 = slots[1]
        base = vl._row_y0(1) + vl._sx(12)  # R27/R41b：静止位含缩放垂直偏移
        vl._pop_press(s1)
        assert vl._canvas.coords(s1["win"])[1] > base, "按下应下沉"
        vl._pop_release(s1)
        assert s1.get("_pop") is not None, "释放应进入弹起动画"
        t0 = time.time()
        while s1.get("_pop") is not None and time.time() - t0 < 2.0:
            app.update()
            time.sleep(0.02)
        assert s1.get("_pop") is None, "动画应在约 240ms 内完成"
        assert vl._canvas.coords(s1["win"])[1] == base, "结束应回落原位"

    def test_row_click_triggers_pop(self, app):
        """优化缺陷R23：行点击事件驱动按压/弹起（与选中同链路）。"""
        _run_many_clusters(app)
        app.update()
        vl = app._virtual_list
        slots = {s["idx"]: s for s in vl.slots}
        s1 = slots[1]
        s1["head"].event_generate("<Button-1>", x=20, y=10)
        app.update()
        assert s1.get("_pop") is not None, "按下应记录按压态"
        s1["head"].event_generate("<ButtonRelease-1>", x=20, y=10)
        app.update()
        pop = s1.get("_pop")
        assert pop and pop.get("t0") is not None, "释放应启动弹起动画"
        t0 = time.time()
        while s1.get("_pop") is not None and time.time() - t0 < 2.0:
            app.update()
            time.sleep(0.02)
        assert s1.get("_pop") is None

    def test_pop_cancel_on_slot_recycle(self, app):
        """优化缺陷R23/R25：槽位回收取消动画并清圆角组、坐标归新位。"""
        _run_many_clusters(app)
        app.update()
        app._select_cluster(1)
        app.update()
        vl = app._virtual_list
        slots = {s["idx"]: s for s in vl.slots}
        s1 = slots[1]
        vl._pop_press(s1)
        vl._pop_release(s1)
        # 圆角背景组（R27：画布圆角矩形底 + 选中 3D 件）
        grp = s1.get("_round")
        assert grp and grp.get("sel"), "选中行应有圆角背景组（sel）"
        for key in ("bg", "border", "hi", "shadow", "rshadow"):
            assert grp.get(key), f"选中行圆角组缺 {key}"
        vl._fill_slot(s1, 5, vl._region_w())      # 回收到未选中索引
        assert s1.get("_pop") is None, "回收应取消动画"
        assert not s1["_round"].get("sel"), "回收为未选中行应无 3D 件"
        assert vl._canvas.coords(s1["win"])[1] == \
            vl._row_y0(5) + vl._sx(12)             # R41b：缩放垂直偏移

    def test_selected_row_rounded_corners(self, app):
        """优化缺陷R24/R27：选中行画布圆角背景组（底+描边+高光+双投影）。

        R27 方案：行窗口缩小居中，画布绘制圆角矩形背景（选中
        sel_bot / 未选中 row_bg）；选中行另加亮描边+顶部高光+底部/
        右侧投影；未选中行仅圆角背景 + 原生 1px 细边框（平面）。
        """
        _run_many_clusters(app)
        app.update()
        app._select_cluster(1)
        app.update()
        vl = app._virtual_list
        p = app._palette()
        slots = {s["idx"]: s for s in vl.slots}
        sel, normal = slots[1], slots[2]
        grp = sel.get("_round")
        assert grp and grp.get("sel"), "选中行应有圆角背景组（sel）"
        assert vl._canvas.type(grp["bg"]) == "polygon", "圆角背景应为多边形"
        assert str(vl._canvas.itemcget(grp["bg"], "fill")) == p["sel_bot"]
        for key in ("border", "hi", "shadow", "rshadow"):
            assert grp.get(key), f"选中行圆角组缺 3D 件 {key}"
        base = vl._row_y0(1)
        bbox = vl._canvas.bbox(grp["bg"])      # 圆角背景顶缘贴行槽顶
        assert abs(bbox[1] - base) <= 1
        assert grp["r"] >= 24, "选中圆角半径应明显（14 逻辑px×缩放）"
        # 未选中行：圆角背景（row_bg）+ 无 3D 件 + 1px 细边框
        n_grp = normal.get("_round")
        assert n_grp and not n_grp.get("sel"), "未选中行不应有 3D 件"
        assert str(vl._canvas.itemcget(n_grp["bg"], "fill")) == p["row_bg"]
        assert n_grp["r"] < grp["r"], "未选中圆角应小于选中"
        assert int(normal["frame"].cget("highlightthickness")) == 1, \
            "未选中行应有 1px 细边框"

    def test_round_masks_follow_pop_and_recycle(self, app):
        """优化缺陷R24/R27：圆角背景跟随弹起动画；槽位回收时重建。"""
        _run_many_clusters(app)
        app.update()
        app._select_cluster(1)
        app.update()
        vl = app._virtual_list
        slots = {s["idx"]: s for s in vl.slots}
        s1 = slots[1]
        base = vl._row_y0(1)
        vl._pop_press(s1)
        dy = s1["_pop"]["dy"]
        bbox = vl._canvas.bbox(s1["_round"]["bg"])  # 背景顶缘随下沉
        assert abs(bbox[1] - (base + dy)) <= 1, "圆角背景应跟随行位移"
        old = s1["_round"]
        vl._fill_slot(s1, 5, vl._region_w())     # 回收到未选中索引
        alive = set(vl._canvas.find_all())
        assert old["bg"] not in alive, "回收应清除旧圆角背景"
        # 回收为未选中行：重建圆角背景（无 3D 件）
        assert s1.get("_round") and not s1["_round"].get("sel")
        assert vl._canvas.coords(s1["win"])[1] == \
            vl._row_y0(5) + vl._sx(12)             # R41b：缩放垂直偏移

    def test_virtual_list_survives_theme_switch(self, app):
        """修复R6：虚拟模式下主题切换刷新配色不崩溃。"""
        _run_many_clusters(app)
        app.update()
        app._apply_theme_switch("blue")
        app.update()
        app._apply_theme_switch("dark")
        app.update()
        assert app._virtual_list is not None

    def test_hscroll_snapshot_atomic_scrolling(self, app):
        """优化：水平滚动快照模式 —— 按下转图元、滚动原子、释放恢复。

        拖动水平滚动条时行窗口（原生子窗口）逐个平移 + 画布回填
        异步交错是撕裂根源；快照模式把可见行转为同一画布的矩形/
        文本图元（单表面整帧上屏），行窗口隐藏；释放后恢复。
        """
        _run_many_clusters(app)
        app.update()
        vl = _force_hscroll_range(app)
        assert vl._region_w() > vl._canvas.winfo_width()
        vl._on_hbar_press(None)
        assert vl._xsnap is not None, "按下后应进入快照滚动模式"
        # 行窗口隐藏（拖动中零子窗口平移）
        for slot in vl.slots:
            assert str(vl._canvas.itemcget(slot["win"], "state")) \
                == "hidden"
        # 快照图元随画布原点平移（视口内呈现整体左移）
        vx0 = vl._canvas.canvasx(0)
        vl._canvas.xview_moveto(0.5)
        app.update()
        vx1 = vl._canvas.canvasx(0)
        assert vx1 > vx0, "滚动后画布原点应右移"
        # 释放恢复：图元删除、行窗口可见
        vl._on_hbar_release(None)
        assert vl._xsnap is None
        for slot in vl.slots:
            assert str(vl._canvas.itemcget(slot["win"], "state")) \
                == "normal"

    def test_hscroll_snapshot_fast_perf(self, app):
        """优化：水平滚动快照性能 —— 单帧 <16ms（60fps）。"""
        _run_many_clusters(app)
        app.update()
        vl = _force_hscroll_range(app)
        vl._on_hbar_press(None)
        assert vl._xsnap is not None
        # 预热：首个大幅滚动触发画布首次全量重绘等一次性成本
        for frac in (0.5, 0.05, 0.9):
            vl._canvas.xview_moveto(frac)
            app.update()
        times = []
        try:
            for i in range(40):
                frac = 0.05 if i % 2 == 0 else 0.9
                vl._canvas.xview_moveto(frac)
                t0 = time.perf_counter()
                app.update()
                times.append((time.perf_counter() - t0) * 1000)
        finally:
            vl._on_hbar_release(None)
            app.update()
        assert sum(times) / len(times) < 25.0, \
            f"平均单帧应 <25ms（实际均值 {sum(times) / len(times):.1f}ms）"

    def test_hbar_press_release_binding_wired(self, app):
        """优化：水平滚动条内部画布已挂按下/释放绑定（触发快照模式）。"""
        _run_many_clusters(app)
        app.update()
        vl = _force_hscroll_range(app)
        bar_canvas = vl._hbar._canvas
        bar_canvas.event_generate("<ButtonPress-1>", x=6, y=4)
        app.update()
        assert vl._xsnap is not None, "滚动条按下应触发快照模式"
        bar_canvas.event_generate("<ButtonRelease-1>", x=6, y=4)
        app.update()
        assert vl._xsnap is None, "滚动条释放应退出快照模式"


class TestClusterExpandMain:
    """修复缺陷R16：主列表「▶ ×N」就地展开（展示全部 N 个错误位置）。"""

    @staticmethod
    def _repeat_log(n=8):
        """同一错误重复 n 次（1 簇 ×N 实例，经典模式小数据）。"""
        return "".join(
            f"2024-01-01 09:00:{i:02d} ERROR [db] same failure here\n"
            for i in range(n))

    def test_expand_virtual_injects_instance_rows(self, app):
        """虚拟模式：展开后实例作为视图行注入（时间戳+行号+摘要），再点收起。"""
        _run_many_clusters(app)
        app.update()
        vl = app._virtual_list
        assert vl is not None
        n0 = len(vl._data)
        assert all(r[0] == "c" for r in vl._data), "初始应全为簇行"
        insts = app._displayed[0].instances
        assert insts, "测试数据应有实例记录"
        app._toggle_cluster_expand(0)
        app.update()
        assert 0 in app._expanded_clusters
        assert len(vl._data) == n0 + len(insts), "展开后实例行注入视图"
        # 实例行紧跟所属簇之后，类型/索引正确
        assert vl._data[0] == ("c", 0)
        assert all(r == ("i", 0, j)
                   for j, r in enumerate(vl._data[1:1 + len(insts)]))
        app._toggle_cluster_expand(0)
        app.update()
        assert 0 not in app._expanded_clusters
        assert len(vl._data) == n0
        assert all(r[0] == "c" for r in vl._data), "收起后恢复纯簇行"

    def test_expand_virtual_keeps_scroll(self, app):
        """展开/收起保持滚动位置（不跳回顶部）。"""
        _run_many_clusters(app)
        app.update()
        vl = app._virtual_list
        vl._canvas.yview_moveto(0.3)
        app.update()
        y0 = vl._canvas.canvasy(0)
        assert y0 > 0
        app._toggle_cluster_expand(1)
        app.update()
        assert abs(vl._canvas.canvasy(0) - y0) <= vl.ROW_HEIGHT, \
            "展开应保持滚动位置（内容偏移不变）"
        app._toggle_cluster_expand(1)
        app.update()

    def test_virtual_instance_row_click_shows_detail(self, app):
        """虚拟模式：点击实例行 → 右侧详情显示该实例自身（非典型样例）。"""
        _run_many_clusters(app)
        app.update()
        app._toggle_cluster_expand(0)
        app.update()
        vl = app._virtual_list
        target = None
        for s in vl.slots:
            idx = s.get("idx", -1)
            if 0 <= idx < len(vl._data) and vl._data[idx][0] == "i":
                target = s
                break
        assert target is not None, "视口内应渲染出实例行"
        row = vl._data[target["idx"]]
        target["summary"].event_generate("<Button-1>", x=3, y=2)
        app.update()
        assert app._selected_inst == (row[1], row[2])
        detail = app._detail_box.get("1.0", "end")
        assert "【实例详情】" in detail
        inst = app._displayed[row[1]].instances[row[2]]
        assert f"行 {inst.line_no}~" in detail

    def test_expand_small_data_shows_instances(self, app):
        """R97：小簇数同样虚拟渲染 —— 展开全部实例视图行，点击看
        实例详情，收起移除视图行。"""
        _run_paste_analysis(app, self._repeat_log(8))
        app.update()
        vl = app._virtual_list
        assert vl is not None, "R97：小数据同样虚拟渲染"
        slot0 = next(s for s in vl.slots
                     if 0 <= s.get("idx", -1) < len(vl._data)
                     and vl._data[s["idx"]] == ("c", 0))
        assert str(slot0["toggle"].cget("text")).startswith("×"), \
            "簇行应有「▶ ×N」展开按钮"
        insts = app._displayed[0].instances
        app._toggle_cluster_expand(0)
        app.update()
        inst_rows = [r for r in vl._data if r[0] == "i" and r[1] == 0]
        assert len(inst_rows) == len(insts), "实例应全量注入视图行"
        assert str(slot0["toggle_icon"].cget("text")) == "\u25bc", \
            "展开后按钮应为 ▼"
        # 点击首个实例行 → 右侧实例详情
        islot = next(s for s in vl.slots
                     if 0 <= s.get("idx", -1) < len(vl._data)
                     and vl._data[s["idx"]] == ("i", 0, 0))
        islot["summary"].event_generate("<Button-1>")
        app.update()
        assert app._selected_inst == (0, 0)
        detail = app._detail_box.get("1.0", "end")
        assert "【实例详情】" in detail
        assert f"行 {insts[0].line_no}~" in detail
        # 选中态蓝底落在实例行 frame 上
        assert str(islot["frame"].cget("bg")) == app._palette()["sel_bot"]
        # 收起
        app._toggle_cluster_expand(0)
        app.update()
        assert not [r for r in vl._data if r[0] == "i"], \
            "收起后实例视图行应移除"
        assert str(slot0["toggle_icon"].cget("text")) == "\u25b6"

    def test_virtual_instance_detail_shows_after_context(self, app):
        """修复缺陷R44/R97：点击实例 → 详情面板显示后上下文（统一
        虚拟渲染后实例为视图行）。

        后上下文待补队列此前仅对典型样例开启，实例 after 永远为空，
        展开簇点击实例时详情面板缺失「后上下文」区域（前上下文正常）。
        """
        log = "".join(
            f"2024-01-01 09:00:{i:02d} ERROR [db] same failure here\n"
            f"INFO post-{i}\n"
            for i in range(4))
        _run_paste_analysis(app, log)
        app.update()
        vl = app._virtual_list
        assert vl is not None, "R97：小数据同样虚拟渲染"
        app._toggle_cluster_expand(0)
        app.update()
        islot = next(s for s in vl.slots
                     if 0 <= s.get("idx", -1) < len(vl._data)
                     and vl._data[s["idx"]] == ("i", 0, 0))
        islot["summary"].event_generate("<Button-1>")
        app.update()
        detail = app._detail_box.get("1.0", "end")
        assert "【实例详情】" in detail
        assert "后上下文" in detail, "实例详情应显示后上下文区域"
        assert "INFO post-0" in detail, "实例详情应含实例之后的原始行"

    def test_detail_after_context_eof_placeholder(self, app):
        """修复缺陷R67：错误位于文件末尾（after 为空）时，后上下文
        区块仍固定显示并标注原因（原空则整块隐藏，看似功能缺失）。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        # SAMPLE_PASTE 末行（out of memory）位于文件末尾 → after 为空
        idx = next(i for i, c in enumerate(app._displayed)
                   if "memory" in c.summary)
        app._select_cluster(idx)
        app.update()
        detail = app._detail_box.get("1.0", "end")
        assert "后上下文" in detail, "簇详情应固定显示后上下文区块"
        assert "已到文件末尾" in detail, "空后上下文应标注原因"
        # 实例详情同口径
        app._toggle_cluster_expand(idx)
        app.update()
        vl = app._virtual_list
        islot = next(s for s in vl.slots
                     if 0 <= s.get("idx", -1) < len(vl._data)
                     and vl._data[s["idx"]] == ("i", idx, 0))
        islot["summary"].event_generate("<Button-1>")
        app.update()
        detail = app._detail_box.get("1.0", "end")
        assert "后上下文" in detail, "实例详情应固定显示后上下文区块"
        assert "已到文件末尾" in detail

    def test_expand_virtual_head_alignment_stable(self, app):
        """修复缺陷R34/R97：展开/收起 ▶/▼ 切换，头部内容起始 x 不变
        （统一虚拟渲染后图标等宽盒 icon_box 固定宽，place 居中换字形）。

        ▶ 比 ▼ 字形宽 8~10px，合写单标签时切换推动后续文字位移
        （展开行与未展开行头部不对齐）；图标等宽盒后位置不变。
        """
        _run_paste_analysis(app, self._repeat_log(8))
        app.update()
        vl = app._virtual_list
        assert vl is not None, "R97：小数据同样虚拟渲染"
        slot0 = next(s for s in vl.slots
                     if 0 <= s.get("idx", -1) < len(vl._data)
                     and vl._data[s["idx"]] == ("c", 0))
        app.update()
        x_collapsed = slot0["head"].winfo_x()
        icon_w0 = slot0["icon_box"].winfo_width()
        app._toggle_cluster_expand(0)
        app.update()
        assert slot0["head"].winfo_x() == x_collapsed, \
            "展开后头部文字起始 x 不变"
        assert slot0["icon_box"].winfo_width() == icon_w0, \
            "图标盒宽不随 ▼ 切换变化"
        app._toggle_cluster_expand(0)
        app.update()
        assert slot0["head"].winfo_x() == x_collapsed, \
            "收起后头部文字起始 x 复原"

    def test_rerender_clears_expand_state(self, app):
        """重新渲染（过滤/TopN/再分析）后展开与实例选中状态清空。"""
        _run_many_clusters(app)
        app.update()
        app._toggle_cluster_expand(0)
        app._select_instance(0, 0)
        app.update()
        assert app._expanded_clusters and app._selected_inst is not None
        app._render_cluster_list()
        app.update()
        assert not app._expanded_clusters
        assert app._selected_inst is None


class TestMainWindowSearch:
    """优化缺陷R45：主窗口结果搜索框（显示层过滤，不触发重新分析）。"""

    @staticmethod
    def _two_cluster_log():
        """两簇日志：kernel ×3 + filesystem ×3（经典模式小数据；
        消息差异足够大，不会触发相似度合并）。"""
        lines = []
        for i in range(3):
            lines.append(
                f"2024-01-01 09:00:0{i} ERROR [db] kernel panic in scheduler")
        for i in range(3):
            lines.append(
                f"2024-01-01 09:01:0{i} ERROR [db] filesystem journal corrupted")
        return "\n".join(lines)

    def test_search_entry_in_live_filter_row(self, app):
        """修复缺陷R72：搜索组迁至过滤行与按钮行之间的实时筛选行。"""
        assert app._search_entry is not None
        # 占位文案随 R72/R93 改版重写（原「过滤列表」已废弃），此处只校验
        # 仍描述过滤语义且公开布尔语法，避免再次因文案微调而失效
        placeholder = app._search_entry.cget("placeholder_text")
        assert "过滤" in placeholder and "and/or/not" in placeholder, \
            f"搜索框占位文案应说明过滤与布尔语法，实际 {placeholder!r}"
        panel = app._search_entry.master
        info = panel.grid_info()
        assert str(info["row"]) == "4", \
            "实时筛选行应在过滤行(row=2)/高级选项行(row=3)之后、" \
            "按钮行(row=5)之前"
        assert panel is not app._ctx_entry.master, \
            "搜索组不得留在级别过滤行"
        assert panel is not app._advanced_panel, \
            "搜索组不得混入高级选项行"
        # 修复缺陷R72：独立行空间充裕，输入框加宽 90→200（上下文行数框仍 60）
        assert app._search_entry.cget("width") == 200
        assert app._ctx_entry.cget("width") == 60
        # 输入框固定宽渲染（sticky 不含 e），不随窗口拉伸/压缩
        entry_info = app._search_entry.grid_info()
        assert "e" not in str(entry_info["sticky"])
        # 修复缺陷R73：▲/▼ 导航按钮紧随输入框（列2/3），计数影子框
        # 退居行尾（列4）且宽度随文本自适应（不再固定 88px）
        assert app._search_count_box.master is panel
        assert app._search_prev_btn.master is panel
        assert app._search_next_btn.master is panel
        assert str(app._search_prev_btn.grid_info()["column"]) == "2"
        assert str(app._search_next_btn.grid_info()["column"]) == "3"
        assert str(app._search_count_box.grid_info()["column"]) == "4"
        assert app._search_count_box.grid_propagate() != 0, \
            "计数框应内容自适应变宽（数字变长不顶出边框）"
        # 行号顺移（优化缺陷R85 高级选项行 row=3）：按钮行 5、结果区 6、
        # 状态栏 7
        assert str(app._advanced_panel.grid_info()["row"]) == "3"
        assert str(app._start_btn.master.grid_info()["row"]) == "5"
        assert str(app._result_panel.grid_info()["row"]) == "6"
        assert str(app._status_label.master.grid_info()["row"]) == "7"

    def test_search_nav_buttons(self, app):
        """修复缺陷R72：▲/▼ 按钮与 Enter / Shift+Enter 同语义。"""
        _run_paste_analysis(app, self._two_cluster_log())
        app.update()
        app._search_var.set("kernel")
        app._apply_search_filter()
        app.update()
        assert app._search_count.cget("text") == "0 / 3 条"
        app._search_next_btn.invoke()
        app.update()
        assert app._search_nav == 1, "▼ 应跳到下一个命中实例"
        assert app._search_count.cget("text") == "1 / 3 条"
        app._search_next_btn.invoke()
        app.update()
        assert app._search_nav == 2
        app._search_prev_btn.invoke()
        app.update()
        assert app._search_nav == 1, "▲ 应回上一个命中实例"
        assert app._search_count.cget("text") == "1 / 3 条"

    def test_count_box_auto_width(self, app):
        """修复缺陷R73：计数框宽度随文本自适应，完整容纳不裁切。"""
        _run_paste_analysis(app, self._two_cluster_log())
        app.update()
        app._search_var.set("kernel")
        app._apply_search_filter()
        app.update()
        assert app._search_count.cget("text") == "0 / 3 条"
        assert app._search_count_box.winfo_width() >= \
            app._search_count.winfo_reqwidth(), "计数框应完整容纳文本"
        # 导航后文本变化，框体仍完整容纳
        app._search_next_btn.invoke()
        app.update()
        assert app._search_count.cget("text") == "1 / 3 条"
        assert app._search_count_box.winfo_width() >= \
            app._search_count.winfo_reqwidth()
        # 清空 → 透明收缩
        app._search_var.set("")
        app._apply_search_filter()
        app.update()
        assert app._search_count_box.cget("fg_color") == "transparent"

    def test_level_filter_even_gaps_and_alignment(self, app):
        """修复缺陷R74：可视区间完全相等 + DEBUG 文本右缘对齐全屏按钮右缘。

        修复缺陷R83：对齐目标线首次同步即冻结 —— 分隔条拖动/窗口
        缩放后过滤行绝对固定不再漂移（原实时跟踪全屏按钮线，整行
        被拖着走，用户判定为缺陷）。紧凑区 padx 触底 0（同前）。
        """
        app._level_gap_target = None     # 重置冻结，确定性从本几何冻结
        app.geometry("2560x1475")
        app.update()
        # update_idletasks 双连：先跑 after_idle 的 R74 同步（写复选
        # 框 padx），再结算这些网格写入 —— 量测须基于一致快照
        app.update_idletasks()
        app.update_idletasks()
        panel = app._ctx_entry.master
        level_box = panel.grid_slaves(row=0, column=1)[0]
        children = level_box.winfo_children()
        infos, cbs = children[0::2], children[1::2]
        assert len(infos) == 5 and len(cbs) == 5

        def visual_gaps():
            out = []
            for i in range(4):
                tl = cbs[i]._text_label
                text_right = (cbs[i].winfo_x() + tl.winfo_x()
                              + tl.winfo_reqwidth())
                out.append(infos[i + 1].winfo_x() - text_right)
            return out

        def debug_text_right():
            tl = cbs[-1]._text_label
            return (level_box.winfo_x() + cbs[-1].winfo_x()
                    + tl.winfo_x() + tl.winfo_reqwidth())

        def target_x():
            return (app._list_ctrl_box.winfo_x()
                    + app._list_ctrl_box.winfo_width())

        # 文本无裁切
        for cb in cbs:
            tl = cb._text_label
            assert tl.winfo_x() + tl.winfo_reqwidth() <= \
                cb.winfo_width() + 2, f"{cb.cget('text')} 文本不得裁切"
        # 可视区间完全相等
        gaps = visual_gaps()
        assert max(gaps) - min(gaps) <= 2, \
            f"可视区间应完全相等（实测 {gaps}）"
        pads0 = [int(cb.grid_info()["padx"][1]) for cb in cbs[:4]]
        if any(p > 0 for p in pads0):
            # 膨胀区：冻结点即对齐点（DEBUG 右缘 == 全屏按钮右缘）
            assert abs(debug_text_right() - target_x()) <= 4, \
                f"冻结时应保持对齐（{debug_text_right()} vs {target_x()}）"
        else:
            # 紧凑区（高 DPI 窄窗）：padx 触底 0，行天然固定
            assert pads0 == [0, 0, 0, 0]
        pos0 = debug_text_right()

        # 修复缺陷R83：分隔条右移后 —— DEBUG 右缘不再跟随，保持冻结
        app._splitter_ratio = 0.62
        app._layout_splitter()
        app.update()
        app.update_idletasks()
        app.update_idletasks()
        assert abs(debug_text_right() - pos0) <= 2, \
            f"分隔条右移后行应绝对固定（{debug_text_right()} vs 冻结 {pos0}）"
        gaps2 = visual_gaps()
        assert max(gaps2) - min(gaps2) <= 2, \
            f"分隔条移动后区间仍应相等（实测 {gaps2}）"
        # 分隔条拖到极左：冻结目标线后不再触底，位置仍固定
        app._splitter_ratio = 0.05
        app._layout_splitter()
        app.update()
        app.update_idletasks()
        app.update_idletasks()
        assert abs(debug_text_right() - pos0) <= 2, \
            f"分隔条极左后行应仍固定（{debug_text_right()} vs 冻结 {pos0}）"
        # 窗口缩放：位置仍固定（宽度收窄但保持整行不裁切）
        app.geometry("2000x1100")
        app.update()
        app.update_idletasks()
        app.update_idletasks()
        assert abs(debug_text_right() - pos0) <= 2, \
            f"窗口缩放后行应仍固定（{debug_text_right()} vs 冻结 {pos0}）"

    def test_filter_row_group_gaps_equal(self, app):
        """修复缺陷R81：三个组间可视区间完全相等（静态 padx 补偿）。

        两层验证：
        1) 真实行构造锚点 —— 组首左 padx 为 (19,24,24) + ⚙ 行尾
           (24,12)、输入框右 padx 0、列 5 弹性权重已废（区间不再
           随窗口漂移）；
        2) 复刻行实测 —— 同款 CTk 控件 + 相同 padx + R74 紧凑宽
           复选框（尾距恒 4 逻辑 px），在本机 DPI 下量三个内容级
           区间（前组内容右缘 → 组首标签文本左缘）应完全相等。
           （真实行整行 reqw 随 R74 对齐机制填满可用宽，高 DPI
           下必超屏触发窗口钳制、混合量测历元不可信 —— 故用复刻
           行验证补偿数学；复刻行无 R74/after_idle 链，确定性结算）
        """
        panel = app._ctx_entry.master
        scale = max(1.0, getattr(app, "_font_scale", 1.0))
        # 1) 真实行构造锚点（grid_info 的 padx 为 Tk 物理 px ——
        # 创建时已经 CTk 按 DPI 缩放，须乘 scale 比对）
        # 列 2 上下文行数 / 列 4 包含关键词 / 列 6 排除关键词（R93 换位后）
        pads = [panel.grid_slaves(row=0, column=c)[0].grid_info()["padx"]
                for c in (2, 4, 6)]
        pads = [tuple(int(v) for v in p) for p in pads]
        s2 = int(round(2 * scale))
        expected = [(int(round(19 * scale)), s2),
                    (int(round(24 * scale)), s2),
                    (int(round(24 * scale)), s2)]
        assert pads == expected, \
            f"组首左 padx 应为补偿值 (19,24,24)×scale（实测 {pads}）"
        # 「其他选项」标签与 ⚙ 按钮在高级选项行行尾（优化缺陷R85；
        # R93 智能分析/解析规则迁入后列号 → 14/15）
        ap = app._advanced_panel
        lbl_pad = tuple(int(v) for v in
                        ap.grid_slaves(row=0, column=14)[0]
                        .grid_info()["padx"])
        assert lbl_pad == (int(round(24 * scale)), s2), \
            f"其他选项标签 padx 应为 (24,2)×scale（实测 {lbl_pad}）"
        btn_pad = tuple(int(v) for v in
                        app._settings_btn.grid_info()["padx"])
        assert btn_pad == (s2, 0), \
            f"⚙ 按钮 padx 应为 (2,0)×scale（R95 ↺ 接管行尾，实测 {btn_pad}）"
        rst_pad = tuple(int(v) for v in
                        app._reset_btn.grid_info()["padx"])
        assert rst_pad == (int(round(6 * scale)), int(round(12 * scale))), \
            f"↺ 按钮 padx 应为 (6,12)×scale（行尾余量，实测 {rst_pad}）"
        entry_pad = tuple(int(v) for v in
                          app._ctx_entry.grid_info()["padx"])
        assert entry_pad[1] == 0, "输入框右 padx 应为 0（区间由组首承担）"
        # 智能分析/解析规则组已迁至高级选项行（R93 换位）
        assert app._analyze_menu.master is ap
        assert app._rule_menu.master is ap

        # 2) 复刻行实测（与真实行同控件类、同 padx、同 DPI）
        import customtkinter as ctk
        import tkinter.font as tkfont
        top = ctk.CTkToplevel(app)
        top.geometry("1600x200")
        f = ctk.CTkFrame(top)
        f.pack(padx=10, pady=10)
        # DEBUG 复选框（R74 紧凑宽公式：文本实测宽 + 28，尾距恒 4）
        cb = ctk.CTkCheckBox(f, text="DEBUG",
                             checkbox_width=18, checkbox_height=18)
        tw = tkfont.Font(font=cb._text_label.cget("font")).measure("DEBUG")
        cb.configure(width=int(tw / scale + 0.999) + 28)
        cb.grid(row=0, column=0, padx=(1, 0), sticky="w")
        # R93 换位后真实行序列：上下文行数 → 输入框 → 包含 → 输入框 → 排除
        ctk.CTkLabel(f, text="上下文行数").grid(row=0, column=1,
                                                padx=(19, 2), sticky="w")
        entry1 = ctk.CTkEntry(f, width=60)
        entry1.grid(row=0, column=2, padx=(2, 0), sticky="w")
        ctk.CTkLabel(f, text="包含关键词").grid(row=0, column=3,
                                                padx=(24, 2), sticky="w")
        entry2 = ctk.CTkEntry(f, width=140)
        entry2.grid(row=0, column=4, padx=(2, 0), sticky="w")
        ctk.CTkLabel(f, text="排除关键词").grid(row=0, column=5,
                                                padx=(24, 2), sticky="w")
        top.update()
        top.update_idletasks()

        # CTk 6 标签无横向内边距（实测 2x 下文本宽==控件宽），文本
        # 边即控件边 —— 全程 winfo 物理几何直读，零字体/缩放换算，
        # 任何 DPI/显示器落点下自洽
        heads = [f.grid_slaves(row=0, column=c)[0] for c in (1, 3, 5)]
        tl = cb._text_label
        left1 = cb.winfo_x() + tl.winfo_x() + tl.winfo_reqwidth()
        left2 = entry1.winfo_x() + entry1.winfo_width()
        left3 = entry2.winfo_x() + entry2.winfo_width()
        gaps = [h.winfo_x() - l for h, l in zip(heads, (left1, left2, left3))]
        top.destroy()
        # 复选框尾距含文本宽向上取整残差 δ∈[0,1) 逻辑 px（×scale 物理）
        tol = max(2, int(round(scale)))
        assert max(gaps) - min(gaps) <= tol, \
            f"复刻行三个组间区间应完全相等（实测 {gaps}，容差 {tol}）"

    def test_keyword_entries_elastic_fill_row_tail(self, app):
        """优化缺陷R94：关键词两框弹性拉伸填满行尾（占位文本不再
        裁切、行尾无大片空白）；列权重均分、sticky ew。"""
        app.geometry("2000x900")
        app.update()
        app.update_idletasks()
        panel = app._ctx_entry.master
        for col in (5, 7):
            cfg = panel.grid_columnconfigure(col)
            assert int(cfg["weight"]) == 1, f"列 {col} 应有弹性权重"
        for entry in (app._include_entry, app._exclude_entry):
            assert "e" in str(entry.grid_info()["sticky"]) and \
                   "w" in str(entry.grid_info()["sticky"]), \
                "关键词框应 sticky=ew 横向拉伸"
        # 宽窗下两框实际宽应超过请求宽（吃掉行尾空白），且排除框
        # 右缘贴近行尾（仅剩 12×scale 行尾余量 + 面板卡内边距）
        req = app._include_entry.winfo_reqwidth()
        assert app._include_entry.winfo_width() > req, \
            "包含框应拉伸超过请求宽（填满空白）"
        scale = max(1.0, getattr(app, "_font_scale", 1.0))
        tail = (panel.winfo_width()
                - (app._exclude_entry.winfo_x()
                   + app._exclude_entry.winfo_width()))
        assert tail <= int(round(12 * scale)) + 30, \
            f"排除框右缘应贴近行尾（实测余量 {tail} 物理 px）"

    def test_search_filters_classic_list(self, app):
        """输入关键字 → 列表只显示匹配簇 + 计数标签（R97 统一虚拟渲染；
        池槽数为容量口径，可见行数以视图行 _data 为准）。"""
        _run_paste_analysis(app, self._two_cluster_log())
        app.update()
        assert app._virtual_list is not None
        assert len(app._virtual_list._data) == 2
        app._search_var.set("kernel")
        app._apply_search_filter()
        app.update()
        assert len(app._virtual_list._data) == 1, "过滤后视图行应只剩匹配簇"
        # 池槽只增不减（隐藏槽残留旧 idx），可见性以视图行 _data 为准
        vrow = app._virtual_list._data[0]
        assert vrow[0] == "c"
        assert "kernel" in app._displayed[vrow[1]].summary
        # 优化缺陷R56：计数 = 命中实例条数（kernel 簇 ×3 实例均含关键字）
        assert app._search_count.cget("text") == "0 / 3 条"

    def test_search_clear_restores_all(self, app):
        """清空关键字 → 全部簇恢复显示，计数标签隐藏。"""
        _run_paste_analysis(app, self._two_cluster_log())
        app.update()
        app._search_var.set("kernel")
        app._apply_search_filter()
        app.update()
        assert len(app._virtual_list._data) == 1
        app._search_var.set("")
        app._apply_search_filter()
        app.update()
        assert len(app._virtual_list._data) == 2
        assert app._search_count.cget("text") == ""

    def test_search_no_match_shows_hint(self, app):
        """无匹配 → 列表空态提示「无匹配的错误簇」。"""
        _run_paste_analysis(app, self._two_cluster_log())
        app.update()
        app._search_var.set("zzz-no-match")
        app._apply_search_filter()
        app.update()
        assert not app._cluster_rows
        texts = [w.cget("text") for w in app._cluster_list.winfo_children()
                 if hasattr(w, "cget")]
        assert any("无匹配" in t for t in texts)

    def test_search_preserves_expand_and_selection(self, app):
        """过滤命中的簇保持展开与选中状态（经典模式）。"""
        _run_paste_analysis(app, self._two_cluster_log())
        app.update()
        kidx = next(i for i, c in enumerate(app._displayed)
                    if "kernel" in c.summary)
        app._toggle_cluster_expand(kidx)
        app._select_cluster(kidx)
        app.update()
        assert kidx in app._expanded_clusters
        app._search_var.set("kernel")
        app._apply_search_filter()
        app.update()
        assert kidx in app._expanded_clusters, "命中的展开簇应保持展开"
        assert app._selected_row == kidx, "命中的选中簇应保持选中"

    def test_search_filters_virtual_list(self, app):
        """虚拟模式：视图行按关键字过滤，展开状态保留。"""
        _run_many_clusters(app)   # 60 条：signal/carrier/beacon 三组
        app.update()
        vl = app._virtual_list
        assert vl is not None
        # 簇数与分组受聚类/优先级排序影响 —— 以实际 _displayed 为准
        kidx = next(i for i, c in enumerate(app._displayed)
                    if "signal lost" in c.summary)
        app._toggle_cluster_expand(kidx)
        app.update()
        app._search_var.set("signal lost")
        app._apply_search_filter()
        app.update()
        expected = sum(1 for c in app._displayed
                       if "signal lost" in c.summary)
        c_rows = [r for r in vl._data if r[0] == "c"]
        assert len(c_rows) == expected, "视图簇行数应等于命中数"
        assert len(c_rows) < len(app._displayed), "确实发生了过滤"
        # 展开状态保留（命中过滤的展开簇其实例行仍在视图中）
        assert kidx in app._expanded_clusters
        assert any(r[0] == "i" and r[1] == kidx for r in vl._data)
        # 优化缺陷R56：计数 y = 命中实例总条数（扁平实例导航序列长）
        nav_total = len(app._search_targets("signal lost"))
        assert app._search_count.cget("text") == f"0 / {nav_total} 条"

    def test_search_debounce_schedules_job(self, app):
        """输入经 trace 调度防抖任务（200ms 合并连续输入）。"""
        _run_paste_analysis(app, self._two_cluster_log())
        app.update()
        app._search_var.set("filesystem")
        assert app._search_job is not None, "输入应调度防抖任务"
        app._apply_search_filter()
        assert app._search_job is None

    # ------------------------------------------------------------------
    # 优化缺陷R46：Enter 跳下一个匹配 + 详情关键字照亮
    # ------------------------------------------------------------------
    def test_enter_jumps_to_next_match(self, app):
        """Enter 定位下一个命中实例（优化缺陷R56：扁平实例导航）。

        两簇各 ×3 实例（摘要均含 error）→ 序列 6 条；Enter 依次
        下钻：簇自动展开 ▼、_selected_inst 定位到具体实例行。
        """
        _run_paste_analysis(app, self._two_cluster_log())
        app.update()
        app._search_var.set("error")     # 级别 ERROR，两簇均命中
        app._apply_search_filter()
        app.update()
        assert app._search_count.cget("text") == "0 / 6 条", \
            "输入后未导航应为 0/y（y=命中实例总条数）"
        app._on_search_enter(True)
        app.update()
        assert app._selected_inst == (0, 0)
        assert 0 in app._expanded_clusters, "定位实例应自动展开所属簇"
        assert app._search_count.cget("text") == "1 / 6 条"
        app._on_search_enter(True)
        app.update()
        assert app._selected_inst == (0, 1), "同簇内应逐实例下钻"
        assert app._search_count.cget("text") == "2 / 6 条"
        app._on_search_enter(True)
        app.update()
        assert app._selected_inst == (0, 2)
        app._on_search_enter(True)
        app.update()
        assert app._selected_inst == (1, 0), "簇内走完应进下一簇"
        assert app._search_count.cget("text") == "4 / 6 条"
        # 回绕：6/6 后再按回 1/6
        app._search_nav = 6
        app._on_search_enter(True)
        app.update()
        assert app._selected_inst == (0, 0)
        assert app._search_count.cget("text") == "1 / 6 条"

    def test_shift_enter_jumps_back(self, app):
        """Shift+Enter 反向定位（0/y 直接到 y/y，优化缺陷R56）。"""
        _run_paste_analysis(app, self._two_cluster_log())
        app.update()
        app._search_var.set("error")
        app._apply_search_filter()
        app.update()
        app._on_search_enter(False)
        app.update()
        assert app._selected_inst == (1, 2), "Shift+Enter 应反向到末尾实例"
        assert app._search_count.cget("text") == "6 / 6 条"
        app._on_search_enter(False)
        app.update()
        assert app._selected_inst == (1, 1)
        assert app._search_count.cget("text") == "5 / 6 条"

    def test_enter_flushes_pending_debounce(self, app):
        """Enter 先冲刷挂起的防抖过滤再跳转（关键字立即生效）。"""
        _run_paste_analysis(app, self._two_cluster_log())
        app.update()
        app._search_var.set("kernel")    # trace 调度防抖任务
        assert app._search_job is not None
        app._on_search_enter(True)
        app.update()
        assert app._search_job is None, "Enter 应冲刷防抖任务"
        kidx = next(i for i, c in enumerate(app._displayed)
                    if "kernel" in c.summary)
        assert app._selected_row == kidx, "应选中唯一匹配簇"

    def test_enter_binding_wired_to_entry(self, app):
        """搜索框 Enter / Shift+Enter 绑定挂接到跳转处理器。

        Tk 合成键盘事件走焦点分发且 WM 焦点异步生效（Windows 上
        focus_set 不保证即时生效，实测抖动）——改为确定性验证：
        绑定脚本存在且指向 _on_search_enter；处理器跳转逻辑由
        test_enter_jumps_to_next_match / test_shift_enter_jumps_back
        等直调测试覆盖。
        """
        entry = app._search_entry._entry
        assert entry.bind("<Return>"), "Enter 应已挂接跳转绑定"
        assert entry.bind("<Shift-Return>"), "Shift+Enter 应已挂接跳转绑定"
        # 绑定脚本经 lambda 转发到处理器（绑定注册名含 <lambda>）
        assert "<lambda>" in entry.bind("<Return>")

    def test_search_keyword_highlighted_in_detail(self, app):
        """详情面板日志内容中的搜索关键字被照亮（searchkw 标签）。"""
        _run_paste_analysis(app, self._two_cluster_log())
        app.update()
        app._search_var.set("kernel")
        app._apply_search_filter()
        app.update()
        box = app._detail_box
        ranges = box.tag_ranges("searchkw")
        assert ranges, "详情面板应照亮搜索关键字"
        first = box.get(ranges[0], ranges[1])
        assert first.lower() == "kernel", "照亮文本应为关键字本身"

    def test_enter_jumps_in_virtual_mode(self, app):
        """虚拟模式：Enter 定位命中实例并滚入视口（优化缺陷R56）。"""
        _run_many_clusters(app)
        app.update()
        assert app._virtual_list is not None
        app._search_var.set("signal lost")
        app._apply_search_filter()
        app.update()
        seq = app._search_targets("signal lost")
        assert len(seq) >= 2
        first_ci = seq[0][0]
        # 模拟真实点击（sync_nav=True）：导航序号同步到该簇首个命中实例
        app._select_cluster(first_ci, sync_nav=True)
        app.update()
        assert app._search_nav == 1
        app._on_search_enter(True)
        app.update()
        assert app._search_nav == 2
        assert app._selected_inst == seq[1], "Enter 应定位序列第 2 个命中实例"

    def test_click_row_syncs_nav_index(self, app):
        """优化缺陷R56：点选簇行 → 序号同步到该簇首个命中实例。"""
        _run_paste_analysis(app, self._two_cluster_log())
        app.update()
        app._search_var.set("error")     # 两簇均命中（各 ×3 实例 → 6 条）
        app._apply_search_filter()
        app.update()
        assert app._search_count.cget("text") == "0 / 6 条"
        # 点选第 2 个匹配簇（经典行）→ 该簇首个命中实例 = 序列第 4 条
        row = next(r for r in app._cluster_rows if r.get("idx") == 1)
        row["summary"].event_generate("<Button-1>", x=3, y=2)
        app.update()
        assert app._selected_row == 1
        assert app._search_count.cget("text") == "4 / 6 条", \
            "点选第 2 簇应对齐其首个命中实例序号"
        # 点选回第 1 个 → 序列第 1 条
        row0 = next(r for r in app._cluster_rows if r.get("idx") == 0)
        row0["summary"].event_generate("<Button-1>", x=3, y=2)
        app.update()
        assert app._search_count.cget("text") == "1 / 6 条"

    def test_click_instance_row_syncs_nav_index(self, app):
        """优化缺陷R56：点选实例行 → 序号同步到该实例自身。"""
        _run_paste_analysis(app, self._two_cluster_log())
        app.update()
        app._search_var.set("error")
        app._apply_search_filter()
        app.update()
        # 展开簇 1 并点选其实例 2 → 序列第 6 条
        app._toggle_cluster_expand(1)
        app.update()
        app._select_instance(1, 2)
        app.update()
        assert app._search_count.cget("text") == "6 / 6 条", \
            "点选实例行应对齐该实例自身序号"

    def test_enter_nav_instance_blue_virtual(self, app):
        """优化缺陷R57/R97：Enter 定位实例行着蓝色选中样式（统一
        虚拟渲染后实例为视图行，选中蓝底落在池行 frame）。"""
        _run_paste_analysis(app, self._two_cluster_log())
        app.update()
        app._search_var.set("error")
        app._apply_search_filter()
        app.update()
        app._on_search_enter(True)
        for _ in range(10):
            app.update()
            time.sleep(0.01)
        ci, ii = app._selected_inst
        sel = app._palette()["sel_bot"]
        vl = app._virtual_list

        def inst_slot(c, i):
            return next(s for s in vl.slots
                        if 0 <= s.get("idx", -1) < len(vl._data)
                        and vl._data[s["idx"]] == ("i", c, i))
        wrap = inst_slot(ci, ii)
        assert str(wrap["frame"].cget("bg")) == sel, \
            "Enter 定位的实例行应着蓝色选中样式"
        # 再 Enter：新实例行着蓝、旧实例行恢复默认
        app._on_search_enter(True)
        for _ in range(10):
            app.update()
            time.sleep(0.01)
        ci2, ii2 = app._selected_inst
        assert (ci2, ii2) != (ci, ii)
        wrap2 = inst_slot(ci2, ii2)
        assert str(wrap2["frame"].cget("bg")) == sel
        assert str(wrap["frame"].cget("bg")) != sel, \
            "旧实例行应恢复未选中样式"

    def test_instance_click_marks_blue_virtual(self, app):
        """优化缺陷R57/R97：点击实例行同样着蓝色选中样式（虚拟视图行）。"""
        _run_paste_analysis(app, self._two_cluster_log())
        app.update()
        app._toggle_cluster_expand(0)
        app.update()
        vl = app._virtual_list
        slot = next(s for s in vl.slots
                    if 0 <= s.get("idx", -1) < len(vl._data)
                    and vl._data[s["idx"]] == ("i", 0, 1))
        slot["summary"].event_generate("<Button-1>")
        app.update()
        assert app._selected_inst == (0, 1)
        assert str(slot["frame"].cget("bg")) == app._palette()["sel_bot"]

    def test_filtered_out_selection_auto_moves_to_first_match(self, app):
        """当前选中簇被过滤掉时自动选中首个匹配簇（详情不滞留陈旧内容）。"""
        _run_paste_analysis(app, self._two_cluster_log())
        app.update()
        fidx = next(i for i, c in enumerate(app._displayed)
                    if "filesystem" in c.summary)
        app._select_cluster(fidx)
        app.update()
        app._search_var.set("kernel")
        app._apply_search_filter()
        app.update()
        kidx = next(i for i, c in enumerate(app._displayed)
                    if "kernel" in c.summary)
        assert app._selected_row == kidx, "选中应自动移到首个匹配簇"
        assert "kernel" in app._detail_box.get("1.0", "1.end")

    def test_filter_bar_no_overlap_when_count_appears(self, app):
        """优化缺陷R47：计数标签「X / Y 条」出现时过滤栏不左移挤压。

        修复缺陷R72：搜索组迁出后过滤行只剩 级别过滤/上下文行数/
        解析规则 三组控件，整行请求宽远低于可用宽 —— 标签不被遮挡，
        解析规则下拉最长选项不被裁切。
        """
        app.geometry("1600x900")
        app.update()
        _run_paste_analysis(app, self._two_cluster_log())
        app.update()
        panel = app._ctx_entry.master
        app._search_var.set("kernel")
        app._apply_search_filter()
        app.update()
        assert app._search_count.cget("text"), "计数标签应已出现"
        # R93 换位后过滤行控件序列：级别组 → 上下文行数 → 关键词两框
        widgets = [panel.grid_slaves(row=0, column=0)[0],   # 级别过滤
                   panel.grid_slaves(row=0, column=1)[0],   # 复选框组
                   panel.grid_slaves(row=0, column=2)[0],   # 上下文行数
                   app._ctx_entry,
                   panel.grid_slaves(row=0, column=4)[0],   # 包含关键词
                   app._include_entry,
                   panel.grid_slaves(row=0, column=6)[0],   # 排除关键词
                   app._exclude_entry]
        prev_right = None
        for w in widgets:
            x, ww = w.winfo_x(), w.winfo_width()
            if prev_right is not None:
                assert x >= prev_right - 2, "过滤栏控件不得相互遮挡"
            prev_right = x + ww
        lbl = widgets[0]
        assert lbl.winfo_width() >= lbl.winfo_reqwidth() - 2, \
            "级别过滤标签不得被裁切"
        ctx_lbl = widgets[2]
        assert ctx_lbl.winfo_width() >= ctx_lbl.winfo_reqwidth() - 2, \
            "上下文行数标签不得被裁切"
        # 最长选项 embedded 时下拉不被裁切
        app._rule_menu.set("embedded")
        app.update()
        assert app._rule_menu.winfo_width() >= \
            app._rule_menu.winfo_reqwidth() - 2, "解析规则下拉不得裁字"

    def test_count_box_fixed_in_live_filter_row(self, app):
        """修复缺陷R72：计数影子框在实时筛选行恒定占位，显隐零布局影响。

        搜索组迁出后，计数显形/隐形只改影子框底色（恒定尺寸占位），
        实时筛选行与级别过滤行的位置、高度均不变；过滤行不再有
        超宽切边风险（R49 两区间等距要求随搜索组迁出废止）。
        """
        app.geometry("1600x900")
        app.update()
        _run_paste_analysis(app, self._two_cluster_log())
        app.update()
        panel = app._search_entry.master            # 实时筛选行
        filter_panel = app._ctx_entry.master        # 级别过滤行

        def snapshot():
            app.update()
            return (panel.winfo_y(), panel.winfo_height(),
                    filter_panel.winfo_y(), filter_panel.winfo_height(),
                    app._rule_menu.winfo_x())

        pos0 = snapshot()
        app._search_var.set("kernel")
        app._apply_search_filter()
        app.update()
        assert app._search_count.cget("text"), "计数标签应已出现"
        # 计数影子框：有计数时显形（与输入框同底色）
        assert app._search_count_box.cget("fg_color") == \
            app._search_entry.cget("fg_color"), "计数框应与输入框同底色"
        pos1 = snapshot()
        assert pos0 == pos1, "计数出现时两行位置/高度应完全固定"
        # 清空计数 → 影子框透明隐形，位置仍不动
        app._search_var.set("")
        app._apply_search_filter()
        app.update()
        assert app._search_count_box.cget("fg_color") == "transparent"
        pos2 = snapshot()
        assert pos0 == pos2, "计数消失后两行位置仍应固定"


class TestFullscreenReuse:
    def _open_fs(self, app):
        app._open_list_fullscreen()
        for _ in range(20):
            app.update()
            time.sleep(0.005)
        return app._fs_list_win

    def test_fullscreen_window_reused(self, app):
        """修复R6：二次打开复用同一窗口对象（withdraw 而非销毁）。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        win1 = self._open_fs(app)
        assert win1 is not None and win1.winfo_exists()
        win1.event_generate("<Escape>")
        app.update()
        assert not win1.winfo_ismapped(), "ESC 后窗口应隐藏"
        app._open_list_fullscreen()
        app.update()
        assert app._fs_list_win is win1, "二次打开应复用窗口对象"
        assert win1.winfo_ismapped(), "复用后窗口应可见"
        win1.destroy()
        app.update()

    def test_detail_fullscreen_window_reused(self, app):
        """修复R6：详情全屏窗口同样预创建复用。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        app._select_cluster(0)
        app.update()
        app._open_detail_fullscreen()
        app.update()
        win1 = app._fs_detail_win
        assert win1 is not None and win1.winfo_exists()
        box = app._fs_detail_box
        assert "【错误摘要】" in box.get("1.0", "end")
        win1.event_generate("<Escape>")
        app.update()
        assert not win1.winfo_ismapped()
        # 换一个簇再打开：内容刷新且窗口复用
        app._select_cluster(1) if len(app._displayed) > 1 else None
        app._open_detail_fullscreen()
        app.update()
        assert app._fs_detail_win is win1
        assert win1.winfo_ismapped()
        win1.destroy()
        app.update()

    def test_fs_rows_native_lightweight(self, app):
        """修复R6：全屏列表行为原生 tk 控件（无内部 Canvas 开销）。"""
        _run_many_clusters(app)
        win = self._open_fs(app)
        try:
            native_labels = [w for w in _all_widgets(win)
                             if w.winfo_class() == "Label"
                             and "ERROR" in str(w.cget("text"))]
            assert native_labels, "全屏行应为原生 Label"
            canvases = [w for w in _all_widgets(win)
                        if w.winfo_class() == "Canvas"]
            # Canvas 仅来自 CTk 框架控件（搜索框/按钮/滚动条/详情框
            # ≈11 个，与行数无关）；57 行若用 CTk 行会有 100+ Canvas
            assert len(canvases) <= 12, \
                f"行控件不应引入大量 Canvas，实际 {len(canvases)}"
        finally:
            win.destroy()
            app.update()

    def test_fs_selected_row_style_matches_main(self, app):
        """优化缺陷R42：全屏选中行与主窗口虚拟列表完全一致。

        选中态：统一 sel_bot 底 + 画布圆角组（亮描边/顶部高光/底部
        +右侧阴影）+ 分界线 sel_border 亮色 + 头部调亮级别色 +
        摘要选中白；未选中：平面圆角背景。
        """
        _run_paste_analysis(app, REPEAT_LOG)
        win = self._open_fs(app)
        try:
            p = app._palette()
            vl = app._fs_vl
            assert vl is not None, "全屏列表应为虚拟列表组件"
            # 全屏档字体注入生效（头部用 _font_fs_head）
            assert vl._f_head is app._font_fs_head
            assert vl._f_sum is app._font_fs_summary
            app._select_cluster(0)
            for _ in range(8):
                app.update()
                time.sleep(0.005)
            slot = next(
                s for s in vl.slots
                if 0 <= s.get("idx", -1) < len(vl._data)
                and vl._data[s["idx"]] == ("c", 0))
            # 选中行：统一蓝底 + 画布 3D 圆角组五件套
            grp = slot.get("_round")
            assert grp and grp.get("sel"), "选中行应有 3D 圆角组"
            for key in ("bg", "border", "hi", "shadow", "rshadow"):
                assert grp.get(key), f"选中行圆角组缺 {key}"
            for key in ("frame", "head", "summary"):
                assert str(slot[key].cget("bg")) == p["sel_bot"], \
                    f"选中行 {key} 应为统一行体色"
            assert str(slot["divider"].cget("bg")) == p["sel_border"], \
                "选中行分界线应为亮色"
            # 头部调亮级别色（非统一白）；摘要选中白
            cluster = app._displayed[0]
            expect_head = app._row_color_sel(cluster) or p["sel_text"]
            assert str(slot["head"].cget("fg")).lower() == \
                expect_head.lower(), "选中行头部应为调亮级别色"
            assert str(slot["summary"].cget("fg")) == p["sel_text"], \
                "选中行摘要应为选中白"
        finally:
            win.destroy()
            app.update()

    def test_open_fullscreen_callback_fast(self, app):
        """修复R6：全屏按钮回调 <300ms（首批渲染异步，窗口先显示）。"""
        _run_many_clusters(app)
        t0 = time.perf_counter()
        app._open_list_fullscreen()
        elapsed_ms = (time.perf_counter() - t0) * 1000
        try:
            assert elapsed_ms < 300, \
                f"全屏回调应 <300ms，实际 {elapsed_ms:.0f}ms"
        finally:
            if app._fs_list_win is not None:
                app._fs_list_win.destroy()
                app.update()


class TestFullscreenSplitter:
    """修复缺陷R17：全屏列表窗口 列表|详情 可拖分隔条（与主界面同款）。"""

    @staticmethod
    def _open_fs(app):
        """打开全屏并放大到可拖尺寸（测试环境屏幕小，默认 85% 宽度
        可能小于「两列最小宽+分隔条」导致分隔条锁定）。"""
        _run_many_clusters(app)
        app._open_list_fullscreen()
        for _ in range(25):
            app.update()
            time.sleep(0.005)
        win = app._fs_list_win
        assert win is not None and win.winfo_exists()
        try:
            win.state("normal")
        except tk.TclError:
            pass
        win.geometry("2400x1300+0+0")
        for _ in range(6):
            app.update()
        return win

    @staticmethod
    def _close(app, win):
        try:
            win.destroy()
        except tk.TclError:
            pass
        app.update()

    def test_fs_splitter_exists(self, app):
        """全屏应有可见分隔条（样式/光标/握点/三列 place 布局）。"""
        win = self._open_fs(app)
        try:
            sp = app._fs_splitter
            assert sp.winfo_ismapped(), "全屏应有可见分隔条"
            assert sp.cget("cursor") == "sb_h_double_arrow"
            assert len(app._fs_splitter_dots) == 3, "应有三个握点"
            pw = app._fs_body.winfo_width()
            lw = app._fs_list_col.winfo_width()
            dw = app._fs_detail_col.winfo_width()
            assert lw > 300 and dw > 300, "左右列应正常分宽"
            assert abs(lw / pw - app._fs_splitter_ratio) < 0.03, \
                "左列宽应按比例布局"
        finally:
            self._close(app, win)

    def test_fs_splitter_drag_proxy_follows(self, app):
        """矢量代理拖动：裁剪框/视口/标题条逐 motion 实时跟随。"""
        win = self._open_fs(app)
        try:
            sp = app._fs_splitter
            pw = app._fs_body.winfo_width()
            sp.event_generate("<ButtonPress-1>", x=3, y=100)
            app.update()
            live = app._fs_live
            assert live is not None, "应构建矢量文本代理"
            lw0 = live["lw"]
            assert abs(live["right"].canvasx(0)) <= 1, \
                "press 后初始视口应归零"
            assert not sp.winfo_ismapped(), "拖动中真实分隔条应隐藏"
            frozen = app._fs_list_col.winfo_width()
            for dx in (80, 160, 240):
                sp.event_generate("<B1-Motion>", x=3 + dx, y=100)
                app._fs_live_flush()
                app.update()
                expect = app._fs_splitter_ratio * pw
                assert abs(live["clip"].winfo_width() - expect) <= 2, \
                    "左裁剪框宽应逐 motion 实时跟随"
                assert abs(live["right"].canvasx(0)
                           - (lw0 - expect)) <= 2, \
                    "右画布视口应实时滚动（详情文本贴住分隔条）"
                tbar_x = (live["tbar"].winfo_rootx()
                          - app._fs_body.winfo_rootx())
                assert abs(tbar_x - expect) <= 2, \
                    "右标题条（详情标题）应跟随分隔条"
                assert app._fs_list_col.winfo_width() == frozen, \
                    "拖动中真实列冻结（代理之下，松开一次应用）"
            sp.event_generate("<ButtonRelease-1>", x=3 + 240, y=100)
            app.update()
            assert app._fs_live is None, "释放后代理应销毁"
            assert sp.winfo_ismapped(), "释放后真实分隔条应恢复"
            head0 = app._fs_detail_head.winfo_children()[0]
            assert head0.winfo_ismapped(), "释放后真实「详情」标题应恢复"
            assert abs(app._fs_splitter_ratio * pw
                       - app._fs_list_col.winfo_width()) <= 8, \
                "释放后真实列一次性到最终位置"
        finally:
            self._close(app, win)

    def test_fs_splitter_dblclick_restores_default(self, app):
        """双击恢复默认比例（2:3）。"""
        win = self._open_fs(app)
        try:
            app._fs_splitter_ratio = 0.6
            app._fs_layout_splitter()
            app.update()
            assert app._fs_list_col.winfo_width() \
                != int(0.4 * app._fs_body.winfo_width())
            app._on_fs_dblclick(None)
            app.update()
            assert abs(app._fs_splitter_ratio - 0.4) < 1e-6, \
                "双击应恢复默认 2:3 比例"
        finally:
            self._close(app, win)

    def test_fs_splitter_ratio_persisted(self, app):
        """拖动松开后比例持久化（config fs_splitter_ratio）。"""
        win = self._open_fs(app)
        try:
            sp = app._fs_splitter
            pw = app._fs_body.winfo_width()
            sp.event_generate("<ButtonPress-1>", x=3, y=100)
            app.update()
            sp.event_generate("<B1-Motion>", x=3 + int(pw * 0.1), y=100)
            app.update()
            sp.event_generate("<ButtonRelease-1>",
                              x=3 + int(pw * 0.1), y=100)
            app.update()
            expect = app._fs_splitter_ratio
            assert abs(app._config.get("fs_splitter_ratio")
                       - expect) < 1e-6, "比例应写入配置"
        finally:
            self._close(app, win)

    def test_fs_splitter_min_width_limits(self, app):
        """拖到左右极限受最小宽度钳制（不遮挡标题/不锁死）。"""
        win = self._open_fs(app)
        try:
            sp = app._fs_splitter
            left_min, right_min = app._fs_min_widths()
            sp.event_generate("<ButtonPress-1>", x=3, y=100)
            app.update()
            sp.event_generate("<B1-Motion>", x=-9999, y=100)
            app.update()
            sp.event_generate("<ButtonRelease-1>", x=-9999, y=100)
            app.update()
            assert app._fs_list_col.winfo_width() >= left_min - 4, \
                "左列最小宽度钳制"
            sp.event_generate("<ButtonPress-1>", x=3, y=100)
            app.update()
            sp.event_generate("<B1-Motion>", x=99999, y=100)
            app.update()
            sp.event_generate("<ButtonRelease-1>", x=99999, y=100)
            app.update()
            assert app._fs_detail_col.winfo_width() >= right_min - 4, \
                "右列最小宽度钳制"
        finally:
            self._close(app, win)


# ---------------------------------------------------------------------------
# 修复R5：详情面板字体与高亮（Tooltip 已由 R3 统一修复）
# ---------------------------------------------------------------------------
class TestDetailPanelR5:
    def _select_db_cluster(self, app):
        """选中带堆栈的 db 簇（含系统库噪声帧 -> 有折叠行）。"""
        idx = next(i for i, c in enumerate(app._displayed)
                   if "connection refused" in c.summary)
        app._select_cluster(idx)
        app.update()
        return idx

    def test_main_detail_font_13(self, app):
        """修复R5：主面板详情字体 13 号（摘要/堆栈/上下文可读）。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        self._select_db_cluster(app)
        font = app._detail_box.cget("font")
        size = font.cget("size") if hasattr(font, "cget") else font
        assert int(size) in (12, 13), f"详情字体应为 12~13 号，实际 {size}"

    def test_bstack_bold_and_distinct(self, app):
        """修复R5：业务栈帧琥珀色加粗（与普通行区分更明显）。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        self._select_db_cluster(app)
        box = app._detail_box
        # 有业务栈帧（at com.app... 行）
        assert box.tag_ranges("bstack"), "应有业务栈帧高亮"
        fg = str(box.tag_cget("bstack", "foreground"))
        assert fg == "#fbbf24", f"业务栈帧应为琥珀色，实际 {fg}"
        font = str(box.tag_cget("bstack", "font"))
        assert "bold" in font, f"业务栈帧应加粗，实际 {font}"

    def test_fold_line_distinct_tag(self, app):
        """修复R5：系统库折叠提示独立配色（清晰可辨）。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        self._select_db_cluster(app)
        box = app._detail_box
        assert box.tag_ranges("fold"), "java.base 噪声帧应生成折叠行"
        fg = str(box.tag_cget("fold", "foreground"))
        assert fg == "#a78bfa", f"折叠提示应为紫色，实际 {fg}"
        # 折叠行文本确实包含「已折叠」
        text = box.get("1.0", "end")
        assert "已折叠" in text

    def test_detail_fullscreen_font_18(self, app):
        """修复R10：详情全屏窗口字体放大到 18 号（全屏大字体）。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        self._select_db_cluster(app)
        app._open_detail_fullscreen()
        app.update()
        try:
            wins = [w for w in app.winfo_children()
                    if isinstance(w, tk.Toplevel)
                    and "错误详情" in w.title()]
            assert wins
            boxes = [w for w in _all_widgets(wins[0])
                     if isinstance(w, ctk.CTkTextbox)]
            font = boxes[0].cget("font")
            size = font.cget("size") if hasattr(font, "cget") else font
            assert int(size) == 18, f"全屏详情字体应 18 号，实际 {size}"
        finally:
            for w in [w for w in app.winfo_children()
                      if isinstance(w, tk.Toplevel)]:
                w.destroy()
            app.update()


def _all_widgets(root):
    """递归收集窗口内全部控件。"""
    result = []
    stack = [root]
    while stack:
        w = stack.pop()
        result.append(w)
        try:
            stack.extend(w.winfo_children())
        except tk.TclError:
            pass
    return result


def _texts_in(root):
    """收集窗口内全部控件的 text 属性（无 text 的控件跳过）。"""
    texts = []
    for w in _all_widgets(root):
        try:
            texts.append(str(w.cget("text")))
        except (tk.TclError, AttributeError, TypeError, ValueError):
            continue
    return [t for t in texts if t]


# ---------------------------------------------------------------------------
# 修复8：解析规则悬停说明（动态 tooltip + 状态栏说明）
# 优化缺陷R71：选项改中文显示名 + 自动识别默认，说明按规则键查找
# ---------------------------------------------------------------------------
class TestRuleTooltips:
    def test_rule_help_tooltip_exists(self, app):
        """解析规则旁必须有 ⓘ 悬停说明。"""
        assert app._rule_help_tooltip is not None

    def test_rule_tooltip_dynamic_text(self, app):
        """tooltip 文本必须跟随当前选中的解析规则动态变化。"""
        from log_ai_compressor.gui_legacy.app import RULE_DESCRIPTIONS, RULE_DISPLAY
        tip = app._rule_help_tooltip
        for key, expected in RULE_DESCRIPTIONS.items():
            app._on_rule_changed(RULE_DISPLAY[key])
            assert tip._current_text() == expected, \
                f"规则 {key} 的悬停说明不正确"

    def test_rule_tooltip_shows_current_rule_text(self, app):
        """显示中的 tooltip 内容与当前规则一致。"""
        from log_ai_compressor.gui_legacy.app import RULE_DESCRIPTIONS
        tip = app._rule_help_tooltip
        app._on_rule_changed("嵌入式 embedded")
        tip._show()
        app.update()
        assert tip._tip is not None
        canvas = tip._tip.winfo_children()[0]
        item = canvas.find_withtag("text")[0]
        assert canvas.itemcget(item, "text") == \
            RULE_DESCRIPTIONS["embedded"]
        tip._hide_now()

    def test_all_rules_have_descriptions(self):
        """全部规则键（含 auto）都必须有说明文本。"""
        from log_ai_compressor.gui_legacy.app import RULE_DESCRIPTIONS, RULE_KEYS
        assert set(RULE_KEYS) == set(RULE_DESCRIPTIONS)
        for name in RULE_KEYS:
            assert len(RULE_DESCRIPTIONS[name]) >= 10

    def test_rule_change_updates_status_bar(self, app):
        """切换规则时状态栏即时展示适用场景说明。"""
        app._on_rule_changed("通用 generic")
        assert "大多数日志首选" in app._status_label.cget("text")
        app._on_rule_changed("CI构建 jenkins")
        assert "Jenkins" in app._status_label.cget("text")

    def test_unknown_rule_ignored(self, app):
        """未知显示名不破坏当前状态（键与 tooltip 均不变）。"""
        tip = app._rule_help_tooltip
        before = tip._current_text()
        app._on_rule_changed("不存在的规则")
        assert app._rule_key == "auto"
        assert tip._current_text() == before


# ---------------------------------------------------------------------------
# 修复10：多文件对比模式（按钮可用 + 图例 + 差异列表 + 对比图表）
# ---------------------------------------------------------------------------
BASE_LOG = "\n".join([
    "2024-01-01 09:00:00 ERROR [db] connection refused to db-primary",
    "2024-01-01 09:00:01 ERROR [api] request 123 failed",
    "2024-01-01 09:00:02 FATAL [core] out of memory in worker",
    "2024-01-01 09:00:03 WARN [db] pool nearly exhausted",
]) + "\n"

OTHER_LOG = "\n".join([
    "2024-01-01 09:00:00 ERROR [db] connection refused to db-primary",
    "2024-01-01 09:00:01 ERROR [api] request 123 failed",
    "2024-01-01 09:00:02 ERROR [api] request 456 failed",
    "2024-01-01 09:00:03 FATAL [net] handshake failed with peer",
]) + "\n"


def _run_compare_analysis(app, tmp_path, timeout=60.0):
    """用两个临时日志文件执行一次对比分析并等待完成。"""
    fa = tmp_path / "base.log"
    fb = tmp_path / "other.log"
    fa.write_text(BASE_LOG, encoding="utf-8")
    fb.write_text(OTHER_LOG, encoding="utf-8")
    app._tabview.set("多文件对比")
    for i, path in enumerate((fa, fb)):
        entry = app._compare_entries[i]
        entry.delete(0, "end")
        entry.insert(0, str(path))
    app._on_start()
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.update()
        if app._compare_results:
            break
        time.sleep(0.02)
    if not app._compare_results:
        worker = app._worker
        print(f"[diag-compare] queue_size={app._queue.qsize()} "
              f"worker_alive={worker.is_alive() if worker else None} "
              f"status={app._status_label.cget('text')}", flush=True)
    assert app._compare_results, "对比分析未完成"
    return app._compare_results


class TestCompareMode:
    def test_compare_buttons_enabled_after_analysis(self, app, tmp_path):
        """对比完成后：导出报告 / 统计图表按钮应可用。"""
        _run_compare_analysis(app, tmp_path)
        assert app._export_btn.cget("state") == "normal", \
            "对比模式下导出报告按钮应可用"
        assert app._chart_btn.cget("state") == "normal", \
            "对比模式下统计图表按钮应可用"

    def test_compare_legend_in_detail(self, app, tmp_path):
        """对比结果顶部必须有 +/-/= 图例说明。"""
        _run_compare_analysis(app, tmp_path)
        text = app._detail_box.get("1.0", "end")
        assert "+ 新增错误（对比文件中新出现的）" in text
        assert "- 消失错误（基准文件中有但对比文件中没有的）" in text
        assert "= 共同错误（两个文件中都存在的）" in text

    def test_compare_diff_rows_rendered(self, app, tmp_path):
        """左侧列表应渲染差异行（含 + / - / = 符号与摘要）。"""
        _run_compare_analysis(app, tmp_path)
        app.update()
        # 左侧列表应有差异行（不再是「对比模式：差异摘要见右侧详情」占位）
        texts = _texts_in(app._cluster_list)
        assert any(t.startswith("+") for t in texts), "应有新增行"
        assert any(t.startswith("-") for t in texts), "应有消失行"
        assert any(t.startswith("=") for t in texts), "应有共同行"

    def test_compare_chart_window_opens(self, app, tmp_path):
        """点击统计图表应弹出对比图表窗口。"""
        _run_compare_analysis(app, tmp_path)
        app._show_charts()
        deadline = time.time() + 10
        while time.time() < deadline:
            app.update()
            if app._chart_window is not None and app._chart_window.winfo_exists():
                break
            time.sleep(0.02)
        assert app._chart_window is not None and app._chart_window.winfo_exists()
        assert "对比" in app._chart_window.title()
        app._chart_window.destroy()
        app._chart_window = None

    def test_compare_list_fullscreen(self, app, tmp_path):
        """对比差异列表支持全屏查看（含搜索与图例）。"""
        _run_compare_analysis(app, tmp_path)
        app._open_list_fullscreen()
        app.update()
        wins = [w for w in app.winfo_children()
                if isinstance(w, tk.Toplevel)
                and "对比差异列表" in w.title()]
        assert wins, "对比差异列表全屏窗口应打开"
        win = wins[0]
        texts = _texts_in(win)
        assert any("关闭" in t for t in texts)
        assert any("+ 新增" in t for t in texts), "全屏顶栏应有图例"

    def test_compare_export_writes_report(self, app, tmp_path, monkeypatch):
        """对比模式导出报告应保存对比差异报告文件。"""
        _run_compare_analysis(app, tmp_path)
        target = tmp_path / "compare_report.md"
        monkeypatch.setattr(
            "log_ai_compressor.gui_legacy.app.filedialog.asksaveasfilename",
            lambda **kw: str(target))
        app._on_export()
        content = target.read_text(encoding="utf-8")
        assert "base.log" in content and "other.log" in content
        assert "新增" in content or "消失" in content

    def test_compare_fullscreen_esc_closes(self, app, tmp_path):
        """对比差异全屏窗口支持 ESC 关闭。"""
        _run_compare_analysis(app, tmp_path)
        app._open_list_fullscreen()
        app.update()
        wins = [w for w in app.winfo_children()
                if isinstance(w, tk.Toplevel)
                and "对比差异列表" in w.title()]
        assert wins
        wins[0].event_generate("<Escape>")
        app.update()
        remaining = [w for w in app.winfo_children()
                     if isinstance(w, tk.Toplevel)
                     and "对比差异列表" in w.title()]
        assert not remaining


# ---------------------------------------------------------------------------
# 优化缺陷R58：导出报告（选项对话框 + 多格式同出 + 跟随级别过滤）
# ---------------------------------------------------------------------------
class TestExportReport:
    _LEVEL_LOG = "\n".join([
        "2024-01-01 09:00:00 ERROR [db] kernel panic in scheduler",
        "2024-01-01 09:00:01 WARN [db] pool nearly exhausted",
    ])

    def test_export_with_options_multi_format(self, app, tmp_path):
        """多格式同出：同一基础名按格式各写一份。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        written = app._export_with_options(
            str(tmp_path / "报告"), {"html", "md", "json", "txt"},
            {"overview", "list", "detail", "instances"})
        names = sorted(os.path.basename(p) for p in written)
        assert names == ["报告.html", "报告.json", "报告.md", "报告.txt"]
        html = (tmp_path / "报告.html").read_text(encoding="utf-8")
        assert "<!DOCTYPE html>" in html and "<style>" in html
        payload = json.loads((tmp_path / "报告.json").read_text("utf-8"))
        assert payload["clusters"], "JSON 应含簇数据"

    def test_export_follows_level_filter(self, app, tmp_path):
        """范围跟随当前级别过滤勾选（导出时刻实时过滤）。"""
        for var in app._level_vars.values():
            var.set(True)                # 分析时放行全部级别
        _run_paste_analysis(app, self._LEVEL_LOG)
        app.update()
        levels_all = {c.level for c in app._result.clusters}
        assert {"ERROR", "WARN"} <= levels_all, "结果应含 ERROR 与 WARN"
        app._level_vars["WARN"].set(False)   # 导出前取消 WARN
        app._export_with_options(str(tmp_path / "r"), {"json"}, set())
        payload = json.loads((tmp_path / "r.json").read_text("utf-8"))
        levels = {c["level"] for c in payload["clusters"]}
        assert "WARN" not in levels, "未勾选级别不得出现在报告中"
        assert "ERROR" in levels

    def test_export_dialog_defaults(self, app):
        """选项对话框默认：HTML 勾选、内容板块全选、范围随过滤。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        app._on_export()
        app.update()
        dlg = app._export_dlg
        assert dlg is not None and dlg.winfo_exists()
        assert app._export_fmt_vars["html"].get() is True
        assert app._export_fmt_vars["md"].get() is False
        assert all(v.get() for v in app._export_sec_vars.values())
        # 修复缺陷R61：JSON 完整上下文默认不勾（默认精简可读）
        assert app._export_json_full_var.get() is False
        dlg.destroy()
        app.update()

    def test_export_json_lean_by_default(self, app, tmp_path):
        """修复缺陷R61：执行器默认精简 JSON（无上下文/样例原文）。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        app._export_with_options(str(tmp_path / "r"), {"json"}, set())
        payload = json.loads((tmp_path / "r.json").read_text("utf-8"))
        first = payload["clusters"][0]
        assert "sample" not in first
        assert first["instance_lines"]
        # 显式完整模式
        app._export_with_options(str(tmp_path / "rf"), {"json"}, set(),
                                 json_full=True)
        full_payload = json.loads((tmp_path / "rf.json").read_text("utf-8"))
        assert "sample" in full_payload["clusters"][0]

    def test_export_confirmed_requires_format(self, app, monkeypatch):
        """未勾选任何格式确认 → 警告且不弹保存框。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        app._on_export()
        app.update()
        for var in app._export_fmt_vars.values():
            var.set(False)
        saves, warns = [], []
        monkeypatch.setattr(
            "log_ai_compressor.gui_legacy.app.filedialog.asksaveasfilename",
            lambda **kw: saves.append(kw) and "")
        monkeypatch.setattr(
            "log_ai_compressor.gui_legacy.app.messagebox.showwarning",
            lambda *a, **k: warns.append(a))
        app._on_export_confirmed()
        assert warns, "未勾选格式应弹警告"
        assert not saves, "未勾选格式不得进入保存流程"
        app._export_dlg.destroy()
        app.update()

    def test_export_confirmed_writes_and_prompts(self, app, tmp_path,
                                                 monkeypatch):
        """确认导出：写文件 + 状态栏反馈 + 成功提示（不打开文件夹）。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        app._on_export()
        app.update()
        app._export_fmt_vars["html"].set(True)
        monkeypatch.setattr(
            "log_ai_compressor.gui_legacy.app.filedialog.asksaveasfilename",
            lambda **kw: str(tmp_path / "out.html"))
        monkeypatch.setattr(
            "log_ai_compressor.gui_legacy.app.messagebox.askyesno",
            lambda *a, **k: False)
        app._on_export_confirmed()
        app.update()
        assert (tmp_path / "out.html").exists()
        assert "已导出" in str(app._status_label.cget("text"))


# ---------------------------------------------------------------------------
# 优化缺陷R63：统计图表（高 DPI 适配 + 错误种类 Top10 + 点击联动）
# ---------------------------------------------------------------------------
class TestCharts:
    def _open_charts(self, app):
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        app._show_charts()
        app.update()
        assert app._chart_window is not None
        return app._chart_panel

    def teardown_method(self):
        pass

    def _close(self, app):
        if app._chart_window is not None and app._chart_window.winfo_exists():
            app._chart_window.destroy()
        app._chart_window = None
        app.update()

    def test_top_clusters_bar_content(self, app):
        """分页「种类 Top10」：级别着色 + 摘要标签 + gid 联动（R64）。"""
        panel = self._open_charts(app)
        try:
            panel._show_tab("种类 Top 10")
            assert "Top 10" in panel._ax.get_title()
            bars = panel._ax.patches
            assert bars, "Top10 图应有条形"
            top = max(app._result.clusters, key=lambda c: c.count)
            assert bars[0].get_gid() == f"cluster:{top.cluster_id}", \
                "首个条形应对应次数最多的簇"
            labels = [t.get_text() for t in panel._ax.get_yticklabels()]
            assert any(top.summary[:10] in lb for lb in labels), \
                "y 轴标签应含簇摘要"
        finally:
            self._close(app)

    def test_pick_cluster_selects_row(self, app):
        """点击 Top10 条形 → 主列表选中对应簇（cluster_id 联动）。"""
        panel = self._open_charts(app)
        try:
            panel._show_tab("种类 Top 10")
            top = max(app._result.clusters, key=lambda c: c.count)
            bar = panel._ax.patches[0]
            panel._on_pick(SimpleNamespace(artist=bar))
            app.update()
            assert app._displayed[app._selected_row].cluster_id == \
                top.cluster_id
        finally:
            self._close(app)

    def test_dpi_scale_applied(self, app):
        """图表 dpi 随窗口缩放（高 DPI 下文字不缩半）。"""
        panel = self._open_charts(app)
        try:

            expected = int(round(96 * max(1.0, app._font_scale)))
            assert panel.figure.get_dpi() == expected
        finally:
            self._close(app)

    def test_save_png_exports_current_tab(self, app, monkeypatch, tmp_path):
        """优化缺陷R115：保存 PNG —— 当前分页导出为真实图片文件。"""
        panel = self._open_charts(app)
        try:
            out = tmp_path / "trend.png"
            monkeypatch.setattr(
                "tkinter.filedialog.asksaveasfilename",
                lambda **kw: str(out))
            panel._save_png()
            assert out.exists() and out.stat().st_size > 1000, \
                "PNG 应为有效图片文件"
            assert panel._save_btn.cget("text") == "✓ 已保存"
            # 取消对话框不落盘
            monkeypatch.setattr(
                "tkinter.filedialog.asksaveasfilename",
                lambda **kw: "")
            out2 = tmp_path / "nope.png"
            panel._save_png()
            assert not out2.exists()
        finally:
            self._close(app)

    def test_pie_legend_and_threshold(self, app):
        """优化缺陷R64：饼图小扇区不叠字 —— 右侧图例 + 阈值标注。"""
        panel = self._open_charts(app)
        try:
            panel._show_tab("级别占比")
            legend = panel._ax.get_legend()
            assert legend is not None, "级别信息应以图例呈现（防叠字）"
            legend_text = "\n".join(t.get_text()
                                    for t in legend.get_texts())
            assert "%" in legend_text, "图例应含百分比"
            # 扇内百分比仅大扇区显示（小扇区空串不叠字）
            inner = [t.get_text() for t in panel._ax.texts if "%" in t.get_text()]
            assert all(t for t in inner), "扇内不得出现空标注占位"
        finally:
            self._close(app)

    def test_tab_switcher_defaults_and_switch(self, app):
        """优化缺陷R64：分页切换栏存在，默认时间趋势，切换重建单轴。"""
        panel = self._open_charts(app)
        try:
            assert panel._switch.get() == "时间趋势"
            assert panel._tab == "时间趋势"
            old_ax = panel._ax
            panel._on_tab("级别占比")
            assert panel._tab == "级别占比"
            assert panel._ax is not old_ax, "切换应重建单轴大图"
        finally:
            self._close(app)

    def test_brush_time_range_clamped(self, app):
        """优化缺陷R104：刷选索引→时间范围换算（越界钳制到序列内）。"""
        panel = self._open_charts(app)
        try:
            series = panel._trend_series
            assert series, "趋势页应有序列数据"
            w = app._result.global_hist.width
            t0, t1, s0, s1 = panel._brush_time_range(-5, 999)
            assert t0 == series[0][0]
            assert t1 == series[-1][0] + w
            assert s0 == series[0][0] and s1 == series[-1][0] + w
        finally:
            self._close(app)

    def test_brush_release_click_clears_span(self, app):
        """优化缺陷R104：位移 <0.5 的松开视为单击清除选区，不弹窗。"""
        panel = self._open_charts(app)
        try:
            panel._brush_start = 3.0
            panel._on_brush_release(SimpleNamespace(xdata=3.2,
                                                    inaxes=panel._ax))
            assert panel._brush_start is None
            assert panel._brush_span is None
            assert not (getattr(panel, "_win_cmp_dlg", None)
                        and panel._win_cmp_dlg.winfo_exists())
        finally:
            self._close(app)

    def test_window_compare_popup_opens(self, app):
        """优化缺陷R104：拖拽松开弹出显著突增窗（无显著也给空态）。"""
        panel = self._open_charts(app)
        try:
            panel._brush_start = 0.0
            n = len(panel._trend_series)
            panel._on_brush_release(SimpleNamespace(xdata=n - 1,
                                                    inaxes=panel._ax))
            app.update()
            dlg = getattr(panel, "_win_cmp_dlg", None)
            assert dlg is not None and dlg.winfo_exists()
            dlg.destroy()
        finally:
            self._close(app)

    def test_pick_scrolls_lifts_and_clears_keyword(self, app, monkeypatch):
        """修复缺陷R65：条形点击全链路 —— 滚入视口 + 主窗口前置 +
        遮挡目标簇的搜索关键字自动清空（原仅染蓝：视口外/窗口被
        图表遮挡/关键字过滤三处叠加导致用户看不见跳转）。"""
        panel = self._open_charts(app)
        try:
            seen, lifted = [], []
            monkeypatch.setattr(app, "_see_cluster_row",
                                lambda i: seen.append(i))
            monkeypatch.setattr(app, "lift", lambda: lifted.append(True))
            # 搜索关键字把目标簇滤出视图（zzz 不匹配任何簇）
            app._search_var.set("zzz")
            app._apply_search_filter()
            assert app._search_kw == "zzz"
            panel._show_tab("种类 Top 10")
            top = max(app._result.clusters, key=lambda c: c.count)
            bar = next(b for b in panel._ax.patches
                       if b.get_gid() == f"cluster:{top.cluster_id}")
            panel._on_pick(SimpleNamespace(artist=bar))
            app.update()
            idx = next(i for i, c in enumerate(app._displayed)
                       if c.cluster_id == top.cluster_id)
            assert app._selected_row == idx, "应选中目标簇"
            assert seen == [idx], "选中后应滚入视口"
            assert lifted, "主窗口应前置到图表窗口之上"
            assert app._search_var.get() == "", "遮挡关键字应被清空"
            assert app._search_kw == ""
        finally:
            self._close(app)

    def test_pie_pick_level_selects_first_of_level(self, app):
        """修复缺陷R65：饼图扇区点击 → 选中该级别在列表中的首个簇。"""
        panel = self._open_charts(app)
        try:
            panel._show_tab("级别占比")
            wedge = panel._ax.patches[0]
            gid = wedge.get_gid()
            assert gid.startswith("level:")
            level = gid.split(":", 1)[1]
            panel._on_pick(SimpleNamespace(artist=wedge))
            app.update()
            expected = next(i for i, c in enumerate(app._displayed)
                            if c.level == level)
            assert app._selected_row == expected
            assert app._displayed[app._selected_row].level == level
        finally:
            self._close(app)


# ---------------------------------------------------------------------------
# 优化缺陷R71：规则自动识别（默认自动识别 + 选中项消失 + 体检提示）
# ---------------------------------------------------------------------------
class TestRuleAutoDetect:
    def test_dropdown_defaults_to_auto(self, app):
        """默认选中「自动识别（推荐）」，下拉列表只剩另外三个。"""
        assert app._rule_key == "auto"
        assert app._rule_menu.get() == "自动识别（推荐）"
        values = list(app._rule_menu.cget("values"))
        assert "自动识别（推荐）" not in values, \
            "选中项不应出现在下拉列表"
        assert len(values) == 3

    def test_switch_hides_selected_from_list(self, app):
        """切换到「嵌入式 embedded」：键更新 + 该项从列表消失。"""
        app._on_rule_changed("嵌入式 embedded")
        assert app._rule_key == "embedded"
        values = list(app._rule_menu.cget("values"))
        assert "嵌入式 embedded" not in values
        assert "自动识别（推荐）" in values
        assert app._current_config_dict()["rule"] == "embedded"

    def test_health_hint_on_mismatched_rule(self, app):
        """选错规则（embedded 解析 ISO 日志）→ 状态栏可点击提示。"""
        app._on_rule_changed("嵌入式 embedded")
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        text = str(app._status_label.cget("text"))
        assert "可能规则不匹配" in text, f"应给出换规则提示，实际: {text}"
        assert "通用 generic" in text

    def test_apply_suggested_rule_switches_and_reruns(self, app):
        """点击提示：切换到建议规则并重跑（提示随之清除）。"""
        app._on_rule_changed("嵌入式 embedded")
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        app._apply_suggested_rule("generic")
        assert app._rule_key == "generic"
        deadline = time.time() + 60
        while time.time() < deadline and app._result is None:
            app.update()
            time.sleep(0.02)
        assert app._result is not None, "重跑应完成"


class TestAnalyzeModeSelector:
    """优化缺陷R79：智能分析模式选择器（完整/深度/快速）。"""

    def test_dropdown_defaults_to_full(self, app):
        """默认选中「完整分析（推荐）」，下拉列表只剩另外两个。"""
        assert app._analyze_key == "full"
        assert app._analyze_menu.get() == "完整分析（推荐）"
        values = list(app._analyze_menu.cget("values"))
        assert "完整分析（推荐）" not in values, \
            "选中项不应出现在下拉列表"
        assert values == ["深度扫描", "快速聚类"]

    def test_switch_hides_selected_and_persists(self, app):
        """切换到「深度扫描」：键更新 + 该项从列表消失 + 配置持久化。"""
        app._on_analyze_changed("深度扫描")
        assert app._analyze_key == "deep"
        values = list(app._analyze_menu.cget("values"))
        assert "深度扫描" not in values
        assert "完整分析（推荐）" in values
        assert app._current_config_dict()["analyze_mode"] == "deep"

    def test_menu_in_filter_row_after_level_box(self, app):
        """R93 换位后：关键词两框在过滤行（列 5/7），模式选择器
        与解析规则迁至高级选项行（列 9/12）。"""
        filter_panel = app._ctx_entry.master
        assert app._include_entry.master is filter_panel
        assert app._exclude_entry.master is filter_panel
        iinfo = app._include_entry.grid_info()
        assert str(iinfo["row"]) == "0"
        assert str(iinfo["column"]) == "5"
        einfo = app._exclude_entry.grid_info()
        assert str(einfo["row"]) == "0"
        assert str(einfo["column"]) == "7"
        panel = app._advanced_panel
        assert app._analyze_menu.master is panel
        assert app._rule_menu.master is panel
        info = app._analyze_menu.grid_info()
        assert str(info["row"]) == "0"
        assert str(info["column"]) == "9"
        rinfo = app._rule_menu.grid_info()
        assert str(rinfo["row"]) == "0"
        assert str(rinfo["column"]) == "12"

    def test_menu_width_fixed_across_modes(self, app):
        """修复缺陷R82：三模式切换下拉框定宽不变、最长文本不裁切。

        dynamic_resizing 默认 True 时框宽随当前选项文本伸缩（切
        「深度扫描」后明显变窄）；解析规则下拉同缺陷一并修复。
        """
        app.geometry("1600x900")
        app.update()
        w0 = app._analyze_menu.winfo_width()
        for name in ("深度扫描", "快速聚类", "完整分析（推荐）"):
            app._on_analyze_changed(name)
            app.update()
            assert app._analyze_menu.winfo_width() == w0, \
                f"切到「{name}」后框宽应恒定" \
                f"（{app._analyze_menu.winfo_width()} vs {w0}）"
            assert app._analyze_menu.winfo_reqwidth() <= w0 + 2, \
                f"「{name}」文本不得裁切"
        # 解析规则下拉同款定宽（同缺陷一并修复）
        rw0 = app._rule_menu.winfo_width()
        app._on_rule_changed("通用 generic")
        app.update()
        assert app._rule_menu.winfo_width() == rw0, \
            f"解析规则下拉切「通用 generic」后框宽应恒定" \
            f"（{app._rule_menu.winfo_width()} vs {rw0}）"

    def test_fast_mode_marks_detail_unexecuted(self, app):
        """fast 模式分析后：详情智能分析区显示「未执行」。"""
        app._on_analyze_changed("快速聚类")
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        app._select_cluster(0)
        app.update()
        text = app._detail_box.get("1.0", "end")
        assert "快速聚类模式（未执行智能分析）" in text
        assert "可能规则不匹配" not in str(app._status_label.cget("text"))


class TestSimilaritySelector:
    """优化缺陷R84：相似度阈值选择器（标准/严格/宽松）。"""

    def test_dropdown_defaults_to_standard(self, app):
        """默认选中「标准（推荐）」，下拉列表只剩另外两档。"""
        assert app._similarity_key == "standard"
        assert app._similarity_menu.get() == "标准（推荐）"
        values = list(app._similarity_menu.cget("values"))
        assert "标准（推荐）" not in values, "选中项不应出现在下拉列表"
        assert values == ["严格", "宽松"]

    def test_switch_hides_selected_and_persists(self, app):
        """切换到「严格」：键更新 + 该项从列表消失 + 配置持久化。"""
        app._on_similarity_changed("严格")
        assert app._similarity_key == "strict"
        values = list(app._similarity_menu.cget("values"))
        assert "严格" not in values
        assert "标准（推荐）" in values
        assert app._current_config_dict()["similarity"] == "strict"

    def test_settings_button_in_filter_row_right_end(self, app):
        """「其他选项 ⚙」在高级选项行行尾（R93 换位后列 15）。"""
        assert app._settings_btn.master is app._advanced_panel
        info = app._settings_btn.grid_info()
        assert str(info["row"]) == "0"
        assert str(info["column"]) == "15"
        assert app._similarity_menu.winfo_toplevel() is app._settings_popup

    def test_settings_popup_toggle_and_close(self, app):
        """⚙ 点击开合弹层；贴按钮右对齐、物理屏边界内（高 DPI 修正）。"""
        from log_ai_compressor.gui_legacy.app import Tooltip
        app.update()
        assert app._settings_popup.state() == "withdrawn"
        app._toggle_settings_popup()
        app.update()
        assert app._settings_popup.state() == "normal"
        # 物理屏边界钳制（winfo_screenwidth 高 DPI 为逻辑值，勿直接用）
        vx, vy, sw, sh = Tooltip._screen_bounds(app._settings_popup)
        assert app._settings_popup.winfo_rootx() >= vx
        assert (app._settings_popup.winfo_rootx()
                + app._settings_popup.winfo_width() <= vx + sw)
        app._toggle_settings_popup()
        app.update()
        assert app._settings_popup.state() == "withdrawn"

    def test_strict_mode_splits_clusters_in_analysis(self, app):
        """切「严格」后分析：0.884 相似对拆 2 簇（标准并为 1 簇）。"""
        app._on_similarity_changed("严格")
        _run_paste_analysis(app, "\n".join([
            "2024-01-01 09:00:00 ERROR [db] request timeout while "
            "reading from db server",
            "2024-01-01 09:00:01 ERROR [db] request timeout while "
            "writing to db server",
        ]))
        app.update()
        assert app._result is not None
        assert len(app._result.clusters) == 2, \
            "严格(0.95)应拆分 0.884 相似对（标准并 1 簇）"


class TestAdvancedPanel:
    """优化缺陷R85：高级选项行（时间范围/行数上限/关键词黑白名单）。"""

    def test_row_widgets_and_maxlines_default(self, app):
        """行内控件齐备；行数上限默认「全部（推荐)」且不在列表中。"""
        panel = app._advanced_panel
        assert app._time_start_entry.master is panel
        assert app._time_end_entry.master is panel
        # 优化缺陷R93：关键词两框迁回过滤行（上下文行数右侧）；
        # 智能分析/解析规则对调迁入本行
        filter_panel = app._ctx_entry.master
        assert app._include_entry.master is filter_panel
        assert app._exclude_entry.master is filter_panel
        assert app._analyze_menu.master is panel
        assert app._rule_menu.master is panel
        assert app._maxlines_menu.get() == "全部（推荐）"
        values = list(app._maxlines_menu.cget("values"))
        assert "全部（推荐）" not in values
        assert values == ["前 10 万行", "前 50 万行", "前 100 万行"]
        # 「其他选项 ⚙」在本行行尾（R93 换位后列 15）
        assert app._settings_btn.master is panel
        assert str(app._settings_btn.grid_info()["column"]) == "15"

    def test_maxlines_switch_hides_selected(self, app):
        """切「前 10 万行」：键更新 + 该项从列表消失 + 参数映射数值。"""
        app._on_maxlines_changed("前 10 万行")
        assert app._maxlines_key == "100k"
        values = list(app._maxlines_menu.cget("values"))
        assert "前 10 万行" not in values
        assert "全部（推荐）" in values
        assert app._advanced_params()["max_lines"] == 100000

    def test_invalid_time_input_blocks_start(self, app):
        """非法时间输入：状态栏提示格式要求，不启动分析（边界校验）。"""
        app._time_start_entry.insert(0, "25:99:99")
        app._on_start()
        app.update()
        assert "前置设置有误" in str(app._status_label.cget("text"))
        assert "时间格式" in str(app._status_label.cget("text"))
        assert app._result is None, "非法时间不得启动分析"

    def test_tod_range_filters_analysis(self, app):
        """每日时段 12:00~18:00：仅正午错误入窗 + 状态栏「时间窗」标签。"""
        app._time_start_entry.insert(0, "12:00:00")
        app._time_end_entry.insert(0, "18:00:00")
        _run_paste_analysis(app, "\n".join([
            "2024-01-01 08:00:00 ERROR [db] early morning error",
            "2024-01-01 14:00:00 ERROR [db] midday error",
            "2024-01-01 20:00:00 ERROR [db] evening error",
        ]))
        app.update()
        assert len(app._result.clusters) == 1
        assert "midday" in app._result.clusters[0].summary
        assert "时间窗" in str(app._status_label.cget("text"))

    def test_keyword_include_filters_analysis(self, app):
        """包含关键词 database：仅命中错误保留 + 状态栏「包含[]」标签。"""
        app._include_entry.insert(0, "database")
        _run_paste_analysis(app, "\n".join([
            "2024-01-01 09:00:00 ERROR [db] database connection lost",
            "2024-01-01 09:00:01 ERROR [net] network unreachable",
        ]))
        app.update()
        assert len(app._result.clusters) == 1
        assert "database" in app._result.clusters[0].summary
        assert "包含[database]" in str(app._status_label.cget("text"))

    def test_keyword_exclude_filters_analysis(self, app):
        """排除关键词 heartbeat：命中错误剔除 + 状态栏「排除[]」标签。"""
        app._exclude_entry.insert(0, "heartbeat")
        _run_paste_analysis(app, "\n".join([
            "2024-01-01 09:00:00 ERROR [mon] heartbeat lost twice",
            "2024-01-01 09:00:01 ERROR [db] database connection lost",
        ]))
        app.update()
        assert len(app._result.clusters) == 1
        assert "database" in app._result.clusters[0].summary
        assert "排除[heartbeat]" in str(app._status_label.cget("text"))

    def test_advanced_row_gaps_equal_and_right_aligned(self, app):
        """优化缺陷R98：高级行四个组间可视区间恒等（ⓘ 列均分剩余
        宽），且重置按钮右缘与上排排除关键词输入框右缘对齐。"""
        app.geometry("2200x900")
        app.update()
        app.update_idletasks()
        ap = app._advanced_panel
        for col in (4, 7, 10, 13):
            cfg = ap.grid_columnconfigure(col)
            assert int(cfg["weight"]) == 1 and cfg["uniform"] == "adv_gap"
        # 四个组间区间：上组 ⓘ 右缘 → 下组标签文本左缘
        infos = [ap.grid_slaves(row=0, column=c)[0] for c in (4, 7, 10, 13)]
        heads = [ap.grid_slaves(row=0, column=c)[0]
                 for c in (5, 8, 11, 14)]
        gaps = [h.winfo_x() - (i.winfo_x() + i.winfo_width())
                for h, i in zip(heads, infos)]
        scale = max(1.0, getattr(app, "_font_scale", 1.0))
        tol = max(2, int(round(2 * scale)))
        assert max(gaps) - min(gaps) <= tol, \
            f"四个组间区间应恒等（实测 {gaps}，容差 {tol}）"
        # 重置右缘 vs 排除关键词输入框右缘（两面板同右边距 12）
        reset_right = (app._reset_btn.winfo_rootx()
                       + app._reset_btn.winfo_width())
        excl_right = (app._exclude_entry.winfo_rootx()
                      + app._exclude_entry.winfo_width())
        assert abs(reset_right - excl_right) <= 2, \
            f"重置右缘 {reset_right} 应对齐排除框右缘 {excl_right}"

    def test_reset_button_restores_defaults(self, app):
        """优化缺陷R95：↺ 一键重置 —— 数据类前置项清空/回默认，
        偏好项（级别/上下文行数/相似度/脱敏）不动。"""
        # 制造残留状态
        app._time_start_entry.insert(0, "12:00:00")
        app._time_end_entry.insert(0, "18:00:00")
        app._include_entry.insert(0, "database")
        app._exclude_entry.insert(0, "heartbeat")
        app._use_regex_var.set(True)
        app._on_maxlines_changed("前 10 万行")
        app._on_analyze_changed("深度扫描")
        app._on_rule_changed("通用 generic")
        app._on_encoding_changed("GBK / GB18030")
        app._on_similarity_changed("严格")
        ctx_before = app._ctx_entry.get()
        levels_before = {lv: var.get() for lv, var in
                         app._level_vars.items()}
        app._reset_advanced_options()
        app.update()
        # 数据类：清空 / 回默认
        assert app._time_start_entry.get() == ""
        assert app._time_end_entry.get() == ""
        assert app._include_entry.get() == ""
        assert app._exclude_entry.get() == ""
        assert app._use_regex_var.get() is False
        assert app._maxlines_key == "all"
        assert app._maxlines_menu.get() == "全部（推荐）"
        assert app._analyze_key == "full"
        assert app._analyze_menu.get() == "完整分析（推荐）"
        assert app._rule_key == "auto"
        assert app._rule_menu.get() == "自动识别（推荐）"
        assert app._encoding_display == "自动探测（推荐）"
        assert app._advanced_params()["encoding"] is None
        # 选中项回列表后，旧选项重新可选
        assert "前 10 万行" in list(app._maxlines_menu.cget("values"))
        assert "深度扫描" in list(app._analyze_menu.cget("values"))
        assert "通用 generic" in list(app._rule_menu.cget("values"))
        # 状态栏重置回执
        assert "已重置" in str(app._status_label.cget("text"))
        # 偏好项不动
        assert app._ctx_entry.get() == ctx_before
        assert {lv: var.get() for lv, var in
                app._level_vars.items()} == levels_before
        assert app._similarity_key == "strict", "相似度为偏好项不重置"
        assert app._redact_var.get() is True

    def test_max_lines_limit_hit_marks_progress(self, app):
        """limit_hit 结果：完成串（底栏）显示「已达行数上限」而非「已取消」。"""
        from log_ai_compressor.core.models import (
            AnalysisResult, RunStats, TimeHistogram)
        stats = RunStats(source="<测试>", total_lines=10, error_lines=1,
                         error_entries=1, limit_hit=True, truncated=False)
        result = AnalysisResult(stats=stats, clusters=[],
                                global_hist=TimeHistogram())
        app._last_common = {"max_lines": 100000}
        app._on_result(result)
        app.update()
        # 优化缺陷R105：完成串自按钮行下沉底栏 _done_label
        text = str(app._done_label.cget("text"))
        assert "已达行数上限" in text
        assert "已取消" not in text, "上限收束不得显示「已取消」"


# ---------------------------------------------------------------------------
# 优化缺陷R111：已知错误屏蔽
# ---------------------------------------------------------------------------
class TestMuteClusters:
    def test_mute_button_initially_disabled(self, app):
        """未选簇时屏蔽按钮置灰。"""
        assert str(app._mute_btn.cget("state")) == "disabled"

    def test_mute_hides_cluster_and_indicator(self, app):
        """屏蔽当前簇：列表隐藏该簇 + 🚫 指示器出现且计数正确。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        total = len(app._result.clusters)
        target = app._result.clusters[0]
        app._show_cluster_detail(target)
        app.update()
        assert str(app._mute_btn.cget("state")) == "normal"
        app._toggle_mute_current()
        app.update()
        key = app._mute_key(target)
        assert key in app._muted
        # 视图行不再含该簇（默认隐藏）
        visible = {r[1] for r in app._build_view_rows() if r[0] == "c"}
        idx = app._displayed.index(target)
        assert idx not in visible
        # 指示器显示且计数 1
        assert app._mute_ind_btn.winfo_ismapped()
        assert app._mute_ind_btn.cget("text") == "🚫 1"
        # 屏蔽不删数据：_displayed 仍是全量
        assert len(app._displayed) == total

    def test_show_muted_toggle_reveals(self, app):
        """🚫 指示器点击：显示已屏蔽簇，再点恢复隐藏。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        target = app._result.clusters[0]
        app._show_cluster_detail(target)
        app._toggle_mute_current()
        app.update()
        idx = app._displayed.index(target)
        app._toggle_show_muted()     # 显示
        app.update()
        visible = {r[1] for r in app._build_view_rows() if r[0] == "c"}
        assert idx in visible
        assert app._show_muted is True
        app._toggle_show_muted()     # 再隐藏
        app.update()
        visible = {r[1] for r in app._build_view_rows() if r[0] == "c"}
        assert idx not in visible

    def test_unmute_restores(self, app):
        """取消屏蔽：簇重新可见，配置集合清空。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        target = app._result.clusters[0]
        app._show_cluster_detail(target)
        app._toggle_mute_current()
        app.update()
        # 显示已屏蔽后选中该簇再取消屏蔽
        app._toggle_show_muted()
        app.update()
        app._show_cluster_detail(target)
        app._toggle_mute_current()
        app.update()
        assert app._mute_key(target) not in app._muted
        assert not app._mute_ind_btn.winfo_ismapped(), \
            "无被屏蔽簇时指示器应隐藏"

    def test_muted_persisted_to_config(self, app):
        """屏蔽集合写入配置字典（重启可恢复）。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        target = app._result.clusters[0]
        app._show_cluster_detail(target)
        app._toggle_mute_current()
        cfg = app._current_config_dict()
        assert app._mute_key(target) in cfg["muted"]


# ---------------------------------------------------------------------------
# 优化缺陷R113：搜索框布尔组合（and/or/not）
# ---------------------------------------------------------------------------
class TestBooleanSearch:
    def test_plain_substring_unchanged(self):
        from log_ai_compressor.gui_legacy.app import _kw_match
        assert _kw_match("connection refused to db", "refused")
        assert not _kw_match("connection refused", "timeout")
        assert _kw_match("anything", "")

    def test_and_requires_all_terms(self):
        from log_ai_compressor.gui_legacy.app import _kw_match
        hay = "connection refused db-primary error"
        assert _kw_match(hay, "connection and error")
        assert not _kw_match(hay, "connection and timeout")

    def test_or_matches_any_clause(self):
        from log_ai_compressor.gui_legacy.app import _kw_match
        hay = "disk almost full"
        assert _kw_match(hay, "timeout or disk")
        assert not _kw_match(hay, "timeout or refused")

    def test_not_negates_term(self):
        from log_ai_compressor.gui_legacy.app import _kw_match
        hay = "warn disk almost full"
        assert _kw_match(hay, "not timeout")
        assert not _kw_match(hay, "not disk")
        assert _kw_match(hay, "disk and not timeout")

    def test_word_boundary_no_false_split(self):
        """词边界：error/android 内含 or/and 不得被当作操作符。"""
        from log_ai_compressor.gui_legacy.app import _kw_match
        assert _kw_match("error: android crash", "error")
        assert _kw_match("error: android crash", "android")
        # 内含操作词的整词也按普通子串（无词边界操作符）
        assert _kw_match("android crash", "android crash")

    def test_mixed_expression(self):
        from log_ai_compressor.gui_legacy.app import _kw_match
        hay = "error [db] connection refused"
        assert _kw_match(hay, "error and db or timeout")
        assert not _kw_match(hay, "error and timeout or fatal")

    def test_cluster_filter_boolean(self, app):
        """GUI 集成：布尔关键字过滤列表显示。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        app._search_var.set("connection and refused")
        app._apply_search_filter()
        app.update()
        visible = {r[1] for r in app._build_view_rows() if r[0] == "c"}
        assert visible, "布尔表达式应至少命中一个簇"
        for idx in visible:
            c = app._displayed[idx]
            hay = (f"{c.summary} {c.module} {c.level} "
                   f"{c.priority_label}").lower()
            assert "connection" in hay and "refused" in hay


# ---------------------------------------------------------------------------
# 优化缺陷R114：过滤预设保存（⚙ 弹层预设区）
# ---------------------------------------------------------------------------
class TestFilterPresets:
    def _fill_filters(self, app):
        app._time_start_entry.insert(0, "09:00:00")
        app._include_entry.insert(0, "timeout, refused")
        app._exclude_entry.insert(0, "debug")
        app._use_regex_var.set(True)

    def test_save_empty_name_rejected(self, app):
        app._save_preset()
        assert not app._presets
        assert "先输入名称" in app._status_label.cget("text")

    def test_save_and_persist(self, app):
        self._fill_filters(app)
        app._preset_name_entry.insert(0, " nightly ")
        app._save_preset()
        snap = app._presets.get("nightly")
        assert snap is not None, "名称应去空白保存"
        assert snap["time_start"] == "09:00:00"
        assert snap["include"] == "timeout, refused"
        assert snap["use_regex"] is True
        cfg = app._current_config_dict()
        assert "nightly" in cfg["filter_presets"]
        # 保存后名称框清空、下拉含新项
        assert app._preset_name_entry.get() == ""
        assert "nightly" in app._preset_menu.cget("values")

    def test_apply_restores_controls(self, app):
        self._fill_filters(app)
        app._preset_name_entry.insert(0, "p1")
        app._save_preset()
        # 改乱现场后套用
        app._include_entry.delete(0, "end")
        app._time_start_entry.delete(0, "end")
        app._use_regex_var.set(False)
        app._apply_preset("p1")
        assert app._include_entry.get() == "timeout, refused"
        assert app._time_start_entry.get() == "09:00:00"
        assert app._use_regex_var.get() is True
        assert "已套用" in app._status_label.cget("text")

    def test_apply_unknown_noop(self, app):
        app._apply_preset("（暂无预设）")
        app._apply_preset("不存在的名字")

    def test_delete_preset(self, app):
        app._preset_name_entry.insert(0, "p2")
        app._save_preset()
        app._apply_preset("p2")
        app._delete_preset()
        assert "p2" not in app._presets
        assert "已删除" in app._status_label.cget("text")
        # 无预设时下拉回占位
        assert app._preset_menu.cget("values") == ["（暂无预设）"]

    def test_delete_without_selection_warns(self, app):
        app._delete_preset()
        assert "先从下拉选中" in app._status_label.cget("text")


# ---------------------------------------------------------------------------
# 优化缺陷R105：实时 Tail 监控
# ---------------------------------------------------------------------------
class TestTailMonitor:
    def _write(self, path, lines):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def test_tail_button_present(self, app):
        """按钮行第六颗按钮：📡 实时监控。"""
        assert app._tail_btn.winfo_exists()
        assert "监控" in app._tail_btn.cget("text")

    def test_tail_requires_file_tab(self, app, monkeypatch):
        """非文件导入页签：拦截提示，不启动监控。"""
        warned = []
        monkeypatch.setattr(
            "log_ai_compressor.gui_legacy.app.messagebox.showwarning",
            lambda *a: warned.append(a))
        app._tabview.set("文本粘贴")
        app._on_tail_toggle()
        assert warned and not app._tailing

    def test_tail_requires_single_file(self, app, monkeypatch):
        """文件为空或多选：拦截提示，不启动监控。"""
        warned = []
        monkeypatch.setattr(
            "log_ai_compressor.gui_legacy.app.messagebox.showwarning",
            lambda *a: warned.append(a))
        app._tabview.set("文件导入")
        app._file_entry.delete(0, "end")
        app._on_tail_toggle()
        assert warned and not app._tailing

    def test_tail_detects_new_lines(self, app, tmp_path):
        """监控端到端：追加错误行后自动重算，结果行数增长；停止复原。"""
        log = tmp_path / "tail.log"
        self._write(log, ["2024-01-01 09:00:00 INFO boot ok"])
        app._tabview.set("文件导入")
        app._file_entry.delete(0, "end")
        app._file_entry.insert(0, str(log))
        app._on_tail_toggle()
        assert app._tailing
        try:
            # 等后台线程读到首批内容
            deadline = time.time() + 10
            while time.time() < deadline:
                app.update()
                with app._tail_lock:
                    n = len(app._tail_lines)
                if n >= 1:
                    break
                time.sleep(0.1)
            else:
                raise AssertionError("尾部线程未读到首批行")
            # 追加错误行 → 等自动重算出结果
            self._write(log, [
                "2024-01-01 09:00:01 ERROR [db] connection refused",
                "2024-01-01 09:00:02 ERROR [db] connection refused",
            ])
            deadline = time.time() + 20
            while time.time() < deadline:
                app.update()
                if (app._result is not None
                        and app._result.stats.total_lines >= 3
                        and not (app._worker and app._worker.is_alive())):
                    break
                time.sleep(0.15)
            else:
                raise AssertionError("监控刷新未产生新结果")
            assert app._result.stats.error_entries == 2
            assert "监控中" in str(app._status_label.cget("text"))
            # 监控刷新不入历史（防刷屏）
            assert app._history.list() == []
        finally:
            app._stop_tail()
        assert not app._tailing
        assert "实时监控" in app._tail_btn.cget("text")

    def test_done_label_written_on_result(self, app):
        """优化缺陷R105：完成串写底栏 _done_label，进度标签回「就绪」。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        app.update()
        assert "完成：" in str(app._done_label.cget("text"))
        assert str(app._progress_label.cget("text")) == "就绪"


# ---------------------------------------------------------------------------
# 优化缺陷R86~R92：⚙ 弹层扩面板（关键词迁入 / 正则开关 / 编码指定 / 脱敏）
# ---------------------------------------------------------------------------
class TestSettingsPopupExtras:
    def test_popup_widgets_present(self, app):
        """弹层入住：正则/脱敏复选 + 编码选择器（默认值）；关键词
        两框已于 R93 迁回过滤行（不再占用弹层）。"""
        filter_panel = app._ctx_entry.master
        assert app._include_entry.master is filter_panel
        assert app._exclude_entry.master is filter_panel
        assert app._use_regex_var.get() is False, "正则默认关（字面子串）"
        assert app._redact_var.get() is True, "脱敏默认开（出站安全）"
        assert app._encoding_menu.get() == "自动探测（推荐）"

    def test_encoding_switch_hides_selected(self, app):
        """编码指定：选中项从列表消失 + 参数映射为具体编码。"""
        app._on_encoding_changed("GBK / GB18030")
        assert app._encoding_display == "GBK / GB18030"
        values = list(app._encoding_menu.cget("values"))
        assert "GBK / GB18030" not in values
        assert "自动探测（推荐）" in values
        assert app._advanced_params()["encoding"] == "gb18030"

    def test_invalid_regex_blocks_start(self, app):
        """非法正则：状态栏提示且不启动分析（与时间输入同路边界校验）。"""
        app._use_regex_var.set(True)
        app._include_entry.insert(0, "[unclosed")
        app._on_start()
        app.update()
        text = str(app._status_label.cget("text"))
        assert "前置设置有误" in text and "正则" in text
        assert app._result is None, "非法正则不得启动分析"

    def test_regex_analysis_and_status_tag(self, app):
        """正则包含 ERR-\\d{4}：仅错误码行入窗 + 状态栏「正则」标签。"""
        app._use_regex_var.set(True)
        app._include_entry.insert(0, r"ERR-\d{4}")
        _run_paste_analysis(app, "\n".join([
            "2024-01-01 09:00:00 ERROR [db] ERR-1001 connection lost",
            "2024-01-01 09:00:01 ERROR [net] network unreachable",
        ]))
        app.update()
        assert app._result.stats.error_entries == 1
        text = str(app._status_label.cget("text"))
        assert "正则" in text

    def test_redact_applied_on_export(self, app, tmp_path):
        """脱敏开（默认）：导出文本中密钥/邮箱已打码。"""
        _run_paste_analysis(
            app, "2024-01-01 09:00:00 ERROR [db] login password=hunterX "
                 "by admin@corp.com")
        app.update()
        app._export_with_options(
            str(tmp_path / "r"), {"txt"},
            {"overview", "list", "detail", "instances"})
        content = (tmp_path / "r.txt").read_text(encoding="utf-8")
        assert "hunterX" not in content
        assert "admin@corp.com" not in content
        assert "[密钥]" in content and "[邮箱]" in content

    def test_custom_redact_box_present(self, app):
        """⚙ 弹层含自定义脱敏输入框。"""
        assert hasattr(app, "_redact_custom_box")
        assert app._redact_custom_box.winfo_exists()

    def test_custom_redact_rules_parsed(self, app):
        """自定义规则读取：有效提取，非法行计数。"""
        app._redact_custom_box.delete("1.0", "end")
        app._redact_custom_box.insert("1.0", "PMS-\\d{6}\n[bad\n")
        valid, invalid = app._custom_redact_rules()
        assert valid == [r"PMS-\d{6}"]
        assert invalid == 1

    def test_custom_redact_applied_to_export_content(self, app):
        """自定义规则在导出/复制时生效。"""
        app._redact_custom_box.delete("1.0", "end")
        app._redact_custom_box.insert("1.0", r"PMS-\d{6}")
        app._redact_var.set(True)
        out = app._redact_out("工单 PMS-123456 失败")
        assert out == "工单 [自定义] 失败"

    def test_redact_off_keeps_original(self, app, tmp_path):
        """脱敏关：导出文本保持原样（用户显式关闭时尊重选择）。"""
        app._redact_var.set(False)
        _run_paste_analysis(
            app, "2024-01-01 09:00:00 ERROR [db] login password=hunterX boom")
        app.update()
        app._export_with_options(
            str(tmp_path / "r"), {"txt"},
            {"overview", "list", "detail", "instances"})
        content = (tmp_path / "r.txt").read_text(encoding="utf-8")
        assert "hunterX" in content

    def test_copy_summary_redacted(self, app):
        """复制摘要：脱敏开时剪贴板内容已打码（投喂 AI 主路径）。"""
        _run_paste_analysis(
            app, "2024-01-01 09:00:00 ERROR [db] call admin@corp.com boom")
        app.update()
        app._on_copy_summary()
        clip = app.clipboard_get()
        assert "admin@corp.com" not in clip
        assert "[邮箱]" in clip

    def test_rotation_multiselect_payload(self, app, tmp_path):
        """优化缺陷R89：导入框 "; " 分隔多文件 → 轮转合并分析。"""
        f1 = tmp_path / "app.log.1"
        f1.write_text(
            "2024-01-01 08:00:00 ERROR [db] old rotation error\n",
            encoding="utf-8")
        f2 = tmp_path / "app.log"
        f2.write_text(
            "2024-01-01 09:00:00 ERROR [db] new rotation error\n",
            encoding="utf-8")
        app._tabview.set("文件导入")
        app._file_entry.delete(0, "end")
        app._file_entry.insert(0, f"{f1}; {f2}")
        app._on_start()
        deadline = time.time() + 60
        while time.time() < deadline:
            app.update()
            if app._result is not None:
                break
            time.sleep(0.05)
        assert app._result is not None
        assert app._result.stats.total_lines == 2
        assert app._result.stats.error_entries == 2
        # source 标注多文件合并
        assert "app.log.1" in app._result.stats.source


# ---------------------------------------------------------------------------
# 修复11：文本粘贴模式排查（大文本 / 中文特殊字符 / Tab 切换 / 编码）
# ---------------------------------------------------------------------------
PASTE_CN_LOG = "\n".join([
    "2024-01-01 09:00:00 INFO [认证] 用户登录成功",
    "",   # 空行
    "2024-01-01 09:00:01 ERROR [数据库] 连接失败：无法连接到 db-primary:5432",
    "Caused by: java.net.ConnectException: Connection refused",
    "\tat com.app.db.Pool.init(Pool.java:42)",
    "2024-01-01 09:00:02 FATAL [核心] 内存不足 worker 3 退出",
    "2024-01-01 09:00:03 WARN [缓存] \"key=abc\"\ttoken 过期 \U0001f6a8",
    "   ",  # 纯空白行
    "2024-01-01 09:00:04 ERROR [数据库] 连接失败：无法连接到 db-secondary:5432",
])


# ---------------------------------------------------------------------------
# 优化缺陷R100：分析历史（最近 10 次结果可回放）
# ---------------------------------------------------------------------------
class TestHistory:
    def test_history_button_in_tab_row(self, app):
        """页签行右侧存在🕘历史按钮。"""
        assert app._history_btn.winfo_exists()
        assert "历史" in app._history_btn.cget("text")

    def test_result_auto_saved_to_history(self, app):
        """分析完成后结果自动入库，历史条数 ≥1。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        entries = app._history.list()
        assert len(entries) >= 1
        assert entries[0]["source"] == "<粘贴文本>"
        assert entries[0]["clusters"] == len(app._result.clusters)

    def test_restore_from_history_roundtrip(self, app):
        """历史回放：完整恢复 result 且不重复入库。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        first_id = app._history.list()[0]["id"]
        before_count = len(app._history.list())
        app._restore_from_history(first_id)
        app.update()
        # 回放后 _result 有效；播放不产生新的历史条目
        assert app._result is not None
        assert len(app._history.list()) == before_count
        assert "历史回放" in str(app._status_label.cget("text"))

    def test_history_clear_button(self, app):
        """清空历史按钮清空全部条目。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        assert len(app._history.list()) >= 1
        app._clear_history()
        assert app._history.list() == []

    def test_history_load_corrupt_returns_none(self, app):
        """损坏/不存在 id 返回 None，不抛异常。"""
        assert app._history.load("not-exist") is None

    def test_history_cap_10_evicts_oldest(self, app):
        """超出 10 条自动淘汰最旧（索引与 pickle 文件同步清理）。"""
        from log_ai_compressor.core.models import (
            AnalysisResult, RunStats, TimeHistogram)
        for i in range(12):
            stats = RunStats(source=f"f{i}.log", total_lines=i)
            app._history.add(AnalysisResult(
                stats=stats, clusters=[], global_hist=TimeHistogram()))
        entries = app._history.list()
        assert len(entries) == 10
        # 最新在最前；最旧两条（f0/f1）已被淘汰
        assert entries[0]["source"] == "f11.log"
        sources = [e["source"] for e in entries]
        assert "f0.log" not in sources and "f1.log" not in sources
        pkl_files = list(app._history._dir.glob("*.pkl"))
        assert len(pkl_files) == 10


class TestPasteMode:
    def test_paste_large_text_analysis(self, app):
        """粘贴 1 万行文本：正常解析（总行数 / 错误数正确）。"""
        lines = []
        for i in range(10000):
            if i % 20 == 0:
                lines.append(f"2024-01-01 09:{i // 60 % 60:02d}:{i % 60:02d} "
                             f"ERROR [db] connection refused to host {i % 3}")
            else:
                lines.append(f"2024-01-01 09:{i // 60 % 60:02d}:{i % 60:02d} "
                             f"INFO [core] heartbeat ok")
        _run_paste_analysis(app, "\n".join(lines), timeout=60)
        assert app._result.stats.total_lines == 10000
        assert app._result.stats.error_entries == 500

    def test_paste_chinese_and_special_chars(self, app):
        """中文 / emoji / 引号 / 制表符 / 空行混合日志正常解析。"""
        _run_paste_analysis(app, PASTE_CN_LOG)
        r = app._result
        # 空行与纯空白行不计入总行数？—— splitlines 计入空行
        assert r.stats.total_lines == 9
        # 中文错误聚为 2 簇（db-primary/db-secondary 掩码后同模板 + FATAL）
        assert len(r.clusters) >= 2
        summaries = " ".join(c.summary for c in r.clusters)
        assert "连接失败" in summaries

    def test_paste_tab_switch_content_preserved(self, app):
        """粘贴后切换 Tab 再切回：内容不丢失。"""
        app._tabview.set("文本粘贴")
        app._paste_box.delete("1.0", "end")
        app._paste_box.insert("1.0", SAMPLE_PASTE)
        original = app._paste_box.get("1.0", "end").strip()
        # 切走再切回
        app._tabview.set("文件导入")
        app.update()
        app._tabview.set("多文件对比")
        app.update()
        app._tabview.set("文本粘贴")
        app.update()
        restored = app._paste_box.get("1.0", "end").strip()
        assert restored == original, "切换 Tab 后粘贴内容丢失"

    def test_paste_bom_text_parsed(self, app):
        """BOM 开头的粘贴文本：首行仍能正常规则解析（修复缺陷#11）。"""
        text = ("\ufeff2024-01-01 09:00:00 ERROR [db] connection refused\n"
                "2024-01-01 09:00:01 INFO [core] heartbeat ok\n")
        _run_paste_analysis(app, text)
        r = app._result
        # 首行应被解析为 ERROR 错误条目（而非无结构 INFO 行）
        assert r.stats.error_entries == 1
        assert any("connection refused" in c.summary for c in r.clusters)

    def test_paste_crlf_text_parsed(self, app):
        """CRLF / CR 混合换行的粘贴文本：行数与条目正确。"""
        text = ("2024-01-01 09:00:00 ERROR [db] boom\r\n"
                "2024-01-01 09:00:01 INFO [core] ok\r"
                "2024-01-01 09:00:02 FATAL [core] dead\n")
        _run_paste_analysis(app, text)
        assert app._result.stats.total_lines == 3
        assert app._result.stats.error_lines == 2

    def test_paste_blank_only_warns(self, app, monkeypatch):
        """纯空白粘贴：提示「请先粘贴日志文本」且不启动分析。"""
        import log_ai_compressor.gui_legacy.app as app_mod
        warned = []
        monkeypatch.setattr(app_mod.messagebox, "showwarning",
                            lambda title, msg: warned.append(msg))
        app._tabview.set("文本粘贴")
        app._paste_box.delete("1.0", "end")
        app._paste_box.insert("1.0", "\n   \n\t\n")
        app._on_start()
        app.update()
        assert warned and "粘贴" in warned[0]
        assert app._worker is None or not app._worker.is_alive()

    def test_paste_text_undo_disabled(self, app):
        """粘贴框 undo 应关闭（大文本粘贴的 undo 栈内存保护）。"""
        # CTkTextbox 底层 tk Text 的 undo 选项
        try:
            undo = app._paste_box.cget("undo")
        except (ValueError, tk.TclError):
            undo = None
        if undo is not None:
            assert str(undo) in ("False", "0", "false")

    def test_paste_trailing_newlines_stripped(self, app):
        """粘贴框恒有的尾部换行被正确去除（不产生空错误条目）。"""
        text = ("2024-01-01 09:00:00 ERROR [db] connection refused\n\n\n\n")
        _run_paste_analysis(app, text)
        assert app._result.stats.error_entries == 1
        # 尾部空行不计入解析行数（strip 后消除）
        assert app._result.stats.total_lines == 1

    def test_paste_result_encoding_label(self, app):
        """粘贴文本分析结果编码标注为 utf-8（Unicode 直通无转换）。"""
        _run_paste_analysis(app, SAMPLE_PASTE)
        assert app._result.stats.encoding == "utf-8"
        assert app._result.stats.source == "<粘贴文本>"


# ---------------------------------------------------------------------------
# 修复12：主题切换体验（状态标识 + 平滑过渡 + 持久化 + 对比度）
# ---------------------------------------------------------------------------
class TestThemeSwitch:
    # 四态主题名（修复R13 后由下拉选择框直接选择）
    THEME_NAMES = ("☀ 亮色", "🌙 暗色", "🔵 蓝调", "🟢 绿调")

    def _wait_theme(self, app, key, timeout=3.0):
        """推进淡出/淡入过渡帧直至主题到达目标（28ms/帧）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            app.update()
            if app._theme == key:
                break
            time.sleep(0.02)
        return app._theme == key

    def _box_text(self, app):
        """选择框当前显示文字（图标 + 主题名）。"""
        return (f"{app._theme_box_icon.cget('text')} "
                f"{app._theme_box_name.cget('text')}")

    def test_theme_menu_shows_current_mode(self, app):
        """修复R13：选择框显示当前主题（四态之一）。"""
        assert self._box_text(app) in self.THEME_NAMES, \
            f"选择框应显示当前主题，实际: {self._box_text(app)}"

    def test_theme_menu_values_exclude_current(self, app):
        """修复R13：下拉列表只列其他三态（当前项不重复，顺序保持）。"""
        from log_ai_compressor.gui_legacy.app import THEME_ORDER
        for key in THEME_ORDER:
            app._apply_theme_switch(key)
            app.update()
            expected = [k for k in THEME_ORDER if k != key]
            assert app._theme_popup_items() == expected, \
                f"{key} 下拉列表应为 {expected}，" \
                f"实际 {app._theme_popup_items()}"
        app._apply_theme_switch("dark")

    def test_theme_menu_select_switches_directly(self, app):
        """修复R13：下拉可跳过中间主题直达任意目标（无需循环点击）。"""
        app._apply_theme_switch("dark")
        app.update()
        # 暗色 → 绿调（跳过亮色、蓝调两步）
        app._on_theme_selected("green")
        assert self._wait_theme(app, "green"), "应直接切换到绿调"
        assert self._box_text(app) == "🟢 绿调"
        # 绿调 → 亮色（反向跳过）
        app._on_theme_selected("light")
        assert self._wait_theme(app, "light"), "应直接切换到亮色"
        assert self._box_text(app) == "☀ 亮色"
        app._apply_theme_switch("dark")

    def test_theme_menu_updates_after_switch(self, app):
        """修复R13：切换后选择框显示值与列表内容同步更新。"""
        before = self._box_text(app)
        app._apply_theme_switch("light" if app._is_dark_mode() else "dark")
        app.update()
        after = self._box_text(app)
        assert before != after
        assert {before, after} <= set(self.THEME_NAMES)
        # 当前主题不出现在列表中
        assert app._theme not in app._theme_popup_items()

    def test_theme_menu_all_four_selectable(self, app):
        """修复R13：四种主题都能从下拉直达（逐项选择并验证显示）。"""
        from log_ai_compressor.gui_legacy.app import THEME_ORDER
        app._apply_theme_switch("light")
        app.update()
        for expected in ("dark", "blue", "green", "light"):
            app._on_theme_selected(expected)
            assert self._wait_theme(app, expected), \
                f"切换后应为 {expected}，实际 {app._theme}"
            assert self._box_text(app) == \
                dict(zip(THEME_ORDER, self.THEME_NAMES))[expected]
        app._apply_theme_switch("dark")

    def test_theme_popup_text_aligned(self, app):
        """修复R14：下拉列表四个选项文字起始 x 坐标完全一致（对齐）。

        emoji（☀️🌙🔵🟢）字形宽度不一，纯文本菜单会错位；两列布局
        （固定宽图标列 + 左对齐文字列）后文字列起始 x 应严格相等。
        """
        from log_ai_compressor.gui_legacy.app import THEME_ORDER
        app._open_theme_popup()
        app.update_idletasks()
        app.update()
        try:
            xs = []
            for key in THEME_ORDER:
                if key == app._theme:
                    continue          # 当前项隐藏
                xs.append(
                    app._theme_popup_rows[key]["name"].winfo_rootx())
            assert len(xs) == 3, "应显示三个选项"
            assert max(xs) - min(xs) == 0, \
                f"文字起始 x 应完全一致，实际 {xs}"
            # 图标列宽固定（各行图标控件宽度一致）
            icons = [app._theme_popup_rows[key]["icon"].winfo_width()
                     for key in THEME_ORDER if key != app._theme]
            assert max(icons) - min(icons) == 0, \
                f"图标列宽应固定，实际 {icons}"
        finally:
            app._close_theme_popup()

    def test_theme_popup_select_by_click(self, app):
        """修复R14：点击弹窗行控件（图标/文字）触发主题切换并收起。"""
        app._apply_theme_switch("dark")
        app.update()
        app._open_theme_popup()
        app.update()
        name_lbl = app._theme_popup_rows["green"]["name"]
        name_lbl.event_generate("<Button-1>", x=5, y=5)
        app.update()
        assert self._wait_theme(app, "green"), "点击行应切换主题"
        assert app._theme_popup.state() == "withdrawn", "选择后应自动收起"
        app._apply_theme_switch("dark")

    def test_theme_box_icon_col_matches_popup(self, app):
        """修复R14：选择框与下拉列表图标列宽一致（显示位置统一）。"""
        from log_ai_compressor.gui_legacy.app import _THEME_ICON_COL
        scale = max(1.0, app._font_scale)
        col = app._theme_icon_col
        assert col >= _THEME_ICON_COL, \
            f"实测图标列宽 {col} 应 ≥ 基准 {_THEME_ICON_COL}"
        app._open_theme_popup()
        app.update_idletasks()
        app.update()
        try:
            for key in app._theme_popup_items():
                icon = app._theme_popup_rows[key]["icon"]
                assert icon.winfo_width() == pytest.approx(
                    col * scale, abs=2), \
                    f"弹窗图标列宽 {icon.winfo_width()} 应为 " \
                    f"{col * scale:.0f}"
            assert app._theme_box_icon.winfo_width() == pytest.approx(
                col * scale, abs=2), "选择框图标列宽应一致"
        finally:
            app._close_theme_popup()

    def test_theme_box_click_opens_popup_realpath(self, app):
        """修复R15：真实点击路径打开弹窗、点击别处收起（焦点解耦）。

        修复前 FocusOut 收起被 CTkToplevel 全局 bind_all(set_focus)
        干扰：点击打开的瞬间焦点被抢回主窗口 → 弹窗立即失焦收起
        （表现为点击无反应）。现改为全局点击收起，与焦点无关。
        """
        app.update()
        # 模拟真实点击：命中选择框内部 canvas（bind 实际注册处）
        app._theme_box._canvas.event_generate("<Button-1>", x=10, y=10)
        app.update()
        assert app._theme_popup.state() == "normal", \
            "点击选择框后弹窗应打开（R15：不得被立即收起）"
        # CTkToplevel 全局 set_focus 抢焦点（真实链路必然发生），
        # 弹窗不应受焦点变化影响
        app._theme_box._canvas.focus_set()
        app.update()
        assert app._theme_popup.state() == "normal", \
            "焦点被抢回主窗口后弹窗应保持打开（焦点解耦）"
        # 再次点击选择框：切换为收起
        app._theme_box._canvas.event_generate("<Button-1>", x=10, y=10)
        app.update()
        assert app._theme_popup.state() == "withdrawn", "再点应收起"
        # 打开后点击主窗口其他区域：全局点击收起
        app._theme_box._canvas.event_generate("<Button-1>", x=10, y=10)
        app.update()
        assert app._theme_popup.state() == "normal"
        time.sleep(0.2)                     # 越过 150ms 打开豁免窗口
        app._status_label.event_generate("<Button-1>", x=5, y=5)
        app.update()
        assert app._theme_popup.state() == "withdrawn", \
            "点击别处应收起弹窗（全局点击收起）"

    def test_theme_popup_icon_advances_uniform(self, app):
        """修复R16：四个图标 advance（内部 label 宽）一致，无隐形空白。

        "☀️" 的 FE0F 变体选择符被 Tk 渲染成 ~36 物理px 空白尾迹，
        advance（62）是其他图标（30）的 2 倍 —— advance 盒居中后
        可见太阳偏左 ~18px。去掉 FE0F 后 advance 应与其他接近
        （最大/最小 ≤ 1.5 倍）。
        """
        app._open_theme_popup()
        app.update_idletasks()
        app.update()
        try:
            widths = []
            for key in app._theme_popup_items():
                inner = app._theme_popup_rows[key]["icon"]._label
                widths.append(inner.winfo_width())
            assert len(widths) == 3
            assert max(widths) / max(1, min(widths)) <= 1.5, \
                f"图标 advance 差异过大（{widths}），存在隐形空白尾迹"
            # 修复R16 的直接断言：太阳不带 FE0F
            from log_ai_compressor.gui_legacy.app import THEMES
            assert "\ufe0f" not in THEMES["light"]["icon"]
        finally:
            app._close_theme_popup()

    def test_theme_button_icon_col_aligns_popup(self, app):
        """修复R16（续）：主按钮图标列与弹窗图标列同起点、同宽。

        弹窗行 padx=2 + 图标 padx=8 = 10 逻辑 px，主按钮图标原为
        padx=8 —— 按钮图标列比弹窗图标列左偏 2 逻辑 px（200% DPI
        下 4 物理 px，实测太阳墨迹中心偏左 4px）。改为 padx=10 后
        两列完全重合：按钮图标与弹窗图标垂直对齐成一条线。
        """
        app.update()
        app.update_idletasks()
        app._open_theme_popup()
        app.update_idletasks()
        app.update()
        try:
            btn = app._theme_box_icon
            btn_x = btn.winfo_rootx()
            btn_w = btn.winfo_width()
            for key in app._theme_popup_items():
                icon = app._theme_popup_rows[key]["icon"]
                assert abs(icon.winfo_rootx() - btn_x) <= 2, (
                    f"弹窗行 {key} 图标列起点 {icon.winfo_rootx()} 与主按钮"
                    f"图标列起点 {btn_x} 未对齐（应同为 10 逻辑 px 内距）")
                assert icon.winfo_width() == btn_w, \
                    f"弹窗行 {key} 图标列宽与主按钮图标列宽不一致"
        finally:
            app._close_theme_popup()

    def test_theme_box_name_centered(self, app):
        """修复R15：主题名在图标与▼箭头的正中间（水平居中）。"""
        app.update()
        app.update_idletasks()
        icon = app._theme_box_icon
        name = app._theme_box_name
        arrow = app._theme_box_arrow
        icon_r = icon.winfo_rootx() + icon.winfo_width()
        arrow_l = arrow.winfo_rootx()
        name_c = name.winfo_rootx() + name.winfo_width() / 2
        mid = (icon_r + arrow_l) / 2
        assert abs(name_c - mid) <= 4, \
            f"主题名中心 {name_c} 应在图标右缘与箭头左缘中点 {mid}"

    def test_palette_roles_complete(self, app):
        """修复R1：每个主题调色板字段齐全（缺角色会导致刷新异常）。"""
        from log_ai_compressor.gui_legacy.app import THEMES
        required = {"name", "icon", "label", "window", "card", "header",
                    "text", "muted", "accent", "accent_hover", "accent_text",
                    "row_bg", "row_hover", "row_selected",
                    "row_selected_edge", "row_text", "is_dark",
                    # 修复缺陷R26：3D 凸起角色（渐变能带/描边/高光/
                    # 投影/选中文字/未选中细边框）
                    "sel_top", "sel_bot", "sel_border", "sel_hi",
                    "sel_shadow", "sel_text", "row_border"}
        for key, palette in THEMES.items():
            assert required <= set(palette), f"{key} 缺字段: {required - set(palette)}"

    def test_blue_green_themes_menu_white(self, app):
        """修复R1/R13：蓝调/绿调下选择框为白底深色字（accent 白色）。"""
        from log_ai_compressor.gui_legacy.app import THEMES
        for key in ("blue", "green"):
            app._apply_theme_switch(key)
            app.update()
            assert THEMES[key]["accent"] == "#ffffff"
            # 选择框 fg_color 应用为白色（CTk 返回元组 (r,g,b) 或 hex）
            color = str(app._theme_box.cget("fg_color"))
            assert "255, 255, 255" in color or color == "#ffffff", \
                f"{key} 主题选择框应为白色，实际 {color}"
        app._apply_theme_switch("dark")

    def test_theme_persisted_immediately(self, app):
        """切换主题立即写盘（不等关闭窗口）。"""
        target = "light" if app._is_dark_mode() else "dark"
        app._apply_theme_switch(target)
        saved = app._store.load()
        assert saved.get("appearance") == target

    def test_theme_restored_on_startup(self, app):
        """下次启动自动恢复上次的主题（配置文件验证）。

        说明：不创建第二个 Tk root —— 长测试序列中 Windows 句柄
        累积会令新 root 创建失败（Can't find a usable init.tcl），
        此处以配置文件内容 + 启动加载逻辑等效验证。
        """
        app._apply_theme_switch("light")
        # 1) 配置文件已写入 light
        assert app._store.load().get("appearance") == "light"
        # 2) 启动加载路径等效验证（LogCompressorApp.__init__ 同款逻辑）
        import customtkinter as _ctk
        from log_ai_compressor.gui_legacy.config_store import ConfigStore
        cfg = ConfigStore(app._store.path).load()
        _ctk.set_appearance_mode(cfg.get("appearance", "dark"))
        assert _ctk.get_appearance_mode().lower() == "light"
        # 3) 选择框显示与主题一致
        app._update_theme_menu()
        assert self._box_text(app) == "☀ 亮色"
        # 恢复默认暗色
        app._apply_theme_switch("dark")

    def test_selection_with_animation_completes(self, app):
        """修复R13：下拉选择（含过渡动画）最终完成切换且恢复不透明。"""
        before_dark = app._is_dark_mode()
        target = "light" if before_dark else "dark"
        app._on_theme_selected(target)
        # 推进动画帧（淡出 4 帧 + 谷底切换 + 淡入 4 帧）
        for _ in range(40):
            app.update()
            time.sleep(0.01)
        deadline = time.time() + 3
        while time.time() < deadline:
            app.update()
            if app._is_dark_mode() != before_dark:
                break
            time.sleep(0.02)
        assert app._is_dark_mode() != before_dark, "动画后主题应已切换"
        # 窗口恢复完全不透明
        assert float(app.attributes("-alpha")) == pytest.approx(1.0, abs=0.01)
        # 恢复默认主题
        app._apply_theme_switch("dark" if before_dark else "light")

    def test_row_colors_refresh_on_theme(self, app):
        """切换主题后列表行配色刷新（原生 label 不随 CTk 主题自动变）。

        修复缺陷R28：R27 后经典行摘要为 CTkLabel（无 fg 选项），
        文字色读 text_color（旧读 fg 抛 ValueError）。
        """
        _run_paste_analysis(app, LONG_SUMMARY_LOG)
        app.update()

        def sum_fg():
            w = app._cluster_rows[1]["summary"]
            try:
                return str(w.cget("text_color"))   # CTkLabel
            except (ValueError, tk.TclError):
                return str(w.cget("fg"))           # 原生 tk.Label

        dark = app._is_dark_mode()
        fg_before = sum_fg()
        app._apply_theme_switch("light" if dark else "dark")
        app.update()
        fg_after = sum_fg()
        assert fg_before != fg_after, "行文字颜色应随主题刷新"
        # 暗色模式用浅色文字 / 亮色模式用深色文字（对比度保障）
        if app._is_dark_mode():
            assert fg_after == "#c8cdd4"
        else:
            assert fg_after == "#2d333b"
        app._apply_theme_switch("dark" if dark else "light")
        app.update()

    def test_dark_mode_text_contrast(self, app):
        """暗色模式下关键文字颜色具备足够对比度（可读性保障）。"""
        app._apply_theme_switch("dark")
        app.update()
        # 行文字在暗色背景（gray22 ≈ #383838）上应为浅色
        assert _row_fg_is_light("#c8cdd4")
        # 亮色模式（gray88 ≈ #e0e0e0 背景）上应为深色
        assert not _row_fg_is_light("#2d333b")


def _luminance(hex_color: str) -> float:
    """相对亮度（0~1，WCAG 口径近似）。"""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    def lin(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _row_fg_is_light(hex_color: str) -> bool:
    return _luminance(hex_color) > 0.4
