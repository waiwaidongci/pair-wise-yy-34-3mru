# 工伤事故调查与纠正措施

记录工伤经过、伤害、现场和证人，维护调查、纠正措施、验证与关闭流程。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制、调查批次、审计链与审计outbox。
- `src/service.py`：权限检查、用例编排、并发控制、批次归并和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则、失败与调查批次测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8311
```

默认端口为`8311`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/items/{id}/records/{rid}/verify`：安全经理验证纠正措施（需先登记`measure_scope`）
- `POST /api/items/{id}/records/{rid}/reopen`：已验证措施退回复核（必须填写`reason`）
- `POST /api/batches`：以`primary_item_id`建调查批次（事故未登记scene/shift时需随请求提供）
- `GET /api/batches` / `GET /api/batches/{id}`：批次列表与详情（归并来源、重算结果、退回记录）
- `POST /api/batches/{id}/members`：并入同现场同班次事故，body含`item_id`、`evidence_scopes`、可选`idempotency_key`；同一关联事故只接受一次，重复提交返回当前批次版本（`duplicate:true`）
- `POST /api/batches/{id}/recover`：审核写入失败后按原请求恢复待补审计
- `GET /api/audit`，支持`entity_id`与`entity_type`（事故或批次）过滤

允许角色：reporter, investigator, safety_manager, viewer。严重度越高、伤害指数越大或未关闭措施越多，优先级越高；严重事故必须在4小时内启动调查。

## 调查批次规则

- 同一现场(scene)、同一班次(shift)的关联事故才能并入；主事故保留原编号(`external_ref`)与状态，仅随并比重算严重度（取成员最高级）、伤害指数（成员求和）、响应期限与未关闭事项，版本递增。
- 关联事故保留各自经过(description)、伤情(injury)、状态和事项记录。
- 已验证措施默认继续有效；只有并入事故的`evidence_scopes`覆盖措施原`measure_scope`时才退回复核（重新打开、记录原因与覆盖来源）。范围按冒号分层，`*`为全局覆盖。
- 并入操作在进程锁和唯一索引下并发安全；业务数据与审计outbox同事务提交，审计写入失败后重放原请求即可补写，审计哈希链保持连续。
- 主事故关闭时按整批成员的未关闭事项校验；未归并批次的旧事故（批次字段为空）继续按单事故流转，旧库启动时自动补列迁移。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
