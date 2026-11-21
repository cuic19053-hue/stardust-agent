# Stardust Agent 桌面客户端

桌面版启动一个本机工作台。项目、上传原件、文字识别结果和草稿保存在应用数据目录，窗口退出后本机工作台会停止运行。在应用菜单「工作台」中可以打开数据目录或查看启动日志。

## 安装与使用

安装包包含 Python 和 OCR 模型。打开客户端即可创建本机项目、上传图片、核对文字、编辑章节和导出 Word。

| 平台 | 选择下载文件 |
| --- | --- |
| macOS 14 或更新版本 | Apple Silicon 选 `mac-arm64.dmg`，Intel 选 `mac-x64.dmg` |
| 64 位 Windows | Intel / AMD 选 `win-x64.exe`，Windows on ARM 选 `win-arm64.exe` |
| 64 位 Linux | x86-64 选 `linux-x64`，AArch64 选 `linux-arm64`。Debian 和 Ubuntu 可用 `.deb`，也可解压 `.tar.gz` 后打开 `stardust-agent` |

在 [GitHub 发行页](https://github.com/cuic19053-hue/stardust-agent/releases/latest) 选择对应包。安装包旁附有 SHA-256 校验清单。

AI 对话需要先启动本机 [Ollama](https://ollama.com/download) 并下载模型，例如 `ollama pull qwen2.5:7b`。也可在「模型配置」选用兼容接口。工作台会在用户主动搜索论文、查询通知或选择远程模型时使用网络。

## 从源码打包

在目标操作系统和 CPU 架构上准备 Node.js 22.12 或更新版本、[uv](https://docs.astral.sh/uv/getting-started/installation/)，再运行以下命令。Windows ARM64 构建还需要 Visual Studio C++ ARM64 工具。

```sh
cd desktop
npm ci
npm run package
npm run smoke
```

安装包生成在 `desktop/dist/`。Linux CI 安装生成的 DEB 后，用 `xvfb-run -a npm run smoke -- --executable="$(command -v stardust-agent)"` 验收实际安装的应用和系统沙盒。Ubuntu 24.04 或更新版本优先使用 DEB 的系统配置。修改根目录 Python 依赖或桌面平台约束后，从仓库根目录重新生成桌面依赖锁。

```sh
uv pip compile pyproject.toml --extra full --constraint desktop/python-platforms.txt --universal --python-version 3.12.14 --generate-hashes --output-file desktop/python-requirements.txt
```

发布新版本见[仓库版本与 GitHub 发行](../docs/DEPLOYMENT_AND_ROLLBACK.md#仓库版本与-github-发行)。
