"""
verifier.py —— 代码核查：对比模型最终乘车方案与题目库/票价库，产出 verdict 与问题清单

判定体系（与参考项目语义 1:1）：
- 层0 normalize_final_plan：统一字段契约（from/to ≡ from_station_id/to_station_id），
  缺"实际乘坐区间"一律按不全处理（不兜底），非法条目带 __invalid__
- 层1 逐购票段：余票核验（同段拆分先合计再对比，防拆单绕过）→ hallucination；
  票价核验 → price_wrong / price_missing（数据端问题，独立列表不进主判）
- 层2：存在性题与 ground_truth 四维对标（车次/购买区间/座位/乘坐区间）；
  选择性题做全程可达性（地点连续 + 时间顺序 + 连接起终点）+ 行为约束检测
- verdict 五档：pass / hallucination / no_plan / empty_plan / db_not_found
  （empty_plan 由批量测评侧对空方案统一覆写，本函数对无可核查段返回 no_plan）

对外接口：normalize_final_plan / verify_final_plan / check_strategy_constraints
"""

import os
import re
import sqlite3
from typing import Any, Dict, List, Optional

from common.database import (
    TICKET_TABLES, get_price, get_prices_conn, get_question_db_path,
    get_question_metadata, get_railway_conn, get_train_stops,
)


# ============================================================
# 层0：字段契约归一化（唯一入口）
# ============================================================

def normalize_final_plan(raw_plan: List[Dict]) -> List[Dict]:
    """统一 final_plan 字段契约。兼容模型输出（from/to）与内部存储
    （from_station_id/to_station_id）两种命名；tickets→int、price→float 类型归一。

    段类型 seg_type：
    - "purchase"（默认）：购票段，必须含 seat_type/tickets，且必须标注 ride_from/ride_to
    - "onboard"：上车补票段（DB 无票不查），乘坐区间即 from/to

    缺关键字段 / tickets 非法或 ≤0 / 非 seat_type 的购票段 → 带 "__invalid__": True
    （调用方按 invalid_plan_item 处理，不误判为幻觉）。
    缺 ride 的购票段 → 带 "__missing_ride__": True（ride 留空，层2 不计入可达拼接）。
    """
    out: List[Dict] = []
    for item in raw_plan or []:
        if not isinstance(item, dict):
            out.append({"__invalid__": True, "error": "条目非对象", "raw": item})
            continue
        train_num = item.get("train_num", "")
        from_id = item.get("from_station_id") or item.get("from", "")
        to_id = item.get("to_station_id") or item.get("to", "")
        if not (train_num and from_id and to_id):
            out.append({"__invalid__": True, "error": "缺关键字段(train_num/from/to)", "raw": item})
            continue

        seg_type = str(item.get("seg_type") or "").strip().lower()
        is_onboard = seg_type == "onboard" or item.get("onboard") is True
        ride_from = item.get("ride_from_station_id") or item.get("ride_from") or ""
        ride_to = item.get("ride_to_station_id") or item.get("ride_to") or ""

        if is_onboard:
            out.append({
                "train_num": str(train_num), "from_station_id": str(from_id),
                "to_station_id": str(to_id),
                "ride_from_station_id": str(ride_from or from_id),
                "ride_to_station_id": str(ride_to or to_id),
                "seg_type": "onboard",
            })
            continue

        if not item.get("seat_type"):
            out.append({"__invalid__": True, "error": "购票段缺 seat_type", "raw": item})
            continue
        try:   # tickets 类型容错："3.0" 等字符串可解析；解析失败按无效条目（不 500）
            tickets = int(float(item.get("tickets", 0) or 0))
        except (ValueError, TypeError):
            out.append({"__invalid__": True,
                        "error": f"购票段 tickets 非法: {item.get('tickets')!r}", "raw": item})
            continue
        if tickets <= 0:   # 0 张购票段无意义（虚高 correct_items），与负数一并无效
            out.append({"__invalid__": True,
                        "error": f"购票段 tickets 非法(≤0): {tickets}", "raw": item})
            continue

        missing_ride = not (ride_from and ride_to)
        entry = {
            "train_num": str(train_num), "from_station_id": str(from_id),
            "to_station_id": str(to_id),
            "ride_from_station_id": "" if missing_ride else str(ride_from),
            "ride_to_station_id": "" if missing_ride else str(ride_to),
            "seat_type": str(item["seat_type"]), "tickets": tickets,
            "seg_type": "purchase",
        }
        if missing_ride:
            entry["__missing_ride__"] = True
        price = item.get("price")
        if price is not None:
            try:
                entry["price"] = float(price)
            except (ValueError, TypeError):
                pass
        method = item.get("method") or item.get("strategy")
        if method:
            entry["method"] = str(method)
        out.append(entry)
    return out


