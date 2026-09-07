"""人格预设的磁盘存取。

格式选择：预设正文（表达方式/人格约束/行为风格）是多行长文本，JSON 只能把
换行存成 ``\\n`` 转义、可读性差；本插件改用 **TOML 多行基本字符串**
（``\"\"\"…\"\"\"``）存储——文件里就是真实换行，用户可直接编辑，读取时
换行原样保留（tomllib 标准库解析，写侧用 MaiBot venv 自带的 tomlkit
``multiline=True``）。

目录布局（相对官方插件数据目录 ``ctx.paths.data_dir``）：

    preset/<预设名称>.toml      预设文件（名称即文件名，不带扩展名）
    preset/backup/<名称>_<时间戳>.toml   同名覆盖前的自动备份
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import tomlkit

PRESET_SUFFIX = ".toml"
BACKUP_DIR_NAME = "backup"
NAME_PATTERN = re.compile(r"^[\w\u4e00-\u9fff\-]{1,64}$")
BACKUP_STAMP_FORMAT = "%Y%m%d-%H%M%S"


@dataclass(slots=True)
class PersonaPreset:
    """一个可切换的人格预设。

    Attributes:
        name: 预设名称（即文件名，不含扩展名）。
        persona_name: 该人格下 bot 的自称名（可覆盖官方昵称；空=沿用官方
            bot.nickname）。覆盖注入重建 system 身份段的名字行时使用。
        expression: 注入表达（怎么说）。
        persona: 人格约束（我是谁/红线）。
        behavior: 行为风格（何时说/参与姿态，注入 planner）。
        duration_minutes: 生效时长（分钟）；0 表示一直替换。
        created_at: 创建时间（ISO 字符串，仅作记录）。
    """

    name: str
    persona_name: str = ""
    expression: str = ""
    persona: str = ""
    behavior: str = ""
    duration_minutes: int = 0
    created_at: str = ""

    def summary_lines(self) -> List[str]:
        """生成回显用的摘要行（合并转发节点内容）。"""

        lines = [f"【预设】{self.name}", f"时长：{'永久' if self.duration_minutes <= 0 else f'{self.duration_minutes} 分钟'}"]
        if str(self.persona_name or "").strip():
            lines.append(f"— 人格名字：{self.persona_name.strip()}（覆盖官方昵称）—")
        lines.append("— 注入表达 —")
        lines.append(self.expression.strip() or "（空）")
        lines.append("— 人格约束 —")
        lines.append(self.persona.strip() or "（空）")
        lines.append("— 行为风格 —")
        lines.append(self.behavior.strip() or "（空）")
        return lines


def validate_preset_name(name: str) -> bool:
    """预设名合法性：字母/数字/下划线/连字符/中文，1~64 位（防路径穿越）。"""

    return bool(NAME_PATTERN.match(str(name or "").strip()))


class PresetStore:
    """预设文件存取与备份轮转。"""

    def __init__(self, base_dir: Path, backup_limit: int = 5) -> None:
        """绑定预设根目录（…/preset）与备份上限。"""

        self._base_dir = Path(base_dir)
        self._backup_limit = max(int(backup_limit), 0)

    @property
    def base_dir(self) -> Path:
        """预设目录。"""

        return self._base_dir

    @property
    def backup_dir(self) -> Path:
        """备份目录（…/preset/backup）。"""

        return self._base_dir / BACKUP_DIR_NAME

    def _preset_path(self, name: str) -> Path:
        """单个预设文件路径。

        名称先过 ``validate_preset_name`` 白名单：非法名（含路径分隔符 / ``..`` /
        超长 / 非法字符）统一映射到 ``_invalid_<hash>`` 前缀——该文件不存在，
        使 ``load``/``exists`` 对任意外部输入天然返回 None/False，杜绝路径穿越
        读取（写入侧由命令层校验，读取侧这里兜底）。
        """

        normalized = str(name or "").strip()
        if not validate_preset_name(normalized):
            # 非法名：映射到不可能存在的安全文件名（保留可复现性便于排查）
            normalized = "_invalid_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
        return self._base_dir / f"{normalized}{PRESET_SUFFIX}"

    def list_names(self) -> List[str]:
        """列出全部预设名称（按文件名排序）。"""

        if not self._base_dir.is_dir():
            return []
        return sorted(
            path.name[: -len(PRESET_SUFFIX)]
            for path in self._base_dir.glob(f"*{PRESET_SUFFIX}")
            if path.is_file()
        )

    def exists(self, name: str) -> bool:
        """预设是否存在。"""

        return self._preset_path(name).is_file()

    def load(self, name: str) -> Optional[PersonaPreset]:
        """读取预设；文件缺失或损坏返回 None（损坏时记录到 error 字段语义由调用方处理）。

        ``name``（预设文件名）恒为返回预设的权威名——TOML 内部的 ``name`` 字段
        仅作展示/迁移用，不一致时以文件名为准（避免状态文件/注入记录与
        ``list_names()`` 错位）。
        """

        path = self._preset_path(name)
        if not path.is_file():
            return None
        try:
            with open(path, "rb") as fh:
                raw = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError):
            return None
        return PersonaPreset(
            name=str(name),
            persona_name=str(raw.get("persona_name") or ""),
            expression=str(raw.get("expression") or ""),
            persona=str(raw.get("persona") or ""),
            behavior=str(raw.get("behavior") or ""),
            duration_minutes=_to_int(raw.get("duration_minutes"), 0),
            created_at=str(raw.get("created_at") or ""),
        )

    def save(self, preset: PersonaPreset, *, backup_existing: bool = True) -> Optional[Path]:
        """写入预设；同名且允许备份时先把旧文件挪进备份目录，返回备份路径。"""

        self._base_dir.mkdir(parents=True, exist_ok=True)
        path = self._preset_path(preset.name)
        backup_path: Optional[Path] = None
        if backup_existing and path.is_file():
            backup_path = self._backup_existing(preset.name)
        doc = tomlkit.document()
        doc["name"] = preset.name
        if str(preset.persona_name or "").strip():
            doc["persona_name"] = str(preset.persona_name).strip()
        # multiline=True：正文里的换行写成真实换行（""" 块），文件可读、可手工编辑
        doc["expression"] = tomlkit.string(preset.expression, multiline=True)
        doc["persona"] = tomlkit.string(preset.persona, multiline=True)
        doc["behavior"] = tomlkit.string(preset.behavior, multiline=True)
        doc["duration_minutes"] = int(preset.duration_minutes)
        doc["created_at"] = preset.created_at or datetime.now().astimezone().isoformat()
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(tomlkit.dumps(doc))
        return backup_path

    def _backup_existing(self, name: str) -> Optional[Path]:
        """备份旧预设并按上限轮转，返回备份文件路径。"""

        source = self._preset_path(name)
        if not source.is_file():
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime(BACKUP_STAMP_FORMAT)
        target = self.backup_dir / f"{name}_{stamp}{PRESET_SUFFIX}"
        # 同秒重复保存时追加序号，避免覆盖
        counter = 1
        while target.exists():
            target = self.backup_dir / f"{name}_{stamp}_{counter}{PRESET_SUFFIX}"
            counter += 1
        target.write_bytes(source.read_bytes())
        self._prune_backups(name)
        return target

    def _prune_backups(self, name: str) -> None:
        """把同一预设的备份裁剪到上限（删除最旧的）。"""

        if self._backup_limit <= 0:
            return
        backups = sorted(
            path
            for path in self.backup_dir.glob(f"{name}_*{PRESET_SUFFIX}")
            if path.is_file()
        )
        overflow = len(backups) - self._backup_limit
        for path in backups[: max(overflow, 0)]:
            path.unlink(missing_ok=True)

    def delete(self, name: str, *, backup: bool = True) -> Optional[Path]:
        """删除预设文件；删除前先把当前文件备份到 backup/（同覆盖前备份机制）。

        Returns:
            备份文件路径（未备份或文件不存在时返回 None）。
            删除后可用 ``exists(name)`` 确认 False。
        """

        path = self._preset_path(name)
        if not path.is_file():
            return None
        backup_path: Optional[Path] = None
        if backup:
            backup_path = self._backup_existing(name)
        path.unlink(missing_ok=True)
        return backup_path


def _to_int(value: object, default: int) -> int:
    """宽松转 int；失败取默认。"""

    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
