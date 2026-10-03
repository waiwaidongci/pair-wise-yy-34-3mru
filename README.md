# 工伤事故调查与纠正措施

记录工伤经过、伤害、现场和证人，维护调查、纠正措施、验证与关闭流程。同一现场、同一班次接连上报的事故自动组成**调查批次**：主事故保留原编号与状态，关联事故保留各自经过与伤情；并入时重算主事故的严重度、响应期限与未关闭事项。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限、关闭不变量、严重度聚合与范围覆盖。
- `src/repository.py`：SQLite建表、事务、版本控制、批次与审计链。
- `src/service.py`：权限检查、用例编排、并发控制、幂等恢复与批次重算。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则、失败与批次测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8311
```

默认端口为`8311`，首次启动自动建库，并为旧库补充批次字段（没有批次字段的旧事故仍按单事故使用）。使用`X-Actor`和`X-Role`请求头传递身份。

## 调查批次

- 上报事故时携带`site`（现场）与`shift`（班次），即按`(现场, 班次)`自动归入同一批次；首报事故为主事故，其余为关联事故。
- 主事故保留原编号（`id`/`external_ref`）与工作流状态；关联事故保留各自经过与伤情（描述与记录不被改写）。
- 新事故并入后重算主事故：严重度取批次最高级别，响应期限按新严重度重算，未关闭事项按批次合计（阻止主事故关闭）。
- 已通过验证的措施（`kind`为`measure`/`action`/`corrective_action`且`status=closed`）默认继续有效；当新证据（`kind=evidence`且带`scope`）的范围覆盖措施原范围时，措施退回未关闭状态复核，并记录退回原因。
- 两个上报入口用同一`external_ref`重复提交只接受一次，后到请求返回当前事故与批次版本；审核写入失败后按原请求重放即可恢复，不产生重复归并。
- 批次详情可查归并来源、重算结果与退回记录；审计可查`batch_created`/`batch_merge`/`batch_recalc`/`measure_returned`事件。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`（可带`site`、`shift`、`external_ref`）
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`（证据可带`scope`）
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/batches`
- `GET /api/batches/{id}`（含主事故、关联事故、归并、重算与退回记录）
- `POST /api/batches/{id}/merge`，提交`item_id`把游离事故并入批次
- `GET /api/audit`

允许角色：reporter, investigator, safety_manager, viewer。严重度越高、伤害指数越大或未关闭措施越多，优先级越高；严重事故必须在4小时内启动调查。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
