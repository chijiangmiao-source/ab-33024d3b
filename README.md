# 轨道载荷升级服务（双槽断电安全）

维护员在升级轨道载荷启动镜像时，需要保证：**任意断电都不会让设备引导到
摘要与清单不符的候选镜像，也不会在新版本生效后回退到旧版本**。本服务提供
双槽（A/B）升级流水线、断电故障注入、恢复裁决依据展示，以及同一确认代次
下的并发升级仲裁。

## 核心规则

- **持久化**：槽位清单、候选阶段（`written/verified/confirmed`）、确认代次
  （`generation`）均同步落盘（SQLite WAL），断电只丢失未提交操作。
- **恢复裁决**：每次打开设备视图（`GET /api/devices/{id}`）都基于持久化
  清单实时裁决——只有「摘要完整 **且** 已确认」的清单具备引导资格，且必须
  **唯一**；未确认候选、损坏候选（保留诊断证据）、已被取代的旧清单一律
  不得引导（禁止回退）。
- **并发仲裁**：提交候选与确认切换都携带 `expected_generation` 做比较并
  交换。两个页面并发提交不同候选时，只有一个请求取得当前代次的升级资格，
  另一个得到稳定的 `409`（`CANDIDATE_IN_PROGRESS` / `GENERATION_CONFLICT`），
  且不得改写活动版本。

## 快速开始（Compose）

```bash
# 启动服务（宿主机端口默认 8080，可用 APP_PORT 覆盖）
APP_PORT=9000 docker compose up --build app

# 健康检查
curl http://localhost:9000/healthz

# 一次性验收：代码测试 + 页面构建 + 断电恢复/并发裁决 HTTP 冒烟
# 服务执行完毕自行退出，退出码即验收结果
docker compose up --build --exit-code-from verify verify
# 或
docker compose run --rm verify
```

打开页面：`http://localhost:8080/`（或 `APP_PORT` 指定的端口）。

## 本地开发

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python tools/build_page.py     # 构建页面到 frontend/dist
.venv/bin/uvicorn app.main:app --port 8000
.venv/bin/pytest -q                      # 代码测试
APP_BASE_URL=http://127.0.0.1:8000 .venv/bin/python -m verify.acceptance
```

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 健康响应 |
| POST | `/api/devices` | 创建双槽设备（当前版本 + 镜像，摘要由服务计算） |
| GET | `/api/devices` / `/api/devices/{id}` | 设备列表 / 设备视图（含恢复裁决依据） |
| POST | `/api/devices/{id}/candidates` | 提交更高版本候选（携带 `expected_generation`） |
| POST | `/api/devices/{id}/verify` | 摘要校验（不符则封存为 `corrupt` 并留证） |
| POST | `/api/devices/{id}/confirm` | 确认切换（原子翻转活动槽位、推进确认代次） |
| POST | `/api/devices/{id}/power-loss` | 模拟断电（可选 `corrupt_candidate` 撕裂写入） |
| GET | `/api/devices/{id}/events` | 诊断事件日志（断电、损坏、切换等证据） |

错误均为稳定结构：`{"error": {"code": "...", "message": "..."}}`，冲突为
`409`，未知设备为 `404`。

## 目录结构

```
app/            FastAPI 服务（store 持久化 / recovery 裁决 / main 路由）
frontend/src    页面源码（构建后内联为 frontend/dist/index.html）
tools/          页面构建脚本
tests/          pytest：恢复裁决、断电场景、并发仲裁
verify/         一次性验收服务（Compose 中的 verify）
```
