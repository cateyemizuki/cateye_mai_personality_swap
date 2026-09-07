# 夺舍麦麦 - 自定义脚本示例（KubeJS 风格）
#
# 本目录（插件目录下的 maips/）下的每个 .py 文件都会被加载，可以放任意多个
# 自编写脚本；修改保存后自动热重载（或用 /mps script reload 手动重载，
# /mps script list 查看状态）。下划线开头的文件（如 _utils.py）视为工具库，
# 不作为脚本加载，可被其他脚本 import。
# 把下面示例的注释去掉即可生效。完整 API 见插件 开发文档.md 的"脚本系统"章节。
#
# 事件：message（用户消息）/ bot_message（bot发出的消息）/ timer（定时）/
#      swap（发生切换后）/ load（脚本加载）
# 提示：脚本与 bot 本体同等信任级别，只运行自己编写或审查过的脚本。

from mps_api import on_event


# ---------------------------------------------------------------------------
# 按聊天流路由人格（非注释示例，开箱即用）
#
# 说明：给两个不同群与一个指定私聊分别固定人格——群成员/私聊在该流说话时
#       自动切到对应预设（若当前不是该预设）。预设名要真实存在
#       （/mps maisave list 查看），否则会被 try/except 静默跳过并记日志。
#       群号/QQ 号按实际替换；数字以字符串形式比较（ctx.group_id 是 str）。
#       ⚠️ 下方目前是**空表**（不路由任何流），请把"群号/QQ号"替换成你自己的：
#          群号可用 /mps status 或日志里的 group_id 查看；私聊 QQ 号同理。
# ---------------------------------------------------------------------------
_ROUTE_GROUPS = {  # 群号(字符串) → 预设名（示例：把某群路由到 p_cheerful）
    # "你的群号1": "p_cheerful",
    # "你的群号2": "p_grumpy",
}
_ROUTE_PRIVATE = {  # 私聊 QQ 号(字符串) → 预设名（示例：把某私聊路由到 p_quiet）
    # "你的QQ号": "p_quiet",
}


@on_event("message")
def route_persona_by_stream(ctx):
    if ctx.is_command or ctx.is_notify:
        return
    target = None
    if ctx.group_id:
        target = _ROUTE_GROUPS.get(ctx.group_id)
    elif ctx.user_id and ctx.group_id == "":
        # 没有群号 = 私聊流（QQ 号即 user_id）
        target = _ROUTE_PRIVATE.get(ctx.user_id)
    if target is None:
        return
    try:
        if ctx.current_preset() != target:
            ctx.swap(target, duration_minutes=0)
    except Exception as exc:
        ctx.log(f"[示例-路由] 切到预设「{target}」失败：{exc}")


# ---------------------------------------------------------------------------
# 以下为注释示例，去掉 @on_event 前的 # 即可启用（完整 API 见根目录文档）
# ---------------------------------------------------------------------------# 示例 0：接管模式参数设定——配置页开启「脚本接管」后，下面的参数取代配置项；
# 未开启时本调用无效（此时各项行为在 WebUI 配置页设置）
# @on_event("load")
# def setup(ctx):
#     ctx.set_swap_params(
#         probability=0.1,                                # 自动替换概率
#         non_bot_keywords=["开会"],                      # 用户消息关键词条件
#         time_windows=["09:00-18:00"],                   # 时段条件
#         preset_weights={"preset1": 0.5},                # 预设权重
#         main_weight=0.8,                                # 主人格权重
#     )

# 示例 1：条件脚本——包含"安静"的消息会把本轮自动替换否决掉（返回 False）
# @on_event("message")
# def quiet_veto(ctx):
#     if "安静" in ctx.text:
#         ctx.veto("群里在安静聊天")
#         return False

# 示例 2：命中关键词直接切换（跳过概率与权重，立即生效）
# @on_event("message")
# def work_mode(ctx):
#     if "进入工作模式" in ctx.text:
#         ctx.swap("preset1", duration_minutes=120)

