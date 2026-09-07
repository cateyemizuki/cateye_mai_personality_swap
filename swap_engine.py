"""替换引擎：触发条件判定 → 概率检查 → 权重抽取。

抽取池 = 主人格（权重 ``weights.main``）+ 全部预设（config ``[weights.presets]``
与 /mps weight 写入的持久化覆盖合并）。权重不要求总和为 1。

触发条件（全部满足才进行概率检查；某项未配置则跳过该项检查）：
- 时段：当前本地时间落在任一 ``HH:MM-HH:MM`` 窗口内（支持跨午夜）；留空 = 全天；
- 非bot消息关键词：最近一条普通用户消息命中任一关键词；
- bot消息关键词：最近一条 bot 发出的消息命中任一关键词。
关键词命中记录带 TTL（见 :data:`CONDITION_TTL_SEC`），过期视为未命中；
关键词列表为空或恰好为占位 ``["default"]`` 时视为未配置（不检查）。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

CONDITION_TTL_SEC = 600.0
KEYWORD_PLACEHOLDER = "default"
MAIN_MARKER = "__main__"


@dataclass(slots=True)
class SwapDecision:
    """一次抽取的结果。"""

    picked: str  # MAIN_MARKER 或预设名
    weights: Dict[str, float] = field(default_factory=dict)
    rolled: bool = False  # 是否真的执行了概率与抽取（False = 条件/概率未通过）


def normalize_keywords(raw: Optional[Sequence[object]]) -> List[str]:
    """清洗关键词列表；空表或仅剩占位符时返回空表（= 不检查）。"""

    words = [str(item).strip() for item in (raw or []) if str(item).strip()]
    if not words:
        return []
    lowered = {word.lower() for word in words}
    if lowered == {KEYWORD_PLACEHOLDER}:
        return []
    return [word for word in words if word.lower() != KEYWORD_PLACEHOLDER] or []


def parse_time_windows(raw: Optional[Sequence[object]]) -> List[Tuple[int, int]]:
    """解析 ``HH:MM-HH:MM`` 时段列表为分钟数区间（支持跨午夜）；空 = 全天。"""

    windows: List[Tuple[int, int]] = []
    for entry in raw or []:
        text = str(entry).strip()
        if not text or "-" not in text:
            continue
        start_text, _, end_text = text.partition("-")
        start = _parse_hhmm(start_text)
        end = _parse_hhmm(end_text)
        if start is None or end is None:
            continue
        windows.append((start, end))
    return windows


def _parse_hhmm(text: str) -> Optional[int]:
    """``HH:MM`` → 当天第几分钟；非法返回 None。"""

    parts = text.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def in_time_window(windows: Sequence[Tuple[int, int]], now: Optional[datetime] = None) -> bool:
    """当前时间是否落在任一时段内；窗口列表为空表示全天放行。

    只取 ``now``（缺省取当前时刻）的**时分**（忽略日期）；跨午夜窗口按
    ``start > end`` 处理。``now`` 只求值一次，避免跨分钟边界两次取值不一致。
    """

    if not windows:
        return True
    current_dt = now if now is not None else datetime.now()
    current = current_dt.hour * 60 + current_dt.minute
    for start, end in windows:
        if start <= end:
            if start <= current < end:
                return True
        elif current >= start or current < end:  # 跨午夜
            return True
    return False


def text_matches_any(text: str, keywords: Sequence[str]) -> bool:
    """文本是否命中任一关键词（不区分大小写的子串匹配）。"""

    if not keywords:
        return False
    lowered = str(text or "").lower()
    return any(word.lower() in lowered for word in keywords)


class ConditionTracker:
    """关键词命中时间戳（内存态，带 TTL，**按聊天流状态键隔离**）。

    在 ``per_stream=True``（默认"仅触发聊天流替换"）下，替换状态按聊天流隔离，
    触发条件也必须按流记录——否则 A 群的关键词命中会错误地点燃 B 群的自动
    替换概率（跨群串扰）。所有 record/fresh 均接受 ``key``（状态键：群号 / QQ 号
    或 "main"）；缺省 ``key`` 时落到全局槽 ``*``（供兼容与无流上下文场景）。

    - ``record_non_bot_hit(key)`` / ``record_bot_hit(key)``：记录某流命中时间；
    - ``non_bot_fresh(key)`` / ``bot_fresh(key)``：该流命中是否仍在 TTL 内；
    - ``reset(key=None)``：key=None 清全部；key 给定只清该流。
    """

    _DEFAULT_KEY = "*"

    def __init__(self, ttl_sec: float = CONDITION_TTL_SEC) -> None:
        """绑定命中记录的有效期。"""

        self._ttl = float(ttl_sec)
        self._hits: Dict[str, Dict[str, float]] = {}

    def _slot(self, key: Optional[str]) -> Dict[str, float]:
        """取（或建）某 key 的命中槽。"""

        normalized = str(key).strip() if key else self._DEFAULT_KEY
        return self._hits.setdefault(normalized, {"non_bot": 0.0, "bot": 0.0})

    def record_non_bot_hit(self, key: Optional[str] = None, *, now: Optional[float] = None) -> None:
        """记录某聊天流一次普通用户消息关键词命中。"""

        self._slot(key)["non_bot"] = now if now is not None else time.time()

    def record_bot_hit(self, key: Optional[str] = None, *, now: Optional[float] = None) -> None:
        """记录某聊天流一次 bot 消息关键词命中。"""

        self._slot(key)["bot"] = now if now is not None else time.time()

    def _fresh(self, stamp: float, now: float) -> bool:
        """命中记录是否仍在有效期内。"""

        return stamp > 0 and (now - stamp) <= self._ttl

    def non_bot_fresh(self, key: Optional[str] = None, *, now: Optional[float] = None) -> bool:
        """该聊天流的非 bot 关键词是否处于命中有效期。"""

        return self._fresh(self._slot(key)["non_bot"], now if now is not None else time.time())

    def bot_fresh(self, key: Optional[str] = None, *, now: Optional[float] = None) -> bool:
        """该聊天流的 bot 关键词是否处于命中有效期。"""

        return self._fresh(self._slot(key)["bot"], now if now is not None else time.time())

    def reset(self, key: Optional[str] = None) -> None:
        """清空命中记录：key=None 清全部；否则只清该 key。"""

        if key is None:
            self._hits.clear()
            return
        self._hits.pop(str(key).strip(), None)


def build_weight_pool(
    main_weight: float,
    preset_weights: Dict[str, float],
    *,
    exclude: str = "",
) -> Dict[str, float]:
    """构建抽取池：``{MAIN_MARKER: main_weight, 预设名: 权重}``。

    Args:
        main_weight: 主人格权重（>0 才入池）。
        preset_weights: 预设权重表（非正值剔除）。
        exclude: 需要排除的当前人格（MAIN_MARKER 或预设名）；空串不排除。
    """

    pool: Dict[str, float] = {}
    if exclude != MAIN_MARKER and main_weight > 0:
        pool[MAIN_MARKER] = float(main_weight)
    for name, weight in (preset_weights or {}).items():
        normalized = str(name).strip()
        if not normalized or normalized == MAIN_MARKER:
            continue
        if exclude and normalized == exclude:
            continue
        if float(weight) > 0:
            pool[normalized] = float(weight)
    return pool


def draw_persona(
    pool: Dict[str, float],
    *,
    probability: float,
    rng: Optional[random.Random] = None,
) -> SwapDecision:
    """概率检查通过后按权重抽取；池为空直接不抽。"""

    if not pool:
        return SwapDecision(picked="", weights=pool, rolled=False)
    picker = rng if rng is not None else random
    if picker.random() >= float(probability):
        return SwapDecision(picked="", weights=pool, rolled=False)
    names = list(pool.keys())
    weights = [pool[name] for name in names]
    picked = picker.choices(names, weights=weights, k=1)[0]
    return SwapDecision(picked=picked, weights=pool, rolled=True)
