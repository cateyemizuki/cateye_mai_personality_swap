"""注入通道的统一导出。"""

from .base import (
    InjectedBlock,
    append_text_item,
    assemble_blocks,
    build_user_message_snapshot,
    remove_official_temp_style_items,
    rewrite_tail_user_item,
)
from .preset_blocks import (
    LABEL_BEHAVIOR,
    LABEL_EXPRESSION,
    LABEL_IDENTITY_CORRECTION,
    LABEL_PERSONA,
    OFFICIAL_TEMP_STYLE_MARKER,
    build_behavior_block,
    build_expression_block,
    build_identity_correction_block,
    build_persona_block,
    is_official_temp_style_text,
    preset_item_texts,
)
from .system_replace import (
    replace_planner_system_text,
    replace_replyer_system_text,
    rewrite_first_system_item,
)

__all__ = [
    "InjectedBlock",
    "LABEL_BEHAVIOR",
    "LABEL_EXPRESSION",
    "LABEL_IDENTITY_CORRECTION",
    "LABEL_PERSONA",
    "OFFICIAL_TEMP_STYLE_MARKER",
    "append_text_item",
    "assemble_blocks",
    "build_behavior_block",
    "build_expression_block",
    "build_identity_correction_block",
    "build_persona_block",
    "build_user_message_snapshot",
    "remove_official_temp_style_items",
    "rewrite_tail_user_item",
    "is_official_temp_style_text",
    "preset_item_texts",
    "replace_planner_system_text",
    "replace_replyer_system_text",
    "rewrite_first_system_item",
]
