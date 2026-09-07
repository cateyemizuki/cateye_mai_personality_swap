# AGENT.md — maips 脚本编写契约（供 AI 编码助手使用）

> 本文件（插件根目录）是 AI 编码助手编写/修改 maips 脚本的**完整自足契约**：
> 不需要阅读 MaiBot 源码或原文档即可完成脚本编写。人类读者请看同目录
> `README.md`（命令/配置/脚本入门已合并于此）与 `自检指南.md`（方法自检步骤）。
> 修改脚本后保存即热重载；`/mps script list` 查看加载状态，`/mps script reload`
> 强制重载，`/mps status` 查看当前各聊天流人格状态。

---

## 1. 背景与心智模型

- MaiBot 的人格由三部分文本描述：**注入表达**（怎么说）、**人格约束**（我是谁）、
  **行为风格**（何时说话）。官方配置 `bot_config.toml` 的 `[personality]` 三字段
  定义了全局默认人格（下称"主人格"）。
- 本插件把这三部分打包成**预设**（preset）。一个预设被"激活"到某个聊天流时，
  以**覆盖式注入**生效（见下）：预设的人格约束/表达替换 replyer system 里官方
  对应段落、行为风格替换 planner system 里官方行为段——聊天流内 bot 的说话
  方式和参与姿态随之改变；主人格状态下则不改写任何东西（完全按官方配置表现）。
- **覆盖式注入是唯一注入方式（无配置开关）**：插件在 LLM 请求前把请求 items
  首条 system 消息里的官方人格/表达/行为段落**整体改写为预设内容**——官方人设
  从请求中消失，模型眼里只有预设人设，不存在"原人格 vs 注入人格"的冲突，也
  不会被当作提示注入而忽视。配置触发（条件/概率/权重自动替换）与 maips 脚本
  触发（`ctx.swap`）一律走同一覆盖路径。若 system 结构锚点未命中（自定义
  system / 非中文 locale / 其它插件改写），自动回退为在 items 尾部追加一段
  （保证预设仍生效），并在插件日志记 warning。
- **当前身份说明（v1.4.0 定稿文案；v1.3.8 起引入，原名"身份纠偏尾注"）**：
  预设激活且配置了 `persona_name` 时（否则新身份=官方名、无新旧之分，不注入），
  向模型说明历史里官方名发言是自己的旧身份：replyer 在 items **最末追加**一条
  "【当前身份提醒】"；planner **改写宿主每轮在 items 末位追加的提醒**（
  "你需要输出对{官方名}发言的分析…"，由宿主 `chat_loop_service` 构造）为同一
  文案，找不到该提醒时回退追加。文案（用户定稿）：
  > 你现在是{persona_name}。上文中{bot_name}的发言是人格切换前你以旧身份
  > 说过的，你只需要知道那是你过去进行过的发言，适当参考即可；一切以当前的
  > 设定与身份优先。

  目的：人格切换后，上下文里 bot 旧身份的发言（planner 中为 `user=官方名` 的
  Session 消息、replyer 中为 assistant/带名 user）会被模型误读为"另一个人"或
  "当前仍是旧身份"。该说明既让模型知道那是自己切换前的发言（语气/关系可参考，
  不切断记忆），又锁定当前身份为新人格名。锚点未命中的回退追加路径同样并入。
- **planner 外壳人格化（v1.3.9 起）**：宿主 planner 模板把 `{bot_name}` 用官方
  昵称渲染写满外壳（"关注 迷迭香 与用户的对话…""你不是 迷迭香 本人…"）。
  预设配置了 `persona_name` 时，覆盖注入会同时把外壳里的官方昵称整体替换为
  persona_name——决策模型眼里的 bot 指称从官方名变人格名（"关注 普瑞赛斯…
  为 普瑞赛斯 选择动作"）。未配 persona_name → 外壳保持官方昵称（旧行为）。
  replyer 的 identity 名字行自 v1.3.6 起已随 persona_name。
- 预设从哪来：用户在聊天里发 `/maisave [名称] [时长]` 把当前官方人格保存为预设
  （`[时长]` = 默认切换时长，见下"时长语义"）；
  或手工在数据目录 `preset/<预设名>.toml` 创建（格式见第 8 节）。
  **脚本 `ctx.swap` 的目标必须是已存在的预设**，编写脚本前先确认预设名
  （用户告知，或让用户先执行 `/maisave`；`/mps maisave list` 可列出）。
- **时长语义（务必区分"预设存续"与"单次切换持续"）**：
  - 预设文件 `preset/<名>.toml` 里的 `duration_minutes` 只是**默认切换时长**——
    当命令/脚本切到该预设且**未显式声明时长**时，作为"本次最多持续多久"生效；
    到点自动恢复主人格。**预设文件本身永不过期、不会被删除**，任何时刻都可再切。
  - 单次切换的生效时长 = 显式声明优先（`ctx.swap(preset, duration_minutes=X)` /
    `/mps swap <名> X`），否则取预设文件记录值；`duration_minutes=0`（或缺省值 0）
    表示"一直用、不自动恢复主人格"。状态持久化在磁盘上，bot 重启后依然有效。

## 2. 文件与环境约定

- 脚本目录：插件目录下的 `maips/`。每个顶层 `.py` 文件是一个独立脚本，
  **数量不限**，全部会被加载；文件按文件名排序依次注册，同文件内按定义顺序。
- `_` 开头的文件不作为脚本加载（当工具库用，可被其他脚本 `import`）。
- 不要创建 `__init__.py`；不要 import 宿主内部模块（`src.*`）。
- 脚本可用的外部模块：Python 标准库 + `mps_api`（宿主注入的特殊模块，
  无需安装）。`mps_api` 暴露：
  - `mps_api.on_event(事件名)`：事件订阅装饰器（见第 3 节）；
  - `mps_api.EVENTS`：全部事件名元组；
  - `mps_api.SWAP_PARAM_KEYS`：`ctx.set_swap_params` 接受的全部参数键
    （用于过滤 LLM 输出，见 5.5）。
- 脚本以**完整 Python 权限**运行在插件 Runner 进程内，与安装一个插件同等信任
  级别；不要在脚本中引入不可信代码。
- 单个处理函数抛异常只记日志并跳过该次调用，不影响其他脚本与 bot 主链路；
  文件顶层语法错误则整个文件不生效（`/mps script list` 显示 ❌）。