# ============================================================
# 主核查
# ============================================================

def verify_final_plan(final_plan: List[Dict], question_id: str) -> Dict[str, Any]:
    """核查最终方案：题目库余票 + 票价库 + 标答/可达性/约束。返回明细与 verdict。"""
    # ---- 层0：归一化 + 拆分 ----
    valid_items, invalid_issues, missing_ride_issues = [], [], []
    for it in normalize_final_plan(final_plan):
        if it.get("__invalid__"):
            invalid_issues.append({
                "type": "invalid_plan_item",
                "detail": f"计划条目无效: {it.get('error', '未知')} | raw={it.get('raw')}",
            })
        else:
            valid_items.append(it)
            if it.get("__missing_ride__"):
                missing_ride_issues.append({
                    "type": "missing_ride",
                    "train_num": it["train_num"],
                    "from_station_id": it["from_station_id"],
                    "to_station_id": it["to_station_id"],
                    "detail": (f"购票段 {it['train_num']} {it['from_station_id']}→"
                               f"{it['to_station_id']} 未标注实际乘坐区间 ride_from/ride_to"
                               "（必须写出）"),
                })
    purchase_items = [i for i in valid_items if i.get("seg_type") != "onboard"]

    meta = get_question_metadata(question_id)
    gt = meta.get("ground_truth") or []
    people_count = meta.get("people_count", 1)
    selective = meta.get("type") == "选择性"
    start_id = meta.get("start_station_id") or (gt[0]["from_station_id"] if gt else None)
    end_id = meta.get("end_station_id") or (gt[-1]["to_station_id"] if gt else None)
    gt_purchase = [g for g in gt if g.get("seg_type") != "onboard"]

    # ---- 题目库缺失 ----
    db_path = get_question_db_path(question_id)
    if not os.path.exists(db_path):
        return _empty_result(
            verdict="db_not_found", invalid=invalid_issues,
            summary="❌ 题目数据库不存在",
            issues=[{"type": "db_not_found",
                     "detail": f"题目 {question_id} 的数据库不存在"}] + invalid_issues)

    # ---- 无可核查购票段 ----
    if not purchase_items:
        return _empty_result(
            verdict="no_plan", invalid=invalid_issues,
            missing_ride=missing_ride_issues, meta_type=meta.get("type"),
            issues=invalid_issues + missing_ride_issues,
            summary="⚠️ 未检测到可核查的购票段（final_plan 缺失或全为无效条目）")

    # ---- 层1 + 层2 ----
    results: List[Dict] = []
    issues: List[Dict] = list(invalid_issues) + list(missing_ride_issues)
    price_missing_issues: List[Dict] = []

    name_map = _station_names()
    conn = sqlite3.connect(db_path)
    p_conn = get_prices_conn()
    try:
        # 余票聚合：同 (车次,购买区间,席别) 拆成多条声称时先合计再对比（防拆单绕过）
        agg_claims: Dict[tuple, int] = {}
        for item in purchase_items:
            key = (item["train_num"], item["from_station_id"],
                   item["to_station_id"], item["seat_type"])
            if key[3] in TICKET_TABLES:
                agg_claims[key] = agg_claims.get(key, 0) + (item.get("tickets") or 0)

        for item in purchase_items:   # 仅购票段查余票/票价；补票段不查 DB
            _verify_tickets_and_price(item, conn, p_conn, name_map, agg_claims,
                                      results, issues, price_missing_issues)

        # ---- 层2a：存在性 = 与标答完全一致（提交段比对 + 标答段反向覆盖） ----
        if not selective and gt_purchase:
            submitted_keys = {_seg_key(i) for i in purchase_items}
            for g in gt_purchase:
                if _seg_key(g) not in submitted_keys:
                    issues.append({
                        "type": "missing_gt_segment",
                        "train_num": g.get("train_num", ""),
                        "from_station_id": g.get("from_station_id", ""),
                        "to_station_id": g.get("to_station_id", ""),
                        "detail": (f"标准答案包含 {g.get('train_num', '')} "
                                   f"{g.get('from_station_id', '')}→{g.get('to_station_id', '')} "
                                   f"{g.get('seat_type', '')} 段，方案未提交"
                                   "（存在性题要求与唯一标答完全一致，漏段按错处理）"),
                    })
        for item in purchase_items:
            if item["tickets"] < people_count:
                issues.append({
                    "type": "ticket_shortage",
                    "train_num": item["train_num"],
                    "from_station_id": item["from_station_id"],
                    "to_station_id": item["to_station_id"],
                    "claimed": item["tickets"], "people_count": people_count,
                    "detail": (f"{item['train_num']} 购票 {item['tickets']} 张，"
                               f"少于需求人数 {people_count}"),
                })
            if selective or not gt_purchase:
                continue
            issues.extend(_gt_mismatch_issues(item, gt_purchase))

        # ---- 层2b：选择性 = 全程可达 + 座位等级符合题目要求 ----
        if selective:
            req_seat = str(meta.get("seat_type") or "")
            if req_seat:
                for item in purchase_items:
                    if str(item.get("seat_type", "")) != req_seat:
                        issues.append({
                            "type": "seat_mismatch",
                            "train_num": item["train_num"],
                            "from_station_id": item["from_station_id"],
                            "to_station_id": item["to_station_id"],
                            "seat_type": item["seat_type"],
                            "detail": (f"题目要求{_SEAT_CN.get(req_seat, req_seat)}，"
                                       f"但 {item['train_num']} 实际购买"
                                       f"{_SEAT_CN.get(str(item.get('seat_type', '')), str(item.get('seat_type', '')))}"),
                        })
        if selective and (start_id or end_id):
            issues.extend(check_reachability(valid_items, start_id, end_id))

        # ---- 层2c：行为约束（仅当题目声明对应约束时触发） ----
        if meta.get("constraints"):
            issues.extend(check_strategy_constraints(purchase_items, meta["constraints"]))
    finally:
        conn.close()
        p_conn.close()

    correct = sum(1 for r in results if r.get("match"))
    total = len(results)
    verdict = "pass" if not issues else "hallucination"
    return {
        "total_items": total,
        "correct_items": correct,
        "issue_count": len(issues),
        "hallucination_count": sum(1 for i in issues if i.get("type") == "hallucination"),
        "price_issue_count": sum(1 for i in issues if i.get("type") == "price_wrong"),
        "price_missing_count": len(price_missing_issues),
        "price_missing_issues": price_missing_issues,
        "invalid_plan_count": len(invalid_issues),
        "route_mismatch_count": sum(1 for i in issues
                                    if str(i.get("type", "")).startswith("route_mismatch")),
        "ticket_shortage_count": sum(1 for i in issues if i.get("type") == "ticket_shortage"),
        "missing_ride_count": len(missing_ride_issues),
        "question_mode": meta.get("type"),
        "verdict": verdict,
        "results": results,
        "issues": issues,
        "summary": _generate_summary(total, correct, len(issues), verdict),
    }


