# LangGraph 云端预审

本功能延续“检查这版草稿”，复用现有项目身份校验、平台模型与积分账本。首版支持大创创新训练；规则检查免费，AI 预审由用户主动勾选，预留 2 积分并按现有平台用量规则结算。检查不修改正文、确认事实或代写审批意见。

## 运行方式

```mermaid
flowchart LR
    U[已登录用户提交草稿指纹] --> Q[Supabase 持久队列与输入快照]
    Q --> N[单次签名调度通知]
    N --> V[Vercel 有界 worker 步骤]
    V --> G[LangGraph 当前节点]
    G --> C[数据库检查点与待写状态]
    C --> Q
    G --> R[版本化报告与账单]
    S[每分钟恢复调度] --> Q
```

API 部署在 Vercel `stardust-agent` / `sin1`，数据库使用现有 Supabase。每次 worker 请求最多运行一个图节点，并在退出前同步保存检查点；后续节点使用新的 serverless 请求。不是在 Vercel `/tmp` 存 SQLite，也不依赖浏览器轮询触发执行。

通知使用现有 `pg_net`，恢复扫描使用 `pg_cron` 每分钟执行一次。`pg_net` 自身队列是 unlogged，仅负责唤醒；业务运行、输入、检查点与租约都保存在正常持久表中，通知丢失后可由扫描补发。正常情况在提交和每个步骤结束后立即通知下一步，恢复延迟可达数分钟。

## 输入与权限

`review_private.snapshot` 在一条 SQL 的 MVCC 快照内读取项目、材料和片段，指纹由捕获的完整 JSON 计算。用户提交旧指纹会得到冲突；同一请求键和输入返回原任务。模型只读取指定快照中的正文及匹配的已确认资料。

所有业务表位于不对 Data API 暴露的 `review_private`，启用 RLS 并撤销客户端直接访问。公开 RPC 使用 security invoker；实际授权逻辑位于受限 private 函数中。

- 用户 RPC 校验 JWT 身份及项目归属，提交还需要独立后端能力；不能从浏览器选择 provider、模型、owner 或计费参数。
- worker RPC 校验独立能力和当前任务租约；检查点、模型结果与账单动作绑定同一个运行。领取使用 `FOR UPDATE SKIP LOCKED`。
- 通知只有单次 delivery ID、时间和 HMAC，不携带正文、用户 JWT、provider key 或长期能力。签名时效和数据库单次消费一起阻止重放。
- provider key 留在 Vercel，Vault 保存调度签名所需的 worker 能力。单独的 configure 能力只用于启停本预审调度器。

删除项目会级联删除预审输入、报告、模型缓存、检查点和通知。每个运行仅保留最近三个检查点以控制重复存储；报告继续保存原始输入指纹及依据版本。财务账本遵循原有保留规则。

## 一次模型调用与结算

AI 预审使用现有平台模型 `deepseek-v4-flash`，本版本不消费用户保存的 BYOK 配置或免费聊天次数。提交前检查 2 积分预留资格；实际调用前再由账本原子预留，防止余额并发使用。模型参数由服务器固定，只允许一次有界文本调用。

调用复用 `WorkbenchMeter` 与现有受控网络出口，记录真实 request ID 和价格版本。费用按现有平台规则计算，浏览器既不能提交费用，也不能代替服务端确认用量。没有权威用量时保留预留，由已有对账调度器继续核对；页面从同一账本展示 reserved / settled / refunded / needs_review。

已知 provider 正文在可选账单查询前持久化，因此进程随后退出仍可继续生成报告。完整响应也缓存到运行中，恢复直接复用。模型结果不确定时进入 needs_review，禁止自动重发和盲目退款。明确拒绝或空回复使用原有退款规则。账单超出预留时延续现有人工核对机制。

规则步骤和无模型调用的输入超限分支不会扣费。上下文预检、结构化输出、逐字引用核对仍不代表事实核验：模型意见标为待人工核对，最多六条主要意见。

## 上线与回滚

1. 验证迁移 `20261117135553_langgraph_cloud_review.sql`，再应用到目标项目；初始数据库开关为 false。
2. 将 `REVIEW_WORKER_SECRET` 与 `REVIEW_CONFIGURE_SECRET` 设为两个独立的 64 字符随机 Secret；设置 `CLOUD_REVIEW_ENABLED=true`。既有 provider 和账本凭据保持原有配置。
3. 构建 Vercel 生产候选，核验版本、依赖与签名拒绝路径，再切换生产域名。
4. 通过 `/api/admin/configure-review-scheduler` 启用数据库调度；请求携带 `x-review-ops-token`，body 为 `{"enabled":true}`。密钥取自服务端专用配置，不能写进 shell 参数、工单或代码。
5. 核验能力入口、Cron、合成项目的规则报告、真实 AI 请求及其账本状态，确认重复提交不重复执行。

能力入口只有在环境开关、密钥和数据库配置一致时才显示。配置不完整时保持关闭。回滚先用相同运维接口设置 `enabled=false`，再回滚 Vercel 到上一版本；迁移是加法变更，旧聊天和导出不依赖这些新表。暂停不能撤销已发送到 provider 的请求，仍须通过原账本核对已发生用量。

## 验证入口

`tests/test_cloud_review_api.py` 覆盖云端价格提示、402、请求范围、签名与专用运维能力；`tests/cloud_review_postgres_cases.py` 经既有 `test_material_postgres.py` 入口运行，验证真实 PostgreSQL 权限、快照、跨请求检查点、真实账本函数、未知结果隔离、缓存恢复及删除级联：

```sh
RUN_POSTGRES_POLICY_TESTS=1 python -m pytest tests/test_material_postgres.py -k test_review_ -q
python -m pytest tests/test_cloud_review_api.py tests/test_review_graph.py tests/test_review_api.py tests/test_review_transport.py -q
node --test tests/js/draft_review.test.js
```

数据库测试仅使用临时、无网络 Docker Postgres 和合成账户。线上冒烟测试应使用明确标注的合成项目，并单独记录真实调用费用；工程测试通过率不是模型效果准确率。人工补充材料的 interrupt 流程、长文分章模型检查及多 Agent 协作仍属于后续工作。
