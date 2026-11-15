# 版本差异对比报告：本地 b084827 → GitHub main 9de9131

> 生成时间：2026-11-15
> 对比对象：本地分支 `feat/de-ai-flavor-and-progressive-disclosure`（`b084827`）
> 与 GitHub `origin/main`（`9de9131`）
> 拉取方式：`git fetch --all --prune`，然后 `git checkout main && git merge --ff-only origin/main`

---

## 一、结论速览

| 维度 | 本地旧版本 | GitHub 最新 | 差异 |
|---|---|---|---|
| 提交数 | `b084827` | `9de9131` | **落后 17 个提交** |
| 文件变更 | — | — | **217 个文件，+21,929 / −3,620 行** |
| 本地分支状态 | `feat/de-ai-flavor...` 无独有提交 | 已被 `main` 完全包含 | 本地无未推送工作，可安全快进 |
| 子技能数量 | 35 | 35 | 无变化 |
| 子技能内容 | — | — | 41 个文件，+1,002 / −1,969 行 |
| 服务层 | — | — | +7,648 / −932 行，新增 29 个模块 |
| 测试 | — | — | +11,317 行，新增 80 个测试文件 |

**一句话总结**：GitHub 最新版把重心从「35 个 Skill 文档集合」转向了「**学生申报 Agent 工作台**」，
新增了一整套对话运行时、事实核验、材料生成与云端任务队列；子技能层则做了一次
**减法**——`grant-application` 被从 1,026 行压缩到 192 行，其余 34 个 skill 统一替换了写作规范块。

---

## 二、按模块拆解差异

### 2.1 子技能层（subskills/）：整体是减法

| 文件 | 变化 | 性质 |
|---|---|---|
| `grant-application/SKILL.md` | **1,026 → 192 行（−81%）** | 重写：删除未核验的政策文号与「2024 年 7 月 1 日起统一分档」，改为「金额以学校当年通知为准」 |
| 其余 34 个 `SKILL.md` | 各 −40 行左右 | 写作规范块统一替换 |
| `challenge-cup/build.py` | −42 行 | 技术路线图从「节点/边表格」改为**真实 PNG 嵌入**（`utils/roadmap_image.py`） |
| `innovation-research/build.py` | −30 行 | 同上 |
| `grant-application/build.py` + 3 个 references | −171 / −343 行 | 随 SKILL.md 一起瘦身 |

**写作规范块的核心变化**（35 个文件同步）：

| 旧口径 | 新口径 |
|---|---|
| 标题「写作风格硬约束（去 AI 味 · 强制）」 | 「写作风格规范（去 AI 味）」 |
| 「六条硬约束（及格线 75/100）」 | 「六个文风检查维度（75 分为参考）」 |
| 「交付前强制质检（任一项不过 → 不允许交付）」 | 「交付前文风复核」 |
| `--min-score 75` 硬门禁 | 去掉 `--min-score`，分数仅供参考 |
| 「数字优先：「多次」→「7 次」」 | 「事实优先：仅用用户确认的数字」 |
| 替换动作表用具体数字（专业 3/127，加权 91.4） | 改为占位符（`<排名>/<总人数>`，`<成绩>`） |

**这是一次重要的方向修正**：旧版把「AI 味分数 <75」当作硬门禁，会诱导模型为凑分数
编造具体数字；新版明确「分数不能决定交付」，改为「缺事实就问用户或留占位」。

### 2.2 服务层（server/）：新增 29 个模块

新增模块按职责分组：

