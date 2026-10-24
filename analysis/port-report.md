# DSH Desktop Linux arm64 可移植性缺口报告

- 上游：`github.com/deepseek-ai/deepseek-harness`，固定 ref `dsh-v0.2.1-rc.3`
- 已核对：tag 解析为 commit `1f81c047fc48fa190a683e469daa699857e9afe8`（`gh api .../git/ref/tags/dsh-v0.2.1-rc.3`），与本地解包源码树 `deepseek-ai-deepseek-harness-1f81c04/` 一致（抽样 `apps/desktop/scripts/desktop-build-paths.mjs` 逐字节 diff 相同）
- 结论依据：全部来自真实读到的 rc.3 源码；外部依赖（electron-builder、Electron）行为单独标注来源
- 行号均为 rc.3 源码行号；仓库路径如 `apps/desktop/...` 即 rc.3 内路径
- 目标产物：`linux-arm64` 的 unpacked 应用目录 + `deb`

---

## 0. 结论速览

### (a) 构建阶段：要产出 linux-arm64 的 unpacked 目录，必须改 8 个文件 + 1 个新增资源目录

| # | 文件 | 性质 | 一句话 |
|---|------|------|--------|
| B1 | `apps/desktop/scripts/desktop-build-paths.mjs` | 硬阻塞 | `SUPPORTED_TARGETS` 无 `linux-arm64`，`desktopTargetPlatform` 只映射 darwin/win32，任何调用都会先抛 `unsupported target` |
| B2 | `apps/desktop/scripts/package-target.ts` | 硬阻塞 | `TARGETS` 表 / 类型 / host 校验 / `builderPlatform` / unsigned 环境选择都只有 mac+win；`desktopBuildRecordFilename()` 对 linux target 也会抛错（见 B3） |
| B3 | `apps/desktop/scripts/desktop-auto-update-environment.mjs` | 硬阻塞 | `UPDATE_TARGETS` 只有 mac/win，`desktopBuildRecordFilename('linux-arm64')` 抛 `unsupported target`，在 `package-target.ts:428` 无条件调用 |
| B4 | `apps/desktop/scripts/desktop-package-environment.mjs` + `package-target.ts:346-350` | 硬阻塞 | 非 win32 目标一律去读 `apps/desktop/.env.macos`；未签名 linux 构建会因缺 `.env.macos` 直接失败；`validateDesktopPackageEnvironment()` 对 linux 还会强制解析 mandatory-policy 环境（`desktop-package-environment.mjs:139`） |
| B5 | `apps/desktop/scripts/electron-builder-config.mjs` | 硬阻塞 | 无 `packagesLinux` 概念：linux 构建仍会强制 `resolveDesktopPolicyEnvironment()`（第 68 行）与非 unsigned 的 `resolveDesktopAutoUpdateConfig()`（第 98 行），两者对 linux 都无合法取值；`linux` 段只有 `category/target`，缺 `executableName/icon/maintainer/vendor/synopsis/packageName`，deb 目标无法产出 |
| B6 | `apps/desktop/scripts/prepare-runtime.ts` | 硬阻塞 | 第 30 行 Electron 可执行文件路径 `Electron.app/Contents/MacOS/Electron`（仅区分 win32），linux 解包后应为 `<electronDist>/electron` |
| B7 | `apps/desktop/scripts/prepare-dsh.ts` | 硬阻塞 | 第 46 行 `NODE` 路径同样写死 macOS 布局，prepare:dsh 无法启动 |
| B8 | `apps/desktop/scripts/prepare-cli.ts` + `apps/desktop/cli/dsh` | 硬阻塞（产物可用性） | `prepare-cli` 类型只有 darwin/win32，且只在 darwin 上 `chmod 0755`（linux 下 `copyFileSync` 得到 0644，CLI 不可执行）；`cli/dsh` 第 14 行写死 `$resources/../MacOS/DeepSeek Harness`，linux 安装布局是 `$resources/../<executableName>` |
| B9 | `apps/desktop/scripts/smoke-packaged-runtime.ts` | 硬阻塞 | 第 15-18 行应用目录只算 `win-unpacked` / `DeepSeek Harness.app/Contents`；linux 是 `linux-arm64-unpacked`，导致打完包就必失败 |
| B10 | 新增 `apps/desktop/resources/icons/NxN.png` | 硬阻塞（品牌/图标） | 无 `build/` 资源目录、也无 electron-builder 可用的 icns/ico 图标源（`resources/icon-macos.png`、`icon-windows.png` 不是 `build/` 下的 icns/ico），linux 图标需显式提供 |

补充（非硬阻塞但会被 CI 卡住）：`apps/desktop/tests/package-target.spec.ts:29-32`、`tests/desktop-build-paths.spec.ts:58-68`、`tests/desktop-auto-update-environment.spec.ts:110-119` 目前显式断言 linux 不受支持，改实现后必须同步改测试，否则 `check:ci` 失败。`apps/desktop/electron-builder.config.d.mts` 的返回类型也要补 `linux`。

**只需“能构建出 unpacked 目录”的最短路径**：B1、B2、B3、B4、B5（仅 `--dir` + unsigned 分支）、B6、B7、B8、B9，加上把 `package-target.ts` 传入的 `builderPlatform` 扩为 `--linux`，并执行：

```sh
pnpm --dir apps/desktop run package:linux:arm64:dir   # 需新增的 package.json 脚本
# 等价于：tsx scripts/package-target.ts linux-arm64 --dir --unsigned
```

> 交叉编译不可行：`prepare-dsh.ts:137` 用 **构建主机** 的 `process.platform/arch` 选依赖（`pnpm install --prod` 会装 host 架构的可选原生包），所以 linux-arm64 必须跑在 linux-arm64 构建机上（与 mac-arm64 要求 Apple Silicon 同构，见 `package-target.ts:186-188`）。

### (b) 运行阶段：构建能过、但 Linux 上会出问题的点

按严重度：

| 优先级 | 问题 | 位置 | 后果 | 处置 |
|---|---|---|---|---|
| **P0 启动即崩** | mandatory-update policy 平台白名单 `!['win32','darwin'].includes(process.platform) → throw` | `apps/desktop/src/main.ts:1303` | 只要包内 manifest 带 `dshMandatoryUpdatePolicy`（当前 linux 构建恰好会带），Linux 启动必崩 | **构建侧不把 policy 写进 linux 包**（推荐，见 B5）；防御性再加 `process.platform === 'linux' ? undefined` |
| **P1 功能错误** | 自动更新：electron-updater 在 Linux 只有 AppImage 路径（`APPIMAGE` 环境变量不存在时 `isUpdaterActive()` 返回 false），deb 永远无法更新 | `update-coordinator.ts:55,184-198` + `updateSchedule` + `main.ts:962` | 手动“检查更新…”必然弹失败对话框；自动检查每 10 分钟静默失败；policy 若开启会逼用户更新却无法安装 | linux 包**不写 `app-update.yml`（publish: null）**；隐藏“检查更新…”菜单、跳过 `automaticCheck()` 的 update 部分；不启用 mandatory policy |
| **P1 功能错误** | 关闭窗口=隐藏，而 Linux 上 tray 只在 win32 创建 | `main.ts:992-998,1079-1088` | 关窗后进程常驻且没有托盘/无入口（`window-all-closed` 不会触发），只能再启动一次靠单实例唤回 | MVP：Linux 上 close 直接走“关闭即退出”；或 v2 增加 Linux 托盘（需 StatusNotifier/AppIndicator） |
| **P1 功能错误** | CLI 启动器写死 macOS 布局 | `apps/desktop/cli/dsh:13-14` | `/usr/bin/dsh`（或 `resources/runtime/cli/bin/dsh`）在 Linux 上找不到可执行文件 | 按 B8 改 OS 探测 |
| **P2 可接受降级** | Platform 账号身份：Host 侧 base bundle 只把 darwin/win32 视作 desktop，Linux→`desktopPlatform: null`（按 web 客户端） | `packages/bundle/base/cordis.patch.yml:120-123`；`packages/credentials/deepseek-account-platform/src/index.ts:27,54` | 账号/用量/充值请求 `x-client-platform: web`；嵌入式 Platform 视图仍报 `desktop-mac`（`main.ts:443-444` 的 else 分支、`platform-view.ts:55-56`），二者不一致 | MVP 接受；要统一则改 `cordis.patch.yml` 表达式把 linux 映射为 `darwin`（服务端是否接受 unknown 平台需实测） |
| **P2 可接受降级** | 无托盘 / 无 Dock / 无 `open-url`（Linux 只有 `second-instance` 带 argv，`main.ts:1235-1239` 的 `open-url` 在 Linux 不触发） | `main.ts:1235-1239` | `dsh://` 深链：Linux 由 `%U` 把 URL 作为 argv 进第二实例，单实例回调只 focus 不解析 argv；但当前唯一 scheme 动作是 `dsh://open → focusPrimaryWindow`，**净效果等价** | 接受；后续如有新深链动作，需在 `second-instance` 中解析 argv |
| **P2 可接受降级** | 麦克风：Linux 上 `setPermissionRequestHandler` 直接 `callback(true)`（无 OS 授权） | `microphone-permissions.ts:28` | 无系统级隐私提示，直接用 Chromium/PipeWire 采集；依赖 `libasound2t64`/PulseAudio | 接受（上游测试已覆盖：`tests/microphone-permissions.spec.ts:63`） |
| **P2 可接受降级** | 目录选择 `createDirectory` 是 macOS-only 属性 | `directory-picker.ts:24` | Linux GTK 对话框忽略该属性；仍可在对话框内手工新建目录 | 接受 |
| **P2 可接受降级** | 原生标题栏：Linux 落到 else 分支（无 `titleBarStyle`、无 vibrancy、无全屏中继、无 backdrop 切换） | `main.ts:215-277`；`preload-platform.ts:24-26` | 有系统标题栏，页面无自绘 chrome/48px 顶部留白；功能不受影响（上游测试 `tests/main-startup.spec.ts:824-844,881-889,922-931` 已按 linux 分支断言） | 接受；若想做自绘标题栏需另行实现 Linux 分支 |
| **P2 可接受降级** | CLI 命令安装（“管理 dsh 命令…”）仅 mac/win：`command-manager-entry.ts:36-38` 对其它平台抛 `EUNSUPPORTED` | `command-manager-entry.ts:17-38`；菜单只在 `main.ts:963` 的 `darwin/win32` 出现 | Linux 菜单里看不到该入口，不会崩；`deepseek-harness` 桌面可执行仍被 deb 模板软链到 `/usr/bin`，`dsh` CLI 需自己加软链 | 接受；如需 `/usr/bin/dsh` 用 deb `afterInstall` 加软链（见 §5） |
| **P2 可接受降级** | 单实例锁本身可用；`second-instance` 在 Linux 会带 argv（Electron 文档 `app.md:1212-1216`），但代码只 focus | `single-instance.ts:16-26` | 无功能损失（当前仅 focus 语义） | 接受 |
| **P2 可接受降级** | Linux 窗口关闭到隐藏时 `backgroundNotice` 为 `undefined`（仅 win32 创建） | `main.ts:999-1002,1086-1087` | 第一次关闭不弹“后台运行”确认，直接隐藏；与 tray 问题叠加 | 随 close 策略一起改 |

**一句话**：Linux 运行期真正会“崩”的只有 mandatory policy 的 `throw`；更新链路和“关窗即隐身且无托盘”是必须处理的体验/功能缺陷；其余均为已由上游测试覆盖或可文档化降级的行为。

---

## 1. 范围、方法与证据

**证据来源**

1. rc.3 完整源码：`gh api` 拉取的 tag tarball（19 MB，commit `1f81c04`），解包后逐文件阅读。tarball 末尾截断导致 `packages/` 中 `client` 之后的部分目录缺失，缺失文件用 `gh api contents?ref=dsh-v0.2.1-rc.3` 单独补齐（`packages/bundle/base/cordis.patch.yml`、`packages/credentials/deepseek-account*/src/*.ts`、`packages/boot/app-boot/src/profile.ts`、`scripts/libreoffice-packages.mjs`）。
2. 上游自身测试：`apps/desktop/tests/*.spec.ts` 中已存在大量以 `platform='linux'` 参数化的断言，用于反推“上游对 Linux 的既有预期”。
3. 外部依赖行为（非本仓库代码，已单独核对源码）：
   - electron-builder v26（仓库 tag `v26.0.20`，与 `apps/desktop/package.json:63,66` 的 `app-builder-lib@26.17.0`/`electron-builder@^26.17.0` 同代）：`packages/app-builder-lib/src/targets/FpmTarget.ts`、`templates/linux/after-install.tpl`、`src/targets/LinuxTargetHelper.ts`、`src/linuxPackager.ts`、`src/options/linuxOptions.ts`、`src/electron/ElectronFramework.ts`、`src/platformPackager.ts`；electron-updater `src/AppImageUpdater.ts`。
   - Electron 官方 API 文档（`electron/electron` repo `docs/api/*.md`）：`app.md:129`（`open-url` 仅 macOS）、`app.md:1212-1216`（macOS/Linux 第二实例参数）、`app.md:1869-1871`（`setAboutPanelOptions` 的 Linux 字段）、`power-monitor.md:57`（`shutdown` 支持 Linux/macOS）、`dialog.md:44`（`createDirectory` 仅 macOS）、`tray.md:37-43`（Linux 使用 StatusNotifierItem，退化 GtkStatusIcon）。
4. 上游文档明确表态：`apps/desktop/packaging/README.md:96` —— “Linux is not a supported Desktop release target.”

**报告约定**

