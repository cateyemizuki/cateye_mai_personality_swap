# 夺舍麦麦（cateye_mai_personality_swap）

夺舍麦麦是 MaiBot 的人格预设与切换插件。它把官方 `bot_config.toml` 的
`[personality]` 三项（`personality` / `reply_style` / `behavior_style`）保存成
TOML 预设，再按聊天流手动切换，或按条件、概率、权重自动切换。预设激活期间，
插件在 LLM 请求前分别改写 replyer / planner 首条 system 消息里的官方人格、
表达、行为段为预设内容；锚点未命中时回退为 items 尾部追加。切换状态落盘，
重启后仍然有效。

适合想给不同群或私聊配置不同人格的 MaiBot 使用者，也适合用 Python 脚本自定义
路由与触发条件的脚本作者。插件不注册 LLM 工具，模型无法调用本插件；可触达的
入口只有斜杠命令、Hook 和 `maips/` 脚本。

## 运行条件

- MaiBot 宿主需提供 `ctx.paths.data_dir`。取不到时插件抛 `RuntimeError`，拒绝
  读写预设与状态（`plugin.py:613-628`）。
- 预设读取使用 Python 标准库 `tomllib`，需要 Python 3.11+；写入依赖宿主环境
  自带的 `tomlkit`（`preset_store.py:5-7,17,27`）。manifest 的 `dependencies`
  为空，没有额外 pip 依赖声明。
- 代码使用 `maibot_sdk` 的命令/Hook/配置基类与 pydantic v2 的 `field_validator`
  （`plugin.py:56-58`），这些由宿主环境提供。
- 加载入口为模块级 `create_plugin()`，`config_model` 为
  `MaiPersonalitySwapConfig`（`plugin.py:472-493,2255-2258`）。插件不随包附带
  `config.toml`；配置字段带中文 label/hint 与 UI 元数据，在 WebUI 插件配置页修改。
- manifest 声明的兼容区间：`host_application` 1.0.0 ~ 1.99.99，`sdk`
  2.0.0 ~ 2.99.99；插件版本 1.4.7，许可证 MIT。
- 把插件目录安装到宿主插件目录后由宿主加载。插件根目录的 `maips/`、`preset/`
  随插件保留；运行数据写入宿主分配的数据目录。

## 核心概念

- **主人格**：官方 `bot_config.toml` 的 `[personality]` 三项。没有预设激活时，
  插件不改写任何请求，bot 完全按官方配置表现。
- **预设**：一份 TOML 文件，包含表达方式、人格约束、行为风格，以及可选的
  `persona_name` 和默认切换时长。预设被激活到某个聊天流后，才参与注入。
- **聊天流**：群聊或私聊。默认每个聊天流有独立状态（`swap.per_stream=true`），
  状态键是群号或 QQ 号；关闭后手动命令与脚本默认使用全局 `main` 状态，自动替换
  在该模式下不会生效（见「自动切换」）。
- **覆盖式注入**：改写请求首条 system 消息中的官方人格/表达/行为段，而不是
  在 system 之外追加一层。锚点未命中时回退为 items 尾部追加，保证预设仍生效。
- **两种“时长”**：预设文件里的 `duration_minutes` 是“将来切到它时的默认持续
  时长”；单次切换还可以显式指定时长。预设文件本身永不过期，随时可以再切。

## 三步上手

只想给某个群或某个私聊换一套人格时，不需要接触自动切换、权重或脚本：准备预设、
配置管理员、在目标聊天流发一条指令即可。

### 第 1 步：准备人格预设

**从当前官方人格派生**：在聊天里发（需要管理员，见第 2 步）

```
/maisave work_mode 0
```

这会把官方 `personality` / `reply_style` / `behavior_style` 保存为
`work_mode` 预设，并用合并转发回显保存内容。`0` 是该预设的默认切换时长，
表示切到它后一直使用、不自动恢复主人格；它不控制预设文件的存续。

**手写或让 AI 生成**：把 `work_mode.toml` 放进插件根目录的 `preset/`。插件
下次加载时会把其中的 `.toml` **复制**到数据目录（源文件保留；数据目录已有
同名预设时跳过该文件）。也可以直接放进数据目录的 `preset/`。格式见下一节。

验证：

```
/mps list
```

`/mps list` 是公开命令，能看到预设名即表示文件已在数据目录；文件能否正常加载
以 `/mps swap` 的结果为准（损坏的 TOML 仍可能出现在列表里）。

> `/maisave` 保存的是官方三件套，不包含 `persona_name`。对同名预设再次执行
> `/maisave` 会用官方当前内容覆盖整个文件（旧文件先备份到 `preset/backup/`），
> 之前手写的 `persona_name` 不会保留。需要保留自称名时，换一个预设名，或覆盖
> 后重新补上该字段。

### 第 2 步：配置管理员

管理命令按以下四类来源判定，满足其一即可：

| 来源 | 条件 | 说明 |
|---|---|---|
| 本地 operator / 控制台 | 宿主传入 `is_local_operator=true` | WebUI、终端等本地入口 |
| 宿主管理员名单 | `plugin.permission` | 插件通过 `ctx.config.get("plugin.permission")` 读取，与插件配置名单合并；读取失败时降级为仅插件配置 |
| 管理员 QQ | `filter.admin_user_ids` | 填 `10001` 或 `qq:10001` 均可，按纯数字 ID 归一；非数字条目不参与匹配 |
| 管理员群 | `filter.admin_group_ids` | 名单内群号的**任何成员**都能执行管理命令 |

