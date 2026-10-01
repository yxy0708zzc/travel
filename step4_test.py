"""
step4_test.py —— 测试：与被测模型对话循环（7 工具），轨迹/统计落盘 logs/test

复刻参考项目测试器工作流（语义 1:1）：
- 系统提示词 SYSTEM_PROMPT + 7 个 OpenAI 工具（基础库/题目库/票价库三类数据源）
- 工具调用循环：每轮 = 一次 LLM API 请求，轮内可并行多工具；max_iterations 轮数上限
- token 口径：prompt_tokens=各轮新增输入之和（≈最终上下文长度），completion 含思考链
- 结束原因：completed / max_iterations / error；统计每轮即时累计（中断不丢已完成轮次）
- 记录结构：trace（user + assistant 轮次嵌套 tools）为唯一对话源，含 final_plan/
  plan_status/model_calls/tool_calls/end_reason

用法：
    # 扫描可测题目（信息完备且该模型未测过）
    python step4_test.py --railway-db data/railway.db --prices-db data/prices.db ^
        --question-dir question --metadata question/metadata.json --logs-test logs/test --list

    # 批量测试
    python step4_test.py ... --model your-model --max-iterations 30 --concurrency 3 [--ids q1 q2]

模型配置：--api-key/--model/--base-url > .env（TEST_API_KEY/TEST_MODEL→DEFAULT_MODEL/
TEST_BASE_URL→DEFAULT_BASE_URL），缺失报错退出，不默认任何平台。
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests

from common import config
from common import database as db
from common.prompts import SYSTEM_PROMPT

# ============================================================
# 工具定义（OpenAI Tools；被测模型可见的全部能力）
# ============================================================
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_trains_between_stations",
            "description": "查询从出发站到到达站之间的所有高铁车次，返回车次号及在出发站和到达站的实际到发时间。不返回整趟车的始发站或终到站。",
            "parameters": {
                "type": "object",
                "properties": {
                    "from_station_id": {"type": "string", "description": "出发站电报码，如 VNP（北京南）"},
                    "to_station_id": {"type": "string", "description": "到达站电报码，如 AOH（上海虹桥）"},
                },
                "required": ["from_station_id", "to_station_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_trains_at_station",
            "description": "查询经过指定车站的所有车次，返回车次号及在该站的时间。结果按车次号排序。",
            "parameters": {
                "type": "object",
                "properties": {
                    "station_id": {"type": "string", "description": "车站电报码，如 VNP（北京南）"},
                },
                "required": ["station_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_train_detail",
            "description": "查询指定车次的完整经停站详情，返回该车次所有经停站的序号、站名、电报码、时间。",
            "parameters": {
                "type": "object",
                "properties": {
                    "train_num": {"type": "string", "description": "车次号，如 G1"},
                },
                "required": ["train_num"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_stations",
            "description": "搜索车站列表，返回车站电报码和中文名。不传 keyword 则返回全部车站（受分页限制）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "搜索关键词，匹配站名或电报码。可选。"},
                    "limit": {"type": "integer", "description": "每页返回数量，默认 50，最大 200"},
                    "page": {"type": "integer", "description": "页码，默认 1"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_trains",
            "description": "搜索车次列表，返回所有车次号。不传 keyword 则返回全部车次（受分页限制）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "匹配车次号，如 G1。可选。"},
                    "limit": {"type": "integer", "description": "每页返回数量，默认 50，最大 200"},
                    "page": {"type": "integer", "description": "页码，默认 1"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_tickets",
            "description": "查询指定车次各站对之间的余票数量。三种座位类型：class2=二等座（经济型），class1=一等座（更舒适），class0=特等座（可躺，十分舒适）。不指定 seat_types 则返回全部三种。不指定 from_station_id/to_station_id 则返回所有站对。",
            "parameters": {
                "type": "object",
                "properties": {
                    "train_num": {"type": "string", "description": "车次号，如 G1"},
                    "seat_types": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["class0", "class1", "class2"]},
                        "description": "要查询的座位类型列表，不传则返回全部三种",
                    },
                    "from_station_id": {"type": "string", "description": "出发站电报码，不传则返回所有站对"},
                    "to_station_id": {"type": "string", "description": "到达站电报码，不传则返回所有站对"},
                },
                "required": ["train_num"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_ticket_price",
            "description": "查询某车次在指定出发站到到达站之间的各席别票价（元）。返回 class2/class1/class0 的价格。如果查不到该区间的票价数据则返回 null。",
            "parameters": {
                "type": "object",
                "properties": {
                    "train_num": {"type": "string", "description": "车次号，如 G1"},
                    "from_station_id": {"type": "string", "description": "出发站电报码，如 VNP"},
                    "to_station_id": {"type": "string", "description": "到达站电报码，如 JGK"},
                },
                "required": ["train_num", "from_station_id", "to_station_id"],
            },
        },
    },
]


# ============================================================
# 工具执行（全部为本地数据库查询，无网络）
# ============================================================

def execute_tool(func_name: str, func_args: Dict, question_id: Optional[str]) -> Any:
    """分发执行被测模型的工具调用。query_tickets 读当前题目库（题内余票），
    其余读基础库/票价库。任何异常都以 {"error": ...} 返回给模型（不中断对话）。"""
    try:
        rw = db.get_railway_conn()
        try:
            if func_name == "query_trains_between_stations":
                return db.get_routes_between(rw, func_args.get("from_station_id", ""),
                                             func_args.get("to_station_id", ""))

            if func_name == "query_trains_at_station":
                result = db.get_station_trains(rw, func_args.get("station_id", ""))
                return result["trains"] if result else {"error": f"车站 {func_args.get('station_id', '')} 未找到"}

            if func_name == "query_train_detail":
                stops = db.get_train_stops(rw, func_args.get("train_num", ""))
                return {"stops": stops} if stops else \
                    {"error": f"车次 {func_args.get('train_num', '')} 未找到或无经停数据"}

            if func_name == "list_stations":
                return _paged(rw, "stations", "station_id, station_name", "station_name",
                              func_args, lambda r: {"station_id": r[0], "station_name": r[1]},
                              search_cols="station_name, station_id")   # 站名/电报码均可搜

            if func_name == "list_trains":
                return _paged(rw, "trains", "train_num", "train_num",
                              func_args, lambda r: {"train_num": r[0]})

            if func_name == "query_tickets":
                if not question_id:
                    return {"error": "未选择题目，无法查询余票"}
                q_path = db.get_question_db_path(question_id)
                if not os.path.exists(q_path):
                    return {"error": f"题目 {question_id} 不存在"}

                seat_types = func_args.get("seat_types")
                if isinstance(seat_types, str):
                    seat_types = [s.strip() for s in seat_types.split(",") if s.strip()]
                if seat_types is not None:
                    bad = [s for s in seat_types if s not in db.TICKET_TABLES]
                    if bad:   # 席别白名单：非法席别显式报错，不伪造 0 票误导模型
                        return {"error": f"无效座位类型: {bad}，可选 class0/class1/class2"}
                q_conn = sqlite3.connect(q_path)
                try:
                    tickets = db.get_train_tickets_filtered(
                        q_conn, func_args.get("train_num", ""),
                        func_args.get("from_station_id"), func_args.get("to_station_id"),
                        seat_types)
                finally:
                    q_conn.close()
                # 指定站对但库中无记录 → 返回 0 张（而非缺失键，避免模型误解）
                f, t = func_args.get("from_station_id"), func_args.get("to_station_id")
                if f and t:
                    key = f"{f}|{t}"
                    for st in (seat_types or db.TICKET_TABLES):
                        tickets.setdefault(st, {}).setdefault(key, 0)
                return tickets

            if func_name == "query_ticket_price":
                p_conn = db.get_prices_conn()
                try:
                    prices = {}
                    for seat in db.TICKET_TABLES:
                        p = db.get_price(p_conn, func_args.get("train_num", ""),
                                         func_args.get("from_station_id", ""),
                                         func_args.get("to_station_id", ""), seat)
                        if p is not None:
                            prices[seat] = p
                    return prices if prices else None
                finally:
                    p_conn.close()

            return {"error": f"未知工具: {func_name}"}
        finally:
            rw.close()
    except Exception as e:
        return {"error": str(e)}


def _paged(rw, table: str, cols: str, order_col: str, args: Dict, fmt,
           search_cols: str = None) -> List[Dict]:
    """list_stations / list_trains 共用的关键词 + 分页查询（limit≤200）；
    search_cols：keyword 匹配的列（默认仅排序列，list_stations 传站名+电报码两列）"""
    keyword = args.get("keyword", "")
    limit = max(1, min(int(args.get("limit", 50) or 50), 200))
    offset = (max(1, int(args.get("page", 1) or 1)) - 1) * limit
    sql = f"SELECT {cols} FROM {table}"
    params: list = []
    if keyword:
        sc = [c.strip() for c in (search_cols or order_col).split(",")]
        sql += " WHERE " + " OR ".join(f"{c} LIKE ?" for c in sc)
        params += [f"%{keyword}%"] * len(sc)
    sql += f" ORDER BY {order_col} LIMIT ? OFFSET ?"
    params += [limit, offset]
    return [fmt(r) for r in rw.execute(sql, params).fetchall()]


# ============================================================
# 对话循环（同步 requests；语义与参考版逐条对应）
# ============================================================

def resolve_llm_config(model: str, api_key: str, base_url: str) -> tuple:
    """显式参数 > .env（TEST_MODEL→DEFAULT_MODEL / TEST_API_KEY / TEST_BASE_URL→DEFAULT_BASE_URL）。
    缺失直接报错（不默认任何平台——避免 A 平台 key 打到 B 平台）。"""
    model = (model or "").strip() or config.env_get("TEST_MODEL", "DEFAULT_MODEL")
    api_key = (api_key or "").strip() or config.env_get("TEST_API_KEY")
    base_url = (base_url or "").strip() or config.env_get("TEST_BASE_URL", "DEFAULT_BASE_URL")
    missing = [n for n, v in (("模型名（TEST_MODEL/DEFAULT_MODEL）", model),
                              ("API Key（TEST_API_KEY）", api_key),
                              ("接口地址（TEST_BASE_URL/DEFAULT_BASE_URL）", base_url)) if not v]
    if missing:
        raise SystemExit(f"❌ 模型配置缺失：{'、'.join(missing)}（命令行传入或写 .env）")
    return model, api_key, base_url


def run_chat(message: str, llm: Dict[str, str], max_iterations: int,
             question_id: str = "", log=lambda msg, level="info": None) -> Dict:
    """单题完整对话：系统提示词 → 用户题面 → 工具调用循环 → 最终回复。
    question_id 按调用传入（工具查询读对应题目库），无全局状态、并发不串题。

    返回 {reply, reasoning, token_usage, duration, iteration_count, end_reason,
          trace, final_answer}；API 层错误返回 {"error": ...}（已完成轮次统计保留在
          token_usage/duration 中）。"""
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": message}]
    trace = [{"type": "user", "content": message}]
    token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    last_prompt = 0          # 上轮 prompt_tokens（增量口径：每轮只计新增输入）
    start = datetime.now()
    end_reason = "max_iterations"
    final_content = final_reasoning = ""
    iteration = 0

    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {llm['api_key']}"}
    url = f"{llm['base_url'].rstrip('/')}/chat/completions"
    body = {"model": llm["model"], "messages": messages, "tools": TOOLS,
            "tool_choice": "auto", "stream": False,
            "temperature": config.TEST_TEMPERATURE}

    try:
        with requests.Session() as session:
            while iteration < max_iterations:
                iteration += 1
                round_start = datetime.now()
                log(f"第 {iteration}/{max_iterations} 轮对话：请求模型 {llm['model']}")

                resp = session.post(url, json=body, headers=headers,
                                    timeout=config.TEST_CHAT_TIMEOUT)
                # 已完成轮次耗时即时累计（超时被掐断的本轮不计）
                meta_duration = (datetime.now() - round_start).total_seconds()
                data = resp.json()

                if "error" in data:   # API 层错误（余额不足/429 等非异常路径）
                    err = data["error"]
                    msg = err.get("message", "API 调用失败") if isinstance(err, dict) else str(err)
                    return {"error": msg, "token_usage": token_usage,
                            "duration": (datetime.now() - start).total_seconds(),
                            "iteration_count": iteration, "end_reason": "error", "trace": trace}

                message_obj = data["choices"][0]["message"]

                # token 增量口径：prompt 只计相邻轮新增（≈最终完整上下文长度），
                # 不扣缓存命中；completion 含思考链
                if "usage" in data:
                    usage = data["usage"]
                    delta = max(0, usage.get("prompt_tokens", 0) - last_prompt)
                    last_prompt = usage.get("prompt_tokens", 0)
                    token_usage["prompt_tokens"] += delta
                    token_usage["completion_tokens"] += usage.get("completion_tokens", 0)
                    token_usage["total_tokens"] = (token_usage["prompt_tokens"]
                                                   + token_usage["completion_tokens"])

                reasoning = (message_obj.get("reasoning_content") or "")
                tool_calls = message_obj.get("tool_calls")

                if not tool_calls:   # 最终回答
                    end_reason = "completed"
                    final_content = message_obj.get("content", "") or ""
                    final_reasoning = reasoning
                    messages.append({"role": "assistant", "content": final_content})
                    trace.append({"type": "assistant", "round": iteration, "reasoning": reasoning,
                                  "content": final_content, "tools": []})
                    break

                # 工具调用轮：assistant 条目（含思考链）+ 嵌套 tools
                trace_entry = {"type": "assistant", "round": iteration,
                               "reasoning": reasoning,
                               "content": message_obj.get("content", ""), "tools": []}
                trace.append(trace_entry)
                assistant_msg = {"role": "assistant", "content": message_obj.get("content", ""),
                                 "tool_calls": []}
                messages.append(assistant_msg)

                for tc in tool_calls:
                    name = tc["function"]["name"]
                    raw_args = tc["function"]["arguments"]
                    try:
                        fargs = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except json.JSONDecodeError:
                        fargs = {}
                    log(f"第 {iteration} 轮执行工具 {name}")
                    result = execute_tool(name, fargs, question_id)
                    trace_entry["tools"].append({"tool_name": name, "arguments": fargs,
                                                 "result": result})
                    assistant_msg["tool_calls"].append({
                        "id": tc["id"], "type": "function",
                        "function": {"name": name, "arguments": raw_args}})
                    messages.append({"role": "tool", "tool_call_id": tc["id"],
                                     "content": json.dumps(result, ensure_ascii=False)})
                body["messages"] = messages

        return {"reply": final_content, "reasoning": final_reasoning,
                "token_usage": token_usage,
                "duration": (datetime.now() - start).total_seconds(),
                "iteration_count": iteration, "end_reason": end_reason,
                "trace": trace, "final_answer": final_content}
    except Exception as e:
        # 异常路径：已完成轮次的统计保留（trace/token/duration），end_reason=error
        return {"error": str(e), "token_usage": token_usage,
                "duration": (datetime.now() - start).total_seconds(),
                "iteration_count": iteration, "end_reason": "error", "trace": trace}


# （题目上下文按调用传参：run_chat(question_id=...) → execute_tool，无全局可变状态）


# ============================================================
# final_plan 解析 + 记录落盘
# ============================================================

def _balanced_json_from(text: str, start: int) -> Optional[str]:
    """从 start（指向 '{'）起做字符串感知的花括号配对扫描（字符串内的 {} 不计深度）"""
    depth = in_string = escaped = 0
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def parse_ai_final_plan(content: str) -> Optional[List[Dict]]:
    """从模型回复解析 final_plan。取**最后一次**出现（模型复述提示词格式示例时，
    取首例会把有解误判成 empty_plan）→ 字符串感知括号配对；```json 代码块兜底。"""
    if not content:
        return None
    json_str = None
    idx = content.rfind('"final_plan"')
    if idx >= 0:
        start = content.rfind("{", 0, idx)
        if start >= 0:
            json_str = _balanced_json_from(content, start)
    if not json_str:
        for m in re.finditer(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", content, re.DOTALL):
            try:
                if '"final_plan"' in json.loads(m.group(1)):
                    json_str = m.group(1)
            except (json.JSONDecodeError, TypeError):
                continue
    if not json_str:
        return None
    try:
        data = json.loads(json_str)
        if not isinstance(data, dict) or not isinstance(data.get("final_plan", []), list):
            return None
        plan = data["final_plan"]
        if len(plan) == 0:
            return []   # 模型明确输出无解
        # 原样返回（含格式非法条目）：丢弃会让 verifier 的 invalid_plan_item 永远触发不了，
        # 全非法时被误记成 empty_plan（模型明明输出了方案，只是格式不对）
        return plan
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


def save_test_record(result: Dict, question_id: str, model_name: str,
                     user_input: str, logs_dir: str) -> Dict:
    """对话结果 → 测试记录落盘 {logs_dir}/{ts}_{model}_{qid}.json（结构同参考）。"""
    final_plan = parse_ai_final_plan(result.get("final_answer", ""))
    if final_plan is None:
        final_plan, plan_status = [], "no_plan"       # 未输出 final_plan JSON
    elif len(final_plan) == 0:
        plan_status = "empty_plan"                     # 明确无解
    else:
        plan_status = "has_solution"

    trace = result.get("trace") or []
    record = {
        "timestamp": datetime.now().isoformat(),
        "question_id": question_id,
        "model_name": model_name,
        "user_input": user_input,
        "final_answer": result.get("final_answer", ""),
        "final_plan": final_plan,
        "plan_status": plan_status,
        "token_usage": result.get("token_usage", {}),
        "duration": result.get("duration", 0),
        "end_reason": result.get("end_reason", "completed"),
        # 模型调用=assistant 轮数；工具调用=各轮嵌套 tools 总和
        "model_calls": sum(1 for e in trace if e.get("type") == "assistant"),
        "tool_calls": sum(len(e.get("tools") or []) for e in trace if e.get("type") == "assistant"),
        "trace": trace,
    }
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    # 模型名可能带 / 或 :（OpenRouter/Ollama 风格），直接拼文件名会 FileNotFoundError/非法路径
    safe_model = re.sub(r'[\\/:*?"<>|]', "_", model_name or "unknown")
    filename = f"{ts}_{safe_model}_{question_id}.json"
    os.makedirs(logs_dir, exist_ok=True)
    path = os.path.join(logs_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    record["filename"] = filename
    return record


# ============================================================
# 批量测试
# ============================================================

def testable(metadata: Dict, qid: str, model: str) -> Tuple[bool, str]:
    """可测 = 题面与 NL 均存在，且该模型无 state 记录（未测过）"""
    m = metadata.get(qid) or {}
    if not m.get("question") or not m.get("nl_question"):
        return False, "缺题面或 nl_question"
    if db.get_question_model_state(m, model) is not None:
        return False, f"该模型已测试过（state={db.get_question_model_state(m, model)}）"
    return True, ""


def run_batch(llm: Dict[str, str], qids: List[str], max_iterations: int,
              concurrency: int, logs_dir: str) -> List[Dict]:
    """并发批量测试：每题独立对话 → 落盘 → state=tested（仅成功推进）。"""
    model = llm["model"]
    metadata = db.load_metadata()
    todo = []
    for qid in qids:
        ok, reason = testable(metadata, qid, model)
        if ok:
            todo.append(qid)
        else:
            print(f"⏭ {qid} 跳过：{reason}")
    print(f"批量测试开始：模型 {model}，待测 {len(todo)}/{len(qids)} 题，"
          f"最大对话轮数 {max_iterations}，并发 {concurrency}")

    details: List[Dict] = []
    lock = threading.Lock()

    def one(seq: int, qid: str) -> Dict:
        entry = metadata.get(qid) or {}

        def plog(msg, level="info"):
            print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                  f"{'✔' if level == 'success' else '✘' if level == 'error' else '·'}"
                  f"（{seq}/{len(todo)}）{qid} {msg}")

        try:
            chat = run_chat(entry.get("nl_question", ""), llm, max_iterations,
                            question_id=qid, log=plog)
            if chat.get("error"):
                raise RuntimeError(chat["error"])
            record = save_test_record(chat, qid, model,
                                      entry.get("nl_question", ""), logs_dir)
            db.set_question_model_state(qid, model, "tested")   # 只记成功；成功后同模型不可重测
            plog(f"完成 plan_status={record['plan_status']}，模型调用 {record['model_calls']} 次，"
                 f"工具调用 {record['tool_calls']} 次，耗时 {round(record['duration'], 1)}s → {record['filename']}",
                 "success")
            return {"question_id": qid, "ok": True, "error": None, "filename": record["filename"],
                    "plan_status": record["plan_status"]}
        except Exception as e:
            plog(f"测试失败：{e}", "error")
            return {"question_id": qid, "ok": False, "error": str(e)}

    concurrency = max(1, min(int(concurrency or 1), 8))
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for fut in as_completed([pool.submit(one, i, q) for i, q in enumerate(todo, 1)]):
            with lock:
                details.append(fut.result())
    ok = sum(1 for d in details if d["ok"])
    print(f"批量测试完成：成功 {ok} / 失败 {len(details) - ok}")
    return details


# ============================================================
# CLI
# ============================================================

def main():
    ap = argparse.ArgumentParser(description="step4 测试（对话循环 + 日志落盘）")
    ap.add_argument("--railway-db", required=True, help="基础数据库路径（只读）")
    ap.add_argument("--prices-db", required=True, help="票价数据库路径（只读）")
    ap.add_argument("--question-dir", required=True, help="题目目录（{qid}.db）")
    ap.add_argument("--metadata", required=True, help="题目元数据路径")
    ap.add_argument("--metadata-nl", default=None,
                    help="NL 元数据路径（默认从 question_dir 推导 metadata_nl.json）")
    ap.add_argument("--logs-test", required=True, help="测试记录输出目录")
    ap.add_argument("--model", default="", help="被测模型名（未填读 .env TEST_MODEL→DEFAULT_MODEL）")
    ap.add_argument("--api-key", default="", help="API Key（未填读 .env TEST_API_KEY）")
    ap.add_argument("--base-url", default="", help="API Base URL（未填读 .env TEST_BASE_URL→DEFAULT_BASE_URL）")
    ap.add_argument("--ids", nargs="*", default=None, help="仅测指定题目（默认扫描全部可测题目）")
    ap.add_argument("--max-iterations", type=int, default=30,
                    help="最大对话轮数（每轮=一次模型调用），1~100，默认 30")
    ap.add_argument("--concurrency", type=int, default=1, help="并发数 1~8，默认 1")
    ap.add_argument("--list", action="store_true", help="只扫描可测题目，不执行测试")
    ap.add_argument("--env", default=None, help=".env 路径（默认脚本目录）")
    args = ap.parse_args()

    if args.env:
        config.ENV.update(config.load_env(args.env))
    model, api_key, base_url = resolve_llm_config(args.model, args.api_key, args.base_url)
    llm = {"model": model, "api_key": api_key, "base_url": base_url}

    db.configure_paths(railway_db=args.railway_db, prices_db=args.prices_db,
                       question_dir=args.question_dir, metadata=args.metadata,
                       metadata_nl=args.metadata_nl)
    metadata = db.load_metadata()

    all_qids = [q for q, m in metadata.items()
                if isinstance(m, dict) and m.get("question") and m.get("nl_question")
                and os.path.exists(db.get_question_db_path(q))]
    if args.ids:
        gone = [q for q in args.ids if q not in metadata]
        if gone:
            sys.exit(f"❌ 指定题目不存在: {', '.join(gone)}")
        qids = args.ids
    else:
        qids = all_qids

    if args.list:
        print(f"{'题号':<24} {'类型':<6} {'可测':<4} 说明")
        for qid in sorted(all_qids):
            ok, reason = testable(metadata, qid, model)
            m = metadata[qid]
            print(f"{qid:<24} {m.get('type', ''):<6} {'是' if ok else '否':<4} "
                  f"{'' if ok else reason}")
        return

    if not qids:
        print("没有可测试的题目（检查 --metadata 是否含 question/nl_question）")
        return
    run_batch(llm, qids, max(1, min(args.max_iterations, 100)), args.concurrency,
              args.logs_test)


if __name__ == "__main__":
    main()
