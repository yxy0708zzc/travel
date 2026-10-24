# 02 出题（step2_generate.py）

按「1.xlsx 题目分布表 + 2.xlsx 到发站对表」批量生成题目库与元数据。
出题算法语义与参考项目逐条对齐：012 前缀三变体、五种购票策略题型、
全局干扰注入、落盘前自检、原子落盘。

## 1. 输入

### 1.xlsx 分布表（模板见 templates/）

| 区域 | 结构 | 含义 |
|---|---|---|
| 顶部 | `总题目 | N` | 计划总量（仅展示） |
| 存在性区 | 单策略/双策略/三策略 × 13 种策略行，两列数量 | 有干扰数 → `1_`；无干扰数 → `0_` |
| 选择性区 | 评判标准（5 种）× 行为约束（随意/不允许换乘/不允许买短补长与额外购买）× 数量 | 数量 → `2_` |

13 种策略 → 题型映射：单策略（换乘/买短补长/额外购买（前）/（后））；
双策略 = 直达 + 指定策略（mixed，1 次换乘）；三策略 = 两个指定策略（mixed）。

### 2.xlsx 站对表

每行 `[出发站, 到达站]`（中文站名或电报码均可，出题端自动解析）。
出题时随机取一对且**方向随机倒置**，提高覆盖率与成功率。
可用 `assist/filter_pairs_by_direct.py` 按直达车次数分区段随机采样生成（见 docs/06）。
读入规则：**只取前 2 列**（第 3 列及以后忽略，可放元数据）；前两列皆空的行视为
空行，连续 1~3 个跳过继续，**连续 ≥4 个视为表结束**；可用 `assist/` 输出的
分块+元数据格式（每块上方元数据行：第 4 列=范围、第 5 列=个数）。

### 关键参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--seat-weights` | 20,30,50 | 特等/一等/二等抽样权重，和须=100 |
| `--interference-density` | 0.02 | 干扰注入密度（1_），0=关闭 |
| `--random-tickets-density` | 0.02 | 随机票注入密度（2_），0=关闭 |
| `--max-retries` | 40 | 单题重试上限（每次重试重随机站对/方向/人数/座位） |
| `--receipt` | 无 | 失败回执 xlsx 输出路径（与 1.xlsx 同构，格值=失败数） |

## 2. 012 变体体系

| 前缀 | mode | interference_mode | 语义 | 解的特征 |
|---|---|---|---|---|
| `0_` | existence | none | 无票注入 | 只有标答路径有票，唯一解 |
| `1_` | existence | interference | 干扰注入 | 干扰票 randint(1, 人数-1) 严格不足 → 唯一解；1 人时退化为不注入 |
| `2_` | selective | random_tickets | 随机票 | randint(0.5×, 1.5×人数)，部分够票（真替代）部分差一张（陷阱）→ 多解，测评不对标答 |

题号：`{前缀}B{时间戳}_{序号:04d}`（批量模式）；查重 = 题目库文件或 metadata 任一命中即换。

## 3. 五题型标答构造（write_legal_solution）

所有票为正数，标答票数 = `randint(人数, 1.5×人数)`（保证 ≥ 人数；1 人退化为 1）。
换乘衔接统一 `min_gap = 20` 分钟。

### transfer 换乘
候选车次 T 要求：经过 A、**不经过 B**（防 T 直达逃逸）、A 之后仍有站。
**核心一**：一条 SQL 枚举 T 上全部可行 (中间站 M, 换乘车 U)——
U 经过 M 且 M 在实际目的地 B 之前、U≠T、`T到达M时刻 + 20min ≤ U 从 M 发车`。
**换乘车一律排除经过出发站 A 的车**（与 mixed 同规则，保证「换乘车不经过 A」
全局不变量）；与目的地**同城**（去"东/南/西/北/站"后缀比较）的中间站被过滤。
均匀随机挑一个方案；写两段：T 乘 A→M、U 乘 M→B。

### short_buy 买短补长
随机选一个中间站 M（A、B 之间），买 A→M，实际乘坐 A→B（M→B 无票需上车补票）。
ground_truth 记 `ride_to = B`。

### extra_front 前额外（买长坐短）
从 A 往前随机退 1~3 站得 Aₖ，买 Aₖ→B，实际从 A 上车（ride_from=A）。
要求 A 前至少 1 站。

### extra_rear 后额外
从 B 往后随机进 1~3 站得 Bₖ，买 A→Bₖ，实际在 B 下车（ride_to=B）。
要求 B 后至少 1 站。

### mixed 混合
路径 A → M1 → … → MN → B（N=换乘数，从中间站随机采样）。
段 0 用 T（记录 T 到达 M1 的时刻作为衔接基准）；段 i>0 在 M(i) 找换乘车
（复用核心一）：换乘车**一律排除经过出发站 A 的车**——否则「只坐换乘车从 A
直达 B」成为第二合法解，破坏存在性题唯一解前提（模型给出合法解反被标答比对
误判）；非末段的 short_buy 策略额外**排除经过最终站 B 的车**（防跳过后续
换乘直达终点）；后续还有换乘时选中车必须在本段终点有合法 HH:MM 到达时刻。
然后在每段车上按段策略（direct/short_buy/extra_front/extra_rear）落票。

