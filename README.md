# DeepSeek Harness · Linux arm64 `.deb` 社区构建

> 非官方构建。上游是 [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)（MIT），
> 本仓库只负责补上上游尚未提供的 **Linux arm64 桌面版打包**，不改动上游功能、不内置任何密钥。

把官方 Electron 桌面端（Windows/macOS 版同源）在原生 `ubuntu-24.04-arm` 上构建，并打成可 `apt install` 的
`.deb`。所有产物都由 GitHub Actions 从固定上游 tag 现编，构建脚本、补丁与校验逻辑全部在本仓库内。

- 上游版本：`dsh-v0.2.1-rc.3`（可改 `upstream_ref` 输入切到其它 tag）
- 目标架构：`arm64`（aarch64），Debian/Ubuntu 系
- 产物：`dist/deepseek-harness_<version>~<tag>_arm64.deb`（GitHub Actions artifact `deepseek-harness-linux-arm64-deb`）

## 用户怎么用

```bash
sudo apt install ./deepseek-harness_*.deb
deepseek-harness            # 图形界面
dsh --help                  # 内置 CLI（同一个安装包提供）
sudo apt purge deepseek-harness
```

应用安装在 `/opt/deepseek-harness`，`/usr/bin/deepseek-harness` 是启动器，
桌面菜单项与 `dsh://` 协议处理器随包注册。

## 为什么不能直接“改后缀”

上游桌面端把 Electron 壳、内置 dsh 运行时（Node 24 + Python 3.12）、原生模块（node-pty、koffi、
landlock 沙箱、LibreOffice 引擎）一起打包。这些二进制都与 CPU 架构/操作系统绑定，跨架构不能靠替换文件，
必须在 arm64 Linux 上原生编译；上游的桌面打包流水线（`release.yml` + 自托管 runner + COS 上传 + 代码签名）
又只覆盖 macOS/Windows，所以本仓库做的是“在官方源码上补一条 Linux 打包支路”。

## 是怎么做到的

上游其实已经把 Linux 需要的底层能力准备好了，缺的只是桌面打包入口：

| 上游已有 | 位置 |
| --- | --- |
| Linux arm64 的 Node 24.21 / Python 3.12 运行时锁定构件 | `scripts/primary-runtime/lock.json`（`linux-arm64` 目标） |
| Linux arm64 的 landlock 沙箱原生包 | `native/system/packages/linux-arm64`（在 `ubuntu-24.04-arm` 上 CI 验证） |
| electron-builder 的 `linux` 配置雏形（AppImage） | `apps/desktop/scripts/electron-builder-config.mjs` |
| 平台判定中已预留 `linux` | `apps/desktop/src/main.ts` 等 |

本仓库补的是“桌面打包目标 + 少量运行期平台分支”：

1. **新增 `linux-arm64` 打包目标**：`desktop-build-paths.mjs` / `package-target.ts` / 工具链预检 / 自动更新目标集合；
2. **Linux 免签环境**：新增 `localLinuxPackageEnvironment`（只保留 appId 与 `DSH_DESKTOP_UNSIGNED=1`），
   跳过 macOS notarization、Windows 签名与强制更新策略；
3. **Electron/运行时路径**：解包出的 Linux Electron 可执行文件是 `electron`（macOS 是 `Electron.app/...`）；
   CLI 启动器新增 `apps/desktop/cli/dsh-linux`；
4. **打包产物定位**：`smoke-packaged-runtime.ts` 认识 `linux-arm64-unpacked/` 目录；
5. **electron-builder linux 段**：`deb` 目标、`executableName=deepseek-harness`、图标、maintainer；
6. **运行期平台分支（`apps/desktop/src/main.ts`）**：Linux 不构造强制更新策略（其平台白名单会直接抛错）、
   隐藏更新菜单并跳过自动检查（deb 没有更新源）、关闭主窗口即退出（Linux 没有托盘，隐藏后无法找回）；
7. **补全被 API 类型收缩波及的 CI 脚本**：`desktop-upload-plan.ts` 的目标表与 `retain-desktop-ci.ts` 的目标守卫；
8. **LibreOfficeKit 的 ASAR 探针修复**：见下文“是怎么做到的”中的缺陷说明。

