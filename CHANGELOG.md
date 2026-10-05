# 更新日志

## 1.4.7

> 2026-09-29 安全与规范加固（市场 #680 审核中提交，本次改动涉及 manifest
> description 与新增配置字段，**需要审核侧 recheck**）。配置版本
> `1.4.6 → 1.4.7`（只增元数据，旧配置自动兼容）。

### 安全：自检脚本默认不加载 + 触发需管理员（防止任意成员触发人格切换/LLM 消耗）

随包的 `maips/maips_selfcheck.py` 改名为 `maips/_maips_selfcheck.py`：下划线
前缀脚本**默认不作为脚本加载**（只作工具库），需自检时在 WebUI 配置页开启新增
的「加载下划线前缀脚本」（`[script].load_underscore_scripts`，默认 `false`）
并重载插件。同时脚本触发路径加双重门控：debug 开启（原有）**且**消息发送者
命中管理员名单（`ctx.caller_is_admin`，复用 `admin_user_ids` / `admin_group_ids`
口径）——此前 debug 开启期间，任意群成员发「自检切换」「自检LLM」等触发词即可
让脚本连续 `ctx.swap`/`ctx.revert` 翻动人格、真实调用 LLM 消耗 token。自检完成
后建议关闭该配置与 debug（用完即关）。

### 修复：`/mps weight <名> 0` 清除路径不再无校验写盘

权重为 0（清除命令级覆盖）的路径此前直接 `save_weight_override(name, 0)`，任意
超长/特殊字符串都能进 `weights.json` 键。现与正权重路径一致：先过预设名白名单
（`validate_preset_name`），且预设存在才写，否则回「名称非法/预设不存在」提示。

### 收紧：黑白名单覆盖脚本事件派发与脚本 `ctx.send_text`

`[filter]` 黑白名单此前只约束内置自动抽取与脚本 `ctx.swap`/`ctx.revert`；被拉黑
聊天流的消息仍会派发给全部脚本 handler，脚本也可向被拉黑聊天流 `send_text`。现
在：被拉黑流的入站/出站消息**不再派发给任何脚本**（对脚本完全不可见），脚本
`ctx.send_text` 的目标流被拉黑时静默丢弃。与文档「黑白名单（接管时仍生效）」
口径完全对齐。

### 收紧：persona_name 外壳替换不再全文替换 system

配置 `persona_name` 的预设激活时，planner system 里官方昵称的人格化替换此前是
对**整段 system** 做整词替换——若宿主模板在 system 其它段落（安全/合规指令等）
也写到官方昵称会被一并改写。现收窄为只替换 `injectors/system_replace.py` 中
`PLANNER_SHELL_SENTENCES` 列出的官方模板已知固定句式（行为段 marker 行、"关注
…与用户的对话…"、"你不是…本人…"、"帮…搜集信息"、"当你判断…应该正式发言时
调用 reply"、行为段收尾句）；句式未命中（宿主模板改版）该处保持官方昵称，
行为段替换不受影响。

### 修复：官方临时风格剔除改锚定边界匹配，不再误删用户消息

`is_official_temp_style_text` 此前用 `in` 子串判定——预设激活期间，任何包含
「你的说话风格可以尝试」的用户消息（引用、复读、测试官方文案）会被整条从请求
上下文剔除。现改为双条件：strip 后以固定前缀**开头** 且整条长度 ≤200 字符
（官方消息远短于该上限），用户引用/复读不再命中。

### 性能：BLOCKING hook 内同步 IO 移出事件循环 + stream 映射刷新节流

- BLOCKING hook（`before_model_request` / planner `before_request`）内的预设
  TOML 解析移入 `asyncio.to_thread`，不再阻塞事件循环（自动抽取路径的预设读取
  同步处理）；
- stream→群号/QQ号 映射的全量刷新（`chat.get_all_streams`）加 5 秒节流——缓存
  miss 时（典型：公开命令 `/mps status` 在流未入缓存时）不会每次都全量拉取宿主
  接口，普通成员无法反复刷该调用。

### 规范：manifest 瘦身与配置热更新、鉴权细节

- `_manifest.json` description 精简为一段功能概述（版本史全部留在 CHANGELOG），
  新增 `"changelog": "CHANGELOG.md"` 字段；**本次 manifest 改动需 recheck**；
- 管理员名单 `platform:user` 形态匹配两侧统一 strip+lower（`QQ:123` 与 `qq:123`
  等价，此前大写 platform 写法匹配失败）；