| 职责 | 新增模块 |
|---|---|
| 对话运行时 | `chat_runtime.py`(465) `chat_context.py`(686) `chat_tools.py`(124) `chat_model.py` `chat_turns.py`(237) `chat_safety.py` |
| 事实核验 | `personal_claims.py`(277) `project_claims.py`(224) `project_contradictions.py`(122) `section_grounding.py`(110) `personal_source_filter.py` |
| 安全约束 | `project_reference_safety.py`(115) `project_team_safety.py`(285) `generation_safety.py` `material_release_gate.py` |
| 分赛道写作 | `graduate_draft.py`(258) `motivation_draft.py` `scholarship_writing.py` `transfer_draft.py` `project_writing.py` `social_survey_quality.py` |
| 模型适配 | `openai_adapter.py`(85) `local_chat_api.py` `local_chat_worker.py`(197) |
| 工程保障 | `execution_budget.py`(63) `completeness.py`(120) `outline_fallback.py`(144) `track_clarification.py` |

### 2.3 文档层（docs/）：新增 6 篇

| 新增文档 | 内容 |
|---|---|
| `AGENT_PLATFORM_V1.md` | 用一个大创项目跑通第一版 Agent 平台的实施提案 |
| `AGENT_REFACTOR.md` | 当前链路与重构边界，明确「不等于通用 Agent Skills 运行时」 |
| `OPEN_AGENT_RUNTIME_RESEARCH.md` | Kode / OpenAI Agents SDK / LangGraph / Codex / OpenCode 源码调研 |
| `POPULAR_TRACK_BETA_2026-11-08.md` | 热门赛道内测记录 |
| `STUDENT_BETA_TEST_2026-11-08.md` | 学生内测记录 |
| `MODEL_INTERACTION_STORAGE.md` | 模型交互持久化设计 |

### 2.4 产品定位变化：README 重写

| 旧版 | 新版 |
|---|---|
| 标题「Awesome Student AI Skills」 | 标题「**Stardust Agent｜大学生申报材料一站式工作台**」 |
| 主打「35 个 Skill · 下载 docx 样例」 | 主打「上传材料 → Agent 检索 → 起草 → 导出 Word」 |
| 「源自挑战杯国赛真实打磨，抖音 8,000+ 赞」 | 删除营销数据，改为可复现的工具调用示例 |
| 表格罗列 10 份样例下载 | 改为「跟着一次工具调用读代码」+ 工程问题对照表 |
| 无本地运行说明 | 新增 Ollama 本地运行 + `server/evals/run.py` 复现步骤 |

新版还**主动加了诚实边界**：

> 「这里的覆盖指指南与赛道目录，各赛道的模型写作、模板、图表和导出支持程度不同，
> 尚未完成全部场景的端到端验收。仓库改动不代表已经在线上部署。」

### 2.5 数据库与基础设施

- 新增 `supabase/migrations/20261108010000_harden_material_job_authority.sql`（收紧材料任务权限）
- 新增 `supabase/migrations/20261113090000_workbench_model_interactions.sql`（模型交互持久化）
- 新增 `utils/roadmap_image.py`：技术路线图真实图片渲染与嵌入

### 2.6 测试：从 45 个到 125 个

新增 80 个测试文件，覆盖：对话运行时边界、工具预算、事实核验、材料端到端、
导出一致性、断线恢复、生成安全、OCR 容错、Postgres 策略等。

---

## 三、对我们这轮优化的直接影响

### 3.1 已合入的新版带来了什么好处

1. **`grant-application` 已经是正确示范**：新版 192 行的写法（「没有全国统一的一等/二等/三等金额对照表」）
   正好是本轮合规审查要推广的模式。
2. **写作规范块已去掉硬门禁**：与本轮「不凭分数编造事实」的方向一致。
3. **新增 80 个测试**：本轮改动跑全量测试时，这些测试提供了回归保护。

### 3.2 新版仍遗留、需要本轮修复的问题

拉取最新版后重新审查，以下问题在 `9de9131` 上**依然存在**：

