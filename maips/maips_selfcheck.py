# maips 方法自检脚本 —— 覆盖 AGENT.md 第 4/5 节列出的 ctx 能力
#
# ⚠️ 门控：仅在 debug 模式开启（/mps debug true）时响应；debug 关闭时所有
# 触发词与事件日志全部静默忽略，不影响正常聊天。
#
# debug 开启后在群里发下面的触发词，脚本执行对应方法，并把每步结果通过
# ctx.send_text 发回**当前聊天流**，方便逐项对照验收（LLM 类会真实调模型耗
# token，慎用）：
#
#   maips检查           非 LLM 全量自检（查询/字段/匹配/参数快照）
#   自检匹配             ctx.matches 各 mode + 消息字段
#   自检表情             表情识别（需消息带表情包；含 any/all 双模式）
#   自检KV               持久 KV 读写（kv_set/get/del/keys + 键名校验）
#   自检@                 @/bot 身份（bot_user_id/nickname、at_segments、
#                        at_targets、is_at_me —— 自测时请 @一下机器人）
#   自检切换             逐预设 swap/revert/current_preset + 不存在预设抛错
#   自检参数             set_swap_params / get_swap_params / SWAP_PARAM_KEYS
#   自检文本             验证 await ctx.send_text 通道
#   自检LLM              recent_context / emotion_score / llm_generate / llm_json
#   自检swap事件         提示后用 /mps swap <预设> 触发，观察日志/swap 事件
#
# 触发词可带任意前后缀；脚本按"包含"匹配。测试完可保留本文件（不影响其它
# 脚本），不需要时直接删除即可。

import json

from mps_api import on_event

# 测试预设（须已存在；/mps maisave list 确认）
_PRESETS = ["p_cheerful", "p_grumpy", "p_quiet"]
_MAIN = "__main__"


def _match_trigger(text: str, keyword: str) -> bool:
    """触发词匹配：去掉空白与标点后包含 keyword 即命中（避免把别的脚本话触发）。"""

    normalized = "".join(ch for ch in str(text or "") if ch.isalnum())
    key = "".join(ch for ch in keyword if ch.isalnum())
    return key in normalized


def _fmt(obj) -> str:
    """稳定格式化（长内容截断）。"""

    s = json.dumps(obj, ensure_ascii=False) if isinstance(obj, (dict, list)) else str(obj)
    return s if len(s) <= 200 else s[:197] + "..."


# ---------------------------------------------------------------------------
# 各自检组：async，通过 ctx.send_text 发回结果
# ---------------------------------------------------------------------------


async def _check_matches(ctx) -> list:
    rows = ["—— ctx.matches / 字段 ——"]
    rows.append(f"event={ctx.event}")
    rows.append(f"text={ctx.text[:60]!r}")
    rows.append(f"matches(contains '自检') = {ctx.matches('自检', mode='contains')}")
    rows.append(f"matches(prefix '自检') = {ctx.matches('自检', mode='prefix')}")
    rows.append(f"matches(regex '自检.*表情|自检匹配') = {ctx.matches(r'自检.*(表情|匹配)', mode='regex')}")
    rows.append(f"matches(suffix '表情') = {ctx.matches('表情', mode='suffix')}")
    rows.append(f"matches(exact 全等) = {ctx.matches(ctx.text.strip(), mode='exact')}")
    rows.append(f"matches(list-any) = {ctx.matches(['__不存在__', '自检'], mode='contains')}")
    rows.append(f"message_types={_fmt(ctx.message_types)}")
    rows.append(f"has_text={ctx.has_text} has_emoji={ctx.has_emoji} has_image={ctx.has_image}")
    rows.append(f"group_id={ctx.group_id} user_id={ctx.user_id}")
    rows.append(f"session_id={ctx.session_id} scope_key={ctx.scope_key}")
    rows.append(f"hour={ctx.hour} minute={ctx.minute} now={ctx.now}")
    return rows


async def _check_emoji(ctx) -> list:
    rows = ["—— 表情识别 ——"]
    rows.append(f"has_emoji={ctx.has_emoji}")
    if not ctx.has_emoji:
        rows.append("本条无表情包：请发一条带表情包的消息再试（如委屈/开心/无语表情）")
        return rows
    emos = ctx.emoji_emotions()
    rows.append(f"emoji_emotions={_fmt(emos)}")
    if not emos:
        rows.append("解析不到标签（可能表情包未入库且 data 无描述）")
        return rows
    first = emos[0]
    rows.append(f"match_emoji any [{first}] = {ctx.match_emoji([first], mode='any')}")
    rows.append(f"match_emoji any ['__不存在__',{first}] = {ctx.match_emoji(['__不存在__', first], mode='any')}")
    rows.append(f"match_emoji any ['__不存在__'] = {ctx.match_emoji(['__不存在__'], mode='any')}")
    rows.append(f"match_emoji all {_fmt(emos)} = {ctx.match_emoji(emos, mode='all')}")
    rows.append(f"match_emoji all [{first},'__不存在__'] = {ctx.match_emoji([first, '__不存在__'], mode='all')}")
    return rows


