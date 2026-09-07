# 夺舍麦麦（cateye_mai_personality_swap）

把 bot 当前官方人格（表达方式 / 人格 / 行为风格）保存为预设，按条件 + 概率 + 权重
自动切换或命令手动切换各聊天流的人格；替换状态持久化，重启不丢。预设激活期间
自动忽略官方"临时说话风格"注入。

## 命令

| 命令 | 说明 |
|---|---|
| `/maisave [名称] [时长]` | 保存当前官方人格为预设；名称缺省自动命名 preset1/preset2…。`[时长]` 是该预设被切到时**默认持续多久后自动恢复主人格**（分钟；缺省/`0` = 永久，即切到就一直用，不自动恢复）。保存成功后用**合并转发**回显保存内容（验证 + 防刷屏）；同名覆盖前自动备份。预设文件本身永久存在、不会到期销毁 |
| `/maisave list` 或 `/mps maisave list` | 列出全部预设名称 |
| `/maisave delete <名称>` 或 `/mps delete <名称>`（v1.4.1） | 删除预设：删除前自动备份到 `preset/backup/`；若该预设正被某些聊天流激活，这些流先恢复主人格；该预设的命令级权重一并清除 |
| `/mps maiload <名称>` | 合并转发输出指定预设（供复制手动修改官方人格配置） |
| `/mps swap [名称] [时长]` | 切换当前聊天流人格（"仅触发聊天流替换"关闭时为全局切换）；**不带名称（留空或 `0`）即切回主人格**；`[时长]`（分钟）为本次切换的持续上限，**省略则回退用该预设保存的时长**（`0`=永久一直用）；预设文件本身不受影响、仍可反复切换 |
| `/mps weight <名称> <权重>` | 设置预设权重（持久化，重启保留；与 WebUI 配置页中的预设权重合并生效，命令设置优先；权重填 0 清除命令级覆盖，回落配置页数值） |
| `/mps script [list\|reload]` | 查看自定义脚本加载状态 / 手动热重载 |
| `/mps status` | 查看所有聊天流的替换状态与剩余时长 |

## 数据位置（宿主统一分配的插件数据目录）

宿主按 **manifest 插件 id** 分配数据目录：`<MaiBot根>/data/plugins/<插件id>`。
本插件 id 为 `github.cateye.mai-personality-swap`，因此实际完整路径是：

```
E:\maibot\MaiBot\data\plugins\github.cateye.mai-personality-swap\
├── preset\<预设名称>.toml     预设文件（多行文本，换行原样可读、可手工编辑）
├── preset\backup\             同名覆盖前的自动备份（数量受 backup_limit 限制）
└── personality\<群号或QQ号>.json   每个聊天流的替换状态
    main.json                  全局模式下的替换状态
    weights.json               /mps weight 写入的权重覆盖
```

代码里通过 `ctx.paths.data_dir`（宿主授予）定位该目录，不自行拼接宿主根路径。
状态文件用 `"is_main": true/false` 布尔值标记主人格，避免把"主人格"误存为预设名称。

### 两个 preset 目录（交付暂存 vs 运行数据）

- **插件根目录的 `preset/`（随插件打包，默认空）** = **交付暂存区**：AI 生成的
  新预设放这里，随插件目录一起打包/同步。插件**加载时检查一次**：有 `.toml` 就
  **自动移入数据目录**（同名跳过保留现有、空目录/无文件忽略）。放这里无需手动
  进数据目录。
- **数据目录的 `preset/`（`<MaiBot根>\data\plugins\<id>\preset\`）** = **运行数据**：
  `/maisave` 保存与自动导入都落这里，`/mps maisave list`、路由/切换脚本读的都是
  它。备份在它的 `preset\backup\` 下。

## 自动替换流程

```
入站消息（黑/白名单过滤 → 关键词命中记录） → 条件全部满足？
  （非bot关键词 + bot关键词 + 时段，留空/占位则跳过该项）
  → 概率检查（默认 0.1） → 按权重抽取（主人格默认 0.8 + 各预设权重）
  → 替换生效（按生效时长计时：显式声明优先、否则取预设记录时长；到时自动
    恢复主人格；时长 0=一直用不自动恢复；预设文件不过期，重启后状态照旧）
