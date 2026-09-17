<#
.SYNOPSIS
    构建 ofdrw-converter 插件所需的自带 Java 资源（jar + Java 运行时）。

.DESCRIPTION
    执行三步：
      1. 用 Maven 把 jvm/ 下的 Java 命令行工程打成可执行 fat jar；
      2. 把 jar 复制到 bin/ofdrw-text-cli.jar；
      3. 用 jdeps + jlink 生成最小化 Java 运行时到 bin/runtime。

    换目标平台时，只要在对应平台上重新执行本脚本即可得到该平台的运行时，
    插件本身（Python 部分）无需改动。

.EXAMPLE
    pwsh -File scripts/build.ps1 -JdkHome "C:\Program Files\Java\jdk-21.0.11"
#>
[CmdletBinding()]
param(
    # 用于编译与 jlink 的 JDK 目录；留空则依次尝试 JAVA_HOME、PATH 中的 javac
    [string]$JdkHome = "",
    # 跳过 Maven 构建（仅重新生成运行时）
    [switch]$SkipMaven
)

$ErrorActionPreference = "Stop"

$PluginRoot = Split-Path -Parent $PSScriptRoot
$JvmDir = Join-Path $PluginRoot "jvm"
$BinDir = Join-Path $PluginRoot "bin"
$RuntimeDir = Join-Path $BinDir "runtime"
$JarName = "ofdrw-text-cli.jar"

function Resolve-Jdk {
    param([string]$Explicit)

    $candidates = @()
    if ($Explicit) { $candidates += $Explicit }
    if ($env:JAVA_HOME) { $candidates += $env:JAVA_HOME }

    foreach ($c in $candidates) {
        if ($c -and (Test-Path (Join-Path $c "bin\jlink.exe"))) { return $c }
    }

    # 从 PATH 中的 javac 反推 JDK 目录
    $javac = Get-Command javac.exe -ErrorAction SilentlyContinue
    if ($javac) {
        $home = Split-Path -Parent (Split-Path -Parent $javac.Source)
        if (Test-Path (Join-Path $home "bin\jlink.exe")) { return $home }
    }

    throw "未找到包含 jlink 的 JDK。请安装 JDK 17+ 并用 -JdkHome 指定目录。"
}

# ---------------------------------------------------------------- 1. Maven 打包
if (-not $SkipMaven) {
    Write-Host "[1/3] Maven 构建 fat jar ..." -ForegroundColor Cyan
    $mvn = Get-Command mvn.cmd -ErrorAction SilentlyContinue
    if (-not $mvn) { $mvn = Get-Command mvn -ErrorAction SilentlyContinue }
    if (-not $mvn) { throw "未找到 Maven，请先安装 Maven 或将其加入 PATH。" }

    & $mvn.Source -f (Join-Path $JvmDir "pom.xml") -B -DskipTests clean package
    if ($LASTEXITCODE -ne 0) { throw "Maven 构建失败，退出码 $LASTEXITCODE" }
} else {
    Write-Host "[1/3] 跳过 Maven 构建" -ForegroundColor DarkGray
}

# ---------------------------------------------------------------- 2. 复制 jar
Write-Host "[2/3] 复制 jar 到 bin\ ..." -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$builtJar = Join-Path $JvmDir "target\$JarName"
if (-not (Test-Path $builtJar)) { throw "未找到构建产物: $builtJar" }
Copy-Item -Force $builtJar (Join-Path $BinDir $JarName)

# ------------------------------------------------------------ 3. jlink 生成 JRE
$jdk = Resolve-Jdk -Explicit $JdkHome
Write-Host "[3/3] 用 $jdk 生成 Java 运行时 ..." -ForegroundColor Cyan

# 已实测的最小可用模块集合（完整 jar 在本运行时下转换成功）：
#   java.base      - 必需
#   java.xml       - dom4j 需要 JDK 内置的 SAX 解析器（ofdrw 通过 SAXReaderFactory 创建 SAXReader）
#   jdk.charsets   - 兼容 GBK/GB18030 等非 UTF-8 编码的 OFD
#   java.logging   - commons-logging / slf4j 需要
#
# 注意：不要加 java.desktop。它会让运行时从 34MB 涨到 46MB，而 TextExporter 走的是
# ContentExtractor，只从 XML 里读文本内容（TextCode.getContent），不做字体渲染，用不到 AWT。
$modules = "java.base,java.xml,jdk.charsets,java.logging"

