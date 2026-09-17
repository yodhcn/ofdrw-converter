# ofdrw-converter

一个 **Dify 工具插件**：输入 OFD（开放版式文档）文件，导出为纯文本。

底层直接使用 [OFDRW](https://github.com/ofdrw/ofdrw) 的
[`TextExporter`](https://github.com/ofdrw/ofdrw/blob/master/ofdrw-converter/doc/EXPORTER.md)。

> 插件**不需要发布**。本仓库即可直接本地安装/调试的插件工程，并通过脚本打包成
> **Dify 可直接安装的离线包**：
>
> | 产物 | 目标平台 | 适用场景 |
> | --- | --- | --- |
> | `dist/ofdrw-converter-offline-linux-amd64.difypkg` | Linux x86_64 | 无外网 / 内网隔离环境，产物随 `platform_arm=false` |
> | `dist/ofdrw-converter-offline-linux-arm64.difypkg` | Linux ARM64 | 同上，产物随 `platform_arm=true` |
> | `dist/ofdrw-converter-linux-<arch>.difypkg` | 同上 | 同上，但安装时**需要**能访问 PyPI 拉 Python 依赖 |
>
> 四个包（两平台 × 常规/离线）的插件代码完全一致，只差 Python 依赖的获取方式。
>
> ⚠️ **只做 Linux 产物**。Dify 的 plugin_daemon 跑在 Linux 容器里，而插件自带的
> Java 运行时是 `jlink` 产物、**平台相关**：Windows 上生成的是 `bin/runtime/bin/java.exe`，
> 打进包里在 Linux 上根本起不来。所以打包固定面向 Linux，且
> `package_offline.py` 会在开工前直接拒绝 Windows 运行时。
> 产物请用 [GitHub Action](#5-用-github-action-重新打包推荐) 生成（或本地 `bash scripts/build.sh` 后在 Linux 上打包）。

## 设计思路（为什么这么做）

`ofdrw` 是 Java 生态的 OFD 解析库，而 Dify 插件运行时是 Python。
为了让插件「自包含、开箱即用、不依赖用户机器上的 Java 环境」，采用三层结构：

```
Dify 工具节点 (Python)
        │  subprocess 调用命令行
        ▼
bin/runtime/bin/java          ← jlink 裁剪出的最小 Java 运行时（随插件分发）
        │  -jar
        ▼
bin/ofdrw-text-cli.jar        ← 自己写的命令行入口 + ofdrw 依赖（shade fat jar）
        │
        ▼
org.ofdrw.converter.export.TextExporter
```

- **自带运行时**：用 `jlink` 从 JDK 21 裁剪出 34.3 MB 的最小 JRE，放进插件包，用户无需安装 Java。
- **命令行封装**：Java 侧只做一个薄薄的 CLI（`jvm/src/main/java/com/ofdrwconverter/cli/Main.java`），
  负责页码解析、异常转 JSON、字符集与换行归一化。
- **Python 只做调度**：`ofdrw_cli.py` 负责定位 Java、拉起子进程、解析结果文件，不碰 OFD 格式细节。

**为什么不用 JNI/JPype**：那会引入「Python 与 JVM 同进程」的兼容性与内存问题；
独立进程模型最简单、最可靠，JVM 崩溃也不会拖垮插件进程。

### 体积约束（重要）

Dify 对插件包大小有上限，**默认是解压后 50 MB**，且客户端与服务端各有一道：

| 位置 | 配置项 | 默认值 | 本项目 |
| --- | --- | --- | --- |
| 打包机（`dify plugin package`） | `--max-size`（单位 MB） | 50 | 显式设 500 |
| Dify 服务端 plugin_daemon | `PLUGIN_MAX_PACKAGE_SIZE`（单位字节） | 52428800 | 放宽到 524288000 |

打包脚本两步都会显式传 `--max-size`（默认 500），不再依赖客户端那个 50 MB 的默认值——
自带 Java 运行时的插件本来就贴着这条线，任何一点多余内容都会把构建打挂。

常规包解压后 45.3 MB（`jar 11.0 MB + runtime 34.3 MB`），刚好卡在默认 50 MB 以内。
为了留出这个余量做了三件事：

| 手段 | 收益 |
| --- | --- |
| 剔除只服务于 PDF / 图片 / SVG / HTML 导出的依赖（iText、PDFBox、Batik、TwelveMonkeys） | jar 31 MB → 15 MB |
| 剔除 MR-JAR 覆盖类 `META-INF/versions/**`（fat jar 未声明 `Multi-Release`，JVM 本来就会忽略）与 BouncyCastle 的后量子密码包 `org/bouncycastle/pqc/**` | jar 15 MB → 11 MB |
| jlink 只保留 `java.base,java.xml,jdk.charsets,java.logging`，**不要 `java.desktop`** | runtime 47 MB → 34.3 MB |

四条反直觉但关键的结论，改动时请务必注意：

1. **BouncyCastle 不能剔除**。`OFDReader` 初始化时会注册 SM3 摘要算法，
   去掉后会直接 `NoClassDefFoundError: org/bouncycastle/jcajce/provider/digest/SM3$Digest`，
   任何 OFD 都打不开。
2. **`java.desktop` 不需要**。`TextExporter` 走的是 `ContentExtractor`，
   只从 XML 里读文本内容（`TextCode.getContent()`），不做任何字体渲染或图形处理。
   加上它会让运行时从 34 MB 涨到 46 MB。`jdeps` 会提示需要 `java.desktop`，
   那是因为 jar 里还留着 `ofdrw-graphics2d`、`ofdrw-font`、`ujmp` 等
   只在渲染/导出路径上才加载的类——它们不会被 `TextExporter` 触达。

3. **`.git/` 必须排除**（`.difyignore` 里已有，别删）。`dify plugin package` **不会**自动跳过
   仓库元数据，而 `.git/objects` 动辄几十 MB：漏掉这一条，常规包会从 45 MB 直接涨到 78 MB，
   报的却是一句指不到病根的 `Plugin package size is too large`。
   打包脚本现在会在打出常规包后**立刻体检包内布局**（顶层白名单 + `.git/` 禁区），
   这类事故会在第一步就带着明确原因失败，而不是等到 CI 里靠体积数字去猜。
4. **自带的可执行文件必须自己补权限位**。`dify plugin package` 写出的 zip **不含任何权限信息**
   （实测 122/122 条都是 `create_system=0`(FAT)、`external_attr=0`），解包后
   `bin/runtime/bin/java` 只是一个普通只读文件，第一次调用就
   `无法启动 Java 进程: [Errno 13] Permission denied`。CLI 没有设置权限的入口，所以两道保险都要在：
   - 打包侧：`patch_unix_modes()` 重写 zip，给每个条目写上 `create_system=3`(Unix) 与真实 mode
     （`bin/runtime/bin/*` 与 `*.so` 为 0755），校验段还会断言可执行位确实存在；
   - 运行侧：`resolve_java()` 返回前确保可执行 —— 先原地 `chmod 0755`，
     失败（插件目录只读）就把**整棵**运行时复制到可写目录再补（`bin/java` 靠 rpath
     `$ORIGIN/../lib` 找 `lib/server/libjvm.so`，只搬一个文件是残的）。

离线包会把 38 个 Python wheel（10.9 MB）一起塞进去，解压后达到 56.2 MB，
**必然超过默认的 50 MB**，所以安装前要把上面两个上限一起放宽——见
[打包离线包](#3-打包离线包无外网环境)。

## 目录结构

```
ofdrw-converter/
├── manifest.yaml                 # Dify 插件清单
├── main.py                       # 插件入口
├── requirements.txt
├── ofdrw_cli.py                  # Java 调用封装（定位运行时 / 拉起进程 / 解析结果）
├── provider/
│   ├── ofdrw-converter.yaml      # 工具提供者
│   └── ofdrw-converter.py        # 提供者代码（凭据校验 = 自检自带资源）
├── tools/
│   ├── text_exporter.yaml        # 工具定义（参数、描述）
│   └── text_exporter.py          # 工具实现（OFD -> 纯文本）
├── _assets/                      # 图标
├── bin/                          # ★ 构建产物，随插件分发
│   ├── ofdrw-text-cli.jar        #   可执行 fat jar（含 ofdrw-converter 2.4.0 依赖）
│   └── runtime/                  #   jlink 生成的最小 Java 运行时
├── jvm/                          # Java 命令行工程源码
│   ├── pom.xml
│   └── src/main/java/com/ofdrwconverter/cli/Main.java
├── scripts/
│   ├── build.ps1                 # 构建 jar + 生成运行时（Windows）
│   ├── build.sh                  # 同上，Linux / macOS / Git Bash（CI 走这条）
│   ├── validate.py               # 本地校验（清单 + 端到端 + 多样本回归）
│   └── package_offline.py        # 打包离线包（依赖 wheel 随包携带，安装不联网）
├── .github/workflows/
│   └── repackage-plugin.yml      # 手工触发的重新打包（Linux/ARM 各一份产物）
├── samples/                      # 13 个真实 OFD 测试样本
└── dist/                         # 打包产物（已在 .gitignore 中）
    ├── ofdrw-converter.difypkg          # 常规包（31.9 MB）
    └── ofdrw-converter-offline.difypkg  # 离线包（42.7 MB）
```

## 快速开始

### 1. 构建自带 Java 资源

需要 **JDK 17+**（含 `jlink`/`jdeps`）与 **Maven**：

```powershell
# Windows
pwsh -File scripts/build.ps1 -JdkHome "C:\Program Files\Java\jdk-21.0.11"
```

```bash
# Linux / macOS / Git Bash —— CI 走的就是这条
bash scripts/build.sh --jdk-home /opt/jdk-21
```

两个脚本等价，依次完成：Maven 打包 fat jar → 复制到 `bin/` → `jlink` 生成 `bin/runtime` →
打印体积 → 用 `samples/` 里的真实 OFD 跑一次转换自检。

> **平台必须选对**：`jlink` 只能产出**当前平台**的运行时。要在 Linux 的 Dify 上跑，
> 就得在 Linux 上执行（`build.ps1` 在 Windows 上产出的是 `bin/runtime/bin/java.exe`，
> 拿到 Linux 容器里用不了）。不确定就交给
> [GitHub Action](#5-用-github-action-重新打包推荐)，它在 Linux runner 上构建。

> 自检这一步是刻意设计的：它能在构建期就抓出「剔除依赖剔过头」或「JDK 模块漏加」的问题。
> 开发过程中它已经捕获过一次 BouncyCastle 被误删的问题。

### 2. 本地校验（不需要 Dify 实例）

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install --no-cache-dir -r requirements.txt pyyaml
.venv/Scripts/python.exe scripts/validate.py
```

校验 4 件事：

1. 用官方 SDK 的 Pydantic 模型校验 `manifest.yaml` / provider / tool 三个清单；
2. 清单引用的 Python 源文件是否都存在；
3. 自带 Java 运行时与 jar 是否就绪，并走一遍真实转换（Python → 自带 JRE → jar → TextExporter）；
4. `samples/*.ofd` 全量批量回归，打印每个样本的页数/字符数/耗时。

最近一次结果：`12 个样本全部跑通`，其中 `h.ofd` 为整页图片文档，正确返回空文本提示而非报错。

> 若环境里没有样例文件（例如 CI 未提交 `samples/`），加 `--no-sample` 跳过第 3、4 步。

### 3. 打包（常规包）

```bash
dify plugin package ./ofdrw-converter -o dist/ofdrw-converter-linux-amd64.difypkg
```

产物在 Dify 的「插件 → 安装插件 → 通过本地文件安装」上传即可——**但安装过程中
plugin_daemon 需要访问 PyPI** 去装 `dify-plugin` 及其传递依赖，无外网环境请用下一步的离线包。
（注意这一步只是把 `bin/` 现成内容压包，所以**前提是 `bin/runtime` 已经是 Linux 版**。）

### 4. 打包离线包（无外网环境）

内网隔离的 Dify 装不上插件的典型日志是
`init environment failed: failed to install dependencies`，原因就是上面那步联网装依赖失败了。

离线包把整条依赖闭包的 wheel 一起塞进包里，并让 pip/uv 只从包内 `wheels/` 目录解析：

```bash
python scripts/package_offline.py
```

脚本会依次完成：

1. **平台预检**：确认 `bin/runtime` 是 Linux 版（Windows 版直接报错退出）；
2. 先用 `dify plugin package --max-size 500` 打出常规包（复用 `.difyignore`，保证不多带也不遗漏文件），
   并立刻体检包内布局——`.git/` 之类混进来会在这里直接报错；
3. 解压到临时暂存目录；
4. 按目标运行环境**交叉下载** wheel：`linux` + `amd64`/`arm64` + Python `3.12`
   （Python 版本从 `manifest.yaml` 的 `meta.runner.version` 读取，不写死）；
5. 把暂存目录里 `requirements.txt` 的首行改成 `--no-index --find-links=./wheels/`，
   并把 `manifest.yaml` 的 `meta.arch` 改成目标架构（Dify 会拿它与宿主比对，不一致会拒装）；
6. 重新打包为 `dist/ofdrw-converter-offline-linux-<arch>.difypkg`；
7. 校验 wheel 数量、必需文件、运行时平台、`meta.arch`、清单改写，
   并用 pip 做一次**纯离线依赖解析试算**。

> 脚本只改写**暂存副本**里的 `requirements.txt` 与 `manifest.yaml`，
> 仓库里那两份保持原样，所以开发机上的 `pip install -r requirements.txt` 与本地调试不受影响。
> wheel 会按「依赖 + 目标环境」的指纹缓存在 `.wheels-cache/`，重复构建不再重新下载。

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--arch arm64` | 产出 `linux-arm64` 版本（默认 `amd64`） |
| `--refresh` | 忽略 wheel 缓存，强制重新下载 |
| `--index-url <URL>` | 指定 PyPI 源，默认沿用 pip 配置 |
| `--max-size <MB>` | 传给 `dify plugin package`。常规包默认 500；离线包默认按体积自动算，下限也是 500 |
| `--dist <DIR>` | 输出目录，默认 `dist/` |
| `--keep-staging` | 保留暂存目录便于排查 |

**安装前必须放宽 Dify 的大小限制**（离线包解压后 56.2 MB，超过默认 50 MB）。
在 Dify 部署目录的 `.env` 里改：

```ini
# 服务端插件包上限，默认 52428800（50 MB）
PLUGIN_MAX_PACKAGE_SIZE=524288000
# Nginx 上传体积上限，否则上传会被 413 拦掉
NGINX_CLIENT_MAX_BODY_SIZE=500M
# 允许安装未签名插件（自行打包的包没有官方签名）
FORCE_VERIFYING_SIGNATURE=false
```

改完重启 plugin_daemon 与 nginx：

```bash
docker compose up -d plugin_daemon nginx
docker compose exec plugin_daemon env | grep -E "PLUGIN_MAX_PACKAGE_SIZE|FORCE_VERIFYING_SIGNATURE"
```

### 5. 用 GitHub Action 重新打包（推荐）

`.github/workflows/repackage-plugin.yml` 是**手工触发**（`workflow_dispatch`）的打包流程，
它把上面 1~4 步串起来在 Linux runner 上跑，产物随 `platform_arm` 变化：

| `platform_arm` | Runner | 产物 |
| --- | --- | --- |
| `false`（默认） | `ubuntu-latest` | `ofdrw-converter-offline-linux-amd64.difypkg` |
| `true` | `ubuntu-24.04-arm` | `ofdrw-converter-offline-linux-arm64.difypkg` |

执行链路：

```
setup-java 21 + setup-python 3.12
  → 装 dify-plugin CLI（linux-<arch>，来自 dify-plugin-daemon release）
  → pip install -r requirements.txt pyyaml
  → bash scripts/build.sh                 # Maven 打 jar + jlink 生成 Linux 运行时 + 真实样例自检
  → python scripts/validate.py            # 清单 + 端到端 + 多样本回归
  → python scripts/package_offline.py --arch <arch>   # 交叉下载 wheel 并打离线包
  → sha256sum + Job Summary
  → actions/upload-artifact
```

几个刻意的设计：

- **不用仓库里提交的 `bin/`**：那是构建机（Windows）的产物，平台不对。CI 每次都从源码重建，
  保证「jar 与运行时」都出自 Linux、且与目标架构一致。
- **真在 Linux 上跑一遍转换**：`build.sh` 会用 `samples/` 里的真实 OFD 自检，漏掉 JDK 模块会当场失败。
- **wheel 缓存**：以 `runner.os + arch + hashFiles('requirements.txt')` 为 key 缓存 `.wheels-cache/`，
  避免每次重新拉 38 个轮子。
- **没有样例也能跑**：若仓库未提交 `samples/`，校验步骤自动加 `--no-sample`（会打一条 warning）。

### 6. 调试 / 不打包的用法（可选）

插件不需要发布，因此也可以完全不打包——复制 `.env.example` 为 `.env`，
填入 Dify 的远程调试地址与 Key，然后：

```bash
python -m main
```

插件会自行注册到该 Dify 实例，同样使用 `bin/` 下自带的 Java 运行时。

## 工具参数

**工具名**：`OFD to Text`（`text_exporter`）

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `ofd_file` | file | 是 | 待转换的 OFD 文档 |
| `pages` | string | 否 | 要导出的页码，**从 1 开始**，支持 `1,3,5-7`；留空导出全部页 |
| `attach_file` | boolean | 否 | 是否额外返回可下载的 `.txt` 文件，默认 `false` |
| `max_chars` | number | 否 | 文本消息截断长度，`0` 表示不限制（默认） |

## 输出

工具会产出三类消息，对应 Dify 工具节点的三个固定输出变量：

| 变量 | 内容 |
| --- | --- |
| `text` | 提取到的纯文本。若文档无法提取文本，则返回一段说明性提示 |
| `json` | 元信息：`page_count`、`char_count`、`line_count`、`elapsed_ms`、`truncated`、`ignored_pages`、`java` 等 |
| `files` | 仅当 `attach_file=true` 时存在，为 UTF-8 编码的 `.txt` 文件 |

## 命令行入口

Java CLI 也可以脱离 Dify 单独使用：

```
java -jar ofdrw-text-cli.jar --input <file.ofd> [--output <out.txt>]
                             [--pages 1,3,5-7] [--result <result.json>]
```

- 成功时向 stdout 与 `--result` 写出一行 JSON（`{"ok":true,...}`），非 ASCII 一律转义，控制台编码无关。
- 退出码：`0` 成功、`1` 转换失败、`2` 参数错误。
- 输出文本统一为 UTF-8 + LF，跨平台一致。

## 已知限制

这些限制来自 `TextExporter` 本身，不是插件引入的：

- **并非所有 OFD 都能导出文本**：整页由图片构成、整页为矢量路径图元、
  或使用字形索引（glyph id）而非 Unicode 定位文字的 OFD，无法提取出文本。
  此时 `text` 会返回提示语，`json.empty` 为 `true`。
- **文本顺序可能与原文不一致**：版式文档的文本流依赖排版信息，导出顺序不保证阅读顺序。
- **不支持加密/带口令的 OFD**。

## 换平台 / 升级

- **换平台**：Python 部分与平台无关，唯一平台相关的是 `bin/runtime`。
  在目标平台（Linux x86_64 / Linux arm64）上执行 `bash scripts/build.sh`，
  再按 [第 4 步](#4-打包离线包无外网环境)打包即可；两平台的产物由
  [GitHub Action](#5-用-github-action-重新打包推荐) 分别产出。
  Windows 上只能生成 Windows 运行时，**打包会被预检直接拒绝**——这是刻意的。
- **升级 ofdrw**：改 `jvm/pom.xml` 里的 `<ofdrw.version>`，重跑构建脚本。
- **新增导出能力（PDF/图片/SVG/HTML）**：必须删掉 `jvm/pom.xml` 里对应的 `<exclusion>`，
  并在构建脚本的模块列表（`scripts/build.ps1` 与 `scripts/build.sh` **两处**）里加回 `java.desktop`。
  注意：加上 `java.desktop` 后总体积约 61 MB，**会超出默认的 50 MB 上限**，
  需要按 [打包离线包](#4-打包离线包无外网环境) 里那样放宽 `PLUGIN_MAX_PACKAGE_SIZE`。
- **不想要自带运行时**：删掉 `bin/runtime`，并设置环境变量 `OFDRW_JAVA_HOME`
  指向任意 JDK/JRE 即可（`ofdrw_cli.py` 按 `OFDRW_JAVA_HOME` → 自带 → `PATH` 的顺序查找）。
  这种模式下的插件包与平台无关，但要求宿主环境自己装好 Java。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OFDRW_TIMEOUT` | `180` | 单次转换的子进程超时（秒） |
| `OFDRW_JAVA_HOME` | 空 | 外部 Java 目录，优先级高于插件自带运行时 |
| `OFDRW_RUNTIME_CACHE` | 系统临时目录 | 运行时被搬迁时的落脚目录。仅在插件目录只读（改不了权限位）时才会用到；若容器 `/tmp` 是 `noexec`，把它指向一个可执行目录 |

## 依赖版本

- `ofdrw-converter` 2.4.0
- 编译与 jlink：JDK 21.0.11
- `dify-plugin` SDK：>= 0.10.2,< 0.11.0（要求 Python 3.12）

离线包内共 38 个 wheel（10.9 MB），全部是 `linux` + `amd64/arm64` + `cp312` 的二进制轮子，
依赖闭包由 `dify-plugin 0.10.2` 解析得到，例如 `gevent 26.8.0`、`greenlet 3.5.6`、
`pydantic-core 2.46.5`、`tiktoken 0.14.0`、`PyYAML 6.0.3` 等。

选轮子时按 glibc 基线（`manylinux2014` → `manylinux_2_28`）取标签：
Dify plugin_daemon 的 local 镜像基于 **Ubuntu 24.04**（glibc 2.39），
所以 `manylinux_2_28` 及更旧的标签都能加载。

## 许可证

本插件代码遵循 Apache-2.0。`bin/ofdrw-text-cli.jar` 内打包了
[OFDRW](https://github.com/ofdrw/ofdrw)（Apache-2.0）及其第三方依赖。
