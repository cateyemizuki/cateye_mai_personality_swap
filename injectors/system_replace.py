"""替换式注入：改写 system 消息里的官方人格/表达/行为段落为预设内容。

背景（对齐宿主 1.2.x 结构）：
- replyer 的 items 首条是唯一的 ``SystemMessageItem``，其文本由
  ``prompts/<locale>/maisaka_replyer.prompt`` 渲染，结构固定为：:

      {identity}                                  # 名字行 + 人格 + 情绪尾巴
      现在请你读读之前的聊天记录，把握当前的话题，然后给出日常且口语化的回复，
      {reply_style}                               # 官方表达风格
      你可以参考【回复信息参考】中的信息，但是视情况而定，不用完全遵守。
      {group_chat_attention_block}
      {replyer_output_instruction}

  因此可把 ``identity`` 段（开头 → 固定句 A）与 ``reply_style`` 段
  （A 行尾 → 固定句 B）替换为预设的人格 / 表达，其余官方场景规则原样保留。

- planner 的 items 首条 SystemMessageItem 由 ``maisaka_chat.prompt`` 渲染，
  其中 ``{bot_name}的行为风格：{behavior_style}`` 之后紧跟固定句
  ``以上 …的行为风格可以帮助你更好地决策``，可作为行为段切分锚点。

安全策略：本模块**只做文本级替换**，任何锚点未命中都返回 None，
由调用方（plugin.py）回退到原有"追加式"注入，保证替换失败不导致人格失效、
也绝不误删官方内容。锚点基于 zh-CN 模板文案；其它 locale / 自定义 system
未命中时自动回退（append），行为与旧版一致。
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional

# replyer system 模板固定句（zh-CN）
REPLYER_ANCHOR_A = "现在请你读读之前的聊天记录"
REPLYER_ANCHOR_B = "你可以参考【回复信息参考】中的信息"
# planner system 模板行为段锚点
PLANNER_STYLE_MARK = "的行为风格："
PLANNER_TAIL_MARK = "\n以上"  # "以上 {bot_name}的行为风格可以帮助你更好地决策"


def replace_replyer_system_text(
    text: str,
    *,
    bot_name: str = "",
    preset_persona: str = "",
    preset_expression: str = "",
) -> Optional[str]:
    """把 replyer system 文本里的人格段(identity)与表达段(reply_style)替换为预设。

    结构切分（宿主模板 ``{identity}`` 是 system 文本的第一段，其后才是固定句）：
        identity_region = text[: idx_a]   # 官方身份段（名字行+人格+情绪尾巴）→ 整段删除
        a_line          = 固定句 A 的完整一行  # 保留（“现在请你读读…”）
        style_region    = A 行尾 → 固定句 B 开头  # 官方 reply_style 段 → 替换为预设表达
        tail            = text[idx_b:]     # 自固定句 B 起（场景规则等，原样保留）

    任一锚点缺失返回 None（调用方回退追加式）。
    """

    normalized = str(text or "")
    idx_a = normalized.find(REPLYER_ANCHOR_A)
    if idx_a < 0:
        return None
    line_end = normalized.find("\n", idx_a)
    a_line = normalized[idx_a:line_end] if line_end != -1 else normalized[idx_a:]
    idx_b = normalized.find(REPLYER_ANCHOR_B, line_end + 1 if line_end != -1 else idx_a)
    if idx_b < 0:
        return None

    # 新身份段（替换掉整段官方 identity）：名字行（拿得到 bot 名时）+ 预设人格
    identity_lines: List[str] = []
    if str(bot_name or "").strip():
        identity_lines.append(f"你的名字是{str(bot_name).strip()}。")
    persona_text = str(preset_persona or "").strip()
    if persona_text:
        identity_lines.append(persona_text)
    new_identity = "\n".join(identity_lines)

    # 新表达段：预设表达
    new_style = str(preset_expression or "").strip()

    # 重组：新身份 + A 行 + 新表达 + 尾部场景规则（自 B 起保留）
    out_blocks: List[str] = []
    if new_identity:
        out_blocks.append(new_identity)
    out_blocks.append(a_line)
    if new_style:
        out_blocks.append(new_style)
    joined = "\n".join(out_blocks)
    tail = normalized[idx_b:]
    if not tail:
        return joined
    return f"{joined}\n{tail}"


def replace_planner_system_text(
    text: str,
    *,
    bot_name: str = "",
    preset_behavior: str = "",
    shell_name: str = "",
) -> Optional[str]:
    """把 planner system 文本里的行为风格段替换为预设行为风格。

    结构：
        idx  = 找到 ``<bot_name>的行为风格：``（缺 bot_name 时回退裸标记）
        tail = 该段之后第一个 ``\\n以上``（固定句“以上 …可以帮助你更好地决策”）
        行为段 = text[idx:] 至 tail → 替换为 预设行为；tail 起（含“以上”）保留。

    ``shell_name``（v1.3.9，人格化外壳）：宿主 planner 模板把 ``{bot_name}``
    （官方昵称）写满外壳——“关注 {bot_name} 与用户的对话”“为 {bot_name} 选择
    动作”“你不是 {bot_name} 本人”“帮{bot_name}搜集信息”等。预设配置了
    ``persona_name`` 时，传该值即可在替换行为段后，把整段文本里出现的官方
    昵称一并替换为 persona_name（外壳也随之人格化，决策模型眼里的 bot 指称
    从“迷迭香”变成“普瑞赛斯”）。为空 → 只换行为段（旧行为）。

    找不到相应锚点时返回 None（回退追加）。
    """

    normalized = str(text or "")
    bot = str(bot_name or "").strip()
    shell = str(shell_name or "").strip()
    mark = f"{bot}{PLANNER_STYLE_MARK}" if bot else ""
    idx = normalized.find(mark) if mark else -1
    if idx < 0:
        idx = normalized.find(PLANNER_STYLE_MARK)
        if idx < 0:
            return None
        mark = PLANNER_STYLE_MARK
    after = idx + len(mark)
    tail = normalized.find(PLANNER_TAIL_MARK, after)
    if tail < 0:
        return None

    new_behavior = str(preset_behavior or "").strip()
    replaced = f"{normalized[:idx]}{mark}{new_behavior}{normalized[tail:]}"

    # 人格化外壳：把官方昵称整体替换为 persona_name（若提供且不同）。
    # 注意此时行为段 marker 行的“{官方名}的行为风格：”仍含官方名，也会被一并
    # 换成 persona_name——行为段锚点已消费，不再参与二次定位，安全。
    if shell and bot and shell != bot:
        replaced = replaced.replace(bot, shell)
    return replaced


def rewrite_first_system_item(
    items: List[Any],
    rewriter: Callable[[str], Optional[str]],
) -> Optional[List[Any]]:
    """对 items 中第一条 SystemMessageItem 的首个文本 part 执行 rewriter。

    - 遍历保留全部 items；仅原地改写第一条 SystemMessageItem 的文本 part，
      其余 item/part 原样透传（meta、顺序、replay 关联不变）。
    - rewriter 返回 None 表示锚点未命中 → 整体返回 None（调用方回退追加）；
    - items 为空 / 无 SystemMessageItem / 无文本 part → 返回 None。
    """

    if not isinstance(items, list):
        return None
    out: List[Any] = []
    rewritten = False
    for raw in items:
        if (
            not rewritten
            and isinstance(raw, dict)
            and raw.get("item_type") == "SystemMessageItem"
        ):
            parts = raw.get("parts")
            if isinstance(parts, list):
                new_parts: List[Any] = []
                inner_changed = False
                for part in parts:
                    if (
                        not inner_changed
                        and isinstance(part, dict)
                        and part.get("type") == "text"
                        and isinstance(part.get("text"), str)
                    ):
                        new_text = rewriter(part.get("text", ""))
                        if new_text is None:
                            return None  # 锚点未命中：整体放弃，调用方回退
                        new_parts.append({**part, "text": new_text})
                        inner_changed = True
                    else:
                        new_parts.append(part)
                if inner_changed:
                    out.append({**raw, "parts": new_parts})
                    rewritten = True
                    continue
        out.append(raw)
    if not rewritten:
        return None
    return out