- 每条格式：`文件:行号` → **现状** → **Linux 后果** → **可直接 patch 的替代代码**。
- “必须改才能构建”= 不改就一定无法产出 linux-arm64 产物（含目录构建）；“构建能过但运行期……”= 产物能出但运行/体验受影响。
- 标“**待验证**”的结论给出验证方法（§9），其余结论均有源码行号支撑。

---

## 2. 构建阶段缺口（必须改才能产出 linux-arm64 unpacked）

### B1. `apps/desktop/scripts/desktop-build-paths.mjs` — 目标集合与平台映射

- `:7` `const SUPPORTED_TARGETS = new Set(['mac-arm64', 'mac-x64', 'win-x64'])`
- `:14` JSDoc 返回 `'mac-arm64' | 'mac-x64' | 'win-x64'`
- `:21-29` `resolveDesktopBuildTarget()`：
  ```js
  const os = platform === 'darwin' ? 'mac' : platform === 'win32' || platform === 'win' ? 'win' : platform
  const target = `${os}-${arch}`
  if (!SUPPORTED_TARGETS.has(target)) throw new Error(`desktop build paths: unsupported target ${target}`)
  ```
  linux 主机 → `os='linux'` → `linux-arm64` → **抛错**。所有构建脚本（`prepare-runtime.ts:17`、`prepare-dsh.ts:39`、`prepare-package-set.ts:162`、`electron-builder-config.mjs:72`）都走这里，所以这是第一个硬阻塞。
- `:32-36` `assertSupportedTarget` 同样拒绝。
- `:40` JSDoc、`:43-61` `desktopTargetBuildPaths` 的 `target` 仅类型层面。
- `:70-76` `desktopTargetPlatform()` 只返回 darwin/win32：
  ```js
  return { platform: target === 'win-x64' ? 'win32' : 'darwin', arch: target === 'mac-arm64' ? 'arm64' : 'x64' }
  ```
  linux 会得到 `{platform:'darwin', arch:'x64'}`（且永远到不了这里，因为先抛错）。

**Linux 后果**：任何 `pnpm run package:linux:*`、`prepare:runtime`、`prepare:dsh`、`prepare:packages` 在第一步就失败；`electron-builder-config.mjs` 的 `resolveDesktopBuildTarget()` 也抛。

**Patch（可直接替换）**

```js
const SUPPORTED_TARGETS = new Set(['mac-arm64', 'mac-x64', 'win-x64', 'linux-arm64'])

/**
 * @param {NodeJS.ProcessEnv} env
 * @param {NodeJS.Platform} hostPlatform
 * @param {string} hostArch
 * @returns {'mac-arm64' | 'mac-x64' | 'win-x64' | 'linux-arm64'}
 */
export function resolveDesktopBuildTarget(env = process.env, hostPlatform = process.platform, hostArch = process.arch) {
  const platform = env.DSH_DESKTOP_TARGET_PLATFORM ?? env.npm_config_platform ?? hostPlatform
  const arch = env.DSH_DESKTOP_TARGET_ARCH ?? env.npm_config_arch
    ?? (platform === 'win32' || platform === 'win' ? 'x64' : hostArch)
  const os = platform === 'darwin' ? 'mac'
    : platform === 'win32' || platform === 'win' ? 'win'
    : platform === 'linux' ? 'linux'
    : platform
  const target = `${os}-${arch}`
  if (!SUPPORTED_TARGETS.has(target)) {
    throw new Error(`desktop build paths: unsupported target ${target}`)
  }
  return /** @type {'mac-arm64' | 'mac-x64' | 'win-x64' | 'linux-arm64'} */ (target)
}

/**
 * @param {'mac-arm64' | 'mac-x64' | 'win-x64' | 'linux-arm64'} target
 * @returns {{ platform: 'darwin' | 'win32' | 'linux', arch: 'arm64' | 'x64' }}
 */
export function desktopTargetPlatform(target) {
  assertSupportedTarget(target)
  if (target === 'linux-arm64') return { platform: 'linux', arch: 'arm64' }
  return {
    platform: /** @type {'darwin' | 'win32'} */ (target === 'win-x64' ? 'win32' : 'darwin'),
    arch: /** @type {'arm64' | 'x64'} */ (target === 'mac-arm64' ? 'arm64' : 'x64'),
  }
}
```

同步把 `:14`、`:40`、`:41`、`:67-68` 的 JSDoc 联合类型补上 `'linux-arm64'` / `'linux'`。

**要改的测试**：`apps/desktop/tests/desktop-build-paths.spec.ts:58,67,68`（当前断言 linux 抛错）。

---

### B2. `apps/desktop/scripts/package-target.ts` — 目标表、host 校验、electron-builder 参数、unsigned 环境

- `:50` `export type DesktopPackageTargetName = 'mac-arm64' | 'mac-x64' | 'win-x64'`
- `:53-59` `DesktopPackageTarget.platform: 'darwin' | 'win32'`，`builderPlatform: '--mac' | '--win'`
- `:61-83` `TARGETS` 只有三项
- `:124-126` `isTargetName` 基于 `TARGETS`
- `:171-193` `resolveDesktopPackageTarget()` host 校验只有 win/darwin 分支；linux target 必须先能进 `TARGETS`
- `:206-210` `hostTargetName()` 在无显式 target 时由 host 推导：linux 主机 → `linux-${hostArch}`，会被 `isTargetName` 拒绝（B1 修完后仍要能在 TARGETS 里找到）
- `:266-288` `desktopElectronBuilderArguments()` 只用 `target.builderPlatform`（mac/win）与 `target.builderArch`
- `:340-407` `main()`：
  - `:346-350` unsigned 环境选择：`target.platform === 'darwin' && unsigned ? localMacOSPackageEnvironment() : loadDesktopPackageEnvironment(target.platform, ...)` → linux unsigned 走 `loadDesktopPackageEnvironment`（拼 `.env.macos`，见 B4）
  - `:379` `requireDesktopToolchain(target.platform, environment)`（类型 `'darwin'|'win32'`）
  - `:380-390` 仅区分 darwin / else（else 把 stage 叫 `windows-package`，linux 只是名字错，不是阻塞）
  - `:401-406` 仅 darwin 打印 DMG 路径（linux 无提示，非阻塞）
- `:428` `desktopBuildRecordFilename(target.name)` **无条件调用** → 依赖 B3 的 `UPDATE_TARGETS`
- `:522` 最终 `execute(desktopElectronBuilderArguments(target, invocation.directory))`

**Linux 后果**：`package:linux:arm64` 在参数解析阶段就报 `unsupported target`；即便 B1 放行，后面 4 处也走不通。

**Patch**

1) 类型与目标表：

```ts
export type DesktopPackageTargetName = 'mac-arm64' | 'mac-x64' | 'win-x64' | 'linux-arm64'

export interface DesktopPackageTarget {
  readonly name: DesktopPackageTargetName
  readonly platform: 'darwin' | 'win32' | 'linux'
  readonly arch: 'arm64' | 'x64'
  readonly builderPlatform: '--mac' | '--win' | '--linux'
  readonly builderArch: '--arm64' | '--x64'
}

const TARGETS: Record<DesktopPackageTargetName, DesktopPackageTarget> = {
  // ... mac-arm64 / mac-x64 / win-x64 原样保留
  'linux-arm64': {
    name: 'linux-arm64',
    platform: 'linux',
    arch: 'arm64',
    builderPlatform: '--linux',
    builderArch: '--arm64',
  },
}
```

2) host 校验（`resolveDesktopPackageTarget` 末尾追加；沿用“原生构建”规则）：

```ts
  if (target.platform === 'linux') {
    if (hostPlatform !== 'linux') {
      throw new Error(`desktop package: ${name} requires a Linux build host`)
    }
    if (hostArch !== target.arch) {
      // prepare:dsh installs native optional dependencies for the host arch, so linux-arm64 needs an arm64 host.
      throw new Error(`desktop package: ${name} requires a native ${target.arch} Linux build host`)
    }
  }
```

3) unsigned 环境选择（`main()` 内 `:346-350`）：

```ts
  // Linux has no release-signing lane; an unsigned local build uses the same
  // configuration-free environment macOS uses, so it never needs .env.macos.
  const environment = invocation.unsigned && target.platform !== 'win32'
    ? (target.platform === 'darwin' ? localMacOSPackageEnvironment() : localLinuxPackageEnvironment())
    : loadDesktopPackageEnvironment(target.platform, process.env, APP_ROOT, {
      unsigned: invocation.unsigned, prepareOnly: invocation.prepareOnly,
    })
```

（`localLinuxPackageEnvironment` 见 B4。）

4) 打包阶段命名（`:380-390`）：

```ts
    if (target.platform === 'darwin') {
      /* ... 原逻辑 ... */
    } else if (target.platform === 'linux') {
      await packagingStep(run.directory, 'linux-package', () => packageTarget(invocation, environment, run), secrets)
    } else {
      await packagingStep(run.directory, 'windows-package', () => packageTarget(invocation, environment, run), secrets)
    }
```

5) 产物提示（`:401-406` 后追加）：

```ts
  if (success && target.platform === 'linux' && !invocation.directory && !invocation.prepareOnly) {
    const output = desktopArtifactOutput(desktopTargetBuildPaths(target.name), invocation)
    console.log(`DEB: ${output.directory}`)
  }
```

6) `package.json` 增加脚本（`apps/desktop/package.json:29-44`）：

```json
"package:linux:arm64": "tsx scripts/package-target.ts linux-arm64",
"package:linux:arm64:unsigned": "tsx scripts/package-target.ts linux-arm64 --unsigned",
"package:linux:arm64:dir": "tsx scripts/package-target.ts linux-arm64 --dir --unsigned"
```

> 说明：`main()` 里 `:423,425` 的 `DSH_DESKTOP_PACKAGING_RUN_DIR`/`resolveMacOSPackageSettings` 都受 `target.platform === 'darwin'` 保护，linux 不会进入；`:447` `signPrimaryRuntime` 只对 win32；`:502-520` 的 mac 专属分支不影响 linux；`:522` 的最终 electron-builder 调用只需 `builderPlatform='--linux'`。

---

### B3. `apps/desktop/scripts/desktop-auto-update-environment.mjs` — 构建记录文件名断言

- `:36` `const UPDATE_TARGETS = new Set(['mac-arm64', 'mac-x64', 'win-x64'])`
- `:57-64` `resolveDesktopAutoUpdateTarget(platform, arch)`：`os = platform === 'darwin' ? 'mac' : platform === 'win32' ? 'win' : platform`（linux 会得到 `linux-arm64`），但集合里没有 → 抛 `unsupported target`
- `:71-76` `desktopBuildRecordFilename(target)` 先校验 `UPDATE_TARGETS` 再返回 `${target}-release.json`
- `:85-94` `desktopUpdateMetadataFilename(version, platform)` 只接受 darwin/win32

**Linux 后果**：`package-target.ts:428` 在每个（包括 `--dir`、unsigned）打包运行都会调用 `desktopBuildRecordFilename(target.name)` → 抛错，构建中止。

**Patch**

```js
const UPDATE_TARGETS = new Set(['mac-arm64', 'mac-x64', 'win-x64', 'linux-arm64'])

/**
 * @param {NodeJS.Platform} platform
 * @param {string} arch
 * @returns {'mac-arm64' | 'mac-x64' | 'win-x64' | 'linux-arm64'}
 */
export function resolveDesktopAutoUpdateTarget(platform, arch) {
  const os = platform === 'darwin' ? 'mac' : platform === 'win32' ? 'win' : platform
  const target = `${os}-${arch}`
  if (!UPDATE_TARGETS.has(target)) throw new Error(`desktop auto-update: unsupported target ${target}`)
  return target
}
```

`desktopUpdateMetadataFilename` 维持“linux 不支持”——它只被 mac 的 `macos-app-update-config` 使用，linux 构建不会走到。若以后要做 linux 更新元数据再扩展。

**要改的测试**：`apps/desktop/tests/desktop-auto-update-environment.spec.ts:110-119`（现在断言 linux 抛错）。

---

### B4. `apps/desktop/scripts/desktop-package-environment.mjs` — 配置来源与 unsigned 校验

- `:39` `platform: 'win32' | 'darwin'` 类型
- `:45-54` `loadDesktopPackageEnvironment(platform, ...)`：
  ```js
  let path = join(appRoot, platform === 'win32' ? '.env.windows' : '.env.macos')
  ...
  const platformSetting = platform === 'win32' ? WINDOWS_SETTING : MACOS_SETTING
  ```
  linux → 读 `apps/desktop/.env.macos`（该文件被 gitignore，仓库只有 `.env.macos.example`，见 `apps/desktop/.env.macos.example`）→ 抛 `cannot read .../.env.macos`。
- `:136-143` `validateDesktopPackageEnvironment()`：
  ```js
  if (target.platform !== 'darwin' || !options.unsigned) resolveDesktopPolicyEnvironment(environment)
  ...
  if (options.unsigned) return
  ```
  linux unsigned 会在 `:139` 强制解析 mandatory policy（需要 `DSH_DESKTOP_MANDATORY_UPDATE_TEST_ORIGIN` 等），与 linux 无更新机制的事实冲突。

**Linux 后果**：未签名 linux 构建在读环境阶段失败（缺 `.env.macos`）；提供 `.env.macos` 后又在 policy 校验处失败。

**Patch**

1) 新增本地 unsigned 环境工厂（与 `localMacOSPackageEnvironment` 并列）：

