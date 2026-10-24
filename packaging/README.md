# DeepSeek Harness Linux `.deb` 打包与校验工具链

本目录把 electron-builder `--linux dir` 产出的**解包应用目录**组装成可 `apt install` 的
Debian 包，并提供纯 Python 的结构校验器。整条链路不联网、不依赖 npm/pip 第三方库，
运行时只需要 `dpkg-deb` + 常规 coreutils（图标缩放优先用 `python3`，没有时回退 ImageMagick）。

- 输入：`linux-arm64-unpacked/`（含 `deepseek-harness`、`chrome-sandbox`、
  `resources/app.asar`、`resources/runtime/`、`resources/runtime/cli/bin/dsh` 等）
- 输出：`deepseek-harness_<version>_<arch>.deb`
- 目标：Ubuntu 24.04 arm64（GitHub Actions `ubuntu-24.04-arm`），同时兼容 Debian/Ubuntu
  旧版的包名（见「运行时依赖」）

## 目录

| 文件 | 作用 |
| --- | --- |
| `packaging/build-deb.sh` | 参数化组装脚本：暂存安装树、生成 control/md5sums、调用 `dpkg-deb` |
| `packaging/deepseek-harness.launcher` | 安装为 `/usr/bin/deepseek-harness` 的 GUI 启动器模板 |
| `packaging/deepseek-harness.desktop` | 桌面项模板（含 `dsh://` 的 `MimeType`） |
| `packaging/postinst` / `prerm` / `postrm` | dpkg 维护者脚本模板（POSIX sh） |
| `packaging/copyright` / `changelog.Debian` | `/usr/share/doc/deepseek-harness/` 文档模板 |
| `packaging/icons/deepseek-harness.png` | 源图标（1024×1024 RGBA，构建时缩放到 hicolor 256/512） |
| `tools/verify_deb.py` | 跨平台 .deb 结构/metadata/ELF 校验器（纯标准库） |
| `tools/png_resize.py` | 纯标准库 PNG 缩放（无 ImageMagick 时生成 hicolor 图标） |
| `tools/selftest_verify_deb.py` | 构造合法/非法合成 .deb，验证校验器断言确实有效 |

## 快速开始

在 `linux-deb/` 仓库根目录（也就是 CI checkout 的位置）：

```bash
bash packaging/build-deb.sh \
  --app-dir  upstream/apps/desktop/.desktop-build/targets/linux-arm64/unsigned-artifacts/linux-arm64-unpacked \
  --version  0.2.1~rc.3 \
  --arch     arm64 \
  --output   dist \
  --icon     packaging/icons/deepseek-harness.png \
  --verify
```

成功时最后一行是机器可读的：

```
DEB_BUILD_OK package=deepseek-harness version=0.2.1~rc.3 arch=arm64 compression=zstd bytes=... sha256=... installed_size_kb=... path=dist/deepseek-harness_0.2.1~rc.3_arm64.deb
```

然后校验：

```bash
python3 tools/verify_deb.py dist/deepseek-harness_0.2.1~rc.3_arm64.deb
# 或显式指定期望值（CI 推荐）
python3 tools/verify_deb.py --arch arm64 --package-name deepseek-harness \
  --exe-name deepseek-harness --version 0.2.1~rc.3 dist/*.deb
```

## 安装布局

| 路径 | 来源 | 说明 |
| --- | --- | --- |
| `/opt/deepseek-harness/` | 整个 `--app-dir`（`cp -a`） | 应用、asar、运行时、原生模块 |
| `/opt/deepseek-harness/chrome-sandbox` | 应用目录内文件 | deb 内 root:root **4755**（`postinst` 安装后再兜底 chmod） |
| `/usr/bin/deepseek-harness` | `deepseek-harness.launcher` | GUI 启动器：切到 `/opt/...` 工作目录后 `exec` 应用；root 身份自动补 `--no-sandbox` |
| `/usr/bin/dsh` | **软链** → `/opt/deepseek-harness/resources/runtime/cli/bin/dsh` | 包内 CLI 命令（`dsh --help`）；软链是 dpkg 管理的文件，卸载自动移除 |
| `/usr/share/applications/deepseek-harness.desktop` | `.desktop` 模板 | `Exec=/usr/bin/deepseek-harness %U`、`MimeType=x-scheme-handler/dsh;` |
| `/usr/share/icons/hicolor/{256x256,512x512}/apps/deepseek-harness.png` | 源图标缩放 | 尺寸由 `png_resize.py` 精确生成，校验器会检查真实 PNG 尺寸 |
| `/usr/share/doc/deepseek-harness/{copyright,changelog.Debian.gz}` | 模板 | changelog 用 `gzip -9 -n` 生成（可复现） |

`postinst` 还会（全部「命令存在才执行、失败不阻断安装」）：

1. `chown root:root` + `chmod 4755 chrome-sandbox`；
2. `update-desktop-database` / `update-mime-database` / `gtk-update-icon-cache`；
3. `xdg-mime default deepseek-harness.desktop x-scheme-handler/dsh`，把 `dsh://` 注册为
   默认处理器（只改默认项，不覆盖恢复策略；`postrm` 只刷新 MIME 数据库，不会强行改回别的应用）。

