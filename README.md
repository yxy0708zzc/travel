# travel2 —— 高铁购票大模型测评流水线（纯 CLI）

从 12306 数据采集到 AI 做题、评测的全流程 Benchmark，拆分为 **5 个独立命令行脚本**，
每个模块的输入输出路径全部由命令行参数显式指定，可插拔、可独立重跑。
（本项目由 benchmark_travelplan 迁移重构而来：算法语义 1:1 复刻，工程架构重新设计——
无前端、无 Web 服务、无隐式路径。）

## 数据流

```
12306 ──step1──▶ railway.db + prices.db ──step1 cleanup/overnight──▶ 清洗后的数据
                        │
                        ▼ (step2 读)
        1.xlsx 分布表 + 2.xlsx 站对表 ──step2──▶ question/*.db + metadata.json
                        │                       （出题产物，step3 后归档只读）
                        ▼ (step3 读)
                 step3 并发调 LLM ──▶ metadata_nl.json（题面+标答快照+state；
                        │               step3 之后唯一元数据事实源）
                        ▼ (step4 读事实源，运行时查三个库)
        被测模型 ◀─工具调用─ step4 对话循环 ──▶ logs/test/*.json（轨迹+统计）
                        │
                        ▼ (step5 eval)
                 verifier 代码核查 ──▶ logs/result/*.json（verdict+问题清单）
                        │
                        ▼ (step5 stats / report)
                    统计汇总 + 排名报告（logs/report/）
```

## 目录结构

```
travel2/
├── common/            # 基础通用包（无业务逻辑）
│   ├── config.py      # .env 读取 + 调参常量（零路径）
│   ├── database.py    # SQLite/元数据读写（路径经 configure_paths 注入）
│   ├── prompts.py     # 被测模型系统提示词
│   └── xlsxio.py      # 分布表/站对表解析、失败回执生成
├── step1_collect.py   # ① 数据爬取与处理（tickets/prices/cleanup/overnight/check/all）
├── step2_generate.py  # ② 出题（012 三变体 × 五题型）
├── step3_nl.py        # ③ 题目自然语言化
├── step4_test.py      # ④ 测试（7 工具对话循环 + 轨迹落盘）
├── step5_eval.py      # ⑤ 测评与统计（eval/stats/report）
├── verifier.py        # 核查规则库（step5 调用；step2/4 复用其子函数）
├── templates/         # 1.xlsx 分布表模板 + 2.xlsx 站对表模板
├── docs/              # 五个模块的技术细节文档（认真读完再用）
└── .env.example       # API 配置模板（复制为 .env 填写）
```

## 安装

```bash
pip install -r requirements.txt   # requests + openpyxl
copy .env.example .env            # 填写 API Key / 模型 / Base URL
```

依赖极少：无 Web 框架、无 pydantic。所有 LLM 调用为 OpenAI 兼容
`POST {base_url}/chat/completions` 直连。

## 端到端快速开始

```bat
rem ① 采集数据（数小时量级，可分天用 --resume / --supplement 断点续爬）
python step1_collect.py tickets  --railway-db data\railway.db
python step1_collect.py prices   --railway-db data\railway.db --prices-db data\prices.db --workers 3
python step1_collect.py cleanup  --railway-db data\railway.db --prices-db data\prices.db
python step1_collect.py overnight --railway-db data\railway.db --prices-db data\prices.db --apply

rem ② 批量出题（模板见 templates/；座位权重和=100）
python step2_generate.py --railway-db data\railway.db --prices-db data\prices.db ^
    --question-dir question --distribution templates\1_题目分布表模板.xlsx ^
    --stations templates\2_到发站对表模板.xlsx ^
    --seat-weights 20,30,50 --interference-density 0.02 --random-tickets-density 0.02 ^
    --receipt 回执.xlsx

rem ③ 自然语言化（并发调 NL 模型）
python step3_nl.py --metadata question\metadata.json --metadata-nl question\metadata_nl.json --concurrency 5

rem ④ 测试被测模型
python step4_test.py --railway-db data\railway.db --prices-db data\prices.db ^
    --question-dir question --metadata-nl question\metadata_nl.json --logs-test logs\test ^
    --max-iterations 30 --concurrency 3 --list   & rem 先扫描
python step4_test.py ... --concurrency 3            & rem 确认后执行

rem ⑤ 测评 + 统计 + 报告
python step5_eval.py eval   --logs-test logs\test --logs-result logs\result ^
    --question-dir question --railway-db data\railway.db --prices-db data\prices.db
python step5_eval.py stats  --logs-result logs\result --logs-test logs\test ^
    --question-dir question --railway-db data\railway.db --prices-db data\prices.db
python step5_eval.py report --logs-result logs\result --logs-test logs\test ^
    --report-dir logs\report --question-dir question --railway-db data\railway.db --prices-db data\prices.db
```