在 WebUI 插件配置页「黑白名单与管理员」中填写后两者。两个插件名单都为空、
宿主名单也为空时，只有本地 operator / 控制台能执行管理命令，聊天里发管理命令
会被拒绝并提示。只读的 `/mps status`、`/mps list` 不受管理员限制。

> 管理员群等于该群任何成员都能用 `/mps maisave` 回显官方人格全文、用
> `/mps maiload` 把预设内容发回群里。只把自己的可信群填入。

### 第 3 步：把人格路由到聊天流

在目标聊天流里发送：

```
/mps swap work_mode        # 切到 work_mode，时长用预设记录的默认值
/mps swap work_mode 120    # 只持续 120 分钟，到点自动恢复主人格
/mps swap                  # 不带名称，切回主人格
/mps swap 0                # 同上；首参 0 表示切回主人格
/mps status                # 查看当前流状态、剩余时长、生效权重
```

- `swap.per_stream=true`（默认）时，只影响发指令的聊天流；关闭后 `/mps swap`
  写入全局 `main`，所有聊天流共用该手动状态。自动替换在关闭状态下不会生效
  （见「自动切换」）。
- 切换状态持久化，重启后仍然记得哪个群/私聊用的是哪个人格。
- 撤销：在同一个聊天流再发 `/mps swap`（或 `/mps swap 0`）。
- 名为 `0` 的预设无法用 `/mps swap` 选中，因为首参 `0` 被当作“切回主人格”；
  给预设换一个名字。要把时长设为永久，应写 `/mps swap work_mode 0`。

## 预设文件

预设存放在数据目录的 `preset/<名字>.toml`。文件名即预设名；文件里的 `name`
仅作记录，读取时以文件名为权威名（`preset_store.py:141-165`）。正文用 TOML
多行基本字符串保存，换行原样保留，可以直接手工编辑。

```toml
name = "work_mode"              # 仅作记录；权威名是文件名
persona_name = "小林"            # 可选：该人格下 bot 的自称
expression = """说话简短、直接，先给结论再补一句理由。"""
persona = """你是专注工作的助手。不闲聊，不编造事实。"""
behavior = """优先处理任务相关话题；与工作无关的闲聊简短回应。"""
duration_minutes = 0            # 0 = 一直用；>0 = 切到它后默认持续 N 分钟
created_at = "2026-09-06T19:00:00+08:00"  # 可选，仅记录
```

| 字段 | 默认 | 含义 |
|---|---|---|
| `name` | 空 | 仅作记录；实际以文件名为准 |
| `persona_name` | 空 | 可选。该人格下 bot 的自称名；空 = 沿用官方 `bot.nickname` |
| `expression` | 空 | 表达方式（怎么说），注入 replyer 的表达段 |
| `persona` | 空 | 人格约束（我是谁、红线），注入 replyer 的身份段 |
| `behavior` | 空 | 行为风格（何时说、参与姿态），注入 planner 的行为段 |
| `duration_minutes` | `0` | 切到该预设且未显式指定时长时的默认持续分钟数；`0` = 一直用 |
| `created_at` | 空 | 创建时间，仅作记录；`/maisave` 会写入 ISO 时间 |

- 预设名由 `^[\w\u4e00-\u9fff\-]{1,64}$` 校验：Unicode 字母、数字、下划线、
  中文和连字符，1~64 个字符；不能包含空格、斜杠、点号。非法名不会被复制导入，
  命令也会拒绝保存。
- 文件缺失或 TOML 解析失败时，读取按“预设不存在”处理，切换会提示预设不存在。
- `persona_name` 不只看 replyer 的名字行。配置后：
  - replyer system 的身份名字行改为 `你的名字是{persona_name}。`；
  - planner system 中**已知外壳固定句式**里的官方昵称会换成 `persona_name`
    （行为段 marker、“关注…与用户的对话…”、“你不是…本人…”、“帮…搜集信息”、
    “当你判断…应该正式发言时调用 reply”、“以上…的行为风格…”）；句式之外的
    段落保持官方昵称；
  - replyer 在请求 items 末尾追加一条「当前身份提醒」，planner 则把宿主末位
    “你需要输出对…”提醒改写为同一文案；找不到该提醒时回退追加。
  - 该提醒需要 `persona_name` 和官方昵称同时非空才会注入。官方昵称读不到时，
    replyer 名字行仍用 `persona_name`，但身份提醒不注入。
- `duration_minutes` 只影响“这次切到它后持续多久”，不会让预设文件过期。
  显式时长（`/mps swap work_mode 120` 或脚本 `duration_minutes=120`）优先于
  文件值；`0` 表示永久，不自动恢复。

### 插件根 `preset/` 与数据目录 `preset/`

- **插件根 `preset/`**：交付暂存区。随插件打包，默认只有 `.gitkeep`。插件每次
  加载（`on_load`）时扫描其中的 `*.toml`，名称合法且数据目录没有同名文件时
  用 `shutil.copy2` 复制过去；源文件保留，同名跳过（`plugin.py:630-675`）。
- **数据目录 `preset/`**：运行数据。`/maisave` 保存、`/mps list` 读取、切换与
  注入都使用这里；备份在它的 `backup/` 子目录。

