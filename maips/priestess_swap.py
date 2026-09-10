# -*- coding: utf-8 -*-
"""夺舍麦麦 - 普瑞赛斯攒点人格脚本

机制总览
--------
- 各处“检查”独立判过：每通过一个检查就累计对应的点数，攒到 20 点一次性清空，
  并把聊天流人格切换成普瑞赛斯 1~5 分钟（+ 挂起的延长分钟数）。
- 点数按聊天流（scope_key）隔离累计（timer/全局等无流场景用 "main" 兜底），
  全部经 ctx.kv_* 落盘，热重载 / 重启不丢。
- “下一次切换的额外延长（可叠加）”：@机器人+普瑞赛斯 命中时往该流的挂起
  bonus 池叠加 3~5 分钟（主时段）/ 1~3 分钟（非主时段）；下一次该流切到
  普瑞赛斯时一并消耗。若该次 @ 恰好把点数顶到 20，延长并入紧接的这次切换。

主要触发时段（主时段）：22:00 - 次日 01:00（hour >= 22 or hour < 1）
其余时间 = 非主时段。

主时段内每类点数来源（每条消息独立抽签，可多项同时命中）：
- 用户消息含“普瑞赛斯”（且非 @bot 普瑞赛斯）：20% +2
- 用户发开心表情：5% +1
- bot 发开心表情：10% +1
- bot 消息含“罗德岛”：10% +2
- bot 或用户消息含“源石”：5% +1
- 用户 @bot 且消息含“普瑞赛斯”：必得 +10，且下一次普瑞赛斯切换额外 +3~5 分钟
  （可叠加）。注意：本项与“含普瑞赛斯 20% +2”互斥，@ 命中时只取本项
  （保证文档示例 11 点 → @bot 后恰好 21 点）。

非主时段内：
- 用户 @bot 且消息含“普瑞赛斯”：必得 +1，且下一次普瑞赛斯切换额外 +1~3 分钟
  （可叠加）。

达标切换（点数 >= 20 的瞬间）：
- 恰好 == 20：正常替换“触发的聊天流”scope（1~5 分钟 + bonus）。
- 一瞬间超过 20（例如 11 点时从 19 @bot 直接 +10 到 29）：
  - 20% 概率改为替换**全局人格**（scope="global"，影响所有流）；
  - 否则照常替换触发的聊天流。

可调参数在文件顶部 _ 开头的常量里；改完保存即热重载（/mps script list 看状态）。
注意：脚本只负责“切到普瑞赛斯”，到点自动恢复主人格由插件自身完成；
预设名要与 /mps maisave list 实际一致（预设交付到插件根 preset/ 后随插件加载
自动导入数据目录，文件名为“普瑞赛斯.toml”）。
"""

import random

from mps_api import on_event

PRESET = "普瑞赛斯"          # 目标预设名（与 preset/普瑞赛斯.toml 一致）
THRESHOLD = 20              # 攒满即切
_BASE_MIN, _BASE_MAX = 1, 5  # 每次触发的基础时长（分钟）

# 主时段：22:00 - 01:00
MAIN_WINDOW_START_HOUR = 22

# “开心表情包”的情绪标签集（MaiBot 表情库标签，可自行增删）
HAPPY_EMOTIONS = ["开心", "高兴", "快乐", "喜悦", "愉快"]

# KV 键前缀（全脚本共享一张 KV 表，带前缀避免冲突；后缀 _<scope> 按流隔离）
_KV_POINTS = "priestess_points"   # 当前点数
_KV_BONUS = "priestess_bonus"     # 下一次切换的挂起延长分钟数（可叠加）


def _main_window(ctx) -> bool:
    """是否处于主要触发时段（22:00~次日 01:00，跨午夜）。"""
    return ctx.hour >= MAIN_WINDOW_START_HOUR or ctx.hour < 1


def _scope(ctx) -> str:
    """事件所属聊天流的 KV/切换 scope；无 scope_key（如 timer）兜底 main。"""
    return ctx.scope_key or "main"


def _points_key(scope: str) -> str:
    return f"{_KV_POINTS}_{scope}"


def _bonus_key(scope: str) -> str:
    return f"{_KV_BONUS}_{scope}"


def _stack_bonus(ctx, scope: str, minutes: int) -> None:
    """向该流挂起 bonus 池叠加分钟（下一次切普瑞赛斯时消耗，可叠加）。"""
    key = _bonus_key(scope)
    ctx.kv_set(key, int(ctx.kv_get(key, 0) or 0) + minutes)