- 管理员判定统一（cateye 系 2026-09-29 二轮）：生效管理员 = 宿主管理员
  （`ctx.config.get("plugin.permission")`）∪ 插件配置 `admin_user_ids`，经
  `admin_util.py`（随包分发的统一模块）纯 ID 归一去重（带 `qq:` 前缀与裸号视为
  同一人）；宿主名单读取失败降级为仅插件配置（debug 日志）。本地 operator/控制台
  放行、`admin_group_ids`（群内任何人可用）语义与"无任何名单时仅本地 operator
  可用"的 fail-closed 行为均保持不变；脚本 `ctx.caller_is_admin` 同口径
  （宿主侧经缓存，on_load / 命令判定 / timer tick 刷新）；
- `on_config_update` 改经新增的 `PresetStore.set_backup_limit()` 公开方法更新
  备份上限，不再跨对象直改私有字段。

## 1.4.6

> 1.3.0 兼容自查修复，无功能语义变化。配置版本 `1.4.5 → 1.4.6`（只增元数据，
> 旧配置自动兼容）；manifest 兼容区间按策略A放宽，继续同时兼容 1.2.x 与 1.3.0。

### 修复：脚本 LLM 调用改为显式 task_name 任务路由（规避 1.2.x 与 1.3.0 的路由差异）

`script_host.py` 的 `llm_generate`（脚本的 `ctx.llm_generate` / `ctx.emotion_score` /
`ctx.llm_json` 都走这里）原来写的是 `llm.generate(prompt, model=任务名)`——
「只传 `model="任务名"`」的写法在 1.3.0（SDK 2.8.2）能命中任务名兼容分支，但 1.2.x
（SDK 2.8.1）会无条件强制发送 `task_name="utils"`，宿主把 `replyer` 等任务名当具体
模型名查找，报「未找到模型」。现改为显式 `task_name=` 传任务（不传 `model`），按
开发文档 §8.3 的正确写法，1.2.x 与 1.3.0 行为一致。

### 修复：配置模型补齐 WebUI 英文翻译

按 v1.3.0 开发文档 §5.1 的强制要求：全部 23 个配置字段的 `json_schema_extra` 补上
`i18n`（至少 `en` 的 label/hint），8 个配置分组补上 `__ui_i18n__`（英文 title/
description）。此前英文界面会把英文字段名直接当标题展示。

### manifest 调整

- `host_application.min_version`：`1.2.0 → 1.0.0`（策略A：min 是硬下限，按规范写
  `1.0.0`；实际并未使用任何 1.2.0 独有 API，放宽后 1.0.x/1.1.x 宿主也可尝试加载）；
- `sdk.min_version` 维持 `2.0.0`（未照抄 2.8.2；插件实际用到的 ctx 代理均为早期
  SDK 即有能力，显式 `task_name` 经 `**kwargs` 透传在旧 SDK 同样支持）；
- `max_version` 维持 `1.99.99` / `2.99.99`，覆盖 1.3.0。

## 1.4.5

> 修复一处**只在私聊暴露**的判空缺陷。功能语义不变，配置版本 `1.4.4 → 1.4.5`
> （旧配置自动兼容）。

### 修复：私聊时入站观察 hook 每轮抛 AttributeError，导致私聊内自动替换评估等全部失效

**现象**：机器人每收到一条私聊消息，日志就出现一条

```
观察型 HookHandler github.cateye.mai-personality-swap.mps_receive_observer 执行失败: 'NoneType' object has no attribute 'get'
  File ".../cateye_mai_personality_swap/plugin.py", line 1028, in on_receive_after_process
    group_id=str(group_info.get("group_id") or ""),
AttributeError: 'NoneType' object has no attribute 'get'
```

**根因**：`on_receive_after_process` 里 `group_info` 的判空守卫**写错了变量**：

```python
message_info = message.get("message_info") if isinstance(message.get("message_info"), dict) else {}
group_info = message_info.get("group_info") if isinstance(message_info, dict) else {}   # ← 判错变量
```

守卫的意图是"`group_info` 不是 dict 就用 `{}` 兜底"，但判的是 `message_info`——
而上一行已保证它必然是 dict，因此**守卫恒为真**，`None` 被原样透传，
紧接着 `group_info.get("group_id")` 崩溃。`user_info` 是同一处复制粘贴错误，
目前因宿主总会提供 `user_info` 而未暴露。

