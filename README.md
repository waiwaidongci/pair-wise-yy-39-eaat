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
- `POST /api/items`，可提交`dam_section`（坝段）、`measure_point`（测点）、`quantity`（本次读数），自动建立复发台账
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`；关闭时可提交`conclusion`（处理结论）
- `POST /api/items/{id}/escalation/confirm`，应急经理确认应急升级
- `GET /api/audit`

允许角色：inspector, dam_engineer, emergency_manager, viewer。异常值比控制阈值越高，缺陷优先级越高；应急处置缺陷必须完成复检并记录证据后才能关闭。

## 复发台账

- 新缺陷登记坝段、测点和本次读数（坝段与测点需同时填写）；同一坝段+测点已有未结缺陷时不新建单，沿用首张单，响应`merged=true`并累计`report_count`（续报次数），同时更新本次读数。
- 新建单按测点关联上次已结缺陷（`previous_item_id`），`recurrence_count`沿测点历史累计复发次数（含各单续报），`last_reading`记录前次读数。
- 复发次数（`recurrence_count + report_count`）超过3次，或本次读数较前次翻倍（≥2倍）时，缺陷自动提到`emergency`并置`escalation_status=pending`，需`emergency_manager`调用确认接口；确认前缺陷不可关闭。
- 详情和列表返回`recurrence_total`（复发次数）、`report_count`（续报次数）、`last_conclusion`（上次已结缺陷结论，来自关闭结论或`conclusion`记录）和`blockers`（阻挡项：未关闭事项、待确认的应急升级）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