因为导入是“同名跳过”，插件根 `preset/` 里放一个与数据目录同名的文件不会覆盖
已有预设。要更新已有预设，请删除或改名数据目录中的旧文件，或换一个新预设名。

## 命令

权限列：**公开** = 任何人可发；**管理员** = 本地 operator/控制台，或命中上述
管理员 QQ / 管理员群 / 宿主管理员名单。

| 命令 | 权限 | 说明 |
|---|---|---|
| `/mps` | 公开 | 不带子命令时返回用法与权限提示 |
| `/mps swap [名称] [时长]` | 管理员 | 在指令所在聊天流（`per_stream=false` 时写入全局 `main`）切换人格；名称留空或 `0` 切回主人格；时长单位为分钟，必须是 ≥0 整数；省略时长时使用预设记录的 `duration_minutes`；显式 `0` 表示永久 |
| `/mps status` | 公开 | 查看替换状态。管理员看到全部非主人格聊天流的明细；非管理员只看到当前聊天流，另有“另有 N 个聊天流非主人格”的计数。同时显示预设总数、替换范围、主人格权重、生效预设权重、debug 状态 |
| `/mps list` | 公开 | 列出全部预设名；等价写法 `/mps maisave list`、`/maisave list` |
| `/mps maisave [名称] [时长]` / `/maisave [名称] [时长]` | 管理员 | 读取官方人格三项保存为预设；名称缺省时自动取 `preset1`、`preset2`…；时长缺省/`0` = 永久；同名覆盖前备份；保存后用合并转发回显内容 |
| `/mps delete <名称>` / `/mps maisave delete <名称>` / `/maisave delete <名称>` | 管理员 | 删除预设：先备份到 `preset/backup/`，把正在激活该预设的聊天流恢复主人格，并清除该预设的命令级权重 |
| `/mps maiload <名称>` | 管理员 | 用合并转发输出指定预设的三段内容，便于复制回官方人格配置 |
| `/mps weight <名称> <权重>` | 管理员 | 设置预设权重并持久化；正数生效，`0` 清除命令级覆盖（要求名称合法且预设存在）；负数、非数字、`inf`/`nan` 被拒绝 |
| `/mps debug [true\|false]` | 管理员 | 无参数查询当前状态；`true`/`1`/`on`/`开` 开启，`false`/`0`/`off`/`关` 关闭，状态持久化 |
| `/mps script [list\|reload]` | 管理员 | 无参数等同 `list`，查看脚本加载状态、处理器数与脱敏错误摘要；`reload` 强制扫描重载。脚本宿主未启用时返回“脚本功能未启用” |

补充：

- `/mps save` 是 `/mps maisave` 的别名。
- `/maisave` 是独立命令，整个参数段都按 `[名称] [时长]` 解析；仅当首词恰好是
  `list` 或 `delete` 时走列出/删除分支。
- `/mps swap` 从 v1.4.3 起属于管理命令；普通成员即使能发消息也不能切人格。
- `debug` 开启后，每次人格切换/恢复会向触发该变换的聊天流发一条
  `[夺舍麦麦] …` 文本；`timer` 等无法定位触发流的变换只记日志，不发消息。
- 命令结果由插件主动发回：不超过 8 行且不超过 500 字符时直接发文本；更长时用
  合并转发（正文每 40 行一块）；转发失败回退为纯文本（`plugin.py:2079-2134`）。

其他常用写法：

```
/mps weight work_mode 2      # 给 work_mode 设权重 2
/mps debug true              # 开启 debug 通知
/mps script list             # 查看脚本加载状态
/mps delete work_mode        # 删除预设（先备份，激活流恢复主人格）
/mps maiload work_mode       # 合并转发预设内容
```

## 自动切换

自动替换只在**非通知、非命令**的入站消息上评估。流程如下（`plugin.py:1337-1415`）：

```
入站消息
  → 群/私聊黑白名单（命中则跳过）
  → 非主人格且 reroll_during_swap=false → 跳过
  → 脚本 veto → 跳过
  → 时段条件
  → 关键词条件（配置了才检查，命中记录 TTL 600 秒，按聊天流隔离）
  → 概率检查（swap.probability，默认 0.1）
  → 权重抽取（主人格 weights.main + 实际存在的预设）
  → 抽中主人格：恢复主人格；抽中预设：切换，时长取预设默认值
```

> **`swap.per_stream=false`（且脚本接管时 `per_stream` 也为 false）时自动替换
> 不会生效**：自动路径固定用消息的群号/QQ 号（映射缺失时为 `stream_id`）作为
> 状态键写入（`plugin.py:1349,1409-1415`），而注入读取 `main`
> （`plugin.py:830-837`），非管理员的 `/mps status` 也按 `main` 查询
> （`plugin.py:1932`）；关键词命中也记录在 `main`，自动路径却按群号/QQ 号检查
> （`plugin.py:1296,1324,1362-1367`）。需要自动替换时保持
> `swap.per_stream=true`。

**默认配置的实际效果**：`condition.non_bot_keywords` 和 `bot_keywords` 默认是
占位值 `["default"]`，会被当作“不检查”；`condition.time_windows` 为空表示
全天；`swap.probability` 为 `0.1`；`weights.presets` 为空。此时抽取池里只有
主人格（权重 0.8），即使概率命中，抽中的也是主人格，不会换人格。要让自动切换
真正生效，必须给预设配置权重（配置页「预设权重表」或 `/mps weight`）。

