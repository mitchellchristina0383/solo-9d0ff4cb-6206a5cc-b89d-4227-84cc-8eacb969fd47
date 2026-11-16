# Battery Cell Cycle Capacity & Step-Table Diagnosis API

分批接收同一电芯的电流采样（UTC 秒 + 电流 A），识别完整充放电循环并按区间
前值积分计算充/放电容量（Ah）；同时接收批准的充放电工步表，把实测电流段与
工步一对一配对，找出漏做或错配的工步，避免偏离程序的数据进入寿命分析。
通过 FastAPI + SQLite 提供上传与查询接口。

## 业务规则

### 采样与容量报告

1. 电流为 0 的采样是**段界**。
2. 只有连续序列 `零 → 正电流 → 零 → 负电流 → 零` 构成一个循环；末尾未闭合
   的序列不计入。
3. 相邻采样时间差 **> 60 秒**即断档：当前序列截断，且该长区间本身不参与积分
   （恰好 60 秒不断档）。
4. 容量按**区间前值积分**（左矩形法）：`Ah += |I_prev| * (t - t_prev) / 3600`，
   充电、放电分别累计。
5. 同电芯同秒同值重复上传**幂等**（不重复插入）；同秒异值则**整批拒收**
   （HTTP 409，库内数据不变）。
6. 迟到补点与历史采样合并后**全量重算**，循环可能被改写、编号重新排列；
   每次成功导入生成一个新的报告版本，历史版本保留。

### 工步表

7. `POST /cells/{cell_id}/steps` 以 CSV **整版替换**该电芯的工步表，字段为
   工步号、起始/结束 UTC 秒、正/负电流极性。整份文件校验通过才落库：
   整数时间戳、`start < end`、工步号唯一、时间段不得重叠（首尾相接可以）。
   分批上传不叫替换；每次替换生成新的 `step_version`，旧版本保留可追溯。
8. 替换失败（CSV 解析、校验或存储异常）时事务回滚，**旧工步表与旧诊断原样
   保留**，并在 `upload_log` 记一条 rejected。

### 诊断（实测段 ↔ 工步配对）

9. 从已入库采样提取**实测段**：同一极性的连续非零采样序列，必须以零电流
   采样封闭，段内相邻采样间隔不得超过 60 秒（>60 秒的断档既不计容量也不能
   跨接）。末尾未封闭的序列不算；无零封闭的直接反号处，前一段作废、新一段
   从反号采样开始。段的容量与容量报告使用同一套前值积分。
10. 实测段按时间顺序与同极性工步**一对一、不交叉**配对；起止各允许
    **15 秒**（含 15 秒）容差。
11. 优化目标按字典序：**先最大化配对数**，再**最小化起止偏差总和**。
12. 并列最优**必须全部列出**，不得任选一组：
    - `matched` 只包含在**所有**最优解中都出现的配对（确定配对）；
    - `ambiguous_steps` / `ambiguous_segments` 列出参与了部分最优解、
      因而存在候选歧义的工步/实测段，附候选对方编号与 `may_be_unmatched`；
    - `candidates` 列出枚举到的全部最优方案（无并列时恰好 1 个）；
    - 计数为精确值 `candidate_count_exact`；极端输入下枚举有上限，
      截断时 `candidates_truncated=true`（确定/歧义判定仍精确）。
13. 诊断查询返回：未配对工步（`unmatched_steps`）、确定配对的实测时段及其
    积分容量（`matched[].measured_start/measured_end/capacity_ah`）、全部实测
    段清单（`segments`）。只有从未参与任何最优解的工步/实测段才进入
    `unmatched_*`，有歧义的走 `ambiguous_*`。

### 一致性

14. 采样导入与工步表替换都会在同一写事务内**重算诊断**（迟到补点或换表可能
    改写诊断；换表本身不改采样与容量报告版本）。因此采样、报告版本、工步表
    与诊断来自**同一已提交状态**。
15. 每次诊断记录其 `report_version`、`step_version` 和采样集合的 SHA-256
    `samples_digest`。`GET /cells/{cell_id}/state` 在**单个读事务**内读取报告、
    工步表、诊断与版本号；`diagnostics.consistent` 仅在摘要与版本完全吻合时
    为 true。
16. 导入/替换/计算失败一律回滚，旧采样、旧报告、旧表、旧诊断与 `upload_log`
    保持一致。

## 接口（启动后见 `/docs`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/cells/{cell_id}/samples` | multipart 上传采样 CSV（字段名 `file`） |
| POST | `/cells/{cell_id}/steps` | multipart 整版替换工步表 CSV（字段名 `file`） |
| GET  | `/cells/{cell_id}/report` | 最新已提交容量报告：版本、总数、循环明细 |
| GET  | `/cells/{cell_id}/steps` | 最新已提交工步表 |
| GET  | `/cells/{cell_id}/diagnostics` | 最新诊断：确定配对、未配对、并列候选、容量 |
| GET  | `/cells/{cell_id}/state` | 同一快照的报告 + 工步表 + 诊断及版本校验 |
| GET  | `/cells` | 全部电芯、报告版本与工步表版本概览 |
| GET  | `/health` | 存活探针 |

