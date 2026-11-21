Stardust Agent 的首个桌面客户端发行版。安装包自带 Python 和 OCR 模型，直接打开可创建本机项目、上传并识别图片、编辑草稿和导出 Word。项目与原始材料保存在电脑上。

| 电脑 | 下载 |
| --- | --- |
| macOS 14 或更新版本，Apple Silicon | `mac-arm64.dmg` |
| macOS 14 或更新版本，Intel | `mac-x64.dmg` |
| Windows，Intel / AMD | `win-x64.exe` |
| Windows on ARM | `win-arm64.exe` |
| Linux，Intel / AMD | `linux-x64.deb` 或 `linux-x64.tar.gz` |
| Linux，ARM64 | `linux-arm64.deb` 或 `linux-arm64.tar.gz` |

macOS 打开 DMG 后将应用拖入「应用程序」。Windows 运行 EXE。Debian 或 Ubuntu 可安装 DEB，其他兼容的 Linux 桌面可解压后运行 `stardust-agent`。

macOS 应用采用临时签名，尚未通过 Apple 公证。Windows 安装包尚未做发布者签名。下载文件可用 `SHA256SUMS.txt` 核对。需要本机 AI 对话时，可按说明安装 Ollama 并下载模型。

[桌面版使用说明](https://github.com/cuic19053-hue/stardust-agent/blob/main/desktop/README.md) · [在线工作台](https://agent.stardust.website)
