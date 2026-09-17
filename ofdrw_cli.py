"""OFDRW 文本导出命令行工具（ofdrw-text-cli.jar）的调用封装。

把「Java 运行时定位 -> 拉起子进程 -> 解析 JSON 结果 -> 读取文本」这套流程集中在一处，
工具代码只需要关心参数与返回值。

调用链：Dify 工具(Python) -> bin/runtime/bin/java -> bin/ofdrw-text-cli.jar
        -> org.ofdrw.converter.export.TextExporter
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent
BIN_DIR = PLUGIN_ROOT / "bin"
JAR_PATH = BIN_DIR / "ofdrw-text-cli.jar"

_JAVA_EXE = "java.exe" if os.name == "nt" else "java"
BUNDLED_JAVA = BIN_DIR / "runtime" / "bin" / _JAVA_EXE

DEFAULT_TIMEOUT = int(os.environ.get("OFDRW_TIMEOUT", "180"))


class OfdrwCliError(RuntimeError):
    """调用 OFDRW 文本导出 CLI 失败。"""


def resolve_java() -> str:
    """定位可用的 Java 可执行文件。

    优先级：环境变量 OFDRW_JAVA_HOME / JAVA_HOME -> 插件自带运行时 -> PATH 中的 java。
    """
    candidates: list[Path] = []

    env_home = os.environ.get("OFDRW_JAVA_HOME") or os.environ.get("JAVA_HOME")
    if env_home:
        candidates.append(Path(env_home) / "bin" / _JAVA_EXE)
        candidates.append(Path(env_home) / "jre" / "bin" / _JAVA_EXE)

    # 插件自带运行时是默认且最可靠的选择，因此排在 PATH 之前。
    candidates.append(BUNDLED_JAVA)

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    found = shutil.which("java")
    if found:
        return found

    raise OfdrwCliError(
        "未找到可用的 Java 运行时。请确认插件包内 bin/runtime/bin/"
        f"{_JAVA_EXE} 存在，或配置环境变量 OFDRW_JAVA_HOME 指向某个 JDK/JRE。"
    )


def check_environment() -> dict[str, Any]:
    """检查自带运行时与 jar 是否就绪，用于安装时的凭据校验与自检。"""
    java = resolve_java()
    missing: list[str] = []
    if not JAR_PATH.is_file():
        missing.append(str(JAR_PATH))
    if not BUNDLED_JAVA.is_file() and java == str(BUNDLED_JAVA):
        missing.append(str(BUNDLED_JAVA))
    return {
        "java": java,
        "jar": str(JAR_PATH),
        "jar_exists": JAR_PATH.is_file(),
        "bundled_runtime": BUNDLED_JAVA.is_file(),
        "missing": missing,
    }


def _java_command(
    java: str,
    ofd_path: Path,
    txt_path: Path,
    result_path: Path,
    pages: str | None,
) -> list[str]:
    cmd = [
        java,
        # TextExporter 内部使用 JVM 默认字符集写文件，这里固定为 UTF-8，避免中文乱码。
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
    ]
    if pages:
        cmd += ["--pages", pages]
    return cmd


def convert_to_text(
    ofd_path: Path,
    txt_path: Path,
    *,
    pages: str | None = None,
    timeout: int | None = None,
) -> dict[str, Any]:
    """把 OFD 文件转换为 UTF-8 纯文本。

    :param ofd_path: 输入 OFD 文件路径
    :param txt_path: 输出纯文本路径
    :param pages: 页码表达式（1 起，支持 ``1,3,5-7``），None 表示全部页
    :param timeout: 子进程超时时间（秒）
    :return: 包含 ``text`` 与 ``meta`` 的字典
    """
    if not JAR_PATH.is_file():
        raise OfdrwCliError(f"缺少转换程序: {JAR_PATH}")

    java = resolve_java()
    result_path = txt_path.with_suffix(txt_path.suffix + ".result.json")
    timeout = timeout or DEFAULT_TIMEOUT

    cmd = _java_command(java, ofd_path, txt_path, result_path, pages)

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
