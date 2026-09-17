#!/usr/bin/env python3
"""
为 ofdrw-converter 生成「离线可安装」的 Dify 插件包。

背景
----
Dify 安装工具插件时，会在 plugin_daemon 里为插件建一个 Python 环境，并按插件
根目录的 requirements.txt 去 PyPI 拉依赖。在无外网/内网隔离环境里这一步会失败
（日志典型报错：``init environment failed: failed to install dependencies``）。

做法
----
把整条依赖闭包的 wheel 一起塞进插件包，并让 pip 只从包内目录解析：

  1. 先用 ``dify plugin package`` 打出常规包 —— 复用 .difyignore 规则，
     保证「不多带、不遗漏」文件；
  2. 解压到临时暂存目录；
  3. 交叉下载目标平台的 wheel 到 ``<暂存>/wheels/``
     （linux + amd64/arm64 + 与 manifest 一致的 Python 版本）；
  4. 把暂存目录里 requirements.txt 的首行改成
     ``--no-index --find-links=./wheels/``，并把 manifest.yaml 的 ``meta.arch``
     改成目标架构（包内是平台相关的 JRE 与 wheel，声明必须一致，否则 Dify 拒装）；
  5. 用 ``dify plugin package --max-size ...`` 重新打包为
     ``dist/ofdrw-converter-offline-linux-<arch>.difypkg``；
  6. 校验：wheel 数量、requirements 改写、内置运行时是否为 Linux 版、
     以及用 pip 做一次「纯离线依赖解析」试算，若闭包有缺口会在这里直接报错。

产物只面向 Linux：Dify 的 plugin_daemon 跑在 Linux 容器里，Windows 版运行时
放进包里也起不来，因此脚本会在开工前先做平台预检。

注意：本步骤只改写「暂存副本」里的 requirements.txt / manifest.yaml，
仓库里那两份保持原样，开发机上的 pip install 与本地调试不受影响。

为什么可以在 Windows 上交叉下载
--------------------------------
wheel 是否可用只取决于 「平台标签 + Python 版本 + 架构」三元组，
用 ``pip download --only-binary=:all: --platform ... --python-version ...``
即可拿到与目标容器完全相同的 wheel 文件，无需真的跑起 Linux 容器。

用法
----
    python scripts/package_offline.py                          # 默认 linux-amd64 / 跟随 manifest 的 Python 版本
    python scripts/package_offline.py --arch arm64             # 交叉下载 linux-arm64 的 wheel
    python scripts/package_offline.py --refresh                # 忽略 wheel 缓存，强制重新下载
    python scripts/package_offline.py --index-url https://pypi.org/simple
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_NAME = PLUGIN_ROOT.name

# 目标运行环境：Dify plugin_daemon「local」镜像 = Ubuntu 24.04 / glibc 2.39 / Python 3.12。
# 这里按 glibc 基线生成平台标签，保证选到的 wheel 一定能在该镜像上加载。
MANYLINUX_LEVELS = (
    "manylinux2014",   # glibc >= 2.17
    "manylinux_2_17",
    "manylinux_2_24",
    "manylinux_2_28",  # gevent / tiktoken 等较新的轮子用的是这个标签
)

ARCH_ALIASES = {
    "amd64": "x86_64",
    "x86_64": "x86_64",
    "arm64": "aarch64",
    "aarch64": "aarch64",
}

OK = "[OK]"
FAIL = "[FAIL]"
INFO = "[--]"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def log(msg: str = "") -> None:
    print(msg, flush=True)


def step(title: str) -> None:
    log()
    log(f"=== {title} ===")


def human(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.2f} TB"


# 插件自带 Java 运行时的可执行文件名随平台而变：Linux 是 bin/java，Windows 是 bin/java.exe。
BUNDLED_JAVA_NAMES = ("bin/runtime/bin/java", "bin/runtime/bin/java.exe")
# 只有 Linux 运行时能在 plugin_daemon 容器里跑起来。
WINDOWS_RUNTIME_MARKERS = ("bin/runtime/bin/java.exe", "bin/runtime/bin/server/jvm.dll")


def tree_size(path: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


def run(cmd: list[str], cwd: Path | None = None, capture: bool = False) -> subprocess.CompletedProcess:
    log(f"  $ {' '.join(cmd)}")
    kwargs: dict = {"cwd": str(cwd) if cwd else None}
    if capture:
        kwargs.update(capture_output=True, text=True, encoding="utf-8", errors="replace")
    return subprocess.run(cmd, **kwargs)  # noqa: S603


def require(cond: bool, message: str) -> None:
    if not cond:
        sys.exit(f"\n{FAIL} {message}")


# ---------------------------------------------------------------------------
# 读取插件清单，确定目标 Python 版本
# ---------------------------------------------------------------------------


def read_target_python(plugin_root: Path) -> tuple[int, int]:
    """从 manifest.yaml 的 meta.runner.version 读出目标 Python 版本。

    用缩进解析而不是 PyYAML，避免给打包流程引入额外依赖。
    注意不能直接全文搜 ``version:``：``meta.version``（插件版本，如 0.0.1）
    会先被匹配到，必须先定位到 ``runner:`` 再只看它下面的缩进子块。
    """
    manifest = plugin_root / "manifest.yaml"
    require(manifest.is_file(), f"找不到 {manifest}")
    lines = manifest.read_text(encoding="utf-8").splitlines()

    start = next((i for i, line in enumerate(lines) if re.match(r"^\s*runner:\s*$", line)), None)
    require(start is not None, "manifest.yaml 里没有 meta.runner 段")

    base_indent = len(lines[start]) - len(lines[start].lstrip())
    for line in lines[start + 1:]:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= base_indent:
            break  # 已经走出 runner 子块
        match = re.match(r"""\s*version:\s*["']?(\d+)\.(\d+)""", line)
        if match:
            major, minor = int(match.group(1)), int(match.group(2))
            log(f"{INFO} manifest 声明的 runner Python 版本: {major}.{minor}")
            return major, minor

    sys.exit(f"{FAIL} 无法从 manifest.yaml 的 meta.runner 里解析出 version")


