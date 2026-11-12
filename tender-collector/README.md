# 市政工程投标截止时间采集服务

面向招投标专员的公告采集 API：提交市政工程公告站的**列表页地址**，服务自动遍历
所有分页与详情页，将**更正公告按其引用的原公告编号归并**到同一项目，并**按发布
时间**（而非页面排列顺序）确定当前生效的投标截止时间，标记是否已过期。

## 核心规则

1. **分页遍历**：从提交的列表页出发，沿 `nav.pagination` 链接抓取全部列表页
   （去重、防环、限量），收集所有详情页链接后逐页抓取。
2. **更正归并**：更正公告通过"原公告编号"归并到对应原公告所在项目；引用悬空
   （原公告不存在/不是原公告）视为数据错误，本次采集整体失败。
3. **生效截止时间**：同一项目内按**发布时间**排序，取发布时间最新且声明了
   截止时间的公告；发布时间相同时按公告编号确定次序，结果与页面顺序完全无关。
   若最新更正公告未给出截止时间（例如只改联系人），沿用上一份带截止时间的公告。
4. **过期判定**：`is_expired = 截止时间 <= 当前时间`（截止时间按服务器本地时区
   的 naive 时间比较，与公告页 `YYYY-MM-DD HH:MM` 格式一致）。
5. **并行抓取**：列表页按分页顺序遍历；收集完详情链接后**并行**抓取详情页
   （`TENDER_FETCH_CONCURRENCY` 控制并发数），任一页面失败仍整体失败。
6. **并发发布顺序**（同一入口 `list_url` 内）：
   - 每一轮采集开始时分配单调递增的轮次号 `seq`；
   - **后发起且先成功**的轮次立即发布、立即可查；
   - 旧轮随后完成时不得覆盖它：本轮结果完整但发现已有更晚发起且已发布的
     轮次，接口返回 **409**（`detail.status = "unpublished"`），运行记录标记
     `status=unpublished / published=0`，公告、来源与生效轮次一概不回写；
   - 后轮抓取/归并失败时，**先发起且完整成功**的轮次仍可正常发布；
   - 公告快照、来源状态（`source_state`）与生效轮次在同一事务内一起更新；
     失败或未发布轮次不留下任何公告片段。
7. **幂等采集**：公告以编号为唯一键 UPSERT，重复采集不产生重复记录；每次发布
   以本轮结果替换该入口的公告快照，源站已消失的公告不会残留。
8. **失败保留**：任一列表页/详情页读取失败（网络错误、非 2xx、字段缺失）或
   归并校验失败时，本次采集不写入任何公告数据，**完整保留上次成功采集结果**；
   失败原因记入 `collection_runs` 表并通过 HTTP 422 返回。

## 目录结构

```
tender-collector/
├── app/                    # FastAPI 应用
│   ├── main.py             # 路由：/api/collect、/api/projects、/api/collections、/api/sources
│   ├── service.py          # 采集编排 + 轮次发布 + 过期判定
│   ├── scraper.py          # httpx + BeautifulSoup 抓取/解析（详情页并行）
│   ├── merge.py            # 更正归并与生效截止时间判定（纯函数）
│   ├── database.py         # SQLite 连接、建表、事务发布、兼容迁移
│   ├── demo_server.py      # 本地可控示例站（慢页/故障页/内容覆盖）
│   ├── models.py           # Pydantic 模型
│   └── config.py           # 环境变量配置
├── demo_site/              # 生成的本地分页 HTML 示例公告站（静态文件）
├── scripts/generate_demo_site.py  # 示例站生成器（数据可复现）
├── scripts/demo_concurrency.py    # 离线并发发布顺序演示（无需 Docker）
├── tests/                  # 端到端、并发发布顺序、发布完整性测试
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 快速开始（Docker Compose）

```bash
# 1. 生成示例公告站静态文件（已随仓库提供，可重复生成）
python3 scripts/generate_demo_site.py

# 2. 启动：API 在 :8000，示例公告站在 :8001
docker compose up --build -d

# 3. 提交列表页地址触发采集（容器内 API 通过 compose 网络访问示例站）
curl -X POST http://localhost:8000/api/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://demo-site:8100/list_1.html"}'

# 4. 查询项目当前截止时间
curl http://localhost:8000/api/projects
```

示例站容器（`demo-site`）托管 `demo_site/` 并提供 `/__control` 控制接口；
从宿主机浏览示例站打开 <http://localhost:8001/list_1.html>。SQLite 数据存于
命名卷 `tender-data`（容器内 `/data/tenders.db`）。

演示并发发布顺序（在宿主机用一条命令跑完两个场景，示例站使用可控慢页）：

```bash
python3 scripts/demo_concurrency.py
```

该脚本会启动可控示例站（默认端口 8765）、用临时 SQLite 库启动 API，并演示：
1. 旧轮读取慢页期间页面更新，后发起轮次先成功并立即可查；旧轮结束时返回 409
   （`unpublished`），查询仍是新截标时间；
2. 后发起轮次遇到故障页失败，先发起且完整成功的轮次仍正常发布。

停止与清理：

```bash
docker compose down          # 停止（保留数据卷）
docker compose down -v       # 停止并删除采集数据
```

## 离线演示（不使用 Docker）

```bash
python3 -m pip install -r requirements-dev.txt
python3 scripts/generate_demo_site.py