def _empty_result(verdict: str, issues: List[Dict], summary: str,
                  invalid: List[Dict] = None, missing_ride: List[Dict] = None,
                  meta_type: str = None) -> Dict[str, Any]:
    """无可核查内容时的统一返回结构"""
    return {
        "total_items": 0, "correct_items": 0,
        "issue_count": len(issues),
        "hallucination_count": 0, "price_issue_count": 0,
        "price_missing_count": 0, "price_missing_issues": [],
        "invalid_plan_count": len(invalid or []),
        "route_mismatch_count": 0, "ticket_shortage_count": 0,
        "missing_ride_count": len(missing_ride or []),
        "question_mode": meta_type,
        "verdict": verdict, "results": [], "issues": issues, "summary": summary,
    }


def _station_names() -> Dict[str, str]:
    """站名映射 id→汉字（验证明细展示用）"""
    rw = get_railway_conn()
    try:
        return dict(rw.execute("SELECT station_id, station_name FROM stations").fetchall())
    finally:
        rw.close()


def _verify_tickets_and_price(item: Dict, q_conn, p_conn, name_map: Dict,
                              agg_claims: Dict, results: List[Dict],
                              issues: List[Dict], price_missing: List[Dict]):
    """层1：单购票段的余票核验 + 票价核验，结果分别追加到 results / issues / price_missing"""
    tn, f_id, t_id = item["train_num"], item["from_station_id"], item["to_station_id"]
    seat, claimed = item["seat_type"], item.get("tickets", 0)
    claimed_price = item.get("price")

    # ---- 余票 ----
    if seat not in TICKET_TABLES:
        results.append(_result_item(item, name_map, actual=None, match=False,
                                    error=f"无效座位类型: {seat}"))
        issues.append({
            "type": "invalid_seat", "train_num": tn, "from_station_id": f_id,
            "to_station_id": t_id, "seat_type": seat, "claimed": claimed,
            "actual": None, "detail": f"{tn} {seat} 无效座位类型",
        })
        return

    row = q_conn.execute(
        f"SELECT tickets FROM {seat} WHERE train_num = ? AND from_station_id = ? "
        "AND to_station_id = ?", (tn, f_id, t_id)).fetchone()
    db_tickets = row[0] if row else 0
    claimed_total = agg_claims.get((tn, f_id, t_id, seat), claimed)
    matched = db_tickets >= claimed_total
    result_item = _result_item(item, name_map, actual=db_tickets, match=matched)
    results.append(result_item)
    if not matched:
        note = f"（含同段拆分合计 {claimed_total} 张）" if claimed_total != claimed else ""
        issues.append({
            "type": "hallucination", "train_num": tn, "from_station_id": f_id,
            "to_station_id": t_id, "seat_type": seat, "claimed": claimed,
            "claimed_total": claimed_total, "actual": db_tickets,
            "detail": (f"{tn} {seat} 声称 {claimed} 张{note}，"
                       f"实际 DB 余票 {db_tickets}"),
        })

    # ---- 票价（声称了才核验；缺失=数据端问题，独立列表不进主判） ----
    if claimed_price is None:
        return
    real_price = get_price(p_conn, tn, f_id, t_id, seat)
    result_item["price_claimed"] = claimed_price
    if real_price is None:
        result_item["price_actual"] = None
        result_item["price_match"] = None
        price_missing.append({
            "type": "price_missing", "train_num": tn, "from_station_id": f_id,
            "to_station_id": t_id, "seat_type": seat, "claimed_price": claimed_price,
            "detail": (f"{tn} {f_id}→{t_id} 声称票价 {claimed_price}元，"
                       "但数据库中无此区间票价数据"),
        })
    elif abs(float(claimed_price) - real_price) > 1.0:
        result_item["price_actual"] = real_price
        result_item["price_match"] = False
        issues.append({
            "type": "price_wrong", "train_num": tn, "from_station_id": f_id,
            "to_station_id": t_id, "seat_type": seat,
            "claimed_price": claimed_price, "actual_price": real_price,
            "detail": (f"{tn} {f_id}→{t_id} 声称票价 {claimed_price}元，"
                       f"实际 {real_price}元，差价 {abs(float(claimed_price)-real_price):.1f}元"),
        })
    else:
        result_item["price_actual"] = real_price
        result_item["price_match"] = True