## 参数说明（全部参数）

约定：标注 **必填** 的参数不传直接报错；「默认」为缺省值；路径一律显式指定，不依赖工作目录。

### step1_collect.py（子命令式：`python step1_collect.py <子命令> [参数]`）

**tickets** —— 采集余票基础库（车站/车次/经停 → 物化 station_trains → 同车映射 same_trains.json → 体检）

| 参数 | 必填 | 说明 |
|---|---|---|
| `--railway-db` | 必填 | 基础数据库路径。**全量重建**其中的 stations/trains/train_stops，并重建 station_trains；same_trains.json 写在其同目录 |

**prices** —— 采集票价库（相邻站对查询 + 非相邻段离线累加）

| 参数 | 必填 | 说明 |
|---|---|---|
| `--railway-db` | 必填 | 基础数据库（只读：trains 车次清单、train_stops 经停；same_trains.json 读其同目录） |
| `--prices-db` | 必填 | 票价数据库（读写）；失败明细 failed_pairs.log 也写在其同目录 |
| `--resume` | — | 断点续爬：跳过已爬车次；已爬车次校验站对完整性（DISTINCT 站对数 < n(n-1)/2 时删净重爬） |
| `--supplement` | — | 补缺模式：候选 = trains 全集 ∪ prices 已有号，只爬缺失的相邻站对，不删已有数据（与 --resume 择一） |
| `--train G1` | — | 仅爬指定车次 |
| `--workers N` | — | 并发线程数，默认 1，建议 3~5；每线程独立 Session 并同步主 Cookie |

**cleanup** —— 一键清理：同车多号去重 → 非相邻段补算 → 删仍不全车次 → 重建 station_trains

| 参数 | 必填 | 说明 |
|---|---|---|
| `--railway-db` / `--prices-db` | 必填 | 两库路径 |
| `--check` | — | 只读体检：仅打印待删清单与票价不全车次，不写任何数据（不加则执行真实清理） |

**overnight** —— 跨天车次清理（相邻经停时间倒退判定）

| 参数 | 必填 | 说明 |
|---|---|---|
| `--railway-db` / `--prices-db` | 必填 | 两库路径（删除会同时清理 prices、trains+train_stops、same_trains.json 并重建物化视图） |
| `--apply` | — | 加上才真正删除；默认仅显示跨天清单 |

**check** —— 只读体检（表计数 / 缺电报码 / 缺时间 / 跨天 / 票价概览 / 待去重）

| 参数 | 必填 | 说明 |
|---|---|---|
| `--railway-db` / `--prices-db` | 必填 | 两库路径 |

**all** —— 串行全流程 tickets → prices → cleanup（执行）→ overnight（执行）→ check

| 参数 | 必填 | 说明 |
|---|---|---|
| `--railway-db` / `--prices-db` | 必填 | 两库路径 |

### step2_generate.py —— 批量出题

| 参数 | 必填/默认 | 说明 |
|---|---|---|
| `--railway-db` | 必填 | 基础数据库（只读：候选车次、经停、站名解析与校验、干扰注入候选池） |
| `--prices-db` | 必填 | 票价数据库（只读；统一接口保留，出题核心不读价） |
| `--question-dir` | 必填 | 输出目录：生成 `{qid}.db` + metadata.json（临时文件 .tmp 也在其中） |
| `--metadata` | 默认 `{question-dir}/metadata.json` | 元数据文件路径覆盖 |
| `--distribution` | 必填 | 1.xlsx 题目分布表（模板见 templates/；含"总题目/存在性/选择性"三区与 13 种策略行） |
| `--stations` | 必填 | 2.xlsx 到发站对表，每行 `[出发站, 到达站]`（中文站名或电报码均可，自动解析） |
| `--seat-weights` | 默认 `20,30,50` | 特等/一等/二等 抽样权重百分比，3 个非负数且**和必须 =100**（如 `10,30,60`） |
| `--interference-density` | 默认 0.02 | 干扰密度（1_ 题）：全局站对池抽取比例，干扰票严格<人数保证唯一解；**0=关闭**，建议 (0, 0.05] |
| `--random-tickets-density` | 默认 0.02 | 随机票密度（2_ 题）：票数 0.5~1.5×人数产生多解；**0=关闭** |
| `--max-retries` | 默认 40 | 每题重试上限；每次重试重新随机站对/方向/人数(3~6)/座位 |
| `--receipt` | — | 失败回执 xlsx 输出路径（与 1.xlsx 同构，各格=该行失败数） |