```js
/**
 * Select a configuration-free unsigned Linux environment: no dotenv, no release settings.
 * @param {NodeJS.ProcessEnv} environment Parent environment retained for build tools and networking.
 * @returns {NodeJS.ProcessEnv} Unsigned build environment with the standard application identifier.
 */
export function localLinuxPackageEnvironment(environment = process.env) {
  return {
    ...Object.fromEntries(Object.entries(environment).filter(([name]) => !AMBIENT_RELEASE_SETTING.test(name))),
    DSH_DESKTOP_APP_ID: 'com.deepseek.harness',
    DSH_DESKTOP_UNSIGNED: '1',
  }
}
```

2) 放宽 `loadDesktopPackageEnvironment` 的平台分支（供未来 signed linux 或 preset 使用）：

```js
  const platformFile = platform === 'win32' ? '.env.windows' : platform === 'linux' ? '.env.linux' : '.env.macos'
  let path = join(appRoot, platformFile)
  ...
  const platformSetting = platform === 'win32' ? WINDOWS_SETTING : platform === 'darwin' ? MACOS_SETTING : /^$/
```
（若暂时不做 signed linux，可只加 `localLinuxPackageEnvironment`，`loadDesktopPackageEnvironment` 保持按 `:45` 的签名但显式拒绝 linux：`if (platform === 'linux') throw new Error('desktop package: signed Linux packaging is not configured')`。）

3) 校验里跳过 linux 的 policy 与 auto-update：

```js
  // Linux deb has no updater and no mandatory-update enforcement: policy/update feeds stay unset.
  if (target.platform !== 'linux' && (target.platform !== 'darwin' || !options.unsigned)) {
    resolveDesktopPolicyEnvironment(environment)
  }
  ...
  if (options.unsigned || target.platform === 'linux') return
  if (!options.prepareOnly) resolveDesktopAutoUpdateConfig(environment, target.platform, target.arch)
```

---

### B5. `apps/desktop/scripts/electron-builder-config.mjs` — 缺 `packagesLinux`，并且 linux 段无法产出 deb

**现状（关键行）**

- `:54-56` `targetPlatform = env.DSH_DESKTOP_TARGET_PLATFORM`，`resolvedPlatform = targetPlatform ?? hostPlatform`（linux 下已是 `'linux'`，后续很多逻辑并不识别它）
- `:66-68`
  ```js
  const packagesMacOS = targetPlatform === 'darwin' || (targetPlatform === undefined && hostPlatform === 'darwin')
  const packagesWindows = resolvedPlatform === 'win32'
  const policy = packagesMacOS && unsigned ? undefined : resolveDesktopPolicyEnvironment(env)
  ```
  **linux（unsigned 也一样）会执行 `resolveDesktopPolicyEnvironment`** → 需要 test/production 的 policy origin → 若按 B4 提供本地 unsigned 环境则直接抛错；若强行提供 policy 环境，产物会带 `dshMandatoryUpdatePolicy`（`:112`），运行期命中 `main.ts:1303` 的 `throw`。
- `:98-99`
  ```js
  const update = unsigned ? undefined : resolveDesktopAutoUpdateConfig(env, resolvedPlatform, resolvedArch)
  ```
  linux signed/unrecognised → `unsupported target`；unsigned 则 OK。
- `:77-78` `asarUnpack` 里 `**/node_modules/@deepseek-ai/libreoffice-kit-${resolvedPlatform}-${resolvedArch}/**/*` —— resolvedPlatform=`linux` 时正确，配合 `beforePack` 的 `officePackageDirectories(..., { platform: resolvedPlatform, arch: resolvedArch })`（`:182`）→ linux-arm64 会选中 `libreoffice-kit-wasm`（若 npm 上无 linux-arm64 原生包；**待验证**，见 §9）。
- `:159-180` 只有 `mac` 段；`:238-247` `win` 段；`:248-251`
  ```js
  linux: {
    category: 'Development',
    target: ['AppImage'],
  },
  ```
- `:155-157` `extraResources` 里 icon 只有 `icon-windows.png → icon.png`；tray 图标仅 `packagesWindows` 才加。
- `:126-135` `beforeBuild` 只在 win32 下跑 PowerShell（安全）。
- `:209-217` `afterSign` 只在 win/signed 或 darwin 下做事（安全）。
- `:195-208` `afterPack` 用 `resolvedPlatform/resolvedArch` 校验 runtime descriptor（linux 可用，前提是 `prepare-dsh` 写出的 descriptor 也是 linux；见 B7）。

**Linux 后果**

1. linux 构建在配置生成阶段因 policy/update 解析失败（同 B4，属于另一层强制）；
2. 即使绕过，`linux` 段只有 `category`/`AppImage`：没有 `deb` 目标、没有 `executableName`（默认会是 `sanitizedProductName`=`"DeepSeek Harness"` 带空格，见 `linuxPackager.ts:20-21`）、没有 `icon`（electron-builder 会用默认 Electron 图标）、deb 需要的 `maintainer`/`packageName`/`homepage` 全缺（见 §5 外部依赖行为）。
3. `target: ['AppImage']` 与目标是 deb 冲突。

**Patch（核心部分；完整建议见 §5）**

```js
  const packagesMacOS = targetPlatform === 'darwin' || (targetPlatform === undefined && hostPlatform === 'darwin')
  const packagesWindows = resolvedPlatform === 'win32'
  const packagesLinux = resolvedPlatform === 'linux'
  // Linux deb has no in-app update path and no mandatory-update enforcement.
  const policy = packagesLinux || (packagesMacOS && unsigned) ? undefined : resolveDesktopPolicyEnvironment(env)
  ...
  const update = unsigned || packagesLinux ? undefined : resolveDesktopAutoUpdateConfig(env, resolvedPlatform, resolvedArch)
```

并把 `:248-251` 替换为：

```js
    linux: {
      executableName: 'deepseek-harness',
      icon: fileURLToPath(new URL('../resources/icons', import.meta.url)),
      category: 'Development',
      synopsis: 'DeepSeek Harness desktop',
      description: 'Desktop shell for the DeepSeek Harness agent runtime.',
      maintainer: 'DeepSeek <desktop@deepseek.com>',
      vendor: 'DeepSeek',
      target: ['deb'],
      desktop: {
        entry: {
          StartupWMClass: 'deepseek-harness',
          Keywords: 'ai;agent;deepseek;harness;',
        },
      },
    },
    deb: {
      packageName: 'deepseek-harness',
      packageCategory: 'devel',
      priority: 'optional',
      depends: [
        'libgtk-3-0t64 | libgtk-3-0',
        'libnotify4',
        'libnss3',
        'libnspr4',
        'libxss1',
        'libxtst6',
        'xdg-utils',
        'libatspi2.0-0t64 | libatspi2.0-0',
        'libuuid1',
        'libsecret-1-0',
        'libasound2t64 | libasound2',
        'libcups2t64 | libcups2',
        'libgbm1',
        'libdrm2',
        'libxkbcommon0',
        'libxcomposite1',
        'libxdamage1',
        'libxfixes3',
        'libxrandr2',
        'libxext6',
        'libx11-6',
        'libx11-xcb1',
        'libxcb1',
        'libexpat1',
        'libdbus-1-3',
        'libpango-1.0-0',
        'libcairo2',
        'libgdk-pixbuf-2.0-0',
        'libxshmfence1',
      ],
      // 仅当启用 Linux 托盘时保留（见 §3 R3）：
      // recommends: ['libayatana-appindicator3-1 | libappindicator3-1'],
    },
```

说明：

- `linux.icon` 指向图标目录，文件名必须含尺寸（electron-builder `linuxOptions.ts:53` 与官方文档“Icon Configuration”）；建议从 `resources/icon.png`（1104×1104）缩出 `16x16/24x24/32x32/48x48/64x64/128x128/256x256/512x512.png`，放在新增目录 `apps/desktop/resources/icons/`。
- `deb.packageName` 必须显式设置：`linuxPackageName` 默认取 `appInfo.sanitizedProductName`（`appInfo.ts:149-152`，因为包名 `@deepseek-ai/dsh-desktop` 以 `@` 开头），会得到带空格的 `"DeepSeek Harness"`，不是合法 deb 包名。
- `maintainer` 必须设置：`FpmTarget.ts:82-97` 在 `linux.maintainer` 为空时回落到 `package.json.author.email`，而 `apps/desktop/package.json` 没有 `author` → 抛 `authorEmailIsMissed`。
- 项目 homepage 必须存在：`FpmTarget.ts:82-86` 在 `computePackageUrl()` 为空时报 `Please specify project homepage`；`apps/desktop/package.json` 与根 `package.json` 都没有 `homepage`/`repository` → 需补（见 §5）。
- `afterSign`/`beforeBuild`/`afterPack` 已按平台守卫，linux 不需要改；`electronFuses.runAsNode`（`:122`）是 CLI/Host 需要的，linux 同样有效。
- `artifactName`（`:118`）里的 `${os}` 在 linux 上展开为 `linux`，无需改。
- **不要**为 linux 设置 `publish`（`:264` 在 `update === undefined` 时自动为 `null`），这样 electron-builder 不会写 `app-update.yml`，运行期 `update-coordinator.ts:55` 的 `enabled()` 返回 false，更新链路安全失败（见 §3 R2）。

**要改的声明**：`apps/desktop/electron-builder.config.d.mts:4-59` 的 `DesktopElectronBuilderConfig` 需加 `readonly linux: {...}` 与 `readonly deb: {...}`，否则类型检查失败。

---

### B6. `apps/desktop/scripts/prepare-runtime.ts` — Electron 解包布局

- `:23-34` `prepareElectron({platform, arch})`：`downloadArtifact({version, platform, arch, artifactName: 'electron', cacheRoot})`（linux 正确，`@electron/get` 支持 linux/arm64）→ `extractZip`
- `:30`
  ```ts
  const executable = join(BUILD_PATHS.electron, platform === 'win32' ? 'electron.exe' : 'Electron.app/Contents/MacOS/Electron')
  ```
- `:31-33` 直接 `execFileSync(executable, ['-p', 'process.versions.node'], { env: { ELECTRON_RUN_AS_NODE: '1' } })` 取内嵌 Node 版本

**Linux 后果**：linux 上 `Electron.app/...` 不存在 → `ENOENT`，`prepare:runtime` 失败。

**Patch**

```ts
  const executable = join(BUILD_PATHS.electron,
    platform === 'win32' ? 'electron.exe'
      : platform === 'darwin' ? 'Electron.app/Contents/MacOS/Electron'
        : 'electron')
```

其余 `:36-49` `prepareCli`：`cpSync(node-bin → runtime/bin)` + `chmodSync(bin/node, 0o755)` 在 linux 正确；`if (platform === 'darwin')` 的 PlistBuddy 块只在 mac 跑；`:48` 拷贝 `command-path.ps1` 在 linux 无害（可加 `if (platform === 'win32')` 清理）。需要同步放宽 `prepareDesktopCli` 的类型（见 B8）。

**待验证**：linux 构建机上 `electron` 以 `ELECTRON_RUN_AS_NODE=1` 启动仍会加载 Electron 的共享库依赖（`libnss3`、`libgtk-3` 等）。最小容器里若未装桌面库会报 `error while loading shared libraries`。验证：`ldd <electronDist>/electron | grep 'not found'`，并预先安装 §6 的运行时依赖（或直接用 electron-builder 官方 Linux 构建镜像）。

---

### B7. `apps/desktop/scripts/prepare-dsh.ts` — Node 模式可执行文件路径与目标平台

- `:46`
  ```ts
  const NODE = join(BUILD_PATHS.electron, process.platform === 'win32' ? 'electron.exe' : 'Electron.app/Contents/MacOS/Electron')
  ```
  该 `NODE` 用于 `:80-105` 的 `spawn(NODE, ['--expose-internals', PNPM, 'install', ...])`（用 Electron 的 Node 模式跑 pnpm）以及 `:170` 的 smoke。
- `:136-137`
  ```ts
  const targetName = resolveDesktopBuildTarget()
  const target = { platform: process.platform, arch: desktopTargetPlatform(targetName).arch }
  ```
  linux-arm64 原生构建机上 `platform='linux'`、`arch='arm64'` 正确；但要注意这意味着必须先有 B1 的 `desktopTargetPlatform('linux-arm64') → {linux, arm64}`。
- `:139` 读 `@deepseek-ai/libreoffice-kit/package.json` → `selectOfficeEngine(officeManifest, target)`（`scripts/libreoffice-packages.mjs:10-13`：无 `libreoffice-kit-linux-arm64` 时回落 `'wasm'`）
- `:157-159` 校验 `libreoffice-kit-${officeEngine}/prebuilds.json` 存在
- `:160-164` mac 签名块，linux 跳过
- `:167` `writeDesktopRuntime(..., target)` 写 `platform:'linux', arch:'arm64'`
- `:168` `verifyDesktopRuntime(..., target)`
- `:41` 临时目录 `mkdtempSync(join(tmpdir(), ...))`，linux `/tmp` 可用

**Linux 后果**：第 46 行路径 ENOENT → `prepare:dsh` 失败；其余逻辑在目标平台正确后可用。

**Patch**

```ts
const NODE = join(BUILD_PATHS.electron,
  process.platform === 'win32' ? 'electron.exe'
    : process.platform === 'darwin' ? 'Electron.app/Contents/MacOS/Electron'
      : 'electron')
```

**确认项**：`runtime-tree.ts:79,208` 的可执行位逻辑是 `process.platform !== 'win32'`，linux 构建机（linux）下会记录 executable 位，且 `verifyDesktopRuntime` 用同一平台计算，一致。`writeDesktopRuntime` 的 `target` 由 `process.platform` 决定，**要求 linux-arm64 产物必须在 linux-arm64 构建机上生成**（否则 descriptor 的 platform/arch 与 electron-builder 的 `verifyDesktopRuntime(resolvedPlatform, resolvedArch)` 对不上，`electron-builder-config.mjs:204-205` 会报 `incompatible platform/architecture`）。