## 4. 干扰 / 随机票注入（add_interference_all_trains）

- 候选池 = 全部车次全部 i<j 站对（约 46 万，来自全库经停内存缓存，
  按 railway.db mtime 自动失效），一次 shuffle 取前 `int(池大小 × density)` 个
  ——分母固定为全局池，跨题可比、低密度不归零。
- **标答占用站对 `(车次, 购买起, 购买讫)` 一律跳过**（含换乘车上的段）——
  干扰/随机票绝不与标答重叠（历史 BUG#1 修复）。
- 随机票题额外：`block_pairs={(A,B)}` 禁直达；transfer/mixed 加**买短补长逃逸
  防护**——对"先经过 A 再经过 B"的车次跳过 (A, X<B) 站对；落注后
  `_clear_direct_route` 删除 T 的 (A,B) 直达票。

## 5. 落盘流程（单题）

```
候选车次（shuffle，≤15 辆）
  └─ 每辆：预检中间站/前后余量 → 题号查重
       └─ 建临时库 {qid}.db.tmp
            ├─ 写标答（失败抛 RetryTrainError → 换下一辆车）
            ├─ 注入干扰/随机票（0_/无则跳过）
            ├─ 标答自反校验（带行为约束时，标答自身必须零违规）
            ├─ 自检 self_check：
            │    ① 标答每段库中票数 == 标答票数 且 ≥ 人数
            │    ② 干扰题唯一解断言：除标答占用外全库票数严格 < 人数
            └─ os.replace 原子改名（任何失败不留半成品库）
  └─ 成功 → update_question_metadata
```

重试判定：`RetryTrainError` 或错误信息含关键词（无中间站/无换乘车/额外站不足/
约束冲突/自检未通过）→ 换下一辆候选车；参数/数据类错误直接失败。
单题重试耗尽抛 GenerateError，批量层换下一对站再试（≤ --max-retries）。

## 6. metadata 字段契约（每题写入）

| 字段 | 示例 | 说明 |
|---|---|---|
| question | "北京南到上海虹桥" | 结构化行程题面（NL 化的输入） |
| question_type | transfer / short_buy / extra_front / extra_rear / mixed | 题型 |
| question_mode | existence / selective | 与 `type`（存在性/选择性）同步推导 |
| interference_mode | interference / random_tickets / 不存 | 1_/2_/0_ |
| interference_density | 0.02 | 注入密度（仅注入题存） |
| people_count / seat_type | 3 / class1 | 需求人数、标答席别 |
| start_station_id / end_station_id | VNP / AOH | 出发/到达电报码 |
| criterion | fastest | 评判标准（仅选择性题写值；存在性写空——题面不带「综合考虑」，防诱导模型偏离唯一解） |
| constraints | ["no_transfer"] | 行为约束（仅选择性；no_transfer 与 no_short_buy_extra 互斥） |
| ground_truth | [{train_num, from_station_id, to_station_id, ride_from_station_id, ride_to_station_id, tickets, seat_type, strategy, …}] | 结构化标答（含站名冗余字段），verifier 存在性对标用 |
| trains / train_count | ["G1","G102"] | 标答涉及车次（合并去重） |
| source | auto | 出题来源 |

## 7. 批量执行语义

- 顺序：先全部存在性（按分布行，先 1_ 有干扰再 0_ 无干扰），后全部选择性。
- 每题 `generate_one`：重试 ≤ max_retries，**每次重试重新随机** 站对/方向/人数(3~6)/
  座位（按权重）——换站对是提高成功率的关键。
- **0_/1_ 配对同解**：同 row 先出 1_，成功后 0_ 复用其基础题号+站对/方向/人数/座位，
  并按 1_ 的**路线结构**（目标车次/各段购买·乘坐区间/票数，`write_legal_solution`
  的 route_hint 重放模式）机械重建标答——0_ 题库 = 1_ 题库 − 干扰注入，
  两题唯一合法解完全相同（对比实验除干扰外完全受控）。
- 进度实时打印；结束时输出回执汇总（总/成功/失败 + 分布行粒度失败数），
  `--receipt` 可写同构 xlsx。
- 全程只读 railway.db / prices.db，只写 question/ 目录。

## 8. 校验规则速查（generate_question 入口）

- 存在性必须指定题型；选择性题型自动推导：constraints 含 no_transfer →
  short_buy，否则 transfer（保证有解）。
- 行为约束：白名单 {no_transfer, no_short_buy_extra}；去重；**两者互斥**
  （同选无合法解，直接 400 语义报错）；仅选择性支持。
- 评判标准：白名单 5 种；仅选择性支持且必选（存在性恒 comprehensive）。
- mixed：transfers ≥ 1 且 segment_plans 长度 = transfers + 1。
- 人数 1~20；seat_type ∈ 三席别。
