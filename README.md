# 会议邀请裁决 HTTP API

FastAPI + SQLite + `icalendar` + `python-dateutil` 实现。服务接收 iCalendar（ICS）的 `REQUEST`、`CANCEL`、`REPLY`，按 `UID / SEQUENCE / DTSTAMP` 维护当前邀请，并在 UTC 半开周窗内给出每位参会人的会议状态。

## 能力范围

- `POST /messages`：接受 ICS 文本，支持 `REQUEST`、`CANCEL`、`REPLY`。
- `GET /agenda`：按参会人邮箱和 `[start, end)` UTC 半开区间查询完整七天周窗。
- IANA `TZID`；不支持未知时区、浮动时间、全天事件。
- 支持 `RRULE:FREQ=DAILY|WEEKLY`、`INTERVAL`、`BYDAY`、`WKST`、`COUNT`、UTC `UNTIL`。
- 支持 `EXDATE`、单次 `RECURRENCE-ID` 例外、`RECURRENCE-ID;RANGE=THISANDFUTURE`。
- 移动场次时输出原场次的 UTC `RECURRENCE-ID`，但 `start/end` 使用新时间；单次例外优先于主规则。
- 春季跳空的本地时间不存在时跳过该次；秋季重复时间以较早一次（PEP 495 `fold=0`）为准。
- 先收到的回复/取消会先保存；匹配版本的请求补到后才进入裁决。
- 回复只属于对应 `UID + SEQUENCE`，新版本不会沿用旧版本回复。
- 较新 `SEQUENCE` 胜出；同 `SEQUENCE` 时较新 `DTSTAMP` 胜出。
- 完全相同的 ICS 字节重放幂等；相同 `UID/SEQUENCE/DTSTAMP` 但内容不同返回 `409`。
- 未知时区或不支持的 RRULE 返回 `422`。
- 无限 RRULE 不预展开，只在查询周窗内通过 dateutil 生成候选。

状态值：

- `accepted`
- `declined`
- `pending`：未回复或 `TENTATIVE`
- `cancelled`

## Docker Compose 一键启动

```bash
docker compose up --build
```

服务监听：

- API：<http://localhost:8000>
- OpenAPI：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/healthz>

SQLite 数据库保存在 Docker volume `meeting-data` 中。

本地不使用容器时：

```bash
python -m pip install -r requirements.txt
mkdir -p data
DATABASE_PATH=data/meeting.db uvicorn app:app --host 0.0.0.0 --port 8000
```

## 与实现一致的请求/响应示例

以下示例文件在 `examples/` 中。

### 1. 发送 REQUEST

请求是一个无限每日重复会议；`America/New_York` 09:00 在 2026-10-05 为 UTC 13:00。

```bash
curl -i \
  -X POST http://localhost:8000/messages \
  -H 'Content-Type: text/calendar; charset=utf-8' \
  --data-binary @examples/request_daily.ics
```

响应：

```json
{
  "uid": "standup-42@example.com",
  "method": "REQUEST",
  "sequence": 0,
  "dtstamp": "2026-10-05T08:00:00Z",
  "result": "applied"
}
```

再次发送同一个文件，不会重复写入：

```json
{
  "uid": "standup-42@example.com",
  "method": "REQUEST",
  "sequence": 0,
  "dtstamp": "2026-10-05T08:00:00Z",
  "result": "idempotent"
}
```

### 2. Ada 接受会议

```bash
curl -i \
  -X POST http://localhost:8000/messages \
  -H 'Content-Type: text/calendar; charset=utf-8' \
  --data-binary @examples/reply_accept.ics
```

响应：

```json
{
  "uid": "standup-42@example.com",
  "method": "REPLY",
  "sequence": 0,
  "dtstamp": "2026-10-05T08:15:00Z",
  "result": "stored"
}
```

该回复只对 `SEQUENCE=0` 生效。之后收到 `SEQUENCE=1` 的请求时，这些场次会重新变成 `pending`。

### 3. 取消 10 月 7 日单次会议

```bash
curl -i \
  -X POST http://localhost:8000/messages \
  -H 'Content-Type: text/calendar; charset=utf-8' \
  --data-binary @examples/cancel_instance.ics
```

响应：

```json
{
  "uid": "standup-42@example.com",
  "method": "CANCEL",
  "sequence": 0,
  "dtstamp": "2026-10-05T08:30:00Z",
  "result": "stored"
}
```

没有 `RECURRENCE-ID` 的 `CANCEL` 取消整个系列；带 `RECURRENCE-ID;RANGE=THISANDFUTURE` 时取消该场次及之后场次。

### 4. 查询七天 Agenda

周窗必须是精确 7 天，结束时间为排他边界：

```bash
curl --get http://localhost:8000/agenda \
  --data-urlencode attendee=ada@example.com \
  --data-urlencode start=2026-10-05T00:00:00Z \
  --data-urlencode end=2026-10-12T00:00:00Z
```

响应：

```json
{
  "attendee": "ada@example.com",
  "start": "2026-10-05T00:00:00Z",
  "end": "2026-10-12T00:00:00Z",
  "items": [
    {
      "uid": "standup-42@example.com",
      "recurrence_id": "2026-10-05T13:00:00Z",
      "start": "2026-10-05T13:00:00Z",
      "end": "2026-10-05T13:30:00Z",
      "status": "accepted"
    },
    {
      "uid": "standup-42@example.com",
      "recurrence_id": "2026-10-06T13:00:00Z",
      "start": "2026-10-06T13:00:00Z",
      "end": "2026-10-06T13:30:00Z",
      "status": "accepted"
    },
    {
      "uid": "standup-42@example.com",
      "recurrence_id": "2026-10-07T13:00:00Z",
      "start": "2026-10-07T13:00:00Z",
      "end": "2026-10-07T13:30:00Z",
      "status": "cancelled"
    },
    {
      "uid": "standup-42@example.com",
      "recurrence_id": "2026-10-08T13:00:00Z",
      "start": "2026-10-08T13:00:00Z",
      "end": "2026-10-08T13:30:00Z",
      "status": "accepted"
    }
  ]
}
```

实际响应包含 10 月 5 日至 11 日共 7 个对象；上面为节省篇幅省略了后三天。`2026-11-01` 起纽约进入标准时间，同一个 09:00 本地会议的 UTC 时间会相应变为 14:00。

### 错误响应

未知时区：

```http
HTTP/1.1 422 Unprocessable Entity
```

```json
{"error":"unknown IANA timezone: Not/AZone"}
```

不支持的 RRULE：

```json
{"error":"only DAILY and WEEKLY RRULE frequencies are supported"}
```

同版本异文：

```json
{
  "error": "same UID/SEQUENCE/DTSTAMP notification has different content",
  "uid": "standup-42@example.com",
  "method": "REQUEST",
  "sequence": 0,
  "dtstamp": "2026-10-05T08:00:00Z"
}
```

## 数据与性能

当前请求快照保存在 `current_components` 与 `current_component_attendees`，历史通知保存在 `messages`，回复和取消分别保存在 `reply_records` 与 `cancel_records`。

SQLite 中保存 RRULE 的主锚点、周期、星期掩码、有限结束时间；查询先用索引和简单日期算术粗筛，再用 dateutil 只生成当前周窗内的场次，不预展开无限 RRULE。设计目标是单人 100,000 封历史通知、结果周窗至多 100 场时，Agenda 查询在 1 秒内完成。

## 测试

```bash
pytest -q
```

测试覆盖请求/回复/取消、幂等、UTC 周窗、SEQUENCE 更新后旧回复失效，以及未知时区 422。
