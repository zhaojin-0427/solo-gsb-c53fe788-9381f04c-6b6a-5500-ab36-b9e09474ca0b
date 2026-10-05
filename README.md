# Meeting Invite Adjudication Service

用 **FastAPI + SQLite + icalendar + python-dateutil** 实现的会议邀请裁决服务。

- `POST /messages`：接收 iTIP（iCalendar）消息，方法为 `REQUEST` / `CANCEL` / `REPLY`。
- `GET /agenda`：按 **参会人** 和 **UTC 半开时间窗**（默认一周，`[周一 00:00, 下周一 00:00)`）返回每个场次的 `UID`、`RECURRENCE-ID`、起止时间以及裁决状态：
  `accepted`（接受）/ `declined`（拒绝）/ `needs-action`（待回复）/ `cancelled`（取消）。

所有日期时间以 UTC 输出（`...+00:00`），区间为**半开区间** `[start, end)`。

---

## 一、快速开始（Docker Compose 一键启动）

需要 Docker 与 Compose v2。

```bash
docker compose up --build
```

服务监听在 <http://localhost:8000>，SQLite 数据保存在命名卷 `meeting-data`
（容器内路径 `/data/meetings.db`）。健康检查：

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

交互式 API 文档：<http://localhost:8000/docs>。

停止并保留数据：

```bash
docker compose down
```

清空数据：

```bash
docker compose down -v
```

### 不用 Docker（本地运行）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export MEETING_DB_PATH=./data/meetings.db
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试（含递归规则属性测试与 10 万历史通知的性能基准）：

```bash
pip install pytest httpx
python3 -m pytest -q
```

---

## 二、裁决规则（与实现一致）

**身份与版本**

- 身份是 `(UID, RECURRENCE-ID)`；`RECURRENCE-ID` 缺省表示整个会议 / 整个序列。
- 通知按 `(SEQUENCE, DTSTAMP)` 裁决：旧版本不会覆盖新版本；**旧 SEQUENCE 的回复不会沿用到新版本**。
- 内容完全相同的消息重复投递是幂等的（返回 `duplicate`，可安全重放）。
- `UID + SEQUENCE + DTSTAMP` 都相同但语义内容不同 → `409 version_conflict`（同版异文冲突）。
- 同 `SEQUENCE`、但 `DTSTAMP` 更新且内容不同 → 按 DTSTAMP 采纳为内容刷新（`200 stored`）。

**调度语义**

- 支持 IANA `TZID`（含夏令时）、`FREQ=DAILY` / `FREQ=WEEKLY`、`INTERVAL`、`BYDAY`、`WKST`、`UNTIL`、`COUNT`、`EXDATE`。
- 单次例外（带 `RECURRENCE-ID` 的 REQUEST）只改那一场；会议被移动时**保留原场次的 RECURRENCE-ID 身份**，只改变展示起止。
- 单次例外**优先于** `EXDATE`（可以在被挖空的槽位上重新创建一场）。
- `RECURRENCE-ID` 不是规则成员时：命中已移动场次的新起点即按原场次绑定；否则**回拨到较早的一次有效场次**，回拨会跳过 `EXDATE` 空洞。
- `RANGE=THISANDFUTURE`（REQUEST）：保留边界之前的场次，边界起按例外的新起点与相同节奏延续，尾部场次仍用各自原始 RECURRENCE-ID。
- `RANGE=THISANDFUTURE`（CANCEL）：取消边界及之后所有场次；边界之后若来一份更新的单场 REQUEST，可单独复活该场。
- 无限 `RRULE`（无 `COUNT/UNTIL`）**从不预展开**：查询只在窗口内有界枚举，场次下标用算术计算。
- 乱序投递：先到的 `REPLY` / `CANCEL`（甚至单场 REQUEST 例外）若还没有对应的 REQUEST，会进入暂存区；等 REQUEST 补齐后在同一事务里补裁决。

**错误**

- 无法解析、浮动时间（无 TZID 又非 UTC）、未知/非 IANA 时区、不支持的 FREQ/RRULE 部件/RANGE → `422 unprocessable_calendar`。

---

## 三、实际请求 / 响应示例

下面所有命令都可直接对正在运行的服务执行（`Content-Type: text/calendar`）。

### 1) 发送一个周期性 REQUEST

每周一 09:00（America/New_York）例会，3 月 24 日挖空（EXDATE），两位参会人。

