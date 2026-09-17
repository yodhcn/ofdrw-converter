# ofdrw-converter 项目长期备忘

## 项目定位

Dify 工具插件：OFD → 纯文本。
插件**不需要发布**，目标是本地可用、自包含。

插件根目录就是仓库根目录。

## 架构约定

三层，保持职责单一，不要混：

```
tools/text_exporter.py     只关心参数与返回值（Dify 语义）
ofdrw_cli.py               只关心 Java 定位 / 子进程 / 结果解析
jvm/.../Main.java          只关心页码解析 / 异常转 JSON / 编码归一化 / 模式分派
jvm/.../LayoutTextExtractor.java   只关心「按坐标还原版面」
```

- Java CLI 的契约：`--input` 必填，`--output` / `--pages` / `--mode` / `--result` 可选；
  结果同时写到 stdout 和 `--result` 文件（Python 优先读文件，避免解析 stdout）。
- 退出码：0 成功 / 1 转换失败 / 2 参数错误。
- 输出文本统一 UTF-8 + LF；stdout 上的 JSON 一律 ASCII 转义，与控制台编码无关。

## 文本抽取：不要用 TextExporter 切行（核心业务约束）

`TextExporter` → `ContentExtractor.getPageContent()` → `pageContent.forEach(out::println)`，
**切行完全不看坐标，一个 `TextCode` 就是一行**。而真实 OFD（发票/公文/回单）大量采用
「一个字一个 TextObject」的排版，直接导出会碎成「1 / - / 202 / 6」这种短行。

所以**默认走 `LayoutTextExtractor`（`--mode layout`）**：把对象展开成逐字图元，
按 y 聚类成行、行内按 x 排序、间隙 > 0.3×字高补空格。`--mode raw` 保留旧行为供对照。

几何依据（已在 12 个真实样本上核对，改代码前先看这段）：

- `TextObject.Boundary` 是**页面坐标系**下的外接矩形（毫米）；
- `TextCode.X/Y` 是**相对对象原点**的偏移（`X` 常为 0），`Y` ≈ 基线偏移；
- `TextCode.DeltaX/DeltaY` 是**逐字步进**，累加值恰好等于文本宽度
  （例：`50.04 = 4×5.57 + 4×2.77 + 5.57 + 2×2.77 + 5.57`）；
- `CTM` 非单位矩阵 → 坐标系被旋转/缩放，**必须放弃逐字展开**，
  退化为「整个对象按 Boundary 当一个图块」（`文字横向-数科.ofd` 就是这种，
  它还会把 `Size` 写成 209mm 这种畸高值，所以字高要用 `Boundary` 高度而不是 `Size`）。

**硬约束：`layout` 只能重排，不能动正文。** `scripts/validate.py` 第 5 步对每个样本
同时跑两种模式，断言「非空白字符的多重集」完全相同且行数不变多。改动抽取逻辑后必须过这一步。

回退开关：`tools/text_exporter.yaml` 的 `mode`（select）、`ofdrw_cli.convert_to_text(mode=)`、
CLI `--mode`。非法值在 Python 侧静默回落 layout，Java 侧报参数错误（退出码 2）。

## 必须遵守的约束

1. **产物只做 Linux，且架构必须与目标一致**。
   `jlink` 运行时是平台相关的：Windows 上生成的是 `bin/runtime/bin/java.exe`，
   打进插件包在 Linux 的 plugin_daemon 容器里起不来。
   - 目标平台 = `linux-amd64` 或 `linux-arm64`，**没有 Windows 版本**。
   - 本机（Windows）上的 `bin/runtime` 只用于调试 Java CLI，不能用来打插件包。
   - `package_offline.py` 开工前会做平台预检，命中 `bin/java.exe` / `bin/server/jvm.dll` 直接退出。
   - `manifest.yaml` 的 `meta.arch` 必须与产物一致（打包时自动改写暂存副本那份）。
   - 产物名带平台：`<name>-offline-linux-amd64.difypkg` / `-linux-arm64.difypkg`。
