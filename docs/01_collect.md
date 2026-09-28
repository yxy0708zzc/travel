# 01 数据爬取与处理（step1_collect.py）

负责 benchmark 的"地基"：从 12306 采集余票与票价数据，清洗到可出题状态，
并对数据完整性做体检。所有子命令均为命令行工具，输入输出路径显式指定。

## 1. 子命令总览

| 子命令 | 功能 | 写入目标 |
|---|---|---|
| `tickets` | 采集车站/车次/经停站，物化 station_trains，生成同车多号映射 | railway.db、same_trains.json |
| `prices` | 采集相邻站对票价 + 离线累加非相邻段 | prices.db、failed_pairs.log |
| `cleanup` | 同车多号去重 → 非相邻段补算 → 删仍不全车次 → 重建物化视图（`--check` 只读） | 两库 |
| `overnight` | 跨天车次清理（`--apply` 才删除） | 两库 + same_trains.json |
| `check` | 只读体检 | 无 |
| `all` | 串行执行以上五步 | — |

## 2. 数据库 schema

### railway.db（基础库，出题/测试只读）

| 表 | 字段 | 说明 |
|---|---|---|
| `stations` | station_id (PK), station_name | 电报码 ↔ 站名。采集期允许 `@站名` 形式的自动注册 ID |
| `trains` | train_num (PK), train_no (UNIQUE) | 车次号 ↔ 12306 内部 train_no |
| `train_stops` | id, train_num, stop_no, station_id, station_name, stop_time | 经停站；`UNIQUE(train_num, stop_no)`；stop_time 恒为 HH:MM 或空串 |
| `station_trains` | station_id (PK), station_name, data(JSON) | 物化视图：每站经停车次列表 `[{train_num, stop_time}]` |

索引：`train_stops(train_num)`、`train_stops(station_id)`。

### prices.db（票价库，出题/测试/核查只读）

| 表 | 字段 | 说明 |
|---|---|---|
| `prices` | id, train_num, from_station_id, to_station_id, seat, price, crawl_date | `UNIQUE(train_num, from, to, seat)`；**crawl_date NOT NULL**（任何 INSERT 必须带）；price 单位元（接口角值 ÷10） |

seat ∈ {class2 二等座, class1 一等座, class0 特等座}（普速车按 硬座/硬卧/软卧 映射入库）。

## 3. tickets 采集算法

1. **车站列表**：拉取 `station_name.js`，正则提取 `var station_names='...'`，
   按 `@` 切分、`|` 取第 2/3 列（站名/电报码）。站名做**去非汉字归一化**
   （`_norm_station`）后建立 name→id 映射，规避 12306 站名中的空格/符号。
   写库顺序：先删 station_trains → train_stops → stations（外键依赖序）。
2. **车次枚举**：`search.12306.cn/search/v1/train/search?keyword=G{prefix}`，
   枚举 G10–G99（部分匹配覆盖全部 G 车）+ G1–G9 精确补录；取 `station_train_code`
   与 `train_no` 写入 trains（先清空）。
3. **经停站**：`/otn/czxx/queryByTrainNo`，日期用基准日（未来第 3 天）。
   - 时间字段按 arrive_time → depart_time → start_time → begin_time 顺序取**首个
     合法 HH:MM**；全部无效则留空串——**绝不写入 "--" 占位符脏数据**，
     由体检与跨天清理的 HH:MM 校验兜住。
   - 站名经归一化映射为电报码；映射失败自动注册 `@{站名}`（INSERT OR IGNORE，
     不覆盖合法映射），防止外键失败。
   - 经停 <2 站的车次：记入失败清单并**从 trains 删除**（下游不会读到空经停）。
   - 批量写入期间 `PRAGMA foreign_keys=OFF`；结束时**先 rollback 结束未决事务
     再恢复 ON**（活动事务内的 PRAGMA 会被 SQLite 静默忽略——这是历史踩坑）。
4. **物化 station_trains**：从 train_stops 聚合（直接用 ts.station_name，避免 JOIN
   丢无 stations 记录的车站）。
5. **同车多号映射**：经停序列（站名:时刻 拼接）完全一致的车次视为同一物理列车。
   **任一站缺有效 HH:MM 的车次不参与分组**——否则 key 退化为纯站名序列，会把
   "经停相同但时刻不同"的合法车误合并，随后被去重整车误删。产物 `same_trains.json`
   （与 railway.db 同目录），票价爬取用它做同组车次轮替。

## 4. prices 采集算法