- **交付前清理缓存产物（硬性要求）**：任何对脚本做语法检查 / 本地 import /
  编译验证 / 测试（如 `python -m py_compile`、`python -c "import ..."`、运行
  测试脚本）后，都会在脚本目录与插件目录下产生 `__pycache__/`（含
  `*.pyc`）。**交付前必须删除这些缓存**，命令示例（在插件根目录执行）：

  ```powershell
  Get-ChildItem -Path . -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
  ```

  检查方式：交付前确认插件目录树里**没有任何** `__pycache__` / `*.pyc` 残留
  （`ls -R` 或资源管理器核对）。缓存在用户侧热重载 / 重启时会被 Python 自动
  重建，无需保留，也不会被脚本宿主读取；留着只会污染目录、可能让用户误以为
  是多余文件或同步进仓库。

### 命令权限（v1.3.1 起，v1.3.5 增加可配置管理员）

插件的 `/mps` 命令按子命令分权限：

- **公开**（任何可发指令的用户）：`/mps swap`、`/mps status`、
  `/mps maisave list` / `list`——切换/查看人格属日常功能；
- **仅管理员**（普通成员会被拒绝并回"仅限管理员"提示）：`/mps maisave`
  （含 `/maisave` 别名）、`/mps maiload`、`/mps weight`、`/mps debug`、
  `/mps script`——写预设、改权重/debug、重载脚本均会改插件状态或触发脚本
  执行，不应暴露给普通群成员。

**管理员如何判定**（满足其一即可）：
1. 宿主**本地 operator / 控制台**（`is_local_operator=True`，WebUI/终端发起）；
2. 消息所在群在配置 `[filter] admin_group_ids` 里（群内任何成员可执行管理
   子命令——最省事的个人 bot 用法）；
3. 发送者 QQ 在配置 `[filter] admin_user_ids` 里（可填裸 QQ 号或
   `platform:user` 如 `qq:123456`）。
配置项在 WebUI 插件配置页「黑白名单与管理员」节（脚本接管时仍生效）；留空
则仅本地 operator/控制台可用。

设计给 AI 脚本的需求提示：**让普通用户"按需切换人格"请走 `/mps swap`
或 maips 路由脚本；不要指望普通用户能执行 `/mps maisave` 等管理命令**——
管理命令需用户先把自己 QQ/群加进管理员配置。

## 3. 事件与触发时机

| 事件 | 触发时机 | ctx 关键字段 |
|---|---|---|
| `message` | 每条**非通知**入站消息（命令消息也有，`ctx.is_command=True`） | 全部字段 + `segments` |
| `bot_message` | bot 每条**发送成功**的消息 | 全部字段 + `segments` |
| `timer` | 周期触发（配置 `script.tick_interval_sec`，默认 60s，最小 10） | `now` / `hour` / `minute`（**无** `scope_key`、`segments`） |
| `swap` | 任何人格切换/恢复发生后（auto / command / script 来源都触发） | `scope_key` / `preset`（None=主人格）/ `source` / `duration_minutes` |
| `load` | 脚本加载/重载后（每次热重载都会重新触发） | 无事件数据 |

注册方式：`@on_event("事件名")`；同一函数只可注册一个事件；同一事件可在多个
脚本中注册（按文件名 + 注册顺序串行执行）。处理函数同步或 `async def` 均可。

## 4. ctx 数据字段

| 字段 | 类型 | 可用事件 | 含义 |
|---|---|---|---|
| `event` | str | 全部 | 当前事件名 |
| `now` | datetime | 全部 | 本地时间 |
| `hour` / `minute` | int | 全部 | 当前时/分（`now.hour` / `now.minute` 便捷属性） |
| `session_id` | str | message / bot_message | 聊天流 ID |
| `scope_key` | str \| None | message / bot_message / swap | 该流对应的持久化状态键（群号或 QQ 号；全局模式为 "main"；timer 事件为 None） |
| `group_id` / `user_id` | str | message | 群号 / 发送者 QQ 号 |
| `bot_user_id` / `bot_nickname` | str | message / bot_message | bot 自身账号（官方 `bot.qq_account`）/ 官方昵称（官方 `bot.nickname`）；**v1.3.7 起**，插件预热后注入；读取失败/非 QQ 平台可能为空串（见 5.1 @/身份） |
| `text` | str | message / bot_message | 消息文本（已处理的纯文本） |
| `is_command` / `is_notify` | bool | message | 是否命令消息 / 通知消息 |
| `segments` | list[dict] | message / bot_message | 原始消息段列表，段形如 `{"type": "text"\|"emoji"\|"image"\|"voice"\|"at"\|"reply"\|..., "data": ...}`。**@ 段（v1.3.7 起契约化）**：`{"type": "at", "data": {"target_user_id": "<被@者账号>", "target_user_nickname": "...", "target_user_cardname": "群名片或 null"}}`——见 5.1 `ctx.at_segments` / `ctx.at_targets()` |
| `preset` / `source` / `duration_minutes` | — | swap | 被切换到的预设名（None=主人格）/ 来源 / 时长 |

## 5. ctx API

### 5.1 查询

> 以下均为同步调用（除标注协程外）；任何一步失败都不影响脚本其它逻辑
> （单点异常被宿主隔离）。

