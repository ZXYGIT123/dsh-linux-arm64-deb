# DeepSeek Harness · Linux arm64 桌面版 r2（社区构建）

基于官方开源仓库 [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) 的
`dsh-v0.2.1-rc.3` 源码，在 GitHub Actions 原生 `ubuntu-24.04-arm` 上构建。**非官方发布**，不含任何密钥。

## r2 相比 r1 的变化（只改了启动器）

在无 GPU 加速、帧率很低的环境（例如 Android 上的 Linux 桌面容器）里，界面的弹窗/下拉入场动画
会被渲染成两三帧的“位置跳变”，看起来就是屏幕在跳。r2 的启动器默认给应用加上 Chromium 的
`--force-prefers-reduced-motion`，让 UI 跳过这些入场动画，画面稳定。

- 想恢复动画：`DSH_HARNESS_KEEP_ANIMATIONS=1 deepseek-harness`
- 版本号变为 `0.2.1~rc.3-2`，Debian 会把它当作 r1（`0.2.1~rc.3`）的升级

## 下载

| 文件 | 说明 |
| --- | --- |
| `deepseek-harness_0.2.1~rc.3-2_arm64.deb` | 安装包（arm64 / aarch64，Debian/Ubuntu） |
| `smoke-test-desktop.png` | CI 内 `apt install` 后真实启动的截图（Xvfb） |

> GitHub 会把资源名中的 `~` 规范为 `.`，下载到的文件名可能是
> `deepseek-harness_0.2.1.rc.3-2_arm64.deb`；`apt install` 时使用实际文件名即可。

## 安装 / 升级

```bash
sudo apt install ./deepseek-harness_0.2.1~rc.3-2_arm64.deb      # 全新安装
sudo apt install ./deepseek-harness_0.2.1~rc.3-2_arm64.deb      # 已装 r1 时即升级
deepseek-harness      # 图形界面
dsh --help            # 内置 CLI
sudo apt purge deepseek-harness
```

安装到 `/opt/deepseek-harness`；`/usr/bin/deepseek-harness` 为启动器，`/usr/bin/dsh` 为 CLI 软链；
同时注册 `.desktop` 菜单项、hicolor 图标与 `dsh://` 协议处理器。

## 已知限制

- **未签名**：请核对构建日志中的 SHA-256。
- **不参与自动更新**：上游 updater 仅覆盖 macOS/Windows；本构建隐藏“检查更新”并跳过自动检查。
- **Office 使用内置 WASM 引擎**：与上游 Linux CLI 默认行为一致。
- **仅 arm64**；系统库按 Ubuntu 24.04/Debian 13 的 `t64` 包名声明。
- 无托盘：关闭主窗口即退出。
- 无 GPU 的虚拟机/容器里如遇 GPU 进程崩溃，可用 `deepseek-harness --disable-gpu` 启动。

## 构建来源

- 构建仓库：<https://github.com/ZXYGIT123/dsh-linux-arm64-deb>
- 上游许可：MIT；Electron/Chromium 及其它第三方组件许可见上游 `THIRD_PARTY_NOTICES.md`。