- **关键词**：只有配置了非空、非 `default` 的关键词才会检查。普通用户消息命中
  任一 `non_bot_keywords` 时记录；bot 发送成功的消息命中任一 `bot_keywords`
  时记录。记录 600 秒内有效，过期视为未命中（`swap_engine.py:25,112-170`）。
- **时段**：`HH:MM-HH:MM`，可配置多个，支持跨午夜；空列表 = 全天。窗口起点包含、
  终点不包含（`swap_engine.py:83-100`）。
- **概率**：条件全部满足后按此概率决定是否抽取；`0` 表示不自动替换。
- **权重**：主人格 `weights.main` 默认 `0.8`，大于 0 才入池；预设权重表只对
  实际存在的预设文件生效，非正数和非有限值跳过，不要求总和为 1。
- **`swap.exclude_current`**：抽取池剔除当前人格。当前是主人格时剔除主人格，
  当前是预设时剔除该预设；如果剔除后池为空，本轮不抽。
- **`swap.reroll_during_swap`**：默认关闭，非主人格期间不再自动抽取；开启后
  允许在替换期间再次抽签。
- **`swap.per_stream`**：默认开启，按聊天流独立。关闭后，手动 `/mps swap` 与
  脚本的默认作用域使用全局 `main` 状态，`/mps status` 显示“全局”，手动/脚本的
  状态文件为 `main.json`；自动替换在该模式下不会生效（见上）。
- **自动切换的时长**：自动路径不显式传时长，因此使用被抽中预设的
  `duration_minutes`；`0` 表示一直用，正数表示到点自动恢复主人格。
- **预设缺失**：抽中的预设文件被删除或损坏时，本轮记 warning 并跳过，不会
  改变当前状态（`plugin.py:1405-1407`）。

### 黑白名单的作用范围

`filter.group_list` / `filter.private_list` 为空时不过滤。名单非空时：

- 自动替换跳过被拦截的聊天流；
- 被拦截聊天流的入站/出站消息不派发给任何 `maips` 脚本（脚本看不到这些消息）；
- 脚本 `ctx.send_text` 向被拦截聊天流发送时静默丢弃；
- 脚本 `ctx.swap` / `ctx.revert` 的黑名单闸门按**事件发送者**的群号/QQ 号判定，
  不看 `scope` 目标：发送者所在流被拦截时静默返回；`scope="global"`（内部键
  `main`）或 `timer` 等没有发送者上下文的调用不检查名单，显式 `scope` 指向另一个
  被拦截的流仍会执行（`plugin.py:924-935`）；
- 手动命令不受名单影响；已经激活的预设也不会因为名单变化而停止注入。

### 状态持久化

每个聊天流一个状态文件，位于数据目录的 `personality/`：

```json
{"is_main": false, "preset": "work_mode", "started_at": 1730000000.0,
 "duration_minutes": 60, "expires_at": 1730003600.0, "source": "command"}
```

- 主人格写 `{"is_main": true}`，不把“主人格”当成预设名，避免和同名预设混淆。
- 到期判定是惰性的：任何读取都会检查 `expires_at`，到期即回写主人格，不需要
  定时器；重启后同样正确（`swap_state.py:123-158`）。
- 状态写入使用同目录临时文件 + `os.replace` 原子替换；写盘失败只记日志，不向
  hook 抛异常。此时该次切换不会写入磁盘，重启后按磁盘上已有的状态恢复
  （没有旧状态时为主人格；`swap_state.py:200-236`）。
- 群号/QQ 号映射缺失时会退回用 `stream_id` 作为状态键，保证功能可用（
  `plugin.py:778-788`）。

## 覆盖式注入

覆盖改写本身只动**首条 `SystemMessageItem` 的第一个文本 part**，其余 item/part
原样透传；此外会按需剔除官方临时说话风格消息、在末尾追加身份提醒或回退块。
两条通道分别处理：

| Hook | 模式/顺序 | 动作 |
|---|---|---|
| `maisaka.replyer.before_model_request` | BLOCKING / NORMAL | 剔除官方临时说话风格；改写首条 system 的身份段与表达段为预设 `persona` / `expression`；配置 `persona_name` 且官方昵称可用时追加当前身份提醒 |
| `maisaka.planner.before_request` | BLOCKING / LATE | 改写首条 system 的行为段为预设 `behavior`；配置 `persona_name` 且官方昵称可用时人格化已知外壳句式，并改写或追加当前身份提醒 |
| `maisaka.replyer.before_request` | BLOCKING / LATE | 保留注册但恒返回 `continue`，不再向 `extra_prompt` 追加内容 |

### replyer 的锚点与回退

`injectors/system_replace.py` 按宿主 zh-CN 模板的固定句切分：

- 锚点 A：`现在请你读读之前的聊天记录`
- 锚点 B：`你可以参考【回复信息参考】中的信息`

锚点 A 之前的文本被视为官方身份段（名字行 + 人格 + 情绪尾巴），整段重建为
名字行（拿得到名字时，`你的名字是{名字}。`）+ 预设 `persona`；A 行保留；
A 行尾到 B 之间的官方 `reply_style` 段替换为预设 `expression`；B 起的群聊/
私聊注意块、输出格式指令原样保留。

删除锚点之前的文本前会先做健全性检查：前缀包含锚点 B、超过 4000 字符，或非空
行数超过 60，就判定“不是官方身份段”（例如宿主在锚点前新增了安全规则），
放弃覆盖并回退追加，绝不静默删除非身份内容（`injectors/system_replace.py:53-106`）。

