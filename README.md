# 地质勘探数据管理平台

面向地质勘探的钻孔编录、岩心取样、物探数据、化探分析、测绘资料与储量估算的综合数据管理后台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 钻孔编录 | `borehole` | 钻孔 | 钻孔编号、勘探区、孔口坐标、历史别名 |
| 岩心管理 | `core` | 岩心样本 | 岩心编号、所属钻孔、取样深度起 |
| 地层划分 | `stratigraphy` | 地层单元 | 单元编号、钻孔编号、地层名称 |
| 地球物理 | `geophysics` | 物探测线 | 测线编号、勘探区、物探方法 |
| 化探分析 | `geochem` | 化探样品 | 样品编号、样品类型、采样点位 |
| 化验数据 | `assay` | 化验结果 | 化验编号、样品编号、元素名称 |
| 地质填图 | `mapping` | 填图单元 | 图幅编号、图幅名称、比例尺 |
| 测绘控制 | `survey_point` | 控制点 | 点号、点类型、坐标X |
| 钻探日志 | `drilling_log` | 钻探记录 | 日志编号、钻孔编号、钻进深度 |
| 储量估算 | `reserve` | 矿体块段 | 块段编号、矿体名称、面积 |
| 样品登记 | `sample_registry` | 送检样品 | 送检编号、样品名称、采样位置 |
| 勘探设备 | `equipment` | 勘探仪器 | 仪器编号、仪器名称、型号规格 |
| 水文地质 | `hydro` | 水文观测点 | 观测编号、观测类型、所在钻孔 |
| 剖面编录 | `section` | 实测剖面 | 剖面编号、剖面名称、剖面长度 |
| 地质报告 | `geological_report` | 勘探报告 | 报告编号、勘探区、报告类型 |
| 遥感解译 | `remote` | 遥感数据 | 数据编号、数据源、分辨率 |
| 矿产评价 | `mineral` | 矿化线索 | 线索编号、勘探区、矿种 |
| 环境地质 | `environmental` | 环境调查点 | 调查编号、调查区域、灾害类型 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 钻孔编录：分批入账闸门

钻孔清单（CSV/TSV，UTF-8）不是逐行导入，而是先过「分批入账闸门」，规则集中在
`backend/app/services/borehole_import_gate.py`：

1. **冲突预检**：上传清单先按勘探区与孔口坐标预检，孔号身份同时比对现行孔号与
   「历史别名」；任一孔号命中存量记录、坐标与存量重合、或文件内孔号/坐标重复，
   都算本轮不通过。
2. **整批原子**：只有本轮全部行通过才一次性落库；任一冲突就整批退至暂存文件
   （`backend/var/staging/`，可用环境变量 `BOREHOLE_STAGING_DIR` 覆盖），
   不允许只导入一半。落库结果同步回写钻孔编录台账与编录待办清单。
3. **指纹幂等**：同一文件按 SHA-256 字节指纹去重，重复上传只返回首次批次结果，
   绝不生成第二份台账（已入账的提示已入账，暂存中的提示修复后续走）。
4. **历史别名迁移**：启动时把早期孔号（见 `app/seed.py` 的
   `LEGACY_BOREHOLE_ALIASES`）幂等补进存量记录的「历史别名」。导入行命中别名时
   视为同一孔；坐标与存量不一致时，以勾选「现场确认」的孔口坐标为准覆盖。
5. **断点续解析**：逐行解析遇到第一个坏行立即中断并暂存，修复后从失败那一行接着走；
   续走时坐标一律以当前文件内容重新归一，不会拿旧坐标顶替。

导入清单表头固定为：

```text
钻孔编号,勘探区,孔口坐标,现场确认,历史别名,设计孔深,终孔深度,开孔日期,终孔日期
```

相关接口：`POST /api/borehole/imports`（上传过闸）、`GET /api/borehole/imports`
（批次台账）、`GET /api/borehole/imports/{批次号}`（暂存明细）、
`POST /api/borehole/imports/{批次号}/resume`（修复续走）、
`GET /api/borehole/todos`（编录待办）、`POST /api/borehole/todos/{id}/actions`。

后端测试：`cd backend && python -m pytest tests/`（覆盖整批退闸、指纹幂等、
断点续走、别名坐标裁决等 18 个用例）。
