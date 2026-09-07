r"""夺舍麦麦（cateye_mai_personality_swap）。

把 bot 当前的官方人格设定（表达方式 / 人格 / 行为风格）保存为预设文件，按
条件 + 概率 + 权重自动切换、或用命令手动切换各聊天流的人格；替换状态持久化，
重启不丢。注入内容由"激活的预设"驱动，而非静态配置。

**覆盖式注入**（唯一注入方式，无配置开关）：预设激活期间，把请求 items 首条
system 消息里的官方人格/表达/行为段落**改写**为预设内容——官方人设从请求中
真正消失（不再是"在 system 之外叠一层、被模型当作提示注入忽略"的追加式），
配置触发与 maips 脚本触发一律走此路径。切分锚点基于 zh-CN 模板文案，锚点
未命中（自定义 system / 其它 locale / 异常）自动回退为 items 尾部追加式，绝不
误删官方内容；主人格（无预设激活）时 system 原样不动，到期/revert 后即恢复
官方原样（不修改官方配置文件，仍可逆）。

命令：
    /mps maisave [名称] [时长]   保存官方当前人格为预设（名称缺省 presetN，时长缺省 0=永久）
    /mps maisave list            列出全部预设
    /mps maiload <名称>          合并转发输出指定预设（供手动改回官方配置）
    /mps swap [名称] [时长]      切换人格；不带名称（留空/0）即切回主人格
    /mps weight <名称> <权重>    设置预设权重（持久化，重启保留）
    /mps debug [true|false]      查询/切换 debug 模式（开启后人格变换通知触发聊天流）
    /mps status                  查看替换状态与剩余时长
    /maisave [名称] [时长]       等价 /mps maisave

    命令反馈：宿主只把命令返回的 response 写日志、不自动回显；插件在命令入口
    统一把结果提示主动发给用户（短文本直发；长文本用合并转发，见
    ``_emit_command_result``）。

数据位置（宿主按 manifest 插件 id 分配，插件经 ctx.paths.data_dir 定位）：
    <MaiBot根>\data\plugins\github.cateye.mai-personality-swap\
      preset\*.toml           预设（TOML 多行字符串，换行可读、原样保留）
      preset\backup\          同名覆盖前的自动备份（数量受 backup_limit 限制）
      personality\*.json      替换状态（按群号/QQ号命名；全局模式 main.json）
      personality\weights.json  /mps weight 写入的权重覆盖
"""

from __future__ import annotations

import asyncio
import os
import random
import shutil
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import HookMode, HookOrder

from .injectors import (
    append_text_item,
    assemble_blocks,
    build_behavior_block,
    build_expression_block,
    build_identity_correction_block,
    build_persona_block,
    remove_official_temp_style_items,
    replace_planner_system_text,
    replace_replyer_system_text,
    rewrite_first_system_item,
    rewrite_tail_user_item,
)
from .preset_store import PresetStore, PersonaPreset, validate_preset_name
from .script_host import ScriptHost
from .swap_engine import (
    MAIN_MARKER,
    ConditionTracker,
    build_weight_pool,
    draw_persona,
    in_time_window,
    normalize_keywords,
    parse_time_windows,
    text_matches_any,
)
from .swap_state import SwapState, SwapStateStore

REPLYER_HOOK = "maisaka.replyer.before_request"
REPLYER_ITEMS_HOOK = "maisaka.replyer.before_model_request"
PLANNER_HOOK = "maisaka.planner.before_request"
RECEIVE_AFTER_HOOK = "chat.receive.after_process"
SEND_AFTER_HOOK = "send_service.after_send"

FORWARD_NICKNAME = "夺舍麦麦"
FORWARD_USER_ID = "0"

# 插件根目录"随包预设暂存区"：打包/交付时把预设 .toml 放这里，插件加载时
# 自动移入数据目录（空目录或没有 .toml 则忽略）
_BUNDLED_PRESET_DIR = "preset"

# stream_id → 群号/QQ 号 映射缓存上限：超出后裁剪最旧一半（防无界增长）
_STREAM_KEY_CACHE_MAX = 4096

# 配置版本：与 _manifest.json 的 version 保持同步（1.2.3 起为硬性要求）
SUPPORTED_CONFIG_VERSION = "1.4.1"


# ======================================================================
# 配置模型（全部中文描述，供 WebUI 渲染）
# 注意：插件目录不随包附带 config.toml——Runner 在加载时按本模型自动生成
# 并增量写回（见 开发文档 02-开发入门/03-配置系统.md）。
# ======================================================================


class PluginSectionConfig(PluginConfigBase):
    """插件。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件（总开关）")
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置版本",
        json_schema_extra={"hidden": True, "disabled": True},
    )


class FilterSectionConfig(PluginConfigBase):
    """生效范围过滤（脚本接管时仍然生效）。"""

    __ui_label__ = "黑白名单与管理员（接管时仍生效）"
    __ui_icon__ = "filter"
    __ui_order__ = 1

    group_list: List[str] = Field(default_factory=list, description="群黑白名单（群号列表）")
    group_list_mode: Literal["whitelist", "blacklist"] = Field(
        default="blacklist",
        description="群名单模式：whitelist=在名单内才生效；blacklist=在名单内则不生效",
    )
    private_list: List[str] = Field(default_factory=list, description="私聊黑白名单（QQ号列表）")
    private_list_mode: Literal["whitelist", "blacklist"] = Field(
        default="blacklist",
        description="私聊名单模式：whitelist=在名单内才生效；blacklist=在名单内则不生效",
    )
    admin_user_ids: List[str] = Field(
        default_factory=list,
        description="管理员 QQ 号列表（可执行 /mps maisave、weight、debug、script 等管理子命令；也接受 platform:user 形态如 qq:123456；留空则仅本地 operator/控制台可用）",
    )
    admin_group_ids: List[str] = Field(
        default_factory=list,
        description="管理员群列表：这些群里任何人可执行管理子命令（群号列表；留空不启用）",
    )


class ScriptSectionConfig(PluginConfigBase):
    """自定义脚本（maips）。"""

    __ui_label__ = "自定义脚本（maips）"
    __ui_icon__ = "file-code"
    __ui_order__ = 2

    takeover: bool = Field(
        default=False,
        description=(
            "是否使用 maips 脚本接管配置项。开启后：忽略本项以下的自动替换行为"
            "配置项（替换行为 / 触发条件 / 权重 / 预设，均按默认值处理），"
            "仅「插件总开关」「配置版本号」与「黑白名单」继续生效；"
            "自动替换行为完全由插件目录 maips/ 下的脚本设定（未设定的项默认留空，即不产生自动替换）；"
            "注入恒为覆盖式（无注入方式配置项，不受接管影响）；"
            "脚本引擎随之自动启用（脚本引擎的 timer 间隔与热重载开关不受影响）"
        ),
    )
    enabled: bool = Field(
        default=True,
        description="启用 KubeJS 风格自定义脚本（插件目录 maips/*.py，可放多个脚本，支持条件控制/直接切换人格）；脚本接管开启时本项被忽略（引擎自动启用）",
    )
    tick_interval_sec: int = Field(
        default=60,
        ge=10,
        description="timer 事件触发间隔（秒），同时用于脚本热重载检查",
    )
    auto_reload: bool = Field(
        default=True,
        description="脚本文件变化时自动热重载；关闭后用 /mps script reload 手动重载",
    )


class SwapSectionConfig(PluginConfigBase):
    """替换行为（脚本接管时忽略）。"""

    __ui_label__ = "替换行为（脚本接管时忽略）"
    __ui_icon__ = "repeat"
    __ui_order__ = 3

    per_stream: bool = Field(default=True, description="仅触发聊天流替换；关闭后全局替换（所有聊天流共用同一状态）")
    reroll_during_swap: bool = Field(default=False, description="替换期间是否能再次触发对话（开启后即使当前不是主人格也可再抽一次）")
    exclude_current: bool = Field(default=False, description="每次替换人格时忽略当前人格（触发时必不抽到当前人格，即必定变更）")
    probability: float = Field(default=0.1, description="条件全部满足后触发替换的概率（0~1）")


class ConditionSectionConfig(PluginConfigBase):
    """触发条件（脚本接管时忽略）。"""

    __ui_label__ = "触发条件（脚本接管时忽略）"
    __ui_icon__ = "list-checks"
    __ui_order__ = 4

    non_bot_keywords: List[str] = Field(
        default_factory=lambda: ["default"],
        description="非bot消息关键词：普通用户消息命中任一才可触发；留空或仅 [default] 占位则不检查",
    )
    bot_keywords: List[str] = Field(
        default_factory=lambda: ["default"],
        description="bot消息关键词：bot自己发出的消息命中任一才可触发；留空或仅 [default] 占位则不检查",
    )
    time_windows: List[str] = Field(
        default_factory=list,
        description="时段列表（可多个），如 [\"08:00-12:00\", \"14:00-18:00\"]，支持跨午夜；留空则全天",
    )


class WeightsSectionConfig(PluginConfigBase):
    """权重（脚本接管时忽略）。"""

    __ui_label__ = "权重（脚本接管时忽略）"
    __ui_icon__ = "scale"
    __ui_order__ = 5

    main: float = Field(default=0.8, description="主人格权重（主人格 = 不注入预设，按官方 bot_config 人格回复）")
    presets: Dict[str, float] = Field(
        default_factory=dict,
        description="预设权重表：键为预设名称（不带扩展名），值为正数；与主人格权重一起参与抽取，不要求总和为 1",
    )


class PresetSectionConfig(PluginConfigBase):
    """预设（脚本接管时忽略）。"""

    __ui_label__ = "预设（脚本接管时忽略）"
    __ui_icon__ = "folder"
    __ui_order__ = 6

    backup_limit: int = Field(default=5, description="同名预设覆盖保存时最多保留的备份数")


class CompatSectionConfig(PluginConfigBase):
    """注入行为（覆盖式注入统一生效，无通道选择配置）。"""

    __ui_label__ = "注入行为"
    __ui_icon__ = "shield"
    __ui_order__ = 7

    suppress_official_temp_style: bool = Field(
        default=True,
        description="预设激活期间忽略官方临时说话风格注入（直接从请求中剔除该消息）",
    )



class MaiPersonalitySwapConfig(PluginConfigBase):
    """夺舍麦麦配置模型。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    filter: FilterSectionConfig = Field(default_factory=FilterSectionConfig)
    swap: SwapSectionConfig = Field(default_factory=SwapSectionConfig)
    condition: ConditionSectionConfig = Field(default_factory=ConditionSectionConfig)
    weights: WeightsSectionConfig = Field(default_factory=WeightsSectionConfig)
    preset: PresetSectionConfig = Field(default_factory=PresetSectionConfig)
    compat: CompatSectionConfig = Field(default_factory=CompatSectionConfig)
    script: ScriptSectionConfig = Field(default_factory=ScriptSectionConfig)


