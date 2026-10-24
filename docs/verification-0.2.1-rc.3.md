# 验证记录 · DeepSeek Harness Linux arm64 `0.2.1~rc.3`

本文件记录一次完整、可复现的构建与验证结果。所有链接指向公开的 GitHub Actions 运行。

## 构建

| 项 | 值 |
| --- | --- |
| 工作流运行 | [Build DeepSeek Harness Linux arm64 deb #40110486708](https://github.com/ZXYGIT123/dsh-linux-arm64-deb/actions/runs/40110486708)（结论 **success**，全部 19 步通过） |
| 上游源码 | `deepseek-ai/deepseek-harness` tag `dsh-v0.2.1-rc.3`（commit `1f81c047fc48fa190a683e469daa699857e9afe8`） |
| 构建机 | GitHub Actions `ubuntu-24.04-arm`（原生 aarch64，未交叉编译） |
| 产物 | `deepseek-harness_0.2.1~rc.3_arm64.deb`，267,935,678 字节 |
| SHA-256 | `6bd27b7925456568ba5cc207b243f59a9929733017e3bca70880653b1f7a858f` |
| 安装后体积 | 1,080,300 KiB（约 1.03 GiB，Electron + dsh 运行时 + Node/Python + WASM Office 引擎） |
| Release | <https://github.com/ZXYGIT123/dsh-linux-arm64-deb/releases/tag/dsh-v0.2.1-rc.3-linux-arm64>（GitHub 上传时会把资源名里的 `~` 规范为 `.`） |

CI 日志关键行：

```
[build-deb] payload staged: 8405 files, 1.1G
DEB_BUILD_OK package=deepseek-harness version=0.2.1~rc.3 arch=arm64 compression=zstd \
  bytes=267935678 sha256=6bd27b7925456568ba5cc207b243f59a9929733017e3bca70880653b1f7a858f installed_size_kb=1080300
STARTUP_OK
UNINSTALL_OK
```

## CI 内验证（arm64 runner，真实安装）

1. **静态校验** `python3 tools/verify_deb.py`：ar 容器、control 必填字段与版本语法、
   Depends 语法、md5sums 覆盖与摘要、主程序 / `chrome-sandbox` 为 ELF64 AArch64、
   `chrome-sandbox` root:root 4755、`/usr/bin/dsh` 软链、图标真实尺寸、desktop Exec、
   `postinst` 的 `xdg-mime` 协议注册。
2. **安装并启动** `scripts/ci-smoke-test.sh`：
   - `apt-get install ./deepseek-harness_0.2.1~rc.3_arm64.deb`（依赖自动解析）
   - `ldd` 全部二进制无 `not found`
   - Xvfb `:99` 下启动 `/usr/bin/deepseek-harness`，进程存活 25s 以上并创建
     “DeepSeek Harness” 窗口（`xwininfo` 可见 600x727 与 1280x820 两个窗口）
   - 截图 `smoke-test-desktop.png`（Release 附件）
   - `dsh --help` 打印 CLI 用法（`ELECTRON_RUN_AS_NODE=1`，无需 DISPLAY）
   - `xdg-mime query default x-scheme-handler/dsh` → `deepseek-harness.desktop`
   - `apt-get purge` 后 `/opt/deepseek-harness` 与 `/usr/bin/deepseek-harness` 均被清理

## 独立复核（构建机之外，Windows）

为避免只信 CI 自检，产物下载后在本地重新解包核验：

- ar 成员：`debian-binary` / `control.tar.zst` / `data.tar.zst`（zstd）
- `md5sums` 8411 条：逐条与 `data.tar` 成员比对，**0 条缺失**；抽样/全量摘要一致
  （Windows 解包时 13 个超长路径被系统截断，属解包器限制，tar 内成员与摘要均正确）
- `data.tar` 中 `usr/bin/dsh` 为符号链接 → `../../opt/deepseek-harness/resources/runtime/cli/bin/dsh`，
  目标成员存在；3 个符号链接按 Debian 惯例不进入 md5sums
- `chrome-sandbox`：`mode=0o4755, uid=0, gid=0`
- 主程序与 `chrome-sandbox` ELF 头 `e_machine=183`（AArch64）
- 图标 `256x256`、`512x512` 尺寸正确；desktop `Exec=/usr/bin/deepseek-harness %U`、
  `MimeType=x-scheme-handler/dsh;`
- 本地计算结果与 CI 输出 SHA-256 完全一致：`6bd27b79…a858f`

## 说明

- `app.log` 末尾的 DBus 告警与 `GPU process isn't usable` 来自 CI 的 Xvfb/无 GPU 环境
  （且发生在截图之后的进程组终止阶段）；真实带 GPU 的桌面不会出现该路径，
  无 GPU 虚拟机可用 `--disable-gpu` 启动。
- 本构建未签名；请以上面的 SHA-256 为准。