| 调用 | 返回 | 说明 |
|---|---|---|
| `ctx.current_preset(scope=None)` | `str \| None` | 当前预设名；**主人格返回 `None`**（不是 `"main"`）。`scope` 解析规则见 5.2：缺省 = 事件所属聊天流；`"global"` = 全局 main；`timer` 事件没有默认 scope，**不传会抛 `ValueError`**。判重（守则 3）标准写法：`if ctx.current_preset() != 目标: ctx.swap(目标)` |
| `ctx.matches(pattern, mode="contains")` | bool | 对 **`ctx.text`**（当前消息已处理纯文本）做文本匹配。`pattern`：字符串**或列表（任一命中即 True）**。`mode`：`contains`（子串包含，**忽略大小写**）/ `exact`（整串完全一致，忽略首尾空白）/ `regex`（正则 `re.search`）/ `prefix`（前缀）/ `suffix`（后缀）；未知 mode 按 contains 处理；正则写错不抛异常只返回 False。**一致性（v1.3.2 起）**：所有模式先 `strip()` 首尾空白再比较；**空 pattern（空串）一律不命中**（避免 regex/startswith 空串恒真的放大器 bug）。例：`ctx.matches(["上班", "工作"], mode="contains")` |
| `ctx.message_types` | list[str] | 当前消息包含的段类型（`text` / `emoji` / `image` / `voice` / `file`…），**去重保序**。例：文字+表情消息 → `["text","emoji"]` |
| `ctx.has_type(t)` | bool | `t in ctx.message_types`；`ctx.has_text` / `ctx.has_emoji` / `ctx.has_image` 是其便捷别名 |
| `ctx.emoji_emotions()` | list[str] | 当前消息中表情包的情绪标签（**去重保序**）。**双来源自动回退**：① bot 自己发的表情包 → emoji 选择器命中情绪；② 入站（用户）表情包 → 优先按 hash 查宿主索引，**索引未命中自动从段 data 文本解析**（即消息里 `[表情包: 标签1,标签2,…]` 那段，MaiBot 默认每表情最多 5 个标签）。无表情/解析不出 → 空列表。**data 文本解析规则（实测）**：按 `, ，、；;`（半/全角逗号顿号分号）拆分；**ASCII 空格不是分隔符**（`开心 可爱` 会被当单个标签）；`[]{}` 等括符会被剔除但剔除后不再 trim（逗号后空格会残留在标签里）；任意非空文本都会作为单标签返回（不会因"无分隔符"返回空） |
| `ctx.match_emoji(emotions, mode="any")` | bool | 表情情绪匹配。`emotions`：字符串或列表。`mode="any"`：消息中**任一**表情带**任一**给定情绪即 True；`mode="all"`：给定情绪**全部**出现在消息表情情绪中才 True。空列表恒 False；大小写不敏感。例：`ctx.match_emoji(["开心","喜悦"], mode="any")` |
| `ctx.get_swap_params()` | dict | 当前**生效**替换参数快照。接管模式 = 脚本已设值 + 未设字段默认值；非接管 = 配置只读快照。键见 5.4 表；始终含全部 9 键 |
| `ctx.segments` | list[dict] | 原始消息段列表，每段 `{"type": "text"\|"emoji"\|"image"\|"voice"\|"at"\|"reply"\|..., "data": ...}`；`message`/`bot_message` 事件可用。**`at` 段**（v1.3.7 起契约化）：`data` 含 `target_user_id`（被 @ 者账号，QQ 号等）/ `target_user_nickname` / `target_user_cardname`（群名片或 null）——要拿"被 @ 者的数字账号"就解析这个 |
| `ctx.at_segments` | list[dict] | （v1.3.7）本消息中类型为 `at` 的**原始段**列表（等价于 `[s for s in ctx.segments if s["type"]=="at"]`）；无 → `[]` |
| `ctx.at_targets()` | list[dict] | （v1.3.7）本消息被 @ 对象摘要：每项 `{"user_id": ..., "nickname": ..., "cardname": ...}`（从 at 段拍平，字段缺省空串）；无 → `[]`。**判断"谁 @ 了谁"用这个** |
| `ctx.is_at_me` | bool | （v1.3.7）本消息是否 @ 了 **bot 自己**。判定顺序：① at 段 `target_user_id == ctx.bot_user_id`（bot_user_id 为空时跳过）；② 文本层含 `@{bot_nickname}`（与宿主渲染一致：@ bot 在纯文本里是官方昵称；bot_nickname 为空时跳过）。**注意**：不区分 @ 者是不是 bot 自己（bot 自 @ 也命中）——要排除 bot 自触发须同时比较 `ctx.user_id != ctx.bot_user_id`（bot_user_id 为空时此项恒真，配合 `ctx.user_id != ""` 判断是否 bot 发出） |
| `ctx.debug_enabled` | bool | debug 模式开关（`/mps debug true\|false`，持久化）。依赖 debug 的自检/通知脚本应据此在关闭时静默（见 5.6 与自检脚本） |

**常用组合**（消息分类惯用法）：
```python
if ctx.has_emoji and ctx.match_emoji(["委屈", "难过"], mode="any"):
    ...           # 消息带委屈/难过情绪的表情包
if ctx.has_image and not ctx.has_emoji:
    ...           # 纯图片消息（如截图）
if ctx.matches("打卡", mode="contains"):
    ...           # 文本含关键词
```

**@ / 身份惯用法（v1.3.7）**：
```python
# 有人 @ 了 bot（bot_user_id 预热后按账号精确判，否则文本层 @昵称 兜底）
if ctx.is_at_me:
    ...
# 且要排除 bot 自 @（避免 bot 自己 @ 人触发 bot_message 时误判）
if ctx.is_at_me and ctx.user_id != ctx.bot_user_id:
    ...          # 只有"别人 @ bot"才命中
# bot @ 了谁（bot_message 事件）：遍历被 @ 对象，拿数字账号
for target in ctx.at_targets():
    if target["user_id"] == "某个群友QQ":
        ...
# 文本层提及某词且该消息 @ 了某人：ctx.text 已把 @ 渲染成 @昵称，可与 ctx.matches 合用
```

> 术语澄清：`ctx.is_at_me` 的判定基于 at 段账号比对 + 文本 `@官方昵称` 兜底，
> **不依赖宿主 is_at/is_mentioned 布尔**（插件观察 hook 早于宿主计算它们，见开发文档）。
> `ctx.bot_user_id` 来自官方 `bot.qq_account`，多账号平台/读不到时为空串 → at 段比对
> 自动跳过、只靠文本兜底，行为保守不误报。

### 5.1b 持久 KV（跨脚本重载 / 插件重启保留）

> 脚本模块在热重载时被整体重建（模块级变量清零），因此"跨事件累计点数 / 挂起标记"
> **必须用 KV 落盘**，不能只存模块全局。KV 是**受信任脚本专用**的持久通道（无权限/
> 频率限制，等同脚本本身权限），数据落数据目录 `personality/script_kv.json`，
> 所有脚本共享一张表——**不同脚本用带前缀的键**（如 `nightly_points`）避免冲突。

| 调用 | 返回 | 说明 |
|---|---|---|
| `ctx.kv_get(key, default=None)` | Any | 读取脚本持久键值；键非法或不存在返回 `default` |
| `ctx.kv_set(key, value)` | bool | 写入/覆盖（值 JSON 可序列化即可：str/int/float/bool/list/dict/None）；成功返回 True。**键名**只允许 `[A-Za-z0-9_.-]`、长度 ≤64；键非法 / 值不可序列化 / 全表超 512 条 → False（不抛异常，可自行 log） |
| `ctx.kv_del(key)` | bool | 删除；键不存在返回 False |
| `ctx.kv_keys()` | list[str] | 全部键（字典序） |

```python
# message 事件里累计点数
@on_event("message")
def count_points(ctx):
    if ctx.is_command or ctx.is_notify:
        return
    n = ctx.kv_get("nightly_points", 0)
    ctx.kv_set("nightly_points", n + 1)
    ctx.log(f"当前点数 {n + 1}")
```

