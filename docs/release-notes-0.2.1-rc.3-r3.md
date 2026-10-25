# DeepSeek Harness · Linux arm64 桌面版 r3（社区构建）

基于官方开源仓库 [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) 的
`dsh-v0.2.1-rc.3` 源码，在 GitHub Actions 原生 `ubuntu-24.04-arm` 上构建。**非官方发布**，不含任何密钥。

## r3 修复：连接状态反复跳（重新连接中 / 连接成功）

**现象**：在 Android 托管的 Linux 桌面容器里，界面底部的连接状态每两秒左右在“重新连接中”和
“连接成功”之间切换，页面跟着抖。

**原因**：客户端连接层把浏览器的 `offline` 事件当作断线理由，会主动中止当前连接并重连。
这种容器通常跑在 Wayland 上、没有 NetworkManager 和 D-Bus，浏览器的在线状态会误报抖动，
于是健康的本机连接被反复掐断（日志里表现为固定周期打印 `[connection] connection lost, retry #1`）。

**修复**：构建时在安装的运行时里，把客户端连接层的 `offline` 监听器改成空操作，只保留
`online` 事件与重连机制。桌面版连接的是本机 Host，系统是否联网不应该影响这条本地连接。
这一改动只作用于 Linux 构建的运行时包，不修改上游源码仓库。

## r2 改动（保留）

启动器默认给应用加上 Chromium 的 `--force-prefers-reduced-motion`，避免低帧率容器里弹窗/下拉
入场动画被渲染成两三帧的位置跳变。想恢复动画：`DSH_HARNESS_KEEP_ANIMATIONS=1 deepseek-harness`。

## 下载

| 文件 | 说明 |
| --- | --- |
| `deepseek-harness_0.2.1~rc.3-3_arm64.deb` | 安装包（arm64 / aarch64，Debian/Ubuntu） |
| `smoke-test-desktop.png` | CI 内 `apt install` 后真实启动的截图（Xvfb） |

> GitHub 会把资源名中的 `~` 规范为 `.`，下载到的文件名可能是
> `deepseek-harness_0.2.1.rc.3-3_arm64.deb`；`apt install` 时使用实际文件名即可。

## 安装 / 升级

```bash
sudo apt install ./deepseek-harness_0.2.1~rc.3-3_arm64.deb
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
