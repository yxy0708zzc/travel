# 04 测试（step4_test.py）

与被测模型进行工具调用对话，落盘完整轨迹与统计，是 benchmark 的"考试"环节。
纯命令行：扫描可测题目 → 并发对话 → 记录落盘 logs/test → 推进 state=tested。

## 1. 接口

```
python step4_test.py --railway-db ... --prices-db ... --question-dir ... \
    --metadata-nl question/metadata_nl.json --logs-test logs/test \
    [--model M] [--api-key K] [--base-url U] [--ids q1 q2] [--list] \
    [--max-iterations 30] [--concurrency 3] [--env .env]
```

- 模型配置：显式参数 > `.env`（TEST_MODEL→DEFAULT_MODEL / TEST_API_KEY /
  TEST_BASE_URL→DEFAULT_BASE_URL）；缺失报错退出。
- `--list`：只打印可测清单（题号/类型/可测性/原因），不执行测试。
- 可测 = 有 `question` 与 `nl_question`、题目库文件存在、且该模型在 state 中
  无记录（未测过；state 读自 --metadata-nl 指向的事实源）。测试成功的题同模型
  不可重测（去重）。

## 2. 工具集（被测模型的全部能力，OpenAI Tools 格式）

通用约定：

- 所有工具都是**本地 SQLite 只读查询**（无网络）；任何异常（含未知工具名）统一
  返回 `{"error": "..."}`，对话不中断。
- `station_id` = 12306 **电报码**（VNP=北京南、AOH=上海虹桥…）。模型获得电报码的
  唯一途径是 `list_stations`。
- 题目上下文（qid）**按调用传参**贯穿 run_chat → execute_tool，无全局可变状态——
  并发测试不会串题。

### 2.1 query_trains_between_stations —— 查两站间直达车次

| 输入 | 必填 | 说明 |
|---|---|---|
| `from_station_id` | ✅ | 出发站电报码 |
| `to_station_id` | ✅ | 到达站电报码 |

输出：数组（按出发时刻升序），元素含车次号、两站到发时刻与旅程时长；
两站间无直达车 → `[]`。

```json
[{"train_num": "G1", "depart_time": "08:00", "arrive_time": "12:30", "duration": "04:30"}]
```

说明：`depart_time`/`arrive_time` 是该车在 from/to 站的实际到发时刻（HH:MM）；
`duration` = 两者差值，跨天回绕 +24h（>24h 的长途车显示如 `"25:10"`）。

### 2.2 query_trains_at_station —— 查经停某站的车次

| 输入 | 必填 | 说明 |
|---|---|---|
| `station_id` | ✅ | 车站电报码 |

输出：数组（按车次号排序），元素 = 车次号 + 该站时刻：

```json
[{"train_num": "G1", "stop_time": "08:00"}, {"train_num": "G11", "stop_time": "09:15"}]
```

站不存在 → `{"error": "车站 VNP 未找到"}`。

### 2.3 query_train_detail —— 查车次完整经停

| 输入 | 必填 | 说明 |
|---|---|---|
| `train_num` | ✅ | 车次号 |

输出：`{"stops": [...]}`，按站序升序（站序 = 经停顺序，可用于判断经过关系与方向）：

```json
{"stops": [
  {"stop_no": 1, "station_id": "VNP", "station_name": "北京南", "stop_time": "08:00"},
  {"stop_no": 2, "station_id": "JGK", "station_name": "济南西", "stop_time": "09:38"}
]}
```

车次不存在或无经停数据 → `{"error": "车次 G999 未找到或无经停数据"}`。

### 2.4 list_stations —— 搜车站（拿电报码）

| 输入 | 必填 | 说明 |
|---|---|---|
| `keyword` | — | 关键词，**站名或电报码均可**（LIKE 模糊匹配） |
| `limit` | — | 每页数量，默认 50，最大 200 |
| `page` | — | 页码，默认 1 |

输出：数组（按站名排序）：`[{"station_id": "VNP", "station_name": "北京南"}]`。
全库约 3300 站，不传 `keyword` 会只返回第一页——找特定站必须带关键词。

### 2.5 list_trains —— 搜车次号

| 输入 | 必填 | 说明 |
|---|---|---|
| `keyword` | — | 匹配车次号（LIKE 模糊，如 `G1` 命中 G1/G10/G100…） |
| `limit` | — | 同 2.4 |
| `page` | — | 同 2.4 |

输出：数组（按车次号排序）：`[{"train_num": "G1"}]`。全库约 4800 个 G 车次。

### 2.6 query_tickets —— 查本题余票（读当前题 `question/{qid}.db`）

| 输入 | 必填 | 说明 |
|---|---|---|
| `train_num` | ✅ | 车次号 |
| `seat_types` | — | 席别数组（或逗号分隔串），白名单 `class0/class1/class2`，不传=全部三种 |
| `from_station_id` | ✅ | 出发站电报码 |
| `to_station_id` | ✅ | 到达站电报码 |

输出：`席别 → "出发电报码|到达电报码" → 余票数` 两层字典：

```json
{"class2": {"VNP|JGK": 12, "VNP|AOH": 0}, "class1": {"VNP|JGK": 8}, "class0": {}}
```

边界行为：**题目库为稀疏存储，无行 = 0 张**；指定站对在库中无记录时显式补
`0`（避免模型把“缺失”误解为有票）；`seat_types` 含白名单外的值 → 显式报错
（不伪造 0 票误导模型）；**站对必填**——12306 不提供整车余票能力，缺站对时
显式报错不查询。**只有本工具读题目库**——题内余票是本题私有数据。

### 2.7 query_ticket_price —— 查票价