# ======================================================================
# 插件主体
# ======================================================================


class MaiPersonalitySwapPlugin(MaiBotPlugin):
    """夺舍麦麦插件主体。"""

    config_model = MaiPersonalitySwapConfig

    def __init__(self) -> None:
        super().__init__()
        self._preset_store: Optional[PresetStore] = None
        self._state_store: Optional[SwapStateStore] = None
        self._tracker = ConditionTracker()
        self._rng = random.Random()
        self._stream_key_cache: Dict[str, str] = {}  # stream_id → 状态键（群号 / QQ号 / stream_id 兜底）
        self._script_host: Optional[ScriptHost] = None
        self._script_task: Optional[asyncio.Task] = None
        self._maips_dir_override: Optional[Path] = None  # 脚本目录覆盖（供无插件目录的环境注入）
        self._bot_name: str = ""  # bot 昵称缓存（替换式注入重建身份段用）
        self._bot_user_id: str = ""  # bot 自身账号缓存（脚本 ctx.bot_user_id / @ 自我识别用）
        # 覆盖式注入锚点未命中告警：只警告一次/每通道（防刷屏且提示宿主模板可能变更）
        self._anchor_warned = {"replyer": False, "planner": False}

    def _warn_anchor_miss(self, channel: str) -> None:
        """覆盖式注入锚点未命中时告警一次（每个通道）。

        锚点文案依赖宿主 zh-CN system 模板（见 injectors/system_replace.py）；
        未命中说明宿主模板措辞/locale 变更，覆盖式已静默退化为追加式——只告警
        一次，避免每条消息刷屏。
        """

        if self._anchor_warned.get(channel):
            return
        self._anchor_warned[channel] = True
        self._log(
            "warning",
            f"[{channel}] 覆盖式注入锚点未命中，已回退为 items 尾部追加式注入："
            "官方 system 模板可能已随宿主版本变更（非 zh-CN / 自定义 system / "
            "模板改版）。覆盖注入将降级为追加式（预设仍生效但可能被模型视为叠加层）；"
            "请检查 injectors/system_replace.py 的锚点文案是否仍与宿主模板一致。",
        )

    @staticmethod
    def _keyword_hit(text: str, raw_keywords: Any) -> bool:
        """条件关键词是否命中（统一入口：未配置=不检查，避免双处 normalize+判空）。

        返回 True 表示"条件配置了关键词且文本命中"；未配置（空/仅 default 占位）
        或未命中返回 False。
        """

        keywords = normalize_keywords(raw_keywords)
        return bool(keywords) and text_matches_any(text, keywords)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def on_load(self) -> None:
        """初始化数据目录并预热 stream→群号 映射。"""

        base = self._data_base()
        self._preset_store = PresetStore(base / "preset", backup_limit=int(self.config.preset.backup_limit))
        self._state_store = SwapStateStore(base / "personality")
        # 导入随包预设（插件根 preset/ 暂存区 → 数据目录），幂等，每次启动检查一次
        self._import_bundled_presets()
        await self._refresh_stream_map()
        await self._warm_bot_name()
        script_loaded = 0
        if bool(self.config.script.enabled) or self._takeover_active():
            self._script_host = ScriptHost(self._scripts_dir(), backend=self)
            self._script_host.install_shim()
            report = self._script_host.scan_and_reload(force=True)
            script_loaded = report["loaded"]
            self._script_task = asyncio.create_task(self._script_timer_loop())
        swapped = len(self._state_store.list_swapped())
        self._log(
            "info",
            f"夺舍麦麦已加载：预设 {len(self._preset_store.list_names())} 个，"
            f"进行中的替换 {swapped} 个，脚本 {script_loaded} 个已载入",
        )

    async def on_unload(self) -> None:
        """插件卸载：停掉脚本 timer（等待退出），清理脚本注册与 mps_api 垫片。"""

        if self._script_task is not None:
            self._script_task.cancel()
            try:
                await self._script_task
            except (asyncio.CancelledError, Exception):
                pass
            self._script_task = None
        if self._script_host is not None:
            self._script_host.shutdown()
        self._log("info", "夺舍麦麦已卸载（替换状态已持久化，重启后自动恢复）")

    async def on_config_update(
        self,
        config_scope: str = "self",
        config_data: Optional[dict] = None,
        config_version: Optional[str] = None,
    ) -> None:
        """配置更新：备份上限即时生效。"""

        if config_scope == "self" and self._preset_store is not None:
            self._preset_store._backup_limit = max(int(self.config.preset.backup_limit), 0)
            self._log("info", "夺舍麦麦配置已更新")

    # ------------------------------------------------------------------
    # 数据目录与映射
    # ------------------------------------------------------------------

    def _data_base(self) -> Path:
        """官方插件数据目录；运行时上下文未注入时回退到当前目录。"""

        try:
            return Path(self.ctx.paths.data_dir)
        except RuntimeError:
            return Path(".")

    def _import_bundled_presets(self) -> None:
        """把插件根目录 ``preset/`` 暂存区里的预设 .toml 移入数据目录。

        - 插件加载（on_load）时检查一次；
        - 暂存区为空 / 没有 .toml → 忽略；
        - 目标数据目录已存在同名预设 → 跳过该文件（不覆盖用户手动版）并记
          warning；
        - 成功移动后文件即从暂存区消失（只剩空目录，下次启动忽略）。

        用途：AI 交付的预设直接放进插件根 ``preset/``，整个插件目录打包部署到
        服务器，启动时即自动进数据目录——用户无需手动进数据目录放文件。
        """

        bundled_dir = Path(__file__).resolve().parent / _BUNDLED_PRESET_DIR
        if not bundled_dir.is_dir():
            return
        presets = sorted(p for p in bundled_dir.glob("*.toml") if p.is_file())
        if not presets:
            return
        if self._preset_store is None:
            return
        moved = 0
        skipped = 0
        for path in presets:
            name = path.name[: -len(".toml")]
            if not validate_preset_name(name):
                self._log("warning", f"随包预设 {path.name} 名称非法，跳过导入")
                skipped += 1
                continue
            if self._preset_store.exists(name):
                self._log("debug", f"数据目录已存在同名预设「{name}」，跳过随包导入（保留现有版本）")
                skipped += 1
                continue
            try:
                target = self._preset_store._preset_path(name)
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    # 同盘用原子 rename；跨盘（插件目录与数据目录在不同盘）os.replace
                    # 会抛 OSError，回退 shutil.move（复制+删源）
                    os.replace(str(path), str(target))
                except OSError:
                    shutil.move(str(path), str(target))
                moved += 1
            except Exception as exc:
                self._log("warning", f"随包预设 {path.name} 导入失败：{exc}")
                skipped += 1
        if moved:
            self._log("info", f"已从插件目录 preset/ 导入 {moved} 个预设到数据目录"
                              + (f"（跳过 {skipped} 个）" if skipped else ""))

    def _scripts_dir(self) -> Path:
        """自定义脚本目录：插件目录下的 ``maips/``（KubeJS 的 kubejs/ 同构）。

        插件目录由 plugin.py 自身位置推导；``_maips_dir_override`` 仅供
        无插件目录形态的环境（如本地验证）注入。
        """

        if self._maips_dir_override is not None:
            return self._maips_dir_override
        return Path(__file__).resolve().parent / "maips"

    @property
    def store(self) -> PresetStore:
        """预设存取器。"""

        if self._preset_store is None:
            base = self._data_base()
            self._preset_store = PresetStore(base / "preset", backup_limit=int(self.config.preset.backup_limit))
        return self._preset_store

    @property
    def state(self) -> SwapStateStore:
        """状态存取器。"""

        if self._state_store is None:
            base = self._data_base()
            self._state_store = SwapStateStore(base / "personality")
        return self._state_store

    async def _refresh_stream_map(self) -> None:
        """从宿主拉取全部聊天流，构建 stream_id → 群号/QQ号 映射。"""

        try:
            streams = await self.ctx.chat.get_all_streams()
        except Exception as exc:
            self._log("debug", f"拉取聊天流失败（映射将从消息流量中逐步补齐）：{exc}")
            return
        for entry in streams or []:
            parsed = self._extract_stream_entry(entry)
            if parsed:
                self._stream_key_cache.update(parsed)
        self._prune_stream_cache()

    def _prune_stream_cache(self) -> None:
        """裁剪 stream→键 映射到上限内（防无界增长；dict 保插入序，去最旧一半）。"""

        if len(self._stream_key_cache) > _STREAM_KEY_CACHE_MAX:
            drop = len(self._stream_key_cache) // 2
            for stale in list(self._stream_key_cache)[:drop]:
                self._stream_key_cache.pop(stale, None)

    @staticmethod
    def _extract_stream_entry(entry: Any) -> Dict[str, str]:
        """从单条聊天流记录提取 {stream_id: 群号或QQ号}。"""

        def pick(obj: Any, key: str) -> Any:
            if isinstance(obj, dict):
                return obj.get(key)
            return getattr(obj, key, None)

        stream_id = str(pick(entry, "stream_id") or pick(entry, "session_id") or "").strip()
        if not stream_id:
            return {}
        group_id = str(pick(entry, "group_id") or "").strip()
        if group_id:
            return {stream_id: group_id}
        user_id = str(pick(entry, "user_id") or "").strip()
        if user_id:
            return {stream_id: user_id}
        return {}

    def _note_stream_from_message(self, message: Any) -> str:
        """从消息 dict 记录 stream→键 映射，返回状态键（群号优先，其次QQ号）。"""

        if not isinstance(message, dict):
            return ""
        session_id = str(message.get("session_id") or "").strip()
        message_info = message.get("message_info") if isinstance(message.get("message_info"), dict) else {}
        group_info = message_info.get("group_info") if isinstance(message_info, dict) else {}
        user_info = message_info.get("user_info") if isinstance(message_info, dict) else {}
        group_id = str(group_info.get("group_id") or "").strip() if isinstance(group_info, dict) else ""
        user_id = str(user_info.get("user_id") or "").strip() if isinstance(user_info, dict) else ""
        key = group_id or user_id
        if session_id and key:
            self._stream_key_cache[session_id] = key
            self._prune_stream_cache()
        return key or session_id

    async def _scope_key_for_stream(self, stream_id: str) -> str:
        """解析聊天流对应的状态键；全局模式恒为 main。"""

        if self._effective_per_stream():
            normalized = str(stream_id or "").strip()
            key = self._stream_key_cache.get(normalized, "")
            if not key:
                await self._refresh_stream_map()
                key = self._stream_key_cache.get(normalized, "")
            return key or normalized  # 映射缺失时兜底用 stream_id 命名，保证功能可用
        return "main"

    def _stream_allowed(self, message: dict) -> bool:
        """群/私聊黑白名单过滤（名单为空 = 不过滤）。"""

        message_info = message.get("message_info") if isinstance(message.get("message_info"), dict) else {}
        group_info = message_info.get("group_info") if isinstance(message_info, dict) else {}
        user_info = message_info.get("user_info") if isinstance(message_info, dict) else {}
        group_id = str(group_info.get("group_id") or "").strip() if isinstance(group_info, dict) else ""
        user_id = str(user_info.get("user_id") or "").strip() if isinstance(user_info, dict) else ""
        filter_cfg = self.config.filter
        if group_id:
            return self._list_allows(group_id, filter_cfg.group_list, filter_cfg.group_list_mode)
        return self._list_allows(user_id, filter_cfg.private_list, filter_cfg.private_list_mode)

    @staticmethod
    def _list_allows(value: str, items: List[str], mode: str) -> bool:
        """黑白名单判定；名单为空视为不过滤。"""

        normalized = {str(item).strip() for item in (items or []) if str(item).strip()}
        if not normalized:
            return True
        if str(mode or "").strip().lower() == "whitelist":
            return value in normalized
        if str(mode or "").strip().lower() == "blacklist":
            return value not in normalized
        return True

    # ------------------------------------------------------------------
    # 激活预设查询与权重
    # ------------------------------------------------------------------

    def _active_preset_for_stream(self, stream_id: str) -> Optional[PersonaPreset]:
        """取当前聊天流（或全局）激活的预设；主人格/未找到返回 None。"""

        if self._state_store is None or not bool(self.config.plugin.enabled):
            return None
        key = self._stream_key_cache.get(str(stream_id or "").strip(), "")
        if self._effective_per_stream():
            if not key:
                key = str(stream_id or "").strip()
            if not key:
                return None
        else:
            key = "main"
        state = self._state_store.get(key)
        if state.is_main or not state.preset:
            return None
        return self._preset_store.load(state.preset)

    def _merged_weights(self) -> Dict[str, float]:
        """配置页 [weights.presets] 与 /mps weight 持久化覆盖合并（覆盖优先）。"""

        merged: Dict[str, float] = {}
        for name, value in (self.config.weights.presets or {}).items():
            try:
                merged[str(name).strip()] = float(value)
            except (TypeError, ValueError):
                continue
        if self._state_store is not None:
            merged.update(self._state_store.load_weight_overrides())
        return merged

    # ------------------------------------------------------------------
    # 接管模式（script.takeover）生效参数
    # ------------------------------------------------------------------

    def _takeover_active(self) -> bool:
        """脚本接管是否开启；开启后下方配置项全部按默认值处理。"""

        return bool(self.config.script.takeover)

    def _effective_per_stream(self) -> bool:
        """生效的"仅触发聊天流替换"：接管时取脚本设定（默认 True）。"""

        if self._takeover_active() and self._script_host is not None:
            return bool(self._script_host.params.get("per_stream", True))
        return bool(self.config.swap.per_stream)

    def _effective_suppress(self) -> bool:
        """生效的官方临时风格抑制开关：接管时默认开启。"""

        if self._takeover_active():
            return True
        return bool(self.config.compat.suppress_official_temp_style)

    def _effective_swap_params(self) -> Dict[str, Any]:
        """自动替换管线的生效参数：接管时取脚本运行时参数，否则取配置。"""

        if self._takeover_active() and self._script_host is not None:
            params = self._script_host.params
            return {
                "probability": float(params.get("probability", 0.0)),
                "per_stream": bool(params.get("per_stream", True)),
                "reroll_during_swap": bool(params.get("reroll_during_swap", False)),
                "exclude_current": bool(params.get("exclude_current", False)),
                "main_weight": float(params.get("main_weight", 0.8)),
                "preset_weights": dict(params.get("preset_weights", {})),
                "non_bot_keywords": normalize_keywords(params.get("non_bot_keywords", [])),
                "bot_keywords": normalize_keywords(params.get("bot_keywords", [])),
                "time_windows": parse_time_windows(params.get("time_windows", [])),
            }
        return {
            "probability": float(self.config.swap.probability),
            "per_stream": bool(self.config.swap.per_stream),
            "reroll_during_swap": bool(self.config.swap.reroll_during_swap),
            "exclude_current": bool(self.config.swap.exclude_current),
            "main_weight": float(self.config.weights.main),
            "preset_weights": self._merged_weights(),
            "non_bot_keywords": normalize_keywords(self.config.condition.non_bot_keywords),
            "bot_keywords": normalize_keywords(self.config.condition.bot_keywords),
            "time_windows": parse_time_windows(self.config.condition.time_windows),
        }

    def swap_filter_allows(self, ctx: Any, key: str) -> bool:
        """脚本切换的黑白名单闸门（黑白名单在接管模式下依然生效）。

        仅对聊天流级作用域检查；全局（main）与解析不到发送者的调用放行。
        """

        if key == "main" or not (getattr(ctx, "group_id", "") or getattr(ctx, "user_id", "")):
            return True
        filter_cfg = self.config.filter
        if ctx.group_id:
            return self._list_allows(ctx.group_id, filter_cfg.group_list, filter_cfg.group_list_mode)
        return self._list_allows(ctx.user_id, filter_cfg.private_list, filter_cfg.private_list_mode)

    # ------------------------------------------------------------------
    # Hook：注入
    # ------------------------------------------------------------------

    @HookHandler(
        REPLYER_HOOK,
        name="mps_replyer_style_inject",
        description="replyer 注入统一在 before_model_request 以覆盖 system 方式完成，本通道不再追加 extra_prompt",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
    )
    async def on_replyer_before_request(
        self,
        session_id: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """replyer extra_prompt 通道（已废弃追加，保留注册以防宿主空转）。

        覆盖注入统一在 ``before_model_request`` 改写首条 system 完成；merge /
        independent 两模式一律不再往 extra_prompt 追加——避免人格以"用户消息"
        形式叠在官方 system 之上、被模型当作提示注入忽略。
        """

        del session_id
        return {"action": "continue"}

    @HookHandler(
        REPLYER_ITEMS_HOOK,
        name="mps_replyer_items_handler",
        description="覆盖注入：预设激活时改写请求首条 system 的官方人格/表达段为预设；锚点未命中回退 items 尾部追加；任意模式生效",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
    )
    async def on_replyer_before_model_request(
        self,
        hook_name: str = "",
        items: Optional[List[Any]] = None,
        item_schema_version: Any = None,
        session_id: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """replyer items 通道：官方临时风格抑制 + 覆盖式注入（恒生效）。

        预设激活时把首条 system 的官方人格/表达段改写为预设内容——官方人设从
        请求中真正消失（模型不再有"原人格 vs 注入人格"冲突），到期/revert 后
        system 恢复官方原样。锚点未命中（自定义 system/其它 locale/异常）回退为
        items 尾部追加，保证人格仍生效、绝不误删。
        """

        if not isinstance(items, list):
            return {"action": "continue"}
        preset = self._active_preset_for_stream(session_id)
        modified = dict(kwargs)
        changed = False

        if preset is not None and self._effective_suppress():
            filtered, removed = remove_official_temp_style_items(items)
            if removed:
                modified["items"] = filtered
                changed = True
                self._log("debug", f"已剔除官方临时说话风格消息 {removed} 条（预设「{preset.name}」激活中）")

        if preset is not None:
            # 覆盖式注入：把首条 system 的官方人格/表达段改写为预设（任意模式恒生效）
            current_items = list(modified.get("items", items))
            try:
                replaced = rewrite_first_system_item(
                    current_items,
                    lambda text: replace_replyer_system_text(
                        text,
                        bot_name=self._preset_display_name(preset),
                        preset_persona=preset.persona,
                        preset_expression=preset.expression,
                    ),
                )
            except Exception as exc:
                self._log("warning", f"replyer 覆盖注入异常，回退追加式：{exc}")
                replaced = None
            if replaced is not None:
                # 覆盖成功后追加“当前身份”尾注（items 最末，紧邻生成位置）：
                # 说明上文中官方名发言是切换前旧身份的，当前身份是新人格名。
                # 仅预设配置了 persona_name 时注入（否则新身份=官方名，无新旧之分）。
                correction = build_identity_correction_block(preset.persona_name, self._bot_name)
                if correction:
                    current_items = list(replaced)
                    replaced = append_text_item(current_items, correction)
                    self._log(
                        "debug",
                        f"replyer 已追加当前身份尾注（预设「{preset.name}」，session={session_id or '未知'}）",
                    )
                modified["items"] = replaced
                changed = True
                self._log("debug", f"replyer 覆盖 system 人格段为预设「{preset.name}」（session={session_id or '未知'}）")
            else:
                # 锚点未命中（自定义 system / 非中文模板 / 异常）：回退 items 尾部
                # 追加式注入，保证预设仍生效；首次告警覆盖已降级
                self._warn_anchor_miss("replyer")
                blocks = assemble_blocks(
                    [build_expression_block(preset), build_persona_block(preset)]
                    + [build_identity_correction_block(preset.persona_name, self._bot_name)]
                )
                if blocks:
                    modified["items"] = append_text_item(current_items, blocks)
                    changed = True
                    self._log("debug", f"replyer system 覆盖锚点未命中，回退追加式注入预设「{preset.name}」")

        if not changed:
            return {"action": "continue"}
        modified["hook_name"] = hook_name
        modified["session_id"] = session_id
        modified["item_schema_version"] = item_schema_version
        return {"action": "continue", "modified_kwargs": modified}

    @HookHandler(
        PLANNER_HOOK,
        name="mps_planner_behavior_inject",
        description="覆盖注入：预设激活时改写 planner 首条 system 的官方行为风格段为预设；锚点未命中回退 items 尾部追加",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
    )
    async def on_planner_before_request(
        self,
        hook_name: str = "",
        items: Optional[List[Any]] = None,
        item_schema_version: Any = None,
        tool_definitions: Any = None,
        session_id: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """planner items 通道：行为风格覆盖注入（恒生效）。

        预设激活时把首条 system 的官方行为风格段改写为预设行为——planner 决策
        依据的新人设直接进入 system，不再是尾部一条易被忽略的追加消息。
        锚点未命中回退为 items 尾部追加。
        """

        preset = self._active_preset_for_stream(session_id)
        if preset is None or not isinstance(items, list):
            return {"action": "continue"}

        try:
            replaced = rewrite_first_system_item(
                items,
                lambda text: replace_planner_system_text(
                    text,
                    bot_name=self._bot_name,
                    preset_behavior=preset.behavior,
                    shell_name=preset.persona_name,
                ),
            )
        except Exception as exc:
            self._log("warning", f"planner 覆盖注入异常，回退追加式：{exc}")
            replaced = None
        if replaced is not None:
            # 覆盖成功后处理身份提醒：
            # ① 优先改写 host 在 items 末位追加的固定提醒
            #   （“你需要输出对{官方名}发言的分析，视情况输出文本内容的分析…”——
            #    宿主用官方昵称渲染，hook 触发前已在 items 里），替换为用户定稿的
            #    “你现在是{人格名}…上文中{官方名}的发言是人格切换前你以旧身份说过的…”
            # ② 找不到该提醒（自定义流程/异常）→ 回退在 items 最末追加同文案尾注。
            correction = build_identity_correction_block(preset.persona_name, self._bot_name)
            if correction:
                rewritten = rewrite_tail_user_item(
                    list(replaced),
                    prefix="你需要输出对",
                    new_text=correction,
                )
                if rewritten is not None:
                    final_items = rewritten
                    self._log(
                        "debug",
                        f"planner 已改写末位 host 提醒为当前身份说明（预设「{preset.name}」，session={session_id or '未知'}）",
                    )
                else:
                    final_items = append_text_item(list(replaced), correction)
                    self._log(
                        "debug",
                        f"planner 未找到末位 host 提醒，追加当前身份尾注（预设「{preset.name}」，session={session_id or '未知'}）",
                    )
            else:
                final_items = replaced
            self._log("debug", f"planner 覆盖 system 行为段为预设「{preset.name}」（session={session_id or '未知'}）")
            return {
                "action": "continue",
                "modified_kwargs": {
                    **kwargs,
                    "hook_name": hook_name,
                    "session_id": session_id,
                    "items": final_items,
                    "item_schema_version": item_schema_version,
                    "tool_definitions": tool_definitions,
                },
            }
        # 锚点未命中 → 回退到下面的追加式（首次告警覆盖已降级）
        self._warn_anchor_miss("planner")

        block = build_behavior_block(preset)
        if not block:
            return {"action": "continue"}
        # 追加式路径：行为块 + 当前身份说明
        block = assemble_blocks([block, build_identity_correction_block(preset.persona_name, self._bot_name)])
        self._log("debug", f"planner 注入预设「{preset.name}」行为风格（session={session_id or '未知'}）")
        return {
            "action": "continue",
            "modified_kwargs": {
                **kwargs,
                "hook_name": hook_name,
                "session_id": session_id,
                "items": append_text_item(items, block),
                "item_schema_version": item_schema_version,
                "tool_definitions": tool_definitions,
            },
        }

    # ------------------------------------------------------------------
    # Hook：触发条件收集与自动替换
    # ------------------------------------------------------------------

    @HookHandler(
        RECEIVE_AFTER_HOOK,
        name="mps_receive_observer",
        description="观察入站消息：记录 stream→群号 映射、收集非bot关键词命中、评估自动替换",
        mode=HookMode.OBSERVE,
        order=HookOrder.LATE,
    )
    async def on_receive_after_process(
        self,
        message: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        """入站观察（只读）：不修改不拦截；派发脚本 message 事件 + 评估自动替换。"""

        del kwargs
        if not isinstance(message, dict):
            return
        self._note_stream_from_message(message)
        if not bool(self.config.plugin.enabled):
            return

        script_decision = None
        if self._script_host is not None and not bool(message.get("is_notify")):
            message_info = message.get("message_info") if isinstance(message.get("message_info"), dict) else {}
            group_info = message_info.get("group_info") if isinstance(message_info, dict) else {}
            user_info = message_info.get("user_info") if isinstance(message_info, dict) else {}
            session_id = str(message.get("session_id") or "")
            script_ctx = self._script_host.build_message_context(
                "message",
                session_id=session_id,
                scope_key=self.script_scope_key_sync(session_id),
                group_id=str(group_info.get("group_id") or ""),
                user_id=str(user_info.get("user_id") or ""),
                text=str(message.get("processed_plain_text") or ""),
                is_command=bool(message.get("is_command")),
                raw_segments=message.get("raw_message"),
            )
            script_decision = await self._script_host.dispatch("message", script_ctx)

        if bool(message.get("is_notify")) or bool(message.get("is_command")):
            return
        text = str(message.get("processed_plain_text") or "")
        # 非bot关键词命中：仅当条件配置了关键词且命中才记录（未配置=不检查）
        if self._keyword_hit(text, self.config.condition.non_bot_keywords):
            # 按聊天流状态键记录命中，避免跨群串扰（见 ConditionTracker）
            self._tracker.record_non_bot_hit(self.script_scope_key_sync(str(message.get("session_id") or "")))
        await self._maybe_auto_swap(message, script_decision=script_decision)

    @HookHandler(
        SEND_AFTER_HOOK,
        name="mps_send_observer",
        description="观察 bot 发出的消息：收集 bot关键词命中",
        mode=HookMode.OBSERVE,
        order=HookOrder.LATE,
    )
    async def on_send_after(
        self,
        message: Optional[Dict[str, Any]] = None,
        sent: bool = False,
        **kwargs: Any,
    ) -> None:
        """出站观察（只读）。"""

        del kwargs
        if not isinstance(message, dict) or not sent:
            return
        self._note_stream_from_message(message)
        if not bool(self.config.plugin.enabled):
            return
        text = str(message.get("processed_plain_text") or "")
        # bot关键词命中：仅当条件配置了关键词且命中才记录（未配置=不检查）
        if self._keyword_hit(text, self.config.condition.bot_keywords):
            # 按聊天流状态键记录命中（见 ConditionTracker 注释）
            self._tracker.record_bot_hit(self.script_scope_key_sync(str(message.get("session_id") or "")))
        if self._script_host is not None:
            session_id = str(message.get("session_id") or "")
            script_ctx = self._script_host.build_message_context(
                "bot_message",
                session_id=session_id,
                scope_key=self.script_scope_key_sync(session_id),
                text=text,
                raw_segments=message.get("raw_message"),
            )
            await self._script_host.dispatch("bot_message", script_ctx)

    async def _maybe_auto_swap(self, message: Dict[str, Any], script_decision: Any = None) -> None:
        """条件评估 → 脚本门控 → 概率检查 → 权重抽取 → 落状态。

        参数来源：接管模式取脚本运行时参数（ctx.set_swap_params 设定，未设定
        默认留空即概率 0、条件为空），否则取配置项。``script_decision`` 是脚本
        层对本轮的裁决：``veto`` 直接跳过内置抽取；``probability`` /
        ``exclude_current`` / ``weight_overrides`` 覆盖对应参数。
        """

        params = self._effective_swap_params()
        if not self._stream_allowed(message):
            return
        key = self._note_stream_from_message(message)
        if not key:
            return
        state = self.state.get(key)
        if not state.is_main and not params["reroll_during_swap"]:
            return

        if script_decision is not None and script_decision.veto:
            self._log("debug", "自动替换被脚本否决，本轮跳过")
            return

        if not in_time_window(params["time_windows"]):
            return
        non_bot_keywords = params["non_bot_keywords"]
        if non_bot_keywords and not self._tracker.non_bot_fresh(key):
            return
        bot_keywords = params["bot_keywords"]
        if bot_keywords and not self._tracker.bot_fresh(key):
            return

        exclude_current_cfg = bool(params["exclude_current"])
        if script_decision is not None and script_decision.exclude_current is not None:
            exclude_current_cfg = script_decision.exclude_current
        exclude = MAIN_MARKER if (state.is_main and exclude_current_cfg) else (state.preset if exclude_current_cfg else "")

        probability = float(params["probability"])
        if script_decision is not None and script_decision.probability is not None:
            probability = max(min(script_decision.probability, 1.0), 0.0)

        existing = set(self._preset_store.list_names())
        preset_weights = {name: weight for name, weight in params["preset_weights"].items() if name in existing}
        pool = build_weight_pool(
            float(params["main_weight"]),
            preset_weights,
            exclude=exclude,
        )
        if script_decision is not None and script_decision.weight_overrides:
            for name, weight in script_decision.weight_overrides.items():
                if float(weight) > 0 and (name == MAIN_MARKER or name in existing):
                    pool[name] = float(weight)
            pool = {name: value for name, value in pool.items() if value > 0}

        decision = draw_persona(pool, probability=probability, rng=self._rng)
        if not decision.rolled or not decision.picked:
            return

        if decision.picked == MAIN_MARKER:
            if not state.is_main:
                self.script_revert(scope=key, source="auto", trigger_stream_id=str(message.get("session_id") or ""))
            return
        preset = self.store.load(decision.picked)
        if preset is None:
            self._log("warning", f"抽中预设「{decision.picked}」但文件缺失，本次跳过")
            return
        self.script_swap(
            preset.name,
            duration_minutes=None,
            scope=key,
            source="auto",
            trigger_stream_id=str(message.get("session_id") or ""),
        )

    # ------------------------------------------------------------------
    # 脚本宿主接入
    # ------------------------------------------------------------------

    def script_scope_key_sync(self, session_id: str) -> str:
        """脚本侧的作用域解析（同步版）：缓存优先，缺失回退 stream_id。"""

        if self._effective_per_stream():
            normalized = str(session_id or "").strip()
            return self._stream_key_cache.get(normalized, "") or normalized
        return "main"

    def script_swap(
        self,
        preset_name: str,
        duration_minutes: Optional[int],
        *,
        scope: str,
        source: str,
        trigger_stream_id: str = "",
    ) -> None:
        """脚本/引擎共用的切换入口（同步生效 + 异步广播 swap 事件）。

        ``trigger_stream_id``：触发本次变换的聊天流（command 的 stream_id /
        脚本事件的 session_id / auto 的触发消息 session_id）。仅用于 debug 模式
        下把变换通知发到"触发方"的聊天流——全局变换（scope=main）时不会因此
        给所有聊天流发消息。
        """

        preset = self.store.load(str(preset_name))
        if preset is None:
            raise ValueError(f"预设「{preset_name}」不存在")
        # 生效时长：调用方未显式声明（None）→ 用预设文件记录的 duration_minutes
        # 作为"最长持续时间"；显式传 0 = 永久。预设文件本身不过期，只决定切换后
        # 多久自动恢复主人格。
        effective_duration = preset.duration_minutes if duration_minutes is None else max(int(duration_minutes), 0)
        self.state.set_preset(
            scope,
            preset.name,
            duration_minutes=effective_duration,
            source=source,
        )
        remaining = "永久" if effective_duration <= 0 else f"{effective_duration} 分钟"
        self._log("info", f"人格切换（{source}）：{scope} → 「{preset.name}」（{remaining}）")
        if self._script_host is not None:
            self._script_host.notify_swap(
                scope_key=scope,
                preset=preset.name,
                source=source,
                duration_minutes=effective_duration,
            )
        if trigger_stream_id:
            self._notify_debug_swap(
                trigger_stream_id,
                f"人格切换（{source}）：「{preset.name}」（{remaining}）"
                + (f"，作用于聊天流 {scope}" if scope != "main" else "，全局生效"),
            )

    def script_revert(self, *, scope: str, source: str, trigger_stream_id: str = "") -> None:
        """脚本/引擎共用的恢复主人格入口。

        ``trigger_stream_id`` 语义同 :meth:`script_swap`。
        """

        self.state.set_main(scope)
        self._log("info", f"人格切换（{source}）：{scope} → 主人格")
        if self._script_host is not None:
            self._script_host.notify_swap(scope_key=scope, preset=None, source=source, duration_minutes=None)
        if trigger_stream_id:
            self._notify_debug_swap(
                trigger_stream_id,
                f"人格切换（{source}）：已恢复主人格"
                + (f"（聊天流 {scope}）" if scope != "main" else "（全局）"),
            )

    def _notify_debug_swap(self, trigger_stream_id: str, message: str) -> None:
        """debug 模式开启时，把一次人格变换通知发到**触发该变换的聊天流**。

        - debug 关闭 → 不发；
        - ``trigger_stream_id`` 为空（无法定位触发方，如 timer 触发）→ 不发，
          仅记 debug 日志，避免广播到无关聊天流。
        """

        try:
            debug_enabled = self.state.load_debug_flag()
        except Exception as exc:
            self._log("warning", f"读取 debug 开关失败：{exc}")
            debug_enabled = False
        if not debug_enabled:
            return
        if not trigger_stream_id:
            self._log("debug", f"debug：人格变换无触发聊天流可通知，跳过：{message}")
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._log("debug", f"debug：无事件循环，跳过通知：{message}")
            return

        async def _send_debug_text() -> None:
            await self.ctx.send.text(f"[夺舍麦麦] {message}", trigger_stream_id)

        def _on_done(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                self._log("warning", f"debug 通知任务异常：{exc!r}")

        try:
            task = loop.create_task(_send_debug_text())
            task.add_done_callback(_on_done)
        except Exception as exc:
            self._log("warning", f"debug 通知发送失败：{exc}")

    async def _script_timer_loop(self) -> None:
        """timer 事件循环：按间隔派发 timer + 检查脚本热重载。"""

        interval = max(int(self.config.script.tick_interval_sec), 5)
        while True:
            await asyncio.sleep(interval)
            try:
                await self._script_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log("warning", f"脚本 tick 异常：{exc}")

    async def _script_tick(self) -> None:
        """单次 tick：热重载检查 + timer 事件派发。"""

        if self._script_host is None:
            return
        if bool(self.config.script.auto_reload):
            self._script_host.scan_and_reload()
        ctx = self._script_host.build_timer_context()
        await self._script_host.dispatch("timer", ctx)

    # ------------------------------------------------------------------
    # 命令组件
    # ------------------------------------------------------------------

    @Command(
        "mps",
        description="夺舍麦麦：/mps maisave [名称] [时长] | maisave list | maisave delete <名称> | maiload <名称> | swap [名称] [时长] | weight <名称> <权重> | debug [true|false] | script [list|reload] | delete <名称> | status（swap 不带名称=切回主人格）",
        pattern=r"^/mps(?:\s+(?P<rest>.+))?\s*$",
    )
    async def handle_mps(
        self,
        stream_id: str = "",
        matched_groups: Optional[Dict[str, str]] = None,
        is_local_operator: bool = False,
        group_id: str = "",
        user_id: str = "",
        platform: str = "",
        **kwargs: Any,
    ) -> Tuple[bool, str, bool]:
        """/mps 命令入口。

        宿主只把返回的 response 写日志、不自动发给用户；这里在返回前把结果
        提示真正发出（短文本直发、长文本合并转发）。``is_local_operator`` 由宿主
        注入（operator / 控制台用户）；管理类子命令的权限见 ``_is_admin_call``
        （operator 或配置的管理员名单）。
        """

        del kwargs
        rest = str((matched_groups or {}).get("rest") or "").strip()
        result = await self._dispatch(
            rest,
            stream_id,
            default_sub="",
            is_local_operator=is_local_operator,
            group_id=group_id,
            user_id=user_id,
            platform=platform,
        )
        await self._emit_command_result(stream_id, result[1])
        return result

    @Command(
        "maisave",
        description="保存当前官方人格为预设：/maisave [名称] [时长]（等价 /mps maisave）",
        pattern=r"^/maisave(?:\s+(?P<rest>.+))?\s*$",
    )
    async def handle_maisave(
        self,
        stream_id: str = "",
        matched_groups: Optional[Dict[str, str]] = None,
        is_local_operator: bool = False,
        group_id: str = "",
        user_id: str = "",
        platform: str = "",
        **kwargs: Any,
    ) -> Tuple[bool, str, bool]:
        """/maisave 别名入口。

        同 /mps：把结果提示真正发给用户（短文本直发、长文本合并转发）。
        """

        del kwargs
        rest = str((matched_groups or {}).get("rest") or "").strip()
        result = await self._dispatch(
            rest,
            stream_id,
            default_sub="maisave",
            is_local_operator=is_local_operator,
            group_id=group_id,
            user_id=user_id,
            platform=platform,
        )
        await self._emit_command_result(stream_id, result[1])
        return result

    # 需要管理员权限的管理类子命令。
    # 这些命令会写预设/改权重/debug 开关/重载执行脚本——普通群成员不应可调。
    # 管理员判定：本地 operator / 控制台，或命中配置的管理员名单（见
    # _is_admin_call）。
    _OPERATOR_SUBS = frozenset({"maisave", "save", "maiload", "weight", "debug", "script", "delete"})

    def _is_admin_call(
        self,
        *,
        is_local_operator: bool,
        group_id: str,
        user_id: str,
        platform: str,
    ) -> bool:
        """管理子命令授权判定：operator / 控制台，或配置的管理员名单命中。

        名单（配置 ``[filter]``，脚本接管时仍生效）：
        - ``admin_user_ids``：QQ 号列表（也接受 ``platform:user`` 形态，
          如 ``qq:123456``）；
        - ``admin_group_ids``：群号列表，群内任何成员视为管理员。

        配置为空 → 仅本地 operator / 控制台可用。
        """

        if is_local_operator:
            return True
        if not bool(self.config.plugin.enabled):
            return False
        filter_cfg = self.config.filter
        group_id_s = str(group_id or "").strip()
        user_id_s = str(user_id or "").strip()
        platform_s = str(platform or "").strip().lower()
        # 群白名单：命中的群内任何成员可用
        if group_id_s and group_id_s in {str(x).strip() for x in (filter_cfg.admin_group_ids or []) if str(x).strip()}:
            return True
        if not user_id_s:
            return False
        # 用户白名单：支持裸 QQ 号 或 platform:user 两种形态
        admin_users = {
            str(x).strip()
            for x in (filter_cfg.admin_user_ids or [])
            if str(x).strip()
        }
        if user_id_s in admin_users:
            return True
        scoped = f"{platform_s}:{user_id_s}" if platform_s else ""
        return bool(scoped) and scoped in admin_users

    async def _dispatch(
        self,
        rest: str,
        stream_id: str,
        *,
        default_sub: str,
        is_local_operator: bool = False,
        group_id: str = "",
        user_id: str = "",
        platform: str = "",
    ) -> Tuple[bool, str, bool]:
        """解析并路由 /mps 子命令。

        ``default_sub`` 非空时来自 /maisave 别名：此时整个 rest 都是参数
        （``/maisave <名称> [时长]``），仅当首词恰为 ``list`` 时视为
        ``/maisave list`` 列出预设。管理类子命令（见 ``_OPERATOR_SUBS``）要求
        operator 或配置的管理员名单命中，否则拒绝。
        """

        if not bool(self.config.plugin.enabled):
            return False, "夺舍麦麦插件未启用", True
        parts = rest.split() if rest else []
        args = parts[1:] if parts else []
        if default_sub:
            sub = default_sub
            if parts and parts[0].lower() == "list":
                sub, args = "list", []
            else:
                args = parts
        else:
            sub = parts[0].lower() if parts else ""
            args = parts[1:] if parts else []
        if not sub:
            return (
                False,
                "用法：/mps maisave [名称] [时长] | maisave list | maisave delete <名称> | maiload <名称> | swap [名称] [时长] | weight <名称> <权重> | debug [true|false] | script [list|reload] | delete <名称> | status",
                True,
            )
        # 管理子命令鉴权：operator 或配置的管理员名单，否则拒绝
        if sub in self._OPERATOR_SUBS and not self._is_admin_call(
            is_local_operator=is_local_operator,
            group_id=group_id,
            user_id=user_id,
            platform=platform,
        ):
            self._log("warning", f"无权限用户尝试管理子命令 /mps {sub}（group={group_id or '-'} user={user_id or '-'}）")
            return (
                False,
                "该子命令仅限 bot 管理员使用（本地 operator/控制台，或 WebUI 插件配置页 "
                "「黑白名单与管理员」中配置的管理员 QQ/群）",
                True,
            )
        if sub in ("maisave", "save"):
            return await self._cmd_maisave(args, stream_id)
        if sub == "maiload":
            return await self._cmd_maiload(args, stream_id)
        if sub == "swap":
            return await self._cmd_swap(args, stream_id)
        if sub == "weight":
            return await self._cmd_weight(args)
        if sub == "debug":
            return await self._cmd_debug(args)
        if sub == "status":
            return await self._cmd_status()
        if sub in ("list",):
            return await self._cmd_list()
        if sub == "delete":
            return await self._cmd_delete(args)
        if sub == "script":
            return self._cmd_script(args)
        return False, f"未知子命令：{sub}", True

    async def _cmd_maisave(self, args: List[str], stream_id: str) -> Tuple[bool, str, bool]:
        """/mps maisave [名称] [时长]：保存官方当前人格为预设；``maisave list`` 列出全部；
        ``maisave delete <名称>`` 删除预设。"""

        if args and args[0].lower() == "list":
            return await self._cmd_list()
        if args and args[0].lower() == "delete":
            return await self._cmd_delete(args[1:])
        name = args[0].strip() if args else ""
        duration_text = args[1].strip() if len(args) > 1 else ""
        if name and not validate_preset_name(name):
            return False, "预设名称只能包含中文、字母、数字、下划线或连字符（1~64 位）", True
        duration_minutes = _parse_minutes(duration_text)
        if duration_minutes is None:
            return False, "时长必须是 ≥0 的整数（分钟），0 表示一直替换", True

        if not name:
            existing = set(self.store.list_names())
            index = 1
            while f"preset{index}" in existing:
                index += 1
            name = f"preset{index}"

        persona_text = await self._read_official("personality.personality")
        reply_style = await self._read_official("personality.reply_style")
        behavior_style = await self._read_official("personality.behavior_style")
        if not any((persona_text, reply_style, behavior_style)):
            return False, "读取官方人格配置失败（personality 三项均为空），未保存", True

        preset = PersonaPreset(
            name=name,
            expression=reply_style or "",
            persona=persona_text or "",
            behavior=behavior_style or "",
            duration_minutes=duration_minutes,
        )
        backup_path = self.store.save(preset, backup_existing=True)
        await self._send_preset_forward(preset, stream_id, header="已保存预设（以下为保存内容）")
        note = f"，旧预设已备份到 {backup_path.name}" if backup_path else ""
        return True, f"✅ 预设「{name}」已保存（时长：{'永久' if duration_minutes <= 0 else f'{duration_minutes} 分钟'}{note}）", True

    async def _cmd_maiload(self, args: List[str], stream_id: str) -> Tuple[bool, str, bool]:
        """/mps maiload <名称>：合并转发输出指定预设。"""

        name = args[0].strip() if args else ""
        if not name:
            return False, "用法：/mps maiload <预设名称>", True
        preset = self.store.load(name)
        if preset is None:
            return False, f"预设「{name}」不存在（用 /mps maisave list 查看全部预设）", True
        await self._send_preset_forward(preset, stream_id, header="预设内容（可复制回官方人格配置）")
        return True, f"已发送预设「{name}」的内容", True

    async def _cmd_swap(self, args: List[str], stream_id: str) -> Tuple[bool, str, bool]:
        """/mps swap [名称] [时长]：切换当前聊天流（或全局）人格。

        不带名称（``/mps swap`` 或 ``/mps swap 0``）时切回主人格。
        """

        name = args[0].strip() if args else ""
        duration_text = args[1].strip() if len(args) > 1 else ""
        key = await self._scope_key_for_stream(stream_id)
        scope_note = "全局" if key == "main" else f"聊天流 {key}"

        if not name or name == "0":
            # 无预设名（留空或 0）→ 切回主人格（同 revert 语义）
            state = self.state.get(key)
            if state.is_main:
                return True, f"已处于主人格（{scope_note}）", True
            self.script_revert(scope=key, source="command", trigger_stream_id=stream_id)
            return True, f"✅ 已恢复主人格（{scope_note}）", True

        preset = self.store.load(name)
        if preset is None:
            return False, f"预设「{name}」不存在（用 /mps maisave list 查看全部预设）", True
        duration_minutes = preset.duration_minutes if duration_text == "" else _parse_minutes(duration_text)
        if duration_minutes is None:
            return False, "时长必须是 ≥0 的整数（分钟），0 表示一直替换", True
        # 统一走 script_swap：落盘 + 广播 swap 事件 + debug 触发流通知一次完成
        self.script_swap(
            preset.name,
            duration_minutes,
            scope=key,
            source="command",
            trigger_stream_id=stream_id,
        )
        state = self.state.get(key)
        return True, self._format_swap_state(scope_note, state), True

    async def _cmd_weight(self, args: List[str]) -> Tuple[bool, str, bool]:
        """/mps weight <名称> <权重>：设置预设权重（持久化）。"""

        if len(args) < 2:
            return False, "用法：/mps weight <预设名称> <权重>（正数，不要求总和为 1）", True
        name = args[0].strip()
        try:
            weight = float(args[1].strip())
        except ValueError:
            return False, "权重必须是数字（0 表示清除该预设的命令级权重，回落到配置页数值；正数为生效权重）", True
        if weight < 0:
            return False, "权重不能是负数", True
        if weight == 0:
            had = name in self.state.load_weight_overrides()
            self.state.save_weight_override(name, 0)
            if had:
                return True, f"✅ 已清除预设「{name}」的命令级权重，回落到配置页数值", True
            return True, f"预设「{name}」本就没有命令级权重覆盖（当前生效值见 /mps status 或配置页）", True
        if name not in self.store.list_names():
            return False, f"预设「{name}」不存在（用 /mps maisave list 查看全部预设）", True
        self.state.save_weight_override(name, weight)
        return True, f"✅ 预设「{name}」权重已设为 {weight}（持久化，与配置页中的预设权重合并生效）", True

    async def _cmd_debug(self, args: List[str]) -> Tuple[bool, str, bool]:
        """/mps debug [true|false]：查询/设置 debug 模式。

        debug 开启后，每次人格变换都会把一条通知发到**触发该变换的聊天流**
        （而非所有受影响的聊天流；无法定位触发方时不发，仅记日志）。
        开关持久化，重启保留。
        """

        arg = args[0].strip().lower() if args else ""
        current = self.state.load_debug_flag()
        if not arg:
            state_text = "开启" if current else "关闭"
            return True, f"debug 模式当前：{state_text}（/mps debug true|false 切换）", True
        if arg in ("true", "1", "on", "开"):
            self.state.save_debug_flag(True)
            return True, "✅ debug 模式已开启：每次人格变换将通知触发的聊天流", True
        if arg in ("false", "0", "off", "关"):
            self.state.save_debug_flag(False)
            return True, "✅ debug 模式已关闭", True
        return False, f"无效参数：{arg}（用法：/mps debug true|false；不带参数查询当前状态）", True

    async def _cmd_status(self) -> Tuple[bool, str, bool]:
        """/mps status：查看替换状态。"""

        swapped = self.state.list_swapped()
        lines = ["【夺舍麦麦状态】"]
        if not swapped:
            lines.append("所有聊天流均为主人格")
        for key, state in swapped.items():
            lines.append(self._format_swap_state(key, state))
        names = self.store.list_names()
        lines.append(f"预设共 {len(names)} 个：{'、'.join(names) if names else '（无）'}")
        params = self._effective_swap_params()
        mode = "每聊天流独立" if params["per_stream"] else "全局"
        takeover_note = "（脚本接管中）" if self._takeover_active() else ""
        lines.append(f"替换范围：{mode}{takeover_note}；主人格权重：{params['main_weight']}")
        debug_text = "开启" if self.state.load_debug_flag() else "关闭"
        lines.append(f"debug 模式：{debug_text}（人格变换通知触发聊天流，/mps debug true|false 切换）")
        return True, "\n".join(lines), True

    async def _cmd_list(self) -> Tuple[bool, str, bool]:
        """/mps maisave list：列出全部预设名称。"""

        names = self.store.list_names()
        if not names:
            return True, "还没有任何预设（用 /maisave [名称] [时长] 保存当前人格）", True
        return True, "预设列表：\n" + "\n".join(f"- {name}" for name in names), True

    async def _cmd_delete(self, args: List[str]) -> Tuple[bool, str, bool]:
        """/mps delete <名称> 或 /mps maisave delete <名称>：删除预设。

        删除前自动备份到数据目录 ``preset/backup/``（与覆盖前备份同机制，受
        ``backup_limit`` 轮转）。连锁处理：若该预设正被某些聊天流激活，这些流
        先恢复主人格（避免状态指向已删除文件）；该预设的命令级权重一并清除。
        """

        name = args[0].strip() if args else ""
        if not name:
            return False, "用法：/mps delete <预设名称>（或 /mps maisave delete <预设名称>）", True
        if not self.store.exists(name):
            return False, f"预设「{name}」不存在（用 /mps maisave list 查看全部预设）", True

        # 1) 正在激活该预设的聊天流 → 恢复主人格（防状态指向已删除文件）
        reverted = []
        try:
            for key, state in self.state.list_swapped().items():
                if state.preset == name:
                    self.script_revert(scope=key, source="command", trigger_stream_id="")
                    reverted.append(key)
        except Exception as exc:
            self._log("warning", f"删除预设「{name}」时回退激活流失败：{exc}")

        # 2) 清除该预设的命令级权重覆盖（若存在）
        weight_cleared = False
        try:
            if name in self.state.load_weight_overrides():
                self.state.save_weight_override(name, 0)
                weight_cleared = True
        except Exception as exc:
            self._log("warning", f"删除预设「{name}」时清除权重覆盖失败：{exc}")

        # 3) 备份 + 删除文件
        backup_path = self.store.delete(name, backup=True)
        notes = []
        if reverted:
            notes.append(f"已在 {len(reverted)} 个激活流中恢复主人格")
        if weight_cleared:
            notes.append("已清除该预设的命令级权重")
        note = f"（{'；'.join(notes)}）" if notes else ""
        backup_note = f"，已备份到 backup/{backup_path.name}" if backup_path else ""
        self._log("info", f"删除预设「{name}」（source=command{backup_note}）")
        return True, f"✅ 预设「{name}」已删除{backup_note}{note}", True

    def _cmd_script(self, args: List[str]) -> Tuple[bool, str, bool]:
        """/mps script [list|reload]：查看脚本加载状态 / 手动热重载。"""

        sub = args[0].strip().lower() if args else "list"
        if self._script_host is None:
            return False, "脚本功能未启用（在 WebUI 配置页开启「自定义脚本」或「脚本接管」后重载插件）", True
        if sub == "reload":
            report = self._script_host.scan_and_reload(force=True)
            lines = [f"已重载：新载入 {report['loaded']} 个，未变化 {report['unchanged']} 个，错误 {report['errors']} 个"]
            lines.extend(self._script_host.describe())
            return True, "\n".join(lines), True
        lines = ["【自定义脚本】"]
        lines.extend(self._script_host.describe())
        lines.append("重载：/mps script reload；脚本目录：插件目录 maips/（可放多个 .py 脚本，_ 开头为工具库不加载）")
        return True, "\n".join(lines), True

    # ------------------------------------------------------------------
    # Hook：表情包情绪采集
    # ------------------------------------------------------------------

    @HookHandler(
        "emoji.maisaka.after_select",
        name="mps_emoji_select_observer",
        description="观察 Maisaka 表情选择结果：记录 bot 发送表情包的命中情绪供脚本匹配",
        mode=HookMode.OBSERVE,
        order=HookOrder.LATE,
    )
    async def on_emoji_select(
        self,
        stream_id: str = "",
        selected_emoji_hash: str = "",
        matched_emotion: str = "",
        **kwargs: Any,
    ) -> None:
        """表情选择观察（只读）：喂给脚本宿主的表情情绪状态。"""

        del kwargs
        if self._script_host is None:
            return
        self._script_host.record_bot_emoji(stream_id, matched_emotion)
        self._script_host.record_emoji_index(selected_emoji_hash, "", [matched_emotion] if matched_emotion else [])

    @HookHandler(
        "emoji.register.after_build_description",
        name="mps_emoji_register_observer",
        description="观察表情包描述生成：维护 hash → 情绪标签索引供脚本匹配",
        mode=HookMode.OBSERVE,
        order=HookOrder.LATE,
    )
    async def on_emoji_register(
        self,
        emoji: Optional[Dict[str, Any]] = None,
        description: str = "",
        **kwargs: Any,
    ) -> None:
        """表情注册观察（只读）：新表情包的情绪标注进入脚本索引。"""

        del kwargs
        if self._script_host is None or not isinstance(emoji, dict):
            return
        self._script_host.record_emoji_index(
            str(emoji.get("file_hash") or ""),
            str(description or emoji.get("description") or ""),
            emoji.get("emotions"),
        )

    # ------------------------------------------------------------------
    # 发送辅助
    # ------------------------------------------------------------------

    # 命令反馈用合并转发的行数阈值：超过则转成合并转发（防刷屏），否则文本直发
    COMMAND_TEXT_MAX_LINES = 8
    COMMAND_TEXT_MAX_CHARS = 500

    async def _send_forward_text(self, stream_id: str, *, title: str, body_lines: List[str]) -> None:
        """把多行内容用合并转发发出（一条标题 + 按行分块），失败回退纯文本。

        与 ``_send_preset_forward`` 共用格式，供命令的长反馈使用。
        """

        body_lines = [str(line) for line in body_lines if str(line).strip()]
        if not body_lines:
            return
        messages = [
            {"user_id": FORWARD_USER_ID, "nickname": FORWARD_NICKNAME, "segments": [{"type": "text", "content": title}]},
        ]
        # 按每块 40 行拆分，避免单条超长
        chunk_size = 40
        for start in range(0, len(body_lines), chunk_size):
            chunk = body_lines[start : start + chunk_size]
            messages.append(
                {
                    "user_id": FORWARD_USER_ID,
                    "nickname": FORWARD_NICKNAME,
                    "segments": [{"type": "text", "content": "\n".join(chunk)}],
                }
            )
        try:
            await self.ctx.send.forward(messages, stream_id)
        except Exception as exc:
            self._log("warning", f"合并转发失败，回退为文本发送：{exc}")
            await self.ctx.send.text(f"{title}\n\n" + "\n".join(body_lines), stream_id)

    async def _emit_command_result(self, stream_id: str, response: Optional[str]) -> None:
        """把命令返回的 response 真正发给用户（宿主只把它写日志，不自动回显）。

        - 内容为空 → 不发（保留"不方便发出"的静默场景）；
        - 短文本（行数/字符数在阈值内）→ ``send.text`` 直发；
        - 长文本 → 合并转发（``send.forward``），标题与正文分离，失败回退纯文本。
        """

        text = str(response or "").strip()
        if not text or not stream_id:
            return
        lines = text.splitlines()
        short = len(lines) <= self.COMMAND_TEXT_MAX_LINES and len(text) <= self.COMMAND_TEXT_MAX_CHARS
        if short:
            try:
                await self.ctx.send.text(text, stream_id)
            except Exception as exc:
                self._log("warning", f"命令反馈文本发送失败：{exc}")
            return
        title = lines[0][:60] if lines else "命令结果"
        body = lines[1:] if len(lines) > 1 else []
        if not body:
            body = [text]
        await self._send_forward_text(stream_id, title=title, body_lines=body)

    async def _send_preset_forward(self, preset: PersonaPreset, stream_id: str, *, header: str) -> None:
        """用合并转发回显预设内容（验证保存 + 防刷屏）。"""

        body = "\n".join(preset.summary_lines())
        messages = [
            {"user_id": FORWARD_USER_ID, "nickname": FORWARD_NICKNAME, "segments": [{"type": "text", "content": header}]},
            {"user_id": FORWARD_USER_ID, "nickname": FORWARD_NICKNAME, "segments": [{"type": "text", "content": body}]},
        ]
        try:
            await self.ctx.send.forward(messages, stream_id)
        except Exception as exc:
            self._log("warning", f"合并转发失败，回退为文本发送：{exc}")
            await self.ctx.send.text(f"{header}\n\n{body}", stream_id)

    def _format_swap_state(self, key: str, state: SwapState) -> str:
        """格式化单个替换状态行。"""

        if state.is_main:
            return f"- {key}：主人格"
        remaining = state.remaining_seconds()
        if remaining is None:
            duration_note = "永久"
        else:
            minutes = int(remaining // 60)
            seconds = int(remaining % 60)
            duration_note = f"剩余 {minutes} 分 {seconds} 秒"
        return f"- {key}：「{state.preset}」（{duration_note}，来源：{state.source or '未知'}）"

    def _preset_display_name(self, preset: PersonaPreset) -> str:
        """预设生效时 system 身份段用的 bot 名字。

        预设配置了 ``persona_name`` → 用预设名（人格化自称）；否则回退官方
        ``bot.nickname``（缓存）。
        """

        preset_name = str(getattr(preset, "persona_name", "") or "").strip()
        return preset_name or self._bot_name

    async def _warm_bot_name(self) -> None:
        """预热 bot 昵称与自身账号（替换式注入重建 system 身份段 / 脚本 @ 自识别用）。

        读取失败/为空时保持缓存为空——替换 identity 段时只写人格文本、
        不写名字行，身份仍成立；@ 自识别则退回文本层兜底。
        """

        nickname = (await self._read_official("bot.nickname")).strip()
        self._bot_name = nickname
        if nickname:
            self._log("debug", f"bot 昵称缓存：{nickname}")
        account = (await self._read_official("bot.qq_account")).strip()
        self._bot_user_id = account
        if account:
            self._log("debug", f"bot 账号缓存：{account}")

    @property
    def script_bot_nickname(self) -> str:
        """脚本可见的 bot 官方昵称（空串=未预热/读取失败）。"""

        return self._bot_name

    @property
    def script_bot_user_id(self) -> str:
        """脚本可见的 bot 自身账号（空串=未预热/读取失败/非 QQ 平台）。

        来自官方 ``bot.qq_account``。空串时脚本的 ``ctx.is_at_me`` 会退回
        文本层 @昵称 匹配。
        """

        return self._bot_user_id

    async def _read_official(self, key: str) -> str:
        """读取官方 bot_config 的一个字符串字段（容错处理宿主返回信封）。

        仅接受 str / None / 标量（int/float/bool）；list/dict 等复合类型返回空串
        ——避免把结构转成字符串注入人格文本。
        """

        try:
            value = await self.ctx.config.get(key)
        except Exception as exc:
            self._log("warning", f"读取官方配置 {key} 失败：{exc}")
            return ""
        if isinstance(value, dict) and "value" in value and "success" in value:
            value = value.get("value")
        if isinstance(value, str):
            return value
        if value is None or isinstance(value, (int, float, bool)):
            return "" if value is None else str(value)
        self._log("debug", f"官方配置 {key} 返回非标量类型 {type(value).__name__}，按空处理")
        return ""

    def _log(self, level: str, message: str) -> None:
        """写插件日志；上下文未就绪时回退到标准 logging。"""

        try:
            logger = self.ctx.logger
        except RuntimeError:
            import logging

            logging.getLogger("maibot_sdk.plugin").log(
                getattr(logging, level.upper(), logging.INFO), message
            )
            return
        getattr(logger, level, logger.info)(message)


def _parse_minutes(text: str) -> Optional[int]:
    """解析分钟数；空串按 0（永久），非法返回 None。"""

    normalized = str(text or "").strip()
    if not normalized:
        return 0
    try:
        value = int(normalized)
    except ValueError:
        return None
    return value if value >= 0 else None


def create_plugin() -> MaiPersonalitySwapPlugin:
    """Runner 加载入口。"""

    return MaiPersonalitySwapPlugin()