### step3_nl.py —— 题目自然语言化

| 参数 | 必填/默认 | 说明 |
|---|---|---|
| `--metadata` | 必填 | 题目元数据路径（读；输出不含它） |
| `--metadata-nl` | 必填 | NL 产物路径（写；每题成功立即落盘，中断不丢） |
| `--ids q1 q2 ...` | — | 仅处理指定题目；存在性配对组（0_/1_）整组处理 |
| `--force` | — | 强制重新生成已有 nl_question 的组（默认跳过） |
| `--concurrency` | 默认 5 | 批量模式并发数，1~20 |
| `--interactive` | — | 交互式逐组确认（[Enter]保存 [x]重生成 [n]跳过）；默认为非交互批量并发 |
| `--api-key` | — | LLM Key；未填读 .env `NL_API_KEY` |
| `--model` | — | 模型名；未填读 .env `NL_MODEL` → `DEFAULT_MODEL` |
| `--base-url` | — | OpenAI 兼容接口地址；未填读 .env `NL_BASE_URL` → `DEFAULT_BASE_URL` |
| `--env` | — | .env 路径（默认脚本目录下的 .env） |

### step4_test.py —— 测试被测模型

| 参数 | 必填/默认 | 说明 |
|---|---|---|
| `--railway-db` | 必填 | 基础数据库（只读；5 个查询工具的数据源） |
| `--prices-db` | 必填 | 票价数据库（只读；query_ticket_price 工具） |
| `--question-dir` | 必填 | 题目目录（query_tickets 工具读当前题 `{qid}.db`） |
| `--metadata-nl` | 必填 | 元数据事实源（metadata_nl.json：题面/标答快照/state；测试成功写回 state=tested） |
| `--logs-test` | 必填 | 测试记录输出目录（`{时间戳}_{model}_{qid}.json`；模型名中 `/`、`:` 等非法字符替换为 `_`） |
| `--model` | — | 被测模型名；未填读 .env `TEST_MODEL` → `DEFAULT_MODEL` |
| `--api-key` | — | 未填读 .env `TEST_API_KEY` |
| `--base-url` | — | 未填读 .env `TEST_BASE_URL` → `DEFAULT_BASE_URL` |
| `--ids q1 q2 ...` | — | 仅测指定题目（默认全部可测题目）；可测 = 有题面+nl 且该模型未测过 |
| `--max-iterations` | 默认 30 | 最大对话轮数（每轮 = 一次模型 API 请求，轮内可并行多工具），1~100 |
| `--concurrency` | 默认 1 | 并发数 1~8（每题独立会话，题目上下文按调用传递不串题）；支持 **Ctrl+C 优雅停止**：在跑的题测完并落盘，未开始的不启动；重跑同命令自动续测（已测过的题被 state 去重跳过） |
| `--list` | — | 只打印可测清单（题号/类型/可测性/原因），不执行测试 |
| `--env` | — | .env 路径 |

### step5_eval.py（子命令式，先公共参数再子命令）

公共参数（三个子命令通用）：

| 参数 | 必填/默认 | 说明 |
|---|---|---|
| `--railway-db` | 必填 | 基础数据库（只读；核查经停顺序/站名映射） |
| `--prices-db` | 必填 | 票价数据库（只读；price_wrong / price_missing 核查） |
| `--question-dir` | 必填 | 题目目录（核查读 `{qid}.db` 余票、metadata 读标答） |
| `--metadata-nl` | 必填 | 元数据事实源（metadata_nl.json：标答快照/题面/state） |
| `--env` | — | .env 路径 |