async def _check_swap(ctx) -> list:
    rows = ["—— 切换操作（逐预设 swap → revert）——"]
    before = ctx.current_preset()
    rows.append(f"current_preset(前)={before}")
    for name in _PRESETS:
        try:
            ctx.swap(name)  # 时长取预设文件（这些预设 0=永久）
            rows.append(f"swap {name} → current_preset={ctx.current_preset()}")
        except Exception as exc:
            rows.append(f"swap {name} 出错：{type(exc).__name__}: {exc}")
        try:
            ctx.revert()
            rows.append(f"revert → current_preset={ctx.current_preset()}")
        except Exception as exc:
            rows.append(f"revert 出错：{type(exc).__name__}: {exc}")
    try:
        ctx.swap("__不存在的预设__")
        rows.append("swap 不存在预设：未抛异常（异常处理失效？）")
    except ValueError as exc:
        rows.append(f"swap 不存在预设：正确抛 ValueError（{exc}）")
    except Exception as exc:
        rows.append(f"swap 不存在预设：抛 {type(exc).__name__}（应 ValueError）")
    if ctx.current_preset() is not None:
        ctx.revert()  # 恢复现场
    rows.append(f"current_preset(后)={ctx.current_preset()}")
    return rows


async def _check_params(ctx) -> list:
    rows = ["—— 接管参数 ——"]
    try:
        snap = ctx.get_swap_params()
        rows.append(f"get_swap_params 键={sorted(snap.keys())}")
        rows.append(f"  probability={snap.get('probability')} per_stream={snap.get('per_stream')}")
        rows.append(f"  preset_weights={snap.get('preset_weights')} main_weight={snap.get('main_weight')}")
    except Exception as exc:
        rows.append(f"get_swap_params 出错：{type(exc).__name__}: {exc}")
    try:
        ctx.set_swap_params(probability=0.5, preset_weights={name: 1.0 for name in _PRESETS}, main_weight=1.0)
        snap2 = ctx.get_swap_params()
        rows.append(f"set 后 probability={snap2.get('probability')}")
        rows.append(f"set 后 preset_weights={_fmt(snap2.get('preset_weights'))}")
        rows.append("set_swap_params(probability=0.5, preset_weights=各1, main_weight=1) 生效")
    except Exception as exc:
        rows.append(f"set_swap_params 出错：{type(exc).__name__}: {exc}")
    import mps_api

    rows.append(f"mps_api.SWAP_PARAM_KEYS={_fmt(mps_api.SWAP_PARAM_KEYS)}")
    return rows


async def _check_llm(ctx) -> list:
    rows = ["—— LLM / 上下文 ——"]
    try:
        recent = await ctx.recent_context(5)
        rows.append(f"recent_context(5) 长度={len(recent or '')} 预览={(recent or '')[:50]!r}")
    except Exception as exc:
        rows.append(f"recent_context 出错：{type(exc).__name__}: {exc}")
    try:
        score = await ctx.emotion_score(recent_n=5, task="utils", temperature=0.2, max_tokens=16)
        rows.append(f"emotion_score(utils)={score}")
    except Exception as exc:
        rows.append(f"emotion_score 出错：{type(exc).__name__}: {exc}")
    try:
        resp = await ctx.llm_generate("只回复：正常", task="utils", temperature=0.2, max_tokens=16)
        rows.append(f"llm_generate='{resp}'")
    except Exception as exc:
        rows.append(f"llm_generate 出错：{type(exc).__name__}: {exc}")
    try:
        data = await ctx.llm_json('只输出 JSON {"ok": true, "n": 1}', task="utils", temperature=0.2, max_tokens=64)
        rows.append(f"llm_json={_fmt(data)}")
    except Exception as exc:
        rows.append(f"llm_json 出错：{type(exc).__name__}: {exc}")
    return rows


async def _check_kv(ctx) -> list:
    rows = ["—— 持久 KV ——"]
    key = "maips_selfcheck_probe"
    try:
        ctx.kv_del(key)
        rows.append(f"kv_get(未写入)={ctx.kv_get(key, '缺省')}")
        ok = ctx.kv_set(key, 7)
        rows.append(f"kv_set({key}, 7)={ok}")
        rows.append(f"kv_get(读回)={ctx.kv_get(key)}")
        rows.append(f"kv_keys 含 {key}={key in ctx.kv_keys()}")
        rows.append(f"kv_del={ctx.kv_del(key)}")
        rows.append(f"kv_get(删除后)={ctx.kv_get(key, '缺省')}")
        rows.append(f"kv_set(非法键 '../x')={ctx.kv_set('../x', 1)}")
        rows.append(f"kv_set(不可序列化)={ctx.kv_set('bad', object())}")
        rows.append("说明：KV 落盘 personality/script_kv.json，热重载/重启保留")
    except Exception as exc:
        rows.append(f"KV 出错：{type(exc).__name__}: {exc}")
    return rows


