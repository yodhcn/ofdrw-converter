#!/usr/bin/env bash
#
# 构建 ofdrw-converter 插件所需的自带 Java 资源（jar + Java 运行时）。
#
# build.ps1 的等价实现，供 Linux / macOS / Git Bash 使用（CI 走这条路径）。
# 执行三步：
#   1. 用 Maven 把 jvm/ 下的 Java 命令行工程打成可执行 fat jar；
#   2. 把 jar 复制到 bin/ofdrw-text-cli.jar；
#   3. 用 jlink 生成最小化 Java 运行时到 bin/runtime。
#
# 关键点：jlink 产出的是**平台相关**的运行时。Dify 的 plugin_daemon 跑在 Linux
# 容器里，因此 CI 必须在 Linux 上执行本脚本，不能用 Windows 上生成的运行时。
#
# 用法：
#   bash scripts/build.sh                       # 自动从 JAVA_HOME / PATH 定位 JDK
#   bash scripts/build.sh --jdk-home /opt/jdk-21
#   bash scripts/build.sh --skip-maven          # 只重新生成运行时
#
set -euo pipefail

SKIP_MAVEN=0
JDK_HOME_ARG=""

usage() {
    sed -n '3,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --jdk-home) JDK_HOME_ARG="${2:-}"; shift 2 ;;
        --jdk-home=*) JDK_HOME_ARG="${1#*=}"; shift ;;
        --skip-maven) SKIP_MAVEN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数: $1" >&2; usage >&2; exit 2 ;;
    esac
done

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(dirname -- "$SCRIPT_DIR")"
JVM_DIR="$PLUGIN_ROOT/jvm"
BIN_DIR="$PLUGIN_ROOT/bin"
RUNTIME_DIR="$BIN_DIR/runtime"
JAR_NAME="ofdrw-text-cli.jar"

# ------------------------------------------------------------------ 小工具

# 在目录里找出第一个可执行文件；兼容 Windows 上的 .exe 后缀。
find_exe() {
    local dir="$1"; shift
    local name
    for name in "$@"; do
        if [[ -x "$dir/$name" ]]; then
            printf '%s\n' "$dir/$name"
            return 0
        fi
    done
    return 1
}

# Git Bash / Cygwin 下，jlink、java 这些宿主程序看不懂 /c/... 这类 POSIX 路径：
# jlink 会把 ``--output /c/Users/...`` 当成相对路径，在 C:\c\ 下凭空造出一份运行时，
# 而且返回 0（错误要等到后面找不到运行时目录才暴露）。所以送给宿主程序的路径
# 必须先转成原生形式。
case "${OSTYPE:-}" in
    msys*|cygwin*)
        to_native() { cygpath -w -- "$1" 2>/dev/null || printf '%s' "$1"; }
        ;;
    *)
        to_native() { printf '%s' "$1"; }
        ;;
esac

# jlink 要求输出目录不存在。这里不做递归删除（受限环境会拦截批量删除），
# 而是把旧运行时移到系统临时目录，由使用者自行清理。
dispose() {
    local target="$1"
    [[ -e "$target" ]] || return 0
    local dest="${TMPDIR:-${TEMP:-/tmp}}/ofdrw-runtime.bak.$(date +%Y%m%d-%H%M%S)"
    echo "      已存在旧运行时，移动到 $dest"
    mv -- "$target" "$dest"
}