```

## 配置要点

配置不随插件附带文件：插件声明了配置模型（`config_model`），Runner 在加载时按
模型自动生成并写回 `config.toml`（字段全部有中文说明），后续在 **WebUI 插件配置页**
修改即可。排版顺序：插件开关 → 黑白名单 → 自定义脚本（含"脚本接管"开关）→
替换行为 / 触发条件 / 权重 / 预设 / 注入行为。要点：

- **脚本接管**（默认关闭）：开启后忽略其下方的**自动替换行为**配置项（替换行为/
  触发条件/权重/预设，按默认值处理），仅插件总开关、配置版本号与黑白名单继续
  生效；自动替换行为完全由 `maips/` 脚本设定（用 `ctx.set_swap_params(...)`，
  未设定的项默认留空即不产生自动替换）；脚本引擎自动启用。
- 群/私聊黑名单、白名单（名单为空 = 不过滤；**接管时仍生效**）
- **管理员名单**（v1.3.5 起，**接管时仍生效**）：`/mps maisave`（含 `/maisave`）、
  `weight`、`debug`、`script`、`maiload` 等管理子命令仅限管理员执行。管理员 =
  本地 operator/控制台，或 ① 消息所在群在 `admin_group_ids`（该群任何成员可用），
  ② 发送者 QQ 在 `admin_user_ids`（裸 QQ 号或 `platform:user`）。**两个名单都留空
  时只有本地 operator/控制台能用管理命令**——从 QQ 群想用 `/maisave`，请先把你的
  群号/QQ 填进这两个名单之一。
- 仅触发聊天流替换（默认开；关闭则全局替换）
- 替换期间是否能再次触发对话（默认关）
- 每次替换忽略当前人格，即必定变更（默认关）
- 触发条件：非bot消息关键词 / bot消息关键词（默认 `["default"]` 占位 = 不检查）、时段（可多个，支持跨午夜，留空 = 全天）
- 主人格权重（默认 0.8）与各预设权重（不要求总和为 1）
- 注入行为：
  - **覆盖式注入（唯一注入方式，无开关）**：预设激活期间不再"在 system 之外叠
    加一层人格"，而是直接**改写请求 system 消息里的官方人格/表达/行为段落为预设
    内容**——官方人设从请求中真正消失，替换成全新人设。这样模型眼里只有预设
    人设，不会出现"原人格还在、注入人格被当作提示注入而忽视"的问题。配置触发
    （自动替换）与 maips 脚本触发（`ctx.swap`）一律走此覆盖路径。
    - 覆盖按宿主 zh-CN 模板的固定句切分：replyer 替换身份段与表达段、planner
      替换行为风格段；其它 locale / 自定义 system / 锚点未命中时**自动回退为
      items 尾部追加式**（保证人格仍生效），并在插件日志记 warning；
    - 主人格（无预设激活）时 system 原样保留；
    - 到期 / `revert` / 切回主人格后 system 恢复官方原样，仍可逆、不动官方配置文件。
    - **预设配了 `persona_name`（自称名）时（v1.3.6/1.3.9/1.4.0 增强）**：除 replyer
      身份段自称外，planner 模板外壳里的官方昵称也会一并换成 persona_name（决策
      层眼里的 bot 就叫这个名），并在请求末尾注入"你现在是{persona_name}，上文中
      {官方名}的发言是切换前旧身份说的…"的身份说明——解决切换后 bot 被自己的
      历史发言带偏、继续自称旧名的问题（详见 预设一节 persona_name 与 AGENT.md）。
  - 忽略官方临时说话风格（默认开）：预设激活期间剔除官方"你的说话风格可以尝试"
    消息，避免与预设表达并存。

## 自定义maips脚本（Mai Python Script，类KubeJS）

**脚本目录**：插件目录下的 `maips/` 文件夹（与 KubeJS 的 `kubejs/` 同构）。
每个顶层 `.py` 文件是一个脚本（数量不限、全部加载；`_` 开头的视为工具库不
加载），保存后自动热重载（或 `/mps script reload`，`/mps script list` 看状态）。

```python
from mps_api import on_event

@on_event("message")            # 用户消息事件
def work_mode(ctx):
    if "进入工作模式" in ctx.text:
        ctx.swap("preset1", duration_minutes=120)   # 直接切换人格

@on_event("message")
def quiet_veto(ctx):
    if "安静" in ctx.text:
        return False            # 否决本轮内置自动替换

@on_event("timer")              # 定时事件（间隔见配置）
def nightly(ctx):
    if ctx.hour >= 23 and ctx.current_preset(scope="global") != "sleep_mode":
        ctx.swap("sleep_mode", scope="global")
