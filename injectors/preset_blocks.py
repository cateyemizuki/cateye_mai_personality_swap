"""由激活预设生成三个注入块，以及官方临时风格的识别标记。"""

from __future__ import annotations

from typing import Optional

from ..preset_store import PersonaPreset
from .base import InjectedBlock

# 官方临时说话风格消息的固定前缀（maisaka_generator_base._build_temporary_reply_style_message）
OFFICIAL_TEMP_STYLE_MARKER = "你的说话风格可以尝试"

LABEL_EXPRESSION = "表达方式要求"
LABEL_PERSONA = "人格约束"
LABEL_BEHAVIOR = "行为风格提示"
LABEL_IDENTITY_CORRECTION = "当前身份提醒"  # 当前身份说明（v1.3.8 引入，v1.4.0 定稿文案）


def build_expression_block(preset: PersonaPreset) -> str:
    """预设的"注入表达"块（replyer 通道）。"""

    return InjectedBlock(label=LABEL_EXPRESSION, text=preset.expression).render()


def build_persona_block(preset: PersonaPreset) -> str:
    """预设的"人格约束"块（replyer 通道）。"""

    return InjectedBlock(label=LABEL_PERSONA, text=preset.persona).render()


def build_behavior_block(preset: PersonaPreset) -> str:
    """预设的"行为风格"块（planner 通道）。"""

    return InjectedBlock(label=LABEL_BEHAVIOR, text=preset.behavior).render()


def build_identity_correction_block(persona_name: str, bot_name: str) -> str:
    """构建"当前身份"提醒文本（replyer 尾注 / planner 替换 host 末位提醒统一用）。

    文案语义（用户定稿，v1.4.0）：
        你现在是{persona_name}。上文中{bot_name}的发言是人格切换前你以旧身份
        说过的，你只需要知道那是你过去进行过的发言，适当参考即可；一切以当前
        的设定与身份优先。

    设计意图：切换后上下文里 bot 旧身份的发言（在 planner 里以 ``user=官方名``
    的 Session 消息出现、在 replyer 里以 assistant/带名 user 出现）会被模型误读
    为"另一个叫 {bot_name} 的人"或"当前仍是旧身份"。本提醒明确：那些发言是
    **同一 bot 在切换前以旧身份说的**——既不否认历史（模型可参考语气/关系），
    又锁定当前身份为新人格名。

    仅在预设配置了 ``persona_name``（新身份 ≠ 官方名）时才有意义；persona_name
    或官方名为空 → 返回空串（不注入）。
    """

    persona = str(persona_name or "").strip()
    official = str(bot_name or "").strip()
    if not persona or not official:
        return ""
    text = (
        f"你现在是{persona}。上文中{official}的发言是人格切换前你以旧身份说过的，"
        f"你只需要知道那是你过去进行过的发言，适当参考即可；"
        f"一切以当前的设定与身份优先。"
    )
    return InjectedBlock(label=LABEL_IDENTITY_CORRECTION, text=text).render()


def is_official_temp_style_text(text: str) -> bool:
    """判断一段 Item 文本是否为官方临时说话风格消息。"""

    return OFFICIAL_TEMP_STYLE_MARKER in str(text or "")


def preset_item_texts(preset: Optional[PersonaPreset]) -> str:
    """预设三个块拼合（items 通道 / independent 模式用）。"""

    if preset is None:
        return ""
    parts = [
        build_expression_block(preset),
        build_persona_block(preset),
    ]
    return "\n\n".join(part for part in parts if part.strip())
