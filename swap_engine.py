"""替换引擎：触发条件判定 → 概率检查 → 权重抽取。

抽取池 = 主人格（权重 ``weights.main``）+ 全部预设（配置页「预设权重表」
``weights.presets``——JSON 文本字段，由 :func:`parse_preset_weights` 解析——
与 /mps weight 写入的持久化覆盖合并）。权重不要求总和为 1。

触发条件（全部满足才进行概率检查；某项未配置则跳过该项检查）：
- 时段：当前本地时间落在任一 ``HH:MM-HH:MM`` 窗口内（支持跨午夜）；留空 = 全天；
- 非bot消息关键词：最近一条普通用户消息命中任一关键词；
- bot消息关键词：最近一条 bot 发出的消息命中任一关键词。
关键词命中记录带 TTL（见 :data:`CONDITION_TTL_SEC`），过期视为未命中；
关键词列表为空或恰好为占位 ``["default"]`` 时视为未配置（不检查）。
"""

from __future__ import annotations

import json
import math
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

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


def parse_preset_weights(raw: Any) -> Dict[str, float]:
    """把配置页「预设权重表」解析成 ``{预设名: 权重}``。

    **为什么是字符串**：WebUI 对 ``dict`` 类型字段会渲染成对象控件、默认值显示为
    ``[object Object]``（见 MaiBot 开发文档《02-开发入门/03-配置系统》§5.6），
    因此配置模型把该字段声明为 ``str``（JSON 文本），解析放在这里。

    兼容写法（配置页是文本框，用户可能手打）：
      1. JSON 对象：``{"乐子人": 1.5, "普瑞赛斯": 2}``（推荐）
      2. 一行一条：``乐子人=1.5``（分隔符 ``=`` / ``:`` / ``：`` / 空格 均可）
      3. JSON 数组或旧版 TOML 表：``["乐子人=1.5"]`` / ``{"乐子人": 1.5}``
         （dict/list 形态由调用方/校验器归一后传入）

    非正权重、非有限权重（``inf`` / ``nan``，例如手打 ``1e400``）与非法条目一律
    跳过（抽取池 :func:`build_weight_pool` 本就会剔除非正值，这里提前丢弃便于
    ``/mps status`` 显示与实际生效一致）；解析失败返回空表，由调用方决定是否告警。
    """

    table: Dict[str, float] = {}
    for name, value in _iter_weight_entries(raw):
        normalized = str(name).strip()
        if not normalized or normalized == MAIN_MARKER:
            continue
        try:
            weight = float(str(value).strip())
        except (TypeError, ValueError):
            continue
        # 非有限值必须丢弃：random.choices 会抛 "Total of weights must be finite"
        if not math.isfinite(weight):
            continue
        if weight > 0:
            table[normalized] = weight
    return table


def _iter_weight_entries(raw: Any) -> Iterator[Tuple[Any, Any]]:
    """把各种形态的权重表拆成 ``(名称, 权重原文)`` 迭代器（解析内部实现）。"""

    if isinstance(raw, dict):
        yield from raw.items()
        return
    if isinstance(raw, (list, tuple)):
        for item in raw:
            yield from _iter_weight_entries(item)
        return
    text = str(raw or "").strip()
    if not text:
        return
    if text[0] in "{[":
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if parsed is not None and not isinstance(parsed, str):
            yield from _iter_weight_entries(parsed)
            return
    for line in text.splitlines():
        entry = line.strip().strip(",").strip()
        entry = entry.strip("{}[]").strip().strip(",").strip()
        if not entry:
            continue
        name, value = _split_weight_line(entry)
        if name:
            yield name, value


def _split_weight_line(entry: str) -> Tuple[str, str]:
    """拆一行 ``名称=权重``（分隔符 ``=`` / ``:`` / ``：`` / 空白）；无分隔返回 ("", "")。"""

    for sep in ("=", ":", "："):
        if sep in entry:
            name, _, value = entry.partition(sep)
            return _strip_quotes(name), _strip_quotes(value)
    name, _, value = entry.rpartition(" ")
    return _strip_quotes(name), _strip_quotes(value)


def _strip_quotes(text: str) -> str:
    """去掉手打 JSON 片段常见的包裹引号/括号。"""

    return str(text).strip().strip(",").strip().strip('"').strip("'").strip()


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
    main_value = _finite_weight(main_weight)
    if exclude != MAIN_MARKER and main_value is not None and main_value > 0:
        pool[MAIN_MARKER] = main_value
    for name, weight in (preset_weights or {}).items():
        normalized = str(name).strip()
        if not normalized or normalized == MAIN_MARKER:
            continue
        if exclude and normalized == exclude:
            continue
        value = _finite_weight(weight)
        if value is not None and value > 0:
            pool[normalized] = value
    return pool


def _finite_weight(value: Any) -> Optional[float]:
    """把任意输入归一为有限正权重值；非数字/非有限（inf、nan）返回 None。

    ``random.choices`` 对 ``inf``/``nan`` 会抛 ``ValueError``（"Total of weights
    must be finite"），而异常会一路冒泡到宿主 hook 派发器——因此所有权重入口
    （配置页文本、/mps weight、脚本覆盖）统一经此处过滤。
    """

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def draw_persona(
    pool: Dict[str, float],
    *,
    probability: float,
    rng: Optional[random.Random] = None,
) -> SwapDecision:
    """概率检查通过后按权重抽取；池为空直接不抽。

    抽取前再次剔除非有限/非正权重（``_finite_weight``）；对"单个有限但总和的
    浮点加法溢出为 ``inf``"的池（如两条 ``1e308``）按最大权重归一。即使调用方
    传入脏池，本函数也不会抛异常，最坏退化为"不抽"（``rolled=False``）。
    """

    safe_pool: Dict[str, float] = {}
    for name, weight in (pool or {}).items():
        value = _finite_weight(weight)
        if value is not None and value > 0:
            safe_pool[str(name)] = value
    if not safe_pool:
        return SwapDecision(picked="", weights=pool, rolled=False)
    picker = rng if rng is not None else random
    if picker.random() >= float(probability):
        return SwapDecision(picked="", weights=pool, rolled=False)
    names = list(safe_pool.keys())
    weights = [safe_pool[name] for name in names]
    # 单个权重有限不代表总有限：如两条 1e308 相加溢出为 inf，random.choices 仍会
    # 抛 "Total of weights must be finite"。此处按最大权重归一（缩放不改变相对
    # 比例，抽取分布等价），保证总和必然有限。
    if not math.isfinite(sum(weights)):
        top = max(weights)
        weights = [weight / top for weight in weights]
        if not math.isfinite(sum(weights)):
            return SwapDecision(picked="", weights=pool, rolled=False)
    picked = picker.choices(names, weights=weights, k=1)[0]
    return SwapDecision(picked=picked, weights=pool, rolled=True)
