# 省级水准网成果平台

面向十万级往返高差导入的水准测量成果系统。平台把“当天发现问题子网”和“最终成果由同一观测快照整体求解”同时作为硬约束：

- React + Cytoscape.js：测点拓扑、连通分量、任务阶段、改正数/残差追踪。
- FastAPI：任务、快照、乐观锁、发布核对和审计 API。
- SciPy 稀疏矩阵：`A^T W A` 加权最小二乘，使用 `scipy.sparse.linalg.spsolve`。
- Celery/Redis：分区质检、连通分量预检、按分量并行求解；分量结果只用于独立诊断和汇总，不拼接伪造跨子网高程。
- PostgreSQL/PostGIS：原始观测、基准、权重规则、不可变快照、任务阶段、残差、审计和发布成果。

## 关键不变量

1. **原始高差永不被平差值覆盖**：`observations.observed_delta_m` 是草稿原始值；快照复制原始值；计算结果在 `observation_results` 中另存 `adjusted_delta_m`、`correction_m`、`residual_v`。
2. **求解绑定不可变快照**：任务的 `generation_key = project:{id}:snapshot:{id}`，payload、输入 SHA-256、权重规则和算法参数固定。
3. **重复提交只产生一个任务代次**：相同项目+快照返回已有 Job，不创建分叉任务。
4. **旧任务只能审计**：测量员修订观测/基准/权重后产生新草稿/快照；运行中的旧任务可完成，但发布检查会标记 `AUDITED_ONLY`，无法覆盖新草稿。
5. **禁止随意正则化**：没有 ridge、人为固定高程或先验“补秩”。病态/秩亏显式进入 QR 诊断；无基准连通分量返回 `blocked_rank_deficient`，多基准矛盾返回 `blocked_datum_contradiction`。
6. **中断恢复按阶段确认**：`import_qc -> component_precheck -> solve -> publish_checks`，只有 `confirmed` 阶段可跳过；失败/运行中阶段重新执行，不回滚已确认产物。

## 快速启动

```bash
docker compose up --build
# API: http://localhost:8000/docs
# Web: http://localhost:5173
```

本地开发：

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
celery -A app.workers.celery_app.celery_app worker --loglevel=INFO --concurrency=4

cd ../frontend
npm install
npm run dev
```

## 算法说明

### 观测模型

对有向测段 `i: from -> to`：

```text
h_to - h_from = Δh_i + v_i
```

权重来自活动权重规则，例如：

```json
{"method":"millimeter_sqrt_km","c_km":1.0,"base_sigma_m":0.001}
```

基准作为带权观测行：

```text
h_p = H_p,  w = 1 / sigma_p^2
```

稀疏正规方程：

```text
A^T W A x = A^T W b
```

仅在下列情况进入 QR 诊断：

- 分量无基准，存在垂直 datum defect；
- 稀疏分解失败或非有限；
- 条件数超过 `LEVEL_ILLCONDITIONED_CONDITION_NUMBER`，默认 `1e12`；
- 基准残差超过 `3 sigma`，作为多基准矛盾阻塞发布。

QR 是诊断路径，不用随机最小范数伪解充当唯一高程。若分量超过 `LEVEL_DENSE_QR_MAX_ROWS`，系统返回显式 sparse rank diagnostic 并阻塞，而不是悄悄正则化。

### 闭合环

`fundamental_closures()` 使用确定性生成森林：每条非树边形成一个基本环，binary-lifting LCA 计算完整环长和理论闭合差。默认公差 `2 mm / sqrt(km)`，可在规则中扩展。发布核对同时记录平差前、平差后闭合差。

## 乐观锁 API

所有测量员修订必须携带当前 `lock_version`：

```bash
curl -X PATCH http://localhost:8000/api/observations/123 \
  -H 'Content-Type: application/json' \
  -d '{"lock_version":3,"distance_m":1250.5}'

curl -X PATCH http://localhost:8000/api/weight-rules/2 \
  -H 'Content-Type: application/json' \
  -d '{"lock_version":1,"rule":{"method":"distance_inverse_km","c_km":1.0}}'
```

版本冲突返回 HTTP 409，审计表 `audit_events` 保存前后值和版本递增链。

## 外业 CSV 修订：先预览，后确认

外业送来的测段修订表必须先逐行预览，再批量确认，原观测不会被静默覆盖。CSV 必需列（支持中英文表头）：

```csv
line_code,lock_version,observed_delta_m,distance_m
测段编号,原锁版本,高差,长度
```

- `line_code`：观测稳定 ID（不是会变化的数据库主键）
- `lock_version`：测量员拿到该行时的**原锁版本**
- `observed_delta_m` / `distance_m`：修订后的高差与长度

预览（只读，不改任何值）按行报告 `applicable` / `already_applied` / `version_conflict` / `missing_record` / `duplicate_row` / `invalid_row`：

```bash
curl -X POST http://localhost:8000/api/projects/1/observations/revisions/preview \
  -F 'file=@scripts/example_observation_revisions.csv'