def _result_item(item: Dict, name_map: Dict, actual, match: bool,
                 error: str = None) -> Dict:
    """单段验证明细行（声称 vs 实际，电报码与站名并存）"""
    f_id, t_id = item["from_station_id"], item["to_station_id"]
    rf = item.get("ride_from_station_id", "")
    rt = item.get("ride_to_station_id", "")
    out = {
        "train_num": item["train_num"],
        "from_station_id": f_id, "to_station_id": t_id,
        "from_name": name_map.get(f_id, f_id), "to_name": name_map.get(t_id, t_id),
        "ride_from_station_id": rf, "ride_to_station_id": rt,
        "ride_from_name": name_map.get(rf, rf) if rf else "",
        "ride_to_name": name_map.get(rt, rt) if rt else "",
        "seat_type": item["seat_type"], "claimed": item.get("tickets", 0),
        "actual": actual, "match": match,
    }
    if error:
        out["error"] = error
    return out


_SEAT_CN = {"class0": "特等座", "class1": "一等座", "class2": "二等座"}


def _seg_key(s: Dict) -> tuple:
    """方案段唯一键（车次/购买区间/座位/乘坐区间）：提交段与标答段比对共用"""
    return (str(s.get("train_num", "")), str(s.get("from_station_id", "")),
            str(s.get("to_station_id", "")), str(s.get("seat_type", "")),
            str(s.get("ride_from_station_id") or s.get("from_station_id", "")),
            str(s.get("ride_to_station_id") or s.get("to_station_id", "")))