### planner 的锚点与回退

- 行为段从 `{官方昵称}的行为风格：` 开始，到第一个 `\n以上` 结束；这一段替换为
  预设 `behavior`，`\n以上` 起的收尾句保留。
- 官方昵称读不到时退回裸标记 `的行为风格：` 定位。
- 配置 `persona_name` 且官方昵称至少 2 个字符、且与 `persona_name` 不同时，只
  替换 `PLANNER_SHELL_SENTENCES` 列出的已知外壳句式中的官方昵称；单字昵称不做
  外壳替换，避免误改其它文本。句式外的段落（如安全/合规指令）保持官方昵称。
- 宿主模板改版导致句式未命中时，该处外壳保持官方昵称，行为段替换不受影响。

### 锚点未命中时

任一通道的锚点缺失、结构不符或改写异常，都会回退为在 items 末尾追加一条
`UserMessageItem`，内容由「表达方式要求」「人格约束」「行为风格提示」「当前身份
提醒」等块拼成。追加项的 `item_id` 为 `mps-<uuid>`，`logical_turn_id` 继承
末条消息。这样即使宿主模板变化，预设仍然生效；插件日志会为每个通道记录一次
“覆盖式注入锚点未命中，已回退为 items 尾部追加式注入”的 warning。

主人格（没有预设激活）时 system 原样保留；到期、`/mps swap` 切回或脚本
`ctx.revert()` 后，请求恢复官方原样。插件不修改官方 `bot_config.toml`。

### 官方临时说话风格

预设激活且 `compat.suppress_official_temp_style=true`（默认）时，插件从请求
items 中整条剔除满足两个条件的 `UserMessageItem`：

1. `strip()` 后以 `你的说话风格可以尝试` 开头；
2. 整条长度不超过 200 字符。

这样只剔除整条以该前缀开头的短消息；用户引用或复读该文案拼成的长消息不会
命中（`injectors/preset_blocks.py:70-86`）。

### 与其它注入类插件的兼容

插件不占用 replyer 的 `extra_prompt`，人格注入统一在
`before_model_request` / planner `before_request` 中改写首条 system。因此与使用
`extra_prompt` 协作式追加的插件互不覆盖；如果其它插件也改写了 system，导致
本插件锚点未命中，会自动回退为 items 尾部追加。

## 配置

插件通过 `config_model` 声明配置，不随包附带 `config.toml`；字段带中文说明与
UI 元数据，在 WebUI 插件配置页修改。字段与默认值如下：

| 配置项 | 默认 | 说明 |
|---|---|---|
| `plugin.enabled` | `true` | 插件总开关。关闭后命令返回“夺舍麦麦插件未启用”，注入与自动替换停止，消息/出站脚本派发提前返回 |
| `plugin.config_version` | `"1.4.7"` | 配置版本，UI 中隐藏且禁用；代码只接收字段，不做版本校验或迁移 |
| `filter.group_list` | `[]` | 群号名单；空名单不过滤 |
| `filter.group_list_mode` | `"blacklist"` | `whitelist` = 仅名单内群生效；`blacklist` = 名单内群不生效 |
| `filter.private_list` | `[]` | 私聊 QQ 号名单；空名单不过滤 |
| `filter.private_list_mode` | `"blacklist"` | 语义同群名单模式 |
| `filter.admin_user_ids` | `[]` | 管理员 QQ 名单；与宿主 `plugin.permission` 合并去重 |
| `filter.admin_group_ids` | `[]` | 管理员群名单；群内任何成员视为管理员；留空不启用 |
| `script.takeover` | `false` | 脚本接管自动替换行为；开启后 `swap` / `condition` / `weights` 配置失效，详见下文 |
| `script.enabled` | `true` | 是否创建脚本宿主；脚本接管开启时本项被忽略，引擎强制启用 |
| `script.tick_interval_sec` | `60` | `timer` 事件间隔，同时用于热重载检查；最小 10 秒 |
| `script.auto_reload` | `true` | 每个 tick 按 mtime+size 检查并重载变化脚本；关闭后用 `/mps script reload` |
| `script.load_underscore_scripts` | `false` | 是否加载 `maips/_*.py`；默认只把下划线文件当工具库 |
| `swap.per_stream` | `true` | `true` = 每个聊天流独立状态；`false` = 手动命令与脚本默认作用域使用全局 `main` 状态；自动替换在该模式下不生效 |
| `swap.reroll_during_swap` | `false` | 非主人格期间是否允许再次自动抽取 |
| `swap.exclude_current` | `false` | 抽取时剔除当前人格，实现“必定变更” |
| `swap.probability` | `0.1` | 条件全部满足后的自动替换概率；`0` 表示不自动替换 |
| `condition.non_bot_keywords` | `["default"]` | 非 bot 消息关键词；空或仅 `default` 占位 = 不检查 |
| `condition.bot_keywords` | `["default"]` | bot 消息关键词；空或仅 `default` 占位 = 不检查 |
| `condition.time_windows` | `[]` | `HH:MM-HH:MM` 时段列表；空 = 全天；支持跨午夜 |
| `weights.main` | `0.8` | 主人格在抽取池中的权重；大于 0 才入池 |
| `weights.presets` | `"{}"` | 预设权重表，JSON 文本；也支持一行一条 `work_mode=1.5`；非正/非有限权重跳过；与 `/mps weight` 合并且命令优先 |
| `preset.backup_limit` | `5` | 同名覆盖保存与删除时保留的备份数；`0` 表示不裁剪 |
| `compat.suppress_official_temp_style` | `true` | 预设激活期间剔除官方临时说话风格消息；脚本接管时恒为 `true` |

