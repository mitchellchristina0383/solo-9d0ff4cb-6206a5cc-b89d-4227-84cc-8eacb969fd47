# Battery Cell Cycle Capacity API

分批接收同一电芯的电流采样（UTC 秒 + 电流 A），识别完整充放电循环并按区间
前值积分计算充/放电容量（Ah），通过 FastAPI + SQLite 提供上传与查询接口。

## 业务规则

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
7. 导入或计算失败时事务回滚，旧采样与旧报告保持不变，并在 `upload_log` 留痕。
8. 并发：写入用 `BEGIN IMMEDIATE` 串行化；查询在单事务快照内读取同一版本的
   版本号、循环明细与总数，保证三者属于同一次已提交状态（SQLite WAL）。

## 接口（启动后见 `/docs`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/cells/{cell_id}/samples` | multipart 上传 CSV（字段名 `file`） |
| GET  | `/cells/{cell_id}/report` | 最新已提交报告：版本、总数、循环明细 |
| GET  | `/cells` | 全部电芯及最新版本概览 |
| GET  | `/health` | 存活探针 |

CSV 必须含表头，列名大小写/空格不敏感，时间列支持
`timestamp/ts/timestamp_utc/timestamp_seconds/utc_seconds/time_s`，
电流列支持 `current/current_a/i/amps` 等。示例见 `examples/`。

```bash
curl -X POST http://localhost:8000/cells/CELL-001/samples \
  -F "file=@examples/sample_one_cycle.csv"
curl http://localhost:8000/cells/CELL-001/report
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
python -m unittest discover -s tests -v       # 算法单测 + HTTP 集成测试
python tests/live_smoke.py                     # 对已运行服务的端到端冒烟
```

覆盖场景：基本循环与前值积分、断档（61s 截断 / 60s 保留）、无零符号直翻、
未闭合不计、补点改写与重新编号、幂等重投、异值整批拒收、失败回滚、
8 写 8 读并发一致性、同秒异值并发竞争。

## 目录结构

```
app/
  algorithm.py    # 分词 + 状态机循环识别、Ah 积分
  csv_import.py   # CSV 表头解析与校验
  db.py           # SQLite 事务、幂等/冲突、版本化报告
  main.py         # FastAPI 路由
tests/            # 单元、集成、实时冒烟测试
examples/         # 示例 CSV
Dockerfile, docker-compose.yml
```

## 设计说明与限制

- 电流以**微安整数**存储（`current_ma` 列，单位 µA），使幂等比较为精确整数
  比较，避免浮点文本往返误差；低于 1 µA 的差异会被取整。
- 报告只增不删：每次导入（含纯幂等重投）都追加一个 `report_versions` 行，
  便于追溯；查询接口只返回最新版本。
- 单文件 SQLite + WAL 适合本任务量级；写入在进程级连接上串行提交。