resolve_jdk() {
    local candidates=()
    [[ -n "$JDK_HOME_ARG" ]] && candidates+=("$JDK_HOME_ARG")
    [[ -n "${JAVA_HOME:-}" ]] && candidates+=("$JAVA_HOME")

    local c exe
    if [[ ${#candidates[@]} -gt 0 ]]; then
        for c in "${candidates[@]}"; do
            [[ -n "$c" ]] || continue
            if exe="$(find_exe "$c/bin" jlink jlink.exe)"; then
                printf '%s\n' "$c"
                return 0
            fi
        done
    fi

    # 从 PATH 里的 javac 反推 JDK 目录
    local javac_path
    if javac_path="$(command -v javac 2>/dev/null)"; then
        # 解掉符号链接（SDKMAN / update-alternatives 常把 bin/javac 指到别处）
        javac_path="$(readlink -f -- "$javac_path" 2>/dev/null || printf '%s' "$javac_path")"
        local home
        home="$(dirname -- "$(dirname -- "$javac_path")")"
        if find_exe "$home/bin" jlink jlink.exe >/dev/null; then
            printf '%s\n' "$home"
            return 0
        fi
    fi
    return 1
}

# ------------------------------------------------------------- 1. Maven 打包

if [[ $SKIP_MAVEN -eq 0 ]]; then
    echo "[1/3] Maven 构建 fat jar ..."

    # Git Bash / Cygwin 下 Maven 自带的 sh 脚本拼不出 Windows 风格的 classpath
    # （报 ClassNotFoundException: org.codehaus.plexus.classworlds.launcher.Launcher），
    # 所以在这类环境里优先用 .cmd 版本。
    MVN=""
    case "${OSTYPE:-}" in
        msys*|cygwin*|win32*) command -v mvn.cmd >/dev/null 2>&1 && MVN="mvn.cmd" ;;
    esac
    if [[ -z "$MVN" ]]; then
        command -v mvn >/dev/null 2>&1 && MVN="mvn"
    fi
    if [[ -z "$MVN" ]]; then
        command -v mvn.cmd >/dev/null 2>&1 && MVN="mvn.cmd"
    fi
    if [[ -z "$MVN" ]]; then
        echo "错误：未找到 Maven，请先安装或将其加入 PATH。（只重建运行时可用 --skip-maven）" >&2
        exit 1
    fi

    # 先 cd 进 jvm/，避免把 POSIX 风格路径传给 .cmd 版本
    ( cd "$JVM_DIR" && "$MVN" -B -DskipTests clean package )
else
    echo "[1/3] 跳过 Maven 构建"
fi

# ----------------------------------------------------------------- 2. 复制 jar

echo "[2/3] 复制 jar 到 bin/ ..."
mkdir -p "$BIN_DIR"
BUILT_JAR="$JVM_DIR/target/$JAR_NAME"
[[ -f "$BUILT_JAR" ]] || { echo "错误：未找到构建产物 $BUILT_JAR" >&2; exit 1; }
cp -f -- "$BUILT_JAR" "$BIN_DIR/$JAR_NAME"

# -------------------------------------------------------------- 3. jlink 运行时

if ! JDK="$(resolve_jdk)"; then
    echo "错误：未找到包含 jlink 的 JDK。请安装 JDK 17+ 并用 --jdk-home 指定目录。" >&2
    exit 1
fi
echo "[3/3] 用 $JDK 生成 Java 运行时 ..."

# 已实测的最小可用模块集合（完整 jar 在本运行时下转换成功）：
#   java.base      - 必需
#   java.xml       - dom4j 需要 JDK 内置的 SAX 解析器（ofdrw 通过 SAXReaderFactory 创建 SAXReader）
#   jdk.charsets   - 兼容 GBK/GB18030 等非 UTF-8 编码的 OFD
#   java.logging   - commons-logging / slf4j 需要
#
# 注意：不要加 java.desktop。它会让运行时从 34MB 涨到 46MB，而 TextExporter 走的是
# ContentExtractor，只从 XML 里读文本内容（TextCode.getContent），不做字体渲染，用不到 AWT。
# 也不要照抄 jdeps --print-module-deps 的结果，它会带上只在渲染路径上加载的模块。
MODULES="java.base,java.xml,jdk.charsets,java.logging"
echo "      模块: $MODULES"

JLINK="$(find_exe "$JDK/bin" jlink jlink.exe)"

# jdeps 结果仅作参考打印，避免误加 java.desktop 等无用模块撑大体积。
if JDEPS="$(find_exe "$JDK/bin" jdeps jdeps.exe)"; then
    echo "      jdeps 参考(未采用): $("$JDEPS" --multi-release 21 --ignore-missing-deps \
        --print-module-deps "$(to_native "$BIN_DIR/$JAR_NAME")" 2>/dev/null | tail -n 1 || true)"
fi

dispose "$RUNTIME_DIR"
"$JLINK" \
    --add-modules "$MODULES" \
    --strip-debug --no-header-files --no-man-pages --compress=zip-6 \
    --output "$(to_native "$RUNTIME_DIR")"

# ------------------------------------------------------------------ 自检

JAVA_EXE="$(find_exe "$RUNTIME_DIR/bin" java java.exe)"
echo ""
echo "完成："
echo "  jar     : bin/$JAR_NAME ($(du -m -- "$BIN_DIR/$JAR_NAME" | cut -f1) MB)"
echo "  runtime : bin/runtime ($(du -sm -- "$RUNTIME_DIR" | cut -f1) MB)"

echo ""
echo "自检："
"$JAVA_EXE" -Dfile.encoding=UTF-8 -jar "$(to_native "$BIN_DIR/$JAR_NAME")" --help

# 用真实样例跑一次转换，验证最小运行时确实够用（可捕获漏掉的 JDK 模块）。
SAMPLE=""
for candidate in "$PLUGIN_ROOT"/samples/*.ofd; do
    [[ -f "$candidate" ]] && { SAMPLE="$candidate"; break; }
done

if [[ -n "$SAMPLE" ]]; then
    echo ""
    echo "用样例 $(basename -- "$SAMPLE") 验证转换 ..."
    PROBE="$(mktemp -d "${TMPDIR:-/tmp}/ofdrw-selfcheck-XXXXXX")"
    "$JAVA_EXE" -Dfile.encoding=UTF-8 -jar "$(to_native "$BIN_DIR/$JAR_NAME")" \
        --input "$(to_native "$SAMPLE")" \
        --output "$(to_native "$PROBE/out.txt")"
    echo "自检通过 ✅"
else
    echo "（samples 目录下没有 .ofd 样例，跳过转换自检）"
fi
