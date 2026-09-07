r"""KubeJS 风格的自定义脚本宿主。

用户脚本放在**插件目录下的 ``maips/``**（与 KubeJS 的 ``kubejs/`` 目录同构），
``*.py`` 全部加载、数量不限，通过 ``mps_api`` 的 ``@on_event`` 装饰器订阅事件，
可以：

- **条件控制**：返回 ``False`` 否决一轮内置自动替换；返回 ``dict`` 覆盖本轮
  概率/权重/排除当前人格（``{"probability": .., "weights": {..}, "exclude_current": ..}``）；
- **直接切换**：``ctx.swap(预设, 时长, scope)`` / ``ctx.revert()``，跳过概率与权重；
- **观察**：``message`` / ``bot_message`` / ``timer`` / ``swap`` / ``load`` 五类事件。

设计约定（对齐 KubeJS）：

- 脚本是**受信任内容**——它们以完整 Python 权限运行在插件 Runner 进程内，
  信任级别等同安装一个插件；只运行自己编写或审查过的脚本。
- 热重载：按文件 mtime 检测变化（timer 顺带检查，或 ``/mps script reload``
  手动触发）；重载时先摘除该文件注册的全部处理器再重新执行。
- 下划线开头的文件（``_utils.py`` 之类）不作为脚本加载，可被其他脚本 import。
- 错误隔离：单个处理函数异常只记日志，不影响其他脚本与 bot 主链路。
- 加载失败的文件保持"未生效"状态，修复后下次重载自动恢复。

``example.py`` 示例脚本随插件静态分发在 ``maips/`` 内。
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
import traceback
import types
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

SHIM_MODULE_NAME = "mps_api"

EVENT_MESSAGE = "message"
EVENT_BOT_MESSAGE = "bot_message"
EVENT_TIMER = "timer"
EVENT_SWAP = "swap"
EVENT_LOAD = "load"
KNOWN_EVENTS = (EVENT_MESSAGE, EVENT_BOT_MESSAGE, EVENT_TIMER, EVENT_SWAP, EVENT_LOAD)

# 接管模式（script.takeover）下的运行时参数默认值：全部"留空"——
# 概率 0（内置管线惰性）、条件为空（不检查）、无权重候选时可被脚本设定。
# 脚本通过 ctx.set_swap_params(...) 在 load/message 等事件里设定这些值。
DEFAULT_SWAP_PARAMS: Dict[str, Any] = {
    "probability": 0.0,
    "per_stream": True,
    "reroll_during_swap": False,
    "exclude_current": False,
    "main_weight": 0.8,
    "preset_weights": {},
    "non_bot_keywords": [],
    "bot_keywords": [],
    "time_windows": [],
}

def _normalize_preset_weights(value: Any) -> Dict[str, float]:
    """规范化预设权重表；非法条目跳过，非正权重剔除。"""

    result: Dict[str, float] = {}
    for name, weight in dict(value or {}).items():
        try:
            normalized = float(weight)
        except (TypeError, ValueError):
            continue
        if normalized > 0:
            result[str(name)] = normalized
    return result


# set_swap_params 允许的字段及其规范化器
_PARAM_NORMALIZERS: Dict[str, Callable[[Any], Any]] = {
    "probability": lambda v: max(min(float(v), 1.0), 0.0),
    "per_stream": lambda v: bool(v),
    "reroll_during_swap": lambda v: bool(v),
    "exclude_current": lambda v: bool(v),
    "main_weight": lambda v: max(float(v), 0.0),
    "preset_weights": _normalize_preset_weights,
    "non_bot_keywords": lambda v: [str(x) for x in (v or [])],
    "bot_keywords": lambda v: [str(x) for x in (v or [])],
    "time_windows": lambda v: [str(x) for x in (v or [])],
}

DEFAULT_EMOTION_PROMPT_TEMPLATE = """以下是最近的聊天记录：
{context}