```bash
curl -s -X POST http://localhost:8000/messages \
  -H 'Content-Type: text/calendar' \
  --data-binary @- <<'ICS'
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Acme//Meetings//EN
METHOD:REQUEST
BEGIN:VEVENT
UID:standup-2025@acme.example
DTSTAMP:20250301T120000Z
SEQUENCE:0
DTSTART;TZID=America/New_York:20250310T090000
DTEND;TZID=America/New_York:20250310T093000
RRULE:FREQ=WEEKLY;BYDAY=MO;WKST=MO
EXDATE;TZID=America/New_York:20250324T090000
ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:alice@example.com
ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:bob@example.com
ORGANIZER:mailto:boss@acme.example
SUMMARY:Weekly Standup
END:VEVENT
END:VCALENDAR
ICS
```

响应：

```json
{
  "method": "REQUEST",
  "results": [
    { "uid": "standup-2025@acme.example", "kind": "REQUEST", "result": "stored" }
  ]
}
```

### 2) 重放幂等

把同一份 ICS 再 POST 一次：

```json
{
  "method": "REQUEST",
  "results": [
    { "uid": "standup-2025@acme.example", "kind": "REQUEST", "result": "duplicate" }
  ]
}
```

### 3) 参会人 REPLY：接受 3 月 10 日那一场

注意 `RECURRENCE-ID` 用纽约本地时间给出，服务换算为 UTC 身份 `2025-03-10T13:00:00+00:00`
（3 月已进入夏令时 EDT，UTC−4）。

```bash
curl -s -X POST http://localhost:8000/messages \
  -H 'Content-Type: text/calendar' \
  --data-binary @- <<'ICS'
BEGIN:VCALENDAR
VERSION:2.0
METHOD:REPLY
BEGIN:VEVENT
UID:standup-2025@acme.example
DTSTAMP:20250302T080000Z
SEQUENCE:0
RECURRENCE-ID;TZID=America/New_York:20250310T090000
ATTENDEE;PARTSTAT=ACCEPTED:mailto:alice@example.com
END:VEVENT
END:VCALENDAR
ICS
```

```json
{
  "method": "REPLY",
  "results": [
    { "uid": "standup-2025@acme.example", "kind": "REPLY", "result": "stored" }
  ]
}
```

### 4) 单次例外 REQUEST：把 3 月 17 日那场移到 11:00

移动后仍保留原 RECURRENCE-ID（13:00 那个槽位身份），展示时间变为 15:00–15:30 UTC。

```bash
curl -s -X POST http://localhost:8000/messages \
  -H 'Content-Type: text/calendar' \
  --data-binary @- <<'ICS'
BEGIN:VCALENDAR
VERSION:2.0
METHOD:REQUEST
BEGIN:VEVENT
UID:standup-2025@acme.example
DTSTAMP:20250305T120000Z
SEQUENCE:0
RECURRENCE-ID;TZID=America/New_York:20250317T090000
DTSTART;TZID=America/New_York:20250317T110000
DTEND;TZID=America/New_York:20250317T113000
SUMMARY:Standup moved 2h later
END:VEVENT
END:VCALENDAR
ICS
```

```json
{
  "method": "REQUEST",
  "results": [
    { "uid": "standup-2025@acme.example", "kind": "REQUEST", "result": "stored" }
  ]
}
```

### 5) CANCEL：取消 3 月 31 日单场

```bash
curl -s -X POST http://localhost:8000/messages \
  -H 'Content-Type: text/calendar' \
  --data-binary @- <<'ICS'
BEGIN:VCALENDAR
VERSION:2.0
METHOD:CANCEL
BEGIN:VEVENT
UID:standup-2025@acme.example
DTSTAMP:20250306T120000Z
SEQUENCE:0
RECURRENCE-ID;TZID=America/New_York:20250331T090000
END:VEVENT
END:VCALENDAR
ICS
```

```json
{
  "method": "CANCEL",
  "results": [
    { "uid": "standup-2025@acme.example", "kind": "CANCEL", "result": "stored" }
  ]
}
```

### 6) 查询议程

用显式 UTC 半开窗口查询 3/10–4/7（也可以用 `week=2025-W11` 这种 ISO 周参数，
或省略窗口，默认查询当前 UTC 周）：

```bash
curl -s -G http://localhost:8000/agenda \
  --data-urlencode attendee=alice@example.com \
  --data-urlencode start=2025-03-10T00:00:00Z \
  --data-urlencode end=2025-04-07T00:00:00Z
```

返回（3/24 被 EXDATE 跳过；3/17 移动但保留原身份；3/31 取消；3/10 已接受，其余待回复）：