```

确认支持两种明确策略，每行都返回行级回执（含 `new_lock_version` 与 `audit_event_id`）：

```bash
# 全成功（默认）：任一行版本冲突/缺失/重复/非法则整批不应用，返回 409
curl -X POST http://localhost:8000/api/projects/1/observations/revisions/confirm \
  -F 'strategy=all_or_nothing' -F 'file=@scripts/example_observation_revisions.csv'

# 逐行结果：合法行走乐观锁+审计，冲突/缺失/重复/非法行回报但不影响其他行
curl -X POST http://localhost:8000/api/projects/1/observations/revisions/confirm \
  -F 'strategy=per_row' -F 'file=@scripts/example_observation_revisions.csv'
```

语义保证：

- 所有应用都走既有 `apply_optimistic_update` 乐观锁/审计链（`action=csv_revision`），`audit_events` 约束 `lock_version_out = lock_version_in + 1`。
- **重复确认幂等**：修订值与当前草稿一致的行报 `already_applied`，不二次改值、不产生新审计事件。
- 同一 `line_code` 在文件内重复出现时整行标记 `duplicate_row`，永不隐式选一行应用。
- 修订只改 `observations` 草稿；旧 `snapshots` 负载与 SHA-256 不变，旧 Job 完成后仍只能 `AUDITED_ONLY`，发布时被 `stale generation` 拒绝。新草稿重新 `/jobs` 生成新快照版本（`v1 -> v2`），未变化的重复提交仍去重。

## 任务流程

```bash
# 生成当前草稿的不可变快照；重复提交同一快照返回 deduplicated=true
curl -X POST http://localhost:8000/api/projects/1/jobs

# Worker 中断/重启后从最后一个 confirmed 阶段之后恢复
curl -X POST http://localhost:8000/api/jobs/1/resume

# 仅当前快照、全部检查通过才发布
curl -X POST http://localhost:8000/api/jobs/1/publish \
  -H 'Content-Type: application/json' -d '{"confirm":true}'
```

## 十万测段压力验收

生成数据（默认 55,000 点、100,000 测段）：

```bash
python3 scripts/generate_pressure_data.py
```

纯算法验收（结果可复现到输入摘要、SHA-256 和算法参数）：

```bash
# 如系统 Python 没有依赖，请在 backend venv 中运行
PYTHONPATH=backend python scripts/acceptance_pressure.py
```

期望结果：

- `component_count = 1`
- `observation_count = 100000`
- `methods = ["sparse_normal_equations"]`
- `regularization = "none"`
- 输出包含输入文件 SHA-256、原始观测摘要 SHA-256、权重规则和求解器签名。

单元测试覆盖连通分量、秩亏、多基准矛盾和闭合环：

```bash
cd backend && pytest -q
```

## 验收场景对应关系

| 场景 | 平台行为 |
| --- | --- |
| 十万测段压力数据 | CSR 稀疏矩阵；按分量 ThreadPool 并行；LCA 基本环检查；结果表分页查看残差 |
| 不连通子网 | 分量预检当天列出点/边/基准数；无基准分量 QR 诊断阻塞，不拼接、不虚构连接 |
| 多基准矛盾 | 基准作为带权行；超过 3σ 的基准残差触发 `blocked_datum_contradiction` |
| 求解途中修订权重 | 旧 Job 继续绑定旧快照；新草稿必须生成新快照；旧任务完成后仅 `AUDITED_ONLY` |
| Worker 重启 | stage `confirmed_at` 作为恢复点；orchestrator 跳过已确认阶段 |
| 重复提交 | `uq_job_generation` 保证项目+快照只有一个 Job 代次 |
| 外业 CSV 批量修订 | 先预览逐行分类（可应用/版本冲突/缺失/重复/非法），确认走全成功或逐行策略；乐观锁+行级审计回执；重复确认幂等；旧快照/旧 Job 不变 |
| 发布 | 核对闭合环、基准约束、改正数/残差统计、快照哈希、算法参数和 `regularization=none` |

## 目录

```text
backend/app/core        配置、数据库
backend/app/models      SQLAlchemy/PostGIS 模型
backend/app/services    快照、稀疏求解、闭合环
backend/app/workers     Celery app、可恢复任务
backend/app/api         FastAPI 路由
frontend/src/components Cytoscape 拓扑、阶段、残差组件
scripts                 十万级数据生成与验收
```