全部改动以 **overlay 全文替换 + 精确锚点补丁** 两种形式保存在本仓库：

```
overlay/                     # 直接覆盖上游同名文件的副本
scripts/apply-linux-overlay.mjs   # overlay 复制 + 26 处精确字符串补丁（每处断言必须命中一次）
```

其中一处是 Linux 特有的真实缺陷修复：Electron 的 ASAR 文件系统在
`lstatSync(path, { throwIfNoEntry: false })` 缺失时返回 **`null`** 而不是 Node 文档规定的
`undefined`，而 LibreOfficeKit 用 `!== undefined` 判断“引擎包是否存在”，于是把“未安装的
Linux 原生引擎”误判成“安装不完整”并中止所有 Office 转换。上游从未在 ASAR 里发布过 Linux 版，
所以这个组合问题没有暴露。构建流水线在 `prepare:dsh` 阶段把该探针改成 `!= null`，
让运行时按上游设计回退到随包内置的 **WASM 引擎**。

上游文件一换 tag、补丁锚点对不上时构建会立刻失败，不会静默产出一个“看起来对”的包。

## 构建流水线

`.github/workflows/build-linux-arm64-deb.yml`：

1. `ubuntu-24.04-arm`（GitHub 免费原生 arm64 runner）+ 清盘；
2. checkout 上游固定 tag → 应用 Linux 补丁；
3. `pnpm install --frozen-lockfile`；
4. `pnpm --filter @deepseek-ai/dsh-desktop run package:dir linux-arm64 --unsigned`
   （内部依次：`build:official` 全量构建 → dsh/vendor/landlock 打包 → 下载 Node/Python 运行时 →
   准备 dsh 运行时 → electron-builder `--linux --arm64 --dir`）；
5. `packaging/build-deb.sh` 把解包目录组装成 `.deb`；
6. `tools/verify_deb.py` 做静态校验（ar/control/md5sums/ELF 架构/权限）；
7. `scripts/ci-smoke-test.sh` 在 runner 上真实 `apt install` → Xvfb 启动 Electron → 截图 → `purge` 卸载校验；
8. 上传 `.deb` 与截图证据。

`.github/workflows/probe-linux-arm64.yml` 是更快的反馈回路：只做补丁 + `build:official`，
用于改补丁时先验证编译/类型检查是否通过，不必等完整打包。

## 目录

| 路径 | 作用 |
| --- | --- |
| `.github/workflows/build-linux-arm64-deb.yml` | 完整构建 + deb + 安装启动冒烟 |
| `.github/workflows/probe-linux-arm64.yml` | 快速构建探针 |
| `scripts/apply-linux-overlay.mjs` | 移植补丁（overlay + 精确锚点） |
| `overlay/` | 替换文件（目标路径与上游一致） |
| `packaging/build-deb.sh` | deb 组装（control、maintainer scripts、docs、图标、md5sums） |
| `packaging/` | .desktop、启动器、postinst/prerm/postrm、图标 |
| `tools/verify_deb.py` | 纯标准库 deb 校验器（Windows 也能跑） |
| `scripts/ci-smoke-test.sh` | 安装→启动→截图→卸载的端到端验证 |
| `scripts/push-to-github.sh` | 无 `git push` 环境下的 Contents API 同步工具 |

## 已知限制

- **未签名**：debian 包与二进制均未做发行方签名；请核对 artifact 的 SHA-256 与构建日志。
- **不参与上游自动更新**：上游 updater 只支持 macOS/Windows 渠道，Linux 构建禁用更新（`update=undefined`），并隐藏“检查更新”菜单与自动检查。
- **Office 使用内置 WASM 引擎**：与上游 Linux CLI 的默认行为一致；Linux 原生 LibreOfficeKit 引擎上游未默认安装，本包不额外引入（见上文修复说明）。
- **仅 arm64**：`amd64` 需要另一套 runner 与目标名，本仓库未覆盖。
- **系统依赖**：Ubuntu 24.04/Debian 13 一代的库名（`libasound2t64`、`libatk-bridge2.0-0t64` 等）；更老的发行版需改 control 的 Depends。
- **CI 里的“启动成功”** 指 Electron 主窗口在 Xvfb 下创建、进程存活并截图；不等于人工验收全部交互。
