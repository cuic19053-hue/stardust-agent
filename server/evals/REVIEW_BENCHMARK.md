# 草稿预审对比评测

本试点回答一个工程问题：在同一批大创草稿上，规则、证据召回及输出校验，分别补足了单次模型预审的哪些能力。数据为 Agent 编写的合成样例，独立人工标注和外部用户评测尚未完成。

## 对比方式

| 路径 | 实际执行 | 模型请求 |
| --- | --- | --- |
| rules | 生产预审图的免费规则分支 | 0 次 |
| direct | 同一个模型直接读取草稿、目录、学校及全部已确认片段，使用相同 system prompt 和输出 schema | 最多 1 次 |
| graph | 实际 ReviewWorker / LangGraph：快照、关键词召回、规则、模型、引用定位校验、报告 | 最多 1 次 |

两条模型路径使用相同模型摘要、temperature=0、seed、上下文大小和输出上限。每个样例的输入项目相同，模型可读取的材料均由该项目快照提供。direct 不执行规则或输出过滤；graph 会选择证据并合并规则。它比较完整处理流程，不将模型效果提升归因于 LangGraph 框架本身。

样例交替交换路径执行顺序，降低冷启动顺序偏差；一次试点不能消除所有缓存和硬件负载差异。当前只测本机执行，不能据此推断云端成功率、吞吐或生产 P95。

## 六个样例

预算总额冲突、无依据已完成成果、正确区分计划和实测的对照样例、上传材料注入审批指令、空实施计划章节、外校历史通知误作本校当前资格。只有草稿和合成材料进入模型输入，targets、rubric 和复核表不会发给被测模型。

## 运行

需要 Python 3.11+、本机 Ollama、qwen2.5:7b，以及服务端依赖和 langgraph-checkpoint-sqlite。评测使用全新输出目录内的 SQLite，不读取默认项目数据，不连接生产数据库，也不使用平台积分。模型地址限制为本机 HTTP 回环，不跟随重定向或环境代理。

```bash
python -m pip install -r server/requirements.txt 'langgraph-checkpoint-sqlite==3.1.3'
ollama pull qwen2.5:7b
python server/evals/review_benchmark.py \
  --output outputs/evals/review-pilot --repeats 1
```

默认六个样例、三条路径，共 18 个运行、最多 12 次真实模型请求。可先用 --cases budget_mismatch,planned_results 限定试点。--arms rules 只跑免费规则分支；CLI 仍检查所选模型的安装信息，不发推理请求。

### 比较提示词修订

`--prompt-variant production` 使用当前生产提示词；`--prompt-variant calibrated` 显式选择离线候选修订。候选规则针对计划与实测、学校规则的依据、预算核算和重复意见，未修改生产模块。每份 manifest 记录实际提示词版本及 SHA256，切换提示词不能恢复到旧实验目录。

```bash
python server/evals/review_benchmark.py --arms graph --prompt-variant calibrated \
  --output outputs/evals/review-calibrated-dev
python server/evals/review_benchmark.py --arms graph \
  --dataset server/evals/review_holdout_cases.json --prompt-variant production \
  --output outputs/evals/review-additional-production
python server/evals/review_benchmark.py --arms graph \
  --dataset server/evals/review_holdout_cases.json --prompt-variant calibrated \
  --output outputs/evals/review-additional-calibrated
```

四个新增合成样例覆盖合理未来计划、真实加总错误、实测断言冲突和草稿编造学校规则。它们是固定候选后的新增回归，仍由 Agent 编写，不属于独立人工金标准或统计上充分的留出集。比较需要同时报告误报和漏检，不能把“少报问题”直接算作改善，也不能在看到新增样例结果后调参再将其作为未见测试。

`review_compare.py` 核对实际模型请求，只允许 system 提示词不同；同时检查资料、模型、执行环境、生产流程源码、所有计划样例及复核口径。误报下降但漏检上升、或总召回率相同却新漏掉原本能发现的问题，都会保留为取舍，不能标成改进。比较只作用于给定合成样例，不构成生产发布许可。