---

### B8. `apps/desktop/scripts/prepare-cli.ts` 与 `apps/desktop/cli/dsh` — CLI 启动器

- `prepare-cli.ts:11`
  ```ts
  export function prepareDesktopCli(destination: string, platform: 'darwin' | 'win32'): void {
    const name = platform === 'win32' ? 'dsh.cmd' : 'dsh'
    ...
    copyFileSync(join(import.meta.dirname, '..', 'cli', name), command)
    if (platform === 'darwin') chmodSync(command, 0o755)
  }
  ```
  `copyFileSync` **不保留源文件权限**：linux 上 `dsh` 会得到 0644（umask 默认），不可执行。类型也不允许 `'linux'`（`prepare-runtime.ts:40` 传 `target.platform`，B1 放宽后会变成 `'linux'`，TS 报错）。
- `apps/desktop/cli/dsh:13-14`
  ```sh
  resources=$(CDPATH= cd -- "$(dirname -- "$launcher")/../../.." && pwd -P)
  ELECTRON_RUN_AS_NODE=1 exec "$resources/../MacOS/DeepSeek Harness" --expose-internals "$resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js" "$@"
  ```
  linux 安装布局（deb 模板 `installPrefix=/opt/<sanitizedProductName>`，`FpmTarget.ts:235`）是：
  `/opt/DeepSeek Harness/resources/{app.asar,runtime}` + `/opt/DeepSeek Harness/deepseek-harness`
  没有 `MacOS/` 目录。

**Linux 后果**：`/usr/bin/dsh`（或 `runtime/cli/bin/dsh`）要么不可执行，要么找不到 Electron 可执行文件；CLI 完全不可用。

**Patch 1：`prepare-cli.ts`**

```ts
export function prepareDesktopCli(destination: string, platform: 'darwin' | 'win32' | 'linux'): void {
  const name = platform === 'win32' ? 'dsh.cmd' : 'dsh'
  const command = join(destination, 'bin', name)
  mkdirSync(join(destination, 'bin'), { recursive: true })
  copyFileSync(join(import.meta.dirname, '..', 'cli', name), command)
  // copyFileSync copies bytes, not permission bits: Linux needs an explicit executable mode.
  if (platform !== 'win32') chmodSync(command, 0o755)
}
```

**Patch 2：`apps/desktop/cli/dsh`（按布局探测，保持 mac 行为）**

```sh
#!/bin/sh
# Run the installed CLI without changing the invoking shell or working directory.
set -e
launcher=$0
while [ -L "$launcher" ]; do
  directory=$(CDPATH= cd -- "$(dirname -- "$launcher")" && pwd -P)
  target=$(readlink "$launcher")
  case "$target" in
    /*) launcher=$target ;;
    *) launcher=$directory/$target ;;
  esac
done
resources=$(CDPATH= cd -- "$(dirname -- "$launcher")/../../.." && pwd -P)
app=$(CDPATH= cd -- "$resources/.." && pwd -P)
# macOS bundles the executable under Contents/MacOS; Linux .deb installs it beside resources/.
executable="$app/MacOS/DeepSeek Harness"
[ -x "$executable" ] || executable="$app/deepseek-harness"
ELECTRON_RUN_AS_NODE=1 exec "$executable" --expose-internals "$resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js" "$@"
```

> 注意：`/opt/DeepSeek Harness` 含空格，上面的 `"$app/..."` 已引用；`prepare-runtime.ts:37-38` 的 `runtime/bin/node` 包装脚本不依赖安装路径，无需改。

---

### B9. `apps/desktop/scripts/smoke-packaged-runtime.ts` — 打包后 smoke 的应用路径

- `:13` `const windows = target === 'win-x64'`
- `:15-18`
  ```ts
  const application = windows ? join(artifacts, 'win-unpacked')
    : join(artifacts, target === 'mac-arm64' ? 'mac-arm64' : 'mac', 'DeepSeek Harness.app', 'Contents')
  const resources = join(application, windows ? 'resources' : 'Resources')
  const executable = windows ? join(application, 'DeepSeek Harness.exe') : join(application, 'MacOS', 'DeepSeek Harness')
  ```
- `:19-22` 随后 `verifyDesktopRuntime(paths.dsh, ..., resolveDesktopPackageTarget(target))` 与 `smokePreparedRuntime(resources/app.asar/dsh, executable, resources/runtime, descriptor)`

**Linux 后果**：linux 上 `application` 会指向不存在的 `.../mac/DeepSeek Harness.app/Contents`，`package-target.ts:522-524` 的 `smoke-packaged-runtime` 必失败，构建在最后一步中止（`--dir` 也一样）。

**Patch**

```ts
const windows = target === 'win-x64'
const linux = target.startsWith('linux-')
const artifacts = desktopArtifactOutput(paths, { unsigned: values.unsigned, skipNotarization: values['skip-notarization'] }).directory
// electron-builder appOutDir = <platform><arch-suffix>-unpacked (platformPackager.ts:375);
// arm64 gets the arch suffix: linux-arm64-unpacked.
const application = windows ? join(artifacts, 'win-unpacked')
  : linux ? join(artifacts, target === 'linux-arm64' ? 'linux-arm64-unpacked' : 'linux-unpacked')
    : join(artifacts, target === 'mac-arm64' ? 'mac-arm64' : 'mac', 'DeepSeek Harness.app', 'Contents')
const resources = join(application, windows || linux ? 'resources' : 'Resources')
const executable = windows ? join(application, 'DeepSeek Harness.exe')
  : linux ? join(application, 'deepseek-harness')
    : join(application, 'MacOS', 'DeepSeek Harness')
```

（`deepseek-harness` 来自 §5 的 `linux.executableName`；`verifyDesktopRuntime` 的 target 参数来自 `resolveDesktopPackageTarget()`，B2 放宽后类型/值都正确。）

---

### B10. linux 图标资源（新增目录）

`electron-builder-config.mjs:248-251` 的 linux 段没有 `icon`；`resources/` 下只有 `icon-macos.png(1024²)`、`icon-windows.png(1024²)`、`icon.png(1104²)`、`tray-windows.ico`（已读文件头得到尺寸），没有 electron-builder 会默认读取的 `build/icon.icns|icon.ico`（也不存在 `apps/desktop/build/`）。`ElectronFramework.ts` 在无自定义图标时用模板里的 `electron-linux` 默认图标。

**Linux 后果**：deb 安装的是默认 Electron 图标（不阻断构建，但产物不可用作品牌发布）。

**Patch**：新增 `apps/desktop/resources/icons/{16x16,24x24,32x32,48x48,64x64,128x128,256x256,512x512}.png`（从 `resources/icon.png` 缩放，`sharp@0.35.5` 已是 devDependency，`apps/desktop/package.json:71`），并在 §5 的 `linux.icon` 指向该目录。

---

### B11. 构建链中“看起来可疑但实际可用”的部分（不改）

| 文件 | 结论 |
|---|---|
| `apps/desktop/scripts/prepare-package-set.ts` | 用系统 `tar -xOzf` 读 tarball（`:90-96`）与 `tarballFiles`；Linux 天然可用。无平台假设，不用改。 |
| `apps/desktop/scripts/runtime-file-policy.ts` | `:38` `node-pty && target.platform !== 'win32' → 丢弃 conpty`、`:41-44` 按 `${platform}-${arch}` 过滤 prebuilds、`:27-29` libreoffice 其它平台引擎过滤；对 linux 已正确（`tests/runtime-file-policy.spec.ts:94-160` 以 linux/arm64 参数化）。不用改。 |
| `apps/desktop/scripts/flock-entry.ts` | `:58-72` 已把 `linux` 与 `darwin` 一起视为需要 host addon 的平台；linux 构建机上 flock addon 由 `build:native-system` 产出（需要本机 C 编译器）。不用改。 |
| `apps/desktop/scripts/desktop-toolchain-preflight.ts` | `:25-44` tar 探测 + 仅 win32 的 vswhere 探测；linux 只探测 tar。需把函数签名 `platform: 'darwin' | 'win32'`（`:77-80`、`:94-97`）扩成 `'linux'`（类型改动，行为不变）。 |
| `apps/desktop/scripts/package-target.ts:476-500` | `build:official` / `release:pack` / pack desktop-host / pack vendor / native landlock / `prepare:runtime` / `prepare:packages` / `prepare:dsh` 都是平台无关命令（根 `package.json:25,204`；`check:ci:linux-primary` 也说明主构建链有 Linux 路径）。 |
| `apps/desktop/scripts/prepare-runtime-manifests.ts` | 仅用 `app-builder-lib/out/fileTransformer.js`，平台无关。 |
| `apps/desktop/scripts/smoke-prepared-runtime.ts` / `smoke-runtime.ts` | 通过传入的 Electron 可执行文件以 Node 模式启动，不创建窗口；HTTP 冒烟。linux 可用（唯一风险是 Office 引擎 vs wasm 的性能/超时，见 §9）。 |
| `apps/desktop/scripts/electron-builder-config.mjs:181-194` `beforePack` | `officePackageDirectories(buildPaths.dsh, { platform: resolvedPlatform, arch: resolvedArch })`；linux-arm64 时若 npm 包没有原生引擎会选 wasm 并把 wasm 包加入 asarUnpack。逻辑正确，前提是 B5 的 `packagesLinux` 让 `resolvedPlatform='linux'` 传下去（当前已传，只是 policy/update 会先失败）。 |

---

## 3. 运行阶段缺口（构建能过，但 Linux 上会出错/降级）

### R0（P0，启动崩溃）mandatory-update policy 平台白名单

- `apps/desktop/src/main.ts:1291-1331`
  ```ts
  const policyInput: unknown = app.isPackaged
    ? ('dshMandatoryUpdatePolicy' in manifest ? manifest.dshMandatoryUpdatePolicy : undefined)
    : ...
  const policyConfig = resolveDesktopPolicyConfig(policyInput, !app.isPackaged)
  if (policyConfig !== undefined) {
    ...
    if (!['win32', 'darwin'].includes(process.platform) || !['x64', 'arm64'].includes(process.arch)) {
      throw new Error('desktop policy: unsupported platform')
    }
    mandatoryPolicy = new DesktopMandatoryUpdatePolicy(policyConfig, {
      platform: process.platform as 'win32' | 'darwin', arch: process.arch as 'x64' | 'arm64', ...
  ```
- `apps/desktop/src/mandatory-update-policy.ts:8` `readonly platform: 'win32' | 'darwin'`；`:154-165` 请求头用 `platformClientHeaders(this.identity.platform, ...)`；`packages/credentials/deepseek-account/src/index.ts:144-146` 只区分 `'win32'` 与“其它（desktop-mac）”。
- 构建侧对应行为：`electron-builder-config.mjs:68,112` 对 linux **会**写入 `extraMetadata.dshMandatoryUpdatePolicy`（因为 `packagesMacOS=false`，且 policy 解析不因 unsigned 跳过）。

**Linux 后果**：任何带 policy 的 linux 包，启动时 `main()` 直接抛异常 → `reportFatal` → 应用不可用。

**修复（推荐：linux 不启用 policy）**

1. 构建侧（B5）：`const policy = packagesLinux || (packagesMacOS && unsigned) ? undefined : resolveDesktopPolicyEnvironment(env)`，这样 linux 包 manifest 里没有 `dshMandatoryUpdatePolicy`。
2. 运行侧防御（改 `main.ts:1296`）：

```ts
  // Linux has no installable update path, so mandatory enforcement would create an unescapable block.
  const policyConfig = process.platform === 'linux'
    ? undefined
    : resolveDesktopPolicyConfig(policyInput, !app.isPackaged)
```

（不再需要改 `:1303` 的白名单；`policyConfig === undefined` 时整块跳过。）

### R1（P1，功能缺陷）自动/手动更新链路在 deb 上不可能成功

- `apps/desktop/src/update-coordinator.ts:55`
  ```ts
  private readonly enabled: () => boolean = () => app.isPackaged && existsSync(join(process.resourcesPath, 'app-update.yml')),
  ```
  `:184-198` `doCheck()`：`if (!this.enabled()) throw new Error('desktop update: this application has no packaged update source')`；否则 `await this.updater.checkForUpdates()`，`null` 视为错误。
- electron-updater 在 Linux 的 `autoUpdater` 是 `AppImageUpdater`；其 `isUpdaterActive()` 在 `process.env.APPIMAGE == null` 时返回 false（electron-builder v26 源码 `packages/electron-updater/src/AppImageUpdater.ts:18-27`），于是 `checkForUpdates()` 直接 `Promise.resolve(null)`（`AppUpdater.ts:514-517`）→ 这里变成 `desktop update: no check result was returned`。
- `apps/desktop/src/main.ts:962` 菜单项“检查更新…”无条件存在；`:921-924` `automaticCheck()` 每 10 分钟检查（`update-schedule.ts:20` 默认 600000ms），`:1332` 启动即调用。
- `apps/desktop/scripts/desktop-auto-update-environment.mjs:85-94` 明确不接受 linux 的 metadata。

**Linux 后果**：deb 包（无 `app-update.yml`）启动后自动检查静默进入 error 状态；用户点“检查更新…”会看到失败弹窗；若将来带 policy 更会阻塞使用。

**修复（最小集）**

1. 构建侧：linux 的 `publish` 为 `null`（B5 的 `update === undefined` 已保证），不生成 `app-update.yml`/`latest-linux.yml`。
2. `main.ts`：linux 上不加“检查更新…”菜单项，并让自动检查跳过 update 部分：