### 5.1c 概率触发 / 计数阈值范式

> `random` 模块在脚本内可直接使用（脚本是完整 Python 权限）。**概率命中 → 改 KV**
> 的惯用法（守则 5：LLM 类调用务必先过随机门再调，避免放大成本）：

```python
import random

@on_event("bot_message")
def bot_emoji_gate(ctx):
    if ctx.is_command or ctx.is_notify:
        return
    if not ctx.has_emoji:
        return
    if not ctx.match_emoji(["开心", "可爱"], mode="any"):
        return            # 不满足"开心或可爱"这一情绪条件
    if random.random() >= 0.5:
        return            # 50% 概率门
    points = ctx.kv_get("nightly_points", 0) + 1
    ctx.kv_set("nightly_points", points)
    ctx.log(f"表情命中，+1 点 → {points}")
```

**计数阈值切换**（攒到 N 才切）：
```python
@on_event("message")
def maybe_flip(ctx):
    if ctx.is_command or ctx.is_notify:
        return
    n = ctx.kv_get("nightly_points", 0)
    if n < 10:
        return
    ctx.kv_set("nightly_points", 0)          # 先清零再切（防重复触发）
    ctx.swap("p_cheerful", duration_minutes=random.randint(1, 3) * 60)
```
（注意 `ctx.swap` 的 `duration_minutes` 单位是**分钟**；若需要秒级时长就
`duration_minutes=max(1, round(秒数/60))`——最小 1 分钟。）

### 5.2 切换操作（同步生效）

```python
ctx.swap(preset: str, duration_minutes: int | None = None, scope: str | None = None)
ctx.revert(scope: str | None = None)
ctx.current_preset(scope: str | None = None) -> str | None
```

**scope 解析规则**（三者一致）：
- `scope=None`（省略）→ **事件所属聊天流**（`message`/`bot_message`/`swap` 事件
  有默认 scope_key）；`timer` 事件**没有**默认 scope，不传/传 `"stream"` 会抛
  `ValueError: 当前事件没有默认作用域，请显式指定 scope='global'`；
- `scope="global"` → **全局 main 状态**（对应状态文件 `personality/main.json`，
  任何聊天流的表现都随之改变——慎用，守则 4）；
- 其它字符串（如群号）→ 显式指定该聊天流状态。

**duration 语义**（见第 1 节"时长语义"）：
- `duration_minutes=None`（省略）→ 本次持续上限**回退取预设文件记录的
  `duration_minutes`**；
- `0` → 一直用、不自动恢复主人格；
- 正整数 → 分钟数，到点自动恢复主人格。
只影响本次激活；**预设文件本身永不过期**。

**返回/异常/边界**：
- `swap` 无返回值；成功会广播 `swap` 事件（`ctx.preset/source/duration_minutes`）；
- `preset` 不存在 → 抛 `ValueError`（脚本应 try/except 或先确认 `/mps maisave list`）；
- **黑白名单命中** → 静默拦截（记日志，不抛异常、不切换）；
- `revert` 在已主人格时幂等（无害）；
- 判重惯用法（守则 3）：`if ctx.current_preset() != "目标": ctx.swap("目标")`；
  定时任务中判重防每个 tick 重复触发。

**示例**：
```python
# message 事件：当前聊天流切到 work_mode，120 分钟后自动回主人格
ctx.swap("work_mode", duration_minutes=120)
# timer 事件：必须显式给 scope（这里切全局）
if ctx.hour >= 23 and ctx.current_preset(scope="global") != "sleep_mode":
    ctx.swap("sleep_mode", scope="global")
# 恢复主人格（当前聊天流）
ctx.revert()
```

### 5.3 对内置自动替换管线的逐轮控制（message / bot_message 事件）

> 内置"自动替换管线"只在消息满足配置/脚本设定的条件后按概率+权重抽签。脚本可在
> 每轮（每条触发消息）**否决**或**改写**该轮参数。直接 `ctx.swap`/`ctx.revert`
> 不受此节影响（那是即时切换，跳过概率与权重）。

| 方式 | 效果 | 说明 |
|---|---|---|
| 处理器 `return False` | 否决本轮自动替换 | 同步返回 False 即可；async 处理器返回 `False` 同样生效 |
| `ctx.veto(reason)` | 否决本轮自动替换 | 方法形式；`reason` 会写 info 日志 |
| `return {"probability": p, "weights": {...}, "exclude_current": bool}` | 覆盖本轮参数（可只给部分键） | dict 里缺的键不覆盖；见下方键说明 |
| `ctx.set_probability(p)` | 覆盖本轮触发概率 | `p` 须在 0~1，越界抛 `ValueError` |
| `ctx.set_weight(name, w)` | 覆盖/新增本轮某候选权重 | `name` 可为预设名或 `"main"`（主人格）；`w>0` 设置、`w<=0` 移除该候选 |
| `ctx.exclude_current_in_round(b)` | 覆盖本轮"忽略当前人格" | `True` = 本轮抽取时剔除当前人格（必变更） |

`weights` 字典与 `set_weight` 的键：预设名或 `"main"`（主人格）。多脚本时这些
覆盖只作用于**当前这轮**（不会持久化到下条消息）；返回 dict 与 ctx 方法形式等价
（dict 内部转成对应方法调用），可混用。

```python
@on_event("message")
def boost(ctx):
    if "来表演" in ctx.text:
        ctx.set_probability(1.0)          # 本轮必触发
        ctx.set_weight("preset1", 10.0)   # 并给 preset1 加权重
        ctx.exclude_current_in_round(True)  # 且不许抽到当前人格

@on_event("message")
def quiet(ctx):
    if "安静" in ctx.text:
        ctx.veto("群里在安静聊天")  # 或 return False
```

> **越界值行为（实测，脚本需自守）**：`ctx.set_probability` 传越界值（<0 或 >1）
> 抛 `ValueError`，且在**处理器内**调用时该异常会被宿主隔离（只记运行期错误，
> 不影响 bot）；但**`return {"probability": 越界}` 这种 dict 返回形式**的越界会从
> 事件派发直接透出（不在单点隔离内）——所以脚本应保证 dict 里的 `probability`
> 恒为 0~1（如需动态计算先 `max(0, min(1, p))`），不要把越界值写进返回 dict。

### 5.4 接管模式的持久参数（`ctx.set_swap_params(**fields)`）

