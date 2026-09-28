# 04 测试（step4_test.py）

与被测模型进行工具调用对话，落盘完整轨迹与统计，是 benchmark 的"考试"环节。
纯命令行：扫描可测题目 → 并发对话 → 记录落盘 logs/test → 推进 state=tested。

## 1. 接口

```
python step4_test.py --railway-db ... --prices-db ... --question-dir ... \
    --metadata ... --logs-test logs/test \
    [--model M] [--api-key K] [--base-url U] [--ids q1 q2] [--list] \
    [--max-iterations 30] [--concurrency 3] [--env .env]
```

- 模型配置：显式参数 > `.env`（TEST_MODEL→DEFAULT_MODEL / TEST_API_KEY /
  TEST_BASE_URL→DEFAULT_BASE_URL）；缺失报错退出。
- `--list`：只打印可测清单（题号/类型/可测性/原因），不执行测试。
- 可测 = 有 `question` 与 `nl_question`、题目库文件存在、且该模型在 state 中
  无记录（未测过）。测试成功的题同模型不可重测（去重）。

## 2. 工具集（被测模型的全部能力，OpenAI Tools 格式）

| 工具 | 数据源 | 说明 |
|---|---|---|
| `query_trains_between_stations` | railway.db | 两站间车次及实际到发时间（不含始发/终到站） |
| `query_trains_at_station` | railway.db（station_trains 物化视图） | 经停某站的车次及在该站时刻 |
| `query_train_detail` | railway.db | 车次完整经停（序号/站名/电报码/时间） |
| `list_stations` | railway.db | 站名/电报码搜索，关键词 + 分页（limit≤200） |
| `list_trains` | railway.db | 车次号搜索，同上 |
| `query_tickets` | **question/{qid}.db** | 当前题目库余票：席别白名单校验（非法席别显式报错，不伪造 0 票）；指定站对但无行 → 显式补 0（稀疏存储无行=0 票） |
| `query_ticket_price` | prices.db | 三席别票价；查不到返回 null |

设计约束：工具内任何异常都以 `{"error": ...}` 返回给模型（对话不中断）；
题目上下文（qid）**按调用传参**贯穿 run_chat → execute_tool，
无全局可变状态——并发测试不会串题（参考项目曾因全局"当前题目"串题污染数据集）。

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
- 解析出的条目经 `verifier.normalize_final_plan` 归一（无效条目剔除）。
- 三态：解析不到 JSON → `no_plan`（未输出）；JSON 且 plan 为空 → `empty_plan`
  （明确无解）；有有效条目 → `has_solution`。

## 7. 执行建议

- 先 `--list` 核对待测集合，再正式运行；`--concurrency` 1~8
  （模型限流严的用 1~3）。
- 失败（error/超时）不推进 state，可直接重跑同题；
  成功后重测同模型会被跳过——需要重测时手工删除
  metadata 中对应 `state` 项与 logs/test 记录。
- 正式跑之前用 1~2 题小样本验证 API 配置、超时与题面格式。
