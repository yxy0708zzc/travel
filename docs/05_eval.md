# 05 测评与统计（step5_eval.py + verifier.py）

对测试记录做代码核查（不看模型解释，只对数据库与规则），产出 verdict 与
问题清单；再聚合为模型对比与报告。核查规则库在 `verifier.py`，
编排与统计在 `step5_eval.py`。

## 1. 三个子命令

| 子命令 | 输入 | 输出 | 功能 |
|---|---|---|---|
| `eval` | logs/test | logs/result + metadata_nl state | 逐条核查测试记录并保存结果 |
| `stats` | logs/result (+logs/test) | 控制台表 | 按模型聚合 verdict/通过率/双调用/问题码 |
| `report` | logs/result (+logs/test) | logs/report/*.json,*.md | 排名 + 题型难度 + 洞察，JSON 只留最近 50 份 |

eval 语义：
- 待测集合 = 全部测试记录 − 已测评（由结果文件 `test_file` 字段识别）；
  `--ids` 指定、`--re-evaluate` 强制重测（重测成功后删除该记录旧结果，不重复计数）。
- **空方案细分**：final_plan 为空时按测试记录 `plan_status` 区分——模型明确
  输出空数组（无解）→ `empty_plan`；未输出 final_plan JSON（轮数用尽/格式错误）
  → `no_plan`，统计不再混同。
- 结果文件结构：`{timestamp, test_file, question_id, model_name, user_input,
  score_summary{hallucination_count, issue_count, total_tokens, duration_seconds},
  verification, human_confirmed:false, human_notes, status}`；文件名含微秒。
- 成功后 `state: 该题该模型 → evaluated`（写入 --metadata-nl 指向的事实源）。

## 2. 核查流程（verify_final_plan）

### 层0 归一化（normalize_final_plan）
- 兼容两种字段命名：模型输出 `from/to` ≡ 内部 `from_station_id/to_station_id`；
  tickets→int、price→float 类型归一。
- 段类型：`purchase`（默认，必须含 seat_type/tickets，且必须标注实际乘坐区间
  ride_from/ride_to）与 `onboard`（补票段，DB 无票不查）。
- 非法条目（缺关键字段/缺 seat_type/tickets 非法或 ≤0/非对象）→ 带 `__invalid__`，
  作为 `invalid_plan_item` 问题处理，**不误判为幻觉**。
- **缺 ride 一律按不全（不兜底）**：ride 留空 + `__missing_ride__`，
  记 `missing_ride` 问题且该段不参与可达性拼接。

### 层1 逐购票段
- **余票**：查题目库对应席别表；同 `(车次,购买区间,席别)` 拆成多条声称时
  **先合计再对比**（防"余 3 张拆 2+2 各自通过"的拆单绕过）；
  库票 < 声称合计 → `hallucination`（detail 带合计说明）。
- **票价**：模型声称了 price 才核验；与库价差 >1.0 元 → `price_wrong`；
  库中无该区间票价 → `price_missing`——**数据端问题，独立列表
  （price_missing_issues / price_missing_count），不进主判 issues、不影响 verdict**。
- 非法席别 → `invalid_seat`。

### 层2a 存在性题：对标 ground_truth（答案唯一）
先做**反向覆盖检查**：标答每段都必须被提交，漏段 → `missing_gt_segment`
（防「只交第一段就 pass」的方案不完整）；再对每段与标答做四维比对，细分问题码：
`route_mismatch_train`（车次）/ `route_mismatch_route`（购买区间）/
`route_mismatch_seat`（座位）/ `route_mismatch_ride`（乘坐区间）；
四维全同但整体不符才落兜底 `route_mismatch`。
另有**两类题通用**：声称票数 < 需求人数 → `ticket_shortage`。

### 层2b 选择性题：座位等级 + 全程可达（check_reachability）
先查**座位等级**：每段席别必须等于题目要求的 seat_type → 否则 `seat_mismatch`。
再拼接全部段的实际乘坐区间，检查：
1. **地点连续**：后段乘坐起点 == 前段乘坐终点 → 否则 `route_discontinuity`；
2. **时间顺序**：跨车次时前段到达 ≤ 后段发车 → 否则 `transfer_time_conflict`。
   双侧必须均为合法 HH:MM（防 "--" 脏数据字典序误报）。字符串比较遇跨天
   （23:xx 到、次日 00:xx 发）判冲突是**有意的当日完成约束**——本系统不支持
   跨天行程，换乘跨天即非法方案；
3. **起终点覆盖**：出发站在首段乘坐区间内、到达站在末段乘坐区间内
   （含端点，兼容买长坐短）→ 否则 `start_not_covered` / `end_not_covered`。
   车次不经停/方向反等 → `route_invalid`；链路已断后不再判连续/时间（防连锁误报）；
   存在断裂/未覆盖/无效段任一 → 追加汇总 `no_route`。

### 层2c 行为约束（check_strategy_constraints，仅题目声明时触发）
- `no_transfer`：相邻两段车次不同 → `no_transfer_violated`
  （买短补长/额外购买均单车次，不会误判）。
- `no_short_buy_extra`（合并项，按同车经停顺序比较区间）：
  乘坐区间**严格包含**购买区间（坐得比买的多）→ `no_short_buy_violated`；
  购买区间**严格包含**乘坐区间（买得比坐的多，前/后都算）→ `no_extra_violated`。

### verdict 判定
`issues` 为空 → `pass`；非空 → `hallucination`。
外加两种前置短路：无可核查购票段 → `no_plan`；题目库缺失 → `db_not_found`。
（eval 侧对空方案按测试记录 plan_status 细分为 `empty_plan`/`no_plan`。）

## 3. 问题码全表

| 码 | 中文 | 组 | 判定条件 |
|---|---|---|---|
| invalid_plan_item | 计划条目无效 | 格式与缺失 | 层0 归一化失败 |
| missing_ride | 未标注乘坐区间 | 格式与缺失 | 购票段缺 ride_from/ride_to |
| invalid_seat | 无效座位类型 | 余票与票价 | seat_type ∉ class0/1/2 |
| hallucination | 余票不符 | 余票与票价 | 声称合计 > 库内余票 |
| price_wrong | 票价不符 | 余票与票价 | 差价 > 1.0 元 |
| ticket_shortage | 票数不足 | 路线与标答 | 声称 < 需求人数 |
| missing_gt_segment | 漏交标答段 | 路线与标答 | 存在性题反向覆盖：标答段未被提交 |
| route_mismatch_train/route/seat/ride(/整体) | 与标答不一致 | 路线与标答 | 存在性题四维比对 |
| seat_mismatch | 座位不符 | 路线与标答 | 选择性题购买席别 ≠ 题目要求席别 |
| route_invalid | 路线段无效 | 全程可达 | 车次不经停/方向反/缺信息 |
| route_discontinuity | 路线不连续 | 全程可达 | 前段终点 ≠ 后段起点 |
| transfer_time_conflict | 换乘时间冲突 | 全程可达 | 前段到达晚于后段发车（含跨天） |
| start_not_covered / end_not_covered | 未连接出发/到达站 | 全程可达 | 起终点不在首/末段乘坐区间 |
| no_route | 无法拼出全程 | 全程可达 | 上述任一触发的汇总行 |
| no_transfer_violated | 违反禁止换乘 | 行为约束 | 相邻段跨车次 |
| no_short_buy_violated | 违反禁止买短补长 | 行为约束 | 乘坐 ⊋ 购买（严格） |
| no_extra_violated | 违反禁止额外购买 | 行为约束 | 购买 ⊋ 乘坐（严格） |
| price_missing | 票价缺失（数据端） | 独立统计 | 声称了票价但库中无数据；**不影响 verdict** |

## 4. 统计口径（stats / report）

- 对错判定只看 `verification.verdict`：pass=对，hallucination=错，
  no_plan/empty_plan/db_not_found 单列。
- 按模型指标：total / pass_rate / error_rate / no_plan / empty / db_not_found /
  unknown 计数与比率；**avg_tokens / avg_duration / avg_model_calls /
  avg_tool_calls**（读关联测试记录：双调用计数优先显式字段，旧记录由 trace
  推导兜底——assistant 轮数与各轮嵌套 tools 总和）；问题码分布。
- report 排名按 pass_rate 降序；题型维度两组：`by_type`（存在性/选择性）、
  `by_question_type`（五题型，读 metadata_nl 事实源）；error_rate>10% 出警告；
  自动洞察（最高/最低通过率、题型从易到难排序）。
- 导出 `summary_{ts}.json`（保留最近 50 份）与 `summary_{ts}.md`。

## 5. 复用关系

- step2 出题的标答自反校验直接调用 `verifier.check_strategy_constraints`——
  出题端与核查端同一约束实现，杜绝口径漂移。
- step4 的 final_plan 解析复用 `verifier.normalize_final_plan`——
  模型输出、记录归一、核查入口三层同一契约。
