"""OFDRW 文本导出命令行工具（ofdrw-text-cli.jar）的调用封装。

把「Java 运行时定位 -> 拉起子进程 -> 解析 JSON 结果 -> 读取文本」这套流程集中在一处，
工具代码只需要关心参数与返回值。

调用链：Dify 工具(Python) -> bin/runtime/bin/java -> bin/ofdrw-text-cli.jar
        -> LayoutTextExtractor（默认，按版式坐标还原阅读顺序）
        -> org.ofdrw.converter.export.TextExporter（--mode raw，历史行为）

关于抽取模式
------------
``layout``（默认）不直接用``TextExporter``，因为它只是「每个 TextCode 打印一行」，
切行不看坐标；而实际 OFD 里大量存在「一个字一个 TextObject」的排版，导出结果会被
打散成大量只含一两个字符的短行。``layout`` 改为按坐标重建版面（详见 Java 侧
``LayoutTextExtractor``），``raw`` 保留旧行为以便对照排查。

关于可执行位
------------
``dify plugin package`` 打出的 zip **不写任何权限信息**（每个条目都是
``create_system=0``、``external_attr=0``），解包后自带的 ``bin/runtime/bin/java``
就是一个普通只读文件，直接 exec 会 ``Permission denied``。
所以 `resolve_java()` 会在返回前确保它可执行：先原地 ``chmod``，实在不行
（目录只读挂载）就把整棵运行时复制到可写目录再补权限位。
打包侧另有 ``scripts/package_offline.py`` 的 ``patch_unix_modes()`` 补上 zip 权限位，
两边互为兜底。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent
BIN_DIR = PLUGIN_ROOT / "bin"
JAR_PATH = BIN_DIR / "ofdrw-text-cli.jar"

_JAVA_EXE = "java.exe" if os.name == "nt" else "java"
BUNDLED_JAVA = BIN_DIR / "runtime" / "bin" / _JAVA_EXE

DEFAULT_TIMEOUT = int(os.environ.get("OFDRW_TIMEOUT", "180"))

# 文本抽取模式：layout = 按版式坐标还原阅读顺序（默认）；raw = 每个 TextCode 一行。
MODE_LAYOUT = "layout"
MODE_RAW = "raw"
MODES = (MODE_LAYOUT, MODE_RAW)

# 运行时被复制到临时目录时，上一次复制的结果可以复用（按进程缓存）。
_EXEC_CACHE: dict[str, str] = {}

# 临时运行时目录的父目录，可用环境变量覆盖（某些容器 /tmp 是 noexec 时需要换地方）。
RUNTIME_CACHE_ENV = "OFDRW_RUNTIME_CACHE"


class OfdrwCliError(RuntimeError):
    """调用 OFDRW 文本导出 CLI 失败。"""


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _ensure_executable(java: Path) -> str | None:
    """确保 java 有可执行位，返回最终能用的路径（失败返回 None）。

    三级策略，从便宜到贵：

    1. 本来就可执行 -> 直接用（正常 Linux 构建 / 权限位完整的包）；
    2. 原地 ``chmod 0755`` 成功 -> 用原路径（插件目录可写，绝大多数情况）；
    3. chmod 失败（目录只读挂载）-> 把整棵运行时复制到临时目录再补权限位。
       **必须是整棵树**：``bin/java`` 通过 rpath ``$ORIGIN/../lib`` 找
       ``lib/server/libjvm.so``，只搬一个文件起来也是残的。

    只对插件自带的运行时做这些动作：系统 Java（PATH / JAVA_HOME 里的）由用户环境负责，
    对它 chmod 属于越界，复制更是无稽之谈。
    """
    key = str(java)
    cached = _EXEC_CACHE.get(key)
    if cached:
        return cached

    if _is_executable(java):
        _EXEC_CACHE[key] = str(java)
        return str(java)

    result = _chmod_in_place(java) or _relocate_runtime(java)
    if result:
        _EXEC_CACHE[key] = result
    return result


def _chmod_in_place(java: Path) -> str | None:
    try:
        os.chmod(java, 0o755)
    except OSError:
        return None
    return str(java) if _is_executable(java) else None


def _relocate_runtime(java: Path) -> str | None:
    """把自带运行时整棵复制到可写目录后补可执行位。

    应对「插件目录只读」这类情况：此时既改不了原文件的权限，也只能另找一块可写空间跑。
    """
    runtime_dir = java.parent.parent
    # jlink 产物根目录一定带 release 文件；没有就说明这不是自带运行时（比如系统 JDK），
    # 绝不能去复制 /usr 这种目录。
    if not (runtime_dir / "release").is_file():
        return None

    rel = java.relative_to(runtime_dir)
    cache_root = Path(os.environ.get(RUNTIME_CACHE_ENV) or tempfile.gettempdir())
    fingerprint = hashlib.sha1(str(runtime_dir).encode("utf-8")).hexdigest()[:12]
    dest = cache_root / f"ofdrw-runtime-{fingerprint}"
    target = dest / rel

    if not _is_executable(target):
        # 先拷到同级的临时目录，补好权限再整体改名发布，避免并发调用读到半成品。
        staging = dest.with_name(f"{dest.name}.{os.getpid()}")
        try:
            cache_root.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(staging, ignore_errors=True)
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(runtime_dir, staging, symlinks=True)
            os.chmod(staging / rel, 0o755)
            staging.replace(dest)
        except OSError:
            shutil.rmtree(staging, ignore_errors=True)
            return None

    return str(target) if _is_executable(target) else None


def resolve_java() -> str:
    """定位可用的 Java 可执行文件。

    优先级：环境变量 OFDRW_JAVA_HOME / JAVA_HOME -> 插件自带运行时 -> PATH 中的 java。

    对自己的运行时附带一次「确保可执行」的修正（见 `_ensure_executable`）。
    """
    candidates: list[Path] = []

    env_home = os.environ.get("OFDRW_JAVA_HOME") or os.environ.get("JAVA_HOME")
    if env_home:
        candidates.append(Path(env_home) / "bin" / _JAVA_EXE)
        candidates.append(Path(env_home) / "jre" / "bin" / _JAVA_EXE)
        # 这几个是别人的 Java，不动它的权限位。

    # 插件自带运行时是默认且最可靠的选择，因此排在 PATH 之前。
    candidates.append(BUNDLED_JAVA)

    for candidate in candidates:
        if not candidate.is_file():
            continue
        if candidate == BUNDLED_JAVA:
            ensured = _ensure_executable(candidate)
            if ensured:
                return ensured
            # 权限修不回来时不要静默回退到别的 java：版本不受控，问题更难查。
            raise OfdrwCliError(
                f"自带 Java 运行时无法执行（缺少可执行位且无法修正）: {candidate}。"
                "请检查插件目录是否可写；若容器 /tmp 为 noexec，可用环境变量 "
                f"{RUNTIME_CACHE_ENV} 指定一个可执行目录。"
            )
        return str(candidate)

    found = shutil.which("java")
    if found:
        return found

    raise OfdrwCliError(
        "未找到可用的 Java 运行时。请确认插件包内 bin/runtime/bin/"
        f"{_JAVA_EXE} 存在，或配置环境变量 OFDRW_JAVA_HOME 指向某个 JDK/JRE。"
    )


def check_environment() -> dict[str, Any]:
    """检查自带运行时与 jar 是否就绪，用于安装时的凭据校验与自检。

    可执行位也算「就绪」的一部分：zip 解包丢权限是必现的坑，让它出现在安装自检里，
    比等到用户第一次转换才报错要好。`resolve_java()` 会顺手尝试修复权限，
    所以这里同时报告修复前与修复后的状态。
    """
    info: dict[str, Any] = {
        "jar": str(JAR_PATH),
        "jar_exists": JAR_PATH.is_file(),
        "bundled_runtime": BUNDLED_JAVA.is_file(),
        "bundled_java_executable": _is_executable(BUNDLED_JAVA),
    }
    missing: list[str] = []

    try:
        java = resolve_java()
    except OfdrwCliError as e:
        java = None
        info["error"] = str(e)
        missing.append(str(BUNDLED_JAVA))

    if java:
        info["java"] = java
        info["java_executable"] = _is_executable(Path(java))
        info["java_relocated"] = java != str(BUNDLED_JAVA)

    if not JAR_PATH.is_file():
        missing.append(str(JAR_PATH))

    info["missing"] = missing
    return info


def _java_command(
    java: str,
    ofd_path: Path,
    txt_path: Path,
    result_path: Path,
    pages: str | None,
    mode: str,
) -> list[str]:
    cmd = [
        java,
        # 内部按 UTF-8 写文件，这里固定为 UTF-8，避免中文乱码。
        "-Dfile.encoding=UTF-8",
        "-Dsun.jnu.encoding=UTF-8",
        # 纯文本提取不需要图形环境，强制 headless 以避免无显示环境下初始化 AWT 失败。
        "-Djava.awt.headless=true",
        "-jar",
        str(JAR_PATH),
        "--input",
        str(ofd_path),
        "--output",
        str(txt_path),
        "--result",
        str(result_path),
        "--mode",
        mode,
    ]
    if pages:
        cmd += ["--pages", pages]
    return cmd


def normalize_mode(value: Any) -> str:
    """把用户输入归一化为合法模式；无法识别时回落到 layout。"""
    text = str(value or "").strip().lower()
    return text if text in MODES else MODE_LAYOUT


def convert_to_text(
    ofd_path: Path,
    txt_path: Path,
    *,
    pages: str | None = None,
    mode: str = MODE_LAYOUT,
    timeout: int | None = None,
) -> dict[str, Any]:
    """把 OFD 文件转换为 UTF-8 纯文本。

    :param ofd_path: 输入 OFD 文件路径
    :param txt_path: 输出纯文本路径
    :param pages: 页码表达式（1 起，支持 ``1,3,5-7``），None 表示全部页
    :param mode: ``layout``（默认，按版式坐标还原阅读顺序）或 ``raw``
    :param timeout: 子进程超时时间（秒）
    :return: 包含 ``text`` 与 ``meta`` 的字典
    """
    if not JAR_PATH.is_file():
        raise OfdrwCliError(f"缺少转换程序: {JAR_PATH}")

    mode = normalize_mode(mode)
    java = resolve_java()
    result_path = txt_path.with_suffix(txt_path.suffix + ".result.json")
    timeout = timeout or DEFAULT_TIMEOUT

    cmd = _java_command(java, ofd_path, txt_path, result_path, pages, mode)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise OfdrwCliError(f"OFD 文本导出超时（超过 {timeout} 秒）。") from e
    except OSError as e:
        raise OfdrwCliError(f"无法启动 Java 进程: {e}") from e

    stdout = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()

    payload = _load_result(result_path, stdout)

    if payload is None:
        detail = stderr or stdout or f"退出码 {proc.returncode}"
        raise OfdrwCliError(f"OFD 文本导出失败: {detail}")

    if not payload.get("ok"):
        error = payload.get("error") or {}
        message = error.get("message") or "未知错误"
        raise OfdrwCliError(f"OFD 文本导出失败: {message}")

    output_path = Path(payload.get("output") or txt_path)
    if not output_path.is_file():
        raise OfdrwCliError(f"导出结果文件不存在: {output_path}")

    text = output_path.read_text(encoding="utf-8", errors="replace")

    meta = {
        "mode": payload.get("mode") or mode,
        "page_count": payload.get("pageCount"),
        "exported_page_count": payload.get("exportedPageCount"),
        "char_count": payload.get("charCount"),
        "line_count": payload.get("lineCount"),
        "ignored_pages": payload.get("ignoredPages") or [],
        "elapsed_ms": payload.get("elapsedMs"),
        "output_file": output_path.name,
        "empty": payload.get("empty"),
        "cli_version": payload.get("version"),
        "java": java,
    }
    # layout 模式额外给出排版诊断信息：展开出的字符图元数，以及因缺坐标信息
    # （无 Boundary / 旋转 CTM，如某些采用字形索引或畸变坐标系的 OFD）
    # 而只能按整块处理的对象数。
    if payload.get("glyphCount") is not None:
        meta["glyph_count"] = payload.get("glyphCount")
        meta["unpositioned_objects"] = payload.get("unpositionedObjects")
    if not meta["empty"] and not text.strip():
        meta["empty"] = True

    return {"text": text, "meta": meta, "stderr": stderr}


def _load_result(result_path: Path, stdout: str) -> dict[str, Any] | None:
    """优先读取 --result 指定的 JSON 文件，回退到解析 stdout 的最后一行 JSON。"""
    if result_path.is_file():
        try:
            return json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None