| 输入 | 必填 | 说明 |
|---|---|---|
| `train_num` / `from_station_id` / `to_station_id` | ✅ | 车次 + 站对（电报码） |

输出：`席别 → 票价(元)`，只含查到的席别；该区间全部无票价数据 → `null`：

```json
{"class2": 553.0, "class1": 923.0, "class0": 1723.0}
```

## 3. 对话循环（run_chat）

```
messages = [SYSTEM_PROMPT, user=nl_question]
循环 iteration = 1..max_iterations:            # 一轮 = 一次 LLM API 请求
    POST {base_url}/chat/completions           # tools=7, tool_choice=auto,
                                               # temperature=TEST_TEMPERATURE(0.7)
    API 层错误（响应体含 error）→ end_reason=error 返回（已完成轮次统计保留）
    累计 token（增量口径，见 §4）
    无 tool_calls → 最终回答：end_reason=completed，break
    有 tool_calls：
        trace 追加 assistant 轮条目（含 reasoning/content/tools 嵌套）
        逐个执行工具，结果以 role=tool 回填（arguments 原样保留字符串形态）
异常路径 → end_reason=error；trace/token/duration 为已完成部分
```

- `max_iterations` 语义 = **最大对话轮数**（每轮一次模型调用，轮内可并行多工具），
  1~100，默认 30。
- 超时：单轮 `TEST_CHAT_TIMEOUT = 600s`。慢推理模型单轮思考可能极长，
  超时被掐断的那一轮不计耗时，但已完成轮次的统计全部保留。
- 思考链：`reasoning_content` 提取入 trace，但**不回传**给模型（messages 只含
  content 与 tool_calls）。

## 4. token 口径（跨平台一致）

- `prompt_tokens` = 各轮**新增输入**之和：每轮计 `max(0, 本轮prompt - 上轮prompt)`
  ——数值上 ≈ 最终完整上下文长度；**不扣**缓存命中（缓存折扣是平台侧口径，
  扣除会双重去重导致少算）。
- `completion_tokens` = 模型全部输出**含思考链**。

## 5. 测试记录（logs/test/{ts}_{model}_{qid}.json）

```json
{
  "timestamp": "ISO8601",
  "question_id": "1_B20260927_120000_0001",
  "model_name": "...",
  "user_input": "自然语言题面",
  "final_answer": "最终回复全文",
  "final_plan": [ ... ],           // 解析失败=null→[]；明确无解=[]
  "plan_status": "has_solution | empty_plan | no_plan",
  "token_usage": {"prompt_tokens", "completion_tokens", "total_tokens"},
  "duration": 秒,
  "end_reason": "completed | max_iterations | error",
  "model_calls": N,                // = assistant 轮数
  "tool_calls": N,                 // = 各轮嵌套 tools 总和
  "trace": [
    {"type": "user", "content": "..."},
    {"type": "assistant", "round": 1, "reasoning": "...", "content": "...",
     "tools": [{"tool_name", "arguments", "result"}]},
    ...
  ]
}
```

- **trace 是唯一对话源**：user 条目 + assistant 轮次条目（工具嵌套在所属轮的
  tools 数组），可完整重建对话；不再冗余存 conversation/平铺 tool_calls。
- 文件名含微秒，同秒同模型同题重复落盘不互相覆盖。

## 6. final_plan 解析（parse_ai_final_plan）

- 定位 `"final_plan"` 取**最后一次**出现（模型可能在正文复述提示词里的格式示例
  `{"final_plan": []}`，取首个会把有解误判成 empty_plan）→ 向左找最近 `{` →
  **字符串感知的花括号配对扫描**（字符串内的 {} 不计深度、跟踪转义）。
- 兜底：最后一个 ```json 代码块（须含 "final_plan" 键）。
- 条目**原样保留落盘**（不做无效剔除）：剔除会让 verifier 的 `invalid_plan_item`
  检查永远触发不了，全非法时还会被误判成「明确无解」。
- 三态：解析不到 JSON → `no_plan`（未输出）；JSON 且 plan 为空 → `empty_plan`
  （明确无解）；有内容（含非法条目）→ `has_solution`。

## 7. 执行建议

- 先 `--list` 核对待测集合，再正式运行；`--concurrency` 1~8
  （模型限流严的用 1~3）。
- 中途想停按一次 **Ctrl+C**：不再派发新题，在跑的题在**当前轮推理结束后**放弃
  （不落盘，一般 ≤1 分钟内程序退出）；已完成的题已即时落盘，重跑同命令
  自动续测剩余部分。**再按一次强制退出：立即杀死进程**，正在跑没结束的题
  全部放弃（已完成的不受影响）。
- 单题失败（限流/超时/网络等）**不释放工位**：原地等 10s 重试（上限 30 次，
  鉴权/余额类永久性错误不重试立即终局），避免启动瞬间并发触发 API 限流的
  雪崩连锁失败；重试参数在 step4_test.py 顶部常量（`_RETRY_WAIT_SECONDS`/
  `_MAX_ATTEMPTS`）。
- 所有 LLM 请求（含重试）经**全局节流**：任意两次请求发出间隔 ≥ 2s
  （`_REQUEST_MIN_INTERVAL`，跨工位共享的加锁限速器，锁内判定+更新原子；
  设 0 关闭）。高并发下实际吞吐上限 = 1 请求 / 2s。
- 失败（error/超时）不推进 state，可直接重跑同题；
  成功后重测同模型会被跳过——需要重测时手工删除
  metadata_nl.json 中对应条目的 `state` 项与 logs/test 记录。
- 正式跑之前用 1~2 题小样本验证 API 配置、超时与题面格式。
