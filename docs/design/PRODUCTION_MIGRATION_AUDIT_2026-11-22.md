# 生产迁移历史审计（2026-11-22）

本表比对仓库 `supabase/migrations` 和生产库 `supabase_migrations.schema_migrations`。
这是历史记录审计，不是将当前结构与全部 SQL 逐项等价证明。生产库有历史迁移漂移，
**不要直接运行 `supabase db push`、`--include-all` 或仅凭名称执行 `migration repair`**。
每次结构变更都应先核对生产对象及依赖，在隔离库验证后，仅应用审查过的新迁移。

同名迁移中，19 条版本相同；另有以下 11 条版本不同。内容按 SQL 文本 SHA-256
比对；“文本不同”也可能只是注释或后续修订，不能据此推断数据库实际结构一致或不同。

| 名称 | 仓库版本 | 生产版本 | 文本比对 |
| --- | --- | --- | --- |
| stardust_agent_core | 20261009043834 | 20261009044208 | 除末尾换行外相同 |
| index_project_templates | 20261009044536 | 20261009044616 | 除末尾换行外相同 |
| workbench_paired_redemption | 20261012090000 | 20261012135501 | 文本不同，语义待审 |
| workbench_backend_capability | 20261012120000 | 20261012143106 | 文本不同，语义待审 |
| workbench_single_inflight | 20261012123000 | 20261012144753 | 文本不同，语义待审 |
| workbench_existing_balance_codes | 20261013010000 | 20261013013335 | 文本不同，语义待审 |
| material_pipeline | 20261022154243 | 20261023114801 | 除末尾换行外相同 |
| rag_project_search | 20261027051631 | 20261103122945 | 文本不同，语义待审 |
| material_pipeline_all_skills | 20261103114550 | 20261103123000 | 文本不同，语义待审 |
| workbench_model_interactions | 20261113090000 | 20261117115630 | 文本相同 |
| restrict_worker_to_trusted_material_jobs | 20261122065513 | 20261122072024 | 文本相同 |

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