```

### 五类事件

| 事件 | 触发时机 |
|---|---|
| `message` | 每条用户消息（命令消息也有，`ctx.is_command` 区分） |
| `bot_message` | bot 自己发出的每条消息 |
| `timer` | 定时触发（间隔在配置"自定义脚本"里设置，默认 60 秒） |
| `swap` | 任何人格切换/恢复发生后（自动、命令、脚本都算） |
| `load` | 脚本加载/重载后（适合做一次性的参数初始化） |

### ctx 能力速览

> 速览只列要点；每个方法的**默认参数/返回/异常/示例**见 `AGENT.md` 第 5 节（AI 契约，
> 最完整）。以下是写作脚本时的快速索引：

- **切换**：`ctx.swap(预设名, 时长分钟=None, scope=None)`、`ctx.revert(scope=None)`、
  `ctx.current_preset(scope=None)`（主人格返回 None）。`scope` 缺省 = 事件所属聊天流；
  定时器等无聊天流场景要显式 `scope="global"`。`时长分钟=None`（省略）→ 回退用预设
  文件记录的时长作为持续上限；`0`=永久；预设文件本身不过期、仅作默认值来源。
- **控制内置自动替换**：处理器返回 `False`（否决本轮）；或返回
  `{"probability": .., "weights": {"preset1": 2.0, "main": 1.0}, "exclude_current": ..}`
  （覆盖本轮参数）；或 `ctx.veto()` / `ctx.set_probability()` / `ctx.set_weight()` /
  `ctx.exclude_current_in_round()` 等价。`weights` 键可为主人格 `"main"`。
- **消息精细匹配**：`ctx.message_types` / `ctx.has_type(t)` / `ctx.has_text` /
  `ctx.has_emoji` / `ctx.has_image`；`ctx.matches(pattern, mode)`（pattern 可传列表，
  mode 支持 contains / exact / regex / prefix / suffix）；
  `ctx.emoji_emotions()`（表情包情绪标签，索引缺失自动从消息文本解析）、
  `ctx.match_emoji(词列表, mode="any"|"all")`（any=命中任一即触发 / all=全部命中）。
- **@ 与 bot 身份**（v1.3.7+）：`ctx.bot_user_id` / `ctx.bot_nickname`（bot 自身账号与
  官方昵称）；`ctx.is_at_me`（本条是否 @ 了 bot；排除 bot 自 @ 再比
  `ctx.user_id != ctx.bot_user_id`）；`ctx.at_targets()`（本条被 @ 对象列表，
  含 `user_id` 数字账号）/ `ctx.at_segments`（原始 at 段）。
- **持久 KV**（v1.3.7+，跨热重载/重启保留）：`ctx.kv_get(key, 缺省)` /
  `ctx.kv_set(key, 值)`（bool）/ `ctx.kv_del(key)`（bool）/ `ctx.kv_keys()`。
  计数累计（攒点数、冷却、挂起标记）用它；键加脚本前缀（所有脚本共享一张表）。
- **接管参数**：`ctx.set_swap_params(probability=.., preset_weights={..}, main_weight=..,
  non_bot_keywords=[..], bot_keywords=[..], time_windows=[..], per_stream=..,
  reroll_during_swap=.., exclude_current=..)` 在 `load` 事件调用一次；`ctx.get_swap_params()`
  读取当前生效快照。
- **LLM 调用**（协程，需 `async def`）：`await ctx.llm_generate(prompt, task=..)`、
  `await ctx.llm_json(prompt, task=..)`（自动解析 JSON）、`await ctx.emotion_score(...)`
  （0~10 情绪分）、`await ctx.recent_context(n)`（最近聊天文本）。`task` 是模型任务名
  （`replyer` / `planner` / `utils` 等）。
- **其它**：`await ctx.send_text(...)`（发文本回聊天流，仅 message/bot_message）、
  `ctx.log(msg)`（写插件日志）、`ctx.hour/minute/now/group_id/user_id/text/segments`。
- **debug 门控**：`ctx.debug_enabled`（`/mps debug true|false` 控制）；自检类脚本应据此
  在 debug 关闭时静默。

### 接管模式（脚本接管）

配置页「脚本接管」开启后，配置里的**自动替换行为**（替换概率/触发条件/权重/预设）
全部失效、由脚本在 `load` 事件用 `ctx.set_swap_params(...)` 接管（未设定项默认
"留空"=不产生自动替换）；黑白名单与插件总开关仍生效；**注入方式（覆盖式 system
改写）不受接管影响**。

### 文档与契约

- `README.md`（本文件）：命令/配置/脚本入门已合并于此（原 maips/README.md 内容）
- `AGENT.md`（插件根目录）：面向 AI 编码助手的**完整自足契约**——事件表、ctx 方法
  签名、set_swap_params 字段表与默认值、编写守则、可直接落盘的完整配方
- `自检指南.md`（插件根目录）：maips 全方法自检步骤（配合 `maips/maips_selfcheck.py`）
- 完整 API 与实现细节见 [开发文档.md](开发文档.md) "脚本系统"章节

> ⚠️ 脚本与 bot 本体同等信任级别（进程内完整 Python 权限），只运行自己编写或
> 审查过的脚本。

## 用 AI 生成新人格（建议流程）

想给 bot 加"只在某些群/某些私聊/某种场景下出现的其它人格"？建议按下面流程，
让 AI 参考你的**主人格**派生新人格（会生成预设 + 路由脚本，见 `AGENT.md` 附录 A
的 AI 侧要求）。

### 第 0 步：保存你的主人格（必做，一次即可）

在聊天里对麦麦发送：

```
/maisave 主人格 0
```

- `主人格` = 预设名（可换成任意名）；`0` = **每次切到该人格后不自动恢复主人格**
  （一直用）。预设文件本身**永久存在**，不会因设了别的时长而被销毁/过期——文件里
  记录的时长只是"将来切换到它时默认持续多久"（到点自动恢复主人格后再切回来仍可用）。
  设不设 `0` 都只是控制"切到它之后的行为"，不控制预设的存续。
- 保存后 bot 会用合并转发回显保存内容 = 你官方 `[personality]` 三件套的副本
- 验证：`/mps maisave list` 应能看到该预设

> 💡 **主人格副本不是系统必需文件，用完可删/可移**
> 这份 `主人格` 预设只是给 AI 当"参考基准"用的**快照**——bot 的主人格表现始终
> 来自官方 `bot_config.toml` 配置本身，与这份副本文件无关；**不激活它就不会起
> 任何作用**。AI 参考完（或你决定不再用它路由/切换）后：
> - 可**直接删除**：`preset/主人格.toml`（或聊天里没有删除命令时手动删文件）；
> - 或**移动到备份**：挪到 `preset/backup/`（同名覆盖自动备份也在那里）；
> 删除/移动后 `/mps maisave list` 不再列出它，但 bot 官方人格表现、其它预设、
> 路由脚本都不受影响。若之后又想让 AI 参考，重新 `/maisave 主人格 0` 即可。

### 情况一：麦麦部署在本地（AI 能直接读插件目录）

1. 告诉 AI："参考我保存的 `主人格` 预设，帮我做 XXX 人格，路由到聊天流 Y /
   按 Z 方式触发"，并指明插件目录路径；
2. AI 会自行读取插件目录（含 `AGENT.md`、你的主人格预设文件、`maips/` 示例），
   生成新预设（**放进插件根目录的 `preset/`**）与 maips 脚本（放进 `maips/`）；
3. 你只需确认文件已写入：maips 脚本保存即热重载；`preset/` 里的预设会在
   **下次加载插件时自动移入数据目录**（无需手动操作）。

### 情况二：麦麦部署在服务器（AI 读不到麦麦目录）

1. 在服务器上发 `/mps maiload 主人格`——bot 会用合并转发把主人格内容发给你
   （或直接打开数据目录 `preset/主人格.toml` 复制内容）；
2. 把这份主人格内容**粘贴给 AI**，说明想要的新人格与触发方式；
3. AI 会交付给你：新预设文件内容 + 路由/触发脚本内容 + **明确的放置位置**：
   - 预设文件 → **放进插件目录根部的 `preset/` 文件夹**
     （`<插件根>/preset/<新名>.toml`），随插件目录一起打包/同步到服务器；
     插件加载时自动移入数据目录，**无需手动进数据目录放文件**；
   - 路由/触发脚本 → 放进插件目录 `maips/<脚本名>.py`；
4. 放好重启/重载插件后验证：`/mps script list` 无 ❌ → `/mps maisave list`
   能看到自动导入的预设 → 在目标聊天流实测 → 插件日志出现「覆盖 system」行即生效。

### 给 AI 提需求时的建议措辞

> "参考我的主人格 `主人格`，做一个【高冷/元气/温柔/毒舌…】的分身人格，命名
> `p_xxx`，路由到群【群号】/私聊【QQ号】；再做一个每天【23:00】触发的【夜猫
> 人格】。"

AI 会按 `AGENT.md` 附录 A 派生预设三件套（保留主人格腔调/禁忌，只换风格维度）
并生成对应 maips 脚本。

## 与其他注入类插件的兼容

人格注入统一在 `before_model_request` / planner `before_request` 以改写 items
首条 system 的方式完成（不再占用 replyer `extra_prompt`，也不往 items 尾部追加
易被模型当作提示注入的"用户消息"），因此与其它使用 `extra_prompt` 协作式追加的
插件（如 `autonomous_planning_plugin`）天然共存、互不覆盖；system 被其它插件
改写导致锚点未命中时自动回退 items 尾部追加。详见 [开发文档.md](开发文档.md)。