# 示例 3：调整本轮概率与权重（不否决，只加权）
# @on_event("message")
# def boost_stage(ctx):
#     if "来表演" in ctx.text:
#         ctx.set_probability(1.0)
#         ctx.set_weight("preset1", 10.0)

# 示例 4：定时切换——每天 23 点后把全局人格换成预设；current_preset 判重防止反复切换
# @on_event("timer")
# def nightly(ctx):
#     if ctx.hour >= 23 and ctx.current_preset(scope="global") != "sleep_mode":
#         ctx.swap("sleep_mode", scope="global")

# 示例 5：切换发生后在当前聊天流播报（send_text 是协程，需要 async 处理器）
# @on_event("swap")
# async def announce(ctx):
#     name = ctx.preset or "主人格"
#     await ctx.send_text(f"人格已切换：{name}")

# 示例 6：表情包情绪切换——消息里的表情包带"开心"情绪时换人格
#（bot 自己发的表情包在 bot_message 事件里同样可匹配）
# @on_event("message")
# def happy_emoji(ctx):
#     if ctx.match_emoji("开心"):
#         ctx.swap("preset1", duration_minutes=30)
#
# @on_event("bot_message")
# def bot_sad_emoji(ctx):
#     if ctx.match_emoji("委屈"):
#         ctx.swap("preset2", duration_minutes=15)

# 示例 7：每 20 条消息让 LLM 评估一次情绪分（可指定 replyer/planner 等任务、
# 温度与最大 token），按分数切人格
# _count = {"n": 0}
# @on_event("message")
# async def mood_check(ctx):
#     _count["n"] += 1
#     if _count["n"] % 20:
#         return
#     score = await ctx.emotion_score(recent_n=20, task="replyer", temperature=0.3, max_tokens=16)
#     if score is None:
#         return
#     if score >= 7:
#         ctx.swap("preset1", duration_minutes=60)
#     elif score <= 3:
#         ctx.revert()

# 示例 8：LLM 返回结构化参数 → 脚本校验后应用（评分只是用法之一）
# import mps_api
# @on_event("message")
# async def llm_director(ctx):
#     context = await ctx.recent_context(15)
#     data = await ctx.llm_json(
#         "根据聊天氛围决定人格切换。输出 JSON："
#         '{"preset": "preset1|preset2|main", "duration": 分钟数, '
#         '"params": {"probability": 0~1}}。聊天记录：' + context,
#         task="planner", temperature=0.2, max_tokens=120,
#     )
#     if not isinstance(data, dict):
#         return
#     params = data.get("params")
#     if isinstance(params, dict):
#         ctx.set_swap_params(**{k: v for k, v in params.items() if k in mps_api.SWAP_PARAM_KEYS})
#     preset = str(data.get("preset") or "")
#     if preset == "main":
#         ctx.revert()
#     elif preset:
#         ctx.swap(preset, duration_minutes=data.get("duration"))

# 示例 9：跨事件累计点数 + @bot 判定（v1.3.7 起）
# —— 持久 KV：脚本热重载会重建模块（模块级变量清零），跨事件累计请用
#    ctx.kv_set/kv_get 落盘（存 personality/script_kv.json，重启保留）。
# —— @ 判定：ctx.is_at_me（是否 @ 了 bot）+ ctx.user_id != ctx.bot_user_id
#    （排除 bot 自 @）；bot @ 了谁用 ctx.at_targets()。
# import random
# _KEY = "example_points"
# @on_event("message")
# def count_points(ctx):
#     if ctx.is_command or ctx.is_notify:
#         return
#     if ctx.is_at_me and ctx.user_id != ctx.bot_user_id:  # 别人 @ 了 bot
#         n = ctx.kv_get(_KEY, 0) + 1
#         ctx.kv_set(_KEY, n)
#         ctx.log(f"被 @ +1 点 → {n}")
# @on_event("message")
# def flip_when_full(ctx):
#     if ctx.kv_get(_KEY, 0) >= 10:
#         ctx.kv_set(_KEY, 0)
#         ctx.swap("preset1", duration_minutes=random.randint(1, 3))

