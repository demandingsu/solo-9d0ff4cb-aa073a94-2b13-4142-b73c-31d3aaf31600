# Fertilizer RBD Trial API

随机区组试验（至多 12 个区组，处理 A/B）干物质产量分析服务。FastAPI + SQLite，
交互式文档在 `/docs`（Swagger UI），可直接完成导入、更正、发布和查询。

**分析口径**

- 干物质产量 = 鲜重 × (1 − 含水率) ÷ 面积（含水率为小数，如 0.62）
- 仅当某区组 A、B 两小区的面积、鲜重、含水率齐全时纳入分析，计算组内差 B − A
- 发布内容：逐组差、均值差、双侧**精确随机化检验** p 值 —— 枚举各区组 A/B 对调的
  全部 2ⁿ 种排列，统计 |均值差| 不小于实测值的比例（含实测排列本身，故 p ≥ 1/2ⁿ）
- 缺测区组列为待补（`pending`，附缺失原因）；没有任何齐全区组时不发布（HTTP 422）

**版本与过期**

- 每次实际改动（导入/含水率/更正）使 `data_version` +1；无变化的重发不升版本
- 发布在单个 `BEGIN IMMEDIATE` 事务内读取同一快照并落库，区组、差值、p 值、来源
  （provenance：数据版本 + 原始行 + sha256）天然同版
- 数据一旦变化，旧发布自动标记 `stale: true`，但始终可查询，来源完整保留
- 发布失败（如全部缺测）不写入任何记录，旧结论不受影响

## 快速开始

```bash
docker compose up --build
# 打开 http://127.0.0.1:8000/docs
```

本地运行（Python 3.11+）：

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

端到端演示（服务启动后）：

```bash
./scripts/demo.sh            # 或 ./scripts/demo.sh http://127.0.0.1:8000
```

手动 curl 流程：

```bash
curl -X POST localhost:8000/imports   -H 'Content-Type: application/json' -d @examples/import.json
curl -X POST localhost:8000/publications                       # 422：全部缺测，不发布
curl -X POST localhost:8000/moisture  -H 'Content-Type: application/json' -d @examples/moisture.json
curl -X POST localhost:8000/publications                       # 201：4 组齐全，5/6 待补
curl -X POST localhost:8000/moisture  -H 'Content-Type: application/json' -d @examples/moisture_late.json
curl    localhost:8000/publications                            # 旧结论 stale: true
curl -X POST localhost:8000/publications                       # 201：6 组，新 p 值
curl -X POST localhost:8000/corrections -H 'Content-Type: application/json' -d @examples/correction.json
curl -X POST localhost:8000/publications                       # 201：更正后的同版结论
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/imports` | 批量导入/重导小区面积+鲜重（可附带含水率）；已有含水率默认保留 |
| POST | `/moisture` | 录入已导入小区的含水率（迟到检测同此入口） |
| POST | `/corrections` | 更正面积/鲜重/含水率；`"moisture": null` 清除错误含水率（该组回到待补） |
| GET | `/plots` | 当前原始数据与 `data_version` |
| GET | `/blocks` | 当前齐全/待补区组及缺失原因 |
| POST | `/publications` | 发布当前结论；无齐全区组时 422，旧结论不受影响 |
| GET | `/publications` | 全部发布（新→旧），含 `stale` 标记 |
| GET | `/publications/latest` | 最新一次发布（可能已过期） |
| GET | `/publications/{id}` | 某次发布全文：逐组差、均值差、p 值、待补、来源 |

校验规则：区组号 1–12；处理仅 A/B；面积 > 0；鲜重 ≥ 0；含水率 ∈ [0, 1)；
同一请求内 (block_id, treatment) 不得重复；含水率/更正只接受已导入的小区。

## 项目结构

```
app/
  main.py       # FastAPI 路由与发布事务
  analysis.py   # 干物质、组内差、精确随机化检验（纯函数）
  db.py         # SQLite 连接、schema、data_version
  schemas.py    # Pydantic 请求/响应模型
examples/       # 示例数据（导入、含水率、迟到含水率、更正）
scripts/demo.sh # 端到端演示
tests/          # pytest 端到端测试
Dockerfile / docker-compose.yml
```

## 测试

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

覆盖：完整流程（导入→缺测拒发→部分齐全→迟到→更正→重发布）、精确 p 值
（Fraction 精确枚举独立复核 + 已知真值用例）、失败发布保留旧结论、
同版来源校验、无操作改动不误判过期、参数校验与 404。