`postrm purge` 会额外 `rm -rf /opt/deepseek-harness`（清除运行时产生的文件），但**不碰**
`$HOME` 下的用户配置/缓存。`prerm` 当前无操作（应用不是服务，launcher 直接 exec）。

## 参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--app-dir DIR` | **必填** | electron-builder `--linux dir` 输出目录 |
| `--version VER` | **必填** | 版本；自动去掉前导 `v`、把 `-` 换成 `~`（Debian 版本排序更合理） |
| `--arch ARCH` | `arm64` | `arm64` 或 `amd64` |
| `--output PATH` | `.` | 目录（生成规范文件名）或显式 `.deb` 文件路径 |
| `--package-name NAME` | `deepseek-harness` | 同时决定 `/opt/<name>`、`/usr/bin/<name>`、doc/desktop/图标名 |
| `--exe-name NAME` | `deepseek-harness` | `--app-dir` 内的主可执行文件名 |
| `--maintainer TEXT` | `DeepSeek Harness Linux community build <dsh-linux@users.noreply.github.com>` | control `Maintainer` |
| `--homepage URL` | 上游仓库 | control `Homepage` |
| `--section NAME` | `devel` | control `Section` |
| `--priority NAME` | `optional` | control `Priority` |
| `--depends LIST` | 内置 Electron 44 依赖集 | 覆盖 control `Depends`（逗号分隔，可用 `a \| b` 备选） |
| `--recommends LIST` | `fonts-noto-color-emoji, fonts-liberation, libvulkan1` | 覆盖 `Recommends` |
| `--compression TYPE` | `auto` | `auto` = 先 zstd，失败回退 xz；也可 `zstd`/`xz`/`gzip`/`none` |
| `--icon PNG` | `--app-dir/resources/icon.png` | 源图标，构建时缩放到 256/512 |
| `--icon-256 PNG` / `--icon-512 PNG` | – | 分别指定预置图标（没有 `--icon` 时作为源） |
| `--epoch UNIX_TIME` | `$SOURCE_DATE_EPOCH` 或 `1700000000` | 固定 mtime / changelog 日期 / `SOURCE_DATE_EPOCH` |
| `--verify` | 关 | 构建后立即调用 `tools/verify_deb.py` |
| `--keep` | 关 | 保留暂存目录便于排查 |
| `--allow-missing-runtime` | 关 | 允许 `resources/runtime` 缺失（仅调试用） |
| `--allow-missing-sandbox` | 关 | 允许 `chrome-sandbox` 缺失（仅调试用） |
| `-h, --help` | – | 用法 |

环境变量：`PYTHON`（指定 Python 解释器）、`SOURCE_DATE_EPOCH`（`--epoch` 默认值）、
`LIBZSTD`（仅 `verify_deb.py` 缺少 zstd 后端时用）。

## 运行时依赖（control `Depends`）

按 Ubuntu 24.04（noble）arm64 的 Electron 44 需求生成；24.04 的 t64 改名库用
`新名 | 旧名` 的备选写法，兼顾 Debian/Ubuntu 旧版：

```
libasound2t64 | libasound2, libatk-bridge2.0-0t64 | libatk-bridge2.0-0,
libatk1.0-0t64 | libatk1.0-0, libatspi2.0-0t64 | libatspi2.0-0,
libcairo2, libcups2t64 | libcups2, libdbus-1-3, libdrm2, libexpat1,
libfontconfig1, libfreetype6, libgbm1, libglib2.0-0t64 | libglib2.0-0,
libgtk-3-0t64 | libgtk-3-0, libnspr4, libnss3, libpango-1.0-0,
libsecret-1-0, libuuid1, libx11-6, libx11-xcb1, libxcb1, libxcb-dri3-0,
libxcomposite1, libxdamage1, libxext6, libxfixes3, libxkbcommon0,
libxrandr2, libxshmfence1, libxss1, libxtst6, libnotify4, xdg-utils
```

`Recommends`: `fonts-noto-color-emoji, fonts-liberation, libvulkan1`。
未启用 Linux 托盘，因此不需要 `libayatana-appindicator3-1`；若后续实现托盘再追加。

## 构建期依赖

- Linux：`dpkg-deb`（dpkg ≥ 1.21 支持 zstd；更老版本会自动回退 xz）、`gzip`、
  `tar`、coreutils（`find/cp/chmod/touch/du/md5sum/sed/sort/xargs/cut/tr/date/awk/mktemp/ln`）
- 图标缩放：`python3`（推荐，纯标准库 `tools/png_resize.py`）或 ImageMagick（`magick`/`convert`）
- 不需要 root、不需要 `fakeroot`：通过 `dpkg-deb --root-owner-group` 生成 root:root 归档

## 可复现性