**eval** —— 批量测评（测试记录 → 代码核查 → 结果落盘 + state=evaluated）

| 参数 | 必填 | 说明 |
|---|---|---|
| `--logs-test` | 必填 | 测试记录目录（输入） |
| `--logs-result` | 必填 | 测评结果目录（输出） |
| `--ids f1.json f2.json` | — | 仅测评指定记录文件名 |
| `--re-evaluate` | — | 已测评的记录也重新测评（默认跳过）；重测成功后自动删除该记录的旧结果，不重复计数 |

**stats** —— 按模型聚合统计

| 参数 | 必填 | 说明 |
|---|---|---|
| `--logs-result` | 必填 | 测评结果目录（输入） |
| `--logs-test` | 必填 | 测试记录目录（token/耗时/模型与工具调用数溯源） |

**report** —— 排名与题型对比报告（JSON + Markdown，JSON 只保留最近 50 份）

| 参数 | 必填 | 说明 |
|---|---|---|
| `--logs-result` | 必填 | 测评结果目录（输入） |
| `--logs-test` | 必填 | 测试记录目录（输入） |
| `--report-dir` | 必填 | 报告输出目录（summary_时间戳.json/.md） |

API 配置优先级：命令行显式参数 > `.env`（`TEST_*`/`NL_*` → `DEFAULT_*` 兜底）。
缺失直接报错，系统不默认指向任何平台。

## 核心概念（详见 docs/）

- **012 题型体系**：`0_` 存在性·无注入（唯一解）/ `1_` 存在性·干扰（票数严格<人数，
  唯一解）/ `2_` 选择性·随机票（0.5~1.5×人数，多解）。五题型：换乘 / 买短补长 /
  前额外 / 后额外 / 混合。
- **state 流水线**：`metadata_nl.json[qid]["state"] = ["模型:tested", "模型:evaluated", ...]`，
  只记成功；成功后同模型不可重测（step4/step5 自动去重）。
- **verdict 五档**：pass / hallucination / no_plan / empty_plan / db_not_found。
  空方案按测试记录 plan_status 细分：模型明确输出空数组（无解）= empty_plan；
  未输出 final_plan JSON = no_plan。
- **判定口径**：存在性题要求与唯一标答完全一致（提交段逐段比对 + 标答段反向
  覆盖检查，漏段按错）；选择性题要求全程可达 + 票真价对 + 座位等级符合题目要求。

## 注意事项

1. **不要并行运行 step2 / step3 / step4 / step5 eval**：step2/3 写 metadata.json、
   step3/4/5 写 metadata_nl.json（进程内有锁，跨进程没有）。step1 与纯读操作
   （`--list`、`stats`）可并行。
2. 采集与反爬：12306 有风控，务必遵守默认限速（限速器已加锁，多线程下全局间隔
   不被击穿）；`prices` 支持 Ctrl+C 优雅退出（按一次等在途请求收尾，再按两次
   强制退出）；`all` 流程中 tickets 阶段同样响应中断（当前车次完成后停止，
   已入库数据保留）。
3. **双文件职责（线性数据流）**：`metadata.json` 是 step2 出题产物，step3 读取后
   即**归档只读**；`metadata_nl.json` 是 step3 之后的**唯一元数据事实源**（每题 =
   标答快照 + nl_question + state），step4/5 只读写它。手工操作认准文件：
   改题面/标答 → 改主文件后重跑 step3（重建快照）；清测试记忆 → 删 `_nl` 中
   对应条目的 `state` 字段。
4. 题目库 `question/{qid}.db` 中 class0/1/2 三表为**稀疏存储**：没有行 = 0 张票。
5. 上传 GitHub：数据目录（data/ question/ logs/）与 .env 已在 .gitignore 中排除；
   templates/ 与 docs/ 应一并提交。

## 文档索引

| 文档 | 内容 |
|---|---|
| docs/01_collect.md | 采集与清洗：数据库 schema、爬虫策略、清理算法 |
| docs/02_generate.md | 出题：参数、题型算法、干扰注入、自检、metadata 契约 |
| docs/03_nl.md | 自然语言化：提示词、分组规则、并发模型 |
| docs/04_test.md | 测试：工具定义、对话循环、token 口径、记录结构 |
| docs/05_eval.md | 测评：核查判定体系、问题码全表、统计口径 |