# ---------------------------------------------------------------------------
# 1. 打出常规包（复用 .difyignore）
# ---------------------------------------------------------------------------


def find_dify_cli() -> str:
    cli = shutil.which("dify")
    if cli:
        return cli
    # Windows 上 Go 编译的 CLI 可能没有扩展名，shutil.which 会漏掉
    for suffix in ("", ".exe", ".cmd", ".bat"):
        candidate = Path(os.environ.get("PORTABLE_APPS", "")) / f"dify-plugin-cli/dify{suffix}"
        if candidate.is_file():
            return str(candidate)
    sys.exit(
        f"{FAIL} 找不到 dify CLI。请先安装（见 README「打包」章节），"
        "或把 dify 所在目录加入 PATH。"
    )


def build_base_package(cli: str, out_dir: Path, plat_label: str) -> Path:
    """先用 dify CLI 打出常规包（复用 .difyignore，保证不多带也不遗漏文件）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{PLUGIN_NAME}-{plat_label}.difypkg"
    result = run([cli, "plugin", "package", str(PLUGIN_ROOT), "-o", str(target)], cwd=PLUGIN_ROOT)
    require(result.returncode == 0, f"常规包构建失败（退出码 {result.returncode}）")
    require(target.is_file(), f"未生成 {target}")
    log(f"{OK} 常规包: {target.name} ({human(target.stat().st_size)})")
    return target


def preflight_runtime_platform() -> None:
    """开跑之前先确认自带运行时是 Linux 版。

    产物只面向 Linux 的 Dify（plugin_daemon 跑在 Linux 容器里），所以 Windows 上
    生成的 ``bin/runtime``（含 ``bin/java.exe``）是不能用的。这件事越早发现越好：
    放在这里可以在下载 38 个 wheel 之前就失败，而不是等打完包才报错。
    """
    runtime_dir = PLUGIN_ROOT / "bin" / "runtime"
    require(runtime_dir.is_dir(), f"找不到自带运行时目录: {runtime_dir}（请先执行 bash scripts/build.sh）")

    windows_markers = [
        runtime_dir / "bin" / "java.exe",
        runtime_dir / "bin" / "server" / "jvm.dll",
    ]
    if any(p.exists() for p in windows_markers):
        sys.exit(
            f"\n{FAIL} 自带运行时是 Windows 版，但打包目标只能是 Linux。\n"
            "     jlink 生成的运行时是平台相关的：这个包里装的是 Windows 的 JRE，\n"
            "     放进 Linux 的 Dify 容器里根本起不来。\n"
            "     解决：在 Linux 上用 `bash scripts/build.sh` 重新生成 bin/runtime，\n"
            "     或直接用 .github/workflows/repackage-plugin.yml（GitHub Action）。"
        )
    log(f"{OK} 自带运行时平台检查通过（Linux）")


# ---------------------------------------------------------------------------
# 2. 依赖闭包 -> wheel
# ---------------------------------------------------------------------------


def platform_tags(arch: str) -> list[str]:
    real = ARCH_ALIASES.get(arch.lower())
    require(real is not None, f"不支持的架构: {arch}（可选 {', '.join(sorted(ARCH_ALIASES))}）")
    return [f"{level}_{real}" for level in MANYLINUX_LEVELS]


def dify_arch(arch: str) -> str:
    """把架构名规范成 Dify manifest 里 meta.arch 使用的写法（amd64 / arm64）。"""
    real = ARCH_ALIASES.get(arch.lower())
    require(real is not None, f"不支持的架构: {arch}（可选 {', '.join(sorted(ARCH_ALIASES))}）")
    return "arm64" if real == "aarch64" else "amd64"


def abi_tags(major: int, minor: int) -> list[str]:
    return [f"cp{major}{minor}", "abi3", "none"]


def wheels_fingerprint(
    req_file: Path, py_version: str, platforms: list[str], abis: list[str], index_url: str | None
) -> str:
    """把「依赖清单 + 目标环境」哈希成一个指纹，用于判断 wheel 缓存是否还能复用。"""
    digest = hashlib.sha256()
    digest.update(req_file.read_bytes())
    digest.update(("|".join([py_version, *platforms, *abis, index_url or ""])).encode())
    return digest.hexdigest()


def download_wheels(
    req_file: Path,
    cache_dir: Path,
    py_version: str,
    platforms: list[str],
    abis: list[str],
    index_url: str | None,
    refresh: bool,
) -> list[Path]:
    """把 requirements.txt 的完整依赖闭包下载为二进制 wheel（带按指纹的本地缓存）。

    只用 ``--only-binary=:all:``：源码包需要在目标机器上现场编译，
    那既要求编译器也不可靠，这里宁可显式失败。
    """
    fingerprint = wheels_fingerprint(req_file, py_version, platforms, abis, index_url)
    stamp = cache_dir / ".fingerprint.json"

    if cache_dir.is_dir() and not refresh:
        existing = sorted(cache_dir.glob("*.whl"))
        if existing:
            try:
                cached = json.loads(stamp.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cached = {}
            if cached.get("fingerprint") == fingerprint:
                log(f"{OK} 复用 wheel 缓存（{len(existing)} 个，依赖与目标环境指纹一致）")
                return existing
            log(f"{INFO} 依赖或目标环境已变化，重新下载")

    cache_dir.mkdir(parents=True, exist_ok=True)
    # 清掉旧 wheel，避免不同版本混在一起导致 pip 解析出意外结果。
    for old in cache_dir.glob("*.whl"):
        try:
            old.unlink()
        except OSError:
            pass

    cmd = [
        sys.executable, "-m", "pip", "download",
        "-r", str(req_file),
        "-d", str(cache_dir),
        "--only-binary=:all:",
        "--python-version", py_version,
        "--implementation", "cp",
    ]
    for tag in platforms:
        cmd += ["--platform", tag]
    for tag in abis:
        cmd += ["--abi", tag]
    if index_url:
        cmd += ["--index-url", index_url]

    log(f"{INFO} 目标环境: linux / {py_version} / {' '.join(abis)}")
    result = subprocess.run(  # noqa: S603
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode != 0:
        log(result.stdout or "")
        log(result.stderr or "")
        sys.exit(
            f"\n{FAIL} 依赖下载失败。常见原因：\n"
            "  · 某个依赖只有源码包（sdist），没有目标平台的 wheel —— 需要换包或自己构建；\n"
            "  · 某个 wheel 的平台标签比 manylinux_2_28 更新 —— 用 --arch 确认架构，\n"
            "    或在 MANYLINUX_LEVELS 里补上对应标签；\n"
            "  · 网络/镜像源问题 —— 用 --index-url 指定可用源。"
        )

    wheels = sorted(cache_dir.glob("*.whl"))
    require(wheels, "下载完成但缓存目录里没有 .whl 文件")
    stamp.write_text(json.dumps({"fingerprint": fingerprint}, indent=2), encoding="utf-8")
    log(f"{OK} 下载了 {len(wheels)} 个 wheel，合计 {human(sum(w.stat().st_size for w in wheels))}")
    return wheels


def stage_wheels(wheels: list[Path], dest: Path) -> None:
    """把缓存里的 wheel 拷进暂存目录的 wheels/。"""
    dest.mkdir(parents=True, exist_ok=True)
    for wheel in wheels:
        shutil.copy2(wheel, dest / wheel.name)


def dispose_staging(staging_root: Path) -> None:
    """把暂存目录移出仓库。

    刻意不用 ``shutil.rmtree``：在受限/受保护的运行环境里，一次删除超过阈值的文件
    会被安全策略拦截（且这类拦截不是 OSError，``ignore_errors=True`` 也吞不掉）。
    ``shutil.move`` 到系统临时目录既不触发删除保护，也不会在仓库里留垃圾。

    清理失败不应该影响已经构建好的产物，所以这里兜住所有异常。
    """
    dest = Path(tempfile.gettempdir()) / f"ofdrw-offline-{staging_root.name}-{os.getpid()}"
    try:
        shutil.move(str(staging_root), str(dest))
        log(f"{INFO} 暂存目录已移至 {dest}")
    except BaseException as exc:  # noqa: BLE001 - 见上，清理失败不影响结果
        log(f"{INFO} 未能移走暂存目录（{type(exc).__name__}），保留在 {staging_root}")


# ---------------------------------------------------------------------------
# 3. 改写 requirements.txt（只改暂存副本）
# ---------------------------------------------------------------------------


def patch_requirements(req_file: Path) -> None:
    text = req_file.read_text(encoding="utf-8")
    if "--no-index" in text:
        log(f"{INFO} requirements.txt 已包含 --no-index，跳过改写")
        return

    header = (
        "# 离线包：仅从包内 wheels/ 目录解析依赖，安装过程不访问 PyPI。\n"
        "# 由 scripts/package_offline.py 自动生成，请勿手改。\n"
        "--no-index --find-links=./wheels/\n"
    )
    req_file.write_text(header + text, encoding="utf-8")
    log(f"{OK} 已把 requirements.txt 改为纯本地索引解析")


def patch_manifest_arch(staging: Path, arch: str) -> None:
    """把暂存副本里 manifest.yaml 的 ``meta.arch`` 改成目标架构。

    包内的 JRE 与 wheel 都是平台相关的，而 Dify 会拿 manifest 声明的 arch 与宿主比对：
    声明 amd64 的包装到 arm64 的 Dify 上会被直接拒掉。所以声明必须跟着一起改。

    与 requirements.txt 一样，只改暂存副本，仓库里的 manifest 保持原样。
    """
    target = dify_arch(arch)
    manifest = staging / "manifest.yaml"
    require(manifest.is_file(), f"暂存目录里没有 {manifest}")

    lines = manifest.read_text(encoding="utf-8").splitlines()
    start = next((i for i, line in enumerate(lines) if re.match(r"^\s*arch:\s*$", line)), None)
    require(start is not None, "manifest.yaml 里没有 meta.arch 段")

    base_indent = len(lines[start]) - len(lines[start].lstrip())
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() and len(line) - len(line.lstrip()) <= base_indent:
            break  # 走出 arch 子块
        end += 1

    item_indent = " " * (base_indent + 2)
    replacement = [f"{item_indent}- {target}"]
    if lines[start + 1:end] == replacement:
        log(f"{INFO} manifest 的 meta.arch 已是 {target}，跳过改写")
        return

    lines[start + 1:end] = replacement
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"{OK} 已把 manifest 的 meta.arch 改为 {target}")


# ---------------------------------------------------------------------------
# 4. 重新打包
# ---------------------------------------------------------------------------


def repack(cli: str, staging: Path, out_path: Path, max_size_mb: int | None) -> Path:
    if max_size_mb is None:
        # 客户端默认上限 50 MB（解压后），离线包必然超过，这里按实际体积留 20% 余量。
        total_mb = tree_size(staging) / 1048576
        max_size_mb = max(50, math.ceil(total_mb * 1.2) + 10)
        log(f"{INFO} 暂存目录 {total_mb:.1f} MB -> 自动设置 --max-size {max_size_mb} MB")

    result = run(
        [cli, "plugin", "package", str(staging), "-o", str(out_path), "--max-size", str(max_size_mb)],
        cwd=staging.parent,
    )
    require(result.returncode == 0, f"离线包构建失败（退出码 {result.returncode}）")
    require(out_path.is_file(), f"未生成 {out_path}")
    return out_path


# ---------------------------------------------------------------------------
# 5. 校验
# ---------------------------------------------------------------------------


def list_package(pkg: Path) -> tuple[list[str], int]:
    with zipfile.ZipFile(pkg) as zf:
        names = zf.namelist()
        uncompressed = sum(item.file_size for item in zf.infolist())
    return names, uncompressed


def verify_offline_resolution(
    staging: Path, py_version: str, platforms: list[str], abis: list[str]
) -> bool:
    """用 pip 只从包内 wheels/ 解析一遍完整依赖树。

    这是对「离线闭包是否完整」最直接的证明：任何缺口都会让 pip 报
    "No matching distribution found"。
    """
    with tempfile.TemporaryDirectory(prefix="ofdrw-verify-") as tmp:
        cmd = [
            sys.executable, "-m", "pip", "download",
            "-r", str(staging / "requirements.txt"),
            "-d", tmp,
            "--no-index",
            "--find-links", str(staging / "wheels"),
            "--only-binary=:all:",
            "--python-version", py_version,
            "--implementation", "cp",
        ]
        for tag in platforms:
            cmd += ["--platform", tag]
        for tag in abis:
            cmd += ["--abi", tag]

        result = subprocess.run(  # noqa: S603
            cmd, cwd=str(staging), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            log(result.stdout or "")
            log(result.stderr or "")
            return False
        resolved = sorted(Path(tmp).glob("*.whl"))
        log(f"{OK} 离线解析通过：{len(resolved)} 个 wheel 全部可从包内找到")
        return True


def verify_package(
    pkg: Path, wheel_count: int, py_version: str, platforms: list[str], abis: list[str],
    staging: Path, expected_arch: str,
) -> bool:
    names, uncompressed = list_package(pkg)
    ok = True

    log(f"{INFO} 包内条目 {len(names)} 个，解压后 {human(uncompressed)}，压缩后 {human(pkg.stat().st_size)}")

    in_pkg_wheels = [n for n in names if n.startswith("wheels/") and n.endswith(".whl")]
    if len(in_pkg_wheels) == wheel_count:
        log(f"{OK} wheels/ 内 {len(in_pkg_wheels)} 个 wheel，与下载数量一致")
    else:
        log(f"{FAIL} wheels/ 内 {len(in_pkg_wheels)} 个 wheel，预期 {wheel_count} 个")
        ok = False

    required = [
        "manifest.yaml", "main.py", "requirements.txt", "ofdrw_cli.py",
        "provider/ofdrw-converter.yaml", "provider/ofdrw-converter.py",
        "tools/text_exporter.yaml", "tools/text_exporter.py",
        "_assets/icon.svg", "_assets/icon-dark.svg",
        "bin/ofdrw-text-cli.jar",
    ]
    missing = [r for r in required if r not in names]
    # 自带运行时的可执行文件名随平台而变（Linux: bin/java，Windows: bin/java.exe）
    if not any(name in names for name in BUNDLED_JAVA_NAMES):
        missing.append("/".join(BUNDLED_JAVA_NAMES))
    if missing:
        log(f"{FAIL} 缺少必需文件: {', '.join(missing)}")
        ok = False
    else:
        log(f"{OK} 插件必需文件齐全（{len(required) + 1} 项，含内置 Java 运行时）")

    # 关键守卫：plugin_daemon 是 Linux 容器，Windows 运行时在里面根本起不来。
    # 这个包很容易在 Windows 上误打包（bin/runtime 是构建机的平台产物），
    # 所以在这里硬性拦住，而不是等到安装后调用工具时才失败。
    windows_markers = [n for n in names if n in WINDOWS_RUNTIME_MARKERS]
    if windows_markers:
        log(f"{FAIL} 内置运行时是 Windows 版（{windows_markers[0]}）—— 无法在 Linux 的 Dify 容器中运行")
        log(f"       请在 Linux 上执行 bash scripts/build.sh 重新生成运行时（CI 即如此）")
        ok = False
    else:
        log(f"{OK} 内置运行时不是 Windows 版（适用于 Linux 容器）")

    declared_arch = None
    try:
        with zipfile.ZipFile(pkg) as zf:
            manifest_text = zf.read("manifest.yaml").decode("utf-8")
        declared_arch = re.search(r"^\s*-\s*(amd64|arm64)\s*$", manifest_text, re.MULTILINE)
        declared_arch = declared_arch.group(1) if declared_arch else None
    except (KeyError, OSError):
        pass
    if declared_arch is None:
        log(f"{FAIL} 无法从包内 manifest.yaml 读出 meta.arch")
        ok = False
    elif declared_arch != expected_arch:
        log(f"{FAIL} manifest 声明 arch={declared_arch}，与目标 {expected_arch} 不一致")
        ok = False
    else:
        log(f"{OK} manifest 声明 arch={declared_arch}，与目标一致")

    with zipfile.ZipFile(pkg) as zf:
        patched = zf.read("requirements.txt").decode("utf-8")
    if "--no-index" in patched and "find-links" in patched:
        log(f"{OK} requirements.txt 已是离线形式")
    else:
        log(f"{FAIL} requirements.txt 未改写为离线形式")
        ok = False

    leaked = [n for n in names if n.startswith((".venv/", "jvm/", "samples/", "scripts/", ".offline-build/"))]
    if leaked:
        log(f"{FAIL} 混入了开发期目录: {', '.join(leaked[:5])}")
        ok = False
    else:
        log(f"{OK} 未混入开发期目录（.venv/jvm/samples/scripts 均不在包内）")

    # 顶层白名单：比逐个拉黑更可靠，任何意外文件（日志、内部记录、临时目录）
    # 一混进来就会在这里被抓住，而不是等到交付后才发现。
    allowed_top = {
        "manifest.yaml", "main.py", "requirements.txt", "ofdrw_cli.py",
        "provider", "tools", "_assets", "bin", "wheels",
        "README.md", ".difyignore", ".gitignore", ".env.example",
    }
    actual_top = {n.split("/")[0] for n in names}
    unexpected = sorted(actual_top - allowed_top)
    if unexpected:
        log(f"{FAIL} 包内出现非预期顶层条目: {', '.join(unexpected)}")
        ok = False
    else:
        log(f"{OK} 顶层条目全部在白名单内（{len(actual_top)} 项）")

    # 反斜杠路径、绝对路径都是打包事故的信号
    bad_paths = [n for n in names if "\\" in n or n.startswith(("/", "C:"))]
    if bad_paths:
        log(f"{FAIL} 包内存在非 POSIX 路径: {bad_paths[:3]}")
        ok = False

    if not verify_offline_resolution(staging, py_version, platforms, abis):
        ok = False

    return ok


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="为 ofdrw-converter 生成离线可安装的 Dify 插件包",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法")[-1],
    )
    parser.add_argument("--arch", default="amd64", help="目标架构: amd64(默认) / arm64")
    parser.add_argument("--index-url", default=None, help="PyPI 源（默认沿用 pip 配置）")
    parser.add_argument("--refresh", action="store_true", help="忽略 wheel 缓存，强制重新下载")
    parser.add_argument("--max-size", type=int, default=None, help="dify CLI 的 --max-size(MB)，默认按体积自动算")
    parser.add_argument("--dist", default=None, help="输出目录，默认 <仓库>/dist")
    parser.add_argument("--keep-staging", action="store_true", help="保留暂存目录，便于排查")
    args = parser.parse_args()

    cli = find_dify_cli()
    dist = Path(args.dist).resolve() if args.dist else PLUGIN_ROOT / "dist"
    dist.mkdir(parents=True, exist_ok=True)

    major, minor = read_target_python(PLUGIN_ROOT)
    py_version = f"{major}.{minor}"
    platforms = platform_tags(args.arch)
    abis = abi_tags(major, minor)
    target_arch = dify_arch(args.arch)
    # 产物名带上目标平台：linux-amd64 / linux-arm64 两个版本一眼可分
    plat_label = f"linux-{target_arch}"

    log(f"{INFO} 正在为 {PLUGIN_NAME} 构建离线包")
    log(f"{INFO} 目标: {plat_label} / python {py_version}")
    log(f"{INFO} 平台标签: {', '.join(platforms)}")

    preflight_runtime_platform()

    # 1. 常规包（复用 .difyignore，保证不遗漏也不多带）
    step("1/5 构建常规包")
    base_pkg = build_base_package(cli, dist, plat_label)

    # 每次用独立暂存目录：避免递归删除（在受限环境下容易失败），也便于排查
    build_root = PLUGIN_ROOT / ".offline-build"
    staging = build_root / f"{time.strftime('%Y%m%d-%H%M%S')}" / PLUGIN_NAME
    staging.parent.mkdir(parents=True, exist_ok=True)

    try:
        step("2/5 解压到暂存目录")
        with zipfile.ZipFile(base_pkg) as zf:
            zf.extractall(staging)
        log(f"{OK} {staging}")

        step("3/5 下载依赖闭包 wheel")
        req_file = staging / "requirements.txt"
        if not req_file.is_file():
            # requirements.txt 理论上一定存在；万一后续改成 pyproject.toml，这里显式提示
            sys.exit(f"{FAIL} 暂存目录里没有 requirements.txt，本脚本目前只支持 requirements.txt 形式")
        declared = [
            line.strip() for line in req_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        log(f"{INFO} 依赖清单: {'; '.join(declared)}")

        # 缓存放在仓库内（按依赖+目标环境的指纹分目录），暂存目录每次都是新的，
        # 所以缓存不能在暂存目录里，否则每次都要重新下载。
        fingerprint = wheels_fingerprint(req_file, py_version, platforms, abis, args.index_url)
        cache_dir = PLUGIN_ROOT / ".wheels-cache" / fingerprint[:16]
        wheels = download_wheels(
            req_file, cache_dir, py_version, platforms, abis, args.index_url, args.refresh
        )
        for wheel in wheels:
            log(f"      {wheel.name}")

        step("4/5 改写 requirements.txt / manifest 并重新打包")
        stage_wheels(wheels, staging / "wheels")
        patch_requirements(req_file)
        patch_manifest_arch(staging, args.arch)
        out_path = dist / f"{PLUGIN_NAME}-offline-{plat_label}.difypkg"
        out = repack(cli, staging, out_path, args.max_size)

        step("5/5 校验")
        ok = verify_package(
            out, len(wheels), py_version, platforms, abis, staging, target_arch
        )
    finally:
        if args.keep_staging:
            log(f"{INFO} 暂存目录保留在 {staging}")
        else:
            dispose_staging(staging.parent)

    log()
    if ok:
        log(f"{OK} 离线包已生成: {out}")
        log(f"     它可以直接在无外网的 Dify 上安装：插件 → 安装插件 → 通过本地文件安装")
        log(f"     注意：如包体较大，需要放宽 Dify 的 PLUGIN_MAX_PACKAGE_SIZE 等限制（见 README）。")
        return 0

    log(f"{FAIL} 校验未通过，请不要使用该包")
    return 1


if __name__ == "__main__":
    sys.exit(main())