def _gt_mismatch_issues(item: Dict, gt_purchase: List[Dict]) -> List[Dict]:
    """存在性题层2：与标答不一致时细分四维（车次/购买区间/座位/乘坐区间）"""
    if _seg_key(item) in {_seg_key(g) for g in gt_purchase}:
        return []

    gt_trains = {str(g.get("train_num", "")) for g in gt_purchase}
    gt_routes = {(str(g.get("from_station_id", "")), str(g.get("to_station_id", "")))
                 for g in gt_purchase}
    gt_seats = {str(g.get("seat_type", "")) for g in gt_purchase}
    gt_rides = {(str(g.get("ride_from_station_id") or g.get("from_station_id", "")),
                 str(g.get("ride_to_station_id") or g.get("to_station_id", "")))
                for g in gt_purchase}

    tn, f_id, t_id = str(item["train_num"]), item["from_station_id"], item["to_station_id"]
    seat = str(item["seat_type"])
    r_f = str(item.get("ride_from_station_id") or f_id)
    r_t = str(item.get("ride_to_station_id") or t_id)

    mismatches = []
    if tn not in gt_trains:
        mismatches.append(("route_mismatch_train", "车次", tn))
    if (f_id, t_id) not in gt_routes:
        mismatches.append(("route_mismatch_route", "购买区间", f"{f_id}→{t_id}"))
    if seat not in gt_seats:
        mismatches.append(("route_mismatch_seat", "座位", seat))
    if (r_f, r_t) not in gt_rides:
        mismatches.append(("route_mismatch_ride", "乘坐区间", f"{r_f}→{r_t}"))
    if not mismatches:
        mismatches.append(("route_mismatch", "整体方案", ""))

    issues = []
    for typ, label, val in mismatches:
        detail = (f"购票段 {tn} {f_id}→{t_id} {seat} 与标准答案不一致：{label}不符（{val}）"
                  if val else
                  f"购票段 {tn} {f_id}→{t_id} {seat} 与标准答案不一致（答案唯一）")
        issues.append({
            "type": typ, "train_num": item["train_num"],
            "from_station_id": f_id, "to_station_id": t_id,
            "seat_type": item["seat_type"], "detail": detail,
        })
    return issues


def _generate_summary(total: int, correct: int, issue_count: int,
                      verdict: str = "pass") -> str:
    """核查总结一句话"""
    if total == 0:
        return "⚠️ 未检测到乘车方案（模型可能未输出 final_plan）"
    label = "✅ 全部通过" if verdict == "pass" else "❌ 存在错误（余票/票价/路线/约束问题）"
    if not issue_count:
        return f"{label}：共 {total} 条方案，均与数据库一致"
    return f"{label}：共 {total} 条方案，{correct} 条正确，{issue_count} 条有问题"


# ============================================================
# 选择性题：全程可达性
# ============================================================

def _is_hhmm(t) -> bool:
    """合法 HH:MM（"--" 等脏数据不参与时间比较，防字典序误报）"""
    return bool(re.match(r"^\d{2}:\d{2}$", str(t or "")))