补充：

- `weights.presets` 是文本字段而不是对象控件，因为 WebUI 对对象类型字段会把
  默认值渲染成 `[object Object]`。旧配置里遗留的 TOML 表由字段校验器序列化为
  JSON 文本，值不丢。
- 配置更新（`on_config_update`，`config_scope="self"`）时，`preset.backup_limit`
  通过 `PresetStore.set_backup_limit()` 立即应用，预设权重表重新解析；其余字段
  按后续读取生效（`plugin.py:593-607`）。
- **脚本接管**开启后：`swap`、`condition`、`weights` 配置按默认值处理，
  `script.enabled` 被忽略，`compat.suppress_official_temp_style` 强制为 `true`；
  `filter` 与 `plugin.enabled` 继续生效；注入方式仍是覆盖式，不受接管影响。
- 配置页把 `[preset]` 标为“脚本接管时忽略”，但 `preset.backup_limit` 在实现中
  始终读取配置值，接管开启时也不会回落到默认 5（`plugin.py:555,604,694`）。

## maips 自定义脚本

脚本目录是插件目录下的 `maips/`。每个顶层 `.py` 文件是一个脚本，数量不限，
全部加载；`_` 开头的文件默认只当工具库，不执行。脚本保存后，在
`script.auto_reload=true`（默认）时每个 tick 按 mtime+size 自动热重载，也可以
用 `/mps script reload` 强制重载；`/mps script list` 查看加载状态、处理器数和
脱敏后的错误摘要。脚本目录不存在时扫描前会自动创建。

单个处理器抛异常只记日志并跳过该次调用，不影响其它脚本和 bot 主链路；文件顶层
语法错误则整个文件不生效，`/mps script list` 会显示错误。

### 五类事件

| 事件 | 触发时机 | 关键字段 |
|---|---|---|
| `message` | 每条非通知的入站消息；命令消息也会派发，用 `ctx.is_command` 区分 | `text` / `session_id` / `scope_key` / `group_id` / `user_id` / `is_command` / `segments` |
| `bot_message` | bot 每条发送成功的消息 | `text` / `session_id` / `scope_key` / `segments` |
| `timer` | 按 `script.tick_interval_sec` 周期触发 | `now` / `hour` / `minute`；没有默认作用域 |
| `swap` | 任何来源的人格切换/恢复发生后（auto / command / script） | `scope_key` / `preset`（`None` = 主人格）/ `source` / `duration_minutes` |
| `load` | 脚本加载或重载后 | 无事件数据 |

处理函数可以同步或 `async def`；同一事件可以在多个脚本中注册，初始加载按文件名
排序、文件内按定义顺序串行执行。单个文件热重载后，它的处理器会移到该事件队列
末尾，不要依赖跨脚本的精确先后关系。

### 最小示例

```python
from mps_api import on_event


@on_event("message")
def route_by_group(ctx):
    if ctx.is_command or ctx.is_notify:
        return
    if ctx.group_id == "123456" and ctx.current_preset() != "work_mode":
        ctx.swap("work_mode", duration_minutes=0)  # work_mode 需已存在


@on_event("message")
def quiet_veto(ctx):
    if "安静" in ctx.text:
        return False


@on_event("timer")
def nightly(ctx):
    if ctx.hour >= 23 and ctx.current_preset(scope="global") != "sleep_mode":
        ctx.swap("sleep_mode", scope="global")
```

`maips/example.py` 里还有按群/私聊路由、表情包匹配、LLM 评分、KV 计数等可直接
改写使用的片段。

### ctx 能力速览

- **切换**：`ctx.swap(preset, duration_minutes=None, scope=None)`、
  `ctx.revert(scope=None)`、`ctx.current_preset(scope=None)`。`scope` 缺省为事件
  所属聊天流；`timer` / `load` 等没有默认作用域的事件必须显式传
  `scope="global"` 或写死的群号/QQ。`duration_minutes=None` 回退到预设文件记录
  的时长，`0` 表示永久。预设不存在时抛 `ValueError`。黑白名单闸门看事件发送者的
  群号/QQ 号，不看 `scope` 目标：发送者所在流被拦截时静默返回；显式切到另一个
  被拦截的流仍会执行（`plugin.py:924-935`）。
- **控制本轮自动替换**：处理器返回 `False` 否决；返回
  `{"probability": ..., "weights": {...}, "exclude_current": ...}` 覆盖本轮参数；
  或调用 `ctx.veto()`、`ctx.set_probability()`、`ctx.set_weight()`、
  `ctx.exclude_current_in_round()`。`weights` 的键可以是预设名或 `"__main__"`
  （主人格标记）。注意不是 `"main"`：抽取池用 `"__main__"` 识别主人格，写成
  `"main"` 会被忽略，除非恰好存在名为 `main` 的预设；`ctx.set_weight("main", …)`
  不会改主人格权重。
- **消息匹配**：`ctx.matches(pattern, mode)` 支持 `contains` / `exact` / `regex` /
  `prefix` / `suffix`，`pattern` 可传列表；`ctx.message_types`、
  `ctx.has_type()`、`ctx.has_text` / `has_emoji` / `has_image`；
  `ctx.emoji_emotions()`、`ctx.match_emoji(词列表, mode="any"|"all")`。
