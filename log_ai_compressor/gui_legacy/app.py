# -*- coding: utf-8 -*-
"""GUI 主界面（CustomTkinter）：单窗口三 Tab 布局。

交互结构
--------
- 顶部 Tab：「文件导入」（主力）/「文本粘贴」（快捷）/「多文件对比」；
- 配置区：级别勾选（默认 ERROR+FAIL）、解析规则；
- 进度区：实时行数 / 速率 / 错误种类数 + 进度条 + 取消按钮；
- 结果区：左侧错误分类列表（图标+次数+摘要+优先级），右侧完整
  堆栈与上下文详情（关键字自动高亮、业务栈帧高亮）；
- 图表窗口：趋势折线 / 级别占比 / 模块分布，点击联动错误列表。

线程模型：管线在后台线程运行，进度经 queue 传递，UI 更新全部在
主线程 after 轮询中完成（Tk 线程安全约束）。
"""
from __future__ import annotations

import queue
import re
import threading
import time
import dataclasses
import os
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox
from typing import Dict, List, Optional

import customtkinter as ctk

from log_ai_compressor import __version__
from log_ai_compressor.constants import (
    DEFAULT_CONTEXT_LINES,
    DEFAULT_SELECTED_LEVELS,
    HUMAN_NAME,
)
from log_ai_compressor.core.analysis import cooccurring_clusters, simplify_stack
from log_ai_compressor.core.clustering import extract_variable_distribution
from log_ai_compressor.core.comparator import CompareResult, compare_files
from log_ai_compressor.core.models import (
    AnalysisResult,
    ClusterInstance,
    ErrorCluster,
    format_timestamp,
)
from log_ai_compressor.core.pipeline import (
    analyze_file,
    analyze_files,
    analyze_text,
)
from log_ai_compressor.core.redact import redact_text
from log_ai_compressor.rules.engine import detect_rule, stratified_sample
from log_ai_compressor.export.reporters import (
    brief_summary,
    compare_to_markdown,
    to_html,
    to_json,
    to_markdown,
    to_text,
    token_report_line,
    token_status_text,
)
from log_ai_compressor.gui_legacy.config_store import ConfigStore, HistoryStore

# 修复缺陷#9：matplotlib 导入约 0.4s，延迟到首次点击「统计图表」时
# 才加载（charts 模块不再在 GUI 启动路径上被导入）。

# 可选拖拽支持（tkinterdnd2 未安装时自动退化为点击选择）
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _HAS_DND = True
except ImportError:
    _HAS_DND = False


def _make_app_base():
    """应用基类：有 tkinterdnd2 时混入拖拽能力，否则退化为纯 CTk。"""
    if _HAS_DND:

        class DnDApp(ctk.CTk, TkinterDnD.DnDWrapper):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.TkdndVersion = TkinterDnD._require(self)

        return DnDApp

    class PlainApp(ctk.CTk):
        pass

    return PlainApp

# 修复缺陷R19：FATAL 复选框删除（R18 起关键词推断不再产生
# FATAL —— 编译命令行 -Wfatal-errors 误判源已消除）。
# 修复缺陷R40：FATAL 级别本身同步删除 —— 显式 [FATAL]/CRITICAL/
# gcc fatal error 经 LEVEL_ALIASES 归一为 ERROR（最高严重级，
# 由 ERROR 复选框统一控制）。剩余五级别自然前移补齐。
LEVEL_CHECKS = ("ERROR", "FAIL", "WARN", "INFO", "DEBUG")
# 优化缺陷R71：解析规则三件套（自动识别默认 + 中文显示名映射）
RULE_KEYS = ("auto", "generic", "embedded", "jenkins")
RULE_DISPLAY = {
    "auto": "自动识别（推荐）",
    "generic": "通用 generic",
    "embedded": "嵌入式 embedded",
    "jenkins": "CI构建 jenkins",
}
_RULE_BY_DISPLAY = {v: k for k, v in RULE_DISPLAY.items()}
# 优化缺陷R79：智能分析模式三件套（完整默认 + 中文显示名映射，
# 与解析规则选择器同款交互：选中项从下拉消失 + ⓘ 悬停说明）
ANALYZE_KEYS = ("full", "deep", "fast")
ANALYZE_DISPLAY = {
    "full": "完整分析（推荐）",
    "deep": "深度扫描",
    "fast": "快速聚类",
}
_ANALYZE_BY_DISPLAY = {v: k for k, v in ANALYZE_DISPLAY.items()}
ANALYZE_DESCRIPTIONS = {
    "full": "完整分析（推荐）：聚类 + 优先级 P0~P4 + 根因▲ + "
            "爆发●(3σ)/周期◔/新型◆/罕见○ + 堆栈降噪。\n"
            "证据确凿才标记（默认阈值），日常排查用它",
    "deep": "深度扫描：与完整分析同样的五件套，但报案门槛降低——"
            "爆发 3σ→2σ、罕见判定 总数≥10→≥5、根因关键词 ≥3→≥2。\n"
            "疑难日志（完整分析啥都没标出）时用它；代价是误报增多",
    "fast": "快速聚类：只做聚类压缩 + 级别排序，跳过全部智能标记"
            "（无 ▲●○◆ 与优先级评分）。\n"
            "超大文件卡顿时用；详情面板智能分析区显示「未执行」",
}
_ANOMALY_LEGEND = ("标记图例：▲ 根因（紫） · ● 集中爆发 · ◔ 周期发作 · "
                   "◆ 新型错误 · ○ 罕见异常")
# 优化缺陷R84：相似度阈值三档（标准默认 + 中文显示名映射，
# 与智能分析/解析规则同款交互：选中项从下拉消失 + ⓘ 悬停说明）
SIMILARITY_KEYS = ("standard", "strict", "lenient")
SIMILARITY_DISPLAY = {
    "standard": "标准（推荐）",
    "strict": "严格",
    "lenient": "宽松",
}
_SIMILARITY_BY_DISPLAY = {v: k for k, v in SIMILARITY_DISPLAY.items()}
SIMILARITY_DESCRIPTIONS = {
    "standard": "标准（推荐）：相似度 ≥0.85 才并簇，准确率优先，日常用它",
    "strict": "严格：≥0.95 才并簇 —— 只看几乎一样的；不同错误被合并"
              "成一簇时用它（簇变多、更准）",
    "lenient": "宽松：≥0.70 即并簇 —— 相似就合并；同一错误被拆成多簇"
               "时用它（簇变少、压缩更狠）",
}
# 优化缺陷R85：行数上限采样四档（全部默认 + 中文显示名映射，
# 与相似度/智能分析同款交互：选中项从下拉消失 + ⓘ 悬停说明）
MAXLINES_KEYS = ("all", "100k", "500k", "1m")
MAXLINES_DISPLAY = {
    "all": "全部（推荐）",
    "100k": "前 10 万行",
    "500k": "前 50 万行",
    "1m": "前 100 万行",
}
_MAXLINES_BY_DISPLAY = {v: k for k, v in MAXLINES_DISPLAY.items()}
MAXLINES_VALUES = {"all": None, "100k": 100000, "500k": 500000,
                   "1m": 1000000}
MAXLINES_DESCRIPTIONS = {
    "all": "全部（推荐）：分析整个文件，结果最完整",
    "100k": "前 10 万行：超大文件快速体检，只看开头错误分布；"
            "达上限时状态栏标记「已达行数上限」",
    "500k": "前 50 万行：大文件折中 —— 覆盖更全但仍可能漏掉尾部错误",
    "1m": "前 100 万行：近全量快速预览",
}
# 优化缺陷R88：编码指定（⚙ 弹层；None=自动探测兜底，老系统中文
# 日志乱码时手动指定覆盖探测结果）
ENCODING_DISPLAY = ("自动探测（推荐）", "UTF-8", "GBK / GB18030", "UTF-16")
_ENCODING_VALUES = {
    "自动探测（推荐）": None,
    "UTF-8": "utf-8",
    "GBK / GB18030": "gb18030",
    "UTF-16": "utf-16",
}
ANOMALY_DESCRIPTIONS = {
    "burst": "集中爆发：错误峰值超过全局 3σ 或簇自身基线（中位数+3×MAD）",
    "periodic": "周期发作：实例间隔变异系数 ≤0.10（定时任务/心跳失败指纹）",
    "novel": "新型错误：仅 1 次且与既有簇模板 Jaccard <0.5（从没见过）",
    "rare": "罕见异常：错误总数可观但该类仅出现 1 次（老错误的偶发尾巴）",
}
_ANOMALY_LABELS = {"burst": "集中爆发", "rare": "罕见异常",
                   "periodic": "周期发作", "novel": "新型错误"}

# 优化：五个级别复选框旁的 ⓘ 悬停说明（每个级别对应自己的解释）
_LEVEL_HELP = {
    "ERROR": "ERROR：错误，程序运行中出现的异常，"
             "可能导致功能异常但程序仍可继续运行",
    "FAIL": "FAIL：失败，操作或测试未成功完成的结果",
    "WARN": "WARN：警告，可能存在问题但不影响程序正常运行，"
            "需要关注",
    "INFO": "INFO：信息，程序正常运行时的一般性记录",
    "DEBUG": "DEBUG：调试，开发调试用的详细信息，"
             "通常生产环境不显示",
}

# 修复缺陷R10：字体大小档位（配置区「字体大小」选择器，持久化）
# - 「中」为基准档（主列表头部 22 加粗 / 摘要 18；全屏 28/24/20）
# - 其余档位按系数整体缩放（经典 / 虚拟 / 全屏 / 对比同步生效）
FONT_SIZE_OPTIONS = ("小", "中", "大", "特大")
FONT_SIZE_SCALE = {"小": 0.85, "中": 1.0, "大": 1.15, "特大": 1.3}

# 错误行智能图标：▲ 根因 / ● 爆发 / ◔ 周期 / ◆ 新型 / ○ 稀有 / • 普通
# 修复缺陷R40：◆ 致命图标随 FATAL 级别删除移除；五级别五色
# + 根因紫（与级别色区分，一眼区分严重程度）
# 优化缺陷R75：新增 ◔ 周期发作 / ◆ 新型错误（异常检测强化产出）
_CLUSTER_ICON = {"root": "\u25b2", "burst": "\u25cf",
                 "periodic": "\u25d4", "novel": "\u25c6",
                 "rare": "\u25cb", "normal": "\u2022"}

# 优化缺陷R113：搜索框布尔组合（and/or/not，词边界识别防误切
# error/android 等内含词；无操作符时退化为普通子串匹配）
_BOOL_OP_RE = re.compile(r"\b(?:and|or|not)\b", re.IGNORECASE)


def _kw_match(hay: str, expr: str) -> bool:
    """关键字匹配：hay/expr 均已小写；含 and/or/not 时按布尔
    表达式求值（or 分句 → and 合项 → not 前缀取反），否则子串。"""
    if not expr:
        return True
    if not _BOOL_OP_RE.search(expr):
        return expr in hay
    for clause in re.split(r"\bor\b", expr, flags=re.IGNORECASE):
        ok = True
        for term in re.split(r"\band\b", clause, flags=re.IGNORECASE):
            term = term.strip()
            if not term:
                continue
            neg, m = False, re.match(r"not\b\s*", term, re.IGNORECASE)
            if m:
                neg, term = True, term[m.end():].strip()
            hit = bool(term) and term in hay
            if neg:
                hit = not hit
            if not hit:
                ok = False
                break
        if ok:
            return True
    return False


_LEVEL_COLORS = {
    "ERROR": "#ff5252",   # 红
    "FAIL": "#ff7a45",    # 橙红
    "WARN": "#ffb74d",    # 橙黄
    "INFO": "#5ac8fa",    # 天蓝
    "DEBUG": "#9ca3af",   # 灰
}
_ROOT_COLOR = "#c084fc"        # 根因紫
# 选中蓝底上的调亮版（级别文字在蓝色背景上的可读性）
_LEVEL_COLORS_SEL = {
    "ERROR": "#ff8a80",
    "FAIL": "#ffab91",
    "WARN": "#ffd54f",
    "INFO": "#80d8ff",
    "DEBUG": "#e5e7eb",
}
_ROOT_COLOR_SEL = "#d8b4fe"

# ---------------------------------------------------------------------------
# 修复缺陷R1：四态主题体系（亮色 → 暗色 → 蓝调 → 绿调 循环）
# ---------------------------------------------------------------------------
# 主色调 #3B82F6（舒适蓝）；蓝调/绿调为柔和浅色渐变近似（Tk 无原生渐变，
# 以「窗口层浅色 + 卡片层更浅」双层色营造层次）。
# - window: 根/边栏背景   card: 卡片/输入区背景   header: 标题栏
# - accent: 主按钮底色 / accent_text: 主按钮文字 / accent_hover: 悬停
# - muted: 次要文字（提示/说明）
# - row_*: 错误列表行 背景/悬停/选中/文字
# 修复缺陷R12：splitter/splitter_grip —— 错误列表与详情面板间的
# 可拖动分隔条底色与握点色（半透明观感的灰系，四态各自协调）
THEMES: Dict[str, Dict[str, str]] = {
    "light": {
        # 修复缺陷R16：icon 不带 FE0F —— Tk 会把变体选择符渲染成
        # 36 物理px 的空白尾迹，"☀️" 的 advance（62）远大于其他图标
        # （30），advance 盒居中后可见太阳被推到左半边（视觉偏左 18px）。
        # 去掉 FE0F 字形不变（同高 32px），advance 26 与其他一致，居中对齐
        "name": "☀ 亮色", "icon": "☀", "label": "亮色",
        "window": "#eef1f6", "card": "#ffffff",
        "header": "#ffffff", "text": "#1f2937", "muted": "#6b7280",
        "accent": "#3B82F6", "accent_hover": "#2563EB", "accent_text": "#ffffff",
        "row_bg": "#ffffff", "row_hover": "#eff6ff", "row_selected": "#bfdbfe",
        "row_selected_edge": "#3b82f6",
        # 修复缺陷R26：选中行 3D 凸起角色（渐变能带/描边/高光/投影/
        # 选中文字/未选中细边框）—— 方案A 画布绘制风格
        "sel_top": "#60a5fa", "sel_bot": "#3b82f6",
        "sel_border": "#93c5fd", "sel_hi": "#dbeafe",
        "sel_shadow": "#b9c0cb", "sel_text": "#ffffff",
        "row_border": "#d5dce6",
        "row_text": "#2d333b", "is_dark": "0",
        "splitter": "#c3ccd9", "splitter_grip": "#8a97a8",
    },
    "dark": {
        "name": "🌙 暗色", "icon": "🌙", "label": "暗色",
        "window": "#111827", "card": "#1c2433",
        "header": "#161e2d", "text": "#e5e7eb", "muted": "#94a3b8",
        "accent": "#3B82F6", "accent_hover": "#60a5fa", "accent_text": "#ffffff",
        "row_bg": "#1c2433", "row_hover": "#2a3547", "row_selected": "#1d4ed8",
        "row_selected_edge": "#60a5fa",
        # 修复缺陷R26：选中行 3D 凸起角色（暗色：投影近黑）
        "sel_top": "#3b82f6", "sel_bot": "#1d4ed8",
        "sel_border": "#60a5fa", "sel_hi": "#93c5fd",
        "sel_shadow": "#0a0f1a", "sel_text": "#ffffff",
        "row_border": "#2e3a4f",
        "row_text": "#c8cdd4", "is_dark": "1",
        "splitter": "#3a485e", "splitter_grip": "#64758f",
    },
    "blue": {
        "name": "🔵 蓝调", "icon": "🔵", "label": "蓝调",
        "window": "#cfe3fa", "card": "#e8f1fd",
        "header": "#bcd6f5", "text": "#173a63", "muted": "#486e9c",
        "accent": "#ffffff", "accent_hover": "#f4f9ff", "accent_text": "#1d4ed8",
        "row_bg": "#e8f1fd", "row_hover": "#cfe0f5", "row_selected": "#8cbaf0",
        "row_selected_edge": "#1d4ed8",
        # 修复缺陷R26：选中行 3D 凸起角色（蓝调：投影深蓝灰）
        "sel_top": "#60a5fa", "sel_bot": "#2563eb",
        "sel_border": "#93c5fd", "sel_hi": "#dbeafe",
        "sel_shadow": "#91a8d3", "sel_text": "#ffffff",
        "row_border": "#b4cdec",
        "row_text": "#173a63", "is_dark": "0",
        "splitter": "#a9c4e4", "splitter_grip": "#6d95c2",
    },
    "green": {
        "name": "🟢 绿调", "icon": "🟢", "label": "绿调",
        "window": "#cdeeda", "card": "#e6f7ec",
        "header": "#b7e3c8", "text": "#14432a", "muted": "#3f7d59",
        "accent": "#ffffff", "accent_hover": "#f2fbf6", "accent_text": "#15803d",
        "row_bg": "#e6f7ec", "row_hover": "#cdecd9", "row_selected": "#8fdcab",
        "row_selected_edge": "#15803d",
        # 修复缺陷R26：选中行 3D 凸起角色（绿调：顶部浅绿底部深绿）
        "sel_top": "#4ade80", "sel_bot": "#16a34a",
        "sel_border": "#86efac", "sel_hi": "#dcfce7",
        "sel_shadow": "#8cb89d", "sel_text": "#ffffff",
        "row_border": "#a3d6ba",
        "row_text": "#14432a", "is_dark": "0",
        "splitter": "#a6d2b9", "splitter_grip": "#5f9c7c",
    },
}
# 主题顺序（下拉列表展示顺序；修复缺陷R13 后不再循环点击）
THEME_ORDER = ("light", "dark", "blue", "green")
# 修复缺陷R14：主题下拉图标列固定宽（逻辑 px，CTk 自动随 DPI 缩放）
# —— 四个 emoji（☀️🌙🔵🟢）字形宽度不一，固定图标列后文字列起始
# 位置完全一致（垂直对齐）；选择框与弹窗共用同一列宽
_THEME_ICON_COL = 28
# 兼容别名（旧配置 appearance 值）
_THEME_ALIASES = {"dark": "dark", "light": "light", "blue": "blue",
                  "green": "green"}

_KW_DEFAULT = ("ERROR", "FAIL", "FATAL", "Caused by", "Exception",
               "Traceback")

# 修复缺陷R36：簇行样式统一常量（经典列表 / 虚拟列表共用；
# 优化缺陷R42 后全屏列表与主窗口同一 VirtualClusterList）——
# 原硬编码在近十处（_make_cluster_row、_apply_row_bg、
# VirtualClusterList.ROW_R_*），R33 一轮改了近十处极易漏改；
# 抽常量后一处调整同步。
# 几何约束：_ROW_PADX > _ROW_R_SEL（内容不压圆角切角区）；
# _ROW_BAR_INSET = _ROW_R_SEL + 6（高光/阴影条端头不压切角区）。
_ROW_R_SEL = 18      # 选中行圆角半径（药丸形，逻辑px）
_ROW_R_FLAT = 9      # 未选中行圆角半径（与选中 2:1，风格统一有区分）
_ROW_PADX = 22       # 行内容左右内边距（逻辑px，tk 控件需按 DPI 换算）
_ROW_BAR_INSET = 24  # 高光/阴影条两端内缩（逻辑px）

# 修复缺陷R12：错误列表 | 详情面板 可拖动分隔条参数
_SPLITTER_WIDTH = 6            # 分隔条宽度（像素）
# 列最小宽兜底值（逻辑像素）—— 实际以标题栏内容动态实测为准
# （_splitter_min_widths；200/300 旧值小于标题栏内容宽导致极限遮挡）
_SPLITTER_MIN_LIST = 420       # 左列兜底（实测标题栏 ~412 + 余量）
_SPLITTER_MIN_DETAIL = 360     # 右列兜底（实测标题栏 ~325 + padx + 余量）
_SPLITTER_DEFAULT_RATIO = 0.4  # 默认左右宽度比（列表:详情 = 2:3）

# 详情文本高亮标签配色（主面板与全屏窗口共用，修复缺陷#7/R5）
# 修复缺陷R5：业务栈帧提亮加粗更明显；系统库折叠提示独立配色更清晰
_DETAIL_TAG_COLORS = {"kw": "#ff6b6b", "bstack": "#fbbf24",
                      "meta": "#8fa4b8", "fold": "#a78bfa",
                      "header": "#4dd0e1"}

# 解析规则说明（悬停提示，修复缺陷#8；优化缺陷R71：附真实样例行）
RULE_DESCRIPTIONS = {
    "auto": "自动识别（推荐）：分析前取 150 行分层采样，按命中率→"
            "时间戳→模块给三套规则打分，自动选最优（无需懂规则）",
    "generic": "通用（大多数日志首选）\n"
               "样例: 2024-01-01 10:00:00 ERROR [db] connection refused",
    "embedded": "嵌入式/UT测试（串口、单测、编译输出）\n"
                "样例: [  123.456] [ERR ] [im_pll] clk config failed",
    "jenkins": "CI构建（Jenkins/流水线控制台）\n"
               "样例: [2024-01-01T12:00:00Z] [err] compile failed",
}


def _rate_text(lps: float) -> str:
    if lps >= 10000:
        return f"{lps / 10000:.1f} 万行/秒"
    return f"{lps:.0f} 行/秒"


def _widget_alive(widget) -> bool:
    """控件是否仍存活（destroy 后登记表清理用）。"""
    try:
        return bool(widget.winfo_exists())
    except (tk.TclError, AttributeError):
        return False


def _sort_rotation_oldest_first(paths) -> list:
    """轮转文件按内容时间排序（最旧在前，优化缺陷R89）。

    依据：轮转文件的修改时间与其内容时间一致（旧轮转先写完），
    mtime 升序即内容时间序；mtime 取不到时按文件名兜底，保证
    顺序确定不崩溃。
    """
    def key(p):
        try:
            return (0, os.path.getmtime(p))
        except OSError:
            return (1, str(p))
    return sorted(paths, key=key)


# ---------------------------------------------------------------------------
# 拖动期 CTk 重绘冻结（真实布局逐 motion 的性能保障，详见
# LogCompressorApp._set_ctk_drag_freeze）
# ---------------------------------------------------------------------------
def _ctk_draw_noop(self, *args, **kwargs) -> None:
    """冻结替换 CTk 类 _draw：拖动期跳过全部 CTk 外壳重绘。"""


def _ctk_draw_classes() -> list:
    """收集所有自定义 _draw 的 CTk 类（含内部模块）。

    CTk 控件在构造时绑定原 _update_dimensions_event（Tk bind 持有
    绑定方法引用，类补丁对已建控件无效）；但 _draw 一律经
    「实例 → 类」查找调用 —— 逐类冻结 _draw 对存量控件立即生效。
    """
    import customtkinter.windows.widgets.core_widget_classes as _cwc
    import customtkinter.windows.widgets.core_rendering.ctk_canvas as _cc
    classes = []
    seen = set()
    for mod in (ctk, _cwc, _cc):
        for name in dir(mod):
            obj = getattr(mod, name)
            if (isinstance(obj, type) and "_draw" in vars(obj)
                    and id(obj) not in seen):
                seen.add(id(obj))
                classes.append(obj)
    return classes


_CTK_DRAW_CLASSES = _ctk_draw_classes()


class Tooltip:
    """鼠标悬停提示（CustomTkinter / 原生控件通用）。

    修复缺陷#6/#8：GUI 关键选项与标题缺少解释性说明，用户无法
    理解「典型样例」「解析规则」等术语的含义。

    修复缺陷R3：原实现字体 10 号过小、定位固定在控件右下方，
    悬停主窗口右上角的 ⓘ 时文本框溢出屏幕右边界看不到完整内容。
    现改为：字体 12 号、宽 420px 自动换行、弹出位置智能判断
    （近右边缘向左弹 / 近下边缘向上弹）、白底深字 + 圆角阴影
    （Windows 平台 transparentcolor 实现真圆角，其他平台退化为
    白底描边矩形）。

    实现：Enter 后延迟显示无边框 Toplevel（不抢焦点、不挡操作），
    Leave / 按下时立即销毁。
    """

    # 优化：字号 18（12→15 用户仍反馈小）；宽度 420（不截断自动换行）
    _FONT = ("Microsoft YaHei UI", 18)
    _WRAP = 420
    _BG = "#ffffff"          # 浅色背景
    _FG = "#1f2937"          # 深色文字
    _BORDER = "#c9d3de"      # 描边
    _SHADOW1 = "#b9c3cf"     # 阴影内层
    _SHADOW2 = "#98a5b3"     # 阴影外层
    _CORNER = 10             # 圆角半径
    _MAGIC = "#ff00ff"       # 透明色（Windows -transparentcolor）
    _PAD = 14                # 文本内边距（指令：上下左右 12~16px）
    _MARGIN = 5              # 阴影外溢边距
    _DELAY_SHOW = 300        # 悬停显示延迟（ms）
    _DELAY_HIDE = 200        # 移出消失延迟（ms）

    def __init__(self, widget, text, delay: int = _DELAY_SHOW,
                 wrap: int = _WRAP):
        """text 支持静态字符串或返回字符串的可调用对象（动态提示）。

        修复缺陷#8：解析规则说明需跟随当前选中规则动态变化。
        优化：显示延迟 300ms（原 400）；移出后 200ms 延迟消失
        （原立即销毁，快速划过多个 ⓘ 时更连贯）。
        """
        self._widget = widget
        self._text = text
        self._delay = delay
        self._wrap = wrap
        self._tip: Optional[tk.Toplevel] = None
        self._after_id: Optional[str] = None
        self._hide_after_id: Optional[str] = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide_now, add="+")

    # ------------------------------------------------------------------
    def _current_text(self) -> str:
        if callable(self._text):
            try:
                return str(self._text() or "")
            except Exception:
                return ""
        return self._text or ""

    def _schedule(self, _event=None) -> None:
        """Enter：调度显示（已显示则保持，绝不触发销毁/重建）。

        修复闪烁：CTkLabel 是复合控件（canvas + 内部 label），bind
        双注册到两层 —— 指针在图标内微动跨子窗口边界时会成对触发
        Leave/Enter（X 的 NotifyInferior 语义）。旧链路 Leave 先
        调度 200ms 延迟销毁、Enter 只重置显示调度不取消销毁 ——
        已显示的 tooltip 被销毁又在 300ms 后重建，往返一次即一次
        视觉闪烁。现在 Enter 时取消挂起的销毁；tooltip 已显示时
        直接保持（零销毁零重建）。
        """
        self._cancel()
        self._cancel_hide()
        if self._tip is not None:
            return          # 已显示：保持显示，无需重建
        self._after_id = self._widget.after(self._delay, self._show)

    def _cancel(self) -> None:
        if self._after_id is not None:
            try:
                self._widget.after_cancel(self._after_id)
            except tk.TclError:
                pass  # 控件已销毁
            self._after_id = None

    @staticmethod
    def _round_rect(canvas: tk.Canvas, x1: float, y1: float,
                    x2: float, y2: float, r: float, **kw):
        """平滑多边形近似圆角矩形（smooth 插值）。"""
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
               x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
               x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return canvas.create_polygon(pts, smooth=True, **kw)

    def _show(self) -> None:
        if self._tip is not None:
            return
        # 优化：显示时取消挂起的延迟消失（快速划回同一 ⓘ）
        self._cancel_hide()
        text = self._current_text()
        if not text:
            return
        try:
            wx = self._widget.winfo_rootx()
            wy = self._widget.winfo_rooty()
            ww = self._widget.winfo_width()
            wh = self._widget.winfo_height()
        except tk.TclError:
            return  # 控件已销毁
        self._tip = tw = tk.Toplevel(self._widget)
        tw.wm_overrideredirect(True)   # 无边框
        tw.withdraw()                  # 先测量后定位再显示（无闪烁）
        try:
            tw.attributes("-topmost", True)
        except tk.TclError:
            pass
        # 圆角支持：Windows 透明色（其他平台捕获异常后走描边矩形）
        rounded = self._enable_rounded(tw)
        canvas = tk.Canvas(
            tw, highlightthickness=0, bd=0,
            bg=self._MAGIC if rounded else self._BG)
        canvas.pack(fill="both", expand=True)
        # 1) 先创建文本测尺寸（tag "text" 便于测试取用）
        # 优化：15 号微软雅黑默认行高 ~1.33 倍（canvas text 无
        # spacing 选项；字号放大后行高自然舒展，多行不拥挤）
        tip_id = canvas.create_text(
            1 + self._PAD, 1 + self._PAD, text=text, anchor="nw",
            justify="left", width=self._wrap, tags="text",
            font=self._FONT, fill=self._FG)
        tw.update_idletasks()
        bx1, by1, bx2, by2 = canvas.bbox(tip_id)
        w = (bx2 - bx1) + 2 * self._PAD + 2
        h = (by2 - by1) + 2 * self._PAD + 2
        W, H = w + self._MARGIN, h + self._MARGIN
        # 2) 绘制圆角卡片 + 双层阴影（压到文本下方）
        if rounded:
            s2 = self._round_rect(canvas, 4, 4, w + 2, h + 2,
                                  self._CORNER, fill=self._SHADOW2,
                                  outline="")
            s1 = self._round_rect(canvas, 2.5, 2.5, w + 0.5, h + 0.5,
                                  self._CORNER, fill=self._SHADOW1,
                                  outline="")
            card = self._round_rect(canvas, 1, 1, w - 2, h - 2,
                                    self._CORNER, fill=self._BG,
                                    outline=self._BORDER)
            canvas.tag_lower(card)
            canvas.tag_lower(s1)
            canvas.tag_lower(s2)
        else:
            card = canvas.create_rectangle(
                1, 1, w - 1, h - 1, fill=self._BG,
                outline=self._BORDER)
            canvas.tag_lower(card)
        canvas.configure(width=W, height=H)
        # 3) 优化：默认显示在控件正上方（水平居中）—— 级别 ⓘ 基准。
        # 此前默认右下方弹出，行内靠右的 ⓘ 触发右缘换向/钳位后
        # tooltip 相对图标位置各异（用户视觉"扭曲"）。统一为：
        # 水平中心对齐控件中心，底边距控件顶边 8px；水平溢出仅
        # 左右平移（不改变"正上方"关系）；贴近屏幕上缘放不下时
        # 才回退到控件下方；最终钳制在物理屏幕内（任意方向不溢出）。
        vx, vy, vw, vh = self._screen_bounds(tw)
        x = wx + ww // 2 - W // 2      # 与控件水平居中
        y = wy - H - 8                 # 控件正上方（8px 间隙）
        if y < vy + 8:                 # 上方空间不足 → 下方
            y = wy + wh + 8
        # 最终钳制在屏幕内（水平钳制只平移，垂直关系保持）
        x = max(vx + 8, min(int(x), int(vx + vw - W - 8)))
        y = max(vy + 8, min(int(y), int(vy + vh - H - 8)))
        tw.wm_geometry(f"+{x}+{y}")
        tw.deiconify()

    def _enable_rounded(self, tw: tk.Toplevel) -> bool:
        """尝试启用透明色圆角（Windows），失败返回 False 走矩形降级。"""
        try:
            tw.configure(bg=self._MAGIC)
            tw.attributes("-transparentcolor", self._MAGIC)
            return True
        except tk.TclError:
            return False

    @staticmethod
    def _screen_bounds(tw: tk.Toplevel):
        """物理像素的屏幕边界（多显示器取虚拟屏），用于钳位。

        优化（定位修正）：winfo_screenwidth() 在高 DPI 下返回
        逻辑像素（200% 时为物理值的一半），而 tooltip 几何
        （wm_geometry / rootx / 控件宽）是物理像素 —— 两者混用
        时右侧控件的 tooltip 被钳制到「逻辑屏宽」内（实际只占
        物理屏左半），远离图标数百像素（视觉"扭曲"）。Windows
        下经 ctypes 取虚拟屏物理边界（DPI 感知进程返回物理值），
        其他平台回退 winfo（无 DPI 缩放时两者一致）。
        """
        try:
            import ctypes
            u = ctypes.windll.user32
            vx = u.GetSystemMetrics(76)      # 虚拟屏左上 x
            vy = u.GetSystemMetrics(77)      # 虚拟屏左上 y
            vw = u.GetSystemMetrics(78)      # 虚拟屏宽
            vh = u.GetSystemMetrics(79)      # 虚拟屏高
            if vw > 0 and vh > 0:
                return vx, vy, vw, vh
        except Exception:
            pass
        return 0, 0, tw.winfo_screenwidth(), tw.winfo_screenheight()

    def _hide(self, _event=None) -> None:
        """Leave：200ms 延迟消失；指针仍在控件内时忽略（防闪烁）。

        修复闪烁（第二道防线）：指针在控件内部子窗口（canvas/
        label）间微移时 Tk 会发 detail=NotifyInferior 的 Leave ——
        此时指针并未真正离开控件矩形，销毁再重建即视觉闪烁。
        Leave 到达时实测指针热点位置，仍在控件矩形内则直接忽略。
        """
        self._cancel()
        if self._pointer_inside():
            return          # 子窗口边界抖动：指针未真正离开控件
        if self._hide_after_id is not None:
            return
        self._hide_after_id = self._widget.after(
            self._DELAY_HIDE, self._destroy_tip)

    def _pointer_inside(self) -> bool:
        """指针热点是否仍在控件矩形内（屏幕物理坐标比较）。"""
        try:
            px = self._widget.winfo_pointerx()
            py = self._widget.winfo_pointery()
            x = self._widget.winfo_rootx()
            y = self._widget.winfo_rooty()
            w = self._widget.winfo_width()
            h = self._widget.winfo_height()
            return x <= px < x + w and y <= py < y + h
        except tk.TclError:
            return False    # 控件已销毁

    def _hide_now(self, _event=None) -> None:
        """按下：立即销毁（不遮挡点击）。"""
        self._cancel()
        self._cancel_hide()
        self._destroy_tip()

    def _cancel_hide(self) -> None:
        if self._hide_after_id is not None:
            try:
                self._widget.after_cancel(self._hide_after_id)
            except tk.TclError:
                pass
            self._hide_after_id = None

    def _destroy_tip(self) -> None:
        self._hide_after_id = None
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


class VirtualClusterList:
    """主错误列表虚拟滚动容器（修复缺陷R6）。

    背景：CTk 复合控件（Frame/Label 各含内部 Canvas）创建开销大，
    Top N 调大后一次性渲染数百行明显卡顿。

    设计：
    - 只为「可见区域 + 缓冲」创建行控件（池化），控件数量与列表
      长度无关；滚动时把池内行重新绑定到不同数据索引（复用控件）；
    - 行使用原生 tk 控件（无内部 Canvas，创建开销约为 CTk 的 1/5）；
    - 固定行高（修复缺陷R9：头部 22 加粗 + 摘要单行 18 号不换行），
      摘要超长截断（完整内容在右侧详情面板查看）；
    - 修复缺陷R9：水平滚动 —— 摘要单行完整显示，内容自然宽度
      超过视口时画布横向扩展（xscrollcommand 接底部水平滚动条，
      Shift+滚轮横向滚动）。
    """

    ROW_HEIGHT = 138       # 类默认（实际在 __init__ 按字体度量动态计算）
    BUFFER = 2             # 视口上下缓冲行数（快速滚动不露白）
    SUMMARY_CLIP = 100     # 虚拟模式摘要截断字符数
    # 优化缺陷R26：行块外边距（逻辑px，随 DPI 缩放）—— 行块间真
    # 间隙（上下各 4 → 块间距 8px 呼吸感；左右各 5px 边距对齐），
    # 圆角/投影外侧统一为画布底色（window），圆角矩形有呼吸空间
    ROW_MX = 5
    ROW_MY = 4
    ROW_R_SEL = _ROW_R_SEL  # 修复缺陷R36：三模式统一模块常量（药丸形）
    ROW_R_FLAT = _ROW_R_FLAT  # 修复缺陷R36：三模式统一模块常量（2:1）

    def __init__(self, host, app, font_head=None, font_summary=None):
        self._app = app
        self._host = host
        self._data: List[ErrorCluster] = []
        self._slots: List[dict] = []      # 行控件池
        self._hovered = -1
        self._content_w = 600             # 数据内容自然宽（水平滚动区域宽）
        # 优化缺陷R42：字体可注入（全屏列表复用本组件时传全屏档
        # 28/24；主窗口默认行字体 22/18 不变）
        self._f_head = font_head or app._font_row_head
        self._f_sum = font_summary or app._font_row_summary
        # 修复缺陷R9：行高按实际渲染字体度量动态计算（DPI 无关，
        # 与经典模式行高一致：头部行距 + 摘要行距 + 行内边距/间隙）
        try:
            self._m_head = tkfont.Font(
                font=app._scaled_font(self._f_head))
            self._m_sum = tkfont.Font(
                font=app._scaled_font(self._f_sum))
            # 修复缺陷R41b：行高内边距 44 同步 DPI 缩放（行距随
            # 缩放字体翻倍而 44 不变 → 200% DPI 下行窗口为容纳内容
            # 被迫底部溢出圆角背景，盖住底部亮描边/阴影条）
            self.ROW_HEIGHT = (self._m_head.metrics("linespace")
                               + self._m_sum.metrics("linespace")
                               + self._sx(44))
        except (tk.TclError, ValueError):
            self._m_head = self._f_head
            self._m_sum = self._f_sum
        p = app._palette()
        # 修复缺陷R9：滚动步进随行高走（每 2 单位 ≈ 1 行）
        self._canvas = tk.Canvas(host, highlightthickness=0,
                                 bg=p["window"],
                                 yscrollincrement=max(1, self.ROW_HEIGHT // 2),
                                 xscrollincrement=60,
                                 width=470, height=400)
        # 修复缺陷R9：垂直/水平滚动条统一用 CTkScrollbar（与整体 UI 风格一致）
        self._vbar = ctk.CTkScrollbar(host, orientation="vertical",
                                      command=self._scroll_cmd)
        self._hbar = ctk.CTkScrollbar(host, orientation="horizontal",
                                      height=14, command=self._canvas.xview)
        self._canvas.configure(yscrollcommand=self._on_yscroll,
                               xscrollcommand=self._on_xscroll)
        # 优化（实时滚动防撕裂）：水平滚动条按下/释放 -> 快照滚动
        # 模式（可见行转画布图元，见 _on_hbar_press）
        self._xsnap = None
        try:
            self._hbar._canvas.bind("<ButtonPress-1>",
                                    self._on_hbar_press, add="+")
            self._hbar._canvas.bind("<ButtonRelease-1>",
                                    self._on_hbar_release, add="+")
        except (AttributeError, tk.TclError):
            pass
        host.grid_columnconfigure(0, weight=1)
        host.grid_rowconfigure(0, weight=1)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        self._vbar.grid(row=0, column=1, sticky="ns")
        self._hbar.grid(row=1, column=0, sticky="ew")
        # 修复缺陷R9：视口尺寸变化 -> 更新水平滚动区域并重对齐池行
        # （scrollregion 变更会经 yscrollcommand 回流 _on_yscroll ->
        # _sync，_sync 内不再改 scrollregion 且带重入护栏，防事件循环）
        self._in_sync = False
        # 优化（实时拖动快速路径）：分隔条拖动期间 _sync 只改行宽
        # （数据/滚动位置未变，跳过文本重填与事件重绑）
        self._dragging_splitter = False
        self._canvas.bind("<Configure>", self._on_canvas_resize)
        # 修复缺陷R9（顺带修复既有缺陷）：滚轮绑定挂在画布与池内行控件
        # 自身而非 bind_all —— bind_all 会覆盖 CTkScrollableFrame 的全局
        # 滚轮处理，虚拟模式销毁后经典列表滚轮失效；控件级绑定随控件
        # 销毁自动解除，无此问题。Shift+滚轮横向滚动。
        self._canvas.bind("<MouseWheel>", self._on_wheel)
        self._canvas.bind("<Shift-MouseWheel>", self._on_wheel_h)

    # ------------------------------------------------------------------
    # 数据 / 生命周期
    # ------------------------------------------------------------------
    def set_data(self, rows: list) -> None:
        """设置列表数据并回到顶部（含水平内容宽测量）。

        修复缺陷R16：数据为【视图行】—— ("c", 簇索引) 簇行 /
        ("i", 簇索引, 实例索引) 实例行；展开簇的全部实例作为独立
        视图行注入（主列表就地展示全部 N 个错误位置）。
        """
        self._data = rows
        self._hovered = -1
        # 数据更换时残留的快照图元一并清理（正常时序不会发生）
        if self._xsnap is not None:
            self._on_hbar_release(None)
        # 修复缺陷R9：内容自然宽（摘要单行不换行后的完整像素宽）
        self._content_w = self._measure_width(rows)
        self._update_region()
        self._canvas.yview_moveto(0.0)
        self._canvas.xview_moveto(0.0)
        self._sync()

    def update_rows(self, rows: list) -> None:
        """更新视图行但【保持滚动位置】（展开/收起簇，修复缺陷R16）。

        与 set_data 不同：不重置 x/y 视口 —— Tk canvas 在
        scrollregion 变化时保持内容偏移（视口内内容不动），用户
        展开/收起簇时浏览位置不跳动。
        """
        self._data = rows
        self._content_w = self._measure_width(rows)
        self._update_region()
        self._sync()

    def see_cluster(self, cidx: int) -> None:
        """滚动视口使指定簇行可见（搜索 Enter 跳转定位，优化缺陷R46）。"""
        for vidx, row in enumerate(self._data):
            if row[0] == "c" and row[1] == cidx:
                self._see_vidx(vidx)
                return

    def see_instance(self, cidx: int, iidx: int) -> None:
        """滚动视口使指定实例行可见（优化缺陷R56：扁平实例导航定位）。"""
        for vidx, row in enumerate(self._data):
            if row[0] == "i" and row[1] == cidx and row[2] == iidx:
                self._see_vidx(vidx)
                return

    def _see_vidx(self, vidx: int) -> None:
        """按视图行索引滚动入视口并重绘（簇/实例行共用）。"""
        total = max(1, len(self._data) * self.ROW_HEIGHT)
        y = vidx * self.ROW_HEIGHT
        vh = self._canvas.winfo_height()
        top = self._canvas.canvasy(0)
        if y < top or y + self.ROW_HEIGHT > top + vh:
            self._canvas.yview_moveto(max(0.0, min(1.0, y / total)))
        self._sync()

    def _tgl_icon_w(self) -> int:
        """「▶/▼」展开图标固定盒宽（物理px，修复缺陷R34）。

        ▶ 比 ▼ 字形宽 8~10px：等宽盒取两字形最大宽 + 1 空格宽
        （与原「▶ ×N」视觉间距一致），展开/收起切换只换盒内字
        形，其后「×N」与头部文字起始 x 不变。
        """
        return (max(self._m_head.measure("\u25b6"),
                    self._m_head.measure("\u25bc"))
                + self._m_head.measure(" "))

    def _measure_width(self, rows: list) -> int:
        """全部行的最大文本像素宽（按实际渲染的缩放字体度量）。"""
        app = self._app
        need = 0
        for row in rows:
            try:
                if row[0] == "c":
                    cluster = app._displayed[row[1]]
                    need = max(
                        need,
                        self._m_sum.measure(app._clip(
                            cluster.summary, self.SUMMARY_CLIP)),
                        self._m_head.measure(app._row_text(
                            cluster, with_count=False))
                        + self._tgl_icon_w()
                        + self._m_head.measure(
                            f"\u00d7{cluster.count}"))
                else:
                    inst = app._displayed[row[1]].instances[row[2]]
                    need = max(
                        need,
                        self._m_head.measure(
                            "      " + app._inst_head_text(inst)),
                        self._m_sum.measure("        " + app._clip(
                            inst.summary, self.SUMMARY_CLIP)))
            except (IndexError, AttributeError, tk.TclError):
                continue
        return need + 2 * self._sx(_ROW_PADX)  # 行内左右边距（R37）

    def _region_w(self) -> int:
        """水平滚动区域宽：内容自然宽与视口宽取大（整行高亮需占满视口）。"""
        try:
            view_w = self._canvas.winfo_width()
        except tk.TclError:
            view_w = 0
        return max(self._content_w, view_w, 200)

    def _update_region(self) -> None:
        self._canvas.configure(
            scrollregion=(0, 0, self._region_w(),
                          len(self._data) * self.ROW_HEIGHT))

    def destroy(self) -> None:
        """销毁虚拟列表（切回经典模式 / 重新分析时）。"""
        self._xsnap = None
        for slot in self._slots:
            try:
                slot["frame"].destroy()
            except tk.TclError:
                pass
        self._slots = []
        for w in (self._canvas, self._vbar, self._hbar):
            try:
                w.destroy()
            except tk.TclError:
                pass

    def apply_palette(self) -> None:
        """主题切换：画布底色 + 可见行重刷（fill 时取最新调色板）。"""
        try:
            self._canvas.configure(bg=self._app._palette()["window"])
        except (tk.TclError, ValueError):
            pass
        self._sync()

    @property
    def slots(self) -> List[dict]:
        return self._slots

    # ------------------------------------------------------------------
    # 滚动
    # ------------------------------------------------------------------
    def _scroll_cmd(self, *args) -> None:
        self._canvas.yview(*args)

    def _on_yscroll(self, first, last) -> None:
        # 优化（实时拖动禁内部重绘）：拖动中画布宽度变化也会回流
        # yscrollcommand（视口比例重算）→ vbar.set → CTkScrollbar
        # 重绘；拖动中跳过（高度未变，滑块位置无视觉差异，松开时
        # set_splitter_drag(False) 统一补齐）
        if self._dragging_splitter:
            return
        self._vbar.set(first, last)
        self._sync()

    def _on_xscroll(self, first, last) -> None:
        # 优化（实时拖动禁内部重绘）：分隔条拖动中画布宽度逐帧
        # 变化 → xview 范围变 → 本回调 → hbar.set → CTkScrollbar
        # 重绘（实测每帧最大头）。拖动中跳过（松开时
        # set_splitter_drag(False) 统一补 region/同步/滑块位置）
        if self._dragging_splitter:
            return
        self._hbar.set(first, last)

    def _on_canvas_resize(self, event) -> None:
        """视口尺寸变化：更新水平滚动区域宽（内容窄时区域=视口宽）。"""
        # 优化（实时拖动禁内部重绘）：拖动中不更新 scrollregion
        # （scrollregion 变更会回流 xscrollcommand 触发滚动条重绘
        # 级联）—— 只走行宽快速路径（_sync 内部判断），region 与
        # 全量同步延迟到松开时一次补齐
        if self._dragging_splitter:
            self._sync()
            return
        self._update_region()
        self._sync()

    def _on_wheel(self, event) -> None:
        steps = -int(event.delta / 120) * 2   # 每 2 单位 ≈ 1 行
        self._canvas.yview_scroll(steps, "units")

    def _on_wheel_h(self, event) -> None:
        # 修复缺陷R9：Shift+滚轮横向滚动（单行长摘要左右查看）
        self._canvas.xview_scroll(-int(event.delta / 120) * 3, "units")

    # ------------------------------------------------------------------
    # 水平滚动快照模式（实时滚动防撕裂）
    # ------------------------------------------------------------------
    def _on_hbar_press(self, _event) -> None:
        """水平滚动条按下：可见行转画布图元（单表面原子滚动）。

        优化（实时滚动防撕裂）：拖动水平滚动条时若照常滚动画布，
        每个行窗口（原生子窗口）逐个平移 + 画布底色回填异步交错
        上屏，快速拖动出现文字重影/重叠。按下时把当前可见行转画
        为同一画布上的矩形/文本图元并隐藏行窗口 —— 拖动中滚动只
        重绘单一画布（Tk 内部双缓冲整帧原子上屏），物理上不可能
        撕裂；且水平滚动中可见行集合不变（仅整体 X 平移），图元
        渲染内容与真实行完全一致。释放时删除图元恢复行窗口
        （行窗口坐标在隐藏期间已随画布同步平移，无需重排）。
        """
        if self._xsnap is not None or not self._data:
            return
        # 优化缺陷R23：行窗口即将隐藏，取消进行中的弹起动画并
        # 清理阴影图元（避免残留矩形悬在快照上方）
        for _s in self._slots:
            self._pop_cancel(_s)
        try:
            if self._canvas.winfo_width() >= self._region_w():
                return          # 无水平滚动范围
        except tk.TclError:
            return
        app = self._app
        p = app._palette()
        states = app._row_states()
        width = self._region_w()
        # 只绘制视口相交行（水平滚动不改变纵向可见集合；池内
        # 含上下缓冲行，全部绘制会成倍增加每帧重绘图元数）
        try:
            top = self._canvas.canvasy(0)
            vh = self._canvas.winfo_height()
        except tk.TclError:
            return
        items = []
        try:
            for slot in self._slots:
                idx = slot.get("idx", -1)
                if idx < 0 or idx >= len(self._data):
                    continue
                y = idx * self.ROW_HEIGHT
                if y + self.ROW_HEIGHT <= top or y >= top + vh:
                    continue
                # 修复缺陷R16：视图行双类型（簇行/实例行）快照
                row = self._data[idx]
                if row[0] == "c":
                    cidx = row[1]
                    cluster = app._displayed[cidx]
                    selected = cidx == app._selected_row
                    expanded = cidx in app._expanded_clusters
                    # 修复缺陷R40：快照与真实行同色 —— 选中行头部
                    # 用调亮级别色（蓝底可辨级别）
                    head_color = (
                        (app._row_color_sel(cluster) or p["sel_text"])
                        if selected
                        else (app._row_color(cluster) or p["row_text"]))
                    # 修复缺陷R37：快照与真实行同构 —— ▶/▼ 画在
                    # 等宽盒中心（展开/收起不位移），次数/元信息分列
                    tgl_icon = "\u25bc" if expanded else "\u25b6"
                    tgl_cnt = f"\u00d7{cluster.count}"
                    head_text = app._row_text(cluster, with_count=False)
                    sum_text = app._clip(cluster.summary,
                                         self.SUMMARY_CLIP)
                    link = ("#60a5fa" if p["is_dark"] == "1"
                            else "#2563EB")
                else:
                    inst = app._displayed[row[1]].instances[row[2]]
                    selected = ((row[1], row[2]) ==
                                getattr(app, "_selected_inst", None))
                    head_color = p["muted"]
                    tgl_icon = None
                    head_text = "      " + app._inst_head_text(inst)
                    sum_text = "        " + app._clip(
                        inst.summary, self.SUMMARY_CLIP)
                if selected:
                    bg = states["selected"]
                elif idx == self._hovered:
                    bg = states["hover"]
                else:
                    bg = states["bg"]
                # 修复缺陷R26：快照矩形对齐行块边距（与真实行同位）
                gx, gy = self._row_gaps()
                items.append(self._canvas.create_rectangle(
                    gx, y + gy, width - gx, y + self.ROW_HEIGHT - gy,
                    fill=bg, width=0))
                # 修复缺陷R37：内容 x = 行窗口内缩 _sx(_ROW_PADX)
                # （与真实行同位）；头部行 = 图标等宽盒+次数+元信息
                tx = gx + self._sx(_ROW_PADX)
                if tgl_icon is not None:
                    _iw = self._tgl_icon_w()
                    items.append(self._canvas.create_text(
                        tx + _iw // 2, y + gy + 7, anchor="n",
                        font=self._m_head, fill=link, text=tgl_icon))
                    items.append(self._canvas.create_text(
                        tx + _iw, y + gy + 7, anchor="nw",
                        font=self._m_head, fill=link, text=tgl_cnt))
                    tx += (_iw + self._m_head.measure(tgl_cnt)
                           + self._sx(10))
                items.append(self._canvas.create_text(
                    tx, y + gy + 7, anchor="nw", font=self._m_head,
                    fill=head_color, text=head_text))
                # 分界细线 y = 头部行底；摘要 y = 分界+线高+上边距2
                _ls = self._m_head.metrics("linespace")
                _dy = y + gy + 7 + _ls + 2
                if tgl_icon is not None:
                    items.append(self._canvas.create_rectangle(
                        gx + self._sx(_ROW_PADX), _dy,
                        width - gx - self._sx(_ROW_PADX),
                        _dy + self._sx(1),
                        fill=(p["sel_border"] if selected
                              else p["row_border"]), width=0))
                sum_y = _dy + self._sx(1) + 2
                items.append(self._canvas.create_text(
                    gx + self._sx(_ROW_PADX), sum_y, anchor="nw",
                    font=self._m_sum, fill=p["row_text"], text=sum_text))
            for slot in self._slots:
                try:
                    self._canvas.itemconfigure(slot["win"], state="hidden")
                except tk.TclError:
                    pass
        except (tk.TclError, ValueError):
            for it in items:
                try:
                    self._canvas.delete(it)
                except tk.TclError:
                    pass
            return
        self._xsnap = items

    def _on_hbar_release(self, _event) -> None:
        """水平滚动条释放：删除快照图元，恢复真实行窗口。"""
        items, self._xsnap = self._xsnap, None
        if items is None:
            return
        for it in items:
            try:
                self._canvas.delete(it)
            except tk.TclError:
                pass
        for slot in self._slots:
            try:
                self._canvas.itemconfigure(slot["win"], state="normal")
            except tk.TclError:
                pass

    def _bind_row_wheel(self, *widgets) -> None:
        """池内行控件挂滚轮绑定（Tk 事件不冒泡）。"""
        for w in widgets:
            w.bind("<MouseWheel>", self._on_wheel)
            w.bind("<Shift-MouseWheel>", self._on_wheel_h)

    # ------------------------------------------------------------------
    # 池化渲染核心
    # ------------------------------------------------------------------
    def _sync(self) -> None:
        """把池内行对齐到当前可见索引区间（滚动/resize 时复用）。

        修复缺陷R9：带重入护栏 —— _on_yscroll 会回调本方法，若本方法
        再触发滚动回调（scrollregion/yview 变化）会形成事件循环死锁。
        """
        if self._in_sync or not self._data:
            return
        if self._xsnap is not None:
            return          # 快照滚动中：行窗口已隐藏，拖动结束后恢复
        height = self._canvas.winfo_height()
        if height < 10:                       # 布局未完成
            return
        self._in_sync = True
        try:
            # 修复缺陷R9：行宽 = 水平滚动区域宽（内容超宽时行随之加宽，
            # 整行高亮与点击区域覆盖全部内容）
            width = self._region_w()
            if self._dragging_splitter:
                # 优化（实时拖动快速路径）：拖动中数据/滚动位置不变、
                # 仅列宽变化 —— 只把池行窗口宽度对齐新列宽（整行高亮
                # 实时跟随），跳过文本重填/事件重绑（每帧仅 ~15 次
                # itemconfigure，<1ms）；松开时 set_splitter_drag(False)
                # 触发一次全量 _sync 补齐。行文本/颜色本就正确（数据
                # 没变），无视觉差异。
                gx, _gy = self._row_gaps()
                for slot in self._slots:
                    try:
                        self._canvas.itemconfigure(
                            slot["win"], width=max(10, width - 2 * gx))
                    except tk.TclError:
                        pass
                return
            top = self._canvas.canvasy(0)
            first = max(0, int(top // self.ROW_HEIGHT) - self.BUFFER)
            need = int(height // self.ROW_HEIGHT) + 2 * self.BUFFER + 1
            last = min(len(self._data), first + need)
            # 池按需增长（只增不减，控件全程复用）
            while len(self._slots) < (last - first):
                self._slots.append(self._make_slot(width))
            for i, slot in enumerate(self._slots):
                idx = first + i
                if idx < last:
                    self._fill_slot(slot, idx, width)
                else:
                    try:
                        self._canvas.itemconfigure(slot["win"], state="hidden")
                    except tk.TclError:
                        pass
        finally:
            self._in_sync = False

    def set_splitter_drag(self, active: bool) -> None:
        """分隔条拖动模式开关：拖动中 _sync 走快速路径（只改行宽）。

        优化（实时拖动）：active=False 时补齐拖动期间跳过的全部
        内部重排 —— scrollregion（列宽变化后水平滚动范围）+ 一次
        全量 _sync（文本填充/事件重绑）+ 滚动条滑块位置（画布
        xview 比例随宽度变化已偏移，set 让 hbar 重新对齐）。
        """
        self._dragging_splitter = active
        if not active and self._data:
            self._update_region()
            self._sync()
            try:
                # 触发一次 xscrollcommand 回调，刷新滑块到真实位置
                self._canvas.xview_moveto(self._canvas.xview()[0])
            except tk.TclError:
                pass

    def _make_slot(self, width: int) -> dict:
        """创建一个池化行（原生 tk 控件，创建后长期复用）。"""
        p = self._app._palette()
        frame = tk.Frame(self._canvas, bg=p["row_bg"], bd=0,
                         highlightthickness=0)
        # 修复缺陷R16：头部行 = 「▶ ×N」展开按钮 + 元信息（实例行
        # 时按钮置空、文本缩进表示层级），按钮独立点击展开/收起
        line = tk.Frame(frame, bg=p["row_bg"], bd=0,
                        highlightthickness=0)
        line.pack(fill="x")
        # 修复缺陷R9：头部/摘要字体均施加与经典模式 CTkLabel 一致的
        # DPI 缩放（原生 tk.Label 不缩放命名字体，直接传会偏小/不一致）
        # 修复缺陷R34：▶/▼ 等宽图标盒（固定宽 Frame + place 居中）——
        # 两字形宽差 8~10px，合写单标签时展开/收起切换推动后续
        # 头部文字左右位移；盒宽固定后切换只换盒内字形
        icon_box = tk.Frame(line, bg=p["row_bg"], bd=0,
                            highlightthickness=0,
                            width=self._tgl_icon_w())
        icon_box.pack_propagate(False)
        # 修复缺陷R37：行窗口已内缩 _sx(_ROW_PADX)（=主列表内容起点），
        # 槽内控件不再额外左 padx —— 三模式内容起始 x 逐像素一致
        icon_box.pack(side="left", fill="y", pady=(7, 2))
        toggle_icon = tk.Label(
            icon_box, anchor="center",
            font=self._app._scaled_font(self._f_head),
            bg=p["row_bg"], fg="#2563EB", cursor="hand2")
        toggle_icon.place(relx=0.5, rely=0.5, anchor="center")
        toggle = tk.Label(
            line, anchor="w",
            font=self._app._scaled_font(self._f_head),
            bg=p["row_bg"], fg="#2563EB", cursor="hand2")
        toggle.pack(side="left", pady=(7, 2))
        head = tk.Label(line, anchor="w",
                        font=self._app._scaled_font(self._f_head),
                        bg=p["row_bg"], fg=p["row_text"])
        head.pack(side="left", fill="x", expand=True,
                  padx=(self._sx(10), self._sx(10)), pady=(7, 2))
        # 修复缺陷R37：头部/摘要间 1px 细分界线（与经典/全屏一致；
        # 簇行选中态 sel_border 亮色、未选中 row_border、实例行隐形）
        divider = tk.Frame(frame, bg=p["row_border"], bd=0,
                           highlightthickness=0,
                           height=self._sx(1))
        divider.pack(fill="x")
        # 修复缺陷R9：摘要单行不换行（wraplength=0），长摘要靠水平滚动查看
        summary = tk.Label(frame, anchor="w", justify="left",
                           font=self._app._scaled_font(self._f_sum),
                           wraplength=0,
                           bg=p["row_bg"], fg=p["row_text"])
        summary.pack(fill="x", padx=(0, self._sx(4)), pady=(2, 6))
        # 修复缺陷R37：行窗口内缩 = _sx(_ROW_PADX)（与主列表内容起点
        # 22 逻辑px 一致，随 DPI 缩放；原固定 24 物理px 高 DPI 下
        # 内容偏右且与主列表错位）；上下各留12px避免覆盖圆角背景
        # 修复缺陷R41b：垂直内缩同步 DPI 缩放（原固定 24 物理px，
        # 200% DPI 下行窗口底部溢出圆角背景 4px，盖住底部亮描边与
        # 阴影条 → 选中行底部「边框开口」）
        _inset = self._sx(_ROW_PADX)
        win = self._canvas.create_window(
            0, 0, window=frame, anchor="nw",
            width=max(10, width - 2 * _inset),
            height=self.ROW_HEIGHT - 2 * self._sx(12))
        self._bind_row_wheel(frame, toggle_icon, icon_box, toggle,
                             head, divider, summary)
        # 修复缺陷R22：line 入字典 —— 选中/悬停着色需覆盖头部条
        # （原未保存引用，line 底色停留 row_bg，选中蓝块被头部
        # 内边距区域的暗色横竖条切割成三段）
        return {"frame": frame, "line": line, "toggle": toggle,
                "toggle_icon": toggle_icon, "icon_box": icon_box,
                "divider": divider,
                "head": head, "summary": summary, "win": win, "idx": -1,
                "virtual": True}

    def _fill_slot(self, slot: dict, idx: int, width: int) -> None:
        """池行填充视图行 idx（簇行/实例行双类型，修复缺陷R16）。"""
        app = self._app
        try:
            row = self._data[idx]
        except IndexError:
            return
        # 优化缺陷R23：槽位回收到新数据索引前清理残留弹起动画
        # （阴影/定时器）；同 idx 刷新保留动画（坐标由动画持有）
        if slot.get("idx") != idx:
            self._pop_cancel(slot)
        p = app._palette()
        states = app._row_states()
        bg = (states["hover"] if idx == self._hovered else states["bg"])
        selected = False
        try:
            if row[0] == "c":
                cidx = row[1]
                cluster = app._displayed[cidx]
                if cidx == app._selected_row:
                    selected = True
                expanded = cidx in app._expanded_clusters
                head_color = app._row_color(cluster) or p["row_text"]
                link = ("#60a5fa" if p["is_dark"] == "1" else "#2563EB")
                slot["idx"] = idx
                # 修复缺陷R37：选中行统一 sel_bot 底（与经典/全屏
                # _apply_row_bg 同色无缝一致，弃用 R26 渐变能带），
                # 文字统一 sel_text 白保可读；未选中行平面
                top_c = p["sel_bot"] if selected else bg
                bot_c = p["sel_bot"] if selected else bg
                fg = p["sel_text"] if selected else None
                slot["frame"].configure(bg=bot_c)
                # 修复缺陷R34：等宽盒内只换 ▶/▼ 字形，次数另列，
                # 盒底随头部条能带色
                slot["toggle_icon"].configure(
                    bg=top_c, fg=fg or link,
                    text="\u25bc" if expanded else "\u25b6")
                slot["toggle"].configure(
                    bg=top_c, fg=fg or link,
                    text=f"\u00d7{cluster.count}")
                slot["icon_box"].configure(bg=top_c)
                # 修复缺陷R40：选中行头部用调亮级别色（蓝底可辨级别）
                slot["head"].configure(
                    bg=top_c,
                    fg=((app._row_color_sel(cluster) or p["sel_text"])
                        if selected else head_color),
                    text=app._row_text(cluster, with_count=False))
                slot["summary"].configure(
                    bg=bot_c, fg=fg or p["row_text"],
                    text=app._clip(cluster.summary, self.SUMMARY_CLIP))
                # 修复缺陷R37：分界线（选中 sel_border 亮色/未选中低调）
                slot["divider"].configure(
                    bg=(p["sel_border"] if selected else p["row_border"]))
                for w in (slot["frame"], slot["head"], slot["summary"],
                          slot["toggle"], slot["toggle_icon"],
                          slot["icon_box"], slot["divider"]):
                    w.bind("<Enter>",
                           lambda e, i=idx: self._hover(i, True))
                    w.bind("<Leave>",
                           lambda e, i=idx: self._hover(i, False))
                for w in (slot["frame"], slot["head"], slot["summary"],
                          slot["divider"]):
                    # 优化缺陷R23：点击触发立体弹起（先选中着色，
                    # 再下沉按压；释放时上弹+阴影回落）
                    w.bind("<Button-1>",
                           lambda e, i=cidx, s=slot:
                           (app._select_cluster(i, sync_nav=True),
                            self._pop_press(s)))
                    w.bind("<ButtonRelease-1>",
                           lambda e, s=slot: self._pop_release(s))
                # 展开按钮独立绑定（不触发行选中）
                for w in (slot["toggle"], slot["toggle_icon"],
                          slot["icon_box"]):
                    w.bind("<Button-1>",
                           lambda e, i=cidx:
                           app._toggle_cluster_expand(i))
            else:
                cidx, iidx = row[1], row[2]
                inst = app._displayed[cidx].instances[iidx]
                if (cidx, iidx) == getattr(app, "_selected_inst", None):
                    selected = True
                slot["idx"] = idx
                # 修复缺陷R37：实例行同款统一底（与簇行一致）
                top_c = p["sel_bot"] if selected else bg
                bot_c = p["sel_bot"] if selected else bg
                fg = p["sel_text"] if selected else None
                slot["frame"].configure(bg=bot_c)
                slot["toggle_icon"].configure(bg=top_c, text="")
                slot["toggle"].configure(bg=top_c, text="")
                slot["icon_box"].configure(bg=top_c)
                # 修复缺陷R37：实例行分界线隐形（与行体同色）
                slot["divider"].configure(bg=top_c)
                slot["head"].configure(
                    bg=top_c, fg=fg or p["muted"],
                    text="      " + app._inst_head_text(inst))
                slot["summary"].configure(
                    bg=bot_c, fg=fg or p["row_text"],
                    text="        " + app._clip(inst.summary,
                                                self.SUMMARY_CLIP))
                for w in (slot["frame"], slot["head"], slot["summary"],
                          slot["toggle"], slot["toggle_icon"],
                          slot["icon_box"], slot["divider"]):
                    # 优化缺陷R23：实例行同款立体弹起
                    w.bind("<Button-1>",
                           lambda e, ci=cidx, ii=iidx, s=slot:
                           (app._select_instance(ci, ii),
                            self._pop_press(s)))
                    w.bind("<ButtonRelease-1>",
                           lambda e, s=slot: self._pop_release(s))
                    w.bind("<Enter>",
                           lambda e, i=idx: self._hover(i, True))
                    w.bind("<Leave>",
                           lambda e, i=idx: self._hover(i, False))
            # 修复缺陷R22：头部条 line 同步着色（消除选中蓝块被
            # line 暗色底切成三段）
            # 修复缺陷R37：line 随行体统一底；选中行亮描边
            # 改由画布圆角轮廓呈现（_round_masks），原生方形描边
            # 仅未选中行保留（row_border 细边框，角部被遮罩切圆）
            slot["line"].configure(bg=top_c)
            slot["frame"].configure(
                highlightthickness=0 if selected else 1,
                highlightbackground=p["row_border"],
                highlightcolor=p["row_border"])
            # 优化缺陷R24/R26：全部行圆角遮罩（选中 18px/未选中 9px，
            # R36 模块常量），选中行另加画布亮描边+顶部高光+底部/
            # 右侧投影（3D 凸起）
            self._round_masks(slot, selected)
            gx, gy = self._row_gaps()
            # 修复缺陷R37：行窗口内缩 = _sx(_ROW_PADX)（与主列表内容
            # 起点一致，随 DPI 缩放；上下各12px），居中在圆角背景内
            # 修复缺陷R41b：垂直内缩同步 DPI 缩放（与 _make_slot 一致）
            self._canvas.itemconfigure(
                slot["win"], state="normal",
                width=max(10, width - 2 * self._sx(_ROW_PADX)),
                height=self.ROW_HEIGHT - 2 * self._sx(12))
            # 优化缺陷R23：弹起动画进行中坐标由动画持有（同 idx
            # 填充刷新不打断位移，避免悬停着色刷新造成 1 帧回跳）
            if slot.get("_pop") is None:
                self._canvas.coords(slot["win"], gx + self._sx(_ROW_PADX),
                                    self._row_y0(idx) + self._sx(12))
        except (tk.TclError, ValueError, IndexError):
            pass

    # ------------------------------------------------------------------
    # 优化（R23）：点击行立体弹起动画 —— 下沉按压 → 上弹浮起+阴影 → 回落
    # ------------------------------------------------------------------
    def _pop_scale(self) -> float:
        """弹起位移的 DPI 缩放因子（不同设备视觉幅度一致）。"""
        return max(1.0, getattr(self._app, "_font_scale", 1.0))

    def _sx(self, v: int) -> int:
        """逻辑px → 物理px（DPI 缩放，与主列表 CTk 缩放一致）。"""
        return int(round(v * self._pop_scale()))

    def _row_gaps(self):
        """行块外边距（gx 左右, gy 上下，物理px，随 DPI 缩放）。"""
        s = self._pop_scale()
        return (int(round(self.ROW_MX * s)), int(round(self.ROW_MY * s)))

    def _row_y0(self, idx: int) -> int:
        """行块窗口顶缘 y（含上间隙；弹起动画的位移基准）。"""
        return idx * self.ROW_HEIGHT + self._row_gaps()[1]

    def _blend(self, hex1: str, hex2: str, t: float) -> str:
        """两色预混合（t=hex2 权重；Tk 无透明度，阴影/高光预混近似）。"""
        try:
            r1, g1, b1 = (int(hex1[i:i + 2], 16) for i in (1, 3, 5))
            r2, g2, b2 = (int(hex2[i:i + 2], 16) for i in (1, 3, 5))
            k = 1.0 - t
            return (f"#{int(r1 * k + r2 * t):02x}"
                    f"{int(g1 * k + g2 * t):02x}{int(b1 * k + b2 * t):02x}")
        except (ValueError, IndexError):
            return hex1

    def _pop_press(self, slot: dict) -> None:
        """按下：行整体下沉 3px（物理按压感，即时无动画）。

        优化缺陷R23：仅画布 window 坐标平移（<0.1ms），不触发任何
        重排；快照滚动/分隔条拖动中跳过（行窗口已隐藏/冻结）。
        """
        if self._xsnap is not None or self._dragging_splitter:
            return
        idx = slot.get("idx", -1)
        if idx < 0:
            return
        self._pop_cancel(slot)                 # 清理上一次残留动画
        dy = int(round(3 * self._pop_scale()))
        gx, _gy = self._row_gaps()
        base = self._row_y0(idx)
        try:
            # 修复缺陷R27：行窗口位置偏移（居中在圆角背景内）
            # 修复缺陷R41b：坐标与 _fill_slot 同款缩放（原固定
            # gx+24/+12，高 DPI 下按压左跳且底部溢出圆角背景）
            self._canvas.coords(slot["win"], gx + self._sx(_ROW_PADX),
                                base + self._sx(12) + dy)
        except tk.TclError:
            return
        slot["_pop"] = {"base": base, "dy": dy,
                        "job": None}
        self._round_move(slot)                 # R24：圆角随下沉

    def _pop_release(self, slot: dict) -> None:
        """释放：上弹 5px → 回落原位，总时长 240ms。

        两段缓动（ease-out 上弹 120ms + ease-in-out 回落 120ms），
        16ms 步进 ≈15 帧；每帧仅 coords 行窗口 + 圆角组跟随
        （<0.5ms），零重排零卡顿。修复缺陷R25：阴影改为圆角组
        常驻投影（_round_move 按 dy 伸缩：浮起加深、按下收缩），
        动画结束立体感保留，不再一次性删除。
        """
        pop = slot.get("_pop")
        if pop is None:
            return
        import time as _time
        pop.update(t0=_time.monotonic(), start=pop["dy"],
                   lift=int(round(5 * self._pop_scale())))
        self._pop_step(slot)

    def _pop_step(self, slot: dict) -> None:
        """动画步进：按分段缓动应用位移，结束自动复位。"""
        import math
        import time as _time
        pop = slot.get("_pop")
        if pop is None or pop.get("t0") is None:
            return
        DUR1, DUR2 = 0.12, 0.12                # 上弹 / 回落（合计 240ms）
        el = _time.monotonic() - pop["t0"]
        base = pop["base"]
        if el < DUR1:                          # ease-out：start → -lift
            x = el / DUR1
            e = 1.0 - (1.0 - x) ** 3
            dy = pop["start"] + (-pop["lift"] - pop["start"]) * e
        elif el < DUR1 + DUR2:                 # ease-in-out：-lift → 0
            x = (el - DUR1) / DUR2
            e = 0.5 * (1.0 - math.cos(math.pi * x))
            dy = -pop["lift"] * (1.0 - e)
        else:                                  # 结束：复位
            self._pop_cancel(slot)
            return
        dy = int(round(dy))
        pop["dy"] = dy
        try:
            gx, _gy = self._row_gaps()
            # 修复缺陷R27：行窗口位置偏移（居中在圆角背景内）
            # 修复缺陷R41b：坐标与 _fill_slot 同款缩放
            self._canvas.coords(slot["win"], gx + self._sx(_ROW_PADX),
                                base + self._sx(12) + dy)
            self._round_move(slot)             # 圆角+投影逐帧跟随
        except tk.TclError:
            slot["_pop"] = None
            return
        try:
            pop["job"] = self._canvas.after(16, self._pop_step, slot)
        except tk.TclError:
            slot["_pop"] = None

    def _pop_cancel(self, slot: dict) -> None:
        """取消动画：停表/坐标复位（槽位回收、快照滚动前调用）。

        修复缺陷R25：圆角组（遮罩/高光/常驻投影）不在此删除 ——
        行仍选中时立体感保留，由 _fill_slot 按选中态统一管理。
        """
        pop = slot.pop("_pop", None)
        if not pop:
            return
        job = pop.get("job")
        if job:
            try:
                self._canvas.after_cancel(job)
            except tk.TclError:
                pass
        idx = slot.get("idx", -1)
        if idx >= 0:
            try:
                gx, _gy = self._row_gaps()
                # 修复缺陷R27：行窗口位置偏移（居中在圆角背景内）
                # 修复缺陷R41b：坐标与 _fill_slot 同款缩放
                self._canvas.coords(slot["win"], gx + self._sx(_ROW_PADX),
                                    self._row_y0(idx) + self._sx(12))
            except tk.TclError:
                pass
            self._round_move(slot)              # 圆角组随复位

    # ------------------------------------------------------------------
    # 优化（R24/R26）：行圆角遮罩 + 选中行 3D 凸起（渐变/描边/高光/投影）
    # ------------------------------------------------------------------
    @staticmethod
    def _arc_pts(cx: float, cy: float, r: float,
                 a0: float, a1: float, n: int = 12) -> list:
        """圆弧采样点（圆心+半径，角度 a0→a1 度；12 段抗锯齿平滑）。"""
        import math
        return [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / n)),
                 cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / n)))
                for i in range(n + 1)]

    def _corner_pts(self, x0, y0, x1, y1, r, corner):
        """单个圆角遮罩多边形（角方块 − 四分之一圆的区域）顶点序列。"""
        if corner == "tl":
            pts = [(x0, y0), (x0 + r, y0)] + self._arc_pts(
                x0 + r, y0 + r, r, 270, 180)
        elif corner == "tr":
            pts = [(x1, y0), (x1, y0 + r)] + self._arc_pts(
                x1 - r, y0 + r, r, 0, -90)
        elif corner == "bl":
            pts = [(x0, y1), (x0 + r, y1)] + self._arc_pts(
                x0 + r, y1 - r, r, 90, 180)
        else:  # "br"
            pts = [(x1, y1), (x1 - r, y1)] + self._arc_pts(
                x1 - r, y1 - r, r, 90, 0)
        flat = []
        for px, py in pts:
            flat.extend((px, py))
        return flat

    def _rrect_pts(self, x0, y0, x1, y1, r):
        """闭合圆角矩形轮廓顶点（4 直线 + 4 圆弧，画布描边用）。"""
        pts = [(x0 + r, y0), (x1 - r, y0)]
        pts += self._arc_pts(x1 - r, y0 + r, r, -90, 0)[1:]
        pts += [(x1, y0 + r), (x1, y1 - r)]
        pts += self._arc_pts(x1 - r, y1 - r, r, 0, 90)[1:]
        pts += [(x1 - r, y1), (x0 + r, y1)]
        pts += self._arc_pts(x0 + r, y1 - r, r, 90, 180)[1:]
        pts += [(x0, y1 - r), (x0, y0 + r)]
        pts += self._arc_pts(x0 + r, y0 + r, r, 180, 270)[1:]
        flat = []
        for px, py in pts:
            flat.extend((px, py))
        return flat

    def _round_masks(self, slot: dict, selected: bool) -> None:
        """创建行圆角背景：全行圆角矩形填充；选中行加描边/高光/投影。

        修复缺陷R27：Canvas子窗口（tk.Frame）覆盖圆角遮罩，导致圆角
        不显示。改方案：行窗口尺寸缩小留出圆角空间，Canvas上绘制圆角
        矩形背景（用行背景色填充），行窗口居中在圆角背景内。
        选中行另加 3D 凸起三件套 —— 圆角亮描边、顶部受光高光条、
        底部+右侧常驻投影；未选中行仅圆角背景，平面无阴影。
        """
        self._unround(slot)
        idx = slot.get("idx", -1)
        if idx < 0:
            return
        p = self._app._palette()
        s = self._pop_scale()
        r = min(int(round((self.ROW_R_SEL if selected
                           else self.ROW_R_FLAT) * s)),
                self.ROW_HEIGHT // 2 - 4)
        # 修复缺陷R27：行背景色（选中sel_bot / 未选中row_bg）
        bg_color = p["sel_bot"] if selected else p["row_bg"]
        grp = {"r": r, "sel": selected}
        try:
            # 修复缺陷R27：圆角矩形背景（替代四个角的遮罩）
            grp["bg"] = self._canvas.create_polygon(
                self._rrect_pts(0, 0, 1, 1, r),
                fill=bg_color, outline="")
            if selected:
                # 修复缺陷R37：亮描边 sel_hi + 3s 宽（近似经典 CTk
                # border_width=4 高光边的视觉分量）
                grp["border"] = self._canvas.create_polygon(
                    self._rrect_pts(0, 0, 1, 1, max(2, r - 1)),
                    fill="", outline=p["sel_hi"],
                    width=max(2, int(round(3 * s))))
                grp["hi"] = self._canvas.create_rectangle(
                    0, 0, 1, 1, fill=p["sel_hi"], width=0)
                grp["shadow"] = self._canvas.create_rectangle(
                    0, 0, 1, 1, fill=p["sel_shadow"], width=0)
                grp["rshadow"] = self._canvas.create_rectangle(
                    0, 0, 1, 1, fill=p["sel_shadow"], width=0)
        except tk.TclError:
            for it in [grp.get("bg")] + [
                    grp[k] for k in ("border", "hi", "shadow", "rshadow")
                    if grp.get(k)]:
                try:
                    self._canvas.delete(it)
                except tk.TclError:
                    pass
            return
        slot["_round"] = grp
        self._round_move(slot)

    def _round_move(self, slot: dict) -> None:
        """圆角组跟随行窗口当前位置（弹起逐帧/复位/创建时调用）。

        底部投影深度随 dy 变化：浮起（dy<0）拉长=升高；下沉
        （dy>0）收缩=按压；静止保持基础深度=常驻凸起感。
        """
        grp = slot.get("_round")
        if not grp:
            return
        idx = slot.get("idx", -1)
        if idx < 0:
            return
        pop = slot.get("_pop")
        dy = pop["dy"] if pop else 0
        gx, gy = self._row_gaps()
        y0 = self._row_y0(idx) + dy
        y1 = y0 + self.ROW_HEIGHT - 2 * gy
        x0 = gx
        x1 = max(x0 + 12, self._region_w() - gx)
        r = grp["r"]
        try:
            # 修复缺陷R27：移动圆角背景（替代四个角的遮罩）
            self._canvas.coords(
                grp["bg"], *self._rrect_pts(x0, y0, x1, y1, r))
            if grp.get("sel"):
                s = self._pop_scale()
                base_d = int(round(4 * s))
                depth = max(2, base_d - dy)    # dy<0 加深 / dy>0 收缩
                hi_h = int(round(2 * s))
                rs_w = max(2, int(round(2.5 * s)))
                # 圆角亮描边（内缩半线宽，外缘与圆角背景齐平）
                _bi = max(1, int(round(1.5 * s)))
                self._canvas.coords(
                    grp["border"],
                    *self._rrect_pts(x0 + _bi, y0 + _bi, x1 - _bi,
                                     y1 - _bi, max(2, r - _bi)))
                # 顶部受光高光条（两端内缩 r 贴合圆角）
                self._canvas.coords(grp["hi"], x0 + r, y0 + 2,
                                    x1 - r, y0 + 2 + hi_h)
                # 底部常驻投影（落在行间间隙画布上，两端内缩 r）
                self._canvas.coords(grp["shadow"], x0 + r, y1,
                                    x1 - r, y1 + depth)
                # 右侧常驻投影（落在右间隙画布上，两端内缩 r）
                self._canvas.coords(grp["rshadow"], x1, y0 + r,
                                    x1 + rs_w, y1 - r)
        except tk.TclError:
            pass

    def _unround(self, slot: dict) -> None:
        """删除圆角组全部图元（槽位回收时调用）。"""
        grp = slot.pop("_round", None)
        if not grp:
            return
        items = [grp.get("bg")] if grp.get("bg") else []
        for key in ("border", "hi", "shadow", "rshadow"):
            if grp.get(key):
                items.append(grp[key])
        for it in items:
            try:
                self._canvas.delete(it)
            except tk.TclError:
                pass

    def _hover(self, idx: int, hovered: bool) -> None:
        self._hovered = idx if hovered else -1
        # 修复缺陷R16：池行悬停统一由 _fill_slot 着色（视图行模型
        # 下 app._hover_row 的 displayed 索引语义不再适用）
        self._sync()


# 修复缺陷R21：tab 容器高度跟随当前页（文件导入页只有一行输入
# 框，原与文本粘贴页共享容器高度（粘贴框 height=100 撑大），大
# 片空白浪费 —— 按页预设紧凑高度，切换时适配，空间让给结果区。
# 高度值实测校准（逻辑 px，随 DPI 缩放）：页内容需求 + 按钮区
# 开销 60 + 4 余量，保证各页内容无裁切（_r21_dbg.py 实测）
_TAB_PAGE_HEIGHTS = {"文件导入": 126, "文本粘贴": 198, "多文件对比": 202}


class LogCompressorApp(_make_app_base()):
    """日志AI压缩器主窗口。"""

    def __init__(self):
        self._store = ConfigStore()
        self._config = self._store.load()
        # 优化缺陷R100：分析历史（随配置目录隔离，测试可注入临时目录）
        self._history = HistoryStore(self._store.path)
        self._from_history = False          # 历史回放中标记（防重复入库）
        # 优化缺陷R105：实时 Tail 监控状态（后台轮询线程 + 行缓冲）
        self._tailing = False
        self._tail_thread: Optional[threading.Thread] = None
        self._tail_stop = threading.Event()
        self._tail_path = ""
        self._tail_encoding = "utf-8"
        self._tail_offset = 0
        self._tail_partial = ""
        self._tail_lines: list = []
        self._tail_new_lines = 0
        self._tail_lock = threading.Lock()
        # 优化缺陷R112：监控新错误提醒基线（上一拍错误条目数）
        self._tail_prev_errors = 0
        # 优化缺陷R111：已知错误屏蔽（message_template 集合，持久化）
        self._muted = set(self._config.get("muted") or [])
        # 优化缺陷R114：过滤预设（名称 → UI 原值快照，⚙ 弹层套用）
        self._presets = dict(self._config.get("filter_presets") or {})
        self._last_preset = None          # 最近套用的预设名（✕ 删除目标）
        self._show_muted = False            # 会话态：列表是否显示已屏蔽
        self._detail_cluster = None         # 详情面板当前簇（屏蔽按钮用）
        # 修复缺陷R1：四态主题（兼容旧配置的 light/dark 值）
        raw_theme = str(self._config.get("appearance", "dark")).lower()
        self._theme = _THEME_ALIASES.get(raw_theme, "dark")
        ctk.set_appearance_mode("dark" if self._theme_is_dark() else "light")
        super().__init__()

        window = self._config.get("window", {})
        # 修复缺陷R9：默认窗口高度 840→1000 —— 840 逻辑高无法同时容纳
        # 22/18 号列表字体与 6 行可视错误（6 行纯文本即约 470px）；对
        # 旧配置一次性升级（标志位防止覆盖用户后续手动调整）。
        if not self._config.get("window_h_upgraded"):
            if int(window.get("height", 840)) < 900:
                window["height"] = 1000
                self._config["window"] = window
            self._config["window_h_upgraded"] = True
        self.title(f"{HUMAN_NAME}  v{__version__}")
        width = window.get("width", 1280)
        height = window.get("height", 1000)
        # 修复缺陷R107：恢复尺寸钳制到屏幕内（配置里存了超屏尺寸时
        # 右侧控件与底栏会被屏幕边缘裁掉，用户看不到状态栏/新按钮）
        try:
            width = min(width, self.winfo_screenwidth() - 40)
            height = min(height, self.winfo_screenheight() - 90)
        except tk.TclError:
            pass
        # 修复缺陷R108：启动位置居中（OS 默认落点在窗口近屏高时会
        # 把标题栏顶出屏幕上缘，用户无法拖动、底栏也被挤出屏幕）
        try:
            pos_x = max(0, (self.winfo_screenwidth() - width) // 2)
            pos_y = max(0, (self.winfo_screenheight() - height) // 2)
        except tk.TclError:
            pos_x = pos_y = 0
        self.geometry(f"{width}x{height}+{pos_x}+{pos_y}")
        self.minsize(1000, 680)
        # 修复缺陷R9：DPI 缩放系数 —— CTkLabel 对 CTkFont 自动施加控件
        # 缩放，而行内摘要用原生 tk.Label（命名字体原样使用、不缩放），
        # 高 DPI 屏上摘要实际渲染只有标称字号的 1/scale，偏小 —— 摘要
        # 字体也需按同倍率换算为缩放元组（_scaled_font）。
        try:
            self._font_scale = ctk.ScalingTracker.get_widget_scaling(self)
        except (AttributeError, tk.TclError):
            self._font_scale = 1.0

        # 运行状态
        self._result: Optional[AnalysisResult] = None
        self._compare_results: List[CompareResult] = []
        self._displayed: List[ErrorCluster] = []
        self._cluster_rows: List[dict] = []
        self._selected_row: int = -1
        # 修复缺陷R16：主列表簇就地展开状态（展示全部 N 个错误位置）
        # + 实例选中态（(簇索引, 实例索引)）
        self._expanded_clusters: Dict[int, bool] = {}
        self._selected_inst = None
        self._queue: "queue.Queue" = queue.Queue()
        # 共享字体：行级字体必须复用（每行新建 CTkFont 会被 GC 在
        # 任意线程析构，tkinter.Font.__del__ 跨线程调用 Tk 造成死锁）
        # 修复缺陷R9：列表字体（级别/次数 22 加粗，摘要 18）
        # 修复缺陷R10：字号随档位缩放（小/中/大/特大，默认中），并
        # 全屏字体再放大（头部 28 加粗 / 摘要 24 / 实例行 20）
        self._font_size = (self._config.get("font_size")
                           if self._config.get("font_size") in FONT_SIZE_SCALE
                           else "中")
        self._font_row_head = ctk.CTkFont(
            family="Consolas", size=self._font_px(22), weight="bold")
        self._font_row_summary = ctk.CTkFont(size=self._font_px(18))
        self._font_hint = ctk.CTkFont(size=12)
        self._font_fs_head = ctk.CTkFont(
            family="Consolas", size=self._font_px(28), weight="bold")
        self._font_fs_summary = ctk.CTkFont(size=self._font_px(24))
        self._font_fs_inst = ctk.CTkFont(size=self._font_px(20))
        self._cancel_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._chart_window: Optional[ctk.CTkToplevel] = None
        # 修复缺陷R6：虚拟列表 / 全屏窗口预创建缓存
        self._virtual_list: Optional[VirtualClusterList] = None
        self._fs_list_win: Optional[ctk.CTkToplevel] = None
        self._fs_list_refresh = None          # 全屏列表内容刷新回调
        self._fs_list_sig: Optional[tuple] = None    # 已渲染数据签名
        self._fs_detail_win: Optional[ctk.CTkToplevel] = None
        self._fs_detail_box: Optional[ctk.CTkTextbox] = None
        # 优化缺陷R45：主窗口结果搜索（显示层过滤，不触发重新分析）
        self._search_kw = ""
        self._search_job = None                 # 输入防抖 after 句柄
        # 优化缺陷R50：查找导航序号（0=未导航，1..y=定位到第几个匹配）
        self._search_nav = 0
        # 优化缺陷R53：全屏列表窗口独立搜索态（与主窗口同口径的
        # 关键字过滤 + x/y 计数导航 + Enter/Shift+Enter 循环定位）
        self._fs_search_kw = ""
        self._fs_search_nav = 0
        # 优化缺陷R54：详情全屏窗口文内查找态（关键字 + 导航序号 +
        # 匹配位置表 [(行,列)…]，与列表搜索同款 x/y 计数语义）
        self._fd_search_kw = ""
        self._fd_search_nav = 0
        self._fd_matches: List[tuple] = []

        # 修复缺陷R1：主题调色板登记表（切换时按角色批量刷新）
        self._bg_widgets: List[tuple] = []       # (控件, "window"/"card"/"header")
        self._muted_labels: List = []            # 次要文字标签
        self._accent_buttons: List[tuple] = []   # (按钮, "accent"/"danger")

        # 修复缺陷R12：分隔条位置（左右宽度比例）持久化恢复
        try:
            r = float(self._config.get("splitter_ratio",
                                       _SPLITTER_DEFAULT_RATIO))
        except (TypeError, ValueError):
            r = _SPLITTER_DEFAULT_RATIO
        self._splitter_ratio = min(max(r, 0.05), 0.95)
        # 修复缺陷R17：全屏列表窗口分隔条比例（独立于主界面持久化）
        try:
            r_fs = float(self._config.get("fs_splitter_ratio",
                                          _SPLITTER_DEFAULT_RATIO))
        except (TypeError, ValueError):
            r_fs = _SPLITTER_DEFAULT_RATIO
        self._fs_splitter_ratio = min(max(r_fs, 0.05), 0.95)

        self._build_ui()
        self._apply_palette()
        self._setup_drag_and_drop()
        self._restore_config()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._poll_queue)

    # ------------------------------------------------------------------
    # 主题调色板
    # ------------------------------------------------------------------
    def _palette(self) -> Dict[str, str]:
        """当前主题调色板。"""
        return THEMES.get(self._theme, THEMES["dark"])

    def _theme_is_dark(self) -> bool:
        """当前主题是否为暗色系（blue/green 视为亮色系）。"""
        return THEMES.get(self._theme, THEMES["dark"])["is_dark"] == "1"

    def _scaled_font(self, font_obj):
        """CTkFont -> 原生 tk.Label 字体（施加与 CTkLabel 一致的缩放）。

        修复缺陷R9：经典/虚拟/全屏行的摘要为原生 tk.Label，直接传
        CTkFont 命名字体不参与 DPI 缩放（高 DPI 屏渲染偏小），换算为
        缩放元组后与 CTkLabel 渲染尺寸一致（经典/虚拟两种模式相同）。
        """
        try:
            return font_obj.create_scaled_tuple(self._font_scale)
        except (AttributeError, ValueError):
            return font_obj

    def _font_px(self, base: int) -> int:
        """基准字号 -> 当前档位实际字号（修复缺陷R10：四档缩放）。"""
        return max(10, round(base * FONT_SIZE_SCALE.get(self._font_size, 1.0)))

    def _dpx(self, v: int) -> int:
        """逻辑px → 物理px（DPI 缩放；原生 tk padx/place 用，修复缺陷R38）。"""
        return int(round(v * max(1.0, getattr(self, "_font_scale", 1.0))))

    def _toggle_icon_w(self, scaled_font, for_ctk: bool) -> int:
        """「▶/▼」展开图标固定盒宽（修复缺陷R34）。

        ▶(U+25B6) 比 ▼(U+25BC) 字形宽 8~10px（Consolas 22/28 实
        测），二者合写一个标签时展开/收起切换使标签总宽变化，
        其后「×N ● 优先级 级别」头部文字随之左右位移（展开行与
        未展开行头部不对齐）。图标拆进等宽盒（两字形最大宽 + 1
        空格宽，保持原「▶ ×N」视觉间距），切换只换盒内字形，
        盒宽与后续文字起始 x 不变。

        scaled_font: 已按 DPI 缩放的字体元组（物理px 度量）。
        for_ctk=True 返回 CTk 逻辑px（CTkLabel width 用，CTk 内部
        再乘控件缩放）；False 返回物理px（原生 tk.Frame width 用）。
        结果按字体元组缓存（字号档位切换后元组变化自动重测）。
        """
        key = (repr(scaled_font), for_ctk)
        cache = getattr(self, "_tgl_w_cache", None)
        if cache is None:
            cache = self._tgl_w_cache = {}
        if key not in cache:
            fm = tkfont.Font(font=scaled_font)
            phys = (max(fm.measure("▶"), fm.measure("▼"))
                    + fm.measure(" "))
            if for_ctk:
                scale = max(1.0, getattr(self, "_font_scale", 1.0))
                phys = int(-(-phys // scale))    # 物理 → CTk 逻辑px
            cache[key] = phys
        return cache[key]

    def _apply_font_size(self, choice: str) -> None:
        """字体大小档位切换（修复缺陷R10：小/中/大/特大，立即生效）。

        - 共享 CTkFont 就地重配（CTk 控件自动跟随更新）；
        - 原生 tk.Label 的缩放元组是创建时快照，需整表重渲染；
        - 虚拟列表行高按字体度量计算，重建后自动适配新字号；
        - 全屏窗口的内联字体（搜索框/详情框）随构建固化 —— 销毁
          缓存窗口，下次打开按新字号重建（列表行随共享字体刷新）。
        """
        if choice not in FONT_SIZE_SCALE or choice == self._font_size:
            return
        self._font_size = choice
        self._font_row_head.configure(size=self._font_px(22))
        self._font_row_summary.configure(size=self._font_px(18))
        self._font_fs_head.configure(size=self._font_px(28))
        self._font_fs_summary.configure(size=self._font_px(24))
        self._font_fs_inst.configure(size=self._font_px(20))
        # 重渲染已有结果（经典 / 虚拟 / 对比模式的行级原生标签）
        if self._compare_results:
            self._render_compare_list()
        elif self._result is not None:
            self._render_cluster_list()
        # 全屏窗口缓存销毁（内联字体下次打开重建）
        for win_attr in ("_fs_list_win", "_fs_detail_win"):
            win = getattr(self, win_attr, None)
            if win is not None and win.winfo_exists():
                win.destroy()
            setattr(self, win_attr, None)
        self._fs_list_refresh = None
        self._fs_list_sig = None
        self._fs_detail_box = None
        # 修复缺陷R14：档位切换后控件组（含选择器）请求宽度变化，
        # 重新贴齐左列右缘
        self._layout_splitter()
        self._save_config()

    # ==================================================================
    # UI 构建
    # ==================================================================
    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        # 修复缺陷R72：新增实时筛选行（row=3），结果区 weight 行 4→5
        # 优化缺陷R85：新增高级选项行（row=3），各行顺移，结果区 5→6
        self.grid_rowconfigure(6, weight=1)

        self._build_header()
        self._build_tabs()
        self._build_config_panel()
        self._build_advanced_panel()
        self._build_search_panel()
        self._build_action_panel()
        self._build_result_panel()
        self._build_status_bar()

    def _build_header(self) -> None:
        # 修复缺陷R1：标题栏升级 —— 项目图标 + 大号加粗标题 + 主题角色登记
        bar = ctk.CTkFrame(self, corner_radius=0)
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_columnconfigure(1, weight=1)
        self._bg_widgets.append((bar, "header"))
        icon = ctk.CTkLabel(bar, text="🗂️", font=ctk.CTkFont(size=26))
        icon.grid(row=0, column=0, padx=(16, 4), pady=6, sticky="w")
        title = ctk.CTkLabel(
            bar, text=f"{HUMAN_NAME}  v{__version__}",
            font=ctk.CTkFont(size=20, weight="bold"))
        title.grid(row=0, column=1, padx=(2, 6), pady=6, sticky="w")
        subtitle = ctk.CTkLabel(
            bar, text="海量日志压缩投喂大模型 · 快速故障排查",
            font=ctk.CTkFont(size=12))
        subtitle.grid(row=0, column=2, padx=6, sticky="w")
        self._muted_labels.append(subtitle)
        # 修复缺陷R13（第三轮#7）：主题切换改为下拉选择 —— 可直接选择
        # 任意主题（无需循环点击）；下拉列表只列其他三态（当前项不重复
        # 出现），选中即触发原四态切换逻辑（调色板批量刷新）。
        # 修复缺陷R14：选择框为复合控件（图标列固定宽 + 主题名 + ▼），
        # 自定义弹窗两列布局 —— emoji 宽度不一不再影响文字对齐。
        # 修复缺陷R15：主题名居中 —— 中间列弹性（weight=1）+ 文字
        # anchor="center"，字样始终位于左侧图标与右侧▼箭头的正中间。
        self._theme_box = ctk.CTkFrame(bar, corner_radius=6, width=120,
                                        cursor="hand2")
        self._theme_box.grid(row=0, column=3, padx=(6, 16))
        self._theme_box.grid_columnconfigure(1, weight=1)  # 中间弹性列
        p = self._palette()
        self._theme_box_icon = ctk.CTkLabel(
            self._theme_box, text=p["icon"],
            width=self._measure_theme_icon_col(), anchor="center")
        # 修复缺陷R16（续）：图标列左距 10 逻辑 px 与弹窗行对齐 ——
        # 弹窗行 padx=2 + 图标 padx=8 = 10，此前按钮为 8，导致按钮
        # 图标列比弹窗图标列左偏 2 逻辑 px（200% DPI 下 4 物理 px，
        # 太阳/月亮可见中心随之偏左），两者左缘同起点后完全重合
        self._theme_box_icon.grid(row=0, column=0, padx=(10, 0), pady=(3, 3))
        self._theme_box_name = ctk.CTkLabel(
            self._theme_box, text=p["label"], anchor="center")
        self._theme_box_name.grid(row=0, column=1, sticky="ew",
                                  padx=(4, 4))
        self._theme_box_arrow = ctk.CTkLabel(
            self._theme_box, text="▼", anchor="center")
        self._theme_box_arrow.grid(row=0, column=2, padx=(0, 8))
        # 点击选择框任意区域（含图标/文字/箭头）弹出下拉
        for w in (self._theme_box, self._theme_box_icon,
                  self._theme_box_name, self._theme_box_arrow):
            w.bind("<Button-1>", self._on_theme_box_click)
        self._build_theme_popup()

    # ------------------------------------------------------------------
    def _build_tabs(self) -> None:
        # 修复缺陷R21：初始高度按当前页（文件导入），切换时适配
        self._tabview = ctk.CTkTabview(
            self, height=_TAB_PAGE_HEIGHTS["文件导入"],
            command=self._fit_tab_height)
        # 修复缺陷R9：顶部区域压缩（结果区获得更大高度）
        self._tabview.grid(row=1, column=0, sticky="ew", padx=10, pady=(3, 1))
        for name in ("文件导入", "文本粘贴", "多文件对比"):
            self._tabview.add(name)
        self._build_file_tab()
        self._build_text_tab()
        self._build_compare_tab()
        # 修复缺陷R21（续）：CTkBaseClass 不关闭 grid 传播，容器请求
        # 高度被最高页内容（文本粘贴框）撑大，显式 height 被忽略，
        # 文件导入页仍留大片空白 —— 建完全部页后关闭传播，显式
        # height 才生效（_set_dimensions 会同步 tk.Frame height 选项）
        self._tabview.grid_propagate(False)
        # 优化缺陷R100：分析历史入口（页签行右侧空地，place 叠于
        # tabview 页头空白区，不挤压三页签）
        hist_btn = ctk.CTkButton(self._tabview, text="🕘 历史", width=76,
                                 height=26,
                                 command=self._open_history_popup)
        hist_btn.place(relx=1.0, rely=0, anchor="ne", x=-8, y=5)
        self._history_btn = hist_btn
        self._accent_buttons.append((hist_btn, "accent"))

    def _fit_tab_height(self) -> None:
        """tab 切换：容器高度按当前页内容紧凑适配（修复缺陷R21）。

        CTkTabview 容器高度 = 全部页请求最大值（文本粘贴框撑大），
        文件导入页因此保留大片空白。切换时按页预设值显式设定
        height（显式值覆盖内容请求），文件导入页只留一行输入框
        高度，结果区随之上移显示更多内容。
        """
        h = _TAB_PAGE_HEIGHTS.get(self._tabview.get())
        if h:
            self._tabview.configure(height=h)

    def _build_file_tab(self) -> None:
        tab = self._tabview.tab("文件导入")
        tab.grid_columnconfigure(0, weight=1)
        hint_text = ("选择或将日志文件拖入窗口任意位置（支持超大文件、"
                     ".gz/.zip 直读、UTF-8/GBK 自动适配；选择文件时"
                     "可多选轮转日志合并分析）")
        if not _HAS_DND:
            hint_text += "  |  拖拽未启用：pip install tkinterdnd2 后重启"
        hint = ctk.CTkLabel(tab, text=hint_text)
        hint.grid(row=0, column=0, sticky="w", pady=(2, 4))
        self._muted_labels.append(hint)
        self._file_entry = ctk.CTkEntry(tab, placeholder_text="日志文件路径…")
        self._file_entry.grid(row=1, column=0, sticky="ew", padx=(0, 6))
        browse = ctk.CTkButton(tab, text="选择文件", width=90,
                               command=self._browse_file)
        browse.grid(row=1, column=1)
        self._accent_buttons.append((browse, "accent"))

    def _build_text_tab(self) -> None:
        tab = self._tabview.tab("文本粘贴")
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)
        hint = ctk.CTkLabel(
            tab, text="直接粘贴日志片段（适合几万行以内的快速排查，无需存为文件）")
        hint.grid(row=0, column=0, sticky="w", pady=(2, 4))
        self._muted_labels.append(hint)
        # 修复缺陷#11：undo=False —— 大段粘贴时 undo 栈与 autoseparator
        # 会随粘贴体量膨胀（几万行日志可达数百 MB），关闭撤销换取内存稳定
        self._paste_box = ctk.CTkTextbox(tab, height=100, undo=False,
                                         font=ctk.CTkFont(family="Consolas",
                                                          size=12))
        self._paste_box.grid(row=1, column=0, sticky="ew")
        self._bg_widgets.append((self._paste_box, "card"))

    def _build_compare_tab(self) -> None:
        tab = self._tabview.tab("多文件对比")
        tab.grid_columnconfigure(1, weight=1)
        cmp_hint = ctk.CTkLabel(
            tab, text="2~3 个日志文件对比（第一个为基准；适配版本对比 / 修复前后对比）")
        cmp_hint.grid(row=0, column=0, columnspan=3, sticky="w", pady=(2, 6))
        self._muted_labels.append(cmp_hint)
        self._compare_entries = []
        for i, label in enumerate(("基准文件 A", "对比文件 B", "对比文件 C（可选）")):
            ctk.CTkLabel(tab, text=label, width=110, anchor="w").grid(
                row=i + 1, column=0, padx=(0, 6), pady=3, sticky="w")
            entry = ctk.CTkEntry(tab, placeholder_text="日志文件路径…")
            entry.grid(row=i + 1, column=1, sticky="ew", padx=(0, 6))
            btn = ctk.CTkButton(tab, text="选择", width=64,
                                command=lambda e=entry: self._browse_file(e))
            btn.grid(row=i + 1, column=2)
            self._accent_buttons.append((btn, "accent"))
            self._compare_entries.append(entry)

    # ------------------------------------------------------------------
    def _build_config_panel(self) -> None:
        panel = ctk.CTkFrame(self)
        # 修复缺陷R9：顶部区域压缩（pady 4→3）
        panel.grid(row=2, column=0, sticky="ew", padx=10, pady=3)
        self._bg_widgets.append((panel, "card"))
        # 修复缺陷R72：搜索组迁出至独立「实时筛选行」—— 本行只剩
        # 「按开始分析才生效」的控件（级别/上下文行数/关键词黑白名单），
        # 整行请求宽远低于可用宽，超宽切边问题根除；
        # 修复缺陷R81：原列 5 弹性空白列废止 —— 它吸收窗口余量导致
        # ⓘ→上下文行数 区间随窗口漂移、与其余两区间（12/18）恒不等；
        # 三个组间区间改由各组首标签静态 padx 补偿实现（见下）；
        # 优化缺陷R93：智能分析/解析规则迁至高级选项行，关键词黑白
        # 名单自 ⚙ 弹层迁回本行（用户审美换位）

        ctk.CTkLabel(panel, text="级别过滤", font=ctk.CTkFont(weight="bold")
                     ).grid(row=0, column=0, padx=(12, 4), sticky="w")
        # 修复缺陷R19：FATAL 复选框已删除（始终放行显示，见 R19 注释）
        # 优化：五个级别复选框左侧各挂一个 ⓘ 悬停说明
        # （样式统一 #3B82F6 + 手型光标 + 悬停加深，垂直与复选框居中对齐）
        self._level_vars: Dict[str, tk.BooleanVar] = {}
        self._level_tooltips: Dict[str, Tooltip] = {}
        level_box = ctk.CTkFrame(panel, fg_color="transparent")
        # 优化缺陷R48：复选框容器只占列 1（原跨列 1~2 时区间A=2E/6、
        # 区间B=E/6 恒不等）；搜索框组跨列 2~4 后区间A=区间B=E/6，
        # 任意窗口宽度两区间恒等距
        level_box.grid(row=0, column=1, sticky="w")
        col = 0
        for level in LEVEL_CHECKS:
            # 默认勾选 ERROR/FAIL（DEFAULT_SELECTED_LEVELS）
            var = tk.BooleanVar(value=level in DEFAULT_SELECTED_LEVELS)
            self._level_vars[level] = var
            # 优化：每个级别复选框左侧 ⓘ（与详情面板 ⓘ 同款蓝色）
            info = ctk.CTkLabel(
                level_box, text="ⓘ", text_color="#3B82F6",
                font=ctk.CTkFont(size=13, weight="bold"),
                cursor="hand2")
            # 优化缺陷R52：ⓘ 左 padx 5→3、右 2→1 —— 复选框组左移收紧
            info.grid(row=0, column=col, padx=(3, 1), sticky="e")
            info.bind("<Enter>", lambda e, w=info: w.configure(
                text_color="#2563EB"))
            info.bind("<Leave>", lambda e, w=info: w.configure(
                text_color="#3B82F6"))
            col += 1
            # 优化缺陷R49：末位复选框右 padx 6→0 —— 拖尾间距会使
            # DEBUG 右侧区间比搜索框右侧区间多出 12px（2x DPI），
            # 破坏两区间严格等距；间隔统一由固定间隔列提供
            # 优化缺陷R52：复选框 padx (2,6)→(1,4) —— 组区左移收紧
            # 修复缺陷R74：复选框按各自文本实测宽紧凑定宽 ——
            # CTkCheckBox 默认 width=100 是固定值（不随文本自适应），
            # 统一 68 又让短文本框尾部余量偏大，五个可视区间长短
            # 不一；紧凑化后各框尾部余量一致（+4 逻辑 px），可视
            # 区间天然相等。28 = 复选框 18 + 内部间隔 6 + 尾部 4
            cb_pad_r = 0 if level == LEVEL_CHECKS[-1] else 4
            cb = ctk.CTkCheckBox(level_box, text=level, variable=var,
                                 checkbox_width=18, checkbox_height=18)
            _tw = tkfont.Font(
                font=cb._text_label.cget("font")).measure(level)
            _scale = max(1.0, getattr(self, "_font_scale", 1.0))
            cb.configure(width=int(_tw / _scale + 0.999) + 28)
            cb.grid(row=0, column=col, padx=(1, cb_pad_r), sticky="w")
            col += 1
            self._level_tooltips[level] = Tooltip(
                info, lambda lv=level: _LEVEL_HELP[lv])

        # 修复缺陷R44：上下文行数输入框 —— 紧随级别组（用户审美换位：
        # 原智能分析组 ⇄ 本组）；修复缺陷R81：组首左 padx 19 静态等距
        # （复选框紧凑宽有效尾距 ≈5 逻辑 px 补偿，19+5=24 与其余组首同）
        ctk.CTkLabel(panel, text="上下文行数").grid(
            row=0, column=2, padx=(19, 2), sticky="w")
        self._ctx_entry = ctk.CTkEntry(panel, width=60)
        self._ctx_entry.insert(0, str(DEFAULT_CONTEXT_LINES))
        self._ctx_entry.grid(row=0, column=3, padx=(2, 0), sticky="w")

        # 优化缺陷R93：关键词黑白名单紧随上下文行数（用户审美：自 ⚙
        # 弹层迁回主行，智能分析/解析规则对调迁至高级选项行换位）；
        # 组首左 padx 24 静态等距（同 R81 补偿体系）
        # 包含=白名单任一命中保留，排除=黑名单任一命中剔除；
        # 匹配范围 模块+消息+堆栈（默认小写子串，⚙ 弹层可切正则）
        # 优化缺陷R94：两框弹性拉伸填满行尾剩余宽（uniform 均分）——
        # 定宽 140 占位文本被裁切且行尾大片空白；弹性列在全部组首
        # 之后，三个组间区间（R81）不受拉伸影响
        ctk.CTkLabel(panel, text="包含关键词").grid(
            row=0, column=4, padx=(24, 2), sticky="w")
        self._include_entry = ctk.CTkEntry(
            panel, width=140, placeholder_text="逗号/空格分隔，留空不限")
        self._include_entry.grid(row=0, column=5, padx=(2, 0),
                                 sticky="ew")
        ctk.CTkLabel(panel, text="排除关键词").grid(
            row=0, column=6, padx=(24, 2), sticky="w")
        self._exclude_entry = ctk.CTkEntry(
            panel, width=140, placeholder_text="逗号/空格分隔，留空不限")
        # 行尾余量：末列右 padx 12（与原规则 ⓘ 行尾余量同义）
        self._exclude_entry.grid(row=0, column=7, padx=(2, 12),
                                 sticky="ew")
        panel.grid_columnconfigure(5, weight=1, uniform="kw")
        panel.grid_columnconfigure(7, weight=1, uniform="kw")

        # 优化缺陷R93：智能分析/解析规则两选组已迁至高级选项行
        # （与关键词黑白名单换位，见 _build_advanced_panel）

    # ------------------------------------------------------------------
    # 优化缺陷R85：高级选项行（时间范围 / 行数上限 / 其他选项⚙）——
    # 全部「开始分析」时生效的前置设置
    # ------------------------------------------------------------------
    def _build_advanced_panel(self) -> None:
        """高级选项行：低频前置过滤（主流日志工具的第一前置项组）。

        优化缺陷R85：时间范围过滤（①）+ 行数上限采样（④）；「其他
        选项 ⚙」从过滤行行尾迁入本行行尾（用户审美），弹层机制不变。
        优化缺陷R93：智能分析/解析规则两选组自过滤行迁入本行（与
        关键词黑白名单换位，用户审美），本行现为 时间范围/行数上限/
        智能分析/解析规则/其他选项⚙ 五组。
        本行各项均不持久化（数据类过滤器：残留旧值会静默隐藏未来
        分析的错误，重启清零；生效时状态栏常驻标签兜底提示）。
        """
        panel = ctk.CTkFrame(self)
        # 修复缺陷R109：行面板 pady 3→1（三行省 12px 补底栏完成串
        # 行高，页签行 126 恢复后文件输入框不再被裁）
        panel.grid(row=3, column=0, sticky="ew", padx=10, pady=1)
        self._bg_widgets.append((panel, "card"))
        self._advanced_panel = panel

        ctk.CTkLabel(panel, text="时间范围").grid(
            row=0, column=0, padx=(12, 4), sticky="w")
        self._time_start_entry = ctk.CTkEntry(
            panel, width=110, placeholder_text="开始 如 14:00:00")
        self._time_start_entry.grid(row=0, column=1, padx=(2, 0),
                                    sticky="w")
        ctk.CTkLabel(panel, text="~").grid(row=0, column=2, padx=2)
        self._time_end_entry = ctk.CTkEntry(
            panel, width=110, placeholder_text="结束 如 14:05:30")
        self._time_end_entry.grid(row=0, column=3, padx=(2, 0), sticky="w")
        time_help = ctk.CTkLabel(
            panel, text="ⓘ", text_color="#4dd0e1",
            font=ctk.CTkFont(size=13, weight="bold"), cursor="question_arrow")
        time_help.grid(row=0, column=4, padx=(4, 0), sticky="w")
        Tooltip(time_help, lambda: (
            "只分析时间窗内的日志行（任一端可留空 = 不限）。\n"
            "格式一：HH:MM:SS（按每日时段过滤，单日日志最常用）\n"
            "格式二：YYYY-MM-DD HH:MM:SS（精确区间，跨天日志用）\n"
            "无时间戳的行始终保留（无法定位，宁留勿丢）"))

        # 行数上限采样（①④⑤之④）
        ctk.CTkLabel(panel, text="行数上限").grid(
            row=0, column=5, padx=(24, 2), sticky="w")
        self._maxlines_key = "all"
        self._maxlines_menu = ctk.CTkOptionMenu(
            panel, width=130, dynamic_resizing=False,
            values=[MAXLINES_DISPLAY[k] for k in MAXLINES_KEYS
                    if k != "all"],
            command=self._on_maxlines_changed)
        self._maxlines_menu.set(MAXLINES_DISPLAY["all"])
        self._maxlines_menu.grid(row=0, column=6, padx=(2, 0), sticky="w")
        maxlines_help = ctk.CTkLabel(
            panel, text="ⓘ", text_color="#4dd0e1",
            font=ctk.CTkFont(size=13, weight="bold"), cursor="question_arrow")
        maxlines_help.grid(row=0, column=7, padx=(4, 0), sticky="w")
        Tooltip(maxlines_help, lambda:
                MAXLINES_DESCRIPTIONS.get(self._maxlines_key, ""))

        # 优化缺陷R93：智能分析/解析规则两选组自过滤行迁入（与关键词
        # 黑白名单换位，用户审美），控件/交互/持久化逻辑原样搬移
        ctk.CTkLabel(panel, text="智能分析").grid(
            row=0, column=8, padx=(24, 2), sticky="w")
        self._analyze_key = "full"
        # 修复缺陷R82：dynamic_resizing=False + 定宽 150（切档不伸缩）；
        # 150 按最长选项「完整分析（推荐）」（8 全角字符+箭头）定
        self._analyze_menu = ctk.CTkOptionMenu(
            panel, width=150, dynamic_resizing=False,
            values=[ANALYZE_DISPLAY[k] for k in ANALYZE_KEYS
                    if k != "full"],
            command=self._on_analyze_changed)
        self._analyze_menu.set(ANALYZE_DISPLAY["full"])
        self._analyze_menu.grid(row=0, column=9, padx=(2, 0), sticky="w")
        analyze_help = ctk.CTkLabel(
            panel, text="ⓘ", text_color="#4dd0e1",
            font=ctk.CTkFont(size=13, weight="bold"), cursor="question_arrow")
        analyze_help.grid(row=0, column=10, padx=(4, 0), sticky="w")
        self._analyze_help_tooltip = Tooltip(
            analyze_help,
            lambda: (ANALYZE_DESCRIPTIONS.get(self._analyze_key, "")
                     + "\n" + _ANOMALY_LEGEND))

        # 优化缺陷R71：下拉显示中文名，默认「自动识别（推荐）」；
        # 当前选中项不出现在下拉列表（与主题选择器同款交互）
        ctk.CTkLabel(panel, text="解析规则").grid(
            row=0, column=11, padx=(24, 2), sticky="w")
        self._rule_key = "auto"
        self._rule_menu = ctk.CTkOptionMenu(
            panel, width=150, dynamic_resizing=False,
            values=[RULE_DISPLAY[k] for k in RULE_KEYS if k != "auto"],
            command=self._on_rule_changed)
        self._rule_menu.set(RULE_DISPLAY["auto"])
        self._rule_menu.grid(row=0, column=12, padx=(2, 0), sticky="w")
        # 修复缺陷#8：解析规则悬停说明（跟随当前选中规则动态变化）
        rule_help = ctk.CTkLabel(
            panel, text="ⓘ", text_color="#4dd0e1",
            font=ctk.CTkFont(size=13, weight="bold"), cursor="question_arrow")
        rule_help.grid(row=0, column=13, padx=(4, 0), sticky="w")
        self._rule_help_tooltip = Tooltip(
            rule_help,
            lambda: RULE_DESCRIPTIONS.get(self._rule_key, ""))

        # 「其他选项 ⚙」行尾（弹层机制不变；R93 换位后列 14/15）
        ctk.CTkLabel(panel, text="其他选项").grid(
            row=0, column=14, padx=(24, 2), sticky="w")
        self._settings_btn = ctk.CTkButton(
            panel, text="⚙", width=36,
            command=self._toggle_settings_popup)
        self._settings_btn.grid(row=0, column=15, padx=(2, 0), sticky="w")
        # 优化缺陷R95：↺ 重置按钮（⚙ 右侧）—— 一键清空数据类前置
        # 设置并恢复选择器默认（时间范围/关键词残留会静默隐藏错误，
        # 状态栏标签虽兜底但不直观；留空不限回归一键可达）
        self._reset_btn = ctk.CTkButton(
            panel, text="↺ 重置", width=64,
            command=self._reset_advanced_options)
        self._reset_btn.grid(row=0, column=16, padx=(6, 12), sticky="w")
        # 优化缺陷R98：四个 ⓘ 列均分行尾剩余宽（uniform 同组）——
        # 四个组间可视区间恒等（基线 padx 24 + 等份余量），且重置
        # 按钮被顶到行尾、右缘与上排排除关键词输入框右缘对齐
        # （两面板同 padx=10 sticky=ew，右边距同 12 → 同一垂线）
        for _col in (4, 7, 10, 13):
            panel.grid_columnconfigure(_col, weight=1, uniform="adv_gap")
        Tooltip(self._reset_btn, lambda: (
            "重置前置设置为默认：\n"
            "清空 时间范围 / 包含·排除关键词\n"
            "行数上限 → 全部（推荐）\n"
            "智能分析 → 完整分析（推荐）\n"
            "解析规则 → 自动识别（推荐）\n"
            "编码 → 自动探测（推荐），关键词正则 → 关\n"
            "（级别勾选/上下文行数/相似度/脱敏为偏好项，不动）"))
        self._build_settings_popup()

    def _reset_advanced_options(self) -> None:
        """↺ 重置：前置设置一键回默认（优化缺陷R95）。

        只动「开始分析才生效」的数据类前置项；级别勾选/上下文行数/
        相似度/脱敏属用户偏好（持久化），不在重置范围。选择器复用
        各 _on_*_changed 回调（键更新 + 选中项回列表 + 状态栏说明），
        最后统一覆盖状态栏为重置回执。
        """
        for entry in (self._time_start_entry, self._time_end_entry,
                      self._include_entry, self._exclude_entry):
            entry.delete(0, "end")
        self._use_regex_var.set(False)
        self._on_maxlines_changed(MAXLINES_DISPLAY["all"])
        self._on_analyze_changed(ANALYZE_DISPLAY["full"])
        self._on_rule_changed(RULE_DISPLAY["auto"])
        self._on_encoding_changed(ENCODING_DISPLAY[0])
        self._status_label.configure(
            text="前置设置已重置：时间范围/关键词已清空，其余回默认")

    def _on_maxlines_changed(self, choice: str) -> None:
        """行数上限档位切换（优化缺陷R85）：全部/10万/50万/100万。

        与相似度选择器同款交互：选中项从下拉列表移除；状态栏即时
        展示该档说明。上限在下一次「开始分析」生效。
        """
        key = _MAXLINES_BY_DISPLAY.get(choice)
        if key is None:
            return
        self._maxlines_key = key
        self._maxlines_menu.set(MAXLINES_DISPLAY[key])
        self._maxlines_menu.configure(
            values=[MAXLINES_DISPLAY[k] for k in MAXLINES_KEYS
                    if k != key])
        desc = MAXLINES_DESCRIPTIONS.get(key)
        if desc:
            self._status_label.configure(
                text=f"行数上限 {MAXLINES_DISPLAY[key]}：{desc}")

    # ------------------------------------------------------------------
    # 修复缺陷R72：实时筛选行（搜索组从级别过滤行迁出）
    # ------------------------------------------------------------------
    def _build_search_panel(self) -> None:
        """实时筛选行：输入即时过滤列表显示，无需按「开始分析」。

        修复缺陷R72：级别过滤行的其余控件（级别/上下文行数/解析
        规则）都要按「开始分析」才生效，唯一实时生效的搜索组混在
        其中语义不符，且整行超宽时计数框被切边 —— 迁出至过滤行与
        按钮行之间的独立行（此处曾是已删除的包含/排除关键字行），
        输入框顺势加宽 90→200，并补 ▲/▼ 导航按钮（与 Enter /
        Shift+Enter 同语义），搜索行为（防抖过滤/导航/计数显隐）
        完全不变。
        """
        panel = ctk.CTkFrame(self)
        # 优化缺陷R85：高级选项行插入后行号 3→4
        # 修复缺陷R109：行面板 pady 3→1（同高级行，补底栏完成串行高）
        panel.grid(row=4, column=0, sticky="ew", padx=10, pady=1)
        self._bg_widgets.append((panel, "card"))

        ctk.CTkLabel(panel, text="搜索").grid(row=0, column=0,
                                              padx=(12, 4), pady=4)
        self._search_var = tk.StringVar()
        # 优化缺陷R45：结果搜索 —— 即时过滤左侧错误分类列表的
        # 【显示】（摘要/模块/级别/优先级档匹配，不触发重新分析）
        self._search_entry = ctk.CTkEntry(
            panel, textvariable=self._search_var, width=200,
            placeholder_text="按摘要/模块/级别过滤，支持 and/or/not…")
        self._search_entry.grid(row=0, column=1, pady=4, sticky="w")
        # 修复缺陷R73：▲/▼ 导航按钮与计数框换位 —— 按钮紧随输入框
        # （与 Enter（下一个）/ Shift+Enter（上一个）同语义），计数
        # 框退居行尾：其宽度随文本自适应变化时只影响行尾空白，不
        # 再挤压任何控件
        self._search_prev_btn = ctk.CTkButton(
            panel, text="▲", width=28, height=24,
            command=lambda: self._on_search_enter(False))
        self._search_prev_btn.grid(row=0, column=2, padx=(8, 2))
        self._search_next_btn = ctk.CTkButton(
            panel, text="▼", width=28, height=24,
            command=lambda: self._on_search_enter(True))
        self._search_next_btn.grid(row=0, column=3, padx=(2, 4))
        self._accent_buttons.append((self._search_prev_btn, "accent"))
        self._accent_buttons.append((self._search_next_btn, "accent"))
        # 修复缺陷R73：计数框宽度随文本自适应 —— 取消固定 88px 与
        # grid_propagate(False)，错误种类多时数字变长（如 12/135 条）
        # 框体同步变宽，不裁切不顶出；无计数时透明收缩隐形
        # （显隐仍零布局影响：行尾空白吸收）
        self._search_count_box = ctk.CTkFrame(
            panel, corner_radius=6, fg_color="transparent")
        self._search_count_box.grid(row=0, column=4, padx=(4, 12))
        self._search_count = ctk.CTkLabel(
            self._search_count_box, text="", text_color="#8fa4b8",
            fg_color="transparent")
        self._search_count.grid(row=0, column=0, padx=8, pady=1)
        # trace 不依赖键盘事件（粘贴/清空/程序赋值均可靠触发）
        self._search_var.trace_add("write", self._on_search_changed)
        # 优化缺陷R46：Enter 跳下一个匹配 / Shift+Enter 跳上一个
        # （CTkEntry.bind 转发到内部 tk.Entry，键盘事件可可靠触发）
        self._search_entry.bind(
            "<Return>", lambda e: self._on_search_enter(True))
        self._search_entry.bind(
            "<Shift-Return>", lambda e: self._on_search_enter(False))

    def _build_action_panel(self) -> None:
        panel = ctk.CTkFrame(self)
        # 修复缺陷R9：顶部区域压缩（pady 4→3，按钮高度 34→30）
        # 修复缺陷R72：实时筛选行插入后行号 3→4
        # 优化缺陷R85：高级选项行插入后行号 4→5
        # 修复缺陷R109：行面板 pady 3→1（补底栏完成串行高）
        panel.grid(row=5, column=0, sticky="ew", padx=10, pady=1)
        self._bg_widgets.append((panel, "card"))
        for col in range(7):
            panel.grid_columnconfigure(col, weight=1)

        self._start_btn = ctk.CTkButton(
            panel, text="开始分析", font=ctk.CTkFont(size=14, weight="bold"),
            height=30, command=self._on_start)
        # 优化缺陷R105：按钮行 pady 6→3（底栏新增完成串行占 22px，
        # 从此处与页签行 reclaim，保列表可视行数 ≥6）
        self._start_btn.grid(row=0, column=0, padx=6, pady=3, sticky="ew")
        self._cancel_btn = ctk.CTkButton(
            panel, text="取消", state="disabled", fg_color="#7b3535",
            hover_color="#94424a", command=self._on_cancel, height=30)
        self._cancel_btn.grid(row=0, column=1, padx=6, sticky="ew")
        self._export_btn = ctk.CTkButton(panel, text="导出报告",
                                         state="disabled", height=30,
                                         command=self._on_export)
        self._export_btn.grid(row=0, column=2, padx=6, sticky="ew")
        self._copy_btn = ctk.CTkButton(panel, text="复制摘要", state="disabled",
                                       height=30, command=self._on_copy_summary)
        self._copy_btn.grid(row=0, column=3, padx=6, sticky="ew")
        self._chart_btn = ctk.CTkButton(panel, text="统计图表", state="disabled",
                                        height=30, command=self._show_charts)
        self._chart_btn.grid(row=0, column=4, padx=6, sticky="ew")
        # 优化缺陷R105：实时 Tail 监控（第六颗按钮，盯文件尾部增量）
        self._tail_btn = ctk.CTkButton(panel, text="📡 实时监控", height=30,
                                       command=self._on_tail_toggle)
        self._tail_btn.grid(row=0, column=5, padx=6, sticky="ew")
        self._accent_buttons.append((self._start_btn, "accent"))
        self._accent_buttons.append((self._export_btn, "accent"))
        self._accent_buttons.append((self._copy_btn, "accent"))
        self._accent_buttons.append((self._chart_btn, "accent"))
        self._accent_buttons.append((self._tail_btn, "accent"))
        self._accent_buttons.append((self._cancel_btn, "danger"))

        # 优化缺陷R105：「完成」状态串下沉底栏（按钮行第六颗按钮
        # 占位后进度区只剩一列，完成串改在底栏整行展示）
        progress_frame = ctk.CTkFrame(panel, fg_color="transparent")
        progress_frame.grid(row=0, column=6, padx=10, sticky="ew")
        progress_frame.grid_columnconfigure(0, weight=1)
        self._progress_label = ctk.CTkLabel(progress_frame, text="就绪",
                                            anchor="w")
        self._progress_label.grid(row=0, column=0, sticky="ew")
        self._muted_labels.append(self._progress_label)
        self._progress_bar = ctk.CTkProgressBar(progress_frame,
                                                progress_color="#3B82F6")
        self._progress_bar.grid(row=1, column=0, sticky="ew", pady=(2, 4))
        self._progress_bar.set(0)

    def _build_result_panel(self) -> None:
        panel = ctk.CTkFrame(self)
        # 修复缺陷R9：结果区上下留白压缩（列表/详情获得更大高度）
        # 修复缺陷R72：实时筛选行插入后行号 4→5
        # 优化缺陷R85：高级选项行插入后行号 5→6
        panel.grid(row=6, column=0, sticky="nsew", padx=10, pady=(2, 2))
        self._bg_widgets.append((panel, "card"))
        # 修复缺陷R12：左右分栏改为 place 比例布局 —— panel 内三个并列
        # 子部件（列表列 / 分隔条 / 详情列），宽度由 _splitter_ratio
        # 精确控制（grid weight 无法保证比例精确且受内容请求宽度干扰）。
        self._result_panel = panel
        self._list_col = ctk.CTkFrame(panel, fg_color="transparent")
        self._detail_col = ctk.CTkFrame(panel, fg_color="transparent")
        for col in (self._list_col, self._detail_col):
            col.grid_columnconfigure(0, weight=1)
            col.grid_rowconfigure(1, weight=1)
        self._build_splitter(panel)
        # 窗口缩放（最大化/还原/手动调整）时按比例重排（最小宽度钳制）
        panel.bind("<Configure>", lambda e: self._layout_splitter())
        self._layout_splitter()

        # 修复缺陷#7：列表 / 详情标题行增加「全屏」按钮（独立最大化窗口）
        # 修复缺陷R11：字体大小选择器移入标题栏（标题 → 字体大小 → 全屏）
        list_head = ctk.CTkFrame(self._list_col, fg_color="transparent")
        # 修复缺陷R12：留存标题栏引用（动态最小宽度实测依据）
        self._list_head = list_head
        list_head.grid(row=0, column=0, padx=10, pady=(4, 2), sticky="ew")
        list_head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(list_head, text="错误分类列表（按优先级降序）",
                     font=ctk.CTkFont(size=13, weight="bold")).grid(
            row=0, column=0, sticky="w")
        # 修复缺陷R14：标题栏右侧控件组（字体大小+选择器+全屏）装进
        # 独立容器并【常驻 panel】（place 贴左列右缘，_layout_splitter
        # 统一跟随）—— 正常态/拖动态同一跟随机制：分隔条拖动时容器
        # 纯移动（SetWindowPos BitBlt，零重绘零失真），真实控件而非
        # 代理近似。Tk place -in 不允许跨容器挂接（实测 TclError:
        # can't place ... relative to ...），故容器直接创建为 panel
        # 子，从机制上避免取出/放回。
        ctrl_box = ctk.CTkFrame(panel, fg_color="transparent")
        self._list_ctrl_box = ctrl_box
        # 修复缺陷R11：「字体大小」选择器（小/中/大/特大档，控制列表字号）
        self._font_label = ctk.CTkLabel(ctrl_box, text="字体大小",
                                        font=ctk.CTkFont(size=12))
        self._font_label.grid(row=0, column=0, padx=(0, 2), sticky="e")
        self._muted_labels.append(self._font_label)
        self._font_menu = ctk.CTkOptionMenu(
            ctrl_box, values=list(FONT_SIZE_OPTIONS), width=80, height=26,
            command=self._apply_font_size)
        self._font_menu.set(self._font_size)
        self._font_menu.grid(row=0, column=1, padx=(0, 6), sticky="e")
        # 优化缺陷R111：已屏蔽计数指示器（>0 才显示；点击切换
        # 显示/隐藏已屏蔽簇）
        self._mute_ind_btn = ctk.CTkButton(
            ctrl_box, text="🚫 0", width=64, height=26,
            fg_color="#6b7280", hover_color="#4b5563",
            command=self._toggle_show_muted)
        self._mute_ind_tooltip = Tooltip(
            self._mute_ind_btn, lambda: (
                f"已屏蔽 {len(self._muted)} 类已知错误"
                + ("（当前显示，点击隐藏）" if self._show_muted
                   else "（当前隐藏，点击显示）")
                + "\n在详情面板点「🚫 屏蔽」可屏蔽/恢复某一类"))
        self._list_fs_btn = ctk.CTkButton(ctrl_box, text="⛶ 全屏", width=84,
                                          height=26,
                                          command=self._open_list_fullscreen)
        self._list_fs_btn.grid(row=0, column=3, padx=(0, 0), sticky="e")
        self._accent_buttons.append((self._list_fs_btn, "accent"))

        # 修复缺陷#6：「典型样例」术语加悬停说明（ⓘ 图标触发）
        detail_head = ctk.CTkFrame(self._detail_col, fg_color="transparent")
        # 修复缺陷R12：留存标题栏引用（动态最小宽度实测依据）
        self._detail_head = detail_head
        detail_head.grid(row=0, column=0, padx=10, pady=(4, 2), sticky="ew")
        detail_head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(detail_head, text="详情（典型样例 · 上下文 · 降噪堆栈）",
                     font=ctk.CTkFont(size=13, weight="bold")).grid(
            row=0, column=0, sticky="w")
        sample_help = ctk.CTkLabel(
            detail_head, text="ⓘ", text_color="#3B82F6",
            font=ctk.CTkFont(size=14, weight="bold"), cursor="question_arrow")
        sample_help.grid(row=0, column=1, padx=(4, 0), sticky="w")
        self._sample_help_tooltip = Tooltip(
            sample_help,
            "该错误类型的代表性日志样例，包含完整的错误信息、堆栈跟踪"
            "和前后上下文，用于快速定位问题")
        self._detail_fs_btn = ctk.CTkButton(detail_head, text="⛶ 全屏", width=84,
                                            height=26,
                                            command=self._open_detail_fullscreen)
        self._detail_fs_btn.grid(row=0, column=3, padx=(6, 0), sticky="e")
        self._accent_buttons.append((self._detail_fs_btn, "accent"))
        # 优化缺陷R111：屏蔽当前簇按钮（状态随详情簇刷新）
        self._mute_btn = ctk.CTkButton(
            detail_head, text="🚫 屏蔽", width=84, height=26,
            command=self._toggle_mute_current)
        self._mute_btn.grid(row=0, column=2, padx=(6, 0), sticky="e")
        self._mute_btn_tooltip = Tooltip(
            self._mute_btn, lambda: (
                "屏蔽后该类错误默认从列表隐藏（配置持久化，重启仍生效），"
                "列表上方 🚫 计数按钮可临时显示/恢复"))
        self._mute_btn.configure(state="disabled")   # 未选簇时置灰

        # 修复缺陷R9：列表宿主容器（经典滚动 / 虚拟滚动两模式切换）
        self._list_host = ctk.CTkFrame(self._list_col, fg_color="transparent")
        # 修复缺陷R7：宿主四向拉伸（sticky 补 e）+ 右边距 10 与
        # 全屏按钮右缘严格对齐，列表占满左列全部可用宽度（>90%）
        # 修复缺陷R9：pady 压缩（2,8→2,4），列表可视高度更大
        self._list_host.grid(row=1, column=0, sticky="nsew", padx=(10, 10),
                             pady=(2, 4))
        self._list_host.grid_columnconfigure(0, weight=1)
        self._list_host.grid_rowconfigure(0, weight=1)
        self._bg_widgets.append((self._list_host, "window"))
        # 修复缺陷R7：去除固定 width=470（固定宽度导致右侧大量留白）
        self._cluster_list = ctk.CTkScrollableFrame(self._list_host)
        self._cluster_list.grid(row=0, column=0, sticky="nsew")
        # 修复缺陷R9：水平滚动条（摘要单行不换行，左右滑动查看长摘要）
        self._list_hbar = self._make_hscroll(self._cluster_list)
        self._list_hbar.grid(row=1, column=0, sticky="ew")
        # 修复缺陷R5：详情字体放大到 13（摘要/堆栈/上下文更易读）
        self._detail_box = ctk.CTkTextbox(
            self._detail_col, font=ctk.CTkFont(family="Consolas", size=13),
            wrap="none")
        self._detail_box.grid(row=1, column=0, sticky="nsew", padx=(4, 10),
                              pady=(2, 8))
        self._bg_widgets.append((self._detail_box, "card"))
        self._setup_detail_tags()
        # 修复缺陷R14：标题栏控件组（常驻 panel）初始定位 —— 构建期
        # 1366 行 _layout_splitter 调用时控件组尚未创建；面板首次
        # <Configure> 也会纠正，此处兜底保证首帧即贴左列右缘
        self.after_idle(self._layout_splitter)

    # ------------------------------------------------------------------
    # 修复缺陷R12：错误列表 | 详情面板 可拖动分隔条
    # ------------------------------------------------------------------
    def _build_splitter(self, panel) -> None:
        """构建分隔条（宽 6px 圆角条 + 三个握点 + ↔ 光标）。"""
        p = self._palette()
        self._splitter_dragging = False
        # 优化（实时拖动）：按下时缓存的拖动几何（motion 内零
        # winfo 查询，纯算术 + place 比例参数直接几何更新）
        self._splitter_drag_ctx = None
        # 优化（矢量文本代理）：拖动期单画布文本代理覆盖层（见
        # _splitter_live_begin；None = 未激活/构建失败回退真实布局）
        self._splitter_live = None
        # 优化（回退路径）：拖动期 CTk 重绘冻结原始方法
        # （_set_ctk_drag_freeze；None = 未冻结）
        self._ctk_freeze_orig = None
        self._splitter = ctk.CTkFrame(
            panel, width=_SPLITTER_WIDTH, corner_radius=3,
            fg_color=p["splitter"], cursor="sb_h_double_arrow")
        # 三个小握点（视觉提示可拖动；绑同一组事件不阻断拖动）
        self._splitter_dots = []
        for dy in (-8, 0, 8):
            dot = tk.Frame(self._splitter, width=2, height=2, bd=0,
                           highlightthickness=0, bg=p["splitter_grip"],
                           cursor="sb_h_double_arrow")
            dot.place(relx=0.5, rely=0.5, y=dy, anchor="center")
            self._splitter_dots.append(dot)
        for target in [self._splitter] + self._splitter_dots:
            target.bind("<ButtonPress-1>", self._on_splitter_press)
            target.bind("<B1-Motion>", self._on_splitter_drag)
            target.bind("<ButtonRelease-1>", self._on_splitter_release)
            target.bind("<Double-Button-1>", self._on_splitter_dblclick)
            target.bind("<Enter>", lambda e: self._splitter_hover(True))
            target.bind("<Leave>", lambda e: self._splitter_hover(False))
        # CTkFrame.bind 实际注册在其内部 canvas（真实点击的命中目标）；
        # 外层 tk.Frame 再绑一份 —— event_generate 直发外层时不经过
        # canvas，双注册保证两种派发路径都能触发（真实点击只命中
        # canvas 一路，不会重复触发）
        for seq, handler in (
                ("<ButtonPress-1>", self._on_splitter_press),
                ("<B1-Motion>", self._on_splitter_drag),
                ("<ButtonRelease-1>", self._on_splitter_release),
                ("<Double-Button-1>", self._on_splitter_dblclick)):
            tk.Frame.bind(self._splitter, seq, handler)

    def _splitter_hover(self, hovered: bool) -> None:
        """悬停/拖动高亮（选中蓝，提示可拖动）。"""
        if self._splitter_dragging and not hovered:
            return          # 拖动中离开仍保持高亮
        try:
            p = self._palette()
            self._splitter.configure(
                fg_color=p["row_selected"] if hovered else p["splitter"])
        except (tk.TclError, ValueError):
            pass

    def _splitter_min_widths(self):
        """动态测量左右列最小宽度（标题栏内容实测宽 + 边距，物理像素）。

        修复缺陷R12（极限遮挡）：固定 200/300 逻辑像素小于标题栏
        内容宽度 —— 拖到最左时左列标题「错误分类列表（按优先级
        降序）」整体被裁，拖到最右时右列「详情」起首两字被分隔条
        挡住。以标题栏请求宽度实测为准（winfo_reqwidth 物理值，
        DPI 缩放已含其中），文案/字号/按钮尺寸变化自动适配；标题
        栏尚未布局完成（请求宽 ≤1）时回退保守常量。
        """
        scale = max(1.0, getattr(self, "_font_scale", 1.0))
        # 标题栏 grid padx 单侧 10 逻辑 px + 两侧；再加 20 逻辑 px 余量
        base = 10 * scale * 2 + 20 * scale
        try:
            lreq = self._list_head.winfo_reqwidth()
            # 修复缺陷R14：左列最小宽度须含标题栏控件组（字体大小+
            # 选择器+全屏，常驻 panel 独立容器）—— 否则拖到最左时
            # 控件组压住标题文字
            creq = self._list_ctrl_box.winfo_reqwidth()
            rreq = self._detail_head.winfo_reqwidth()
        except (tk.TclError, AttributeError):
            lreq = creq = rreq = 0
        if min(lreq, creq, rreq) <= 1:    # 标题栏尚未布局完成
            return _SPLITTER_MIN_LIST * scale, _SPLITTER_MIN_DETAIL * scale
        return lreq + creq + base, rreq + base

    def _sync_level_filter_gaps(self) -> None:
        """级别过滤组可视区间等距 + DEBUG 文本右缘对齐全屏按钮右缘。

        修复缺陷R74：以对齐线（左列「⛶ 全屏」按钮右缘 = 控件组
        容器右缘）反解目标可视区间 G = (目标线 - 自然G右缘 + Σ自然
        区间)/4，四个区间逐框独立设定右 padx 向 G 收敛 —— 吸收复选
        框宽度取整的 1px 级余量差异（统一 padx 只能到 [24,23,22,21]
        阶梯）。所有测量基于 update_idletasks 结算后的一致快照、
        当前 padx 一律读自 grid_info（Tk 几何真值）—— 绝对解算
        而非增量叠加（增量法在 place/grid 延迟结算下必然累积失真：
        实测过叠加翻倍与欠补 114px 两种事故）；天然幂等，重复调用
        零副作用。注意 padx 全程用 Tk 物理 px：CTk 仅 grid()/pack()
        创建时做 DPI 缩放，grid_configure 不缩放（原生透传），
        换算会在高 DPI 下打对折。随 _layout_splitter 重入（窗口
        缩放/分隔条拖动/字体档位，经 after_idle 调度）自动重同步。
        修复缺陷R83：对齐目标线冻结 —— 原实现每次同步读实时按钮
        右缘（随分隔条/窗口移动），级别组 padx 被反复改写，整行
        （级别组及其后智能分析/上下文行数/解析规则链）随分隔条
        漂移；改为首次同步即冻结目标线（含紧凑区：紧凑起始后拖
        入膨胀区也不再跳变），此后同步恒收敛于同一组 padx ——
        过滤行绝对固定，启动布局的对齐观感永久保持；目标线左于
        组体自然宽时 padx 触底 0（冻结值不变，行为同前）。
        """
        try:
            panel = self._ctx_entry.master
            panel.update_idletasks()      # 结算挂起几何（测量一致性前提）
            level_box = panel.grid_slaves(row=0, column=1)[0]
            children = level_box.winfo_children()
            infos, cbs = children[0::2], children[1::2]
            if len(cbs) != 5:
                return
            # 修复缺陷R83：冻结目标线（未冻结时读实时按钮右缘）
            frozen = getattr(self, "_level_gap_target", None)
            if frozen is None:
                target = (self._list_ctrl_box.winfo_x()
                          + self._list_ctrl_box.winfo_width())
            else:
                target = frozen
            nat_gaps, pads_tk, lefts_tk = [], [], []
            for i in range(4):
                pad_l, pad_r = (int(v) for v in
                                cbs[i].grid_info()["padx"])   # Tk 几何真值
                tl = cbs[i]._text_label
                tr = (cbs[i].winfo_x() + tl.winfo_x()
                      + tl.winfo_reqwidth())
                nat_gaps.append(infos[i + 1].winfo_x() - tr - pad_r)
                pads_tk.append(pad_r)
                lefts_tk.append(pad_l)
            tl = cbs[-1]._text_label
            text_right = (level_box.winfo_x() + cbs[-1].winfo_x()
                          + tl.winfo_x() + tl.winfo_reqwidth())
            nat_right = text_right - sum(pads_tk)
            if frozen is None and target > 0:
                # 修复缺陷R83：首次同步即冻结目标线（此刻控件组已
                # place 且经 update_idletasks 结算，读数为真值）——
                # 此后分隔条/窗口变化不再牵引本行
                self._level_gap_target = target
            want = (target - nat_right + sum(nat_gaps)) / 4.0
            for i in range(4):
                new_tk = max(0, int(round(want - nat_gaps[i])))
                if new_tk != pads_tk[i]:
                    # grid_configure 为原生透传（不经 CTk 缩放），
                    # 直接写 Tk 物理 px，左 pad 原样保留
                    cbs[i].grid_configure(padx=(lefts_tk[i], new_tk))
        except (tk.TclError, AttributeError, IndexError):
            pass

    def _layout_splitter(self) -> None:
        """按比例布局左右列与分隔条（全部 relx/relwidth 比例参数）。

        修复缺陷R12（高DPI错位）：CTk place() 会对显式 x/y 乘控件缩放
        系数，而 relwidth/relheight 不缩放 —— x（像素）与 relwidth
        （比例）混用在高 DPI（widget_scaling>1）下两套坐标系分裂：
        左列按比例、分隔条/右列按被二次缩放的像素放置，整体被推出
        面板外（详情面板消失、中间大片空白、拖动反馈错乱）。全部改用
        比例参数后缩放天然无关；分隔条自身宽度由构造器 width=6
        （逻辑 px，CTk 自动换算物理）承担。三列 relx 首尾相接占满面板。
        最小宽度以标题栏实测动态值为准（防极限遮挡）。
        优化（实时拖动）：拖动中复用按下时缓存的最小宽度（每帧
        免两次 winfo_reqwidth Tcl 同步调用），松开时清除缓存。
        """
        panel = self._result_panel
        pw = panel.winfo_width()          # 物理像素
        r = self._splitter_ratio
        scale = max(1.0, getattr(self, "_font_scale", 1.0))
        sp_w = _SPLITTER_WIDTH * scale    # 分隔条物理宽
        cached = getattr(self, "_splitter_drag_mins", None)
        if cached is not None:
            left_min, right_min = cached
        else:
            left_min, right_min = self._splitter_min_widths()
        # 面板太窄放不下「两列最小宽 + 分隔条」：按比例铺满但不改写
        # 比例（<Configure> 中间态宽度逐级增长，此处钳制会把比例
        # 污染成窄面板下的极值），等宽度足够时再按保存比例重排
        if pw < left_min + right_min + sp_w:
            sp_r = sp_w / max(1, pw)
            self._list_col.place(relx=0, rely=0, relwidth=r, relheight=1)
            self._splitter.place(relx=r, rely=0, relheight=1)
            self._detail_col.place(relx=min(1.0, r + sp_r), rely=0,
                                   relwidth=max(0.0, 1 - r - sp_r),
                                   relheight=1)
            self._place_list_ctrl(r, pw)
            # 修复缺陷R74：place 几何变更在 Tk 空闲时才结算，
            # 同步读到的控件组位置是旧值 —— 延迟到空闲再同步
            self.after_idle(self._sync_level_filter_gaps)
            return
        # 最小宽度换算为比例钳制（左≥标题栏实测宽 / 右≥标题栏实测宽）
        lo_r = left_min / pw
        hi_r = 1 - (right_min + sp_w) / pw
        r = min(max(r, lo_r), hi_r)
        self._splitter_ratio = r
        sp_r = sp_w / pw
        self._list_col.place(relx=0, rely=0, relwidth=r, relheight=1)
        self._splitter.place(relx=r, rely=0, relheight=1)
        self._detail_col.place(relx=r + sp_r, rely=0,
                               relwidth=max(0.0, 1 - r - sp_r),
                               relheight=1)
        self._place_list_ctrl(r, pw)
        # 修复缺陷R74：place 几何变更在 Tk 空闲时才结算，
        # 同步读到的控件组位置是旧值 —— 延迟到空闲再同步
        self.after_idle(self._sync_level_filter_gaps)

    def _place_list_ctrl(self, r: float, pw: int) -> None:
        """标题栏控件组贴左列右缘（修复缺陷R14：正常/拖动同一跟随）。

        tk 原生 place（x/y 为面板物理像素）—— 绕过 CTk place 对显式
        像素参数的二次缩放（高 DPI 错位）；容器内控件不 resize，纯
        SetWindowPos 移动（BitBlt 零重绘）。y 取左列标题栏实测位置
        （_list_col y=0 相对面板，其 winfo_y 即面板坐标）。
        """
        try:
            cw = self._list_ctrl_box.winfo_reqwidth()
            hy = self._list_head.winfo_y()
            # 右边距与标题栏 grid padx=10（逻辑 px）一致：换算物理
            margin = int(10 * max(1.0, getattr(self, "_font_scale", 1.0)))
            tk.Frame.place(self._list_ctrl_box,
                           x=max(0, int(r * pw) - cw - margin),
                           y=max(0, hy))
        except (tk.TclError, AttributeError):
            pass

    def _on_splitter_press(self, event) -> None:
        """按下：缓存拖动几何 + 构建矢量文本代理（内容实时延展 + 无撕裂）。

        优化（矢量文本代理）：三方案实测均不满足——纯真实重排
        315ms/帧、真实重排+CTk 冻结 ~140ms/帧（原生内容渲染物理
        下限）都卡且撕裂；位图截图代理内容不动。现按下时把左右
        内容以【真实文本 items】绘制到单画布代理上（复用已验证
        的左裁剪框+右视口滚动骨架）：文本完整绘制、视口/裁剪框
        移动时 canvas 边界自然露出更多文字——内容真延展；每帧仅
        裁剪框 place + 画布视口滚动（GDI 级，实测 ~8ms/帧）；单
        画布渲染原子无撕裂。代理不可用（经典小列表/布局未完成）
        时回退真实布局 + CTk 重绘冻结。详见 _splitter_live_begin。
        """
        self._splitter_dragging = True
        self._splitter_hover(True)
        panel = self._result_panel
        sp_w = _SPLITTER_WIDTH * max(1.0, getattr(self, "_font_scale", 1.0))
        left_min, right_min = self._splitter_min_widths()
        try:
            pw = max(1, panel.winfo_width())
            rootx = panel.winfo_rootx()
        except (tk.TclError, AttributeError):
            return
        lo, hi = left_min, pw - right_min - sp_w
        self._splitter_drag_ctx = {"pw": pw, "rootx": rootx,
                                   "sp_w": sp_w, "lo": lo, "hi": hi}
        self._splitter_drag_mins = (left_min, right_min)
        if hi >= lo:
            if not self._splitter_live_begin():
                # 回退：真实布局逐 motion（需冻结 CTk 重绘级联）
                self._set_ctk_drag_freeze(True)
        # 拖动中窗口失焦（alt-tab 等）→ 结束拖动并应用当前位置
        self.bind("<FocusOut>", self._on_splitter_focusout)
        if self._virtual_list is not None:
            self._virtual_list.set_splitter_drag(True)

    def _splitter_live_begin(self) -> bool:
        """构建矢量文本代理（详见 _on_splitter_press 说明）。

        结构（复用已验证骨架，内容从静态截图换成真实文本 items）：
        - 右画布：固定全幅 + 视口滚动（GDI 级）；详情文本按行绘制
          在「面板像素坐标系」，分隔条竖线/握点用固定内容坐标图元
          —— 随视口平移自动贴住分隔条，零逐帧图元更新。
        - 左裁剪框：可变宽 Frame（纯色填充，resize 只露新区域）+
          固定宽左画布（永不缩放 → 永不整体失效）；可见行（矩形
          背景+标题+摘要）完整绘制为文本 items —— 裁剪框变宽时
          canvas 边界自然露出更多文字（真实延展，非静态像素）。
        - 叠放次序：右画布（底，全幅含竖线）< 左裁剪框（顶，只
          覆盖列表区域，标题栏由右画布近似文本呈现）。
        返回 False = 代理不可用（经典小列表/布局未完成/无数据）。
        """
        vl = self._virtual_list
        if vl is None or not vl._data:
            return False          # 经典小列表：回退真实布局（行少成本低）
        panel = self._result_panel
        ctx = self._splitter_drag_ctx or {}
        pw = ctx.get("pw") or max(1, panel.winfo_width())
        sp_w = ctx.get("sp_w") or _SPLITTER_WIDTH
        lo, hi = ctx.get("lo", 0), ctx.get("hi", pw)
        try:
            panel.update_idletasks()   # 确保 winfo 几何为最终值
            ph = panel.winfo_height()
            lw = self._list_col.winfo_width()
            if min(pw, ph, lw) <= 2 or pw - lw - sp_w <= 2:
                return False
        except tk.TclError:
            return False
        p = self._palette()
        hi = max(hi, lw)
        # 列表宿主几何（右画布与左裁剪框共同的内容区起点；标题栏
        # 区域不覆盖 —— 真实标题栏含字体选择器/全屏按钮/ⓘ 保持
        # 露出，字体/交互自然正确）
        try:
            host_y = (self._list_host.winfo_rooty()
                      - panel.winfo_rooty())
            host_h = max(1, self._list_host.winfo_height())
        except tk.TclError:
            return False
        # --- 右画布：固定全幅宽、只覆盖内容区（y≥host_y）+ 视口滚动
        # （内容映射：屏幕 s ↔ 面板像素 s + lw − left，详情文本/竖线/
        # 左列滚动条随视口贴住分隔条） ---
        cvh = ph - host_y
        right_c = tk.Canvas(panel, bd=0, highlightthickness=0,
                            bg=p["card"], cursor="sb_h_double_arrow")
        right_c.place(x=0, y=host_y, relwidth=1, height=cvh)
        m2 = max(0, hi - lw)         # 左侧余量（拖右时视口为负）
        m1 = max(0, lw - lo)         # 右侧余量（拖左时视口越过右缘）
        right_c.configure(scrollregion=(-m2, 0, pw + m1, cvh),
                          xscrollincrement=1)
        # 修复缺陷R13（初始视口错位双线）：xview_moveto 的分数语义依赖
        # 画布当前实际宽度，place 尚未生效时计算结果错误（实测初始
        # canvasx(0) 错位 573px —— 代理竖线与真实分隔条分离成双竖线
        # 残影）。改为画布尺寸生效后按【实测视口】自校正归零：像素级
        # xview_scroll（xscrollincrement=1）无论初始值如何都精确到 0。
        right_c.update_idletasks()
        cur0 = right_c.canvasx(0)
        if cur0:
            right_c.xview_scroll(int(round(-cur0)), "units")
        self._live_draw_detail(right_c, p, cvh)
        bars = self._live_draw_scrollbars(right_c, lw, sp_w, pw,
                                          cvh, host_h, p)
        # 分隔条竖线：固定内容坐标 [lw, lw+sp]，随视口平移自动出现在
        # 屏幕 [left, left+sp]（标题栏段由真实分隔条跟随呈现，见
        # _live_flush 的 splitter place）
        right_c.create_rectangle(lw, 0, lw + sp_w, cvh,
                                 fill=p["row_selected"], width=0)
        # --- 左裁剪框（只覆盖列表区域）+ 固定宽左画布 ---
        left_clip = tk.Frame(panel, bg=p["window"], bd=0,
                             highlightthickness=0,
                             cursor="sb_h_double_arrow")
        left_clip.place(x=0, y=host_y, width=lw, height=host_h)
        left_c = tk.Canvas(left_clip, bd=0, highlightthickness=0,
                           bg=p["window"], width=hi)
        left_c.place(x=0, y=0, relheight=1)
        self._live_draw_rows(left_c, pw, p)
        if not left_c.find_all():
            # 修复缺陷R13：行池为空/绘制失败时不呈现空白代理（用户
            # 所见「左列空白不跟随」），销毁已建覆盖层回退真实布局
            try:
                right_c.destroy()
                left_clip.destroy()
            except tk.TclError:
                pass
            return False
        # --- 右标题条：分隔条标题栏段竖线 + 「详情」标题跟随 ---
        # 修复缺陷R13（双线/撕裂/字小/位置左上）：
        # 1) 真实分隔条 press 时 place_forget 隐藏 —— 每帧窗口操作
        #    4→3（少一次 SetWindowPos/stacking 变动，跨 VSync 拆分
        #    概率下降），且竖线只有一个代理实体（内容区段在右画布、
        #    标题栏段在本条 canvas [0, sp_w]，同色同宽首尾相接），
        #    物理上根除双竖线残影；
        # 2) 覆盖条左扩到 x=left（含分隔条区），每帧 move+resize —
        #    Frame 边缘擦除 + 内部固定宽画布被裁剪，均不重绘；
        # 3) 标题字体【克隆真实 Label 实际渲染字体】
        #    （_font.create_scaled_tuple(_get_widget_scaling())），
        #    与真实标题逐像素一致 —— 原 _scaled_font(CTkFont(13))
        #    依赖全局 _font_scale，用户机实测代理标题偏小；
        # 4) 文字垂直居中（y=host_y/2, anchor="w"）—— 与真实标题
        #    在标题栏内的纵向位置一致（原 y=4 贴顶偏左上）。
        # 修复缺陷R15：ⓘ 不隐藏、保持真实控件 —— 正常态 grid column 0
        # weight=1 拉伸使 ⓘ 固定在右端「⛶ 全屏」按钮左边（位置与分隔
        # 条无关），真实露出即与正常态逐像素一致且 Tooltip 可用；只
        # 隐藏标题 Label（它随右列左缘移动，由本条 canvas 近似）。
        hidden = []
        try:
            head_w = self._detail_head.winfo_children()
            head_w[0].grid_remove()      # 仅标题 Label
            hidden.append(head_w[0])
        except (tk.TclError, AttributeError, IndexError):
            head_w = ()
            hidden = []
        try:
            f_title = head_w[0]._font.create_scaled_tuple(
                head_w[0]._get_widget_scaling())
        except (AttributeError, IndexError, ValueError, tk.TclError):
            f_title = self._scaled_font(ctk.CTkFont(size=13, weight="bold"))
        # 修复缺陷R14：右端「ⓘ + ⛶ 全屏」实测总宽+边距作为覆盖条右缘
        # 余量 —— 原固定 100px 在 200% DPI 下小于按钮实际宽（84 逻辑
        # ×2+padx ≈ 190px），按钮左半被覆盖条吃掉；R15 起 ⓘ 真实露出，
        # 余量须含 ⓘ 宽度与 grid padx（ⓘ 4 / 按钮 6 / 栏右缘 10）
        scale = max(1.0, getattr(self, "_font_scale", 1.0))
        try:
            info_w = head_w[1].winfo_reqwidth() if len(head_w) > 1 \
                else int(14 * scale)
            fs_w = int(self._detail_fs_btn.winfo_reqwidth() + info_w
                       + 20 * scale + 6)
        except (tk.TclError, AttributeError):
            fs_w = 100
        tbar_w = max(1, pw - lo - fs_w)  # 最宽值（拖左时右缘留 fs_w）
        tbar = tk.Frame(panel, bg=p["card"], bd=0, highlightthickness=0,
                        cursor="sb_h_double_arrow")
        tbar.place(x=lw, y=0, width=max(1, pw - lw - fs_w), height=host_y)
        tbar_c = tk.Canvas(tbar, bd=0, highlightthickness=0, bg=p["card"],
                           width=tbar_w)
        tbar_c.place(x=0, y=0, relheight=1)
        # 分隔条标题栏段竖线（与右画布内容区段同色同宽、y 向相接）
        tbar_c.create_rectangle(0, 0, sp_w, host_y,
                                fill=p["row_selected"], width=0)
        title = "详情（典型样例 · 上下文 · 降噪堆栈）"
        mid_y = host_y / 2
        tbar_c.create_text(sp_w + 10, mid_y, anchor="w", font=f_title,
                           fill=p["row_text"], text=title)
        # 修复缺陷R15：ⓘ 由真实控件呈现（固定在右端全屏按钮左边），
        # 本条不再绘制近似 ⓘ（原画在标题文字后与正常态布局不符）
        # 隐藏真实分隔条（代理竖线全权呈现；release/dblclick 经
        # _layout_splitter 重新 place 恢复显示，位置一致无跳变）
        try:
            self._splitter.place_forget()
        except tk.TclError:
            pass
        # 修复缺陷R14：左列标题栏控件组跟随参数（容器常驻 panel，
        # flush 逐帧 tk 原生 place 纯移动至左列右缘；ctrl_dx=容器宽+
        # 右边距 10 逻辑 px 换算物理，与 _place_list_ctrl 一致）
        try:
            margin = int(10 * max(1.0, getattr(self, "_font_scale", 1.0)))
            ctrl_dx = int(self._list_ctrl_box.winfo_reqwidth()) + margin
            ctrl_y = max(0, self._list_head.winfo_y())
        except (tk.TclError, AttributeError):
            ctrl_dx, ctrl_y = 0, 0
        self._splitter_live = {
            "clip": left_clip, "left": left_c, "right": right_c,
            "lw": lw, "sp_w": sp_w, "ph": ph, "pw": pw, "bars": bars,
            "lo": lo, "tbar": tbar, "tbar_c": tbar_c, "hidden": hidden,
            "fs_w": fs_w, "ctrl_dx": ctrl_dx, "ctrl_y": ctrl_y,
            "pending": lw, "t0": 0.0, "after_id": None}
        self._live_flush()     # 初始帧同步（消除 press 瞬间错位）
        return True

    def _splitter_live_end(self) -> None:
        """销毁矢量文本代理（幂等；回退路径为空操作）。"""
        live = getattr(self, "_splitter_live", None)
        self._splitter_live = None
        if live is None:
            return
        after_id = live.get("after_id")
        if after_id is not None:
            try:
                self.after_cancel(after_id)   # 取消挂起的节流帧
            except tk.TclError:
                pass
        for key in ("right", "clip", "tbar"):
            try:
                live[key].destroy()     # 左画布随裁剪框一并销毁
            except (tk.TclError, KeyError):
                pass
        for w in live.get("hidden", ()):  # 恢复真实「详情」标题+ⓘ
            try:
                w.grid()
            except tk.TclError:
                pass

    def _live_draw_rows(self, canvas, pw, p, vl=None, panel=None) -> None:
        """左列可见行 items（标题+摘要完整文本，裁剪框自然延展）。

        行按【裁剪框坐标系】绘制（左画布 place 在裁剪框内，裁剪框
        自身已定位于列表宿主 y 处——此处若再加 host_y 会导致内容
        整体下移一个标题栏高度），y = 行在虚拟列表画布内的 y 减去
        滚动偏移（= 行在列表宿主内的屏幕 y）。文本完整绘制（含
        水平滚动偏移），裁剪框边界裁剪显示区域。
        优化缺陷R42：vl/panel 可注入（全屏列表代理复用本绘制）。
        """
        vl = vl or self._virtual_list
        panel = panel or self._result_panel
        if vl is None or not vl._data:
            return
        states = self._row_states()
        try:
            # x：裁剪框与面板左缘对齐（x=0）→ 用面板坐标系
            x_base = (vl._canvas.winfo_rootx()
                      - panel.winfo_rootx())
            scroll_x = vl._canvas.canvasx(0)
            scroll_y = vl._canvas.canvasy(0)
        except tk.TclError:
            x_base, scroll_x, scroll_y = 0, 0, 0
        rh = vl.ROW_HEIGHT
        head_lh = vl._m_head.metrics("linespace")
        for slot in vl._slots:
            idx = slot.get("idx", -1)
            if idx < 0 or idx >= len(vl._data):
                continue
            try:
                sy = vl._canvas.coords(slot["win"])[1]
            except (tk.TclError, KeyError):
                continue
            y = sy - scroll_y
            # 修复缺陷R16：视图行双类型（簇行/实例行）代理绘制
            row = vl._data[idx]
            if row[0] == "c":
                cidx = row[1]
                cluster = self._displayed[cidx]
                selected = cidx == self._selected_row
                expanded = cidx in self._expanded_clusters
                # 修复缺陷R40：代理与真实行同色 —— 选中行头部
                # 用调亮级别色（蓝底可辨级别）
                head_color = (
                    (self._row_color_sel(cluster) or p["sel_text"])
                    if selected
                    else (self._row_color(cluster) or p["row_text"]))
                link = ("#60a5fa" if p["is_dark"] == "1" else "#2563EB")
                tgl = (f"\u25bc \u00d7{cluster.count}" if expanded
                       else f"\u25b6 \u00d7{cluster.count}")
                head_text = self._row_text(cluster, with_count=False)
                sum_text = self._clip(cluster.summary, vl.SUMMARY_CLIP)
                sum_fill = p["row_text"]
            else:
                inst = self._displayed[row[1]].instances[row[2]]
                selected = ((row[1], row[2]) == self._selected_inst)
                tgl = ""
                head_color = p["muted"]
                head_text = "      " + self._inst_head_text(inst)
                sum_text = "        " + self._clip(inst.summary,
                                                   vl.SUMMARY_CLIP)
                sum_fill = p["row_text"]
                link = None
            if selected:
                bg = states["selected"]
            elif idx == vl._hovered:
                bg = states["hover"]
            else:
                bg = states["bg"]
            # 修复缺陷R26：代理矩形/文本对齐行块边距（sy 已含上
            # 间隙 gy；宽度收 2gx、高度收 2gy，与真实行块同位，
            # 拖动结束回真实行时无跳变）
            gx, gy = vl._row_gaps()
            rw = vl._region_w()
            canvas.create_rectangle(x_base + gx - scroll_x, y,
                                    x_base + rw - gx - scroll_x,
                                    y + rh - 2 * gy, fill=bg, width=0)
            tx = x_base + gx + vl._sx(_ROW_PADX) - scroll_x
            if tgl:
                # 修复缺陷R34：图标画在等宽盒中心（与真实行
                # _make_slot 布局一致），盒宽固定，「×N」与头部
                # 文字起始 x 不随 ▶/▼ 切换变化
                _iw = (max(vl._m_head.measure("\u25b6"),
                           vl._m_head.measure("\u25bc"))
                       + vl._m_head.measure(" "))
                _ic, _sp, _cnt = tgl.partition(" ")
                canvas.create_text(tx + _iw // 2, y + 7, anchor="n",
                                   font=vl._m_head, fill=link, text=_ic)
                canvas.create_text(tx + _iw, y + 7, anchor="nw",
                                   font=vl._m_head, fill=link, text=_cnt)
                tx += _iw + vl._m_head.measure(_cnt) + vl._sx(10)
            canvas.create_text(tx, y + 7, anchor="nw", font=vl._m_head,
                               fill=head_color, text=head_text)
            # 修复缺陷R37：分界细线（与真实行同位：头部行底与摘要
            # 之间；选中 sel_border 亮色/未选中 row_border 低调色）
            _dy = y + 7 + head_lh + 2
            if tgl:
                canvas.create_rectangle(
                    x_base + gx + vl._sx(_ROW_PADX) - scroll_x, _dy,
                    x_base + rw - gx - vl._sx(_ROW_PADX) - scroll_x,
                    _dy + vl._sx(1),
                    fill=(p["sel_border"] if selected else p["row_border"]),
                    width=0)
            canvas.create_text(x_base + gx + vl._sx(_ROW_PADX) - scroll_x,
                               _dy + vl._sx(1) + 2, anchor="nw",
                               font=vl._m_sum, fill=sum_fill,
                               text=sum_text)

    def _live_draw_detail(self, canvas, p, cvh) -> None:
        """右列详情文本行（画布坐标系，y0=详情框在内容区的 y）。

        详情按行完整绘制（text widget 当前可见行区间），视口平移
        自然露出更多行尾内容（真实延展）。字体用 _scaled_font 缩放
        元组 —— 与真实 textbox 渲染尺寸一致（直接 cget("font") 在
        高 DPI 下尺寸不可靠）。标题栏由真实控件呈现（不覆盖），
        此处不画。
        """
        panel = self._result_panel
        try:
            tb = self._detail_box._textbox
            f = self._scaled_font(self._detail_box._font)
            import tkinter.font as _tkfont
            fm = _tkfont.Font(font=f)   # 元组经 _stringify 解析（同 _m_head）
            lh = max(1, fm.metrics("linespace"))
            x0 = tb.winfo_rootx() - panel.winfo_rootx() + 4
            y0 = (tb.winfo_rooty() - panel.winfo_rooty()
                  - canvas.winfo_y() + 2)
            start = int(str(tb.index("@0,0")).split(".")[0])
            nlines = cvh // lh + 2
            text = tb.get(f"{start}.0", f"{start + nlines}.end")
            for i, line in enumerate(text.splitlines()):
                canvas.create_text(x0, y0 + i * lh, anchor="nw", font=f,
                                   fill=p["row_text"], text=line)
        except (tk.TclError, AttributeError, ValueError):
            pass

    def _live_draw_scrollbars(self, canvas, lw, sp_w, pw, cvh,
                              host_h, p) -> dict:
        """滚动条近似（槽+滑块矩形），返回需逐帧补偿的 items。

        - 左列垂直条：内容坐标 [lw-B, lw] —— 随视口平移自动贴住
          左列右缘（同竖线原理），静态；
        - 左列水平条 / 右列垂直条：屏幕位置固定（左缘/右缘贴面板
          边缘），视口滚动会带走 —— 每帧 coords 补偿（flush 中）。
        颜色复用分隔条色系（splitter 槽 / splitter_grip 滑块）。
        """
        B = 12                     # 条宽（近似 CTkScrollbar 14 缩放）
        bars = {}
        try:
            grip, slot = p["splitter_grip"], p["splitter"]
            # 左列垂直条（静态，随视口自动贴左缘）
            canvas.create_rectangle(lw - B, 0, lw, host_h,
                                    fill=slot, width=0)
            y0, y1 = self._vbar_thumb(self._virtual_list._canvas)
            canvas.create_rectangle(lw - B + 2, host_h * y0,
                                    lw - 2, host_h * max(y1, y0 + 0.08),
                                    fill=grip, width=0)
            # 左列水平条（屏幕左缘固定 → 每帧补偿）
            hx0, hx1 = self._hbar_thumb(self._virtual_list._canvas)
            bars["hslot"] = canvas.create_rectangle(
                10, host_h - B, lw - B, host_h, fill=slot, width=0)
            bars["hthumb"] = canvas.create_rectangle(
                10, host_h - B + 2, 60, host_h - 2, fill=grip, width=0)
            bars["hfrac"] = (hx0, hx1)
            # 右列垂直条（屏幕右缘固定 → 每帧补偿）
            bars["rslot"] = canvas.create_rectangle(
                pw - B, 0, pw, cvh, fill=slot, width=0)
            ry0, ry1 = self._vbar_thumb(self._detail_box._textbox)
            bars["rthumb"] = canvas.create_rectangle(
                pw - B + 2, 2, pw - 2, 40, fill=grip, width=0)
            bars["rfrac"] = (ry0, ry1)
            bars["B"] = B
        except (tk.TclError, AttributeError, KeyError):
            pass
        return bars

    @staticmethod
    def _vbar_thumb(widget) -> tuple:
        """垂直滚动条滑块区间（yview first/last，异常时全幅）。"""
        try:
            return tuple(widget.yview())
        except (tk.TclError, AttributeError):
            return (0.0, 1.0)

    @staticmethod
    def _hbar_thumb(widget) -> tuple:
        """水平滚动条滑块区间（xview first/last，异常时全幅）。"""
        try:
            return tuple(widget.xview())
        except (tk.TclError, AttributeError):
            return (0.0, 1.0)

    def _iter_ctk_widgets(self, root):
        """深度优先遍历 root 子树的全部控件（含 root，供冻结遍历）。"""
        stack = [root]
        while stack:
            w = stack.pop()
            yield w
            try:
                stack.extend(w.winfo_children())
            except (tk.TclError, AttributeError):
                continue

    def _set_ctk_drag_freeze(self, active: bool) -> None:
        """拖动期冻结全部 CTk 外壳重绘（真实布局逐 motion 的性能保障）。

        实测特大字体+200%DPI+57 簇逐 motion 真实重排单帧 261-315ms
        （cProfile），热点全部在 CTk 外壳而非内容：
        1) 每个 CTk 控件 resize 全量 _draw（17 次 ≈115ms/帧）；
        2) CTkScrollbar._draw 末尾强制 self._canvas.update_idletasks()
          （CTk 源码 ctk_scrollbar.py）—— 嵌套排空全部待处理几何/
          重绘形成重入级联（6 次 ≈124ms/帧），set() 无条件 _draw。
        CTk 控件构造时绑定原 _update_dimensions_event（类补丁对已建
        控件无效），但 _draw 一律经「实例 → 类」查找调用 —— 逐类
        冻结 _draw（CTk 15 个自定义类）同时掐断维度重绘、set 重绘
        与嵌套级联。原生内容控件（详情 tk.Text 重排换行、虚拟列表
        tk.Canvas/行窗口文字延展）不受影响照常逐帧实时跟随，视觉
        内容完全跟手；CTk 外壳（圆角边框/滚动条滑块）冻结至松开
        一次性补齐（_refresh_ctk_chrome）。幂等：可安全重复调用。
        """
        if active:
            if getattr(self, "_ctk_freeze_orig", None) is None:
                self._ctk_freeze_orig = {
                    cls: cls.__dict__["_draw"] for cls in _CTK_DRAW_CLASSES}
                for cls in self._ctk_freeze_orig:
                    cls._draw = _ctk_draw_noop
                # 尺寸快照：松开时只补绘拖动期尺寸变化的控件
                # 修复缺陷R17：遍历根窗口（含全屏 Toplevel 子树 —
                # 全屏分隔条回退路径冻结时其 CTk 控件也在冻结范围）
                self._ctk_dims_snapshot = {
                    w: (w._current_width, w._current_height)
                    for w in self._iter_ctk_widgets(self)
                    if isinstance(w, ctk.CTkBaseClass)}
        else:
            saved = getattr(self, "_ctk_freeze_orig", None)
            if saved:
                for cls, fn in saved.items():
                    cls._draw = fn
                self._ctk_freeze_orig = None
                self._ctk_dims_snapshot = None

    def _refresh_ctk_chrome(self) -> None:
        """松开后一次性补齐拖动期冻结的 CTk 外壳重绘（幂等）。

        只重绘拖动期尺寸相对按下快照变化的控件（其余视觉本就未变）；
        控件已销毁/异常则跳过。
        """
        snap = getattr(self, "_ctk_dims_snapshot", None) or {}
        try:
            # 修复缺陷R17：遍历根窗口（含全屏 Toplevel 子树）
            for w in self._iter_ctk_widgets(self):
                if not isinstance(w, ctk.CTkBaseClass):
                    continue
                old = snap.get(w)
                if old is None:
                    continue
                try:
                    if (round(old[0]) != round(w._current_width)
                            or round(old[1]) != round(w._current_height)):
                        w._draw(no_color_updates=True)
                except (tk.TclError, AttributeError, KeyError):
                    continue
        except (tk.TclError, AttributeError):
            pass
        self._ctk_dims_snapshot = None

    def _on_splitter_focusout(self, _event) -> None:
        """拖动中窗口失焦：结束拖动并应用当前位置（兼容性边界）。"""
        if self._splitter_dragging:
            self._on_splitter_release(None)

    def _on_splitter_drag(self, event) -> None:
        """拖动：矢量代理 rAF 节流应用（跟手不积压、同帧合成防撕裂）。

        修复缺陷R12（拖动错位/拖不回）：x_root 是事件自带的屏幕
        绝对坐标，与事件接收窗口（分隔条内部 canvas / 2px 握点）
        无关。
        优化（矢量文本代理 + 节流）：撕裂/卡的根因是 motion 事件
        ~120Hz 涌入而每帧两窗口（裁剪框 resize + 画布视口滚动）
        更新被 CPU 抢占拆到不同 VSync 周期（跨帧=撕裂）且事件
        积压（越拖越卡）。现 motion 只做钳制算术并记录最新位置，
        按 ≤83fps 节拍统一应用（_live_flush）——两窗口更新集中在
        同一应用帧发出，DWM 同帧合成一致画面；负载再高也只丢
        中间帧、永不错乱积压，松开/停止后最后一次 flush 必应用
        到最终位置（无残差）。无代理（经典小列表）时回退真实
        布局逐 motion（CTk 已冻结）。
        """
        if not self._splitter_dragging:
            return
        ctx = getattr(self, "_splitter_drag_ctx", None)
        if ctx is None:
            return
        if ctx["hi"] < ctx["lo"]:          # 窗口太窄：锁定分隔条
            return
        try:
            x = event.x_root - ctx["rootx"] - ctx["sp_w"] // 2
        except AttributeError:
            return
        left = min(max(x, ctx["lo"]), ctx["hi"])
        self._splitter_ratio = left / ctx["pw"]
        live = getattr(self, "_splitter_live", None)
        if live is not None:
            import time as _time
            live["pending"] = left
            now = _time.monotonic()
            if now - live["t0"] >= 0.012:      # 节流：≤83fps 直接应用
                live["t0"] = now
                self._live_flush()
            elif live["after_id"] is None:     # 兜底帧：应用最新位置
                try:
                    live["after_id"] = self.after(12, self._live_flush)
                except tk.TclError:
                    pass
            return
        try:
            self._layout_splitter()      # 回退：真实布局逐 motion
        except tk.TclError:
            self._set_ctk_drag_freeze(False)

    def _live_flush(self) -> None:
        """应用节流的最新拖动位置（裁剪框宽 + 视口滚动 + 分隔条跟随）。

        每应用帧的原语（合计 ~8ms，GDI 级）：
        1. 左裁剪框 place_configure(width)（内部固定画布不缩放，
           文本 items 边界自然露出更多 → 内容延展）；
        2. 右画布 xview_scroll 到 lw−left（视口平移使详情文本/竖线/
           左列滚动条精确贴住分隔条）；
        3. 真实分隔条 place(relx) 跟随（纯位置移动不触发重绘 ——
           标题栏段真实呈现，内容区段被右画布遮住由代理竖线呈现）；
        4. 左水平条/右垂直条 coords 补偿（屏幕固定元素反向抵消
           视口平移）。不触碰真实内容控件、不调用 update()。
        """
        live = getattr(self, "_splitter_live", None)
        if live is None:
            return
        live["after_id"] = None
        left = live.get("pending")
        if left is None:
            return
        try:
            lw, pw = live["lw"], live["pw"]
            live["clip"].place_configure(width=left)
            shift = lw - left               # 视口平移量（内容=屏幕+shift）
            right_c = live["right"]
            cur = right_c.canvasx(0)
            delta = int(round(shift - cur))
            if delta:
                right_c.xview_scroll(delta, "units")
            # 修复缺陷R13：真实分隔条已 place_forget（press 时），
            # 不再逐帧 place —— 每帧窗口操作 4→3，且根除与代理竖线
            # 的双线残影；竖线由右画布（内容区段）+ tbar（标题栏
            # 段）两个代理图元呈现。
            # 右标题条跟随：左缘 x=left（含分隔条区，内部 canvas 在
            # [0, sp_w] 画竖线），右缘留 fs_w（右「⛶ 全屏」按钮实测
            # 宽+边距，修复缺陷R14：原 100px 高 DPI 下吃掉按钮左半）
            tbar = live.get("tbar")
            if tbar is not None:
                tbar.place_configure(
                    x=left, width=max(1, pw - left - live["fs_w"]))
            # 修复缺陷R14：左列标题栏控件组（字体大小+全屏）逐帧实时
            # 跟随左列右缘 —— 真实容器 tk 原生 place 纯移动（BitBlt
            # 零重绘零失真），正常态定位见 _place_list_ctrl
            ctrl_dx = live.get("ctrl_dx")
            if ctrl_dx:
                try:
                    tk.Frame.place(self._list_ctrl_box,
                                   x=max(0, left - ctrl_dx),
                                   y=live.get("ctrl_y", 0))
                except tk.TclError:
                    pass
            # 屏幕固定滚动条的反向补偿（内容坐标 = 屏幕 + shift）
            bars = live.get("bars") or {}
            B = bars.get("B", 12)
            if "hslot" in bars:
                x0, x1 = 10 + shift, left - B + shift
                right_c.coords(bars["hslot"], x0,
                               right_c.coords(bars["hslot"])[1],
                               x1, right_c.coords(bars["hslot"])[3])
                hx0, hx1 = bars.get("hfrac", (0.0, 1.0))
                tw = max(16.0, (x1 - x0) * (hx1 - hx0))
                right_c.coords(bars["hthumb"], x0 + (x1 - x0) * hx0,
                               right_c.coords(bars["hthumb"])[1],
                               x0 + (x1 - x0) * hx0 + tw,
                               right_c.coords(bars["hthumb"])[3])
            if "rslot" in bars:
                rx0, rx1 = pw - B + shift, pw + shift
                right_c.coords(bars["rslot"], rx0, 0, rx1,
                               right_c.coords(bars["rslot"])[3])
                ry0, ry1 = bars.get("rfrac", (0.0, 1.0))
                cvh = right_c.winfo_height()
                th0 = cvh * ry0
                th1 = cvh * max(ry1, ry0 + 0.08)
                right_c.coords(bars["rthumb"], rx0 + 2, th0,
                               rx1 - 2, th1)
        except (tk.TclError, KeyError, AttributeError):
            pass

    def _on_splitter_release(self, event) -> None:
        """松开：真实布局一次到位 + 销毁代理 + 解除 CTk 冻结。

        顺序：先 _layout_splitter() + 一次 update_idletasks() 让
        真实控件在覆盖层之下完成几何应用与重排（内容位置与代理
        显示一致，无跳变），再销毁覆盖层；回退路径（无代理）则
        解除 CTk 冻结并补绘变化的控件。虚拟列表补一次全量 _sync
        并持久化比例。
        """
        if not self._splitter_dragging:
            return
        self._splitter_dragging = False
        self._splitter_hover(False)
        self._splitter_drag_ctx = None
        self._splitter_drag_mins = None
        self.unbind("<FocusOut>")
        self._layout_splitter()
        try:
            self._result_panel.update_idletasks()
        except (tk.TclError, AttributeError):
            pass
        self._splitter_live_end()
        self._set_ctk_drag_freeze(False)
        self._refresh_ctk_chrome()
        if self._virtual_list is not None:
            self._virtual_list.set_splitter_drag(False)
        self._save_config()

    def _on_splitter_dblclick(self, event) -> None:
        """双击恢复默认比例（2:3）并闪烁反馈。"""
        self._splitter_dragging = False
        self._splitter_drag_ctx = None
        self._splitter_drag_mins = None
        self.unbind("<FocusOut>")
        self._splitter_ratio = _SPLITTER_DEFAULT_RATIO
        # 修复缺陷R13：先真实布局（_layout_splitter 重新 place 被
        # place_forget 的真实分隔条，三列在代理之下一次到位），再
        # 销毁代理 —— 与 release 顺序一致，恢复无跳变
        self._layout_splitter()
        self._splitter_live_end()
        self._set_ctk_drag_freeze(False)
        if self._virtual_list is not None:
            self._virtual_list.set_splitter_drag(False)
        try:
            self._result_panel.update_idletasks()
        except (tk.TclError, AttributeError):
            pass
        self._refresh_ctk_chrome()
        self._flash_splitter()
        self._save_config()

    def _flash_splitter(self) -> None:
        """分隔条闪烁（高亮 150ms 后回落主题色）。"""
        try:
            p = self._palette()
            self._splitter.configure(fg_color=p["accent"])
            self.after(150, lambda: self._splitter.configure(
                fg_color=self._palette()["splitter"]))
        except (tk.TclError, ValueError):
            pass

    def _make_hscroll(self, scrollable: "ctk.CTkScrollableFrame"
                      ) -> "ctk.CTkScrollbar":
        """为垂直方向 CTkScrollableFrame 补水平滚动（修复缺陷R9）。

        CTkScrollableFrame 内部把内容窗口宽度锁死为画布宽（超宽内容
        直接被裁剪、无横向滚动）。此处接入一条水平 CTkScrollbar：
        - 画布 xscrollcommand <-> 滚动条（command=xview）双向联动；
        - 画布/内容尺寸变化后把内容窗口宽度放宽到内容自然宽
          （覆盖 CTk 内部的锁宽行为，绑定注册在其后、add=True）；
        - Shift+滚轮横向滚动复用 CTk 内建处理（xview 有范围时生效）。
        返回滚动条（调用方自行 grid/pack 到宿主底部）。
        """
        canvas = scrollable._parent_canvas
        # 真正的宿主：CTkScrollableFrame 自身 master 是内部画布，
        # 其外层 _parent_frame 的 master 才是调用方传入的宿主容器
        host = scrollable._parent_frame.master
        hbar = ctk.CTkScrollbar(host, orientation="horizontal",
                                height=14, command=canvas.xview)
        canvas.configure(xscrollcommand=hbar.set)

        def fit(*_args) -> None:
            try:
                # 内容自然宽 = 内部行控件请求宽的最大值（不换行时为
                # 完整文本宽）；与画布宽取大（窄内容时行仍占满视口）
                need = canvas.winfo_width()
                for child in scrollable.winfo_children():
                    w = child.winfo_reqwidth()
                    if w > need:
                        need = w
                canvas.itemconfigure(scrollable._create_window_id,
                                     width=need)
            except (tk.TclError, ValueError, AttributeError):
                pass

        canvas.bind("<Configure>", fit, add=True)
        scrollable.bind("<Configure>", fit, add=True)
        return hbar

    def _setup_detail_tags(self) -> None:
        """主面板详情标签配置（转发共享方法）。"""
        self._apply_detail_tags(self._detail_box)

    def _apply_detail_tags(self, box: ctk.CTkTextbox,
                          big: bool = False) -> None:
        """详情文本高亮标签：关键字 / 业务栈帧 / 元信息 / 折叠提示 / 段落标题。

        修复缺陷R5：业务栈帧（bstack）琥珀色加粗，与普通日志行区分
        更明显；系统库折叠提示（fold）独立紫色，视觉清晰。
        修复缺陷R10：big=True 时启用全屏大字号标签（摘要 20 加粗 /
        元信息 16 / 栈帧 18 加粗——正文为 box 基础字体 18）。

        说明：CTkTextbox.tag_config 禁用 font 选项（与 DPI 缩放不
        兼容），加粗经内部 tk.Text 配置并手动缩放；失败时降级为
        无高亮（不阻断渲染）。
        """
        try:
            for tag, color in _DETAIL_TAG_COLORS.items():
                box.tag_config(tag, foreground=color)
            # 优化缺陷R46：结果搜索关键字照亮标签（醒目黄底黑字，
            # 全主题可读；优先级抬到最高，压过内置错误词高亮）
            box.tag_config("searchkw", background="#fbbf24",
                           foreground="#1f2937")
            try:
                box.tag_raise("searchkw")
            except tk.TclError:
                pass
            # 业务栈帧加粗（内部 tk.Text 支持 tag font）
            inner = getattr(box, "_textbox", None)
            if inner is not None:
                size_b = self._font_px(13 if not big else 18)
                font = ("Consolas", size_b, "bold")
                scaler = getattr(box, "_apply_font_scaling", None)
                if callable(scaler):
                    font = scaler(font)
                inner.tag_config("bstack", font=font)
                if big:
                    # 全屏详情大字号：段标题 20 加粗 / 元信息 16
                    for tag, spec in (
                            ("header", ("Consolas", self._font_px(20),
                                        "bold")),
                            ("meta", ("Consolas", self._font_px(16)))):
                        f = scaler(spec) if callable(scaler) else spec
                        inner.tag_config(tag, font=f)
        except (tk.TclError, AttributeError):
            pass

    def _build_status_bar(self) -> None:
        bar = ctk.CTkFrame(self, corner_radius=0)
        # 修复缺陷R72：实时筛选行插入后行号 5→6
        # 优化缺陷R85：高级选项行插入后行号 6→7
        bar.grid(row=7, column=0, sticky="ew")
        self._bg_widgets.append((bar, "header"))
        bar.grid_columnconfigure(0, weight=1)
        # 优化缺陷R105：底栏第一行「完成」状态串（自按钮行下沉，
        # 整行展示 token/压缩率不被挤压）
        self._done_label = ctk.CTkLabel(bar, text="", anchor="w")
        self._done_label.grid(row=0, column=0, padx=12, pady=(1, 0),
                              sticky="w")
        self._muted_labels.append(self._done_label)
        self._status_label = ctk.CTkLabel(bar, text="就绪 · 支持文件导入 / 文本粘贴 / 多文件对比",
                                          anchor="w")
        self._status_label.grid(row=1, column=0, padx=12, pady=(0, 1),
                                sticky="w")
        self._muted_labels.append(self._status_label)

    # ==================================================================
    # 配置持久化
    # ==================================================================
    def _restore_config(self) -> None:
        cfg = self._config
        # 修复缺陷R19：FATAL 复选框已删除（始终放行）—— 旧配置中的
        # FATAL/fatal_level_upgraded 键静默忽略（_level_vars 无 FATAL）
        levels = list(cfg.get("levels") or DEFAULT_SELECTED_LEVELS)
        for level, var in self._level_vars.items():
            var.set(level in levels)
        # 优化缺陷R43：旧配置中的 include/exclude/top_n 键静默忽略
        # （过滤输入区已删除）
        # 优化缺陷R44：恢复上次设置的上下文行数
        if isinstance(cfg.get("context_lines"), int):
            self._ctx_entry.delete(0, "end")
            self._ctx_entry.insert(0, str(cfg["context_lines"]))
        if cfg.get("rule") in RULE_KEYS:
            # 优化缺陷R71：配置存规则键（旧配置 generic 等键直接兼容），
            # 下拉显示中文名且选中项移出列表
            self._on_rule_changed(RULE_DISPLAY[cfg["rule"]])
        if cfg.get("analyze_mode") in ANALYZE_KEYS:
            # 优化缺陷R79：恢复智能分析模式（默认完整分析）
            self._on_analyze_changed(ANALYZE_DISPLAY[cfg["analyze_mode"]])
        if cfg.get("similarity") in SIMILARITY_KEYS:
            # 优化缺陷R84：恢复相似度阈值档位（默认标准）
            self._on_similarity_changed(SIMILARITY_DISPLAY[cfg["similarity"]])
        if cfg.get("redact_custom"):
            # 优化缺陷R103：恢复自定义脱敏规则（多行文本原样回填）
            self._redact_custom_box.insert("1.0", str(cfg["redact_custom"]))
        # 修复缺陷R10：字体大小档位恢复（__init__ 已按档位建字体，
        # 此处仅同步选择器显示；字号一致时回调为空操作）
        if cfg.get("font_size") in FONT_SIZE_SCALE:
            self._font_menu.set(cfg["font_size"])
        last = cfg.get("last_files") or []
        if last and self._file_entry.get() == "":
            self._file_entry.insert(0, str(last[0]))
        for entry, path in zip(self._compare_entries, last[1:]):
            if path:
                entry.insert(0, str(path))

    def _current_config_dict(self) -> dict:
        return {
            "levels": [lv for lv, var in self._level_vars.items() if var.get()],
            "context_lines": self._current_context_lines(),
            "rule": self._rule_key,
            # 优化缺陷R79：智能分析模式持久化
            "analyze_mode": self._analyze_key,
            # 优化缺陷R84：相似度阈值档位持久化
            "similarity": self._similarity_key,
            # 优化缺陷R103：自定义脱敏规则持久化
            "redact_custom": self._redact_custom_box.get(
                "1.0", "end").strip(),
            # 优化缺陷R111：已屏蔽错误模板持久化
            "muted": sorted(self._muted),
            # 优化缺陷R114：过滤预设持久化
            "filter_presets": dict(self._presets),
            # 修复缺陷R1：保存四态主题名（light/dark/blue/green）
            "appearance": self._theme,
            # 修复缺陷R10：字体大小档位持久化（下次启动自动恢复）
            "font_size": self._font_size,
            # 修复缺陷R12：分隔条位置持久化（左右宽度比例）
            "splitter_ratio": self._splitter_ratio,
            # 修复缺陷R17：全屏列表窗口分隔条位置持久化
            "fs_splitter_ratio": self._fs_splitter_ratio,
            "window": {"width": self.winfo_width(),
                       "height": self.winfo_height()},
            "last_files": [self._file_entry.get()] +
                          [e.get() for e in self._compare_entries],
        }

    def _save_config(self) -> None:
        self._config = self._current_config_dict()
        self._store.save(self._config)

    def _current_context_lines(self) -> int:
        """读取上下文行数（优化缺陷R44：≥0 任意整数有效；负数
        按 0 行处理（用户填负数仍分析 → 上下文 0 行）；非法/空
        输入回退默认 50；无上限，内存代价随「行数×簇数」线性增长）。
        """
        try:
            value = int(self._ctx_entry.get() or DEFAULT_CONTEXT_LINES)
        except ValueError:
            return DEFAULT_CONTEXT_LINES
        return max(0, value)

    # ------------------------------------------------------------------
    # 优化缺陷R85：高级选项参数解析（时间范围 / 关键词 / 行数上限）
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_time_input(text: str):
        """时间输入归一化：空 → (None, None)；HH:MM[:SS] → ("tod", 当日
        秒数)；YYYY-MM-DD[ T]HH:MM[:SS] → ("epoch", 时间戳)；非法 →
        抛 ValueError（调用方拦截并提示，不启动分析）。"""
        import time as _time
        text = (text or "").strip()
        if not text:
            return None, None
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                t = _time.strptime(text, fmt)
                return "tod", t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec
            except ValueError:
                pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
            try:
                return "epoch", _time.mktime(_time.strptime(text, fmt))
            except ValueError:
                pass
        raise ValueError(f"无法识别的时间格式：{text}（支持 HH:MM:SS 或 "
                         f"YYYY-MM-DD HH:MM:SS，留空不限）")

    def _advanced_params(self) -> dict:
        """采集高级选项行 + ⚙ 弹层参数（开始分析时下发管线）。

        优化缺陷R85：时间范围（epoch/tod 双模式，任端可空）+ 行数
        上限（档位键映射数值）+ 包含/排除关键词（逗号/空格/中文逗号
        分隔，小写子串语义见 EntryFilter）。
        优化缺陷R91/R92：关键词两框已撤入 ⚙ 弹层并新增正则开关 —
        勾选时各项预编译校验，非法正则抛 ValueError（与非法时间输入
        同路：_on_start 拦截提示并放弃启动，边界校验不静默丢弃）。
        优化缺陷R88：编码指定（None=自动探测）。
        """
        params: dict = {
            "time_start": None, "time_end": None,
            "tod_start": None, "tod_end": None,
            "max_lines": MAXLINES_VALUES.get(self._maxlines_key),
            "use_regex": bool(self._use_regex_var.get()),
            "encoding": _ENCODING_VALUES.get(self._encoding_display),
        }
        for entry, prefix in ((self._time_start_entry, "start"),
                              (self._time_end_entry, "end")):
            mode, value = self._parse_time_input(entry.get())
            if mode == "epoch":
                params[f"time_{prefix}"] = value
            elif mode == "tod":
                params[f"tod_{prefix}"] = value
        for entry, key in ((self._include_entry, "include"),
                           (self._exclude_entry, "exclude")):
            raw = (entry.get() or "").replace("，", ",").replace("、", ",")
            keywords = [k.strip() for k in re.split(r"[,\s]+", raw)
                        if k.strip()]
            if keywords:
                params[key] = keywords
        if params["use_regex"]:
            bad = []
            for key in ("include", "exclude"):
                for kw in params.get(key) or []:
                    try:
                        re.compile(kw)
                    except re.error as exc:
                        bad.append(f"{kw}（{exc}）")
            if bad:
                raise ValueError("关键词正则无法编译：" + "、".join(bad))
        return params

    def _on_rule_changed(self, choice: str) -> None:
        """解析规则切换：状态栏即时展示该规则的适用场景说明。

        修复缺陷#8：与悬停 tooltip 互补——切换规则时无需悬停
        即可看到当前规则的含义。
        优化缺陷R71：选项为中文显示名（内部映射规则键）；当前
        选中项从下拉列表移除（与主题选择器同款交互）。
        """
        key = _RULE_BY_DISPLAY.get(choice)
        if key is None:
            return
        self._rule_key = key
        self._rule_menu.set(RULE_DISPLAY[key])
        self._rule_menu.configure(
            values=[RULE_DISPLAY[k] for k in RULE_KEYS if k != key])
        desc = RULE_DESCRIPTIONS.get(key)
        if desc:
            self._status_label.configure(
                text=f"解析规则 {RULE_DISPLAY[key]}：{desc}")

    def _on_analyze_changed(self, choice: str) -> None:
        """智能分析模式切换（优化缺陷R79）：完整/深度/快速。

        与解析规则选择器同款交互：选项为中文显示名（内部映射模式
        键）；当前选中项从下拉列表移除；状态栏即时展示该模式说明
        （与 ⓘ 悬停 tooltip 互补）。模式在下一次「开始分析」生效。
        """
        key = _ANALYZE_BY_DISPLAY.get(choice)
        if key is None:
            return
        self._analyze_key = key
        self._analyze_menu.set(ANALYZE_DISPLAY[key])
        self._analyze_menu.configure(
            values=[ANALYZE_DISPLAY[k] for k in ANALYZE_KEYS
                    if k != key])
        desc = ANALYZE_DESCRIPTIONS.get(key)
        if desc:
            self._status_label.configure(
                text=f"智能分析 {ANALYZE_DISPLAY[key]}：{desc}")
        # 注：不在此 _save_config —— _restore_config 启动回放本方法时
        # 窗口尚未映射，winfo_height 只有 ~200，会冲掉已保存的窗口
        # 尺寸（修复缺陷R80）；持久化由后续保存点（分析完成/关窗）负责

    def _on_similarity_changed(self, choice: str) -> None:
        """相似度阈值切换（优化缺陷R84）：标准/严格/宽松。

        与智能分析/解析规则选择器同款交互：选项为中文显示名（内部
        映射档位键）；当前选中项从下拉列表移除；状态栏即时展示该
        档说明（与 ⓘ 悬停 tooltip 互补）。阈值在下一次「开始分析」
        生效（聚类器构造时映射为数值阈值）。
        """
        key = _SIMILARITY_BY_DISPLAY.get(choice)
        if key is None:
            return
        self._similarity_key = key
        self._similarity_menu.set(SIMILARITY_DISPLAY[key])
        self._similarity_menu.configure(
            values=[SIMILARITY_DISPLAY[k] for k in SIMILARITY_KEYS
                    if k != key])
        desc = SIMILARITY_DESCRIPTIONS.get(key)
        if desc:
            self._status_label.configure(
                text=f"相似度 {SIMILARITY_DISPLAY[key]}：{desc}")
        # 注：不在此 _save_config（同 R80 窗口尺寸覆盖教训）

    def _build_settings_popup(self) -> None:
        """构建 ⚙ 设置弹层（优化缺陷R84）：低频分析前置设置收纳处。

        入住项：相似度阈值（R84 自过滤行撤入）、关键词正则开关
        （R91）、编码指定（R88）、出站脱敏开关（R86）；关键词黑白
        名单两框已于 R93 迁回过滤行（用户审美，与智能分析/解析
        规则换位）。弹层机制与主题下拉同款（修复缺陷R15 验证过）：
        无边框 CTkToplevel + 全局点击收起（150ms 打开豁免 + 落点
        在弹层内豁免），与焦点完全解耦；主窗最小化（<Unmap>）时
        同步收起（topmost 不随主窗隐藏）。
        """
        win = ctk.CTkToplevel(self)
        win.overrideredirect(True)
        win.withdraw()
        win.attributes("-topmost", True)
        self._settings_popup = win
        self._settings_popup_opened_at = 0.0
        frame = ctk.CTkFrame(win, corner_radius=10)
        frame.pack(fill="both", expand=True)
        ctk.CTkLabel(frame, text="分析设置",
                     font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, columnspan=3, padx=12, pady=(10, 2),
            sticky="w")
        ctk.CTkLabel(frame, text="相似度").grid(
            row=1, column=0, padx=(12, 4), pady=2, sticky="w")
        self._similarity_key = "standard"
        self._similarity_menu = ctk.CTkOptionMenu(
            frame, width=150, dynamic_resizing=False,
            values=[SIMILARITY_DISPLAY[k] for k in SIMILARITY_KEYS
                    if k != "standard"],
            command=self._on_similarity_changed)
        self._similarity_menu.set(SIMILARITY_DISPLAY["standard"])
        self._similarity_menu.grid(row=1, column=1, padx=(4, 0),
                                   pady=2, sticky="w")
        sim_help = ctk.CTkLabel(
            frame, text="ⓘ", text_color="#4dd0e1",
            font=ctk.CTkFont(size=13, weight="bold"), cursor="question_arrow")
        sim_help.grid(row=1, column=2, padx=(6, 12), pady=2, sticky="w")
        self._similarity_help_tooltip = Tooltip(
            sim_help,
            lambda: SIMILARITY_DESCRIPTIONS.get(self._similarity_key, ""))

        # 关键词正则开关（优化缺陷R91：默认小写子串；勾选后各项按
        # 正则编译，非法正则在开始分析时拦截提示；R93 关键词两框已
        # 迁回过滤行，开关留弹层 —— 低频开关不占用主行宽度）
        self._use_regex_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            frame, text="关键词按正则匹配", variable=self._use_regex_var,
            checkbox_width=18, checkbox_height=18).grid(
            row=2, column=1, padx=(4, 0), pady=2, sticky="w")
        regex_help = ctk.CTkLabel(
            frame, text="ⓘ", text_color="#4dd0e1",
            font=ctk.CTkFont(size=13, weight="bold"), cursor="question_arrow")
        regex_help.grid(row=2, column=2, padx=(6, 12), pady=2, sticky="w")
        Tooltip(regex_help, lambda: (
            "作用于过滤行的包含/排除关键词：\n"
            "不勾选（默认）：关键词按小写子串匹配，逗号/空格分隔多个\n"
            "勾选后：每项按正则表达式匹配（不区分大小写），例如\n"
            "  timeout|refused   —— 命中两者之一\n"
            "  ERR-\\d{4}         —— 命中 ERR-0001 这类错误码\n"
            "非法正则在开始分析时拦截提示，不会静默丢弃"))

        # 编码指定（优化缺陷R88：None=自动探测；手动覆盖乱码场景）
        ctk.CTkLabel(frame, text="编码").grid(
            row=3, column=0, padx=(12, 4), pady=2, sticky="w")
        self._encoding_display = ENCODING_DISPLAY[0]
        self._encoding_menu = ctk.CTkOptionMenu(
            frame, width=150, dynamic_resizing=False,
            values=list(ENCODING_DISPLAY[1:]),
            command=self._on_encoding_changed)
        self._encoding_menu.set(ENCODING_DISPLAY[0])
        self._encoding_menu.grid(row=3, column=1, padx=(4, 0), pady=2,
                                 sticky="w")
        enc_help = ctk.CTkLabel(
            frame, text="ⓘ", text_color="#4dd0e1",
            font=ctk.CTkFont(size=13, weight="bold"), cursor="question_arrow")
        enc_help.grid(row=3, column=2, padx=(6, 12), pady=2, sticky="w")
        Tooltip(enc_help, lambda: (
            "自动探测覆盖 UTF-8/GBK/UTF-16/UTF-32 及 BOM，日常无需指定\n"
            "老系统中文日志出现乱码时，手动指定 GBK / GB18030 重跑\n"
            "多文件合并分析时，全部文件按同一编码读取"))

        # 出站脱敏开关（优化缺陷R86：导出报告/复制摘要时打码；本地
        # 分析结果保持原样便于排查，仅出站文本过 redact_text）
        self._redact_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            frame, text="导出/复制时脱敏", variable=self._redact_var,
            checkbox_width=18, checkbox_height=18).grid(
            row=4, column=1, padx=(4, 0), pady=(2, 2), sticky="w")
        redact_help = ctk.CTkLabel(
            frame, text="ⓘ", text_color="#4dd0e1",
            font=ctk.CTkFont(size=13, weight="bold"), cursor="question_arrow")
        redact_help.grid(row=4, column=2, padx=(6, 12), pady=(2, 2),
                         sticky="w")
        Tooltip(redact_help, lambda: (
            "投喂外部大模型前的合规打码（纯本地规则，离线可用）\n"
            "密钥/凭据 → [密钥]    URL 账号段 → [账号]\n"
            "邮箱 → [邮箱]    手机号 → [电话]    IPv4 → [IP]\n"
            "仅作用于导出报告与复制摘要；界面内的分析结果保持原样"))

        # 自定义脱敏规则（优化缺陷R103：内置规则覆盖不了的内部敏感
        # 词 —— 工单号/内部域名等，用户每行写一条正则）
        ctk.CTkLabel(frame, text="自定义脱敏").grid(
            row=5, column=0, padx=(12, 4), pady=(2, 2), sticky="nw")
        self._redact_custom_box = ctk.CTkTextbox(
            frame, width=170, height=64,
            font=ctk.CTkFont(family="Consolas", size=11))
        self._redact_custom_box.grid(row=5, column=1, padx=(4, 0),
                                     pady=(2, 2), sticky="w")
        custom_help = ctk.CTkLabel(
            frame, text="ⓘ", text_color="#4dd0e1",
            font=ctk.CTkFont(size=13, weight="bold"), cursor="question_arrow")
        custom_help.grid(row=5, column=2, padx=(6, 12), pady=(2, 2),
                         sticky="nw")
        Tooltip(custom_help, lambda: (
            "每行一条正则，命中统一打码为 [自定义]\n"
            "例：PMS-\\d{6}      —— 内部工单号\n"
            "例：[\\w.-]+\\.corp  —— 内部域名\n"
            "非法行在导出/复制时忽略并在状态栏提示\n"
            "与上方开关联动：开关关闭时自定义规则也不生效"))

        # 过滤预设（优化缺陷R114）：高级选项行 + ⚙ 弹层的过滤参数
        # 一键存取 —— 存 UI 原值快照（未解析字符串），套用即完整
        # 还原；名称即键，重名覆盖；下拉套用 / ✕ 删除当前选中项
        ctk.CTkLabel(frame, text="过滤预设").grid(
            row=6, column=0, padx=(12, 4), pady=2, sticky="w")
        self._preset_name_entry = ctk.CTkEntry(
            frame, width=170, placeholder_text="输入名称后点 💾 保存当前过滤")
        self._preset_name_entry.grid(row=6, column=1, padx=(4, 0),
                                     pady=2, sticky="w")
        self._preset_save_btn = ctk.CTkButton(
            frame, text="💾", width=28, command=self._save_preset)
        self._preset_save_btn.grid(row=6, column=2, padx=(6, 12),
                                   pady=2, sticky="w")
        self._preset_menu = ctk.CTkOptionMenu(
            frame, width=170, dynamic_resizing=False,
            values=self._preset_names(), command=self._apply_preset)
        self._preset_menu.set("选择预设套用…" if not self._presets
                              else "选择预设…")
        self._preset_menu.grid(row=7, column=1, padx=(4, 0),
                               pady=(2, 12), sticky="w")
        self._preset_del_btn = ctk.CTkButton(
            frame, text="✕", width=28, fg_color="#6b7280",
            hover_color="#4b5563", command=self._delete_preset)
        self._preset_del_btn.grid(row=7, column=2, padx=(6, 12),
                                  pady=(2, 12), sticky="w")
        preset_help = ctk.CTkLabel(
            frame, text="ⓘ", text_color="#4dd0e1",
            font=ctk.CTkFont(size=13, weight="bold"), cursor="question_arrow")
        preset_help.grid(row=7, column=0, padx=(12, 4), pady=(2, 12),
                         sticky="e")
        Tooltip(preset_help, lambda: (
            "快照内容：时间范围 / 行数上限 / 包含·排除关键词 /\n"
            "正则开关 / 相似度 / 编码 —— 即「开始分析」下发的全部过滤\n"
            "💾 保存：以上方输入框的名称为键（重名覆盖）\n"
            "下拉：选中即套用到各控件；✕：删除最近套用的预设"))
        # 全局点击收起（同主题弹窗 R15 机制，与焦点解耦）
        self.bind_all("<Button-1>", self._on_settings_global_click,
                      add=True)
        self.bind("<Unmap>", lambda e: self._close_settings_popup())

    def _on_encoding_changed(self, choice: str) -> None:
        """编码指定切换（优化缺陷R88）：选中项从下拉列表移除（与
        相似度选择器同款交互）；在下一次「开始分析」生效。"""
        if choice not in _ENCODING_VALUES:
            return
        self._encoding_display = choice
        self._encoding_menu.set(choice)
        self._encoding_menu.configure(
            values=[d for d in ENCODING_DISPLAY if d != choice])
        self._status_label.configure(text=f"编码 {choice}：下一次分析生效")

    def _toggle_settings_popup(self) -> None:
        """⚙ 按钮：切换设置弹层开合（贴按钮正下方、右对齐防出右屏）。"""
        win = self._settings_popup
        if win.state() == "normal":
            self._close_settings_popup()
            return
        try:
            x = (self._settings_btn.winfo_rootx()
                 + self._settings_btn.winfo_width()
                 - win.winfo_reqwidth())
            y = (self._settings_btn.winfo_rooty()
                 + self._settings_btn.winfo_height() + 4)
        except tk.TclError:
            return
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        # 物理像素定位（wm_geometry / rootx 均为物理系）；屏宽钳制
        # 必须用物理边界 —— winfo_screenwidth 高 DPI 下是逻辑值，
        # 混用会把弹层钳到屏幕左半（修复：复用 Tooltip._screen_bounds）
        vx, vy, sw, sh = Tooltip._screen_bounds(win)
        x = max(vx + 8, min(x, vx + sw - w - 8))
        y = max(vy + 8, min(y, vy + sh - h - 8))
        win.wm_geometry(f"{w}x{h}+{x}+{y}")
        self._settings_popup_opened_at = time.perf_counter()
        win.deiconify()

    def _on_settings_global_click(self, event) -> None:
        """全局点击收起设置弹层（同主题弹窗：150ms 与弹层内两豁免）。"""
        try:
            if self._settings_popup.state() != "normal":
                return
            if time.perf_counter() - self._settings_popup_opened_at < 0.15:
                return
            hit = self.winfo_containing(event.x_root, event.y_root)
            if hit is not None and \
                    hit.winfo_toplevel() is self._settings_popup:
                return          # 点在弹层内（下拉/ⓘ/空白区）
        except tk.TclError:
            return
        self._close_settings_popup()

    def _close_settings_popup(self) -> None:
        """收起设置弹层。"""
        if self._settings_popup.state() != "withdrawn":
            self._settings_popup.withdraw()

    # ------------------------------------------------------------------
    # 优化缺陷R114：过滤预设（⚙ 弹层；UI 原值快照存取）
    # ------------------------------------------------------------------
    def _preset_names(self) -> list:
        """预设下拉选项（无预设时给占位项防空列表异常）。"""
        return list(self._presets) or ["（暂无预设）"]

    def _preset_snapshot(self) -> dict:
        """当前过滤参数的 UI 原值快照（未解析字符串，套用即还原）。"""
        return {
            "time_start": self._time_start_entry.get(),
            "time_end": self._time_end_entry.get(),
            "maxlines": self._maxlines_key,
            "include": self._include_entry.get(),
            "exclude": self._exclude_entry.get(),
            "use_regex": bool(self._use_regex_var.get()),
            "similarity": self._similarity_key,
            "encoding": self._encoding_display,
        }

    def _refresh_preset_menu(self) -> None:
        """预设下拉重填（增删后）；显示回占位文案。"""
        self._preset_menu.configure(values=self._preset_names())
        self._preset_menu.set("选择预设…" if self._presets
                              else "（暂无预设）")

    def _save_preset(self) -> None:
        """💾 保存当前过滤参数为预设（名称为键，重名覆盖）。"""
        name = self._preset_name_entry.get().strip()
        if not name:
            self._status_label.configure(text="预设保存失败：请先输入名称")
            return
        self._presets[name] = self._preset_snapshot()
        self._save_config()
        self._refresh_preset_menu()
        self._preset_name_entry.delete(0, "end")
        self._status_label.configure(
            text=f"预设「{name}」已保存（{len(self._presets)} 个）")

    def _apply_preset(self, name: str) -> None:
        """下拉选中即套用：快照原值回填各过滤控件（下次分析生效）。"""
        snap = self._presets.get(name)
        if snap is None:
            return
        for entry, key in ((self._time_start_entry, "time_start"),
                           (self._time_end_entry, "time_end"),
                           (self._include_entry, "include"),
                           (self._exclude_entry, "exclude")):
            entry.delete(0, "end")
            value = str(snap.get(key) or "")
            if value:
                entry.insert(0, value)
        if snap.get("maxlines") in MAXLINES_KEYS:
            self._on_maxlines_changed(
                MAXLINES_DISPLAY[snap["maxlines"]])
        self._use_regex_var.set(bool(snap.get("use_regex")))
        if snap.get("similarity") in SIMILARITY_KEYS:
            self._on_similarity_changed(
                SIMILARITY_DISPLAY[snap["similarity"]])
        if snap.get("encoding") in _ENCODING_VALUES:
            self._on_encoding_changed(str(snap["encoding"]))
        self._last_preset = name
        self._status_label.configure(
            text=f"预设「{name}」已套用：下一次「开始分析」生效")

    def _delete_preset(self) -> None:
        """✕ 删除最近套用的预设（无套用记录则删下拉当前项）。"""
        name = getattr(self, "_last_preset", None) or \
            self._preset_menu.get()
        if name not in self._presets:
            self._status_label.configure(
                text="无可删预设：先从下拉选中一个")
            return
        del self._presets[name]
        self._last_preset = None
        self._save_config()
        self._refresh_preset_menu()
        self._status_label.configure(
            text=f"预设「{name}」已删除（剩 {len(self._presets)} 个）")

    # ==================================================================
    # 文件选择 / 拖拽
    # ==================================================================
    def _setup_drag_and_drop(self) -> None:
        """拖拽初始化：注册整窗为拖放目标。

        说明：注册在根窗口（而非单个输入框）上，任何位置松手均可接收，
        规避 CTk 控件级注册的兼容性问题；tkinterdnd2 缺失时自动降级
        为纯点击选择并给出安装提示。
        """
        if not _HAS_DND:
            return
        try:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_drop_file)
        except tk.TclError:
            # tkdnd 二进制与当前环境不兼容时静默降级
            pass

    def _browse_file(self, entry: Optional[ctk.CTkEntry] = None) -> None:
        target = entry or self._file_entry
        # 优化缺陷R87：文件类型补 .gz/.zip（压缩包透明解压直读）
        filetypes = [("日志文件", "*.log *.txt *.out *.gz *.zip"),
                     ("所有文件", "*.*")]
        if entry is not None:
            # 对比区输入框：维持单选
            path = filedialog.askopenfilename(
                title="选择日志文件", filetypes=filetypes)
            paths = (path,) if path else ()
        else:
            # 优化缺陷R89：主导入框支持多选 → 轮转文件合并分析
            # （";" 分隔填入，开始分析时按 mtime 最旧在前排序）
            paths = filedialog.askopenfilenames(
                title="选择日志文件（可多选轮转文件合并分析）",
                filetypes=filetypes)
        if paths:
            target.delete(0, "end")
            target.insert(0, "; ".join(paths))

    def _on_drop_file(self, event) -> None:
        """拖拽导入：整窗接收，按当前 Tab 路由填充文件路径。"""
        try:
            paths = [str(p) for p in self.tk.splitlist(event.data)]
        except (tk.TclError, AttributeError, TypeError):
            raw = getattr(event, "data", "") or ""
            paths = [raw.strip("{}")] if raw else []
        paths = [p for p in paths if p]
        if not paths:
            return

        if self._tabview.get() == "多文件对比":
            # 对比模式：按 A/B/C 顺序填充
            for entry, path in zip(self._compare_entries, paths[:3]):
                entry.delete(0, "end")
                entry.insert(0, path)
            self._status_label.configure(
                text=f"已拖入 {min(len(paths), 3)} 个文件（对比模式，点击开始分析）")
        else:
            # 常规模式：首个文件进导入框，其余预填对比区
            self._file_entry.delete(0, "end")
            self._file_entry.insert(0, paths[0])
            if len(paths) > 1:
                for entry, path in zip(self._compare_entries, paths[1:3]):
                    entry.delete(0, "end")
                    entry.insert(0, path)
                self._status_label.configure(
                    text=f"已拖入 {len(paths)} 个文件：首个进入文件导入，"
                         f"其余已填入多文件对比区（可切换 Tab 对比）")
            else:
                self._status_label.configure(
                    text=f"已拖入文件：{paths[0]}（点击开始分析）")

    def _measure_theme_icon_col(self) -> int:
        """实测主题 emoji 最宽渲染宽 → 图标列宽（逻辑 px，≥ 基准 28）。

        修复缺陷R14：CTkLabel 的 width 只是**最小值** —— 内容更宽时
        标签自动扩展（如 200% DPI 下 🔵 渲染 62 物理 px > 28 逻辑
        ×2=56），固定列宽失效、文字错位。启动时按真实渲染宽度实测
        取 max+余量，保证四行图标列宽严格一致且不裁剪图标。
        """
        if getattr(self, "_theme_icon_col", 0):
            return self._theme_icon_col
        scale = max(1.0, getattr(self, "_font_scale", 1.0))
        probe = ctk.CTkFrame(self, fg_color="transparent")
        labels = [ctk.CTkLabel(probe, text=THEMES[k]["icon"])
                  for k in THEME_ORDER]
        for i, lbl in enumerate(labels):
            lbl.grid(row=0, column=i)
        probe.update_idletasks()
        try:
            max_phys = max(lbl.winfo_reqwidth() for lbl in labels)
        finally:
            probe.destroy()
        self._theme_icon_col = max(_THEME_ICON_COL,
                                   int(-(-max_phys // scale)) + 2)
        return self._theme_icon_col

    def _build_theme_popup(self) -> None:
        """构建主题下拉弹窗（两列对齐：固定宽图标列 + 左对齐文字列）。

        修复缺陷R14：CTkOptionMenu 内部是 tkinter.Menu（纯文本项），
        四个 emoji（☀️🌙🔵🟢）字形宽度不一导致文字起始位置错乱。
        自定义 CTkToplevel 弹窗：每行 grid 两列 —— 图标列固定宽
        _THEME_ICON_COL 且居中（图标宽度差异被列宽吸收），文字列
        左对齐 —— 四行文字起始 x 完全一致。当前主题行打开时隐藏
        （R13 排除当前项语义保留）。
        """
        win = ctk.CTkToplevel(self)
        win.overrideredirect(True)          # 无边框原生弹窗
        win.withdraw()
        win.attributes("-topmost", True)
        self._theme_popup = win
        self._theme_popup_rows: Dict[str, dict] = {}
        self._theme_popup_opened_at = 0.0
        win.grid_columnconfigure(0, weight=1)
        # 修复缺陷R15：全局点击收起（常驻 all 绑定，弹窗未开时
        # 直接返回；与焦点解耦，不受 CTkToplevel 全局 set_focus
        # 干扰 —— FocusOut 方案会被它立即误触发关闭）
        self.bind_all("<Button-1>", self._on_theme_global_click, add=True)
        # 主窗口最小化时同步收起（弹窗 topmost 不随主窗隐藏）
        self.bind("<Unmap>", lambda e: self._close_theme_popup())
        icon_col = self._measure_theme_icon_col()
        for i, key in enumerate(THEME_ORDER):
            row = ctk.CTkFrame(win, corner_radius=4,
                              fg_color="transparent", cursor="hand2")
            row.grid(row=i, column=0, sticky="ew", padx=2, pady=1)
            t = THEMES[key]
            icon = ctk.CTkLabel(row, text=t["icon"], width=icon_col,
                               anchor="center")
            icon.grid(row=0, column=0, padx=(8, 0), pady=(4, 4))
            name = ctk.CTkLabel(row, text=t["label"], anchor="w")
            name.grid(row=0, column=1, padx=(4, 8))
            # 行 + 子控件均可点击选择；悬停整行高亮。
            # CTk 控件 bind 实际注册在内部 canvas（真实点击命中处），
            # 外层 tk 部件再绑一份 —— event_generate 直发外层时不经过
            # canvas，双注册保证两种派发路径都能触发（真实点击只命中
            # canvas 一路，不会重复触发；与分隔条同方案）
            for w in (row, icon, name):
                w.bind("<Button-1>",
                       lambda e, k=key: self._on_theme_selected(k))
                w.bind("<Enter>",
                       lambda e, r=row: self._theme_row_hover(r, True))
                w.bind("<Leave>",
                       lambda e, r=row: self._theme_row_hover(r, False))
                tk.Frame.bind(w, "<Button-1>",
                             lambda e, k=key: self._on_theme_selected(k))
            self._theme_popup_rows[key] = {
                "row": row, "icon": icon, "name": name}

    def _theme_row_hover(self, row, hovered: bool) -> None:
        """弹窗行悬停高亮（当前调色板 row_hover 色）。"""
        try:
            row.configure(fg_color=self._palette()["row_hover"]
                          if hovered else "transparent")
        except (tk.TclError, ValueError):
            pass

    def _on_theme_box_click(self, _event=None) -> None:
        """点击选择框：切换弹窗开合。"""
        if self._theme_popup.state() == "normal":
            self._close_theme_popup()
        else:
            self._open_theme_popup()

    def _open_theme_popup(self) -> None:
        """弹出下拉列表：贴选择框正下方（物理像素定位，DPI 精确）。

        修复缺陷R15（点击无反应）：不用 focus/FocusOut 收起 ——
        CTkToplevel.__init__ 注册了全局 bind_all("<Button-1>",
        set_focus)，每次点击把焦点强制设回被点击控件；弹窗
        deiconify 的瞬间焦点先到弹窗、随即被其抢回 → FocusOut
        立即收起（表现为点击无反应、列表闪没）。改为全局点击
        收起机制（_on_theme_global_click），与焦点完全解耦。
        """
        self._update_theme_menu()           # 刷新可见行（排除当前项）
        box = self._theme_box
        win = self._theme_popup
        try:
            x = box.winfo_rootx()
            y = box.winfo_rooty() + box.winfo_height() + 2
        except tk.TclError:
            return
        win.update_idletasks()
        w = max(box.winfo_width(), win.winfo_reqwidth() + 8)
        # wm_geometry 用物理像素（CTk 的 geometry 会二次缩放）
        win.wm_geometry(f"{w}x{win.winfo_reqheight()}+{x}+{y}")
        self._theme_popup_opened_at = time.perf_counter()
        win.deiconify()

    def _on_theme_global_click(self, event) -> None:
        """全局点击收起下拉（弹窗外任意点击；不依赖焦点）。

        豁免两类点击：弹窗打开后 150ms 内（打开动作本身的同一
        事件链，all bindtag 在 widget 绑定之后触发，若不豁免会
        开了立刻关）与落点在弹窗内的点击（行间隙等空白区）。
        """
        try:
            if self._theme_popup.state() != "normal":
                return
            if time.perf_counter() - self._theme_popup_opened_at < 0.15:
                return
            hit = self.winfo_containing(event.x_root, event.y_root)
            if hit is not None and hit.winfo_toplevel() is self._theme_popup:
                return          # 点在弹窗内（行间隙）
        except tk.TclError:
            return
        self._close_theme_popup()

    def _close_theme_popup(self) -> None:
        """收起下拉列表。"""
        if self._theme_popup.state() != "withdrawn":
            self._theme_popup.withdraw()

    def _theme_popup_items(self) -> List[str]:
        """下拉列表当前可见项（除当前主题外的其他三态，保持顺序）。

        修复缺陷R13：当前主题不重复出现在列表中（避免重复选择）；
        列表顺序沿用 THEME_ORDER（亮色、暗色、蓝调、绿调）去掉当前项。
        """
        return [k for k in THEME_ORDER if k != self._theme]

    def _on_theme_selected(self, key: str) -> None:
        """下拉选择主题：直接切换到所选主题（淡出 -> 切换 -> 淡入）。

        修复缺陷R13（第三轮#7）：由循环点击改为下拉直达 —— 可跳过
        中间主题直接选中任意目标；切换动画与底层刷新逻辑不变。
        """
        if key not in THEMES or key == self._theme:
            return
        self._close_theme_popup()
        self._fade_out(key, 0)

    # 主题过渡帧序列（窗口透明度）
    _FADE_OUT_STEPS = (1.0, 0.82, 0.66, 0.55)
    _FADE_IN_STEPS = (0.55, 0.66, 0.82, 1.0)

    def _set_window_alpha(self, alpha: float) -> bool:
        """设置窗口透明度（平台不支持时返回 False 降级直切）。"""
        try:
            self.attributes("-alpha", alpha)
            return True
        except tk.TclError:
            return False

    def _fade_out(self, target: str, step: int) -> None:
        """过渡第一阶段：窗口渐隐（4 帧后于谷底切换主题）。"""
        if step >= len(self._FADE_OUT_STEPS):
            self._apply_theme_switch(target)
            self._fade_in(0)
            return
        if not self._set_window_alpha(self._FADE_OUT_STEPS[step]):
            # 平台不支持透明度：直接切换（无动画但功能完整）
            self._apply_theme_switch(target)
            return
        self.after(28, lambda: self._fade_out(target, step + 1))

    def _fade_in(self, step: int) -> None:
        """过渡第二阶段：窗口渐显恢复。"""
        if step >= len(self._FADE_IN_STEPS):
            self._set_window_alpha(1.0)
            return
        if not self._set_window_alpha(self._FADE_IN_STEPS[step]):
            return
        self.after(28, lambda: self._fade_in(step + 1))

    def _apply_theme_switch(self, target: str) -> None:
        """实际执行主题切换（调色板 / 按钮标识 / 行颜色 / 持久化）。"""
        self._theme = target
        self._apply_palette()
        # 修复缺陷#12/R1：切换立即持久化（不等关闭/下次分析）
        self._store.save(self._current_config_dict())

    def _apply_palette(self) -> None:
        """把当前主题调色板应用到全部登记控件（修复缺陷R1）。

        CTk 的 appearance mode 只有两态，蓝调/绿调在 light 基础上
        按登记角色批量覆盖颜色实现。
        """
        p = self._palette()
        ctk.set_appearance_mode("dark" if p["is_dark"] == "1" else "light")
        try:
            self.configure(fg_color=p["window"])
        except (tk.TclError, ValueError):
            pass
        # 背景类容器（部分控件已销毁时跳过）
        for widget, role in self._bg_widgets:
            try:
                widget.configure(fg_color=p[role])
            except (tk.TclError, ValueError, AttributeError):
                continue
        # 次要文字
        for label in self._muted_labels:
            try:
                label.configure(text_color=p["muted"])
            except (tk.TclError, ValueError, AttributeError):
                continue
        # 主按钮（accent 蓝系 / 蓝调绿调下白底深字）与危险按钮（红系）
        for btn, kind in self._accent_buttons:
            try:
                if kind == "danger":
                    btn.configure(text_color="#ffffff")
                else:
                    btn.configure(fg_color=p["accent"],
                                  hover_color=p["accent_hover"],
                                  text_color=p["accent_text"])
            except (tk.TclError, ValueError, AttributeError):
                continue
        # 修复缺陷R12：分隔条主题色刷新（条身 + 握点）
        try:
            self._splitter.configure(fg_color=p["splitter"])
        except (tk.TclError, ValueError, AttributeError):
            pass
        for dot in getattr(self, "_splitter_dots", []):
            try:
                dot.configure(bg=p["splitter_grip"])
            except (tk.TclError, ValueError):
                pass
        # 修复缺陷R13/R14：主题选择框（复合控件）+ 下拉弹窗配色 ——
        # 框身用 accent 底色 + accent_text 文字（蓝调/绿调下为白底
        # 深字）；弹窗用卡片底色 + 正文色，行悬停 row_hover，四态协调
        box = getattr(self, "_theme_box", None)
        if box is not None:
            try:
                box.configure(fg_color=p["accent"])
                for lbl in (self._theme_box_icon, self._theme_box_name):
                    lbl.configure(text_color=p["accent_text"])
                self._theme_box_arrow.configure(text_color=p["accent_text"])
                self._theme_popup.configure(fg_color=p["card"])
                for entry in self._theme_popup_rows.values():
                    entry["icon"].configure(text_color=p["text"])
                    entry["name"].configure(text_color=p["text"])
            except (tk.TclError, ValueError, AttributeError):
                pass
        self._refresh_row_colors()
        self._update_theme_menu()

    def _update_theme_menu(self) -> None:
        """同步选择框显示与弹窗可见行（当前主题不重复出现）。

        修复缺陷R14：图标/文字分列显示（两列布局对齐）；弹窗当前
        主题行 grid_remove 隐藏（其余行按 THEME_ORDER 原顺序重排，
        图标列固定宽不受行增删影响）。
        """
        box = getattr(self, "_theme_box", None)
        if box is None:
            return
        p = self._palette()
        if self._theme_box_icon.cget("text") != p["icon"]:
            self._theme_box_icon.configure(text=p["icon"])
        if self._theme_box_name.cget("text") != p["label"]:
            self._theme_box_name.configure(text=p["label"])
        vis = 0
        for key in THEME_ORDER:
            row = self._theme_popup_rows[key]["row"]
            if key == self._theme:
                row.grid_remove()          # 排除当前项（R13 语义）
            else:
                row.grid()
                row.grid_configure(row=vis)   # 压实行序不留空行
                vis += 1

    def _refresh_row_colors(self) -> None:
        """主题切换后刷新列表行配色（原生 tk.Label 不随 CTk 主题）。

        优化缺陷R97：主列表任意簇数一律 VirtualClusterList —— 经典
        行刷新链（_apply_row_bg/展开实例区/实例选中容器）随经典
        渲染链一并删除。
        """
        # 优化缺陷R42：全屏虚拟列表同步刷新（与主列表同组件）
        fs_vl = getattr(self, "_fs_vl", None)
        if fs_vl is not None:
            fs_vl.apply_palette()
        # 虚拟模式由虚拟列表自刷（池行原生控件配色）
        if self._virtual_list is not None:
            self._virtual_list.apply_palette()

    # ==================================================================
    # 任务调度（后台线程 + 队列轮询）
    # ==================================================================
    def _on_start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        mode = self._tabview.get()

        payload: dict = {"mode": mode}
        # 全部 UI 状态必须在主线程采集（Tk 控件禁止跨线程访问）
        # 优化缺陷R85：前置输入边界校验 —— 非法格式提示并放弃启动
        # （时间格式 / R91 非法正则同路拦截）
        try:
            advanced = self._advanced_params()
        except ValueError as exc:
            self._status_label.configure(text=f"前置设置有误：{exc}")
            return
        payload["common"] = dict(
            levels=[lv for lv, var in self._level_vars.items() if var.get()],
            context_lines=self._current_context_lines(),
            rule=self._rule_key,
            # 优化缺陷R79：智能分析模式随分析任务下发（full/deep/fast）
            analysis_mode=self._analyze_key,
            # 优化缺陷R84：相似度阈值档位随分析任务下发（strict/standard/lenient）
            similarity=self._similarity_key,
            # 优化缺陷R85：时间范围/行数上限/关键词黑白名单随任务下发
            **advanced,
        )
        # 优化缺陷R85：留存本次生效参数（结果状态栏常驻过滤标签用）
        self._last_common = dict(payload["common"])
        if mode == "文件导入":
            path = self._file_entry.get().strip()
            if not path:
                messagebox.showwarning("提示", "请先选择日志文件")
                return
            # 优化缺陷R89：文件对话框多选（";" 分隔）→ 轮转文件合并
            # 分析（按 mtime 最旧在前排序，跨轮转因果链不断）
            parts = [p.strip() for p in path.split(";") if p.strip()]
            if len(parts) > 1:
                payload["rotate_files"] = _sort_rotation_oldest_first(
                    parts)
            else:
                payload["file"] = path
        elif mode == "文本粘贴":
            # 修复缺陷#11：粘贴文本读取健壮性处理
            # - Tk Text 的 get("1.0","end") 恒返回尾部换行（strip 去除）；
            # - BOM（\ufeff）非空白字符 strip 不去除，会把首行变成
            #   无结构行导致解析失败 —— 显式剥离；
            # - \r\n / \r 由 run_text 的 splitlines 正确分行，此处不动。
            text = self._paste_box.get("1.0", "end").strip()
            text = text.lstrip("\ufeff").strip()
            if not text:
                messagebox.showwarning("提示", "请先粘贴日志文本")
                return
            payload["text"] = text
        else:
            paths = [e.get().strip() for e in self._compare_entries
                     if e.get().strip()]
            if len(paths) < 2:
                messagebox.showwarning("提示", "对比分析至少需要 2 个日志文件")
                return
            payload["files"] = paths

        # 重置状态并启动
        self._result = None
        self._compare_results = []
        self._displayed = []
        self._reset_rule_hint()     # 优化缺陷R71：新分析清除旧体检提示
        self._clear_list()
        self._detail_box.configure(state="normal")
        self._detail_box.delete("1.0", "end")
        self._detail_box.configure(state="disabled")

        self._cancel_event.clear()
        self._set_running(True)
        self._done_label.configure(text="")     # 优化缺陷R105：新分析清空旧完成串
        self._progress_label.configure(text="解析中…")
        self._progress_bar.configure(mode="indeterminate")
        self._progress_bar.start()

        self._worker = threading.Thread(target=self._run_worker,
                                        args=(payload,), daemon=True)
        self._worker.start()

    def _run_worker(self, payload: dict) -> None:
        """后台线程：调用核心管线（UI 更新一律经 queue 回主线程）。"""
        common = payload["common"]
        progress = lambda d: self._queue.put(("progress", d))  # noqa: E731
        try:
            if payload["mode"] == "多文件对比":
                # 修复缺陷R80：对比模式内置 analyze=False（diff 不需要
                # 智能标记），不下发 analysis_mode（compare_files 不收）；
                # 优化缺陷R84：similarity 同样不下发（compare_files 不收）；
                # 优化缺陷R85：时间范围/行数上限不下发（compare_files
                # 不收）；include/exclude 为其原生参数，照常透传；
                # 优化缺陷R88/R91：编码指定与关键词正则开关已透传支持
                cmp_common = {k: v for k, v in payload["common"].items()
                              if k not in ("analysis_mode", "similarity",
                                           "time_start", "time_end",
                                           "tod_start", "tod_end",
                                           "max_lines")}
                results = compare_files(payload["files"], **cmp_common)
                self._queue.put(("compare_done", results))
            elif payload["mode"] == "文件导入" and "rotate_files" in payload:
                # 优化缺陷R89：轮转文件合并分析（已按最旧在前排序）
                result = analyze_files(payload["rotate_files"],
                                       analyze=True,
                                       progress_cb=progress,
                                       cancel_event=self._cancel_event,
                                       **common)
                self._queue.put(("done", result))
            elif payload["mode"] == "文件导入":
                result = analyze_file(payload["file"], analyze=True,
                                      progress_cb=progress,
                                      cancel_event=self._cancel_event, **common)
                self._queue.put(("done", result))
            else:
                # 文本已是 str 无编码概念：剔除 encoding（analyze_text
                # 不收；文件/轮转/对比模式才透传编码指定）
                text_common = {k: v for k, v in common.items()
                               if k != "encoding"}
                result = analyze_text(payload["text"], analyze=True,
                                      progress_cb=progress,
                                      cancel_event=self._cancel_event,
                                      **text_common)
                self._queue.put(("done", result))
        except FileNotFoundError as exc:
            self._queue.put(("error", f"文件不存在：{exc}"))
        except Exception as exc:  # 规则文件错误等
            self._queue.put(("error", f"处理失败：{exc}"))

    def _on_cancel(self) -> None:
        # 完成后「取消」按钮保持可点击（规范要求），空任务时仅提示
        if not (self._worker and self._worker.is_alive()):
            self._status_label.configure(text="当前没有进行中的分析任务")
            return
        self._cancel_event.set()
        self._progress_label.configure(text="正在取消…")
        self._cancel_btn.configure(state="disabled")

    # ------------------------------------------------------------------
    # 优化缺陷R105：实时 Tail 监控（盯文件尾部增量，新错误实时重算）
    # ------------------------------------------------------------------
    _TAIL_POLL_SEC = 1.0        # 尾部增量轮询间隔（后台线程）
    _TAIL_REFRESH_MS = 1500     # GUI 刷新节拍（有新行且空闲才重算）

    def _on_tail_toggle(self) -> None:
        """📡 实时监控开关：开启盯当前文件，再点停止。"""
        if self._tailing:
            self._stop_tail("实时监控已停止")
            return
        if self._tabview.get() != "文件导入":
            messagebox.showwarning("实时监控",
                                   "实时监控仅支持「文件导入」页签")
            return
        parts = [p.strip() for p in self._file_entry.get().split(";")
                 if p.strip()]
        if len(parts) != 1 or not os.path.isfile(parts[0]):
            messagebox.showwarning(
                "实时监控", "请先选择单个日志文件（多选轮转合并不支持监控）")
            return
        self._tail_path = parts[0]
        # 编码：手动指定优先，否则一次性探测（监控期间不变）
        enc = _ENCODING_VALUES.get(self._encoding_display)
        if enc is None:
            from pathlib import Path as _P
            from log_ai_compressor.core.encoding import detect_encoding
            enc = detect_encoding(_P(parts[0]))
        self._tail_encoding = enc
        with self._tail_lock:
            self._tail_lines = []
            self._tail_new_lines = 0
        self._tail_offset = 0
        self._tail_partial = ""
        self._tail_stop.clear()
        self._tailing = True
        self._tail_prev_errors = 0      # 优化缺陷R112：提醒基线清零
        self._tail_btn.configure(text="⏹ 停止监控", fg_color="#1f8a4c",
                                 hover_color="#176b3b")
        self._status_label.configure(
            text=f"📡 监控中：{parts[0]}（有新错误会自动刷新，再点按钮停止）")
        self._tail_thread = threading.Thread(target=self._tail_loop,
                                             daemon=True)
        self._tail_thread.start()
        self.after(self._TAIL_REFRESH_MS, self._tail_tick)

    def _stop_tail(self, note: str = "实时监控已停止") -> None:
        """停止监控：轮询线程退出、进行中的重算取消、按钮复原。"""
        self._tailing = False
        self._tail_stop.set()
        if self._worker and self._worker.is_alive():
            self._cancel_event.set()
        p = self._palette()
        self._tail_btn.configure(text="📡 实时监控",
                                 fg_color=p["accent"],
                                 hover_color=p["accent_hover"])
        self._status_label.configure(text=note)

    def _tail_loop(self) -> None:
        """后台线程：轮询文件大小，增量字节解码后追加到行缓冲。

        尾部不完整行（无 \n 结尾）留在 _tail_partial 与下一段拼接；
        文件变小视为轮转/截断，行缓冲清零从头再读。
        """
        while not self._tail_stop.is_set():
            try:
                size = os.path.getsize(self._tail_path)
                if size < self._tail_offset:
                    with self._tail_lock:
                        self._tail_lines = []
                        self._tail_new_lines = 0
                    self._tail_offset = 0
                    self._tail_partial = ""
                if size > self._tail_offset:
                    with open(self._tail_path, "rb") as fh:
                        fh.seek(self._tail_offset)
                        chunk = fh.read(size - self._tail_offset)
                    self._tail_offset = size
                    text = self._tail_partial + chunk.decode(
                        self._tail_encoding, errors="replace")
                    lines = text.split("\n")
                    self._tail_partial = lines.pop()     # 尾段可能不完整
                    if lines:
                        with self._tail_lock:
                            self._tail_lines.extend(lines)
                            self._tail_new_lines += len(lines)
            except OSError:
                pass
            self._tail_stop.wait(self._TAIL_POLL_SEC)

    def _tail_tick(self) -> None:
        """GUI 节拍：有新行且分析空闲 → 按当前过滤器全量重算。

        重算口径与「开始分析」完全一致（级别/上下文/关键词/规则等
        实时采集），结果走 tail_done 队列事件回主线程渲染。
        """
        if not self._tailing:
            return
        self.after(self._TAIL_REFRESH_MS, self._tail_tick)
        if self._worker and self._worker.is_alive():
            return                                    # 上次重算未完成
        with self._tail_lock:
            if self._tail_new_lines <= 0:
                return
            new_n = self._tail_new_lines
            self._tail_new_lines = 0
            snapshot = list(self._tail_lines)
        try:
            advanced = self._advanced_params()
        except ValueError:
            return
        common = dict(
            levels=[lv for lv, var in self._level_vars.items() if var.get()],
            context_lines=self._current_context_lines(),
            rule=self._rule_key,
            analysis_mode=self._analyze_key,
            similarity=self._similarity_key,
            **advanced,
        )
        common.pop("encoding", None)     # analyze_text 不收 encoding
        self._cancel_event.clear()
        self._set_running(True)
        self._progress_bar.configure(mode="indeterminate")
        self._progress_bar.start()
        tail_path = self._tail_path

        def work() -> None:
            try:
                result = analyze_text(
                    "\n".join(snapshot), source=tail_path,
                    analyze=True, cancel_event=self._cancel_event,
                    **common)
                self._queue.put(("tail_done", (result, new_n)))
            except Exception as exc:
                self._queue.put(("error", f"监控刷新失败：{exc}"))

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    def _alert_new_errors(self, n: int) -> None:
        """优化缺陷R112：监控到新错误 —— 提示音 + 任务栏闪烁提醒
        （窗口未聚焦时才闪，盯着屏幕不打扰）。"""
        try:
            self.bell()
        except tk.TclError:
            pass
        try:
            if self.focus_displayof() is not None:
                return                            # 已聚焦不闪
            import ctypes

            class FLASHWINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint),
                            ("hwnd", ctypes.c_void_p),
                            ("dwFlags", ctypes.c_uint),
                            ("uCount", ctypes.c_uint),
                            ("dwTimeout", ctypes.c_uint)]

            # FLASHW_ALL(3)：标题栏 + 任务栏按钮同闪 3 次
            info = FLASHWINFO(ctypes.sizeof(FLASHWINFO),
                              self.winfo_frame(), 3, 3, 0)
            ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
        except (tk.TclError, AttributeError, OSError):
            pass

    def _poll_queue(self) -> None:
        """队列轮询（80ms 周期）。

        健壮性设计：先重调度再处理事件——任何处理器异常都不会
        终止轮询循环（否则 GUI 将永久失去响应）；异常降级为
        状态栏提示并打印到 stderr 便于定位。
        """
        self.after(80, self._poll_queue)
        try:
            while True:
                kind, data = self._queue.get_nowait()
                if kind == "progress":
                    self._update_progress(data)
                elif kind == "done":
                    self._on_result(data)
                elif kind == "tail_done":
                    # 优化缺陷R105：监控刷新（渲染同正常结果，状态栏
                    # 改监控口径）；优化缺陷R112：新错误任务栏闪烁提醒
                    result, new_n = data
                    prev_err = self._tail_prev_errors
                    self._on_result(result)
                    if self._tailing:
                        cur_err = result.stats.error_entries
                        if cur_err > prev_err:
                            self._alert_new_errors(cur_err - prev_err)
                        self._tail_prev_errors = cur_err
                        self._status_label.configure(
                            text=f"📡 监控中 · 本拍 +{new_n} 行 · "
                                 f"{self._tail_path}")
                elif kind == "compare_done":
                    self._on_compare_result(data)
                elif kind == "error":
                    self._on_error(data)
        except queue.Empty:
            pass
        except Exception:
            import traceback
            traceback.print_exc()
            self._status_label.configure(text="内部错误（详情见控制台），轮询已继续")

    def _update_progress(self, data: dict) -> None:
        if data.get("phase") == "parsing":
            self._progress_label.configure(
                text=f"已处理 {data['lines']:,} 行 | "
                     f"{_rate_text(data['lps'])} | "
                     f"错误种类 {data['clusters']} | 耗时 {data['elapsed']:.1f}s")

    def _on_error(self, message: str) -> None:
        self._set_running(False)
        self._progress_bar.stop()
        self._progress_bar.set(0)
        self._progress_label.configure(text="失败")
        self._status_label.configure(text=message)
        messagebox.showerror("错误", message)

    # ==================================================================
    # 结果渲染
    # ==================================================================
    def _on_result(self, result: AnalysisResult) -> None:
        # 先赋值再切换按钮状态：状态机依据 self._result 判断可用性
        self._result = result
        self._compare_results = []
        self._set_running(False)
        self._progress_bar.stop()
        self._progress_bar.set(1.0)
        s = result.stats
        # 优化缺陷R85：上限采样命中与取消中断分文案显示
        if s.limit_hit:
            suffix = "（已达行数上限）"
        else:
            suffix = "（已取消，增量结果）" if s.truncated else ""
        # 优化缺陷R99：Token 估算 + 压缩率（贴合「压缩投喂大模型」
        # 定位 —— 压缩侧取投喂用精简摘要的文本量）
        token_text = token_status_text(
            result, brief_summary(result, top_n=len(result.clusters)))
        self._token_status = token_text     # 导出对话框预估行复用
        # 优化缺陷R99：导出对话框预估行全文（原始 → 压缩 双向）
        self._token_report = token_report_line(
            result, brief_summary(result, top_n=len(result.clusters)))
        # 优化缺陷R105：「完成」状态串写底栏 _done_label（原进度区
        # 只剩一列宽，按钮行第六颗按钮后不再够用）
        self._done_label.configure(
            text=f"完成：{s.total_lines:,} 行 | 错误 {s.error_lines:,} 行 | "
                 f"{len(result.clusters)} 种 | {token_text} | "
                 f"{_rate_text(s.lines_per_second)}"
                 f"{suffix}")
        self._progress_label.configure(text="就绪")
        # 优化缺陷R85：生效中的前置过滤器常驻状态栏标签（防静默隐藏
        # 错误 —— 三项均不持久化，重启清零，本标签为可见性兜底）
        tags = []
        common = getattr(self, "_last_common", {}) or {}
        if (common.get("time_start") or common.get("time_end")
                or common.get("tod_start") or common.get("tod_end")):
            tags.append("时间窗")
        if common.get("max_lines"):
            tags.append(f"上限{MAXLINES_DISPLAY.get(self._maxlines_key, '')}")
        if common.get("include"):
            tags.append(f"包含[{','.join(common['include'])}]")
        if common.get("exclude"):
            tags.append(f"排除[{','.join(common['exclude'])}]")
        if common.get("use_regex"):
            # 优化缺陷R91：正则模式常驻标签（同三项前置过滤的兜底语义）
            tags.append("正则")
        tag_text = (" | 生效过滤: " + " ".join(tags)) if tags else ""
        self._status_label.configure(
            text=f"{s.source} | 编码 {s.encoding} | 规则 {s.rule_name} | "
                 f"时间范围 {format_timestamp(s.time_start)} ~ "
                 f"{format_timestamp(s.time_end)} | 智能分析耗时 "
                 f"{s.analysis_cost * 1000:.0f}ms{tag_text}")
        self._render_cluster_list()
        self._save_config()
        # 优化缺陷R111：新结果刷新屏蔽按钮态与 🚫 计数指示器
        self._detail_cluster = None
        self._update_mute_btn()
        self._update_mute_indicator()
        # 优化缺陷R100：分析完成自动入历史（回放自身/监控刷新不重复入库）
        if not self._from_history and not self._tailing:
            self._history.add(result)
        # 优化缺陷R71：规则体检（疑似不匹配时覆盖状态栏为可点击提示）
        self._check_rule_health(result)
        if self._displayed:
            # 优化缺陷R45：搜索框有残留关键字时选中首个【可见】簇
            # （原固定选 0，簇 0 被过滤时会选中不可见行）
            # 优化缺陷R111：可见 = 关键字命中且未被屏蔽（屏蔽显示态除外）
            first = next((i for i, c in enumerate(self._displayed)
                          if self._cluster_matches(c)
                          and (self._show_muted or not self._is_muted(c))),
                         None)
            if first is not None:
                self._select_cluster(first)

    # ------------------------------------------------------------------
    # 优化缺陷R71：规则体检（错误 0 行 / 时间戳识别率 <50% 提示换规则）
    # ------------------------------------------------------------------
    def _check_rule_health(self, result: AnalysisResult) -> None:
        """分析后体检：疑似规则不匹配时状态栏给可点击换规则提示。"""
        self._reset_rule_hint()
        # 优化缺陷R100：历史回放不做体检（当前输入与历史结果未必
        # 同源，体检结论会误导用户换规则重跑）
        if self._from_history:
            return
        s = result.stats
        ts_rate = s.ts_entries / max(1, s.entry_lines)
        if s.error_lines > 0 and ts_rate >= 0.5:
            return                              # 体检通过
        sample = self._current_input_sample()
        if not sample:
            return
        try:
            suggested = detect_rule(sample)
        except Exception:
            return
        if suggested == self._rule_key:
            return      # 已是最优（日志本身无错误/无时间戳，不打扰）
        reason = ("未识别到错误" if s.error_lines == 0
                  else f"时间戳识别率仅 {ts_rate:.0%}")
        self._status_label.configure(
            text=f"⚠ 可能规则不匹配（{reason}）→ 试试 "
                 f"{RULE_DISPLAY[suggested]}（点击切换并重跑）",
            text_color="#ffb300", cursor="hand2")
        self._status_label.bind(
            "<Button-1>",
            lambda e: self._apply_suggested_rule(suggested))

    def _reset_rule_hint(self) -> None:
        """清除体检提示的可点击态（新分析/新结果回填前调用）。"""
        try:
            self._status_label.unbind("<Button-1>")
            self._status_label.configure(
                cursor="", text_color=self._palette()["muted"])
        except (tk.TclError, AttributeError):
            pass

    def _current_input_sample(self) -> list:
        """当前输入的分层采样（体检重新打分用；对比模式不适用）。"""
        try:
            mode = self._tabview.get()
            if mode == "文件导入":
                path = self._file_entry.get().strip()
                if not path or not os.path.isfile(path):
                    return []
                from pathlib import Path as _P
                from log_ai_compressor.core.encoding import (
                    detect_encoding)
                from log_ai_compressor.core.pipeline import (
                    _sample_log_file)
                p = _P(path)
                return _sample_log_file(p, detect_encoding(p))
            if mode == "文本粘贴":
                text = self._paste_box.get("1.0", "end").strip()
                text = text.lstrip("\ufeff")
                return stratified_sample(text.splitlines()) if text else []
        except (tk.TclError, OSError):
            pass
        return []

    def _apply_suggested_rule(self, key: str) -> None:
        """采纳体检建议：切换到建议规则并立即重跑（缺陷R71）。"""
        self._reset_rule_hint()
        self._on_rule_changed(RULE_DISPLAY[key])
        self._on_start()

    # ------------------------------------------------------------------
    # 优化缺陷R100：分析历史弹层（最近 10 次，点击回放免重跑）
    # ------------------------------------------------------------------
    def _open_history_popup(self) -> None:
        """历史弹层：元数据列表（新→旧），点行回放，底部可清空。"""
        old = getattr(self, "_history_dlg", None)
        if old is not None and old.winfo_exists():
            old.focus_set()
            return
        entries = self._history.list()
        dlg = ctk.CTkToplevel(self)
        self._history_dlg = dlg
        dlg.title("分析历史（最近 10 次）")
        dlg.transient(self)
        dlg.resizable(False, False)
        rows = max(1, len(entries))
        w, h = 640, min(500, 150 + 48 * rows)
        self.update_idletasks()
        cx = self.winfo_x() + max(0, (self.winfo_width() - w) // 2)
        cy = self.winfo_y() + max(0, (self.winfo_height() - h) // 2)
        dlg.geometry(f"{w}x{h}+{cx}+{cy}")

        ctk.CTkLabel(dlg, text="点击任意一条直接回放完整分析结果（免重跑）",
                     font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=16, pady=(12, 4))
        body = ctk.CTkScrollableFrame(dlg)
        body.pack(fill="both", expand=True, padx=16, pady=(0, 6))
        if not entries:
            ctk.CTkLabel(body, text="暂无历史记录（分析完成后自动保存）",
                         text_color="#8fa4b8").pack(pady=18)
        for meta in entries:
            saved = time.strftime("%m-%d %H:%M",
                                  time.localtime(meta.get("saved_at", 0)))
            src = os.path.basename(meta.get("source", "")) or \
                meta.get("source", "")
            text = (f"{saved}  |  {src}  |  "
                    f"{meta.get('total_lines', 0):,} 行  |  "
                    f"错误 {meta.get('error_lines', 0):,}  |  "
                    f"{meta.get('clusters', 0)} 种  |  "
                    f"{meta.get('rule', '')}")
            p = self._palette()
            row = ctk.CTkButton(
                body, text=text, anchor="w", height=36,
                fg_color=p["card"], text_color=p["text"],
                hover_color=p["row_hover"],
                command=lambda hid=meta.get("id", ""):
                    self._restore_from_history(hid))
            row.pack(fill="x", pady=3)

        foot = ctk.CTkFrame(dlg, fg_color="transparent")
        foot.pack(fill="x", padx=16, pady=(0, 12))
        ctk.CTkButton(foot, text="关闭", width=90, fg_color="#6b7280",
                      command=dlg.destroy).pack(side="right", padx=(6, 0))
        if entries:
            ctk.CTkButton(foot, text="清空历史", width=90,
                          fg_color="#8a3a3a",
                          command=self._clear_history).pack(side="right")
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.after(60, dlg.focus_set)

    def _restore_from_history(self, hid: str) -> None:
        """回放历史结果（优化缺陷R100）：完整恢复列表/详情/图表数据。"""
        result = self._history.load(hid)
        if result is None:
            messagebox.showwarning(
                "分析历史", "该条历史已损坏或版本不兼容，无法回放")
            return
        dlg = getattr(self, "_history_dlg", None)
        if dlg is not None and dlg.winfo_exists():
            dlg.destroy()
        self._from_history = True
        try:
            self._on_result(result)
        finally:
            self._from_history = False
        # 回放标注（状态栏前缀，区分实时分析结果）
        self._status_label.configure(
            text=f"历史回放 · {self._status_label.cget('text')}")

    def _clear_history(self) -> None:
        """清空全部历史并刷新弹层（优化缺陷R100）。"""
        self._history.clear()
        dlg = getattr(self, "_history_dlg", None)
        if dlg is not None and dlg.winfo_exists():
            dlg.destroy()
        self._status_label.configure(text="分析历史已清空")

    def _clear_list(self) -> None:
        """清空左侧列表（销毁虚拟模式 / 经典行，恢复经典滚动容器）。"""
        # 修复缺陷R6：虚拟列表销毁 + 经典容器恢复
        if self._virtual_list is not None:
            self._virtual_list.destroy()
            self._virtual_list = None
        # 修复缺陷R9：恢复经典水平滚动条（虚拟模式自带独立的 hbar）
        if _widget_alive(self._cluster_list):
            self._cluster_list.grid()          # grid_remove 后恢复
            if _widget_alive(self._list_hbar):
                self._list_hbar.grid()
            for child in self._cluster_list.winfo_children():
                child.destroy()
        self._cluster_rows = []

    # ------------------------------------------------------------------
    # 优化缺陷R45：结果搜索（显示层过滤，不触发重新分析）
    # ------------------------------------------------------------------
    def _on_search_changed(self, *args) -> None:
        """搜索输入防抖（200ms）：连续输入合并为一次列表刷新。"""
        if self._search_job is not None:
            try:
                self.after_cancel(self._search_job)
            except (tk.TclError, ValueError):
                pass
        self._search_job = self.after(200, self._apply_search_filter)

    def _cluster_matches_kw(self, cluster: ErrorCluster, kw: str) -> bool:
        """关键字匹配（摘要/模块/级别/优先级档，小写子串；主窗口
        与全屏同口径；空关键字全部命中）。优化缺陷R113：含
        and/or/not 操作符时按布尔表达式求值。"""
        if not kw:
            return True
        hay = (f"{cluster.summary} {cluster.module} "
               f"{cluster.level} {cluster.priority_label}").lower()
        return _kw_match(hay, kw)

    def _cluster_matches(self, cluster: ErrorCluster) -> bool:
        """搜索关键字匹配（主窗口关键字；空关键字全部命中）。"""
        return self._cluster_matches_kw(cluster, self._search_kw)

    # ------------------------------------------------------------------
    # 优化缺陷R111：已知错误屏蔽（message_template 持久化，默认隐藏）
    # ------------------------------------------------------------------
    @staticmethod
    def _mute_key(cluster: ErrorCluster) -> str:
        """屏蔽指纹：消息模板（掩码后跨运行稳定）；无模板退化为摘要。"""
        return cluster.message_template or cluster.summary

    def _is_muted(self, cluster: ErrorCluster) -> bool:
        return self._mute_key(cluster) in self._muted

    def _toggle_mute_current(self) -> None:
        """详情面板「🚫 屏蔽/↩ 恢复」：切换当前簇屏蔽态并持久化。"""
        cluster = self._detail_cluster
        if cluster is None:
            return
        key = self._mute_key(cluster)
        if key in self._muted:
            self._muted.discard(key)
            self._status_label.configure(
                text=f"已恢复显示：{cluster.summary[:40]}")
        else:
            self._muted.add(key)
            self._status_label.configure(
                text=f"已屏蔽：{cluster.summary[:40]}"
                     f"（列表上方 🚫 计数按钮可临时显示）")
        self._save_config()
        self._update_mute_btn()
        self._update_mute_indicator()
        # 列表按新屏蔽态重建（保留展开/选中；被屏蔽的选中簇由
        # _apply_search_filter 的兜底逻辑改选首个可见簇）
        if self._result is not None:
            self._render_cluster_list(preserve_state=True)

    def _update_mute_btn(self) -> None:
        """屏蔽按钮文案/配色随详情簇刷新。"""
        btn = getattr(self, "_mute_btn", None)
        if btn is None:
            return
        cluster = self._detail_cluster
        if cluster is not None and self._is_muted(cluster):
            btn.configure(text="↩ 取消屏蔽", state="normal",
                          fg_color="#6b7280", hover_color="#4b5563")
        else:
            p = self._palette()
            btn.configure(text="🚫 屏蔽", fg_color=p["accent"],
                          hover_color=p["accent_hover"],
                          state="normal" if cluster is not None
                          else "disabled")

    def _update_mute_indicator(self) -> None:
        """列表头 🚫 计数指示器：当前结果中被屏蔽簇数 >0 才显示。"""
        btn = getattr(self, "_mute_ind_btn", None)
        if btn is None:
            return
        n = 0
        if self._result is not None:
            n = sum(1 for c in self._result.clusters if self._is_muted(c))
        if n <= 0:
            btn.grid_remove()
            return
        btn.configure(text=f"🚫 {n}")
        p = self._palette()
        if self._show_muted:
            btn.configure(fg_color=p["accent"],
                          hover_color=p["accent_hover"])
        else:
            btn.configure(fg_color="#6b7280", hover_color="#4b5563")
        btn.grid(row=0, column=2, padx=(0, 6), sticky="e")

    def _toggle_show_muted(self) -> None:
        """🚫 指示器点击：切换显示/隐藏已屏蔽簇（会话态）。"""
        self._show_muted = not self._show_muted
        self._update_mute_indicator()
        if self._result is not None:
            self._render_cluster_list(preserve_state=True)

    def _search_targets(self, kw: str) -> list:
        """优化缺陷R56：扁平实例导航序列 [(簇idx, 实例idx)…]。

        每个匹配簇内「实例行摘要含关键字」的全部实例按列表顺序
        收集；簇匹配（摘要/级别/优先级档）但无实例行含关键字时
        回退该簇首实例（典型样例），保证每个匹配簇均可定位到。
        """
        seq = []
        for ci, cluster in enumerate(self._displayed):
            if not self._cluster_matches_kw(cluster, kw):
                continue
            # 实例命中判定含级别/模块上下文 —— 实例摘要本身不含
            # 级别词（如 error），仅按摘要判定会把按级别命中的簇
            # 全部回退首实例（导航退化为簇级，R56 测试实测暴露）
            hits = [ii for ii, inst in enumerate(cluster.instances)
                    if _kw_match((f"{inst.summary} {cluster.level} "
                                  f"{cluster.module}").lower(), kw)]
            if hits:
                seq.extend((ci, ii) for ii in hits)
            elif cluster.instances:
                seq.append((ci, 0))
        return seq

    @staticmethod
    def _seq_index(seq: list, cidx: int, iidx=None) -> int:
        """导航序列中的 1-based 位置（点实例按实例查、点簇取该簇
        首个命中实例；不在序列归 0 = 未导航，优化缺陷R56）。"""
        if iidx is not None:
            try:
                return seq.index((cidx, iidx)) + 1
            except ValueError:
                return 0
        for pos, (ci, _ii) in enumerate(seq):
            if ci == cidx:
                return pos + 1
        return 0

    def _apply_search_filter(self) -> None:
        """应用搜索过滤（防抖后执行）。

        虚拟模式仅刷新视图行（展开/选中天然保留 —— 视图行索引即
        _displayed 索引）；经典模式保留展开/选中重建可见行。
        """
        # 直接调用（测试/程序化）时取消挂起的防抖任务，避免重复刷新
        if self._search_job is not None:
            try:
                self.after_cancel(self._search_job)
            except (tk.TclError, ValueError):
                pass
        self._search_job = None
        self._search_kw = self._search_var.get().strip().lower()
        # 优化缺陷R50：关键字变化重置查找导航（计数回到 0/y 未定位态）
        self._search_nav = 0
        if self._result is None:
            self._update_search_count()
            return
        if self._virtual_list is not None:
            rows = self._build_view_rows()
            if rows:
                self._virtual_list.set_data(rows)
                # 优化缺陷R46：关键字变化即时刷新详情面板高亮（虚拟
                # 模式 set_data 不重填详情，选中态保持不变）
                self._refresh_current_detail()
            else:
                # 优化缺陷R45/R97：无匹配时整列表重建走空态提示
                # （set_data([]) 只会留空白画布，用户读不出原因）
                self._render_cluster_list(preserve_state=True)
        elif self._displayed:
            self._render_cluster_list(preserve_state=True)
        self._update_search_count()
        # 优化缺陷R46：当前选中簇被过滤掉（或无选中）时自动选中
        # 首个匹配簇 —— 否则详情面板停留陈旧内容、无关键字高亮
        # 优化缺陷R111：匹配口径同视图行（被屏蔽簇不算可见匹配）
        matches = [i for i, c in enumerate(self._displayed)
                   if self._cluster_matches(c)
                   and (self._show_muted or not self._is_muted(c))]
        if matches and self._selected_row not in matches:
            self._select_cluster(matches[0])

    def _on_search_enter(self, forward: bool = True):
        """搜索框 Enter（Shift+Enter 反向）：定位下/上一个命中实例。

        优化缺陷R46：先冲刷挂起的防抖过滤（关键字立即生效）。
        优化缺陷R56：扁平实例导航 —— 序列 = 各匹配簇内「实例行
        摘要含关键字」的全部实例（无命中实例的簇回退首实例）；
        定位时自动展开簇 ▼、选中该实例行、滚入视口，右侧详情
        显示该实例自身上下文（高亮聚焦，不再只见簇父级）。
        """
        if self._search_job is not None:
            self._apply_search_filter()
        if self._result is None or not self._search_kw:
            return "break"
        seq = self._search_targets(self._search_kw)
        if not seq:
            return "break"
        n = len(seq)
        nav = self._search_nav
        if nav == 0:
            nav = 1 if forward else n
        elif forward:
            nav = nav % n + 1
        else:
            nav = n if nav == 1 else nav - 1
        self._search_nav = nav
        ci, ii = seq[nav - 1]
        self._jump_to_instance(ci, ii)
        self._update_search_count()
        return "break"

    def _jump_to_instance(self, ci: int, ii: int) -> None:
        """优化缺陷R56：定位实例行 —— 确保簇展开 + 选中实例 +
        滚入视口（主列表/全屏虚拟列表经共享态同步）。"""
        if ci not in self._expanded_clusters:
            self._toggle_cluster_expand(ci)
        self._select_instance(ci, ii)
        self._see_instance_row(ci, ii)

    def _see_instance_row(self, ci: int, ii: int) -> None:
        """滚动左侧列表使实例行可见（优化缺陷R56；R97 统一虚拟渲染）。"""
        if self._virtual_list is not None:
            self._virtual_list.see_instance(ci, ii)

    def _see_cluster_row(self, idx: int) -> None:
        """滚动左侧列表使簇行可见（Enter 跳转定位；R97 统一虚拟渲染）。"""
        if self._virtual_list is not None:
            self._virtual_list.see_cluster(idx)

    def _refresh_current_detail(self) -> None:
        """按当前选中态重填详情面板（优化缺陷R46：搜索关键字变化
        后即时更新「searchkw」高亮；实例选中优先于簇选中）。"""
        if self._selected_inst is not None:
            ci, ii = self._selected_inst
            if 0 <= ci < len(self._displayed):
                cluster = self._displayed[ci]
                if 0 <= ii < len(cluster.instances):
                    self._select_instance(ci, ii)
                    return
        if 0 <= self._selected_row < len(self._displayed):
            self._select_cluster(self._selected_row)

    def _update_search_count(self) -> None:
        """搜索结果计数（过滤时显示「X / Y 条」）。

        优化缺陷R49：计数显示在输入框右侧的恒定占位影子框内 ——
        有计数时影子框显形（与输入框同底色，fg_color 元组随主题
        自适应），无计数时透明隐形；显隐切换不改变任何布局需求。
        """
        box = getattr(self, "_search_count_box", None)
        label = getattr(self, "_search_count", None)
        if label is None:
            return
        show = bool(self._search_kw and self._result is not None)
        if box is not None:
            box.configure(
                fg_color=(self._search_entry.cget("fg_color")
                          if show else "transparent"))
        if not show:
            label.configure(text="")
            return
        # 优化缺陷R56：计数 = 导航序号 / 命中实例总条数（扁平实例
        # 导航序列长度，随关键字动态变化；输入后未导航为 0/y）
        total = len(self._search_targets(self._search_kw))
        label.configure(text=f"{self._search_nav} / {total} 条")

    def _on_fs_search_enter(self, forward: bool = True):
        """全屏搜索 Enter（Shift+Enter 反向）：定位下/上一个命中实例。

        优化缺陷R53：与主窗口同一导航语义（0/y→1/y→…→y/y→回绕）。
        优化缺陷R56：扁平实例导航 —— 自动展开簇、选中实例行、
        滚入全屏虚拟列表视口，详情经共享选中态联动到该实例。
        """
        if self._result is None or not getattr(self, "_fs_search_kw", ""):
            return "break"
        seq = self._search_targets(self._fs_search_kw)
        if not seq:
            return "break"
        n = len(seq)
        nav = getattr(self, "_fs_search_nav", 0)
        if nav == 0:
            nav = 1 if forward else n
        elif forward:
            nav = nav % n + 1
        else:
            nav = n if nav == 1 else nav - 1
        self._fs_search_nav = nav
        ci, ii = seq[nav - 1]
        self._jump_to_instance(ci, ii)        # 共享选中态：全屏详情联动
        fs_vl = getattr(self, "_fs_vl", None)
        if fs_vl is not None:
            fs_vl.see_instance(ci, ii)
        self._update_fs_search_count()
        return "break"

    def _update_fs_search_count(self) -> None:
        """全屏搜索计数影子框（优化缺陷R53：与主窗口同款 x/y 条）。

        有计数显形（与输入框同底色）、无计数透明隐形；x=导航序号
        （点击簇/实例行同步），y=全屏关键字实时匹配总数。
        """
        box = getattr(self, "_fs_count_box", None)
        label = getattr(self, "_fs_count", None)
        if label is None:
            return
        show = bool(getattr(self, "_fs_search_kw", "")
                    and self._result is not None)
        if box is not None:
            entry = getattr(self, "_fs_search_entry", None)
            box.configure(
                fg_color=(entry.cget("fg_color")
                          if show and entry is not None
                          else "transparent"))
        if not show:
            label.configure(text="")
            return
        total = len(self._search_targets(
            getattr(self, "_fs_search_kw", "")))
        label.configure(text=f"{self._fs_search_nav} / {total} 条")

    def _apply_fd_search(self) -> None:
        """详情全屏文内查找（优化缺陷R54）。

        输入关键字 → 全部匹配黄底「searchkw」高亮 + 计数 0/y 条
        （y=匹配处数实时计算）；关键字变化重置导航序号与当前匹配
        橙底标签；清空关键字移除全部查找高亮、影子框隐形。
        """
        box = getattr(self, "_fs_detail_box", None)
        var = getattr(self, "_fd_search_var", None)
        if box is None or var is None:
            return
        kw = var.get().strip().lower()
        self._fd_search_kw = kw
        self._fd_search_nav = 0
        self._fd_matches = []
        try:
            box.tag_remove("searchkw", "1.0", "end")
            box.tag_remove("fdcur", "1.0", "end")
        except tk.TclError:
            pass
        if kw:
            lowered = box.get("1.0", "end").lower()
            start = 0
            while True:
                pos = lowered.find(kw, start)
                if pos < 0:
                    break
                idx = self._index_of_offset(box, pos)
                if idx:
                    line, col = idx
                    box.tag_add("searchkw", f"{line}.{col}",
                                f"{line}.{col + len(kw)}")
                    self._fd_matches.append((line, col))
                start = pos + len(kw)
        self._update_fd_search_count()

    def _on_fd_search_enter(self, forward: bool = True):
        """详情全屏查找 Enter（Shift+Enter 反向）：定位下/上一个匹配。

        优化缺陷R54：与列表搜索同一导航语义 —— 输入后 0/y（未定位）；
        Enter 1/y→2/y→…→y/y→回绕 1/y，Shift+Enter 反向；当前匹配
        橙底「fdcur」高亮并滚动进视口。
        """
        box = getattr(self, "_fs_detail_box", None)
        n = len(self._fd_matches)
        if box is None or not self._fd_search_kw or n == 0:
            return "break"
        nav = self._fd_search_nav
        if nav == 0:
            nav = 1 if forward else n
        elif forward:
            nav = nav % n + 1
        else:
            nav = n if nav == 1 else nav - 1
        self._fd_search_nav = nav
        line, col = self._fd_matches[nav - 1]
        box.tag_remove("fdcur", "1.0", "end")
        box.tag_add("fdcur", f"{line}.{col}",
                    f"{line}.{col + len(self._fd_search_kw)}")
        box.see(f"{line}.{col}")
        self._update_fd_search_count()
        return "break"

    def _update_fd_search_count(self) -> None:
        """详情全屏查找计数影子框（优化缺陷R54：同款 x/y 条）。

        有关键字显形（与输入框同底色）、清空透明隐形；x=定位序号，
        y=详情文本内关键字匹配处数。
        """
        box = getattr(self, "_fd_count_box", None)
        label = getattr(self, "_fd_count", None)
        if label is None:
            return
        show = bool(self._fd_search_kw)
        if box is not None:
            entry = getattr(self, "_fd_search_entry", None)
            box.configure(
                fg_color=(entry.cget("fg_color")
                          if show and entry is not None
                          else "transparent"))
        if not show:
            label.configure(text="")
            return
        label.configure(
            text=f"{self._fd_search_nav} / {len(self._fd_matches)} 处")

    def _render_cluster_list(self, preserve_state: bool = False) -> None:
        """左侧错误列表：全部错误行（图标/优先级/次数行 + 单行摘要）。

        修复缺陷：原单行 CTkButton 长文本溢出右侧且无横向滚动能力，
        R9 起摘要单行不换行 + 底部水平滚动条左右滑动查看完整内容。
        优化缺陷R43：Top N 截断删除（全量簇显示，不再「其余 N 种」提示）。
        优化缺陷R45：preserve_state=True（搜索过滤重建）时保留簇
        展开与行选中状态；搜索关键字过滤仅作用于显示，_displayed
        索引语义不变。
        优化缺陷R97：渲染路径统一 —— 任意簇数一律 VirtualClusterList
        （与全屏列表同一组件）。此前小簇数走经典 CTkFrame 行、大簇数
        走虚拟行，两条渲染链的选中描边/3D 按压/圆角实现各自为政，
        勾不勾 INFO（簇数增减跨阈值）手感与观感明显分裂（用户两次
        截图举证）；经典行渲染链（_make_cluster_row 等）随之删除。
        """
        assert self._result is not None
        # 优化缺陷R45：同步搜索关键字（重新分析后框内文本仍然生效）
        self._search_kw = self._search_var.get().strip().lower()
        # 优化缺陷R45：搜索重建前捕获展开/选中状态（事后恢复）
        expanded = set(self._expanded_clusters) if preserve_state else set()
        selected = self._selected_row if preserve_state else -1
        self._displayed = list(self._result.clusters)
        self._update_search_count()
        self._clear_list()
        # 清理随列表销毁的动态 muted 标签（防登记表无限累积）
        self._muted_labels = [
            w for w in self._muted_labels
            if not hasattr(w, "winfo_exists") or _widget_alive(w)]
        self._selected_row = -1
        # 修复缺陷R16：重新渲染清空簇展开与实例选中状态
        self._expanded_clusters.clear()
        self._selected_inst = None
        if not self._displayed:
            empty = ctk.CTkLabel(self._cluster_list, text="未发现符合条件的错误")
            empty.pack(pady=20)
            self._muted_labels.append(empty)
            return
        # 优化缺陷R97：统一虚拟渲染（小簇数同样走 VirtualClusterList）
        view_rows = self._build_view_rows()
        if not view_rows:
            # 优化缺陷R45：关键字过滤后无匹配簇的空态提示
            empty = ctk.CTkLabel(self._cluster_list,
                                 text="无匹配的错误簇（调整搜索关键字）")
            empty.pack(pady=20)
            self._muted_labels.append(empty)
            return
        self._cluster_list.grid_remove()
        # 修复缺陷R9：虚拟模式隐藏经典 hbar（虚拟列表自带 hbar）
        if _widget_alive(self._list_hbar):
            self._list_hbar.grid_remove()
        self._virtual_list = VirtualClusterList(self._list_host, self)
        self._cluster_rows = self._virtual_list.slots
        # 修复缺陷R16：虚拟列表数据为视图行（簇行+展开实例行）
        self._virtual_list.set_data(view_rows)
        if not preserve_state:
            return
        # 优化缺陷R45/R97：恢复搜索前的展开/选中状态（仍可见的才
        # 恢复；展开经 toggle 走 update_rows，浏览位置不跳动）
        for idx in sorted(expanded):
            if (0 <= idx < len(self._displayed)
                    and self._cluster_matches(self._displayed[idx])):
                self._toggle_cluster_expand(idx)
        if (0 <= selected < len(self._displayed)
                and self._cluster_matches(self._displayed[selected])):
            self._select_cluster(selected)

    @staticmethod
    def _is_dark_mode() -> bool:
        """兼容旧接口：CTk appearance 模式是否为 dark。

        修复缺陷R1：四态下 blue/green 映射到 light 基础，
        此方法仅反映 CTk 底层模式（供 Tooltip 等外部类使用）。
        """
        return ctk.get_appearance_mode().lower() == "dark"

    @staticmethod
    def _resolve_row_color(color) -> str:
        """主题色元组 -> 当前模式下的实际颜色值。"""
        if isinstance(color, (tuple, list)):
            return color[1] if LogCompressorApp._is_dark_mode() else color[0]
        return color

    def _row_states(self) -> Dict[str, str]:
        """行三态配色（背景/悬停/选中），随主题切换。"""
        p = self._palette()
        return {"bg": p["row_bg"], "hover": p["row_hover"],
                "selected": p["row_selected"]}

    def _hover_row(self, idx: int, hovered: bool) -> None:
        """行悬停高亮（选中行保持选中色）。

        修复缺陷R16：池行悬停统一由 vl._hover + _fill_slot 着色
        （视图行模型下 displayed 索引与池行视图索引不再等价）。
        优化缺陷R97：经典渲染链删除后本方法仅保留虚拟语义（悬停
        事件由 VirtualClusterList 内部处理，此处为兼容旧调用的
        空操作收口）。
        """

    def _mark_selected_row(self, idx: int) -> None:
        """更新选中行高亮（清除旧选中，标记新选中；蓝色选中态）。

        修复缺陷R16：池行着色统一走 _fill_slot（按
        _selected_row/_selected_inst/_hovered 计算），避免视图索引
        与簇索引不匹配导致的错位着色。
        优化缺陷R97：经典渲染链删除后只剩虚拟路径。
        """
        self._selected_row = idx
        if self._virtual_list is not None:
            self._virtual_list._sync()

    @staticmethod
    def _row_text(cluster: ErrorCluster, with_count: bool = True) -> str:
        """行首元信息：图标 + 优先级 + 级别 + （次数） + 模块（不含摘要）。

        修复缺陷R4：with_count=False 时次数移至独立的「▶ ×N」展开
        按钮（全屏窗口用），行首不再重复显示。
        """
        if cluster.is_root_cause:
            icon = _CLUSTER_ICON["root"]
        elif cluster.anomaly in _CLUSTER_ICON and cluster.anomaly != "normal":
            # 优化缺陷R75：burst/periodic/novel/rare 统一查表
            # （优先级递降由 _mark_anomalies 保证单值）
            icon = _CLUSTER_ICON[cluster.anomaly]
        else:
            icon = _CLUSTER_ICON["normal"]
        module = f"  {cluster.module}" if cluster.module else ""
        if with_count:
            return (f"{icon} {cluster.priority_label} {cluster.level:<5} "
                    f"\u00d7{cluster.count:<4}{module}")
        return f"{icon} {cluster.priority_label} {cluster.level:<5}{module}"

    @staticmethod
    def _clip(text: str, width: int) -> str:
        return text if len(text) <= width else text[:width - 1] + "…"

    @staticmethod
    def _row_color(cluster: ErrorCluster) -> Optional[str]:
        # 修复缺陷R40：五级别五色 + 根因紫（原仅 FATAL 红色特殊化；
        # FATAL 删除后全级别着色，一眼区分严重程度）
        if cluster.is_root_cause:
            return _ROOT_COLOR
        return _LEVEL_COLORS.get(cluster.level)

    @staticmethod
    def _row_color_sel(cluster: ErrorCluster) -> Optional[str]:
        # 修复缺陷R40：选中蓝底上的调亮版级别色（替代统一白字，
        # 选中行仍能一眼区分级别严重程度）
        if cluster.is_root_cause:
            return _ROOT_COLOR_SEL
        return _LEVEL_COLORS_SEL.get(cluster.level)

    # ------------------------------------------------------------------
    # 详情渲染
    # ------------------------------------------------------------------
    def _select_cluster(self, idx: int, sync_nav: bool = False) -> None:
        if not (0 <= idx < len(self._displayed)):
            return
        self._selected_inst = None      # R16：切簇清除实例选中态
        self._mark_selected_row(idx)
        self._show_cluster_detail(self._displayed[idx])
        self._sync_fs_detail()          # 优化缺陷R42：全屏联动
        if sync_nav:
            self._sync_search_nav(idx)

    def _sync_search_nav(self, cidx: int, iidx=None) -> None:
        """优化缺陷R50：用户主动点选时同步查找导航序号（计数
        x/y 的 x），不在序列归 0（未导航）。
        优化缺陷R53：全屏窗口计数同步（独立关键字/序号，同口径）。
        优化缺陷R56：扁平实例导航 —— 点簇行同步到该簇首个命中
        实例序号；点实例行同步到该实例自身序号。"""
        if self._result is None:
            return
        if self._search_kw:
            seq = self._search_targets(self._search_kw)
            self._search_nav = self._seq_index(seq, cidx, iidx)
            self._update_search_count()
        if getattr(self, "_fs_search_kw", ""):
            seq = self._search_targets(self._fs_search_kw)
            self._fs_search_nav = self._seq_index(seq, cidx, iidx)
            self._update_fs_search_count()

    def _sync_fs_detail(self) -> None:
        """优化缺陷R42：全屏列表联动 —— 选中簇/实例时全屏详情
        同步填充 + 全屏虚拟列表选中态重绘（与主列表共享应用态，
        主窗口/全屏窗口点击互相联动）。"""
        vl = getattr(self, "_fs_vl", None)
        win = getattr(self, "_fs_list_win", None)
        if vl is None or win is None:
            return
        try:
            if not win.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            inst = getattr(self, "_selected_inst", None)
            filled = False
            if inst is not None and 0 <= inst[0] < len(self._displayed):
                cluster = self._displayed[inst[0]]
                if 0 <= inst[1] < len(cluster.instances):
                    self._fill_instance_detail(
                        self._fs_list_detail, cluster,
                        cluster.instances[inst[1]])
                    filled = True
            if not filled and 0 <= self._selected_row < len(self._displayed):
                self._fill_cluster_detail(
                    self._fs_list_detail,
                    self._displayed[self._selected_row])
        finally:
            vl._sync()      # 重绘可见槽位（选中态共享自应用层）

    def _fs_cluster_matches(self, cluster: ErrorCluster) -> bool:
        """优化缺陷R53：全屏搜索关键字匹配（与主窗口同口径）。"""
        return self._cluster_matches_kw(
            cluster, getattr(self, "_fs_search_kw", ""))

    def _fs_view_rows(self) -> list:
        """优化缺陷R42：全屏列表视图行（搜索过滤后的簇行 +
        展开簇实例行；与主列表 _build_view_rows 同构 + 关键字过滤）。
        优化缺陷R111：已屏蔽簇隐藏口径与主列表一致。"""
        rows = []
        for idx, cluster in enumerate(self._displayed):
            if not self._fs_cluster_matches(cluster):
                continue
            if self._is_muted(cluster) and not self._show_muted:
                continue
            rows.append(("c", idx))
            if idx in self._expanded_clusters:
                for iidx in range(len(cluster.instances)):
                    rows.append(("i", idx, iidx))
        return rows

    # ------------------------------------------------------------------
    # 修复缺陷R16：主列表簇就地展开（展示全部 N 个错误位置）
    # ------------------------------------------------------------------
    @staticmethod
    def _inst_head_text(inst) -> str:
        """实例行头部文本（时间戳 + 行号区间）。"""
        return (f"{format_timestamp(inst.timestamp)}   "
                f"行 {inst.line_no}~{inst.last_line_no}")

    def _build_view_rows(self) -> list:
        """生成主列表视图行：簇行 + 展开簇的全部实例行。

        视图行 = ("c", 簇索引) | ("i", 簇索引, 实例索引)；虚拟列表
        以此为数据（池化渲染两种行）。
        优化缺陷R45：搜索关键字过滤（与全屏 _fs_view_rows 同口径，
        行索引保持 _displayed 语义，展开/选中状态不受影响）。
        优化缺陷R111：已屏蔽簇默认隐藏（🚫 指示器切换显示）。
        """
        rows = []
        for idx, cluster in enumerate(self._displayed):
            if not self._cluster_matches(cluster):
                continue
            if self._is_muted(cluster) and not self._show_muted:
                continue
            rows.append(("c", idx))
            if idx in self._expanded_clusters:
                for iidx in range(len(cluster.instances)):
                    rows.append(("i", idx, iidx))
        return rows

    def _toggle_cluster_expand(self, idx: int) -> None:
        """展开/收起簇实例列表（「▶ ×N」按钮，主列表就地）。

        展开后该簇全部 N 个实例以独立行呈现（时间戳+行号+摘要），
        点击实例行右侧详情显示【该实例自身】的上下文与堆栈
        （_fill_instance_detail）—— 不再局限于典型样例（第一次
        出现的错误位置）。
        优化缺陷R97：经典渲染链删除后只剩虚拟路径（update_rows
        保持滚动位置，浏览不跳动）。
        """
        if not (0 <= idx < len(self._displayed)):
            return
        if self._virtual_list is not None:
            if idx in self._expanded_clusters:
                self._expanded_clusters.pop(idx, None)
            else:
                self._expanded_clusters[idx] = True
            # 保持滚动位置更新（Tk canvas 保持内容偏移，浏览位置不动）
            self._virtual_list.update_rows(self._build_view_rows())
        # 优化缺陷R42：全屏虚拟列表同步刷新（共享 _expanded_clusters）
        fs_vl = getattr(self, "_fs_vl", None)
        if fs_vl is not None:
            fs_vl.update_rows(self._fs_view_rows())

    def _select_instance(self, cidx: int, iidx: int) -> None:
        """点击实例行：右侧详情显示该实例自身的上下文/堆栈。

        所属簇保持选中高亮；_selected_inst 记录实例选中态（虚拟
        列表 _fill_slot 据此对实例行着选中色）。
        """
        if not (0 <= cidx < len(self._displayed)):
            return
        cluster = self._displayed[cidx]
        if not (0 <= iidx < len(cluster.instances)):
            return
        self._selected_inst = (cidx, iidx)
        self._mark_selected_row(cidx)
        self._detail_cluster = cluster      # 优化缺陷R111：屏蔽按钮目标
        self._update_mute_btn()
        self._fill_instance_detail(self._detail_box, cluster,
                                   cluster.instances[iidx])
        self._sync_fs_detail()          # 优化缺陷R42：全屏联动
        # 优化缺陷R56：点实例按实例自身同步导航序号（扁平实例导航）
        self._sync_search_nav(cidx, iidx)

    def _show_cluster_detail(self, cluster: ErrorCluster) -> None:
        """显示错误簇的详情（典型样例+上下文+堆栈）。"""
        self._detail_cluster = cluster      # 优化缺陷R111：屏蔽按钮目标
        self._update_mute_btn()
        self._fill_cluster_detail(self._detail_box, cluster)

    @staticmethod
    def _detail_writer(box: ctk.CTkTextbox):
        """详情文本写入三件套（段标题 / 元信息 / 普通行）。"""
        def header(text: str) -> None:
            box.insert("end", text + "\n", "header")

        def meta(text: str) -> None:
            box.insert("end", text + "\n", "meta")

        def plain(text: str = "") -> None:
            box.insert("end", text + "\n")
        return header, meta, plain

    def _fill_cluster_detail(self, box: ctk.CTkTextbox,
                             cluster: ErrorCluster) -> None:
        """簇详情渲染（主面板与全屏详情面板共用，修复缺陷R4）。"""
        header, meta, plain = self._detail_writer(box)
        box.configure(state="normal")
        box.delete("1.0", "end")

        header(f"【错误摘要】{cluster.summary}")
        meta(f"出现 {cluster.count} 次 | 级别 {cluster.level} | "
             f"模块 {cluster.module or '-'} | "
             f"行 {cluster.first_line}~{cluster.last_line}")
        # 优化缺陷R77：优先级附评分构成（判断依据，这分怎么算的）
        prio = f"优先级 {cluster.priority_label}（{cluster.priority:.0f}）"
        if cluster.priority_detail:
            prio += f" = {cluster.priority_detail}"
        meta(f"时间范围 {format_timestamp(cluster.first_seen)} ~ "
             f"{format_timestamp(cluster.last_seen)} | {prio}")
        notes = []
        if cluster.is_root_cause:
            notes.append(f"根因：{cluster.root_cause_reason}")
        elif cluster.root_cause_reason:
            notes.append(cluster.root_cause_reason)
        if cluster.anomaly:
            notes.append(f"异常：{_ANOMALY_LABELS.get(cluster.anomaly, cluster.anomaly)}")
        if notes:
            meta("【智能分析】" + "；".join(notes))
        elif self._analyze_key == "fast":
            # 优化缺陷R79：快速聚类模式明示「未执行」（区别于无标记）
            meta("【智能分析】快速聚类模式（未执行智能分析）")
        else:
            meta("【智能分析】无特殊标记")
        # 优化缺陷R78：根因传播链（因果图叙事）与相关簇（模板相似）
        if cluster.root_timeline:
            meta(f"【传播链】{cluster.root_timeline}")
        if cluster.related_clusters:
            _by_id = {c.cluster_id: c
                      for c in (self._result.clusters
                                if self._result else [])}
            rels = [_by_id[i].summary[:20]
                    for i in cluster.related_clusters if i in _by_id]
            if rels:
                meta("【相关簇】" + "、".join(rels[:3])
                     + ("…" if len(rels) > 3 else "") + "（模板相似 ≥80%）")
        # 优化缺陷R102：错误共现（±60s/±100行 同窗反复同现 ≥2 次的
        # 簇 —— "一起炸"直指同一根因，与"长得像"的相关簇互补）
        if self._result is not None and cluster.instances:
            coocs = cooccurring_clusters(cluster, self._result.clusters)
            if coocs:
                parts = [f"{c.summary[:20]}（同窗 {n} 次）"
                         for c, n in coocs[:3]]
                meta("【共现】常一起出现：" + "、".join(parts)
                     + ("…" if len(coocs) > 3 else ""))
        # 优化缺陷R101：变量分布（模板槽位实际取值 Top3，如
        # 「数值: 93495 ×3」—— 一眼看清同一错误的哪部分在变）
        var_msgs = []
        if cluster.sample is not None and cluster.sample.entry is not None:
            var_msgs.append(cluster.sample.entry.full_message)
        for inst in cluster.instances:
            if inst.entry is not None:
                var_msgs.append(inst.entry.full_message)
            elif inst.summary:
                var_msgs.append(inst.summary)
        var_n, var_slots = extract_variable_distribution(
            cluster.message_template, var_msgs)
        if var_slots:
            meta(f"【变量分布】按 {var_n} 条实例消息提取：")
            for slot_name, tops in var_slots:
                vals = "、".join(f"{v} ×{c}" for v, c in tops)
                meta(f"　{slot_name}: {vals}")

        sample = cluster.sample
        if sample is None:
            plain()
            plain("（无典型样例）")
            box.configure(state="disabled")
            return

        entry = sample.entry
        if sample.before:
            plain()
            header("──── 前上下文 ────")
            for line in sample.before:
                plain(line)
        plain()
        header("──── 典型样例 ────")
        plain(entry.raw)
        for extra in entry.message_extra:
            plain(extra)

        if entry.stack:
            simplified = simplify_stack(entry.stack)
            plain()
            header(f"──── 堆栈（业务帧高亮，折叠噪声帧 {simplified.noise_count} 行）────")
            for line in simplified.lines:
                if "已折叠" in line:
                    # 修复缺陷R5：折叠提示独立配色（清晰可辨）
                    box.insert("end", line + "\n", "fold")
                else:
                    box.insert("end", line + "\n", "bstack")

        # 修复缺陷R67：后上下文区块固定显示 —— 空则明确标注原因
        # （错误位于文件末尾时 after 为空，整块隐藏会被误认为功能缺失）
        plain()
        header("──── 后上下文 ────")
        if sample.after:
            for line in sample.after:
                plain(line)
        else:
            meta("（已到文件末尾，无后续日志行）")
        box.configure(state="disabled")
        self._highlight_keywords(box)

    def _fill_instance_detail(self, box: ctk.CTkTextbox,
                              cluster: ErrorCluster,
                              instance: "ClusterInstance") -> None:
        """单个错误实例详情（修复缺陷R4：全屏展开实例点击查看）。

        展示该实例自身的前后上下文、原始日志与堆栈（区别于簇的
        典型样例）；超岀详情保留上限的实例仅有元数据（提示降级）。
        """
        header, meta, plain = self._detail_writer(box)
        box.configure(state="normal")
        box.delete("1.0", "end")
        header(f"【实例详情】{instance.summary}")
        meta(f"时间 {format_timestamp(instance.timestamp)} | "
             f"行 {instance.line_no}~{instance.last_line_no} | "
             f"所属簇 ×{cluster.count}（{cluster.level}）")
        if instance.entry is None:
            plain()
            meta("该实例超出详情保留上限（仅记录时间与摘要）。"
                 "完整堆栈与上下文请查看该簇的「典型样例」。")
            box.configure(state="disabled")
            return
        entry = instance.entry
        if instance.before:
            plain()
            header("──── 前上下文 ────")
            for line in instance.before:
                plain(line)
        plain()
        header("──── 原始日志 ────")
        plain(entry.raw)
        for extra in entry.message_extra:
            plain(extra)
        if entry.stack:
            simplified = simplify_stack(entry.stack)
            plain()
            header(f"──── 堆栈（业务帧高亮，折叠噪声帧 {simplified.noise_count} 行）────")
            for line in simplified.lines:
                if "已折叠" in line:
                    # 修复缺陷R5：折叠提示独立配色（清晰可辨）
                    box.insert("end", line + "\n", "fold")
                else:
                    box.insert("end", line + "\n", "bstack")
        # 修复缺陷R44：实例后上下文渲染（此前实例无 after 数据，
        # 点击实例后详情面板缺失后上下文）
        # 修复缺陷R67：后上下文区块固定显示，空则标注已到文件末尾
        plain()
        header("──── 后上下文 ────")
        if instance.after:
            for line in instance.after:
                plain(line)
        else:
            meta("（已到文件末尾，无后续日志行）")
        box.configure(state="disabled")
        self._highlight_keywords(box)

    def _highlight_keywords(self, box: ctk.CTkTextbox) -> None:
        """关键字自动高亮（常见错误特征词）。

        优化缺陷R43：包含关键字输入框已删除，高亮词表固定为
        内置错误特征词（_KW_DEFAULT）。
        """
        text = box.get("1.0", "end")
        keywords = set(_KW_DEFAULT)
        for kw in keywords:
            if not kw:
                continue
            start = 0
            lowered = text.lower()
            kw_low = kw.lower()
            while True:
                pos = lowered.find(kw_low, start)
                if pos < 0:
                    break
                before = pos > 0 and text[pos - 1].isalnum()
                after = (pos + len(kw) < len(text)
                         and text[pos + len(kw)].isalnum())
                if not before and not after:
                    line, col = self._index_of_offset(box, pos)
                    if line:
                        box.tag_add("kw", f"{line}.{col}",
                                    f"{line}.{col + len(kw)}")
                start = pos + len(kw)
        # 优化缺陷R46：结果搜索关键字照亮 —— 子串匹配（不做词边界
        # 检查，用户输入什么就找什么），醒目黄底「searchkw」标签；
        # 主/全屏详情面板共用本函数，两路同步生效
        kw = self._search_kw
        # 优化缺陷R53：全屏列表窗口可见且有其独立搜索关键字时，详情
        # 优先照亮全屏关键字（全屏搜索体验与主窗口完全一致）
        fs_win = getattr(self, "_fs_list_win", None)
        fs_kw = getattr(self, "_fs_search_kw", "")
        try:
            fs_visible = (fs_win is not None and fs_win.winfo_exists()
                          and fs_win.winfo_viewable())
        except tk.TclError:
            fs_visible = False
        if fs_kw and fs_visible:
            kw = fs_kw
        if kw:
            start = 0
            while True:
                pos = lowered.find(kw, start)
                if pos < 0:
                    break
                line, col = self._index_of_offset(box, pos)
                if line:
                    box.tag_add("searchkw", f"{line}.{col}",
                                f"{line}.{col + len(kw)}")
                start = pos + len(kw)

    @staticmethod
    def _index_of_offset(box: ctk.CTkTextbox, offset: int):
        """字符偏移 -> (行, 列)（逐行累计，避免行尾符计数差异）。"""
        line = 1
        remaining = offset
        while line < 100000:
            line_len = len(box.get(f"{line}.0", f"{line}.end")) + 1
            if remaining < line_len:
                return line, remaining
            remaining -= line_len
            line += 1
        return None, 0

    # ------------------------------------------------------------------
    # 对比结果渲染
    # ------------------------------------------------------------------
    def _on_compare_result(self, results: List[CompareResult]) -> None:
        # 先赋值再切换按钮状态：状态机依据结果判断可用性
        self._compare_results = results
        self._result = None
        self._displayed = []
        self._set_running(False)
        self._progress_bar.stop()
        self._progress_bar.set(1.0)
        self._progress_label.configure(text="对比完成")

        box = self._detail_box
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("end", "【多文件对比结果】\n", "header")
        # 修复缺陷#10：对比符号图例说明（顶部一次性展示）
        box.insert("end", "图例：", "header")
        box.insert("end", "+ 新增错误（对比文件中新出现的）\n")
        box.insert("end", "        - 消失错误（基准文件中有但对比文件中没有的）\n")
        box.insert("end", "        = 共同错误（两个文件中都存在的）\n", "meta")
        for cmp in results:
            box.insert("end", f"\n基准 {cmp.base_name} vs {cmp.other_name}\n",
                       "header")
            box.insert(
                "end",
                f"新增 {len(cmp.new_items)} 种（{sum(i.count_b for i in cmp.new_items)} 次） | "
                f"消失 {len(cmp.gone_items)} 种 | "
                f"共同 {len(cmp.common_items)} 种\n", "meta")
            for i in cmp.new_items[:10]:
                box.insert("end", f"  + [{i.level} ×{i.count_b}] {i.summary}\n")
            for i in cmp.gone_items[:10]:
                box.insert("end", f"  - [{i.level} ×{i.count_a}] {i.summary}\n")
            for i in cmp.common_items[:10]:
                box.insert("end",
                           f"  = [{i.level} {i.count_a}->{i.count_b} "
                           f"{i.change_text}] {i.summary}\n")
        box.insert("end", "\n提示：点击「导出报告」保存完整对比差异报告；"
                           "「统计图表」查看两文件错误对比图\n", "meta")
        box.configure(state="disabled")
        # 修复缺陷#10：左侧渲染对比差异列表（支持全屏查看）
        self._render_compare_list()
        self._status_label.configure(
            text=f"对比完成：{' vs '.join([cmp.other_name for cmp in results])}")
        self._save_config()

    # 对比行符号与配色
    _CMP_SYMBOL = {"new": "+", "gone": "-", "common": "="}
    _CMP_COLOR = {"new": "#66bb6a", "gone": "#ef5350", "common": "#4dd0e1"}

    def _iter_compare_rows(self):
        """按「新增 → 消失 → 共同」顺序产出 (kind, 对比对, 差异项)。"""
        for cmp in self._compare_results:
            for i in cmp.new_items:
                yield "new", cmp, i
            for i in cmp.gone_items:
                yield "gone", cmp, i
            for i in cmp.common_items:
                yield "common", cmp, i

    def _make_compare_row(self, parent, kind: str, cmp: CompareResult,
                          item) -> dict:
        """构建单条对比差异行：符号 + 级别 + 次数变化 + 摘要（自动换行）。

        修复缺陷R2：字体/行距随主列表放大（头部加粗 / 摘要）。
        """
        p = self._palette()
        # 修复缺陷R2：行距/内边距与主列表一致（大字体下行高充足）
        frame = ctk.CTkFrame(parent, corner_radius=6,
                             fg_color=p["row_bg"])
        frame.pack(fill="x", padx=5, pady=3)
        if kind == "new":
            count_text = f"×{item.count_b} 次"
        elif kind == "gone":
            count_text = f"×{item.count_a} 次"
        else:
            count_text = f"{item.count_a} -> {item.count_b}（{item.change_text}）"
        head = ctk.CTkLabel(
            frame,
            text=(f"{self._CMP_SYMBOL[kind]} [{item.level}] {count_text}"
                  f"   {cmp.base_name} vs {cmp.other_name}"),
            anchor="w", text_color=self._CMP_COLOR[kind],
            font=self._font_row_head)
        # 修复缺陷R9：行距与主列表一致 + 摘要单行不换行（水平滚动查看）
        head.pack(fill="x", padx=(10, 10), pady=(7, 2))
        # 修复缺陷R10：对比行摘要补 DPI 缩放（与主列表渲染一致）
        summary = tk.Label(
            frame, text=item.summary, anchor="w", justify="left",
            wraplength=0,
            font=self._scaled_font(self._font_row_summary),
            bg=p["row_bg"], fg=p["row_text"])
        summary.pack(fill="x", padx=(10, 4), pady=(2, 6))
        return {"frame": frame, "summary": summary, "kind": kind,
                "item": item, "text": f"{item.summary} {item.level} "
                                      f"{self._CMP_SYMBOL[kind]}"}

    def _render_compare_list(self) -> None:
        """左侧对比差异列表（新增/消失/共同 全量渲染，经典模式）。"""
        self._clear_list()
        self._selected_row = -1
        if not self._compare_results:
            ctk.CTkLabel(self._cluster_list,
                         text="对比模式：差异摘要见右侧详情",
                         text_color="#8fa4b8").pack(pady=20)
            return
        rows = list(self._iter_compare_rows())
        if not rows:
            ctk.CTkLabel(self._cluster_list,
                         text="两文件错误完全一致（无差异项）",
                         text_color="#8fa4b8").pack(pady=20)
            return
        for kind, cmp, item in rows[:200]:   # 上限保护（超长列表性能）
            # 登记进 _cluster_rows：主题切换时统一刷新明暗配色（修复缺陷#12）
            self._cluster_rows.append(
                self._make_compare_row(self._cluster_list, kind, cmp, item))

    # ==================================================================
    # 导出 / 复制 / 图表
    # ==================================================================
    def _on_export(self) -> None:
        if self._result is None and not self._compare_results:
            return
        # 对比模式：单格式差异报告（保持旧流程）
        if self._result is None:
            path = filedialog.asksaveasfilename(
                title="导出报告",
                defaultextension=".md",
                filetypes=[("Markdown 报告", "*.md"), ("JSON 数据", "*.json"),
                           ("纯文本", "*.txt")])
            if not path:
                return
            try:
                content = compare_to_markdown(self._compare_results)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(content)
                self._status_label.configure(text=f"报告已导出：{path}")
            except OSError as exc:
                messagebox.showerror("导出失败", str(exc))
            return
        # 分析模式：优化缺陷R58 —— 导出选项对话框（格式多选 + 内容
        # 板块 + 范围跟随当前级别过滤）
        self._open_export_dialog()

    # 优化缺陷R58：导出格式（对话框勾选 → _export_with_options 写出）
    _EXPORT_FORMATS = (
        ("html", "HTML 报告 (.html)"),
        ("md", "Markdown (.md)"),
        ("json", "JSON 数据 (.json)"),
        ("txt", "纯文本 (.txt)"),
    )
    _EXPORT_SECTIONS = (
        ("overview", "概览统计"),
        ("list", "错误清单"),
        ("detail", "典型样例详情"),
        ("instances", "实例行号索引"),
    )

    def _open_export_dialog(self) -> None:
        """导出选项对话框（优化缺陷R58）。

        格式多选（一次同出多份，同名不同扩展名）；范围固定跟随当前
        级别过滤勾选（用户决策）；内容板块勾选作用于 md/html/txt。
        """
        old = getattr(self, "_export_dlg", None)
        if old is not None and old.winfo_exists():
            old.focus_set()
            return
        dlg = ctk.CTkToplevel(self)
        self._export_dlg = dlg
        dlg.title("导出报告")
        dlg.transient(self)
        dlg.resizable(False, False)
        # 居中于主窗口（物理像素坐标，winfo 已是 DPI 感知值）
        self.update_idletasks()
        cx = self.winfo_x() + max(0, (self.winfo_width() - 460) // 2)
        cy = self.winfo_y() + max(0, (self.winfo_height() - 348) // 2)
        dlg.geometry(f"460x348+{cx}+{cy}")

        ctk.CTkLabel(dlg, text="导出格式（可多选，同名多份同出）",
                     font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=16, pady=(14, 2))
        self._export_fmt_vars: Dict[str, tk.BooleanVar] = {}
        fmt_box = ctk.CTkFrame(dlg, fg_color="transparent")
        fmt_box.pack(anchor="w", padx=16, fill="x")
        for i, (key, label) in enumerate(self._EXPORT_FORMATS):
            # 默认勾选 HTML（用户决策新增格式）
            var = tk.BooleanVar(value=key == "html")
            self._export_fmt_vars[key] = var
            ctk.CTkCheckBox(fmt_box, text=label, variable=var,
                            checkbox_width=18, checkbox_height=18).grid(
                row=i // 2, column=i % 2, padx=(0, 18), pady=3, sticky="w")

        # 范围：跟随当前级别过滤（导出时按勾选实时过滤）
        active = [lv for lv, var in self._level_vars.items() if var.get()]
        scope = "、".join(active) if active else "（未勾选级别）"
        ctk.CTkLabel(dlg, text=f"范围：跟随当前级别过滤（{scope}）",
                     text_color="#8fa4b8").pack(anchor="w", padx=16,
                                                pady=(8, 2))
        # 优化缺陷R99：token 预估行（导出前即可见压缩收益）
        ctk.CTkLabel(
            dlg, text=f"预估：{getattr(self, '_token_report', '-')}",
            text_color="#3B82F6").pack(anchor="w", padx=16, pady=(0, 2))

        ctk.CTkLabel(dlg, text="内容板块（作用于 HTML / Markdown / 纯文本）",
                     font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=16, pady=(8, 2))
        self._export_sec_vars: Dict[str, tk.BooleanVar] = {}
        sec_box = ctk.CTkFrame(dlg, fg_color="transparent")
        sec_box.pack(anchor="w", padx=16, fill="x")
        for i, (key, label) in enumerate(self._EXPORT_SECTIONS):
            var = tk.BooleanVar(value=True)
            self._export_sec_vars[key] = var
            ctk.CTkCheckBox(sec_box, text=label, variable=var,
                            checkbox_width=18, checkbox_height=18).grid(
                row=i // 2, column=i % 2, padx=(0, 18), pady=3, sticky="w")
        # 修复缺陷R61：JSON 默认精简（核心字段+实例行号，人可通读）；
        # 完整上下文/堆栈为显式勾选（体积 >10 倍，供脚本二次处理）
        self._export_json_full_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            dlg, text="JSON 含完整上下文与堆栈（体积大，供脚本处理）",
            variable=self._export_json_full_var,
            checkbox_width=18, checkbox_height=18).pack(
            anchor="w", padx=16, pady=(6, 0))

        btn_box = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_box.pack(fill="x", padx=16, pady=(14, 14))
        ctk.CTkButton(btn_box, text="取消", width=90, fg_color="#6b7280",
                      command=dlg.destroy).pack(side="right", padx=(6, 0))
        ok = ctk.CTkButton(btn_box, text="导出…", width=110,
                           command=self._on_export_confirmed)
        ok.pack(side="right")
        self._accent_buttons.append((ok, "accent"))
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.after(60, dlg.focus_set)

    def _on_export_confirmed(self) -> None:
        """对话框确认：收集选项 → 选保存位置 → 多格式同出。"""
        formats = [k for k, _ in self._EXPORT_FORMATS
                   if self._export_fmt_vars[k].get()]
        if not formats:
            messagebox.showwarning("导出报告", "请至少勾选一种导出格式")
            return
        sections = {k for k, _ in self._EXPORT_SECTIONS
                    if self._export_sec_vars[k].get()}
        json_full = self._export_json_full_var.get()
        dlg = getattr(self, "_export_dlg", None)
        if dlg is not None and dlg.winfo_exists():
            dlg.destroy()
        first = formats[0]
        path = filedialog.asksaveasfilename(
            title="导出报告（同名多份按格式加扩展名）",
            initialfile="日志分析报告",
            defaultextension=f".{first}",
            filetypes=[("报告文件", f"*.{first}"), ("全部文件", "*.*")])
        if not path:
            return
        try:
            written = self._export_with_options(path, formats, sections,
                                                json_full=json_full)
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc))
            return
        folder = os.path.dirname(written[0])
        self._status_label.configure(
            text=f"已导出 {len(written)} 个报告文件至 {folder}"
                 + self._redact_invalid_note())
        if messagebox.askyesno(
                "导出成功",
                f"已导出 {len(written)} 个文件：\n"
                + "\n".join(os.path.basename(p) for p in written)
                + "\n\n是否打开所在文件夹？"):
            try:
                os.startfile(folder)        # Windows 资源管理器定位
            except (AttributeError, OSError):
                pass

    def _export_with_options(self, path: str, formats, sections,
                             json_full: bool = False) -> list:
        """多格式导出执行器（优化缺陷R58；无对话框，可测试）。

        范围跟随当前级别过滤勾选：按导出时刻的级别复选框实时过滤
        簇（分析后改勾选不重新分析也能导出所见范围）；同一基础名
        按格式各写一份文件，返回写出的路径列表。
        修复缺陷R61：json_full=False 时 JSON 精简输出（核心字段 +
        实例行号），True 时含完整上下文/堆栈（供脚本二次处理）。
        """
        base, _ext = os.path.splitext(path)
        active = {lv for lv, var in self._level_vars.items() if var.get()}
        clusters = [c for c in self._result.clusters if c.level in active]
        view = dataclasses.replace(self._result, clusters=clusters)
        full = len(view.clusters)
        writers = {
            "html": (".html", lambda: to_html(view, top_n=full,
                                              sections=sections)),
            "md": (".md", lambda: to_markdown(view, top_n=full,
                                              sections=sections)),
            "json": (".json", lambda: to_json(view, top_n=full,
                                              full=json_full)),
            "txt": (".txt", lambda: to_text(view, top_n=full,
                                            sections=sections)),
        }
        written = []
        # 优化缺陷R86：出站脱敏（⚙ 弹层开关，默认开）—— 分析结果
        # 本地保持原样，仅写盘文本过 redact_text
        redact = bool(getattr(self, "_redact_var", None)
                      and self._redact_var.get())
        for fmt in ("html", "md", "json", "txt"):
            if fmt not in formats:
                continue
            ext, build = writers[fmt]
            out = base + ext
            content = build()
            if redact:
                content = self._redact_out(content)
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(content)
            written.append(out)
        return written

    # ------------------------------------------------------------------
    # 优化缺陷R103：自定义脱敏规则（⚙ 弹层多行正则，每行一条）
    # ------------------------------------------------------------------
    def _custom_redact_rules(self):
        """读取自定义规则：返回 (有效正则列表, 无效行数)。

        非法行不静默丢弃 —— 计数后由出站动作在状态栏提示用户。
        """
        box = getattr(self, "_redact_custom_box", None)
        if box is None:
            return [], 0
        valid, invalid = [], 0
        for line in box.get("1.0", "end").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                re.compile(line)
                valid.append(line)
            except re.error:
                invalid += 1
        return valid, invalid

    def _redact_out(self, text: str) -> str:
        """出站脱敏统一入口：内置规则 + 自定义正则（无效行数记入
        _last_redact_invalid，供状态栏提示）。"""
        rules, self._last_redact_invalid = self._custom_redact_rules()
        return redact_text(text, custom_rules=rules)

    def _redact_invalid_note(self) -> str:
        """自定义正则无效行的状态栏附注（无无效行/开关关时为空）。"""
        bad = getattr(self, "_last_redact_invalid", 0)
        if bad and getattr(self, "_redact_var", None) \
                and self._redact_var.get():
            return f"（{bad} 行自定义正则无效已忽略）"
        return ""

    def _on_copy_summary(self) -> None:
        if self._result is None:
            return
        summary = brief_summary(self._result,
                                top_n=len(self._result.clusters))
        # 优化缺陷R86：出站脱敏（同导出，⚙ 弹层开关默认开）
        if getattr(self, "_redact_var", None) and self._redact_var.get():
            summary = self._redact_out(summary)
        self.clipboard_clear()
        self.clipboard_append(summary)
        self._status_label.configure(
            text="摘要已复制到剪贴板，可直接粘贴投喂 AI 助手"
                 + self._redact_invalid_note())

    def _show_charts(self) -> None:
        if self._result is None and not self._compare_results:
            return
        if self._chart_window is not None and self._chart_window.winfo_exists():
            self._chart_window.destroy()
        # 懒加载：首次点击图表时才导入 matplotlib（修复缺陷#9）
        from log_ai_compressor.gui_legacy.charts import (
            ChartsPanel,
            CompareChartsPanel,
        )
        self._chart_window = ctk.CTkToplevel(self)
        # 优化缺陷R63：高 DPI 适配（2x 屏图表文字不再缩半）+ 窗口放大
        dpi_scale = max(1.0, getattr(self, "_font_scale", 1.0))
        if self._compare_results:
            # 修复缺陷#10：对比模式展示两文件错误对比图表
            self._chart_window.title("错误对比图表")
            self._chart_window.geometry("1250x500")
            CompareChartsPanel(self._chart_window, self._compare_results,
                               dpi_scale=dpi_scale)
        else:
            self._chart_window.title("错误统计图表")
            # 优化缺陷R64：分页单图切换（每图独占窗口，适当加高）
            self._chart_window.geometry("1200x560")
            # 优化缺陷R63：面板实例留存（测试可检视图表内容）
            self._chart_panel = ChartsPanel(
                self._chart_window, self._result,
                on_select_level=self._select_by_level,
                on_select_cluster=self._select_by_cluster_id,
                dpi_scale=dpi_scale)

    def _select_by_level(self, level: str) -> None:
        for i, c in enumerate(self._displayed):
            if c.level == level:
                self._focus_cluster_from_chart(i)
                return

    def _select_by_cluster_id(self, cluster_id: str) -> None:
        """图表点击联动：按 cluster_id 定位簇（优化缺陷R63：错误
        种类 Top 10 条形点击 → 主列表选中该簇）。"""
        try:
            cid = int(cluster_id)
        except (TypeError, ValueError):
            return
        for i, c in enumerate(self._displayed):
            if c.cluster_id == cid:
                self._focus_cluster_from_chart(i)
                return

    def _focus_cluster_from_chart(self, idx: int) -> None:
        """修复缺陷R65：图表点击定位三处失灵补救 ——
        1) 搜索关键字把目标簇滤出视图时自动清空搜索框（视图过滤仅
           作用于显示，清空即恢复可见）；
        2) 选中后滚入视口（原只染蓝不滚动，视口外看不见高亮）；
        3) 主窗口 deiconify+lift 前置（图表 Toplevel 盖住主窗口时，
           选中/详情更新全部发生在被遮挡窗口里，用户误以为没跳转）。
        """
        if not (0 <= idx < len(self._displayed)):
            return
        if self._search_kw and not self._cluster_matches(
                self._displayed[idx]):
            self._search_var.set("")
            self._apply_search_filter()     # 立即生效（取消防抖）
        self._select_cluster(idx)
        self._see_cluster_row(idx)
        try:
            self.deiconify()
            self.lift()
            self.focus_force()
        except tk.TclError:
            pass

    # ==================================================================
    # 全屏查看（修复缺陷#7：列表 / 详情独立最大化窗口）
    # ==================================================================
    def _make_fullscreen_window(self, title: str) -> ctk.CTkToplevel:
        """创建全屏窗口（屏幕正中央、尺寸 ≥80%、ESC 返回主界面）。

        修复缺陷R2：原实现仅 state("zoomed")，在 zoomed 未生效的
        环境下窗口落在系统默认偏移位置（左上角附近）。现先显式
        计算居中几何（屏幕 85% × 88%，正中央），再尝试系统最大
        化——最大化失败时仍保持居中大窗。
        """
        win = ctk.CTkToplevel(self)
        win.title(f"{title}（全屏 · ESC 返回）")
        try:
            sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
            w, h = int(sw * 0.85), int(sh * 0.88)
            win.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
        except tk.TclError:
            pass
        try:
            win.state("zoomed")            # Windows / macOS（100% 全屏）
        except tk.TclError:
            try:
                win.attributes("-fullscreen", True)   # Linux 回退
            except tk.TclError:
                pass
        win.bind("<Escape>", lambda e: win.destroy())
        win.after(60, win.focus_set)       # 抢焦点以接收 ESC
        return win

    def _open_list_fullscreen(self) -> None:
        """错误分类列表全屏：左右分栏 + 簇展开 + 实例详情联动。

        修复缺陷#10：对比模式下展示差异列表（+ 新增 / - 消失 / = 共同）。
        修复缺陷R4（核心功能）：
        - 左侧错误簇列表，每簇「▶ ×N」可点击展开全部 N 个实例
          （时间戳 + 摘要；展开 ▼ / 收起 ▶，批量渐进动画）；
        - 展开的实例可点击，右侧详情面板显示该实例的前上下文、
          原始日志与堆栈；
        - 点击簇行，右侧显示簇详情（典型样例/上下文/降噪堆栈），
          同时联动主界面选中；
        - 实例行用原生 tk.Label 复用（不为每实例建独立 CTkFrame），
          展开按 25 条/帧批量创建（大簇不卡顿）。
        """
        compare_mode = bool(self._compare_results) and not self._displayed
        if not self._displayed and not compare_mode:
            messagebox.showinfo("提示", "请先完成一次分析再使用全屏")
            return
        if compare_mode:
            # 对比模式结构不同（无簇/实例概念）：独立窗口，每次新建
            self._open_compare_fullscreen()
            return
        # 修复缺陷R6：全屏窗口预创建复用 —— 首次创建后 withdraw 隐藏，
        # 二次打开仅 deiconify（数据签名变化时才重渲染列表内容）
        win = self._fs_list_win
        if win is None or not win.winfo_exists():
            win = self._build_fs_list_window()
        else:
            win.deiconify()
            try:
                win.state("zoomed")        # 恢复最大化（个别环境丢失）
            except tk.TclError:
                pass
            # 数据未变则跳过重渲染（复用已有行控件）
            # 优化缺陷R43：签名去掉 top_n 维度（Top N 功能已删除）
            sig = (id(self._result), len(self._displayed))
            if sig != self._fs_list_sig and callable(self._fs_list_refresh):
                self._fs_list_refresh()
                self._fs_list_sig = sig
        win.after(60, win.focus_set)

    def _build_fs_list_window(self) -> ctk.CTkToplevel:
        """构建（并缓存）错误分类列表全屏窗口（修复缺陷R6）。"""
        win = self._make_fullscreen_window("错误分类列表")
        self._fs_list_win = win
        # 隐藏而非销毁：ESC / 关闭按钮返回主界面但保留窗口复用
        def hide() -> None:
            win.withdraw()
        win.bind("<Escape>", lambda e: hide())

        bar = ctk.CTkFrame(win, corner_radius=0)
        bar.pack(fill="x")
        # 修复缺陷R10：全屏搜索框字体放大到 18（标签同步，视觉平衡）
        ctk.CTkLabel(bar, text="搜索：",
                     font=ctk.CTkFont(size=18)).pack(
            side="left", padx=(12, 4))
        search_var = tk.StringVar()
        search = ctk.CTkEntry(bar, textvariable=search_var,
                             font=ctk.CTkFont(size=18),
                             placeholder_text="按摘要/模块/级别过滤，支持 and/or/not…")
        search.pack(side="left", fill="x", expand=True, padx=(0, 8), pady=8)
        self._fs_search_entry = search
        # 优化缺陷R53：计数影子框（与主窗口同款）—— 输入框右侧恒定
        # 占位，有关键字显形「x / y 条」、清空透明隐形（布局零扰动）；
        # Enter / Shift+Enter 循环定位匹配簇，点击行同步导航序号
        self._fs_count_box = ctk.CTkFrame(
            bar, width=120, height=32, corner_radius=6,
            fg_color="transparent")
        self._fs_count_box.pack(side="left", padx=(0, 8))
        self._fs_count_box.pack_propagate(False)
        self._fs_count = ctk.CTkLabel(
            self._fs_count_box, text="", text_color="#8fa4b8",
            font=ctk.CTkFont(size=15), fg_color="transparent")
        self._fs_count.place(relx=0.5, rely=0.5, anchor="c")
        search.bind("<Return>",
                    lambda e: self._on_fs_search_enter(True))
        search.bind("<Shift-Return>",
                    lambda e: self._on_fs_search_enter(False))
        # 修复缺陷R69：删除「显示 x/y 簇」过滤计数标签 —— 与导航
        # 计数（x/y 条）并排双计数器语义重叠，用户误读为新 bug
        ctk.CTkButton(bar, text="关闭 (ESC)", width=110,
                      command=hide).pack(side="right", padx=(0, 12))

        # ---------------- 常规模式：左右分栏 ----------------
        body = ctk.CTkFrame(win, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        # 修复缺陷R17：左右分栏改 place 比例布局 + 可拖分隔条（与主
        # 界面同一套交互：实时矢量代理拖动/双击复位 2:3/位置持久化
        # fs_splitter_ratio/最小宽度钳制/悬停高亮）
        fs_list_col = ctk.CTkFrame(body, fg_color="transparent")
        fs_detail_col = ctk.CTkFrame(body, fg_color="transparent")
        for col in (fs_list_col, fs_detail_col):
            col.grid_columnconfigure(0, weight=1)
        fs_list_col.grid_rowconfigure(0, weight=1)
        fs_detail_col.grid_rowconfigure(1, weight=1)
        self._fs_body = body
        self._fs_list_col = fs_list_col
        self._fs_detail_col = fs_detail_col

        list_host = ctk.CTkFrame(fs_list_col, fg_color="transparent")
        list_host.grid(row=0, column=0, sticky="nsew", padx=(6, 3))
        # 优化缺陷R42：全屏列表直接复用虚拟列表组件 —— 与主窗口
        # 左下角完全同一渲染（圆角药丸行/亮边框/选中 3D 蓝药丸+
        # 阴影/就地展开实例行/底部水平滚动条）；字体传全屏档 28/24
        self._fs_vl = VirtualClusterList(
            list_host, self,
            font_head=self._font_fs_head,
            font_summary=self._font_fs_summary)

        # 右侧详情面板（修复缺陷R4：点击簇/实例即时联动）
        detail_head = ctk.CTkFrame(fs_detail_col, fg_color="transparent")
        detail_head.grid(row=0, column=0, sticky="new", padx=(3, 6))
        self._fs_detail_head = detail_head
        # 修复缺陷R10：全屏详情面板字体放大（正文 18，标题随行放大）
        ctk.CTkLabel(detail_head, text="详情（簇典型样例 / 实例原始日志）",
                     font=ctk.CTkFont(size=15, weight="bold")).pack(
            side="left", pady=(4, 2))
        fs_detail = ctk.CTkTextbox(
            fs_detail_col, font=ctk.CTkFont(family="Consolas",
                                            size=self._font_px(18)),
            wrap="none")
        fs_detail.grid(row=1, column=0, sticky="nsew", padx=(3, 6))
        self._fs_list_detail = fs_detail
        # 分隔条与比例布局（构建即按持久化比例就位）
        self._build_fs_splitter(body)
        body.bind("<Configure>", lambda e: self._fs_layout_splitter())
        self._fs_layout_splitter()
        # 详情高亮标签（与主面板同一套配色，修复缺陷R5）
        # 修复缺陷R10：全屏详情大字号标签（摘要 20 / 元信息 16 / 栈帧 18）
        self._apply_detail_tags(fs_detail, big=True)

        def render(keyword: str = "") -> None:
            """优化缺陷R42：搜索过滤 + 全屏虚拟列表刷新（即时计数）。

            全屏列表与主窗口共用 VirtualClusterList（池化渲染，无
            分批创建开销，R6 渐进批次退役）；行选中/展开/实例点击
            全部经应用态共享 —— _select_cluster / _select_instance /
            _toggle_cluster_expand 内部联动全屏详情与选中态。
            """
            self._fs_search_kw = keyword.strip().lower()
            # 优化缺陷R53：关键字变化重置查找导航（计数回到 0/y 未
            # 定位态，与主窗口 _apply_search_filter 同语义）
            self._fs_search_nav = 0
            rows = self._fs_view_rows()
            # 修复缺陷R69：「显示 x/y 簇」标签已删除（见构建处注释）
            self._fs_vl.set_data(rows)
            # 优化缺陷R53：计数影子框 + 详情关键字高亮即时刷新
            self._update_fs_search_count()
            self._sync_fs_detail()
            # 优化缺陷R53：当前选中簇被过滤掉时自动选中首个匹配簇
            # （与主窗口同语义，详情面板不停留陈旧内容）
            matches = [i for i, c in enumerate(self._displayed)
                       if self._fs_cluster_matches(c)]
            if matches and self._selected_row not in matches:
                self._select_cluster(matches[0])

        # 文本变化即过滤（trace 不依赖键盘事件，无焦点也可靠触发）
        search_var.trace_add("write", lambda *a: render(search_var.get()))
        # 修复缺陷R6：登记刷新回调 + 数据签名（数据未变时跳过重渲染）
        # 优化缺陷R53：刷新读取输入框当前文本（而非空串重置过滤，
        # 避免重开窗口时框内文字与实际过滤口径不一致）
        self._fs_list_refresh = lambda: render(search_var.get())
        self._fs_list_sig = (id(self._result), len(self._displayed))
        # 首批异步渲染：回调即刻返回（<300ms 交互不卡，同 R6 语义），
        # 窗口 deiconify 后 1ms 填充数据行
        win.after(1, render)
        return win

    # ------------------------------------------------------------------
    # 修复缺陷R17：全屏列表窗口 列表 | 详情 可拖动分隔条
    # （与主界面同一套：place 比例布局 + 矢量文本代理实时拖动 +
    # 双击复位 + 位置持久化 + 最小宽度钳制 + 悬停高亮）
    # ------------------------------------------------------------------
    def _build_fs_splitter(self, body) -> None:
        """构建全屏分隔条（6px 圆角条 + 三握点 + ↔ 光标）。"""
        p = self._palette()
        self._fs_dragging = False
        self._fs_drag_ctx = None
        self._fs_live = None
        sp = ctk.CTkFrame(body, width=_SPLITTER_WIDTH, corner_radius=3,
                          fg_color=p["splitter"], cursor="sb_h_double_arrow")
        self._fs_splitter = sp
        dots = []
        for dy in (-8, 0, 8):
            dot = tk.Frame(sp, width=2, height=2, bd=0,
                           highlightthickness=0, bg=p["splitter_grip"],
                           cursor="sb_h_double_arrow")
            dot.place(relx=0.5, rely=0.5, y=dy, anchor="center")
            dots.append(dot)
        self._fs_splitter_dots = dots
        for target in [sp] + dots:
            target.bind("<ButtonPress-1>", self._on_fs_press)
            target.bind("<B1-Motion>", self._on_fs_drag)
            target.bind("<ButtonRelease-1>", self._on_fs_release)
            target.bind("<Double-Button-1>", self._on_fs_dblclick)
            target.bind("<Enter>", lambda e: self._fs_hover(True))
            target.bind("<Leave>", lambda e: self._fs_hover(False))
        # CTkFrame.bind 实际注册在内部 canvas；外层 tk.Frame 再绑一份
        # （event_generate 直发外层路径也能触发，同主界面）
        for seq, handler in (
                ("<ButtonPress-1>", self._on_fs_press),
                ("<B1-Motion>", self._on_fs_drag),
                ("<ButtonRelease-1>", self._on_fs_release),
                ("<Double-Button-1>", self._on_fs_dblclick)):
            tk.Frame.bind(sp, seq, handler)

    def _fs_hover(self, hovered: bool) -> None:
        """悬停/拖动高亮（选中蓝，提示可拖动）。"""
        if self._fs_dragging and not hovered:
            return
        try:
            p = self._palette()
            self._fs_splitter.configure(
                fg_color=p["row_selected"] if hovered else p["splitter"])
        except (tk.TclError, ValueError, AttributeError):
            pass

    def _fs_min_widths(self):
        """全屏左右列最小宽度（左列兜底 / 右列详情标题实测+余量）。"""
        scale = max(1.0, getattr(self, "_font_scale", 1.0))
        left_min = 300 * scale
        try:
            right_min = max(300 * scale,
                            self._fs_detail_head.winfo_reqwidth()
                            + 20 * scale)
        except (tk.TclError, AttributeError):
            right_min = 300 * scale
        return left_min, right_min

    def _fs_layout_splitter(self) -> None:
        """全屏左右列与分隔条按比例 place 布局（全部 rel 参数）。

        与主界面 _layout_splitter 同一模式：relx/relwidth 比例参数
        天然免疫 CTk 像素缩放（高 DPI 不错位）；最小宽度钳制；面板
        过窄时按比例铺满但不污染保存的比例。
        """
        body = getattr(self, "_fs_body", None)
        sp = getattr(self, "_fs_splitter", None)
        if body is None or sp is None:
            return
        try:
            pw = body.winfo_width()
        except tk.TclError:
            return
        if pw <= 2:
            return
        scale = max(1.0, getattr(self, "_font_scale", 1.0))
        sp_w = _SPLITTER_WIDTH * scale
        cached = getattr(self, "_fs_drag_mins", None)
        if cached is not None:
            left_min, right_min = cached
        else:
            left_min, right_min = self._fs_min_widths()
        r = self._fs_splitter_ratio
        if pw < left_min + right_min + sp_w:
            sp_r = sp_w / max(1, pw)
            self._fs_list_col.place(relx=0, rely=0, relwidth=r,
                                    relheight=1)
            sp.place(relx=r, rely=0, relheight=1)
            self._fs_detail_col.place(relx=min(1.0, r + sp_r), rely=0,
                                      relwidth=max(0.0, 1 - r - sp_r),
                                      relheight=1)
            return
        lo_r = left_min / pw
        hi_r = 1 - (right_min + sp_w) / pw
        r = min(max(r, lo_r), hi_r)
        self._fs_splitter_ratio = r
        sp_r = sp_w / pw
        self._fs_list_col.place(relx=0, rely=0, relwidth=r, relheight=1)
        sp.place(relx=r, rely=0, relheight=1)
        self._fs_detail_col.place(relx=r + sp_r, rely=0,
                                  relwidth=max(0.0, 1 - r - sp_r),
                                  relheight=1)

    def _on_fs_press(self, _event) -> None:
        """按下：缓存拖动几何 + 构建矢量文本代理（回退真实布局+冻结）。"""
        self._fs_dragging = True
        self._fs_hover(True)
        body = self._fs_body
        scale = max(1.0, getattr(self, "_font_scale", 1.0))
        sp_w = _SPLITTER_WIDTH * scale
        left_min, right_min = self._fs_min_widths()
        try:
            pw = max(1, body.winfo_width())
            rootx = body.winfo_rootx()
        except (tk.TclError, AttributeError):
            return
        lo, hi = left_min, pw - right_min - sp_w
        self._fs_drag_ctx = {"pw": pw, "rootx": rootx,
                             "sp_w": sp_w, "lo": lo, "hi": hi}
        self._fs_drag_mins = (left_min, right_min)
        if hi >= lo:
            if not self._fs_live_begin():
                # 回退：真实布局逐 motion（需冻结 CTk 重绘级联）
                self._set_ctk_drag_freeze(True)
        win = self._fs_list_win
        if win is not None:
            try:
                win.bind("<FocusOut>", self._on_fs_focusout)
            except tk.TclError:
                pass

    def _on_fs_focusout(self, _event) -> None:
        """拖动中窗口失焦：结束拖动并应用当前位置。"""
        if self._fs_dragging:
            self._on_fs_release(None)

    def _on_fs_drag(self, event) -> None:
        """拖动：rAF 节流应用（motion 只记位置，节拍统一 flush）。"""
        if not self._fs_dragging:
            return
        ctx = self._fs_drag_ctx
        if ctx is None or ctx["hi"] < ctx["lo"]:
            return
        try:
            x = event.x_root - ctx["rootx"] - ctx["sp_w"] // 2
        except AttributeError:
            return
        left = min(max(x, ctx["lo"]), ctx["hi"])
        self._fs_splitter_ratio = left / ctx["pw"]
        live = self._fs_live
        if live is not None:
            import time as _time
            live["pending"] = left
            now = _time.monotonic()
            if now - live["t0"] >= 0.012:      # 节流：≤83fps 直接应用
                live["t0"] = now
                self._fs_live_flush()
            elif live["after_id"] is None:     # 兜底帧：应用最新位置
                try:
                    live["after_id"] = self.after(12, self._fs_live_flush)
                except tk.TclError:
                    pass
            return
        try:
            self._fs_layout_splitter()      # 回退：真实布局逐 motion
        except tk.TclError:
            self._set_ctk_drag_freeze(False)

    def _on_fs_release(self, _event) -> None:
        """松开：真实布局一次到位 + 销毁代理 + 解除冻结 + 持久化。"""
        if not self._fs_dragging:
            return
        self._fs_dragging = False
        self._fs_hover(False)
        self._fs_drag_ctx = None
        self._fs_drag_mins = None
        win = self._fs_list_win
        if win is not None:
            try:
                win.unbind("<FocusOut>")
            except tk.TclError:
                pass
        self._fs_layout_splitter()
        try:
            self._fs_body.update_idletasks()
        except (tk.TclError, AttributeError):
            pass
        self._fs_live_end()
        self._set_ctk_drag_freeze(False)
        self._refresh_ctk_chrome()
        self._save_config()

    def _on_fs_dblclick(self, _event) -> None:
        """双击恢复默认比例（2:3）。"""
        self._fs_dragging = False
        self._fs_drag_ctx = None
        self._fs_drag_mins = None
        win = self._fs_list_win
        if win is not None:
            try:
                win.unbind("<FocusOut>")
            except tk.TclError:
                pass
        self._fs_splitter_ratio = _SPLITTER_DEFAULT_RATIO
        self._fs_layout_splitter()
        self._fs_live_end()
        self._set_ctk_drag_freeze(False)
        try:
            self._fs_body.update_idletasks()
        except (tk.TclError, AttributeError):
            pass
        self._refresh_ctk_chrome()
        self._save_config()

    def _fs_live_begin(self) -> bool:
        """全屏矢量文本代理（与主界面同一骨架，R17）。

        - 右画布：固定全幅 + 视口滚动（详情文本/竖线/滚动条近似在
          面板像素坐标系，随视口平移贴住分隔条）；
        - 左裁剪框：可变宽 Frame + 固定宽左画布，可见列表内容按
          【真实控件当前位置/字体/颜色/文本】采集绘制为文本 items
          —— 行结构无关（簇行 toggle/标题/摘要/实例区/选中底色）
          天然逐像素保真；裁剪框边界移动自然露出更多 = 内容真延展；
        - 右标题条：「详情」标题跟随（真实标题 pack_forget，代理
          近似；右端无按钮，覆盖到面板右缘）。
        返回 False = 代理不可用（布局未完成/无内容）→ 回退真实布局。
        """
        body = self._fs_body
        ctx = self._fs_drag_ctx or {}
        pw = ctx.get("pw") or max(1, body.winfo_width())
        sp_w = ctx.get("sp_w") or _SPLITTER_WIDTH
        lo, hi = ctx.get("lo", 0), ctx.get("hi", pw)
        try:
            body.update_idletasks()
            ph = body.winfo_height()
            lw = self._fs_list_col.winfo_width()
            if min(pw, ph, lw) <= 2 or pw - lw - sp_w <= 2:
                return False
        except tk.TclError:
            return False
        p = self._palette()
        hi = max(hi, lw)
        # 修复缺陷R17：代理底色取真实控件实际色（全屏窗口未登记调色
        # 板角色，p["card"]/p["window"] 与 CTk 默认底色有色差 ——
        # 拖动中整幅变色、松开跳回）；transparent 容器回退到窗口底色
        def actual_bg(widget, fallback: str) -> str:
            try:
                c = widget.cget("fg_color")
                if isinstance(c, str) and c == "transparent":
                    raise ValueError
                return self._resolve_row_color(c) or fallback
            except (tk.TclError, ValueError, AttributeError):
                try:
                    c = self._fs_list_win.cget("fg_color")
                    return self._resolve_row_color(c) or fallback
                except (tk.TclError, ValueError, AttributeError):
                    return fallback
        right_bg = actual_bg(self._fs_list_detail, p["card"])
        # 优化缺陷R42：左列即虚拟列表画布（底色 = window）
        left_bg = p["window"]
        # --- 右画布：全幅 + 视口滚动 ---
        right_c = tk.Canvas(body, bd=0, highlightthickness=0,
                            bg=right_bg, cursor="sb_h_double_arrow")
        right_c.place(x=0, y=0, relwidth=1, relheight=1)
        m2 = max(0, hi - lw)
        m1 = max(0, lw - lo)
        right_c.configure(scrollregion=(-m2, 0, pw + m1, ph),
                          xscrollincrement=1)
        # 初始视口自校正精确归零（同主界面 R13 修复）
        right_c.update_idletasks()
        cur0 = right_c.canvasx(0)
        if cur0:
            right_c.xview_scroll(int(round(-cur0)), "units")
        self._fs_live_draw_detail(right_c, p, ph)
        bars = self._fs_live_draw_scrollbars(right_c, lw, pw, ph, p)
        # 分隔条竖线：固定内容坐标 [lw, lw+sp]，随视口贴住分隔条
        right_c.create_rectangle(lw, 0, lw + sp_w, ph,
                                 fill=p["row_selected"], width=0)
        # --- 左裁剪框 + 固定宽左画布（列表 Label 采集绘制） ---
        left_clip = tk.Frame(body, bg=left_bg, bd=0,
                             highlightthickness=0,
                             cursor="sb_h_double_arrow")
        left_clip.place(x=0, y=0, width=lw, relheight=1)
        left_c = tk.Canvas(left_clip, bd=0, highlightthickness=0,
                           bg=left_bg, width=hi)
        left_c.place(x=0, y=0, relheight=1)
        self._fs_live_draw_list(left_c, p)
        if not left_c.find_all():
            try:
                right_c.destroy()
                left_clip.destroy()
            except tk.TclError:
                pass
            return False
        # --- 右标题条：「详情」标题跟随 ---
        hidden = []
        try:
            head_w = self._fs_detail_head.winfo_children()
            if head_w:
                head_w[0].pack_forget()      # 真实标题（pack 布局）
                hidden.append(head_w[0])
        except (tk.TclError, AttributeError, IndexError):
            head_w = ()
            hidden = []
        try:
            f_title = head_w[0]._font.create_scaled_tuple(
                head_w[0]._get_widget_scaling())
        except (AttributeError, IndexError, ValueError, tk.TclError):
            f_title = self._scaled_font(
                ctk.CTkFont(size=15, weight="bold"))
        try:
            head_h = max(1, self._fs_detail_head.winfo_height())
        except tk.TclError:
            head_h = 26
        tbar = tk.Frame(body, bg=right_bg, bd=0, highlightthickness=0,
                        cursor="sb_h_double_arrow")
        tbar.place(x=lw, y=0, width=max(1, pw - lw), height=head_h)
        tbar_c = tk.Canvas(tbar, bd=0, highlightthickness=0,
                           bg=right_bg, width=max(1, pw - lo))
        tbar_c.place(x=0, y=0, relheight=1)
        tbar_c.create_rectangle(0, 0, sp_w, head_h,
                                fill=p["row_selected"], width=0)
        tbar_c.create_text(sp_w + 6, head_h / 2, anchor="w",
                           font=f_title, fill=p["row_text"],
                           text="详情（簇典型样例 / 实例原始日志）")
        # 隐藏真实分隔条（代理竖线全权呈现；release 经布局恢复）
        try:
            self._fs_splitter.place_forget()
        except tk.TclError:
            pass
        self._fs_live = {
            "clip": left_clip, "left": left_c, "right": right_c,
            "lw": lw, "sp_w": sp_w, "ph": ph, "pw": pw, "bars": bars,
            "tbar": tbar, "hidden": hidden,
            "pending": lw, "t0": 0.0, "after_id": None}
        self._fs_live_flush()     # 初始帧同步
        return True

    def _fs_live_end(self) -> None:
        """销毁全屏矢量文本代理（幂等）。"""
        live = self._fs_live
        self._fs_live = None
        if live is None:
            return
        after_id = live.get("after_id")
        if after_id is not None:
            try:
                self.after_cancel(after_id)
            except tk.TclError:
                pass
        for key in ("right", "clip", "tbar"):
            try:
                live[key].destroy()
            except (tk.TclError, KeyError):
                pass
        for w in live.get("hidden", ()):      # 恢复真实「详情」标题
            try:
                w.pack(side="left", pady=(4, 2))
            except tk.TclError:
                pass

    def _fs_live_flush(self) -> None:
        """应用节流的最新拖动位置（裁剪框宽 + 视口滚动 + 标题条）。"""
        live = self._fs_live
        if live is None:
            return
        live["after_id"] = None
        left = live.get("pending")
        if left is None:
            return
        try:
            lw, pw = live["lw"], live["pw"]
            live["clip"].place_configure(width=left)
            shift = lw - left               # 视口平移量（内容=屏幕+shift）
            right_c = live["right"]
            cur = right_c.canvasx(0)
            delta = int(round(shift - cur))
            if delta:
                right_c.xview_scroll(delta, "units")
            # 右标题条跟随：左缘 x=left（含分隔条区），右缘到面板右缘
            tbar = live.get("tbar")
            if tbar is not None:
                tbar.place_configure(x=left, width=max(1, pw - left))
            # 屏幕固定滚动条的反向补偿（内容坐标 = 屏幕 + shift）
            bars = live.get("bars") or {}
            B = bars.get("B", 12)
            if "hslot" in bars:
                x0, x1 = 10 + shift, left - B + shift
                coords = right_c.coords(bars["hslot"])
                right_c.coords(bars["hslot"], x0, coords[1],
                               x1, coords[3])
                hx0, hx1 = bars.get("hfrac", (0.0, 1.0))
                tw = max(16.0, (x1 - x0) * (hx1 - hx0))
                coords = right_c.coords(bars["hthumb"])
                right_c.coords(bars["hthumb"], x0 + (x1 - x0) * hx0,
                               coords[1],
                               x0 + (x1 - x0) * hx0 + tw, coords[3])
            if "rslot" in bars:
                rx0, rx1 = pw - B + shift, pw + shift
                coords = right_c.coords(bars["rslot"])
                right_c.coords(bars["rslot"], rx0, coords[1],
                               rx1, coords[3])
                ry0, ry1 = bars.get("rfrac", (0.0, 1.0))
                y_top, y_bot = bars.get("ry", (0.0, 1.0))
                th0 = y_top + (y_bot - y_top) * ry0
                th1 = y_top + (y_bot - y_top) * max(ry1, ry0 + 0.08)
                right_c.coords(bars["rthumb"], rx0 + 2, th0,
                               rx1 - 2, th1)
        except (tk.TclError, KeyError, AttributeError):
            pass

    def _fs_live_draw_list(self, canvas, p) -> None:
        """左列列表内容：全屏虚拟列表矢量行绘制（优化缺陷R42）。

        全屏列表与主窗口共用 VirtualClusterList —— 直接复用主界面
        代理绘制（同一套行结构/选中态/颜色），面板坐标系取全屏
        body（vl 画布在其左上角原点）。
        """
        vl = getattr(self, "_fs_vl", None)
        if vl is None:
            return
        try:
            pw = self._fs_body.winfo_width()
        except tk.TclError:
            pw = 0
        self._live_draw_rows(canvas, pw, p, vl=vl, panel=self._fs_body)

    def _fs_live_draw_detail(self, canvas, p, cvh) -> None:
        """右列详情文本行（全屏详情框当前可见行区间，逐行完整绘制）。"""
        box = getattr(self, "_fs_list_detail", None)
        panel = self._fs_body
        if box is None:
            return
        try:
            tb = box._textbox
            f = self._scaled_font(box._font)
            import tkinter.font as _tkfont
            fm = _tkfont.Font(font=f)
            lh = max(1, fm.metrics("linespace"))
            x0 = tb.winfo_rootx() - panel.winfo_rootx() + 4
            y0 = tb.winfo_rooty() - panel.winfo_rooty() + 2
            start = int(str(tb.index("@0,0")).split(".")[0])
            nlines = cvh // lh + 2
            text = tb.get(f"{start}.0", f"{start + nlines}.end")
            for i, line in enumerate(text.splitlines()):
                canvas.create_text(x0, y0 + i * lh, anchor="nw", font=f,
                                   fill=p["row_text"], text=line)
        except (tk.TclError, AttributeError, ValueError):
            pass

    def _fs_live_draw_scrollbars(self, canvas, lw, pw, ph, p) -> dict:
        """滚动条近似（左列垂直条静态 / 左水平条与右垂直条逐帧补偿）。"""
        B = 12
        bars: dict = {}
        try:
            grip, slot = p["splitter_grip"], p["splitter"]
            panel = self._fs_body
            # 左列垂直条（虚拟列表画布 yview；贴左列右缘，静态）
            # 优化缺陷R42：滚动位置取自全屏虚拟列表自身画布/滚动条
            canvas.create_rectangle(lw - B, 0, lw, ph, fill=slot, width=0)
            pc = self._fs_vl._canvas
            y0, y1 = self._vbar_thumb(pc)
            canvas.create_rectangle(lw - B + 2, ph * y0,
                                    lw - 2, ph * max(y1, y0 + 0.08),
                                    fill=grip, width=0)
            # 左列水平条（虚拟列表自带 hbar 实测 y；屏幕左缘固定 → 每帧补偿）
            fs_hbar = self._fs_vl._hbar
            hy = fs_hbar.winfo_rooty() - panel.winfo_rooty()
            hh = max(B, fs_hbar.winfo_height())
            hx0, hx1 = self._hbar_thumb(pc)
            bars["hslot"] = canvas.create_rectangle(
                10, hy, lw - B, hy + hh, fill=slot, width=0)
            bars["hthumb"] = canvas.create_rectangle(
                10, hy + 2, 60, hy + hh - 2, fill=grip, width=0)
            bars["hfrac"] = (hx0, hx1)
            # 右列垂直条（详情框滚动条；屏幕右缘固定 → 每帧补偿）
            tb = self._fs_list_detail._textbox
            dy0 = self._fs_list_detail.winfo_rooty() - panel.winfo_rooty()
            dy1 = dy0 + self._fs_list_detail.winfo_height()
            bars["rslot"] = canvas.create_rectangle(
                pw - B, dy0, pw, dy1, fill=slot, width=0)
            ry0, ry1 = self._vbar_thumb(tb)
            bars["rthumb"] = canvas.create_rectangle(
                pw - B + 2, dy0 + 2, pw - 2, dy0 + 40,
                fill=grip, width=0)
            bars["rfrac"] = (ry0, ry1)
            bars["ry"] = (float(dy0), float(dy1))
            bars["B"] = B
        except (tk.TclError, AttributeError, KeyError):
            pass
        return bars

    def _open_compare_fullscreen(self) -> None:
        """对比差异列表全屏（独立窗口，每次新建后销毁）。"""
        win = self._make_fullscreen_window("对比差异列表")
        bar = ctk.CTkFrame(win, corner_radius=0)
        bar.pack(fill="x")
        # 修复缺陷R10：对比全屏搜索框字体 18（与全屏列表窗口一致）
        ctk.CTkLabel(bar, text="搜索：",
                     font=ctk.CTkFont(size=18)).pack(side="left",
                                                      padx=(12, 4))
        search_var = tk.StringVar()
        search = ctk.CTkEntry(bar, textvariable=search_var,
                             font=ctk.CTkFont(size=18),
                             placeholder_text="按摘要/模块/级别过滤，支持 and/or/not…")
        search.pack(side="left", fill="x", expand=True, padx=(0, 8), pady=8)
        count_label = ctk.CTkLabel(bar, text="", text_color="#8fa4b8")
        count_label.pack(side="right", padx=12)
        ctk.CTkButton(bar, text="关闭 (ESC)", width=110,
                      command=win.destroy).pack(side="right", padx=(0, 12))
        # 图例常驻顶栏（对比模式）
        ctk.CTkLabel(bar, text="+ 新增  - 消失  = 共同",
                     text_color="#8fa4b8").pack(side="right", padx=12)
        list_area = ctk.CTkScrollableFrame(win)
        list_area.pack(fill="both", expand=True)
        # 修复缺陷R9：对比全屏列表水平滚动条（摘要单行完整显示）
        cmp_hbar = self._make_hscroll(list_area)
        cmp_hbar.pack(fill="x")
        fs_rows: List[dict] = []

        def render_compare(keyword: str = "") -> None:
            kw = keyword.strip().lower()
            for child in list_area.winfo_children():
                child.destroy()
            fs_rows.clear()
            shown = 0
            total = 0
            for kind, cmp, item in self._iter_compare_rows():
                total += 1
                hay = (f"{item.summary} {item.level} {item.module} "
                       f"{self._CMP_SYMBOL[kind]}").lower()
                if kw and kw not in hay:
                    continue
                if total > 500:
                    continue
                shown += 1
                fs_rows.append(
                    self._make_compare_row(list_area, kind, cmp, item))
            count_label.configure(text=f"显示 {shown} / {total} 条")

        search_var.trace_add(
            "write", lambda *a: render_compare(search_var.get()))
        render_compare()

    def _open_detail_fullscreen(self) -> None:
        """详情面板全屏：完整详情 + 上下左右滚动（高亮一并复制）。

        修复缺陷R6：窗口预创建复用（隐藏而非销毁，二次打开仅刷新）。
        """
        content = self._detail_box.get("1.0", "end").rstrip("\n")
        if not content:
            messagebox.showinfo("提示", "请先选择一个错误查看详情")
            return
        win = self._fs_detail_win
        if win is None or not win.winfo_exists():
            win = self._fs_detail_win = self._build_fs_detail_window()
        else:
            win.deiconify()
            try:
                win.state("zoomed")
            except tk.TclError:
                pass
        box = self._fs_detail_box
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", content)
        # 复制主面板的高亮标签配置与范围（内容一致 -> 索引一致）
        for tag, color in _DETAIL_TAG_COLORS.items():
            try:
                box.tag_config(tag, foreground=color)
                ranges = self._detail_box.tag_ranges(tag)
                for i in range(0, len(ranges) - 1, 2):
                    box.tag_add(tag, str(ranges[i]), str(ranges[i + 1]))
            except (tk.TclError, AttributeError):
                pass
        box.configure(state="disabled")
        # 优化缺陷R54：内容刷新后按输入框当前关键字重扫高亮/计数
        # （窗口复用时框内文字保留，trace 不会自动重触发）
        self._apply_fd_search()
        win.after(60, win.focus_set)

    def _build_fs_detail_window(self) -> ctk.CTkToplevel:
        """构建（并缓存）详情全屏窗口（修复缺陷R6）。"""
        win = self._make_fullscreen_window("错误详情")
        # 隐藏而非销毁：ESC / 关闭返回主界面但保留窗口复用
        def hide() -> None:
            win.withdraw()
        win.bind("<Escape>", lambda e: hide())
        bar = ctk.CTkFrame(win, corner_radius=0)
        bar.pack(fill="x")
        ctk.CTkLabel(bar, text="错误详情（典型样例 · 上下文 · 降噪堆栈 · "
                               "支持上下左右滚动）").pack(side="left", padx=12)
        # 优化缺陷R54：详情文内查找 —— 搜索框 + 计数影子框（与列表
        # 全屏/主窗口同款 x/y 条；Enter/Shift+Enter 循环定位匹配，
        # 全部匹配黄底、当前定位匹配橙底并滚入视口）
        ctk.CTkLabel(bar, text="搜索：",
                     font=ctk.CTkFont(size=15)).pack(side="left",
                                                     padx=(8, 4))
        self._fd_search_var = tk.StringVar()
        fd_entry = ctk.CTkEntry(
            bar, textvariable=self._fd_search_var,
            font=ctk.CTkFont(size=15),
            placeholder_text="在详情中查找…")
        fd_entry.pack(side="left", fill="x", expand=True,
                      padx=(0, 8), pady=8)
        self._fd_search_entry = fd_entry
        self._fd_count_box = ctk.CTkFrame(
            bar, width=120, height=32, corner_radius=6,
            fg_color="transparent")
        self._fd_count_box.pack(side="left", padx=(0, 8))
        self._fd_count_box.pack_propagate(False)
        self._fd_count = ctk.CTkLabel(
            self._fd_count_box, text="", text_color="#8fa4b8",
            font=ctk.CTkFont(size=15), fg_color="transparent")
        self._fd_count.place(relx=0.5, rely=0.5, anchor="c")
        fd_entry.bind("<Return>",
                      lambda e: self._on_fd_search_enter(True))
        fd_entry.bind("<Shift-Return>",
                      lambda e: self._on_fd_search_enter(False))
        self._fd_search_var.trace_add(
            "write", lambda *a: self._apply_fd_search())
        ctk.CTkButton(bar, text="关闭 (ESC)", width=110,
                      command=hide).pack(side="right", padx=12, pady=8)
        # 修复缺陷R10：详情全屏基础字号 13 -> 18（全屏窗口大字体）
        box = ctk.CTkTextbox(win, font=ctk.CTkFont(family="Consolas",
                                                  size=self._font_px(18)),
                             wrap="none")
        # 水平滚动条（垂直滚动条 CTkTextbox 自带）
        xbar = tk.Scrollbar(win, orient="horizontal", command=box.xview)
        box.configure(xscrollcommand=xbar.set)
        xbar.pack(side="bottom", fill="x")
        box.pack(fill="both", expand=True, pady=(0, 4))
        # 优化缺陷R54：文内查找高亮标签 —— searchkw 全部匹配黄底、
        # fdcur 当前定位匹配橙底（tag_raise 压过 searchkw）
        box.tag_config("searchkw", background="#fbbf24",
                       foreground="#1f2937")
        box.tag_config("fdcur", background="#f97316",
                       foreground="#1f2937")
        box.tag_raise("fdcur")
        self._fs_detail_box = box
        return win

    # ==================================================================
    # 状态切换 / 退出
    # ==================================================================
    def _set_running(self, running: bool) -> None:
        """按钮状态机（调用前提：self._result / _compare_results 已就位）。

        - 未开始分析：四个操作按钮全部置灰；
        - 分析进行中：仅「取消」可点击，其余三个置灰；
        - 分析完成后：「取消 / 导出报告 / 复制摘要 / 统图表」全部可点击。
        """
        if running:
            self._start_btn.configure(state="disabled")
            for btn in (self._export_btn, self._copy_btn, self._chart_btn):
                btn.configure(state="disabled")
            self._cancel_btn.configure(state="normal")
            return
        self._start_btn.configure(state="normal")
        has_result = self._result is not None
        has_any = has_result or bool(self._compare_results)
        self._cancel_btn.configure(
            state="normal" if has_any else "disabled")
        self._export_btn.configure(
            state="normal" if has_any else "disabled")
        # 修复缺陷#10：统计图表在对比模式下也可用（展示两文件错误对比图）
        self._chart_btn.configure(
            state="normal" if has_any else "disabled")
        # 复制摘要面向单次分析结果（对比模式导出报告即可）
        self._copy_btn.configure(
            state="normal" if has_result else "disabled")

    def _on_close(self) -> None:
        self._save_config()
        # 优化缺陷R105：退出前停监控（释放文件句柄与后台线程）
        if self._tailing:
            self._stop_tail()
        # 修复缺陷R6：清理虚拟列表与全屏缓存窗口（释放控件/句柄）
        self._fs_list_refresh = None
        for win in (self._fs_list_win, self._fs_detail_win,
                    self._chart_window):
            if win is not None:
                try:
                    win.destroy()
                except (tk.TclError, ValueError):
                    pass
        self._fs_list_win = None
        self._fs_detail_win = None
        if self._virtual_list is not None:
            self._virtual_list.destroy()
            self._virtual_list = None
        self.destroy()


def main() -> None:
    """GUI 启动入口。"""
    app = LogCompressorApp()
    app.mainloop()


if __name__ == "__main__":
    main()