Write-Host "      模块: $modules" -ForegroundColor DarkGray

# jdeps 结果仅作参考打印，避免误加 java.desktop 等无用模块撑大体积。
$jdepsOut = & (Join-Path $jdk "bin\jdeps.exe") --multi-release 21 --ignore-missing-deps `
    --print-module-deps (Join-Path $BinDir $JarName) 2>$null
$jdepsModules = ($jdepsOut | Select-Object -Last 1).Trim()
if ($jdepsModules) {
    Write-Host "      jdeps 参考(未采用): $jdepsModules" -ForegroundColor DarkGray
}

# jlink 要求输出目录不存在或为空。这里不做递归删除（避免误删与沙箱/杀软拦截），
# 而是把旧运行时移到系统临时目录，由使用者自行清理。
if (Test-Path $RuntimeDir) {
    $backup = Join-Path ([System.IO.Path]::GetTempPath()) `
        ("ofdrw-runtime.bak." + (Get-Date -Format "yyyyMMdd-HHmmss"))
    Write-Host "      已存在旧运行时，移动到 $backup" -ForegroundColor DarkGray
    Move-Item -Path $RuntimeDir -Destination $backup
}

& (Join-Path $jdk "bin\jlink.exe") `
    --add-modules $modules `
    --strip-debug --no-header-files --no-man-pages --compress=zip-6 `
    --output $RuntimeDir
if ($LASTEXITCODE -ne 0) { throw "jlink 执行失败，退出码 $LASTEXITCODE" }

$sizeMb = [math]::Round(((Get-ChildItem -Recurse -File $RuntimeDir | Measure-Object Length -Sum).Sum / 1MB), 1)
$jarMb = [math]::Round((Get-Item (Join-Path $BinDir $JarName)).Length / 1MB, 1)
$totalMb = [math]::Round($sizeMb + $jarMb, 1)

Write-Host ""
Write-Host "完成：" -ForegroundColor Green
Write-Host "  jar     : bin\$JarName ($jarMb MB)"
Write-Host "  runtime : bin\runtime ($sizeMb MB)"
Write-Host "  合计    : $totalMb MB"

Write-Host ""
Write-Host "注意：jlink 只能生成当前平台的运行时。这里产出的是 **Windows** 版" -ForegroundColor Yellow
Write-Host "      （bin\runtime\bin\java.exe），仅用于本机调试 Java CLI；" -ForegroundColor Yellow
Write-Host "      打 Dify 插件包必须用 Linux 版运行时（plugin_daemon 是 Linux 容器）：" -ForegroundColor Yellow
Write-Host "          bash scripts/build.sh        # 在 Linux 上执行" -ForegroundColor Yellow
Write-Host "          或 .github/workflows/repackage-plugin.yml（GitHub Action）" -ForegroundColor Yellow
Write-Host "      scripts/package_offline.py 会在开工前直接拒绝 Windows 运行时。" -ForegroundColor Yellow

Write-Host ""
Write-Host "自检：" -ForegroundColor Green
$javaExe = Join-Path $RuntimeDir "bin\java.exe"
& $javaExe @("-Dfile.encoding=UTF-8", "-jar", (Join-Path $BinDir $JarName), "--help")

# 用真实样例跑一次转换，验证最小运行时确实够用（可捕获漏掉的 JDK 模块）。
$sample = Get-ChildItem -Path (Join-Path $PluginRoot "samples") -Filter "*.ofd" -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($sample) {
    Write-Host ""
    Write-Host "用样例 $($sample.Name) 验证转换 ..." -ForegroundColor Green
    $probe = Join-Path ([System.IO.Path]::GetTempPath()) ("ofdrw-selfcheck-" + (Get-Date -Format "HHmmss"))
    New-Item -ItemType Directory -Force -Path $probe | Out-Null
    & $javaExe @("-Dfile.encoding=UTF-8", "-jar", (Join-Path $BinDir $JarName),
        "--input", $sample.FullName,
        "--output", (Join-Path $probe "out.txt"))
    if ($LASTEXITCODE -ne 0) { throw "自检转换失败，退出码 $LASTEXITCODE（可能是 JDK 模块缺失）" }
    Write-Host "自检通过 ✅" -ForegroundColor Green
} else {
    Write-Host "（samples 目录下没有 .ofd 样例，跳过转换自检）" -ForegroundColor DarkGray
}