```ts
  const applicationItems = (): MenuItemConstructorOptions[] => [
    ...
    ...(process.platform === 'linux' ? [] : [
      { label: currentDesktopLocale().messages.checkUpdatesMenu, click: () => { void openUpdatePrompt(true) } },
    ]),
    ...
  ]

  const automaticCheck = (): void => {
    if (quitting) return
    void mandatoryPolicy?.check('foreground-or-resume').catch((error: unknown) => { console.error(error) })
    // electron-updater has no installable Linux target other than AppImage; a .deb cannot update itself.
    if (process.platform === 'linux') return
    void updateSchedule.check().catch((error: unknown) => { console.error(error) })
  }
```

3. 可选（更彻底）：`updatesOpen` IPC（`:795-798`）在 linux 上直接返回而不打开失败流程，避免 renderer 设置页的“检查更新”入口触发错误弹窗。
4. 如果 Linux 也需要自动更新，唯一被 electron-updater 支持的形态是 **AppImage**（设置 `APPIMAGE` 环境变量由 AppImage runtime 注入），deb 需要走 apt 仓库/自建发布器，超出本次范围。

### R2（P1，功能缺陷）关闭窗口 → 隐藏，而 Linux 没有托盘

- `main.ts:992-998` tray 仅 `process.platform === 'win32'` 创建
- `main.ts:999-1002` `backgroundNotice` 仅 win32
- `main.ts:1079-1088`
  ```ts
  window.on('close', (event) => {
    if (quitting || shellInstallerOwnsQuit || sessionEnding) return
    event.preventDefault()
    ...
    if (backgroundNotice === undefined) hide()
    else backgroundNotice.close(hide)
  })
  ```
- `main.ts:1244-1245` `window-all-closed` 非 darwin 退出——但关闭被 `preventDefault` + `hide()`，该事件不会触发。
- `tray.ts:1` 头注释“Windows system tray”；`main.ts:1013` 任务对话框图标也仅 win32。

**Linux 后果**：用户按窗口关闭按钮后应用从屏幕消失但进程常驻（Host 继续跑），无托盘入口；唯一唤回方式是再次启动应用（单实例 `focusPrimaryWindow`，`main.ts:1219-1233`）。“关闭”在 Linux 桌面惯例里通常是退出，这一行为意外。

**修复 A（MVP，推荐）：Linux 关闭即退出**

```ts
    window.on('close', (event) => {
      if (quitting || shellInstallerOwnsQuit || sessionEnding) return
      // There is no always-present tray on Linux; hiding would leave an unreachable background process.
      if (process.platform === 'linux') { quitting = true; app.quit(); return }
      event.preventDefault()
      ...
```

（`quitting = true` 会触发 `before-quit` 的确认流程；`window-all-closed` 也会兜底退出。）

**修复 B（v2）：增加 Linux 托盘**

- Electron Linux 托盘使用 StatusNotifierItem，退化 GtkStatusIcon（`tray.md:37-43`），GNOME 默认无 AppIndicator 扩展时可能不显示，需 `libayatana-appindicator3-1`；
- 需要新增 `resources/tray-linux.png`（22/24px）并在 `electron-builder-config.mjs:153-158` 的 `extraResources` 中仅对 linux 加 `{ from: '../resources/tray-linux.png', to: 'tray.png' }`；
- `main.ts:991` 的 `trayIconPath` 按平台切换，`:992` 条件改为 `win32 || linux`，`tray.ts` 的 ICO 多尺寸注释改为平台相关；
- close 逻辑保持 hide（恢复原语义）。

### R3（P2，降级）Platform 账号身份与嵌入式 Platform 视图的平台标记不一致

- `packages/bundle/base/cordis.patch.yml:120-123`
  ```yaml
  - id: deepseek-account
    name: '@deepseek-ai/dsh-deepseek-account-platform'
    config:
      desktopPlatform: !!js "ctx.get('profileContext')?.name === 'desktop' && ['darwin', 'win32'].includes(process.platform) ? process.platform : null"
  ```
  Linux → `null`，即 Host 按 **web 客户端** 处理账号请求。
- `packages/credentials/deepseek-account-platform/src/index.ts:27,54`：`desktopPlatform?: 'darwin' | 'win32' | null`，默认 `null`。
- `packages/credentials/deepseek-account/src/index.ts:144-146` `desktopClientHeaders`：只有 `'win32'` → `desktop-win`，其余非空 → `desktop-mac`。
- 但 Electron 侧：`main.ts:443-444`
  ```ts
  const platformView = new DesktopPlatformView(..., process.platform === 'win32' ? 'win32' : 'darwin')
  ```
  `platform-view.ts:55-56` 构造参数类型就是 `'darwin' | 'win32'`；嵌入式 Platform 视图的请求头按 `desktop-mac` 注入（`:113-137`）。

**Linux 后果**：Host 账号/用量/充值请求以 `x-client-platform: web` 发送，而嵌入式 Platform 页面以 `desktop-mac` 发送；官方平台端是否限制 desktop-only 能力**待验证**。这是最容易“看着能跑但服务端行为不同”的隐藏风险。

**修复（可选，需评估服务端）**

- 方案 1（MVP，接受降级）：不改，文档记录“Linux 作为 web 客户端接入 Platform”。
- 方案 2（统一为 desktop-mac 语义）：在运行期补丁 `cordis.patch.yml`（`prepare-dsh` 之后、打包之前）：
  ```yaml
  desktopPlatform: !!js "ctx.get('profileContext')?.name === 'desktop' && ['darwin', 'win32', 'linux'].includes(process.platform) ? (process.platform === 'linux' ? 'darwin' : process.platform) : null"
  ```
  这样 Host 与嵌入式视图一致（都是 desktop-mac），但服务端会把 Linux 客户端计成 macOS；是否可接受**待验证**（对比 `x-client-platform` 的服务端使用点）。
- 方案 3（正规）：扩展 `dsh-deepseek-account` 与 `dsh-deepseek-account-platform` 接受 `'linux'` 并新增 `desktop-linux` 头，同时平台端支持该值——跨仓库改造，超出本次范围。

### R4（P2，降级）窗口 chrome：Linux 落到“无自绘标题栏”的 else 分支

`main.ts` 中 `darwin`/`win32` 分支在 Linux 的落点（逐条）：

| 行 | 代码 | Linux 行为 |
|---|---|---|
| `:215-219` | `win32 && primary → titleBarStyle:'hidden' + titleBarOverlay` | 不生效，使用系统标题栏 |
| `:222-230` | `darwin → hiddenInset + trafficLightPosition + vibrancy + 透明背景` | 不生效 |
| `:245-255` | `darwin || win32 → 全屏状态中继` | **不注册**（上游测试 `main-startup.spec.ts:881-889` 断言） |
| `:256-277` | `darwin → 最小化/隐藏时切换 vibrancy/背景色` | **不注册**（`main-startup.spec.ts:922-931` 断言） |
| `:299-300` | `win32 → 右键菜单本地化 label` | 使用 Electron 默认 role 文案；菜单仍可用 |
| `:444` | `process.platform === 'win32' ? 'win32' : 'darwin'` | 平台视图上报 **darwin**（见 R3） |
| `:687` | `darwin?macos:win32?windows:'linux'` | **显式支持 `'linux'`**，快捷键按 linux 分支工作 |
| `:946-953` | darwin → File/Window 菜单与 hide 命令 | Linux 使用 non-darwin 菜单：`[{role:'editMenu'}]` + 应用子菜单 |
| `:957-960` | win32 自绘 About，否则 `role:'about'` | Linux 走 `role:'about'`（`app.showAboutPanel()`，`app.md:1869-1871` 说明 Linux 支持 `authors/website/iconPath`）；**待验证** GTK 关于面板实际展示 |
| `:963` | `darwin||win32 → “管理 dsh 命令…”` | **Linux 不显示该菜单**（与 `command-manager-entry.ts:36-38` 的 EUNSUPPORTED 一致） |
| `:977` | `darwin?quit标签:win32?exit标签:{}` | Linux 使用默认 Quit 文案 |
| `:984` | win32 → 隐藏 devtools 占位菜单，否则应用菜单 | Linux 走应用菜单（同 mac 结构但少 File/Window 菜单） |
| `:991-998` | tray 仅 win32 | **Linux 无托盘**（见 R2） |
| `:999-1002` | backgroundNotice 仅 win32 | Linux `undefined`（见 R2） |
| `:1010` | `darwin → app.focus({steal:true})` | Linux 不抢焦点，确认框可能被遮挡；可接受 |
| `:1013` | win32 → 退出对话框 icon | Linux 无自定义图标 |
| `:1016-1061` | win32 专属 IPC（窗口菜单、标题栏外观） | 不注册；renderer 只在 `data-windows-titlebar` 时调用（`preload-windows.ts:9` 仅 win32 安装） |
| `:1064-1070` | `darwin && fullscreen` 先退全屏再隐藏 | Linux 直接 hide（无全屏处理；可接受） |
| `:1089-1098` | win32 `session-end` / else 在 focus/show 清除 sessionEnding | Linux 走 else，行为安全 |
| `:1196` | welcome 窗口关闭：darwin 销毁主窗，否则退出 | Linux 退出（符合桌面惯例） |
| `:1244-1245` | 非 darwin `window-all-closed → app.quit` | Linux 触发（配合 R2 的 close 策略才有效） |
| `:1247` | 非 win32 注册 `powerMonitor.on('shutdown')` | Linux 支持该事件（`power-monitor.md:57`：Linux/macOS），行为正确 |
| `:1303-1306` | policy 平台白名单与 identity | **Linux 会抛错**（见 R0） |

`preload-platform.ts:24-26` 也按“仅 darwin/win32 订阅全屏”处理，与 `main.ts:245` 一致，Linux 无该死代码。

**结论**：Linux 的窗口 chrome 是“系统标题栏 + 无自绘控件”的可用降级；上游测试已按此预期断言（`main-startup.spec.ts:824-844`），无需改代码。

### R5（P2，降级）其他已确认的平台行为

| 项 | 位置 | Linux 行为 | 结论 |
|---|---|---|---|
| 麦克风 | `microphone-permissions.ts:17-30` | 非 darwin 直接 `callback(true)`；无 OS 授权状态检查。需要 `libasound2t64`/PulseAudio 或 PipeWire；上游测试 `microphone-permissions.spec.ts:63` 已断言 “leaves system authorization to Chromium on linux” | 接受 |
| 目录选择 | `directory-picker.ts:24` `properties: ['openDirectory','createDirectory']` | `createDirectory` 仅 macOS（`dialog.md:44`），Linux 忽略；GTK 对话框仍可新建目录 | 接受 |
| 深链/协议 | `main.ts:1235-1239` `app.on('open-url')` | `open-url` 仅 macOS（`app.md:129`）；Linux 由 `.desktop Exec ... %U`（electron-builder v26 `LinuxTargetHelper.ts:113-127`）把 URL 作为 argv 传给新进程，再由单实例把 argv 送到主进程（`app.md:1212-1216`）。主进程 `second-instance` 监听器（`single-instance.ts:24`）只 focus，不解析 argv | 当前唯一 scheme 动作为 `dsh://open → focusPrimaryWindow`，净效果等价；若增加新深链动作需解析 argv |
| 协议注册 | `main.ts:1235` `app.setAsDefaultProtocolClient('dsh')` | deb 的 `.desktop` 会写 `MimeType=x-scheme-handler/dsh;`（electron-builder `LinuxTargetHelper.ts:158-165`），安装脚本跑 `update-desktop-database`（after-install.tpl:24-27）。但 Electron 在 Linux 通过 `xdg-settings` 设默认处理程序，桌面文件名与 `app.name` 是否匹配**待验证** | 建议在 `deb.afterInstall` 里补 `xdg-mime default deepseek-harness.desktop x-scheme-handler/dsh`（见 §5） |
| 日志路径 | `main.ts:85,110,319` `app.setAppLogsPath()` | Linux 默认 `~/.config/<app>/logs`（Electron 行为），崩溃报告可写 | 接受 |
| 单实例 | `single-instance.ts:16-26` | `requestSingleInstanceLock` 在 Linux 可用；`second-instance` 的 argv 限制见上 | 接受 |
| 关闭确认 | `quit-confirmation.ts:78-91` | `windows=false`，`type:'warning'`；Linux 原生框正常 | 接受 |
| 通知/任务栏 | `update-attention.ts:34-44` | `flashFrame` 仅 win32；`app.dock` 可选链；`Notification` 在 Linux 依赖 libnotify（打包后桌面环境有） | 接受 |
| CLI 命令管理 | `command-manager-entry.ts:17-38` | 非 darwin/win32 抛 `EUNSUPPORTED`；菜单不显示（`main.ts:963`），不会崩 | 接受；如需 `/usr/bin/dsh` 用 deb afterInstall |
| `app.setAboutPanelOptions` | `main.ts:934-941` | Linux 支持 `iconPath/authors/website`（`app.md:1869-1871`）；`version` 字段 macOS-only，传空串无影响 | 接受；GTK 面板外观待验证 |
| `role:'about'` | `main.ts:960` | 通用 role；Linux 应触发 GTK About（**待验证**） | 接受 |
| `powerMonitor.on('resume')` | `main.ts:925` | Linux 支持 | 接受 |
| Host 子进程 | `host-process.ts:188-201` 用 `process.execPath` + `ELECTRON_RUN_AS_NODE=1` | Linux 正常；Electron 二进制路径含空格时 `spawn` 以 argv 传递不受影响 | 接受 |
| 登录 shell 环境 | `login-shell-environment.ts:176` 仅 win32 跳过探测 | Linux 走 POSIX 探测（`/bin/zsh|bash|sh -ilc`），这正是在桌面启动时需要的行为；上游测试 `login-shell-environment.spec.ts:102,139,176` 以 linux 参数覆盖 | 接受（无需改） |
| 运行时完整性 | `runtime-tree.ts:79,195,208` | linux 记录/校验可执行位，`process.platform !== 'win32'` 分支正确 | 接受（无需改） |

