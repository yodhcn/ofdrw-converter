"""本地校验脚本：不依赖 Dify 实例，直接验证插件清单与转换链路。

做五件事：
  1. 用 dify-plugin SDK 的 Pydantic 模型校验 manifest / provider / tool 三个 YAML；
  2. 检查自带 Java 运行时与 jar 是否就绪；
  3. 走一遍真实的转换（Python -> 自带 Java 运行时 -> jar -> LayoutTextExtractor）；
  4. 把 samples/*.ofd 全部跑一遍，作为「瘦身后依赖是否够用」的回归测试；
  5. 对每个样本同时跑 layout 与 raw 两种模式，断言 layout 只做「重排」：
     非空白字符的多重集必须与 raw 完全一致（不增、不删、不改字），且行数不应变多。

用法:
    .venv\\Scripts\\python.exe scripts\\validate.py [样例.ofd]
    .venv\\Scripts\\python.exe scripts\\validate.py --no-sample   # 无样例时（如 CI 未提交 samples/）
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

import yaml

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

# 保证在 Windows 控制台（默认 GBK）下也能正常打印中文样本名。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

OK = "[OK]"
FAIL = "[FAIL]"


def check(label: str, fn) -> bool:
    try:
        result = fn()
    except Exception as e:  # noqa: BLE001
        print(f"{FAIL} {label}: {type(e).__name__}: {e}")
        return False
    print(f"{OK} {label}" + (f" -> {result}" if result else ""))
    return True


def validate_manifest() -> str:
    from dify_plugin.core.entities.plugin.setup import PluginConfiguration

    data = yaml.safe_load((PLUGIN_ROOT / "manifest.yaml").read_text(encoding="utf-8"))
    model = PluginConfiguration.model_validate(data)
    return f"name={model.name} version={model.version} tools={model.plugins.tools}"


def validate_provider() -> str:
    from dify_plugin.entities.tool import ToolProviderConfiguration

    path = PLUGIN_ROOT / "provider" / "ofdrw-converter.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    model = ToolProviderConfiguration.model_validate(data)
    return f"name={model.identity.name} tools={len(model.tools)}"


def validate_tool() -> str:
    from dify_plugin.entities.tool import ToolConfiguration

    path = PLUGIN_ROOT / "tools" / "text_exporter.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    model = ToolConfiguration.model_validate(data)
    return f"name={model.identity.name} params={[p.name for p in model.parameters]}"


def validate_sources_exist() -> str:
    from dify_plugin.entities.tool import ToolConfiguration, ToolProviderConfiguration

    provider = ToolProviderConfiguration.model_validate(
        yaml.safe_load((PLUGIN_ROOT / "provider" / "ofdrw-converter.yaml").read_text("utf-8"))
    )
    sources = [provider.extra.python.source]
    for tool in provider.tools:
        sources.append(tool.extra.python.source)
    tool_yaml = ToolConfiguration.model_validate(
        yaml.safe_load((PLUGIN_ROOT / "tools" / "text_exporter.yaml").read_text("utf-8"))
    )
    sources.append(tool_yaml.extra.python.source)

    missing = [s for s in sources if not (PLUGIN_ROOT / s).is_file()]
    if missing:
        raise FileNotFoundError(f"缺少源码文件: {missing}")
    return f"{len(sources)} 个源文件均存在"


def validate_runtime() -> str:
    from ofdrw_cli import check_environment

    status = check_environment()
    if status["missing"]:
        raise FileNotFoundError(status["missing"])
    return (
        f"java={status['java']} bundled={status['bundled_runtime']} "
        f"jar={status['jar_exists']}"
    )


def run_end_to_end(sample: Path) -> str:
    from dify_plugin.core.runtime import Session
    from dify_plugin.entities.tool import ToolRuntime
    from dify_plugin.file.entities import FileType
    from dify_plugin.file.file import File as DifyFile
    from tools.text_exporter import TextExporterTool

    tool = TextExporterTool(
        runtime=ToolRuntime(credentials={}, user_id="local-test", session_id=None),
        session=Session.empty_session(),
    )

    ofd_file = DifyFile(
        url="file:///local-test.ofd",
        mime_type="application/ofd",
        filename=sample.name,
        extension="ofd",
        size=sample.stat().st_size,
        type=FileType.DOCUMENT,
    )
    # 本地测试直接注入字节，避免真去下载 URL。
    ofd_file._blob = sample.read_bytes()

    messages = list(
        tool.invoke({"ofd_file": ofd_file, "attach_file": True, "max_chars": 0})
    )

    text = ""
    meta = {}
    files = []
    for m in messages:
        if m.type.value == "text":
            text = m.message.text
        elif m.type.value == "json":
            meta = dict(m.message.json_object)
        elif m.type.value == "blob":
            files.append(len(m.message.blob))

    if not text:
        raise AssertionError("未返回任何文本")
    if meta.get("char_count") != len(text):
        raise AssertionError(f"文本长度与元信息不一致: {meta.get('char_count')} vs {len(text)}")
    if not files:
        raise AssertionError("attach_file=true 但没有返回 blob 文件")

    preview = text.strip().splitlines()[:3]
    print(f"      页数={meta.get('page_count')} 字符数={meta.get('char_count')} "
          f"行数={meta.get('line_count')} 耗时={meta.get('elapsed_ms')}ms "
          f"txt附件={files[0]} 字节")
    print(f"      文本预览: {preview}")
    return f"{len(messages)} 条消息"


def run_batch() -> str:
    """把 samples/ 下所有 .ofd 都跑一遍，作为「瘦身后依赖是否够用」的回归测试。"""
    from ofdrw_cli import convert_to_text

    files = sorted((PLUGIN_ROOT / "samples").glob("*.ofd"))
    if not files:
        return "samples 目录下没有 .ofd，跳过"

    print(f"      {'文件':<26} {'页数':>4} {'字符数':>7} {'耗时':>7}  结果")
    failed: list[str] = []
    for f in files:
        with tempfile.TemporaryDirectory(prefix="ofdrw-batch-") as tmp:
            t0 = time.time()
            try:
                result = convert_to_text(f, Path(tmp) / "out.txt")
                meta = result["meta"]
                note = "空文本(整页为图片/路径图元)" if meta.get("empty") else "OK"
                print(
                    f"      {f.name:<26} {meta.get('page_count'):>4} "
                    f"{meta.get('char_count'):>7} {int((time.time() - t0) * 1000):>6}ms  {note}"
                )
            except Exception as e:  # noqa: BLE001
                failed.append(f.name)
                print(f"      {f.name:<26} {'-':>4} {'-':>7} {'-':>7}  失败: {e}")

    if failed:
        raise AssertionError(f"{len(failed)} 个样本转换失败: {failed}")
    return f"{len(files)} 个样本全部跑通"


def run_consistency() -> str:
    """layout 模式的一致性回归：相对 raw 只能「重排」，不能动到正文内容。

    layout 是重写过的抽取逻辑，最大的风险是**静默丢字或错字**——这类问题在
    抽查几行输出时很难发现。这里对每个样本跑两种模式，用「非空白字符的多重集」
    做严格比对：它允许换行与空格变化（那正是 layout 要做的事），
    但只要少一个字、多一个字或写错一个字，多重集就会不等。
    """
    from ofdrw_cli import MODE_LAYOUT, MODE_RAW, convert_to_text

    files = sorted((PLUGIN_ROOT / "samples").glob("*.ofd"))
    if not files:
        return "samples 目录下没有 .ofd，跳过"

    print(f"      {'文件':<26} {'raw行':>6} {'lay行':>6} {'非空白字符':>10}  结果")
    failed: list[str] = []
    raw_total = lay_total = 0
    for f in files:
        with tempfile.TemporaryDirectory(prefix="ofdrw-consistency-") as tmp:
            try:
                raw = convert_to_text(f, Path(tmp) / "raw.txt", mode=MODE_RAW)
                lay = convert_to_text(f, Path(tmp) / "layout.txt", mode=MODE_LAYOUT)
            except Exception as e:  # noqa: BLE001
                failed.append(f"{f.name}: 转换异常 {e}")
                print(f"      {f.name:<26} {'-':>6} {'-':>6} {'-':>10}  失败: {e}")
                continue

            counter_raw = Counter(c for c in raw["text"] if not c.isspace())
            counter_lay = Counter(c for c in lay["text"] if not c.isspace())
            raw_lines = raw["meta"]["line_count"] or 0
            lay_lines = lay["meta"]["line_count"] or 0
            raw_total += raw_lines
            lay_total += lay_lines

            problems = []
            if counter_raw != counter_lay:
                lost = (counter_raw - counter_lay).most_common(5)
                added = (counter_lay - counter_raw).most_common(5)
                problems.append(f"字符集不一致 丢失={lost} 多出={added}")
            if lay_lines > raw_lines:
                problems.append(f"行数变多 {raw_lines} -> {lay_lines}")

            if problems:
                failed.append(f"{f.name}: {'; '.join(problems)}")
                note = "❌ " + problems[0]
            else:
                note = "OK"
            print(f"      {f.name:<26} {raw_lines:>6} {lay_lines:>6} "
                  f"{sum(counter_lay.values()):>10}  {note}")

    if failed:
        raise AssertionError(f"{len(failed)} 个样本未通过一致性校验: {failed}")
    return f"{len(files)} 个样本字符集完全一致，行数合计 {raw_total} -> {lay_total}"


def main() -> int:
    sample = Path(sys.argv[1]) if len(sys.argv) > 1 else PLUGIN_ROOT / "samples" / "999.ofd"

    parser = argparse.ArgumentParser(description="校验 ofdrw-converter 插件")
    parser.add_argument("sample", nargs="?", default=None, help="端到端转换用的样例 OFD 路径")
    parser.add_argument(
        "--no-sample",
        action="store_true",
        help="跳过依赖样例的端到端与批量回归（适用于 samples/ 未提供的环境，如 CI）",
    )
    args = parser.parse_args()

    print("=== 1. 清单校验 ===")
    ok = all(
        [
            check("manifest.yaml", validate_manifest),
            check("provider/ofdrw-converter.yaml", validate_provider),
            check("tools/text_exporter.yaml", validate_tool),
            check("源码路径引用", validate_sources_exist),
        ]
    )

    print("\n=== 2. 运行时自检 ===")
    ok = check("自带 Java 运行时 + jar", validate_runtime) and ok

    if args.no_sample:
        print("\n=== 3. 端到端转换 ===")
        print("      已指定 --no-sample，跳过")
        print("\n=== 4. 多样本回归 ===")
        print("      已指定 --no-sample，跳过")
        print("\n=== 5. layout/raw 一致性回归 ===")
        print("      已指定 --no-sample，跳过")
    else:
        sample = Path(args.sample) if args.sample else PLUGIN_ROOT / "samples" / "999.ofd"

        print("\n=== 3. 端到端转换 ===")
        if not sample.is_file():
            print(f"{FAIL} 样例文件不存在: {sample}")
            print("      若此环境本就没有样例文件，请加 --no-sample 跳过。")
            return 1
        print(f"      样例: {sample}")
        ok = check("输入 OFD -> 导出纯文本", lambda: run_end_to_end(sample)) and ok

        print("\n=== 4. 多样本回归 ===")
        ok = check("samples/*.ofd 批量转换", run_batch) and ok

        print("\n=== 5. layout/raw 一致性回归 ===")
        ok = check("两种模式的内容一致性", run_consistency) and ok

    print()
    print("全部通过 ✅" if ok else "存在失败项 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
