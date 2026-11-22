# 生产迁移历史审计（2026-11-22）

本表比对仓库 `supabase/migrations` 和生产库 `supabase_migrations.schema_migrations`。
这是历史记录审计，不是将当前结构与全部 SQL 逐项等价证明。生产库有历史迁移漂移，
**不要直接运行 `supabase db push`、`--include-all` 或仅凭名称执行 `migration repair`**。
每次结构变更都应先核对生产对象及依赖，在隔离库验证后，仅应用审查过的新迁移。

同名迁移中，20 条版本相同；另有以下 11 条版本不同。内容按 SQL 文本 SHA-256
比对；“文本不同”也可能只是注释或后续修订，不能据此推断数据库实际结构一致或不同。

| 名称 | 仓库版本 | 生产版本 | 文本比对 |
| --- | --- | --- | --- |
| stardust_agent_core | 20261009043834 | 20261009044208 | 除末尾换行外相同 |
| index_project_templates | 20261009044536 | 20261009044616 | 除末尾换行外相同 |
| workbench_paired_redemption | 20261012090000 | 20261012135501 | 忽略注释、空白和未引用标识符大小写后 token 相同 |
| workbench_backend_capability | 20261012120000 | 20261012143106 | 忽略注释、空白和未引用标识符大小写后 token 相同 |
| workbench_single_inflight | 20261012123000 | 20261012144753 | 忽略注释、空白和未引用标识符大小写后 token 相同 |
| workbench_existing_balance_codes | 20261013010000 | 20261013013335 | 忽略注释、空白和未引用标识符大小写后 token 相同 |
| material_pipeline | 20261022154243 | 20261023114801 | 除末尾换行外相同 |
| rag_project_search | 20261027051631 | 20261103122945 | 生产脚本增加重复执行保护，见下文 |
| material_pipeline_all_skills | 20261103114550 | 20261103123000 | 生产脚本重复声明函数权限，见下文 |
| workbench_model_interactions | 20261113090000 | 20261117115630 | 文本相同 |
| restrict_worker_to_trusted_material_jobs | 20261122065513 | 20261122072024 | 文本相同 |

上述 token 比较保留字符串和带引号的标识符；它只比较历史 SQL，不证明当前数据库结构
或执行顺序等价。`workbench_single_inflight` 的阻塞逻辑后来被仓库和生产共同存在的
`20261028081757_workbench_nonblocking_reconciliation` 替代：历史预留仍占用余额，但不会
仅因存在旧预留而拒绝新的请求。

`rag_project_search` 的生产历史脚本为表、索引和策略创建增加存在性判断，两个函数采用
`create or replace`；仓库版本按新库首次创建编写。不能仅因名字相同就重放任一脚本。
`material_pipeline_all_skills` 的生产历史脚本额外重申旧两参数函数的 revoke/grant；仓库
的前置迁移已设置权限，而 `create or replace` 保留原函数权限。生产后续已移除该旧签名；
现用三参数函数检查结果为 anon 不可执行、authenticated 可执行。这些检查不是新库完整
迁移链或全部 RLS 策略的验收。

仓库独有的四条名称：

- `20261015153000_workbench_free_chats_policy`：历史脚本会重新定义免费次数函数并授予调用权限；生产后续已实施更严格的试用策略，普通认证用户当前无权直接调用退款函数，不能倒序执行旧脚本。
- `20261027053127_rag_public_knowledge`：审计时生产不存在 `public.rag_public_sources`；是否启用公共知识库需要单独验证配置、授权数据和向量索引资源，不能把缺少迁移记录直接当作应立即执行的依据。
- `20261108010000_harden_material_job_authority` 与 `20261122062822_allow_hardened_material_backups`：生产采用原子组合迁移 `20261122063022_harden_material_authority_and_preserve_backups`，归档在 `ops/production_migrations/`，不能再重复应用两条历史脚本。

生产独有的其他名称包括临时批次导入及删除、五条备份权限与超时迁移，及
`20261122072247_resume_trusted_material_worker`；后者为运维恢复步骤，脚本留在
`ops/production_migrations/`。这些记录不能因为仓库中没有同名历史而删除。

修复历史漂移前应导出不含密钥和用户数据的结构快照，在隔离数据库逐项对照这些文本差异、
依赖和当前权限；确认来源后制定可回滚的历史映射和新环境初始化方案，再分别验证生产与新库。
不要覆盖历史账单、任务、私有材料或备份权限来使版本号看起来一致。

材料模型交互权限迁移于生产记录为 `20261122083113_restore_worker_interaction_capability`；仓库对应文件采用相同版本，SQL 文本校验和与生产记录一致。