### R6. `apps/desktop-host` 是否有平台注册表？

**没有平台注册表/派发表**。逐文件核对：

- `src/index.ts:18-104`：与平台无关，参数为 runtimeDir/projectDir/primaryRuntime/包管理器入口；启动 `runProfile` 并回报 ready/platform-session/shutdown。
- `src/platform-session.ts`：纯 Cordis 事件发布，无平台分支。
- `src/office.ts:35`：`process.platform === 'win32' ? 'node.exe' : 'node'` —— 唯一一处平台判断，Linux 落到 `'node'`（正确，`prepare-runtime.ts` 把 node 放在 `runtime/primary-runtime/dependencies/node/bin/node`）。
- `src/office-engine.ts:33-34`：ASAR 引擎路径重写的正则 **已包含 `linux`**（`(?:darwin|win32|linux)-`），Linux 原生/`wasm` 引擎都走同一套解析。
- `src/cli.ts:29-36`：仅 win32 动态 import `windows-cli-signals.ts`；Linux 无 SIGINT 特殊处理（Electron Node 模式在 Linux 下继承终端信号，正常）。
- `src/windows-cli-signals.ts`：仅 Windows 使用。
- `src/update-tasks.ts` / `src/quit-inspection.ts`：纯 Cordis 服务查询，无平台假设。

**结论**：desktop-host 在 Linux 上不需要平台相关改动；唯一跑不通的是它依赖的 dsh bundle 里 Platform 账号身份（R3）和 UI 侧 shell（R0/R1/R2）。

---

## 4. 上游已有的 Linux 能力（单独列出）

上游并非“完全没考虑 Linux”：**运行时/沙箱/CLI 主链**已有完整 Linux 支持，缺的只是 Desktop 打包与 Electron 壳的少量适配。

| 能力 | 证据 |
|---|---|
| Linux x64/arm64 原生 system 包（Landlock + flock） | `native/system/docs/support-matrix.md:5-6`（linux-x64/linux-arm64，ubuntu-24.04/24.04-arm）；`:12-14`（Landlock 需要 enforcing kernel）；`native/system/README.md:11-14`；`native/system/packages/linux-arm64/package.json`、`prebuilds.json`（`bin/landlock-run` + glibc/musl `system.node`） |
| 沙箱后端含 Linux | `docs/subsystems/sandbox.md:5,30`（bwrap/Landlock/Seatbelt/Windows ACL）；`packages/README.md:47` |
| 构建链有 Linux 主 CI | 根 `package.json:89` `check:ci:linux-primary` |
| Desktop 打包把 landlock 放进运行时 | `apps/desktop/scripts/desktop-build-paths.mjs:59` `packedLandlock`；`package-target.ts:486-495`（`native/system/packages/entry` 打包进 `packedLandlock`）；`prepare-package-set.ts:163-167` |
| runtime 文件策略含 Linux | `runtime-file-policy.ts:38-45`；测试 `tests/runtime-file-policy.spec.ts:94-160` 以 linux/arm64 参数化（含 `libreoffice-kit-wasm` 预期） |
| primary-runtime 有 linux-x64/arm64 锁定构件 | `scripts/primary-runtime/lock.json:78-125`（Node 24.21.0 / Python 3.12.14 / numpy/pandas/pillow/lxml manylinux arm64 wheels）；`scripts/primary-runtime/prepare.ts:216-217` 已映射 `linux` 平台 |
| Electron 壳部分代码已按 Linux 写 | `main.ts:687`（快捷键平台 `'linux'`）；`main.ts:824-844,881-889,922-931,998-1009`（测试以 linux 参数断言标题栏/全屏/菜单行为）；`preload-platform.ts:24-26`；`microphone-permissions.ts:28`；`login-shell-environment.ts:176`；`flock-entry.ts:61`；`office-engine.ts:33-34`；`keyboard.ts`/`keybindings.ts` 的 `desktop:linux` 绑定；`tests/device-info.spec.ts:39`、`tests/fatal-recovery.spec.ts:51` |
| 非 Desktop 的 Linux 测试支持 | `apps/desktop/tests/platform-storage.e2e.ts:20-22`（Linux 下 xvfb） |
| 明确“不支持 Desktop Linux” | `apps/desktop/packaging/README.md:96`；`tests/package-target.spec.ts:29`、`desktop-build-paths.spec.ts:58/67/68`、`desktop-auto-update-environment.spec.ts:110-119`（断言 linux 抛错） |
| 没有 linux 打包 workflow | `.github/workflows/` 只有 `macos-package.yml`、`windows-package.yml`，无 `linux-package.yml` |
| libreoffice 引擎选择机制已支持“无原生包 → wasm” | `scripts/libreoffice-packages.mjs:10-13`；`prepare-dsh.ts:139-140`；`runtime-file-policy.spec.ts:150-160` 期望 `linux-arm64 → wasm` |

**含义**：Linux 移植的工作量集中在 `apps/desktop/scripts/*` 与 `apps/desktop/src/main.ts` 少量分支，以及 electron-builder 配置；底层 dsh/sandbox/office/runtime 不需要重写。

---

## 5. electron-builder linux/deb 配置建议（可直接落地）

### 5.1 `apps/desktop/electron-builder-config.mjs` 的 `linux`/`deb` 段

```js
    linux: {
      executableName: 'deepseek-harness',
      icon: fileURLToPath(new URL('../resources/icons', import.meta.url)),
      category: 'Development',
      synopsis: 'DeepSeek Harness desktop',
      description: 'Desktop shell for the DeepSeek Harness agent runtime.',
      maintainer: 'DeepSeek <desktop@deepseek.com>',   // 必填：包内无 author.email
      vendor: 'DeepSeek',
      target: ['deb'],
      desktop: {
        entry: {
          StartupWMClass: 'deepseek-harness',
          Keywords: 'ai;agent;deepseek;harness;',
        },
      },
      // protocols 已由顶层 `protocols: [{ schemes: ['dsh'] }]` 自动写入 MimeType=x-scheme-handler/dsh;
      // 如需其他 MIME 再写 mimeTypes。
    },
    deb: {
      packageName: 'deepseek-harness',                  // 必填：默认 linuxPackageName 含空格
      packageCategory: 'devel',
      priority: 'optional',
      depends: [ /* 见 §6 */ ],
      // recommends: ['libayatana-appindicator3-1 | libappindicator3-1'],  // 仅当启用 Linux 托盘
      // afterInstall: fileURLToPath(new URL('./deb-after-install.sh', import.meta.url)),  // 需要 /usr/bin/dsh 时
    },
```

**为什么这些字段是必须的（外部依赖行为，已核对 electron-builder v26 源码）**

| 字段 | 依据 |
|---|---|
| `executableName` | v26 `linuxPackager.ts:20-21`：默认取 `appInfo.sanitizedProductName`，本应用为 `"DeepSeek Harness"`（含空格）；显式设置为 `deepseek-harness` 后，`/opt/<sanitizedProductName>/deepseek-harness`、`.desktop` 的 `Icon=`、`/usr/bin/` 软链都用它 |
| `icon` | v26 `linuxOptions.ts:53`：目录或单 png，文件名必须含尺寸；否则退回 Electron 默认图标 |
| `maintainer` | v26 `FpmTarget.ts:82-97`：`linux.maintainer` 为空时要求 `package.json.author.email`；`apps/desktop/package.json` 无 `author` → 构建报 `authorEmailIsMissed` |
| 项目 homepage | v26 `FpmTarget.ts:82-86`：`computePackageUrl()` 为空时报 `Please specify project homepage`。`apps/desktop/package.json` 与根 `package.json` 都没有 `homepage`/`repository` → 需在 `apps/desktop/package.json` 增加 `"homepage": "https://github.com/deepseek-ai/deepseek-harness"`（或 electron-builder 顶层 `extraMetadata.homepage`） |
| `deb.packageName` | v26 `appInfo.ts:149-152`：包名 `@deepseek-ai/dsh-desktop` 以 `@` 开头 → 用 `sanitizedProductName`（含空格）作为 deb 名，非法 |
| `target: ['deb']` | 现有 `linux.target: ['AppImage']` 与目标不符；可改为 `['deb']`（要同时要 AppImage 就 `['deb','AppImage']`，但注意 AppImage 下载体积/图标/FUSE2 依赖） |
| `desktop.entry.StartupWMClass` | v26 `LinuxTargetHelper.ts:130-144` 默认 `StartupWMClass=productName`（`"DeepSeek Harness"`），与 Electron 的 app_id/WM_CLASS 可能不匹配；显式设为 `deepseek-harness` 并在 `package.json` 加 `"desktopName": "deepseek-harness"`（Electron 44 支持该字段，**待验证** `xprop WM_CLASS`） |
| `afterInstall` | 默认模板（v26 `templates/linux/after-install.tpl`）会：`update-alternatives` 建 `/usr/bin/<executableName>` 软链、按 userns 情况给 `chrome-sandbox` 设 4755/0755、`update-mime-database`、`update-desktop-database`、按需安装 AppArmor profile（Ubuntu 24+）。**覆盖 `afterInstall` 会整体替换该模板**，必须把上述逻辑一起复制 |
| `deb.appArmorProfile` | v26 `linuxOptions.ts:138` 支持；不配置则用 electron-builder 默认 profile（`FpmTarget.ts:154-156` 复制到 `resources/apparmor-profile`，安装脚本装载）。Ubuntu 24.04 上如果 AppArmor 限制 unprivileged userns 而未装 profile，Electron 可能启动失败；**待验证**（见 §9） |

### 5.2 附加：`/usr/bin/dsh` CLI 软链（可选）

若需要 `dsh` 命令在 PATH 中，创建 `apps/desktop/scripts/deb-after-install.sh`（复制 v26 默认模板全文后追加）：

```sh
# Custom part: expose the bundled CLI as `dsh` (the default template only links the GUI executable).
CLI='/opt/${sanitizedProductName}/resources/runtime/cli/bin/dsh'
if [ -x "$CLI" ]; then
  ln -sf "$CLI" /usr/bin/dsh || true
fi
# Ensure the URI handler default is registered (desktop MimeType is written by update-desktop-database).
DESKTOP_FILE='${executable}.desktop'
if command -v xdg-mime >/dev/null 2>&1; then
  xdg-mime default "$DESKTOP_FILE" x-scheme-handler/dsh || true
fi
```

并在 `deb.afterInstall` 指向它（注意 `${executable}`/`${sanitizedProductName}` 是 electron-builder 的模板变量，v26 `FpmTarget.ts:399-409` 对自定义脚本同样做 `${var}` 替换）。

**风险**：必须逐行保留默认模板内容（否则 chrome-sandbox 权限、AppArmor、MIME 数据库都会丢），维护成本高于“不加 CLI 软链”。

### 5.3 `apps/desktop/package.json` 需要的元数据

```json
{
  "name": "@deepseek-ai/dsh-desktop",
  "description": "Electron desktop shell for a bundled dsh runtime and external plugins",
  "version": "0.2.1-rc.3",
  "homepage": "https://github.com/deepseek-ai/deepseek-harness",
  "desktopName": "deepseek-harness",
  "license": "MIT",
  ...
}
```

（`homepage` 解决 deb 的 project URL；`desktopName` 供 Electron 44 计算 Linux app_id/WM_CLASS，且让 electron-builder 的 `StartupWMClass` 有可对齐的值。两者都不影响 mac/win 现有行为。）

---

## 6. deb 运行时依赖清单建议（Ubuntu 24.04 arm64 / Electron 44）

> 依据：electron-builder v26 deb 默认列表（`FpmTarget.ts:344-347`：`libgtk-3-0, libnotify4, libnss3, libxss1, libxtst6, xdg-utils, libatspi2.0-0, libuuid1, libsecret-1-0`）**不含** Electron 44/Chromium 需要的 `libasound2`、`libgbm1`、`libxkbcommon0`、`libcups2` 等；Ubuntu 24.04 的 64-bit time_t 迁移把其中一部分改名为 `*t64`。以 `A | B` 备选写法同时兼容 22.04/24.04（fpm 会把 `--depends` 原样写进 control，deb 控制字段支持 `|`）。

### 6.1 建议的 `deb.depends`（完整列表，放入 §5.1）

```yaml
- libgtk-3-0t64 | libgtk-3-0          # GTK3（24.04 t64）
- libnotify4                          # 通知（更新/后台提示）
- libnss3                             # Chromium 网络加密
- libnspr4
- libxss1
- libxtst6
- xdg-utils                           # xdg-open / xdg-mime（协议、外链）
- libatspi2.0-0t64 | libatspi2.0-0    # 无障碍
- libuuid1
- libsecret-1-0                       # safeStorage / Secret Service
- libasound2t64 | libasound2          # 音频采集（麦克风）
- libcups2t64 | libcups2              # 打印（Chromium 打印栈）
- libgbm1                             # Chromium GPU/GBM
- libdrm2
- libxkbcommon0                       # 输入法/键盘映射（Wayland/X11）
- libxcomposite1
- libxdamage1
- libxfixes3
- libxrandr2
- libxext6
- libx11-6
- libx11-xcb1
- libxcb1
- libexpat1
- libdbus-1-3
- libpango-1.0-0
- libcairo2
- libgdk-pixbuf-2.0-0
- libxshmfence1
```

**可选**

```yaml
# 仅当启用 Linux 托盘（R2 修复 B）：
- libayatana-appindicator3-1 | libappindicator3-1
# 音频走 PipeWire 的发行版（24.04 默认已装，列了更稳）：
- libpipewire-0.3-0
# GPU 解码/视频（可选）：
- libva2
```