采样 CSV 必须含表头，列名大小写/空格不敏感，时间列支持
`timestamp/ts/timestamp_utc/timestamp_seconds/utc_seconds/time_s`，
电流列支持 `current/current_a/i/amps` 等。

工步表 CSV 必须含表头，列名大小写、空格、下划线、连字符不敏感，并接受中文
别名：工步号列 `step_no/step/step_number/序号/工步号`，起始列
`start_utc_seconds/start/start_ts/起始UTC秒/开始时间`，结束列
`end_utc_seconds/end/end_ts/结束UTC秒/终止时间`，极性列
`polarity/current_sign/极性/方向/电流极性`。极性值接受
`positive/pos/+/1/charge/正/充电` 与 `negative/neg/-/-1/discharge/负/放电`。
示例见 `examples/`。

```bash
curl -X POST http://localhost:8000/cells/CELL-001/samples \
  -F "file=@examples/sample_one_cycle.csv"
curl -X POST http://localhost:8000/cells/CELL-001/steps \
  -F "file=@examples/sample_steps.csv"
curl http://localhost:8000/cells/CELL-001/report
curl http://localhost:8000/cells/CELL-001/diagnostics
curl http://localhost:8000/cells/CELL-001/state
```

`diagnostics` 关键字段：

```jsonc
{
  "report_version": 1, "step_version": 1, "diagnostics_version": 2,
  "samples_digest": "…", "consistent": true,
  "max_pairs": 2, "min_total_deviation": 0,
  "ambiguous": false, "candidate_count_exact": 1,
  "matched": [{"step_no": 1, "segment_no": 1, "measured_start": 10,
               "measured_end": 20, "capacity_ah": 0.00555,
               "start_deviation_s": 0, "end_deviation_s": 0}, …],
  "unmatched_steps": [], "unmatched_segments": [],
  "ambiguous_steps": [], "ambiguous_segments": [],
  "candidates": [{"pairs": [...], "unmatched_step_nos": [],
                  "unmatched_segment_nos": []}],
  "segments": [...]
}
```

## 运行

### Docker Compose（推荐）

```bash
docker compose up --build
# API:   http://localhost:8000
# Docs:  http://localhost:8000/docs
```

SQLite 数据保存在命名卷 `battery-data`（容器内 `/data/battery.db`）。

### 本地运行

```bash
pip install -r requirements.txt
BATTERY_DB_PATH=./data/battery.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 测试

```bash
python -m unittest discover -s tests -v       # 单测 + HTTP 集成测试
python tests/live_smoke.py                     # 对已运行服务的端到端冒烟
```

覆盖场景：

- 容量报告：基本循环与前值积分、断档（61s 截断 / 60s 保留）、无零符号直翻、
  未闭合不计、补点改写与重新编号、幂等重投、异值整批拒收、失败回滚。
- 实测段：零封闭、未封闭丢弃、段内断档、恰好 60 秒、反号、连续零。
- 配对：15 秒边界（15 接受 / 16 拒绝）、极性不符、漏做工步后重新对齐、
  多测段、配对数优先于偏差、交错顺序不交叉、并列最优候选、
  部分最优共享确定配对、随机输入与穷举对拍。
- 工程：工步表重叠整表拒收且旧表保留、中文表头/极性、迟到补点改写诊断、
  换表在不改采样时重算诊断、存储异常回滚、并发换表与查询、`state` 版本一致。
- 并发：8 写 8 读快照一致性、同秒异值并发竞争。

## 目录结构

```
app/
  algorithm.py    # 分词 + 状态机循环识别、Ah 积分、实测段提取
  csv_import.py   # 采样 CSV 表头解析与校验
  steps_csv.py    # 工步表 CSV 解析、整表校验（重叠/极性/工步号）
  diagnostics.py  # 配对 DP：最大化配对数 → 最小化偏差、并列枚举、确定配对
  db.py           # SQLite 事务、幂等/冲突、版本化报告/工步表/诊断
  main.py         # FastAPI 路由
tests/            # 单元、集成、实时冒烟测试
examples/         # 示例采样与工步表 CSV
Dockerfile, docker-compose.yml
```

## 设计说明与限制

- 电流以**微安整数**存储（`current_ma` 列，单位 µA），使幂等比较为精确整数
  比较，避免浮点文本往返误差；低于 1 µA 的差异会被取整。
- 报告/工步表/诊断均只增不删：每次导入、换表都追加新版本行，便于追溯；查询
  接口只返回最新版本，`state` 同时给出三者的版本与采样摘要。
- 配对使用 O(步骤数 × 实测段数) 的“配对数/偏差和”后缀 DP，`candidate_count_exact`
  由另一组计数 DP 精确给出；枚举最优方案有 1000 个候选 / 20 万搜索节点的安全
  上限，超限时 `candidates_truncated=true`，但确定配对与歧义集合仍按精确计数
  判定。
- 单文件 SQLite + WAL 适合本任务量级；写入在进程级连接上串行提交。
- 工步表替换是**整版覆盖**（不提供增量修改），避免半张表生效。
