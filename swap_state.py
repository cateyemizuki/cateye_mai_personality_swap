"""替换状态的持久化（确保重启后仍知道"哪个群换成了哪个人格、还剩多久"）。

目录：``<官方插件数据目录>/personality/``

- 每个聊天流一个文件：群聊用群号命名（``<群号>.json``），私聊用 QQ 号命名；
  全局模式（仅触发聊天流替换 = 关闭）统一使用 ``main.json``。
- **主人格用布尔值标记**：``{"is_main": true}``，而不是把 "main" 之类的字面量
  写进 preset 字段——防止用户恰好创建了一个叫 "main"/"主人格" 的预设时，
  状态文件无法区分"主人格"与"同名预设"。
- 非主人格状态形如::

    {"is_main": false, "preset": "preset1", "started_at": 1730000000.0,
     "duration_minutes": 60, "expires_at": 1730003600.0, "source": "command"}

  ``duration_minutes = 0``（一直替换）时 ``expires_at`` 为 null。
- **语义澄清**：本文件的 ``duration_minutes`` / ``expires_at`` 是**某次切换的
  "本次激活"计时**——切到某预设后最多持续多久、何时自动恢复主人格；它来自
  切换时生效的时长（命令/脚本显式声明，或回退到预设文件 ``duration_minutes``）。
  **预设文件（preset/*.toml）本身永不过期、不会被删除**，只作为"缺省切换时长"
  的记录被读取。
- 到期判定是惰性的：任何读取都会检查 ``expires_at``，到期即回写主人格状态。
- 命令设置的权重覆盖单独存放在 ``weights.json``（``{预设名: 权重}``），
  与 config.toml 的 [weights.presets] 合并生效。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

STATE_SUFFIX = ".json"
MAIN_STATE_FILE = "main"
WEIGHTS_FILE = "weights"
DEBUG_FILE = "debug"
SCRIPT_KV_FILE = "script_kv"  # 脚本级持久 KV（跨重载/重启保留，供计数/状态累计）
MAIN_MARKER = "__main__"

# 脚本 KV 键名规则：只保留 [A-Za-z0-9_.-]，长度上限，防路径穿越与超长滥用
_SCRIPT_KV_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_SCRIPT_KV_KEY_MAX = 64
_SCRIPT_KV_ITEMS_MAX = 512


@dataclass(slots=True)
class SwapState:
    """单个聊天流（或全局）的当前人格状态。"""

    is_main: bool = True
    preset: str = ""
    started_at: float = 0.0
    duration_minutes: int = 0
    expires_at: Optional[float] = None
    source: str = ""

    def remaining_seconds(self, now: Optional[float] = None) -> Optional[float]:
        """剩余秒数；主人格或永久替换返回 None。"""

        if self.is_main or self.expires_at is None:
            return None
        now_value = now if now is not None else time.time()
        return max(self.expires_at - now_value, 0.0)

    def is_expired(self, now: Optional[float] = None) -> bool:
        """是否已到期（主人格/永久替换永不过期）。"""

        if self.is_main or self.expires_at is None:
            return False
        now_value = now if now is not None else time.time()
        return now_value >= self.expires_at


class SwapStateStore:
    """替换状态文件的读写（含惰性到期回写与权重覆盖）。"""

    def __init__(self, base_dir: Path) -> None:
        """绑定状态根目录（…/personality）。"""

        self._base_dir = Path(base_dir)

    # ------------------------------------------------------------------
    # 状态读写
    # ------------------------------------------------------------------

    def _state_path(self, key: str) -> Path:
        """单个聊天流的状态文件路径（key 先过白名单，防路径穿越）。"""

        return self._base_dir / f"{self._safe_key(key)}{STATE_SUFFIX}"

    @staticmethod
    def _safe_key(key: str) -> str:
        """把状态键规范化为安全的单级文件名（防路径穿越 / 异常字符）。

        状态键来源是群号 / QQ 号 / 会话流 ID（可能含渠道前缀与连字符）。规则：
        - 只保留 ``[A-Za-z0-9_-]`` 字符，其余替换为 ``_``（含路径分隔符与 ``..``）；
        - 空 / 全被替换为空 → 回退 ``main``（不产生不可寻址或逃逸路径）。
        """

        normalized = re.sub(r"[^A-Za-z0-9_-]", "_", str(key or "").strip())
        return normalized if normalized else MAIN_STATE_FILE

    def get(self, key: str, *, now: Optional[float] = None) -> SwapState:
        """读取状态；文件缺失按主人格处理，到期自动回写主人格。

        损坏/字段类型非法的文件按主人格处理并回写（不冒泡异常到 hook 链；
        历史半截文件在原子写之前可能产生，这里兜底）。
        """

        path = self._state_path(key)
        if not path.is_file():
            return SwapState()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._base_dir.mkdir(parents=True, exist_ok=True)
            self._state_path(key).write_text(json.dumps({"is_main": True}, ensure_ascii=False), encoding="utf-8")
            return SwapState()
        try:
            state = SwapState(
                is_main=bool(raw.get("is_main", True)),
                preset=str(raw.get("preset") or ""),
                started_at=float(raw.get("started_at") or 0.0),
                duration_minutes=int(raw.get("duration_minutes") or 0),
                expires_at=float(raw["expires_at"]) if raw.get("expires_at") is not None else None,
                source=str(raw.get("source") or ""),
            )
        except (TypeError, ValueError):
            # 字段类型非法（损坏文件）：回写主人格，避免每次读取都报错
            self._base_dir.mkdir(parents=True, exist_ok=True)
            self._state_path(key).write_text(json.dumps({"is_main": True}, ensure_ascii=False), encoding="utf-8")
            return SwapState()
        if not state.is_main and state.is_expired(now):
            self.set_main(key)
            return SwapState()
        if state.is_main or not state.preset:
            return SwapState(is_main=True)
        return state

    def set_main(self, key: str) -> None:
        """回写主人格状态。"""

        self._write(key, {"is_main": True})

    def set_preset(
        self,
        key: str,
        preset_name: str,
        *,
        duration_minutes: int,
        source: str,
        now: Optional[float] = None,
    ) -> SwapState:
        """写入"已替换为某预设"状态并返回。"""

        now_value = now if now is not None else time.time()
        duration = max(int(duration_minutes), 0)
        expires_at = (now_value + duration * 60) if duration > 0 else None
        state = SwapState(
            is_main=False,
            preset=preset_name,
            started_at=now_value,
            duration_minutes=duration,
            expires_at=expires_at,
            source=source,
        )
        self._write(
            key,
            {
                "is_main": False,
                "preset": preset_name,
                "started_at": state.started_at,
                "duration_minutes": duration,
                "expires_at": expires_at,
                "source": source,
            },
        )
        return state

    def _write(self, key: str, payload: dict) -> None:
        """落盘单个状态文件（临时文件 + ``os.replace`` 原子替换）。"""

        self._base_dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        self._atomic_write_text(self._state_path(key), text)

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        """把文本原子写入目标文件（同目录临时文件 + os.replace）。

        避免"先截断再写"在并发/崩溃窗口产生半截 JSON；读侧不再需要处理
        "写了一半"的竞态文件（仍保留对历史损坏文件的容错，见 get()）。
        """

        tmp_path = path.with_name(f".{path.name}.tmp")
        tmp_path.write_text(text, encoding="utf-8")
        os.replace(str(tmp_path), str(path))

    # ------------------------------------------------------------------
    # 工具类查询
    # ------------------------------------------------------------------

    def list_swapped(self) -> Dict[str, SwapState]:
        """列出所有处于"非主人格"状态的聊天流（到期惰性回写）。"""

        result: Dict[str, SwapState] = {}
        if not self._base_dir.is_dir():
            return result
        for path in sorted(self._base_dir.glob(f"*{STATE_SUFFIX}")):
            key = path.name[: -len(STATE_SUFFIX)]
            # 跳过非状态文件（权重/debug 存储、原子写临时残留）
            if key in (WEIGHTS_FILE, DEBUG_FILE) or key.startswith("."):
                continue
            state = self.get(key)
            if not state.is_main:
                result[key] = state
        return result

    # ------------------------------------------------------------------
    # 命令设置的权重覆盖
    # ------------------------------------------------------------------

    def load_weight_overrides(self) -> Dict[str, float]:
        """读取 /mps weight 写入的权重覆盖。"""

        path = self._base_dir / f"{WEIGHTS_FILE}{STATE_SUFFIX}"
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        overrides: Dict[str, float] = {}
        for name, value in raw.items():
            try:
                weight = float(value)
            except (TypeError, ValueError):
                continue
            if weight > 0:
                overrides[str(name)] = weight
        return overrides

    def save_weight_override(self, name: str, weight: float) -> None:
        """写入单个权重覆盖（持久化，重启保留）。"""

        current = self.load_weight_overrides()
        if weight > 0:
            current[str(name)] = float(weight)
        else:
            current.pop(str(name), None)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        path = self._base_dir / f"{WEIGHTS_FILE}{STATE_SUFFIX}"
        self._atomic_write_text(path, json.dumps(current, ensure_ascii=False, indent=2) + "\n")

    # ------------------------------------------------------------------
    # debug 模式开关（/mps debug true|false，持久化，重启保留）
    # ------------------------------------------------------------------

    def load_debug_flag(self) -> bool:
        """读取 debug 开关（默认关）。"""

        path = self._base_dir / f"{DEBUG_FILE}{STATE_SUFFIX}"
        if not path.is_file():
            return False
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return bool(raw.get("enabled", False)) if isinstance(raw, dict) else False

    def save_debug_flag(self, enabled: bool) -> None:
        """写入 debug 开关（持久化，重启保留）。"""

        self._base_dir.mkdir(parents=True, exist_ok=True)
        path = self._base_dir / f"{DEBUG_FILE}{STATE_SUFFIX}"
        self._atomic_write_text(path, json.dumps({"enabled": bool(enabled)}, ensure_ascii=False, indent=2) + "\n")

    # ------------------------------------------------------------------
    # 脚本级持久 KV（跨脚本重载 / 插件重启保留，供计数与状态累计）
    # ------------------------------------------------------------------
    #
    # 存 ``personality/script_kv.json``（单文件 {key: value}）。脚本上下文
    # （ctx.kv_get/kv_set/kv_del/kv_keys）转调这里。键名限制单级安全字符；
    # 值仅接受 JSON 可序列化标量/list/dict。同一插件内所有脚本共享该表——
    # 不同脚本用带前缀的键（如 ``nightly_points`` / ``nightly_boost``）避免冲突。
    # ------------------------------------------------------------------

    @staticmethod
    def _valid_script_kv_key(key: str) -> bool:
        """脚本 KV 键名是否合法（单级安全字符 + 长度上限）。"""

        text = str(key or "").strip()
        return bool(text) and len(text) <= _SCRIPT_KV_KEY_MAX and _SCRIPT_KV_KEY_PATTERN.match(text) is not None

    def _script_kv_path(self) -> Path:
        return self._base_dir / f"{SCRIPT_KV_FILE}{STATE_SUFFIX}"

    def _load_script_kv(self) -> Dict[str, Any]:
        """读取脚本 KV 全表；缺失/损坏返回空表（不冒泡异常）。"""

        path = self._script_kv_path()
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save_script_kv(self, table: Dict[str, Any]) -> None:
        """原子写脚本 KV 全表（键按名字典序，文件稳定可读）。"""

        self._base_dir.mkdir(parents=True, exist_ok=True)
        ordered = {k: table[k] for k in sorted(table)}
        self._atomic_write_text(self._script_kv_path(), json.dumps(ordered, ensure_ascii=False, indent=2) + "\n")

    def script_kv_get(self, key: str, default: Any = None) -> Any:
        """读取单个脚本 KV；键非法或不存在返回 default。"""

        if not self._valid_script_kv_key(key):
            return default
        table = self._load_script_kv()
        return table.get(str(key), default)

    def script_kv_set(self, key: str, value: Any) -> bool:
        """写入单个脚本 KV（覆盖）；返回是否成功。

        键非法 / 值不是 JSON 可序列化（str/int/float/bool/list/dict/None）/
        全表条目数超限 → 拒绝并返回 False（不抛异常，脚本可自行 log）。
        """

        if not self._valid_script_kv_key(key):
            return False
        try:
            json.dumps(value)  # 仅校验可序列化
        except (TypeError, ValueError):
            return False
        table = self._load_script_kv()
        if str(key) not in table and len(table) >= _SCRIPT_KV_ITEMS_MAX:
            return False
        table[str(key)] = value
        try:
            self._save_script_kv(table)
        except OSError:
            return False
        return True

    def script_kv_del(self, key: str) -> bool:
        """删除单个脚本 KV；键不存在返回 False。"""

        if not self._valid_script_kv_key(key):
            return False
        table = self._load_script_kv()
        if str(key) not in table:
            return False
        table.pop(str(key), None)
        try:
            self._save_script_kv(table)
        except OSError:
            return False
        return True

    def script_kv_keys(self) -> List[str]:
        """列出脚本 KV 全部键（字典序）。"""

        return list(sorted(self._load_script_kv()))

    def script_kv_clear(self) -> None:
        """清空脚本 KV（调试/重置用）。"""

        self._save_script_kv({})
