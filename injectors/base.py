"""注入通道的底层工具：提示块渲染与 Context Item 快照构造。

items 通道（planner 行为风格、independent 兼容模式、官方临时风格过滤）直接
操作宿主传来的 Context Item 快照列表。快照结构见宿主
``src/llm_models/request_snapshot.py::deserialize_context_item_snapshot``：
``{"item_type", "meta": {"item_id", "logical_turn_id", "timestamp"}, "parts"}``。
普通消息 Item 不携带工具调用，不触碰 ``validate_context_items`` 的工具链校验，
唯一硬性要求是 ``item_id`` 全局唯一。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, List, Optional
from uuid import uuid4

ITEM_ID_PREFIX = "mps"


@dataclass(slots=True)
class InjectedBlock:
    """一个待注入的提示块。"""

    label: str
    text: str

    def render(self) -> str:
        """渲染为 ``【标签】\\n正文``；正文为空返回空串。"""

        body = (self.text or "").strip()
        if not body:
            return ""
        return f"【{self.label}】\n{body}"


def assemble_blocks(blocks: Iterable[str]) -> str:
    """拼接多个非空提示块为一段注入文本。"""

    rendered = [block.strip() for block in blocks if block and block.strip()]
    return "\n\n".join(rendered)


def build_user_message_snapshot(text: str, items: Optional[Iterable[Any]] = None) -> dict:
    """构造一条可追加到 Context Items 末尾的 UserMessageItem 快照。

    ``logical_turn_id`` 继承列表末尾最后一条消息，保持回合分组；``item_id``
    用 ``mps-<uuid4>`` 保证全局唯一。
    """

    logical_turn_id: Any = None
    if items:
        for raw in reversed(list(items)):
            meta = raw.get("meta") if isinstance(raw, dict) else None
            if isinstance(meta, dict):
                candidate = meta.get("logical_turn_id")
                logical_turn_id = candidate if isinstance(candidate, str) and candidate.strip() else None
                break
    return {
        "item_type": "UserMessageItem",
        "meta": {
            "item_id": f"{ITEM_ID_PREFIX}-{uuid4().hex}",
            "logical_turn_id": logical_turn_id,
            "timestamp": datetime.now().astimezone().isoformat(),
        },
        "parts": [{"type": "text", "text": text}],
    }


def append_text_item(items: List[Any], text: str) -> List[Any]:
    """复制 items 列表并在末尾追加一条注入消息，返回新列表。"""

    snapshot = build_user_message_snapshot(text, items)
    return [*items, snapshot]


def rewrite_tail_user_item(items: List[Any], *, prefix: str, new_text: str) -> Optional[List[Any]]:
    """把 items 中**最后一条**以 ``prefix`` 开头的 UserMessageItem 文本改写。

    - 用于替换宿主在请求 items 末尾追加的固定提醒（如 planner 的
      "你需要输出对{bot_name}发言的分析…"——它在 hook 触发前已由宿主置于
      items 最末）。
    - 只改写**最后一条**命中项（保留其它）；找不到返回 None（不新增）。
    - 返回浅拷贝新列表；未命中返回 None。
    """

    if not isinstance(items, list) or not prefix:
        return None
    hit_index = -1
    for index in range(len(items) - 1, -1, -1):
        raw = items[index]
        if not isinstance(raw, dict) or raw.get("item_type") != "UserMessageItem":
            continue
        parts = raw.get("parts")
        if not isinstance(parts, list):
            continue
        text = "".join(
            str(part.get("text") or "")
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text"
        )
        if text.startswith(str(prefix)):
            hit_index = index
            break
    if hit_index < 0:
        return None
    out = [dict(item) if isinstance(item, dict) else item for item in items]
    raw = out[hit_index]
    new_parts: List[Any] = []
    text_written = False
    for part in raw.get("parts", []):
        if (
            not text_written
            and isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        ):
            new_parts.append({**part, "text": str(new_text)})
            text_written = True
        else:
            new_parts.append(part)
    if not text_written:
        new_parts.append({"type": "text", "text": str(new_text)})
    out[hit_index] = {**raw, "parts": new_parts}
    return out


def remove_official_temp_style_items(items: List[Any]) -> tuple[List[Any], int]:
    """从 items 中移除官方临时说话风格消息，返回 (新列表, 移除数)。

    官方临时风格（``_build_temporary_reply_style_message``）是请求 items 里
    一条以固定文案开头的 UserMessageItem；预设激活期间按配置整条剔除。
    """

    from .preset_blocks import is_official_temp_style_text

    kept: List[Any] = []
    removed = 0
    for raw in items:
        text = ""
        if isinstance(raw, dict) and raw.get("item_type") == "UserMessageItem":
            parts = raw.get("parts")
            if isinstance(parts, list):
                text = "\n".join(
                    str(part.get("text") or "")
                    for part in parts
                    if isinstance(part, dict) and part.get("type") == "text"
                )
        if text and is_official_temp_style_text(text):
            removed += 1
            continue
        kept.append(raw)
    return kept, removed