请评估当前聊天的整体情绪强度，只输出 0 到 {scale} 之间的一个数字（0 = 平静/平淡，{scale} = 情绪非常强烈），不要输出任何其他内容。"""

_TEXT_MATCH_MODES = ("contains", "exact", "regex", "prefix", "suffix")
_EMOJI_TAG_SPLIT = re.compile(r"[,，、;；\r\n\t]+")


def split_emoji_tags(raw_value: Any) -> List[str]:
    """按 MaiBot 官方规则把表情包描述拆成情绪标签列表（去重保序）。"""

    if raw_value is None:
        return []
    if isinstance(raw_value, str):
        parts = _EMOJI_TAG_SPLIT.split(raw_value.strip())
    elif isinstance(raw_value, list):
        parts = [str(item).strip() for item in raw_value]
    else:
        return []
    seen: set = set()
    tags: List[str] = []
    for part in parts:
        if part and part not in seen:
            seen.add(part)
            tags.append(part)
    return tags


# 表情包段 data 的文本包裹：形如 `[表情包: 标签1,标签2]` 或 `[表情：doge]`
_EMOJI_DATA_WRAP = re.compile(r"^\s*\[表情包?[:：]\s*(.*?)\s*\]\s*$", re.S)
# 非标签内容（如 CQ 码、纯文件名残留）剔除
_EMOJI_DATA_JUNK = re.compile(r"[\[\]{}]")

# 错误行中的绝对路径（盘符 / Unix / 引号包裹）——命令回显时剔除，防信息泄漏
_ERROR_PATH_PATTERN = re.compile(r'[A-Za-z]:[\\/][^\s"\']*|/[A-Za-z_][^\s"\']*/[^\s"\']*|File "[^"]*"')


def _sanitize_error_line(line: str) -> str:
    """脱敏错误摘要行：剔除盘符/绝对路径，保留异常类型与消息主体。

    完整 traceback 仍留在本地日志；命令回显（/mps script list 等）只用脱敏
    后的摘要，避免把宿主目录结构泄漏给可发指令的用户。
    """

    text = str(line or "").strip()
    text = _ERROR_PATH_PATTERN.sub("", text)
    # 压缩多空格
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


def _parse_emoji_data_tags(data_text: str) -> List[str]:
    """从表情包段 data 文本解析情绪标签（不依赖 hash 索引）。

    入站表情包经 MaiBot 处理后 ``data`` 形如 ``[表情包: 尴尬,心虚,无奈,委屈,无语]``
    （即 processed_plain_text 里的同一段描述）；本函数剥掉包裹后按
    逗号/顿号/分号拆分，返回去重保序的标签列表。解析不出返回空表。
    """

    text = str(data_text or "").strip()
    if not text:
        return []
    match = _EMOJI_DATA_WRAP.match(text)
    inner = match.group(1) if match else text
    inner = _EMOJI_DATA_JUNK.sub("", inner)
    return split_emoji_tags(inner)


def match_text(text: str, pattern: Any, mode: str = "contains") -> bool:
    """文本匹配：pattern 可为字符串或列表（任一命中即 True）。

    mode：contains（包含，忽略大小写）/ exact（完全一致）/ regex（正则）/
    prefix（前缀）/ suffix（后缀）；未知模式按 contains 处理。

    一致性约定（v1.3.2 起统一）：所有模式对 ``text`` 先 ``strip()`` 再比较
    （首尾空白不参与任何模式）；**空 pattern（空串）一律不命中**（否则
    ``re.search("", s)`` 恒真、``startswith("")`` 恒真会放大脚本 bug）。
    """

    patterns = [pattern] if isinstance(pattern, str) else list(pattern or [])
    stripped = str(text or "").strip()
    for raw_pattern in patterns:
        candidate = str(raw_pattern)
        if not candidate:
            continue  # 空 pattern 不命中
        try:
            if mode == "exact":
                hit = stripped == candidate
            elif mode == "regex":
                hit = re.search(candidate, stripped) is not None
            elif mode == "prefix":
                hit = stripped.startswith(candidate)
            elif mode == "suffix":
                hit = stripped.endswith(candidate)
            else:
                hit = candidate.lower() in stripped.lower()
        except re.error:
            hit = False
        if hit:
            return True
    return False


SWAP_PARAM_KEYS = tuple(sorted(_PARAM_NORMALIZERS))
_JSON_FENCE = re.compile(r"```(?:json)?[ \t]*\r?\n?(.*?)\r?\n?[ \t]*```", re.S)


def parse_json_response(response: str) -> Any:
    """从 LLM 响应解析 JSON；自动剥离代码围栏，失败返回 None。"""

    text = str(response or "").strip()
    if not text:
        return None
    fenced = _JSON_FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, (dict, list)) else None


class ScriptDecision:
    """一轮事件派发后聚合出的脚本决策。"""

    __slots__ = ("veto", "probability", "weight_overrides", "exclude_current", "direct_swaps")

    def __init__(self) -> None:
        self.veto: bool = False
        self.probability: Optional[float] = None
        self.weight_overrides: Dict[str, float] = {}
        self.exclude_current: Optional[bool] = None
        self.direct_swaps: int = 0


class ScriptContext:
    """传给脚本处理函数的事件上下文与操作接口。"""

    def __init__(
        self,
        host: "ScriptHost",
        event: str,
        *,
        session_id: str = "",
        scope_key: Optional[str] = None,
        group_id: str = "",
        user_id: str = "",
        text: str = "",
        is_command: bool = False,
        is_notify: bool = False,
        preset: Optional[str] = None,
        source: str = "",
        duration_minutes: Optional[int] = None,
        now: Optional[datetime] = None,
        raw_segments: Any = None,
        bot_user_id: str = "",
        bot_nickname: str = "",
    ) -> None:
        """由宿主构造；脚本侧只读属性 + 调用方法。"""

        self.event = event
        self.now: datetime = now or datetime.now()
        self.session_id = session_id
        # None = 当前事件没有默认作用域（如 timer），swap/revert 需显式 scope
        self.scope_key = scope_key
        self.group_id = group_id
        self.user_id = user_id
        self.text = text
        self.is_command = is_command
        self.is_notify = is_notify
        self.preset = preset
        self.source = source
        self.duration_minutes = duration_minutes
        self._segments: List[Dict[str, Any]] = [seg for seg in (raw_segments or []) if isinstance(seg, dict)]
        # bot 自身身份（官方账号/昵称；message/bot_message 事件注入，其余事件为空串）
        self.bot_user_id: str = str(bot_user_id or "").strip()
        self.bot_nickname: str = str(bot_nickname or "").strip()
        self._host = host
        self.veto_flag = False
        self.probability: Optional[float] = None
        self.weight_overrides: Dict[str, float] = {}
        self.exclude_current: Optional[bool] = None
        self.swap_count = 0

    # -- 只读便捷属性 ---------------------------------------------------

    @property
    def hour(self) -> int:
        """当前小时（0-23）。"""

        return self.now.hour

    @property
    def minute(self) -> int:
        """当前分钟（0-59）。"""

        return self.now.minute

    # -- 条件/覆盖操作 ---------------------------------------------------

    def veto(self, reason: str = "") -> None:
        """否决本轮内置自动替换（直接切换不受影响）。"""

        self.veto_flag = True
        if reason:
            self._host._backend._log("info", f"脚本否决自动替换：{reason}")

    def set_probability(self, value: float) -> None:
        """覆盖本轮自动替换概率（0~1）。"""

        value = float(value)
        if not 0.0 <= value <= 1.0:
            raise ValueError("probability 取值 0~1")
        self.probability = value

    def set_weight(self, name: str, weight: float) -> None:
        """覆盖/新增本轮某个候选（主人格为 "main"）的权重；weight<=0 移除。"""

        name = str(name).strip()
        if not name:
            return
        weight = float(weight)
        if weight > 0:
            self.weight_overrides[name] = weight
        else:
            self.weight_overrides.pop(name, None)

    def set_swap_params(self, **fields: Any) -> None:
        """持久设定接管模式下的替换参数（未提供的字段保持原值）。

        可用字段（对应被接管的配置项，接管未开启时设定无效）：
            probability（0~1）、per_stream、reroll_during_swap、exclude_current、
            main_weight、preset_weights（{预设名: 权重}）、
            non_bot_keywords / bot_keywords（关键词列表）、time_windows（时段列表）。
        建议在 ``load`` 事件中调用一次完成初始化。
        """

        for key, value in fields.items():
            normalizer = _PARAM_NORMALIZERS.get(key)
            if normalizer is None:
                raise ValueError(f"未知的替换参数：{key}（可用：{', '.join(_PARAM_NORMALIZERS)}）")
            try:
                self._host.params[key] = normalizer(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"替换参数 {key} 的值非法：{exc}") from exc
        self._host._backend._log("info", f"脚本已更新替换参数：{', '.join(fields) or '（无变更）'}")

    def exclude_current_in_round(self, value: bool) -> None:
        """覆盖本轮"忽略当前人格"开关。"""

        self.exclude_current = bool(value)

    # -- 切换操作 ---------------------------------------------------------

    def _resolve_scope(self, scope: Optional[str]) -> str:
        """解析作用域：默认取事件所属聊天流；timer 等事件需显式 \"global\"。"""

        if scope in (None, "", "stream"):
            if self.scope_key:
                return self.scope_key
            raise ValueError("当前事件没有默认作用域，请显式指定 scope='global'")
        if scope == "global":
            return "main"
        return str(scope)

    def swap(self, preset: str, duration_minutes: Optional[int] = None, scope: Optional[str] = None) -> None:
        """立即切换人格（同步生效）；时长缺省取预设文件记录值。"""

        key = self._resolve_scope(scope)
        if not self._host._backend.swap_filter_allows(self, key):
            return
        self._host._backend.script_swap(
            str(preset),
            duration_minutes,
            scope=key,
            source="script",
            trigger_stream_id=self.session_id,
        )
        self.swap_count += 1

    def revert(self, scope: Optional[str] = None) -> None:
        """立即恢复主人格。"""

        key = self._resolve_scope(scope)
        if not self._host._backend.swap_filter_allows(self, key):
            return
        self._host._backend.script_revert(scope=key, source="script", trigger_stream_id=self.session_id)
        self.swap_count += 1

    def current_preset(self, scope: Optional[str] = None) -> Optional[str]:
        """查询当前人格：预设名，主人格返回 None。"""

        key = self._resolve_scope(scope)
        state = self._host._backend.state.get(key)
        return None if state.is_main else state.preset

    # -- 其他 -------------------------------------------------------------

    @property
    def debug_enabled(self) -> bool:
        """当前 debug 模式开关（/mps debug true|false 控制，持久化）。

        debug 未开启时，依赖 debug 的自检/通知类脚本应静默跳过（不响应、
        不发送、不调用 LLM）。读取失败按 False（关闭）处理。
        """

        try:
            backend = self._host._backend
            if hasattr(backend, "state") and hasattr(backend.state, "load_debug_flag"):
                return bool(backend.state.load_debug_flag())
        except Exception:
            pass
        return False

    async def send_text(self, text: str) -> None:
        """向事件所属聊天流发送文本（仅 message/bot_message 事件可用）。"""

        if not self.session_id:
            raise ValueError("当前事件没有所属聊天流，无法 send_text")
        await self._host._backend.ctx.send.text(str(text), self.session_id)

    # -- 消息类型与精细匹配 ------------------------------------------------

    @property
    def segments(self) -> List[Dict[str, Any]]:
        """消息原始段列表（raw_message；bot_message 事件同样可用）。"""

        return self._segments

    @property
    def message_types(self) -> List[str]:
        """消息包含的段类型（text/emoji/image/voice...，去重保序）。"""

        types: List[str] = []
        for segment in self._segments:
            seg_type = str(segment.get("type") or "").strip() if isinstance(segment, dict) else ""
            if seg_type and seg_type not in types:
                types.append(seg_type)
        return types

    def has_type(self, seg_type: str) -> bool:
        """消息是否包含某种类型的段（text/emoji/image/voice...）。"""

        return str(seg_type) in self.message_types

    @property
    def has_text(self) -> bool:
        """消息是否含文本段。"""

        return self.has_type("text")

    @property
    def has_emoji(self) -> bool:
        """消息是否含表情包段。"""

        return self.has_type("emoji")

    @property
    def has_image(self) -> bool:
        """消息是否含图片段。"""

        return self.has_type("image")

    # -- @ 提及 / bot 身份 ------------------------------------------------

    @property
    def at_segments(self) -> List[Dict[str, Any]]:
        """本消息中类型为 ``at`` 的原始段列表（含 data.target_user_id 等）。

        段结构（宿主序列化定型）：:
            {"type": "at",
             "data": {"target_user_id": "12345",
                      "target_user_nickname": "昵称",
                      "target_user_cardname": "群名片或 None"}}

        无 at 段返回空列表。``message`` / ``bot_message`` 事件可用（timer/swap
        无消息段，返回空）。
        """

        return [seg for seg in self._segments if str(seg.get("type") or "").strip() == "at"]

    def at_targets(self) -> List[Dict[str, str]]:
        """本消息中被 @ 的对象摘要列表（每项含 user_id/nickname/cardname）。

        相当于把 ``ctx.at_segments`` 拍平成易读形态；无 at 段返回空列表。
        ``user_id`` 是被 @ 者的平台账号（QQ 号等）；nickname/cardname 可能为空串。
        """

        result: List[Dict[str, str]] = []
        for seg in self.at_segments:
            data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
            result.append(
                {
                    "user_id": str(data.get("target_user_id") or ""),
                    "nickname": str(data.get("target_user_nickname") or ""),
                    "cardname": str(data.get("target_user_cardname") or ""),
                }
            )
        return result

    @property
    def is_at_me(self) -> bool:
        """本消息是否 @ 了 bot 自己（含文本层 @官方昵称 兜底）。

        判断顺序（任一命中即 True）：
        1. 任一 ``at`` 段的 ``data.target_user_id`` 等于 ``ctx.bot_user_id``
           （bot_user_id 为空时跳过此项）；
        2. 文本层出现 ``@{bot_nickname}``（bot_nickname 为空时跳过此项）——
           与宿主 at 渲染行为一致：@ bot 时 processed_plain_text 里是官方昵称。

        注意：这只表示"消息里有没有 @ bot"，**不区分 @ 者是否 bot 自己**
        （bot 自 @ 也会命中）。要排除 bot 自触发请同时比较 ``ctx.user_id`` 与
        ``ctx.bot_user_id``。
        """

        if self.bot_user_id:
            for item in self.at_targets():
                if item["user_id"] and item["user_id"] == self.bot_user_id:
                    return True
        if self.bot_nickname and self.text:
            return f"@{self.bot_nickname}" in self.text
        return False

    # -- 脚本级持久 KV（跨重载/重启保留） --------------------------------

    def kv_get(self, key: str, default: Any = None) -> Any:
        """读取脚本持久键值；键非法或不存在返回 default。

        存储为数据目录 ``personality/script_kv.json``，**跨脚本热重载与插件重启
        保留**——适合计数累计（如"点数攒到 N 才切"）。同一插件的所有脚本共享
        一张表，不同脚本请用带前缀的键名（如 ``nightly_points``）避免冲突。
        写入限制见 :meth:`kv_set`。
        """

        try:
            return self._host._backend.state.script_kv_get(str(key), default)
        except AttributeError:
            return default

    def kv_set(self, key: str, value: Any) -> bool:
        """写入脚本持久键值（覆盖旧值）；成功返回 True。

        失败返回 False（不抛异常，可自行 log）：键名非法（仅允许字母数字
        下划线点连字符，长度 ≤64）或值不是 JSON 可序列化（str/int/float/
        bool/list/dict/None）或全表条目超过上限。例：:

            ctx.kv_set("nightly_points", 3)
            ctx.kv_set("nightly_boost", True)
        """

        try:
            return bool(self._host._backend.state.script_kv_set(str(key), value))
        except AttributeError:
            return False

    def kv_del(self, key: str) -> bool:
        """删除脚本持久键值；键不存在返回 False。"""

        try:
            return bool(self._host._backend.state.script_kv_del(str(key)))
        except AttributeError:
            return False

    def kv_keys(self) -> List[str]:
        """列出脚本持久键值全部键名（字典序）。"""

        try:
            return list(self._host._backend.state.script_kv_keys())
        except AttributeError:
            return []

    def matches(self, pattern: Any, mode: str = "contains") -> bool:
        """文本匹配：pattern 可为字符串或列表（任一命中）。

        mode：contains（包含，忽略大小写）/ exact（完全一致）/ regex（正则）/
        prefix（前缀）/ suffix（后缀）。
        """

        return match_text(self.text, pattern, mode)

    def emoji_emotions(self) -> List[str]:
        """当前消息中表情包对应的情绪标签（去重保序）。

        标签来源（按序尝试，保证可靠）：
        1. 入站表情包优先按 hash 查宿主索引（``emoji.register.after_build_description``
           维护，覆盖已入库表情包）；
        2. 索引未命中时**回退解析表情包段 data 文本**（即消息里 ``[表情包: 标签1,
           标签2,...]`` 的描述内容，按逗号/顿号/分号拆分）——索引缺失/未入库的
           表情包也能匹配，且与 MaiBot"表情包默认带 5 个关键词"的渲染一致；
        3. bot 自己发送的表情包再并入 emoji 选择器命中情绪。
        """

        tags: List[str] = []
        for segment in self._segments:
            if not isinstance(segment, dict) or str(segment.get("type")) != "emoji":
                continue
            # 优先 hash 索引
            indexed = self._host.emoji_index_emotions(str(segment.get("hash") or ""))
            if indexed:
                for tag in indexed:
                    if tag not in tags:
                        tags.append(tag)
                continue
            # 回退：从段 data 文本解析描述标签
            for tag in _parse_emoji_data_tags(str(segment.get("data") or "")):
                if tag not in tags:
                    tags.append(tag)
        if self.event == "bot_message":
            last = self._host.last_bot_emoji_emotion(self.session_id)
            if last and last not in tags:
                tags.append(last)
        return tags

    def match_emoji(self, emotions: Any, mode: str = "any") -> bool:
        """表情包情绪匹配：emotions 可为字符串或列表。

        mode="any"：消息中任一表情命中任一给定情绪即 True；
        mode="all"：给定情绪全部出现在消息表情情绪中才 True。
        """

        wanted_raw = emotions if isinstance(emotions, (list, tuple)) else [emotions]
        wanted = [str(item).strip() for item in wanted_raw if str(item).strip()]
        if not wanted:
            return False
        present = [tag.lower() for tag in self.emoji_emotions()]
        if str(mode).strip().lower() == "all":
            return all(item.lower() in present for item in wanted)
        return any(item.lower() in present for item in wanted)

    # -- LLM 调用与情绪评分 ------------------------------------------------

    async def llm_generate(self, prompt: str, task: str = "replyer", temperature: Optional[float] = None, max_tokens: Optional[int] = None) -> str:
        """调用 MaiBot 的 LLM 任务生成文本；失败返回空串。

        task 为宿主任务名（replyer / planner / utils 等），可附加
        temperature / max_tokens 限制生成行为。
        """

        payload: Dict[str, Any] = {"prompt": str(prompt), "model": str(task)}
        if temperature is not None:
            payload["temperature"] = float(temperature)
        if max_tokens is not None:
            payload["max_tokens"] = int(max_tokens)
        try:
            result = await self._host._backend.ctx.llm.generate(**payload)
        except Exception as exc:
            self._host._backend._log("warning", f"脚本 LLM 调用失败：{exc}")
            return ""
        if isinstance(result, dict) and result.get("success"):
            return str(result.get("response") or "")
        self._host._backend._log("warning", f"脚本 LLM 调用失败：{result}")
        return ""

    async def recent_context(self, recent_n: int = 10) -> str:
        """拉取当前聊天流最近 N 条消息的可读文本（用于提交给 LLM）。"""

        if not self.session_id:
            return ""
        backend = self._host._backend
        try:
            messages = await backend.ctx.message.get_recent(self.session_id, int(recent_n))
            readable = await backend.ctx.message.build_readable(messages)
        except Exception as exc:
            backend._log("warning", f"脚本拉取聊天上下文失败：{exc}")
            return ""
        if isinstance(readable, dict):
            readable = readable.get("readable") or readable.get("text") or str(readable)
        return str(readable or "")

    async def emotion_score(self, recent_n: int = 10, prompt_template: Optional[str] = None, task: str = "replyer", temperature: Optional[float] = 0.3, max_tokens: Optional[int] = 16, scale: float = 10.0) -> Optional[float]:
        """让 LLM 评估当前聊天氛围的情绪分（0~scale，默认 0~10）；解析失败返回 None。

        Args:
            recent_n: 提交给 LLM 的最近消息条数。
            prompt_template: 自定义提示模板，需含 ``{context}`` 占位符（可选 ``{scale}``）；
                缺省使用内置的"情绪强度评分"提示。
            task: 宿主 LLM 任务名（replyer / planner / utils 等）。
            temperature / max_tokens: 生成参数；max_tokens 默认 16（只需输出一个数字）。
        """

        context = await self.recent_context(recent_n)
        template = str(prompt_template) if prompt_template else DEFAULT_EMOTION_PROMPT_TEMPLATE
        prompt = template.replace("{context}", context).replace("{scale}", str(scale))
        response = await self.llm_generate(prompt, task=task, temperature=temperature, max_tokens=max_tokens)
        match = re.search(r"[0-9]+(?:[.][0-9]+)?", response or "")
        if not match:
            return None
        value = float(match.group())
        return max(0.0, min(value, float(scale)))

    async def llm_json(self, prompt: str, task: str = "replyer", temperature: Optional[float] = None, max_tokens: Optional[int] = None) -> Any:
        """调用 LLM 并把响应解析为 JSON（dict/list）；失败返回 None。

        自动剥离 ```json 代码围栏。配合 ``ctx.set_swap_params`` /
        ``ctx.swap`` 可实现"LLM 返回参数 → 脚本校验后应用"，例如让 LLM 输出
        ``{"preset": "preset1", "duration": 60}`` 或
        ``{"probability": 0.3, "time_windows": ["20:00-23:00"]}``；
        回填前可用 ``mps_api.SWAP_PARAM_KEYS`` 过滤未知键。
        """

        response = await self.llm_generate(prompt, task=task, temperature=temperature, max_tokens=max_tokens)
        return parse_json_response(response)

    def get_swap_params(self) -> Dict[str, Any]:
        """读取当前生效的替换参数快照（含脚本已设定的接管参数）。"""

        return self._host._backend._effective_swap_params()

    def log(self, message: str) -> None:
        """写插件日志（info 级）。"""

        self._host._backend._log("info", f"[脚本] {message}")