- 所有暂存文件/目录统一 `touch -h -d @<epoch>`，并导出 `SOURCE_DATE_EPOCH`；
- `md5sums` 按字节序排序，路径规范化为 `usr/...`；
- `changelog.Debian.gz` 用 `gzip -9 -n`（不写时间戳/文件名）；
- `dpkg-deb --root-owner-group --compression=zstd`（zstd 不可用回退 xz）；
- 同一 dpkg 版本、同一输入、同一 epoch 下多次构建产物字节一致（本地已用伪 dpkg 流程验证；
  不同 dpkg 版本的 tar 成员顺序/压缩实现可能不同）。

## 在 CI 中使用

对应 workflow 已在本仓库 `.github/workflows/build-linux-arm64-deb.yml`：

1. `bash packaging/build-deb.sh --app-dir "$DSH_APP_DIR" --version "$DSH_PKG_VERSION" --arch arm64 --output dist --icon packaging/icons/deepseek-harness.png`
2. `python3 tools/verify_deb.py dist/*.deb`（静态兜底：control、md5sums、desktop、图标、
   `/usr/bin/dsh` 软链、`postinst` 的 `xdg-mime default`、ELF64 AArch64、chrome-sandbox 4755）
3. `scripts/ci-smoke-test.sh dist/*.deb`（apt 安装 → 启动 → 卸载清理；建议同时断言
   `test -L /usr/bin/dsh && dsh --help`）

关于 `dsh --help`：CLI 启动器在 `resources/runtime/cli/bin/dsh` 中设置
`ELECTRON_RUN_AS_NODE=1` 后执行 Electron 二进制，**不需要 DISPLAY/Xvfb**（Electron 以纯 Node
模式运行 cli.js），普通 CI 无桌面环境也能打印帮助。若未来 CLI 增加 GUI 相关初始化，再改为
`xvfb-run -a dsh --help`。

## 本地验证

### Windows（无 dpkg-deb）

```powershell
python linux-deb/tools/verify_deb.py 某个.deb            # 校验现成 .deb（支持 zstd/xz/gzip）
python linux-deb/tools/selftest_verify_deb.py            # 30 个合法/非法合成样本断言
python linux-deb/tools/png_resize.py --info packaging/icons/deepseek-harness.png
```

`verify_deb.py` 的 zstd 解压后端按顺序尝试：Python ≥ 3.14 的 `compression.zstd` →
`ctypes` 加载系统 `libzstd`（Windows 会自动找 `.dsh`/MSYS2 的 DLL）→ `zstd` CLI。
三者都不可用时以退出码 2 明确报错（而不是静默通过）。

### Linux（真实构建）

```bash
bash -n packaging/build-deb.sh packaging/postinst packaging/prerm packaging/postrm
bash packaging/build-deb.sh --app-dir /path/to/linux-arm64-unpacked --version 0.2.1~rc.3 --output dist --verify
dpkg-deb --info dist/*.deb
dpkg-deb --contents dist/*.deb | grep -E 'usr/bin/(dsh|deepseek-harness)|chrome-sandbox'
dpkg-deb --extract dist/*.deb /tmp/extract && ls -l /tmp/extract/usr/bin/dsh
apt-get install -s ./dist/*.deb      # 依赖可满足性 dry-run
sudo apt-get install ./dist/*.deb
dsh --help                            # CLI（无需 DISPLAY）
deepseek-harness                      # GUI
xdg-mime query default x-scheme-handler/dsh
sudo apt-get purge deepseek-harness
test ! -e /opt/deepseek-harness && test ! -e /usr/bin/dsh
```

## 已知限制

- **Windows 不能真正产出 deb**：没有 `dpkg-deb`，且 NTFS/MSYS 上 `ln -s` 会退化为复制文件
  （构建脚本会打印 WARN，`/usr/bin/dsh` 必须在 Linux 上构建才是软链）。Windows 仅用于
  `verify_deb.py` 校验现成产物。
- **依赖名以 Ubuntu 24.04 为准**：t64 备选写法可覆盖 jammy/bookworm，但只在 24.04 arm64 上验证。
- **verify_deb.py 的 zstd 后端**需要 Python ≥ 3.14 或系统 libzstd/zstd CLI；CI（python3.12 +
  libzstd1）走 ctypes 后端。纯 Python < 3.14 且无 libzstd 的机器上请用 `--compression xz` 构建后再验证。
- **未安装 AppArmor profile**：electron-builder 官方 deb 模板会在 24.04 上尝试加载 profile；
  本方案没有生成。若目标机器禁止非特权 user namespace，Chromium 的 setuid sandbox 仍应可用；
  如遇 sandbox 启动失败，可临时 `deepseek-harness --no-sandbox`（launcher 在 root 下会自动加）。
- **`dsh://` 默认处理器**只有安装时通过 `xdg-mime` 设置；在无 `xdg-utils` 的最小系统上不会强行设置，
  可用 `xdg-mime default deepseek-harness.desktop x-scheme-handler/dsh` 手动补。
- **未启用 Linux 托盘 / 自动更新**（deb 没有 electron-updater feed）；这两项属于运行期功能，不在打包范围。
- `dpkg-deb` 的 tar 成员排序/压缩实现随版本略有差异，跨 dpkg 版本不保证字节级一致。
