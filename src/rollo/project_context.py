"""ProjectContext — 显式的项目上下文边界。

把 workspace 根目录规范化为稳定身份，并一次性解析文件、配置、规则、MCP、
memory、skills 与工具 cwd 的来源；消费方通过该不可变对象读取，不再依赖
``Path.cwd()`` 或导入期路径常量。

本模块对应 OpenSpec change ``introduce-project-context-isolation`` 的
D1—D3、D8 决策，并且**不提供**进程级可变的「当前 workspace」。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ProjectContextError",
    "WorkspaceNotFoundError",
    "AmbiguousWorkspaceError",
    "ProjectContext",
    "resolve_workspace_root",
    "workspace_id_for",
    "require_context",
]


class ProjectContextError(RuntimeError):
    """ProjectContext 解析失败的基类。"""


class WorkspaceNotFoundError(ProjectContextError):
    """workspace 根目录不存在且未获显式创建指示。"""


class AmbiguousWorkspaceError(ProjectContextError):
    """输入无法可靠归一为唯一绝对根。"""


def _normalize(path: Path) -> Path:
    """返回规范化绝对路径。

    仅使用 ``os.path.realpath``：在本目标平台上它已把大小写等价形式与 8.3
    短名解析为真实长名，因此 ``D:\\proj``、``d:/proj/``、``D:\\proj\\sub\\..``
    归一到同一结果，且**与历史 memory 目录所用的 ``str(Path.cwd())`` 写法和
    取值一致**（design D3）。realpath 只是"保证的下界"：实现可能比 spec 的
    等价类更强，但契约以 spec 声明的等价类为准。
    """

    return Path(os.path.realpath(os.fspath(path)))


def workspace_id_for(root: Path) -> str:
    """由规范化 workspace 根派生稳定身份（sha256 前 16 hex）。

    身份只依赖 realpath 结果，因此不随进程当前目录、书写形式或本次算法重构
    而改变既有 workspace 的 memory 目录。
    """

    return hashlib.sha256(str(_normalize(root)).encode("utf-8")).hexdigest()[:16]


def _is_drive_relative(raw: str) -> bool:
    """判断是否为 Windows 驱动器相对路径（如 ``C:foo``）。"""

    if len(raw) < 2 or raw[1] != ":":
        return False
    tail = raw[2:]
    return bool(tail) and tail[0] not in ("\\", "/")


def _process_cwd() -> Path:
    """读取进程当前目录，并把底层失败转换为可诊断的 ProjectContext 错误。

    当进程当前目录已被删除或不可访问时，``Path.cwd()`` 会抛出底层 OSError；
    此时静默回退没有意义，必须显式失败（design D8）。
    """

    try:
        return Path.cwd()
    except OSError as exc:  # pragma: no cover - 由入口失败契约测试覆盖
        raise WorkspaceNotFoundError(
            f"无法读取进程当前目录（可能已被删除或不可访问）：{exc}"
        ) from exc


def resolve_workspace_root(
    workspace: str | os.PathLike[str] | None = None,
    *,
    base: Path | None = None,
    must_exist: bool = True,
) -> Path:
    """把 workspace 解析为规范化绝对根。

    - ``workspace`` 为 ``None`` 时使用 ``base``（缺省为进程当前目录）。
    - 相对路径以 ``base`` 为基准解析；驱动器相对路径（``C:foo``）、含 NUL 的
      输入与空输入被拒绝，因为它们无法可靠归一为唯一绝对根。
    - ``must_exist=True`` 时要求根目录已存在；不静默创建目录。
    """

    if workspace is None:
        anchor = base if base is not None else _process_cwd()
        candidate = anchor
    else:
        raw = os.fspath(workspace)
        if not isinstance(raw, str):
            raise AmbiguousWorkspaceError(f"workspace 输入必须是路径字符串：{workspace!r}")
        if "\x00" in raw:
            raise AmbiguousWorkspaceError("workspace 路径包含 NUL 字符，无法归一")
        if not raw.strip():
            raise AmbiguousWorkspaceError("workspace 路径为空，无法归一")
        if _is_drive_relative(raw):
            raise AmbiguousWorkspaceError(
                f"workspace 为驱动器相对路径，缺少基准而无法可靠归一：{raw}"
            )
        path = Path(raw).expanduser()
        anchor = base if base is not None else _process_cwd()
        candidate = path if path.is_absolute() else anchor / path

    normalized = _normalize(candidate)
    if must_exist and not normalized.is_dir():
        raise WorkspaceNotFoundError(
            f"workspace 根目录不存在或不是目录：{normalized}（不会静默创建）"
        )
    return normalized


@dataclass(frozen=True)
class ProjectContext:
    """单个 workspace 的不可变上下文。

    所有字段在构造时解析完成，其中包括 memory 目录：memory 路径在构造时冻结，
    之后不随文件系统变化而改判，保证同一 workspace 只有一个 memory 根。
    """

    root: Path
    workspace_id: str
    config_root: Path
    runtime_data_dir: Path
    rules_dir: Path
    skills_dir: Path
    agents_dir: Path
    settings_path: Path
    mcp_config_path: Path
    tool_cwd: Path
    memory_root: Path

    def resolve_memory_dir(self) -> Path:
        """返回本 workspace 的 memory 目录。

        取值在构造时已冻结；本方法只是访问器，**不**重新判定文件系统状态，
        因此同一 context 的多次调用结果恒定。消费方 MUST 通过本方法（或该
        冻结字段）取 memory 目录，MUST NOT 用其它推导另建 memory 目录。
        """

        return self.memory_root

    @classmethod
    def from_root(
        cls,
        root: Path | str,
        *,
        must_exist: bool = True,
        runtime_data_dir: str | os.PathLike[str] | None = None,
    ) -> "ProjectContext":
        resolved = resolve_workspace_root(root, must_exist=must_exist)
        data_dir = (
            Path(runtime_data_dir).expanduser()
            if runtime_data_dir is not None
            else Path(os.environ.get("ROLLO_RUNTIME_DIR") or (Path.home() / ".rollo"))
        ).resolve()
        config_root = resolved / ".rollo"
        identity = workspace_id_for(resolved)
        return cls(
            root=resolved,
            workspace_id=identity,
            config_root=config_root,
            runtime_data_dir=data_dir,
            rules_dir=config_root / "rules",
            skills_dir=config_root / "skills",
            agents_dir=config_root / "agents",
            settings_path=config_root / "settings.json",
            mcp_config_path=resolved / ".mcp.json",
            tool_cwd=resolved,
            memory_root=data_dir / "projects" / identity / "memory",
        )


def require_context(context: ProjectContext | None) -> ProjectContext:
    """校验严格调用方显式传入的上下文。

    本模块**不提供**进程级「当前 workspace」：严格模式（入口、未来的 worker 与
    GUI 路径）必须显式传入 ProjectContext，缺少时直接失败，而不是回退到
    ``Path.cwd()``——静默回退会把「选错目录」变成「写错项目」（design D1/D2）。
    """

    if context is None:
        raise ProjectContextError(
            "缺少 ProjectContext：严格模式调用方必须显式传入 workspace 上下文，"
            "本模块不会回退到进程当前目录"
        )
    return context