配置页「脚本接管」开关开启后，配置文件中的**自动替换行为**（替换概率/触发条件/
权重/预设）**全部失效**（按默认值处理；黑白名单与插件总开关除外），由本方法设定
的运行时参数接管。**未设定的字段默认"留空"**：`probability=0.0`（自动替换惰性）、
条件列表为空（不检查）、`preset_weights={}`（无候选）——即不写逻辑就没有自动
替换。**注入方式（覆盖式 system 改写）不受接管影响**——接管与否都恒为覆盖注入。

> 各配置字段与接管的对应关系补全如下（含字段名、类型、默认、含义）：

| 字段 | 类型 | 默认 | 含义（对应被接管的配置） |
|---|---|---|---|
| `probability` | float 0~1 | `0.0` | 自动替换触发概率；0=不自动替换（`[swap] probability`） |
| `per_stream` | bool | `True` | 是否仅触发聊天流级替换（关闭=全局替换）（`[swap] per_stream`） |
| `reroll_during_swap` | bool | `False` | 替换期间是否还能再次抽签（`[swap] reroll_during_swap`） |
| `exclude_current` | bool | `False` | 每次抽取是否排除当前人格（`[swap] exclude_current`） |
| `main_weight` | float ≥0 | `0.8` | 主人格在抽取池中的权重（`[weights] main`） |
| `preset_weights` | dict[str,float] | `{}` | 各预设权重，与主人格一起参与抽取，不要求总和为 1（`[weights.presets]`） |
| `non_bot_keywords` | list[str] | `[]` | 用户消息关键词条件；命中任一才可触发（空=不检查，无 default 占位语义）（`[condition] non_bot_keywords`） |
| `bot_keywords` | list[str] | `[]` | bot 消息关键词条件（空=不检查）（`[condition] bot_keywords`） |
| `time_windows` | list[str] `HH:MM-HH:MM` | `[]` | 时段条件（可多个，支持跨午夜；空=全天）（`[condition] time_windows`） |

- 推荐在 `load` 事件里调用一次（每次热重载后 load 重新触发，可安全重复设定）；
  多个脚本调用时**后写者覆盖**同名字段。
- 未接管时调用不报错但也不生效。
- **非原子（实测）**：一次 `set_swap_params(a=合法, b=非法)` 遇到非法/未知字段会
  抛 `ValueError`，但**前面已处理的合法字段已写入、不会回滚**。因此调用前应先按
  `mps_api.SWAP_PARAM_KEYS` 过滤并保证值合法，或把"全量重置"拆成先逐个设合法值。
- 接管模式下仍保持默认行为的部分：**注入恒为覆盖式**（system 改写，无通道/
  兼容模式配置）、官方临时说话风格抑制开启、预设备份上限 5。

### 5.5 LLM 调用与结构化参数（均为协程，需 `async def` 处理器）

> LLM 调用会真实消耗模型 token（守则 5），务必按消息计数/冷却/降频使用；小任务
> 用 `utils`、`max_tokens` 给小值。所有失败**不抛异常**，返回空/None 并写 warning。

| 调用 | 返回 | 参数与语义 |
|---|---|---|
| `await ctx.llm_generate(prompt, task="replyer", temperature=None, max_tokens=None)` | `str` | 通用文本生成。`task` = 宿主模型任务名（见下）；`temperature`/`max_tokens` 省略用宿主默认。**失败返回 `""`**（不抛异常）。例：`resp = await ctx.llm_generate("给一句话总结", task="utils", max_tokens=50)` |
| `await ctx.llm_json(prompt, task="replyer", temperature=None, max_tokens=None)` | `dict \| list \| None` | 让模型返回 JSON 并解析；**自动剥离 ` ```json ` 代码围栏**；非 JSON / 失败返回 `None`。prompt 里务必强调"只输出 JSON"并给出字段约束 |
| `await ctx.emotion_score(recent_n=10, prompt_template=None, task="replyer", temperature=0.3, max_tokens=16, scale=10.0)` | `float \| None` | 让 LLM 给最近 `recent_n` 条聊天打情绪分（0~`scale`，默认 0~10）。`prompt_template` 需含 `{context}`（可选 `{scale}`）占位，缺省用内置"情绪强度评分"模板；响应里抓第一个数字，解析不出返回 `None`；结果钳制在 `0..scale`。`max_tokens` 默认 16（只需输出数字） |
| `await ctx.recent_context(recent_n=10)` | `str` | 当前聊天流最近 `recent_n` 条消息的可读文本（带发送者/时间），用于拼 LLM 提示。失败返回 `""` |

**任务名（task）**：必须是 MaiBot 模型配置 `[model_task_config.*]` 的键。常用：
`replyer`（回复模型）、`planner`（规划模型，有 Agent 能力）、`utils`（小任务，
快且便宜，评分/分类首选）、`memory`、`learner` 等。传不存在的任务名 → 失败返回
空（记 warning）。按用途选任务：

```python
task = "planner" if score >= 5 else "utils"
```

**结构化回填模式**（LLM 返回参数 → `SWAP_PARAM_KEYS` 过滤 → 应用）：

```python
import mps_api

data = await ctx.llm_json("根据聊天氛围输出替换参数 JSON：{\"probability\": 0~1}。聊天记录：" + ctx.text)
if isinstance(data, dict):
    ctx.set_swap_params(**{k: v for k, v in data.items() if k in mps_api.SWAP_PARAM_KEYS})