```bash
python server/evals/review_compare.py \
  --baseline server/evals/baselines/review_pilot_20261117.json \
  --candidate server/evals/baselines/review_calibration_20261118.json \
  --candidate-experiment development_calibrated \
  --output outputs/evals/development-comparison.json
```

[首轮候选结果与失败分析](../../docs/reports/REVIEW_CALIBRATION_2026-11-18.md)保留了全部记录；无需调用模型即可重新计算比较结果。

```bash
python server/evals/review_benchmark.py \
  --output outputs/evals/review-pilot --repeats 1 --resume
```

恢复时必须使用相同参数、资料、源码哈希、依赖版本及模型摘要。已完成失败不会被重试替换；有 attempt 记录但缺少最终结果的运行保留为 interrupted，不再次调用模型。正在执行时修改相关源码会中止后续运行。若需复测，使用新目录，保留原始失败。

## 指标与复核

自动统计：完整运行数、失败或不完整数、实际模型请求数、已报告 Token、未知用量次数、中位耗时、正文定位率、引文原文匹配率。分母为零时显示空值，不算成 100%。模型结构化输出合法、引文逐字匹配，都不等于问题判断正确或引文支持结论。

精确率、召回率、误报、漏检及引文语义支持率需要独立复核决定。先复制 review-template.json 为 reviews.json，并填写 reviewer_kind（human 或 agent）和 reviewer_id。每条问题必须保留实际 problem 原文、判断和理由，不能删除难判意见。

- matched：命中某个 targets 问题，target_id 必须存在；同一问题重复报告只算一次命中，重复项算冗余误报。
- spurious：错误断言、错误指控或重复问题，计入误报。
- out_of_scope：合理但不属于本试点目标问题的额外写作建议，单独计数，既不当命中也不当错误；不能用此标记隐藏目标范围内的误报。
- pending：尚未判定，所在路径的精确率和召回率保持待复核。

召回分母包括该路径所有已计划样例的目标问题。全部运行完成且所有已报告意见得到复核后才汇总语义指标；失败运行的目标问题保留为漏检。精确率 = TP/(TP+FP)，召回率 = TP/(TP+FN)。因此不会只挑成功返回的样例统计效果。

每条引用另填 supports / does_not_support / pending 及理由，核对具体结论和引用原文是否相关。引用没做语义复核时支持率保持空值。Agent 辅助复核必须标为 agent，不能改记为独立人工评测。

```bash
python server/evals/review_benchmark.py \
  --output outputs/evals/review-pilot \
  --report-only --reviews outputs/evals/review-pilot/reviews.json
```

--report-only 使用运行时保存的数据，不查询或调用模型；复核记录绑定 result_sha256，实际输出变化后旧复核会被拒绝。human_review_complete 只表示独立人工复核材料是否齐全，不是通过申报、模型质量达标或准许发布的判断。

## 产物

| 文件 | 用途 |
| --- | --- |
| manifest.json / dataset.json | 固定模型摘要、源码、依赖、参数、顺序与样例版本 |
| inputs/样例/snapshot.json | 实际输入快照及本次独立 SQLite |
| runs/运行/request.json / response.json | 真实模型消息、schema、生成参数和原始响应 |
| runs/运行/result.json | 原始与最终定位统计、意见、节点耗时、正文未改变检查 |
| report.json / report.md / results.csv | 汇总结果、失败记录和可比较指标 |
| review-template.json / reviews.json | 逐项判断、逐条引文复核及判断依据 |

输出目录默认不进入 Git。公开基线只能选用这些合成样例，并同时提供逐项意见和复核依据。供应商按次 API 费用为零，因为仅调用本机 Ollama；电力和硬件成本未测量，不记为零。缺失的 Token 不估算补齐。六个样例一次回放只适合工程诊断，不能外推成 35 赛道准确率。

离线回归：

```bash
python -m pytest tests/test_review_benchmark.py tests/test_review_graph.py -q
```
