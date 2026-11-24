# 本机学生口述提取微调实验

本目录处理模拟对话、待确认候选和评测文件。工作台与本目录共用候选结构和来源校验。拟合、解码和离线评分独立于工作台。

## 输入和版本

在具备本地实验数据使用授权的工作区，训练和验证读取 `datasets/student_intake/v2/` 的封存文件，并检查摘要。v2 语料、训练适配器、逐例原答及失败记录保存在授权工作区。公开仓库提供代码和汇总结果。24 条旧题从原始开发 fixture 读入，只用于回归。评测时传给模型的内容只有 system 提示词及带角色、回合号的对话。参考答案、场景标签、家族 ID 和评分结果都保存在评测端。

实验适用于 Apple Silicon。实际运行使用 Python 3.12、MLX 0.32.1、mlx-lm 0.31.3、llguidance 1.9.1、tokenizers 0.23.2 和 transformers 5.17.0。通过只读的本机 Python 环境复用项目外已有库，其余小包在本次实验的独立 runtime 目录。项目依赖没有新增 MLX。推理、训练、判分及 Ollama 请求均在本机完成。

固定的 MLX 权重版本如下。这两份量化权重的模型卡标注 Apache 2.0。每次加载会校验权重、配置和分词器的 SHA256。

| 模型仓库 | 固定修订 |
| --- | --- |
| mlx-community/Qwen2.5-0.5B-Instruct-4bit | a5339a4131f135d0fdc6a5c8b5bbed2753bbe0f3 |
| mlx-community/Qwen2.5-7B-Instruct-4bit | c26a38f6a37d0a51b4e9a1eb3026530fa35d9fed |

## 复现 7B 试验

从项目根目录执行。在本机运行原实验时，两份基础模型分别保存在本次输出目录的 `base/` 和 `7b/base/`。其他机器可按上表公开修订取得 `config.json`、`tokenizer.json`、`tokenizer_config.json` 和 `model.safetensors`。训练参数在 `model.py` 与 `train.py`，实际运行还保存了配置快照。新实验使用新的输出目录。

```bash
run_dir=outputs/evals/student-intake-astra-v2
replay_dir=outputs/evals/student-intake-7b-replay
pilot_python=python3.12
export PYTHONPATH="$PWD/$run_dir/runtime:$PWD"
export DYLD_LIBRARY_PATH=/opt/homebrew/opt/mlx/lib
export HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false

"$pilot_python" -m server.evals.student_intake_pilot.train \
  --base "$run_dir/7b/base" --out "$replay_dir/fit"

"$pilot_python" -m server.evals.student_intake_pilot.predict \
  --base "$run_dir/7b/base" --split validation --out "$replay_dir/base-validation"

"$pilot_python" -m server.evals.student_intake_pilot.predict \
  --base "$run_dir/7b/base" --fit "$replay_dir/fit" \
  --split validation --out "$replay_dir/lora-validation"

.venv/bin/python -m server.evals.student_intake_pilot.assess \
  --input "$replay_dir/lora-validation" --split validation --out "$replay_dir/scored-validation"
```

`predict --prompt original` 复用原系统提示词。`--split regression` 运行旧 24 题。首次正式测试前已锁定配置、分词、语法、提示词、规则和判分器。完整实验编排随证据包保存。再次运行已开卷测试属于复现。本轮未根据测试结果选择检查点或改动规则。

## 计分和产物

`predictions.jsonl` 包含原始回答、输入回合、输出 token 数、停止原因和耗时。解码使用贪心、同一份 JSON Schema 和紧凑格式；可确定的语法 token 一起送入上下文。达到上限的回答计失败。每次只跑一遍。

`assess` 同时保留原始模型分与加入规则后的分数。规则检查出典、申请范围和拒答，检查单值数字是否有依据，并约束追问。其标量冲突处理覆盖部分绩点、团队人数、志愿时长和预算表达。复杂任职关系、多期绩点和代词指代仍需要语义判断。结构无效时转人工核对，可解析的原始风险仍记录。全部成功和失败保存在 `scored.jsonl`，未达到本轮严格条件的条目另存 `failures.jsonl`。

对照原报告时使用旧口径的字段 TP、FP、FN、F1 和整例通过。新口径另记逐字证据支持、国奖与拒答追问、跨字段经济引文、状态危害，以及新参考集中多出的追问。引文锚点可做机械核对，仍需要读原话才能判断蕴含关系。比较器按同一会话列出得分升降与风险变化。

## 公开演示

工作台的[学生口述微调实测](https://agent.stardust.website/student-intake)提供五段专为公开展示编写的虚构口述。用相同底座、提示词和解码器分别推理一次，保存原答与来源校验后的候选。可在持有检查点的实验环境中复现至新文件。

```bash
python -m server.evals.student_intake_pilot.public_demo \
  --base "$run_dir/7b/base" --checkpoint "$run_dir/7b/fit/epoch-3" \
  --out /tmp/student-intake-demo-replay.json
```

实时提取入口位于项目的「事实矩阵 → 学生口述档案」。平台默认模型为 DeepSeek V4 Flash，使用工作台现有计费。候选按本人原话和状态逐项确认，拒答、未知及未解决的冲突仅在核对时显示。已核对的计划保留计划状态。
