# 大坝巡检、缺陷与应急管理

安排巡检，记录渗流、位移、裂缝等缺陷并跟踪修复、复检和应急预案。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8316
```

默认端口为`8316`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`，必须登记`dam_section`（坝段）、`measure_point`（测点）和`reading`（本次读数）
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`；关闭时可提交`conclusion`（结论）
- `POST /api/items/{id}/confirm_escalation`，应急经理确认应急升级，必须提交`expected_version`
- `GET /api/audit`

允许角色：inspector, dam_engineer, emergency_manager, viewer。异常值比控制阈值越高，缺陷优先级越高；应急处置缺陷必须完成复检并记录证据后才能关闭。

## 复发台账

- 同一测点存在未结缺陷时，重复上报不新建单，沿用首张并累计`report_count`（续报次数），读数更新为最新值，响应返回`merged: true`（HTTP 200）。
- 测点无未结缺陷时新建单，自动关联该测点上次已结缺陷（`previous_item_id`），`recurrence_count`（复发次数）加一，并带入上次结论`last_conclusion`。
- 复发超过三次（`recurrence_count > 3`）或本次读数较前次翻倍时，缺陷自动提到应急级（`emergency`）并置`escalation_pending`，期间所有状态转换被阻挡，须应急经理调用`confirm_escalation`确认后方可继续流转。
- 详情和列表均返回`recurrence_count`、`report_count`、`last_conclusion`和`blockers`（阻挡项）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