- **@ 与 bot 身份**：`ctx.bot_user_id` / `ctx.bot_nickname`、`ctx.is_at_me`、
  `ctx.at_targets()`、`ctx.at_segments`、`ctx.segments`。`ctx.is_at_me` 不排除
  bot 自己 @ 自己，需要时同时判断 `ctx.user_id != ctx.bot_user_id`。
- **持久 KV**：`ctx.kv_get(key, default)`、`ctx.kv_set(key, value)`、
  `ctx.kv_del(key)`、`ctx.kv_keys()`。数据落在
  `personality/script_kv.json`，脚本重载和插件重启后保留。键名只允许
  `[A-Za-z0-9_.-]`、长度 ≤64；值必须 JSON 可序列化；全表最多 512 条；所有脚本
  共享一张表，键名请加脚本前缀。
- **LLM 调用**（协程，需要 `async def`）：
  `await ctx.llm_generate(prompt, task=...)` 失败返回空串；
  `await ctx.llm_json(prompt, task=...)` 失败返回 `None`；
  `await ctx.emotion_score(...)` 返回 0~`scale` 的分数或 `None`；
  `await ctx.recent_context(n)` 返回最近聊天文本或空串。`task` 是宿主
  `[model_task_config.*]` 中的任务名（如 `replyer` / `planner` / `utils`）。
- **其它**：`await ctx.send_text(text)`（仅 `message` / `bot_message` 事件；
  事件所属流被黑白名单拦截时静默丢弃）、`ctx.log(message)`、`ctx.debug_enabled`、
  `ctx.caller_is_admin`、`ctx.hour` / `minute` / `now`、`ctx.group_id` /
  `user_id` / `text` / `segments`。

完整的方法签名、默认参数、返回值和可直接使用的配方见 [AGENT.md](AGENT.md)。

### 脚本接管模式

配置页开启「脚本接管」后，`swap` / `condition` / `weights` 配置不再决定自动
替换，由脚本在 `load` 事件用 `ctx.set_swap_params(...)` 设定。未设定的字段取
以下默认值，即“不产生自动替换”：

| 字段 | 默认 |
|---|---|
| `probability` | `0.0` |
| `per_stream` | `true` |
| `reroll_during_swap` | `false` |
| `exclude_current` | `false` |
| `main_weight` | `0.8` |
| `preset_weights` | `{}` |
| `non_bot_keywords` | `[]` |
| `bot_keywords` | `[]` |
| `time_windows` | `[]` |

```python
@on_event("load")
def setup(ctx):
    ctx.set_swap_params(
        probability=0.1,
        non_bot_keywords=["开会"],
        time_windows=["09:00-18:00"],
        preset_weights={"work_mode": 0.5},
        main_weight=0.8,
    )
```

- 接管开启时，`script.enabled` 被忽略，脚本引擎强制启用；`tick` 间隔与自动热重载
  设置仍然生效；`per_stream` 由脚本参数决定；官方临时风格抑制强制开启。黑白名单、
  插件总开关仍然生效；注入方式仍是覆盖式。
- `set_swap_params` 不是原子操作：遇到未知字段或非法值会抛 `ValueError`，但
  之前已处理的合法字段已经写入，不会回滚。用
  `mps_api.SWAP_PARAM_KEYS` 过滤 LLM 输出，或保证每个值合法。
- 多个脚本设置同一字段时，后写者覆盖。

### 自检脚本

`maips/_maips_selfcheck.py` 是随包的自检脚本，下划线前缀默认不加载。使用时：

1. 在配置页开启「加载下划线前缀脚本」并重载插件；
2. `/mps debug true`；
3. 由管理员在聊天里发送触发词。

它同时要求 `ctx.debug_enabled` 与 `ctx.caller_is_admin` 为真，非管理员发触发词
会被静默忽略。触发词与检查内容：

| 触发词 | 检查内容 |
|---|---|
| `maips检查` / `全量自检` | 非 LLM 总览：当前人格、字段、匹配、参数快照 |
| `自检匹配` | `ctx.matches` 各 mode 与消息字段 |
| `自检表情`（消息需带表情包） | `has_emoji`、`emoji_emotions`、`match_emoji` 的 any/all |
| `自检切换` | 逐个预设 `swap` → `revert` → `current_preset`，以及不存在预设抛 `ValueError` |
| `自检参数` | `get_swap_params` / `set_swap_params` / `SWAP_PARAM_KEYS` |
| `自检文本` | `await ctx.send_text` 通道 |
| `自检LLM` | `recent_context` / `emotion_score` / `llm_generate` / `llm_json`，会真实消耗 token |
| `自检swap事件` | 提示后用 `/mps swap <预设>` 触发，观察 swap 事件 |

`自检切换` 使用 `p_cheerful`、`p_grumpy`、`p_quiet` 三个预设名（脚本内
`_PRESETS`）。这些预设不随包提供，需要先自行创建，或按实际预设名修改脚本。
自检完成后建议关闭「加载下划线前缀脚本」和 debug，用完即关。

