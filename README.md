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
- `POST /api/sync/batches`：离线同步批次（见下节）。
- `GET /api/audit`：读取审计记录，可用`?entity_id=`过滤。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 离线同步批次

野外队回驻点后，把同一观察事件拆出的多条离线记录一次上报：

```json
POST /api/sync/batches
{
  "batch_id": "B-100",
  "records": [
    {
      "offline_id": "off-1",
      "kind": "observation",
      "entity_id": "可选，服务端记录id",
      "baseline_version": 1,
      "data": {"location": "North Ridge", "remark": "saw tracks"},
      "field_timestamps": {"location": "2026-04-02T09:00:00+00:00"}
    }
  ]
}
```

- 每条记录带`offline_id`（离线编号）、`field_timestamps`（字段时间，标记客户端离线改过的字段）和`baseline_version`（基线版本）。
- 记录定位：优先`entity_id`，否则按自然键匹配（observation用`event_id`，sample用`sample_code`），都没有则新建；单笔补传时的"重复事件"因此自动转为合并。
- 字段级合并：只有客户端改过的字段（以字段时间为准）参与合并；基线版本之后服务端也改了同一字段时，保留服务端内容，客户端值列入该条的`conflicts`冲突清单。
- `batch_id`是幂等键：批次重传原样返回首次结果（`replayed: true`），不会重复写入。
- 单条记录校验失败只标记该条为`failed`，不影响批次内其他记录写入。
- 响应按`offline_id`给出每条最终记录、冲突清单和批次审计入口（`batch:<batch_id>`，可用`GET /api/audit?entity_id=`读取）。
- 原单笔登记`POST /api/<kind>`不受影响，照常可用。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

离线同步使用批次和幂等键演示，不包含真实野外通信协议、地图底图或完整空间索引。
