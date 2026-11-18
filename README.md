# Fertilizer RBD Trial API

随机区组试验（至多 12 个区组，处理 A/B）干物质产量分析服务。FastAPI + SQLite，
交互式文档在 `/docs`（Swagger UI），可直接完成导入、更正、发布、误差核算和查询。

**分析口径**

- 干物质产量 = 鲜重 × (1 − 含水率) ÷ 面积（含水率为小数，如 0.62）
- 仅当某区组 A、B 两小区的面积、鲜重、含水率齐全时纳入分析，计算组内差 B − A
- 发布内容：逐组差、均值差、双侧**精确随机化检验** p 值 —— 枚举各区组 A/B 对调的
  全部 2ⁿ 种排列，统计 |均值差| 不小于实测值的比例（含实测排列本身，故 p ≥ 1/2ⁿ）
- 缺测区组列为待补（`pending`，附缺失原因）；没有任何齐全区组时不发布（HTTP 422）

**测量误差核算（误差范围报告）**

针对已发表结论，判断肥效均值差 B−A 的正负号是否会被测量误差推翻。误差模型：

- **共用秤增益误差**：全试验同一个增益系数 g ∈ [1−e, 1+e]，其中 0 ≤ e < 1；
  所有小区真实重量共享这一个 g（系统性偏差，不可各取各的）
- **各小区独立误差**：称重绝对误差幅度 `weight_error`（kg）与含水率绝对误差幅度
  `moisture_error`（小数），小区之间相互独立
- 真实鲜重 = 记录鲜重 × g + δ，|δ| ≤ `weight_error`，且**不得为负**（下界截断到 0）；
  真实含水率 = 记录含水率 + 误差，**限制在 [0, 1]**（两端截断）
- 对该发表参与分析的全部区组，求均值差 B−A 的**可达上下界**（精确值，非近似）：
  固定 g 时各小区变量独立、极值都在区间端点；整体目标关于 g 分段线性，拐点只可能
  出现在非负截断处（g = weight_error / 记录鲜重），枚举区间端点与拐点即得全局上下界
- `sign`：下界 > 0 为 `positive`，上界 < 0 为 `negative`，跨零为 `indeterminate`
  （`sign_certain` 同步给出布尔值，带 1e-12 容差防止浮点误判）

**版本与过期**

- 每次实际改动（导入/含水率/更正）使 `data_version` +1；无变化的重发不升版本
- 发布在单个 `BEGIN IMMEDIATE` 事务内读取同一快照并落库，区组、差值、p 值、来源
  （provenance：数据版本 + 原始行 + sha256）天然同版
- 数据一旦变化，旧发布自动标记 `stale: true`，但始终可查询，来源完整保留
- 发布失败（如全部缺测）不写入任何记录，旧结论不受影响
- 误差设定每次实质修订插入新版本（逐小区设定随版本快照保存）；内容完全相同的
  替换是 no-op，不升版本、不会误伤旧报告
- 误差报告固定在「发表时的数据版本 + 当时的误差设定版本」上；源数据更正或误差
  设定修订后，旧报告标记 `stale` 并给出原因（`source_data_changed` /
  `error_settings_revised`），报告本体永不变更。**更正不改写旧发表与旧报告**
- 新报告与它依据的发表、设定在同一个 `BEGIN IMMEDIATE` 事务、同一已提交状态内
  读取；设定未覆盖参与小区（422）或发表不存在（404）时事务回滚，失败不覆盖旧版

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
curl -X PUT  localhost:8000/error-settings -H 'Content-Type: application/json' -d @examples/error_settings.json
curl -X POST localhost:8000/publications/1/error-reports       # 201：均值差可达上下界与正负号判定
curl        localhost:8000/error-reports                       # 报告列表（含 stale 与原因）
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
| PUT | `/error-settings` | 全量替换误差设定（共用增益 e + 各小区称重/含水率误差）；实质修订升版本，相同替换 no-op |
| GET | `/error-settings` | 当前误差设定版本（含每个小区的误差幅度） |
| POST | `/publications/{id}/error-reports` | 按发表编号核算均值差 B−A 可达上下界并判定正负号；同事务取同一已提交状态，失败不写库 |
| GET | `/error-reports` | 全部误差报告（新→旧），支持 `?publication_id=` 过滤，含 `stale`/`stale_reasons` |
| GET | `/error-reports/latest` | 最新一次误差报告（可能已过期） |
| GET | `/error-reports/{id}` | 某份报告全文：上下界、正负号、参与区组、误差设定快照、发表来源 |

校验规则：区组号 1–12；处理仅 A/B；面积 > 0；鲜重 ≥ 0；含水率 ∈ [0, 1)；
同一请求内 (block_id, treatment) 不得重复；含水率/更正只接受已导入的小区。
误差设定：`gain_error` ∈ [0, 1)；`weight_error`、`moisture_error` ≥ 0；
只接受已导入小区；核算时设定必须覆盖该发表参与区组的全部小区，否则 422。

## 项目结构

```
app/
  main.py       # FastAPI 路由、发布事务、误差设定与核算事务
  analysis.py   # 干物质、组内差、精确随机化检验、误差上下界（纯函数）
  db.py         # SQLite 连接、schema、data_version、误差设定版本
  schemas.py    # Pydantic 请求/响应模型
examples/       # 示例数据（导入、含水率、迟到含水率、更正、误差设定）
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
同版来源校验、无操作改动不误判过期、参数校验与 404；以及误差核算全流程
（设定版本化/no-op、两类过期原因、更正不改旧报告与旧发表、失败回滚不覆盖旧版、
404/422 路径）、共用增益与独立误差的正负号判定、重量非负与含水率 [0,1] 截断，
并用密集 g 网格 + 20 万次随机内部抽样独立复核上下界的精确性与可达性。