```json
{
  "attendee": "alice@example.com",
  "start": "2025-03-10T00:00:00+00:00",
  "end": "2025-04-07T00:00:00+00:00",
  "events": [
    {
      "uid": "standup-2025@acme.example",
      "recurrence_id": "2025-03-10T13:00:00+00:00",
      "start": "2025-03-10T13:00:00+00:00",
      "end": "2025-03-10T13:30:00+00:00",
      "status": "accepted"
    },
    {
      "uid": "standup-2025@acme.example",
      "recurrence_id": "2025-03-17T13:00:00+00:00",
      "start": "2025-03-17T15:00:00+00:00",
      "end": "2025-03-17T15:30:00+00:00",
      "status": "needs-action"
    },
    {
      "uid": "standup-2025@acme.example",
      "recurrence_id": "2025-03-31T13:00:00+00:00",
      "start": "2025-03-31T13:00:00+00:00",
      "end": "2025-03-31T13:30:00+00:00",
      "status": "cancelled"
    }
  ]
}
```

同一窗口查 `bob@example.com`：他没有回复，所有未取消场次都是 `needs-action`，
且 3/10 不会因为 alice 接受而改变。

### 7) 乱序投递：先 REPLY，后 REQUEST

REPLY 先到时会议还不存在，进入暂存区（仍返回 `staged`）；随后 REQUEST 补齐，
暂存的回复在同一事务里补裁决，议程直接显示 `declined`：

```text
# 先到的 REPLY
{ "method": "REPLY",  "results": [ { "uid": "oneoff@acme.example", "kind": "REPLY",  "result": "staged" } ] }
# 后到的 REQUEST
{ "method": "REQUEST","results": [ { "uid": "oneoff@acme.example", "kind": "REQUEST","result": "stored" } ] }
```

```json
{
  "attendee": "alice@example.com",
  "start": "2025-03-10T00:00:00+00:00",
  "end": "2025-03-17T00:00:00+00:00",
  "events": [
    {
      "uid": "oneoff@acme.example",
      "recurrence_id": null,
      "start": "2025-03-12T15:00:00+00:00",
      "end": "2025-03-12T16:00:00+00:00",
      "status": "declined"
    }
  ]
}
```

### 8) 同版异文冲突 → 409

同一个 `UID + SEQUENCE=0 + DTSTAMP`，但时间不同（两份"同一版本"内容不一致）：

```json
{
  "error": "version_conflict",
  "detail": "conflicting content for standup-2025@acme.example at SEQUENCE 0",
  "uid": "standup-2025@acme.example",
  "recurrence_id": null,
  "sequence": 0
}
```

若 SEQUENCE 相同但 **DTSTAMP 更晚**，则按时间裁决为内容刷新（`200 stored`），不报冲突。

### 9) 未知时区 → 422

把上面的 `TZID=America/New_York` 改成 `TZID=Mars/Olympus`：

```json
{
  "error": "unprocessable_calendar",
  "detail": "unknown or unsupported TZID: 'Mars/Olympus'"
}
```

---

## 四、HTTP 接口摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/messages` | body 为一个 iTIP `VCALENDAR`（`METHOD: REQUEST/CANCEL/REPLY`），可含多个 VEVENT |
| `GET` | `/agenda` | 参数：`attendee`（必填，邮箱，大小写不敏感）；窗口三选一：`start`+`end`（UTC ISO-8601）、`week=YYYY-Www`、省略（当前 UTC 周） |
| `GET` | `/health` | 存活探针 |

`POST /messages` 每个 VEVENT 的 `result` 取值：

- `stored`：已采纳；`duplicate`：完全相同的重放；`ignored`：旧版本，丢弃；
- `staged`：会议 REQUEST 尚未到达，先暂存，补齐后自动补裁决。

状态枚举：`accepted` / `declined` / `needs-action` / `cancelled`
（`TENTATIVE`/`DELEGATED` 回复会分别透传为 `tentative`/`delegated`）。

---

## 五、性能

- 单次/周期主表使用部分索引（单次按时间、周期按首场、例外按移动后起点）。
- 周期查询默认只在请求窗口内有界枚举；只有存在移动例外的序列才向前后各扩 400 天
  的填充窗以定位移动场次。
- 场次下标为算术计算（O(1)），不从头枚举。
- 基准测试 `tests/test_perf.py`：单个参会人 **100,000** 条历史通知（外加一条无限周规则），
  周窗查询约 **15–35 ms**（远低于 1 秒预算）；周窗约 100 场时同样在数十毫秒内。