# 一条命令跑完两个并发发布场景（临时 DB，不污染本地数据）
python3 scripts/demo_concurrency.py

# 手动演示：终端 A 启动可控示例站（含慢页/故障页接口）
python3 -m app.demo_server --directory demo_site --port 8001

# 终端 B：API（数据库默认在 ./data/tenders.db）
uvicorn app.main:app --reload --port 8000

# 终端 C：测试
pytest -q

# 采集（宿主机访问示例站）
curl -X POST http://localhost:8000/api/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://127.0.0.1:8001/list_1.html"}'

# 慢页/故障页/内容覆盖（模拟"上一轮还在读旧页面"）
curl -X POST http://127.0.0.1:8001/__control/slow \
  -H 'Content-Type: application/json' \
  -d '{"path": "/SZ-2026-005-G1.html", "delay": 3.0, "times": 1}'
curl -X POST http://127.0.0.1:8001/__control/fail \
  -H 'Content-Type: application/json' \
  -d '{"path": "/SZ-2026-002-G2.html", "status": 500}'
curl -X POST http://127.0.0.1:8001/__control/override \
  -H 'Content-Type: application/json' \
  -d '{"path": "/SZ-2026-005-G1.html", "field": "投标截止时间", "value": "2026-12-11 15:00"}'
curl http://127.0.0.1:8001/__control/status
```

交互式 API 文档（Swagger UI）：<http://localhost:8000/docs>

## API 说明

### `POST /api/collect` — 提交列表页，触发采集

请求：

```json
{ "list_url": "http://demo-site:8100/list_1.html" }
```

成功响应（200）：

```json
{
  "status": "success",
  "source_url": "http://demo-site:8100/list_1.html",
  "run_id": 2,
  "seq": 2,
  "published": true,
  "published_run_id": 2,
  "collected": 10,
  "projects": [
    {
      "project_name": "城北污水处理厂扩建工程",
      "current_deadline": "2026-11-30 10:00",
      "deadline_source_code": "SZ-2026-002-G2",
      "deadline_source_url": "http://demo-site:8100/SZ-2026-002-G2.html",
      "is_expired": false
    }
  ]
}
```

失败响应（422，任一页面不可读/数据无法归并；本轮未发布）：

```json
{ "detail": "读取页面失败 http://demo-site:8100/SZ-2026-002-G2.html: HTTP 404" }
```

未发布响应（409，本轮采集完整成功，但同一入口已有后发起且已成功的轮次生效）：

```json
{
  "detail": {
    "status": "unpublished",
    "message": "本轮采集已完整完成，但同一入口已有后发起且已成功的轮次生效，本轮未发布（run_id=1, seq=1）",
    "run_id": 1,
    "seq": 1,
    "published_run_id": 2
  }
}
```

### `GET /api/projects` — 查询全部项目的当前截止时间

返回字段：项目名称、当前生效截止时间、截止时间来源公告编号、来源链接、是否过期。

### `GET /api/collections?limit=20` — 采集运行历史

包含每轮的 `seq`、`status`（`success` / `failed` / `unpublished`）、`published`
标志、采集条数与错误信息。

### `GET /api/sources` — 每个入口当前生效的轮次

返回 `source_url`、`published_run_id`、`published_seq`、`published_at`。

## 示例站数据设计

`demo_site/` 含 5 个项目、10 条公告、3 个列表页，列表按发布时间**倒序**排列
（更正公告排在原公告之前），专门用于验证归并不受页面顺序影响：

| 项目 | 场景 | 生效截止时间来源 |
| --- | --- | --- |
| 滨河路市政改造工程 | 一次更正延期 | 更正公告 SZ-2026-001-G1 |
| 城北污水处理厂扩建工程 | 两次更正，取最新一次 | 第二次更正 SZ-2026-002-G2 |
| 东风桥加固维修工程 | 无更正，已过期 | 原公告 SZ-2026-003 |
| 高新区雨污分流管网工程 | 更正不改截止时间 | 原公告 SZ-2026-004 |
| 湖滨公园配套市政道路工程 | 原公告在末页、更正在首页（跨页乱序） | 更正公告 SZ-2026-005-G1 |

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TENDER_DB_PATH` | 本地 `./data/tenders.db`；容器内 `/data/tenders.db` | SQLite 文件路径 |
| `TENDER_HTTP_TIMEOUT` | `15` | 单个页面请求超时（秒） |
| `TENDER_MAX_LIST_PAGES` | `200` | 单次采集最多遍历的列表页数（防环） |
| `TENDER_FETCH_CONCURRENCY` | `8` | 详情页并行抓取的最大并发数 |
| `TENDER_USER_AGENT` | `TenderCollector/1.0 ...` | 请求 UA |

## 解析约定（接入真实站点时）

解析器依赖以下稳定选择器，适配其他站点时调整 `app/scraper.py` 即可：

- 列表页：`ul.announcement-list a.detail-link` 为详情链接，
  `nav.pagination a` 为分页链接；
- 详情页：`article.announcement-detail` 容器，支持
  `data-code` / `data-kind` / `data-published-at` / `data-corrects` 属性，
  以及 `table.detail-table` 内 `th[data-field="字段名"] + td` 表格字段
  （公告编号、公告类型、项目名称、发布时间、投标截止时间、原公告编号）。