**为什么只在私聊出现**：宿主对私聊给的 `group_info` **就是 `None`**
（`plugin_runtime/host/message_utils.py:398-407`：`group_info_dict: Optional[...] = None`，
仅当 `message_info.group_info` 存在时才填充；`chat/message_receive/bot.py:699` 宿主
自己也写 `if ... .get("group_info") is not None`）。群聊时 `group_info` 是 dict，
所以群聊路径完全正常——**该缺陷一直存在，直到机器人开始处理私聊才被触发**。

**影响**：该 handler 是 `mode=OBSERVE` + `order=LATE`，异常被宿主 hook 派发器捕获，
**不中断主流程**（机器人照常回消息），但后果是：
1. 每条私聊刷一条 `error` 日志；
2. **该 hook 的后续逻辑全部被跳过** —— 私聊场景下的
   「stream→群号映射记录、脚本 `message` 事件派发、非 bot 关键词命中收集、
   自动替换评估」**全部不生效**，即**私聊里的人格自动替换实际是失效的**。

**修法**：守卫改判取出来的值本身：

```python
group_info = message_info.get("group_info") if isinstance(message_info.get("group_info"), dict) else {}
user_info = message_info.get("user_info") if isinstance(message_info.get("user_info"), dict) else {}
```

**验证**：用 AST 从源文件原样抽出该方法的赋值语句，喂两种真实形态载荷执行崩溃点表达式：
- 私聊（`group_info=None`）→ 兜底为 `{}`，`group_id=""`，不再抛异常；
- 群聊（`group_info={"group_id": …}`）→ 原样保留，`group_id` 正确；
- 边界（`message_info` 缺失 / 为 `None` / 为字符串、`group_info` 为字符串）→ 均不崩且兜底为空。

### 已知遗留（未在本次修改）

同一文件另有 2 处相同的错误守卫，位于 `_note_stream_from_message`（L688-689）与
`_stream_allowed`（L714-715）。它们**不崩**，因为紧随其后还有第二道正确守卫
（`str(group_info.get("group_id") or "") if isinstance(group_info, dict) else ""`）兜住。
属于同一模式，建议后续统一，但不影响功能。

## 1.4.4

> 提交插件中心前的自查修复：1 处真实缺陷、若干健壮性与隐私收敛、1 处声明与
> 实现不一致。功能语义不变，配置版本 `1.4.3 → 1.4.4`（旧配置自动兼容）。

### 修复：非有限权重导致自动抽取每轮抛异常

- `/mps weight <预设> inf`（或 `nan` / `1e400`）此前能通过校验并持久化；配置页
  「预设权重表」手打 `1e400` 同样会解析成 `inf`。这类权重进入
  `random.choices` 会抛 `ValueError: Total of weights must be finite`，而
  `_maybe_auto_swap` 全程无异常保护——异常冒泡到宿主 hook 派发器，**该聊天流的
  自动替换评估每轮都失败**。
- 修复：`swap_engine` 新增 `_finite_weight()` 统一过滤非有限值
  （`parse_preset_weights` / `build_weight_pool` / `draw_persona` 三个入口全覆盖）；
  `/mps weight` 对非有限值直接回错误提示；抽取段另加 try/except 记 warning 作最后保险。
- 另修一处同源缺陷：单个权重均有限但**总和的浮点加法溢出**为 `inf`（如两条
  `1e308`）时同样会抛异常——现按最大权重归一（缩放不改变相对比例），抽取照常工作。

### 隐私收敛：`/mps status` 对非管理员不再列出其它聊天流

- `/mps status` 是公开命令，此前会列出全部处于非主人格状态的聊天流明细，而状态键
  就是群号 / QQ 号——任意群成员都能借此枚举其它群与私聊的会话标识。
- 现改为：**非管理员只显示当前聊天流的明细**，另附「另有 N 个聊天流处于非主人格
  状态」的计数；管理员（本地 operator / 配置的管理员名单）仍可见全量明细。

### 健壮性：替换式注入的健全性检查与词边界

- replyer 替换会删除锚点 A 之前的整段文本。此前只校验锚点存在与否，若宿主模板日后
  在锚点前新增段落（安全规则 / 输出格式 / 群规），会被**静默删除且不触发回退**。
  现增加健全性检查：前缀含固定句 B、或超过 4000 字符 / 60 非空行即判定结构异常 →
  放弃替换，回退到 items 尾部追加式（宁可降级，绝不误删官方内容）。
- planner 外壳人格化原为全局子串替换，官方昵称若很短（如「小」「麦」）会误改
  system 中其它无关文本。现改为整词匹配（前后不得紧邻 ASCII 字母/数字/下划线），
  且官方昵称长度 < 2 时放弃外壳替换、只替换行为段。