第一次部署可以按这个顺序检查：`/mps script list`（脚本已加载、无错误）→
`/mps list`（预设可见）→ `/mps status`（状态正常）→ `/mps debug true` →
`maips检查`。注入是否生效还可以看插件日志：执行 `/mps swap` 后让 bot 回复一次，
应出现 `replyer 覆盖 system 人格段为预设「…」` 和
`planner 覆盖 system 行为段为预设「…」`；出现
`锚点未命中，已回退为 items 尾部追加式注入` 时，说明宿主 system 模板与锚点
不匹配，预设仍会以追加方式生效。

常见现象：

| 现象 | 处理 |
|---|---|
| 管理命令回“仅限 bot 管理员使用” | 把群号填入管理员群列表，或把 QQ 号填入管理员 QQ 列表；插件与宿主管理员名单都空时只有本地控制台可用 |
| `/mps script list` 显示错误 | 脚本语法或运行期错误；按摘要修复后 `/mps script reload` |
| `/mps swap` 提示预设不存在 | 预设名拼错、文件未放入数据目录，或 TOML 解析失败；用 `/mps list` 核对名称，并检查文件内容 |
| bot 把预设当“注入”拒绝 | 检查日志是否出现“锚点未命中”回退；回退路径仍生效，但模型可能把它当叠加提示 |

### 安全边界

脚本以完整 Python 权限运行在插件 Runner 进程内，与安装一个插件同等信任级别。
只运行自己编写或审查过的脚本。插件不提供经聊天写入脚本文件或执行聊天内容的
通道；`/mps script reload` 仅管理员可执行，下划线脚本的加载开关在 WebUI 配置页、
默认关闭。

## 用 AI 生成新人格

推荐先保存一份主人格快照，再让 AI 参考它派生新人格：

```
/maisave 主人格 0
```

`主人格` 可以换成任意预设名，`0` 表示默认永久（该命令需要管理员）。快照只是
参考基准：bot 的主人格表现始终来自官方 `bot_config.toml`，不激活这份预设就不会
起作用。参考完可以删除 `preset/主人格.toml` 或把它移到 `preset/backup/`，不影响
bot 表现；以后需要时重新 `/maisave` 即可。

**麦麦在本地、AI 能读插件目录**：

1. 告诉 AI 参考 `主人格` 预设，说明想要的人格与路由/触发方式，并给出插件目录；
2. AI 读取 `AGENT.md`、主人格预设和 `maips/` 示例，生成新预设与脚本；
3. 新预设放进插件根目录 `preset/`，脚本放进 `maips/`；脚本保存即热重载，
   预设在下一次加载插件时复制到数据目录。

**麦麦在服务器、AI 读不到目录**：

1. 在服务器上执行 `/mps maiload 主人格`（管理员），把合并转发内容复制给 AI；
   或直接打开数据目录 `preset/主人格.toml` 复制；
2. AI 交付新预设内容、脚本内容和放置位置；
3. 预设放进插件根 `preset/`，脚本放进 `maips/`，随插件目录同步到服务器；
4. 重载插件后依次验证：`/mps script list` 无错误 → `/mps list` 能看到预设 →
   在目标聊天流 `/mps swap <预设>` → 插件日志出现覆盖 system 的记录。

放置时注意：插件根 `preset/` 的导入是“同名跳过”。如果数据目录已经有同名预设，
新文件不会覆盖它；请使用新名字，或先删除/改名旧预设。

给 AI 的提示可以写成：

> 参考我的主人格 `主人格`，做一个【高冷/元气/温柔/毒舌…】的分身人格，命名
> `p_xxx`，路由到群 `123456` / 私聊 `10001`；再做一个每天 23:00 触发的
> 夜间人格。

AI 侧的完整交付要求（预设字段、脚本触发方式、验证清单）见
[AGENT.md 附录 A](AGENT.md)。

## 数据位置

宿主按 manifest 插件 id 分配数据目录：

```
<MaiBot根>/data/plugins/github.cateye.mai-personality-swap/
├── preset/
│   ├── <预设名>.toml
│   └── backup/                    同名覆盖或删除前的备份
└── personality/
    ├── <群号或QQ号>.json          每个聊天流的替换状态
    ├── main.json                  手动全局切换的状态（`per_stream=false`，按需生成）
    ├── weights.json               /mps weight 写入的命令级权重
    ├── debug.json                 /mps debug 的持久开关
    └── script_kv.json             脚本持久 KV
```

- 状态键：群聊用群号，私聊用 QQ 号。`swap.per_stream=false` 时，手动命令与脚本
  默认作用域使用 `main`；自动替换路径仍按消息的群号/QQ 号写状态，因此不会反映到
  注入（见「自动切换」）。
  文件名会把非 `[A-Za-z0-9_-]` 字符替换为 `_`，防止路径穿越。
- 预设写入和状态写入都使用原子替换；状态写入失败不会中断对话，只会让该次切换
  无法持久化。
- 插件根目录的 `preset/` 是交付暂存区，不是运行数据；数据目录的 `preset/` 才是
  `/mps list`、`/maisave` 和切换实际读取的位置。

## 兼容、许可与仓库

- 与其它使用 `extra_prompt` 协作式追加的注入插件互不占用；system 被其它插件
  改写导致锚点未命中时，回退为 items 尾部追加。
- 插件不修改官方 `bot_config.toml`，也不注册 LLM 工具；切回主人格或预设到期后，
  请求恢复官方原样。
- 许可证：MIT。仓库：<https://github.com/cateyemizuki/cateye_mai_personality_swap>。
- 版本历史见 [CHANGELOG.md](CHANGELOG.md)；维护者实现说明见
  [开发文档.md](开发文档.md)。
