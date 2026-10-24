# DeepSeek Harness · Linux arm64 桌面版（社区构建）

基于官方开源仓库 [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) 的
`dsh-v0.2.1-rc.3` 源码，在 GitHub Actions 原生 `ubuntu-24.04-arm` 上构建。**非官方发布**，不含任何密钥。

## 下载

| 文件 | 说明 |
| --- | --- |
| `deepseek-harness_0.2.1~rc.3_arm64.deb` | 安装包（arm64 / aarch64，Debian/Ubuntu） |
| `smoke-test-desktop.png` | CI 内 `apt install` 后真实启动的截图（Xvfb） |

SHA-256（deb）：

```
6bd27b7925456568ba5cc207b243f59a9929733017e3bca70880653b1f7a858f  deepseek-harness_0.2.1~rc.3_arm64.deb
```

## 安装

```bash
sudo apt install ./deepseek-harness_0.2.1~rc.3_arm64.deb
deepseek-harness      # 图形界面
dsh --help            # 内置 CLI
```

卸载：`sudo apt purge deepseek-harness`

安装到 `/opt/deepseek-harness`；`/usr/bin/deepseek-harness` 为启动器，`/usr/bin/dsh` 为 CLI 软链；
同时注册 `.desktop` 菜单项、hicolor 图标与 `dsh://` 协议处理器。

## 本次构建验证

全部在 `ubuntu-24.04-arm` 上端到端执行：

- 安装包结构校验（`tools/verify_deb.py`）：ar/control/md5sums（8411 条）/ELF64 AArch64/
  `chrome-sandbox` root:root 4755/desktop 文件/图标尺寸/`/usr/bin/dsh` 相对软链/`postinst` 协议注册
- `apt install` → Xvfb 启动主窗口 → 截图 → `apt purge` 清理校验
- `dsh --help` 可用；`xdg-mime query default x-scheme-handler/dsh` 返回 `deepseek-harness.desktop`
- 包内应用可执行文件与 `chrome-sandbox` 均为 arm64 ELF，依赖闭包无缺失（`ldd` 无 `not found`）

## 已知限制

- **未签名**：请核对上面的 SHA-256。
- **不参与自动更新**：上游 updater 仅覆盖 macOS/Windows；本构建隐藏“检查更新”并跳过自动检查。
- **Office 使用内置 WASM 引擎**：与上游 Linux CLI 默认行为一致（上游未默认安装 Linux 原生引擎）。
- **仅 arm64**；系统库按 Ubuntu 24.04/Debian 13 的 `t64` 包名声明。
- 无托盘：关闭主窗口即退出（Linux 桌面版的行为差异，见仓库说明）。
- 无 GPU 的虚拟机/容器里如遇 GPU 崩溃，可用 `deepseek-harness --disable-gpu` 启动。

## 构建来源

- 构建仓库固定上游 tag 与补丁锚点：<https://github.com/ZXYGIT123/dsh-linux-arm64-deb>
- 上游许可：MIT；Electron/Chromium 及其它第三方组件许可见包内 `/opt/deepseek-harness` 与上游 `THIRD_PARTY_NOTICES.md`。