```

`SWAP_PARAM_KEYS` 过滤**必须做**：未知键会让 `set_swap_params` 抛 `ValueError`。
同模式可让 LLM 决定切换目标（`{"preset": "...", "duration": 分钟}`），但要先校验
`preset` 真实存在再 `ctx.swap`（见 5.2 异常清单）。

### 5.6 其他

- `await ctx.send_text(text)`：协程（**需 `async def` 处理器**），向事件所属聊天流
  发一条纯文本。仅 `message` / `bot_message` 事件可用（有 `session_id`）；
  无 `session_id`（如 timer）抛 `ValueError`。失败会被宿主记录，不影响脚本。
  示例（在 swap 后播报）：
  ```python
  @on_event("swap")
  async def announce(ctx):
      name = ctx.preset or "主人格"
      await ctx.send_text(f"人格已切换：{name}")
  ```
- `ctx.log(message)`：写插件日志（info 级，前缀 `[脚本]`）。调试用
  `/mps script list` 看运行期错误、插件日志看 info/debug 输出。
- `ctx.debug_enabled`（bool）：`/mps debug true|false` 控制（持久化）。自检/
  通知类脚本应在关闭时**完全静默**（不响应、不发送、不调 LLM），只在开启时工作
  ——参考 `maips/maips_selfcheck.py` 的写法。

## 6. 编写守则

1. **幂等**：脚本会被反复热重载，`load` 里的初始化必须可重复执行；不要依赖
   "只执行一次"。
2. **不阻塞**：模块顶层只做注册和轻量初始化；不要 sleep、不要跑长循环、不要起
   常驻线程——周期任务用 `timer` 事件。
3. **切换判重**：`timer` 里做定时切换时用 `ctx.current_preset(scope=...) != 目标`
   防止重复触发。
4. **切换频率**：尽量**按触发群切人格**（默认 per_stream 即聊天流级；直接切换也
   尽量不带 `scope="global"`），只在确有必要时全局切换——人格切换过频繁会导致
   上下文与人设混乱。
5. **LLM 成本**：按消息触发的 LLM 调用会放大调用量，务必配合计数器 / 冷却 /
   timer 使用；评分/分类类小任务用 `utils` 任务，`max_tokens` 给小值。
6. **错误处理**：单点异常已被隔离，但逻辑错误（如 swap 了不存在的预设）会导致
   功能静默失效——用 `ctx.log` 记录关键分支，`/mps script list` 检查加载状态。
7. **持久状态必须用 KV**：脚本热重载会**重建模块**（模块级变量清零，见 10 已知边界），
   跨事件/跨重启要保留的计数、挂起标记等**一律存 `ctx.kv_*`**，不要只放模块全局
   `_count = {...}`（那只能活到下一次重载）。多脚本共享同一张 KV 表，键名加前缀。

## 7. 场景配方

以下均为可直接落盘的完整脚本片段（预设名按实际替换）。

### 7.1 关键词直接切换（列表任一命中）

```python
from mps_api import on_event

@on_event("message")
def work_mode(ctx):
    if ctx.is_command:
        return
    if ctx.matches(["进入工作模式", "开工"], mode="contains"):
        ctx.swap("work_mode", duration_minutes=120)
```

### 7.2 表情包情绪切换（用户 + bot）

```python
from mps_api import on_event

@on_event("message")
def happy_sticker(ctx):
    if ctx.match_emoji("开心"):
        ctx.swap("cheerful", duration_minutes=30)

@on_event("bot_message")
def bot_sticker(ctx):
    if ctx.match_emoji("委屈"):
        ctx.revert()
```

### 7.3 每 N 条消息让 LLM 评情绪分，按分数切换

```python
from mps_api import on_event

_count = {"n": 0}

@on_event("message")
async def mood_check(ctx):
    _count["n"] += 1
    if _count["n"] % 20:
        return
    score = await ctx.emotion_score(recent_n=20, task="replyer",
                                    temperature=0.3, max_tokens=16)
    if score is None:
        return
    if score >= 7:
        ctx.swap("preset1", duration_minutes=60)
    elif score <= 3:
        ctx.revert()
```

### 7.4 LLM 返回结构化参数并应用

```python
import mps_api
from mps_api import on_event

@on_event("message")
async def llm_director(ctx):
    if ctx.is_command:
        return
    context = await ctx.recent_context(15)
    data = await ctx.llm_json(
        "根据聊天氛围决定人格切换。只输出 JSON："
        '{"preset": "preset1|preset2|main", "duration": 30,'
        ' "params": {"probability": 0~1}}。聊天记录：' + context,
        task="planner", temperature=0.2, max_tokens=120,
    )
    if not isinstance(data, dict):
        return
    params = data.get("params")
    if isinstance(params, dict):
        ctx.set_swap_params(**{k: v for k, v in params.items() if k in mps_api.SWAP_PARAM_KEYS})
    preset = str(data.get("preset") or "")
    if preset == "main":
        ctx.revert()
    elif preset:
        ctx.swap(preset, duration_minutes=data.get("duration"))
```

### 7.5 定时切换（夜间人格，带判重）

```python
from mps_api import on_event

@on_event("timer")
def nightly(ctx):
    if ctx.hour >= 23 and ctx.current_preset(scope="global") != "sleep_mode":
        ctx.swap("sleep_mode", scope="global")
```

### 7.6 按群差异化（接管模式参数初始化）

```python
from mps_api import on_event

@on_event("load")
def setup(ctx):
    ctx.set_swap_params(
        probability=0.1,
        non_bot_keywords=["开会"],
        time_windows=["09:00-18:00"],
        preset_weights={"work_mode": 0.5},
        main_weight=0.8,
    )

@on_event("message")
def group_tuned(ctx):
    if ctx.group_id == "123456" and "摸鱼" in ctx.text:
        ctx.swap("slacker", duration_minutes=45)
```

### 7.7 综合：晚间点数累计 + 概率门 + @bot 重击 + 随机时长切换

> 这是"攒点 → 到阈值才切"的完整骨架（v1.3.7 起可用 KV 持久化实现）。业务规则
> （数值、时段、条件）按需改；所有计数走 `ctx.kv_*` 落盘，热重载/重启不丢。

```python
import random
from mps_api import on_event

THRESHOLD = 10            # 攒满自动切
PRESET = "p_cheerful"     # 目标预设（按实际替换）
_P = "nightly_points"     # KV 键（带前缀防与其他脚本冲突）


def _in_night_window(ctx) -> bool:
    """只在 22:00–01:00 触发检查。"""
    h = ctx.hour
    return h >= 22 or h < 1


@on_event("bot_message")
def bot_actions_count(ctx):
    """bot 自己发的消息：开心/可爱表情 50% +1 点；提及关键词 20% +1 点。"""
    if ctx.is_command or ctx.is_notify or not _in_night_window(ctx):
        return
    gained = 0
    if ctx.has_emoji and ctx.match_emoji(["开心", "可爱"], mode="any") and random.random() < 0.5:
        gained += 1
    if ctx.matches("博士", mode="contains") and random.random() < 0.2:
        gained += 1
    if not gained:
        return
    n = ctx.kv_get(_P, 0) + gained
    ctx.kv_set(_P, n)
    ctx.log(f"bot 行为 +{gained} 点 → {n}")


@on_event("message")
def others_at_bot_count(ctx):
    """别人 @bot 且消息提关键词：+9 点（含 bot 自 @ 排除）。"""
    if ctx.is_command or ctx.is_notify or not _in_night_window(ctx):
        return
    if not ctx.is_at_me:
        return
    if ctx.user_id and ctx.user_id == ctx.bot_user_id:
        return                      # 排除 bot 自己 @ 自己
    if not ctx.matches("普瑞赛斯", mode="contains"):
        return
    n = ctx.kv_get(_P, 0) + 9
    ctx.kv_set(_P, n)
    ctx.log(f"@bot+关键词 +9 点 → {n}")