### 6.2 说明与验证

- **t64 名称必须实测**：本机网络只能走 `gh`，`packages.ubuntu.com` 在本次会话返回 HTTP 500，无法直接查 noble 索引；以上 `t64` 名称基于 Ubuntu 24.04 的 t64 迁移（同类项目修例：`libasound2`→`libasound2t64` 等）。用 `A | B` 写法可避免猜错：apt 会选可安装的那个。
  **验证方法**：在干净的 `ubuntu:24.04` arm64 容器里
  ```sh
  apt-get update
  apt-get install -y ./deepseek-harness_0.2.1~rc.3_arm64.deb   # 或 -s 只求解
  apt-cache policy libgtk-3-0t64 libasound2t64 libatspi2.0-0t64 libcups2t64
  ```
- `libsecret-1-0` 只在用到 safeStorage 时才必需；Electron 默认 deb 列表里有它，保留。
- **不需要**：`python3`（primary-runtime 自带 Python 3.12）、`libfuse2`（仅 AppImage）、`node`/`pnpm`（自带 Electron Node 模式与 pnpm）。
- **不要**把 `libgtk-3-0`（无 t64）写成唯一依赖——24.04 上可能无此包名。
- `chrome-sandbox` 权限由 electron-builder 安装脚本按 userns 支持情况处理（v26 `after-install.tpl:13-19`）：userns 可用 → 0755 + AppArmor profile；不可用 → 4755。Ubuntu 24.04 默认 userns 可用但受 AppArmor 限制，因此 **AppArmor profile 是否成功装载是能否启动的关键**，需在真机验证（§9）。
- 若 AppArmor 装载失败（如容器/无 systemd 环境），启动排错命令：
  ```sh
  /opt/DeepSeek\ Harness/deepseek-harness --no-sandbox   # 临时验证用，不能作为发布默认
  journalctl -k | grep -i apparmor
  aa-status
  ```

---

## 7. 构建主机与 CI 前置条件

1. **必须 linux-arm64 原生构建机**（理由：`prepare-dsh.ts:137` 用 host 的平台/架构安装生产依赖与可选原生包；`runtime-tree` 的 descriptor 平台/架构也取 host）。不能像 mac 那样在 x64 上交叉。
2. **构建机需预装 Electron 运行库**：`prepare:runtime` 会用解包出来的 Electron 以 `ELECTRON_RUN_AS_NODE=1` 跑 `-p process.versions.node`（`prepare-runtime.ts:31-33`），`prepare:dsh` 用它跑 pnpm（`prepare-dsh.ts:80-105`）；Electron 二进制即使 Node 模式也会加载其链接的共享库。验证：
   ```sh
   ldd apps/desktop/.desktop-build/targets/linux-arm64/electron/electron | grep 'not found'
   ```
   缺库时按 §6 清单装到构建机（或用 electron-builder 官方 `electronuserland/builder` 镜像）。
3. **工具链**：Node 24（`engines: ^22.19 || >=24`，根 `package.json:8-9`）、pnpm 11.28.5（`packageManager`）、`git`、GNU/bsd `tar`（`desktop-toolchain-preflight.ts:25-44`、`prepare-package-set.ts:90-96`）、C 编译器（`build:native-system` 编 flock addon，`flock-entry.ts:74-84`）、`xz`（fpm deb 压缩；fpm 报 `xz: not found` 时按 `FpmTarget.ts:318-324` 提示装 `xz-utils`）。
4. **electron-builder 在 arm64 上的二进制**：
   - fpm 有 `linux-arm64v8` 包（v26 `targets/tools.ts` 的 `getKey()` 对 `process.arch==='arm64'` 返回该文件）；
   - `app-builder`（unpack-electron、签名等）是否有 arm64 版本**待验证**（`app-builder-bin` 的平台包）；若缺失，退路是在 x64 构建机用 `electronuserland/builder` Docker（`--platform linux/arm64`，QEMU）执行，但 `prepare-dsh` 的“原生架构依赖”要求在 arm64 用户态里跑——Docker+QEMU 可行但慢。
   - 需要网络：npm registry（含 `@deepseek-ai/*` Linux optional deps）、nodejs.org（Node 24.21.0）、github.com/astral-sh/python-build-standalone、Electron 下载、electron-builder-binaries（fpm/7z/app-builder）。
5. **CI**：新增 `.github/workflows/linux-package.yml`（当前只有 `macos-package.yml`、`windows-package.yml`）；runner 用 `ubuntu-24.04-arm`。构建命令：
   ```sh
   pnpm install --frozen-lockfile
   pnpm run package:linux:arm64:dir     # 先确认目录可用
   pnpm run package:linux:arm64         # deb（如仍 unsigned，需在 B4/B5 放行）
   ```
6. **无显示环境**：`prepare:*`/`smoke-packaged-runtime` 都用 Electron Node 模式 + HTTP 冒烟，不需要 X；只有真正启动 GUI 验证时才需 `xvfb-run -a`（上游 e2e 也这样做，`tests/platform-storage.e2e.ts:20-22`）。

---

## 8. 最小修改文件清单与实施顺序

### 8.1 先做到 `--dir` 产出 linux-arm64 unpacked（构建 P0）

1. `apps/desktop/scripts/desktop-build-paths.mjs`（B1）
2. `apps/desktop/scripts/desktop-auto-update-environment.mjs`（B3）
3. `apps/desktop/scripts/desktop-package-environment.mjs`（B4：`localLinuxPackageEnvironment` + policy/update 跳过）
4. `apps/desktop/scripts/package-target.ts`（B2：TARGETS/类型/host 校验/unsigned 环境/`--linux`/产物提示）
5. `apps/desktop/scripts/electron-builder-config.mjs`（B5：`packagesLinux` + policy/update 跳过 + 临时 `linux` 段）
6. `apps/desktop/scripts/prepare-runtime.ts`（B6）
7. `apps/desktop/scripts/prepare-dsh.ts`（B7）
8. `apps/desktop/scripts/prepare-cli.ts` + `apps/desktop/cli/dsh`（B8）
9. `apps/desktop/scripts/smoke-packaged-runtime.ts`（B9）
10. `apps/desktop/scripts/desktop-toolchain-preflight.ts`（类型放宽，B11）
11. `apps/desktop/electron-builder.config.d.mts`（类型声明）
12. `apps/desktop/package.json`（新增 `package:linux:*` 脚本 + homepage/desktopName）
13. 更新测试：`tests/package-target.spec.ts:29-32`、`tests/desktop-build-paths.spec.ts:58-68`、`tests/desktop-auto-update-environment.spec.ts:110-119`

验收：

```sh
pnpm --dir apps/desktop run package:linux:arm64:dir -- --unsigned
ls apps/desktop/.desktop-build/targets/linux-arm64/unsigned-artifacts/linux-arm64-unpacked/
# 期望存在：deepseek-harness、resources/app.asar、resources/runtime/{bin,primary-runtime,cli}
# 以及 smoke-packaged-runtime 通过（apply 后）
```

### 8.2 产出 deb（构建 P1）

14. `apps/desktop/scripts/electron-builder-config.mjs` 的 `linux`/`deb` 完整段（§5.1）
15. 新增 `apps/desktop/resources/icons/*.png`（B10）
16. `apps/desktop/package.json`：`homepage`、`desktopName`（§5.3）
17. （可选）`deb.afterInstall` 自定义脚本（§5.2）

验收：

```sh
pnpm --dir apps/desktop run package:linux:arm64 -- --unsigned
ls apps/desktop/.desktop-build/targets/linux-arm64/unsigned-artifacts/*.deb
dpkg-deb -I *.deb        # 检查 Depends/Maintainer/Homepage/Description
dpkg-deb -c *.deb | head # 检查 /opt/DeepSeek Harness/{deepseek-harness,resources/...}
```

### 8.3 运行期最小修复集（发布 deb 前）

18. `apps/desktop/src/main.ts:1296`（R0：linux 不加载 policy；构建侧 B5 已保证 manifest 无 policy）
19. `apps/desktop/src/main.ts:962`（R1：linux 隐藏“检查更新…”）与 `:921-924`（跳过自动 update 检查）
20. `apps/desktop/src/main.ts:1079-1088`（R2：linux 关闭即退出；或实现 Linux 托盘）
21. 运行验证：`xvfb-run -a /opt/DeepSeek\ Harness/deepseek-harness`，与 `dsh` CLI（若加软链）

### 8.4 可选/后续

- R3：Linux 的 Platform 账号身份统一（改 bundle 配置或扩展账号包，需服务端确认）
- 自绘标题栏、Linux 托盘、AppImage 同步发布、deb 自动更新（需自建 apt 仓库）

---

## 9. 待验证清单与验证方法

| # | 待验证事项 | 为什么不确定 | 验证方法 |
|---|---|---|---|
| V1 | Linux 构建机上 Electron 能否以 Node 模式运行（共享库齐全） | Electron 二进制链接 GTK/NSS 等库，本次无 Linux 环境实测 | `ldd <electronDist>/electron \| grep 'not found'`；`ELECTRON_RUN_AS_NODE=1 <electron> -p process.versions.node` |
| V2 | electron-builder/app-builder 在 linux-arm64 构建机上的原生二进制可用性 | `app-builder-bin` 各版本平台包内容未能列出（npm 包体积超 jsdelivr 限制） | 在 arm64 构建机跑 `npx electron-builder --linux --arm64 --dir`，观察 `app-builder` 执行是否报 `Exec format error`；失败则用 x64 构建机 + Docker/qemu 的 `electronuserland/builder` |
| V3 | `libreoffice-kit` 是否有 linux-arm64 原生引擎 | 仓库不含该 npm 包 manifest；上游测试 `runtime-file-policy.spec.ts:152` 期望 `wasm` | `npm view @deepseek-ai/libreoffice-kit@0.2.1-rc.3 optionalDependencies`（在有 npm 网络的机器）；或看 `prepare:dsh` 产出的 `runtime/primary-runtime/.../libreoffice-kit*/prebuilds.json` |
| V4 | wasm 引擎在 linux-arm64 上的 Office 转换是否在 smoke 的 120s 超时内 | `smoke-runtime.ts` 对 docx/xlsx/pptx 各限 120s；wasm 比原生慢 | 在 arm64 构建机跑 `prepare:dsh` 的 `runtime:smoke`，记录耗时；必要时提高 `smokePreparedRuntime` 超时或 `--defer-runtime-smoke`（仅调试） |
| V5 | Ubuntu 24.04 上 deb 依赖的 t64 名称是否正确可装 | `packages.ubuntu.com` 本次 HTTP 500；t64 名称靠已知迁移推断 | 干净 `ubuntu:24.04`（arm64）里 `apt-get install -s ./x.deb`，逐个 `apt-cache policy` 核对 |
| V6 | AppArmor profile 是否成功装载、应用能否在 24.04 启动 | electron-builder 模板“dry-run 失败则跳过 profile”（`after-install.tpl:42-56`），跳过时 userns 受 AppArmor 限制可能起不来 | 真机装 deb 后 `aa-status \| grep deepseek`、启动应用；失败时看 `dmesg \| grep -i apparmor` |
| V7 | `app.setAsDefaultProtocolClient('dsh')` 在 deb 安装后是否生效（桌面文件名与 `app.name` 是否匹配） | Electron Linux 用 `xdg-settings` 设默认处理程序，桌面文件名由 electron-builder 决定 | 装包后：`xdg-mime query default x-scheme-handler/dsh`；`gio mime x-scheme-handler/dsh`；点击 `dsh://open` 链接验证；必要时用 §5.2 的 `xdg-mime default` 兜底 |
| V8 | `role:'about'` 在 Linux 是否正确弹出 GTK About 面板、icon 是否显示 | Electron 文档只列出 role 可用，未写 Linux 细节 | 真机点菜单“关于”；看是否显示 520×... GTK 面板与图标 |
| V9 | `desktopName` + `StartupWMClass` 是否让任务栏正确归组 | Electron 44 的 app_id 取自 `package.json.desktopName`（`xprop WM_CLASS` 是最终事实） | `xprop WM_CLASS`（X11）或 `gdbus`/`hyprctl`（Wayland）对比 `.desktop` 的 `StartupWMClass` |
| V10 | Linux 上 `dsh://` 深链在“应用未启动”时是否传参并聚焦 | `open-url` 仅 macOS，Linux 依赖 `.desktop Exec %U` + 单实例 argv；`main.ts` 不解析 argv | 冷启动：`xdg-open 'dsh://open'` 看是否启动并聚焦；热启动：应用已运行再 `xdg-open`，确认单实例聚焦 |
| V11 | 托盘（若实现）在 GNOME/KDE 的显示与点击语义 | Electron Linux 用 StatusNotifierItem，退化 GtkStatusIcon（`tray.md:37-43`）；GNOME 需扩展 | 真机 GNOME 44+/KDE 5/6 各测：图标是否出现、左键是否唤回窗口、右键菜单是否可用 |
| V12 | Linux 上 Host 启动 + Office 转换 + 插件安装的整链路（smoke 已覆盖部分） | 上游没有 Desktop Linux 真实安装验证 | 装 deb 后：首次启动→登录/跳过→发消息→Office 导出 PDF→插件安装；观察 `~/.config/DeepSeek Harness/logs` 与 Host 日志 |
| V13 | 上游 npm 包是否包含 `dsh`/`desktop-host` 的 linux optional deps（node-addon-system-linux-arm64、node-pty prebuilds、@vscode/ripgrep 的 linux-arm64 包、sharp/koffi 的 linux-arm64 二进制） | 这些是 registry 上的 optionalDependencies，仓库内只有消费侧逻辑 | `pnpm --dir apps/desktop run prepare:dsh` 后检查 `resources/app.asar.unpacked/dsh/node_modules` 与 `desktop-runtime.json` 里的文件平台；`runtime-payload-smoke.mjs` 会实际加载 koffi/sharp/pty/grep（`tests/fixtures/runtime-payload-smoke.mjs:95-149`） |
| V14 | `app.asar` 内 `@vscode/ripgrep-*/bin/rg` 的 asarUnpack 模式是否覆盖 Linux 实际包名 | `electron-builder-config.mjs:77` 只匹配 `@vscode/ripgrep-*/bin/rg` 与 `**/*.so*`；若 dsh 用自建 ripgrep 包或不同路径，rg 会被打进 asar 不可执行 | 在 unpacked 应用里 `find resources -name rg`；运行一次 `grep`/`glob` 工具；失败则在 `asarUnpack` 增加对应 pattern |

