# -*- coding: utf-8 -*-
"""GUI 配置持久化：用户目录 JSON 存储，自动保存 / 恢复常用参数。

设计说明：
- 配置文件位于 ~/.log_ai_compressor/config.json，避免污染仓库；
- 深度合并默认值：新增配置项后老配置文件仍可正常加载；
- 保存动作自动创建目录，任何 IO 异常静默降级（配置失败不阻断主流程）。
"""
from __future__ import annotations

import copy
import json
import pickle
import time
from pathlib import Path
from typing import Any, Dict, Optional

from log_ai_compressor.constants import CONFIG_FILE

DEFAULT_CONFIG: Dict[str, Any] = {
    # 修复缺陷R10：默认级别含 FATAL（FATAL 受复选框控制）
    "levels": ["FATAL", "ERROR", "FAIL"],
    "include": [],
    "exclude": [],
    "top_n": 20,
    # 修复缺陷#5：默认上下文行数 5 -> 50（详情可看内容太少）
    "context_lines": 50,
    # 修复缺陷R10：字体大小档位（小/中/大/特大，持久化恢复）
    "font_size": "中",
    # 修复缺陷R12：错误列表|详情面板 分隔条位置（左列宽度比例）
    "splitter_ratio": 0.4,
    # 优化缺陷R71：默认自动识别（150 行分层采样打分选最优规则）
    "rule": "auto",
    # 优化缺陷R79：智能分析模式（full 完整 / deep 深度 / fast 快速）
    "analyze_mode": "full",
    # 优化缺陷R84：相似度阈值档位（standard 标准 / strict 严格 / lenient 宽松）
    "similarity": "standard",
    # 优化缺陷R103：自定义脱敏正则（⚙ 弹层多行文本，每行一条）
    "redact_custom": "",
    # 优化缺陷R111：已屏蔽错误模板（message_template 列表，已知
    # 噪音错误右键屏蔽后默认从列表隐藏）
    "muted": [],
    # 优化缺陷R114：过滤预设（名称 → 时间范围/行数上限/关键词/
    # 正则开关/相似度/编码 的 UI 原值快照，⚙ 弹层一键套用）
    "filter_presets": {},
    "appearance": "dark",
    "window": {"width": 1280, "height": 840},
    "last_files": [],
}


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """深度合并（override 优先，dict 递归合并）。"""
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if (key in merged and isinstance(merged[key], dict)
                and isinstance(value, dict)):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


class ConfigStore:
    """配置存取器（JSON 文件）。"""

    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path) if path else CONFIG_FILE

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Dict[str, Any]:
        """读取配置（不存在或损坏时返回默认值）。"""
        try:
            if self._path.is_file():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return _merge(DEFAULT_CONFIG, data)
        except (OSError, ValueError):
            pass
        return copy.deepcopy(DEFAULT_CONFIG)

    def save(self, config: Dict[str, Any]) -> bool:
        """保存配置（自动建目录；失败静默返回 False）。"""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            merged = _merge(DEFAULT_CONFIG, config)
            self._path.write_text(
                json.dumps(merged, ensure_ascii=False, indent=2),
                encoding="utf-8")
            return True
        except OSError:
            return False

    def reset(self) -> bool:
        """恢复默认配置。"""
        return self.save(copy.deepcopy(DEFAULT_CONFIG))


# ---------------------------------------------------------------------------
# 优化缺陷R100：分析历史（最近 N 次结果可回放，重开软件免重跑大文件）
# ---------------------------------------------------------------------------
HISTORY_KEEP = 10


class HistoryStore:
    """分析历史存取：结果对象 pickle + index.json 元数据索引。

    - 存储于配置目录 history/ 子目录，最多保留 HISTORY_KEEP 条（超出
      删最旧，连 pickle 文件一起清）；
    - pickle 仅加载本应用自己写出的文件（本地桌面场景可信源）；跨
      版本反序列化失败静默丢弃该条（返回 None，不阻断主流程）；
    - 一切 IO 异常静默降级（历史是增强功能，永不阻断分析主流程）。
    """

    def __init__(self, config_path: Optional[Path] = None):
        base = (Path(config_path).parent if config_path
                else CONFIG_FILE.parent)
        self._dir = base / "history"
        self._index = self._dir / "index.json"

    def _read_index(self) -> list:
        try:
            if self._index.is_file():
                data = json.loads(self._index.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return [d for d in data if isinstance(d, dict)]
        except (OSError, ValueError):
            pass
        return []

    def _write_index(self, entries: list) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        self._index.write_text(
            json.dumps(entries, ensure_ascii=False, indent=1),
            encoding="utf-8")

    def _remove_file(self, meta: dict) -> None:
        try:
            (self._dir / f"{meta.get('id', '')}.pkl").unlink(
                missing_ok=True)
        except OSError:
            pass

    def add(self, result) -> None:
        """分析完成自动入库（新条目置顶，超出上限删最旧）。"""
        try:
            entries = self._read_index()
            hid = f"{time.time_ns()}"
            self._dir.mkdir(parents=True, exist_ok=True)
            with open(self._dir / f"{hid}.pkl", "wb") as fh:
                pickle.dump(result, fh, protocol=pickle.HIGHEST_PROTOCOL)
            s = result.stats
            entries.insert(0, {
                "id": hid,
                "saved_at": time.time(),
                "source": s.source,
                "total_lines": s.total_lines,
                "error_lines": s.error_lines,
                "clusters": len(result.clusters),
                "rule": s.rule_name,
            })
            stale = entries[HISTORY_KEEP:]
            for meta in stale:
                self._remove_file(meta)
            self._write_index(entries[:HISTORY_KEEP])
        except OSError:
            pass

    def list(self) -> list:
        """元数据列表（新→旧）。"""
        return self._read_index()

    def load(self, hid: str):
        """按 id 加载完整分析结果（损坏/不兼容返回 None）。"""
        try:
            with open(self._dir / f"{hid}.pkl", "rb") as fh:
                return pickle.load(fh)
        except Exception:                      # 反序列化失败静默丢弃
            return None

    def clear(self) -> None:
        """清空全部历史（索引与 pickle 文件一起删）。"""
        for meta in self._read_index():
            self._remove_file(meta)
        try:
            self._index.unlink(missing_ok=True)
        except OSError:
            pass