def _covers(item: Dict, station_id: str, rw: sqlite3.Connection) -> bool:
    """station_id 是否在 item 实际乘坐区间内（含端点，兼容买长坐短）"""
    tn = str(item.get("train_num", ""))
    f = str(item.get("ride_from_station_id") or item.get("from_station_id", ""))
    t = str(item.get("ride_to_station_id") or item.get("to_station_id", ""))
    if not (tn and f and t):
        return False
    ids = [s["station_id"] for s in get_train_stops(rw, tn)]
    if f not in ids or t not in ids or station_id not in ids:
        return False
    return ids.index(f) <= ids.index(station_id) <= ids.index(t)


def check_reachability(plan_items: List[Dict], start_id: str,
                       end_id: str) -> List[Dict]:
    """选择性题层2：拼接所有段实际乘坐区间，检查：
    1. 地点连续：后段乘坐起点 == 前段乘坐终点
    2. 时间顺序：跨车次时前段到达 ≤ 后段发车（双侧均须合法 HH:MM；字符串比较遇
       跨天判冲突是**有意的当日完成约束**——本系统不支持跨天行程）
    3. 起终点覆盖：start 在首段乘坐区间内、end 在末段乘坐区间内
    存在断裂/未覆盖/无效段任一 → 追加汇总行 no_route。链路已断后不再判连续/时间（防连锁误报）。
    """
    issues: List[Dict] = []
    if not plan_items:
        return [{"type": "no_route", "detail": "方案为空，无法拼接出 A→B 全程"}]

    rw = get_railway_conn()
    try:
        prev_to = prev_train = prev_arr = None
        broken = False
        for i, item in enumerate(plan_items):
            if item.get("__missing_ride__"):
                issues.append({"type": "route_invalid",
                               "train_num": item.get("train_num", ""),
                               "detail": (f"第{i + 1}段缺少实际乘坐区间 ride_from/ride_to"
                                          "（按不全处理）")})
                broken = True   # 缺 ride 段衔接无法验证，避免下一段连带误报“不连续”
                continue
            tn = str(item.get("train_num", ""))
            f = str(item.get("ride_from_station_id") or item.get("from_station_id", ""))
            t = str(item.get("ride_to_station_id") or item.get("to_station_id", ""))
            if not (tn and f and t):
                issues.append({"type": "route_invalid",
                               "detail": f"第{i + 1}段缺少车次/区间信息"})
                broken = True
                continue
            stops = get_train_stops(rw, tn)
            ids = [s["station_id"] for s in stops]
            times = {s["station_id"]: s.get("stop_time") for s in stops}
            if f not in ids or t not in ids:
                issues.append({"type": "route_invalid", "train_num": tn,
                               "from_station_id": f, "to_station_id": t,
                               "detail": f"车次 {tn} 不经过 {f} 或 {t}"})
                broken = True
                continue
            if ids.index(f) >= ids.index(t):
                issues.append({"type": "route_invalid", "train_num": tn,
                               "from_station_id": f, "to_station_id": t,
                               "detail": f"车次 {tn} 上 {f}→{t} 方向/顺序错误"})
                broken = True
                continue

            if not broken and prev_to is not None and f != prev_to:
                issues.append({"type": "route_discontinuity",
                               "detail": (f"第{i}段终点 {prev_to} 与"
                                          f"第{i + 1}段起点 {f} 不连续")})
            if (not broken and prev_train is not None and prev_train != tn
                    and _is_hhmm(prev_arr) and _is_hhmm(times.get(f))
                    and str(times[f]) < str(prev_arr)):
                issues.append({"type": "transfer_time_conflict",
                               "detail": (f"{prev_train} 到达 {prev_to} {prev_arr}，"
                                          f"晚于 {tn} 从 {f} 发车 {times[f]}"
                                          "（换乘跨天/时间倒退，违反当日完成约束）")})
            prev_to, prev_train, prev_arr = t, tn, times.get(t)

        if start_id and not _covers(plan_items[0], start_id, rw):
            issues.append({"type": "start_not_covered",
                           "detail": f"方案未连接出发站 {start_id}"})
        if end_id and not _covers(plan_items[-1], end_id, rw):
            issues.append({"type": "end_not_covered",
                           "detail": f"方案未连接到达站 {end_id}"})
        if any(i.get("type") in ("route_discontinuity", "start_not_covered",
                                 "end_not_covered", "route_invalid") for i in issues):
            issues.append({"type": "no_route",
                           "detail": (f"综合以上问题，无法拼接出 {start_id or '出发站'}→"
                                      f"{end_id or '到达站'} 的完整全程")})
    finally:
        rw.close()
    return issues