---

## 10. 附：关键外部依赖行为依据（便于复核）

| 结论 | 依据 |
|---|---|
| deb 默认 depends 与 t64 无关 | electron-builder v26 `packages/app-builder-lib/src/targets/FpmTarget.ts:344-347`；`src/options/linuxOptions.ts:152` |
| deb 必须有 maintainer email 与 project homepage | 同上 `FpmTarget.ts:82-97` |
| deb 包名默认可能含空格 | `app-builder-lib/src/appInfo.ts:149-152` + `linuxPackager.ts:20-21` |
| deb 安装布局 `/opt/<sanitizedProductName>` + `/usr/bin` 软链 + chrome-sandbox/chmod/AppArmor | `FpmTarget.ts:235`；`templates/linux/after-install.tpl:3-56` |
| `.desktop` 会自动写入 `MimeType=x-scheme-handler/dsh;` 与 `Exec=... %U` | `src/targets/LinuxTargetHelper.ts:113-166` |
| `--dir` 输出目录命名 `linux-arm64-unpacked` | `src/platformPackager.ts:375`（`buildConfigurationKey + archSuffix + '-unpacked'`） |
| `electronDist` 可以是“已解包目录”（所以 prepare-runtime 的解压目录可复用） | `src/electron/ElectronFramework.ts:191-204` |
| fpm 有 linux-arm64 二进制 | electron-builder v26 `src/targets/tools.ts` 的 `getKey()`（`process.arch==='arm64' → fpm-...-linux-arm64v8.7z`） |
| electron-updater 在非 AppImage 的 Linux 上不可用 | `electron-updater/src/AppImageUpdater.ts:18-27`（`isUpdaterActive` 检查 `process.env.APPIMAGE`）；`AppUpdater.ts:514-517`（inactive 时 `checkForUpdates()` 返回 null） |
| `open-url` 仅 macOS；Linux 深链走 `.desktop Exec %U` + 单实例 argv | Electron `docs/api/app.md:129`、`app.md:1212-1216` |
| `powerMonitor 'shutdown'` 支持 Linux | Electron `docs/api/power-monitor.md:57` |
| `dialog.createDirectory` 仅 macOS | Electron `docs/api/dialog.md:44` |
| Linux 托盘使用 StatusNotifierItem / GtkStatusIcon | Electron `docs/api/tray.md:37-43` |
| Linux `setAboutPanelOptions` 支持 `iconPath`，`version` 字段 macOS-only | Electron `docs/api/app.md:1861-1871` |

---

本报告所有“现状”均以 `dsh-v0.2.1-rc.3` 源码行号为准；外部依赖行为已注明来源。标 **待验证** 的 14 项（§9）需要 Linux arm64 真机/构建机才能落定，其中 V1/V2/V3/V4 直接影响“构建链路能否跑通”，建议在动手改代码前先用最小脚本验证。

---

## 11. 与团队现有 overlay / 自定义 deb 方案的对照（缺口审查）

> 本节在报告完成后补充：工作区 `linux-deb/` 下已有并行队友产出的移植方案（`scripts/apply-linux-overlay.mjs` + `overlay/` + `packaging/build-deb.sh`）。其路线是 **`electron-builder --linux --arm64 --dir` 产出 `linux-arm64-unpacked`，再由 `packaging/build-deb.sh` 自行组装 deb**（不走 electron-builder 的 `deb` target）。对照本节可快速判断“还差什么”。

### 11.1 已被 overlay/EDITS 覆盖的本报告条目（等价实现）

| 报告条目 | 现有实现（`linux-deb/scripts/apply-linux-overlay.mjs`） | 一致性 |
|---|---|---|
| B1 | overlay 替换 `desktop-build-paths.mjs`（`SUPPORTED_TARGETS` 加 `linux-arm64`，`desktopTargetPlatform` 返回 `{linux,arm64}`），并补 `.d.mts` | ✅ 与 §2 B1 一致 |
| B2 | EDITS：`DesktopPackageTargetName`/`platform`/`builderPlatform`/`TARGETS` 加 linux、host 校验、`localLinuxPackageEnvironment` 分支 | ✅ 与 §2 B2 一致（差异：报告建议未签名 linux 也拒绝 signed，overlay 在 `desktop-package-environment` 里直接 `throw` linux 非 unsigned） |
| B3 | EDITS：`UPDATE_TARGETS` 加 `linux-arm64`，并补 `desktop-auto-update-environment.d.mts` | ✅ 一致 |
| B4 | EDITS：新增 `localLinuxPackageEnvironment`；`validateDesktopPackageEnvironment` 对 linux 提前返回 | ✅ 一致 |
| B5（policy/update 部分） | EDITS：`policy = (packagesMacOS && unsigned) \|\| resolvedPlatform === 'linux' ? undefined : ...`；`update = unsigned \|\| resolvedPlatform === 'linux' ? undefined : ...`；`linux` 段改 `target:['deb']` + `executableName` + `icon` + `maintainer/synopsis/description` | ✅ 关键部分一致（linux 包无 policy、无 app-update.yml） |
| B6 | EDITS：`prepare-runtime.ts` 的 Electron 可执行文件加 `platform === 'linux' ? 'electron'` | ✅ 一致 |
| B7 | EDITS：`prepare-dsh.ts` 的 `NODE` 路径加 linux | ✅ 一致 |
| B8 | overlay 替换 `prepare-cli.ts`（新增 `cli/dsh-linux` 模板、非 win32 一律 `chmod 0755`） | ✅ 一致（且比“改 OS 探测”更干净） |
| B9 | overlay 替换 `smoke-packaged-runtime.ts`（识别 `linux-arm64-unpacked` / `resources` / `deepseek-harness`） | ✅ 一致 |
| B11 | EDITS：`desktop-toolchain-preflight.ts` 平台联合类型加 `'linux'` | ✅ 一致 |
| R0（构建侧规避） | policy 在 linux 构建为 `undefined` → 包内 manifest 无 `dshMandatoryUpdatePolicy` → `main.ts:1296` 的 `policyConfig` 为 `undefined`，整块跳过 | ✅ **P0 启动崩溃已被构建策略规避**，无需改 `main.ts:1303` |

### 11.2 现有方案仍缺 / 需要决策的点（按优先级）

| # | 缺口 | 证据 | 影响 | 建议 |
|---|---|---|---|---|
| G1 | **运行期完全没有 `main.ts` 补丁**：关闭窗口=隐藏 + Linux 无托盘；更新菜单/自动检查仍按 mac/win 逻辑走 | overlay 无 `apps/desktop/src/main.ts`；`linux-deb/overlay/` 仅含 cli/scripts；`main.ts:992-1002,1079-1088,962,921-924` | 用户点关闭后应用“消失但仍在后台”，无托盘入口（只能再启动一次唤回）；“检查更新…”会弹失败对话框（deb 无更新源） | 发布前至少补 R2（Linux close→quit）；R1 更新菜单建议补（否则误导用户）。若要保持 hide 语义，则必须实现 Linux 托盘（tray.md:37-43 + AppIndicator 依赖） |
| G2 | **`/usr/bin/dsh` 不存在**：README 宣称 `dsh --help` 可用，但 deb 只装 `/usr/bin/deepseek-harness` 启动器 | `linux-deb/README.md:18` vs `packaging/build-deb.sh:626`（仅 `deepseek-harness.launcher`）、`packaging/postinst` 无 `dsh` 软链；`scripts/ci-smoke-test.sh:11,37` 也只测 launcher | README 与产物不一致；CLI 只能手动执行 `/opt/deepseek-harness/resources/runtime/cli/bin/dsh` | 二选一：(a) `postinst` 增加 `ln -sf /opt/deepseek-harness/resources/runtime/cli/bin/dsh /usr/bin/dsh` 并把 smoke 改为 `dsh --help`；(b) 修改 README 不承诺 `dsh`。注意上线前确认 overlay 的 `cli/dsh-linux` 已随 `prepare:runtime` 以 0755 装进 runtime |
| G3 | **`dsh://` 协议默认处理器未设置**：desktop 文件只声明 `MimeType`，postinst 只刷新数据库 | `packaging/deepseek-harness.desktop:15`；`packaging/postinst:19-24`；`main.ts:1235` 的 `setAsDefaultProtocolClient` 依赖 Electron 自行推导桌面文件名 | 安装后 `xdg-mime query default x-scheme-handler/dsh` 可能为空；深链是否可用**待验证**（§9 V7） | `postinst` 增加 `command -v xdg-mime >/dev/null && xdg-mime default deepseek-harness.desktop x-scheme-handler/dsh \|\| true`；真机用 `xdg-open 'dsh://open'` 验证冷/热启动 |
| G4 | **electron-builder `linux.icon` 指向 `../resources/icon.png`（1104×1104，文件名不含尺寸）**，且 `apps/desktop/package.json` 仍无 `homepage` | apply-linux-overlay.mjs 第 227-235 行；electron-builder v26 `linuxOptions.ts:53`（文件名必须含尺寸）、`FpmTarget.ts:82-86`（缺 homepage 报错） | 当前路线走 `--dir`，electon-builder 不消费 linux icon/deb 字段 → **暂不影响**；一旦直接 `pnpm run package:linux:arm64`（非 --dir）走 electron-builder deb target 就会失败或退回默认图标 | 二选一：(a) 明确只支持 `--dir` + build-deb.sh，把 config 的 linux 图标改为注释/尺寸目录；(b) 补 `resources/icons/NxN.png` 与 `package.json.homepage`（本报告 §5.3/B10） |
| G5 | **上游 desktop 单测断言未更新**：三处 spec 仍断言 linux 抛错 | `tests/package-target.spec.ts:29-32`、`tests/desktop-build-paths.spec.ts:58-68`、`tests/desktop-auto-update-environment.spec.ts:110-119` | 当前 CI（probe/build）只跑 `build:official` 与打包，不跑 desktop vitest → 暂不阻塞；若 workflow 未来加 `pnpm test` 会红 | 后续随补丁一起更新断言（把“抛错”改为“支持 linux”），并新增 `linux-arm64` 的负例（非 linux host 拒绝） |
| G6 | **sandbox 策略差异**：build-deb.sh 固定 `chrome-sandbox` 4755 + postinst 再 chmod；electron-builder 官方模板会按 userns 能力选 4755/0755 并在 24.04 装 AppArmor profile | `packaging/build-deb.sh:597-603`；`packaging/postinst:12-15`；对照 v26 `templates/linux/after-install.tpl:13-56` | 4755 setuid 是不支持 userns 时的官方回退，Ubuntu 24.04 上通常可用；但 CI 以 root 启动 launcher 会走 `--no-sandbox`（`packaging/deepseek-harness.launcher:28-33`），**CI 通过不能证明普通用户 sandbox 正常** | 真机以普通用户安装启动，检查 `chrome-sandbox` 属主/权限与是否出现 sandbox 报错；必要时补 AppArmor profile（§9 V6） |
| G7 | **账号身份仍为 web 客户端**（R3） | `packages/bundle/base/cordis.patch.yml:120-123`；overlay 未改该 bundle 配置 | Platform 账号相关请求 `x-client-platform: web`；嵌入式视图为 `desktop-mac`（`main.ts:443-444`），两者不一致 | 可接受降级；若要统一，按 R3 方案 2 在 `prepare-dsh` 后对 bundle 配置打运行期补丁，并先确认服务端语义 |
| G8 | **deb 依赖清单**：已比报告 §6 更全（含 `libatk*`/`libcairo2`/`libfontconfig1`/`libfreetype6`/`libglib*`/`libvulkan1` 等），并已使用 t64 `|` 备选 | `packaging/build-deb.sh:70-71` DEFAULT_DEPENDS/RECOMMENDS | 覆盖 Electron 44 需求；未启用托盘故不需要 appindicator | 无需改；真机按 §9 V5 实测可装即可。若启用 Linux 托盘，追加 `libayatana-appindicator3-1 \| libappindicator3-1` |
| G9 | **overlay 的 smoke 路径固定为 `linux-arm64-unpacked`**（`overlay/apps/desktop/scripts/smoke-packaged-runtime.ts:14-22`） | 当前只有 arm64 目标，够用 | 若未来加 `linux-x64`，需改为 `target.startsWith('linux-')` + arch 后缀判断 | 非当前范围，记账即可 |

### 11.3 结论

现有 overlay 方案**已经覆盖本报告识别的全部构建硬阻塞（B1–B9）**，与 §2 的 patch 建议等价（B8 用独立 `dsh-linux` 模板更稳），因此“能否产出 linux-arm64 unpacked 目录”这条路上只剩实测风险（§9 V1–V4、V13）。发布 deb 前建议按 G1（close/更新菜单）、G2（`/usr/bin/dsh`）、G3（协议默认处理）补齐，其余为文档/后续项。