def _add_and_check(ctx, scope: str, gained: int, desc: str) -> None:
    """加点数并记日志；若因此跨过阈值则触发切换。"""
    key = _points_key(scope)
    cur = int(ctx.kv_get(key, 0) or 0)
    new = cur + gained
    ctx.kv_set(key, new)
    ctx.log(f"[普瑞赛斯] {desc} +{gained} 点 → {new}（scope={scope}）")
    if new >= THRESHOLD:
        _do_flip(ctx, scope, overshoot=(new > THRESHOLD))


def _do_flip(ctx, scope: str, overshoot: bool) -> None:
    """达标切换：先清零点数与挂起 bonus，再切普瑞赛斯 1~5(+bonus) 分钟。

    overshoot=True（一瞬间超过 20）且处于主时段时，以 20% 概率改切全局人格，
    否则正常替换触发事件所在的聊天流。
    """
    pkey, bkey = _points_key(scope), _bonus_key(scope)
    bonus = int(ctx.kv_get(bkey, 0) or 0)
    ctx.kv_set(pkey, 0)      # 先清零再切，防重复触发
    ctx.kv_set(bkey, 0)
    minutes = max(1, random.randint(_BASE_MIN, _BASE_MAX) + bonus)

    target = scope
    if overshoot and _main_window(ctx) and random.random() < 0.2:
        target = "global"
        ctx.log(f"[普瑞赛斯] 点数一瞬间超过 {THRESHOLD}，20% 全局人格替换 → "
                f"scope=global，持续 {minutes} 分钟（含 bonus {bonus}）")
    else:
        ctx.log(f"[普瑞赛斯] 点数达到 {THRESHOLD}，替换聊天流 {scope}，"
                f"持续 {minutes} 分钟（含 bonus {bonus}）")
    try:
        ctx.swap(PRESET, duration_minutes=minutes, scope=target)
    except Exception as exc:
        ctx.log(f"[普瑞赛斯] 切换失败（预设「{PRESET}」是否存在？）：{exc}")


@on_event("message")
def priestess_user_points(ctx):
    """用户消息：主时段按检查攒点 + @bot 普瑞赛斯重击（含 bonus）+ 达标切换。"""
    if ctx.is_command or ctx.is_notify:
        return
    # 排除 bot 自己 @ 自己造成的假“用户”消息（bot 自发言走 bot_message 事件）
    if ctx.bot_user_id and ctx.user_id == ctx.bot_user_id:
        return

    scope = _scope(ctx)
    in_main = _main_window(ctx)
    text_has_priestess = ctx.matches("普瑞赛斯", mode="contains")
    at_me = bool(ctx.is_at_me)

    # --- @bot 且含“普瑞赛斯”的重击（两种时段规则互斥，只在主时段与
    #     “提及普瑞赛斯 20%”二选一：@ 命中时按重击计，保证一次恰 +10）---
    if at_me and text_has_priestess:
        if in_main:
            _stack_bonus(ctx, scope, random.randint(3, 5))
            _add_and_check(ctx, scope, 10, "@bot+普瑞赛斯（主时段）")
        else:
            _stack_bonus(ctx, scope, random.randint(1, 3))
            _add_and_check(ctx, scope, 1, "@bot+普瑞赛斯（非主时段）")
        return  # 本条消息不再计入其它“普瑞赛斯”词检查

    if not in_main:
        return  # 非主时段只有上面的 @bot 规则

    # --- 主时段各检查（彼此独立，可同时命中多项）---
    if text_has_priestess and random.random() < 0.2:
        _add_and_check(ctx, scope, 2, "用户提及普瑞赛斯")
    if ctx.has_emoji and ctx.match_emoji(HAPPY_EMOTIONS, mode="any") and random.random() < 0.05:
        _add_and_check(ctx, scope, 1, "用户发开心表情")
    if ctx.matches("源石", mode="contains") and random.random() < 0.05:
        _add_and_check(ctx, scope, 1, "用户提及源石")


@on_event("bot_message")
def priestess_bot_points(ctx):
    """bot 自己发的消息：主时段内开心表情 / 罗德岛 / 源石各检查独立抽签。"""
    if not _main_window(ctx):
        return
    if ctx.has_emoji and ctx.match_emoji(HAPPY_EMOTIONS, mode="any") and random.random() < 0.1:
        _add_and_check(ctx, _scope(ctx), 1, "bot 发开心表情")
    if ctx.matches("罗德岛", mode="contains") and random.random() < 0.1:
        _add_and_check(ctx, _scope(ctx), 2, "bot 提及罗德岛")
    if ctx.matches("源石", mode="contains") and random.random() < 0.05:
        _add_and_check(ctx, _scope(ctx), 1, "bot 提及源石")