# 参考版别名（旧文档/调用习惯）：check_reachability 即 _check_reachability
_check_reachability = check_reachability


# ============================================================
# 行为约束检测
# ============================================================

def check_strategy_constraints(purchase_items: List[Dict],
                               constraints: List[str]) -> List[Dict]:
    """按题目声明的行为约束检测方案购票策略（仅声明了的约束才触发）。

    - no_transfer：相邻两段车次不同 → no_transfer_violated
      （买短补长/额外购买均为单车次，不会误判换乘）
    - no_short_buy_extra（合并项，经停顺序比较区间）：
      - 乘坐区间严格包含购买区间（买短补长）→ no_short_buy_violated
      - 购买区间严格包含乘坐区间（额外购买，前/后都算）→ no_extra_violated
    """
    issues: List[Dict] = []
    no_transfer = "no_transfer" in (constraints or [])
    no_short_extra = "no_short_buy_extra" in (constraints or [])
    if not (no_transfer or no_short_extra):
        return issues

    if no_transfer:
        for s1, s2 in zip(purchase_items, purchase_items[1:]):
            if str(s1.get("train_num", "")) != str(s2.get("train_num", "")):
                issues.append({
                    "type": "no_transfer_violated",
                    "train_num": str(s1.get("train_num", "")),
                    "from_station_id": s1.get("from_station_id", ""),
                    "to_station_id": s1.get("to_station_id", ""),
                    "detail": (f"题目要求不允许换乘，但方案包含 "
                               f"{s1.get('train_num')}→{s2.get('train_num')} 跨车次换乘"),
                })
                break   # 已判定违规，继续检查短补/额外维度

    if not no_short_extra:
        return issues

    rw = get_railway_conn()
    try:
        # 同车次经停序缓存（多段同车时免重复查）
        stops_cache: Dict[str, List[str]] = {}

        def _ids(tn: str) -> List[str]:
            if tn not in stops_cache:
                stops_cache[tn] = [s["station_id"] for s in get_train_stops(rw, tn)]
            return stops_cache[tn]

        for item in purchase_items:
            tn = str(item.get("train_num", ""))
            f = str(item.get("from_station_id", ""))
            t = str(item.get("to_station_id", ""))
            rf = str(item.get("ride_from_station_id") or "")
            rt = str(item.get("ride_to_station_id") or "")
            if not (tn and f and t and rf and rt):
                continue   # 缺乘坐区间由 missing_ride 负责
            ids = _ids(tn)
            if f not in ids or t not in ids or rf not in ids or rt not in ids:
                continue
            nf, nt, nrf, nrt = ids.index(f), ids.index(t), ids.index(rf), ids.index(rt)
            if nf >= nt or nrf >= nrt:
                continue
            if nrf <= nf and nt <= nrt and (nrf < nf or nt < nrt):
                issues.append({
                    "type": "no_short_buy_violated", "train_num": tn,
                    "from_station_id": f, "to_station_id": t,
                    "detail": (f"题目要求不允许买短补长与额外购买，但 {tn} 买 {f}→{t}、"
                               f"实际乘坐 {rf}→{rt}（买短补长）"),
                })
            if nf <= nrf and nrt <= nt and (nf < nrf or nrt < nt):
                issues.append({
                    "type": "no_extra_violated", "train_num": tn,
                    "from_station_id": f, "to_station_id": t,
                    "detail": (f"题目要求不允许买短补长与额外购买，但 {tn} 买 {f}→{t}、"
                               f"实际乘坐 {rf}→{rt}（额外购买，前/后均算）"),
                })
    finally:
        rw.close()
    return issues