2. **打包体积上限可配，默认「解压后 50 MB」**。
   打包机侧 `dify plugin package --max-size`（MB，默认 50）；服务端 `PLUGIN_MAX_PACKAGE_SIZE`
   （字节，默认 52428800）+ `NGINX_CLIENT_MAX_BODY_SIZE`（默认 1M）+ `FORCE_VERIFYING_SIGNATURE=false`。
   常规包 45.3 MB 卡在默认值内；离线包 56.2 MB，安装前必须放宽。**仍应尽量压体积，不要因为能放宽就放任**。
   `package_offline.py` 里**两步打包都显式传 `--max-size`**（`DEFAULT_MAX_SIZE_MB = 500`），
   不要依赖客户端那个 50 MB 默认值——自带运行时的包本来就贴着这条线。
3. **BouncyCastle 必须保留**（`OFDReader` 注册 SM3）。
4. **jlink 模块集固定为** `java.base,java.xml,jdk.charsets,java.logging`，
   不加 `java.desktop`。**不要照抄 jdeps 的输出**。
   `scripts/build.ps1` 与 `scripts/build.sh` 里各有一份，改的时候两处都要同步。
5. 改动 `jvm/pom.xml` 的 exclusions 后，必须跑构建脚本（含真实样例自检）
   和 `scripts/validate.py`（多样本回归）双保险。
6. **`.difyignore` 里的 `.git/` 不能删**。`dify plugin package` 不会自动跳过仓库元数据，
   漏掉它会把 `.git/objects`（本项目 ~33 MB）完整打进包：常规包解压后从 45 MB 涨到 78 MB，
   报错却是一句含糊的 `Plugin package size is too large`，很容易误判成运行时没压住。
   `assert_clean_base_package()` 会在常规包打完**立刻**体检包内布局（顶层白名单 + `.git/` 禁区），
   把这类事故变成第一步就带明确原因的失败。顶层白名单逻辑（`check_layout()`）与第 5 步校验共用。
7. **包内自带的可执行文件必须自己补权限位**（`dify plugin package` 一条 mode 都不写）。
   打包侧 `package_offline.py` 的 `patch_unix_modes()` 重写 zip 条目
   （`create_system=3` + `bin/runtime/bin/*`、`*.so` = 0755），校验段断言可执行位存在；
   运行侧 `ofdrw_cli.py` 的 `resolve_java()` 在返回前确保可执行（原地 chmod，失败则整棵
   运行时复制到 `OFDRW_RUNTIME_CACHE` 或临时目录再补，然后按路径缓存）。
   改动这两处时别只留一层：运行侧那层不依赖解包器是否尊重 zip mode，是最可靠的。

## 产物生成方式

**由 GitHub Action 产出，不在本机打包**：`.github/workflows/repackage-plugin.yml`
（手工触发，输入 `platform_arm`）。

| `platform_arm` | Runner | 产物 |
| --- | --- | --- |
| `false` | `ubuntu-latest` | `ofdrw-converter-offline-linux-amd64.difypkg` |
| `true` | `ubuntu-24.04-arm` | `ofdrw-converter-offline-linux-arm64.difypkg` |

CI 每次从源码重建（不用仓库里提交的 `bin/`）：setup-java 21 → 装 dify CLI
（`dify-plugin-daemon` release 的 `dify-plugin-linux-<arch>`）→ `bash scripts/build.sh`
→ `scripts/validate.py` → `scripts/package_offline.py --arch <arch>` → 校验和 + artifact。
`dist/` 已在 `.gitignore` 中。

## 离线包机制（已实测确认）

- 做法：常规包 → 解压到暂存目录 → `pip download` 交叉下载 wheel 到 `<staging>/wheels/`
  → 把 `requirements.txt` 首行改成 `--no-index --find-links=./wheels/`
  → 改写 `manifest.yaml` 的 `meta.arch` → 重新打包。