- 查询接口 `queryAllPublicPrice`（日期=未来第 3/9/13 天候选，失败逐天后移）。
- **席别映射**：车型为高速/动车用 ze/zy/swz → class2/1/0；其余车型先试
  yz/yw/rw（硬座/硬卧/软卧），**全空时再按 G 映射兜底探一次**——防止车型字段
  异常导致整车查价全空、被误判全失败。
- **站对重试矩阵**（每个相邻站对）：从当前生效日期起逐天尝试 × 3 轮 ×
  同组车次各 5 次。同组车次任一查到即成功；成功车次记为 effective（下次优先），
  成功日期记为当前生效日期（后续站对先用最可能成功的日期）。
- **全站对失败**：全量模式把该车剩余站对如实写入 failed_pairs.log（"未爬取"）
  并放弃整车；补充模式只计实际尝试失败的数量。
- **非相邻段累加**：`_compute_train_pairs`——相邻段齐全时，对每个席别把
  A→C、A→D… 票价按相邻段求和写入（`crawl_date=今天`）。相邻段不齐直接跳过。
- **断点续爬 `--resume`**：已爬车次校验 `DISTINCT 站对数 ≥ n(n-1)/2`，
  不全的自动重爬（先 DELETE 该车全部票价再重爬，保证无残留半成品）。
- **补缺 `--supplement`**：候选集 = **trains 全集 ∪ prices 已有号**（零票价记录
  的车次也能被扫到补爬——否则会被 cleanup 一键流程整车误删），只爬缺失相邻站对。
- **并发**：`--workers`，每线程独立 Session 并**同步主进程 Cookie**
  （只复制 headers 会导致并发线程大面积查询失败）；统计由锁保护。
- **优雅退出**：Ctrl+C 第一次置停止标志（在途请求完成后终止、已爬数据已提交），
  第二次强制退出。failed_pairs.log 写在 prices-db 同目录。

反爬与限速（两爬虫共用 `HttpBase`）：
- 相邻请求最小间隔 0.15s（monotonic 时钟限速器）；
- 反爬判定：HTTP 403/429，或 200 但响应体 <1KB 且含关键词（验证码/频繁/风控等，
  正常页面 ≥1KB）；
- 退避：反爬休眠 10–20s×尝试次数；非 200 与网络异常休眠 min(6s, 1s×次数)+随机抖动；
  单请求最多重试 3 次。

## 5. cleanup 清理算法（一键流程）

```
① 同车多号去重      → ② 非相邻段补算 → ③ 删除仍不全车次 → ④ 重建 station_trains
```

- **去重**：每组（same_trains.json）保留一个**主号**——在 trains 表中、票价记录
  最多者（并列取字典序最小）；其余为重复号。**纯孤儿**（在 train_stops 但不在
  trains 且不属于任何同车组）一并删除。删除 = 从 prices、trains+train_stops、
  same_trains.json（剩余 <2 的组整组移除）三处全量清除。
- **补算**：对票价不全车次离线累加非相邻段（相邻段不齐的补不动，留给 supplement）。
- **删除仍不全者**（补算后复查）：**零票价记录保护**——prices 完全无行的车次
  是"全量爬取时全部站对失败"而非"爬到了但不全"，跳过删除并提示补爬，
  避免把辛辛苦苦爬到的经停数据连坐删掉。
- `--check`：只读体检，输出待删清单与票价不全车次，不写任何数据。

## 6. overnight 跨天车清理

- 判定：相邻经停站按**下一个实际存在的 stop_no** 配对（容忍断号），
  双侧时间均为合法 HH:MM（脏数据如 `--` 不参与比较，防字典序误报），
  且前站时刻 > 后站时刻（如 23:20 → 00:07）→ 跨天。
- 依据"当日完成约束"（见 docs/05），跨天车次不可用于出题，需删除。
- 默认仅显示清单；`--apply` 才执行删除（复用 cleanup 的全量删除），
  删除后重建物化视图并复查归零。

## 7. check 体检项

基础库：四表计数、缺电报码数、缺经停时间数；票价库：有票价车次数、票价记录数；
问题清单：待去重数、跨天车次数。全部只读。

## 8. 常见问题

- **重爬后需要做什么**：重跑 tickets 会重建全部表与 same_trains.json；
  之后必须重跑 cleanup（12306 重爬后同车双号会再现）。
- **@前缀站**：合法数据，是采集期自动注册的未知车站，出题/测试不受影响。
- **某车次票价永远失败**：查 failed_pairs.log；若整车无数据可能是该车当日停运，
  换 `query_date_days_list` 中的其他日期或等待下一采集窗口。