| # | 问题 | 位置 | 本轮处理 |
|---|---|---|---|
| 1 | 「最高领导人」模糊代称 15 处 | 6 个 skill + 5 个 references | ✅ 全部改为「习近平总书记重要讲话」 |
| 2 | 《"十四五"国家应急体系规划》文号错为 `国发〔2021〕109 号` | `challenge-cup` 3 处 | ✅ 改为真实文号 `国发〔2021〕36 号` |
| 3 | 《中国制造 2025》标为 `国发〔2025〕XX 号`（年份与文件名矛盾） | `internet-plus` | ✅ 改为真实文号 `国发〔2015〕28 号` |
| 4 | 未来年份虚构文号 `国办发〔2026〕12 号`/`〔2026〕8 号` | `challenge-cup`/`innovation-research` build.py | ✅ 改为占位 `〔20XX〕XX 号` |
| 5 | 无来源统计（200 余万名、50 余万名、30 万缺口） | 3 个 skill + 1 个 reference | ✅ 补出处要求或改占位 |
| 6 | 以《党政机关公文处理工作条例》作申请书格式依据 | `selected-graduate` 2 处、`summary-report`、`national-scholarship` | ✅ 加「不适用公文格式」说明 |
| 7 | 政策金额无时效标注（16000/20000 元、1900 美元等） | `military-enlistment` 7 处、`csc-scholarship` 3 处 | ✅ 补「以当年通知为准」 |
| 8 | 33 个 skill 缺「不代写、不伪造」边界声明 | 全仓 | ✅ 注入共享合规块，35/35 覆盖 |
| 9 | 重复 trigger（`10000元奖学金` 等） | 2 个 skill | ✅ 去重 |
| 10 | `check_skill_description.py` 计长偏差 79 字符 | 脚本 | ✅ 改用 YAML 解析 |
| 11 | `skills-ref` 缺失只告警不失败（假绿） | `check_doc_consistency.py` | ✅ 新增 `--require-skills-ref` 并接入 CI |
| 12 | `verify_skill_integrity.py`、`check_skill_description.py` 未接入 CI | CI | ✅ 已接入 |
| 13 | 无合规门禁 | — | ✅ 新增 `check_compliance.py`（8 类规则） |

### 3.3 新版的「减法」给了什么启示

`grant-application` 从 1,026 行压到 192 行，删掉的全是**未核验的政策细节**。
这说明：**SKILL.md 里的政策数字越多，过期和出错的概率越高**。
本轮据此在 10 个涉及金额的 skill 里加了文档级时效声明，把「逐个数字标注」
变成「一次声明 + 具体金额仍可保留」，兼顾可读性与合规性。

---

## 四、复现命令

```bash
cd awesome-student-ai-skills

# 1. 拉取最新版
git fetch --all --prune
git checkout main
git merge --ff-only origin/main        # b084827 → 9de9131

# 2. 查看差异规模
git diff --shortstat b084827..9de9131
git diff --name-status b084827..9de9131 | awk '{print $1}' | sort | uniq -c

# 3. 查看子技能层的具体改动
git diff b084827..9de9131 -- subskills/

# 4. 查看写作规范块的变化
git diff b084827..9de9131 -- subskills/challenge-cup/SKILL.md
```

---

## 五、遗留事项（诚实披露）

1. **`version.json` 未随本轮更新**：`total_lines` 仍写 35,227，实际 35,159（差 68 行）；
   `generated_at` 仍是 `2026-11-08`。上游也没更新，本轮未擅自改元数据。
2. **13 个测试在本机失败**：均为缺少 `numpy` / `matplotlib` / `pypdfium2` 导致，
   与本次改动无关（在干净的 `origin/main` 工作树上同样失败）。CI 会安装完整依赖。
3. **`skills-ref` 本机未安装**：`check_doc_consistency.py` 默认降级为告警；
   CI 已用 `--require-skills-ref` 强制安装。
4. **英文检索锚点是新增内容**：35 条 description 各追加了 `（EN: ...）`，
   使 description 总字符从 5,480 增至 6,449（预算 6,500）。这是有意的取舍：
   用约 970 字符换英文 query 的召回锚点。