async def _check_at(ctx) -> list:
    rows = ["—— @ / bot 身份 ——"]
    rows.append(f"bot_user_id={ctx.bot_user_id!r} bot_nickname={ctx.bot_nickname!r}")
    rows.append(f"user_id={ctx.user_id!r}（本消息发送者）")
    rows.append(f"at_segments={_fmt(ctx.at_segments)}")
    rows.append(f"at_targets={_fmt(ctx.at_targets())}")
    rows.append(f"is_at_me={ctx.is_at_me}（本消息是否 @ 了 bot）")
    rows.append("自测方法：本条消息 @一下机器人 → is_at_me 应变 True；不 @ → False")
    rows.append("bot_message 事件里 bot @ 别人时 at_targets() 会列出被 @ 对象")
    return rows


# ---------------------------------------------------------------------------
# 分发
# ---------------------------------------------------------------------------


@on_event("message")
async def maips_selfcheck(ctx):
    if ctx.is_command or ctx.is_notify:
        return
    # debug 未开启：自检功能全部静默忽略（不响应、不发送、不调用 LLM）
    if not ctx.debug_enabled:
        return
    text = ctx.text or ""
    keyword = None
    runner = None
    if "自检表情" in text:
        keyword, runner = "自检表情", _check_emoji
    elif "自检切换" in text:
        keyword, runner = "自检切换", _check_swap
    elif "自检参数" in text:
        keyword, runner = "自检参数", _check_params
    elif "自检匹配" in text:
        keyword, runner = "自检匹配", _check_matches
    elif "自检KV" in text or "自检kv" in text:
        keyword, runner = "自检KV", _check_kv
    elif "自检@" in text or "自检at" in text or "自检AT" in text:
        keyword, runner = "自检@", _check_at
    elif "自检LLM" in text or "自检llm" in text:
        keyword, runner = "自检LLM", _check_llm
    elif "自检文本" in text:
        await ctx.send_text("[maips自检] await ctx.send_text 通道正常，你收到了这条消息。")
        return
    elif "自检swap事件" in text:
        await ctx.send_text(
            "[maips自检] swap 事件监听已就绪。请接着发：/mps swap p_cheerful\n"
            "（或 /mps swap 切回主人格），观察日志中的『swap 事件收到』记录，"
            "确认 swap 事件能收到 scope_key/preset/source。"
        )
        return
    elif "maips检查" in text or "全量自检" in text:
        keyword, runner = "全量自检", _run_all_basic
    if runner is None:
        return
    rows = await runner(ctx)
    body = f"【maips 自检·{keyword}】\n" + "\n".join(f"  {r}" for r in rows)
    try:
        await ctx.send_text(body)
    except Exception as exc:
        ctx.log(f"[自检] 发送结果失败：{exc}")
        for r in rows:
            ctx.log(f"[自检] {r}")


async def _run_all_basic(ctx) -> list:
    rows = []
    rows.append(f"current_preset={ctx.current_preset()}")
    rows.append(f"matches(contains 'maips')={ctx.matches('maips', mode='contains')}")
    rows.append(f"message_types={_fmt(ctx.message_types)}")
    rows.append(f"has_text={ctx.has_text} has_emoji={ctx.has_emoji}")
    rows.append(f"group_id={ctx.group_id} user_id={ctx.user_id}")
    rows.append(f"session_id={ctx.session_id} scope_key={ctx.scope_key}")
    rows.append(f"event={ctx.event} hour={ctx.hour}")
    rows.append(f"get_swap_params 键={sorted(ctx.get_swap_params().keys())}")
    rows.append("更多单项：自检匹配 / 自检表情(带表情包) / 自检切换 / 自检参数 / 自检LLM")
    return rows


@on_event("load")
def selfcheck_setup(ctx):
    if not ctx.debug_enabled:
        return
    ctx.log("[maips自检] load 事件触发正常（脚本加载/重载成功）")


@on_event("swap")
def selfcheck_swap_event(ctx):
    if not ctx.debug_enabled:
        return
    preset = ctx.preset or "主人格"
    ctx.log(
        f"[maips自检] swap 事件收到：scope={ctx.scope_key} → {preset} "
        f"来源={ctx.source} 时长={ctx.duration_minutes}"
    )
