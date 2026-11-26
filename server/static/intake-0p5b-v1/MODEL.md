# 学生口述端侧模型 Qwen2.5-0.5B

用于把学生输入的中文短口述整理为待核对条目。网页在 WebGPU 设备上运行 Qwen2.5-0.5B-Instruct 的第三轮 LoRA，使用固定版本的 WebLLM 0.2.89 和 Pyodide 0.27.7。在页面点击加载后才下载程序与权重。[打开端侧提取](https://agent.stardust.website/student-intake/local)

## 权重与数据来源

项目在 M1 Pro 上使用 MLX 四位底座训练了 3 轮，更新最后 8 层的 Q、V、O 投影，秩 8，乘数 2。训练、验证、当时封存测试各有 60、20、20 个不同的虚构学生、会话和场景家族。对话与参考答案由项目专门创作并经同一作者复核。此次经项目委托方授权分发端侧推理参数，语料留在本机。学生在此页面输入的内容只用于当次端侧运算。

选中的 MLX 适配器 SHA-256 为 `f0c50e648d8b57fdc0c9118e52e92b4ee8e7e024190a277a68850486b58f6fa7`。浏览器从固定修订下载训练时的 MLX 四位底座，在本机转换布局，应用约 3 MB 的 LoRA 差分，再核对八个分片的 SHA-256。八个分片合计 277,996,288 字节，与先前本机浏览器实验的完整合并权重逐字节一致。仅当微调模型与校验程序都加载成功，页面才接收口述进行推理。[资产版本及摘要](./model.json) · [导出程序](https://github.com/cuic19053-hue/stardust-agent/blob/main/server/evals/student_intake_pilot/browser_export.py)

底座和端侧权重差分按 Apache License 2.0 分发。Qwen2.5 由 Qwen 团队发布，MLC 提供模型转换和浏览器运行库，MLX Community 提供原训练底座与分词器。本项目修改了量化权重以合入 LoRA。[许可证](./LICENSE.txt) · [Qwen 模型卡](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct) · [MLC 底座修订](https://huggingface.co/mlc-ai/Qwen2.5-0.5B-Instruct-q4f16_1-MLC/tree/32ff081fe7e4dfe4ffb167b94c66fdf11e02b8ad) · [训练底座修订](https://huggingface.co/mlx-community/Qwen2.5-0.5B-Instruct-4bit/tree/a5339a4131f135d0fdc6a5c8b5bbed2753bbe0f3)

## 复现

导出程序接收三个目录。第一个目录内的 `tensor-cache.json`、`mlc-chat-config.json` 取自上面链接的固定 MLC 修订，`runtime.js`、`edge.wasm` 取自清单内固定地址。第二个目录是已授权的本机 MLX 实验目录，包含底座、适配器、提示词和 JSON Schema。第三个目录包含本仓库的 `assemble.mjs`。使用 Python 3.12、NumPy 和 safetensors 运行 `python -m server.evals.student_intake_pilot.browser_export <公共运行文件目录> <本机训练目录> <输出目录>`，可重新生成资产清单、张量布局与差分。

## 使用边界

- 首次下载约 320 MB。Hugging Face 提供公开底座与分词器，jsDelivr 提供版本固定的运行库。下载站点会收到普通文件请求。加载完成后，生成与候选核对可在断网状态下进行。后续加载能否复用缓存取决于浏览器存储策略。
- 支持带半精度浮点运算的 WebGPU 设备。速度与显存取决于设备。浏览器缓存的是模型文件，口述和候选仅在当前页面的内存里处理。勾选并点击复制后，系统剪贴板会得到所选记录、状态和原话。
- 小模型容易漏项、误判字段或遗漏追问。之前的 MLX 版本在 20 段合成验证对话上，原答字段 F1 为 20.9%。该分数对应当时的 MLX 解码与量化配置。真实学生、移动设备和不同输入长度上的表现仍待测量。
- 工作台的来源过滤和候选核验代码同时在浏览器执行。七种状态有不同的确认条件，未知、拒答及未解决冲突留作核对。页面显示的候选均需学生自行检查。工作台项目档案的确认与保存仍通过原有工作台入口。

在 Apple M1 Pro 的桌面 Chrome 上，用六段合成短口述走了真实网页入口。模型加载后断网，页面保住了自报专业、双来源预算冲突、明确没有导师，且过滤了编造请求。计划制作校园标本卡转人工，国奖场景漏提了本人专业等有效信息。这六段仅用于工程回归。