@on_event("timer")
def flip_when_full(ctx):
    """攒到阈值：切随机 1–3 分钟（timer 无 scope，须显式 global 或写死群号）。"""
    if not _in_night_window(ctx):
        return
    n = ctx.kv_get(_P, 0)
    if n < THRESHOLD:
        return
    ctx.kv_set(_P, 0)               # 先清零再切，防重复触发
    minutes = random.randint(1, 3)
    ctx.swap(PRESET, duration_minutes=minutes, scope="global")
    ctx.log(f"点数满 {THRESHOLD}，切 {PRESET} {minutes} 分钟")
```
> 说明：timer 事件没有默认 scope，上面切的是全局 main；若想按聊天流累计/切换，
> 请在 `message`/`bot_message` 事件里判断 `ctx.kv_get(_P, 0)` 达标即切
> （用 `ctx.scope_key` 拼 KV 键，如 `f"points_{ctx.scope_key}"` 实现逐流隔离）。

## 8. 预设文件（供脚本切换的目标）

预设存放于**插件数据目录** `preset/<预设名>.toml`（数据目录即
`<MaiBot根>/data/plugins/<插件id>/`），通常由 `/maisave` 生成，也可手工创建：

```toml
name = "work_mode"
persona_name = "工作狂小林"   # 可选：该人格下 bot 的自称名（覆盖官方昵称）
expression = """说话简短利落。"""
persona = """你是专注工作的助手。"""
behavior = """优先处理任务相关话题。"""
duration_minutes = 0
created_at = "2026-09-06T19:00:00+08:00"
```

- **`persona_name`（可选，v1.3.6 起）**：该人格下 bot 的**自称名**。激活时覆盖式
  注入会把 replyer system 身份段的"你的名字是官方昵称。"改为"你的名字是
  {persona_name}。"——用于人格化改名（如预设是"哼酱/夜/蹦蹦"时让 model 自称
  对应名字）。**配了它时注入范围不止 replyer 身份行**：planner 外壳里的官方昵称
  一并替换（v1.3.9，"关注 普瑞赛斯…为 普瑞赛斯 选择动作"），replyer/planner 的
  "当前身份说明"尾注/提醒也会使用它（v1.4.0，"你现在是{persona_name}…"，见第 1
  节注入说明）。缺省/空串 = 沿用官方 `bot.nickname`（上述人格化全部不生效，保持
  官方口径）。手工创建预设时填；`/maisave` 保存官方人格时此项为空（回退官方昵称）。
- **`duration_minutes` = 该预设的"默认切换时长"**：当有人/脚本用
  `ctx.swap("work_mode")` 或 `/mps swap work_mode`（不带显式时长）切到它时，
  本次激活最多持续该分钟数，到点自动恢复主人格；`0`（默认推荐）= 一直用、
  不自动恢复。显式声明时长时（`ctx.swap("work_mode", duration_minutes=120)` /
  `/mps swap work_mode 120`）以显式值为准，不读文件。
  **它不表示预设会过期**：文件永久存在，随时可再切；改时长只是改"将来默认
  持续多久"。
- 预设名即文件名（中文/字母/数字/下划线/连字符）。

## 9. 调试闭环

1. `/mps script list` — 脚本是否加载、处理器数量、错误详情；
2. `/mps script reload` — 修改后强制重载；
3. `/mps status` — 各聊天流当前人格与剩余时长；
4. `/mps maisave list` — 可用的预设名；
5. 插件日志（debug 级）记录每次注入与切换；`ctx.log(...)` 可输出脚本侧信息。

## 10. 已知边界

- 入站表情包的情绪匹配仅覆盖 MaiBot 表情库中已标注的表情包；未入库的
  `emoji_emotions()` 返回空——MaiBot 收集并标注新表情后索引自动补全。
- 接管模式下未由脚本设定的参数全部取默认（概率 0 = 没有自动替换），黑白名单
  与插件总开关仍然生效。
- 命令设置的权重（`/mps weight`）在接管模式下与 `preset_weights` 合并且命令优先。
- 事件派发顺序 = 文件名排序 + 文件内定义顺序；依赖执行顺序的逻辑应写在同一文件。
  注意：单个文件热重载后，其处理器会移动到派发顺序末尾——不要依赖跨脚本的
  精确先后关系。
- **脚本模块在热重载/插件重启时被整体重建**（模块级变量清零）——跨事件状态必须用
  `ctx.kv_*`（5.1b）落盘；模块全局只适合"本次加载内的一次性缓存"。
- **KV 表是全部脚本共享的一张表**（`personality/script_kv.json`），键名请带脚本
  前缀；条目上限 512，超限写入返回 False（记 log 排查）。
- `ctx.bot_user_id` 来自官方 `bot.qq_account`——读取失败/为空/多账号平台时为空串，
  此时 `ctx.is_at_me` 的段比对自动跳过、只靠 `@官方昵称` 文本兜底，可能误报
  （昵称撞车）或漏报（对方用 ID 而非昵称 @）。要 100% 可靠请确认宿主已配置
  `bot.qq_account`。
- @ 检测基于 at 段账号比对 + 文本兜底，**不读取宿主 is_at/is_mentioned 布尔**
  （插件观察入站 hook 早于宿主计算这两个布尔，见开发文档；直接依赖它们会拿到
  恒 False 的假象）。

---

## 附录 A：AI 生成新人格的交付工作流（AGENT 侧要求）

> 配合本契约使用：**第 1-10 节是"怎么写脚本"的静态规范**，本节是"接到
> 生成人格需求时按什么流程做、交付什么"的动态要求。用户侧的手动步骤见
> 插件根 `README.md`「用 AI 生成新人格」章节——AI 按本节执行即可。

### A.1 两种来源，先确认你拿到什么

> **预设交付位置（重要）**：插件根目录下的 `preset/`（随插件打包的**空暂存区**）
> 是 AI 交付预设的**标准落点**——插件在加载（on_load）时会把该目录里的 `*.toml`
> **自动移入数据目录** `preset/<名>.toml`（同名跳过保留现有、空目录忽略）。
> 因此**不需要也不建议**把预设直接指到数据目录：把文件放插件根 `preset/` 后
> 整个插件目录打包/同步到服务器，重启即自动就位。

- **本地部署（AI 能直接读插件目录）**：用户已把麦麦的"主人格"用
  `/maisave <名> 0` 保存为预设文件（数据目录 `preset/<名>.toml`）。你应：
  1. 读插件目录（`README.md` / `AGENT.md` / `开发文档.md` / `maips/` 全部脚本）理解
     现有预设、路由脚本与注入方式；
  2. 读用户保存的主人格预设文件（数据目录 `preset/<主人格名>.toml`）——它即"参考基准"；
  3. 按 A.2 派生新预设，**把每个新预设 `.toml` 写进插件根目录 `preset/`**
     （路由/触发脚本写进 `maips/`）并告诉用户已放好——插件下次加载会自动把
     `preset/` 里的预设移入数据目录；maips 保存即热重载。
  4. **交付后提醒用户**：主人格预设只是参考快照，**非系统必需**——bot 主表现来自
     官方配置本身，该文件不激活就不生效。可让用户**删除它**或**移入数据目录
     `preset/backup/`**（不影响任何功能，重新 `/maisave` 可再生成）。别让它作为
     常驻预设留在列表里造成混淆（尤其命名含"主人格"时）。
- **服务器部署（AI 读不到麦麦目录）**：用户会手动复制主人格预设文件内容给
  你（或按 README 里的格式粘贴）。你**只生成文件内容并交付**，明确告诉用户：
  - **预设文件 → 放进插件目录根部的 `preset/` 文件夹**（`<插件根>/preset/<预设名>.toml`，
    随插件目录一起打包/同步）；插件加载时自动移入数据目录，**无需手动进数据目录**；
  - 路由/触发脚本 → 放进插件目录 `maips/<脚本名>.py`；
  - 放好后重启/重载插件，验证 `/mps script list` 无 ❌ → `/mps maisave list`
    能看到导入的预设。

### A.2 从主人格派生新人格的模板

用户给你主人格后，通常会这样提要求：
> "参考我的主人格，做一个 XXX 风格的人格，路由到群 A / 私聊 B；另一个 YYY
> 风格人格在每天 Z 点触发。"

按此派生每个新人格 = **一个预设文件**（TOML）+ **按需一个脚本**。预设结构
（字段与格式见第 8 节，字段语义见第 1 节）：

- `name`：有意义、与脚本对应的小写/下划线名；
- `persona_name`（可选，推荐给风格化人格配一个）：该人格下 bot 的自称名，激活后
  replyer system 会自称这个名字（如"哼酱"），planner 外壳与身份说明尾注/提醒
  也随它人格化（v1.3.9/1.4.0，见第 1 节）。缺省则沿用官方昵称（见第 8 节）；
- `expression`：基于主人格的"表达"改写为风格化表达（怎么说）；
- `persona`：基于主人格的"人格约束"改写为风格化人格（我是谁/红线）；
- `behavior`：基于主人格的"行为风格"改写（何时说/参与姿态）；
- `duration_minutes`：新人格的**默认切换时长**——切换时未显式声明则按此计时、到点
  自动恢复主人格。默认给 `0`（一直用、不自动恢复）除非用户明确要"切到后 X 分钟
  自动回主人格"；预设文件本身永不过期；
- **主人格三段的"腔调/口头禅/禁忌"要保留**，只替换风格维度——否则生成的
  人格会丢掉用户原设的精髓。

要求脚本时按触发方式选择（见第 7 节配方与 5.x 的 ctx API）：

| 触发方式 | 做法 |
|---|---|
| 固定聊天流 | `@on_event("message")` 里按 `ctx.group_id` / `ctx.user_id` 路由，见 `maips/example.py` 的路由示例 |
| 关键词/表情 | `ctx.matches(...)` / `ctx.has_emoji` + `ctx.match_emoji(词, mode="any"\|"all")` |
| 定时 | `@on_event("timer")` + `ctx.hour/minute`，切换前用 `ctx.current_preset()` 判重 |
| 周期性/自动轮换 | `load` 事件 `ctx.set_swap_params(...)`（见 5.4）接管自动替换 |

**路由与定时的实现细节（务必写对）**：
- **群/私聊判定**：群消息 `ctx.group_id` = 群号（字符串）；私聊消息
  `ctx.group_id` = **空字符串 `""`**（不是 None、不是缺省键），此时 `ctx.user_id`
  即对方 QQ 号。判私聊标准写法：`if not ctx.group_id and ctx.user_id == 目标QQ:`；
  判指定群：`if ctx.group_id == "群号":`。群号/QQ 均为字符串，比较用字符串字面量。
- **timer 的 scope 局限**：脚本 API **没有**"枚举/遍历所有聊天流逐个切换"的能力；
  timer 驱动的时间表切换只能作用于**全局 main 状态**（`scope="global"`），
  或对显式写死的 scope。被命令/自动替换设过独立人格的聊天流不受 timer 计划影响
  （用户可用 `/mps status` 查看）。定时切换建议显式 `duration_minutes=0` 并在
  窗口边界主动 `ctx.revert(scope="global")`，避免预设默认时长在窗口内提前把人格
  收回（见 night_weekend 风格写法）。
- **多条规则交叉时刻**（如"周末整天"与"夜间窗口"在周六凌晨/周一凌晨重叠）：
  脚本引擎不定义优先级，由你确定并集中在单一判定函数，文件注释里写明边界取舍，
  让用户可改。
- 切到已处于的人格是**无害但多余**——路由脚本里先 `current_preset()` 判重，
  避免每条消息都触发一次 set_preset 落盘（也避免重置计时）。

### A.3 交付清单与自检（交付前必做）

1. 交付内容 = 每个新人格 1 个 TOML（**放插件根 `preset/`**）+ 路由/触发脚本
   （放 `maips/`，需要时）+ 一段给用户的"怎么验证"说明；
2. **预设名必须与用户 `/mps maisave list` 的实际预设一致**；脚本 `_PRESETS` /
   候选里的每个名字都要真实存在，否则运行期抛 `ValueError`（见第 6 节守则 6）；
3. 脚本健壮性：`ctx.swap` 包 `try/except`；所有触发词判断避开命令消息
   （`ctx.is_command`）；不要在模块顶层做耗时/副作用操作（第 6 节守则 2）；
4. 路由脚本中不要写死用户真实群号/QQ 号作"示例"——用注释占位，让用户填；
5. **清理缓存产物**：交付前删除你测试/编译产生的 `__pycache__` 与 `*.pyc`
   （见第 2 节"交付前清理缓存产物"），确保插件目录树干净；
6. 交付后提示用户验证路径：重启/重载插件（`preset/` 里的预设自动移入数据目录）
   → `/mps script list` 无 ❌ → `/mps maisave list` 能看到导入的预设 →
   触发聊天流实测 → 看插件日志「覆盖 system」行确认覆盖注入生效。