- **pip 与 uv 都认 requirements 文件里的这两个选项**（用「只存在于本地 wheels/ 的探针包」验证过）。
- 目标环境：linux + amd64/arm64 + Python 3.12（版本从 `manifest.yaml` 的 `meta.runner.version` 读）。
  plugin_daemon 的 local 镜像 = Ubuntu 24.04 / glibc 2.39，故 `manylinux_2_28` 及更旧标签可用。
- 平台标签**不能只给** `manylinux2014`：`gevent 26.8.0` 只有 `manylinux_2_28_x86_64`。
- 只改**暂存副本**的 `requirements.txt` / `manifest.yaml`，仓库那两份保持原样。
- wheel 缓存在 `.wheels-cache/<依赖+环境指纹>/`，不要放在暂存目录里（暂存目录每次新建）。

## 常用命令

```bash
# 构建 jar + 运行时 + 自检（Linux / CI）
bash scripts/build.sh --jdk-home /opt/jdk-21

# 构建（Windows，仅供本机调试 CLI；产出的运行时不能打插件包）
pwsh -File scripts/build.ps1 -JdkHome "C:\Program Files\Java\jdk-21.0.11"

# 清单/运行时/端到端/多样本/一致性 五合一校验（不需要 Dify）
.venv/Scripts/python.exe scripts/validate.py
.venv/Scripts/python.exe scripts/validate.py --no-sample   # 无 samples 时（CI 分支）

# 离线包（平台预检 + 下载 wheel + 改写清单 + 打包 + 校验）
python scripts/package_offline.py --arch amd64
```

带 `-o` 指定输出路径时**不需要**先 `cd` 到上一级（官方文档那句只是针对不带 `-o` 的默认行为）。

## 本机环境要点（Windows bash 沙箱）

- 这个 shell 里的 bash 缺 coreutils（`dirname`/`head`/`tail` 报 not found），
  先 `export PATH="/usr/bin:/bin:$PATH"` 就都有了。
- Git Bash 下 `mvn`（sh 版本）会报 `ClassNotFoundException: ...classworlds...` → 用 `mvn.cmd` 且先 `cd jvm/`。
- **别把 POSIX 路径传给原生 Windows 程序**（`jlink`、`.venv/Scripts/python.exe`、`java.exe`）：
  它们会把 `/c/xxx` 当成相对路径，在 `C:\c\...` 下另建一份**并返回 0**（踩过两次：
  一次 jlink 造运行时，一次 Python 写测试输出）。给 `java.exe` 传参数时用 `cygpath -w`。
- `scripts/build.ps1` 依赖 `mvn.cmd`，本机 `mvn` 是 sh 版会失败；build.ps1 在
  PowerShell 工具里运行时收不到输出，需要日志就改用手工步骤（`mvn.cmd` → 复制 jar 到 `bin/`）。
  jlink 运行时不必每次重建：新代码若只用 `java.base`，现有 `bin/runtime` 直接可用。
- **`.git/refs/remotes/**` 的写入会被沙箱拦掉**（`git fetch` / `git update-ref` 都返回 0
  但引用不落盘，`git status` 显示 `[gone]`）。远端本身没问题，用
  `git ls-remote origin refs/heads/main` 核对即可；要修本地跟踪引用就直接往
  `.git/refs/remotes/origin/main` 写 SHA。
- `git push` 走代理 `http://127.0.0.1:7890`（配置在仓库 git config 里，直连不通）。
  带 `| tail` 的 push 可能长时间不返回，别急着当失败——先 `git ls-remote` 核对远端。

## 合规提示

`bin/ofdrw-text-cli.jar` 内打包了 OFDRW（Apache-2.0）及其第三方依赖，
分发时需保留相应许可证信息。