### 健壮性：文件写入与数据目录

- `PresetStore.save()` 改为**原子写**（同目录临时文件 + `os.replace`，临时名含
  pid 与随机后缀）：预设正文很长，原先「先截断再写」在崩溃/断电时会留下半截 TOML，
  表现为预设"消失"。
- `SwapStateStore` 状态写入同样改为唯一临时名 + 少量重试，且**失败不再抛异常**
  （新增 `on_error` 回调，插件侧记 warning）——原先固定临时名在 Windows 下并发写
  会抛 `PermissionError` 并冒泡到 hook。失败时该次切换不持久化（重启回主人格）。
- 随包预设导入从 `os.replace` / `shutil.move`（移动=删除插件安装目录内文件）改为
  `shutil.copy2`（复制、源文件保留）：不再改写插件安装目录内容。
- `_data_base()` 去掉 `Path(".")` 回退：拿不到宿主授予的数据目录时显式抛错，而不是
  把 `preset/`、`personality/` 写到 bot 进程的当前工作目录。

### 文档与注释一致性

- 模块头命令表：`/mps maisave list` 原标注为 ★（仅管理员），与实现、README、
  AGENT.md 的"只读公开"不符 → 统一为公开；`/mps status` 补注隐私行为。
- README 命令表与「最快上手」同步 `/mps status` 的新行为；AGENT.md 命令权限段
  同步，并补充 `preset_weights` 在配置侧自 v1.4.3 起为 JSON 文本、非有限值会被忽略。

## 1.4.3

### 修复：WebUI「预设权重表」显示为 `[object Object]`

配置模型里 `[weights] presets` 原本是 `Dict[str, float]`。WebUI 对对象类型字段会
渲染成对象控件，**默认值显示成 `[object Object]`**（MaiBot 开发文档《02-开发入门/
03-配置系统》§5.6 明确记录了这个坑），导致该项既看不懂、也改不了。

- 字段类型改为 **`str`（JSON 文本）**，默认 `{}`，WebUI 里是一个正常的文本框，
  并补了 `rows` / `placeholder` / `example`（例：`{"乐子人": 1.5, "普瑞赛斯": 2}`）；
- 解析新增 `swap_engine.parse_preset_weights()`，**宽松兼容三种写法**：
  ① JSON 对象（推荐）② 一行一条 `乐子人=1.5`（分隔符 `=`/`:`/`：`/空格 均可）
  ③ JSON 数组；非正权重与非法条目自动跳过；
- **旧配置不丢**：新增 `field_validator(mode="before")`，旧 `config.toml` 里遗留的
  `[weights.presets]` TOML 表会被自动序列化成 JSON 文本继续生效，下次在 WebUI
  保存即写回文本形态；
- 解析不出有效条目时在插件日志告警一次（改配置后重新计数），不再静默失效；
- `/mps status` 增列「预设权重」一行（配置页 + 命令级覆盖后的生效值），
  方便直接核对配置页里手写的 JSON 是否被正确解析。

### 变更：`/mps swap` 纳入管理员权限

切人格会改变整个聊天流的表现，属于管理操作。`swap` 已加入 `_OPERATOR_SUBS`：

- `/mps swap [名称] [时长]`（含不带名称切回主人格）**仅管理员可执行**；管理员 =
  本地 operator/控制台，或配置页「黑白名单与管理员」里的管理员 QQ / 管理员群；
- 仍**公开**的只有只读命令：`/mps status`、`/mps list`；
- 未配管理员名单时，请先在 WebUI 插件配置页把群号填进「管理员群列表」、
  或把 QQ 填进「管理员 QQ 列表」，否则聊天里无法切人格（只能用本地控制台）；
- 命令用法提示、拒绝提示、manifest 描述与文档已同步。

### 文档

- `README.md` 新增 **「最快上手（基本用法）」**：写好人格 → 配置管理员/管理群 →
  用 `/mps swap` 把指定聊天流路由到该人格，三步零配置成本（不碰触发条件、
  概率、权重、脚本）；
- 同步更新 `AGENT.md`（命令权限）、`自检指南.md`、`maips/maips_selfcheck.py`
  提示语、`开发文档.md`（§3.2 / §5 / §6）。

## 1.4.2

为全部配置项补充/完善了用户友好的中文注释与说明（悬停提示），完善配置节说明；插件功能与行为不变。
