# 实验室仪器校准与方法验证

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8309`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8309
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `instrument`：仪器状态；`calibration`：校准记录；`method`：方法版本；`result`：检测结果。
- `standard`：标准器（含有效期 `valid_from`/`valid_until` 与并发 `capacity`，容量默认 1）。
- `work_order`：校准工单（仪器 × 标准器 × 半开时段 `[start_at, end_at)`）。

## 标准器占用账

- 占用账落在独立的 `occupancy` 表：每张已排工单对应一条 `held` 记录，
  完成转 `completed`，取消/作废转 `released`；`(work_order_id)` 上有
  `WHERE state='held'` 的唯一索引，一张工单不可能同时占两个位。
- **有效期覆盖**：建单时校验标准器有效期必须盖住整段（右边界也含）。
- **先到先得**：`schedule` 在一个 `BEGIN IMMEDIATE` 事务内查重+占位+改工单状态。
  两名计量员同时提交重叠时段时，先拿到写锁的留下，后到者收到 `409 Conflict`，
  且不留下任何占位。
- **容量排队**：容量满时改用 `enqueue` 进入 `queued`；占用释放（完成/取消）后，
  系统按 FIFO 自动把排队工单提升为 `scheduled`（审计动作为 `reschedule`，
  执行人 `scheduler`）；队头因容量满放不下时停止，失效窗口跳过不堵队。
- **标准器状态联动**：标准器 `suspend`/`expire`/`reactivate` 时，依赖它的
  `scheduled`/`queued` 工单一律作废、退回 `pending` 并释放占用；
  已 `completed` 的校准保留不动。作废工单需计量员核对窗口后重新排。
- **故障恢复**：服务启动时按工单核对占用账，`held` 但工单已不在
  `scheduled` 的孤儿记录（排程写入一半失败）会被释放，重排不会重复占位。

标准器动作：`suspend`、`reactivate`、`expire`、`renew`（只改有效期，
不联动作废）。工单动作：`schedule`、`enqueue`、`complete`、`cancel`。
时间接受 `2026-05-01` 或 `2026-05-01T09:00`，内部统一为秒级 ISO 串比较。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/occupancy?standard_id=&state=`：查看占用账（held/completed/released）。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

校准周期、误差和放行规则是可演示的业务模型，不替代实验室质量体系或计量认证。
