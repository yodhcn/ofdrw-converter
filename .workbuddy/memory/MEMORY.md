# ofdrw-converter 项目长期备忘

## 项目定位

Dify 工具插件：OFD → 纯文本（OFDRW `TextExporter`）。
插件**不需要发布**，目标是本地可用、自包含。

插件根目录就是仓库根目录。

## 架构约定

三层，保持职责单一，不要混：

```
tools/text_exporter.py     只关心参数与返回值（Dify 语义）
ofdrw_cli.py               只关心 Java 定位 / 子进程 / 结果解析
jvm/.../Main.java          只关心页码解析 / 异常转 JSON / 编码归一化
```

- Java CLI 的契约：`--input` 必填，`--output` / `--pages` / `--result` 可选；
  结果同时写到 stdout 和 `--result` 文件（Python 优先读文件，避免解析 stdout）。
- 退出码：0 成功 / 1 转换失败 / 2 参数错误。
- 输出文本统一 UTF-8 + LF；stdout 上的 JSON 一律 ASCII 转义，与控制台编码无关。

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

# 清单/运行时/端到端/多样本 四合一校验（不需要 Dify）
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
- **别把 POSIX 路径传给 `jlink`**：它会当成相对路径，在 `C:\c\...` 下造一份运行时**并返回 0**。
  脚本里已用 `cygpath -w` 处理。

## 合规提示

`bin/ofdrw-text-cli.jar` 内打包了 OFDRW（Apache-2.0）及其第三方依赖，
分发时需保留相应许可证信息。