class _Handler:
    """一个脚本注册的事件处理函数。"""

    __slots__ = ("owner", "name", "fn")

    def __init__(self, owner: str, name: str, fn: Callable[..., Any]) -> None:
        self.owner = owner
        self.name = name
        self.fn = fn


class ScriptHost:
    """脚本加载、热重载与事件派发。"""

    def __init__(self, scripts_dir: Path, backend: Any) -> None:
        """绑定脚本目录与后端（插件实例）。"""

        self._dir = Path(scripts_dir)
        self._backend = backend
        self._handlers: Dict[str, List[_Handler]] = {event: [] for event in KNOWN_EVENTS}
        self._owner_counts: Dict[str, int] = {}
        self._modules: Dict[str, dict] = {}
        self._mtimes: Dict[str, Tuple[float, int]] = {}
        self._errors: Dict[str, str] = {}
        self._loading_owner: Optional[str] = None
        self._shim: Optional[types.ModuleType] = None
        self._tasks: set = set()
        self._last_scan_at: float = 0.0
        # 接管模式下的运行时参数（脚本经 ctx.set_swap_params 设定；重载脚本后
        # 由 load 事件重新初始化）
        self.params: Dict[str, Any] = dict(DEFAULT_SWAP_PARAMS)
        # 表情包情绪：hash → {description, emotions} 索引（来源：emoji 注册/选择钩子）
        self.emoji_index: Dict[str, Dict[str, Any]] = {}
        # 各聊天流最近一次 bot 发送表情包的情绪（emoji 选择器命中的标签）
        self.bot_emoji_emotions: Dict[str, Tuple[float, str]] = {}

    # ------------------------------------------------------------------
    # 垫片与注册
    # ------------------------------------------------------------------

    def install_shim(self) -> None:
        """把 ``mps_api`` 垫片装进 sys.modules（脚本 import 的入口）。"""

        shim = types.ModuleType(SHIM_MODULE_NAME)
        shim.__doc__ = "夺舍麦麦脚本 API：@on_event 订阅事件；ctx 提供切换/否决/覆盖操作。"

        def on_event(event: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
            host = self
            normalized = str(event or "").strip().lower()
            if normalized not in KNOWN_EVENTS:
                raise ValueError(f"未知事件 {event!r}，可选：{', '.join(KNOWN_EVENTS)}")

            def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
                if host._loading_owner is None:
                    raise RuntimeError("on_event 只能在脚本加载期调用（写在模块顶层）")
                handler = _Handler(owner=host._loading_owner, name=getattr(fn, "__name__", "<anonymous>"), fn=fn)
                host._handlers[normalized].append(handler)
                host._owner_counts[host._loading_owner] = host._owner_counts.get(host._loading_owner, 0) + 1
                return fn

            return decorator

        shim.on_event = on_event
        shim.EVENTS = KNOWN_EVENTS
        shim.SWAP_PARAM_KEYS = SWAP_PARAM_KEYS
        self._shim = shim
        sys.modules[SHIM_MODULE_NAME] = shim

    def shutdown(self) -> None:
        """清空全部处理器与后台任务（插件卸载时调用）。"""

        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._handlers = {event: [] for event in KNOWN_EVENTS}
        self._owner_counts.clear()
        self._modules.clear()
        self._mtimes.clear()
        self._errors.clear()
        self.params = dict(DEFAULT_SWAP_PARAMS)
        self.emoji_index.clear()
        self.bot_emoji_emotions.clear()
        # 卸载 mps_api 垫片：避免同名模块被后续宿主/其它插件误用、或二次 install 覆盖
        if self._shim is not None and sys.modules.get(SHIM_MODULE_NAME) is self._shim:
            sys.modules.pop(SHIM_MODULE_NAME, None)
        self._shim = None

    # ------------------------------------------------------------------
    # 加载与热重载
    # ------------------------------------------------------------------

    def scan_and_reload(self, *, force: bool = False, min_interval_sec: float = 1.0) -> Dict[str, int]:
        """扫描脚本目录：加载新文件、重载变化文件、卸载消失文件。

        Returns:
            Dict[str, int]: ``{"loaded": 新载入数, "unchanged": 未变数, "errors": 错误数}``。
        """

        import time as _time

        now = _time.monotonic()
        if not force and (now - self._last_scan_at) < min_interval_sec:
            return {"loaded": 0, "unchanged": 0, "errors": len(self._errors)}
        self._last_scan_at = now

        self._dir.mkdir(parents=True, exist_ok=True)
        seen: Dict[str, Tuple[float, int]] = {}
        loaded = 0
        unchanged = 0
        for path in sorted(self._dir.glob("*.py")):
            if path.name.startswith("_"):
                continue  # 下划线开头 = 工具库，不作为脚本加载
            try:
                stat = path.stat()
            except OSError:
                continue
            stamp = (stat.st_mtime, stat.st_size)
            seen[path.name] = stamp
            if self._mtimes.get(path.name) == stamp:
                unchanged += 1
                continue
            if self._load_file(path):
                loaded += 1

        for name in list(self._mtimes):
            if name not in seen:
                self._unload_file(name)
        # 以本次扫描为准：新/变更文件已重载，消失的已卸载，报错文件记录时间戳避免反复重试
        self._mtimes = seen
        if loaded:
            self.notify_load()
        return {"loaded": loaded, "unchanged": unchanged, "errors": len(self._errors)}

    def _load_file(self, path: Path) -> bool:
        """执行单个脚本文件；成功返回 True，失败记录错误返回 False。"""

        owner = path.name
        self._unload_file(owner)
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            self._errors[owner] = f"读取失败: {exc}"
            self._backend._log("warning", f"脚本 {owner} 读取失败：{exc}")
            return False
        namespace: dict = {"__name__": f"mps_script_{path.stem}", "__file__": str(path)}
        self._loading_owner = owner
        try:
            exec(compile(source, str(path), "exec"), namespace)  # noqa: S102 - 用户脚本即受信任内容
        except Exception:
            error = traceback.format_exc(limit=6)
            self._errors[owner] = error
            self._backend._log("warning", f"脚本 {owner} 加载失败（保持未生效）：\n{error}")
            return False
        finally:
            self._loading_owner = None
        self._modules[owner] = namespace
        self._errors.pop(owner, None)
        return True

    def _unload_file(self, owner: str) -> None:
        """摘除某文件注册的全部处理器并丢弃其模块状态。"""

        for event in KNOWN_EVENTS:
            self._handlers[event] = [h for h in self._handlers[event] if h.owner != owner]
        self._owner_counts.pop(owner, None)
        self._modules.pop(owner, None)
        self._mtimes.pop(owner, None)
        self._errors.pop(owner, None)

    def register(self, owner: str, event: str, fn: Callable[..., Any]) -> None:
        """（垫片调用）注册当前加载文件的处理函数。"""

        handler = _Handler(owner=owner, name=getattr(fn, "__name__", "<anonymous>"), fn=fn)
        self._handlers[event].append(handler)
        self._owner_counts[owner] = self._owner_counts.get(owner, 0) + 1

    # ------------------------------------------------------------------
    # 事件派发
    # ------------------------------------------------------------------

    def build_message_context(
        self,
        event: str,
        *,
        session_id: str = "",
        scope_key: Optional[str] = None,
        group_id: str = "",
        user_id: str = "",
        text: str = "",
        is_command: bool = False,
        is_notify: bool = False,
        raw_segments: Any = None,
    ) -> ScriptContext:
        """构造 message/bot_message 事件上下文。"""

        backend = self._backend
        return ScriptContext(
            self,
            event,
            session_id=session_id,
            scope_key=scope_key,
            group_id=group_id,
            user_id=user_id,
            text=text,
            is_command=is_command,
            is_notify=is_notify,
            raw_segments=raw_segments,
            bot_user_id=str(getattr(backend, "script_bot_user_id", "") or ""),
            bot_nickname=str(getattr(backend, "script_bot_nickname", "") or ""),
        )

    def build_swap_context(self, *, scope_key: str, preset: Optional[str], source: str, duration_minutes: Optional[int]) -> ScriptContext:
        """构造 swap 事件上下文。"""

        return ScriptContext(self, EVENT_SWAP, scope_key=scope_key, preset=preset, source=source, duration_minutes=duration_minutes)

    def build_timer_context(self) -> ScriptContext:
        """构造 timer 事件上下文（无默认作用域）。"""

        return ScriptContext(self, EVENT_TIMER)

    async def dispatch(self, event: str, ctx: ScriptContext) -> ScriptDecision:
        """派发事件给该事件的全部处理器（逐个执行，单点异常隔离）。"""

        decision = ScriptDecision()
        for handler in list(self._handlers.get(event, [])):
            result: Any = None
            try:
                result = handler.fn(ctx)
                if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                    result = await result
            except Exception:
                self._record_runtime_error(event, handler)
                continue
            self._apply_result(ctx, decision, result)
        decision.veto = decision.veto or ctx.veto_flag
        if ctx.probability is not None:
            decision.probability = ctx.probability
        if ctx.weight_overrides:
            decision.weight_overrides.update(ctx.weight_overrides)
        if ctx.exclude_current is not None:
            decision.exclude_current = ctx.exclude_current
        decision.direct_swaps = ctx.swap_count
        return decision

    @staticmethod
    def _apply_result(ctx: ScriptContext, decision: ScriptDecision, result: Any) -> None:
        """解释处理函数返回值：False=否决；dict=覆盖；其他=无操作。"""

        if result is False:
            decision.veto = True
            ctx.veto_flag = True
            return
        if isinstance(result, dict):
            probability = result.get("probability")
            if probability is not None:
                ctx.set_probability(probability)
            weights = result.get("weights")
            if isinstance(weights, dict):
                for name, weight in weights.items():
                    ctx.set_weight(name, weight)
            exclude = result.get("exclude_current")
            if exclude is not None:
                ctx.exclude_current_in_round(bool(exclude))

    def _record_runtime_error(self, event: str, handler: _Handler) -> None:
        """记录运行期异常（截断堆栈），连续失败只保留最后一次详情。"""

        error = traceback.format_exc(limit=4)
        self._errors[f"{handler.owner}（运行期）"] = f"事件 {event} 处理器 {handler.name}：\n{error}"
        self._backend._log("warning", f"脚本 {handler.owner} 的 {handler.name} 在 {event} 事件中抛出异常（已跳过）")

    # ------------------------------------------------------------------
    # swap 事件广播（fire-and-forget）
    # ------------------------------------------------------------------

    def notify_swap(self, scope_key: str, preset: Optional[str], source: str, duration_minutes: Optional[int]) -> None:
        """广播 swap 事件；在无事件循环时静默跳过。"""

        ctx = self.build_swap_context(scope_key=scope_key, preset=preset, source=source, duration_minutes=duration_minutes)
        self._fire(self._guarded_dispatch(EVENT_SWAP, ctx))

    def notify_load(self) -> None:
        """广播 load 事件（脚本加载/重载后触发，用于初始化接管参数）。"""

        ctx = ScriptContext(self, EVENT_LOAD, scope_key=None)
        self._fire(self._guarded_dispatch(EVENT_LOAD, ctx))

    def _fire(self, coro) -> None:
        """在有事件循环时后台执行协程，否则静默丢弃。

        任务异常不静默吞掉：done 回调里取 ``task.exception()`` 记日志
        （防止 "Task exception was never retrieved" 与无主异常）。
        """

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return

        def _on_done(task: asyncio.Task) -> None:
            self._tasks.discard(task)
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                self._backend._log("warning", f"脚本后台任务异常：{exc!r}")

        task = loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(_on_done)

    async def _guarded_dispatch(self, event: str, ctx: ScriptContext) -> None:
        try:
            await self.dispatch(event, ctx)
        except Exception as exc:  # 防御：dispatch 内部已隔离，这里兜底
            self._backend._log("warning", f"脚本事件 {event} 派发异常：{exc}")

    # ------------------------------------------------------------------
    # 描述（/mps script list）
    # ------------------------------------------------------------------

    def record_emoji_index(self, file_hash: str, description: str, emotions: Any) -> None:
        """登记表情包 hash → 情绪标签索引（emotions 为空时从 description 拆分）。

        同 hash 重复登记时**合并**情绪标签（去重保序）而非覆盖——不同来源
        （register 描述 vs select 单标签命中）可能给同一 hash 补充不同标签，
        覆盖会静默丢标签。
        """

        normalized_hash = str(file_hash or "").strip()
        if not normalized_hash:
            return
        tags = split_emoji_tags(emotions) or split_emoji_tags(description)
        if not tags:
            return
        existing = self.emoji_index.get(normalized_hash, {})
        merged = list(existing.get("emotions", []))
        for tag in tags:
            if tag not in merged:
                merged.append(tag)
        self.emoji_index[normalized_hash] = {
            "description": str(existing.get("description") or description or ""),
            "emotions": merged,
        }

    def record_bot_emoji(self, stream_id: str, emotion: str) -> None:
        """记录某聊天流最近一次 bot 发送表情包的情绪标签。"""

        normalized_stream = str(stream_id or "").strip()
        emotion_text = str(emotion or "").strip()
        if not normalized_stream or not emotion_text:
            return
        self.bot_emoji_emotions[normalized_stream] = (time.time(), emotion_text)

    def emoji_index_emotions(self, file_hash: str) -> List[str]:
        """查询表情包 hash 对应的情绪标签；未入索引返回空列表。"""

        entry = self.emoji_index.get(str(file_hash or "").strip())
        return list(entry.get("emotions", [])) if entry else []

    def last_bot_emoji_emotion(self, session_id: str, ttl_sec: float = 600.0) -> Optional[str]:
        """查询聊天流最近一次 bot 表情包情绪（超过 TTL 视为无）。"""

        record = self.bot_emoji_emotions.get(str(session_id or "").strip())
        if not record:
            return None
        stamp, emotion = record
        if time.time() - stamp > ttl_sec:
            return None
        return emotion

    def describe(self) -> List[str]:
        """生成脚本加载状态文本（快照读取，错误行做路径脱敏）。"""

        lines: List[str] = []
        modules = dict(self._modules)
        counts = dict(self._owner_counts)
        errors = dict(self._errors)
        if not modules and not errors:
            lines.append("（没有已加载的脚本）")
        for owner in sorted(modules):
            count = counts.get(owner, 0)
            lines.append(f"- {owner}：已加载，{count} 个处理器")
        for owner, error in sorted(errors.items()):
            # 取首个非 Traceback 的非空行；再做路径脱敏，避免把宿主绝对路径
            # 发给触发命令的用户（完整堆栈仍留在本地日志）
            first_line = next(
                (line for line in error.splitlines() if line.strip() and not line.startswith("Traceback")),
                "未知错误",
            )
            lines.append(f"- {owner}：❌ {_sanitize_error_line(first_line)[:120]}")
        return lines
