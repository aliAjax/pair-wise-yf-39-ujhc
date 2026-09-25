# 野生动物疫病监测与离线同步

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8305`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

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
python3 app.py --db ./data.db --port 8305
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `observation`：现场观察；`sample`：样本与实验室结果；`cluster`：异常聚集事件。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `POST /api/sync/batches`：提交离线同步批次（见下节）。
- `GET /api/sync/batches/<batch_id>`：查询批次首次处理结果与批次审计。
- `GET /api/audit`：读取审计记录，可用`?entity_id=`过滤。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 离线同步批次

野外队回驻点后，把同一事件拆出的多条离线记录作为一个批次上传：

```json
POST /api/sync/batches
{
  "batch_id": "team-a-2026-04-05",
  "records": [
    {
      "offline_id": "OFF-1",
      "kind": "observation",
      "entity_id": "可选；缺省时按 event_id/sample_code 匹配已有记录",
      "base_version": 1,
      "field_timestamps": {"location": "2026-04-02T08:00:00"},
      "data": {"event_id": "E-1", "location": "South"}
    },
    {
      "offline_id": "OFF-2",
      "kind": "sample",
      "entity_id": "...",
      "base_version": 2,
      "action": "lab_result",
      "data": {"result": "positive", "result_at": "2026-04-05"}
    }
  ]
}
```

每条记录必须带`offline_id`（离线编号）、`base_version`（客户端基线版本，无基线传`0`）；`field_timestamps`（字段时间）可选但建议提供。记录按三种方式处理：

- 找不到目标实体：按原单笔规则创建（`created`）。
- 带`action`：基线版本等于当前版本时走原状态机执行（`applied`），否则保留服务端并记入冲突清单（`conflict`）。
- 否则做字段级合并：不同字段直接合入（`merged`）；同一字段在基线版本之后服务端也改过时，保留服务端内容，客户端值列入冲突清单（`conflict`）；批次内多条记录写同一字段时，字段时间较新的生效（`batch_kept`为被保留方）。

语义约定：

- 批次按`batch_id`幂等：重传沿用首次处理结果，不产生新的写入。
- 单条记录校验失败只标记该条为`error`，不影响批次内其他记录写入。
- 响应中`records`给出每个离线编号的最终记录，`results`给出每条状态与冲突清单，`audit`给出批次审计入口（审计实体`sync-batch:<batch_id>`）。
- 原单笔登记接口（`POST /api/<kind>`、`POST /api/entities/<id>/actions`）行为不变。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

离线同步使用批次和幂等键演示，不包含真实野外通信协议、地图底图或完整空间索引。批次幂等按`(操作者, batch_id)`存储首次结果，未对并发提交同一批次做严格互斥。
