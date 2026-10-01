"""
step5_eval.py —— 测评与统计：核查测试记录 → 结果落盘 → 聚合对比 → 报告导出

复刻参考项目工作流（语义 1:1）：
- eval：读 logs/test 测试记录 → verifier 核查 → 结果写 logs/result → state=evaluated；
  空方案按测试记录 plan_status 细分 verdict（模型明确无解=empty_plan / 未输出 JSON=no_plan），
  不再一律覆写成 empty_plan；重测时删除同测试记录的旧结果，避免重复计数
- stats：按模型聚合 verdict 分布/通过率/token/耗时/模型与工具调用计数/问题码分布
- report：模型 pass_rate 排名 + 题型（存在性/选择性、五题型）难度对比 + 洞察，
  导出 JSON 与 Markdown 到 --report-dir（JSON 只保留最近 50 份）

用法：
    python step5_eval.py eval   --logs-test logs/test --logs-result logs/result ^
        --question-dir question --railway-db data/railway.db [--ids f1.json f2.json] [--re-evaluate]
    python step5_eval.py stats  --logs-result logs/result --logs-test logs/test ^
        --question-dir question --railway-db data/railway.db
    python step5_eval.py report --logs-result logs/result --logs-test logs/test ^
        --report-dir logs/report --question-dir question --railway-db data/railway.db
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import database as db
from verifier import verify_final_plan


# ============================================================
# 通用：测评结果/测试记录读取
# ============================================================

def load_results(logs_result: str) -> list:
    """读目录下全部测评结果 json（损坏文件跳过并提示）"""
    results = []
    if not os.path.isdir(logs_result):
        return results
    for f in sorted(os.listdir(logs_result)):
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(logs_result, f), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            data["_filename"] = f
            results.append(data)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[step5] 读取失败 {f}: {e}")
    return results


def evaluated_test_files(logs_result: str) -> dict:
    """test_file → 旧结果文件名列表（--re-evaluate 重测时删除旧结果，防重复计数）"""
    out: dict = {}
    for r in load_results(logs_result):
        tf = r.get("test_file")
        if tf:
            out.setdefault(tf, []).append(r.get("_filename", ""))
    return out


def verdict_of(result: dict) -> str:
    return (result.get("verification") or {}).get("verdict", "unknown")


def load_test_record(logs_test: str, filename: str) -> dict:
    path = os.path.join(logs_test, os.path.basename(filename))
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


# ============================================================
# eval 子命令
# ============================================================

def run_eval(logs_test: str, logs_result: str, ids: list, re_evaluate: bool):
    os.makedirs(logs_result, exist_ok=True)
    done = evaluated_test_files(logs_result)
    files = [f for f in sorted(os.listdir(logs_test), reverse=True)
             if f.endswith(".json")] if os.path.isdir(logs_test) else []
    if ids:
        files = [f if f.endswith(".json") else f + ".json" for f in ids]
        missing = [f for f in files if not os.path.exists(os.path.join(logs_test, f))]
        if missing:
            sys.exit(f"❌ 测试记录不存在: {', '.join(missing)}")
    todo = [f for f in files if re_evaluate or f not in done]
    skipped = len(files) - len(todo)
    if skipped:
        print(f"⏭ 跳过 {skipped} 条已测评记录（--re-evaluate 可强制重测）")
    if not todo:
        print("没有待测评的测试记录")
        return

    print(f"批量测评开始：共 {len(todo)} 条测试记录")
    verdict_count: dict = {}
    ok_count = 0
    for i, fn in enumerate(todo, 1):
        try:
            with open(os.path.join(logs_test, fn), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            qid = data.get("question_id", "") or "unknown"
            final_plan = data.get("final_plan") or []
            verification = verify_final_plan(final_plan, qid)
            plan_status = str(data.get("plan_status") or "")
            if not final_plan:
                # 空方案按测试记录的 plan_status 细分：模型明确输出空数组（无解）≠ 没输出 JSON
                if plan_status == "empty_plan":
                    verification["verdict"] = "empty_plan"
                    verification["summary"] = "⚠️ 模型明确输出无解（final_plan 为空数组）"
                else:
                    verification["verdict"] = "no_plan"
                    verification["summary"] = ("⚠️ 模型未输出 final_plan JSON"
                                               "（对话轮数用尽/输出格式错误）")
            verdict = verification.get("verdict", "unknown")
            verdict_count[verdict] = verdict_count.get(verdict, 0) + 1

            tu = data.get("token_usage") or {}
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            # 模型名可能带 / 或 :（OpenRouter/Ollama 风格），直接拼文件名会 FileNotFoundError/非法路径
            safe_model = re.sub(r'[\\/:*?"<>|]', "_",
                                data.get("model_name", "unknown") or "unknown")
            result_name = f"{ts}_{safe_model}_{qid}.json"
            record = {
                "timestamp": datetime.now().isoformat(),
                "test_file": fn,
                "question_id": qid,
                "plan_status": plan_status,
                "model_name": data.get("model_name", "unknown"),
                "user_input": data.get("user_input", ""),
                "score_summary": {
                    "hallucination_count": verification.get("hallucination_count", 0),
                    "issue_count": verification.get("issue_count", 0),
                    "total_tokens": tu.get("total_tokens", 0),
                    "duration_seconds": data.get("duration", 0),
                    "tool_calls": [],
                },
                "verification": verification,
                "human_confirmed": False,
                "human_notes": "批量测评自动生成",
                "status": "success",
            }
            with open(os.path.join(logs_result, result_name), "w", encoding="utf-8") as fh:
                json.dump(record, fh, ensure_ascii=False, indent=2)

            # 重测场景：删除该测试记录的旧结果文件（防 aggregate 重复计数）
            olds = [o for o in done.get(fn, []) if o and o != result_name]
            for old in olds:
                old_path = os.path.join(logs_result, old)
                if os.path.exists(old_path):
                    os.remove(old_path)
            if olds:
                print(f"  🗑 已删除 {fn} 的 {len(olds)} 份旧测评结果")

            model_name = data.get("model_name", "unknown")
            if qid != "unknown" and model_name:
                db.set_question_model_state(qid, model_name, "evaluated")

            ok_count += 1
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ✔（{i}/{len(todo)}）{fn} → "
                  f"{qid} verdict={verdict}，问题 {verification.get('issue_count', 0)} 个"
                  f" → {result_name}")
        except SystemExit:
            raise
        except Exception as e:
            verdict_count["error"] = verdict_count.get("error", 0) + 1
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ✘（{i}/{len(todo)}）{fn} 测评失败：{e}")

    print(f"\n批量测评完成：成功 {ok_count} / 失败 {len(todo) - ok_count}，"
          f"verdict 分布 {verdict_count}")


# ============================================================
# stats 子命令（聚合口径同参考 aggregator）
# ============================================================

def _trace_counts(test_data: dict) -> tuple:
    """从测试记录 trace 统计 (模型调用, 工具调用)：assistant 轮数 / 各轮嵌套 tools 总和"""
    trace = test_data.get("trace") or []
    rounds = [e for e in trace if isinstance(e, dict) and e.get("type") == "assistant"]
    return len(rounds), sum(len(e.get("tools") or []) for e in rounds)


def aggregate(logs_result: str, logs_test: str) -> dict:
    results = load_results(logs_result)
    if not results:
        return {"total_tests": 0, "models": {}, "summary": {}}

    # 按模型分组
    groups: dict = {}
    for r in results:
        groups.setdefault(r.get("model_name", "unknown"), []).append(r)

    models_data = {}
    for model, group in groups.items():
        n = len(group)
        vcount = {"pass": 0, "hallucination": 0, "no_plan": 0,
                  "empty_plan": 0, "db_not_found": 0}
        for r in group:
            v = verdict_of(r)
            vcount[v] = vcount.get(v, 0) + 1

        # token/耗时/双调用计数（读关联测试记录；同文件只读一次）
        total_tokens = total_duration = total_model = total_tool = 0
        td_map: dict = {}
        for r in group:
            base = os.path.basename(r.get("test_file", "") or "")
            if not base:
                continue
            td = td_map.get(base)
            if td is None:
                td = load_test_record(logs_test, base)
                td_map[base] = td
            if td:
                total_tokens += (td.get("token_usage") or {}).get("total_tokens", 0)
                total_duration += td.get("duration", 0)
                mc, tc = _trace_counts(td)
                # 双调用计数：优先测试记录显式字段，旧记录由 trace 推导兜底
                total_model += td["model_calls"] if td.get("model_calls") is not None else mc
                total_tool += td["tool_calls"] if td.get("tool_calls") is not None else tc
        k = len(td_map)

        issue_types: dict = {}
        for r in group:
            for issue in (r.get("verification") or {}).get("issues", []):
                t = issue.get("type", "")
                issue_types[t] = issue_types.get(t, 0) + 1

        models_data[model] = {
            "total_tests": n,
            "success_count": sum(1 for r in group if r.get("status") == "success"),
            "completion_rate": round(sum(1 for r in group if r.get("status") == "success")
                                     / n * 100, 1) if n else 0,
            "pass_count": vcount["pass"],
            "pass_rate": round(vcount["pass"] / n * 100, 1) if n else 0,
            "error_count": vcount["hallucination"],
            "error_rate": round(vcount["hallucination"] / n * 100, 1) if n else 0,
            "no_plan_count": vcount["no_plan"],
            "empty_count": vcount["empty_plan"],
            "db_count": vcount["db_not_found"],
            "unknown_count": n - sum(vcount.values()),
            "avg_tokens": round(total_tokens / k, 1) if k else 0,
            "avg_duration": round(total_duration / k, 2) if k else 0,
            "avg_model_calls": round(total_model / k, 1) if k else 0,
            "avg_tool_calls": round(total_tool / k, 1) if k else 0,
            "issue_type_counts": issue_types,
        }

    total = len(results)
    return {
        "total_tests": total,
        "models": models_data,
        "summary": {
            "total_tests": total,
            "pass_rate": round(sum(m["pass_count"] for m in models_data.values()) / total * 100, 1)
                         if total else 0,
            "error_rate": round(sum(m["error_count"] for m in models_data.values()) / total * 100, 1)
                          if total else 0,
        },
    }


def run_stats(logs_result: str, logs_test: str):
    data = aggregate(logs_result, logs_test)
    if not data["models"]:
        print("尚无测评数据（先运行 eval 子命令）")
        return
    print(f"总测试数：{data['total_tests']}\n")
    header = (f"{'模型':<28}{'题数':>5}{'通过率':>8}{'错误率':>8}{'未规划':>7}{'空方案':>7}"
              f"{'平均token':>10}{'平均耗时s':>10}{'模型调用':>9}{'工具调用':>9}")
    print(header)
    print("-" * len(header))
    for name, m in sorted(data["models"].items(), key=lambda kv: -kv[1]["pass_rate"]):
        print(f"{name:<28}{m['total_tests']:>5}{m['pass_rate']:>7}%{m['error_rate']:>7}%"
              f"{m['no_plan_count']:>7}{m['empty_count']:>7}{m['avg_tokens']:>10}"
              f"{m['avg_duration']:>10}{m['avg_model_calls']:>9}{m['avg_tool_calls']:>9}")
    print("\n问题码分布（按模型）：")
    for name, m in data["models"].items():
        if m["issue_type_counts"]:
            dist = "，".join(f"{k}×{v}" for k, v in
                             sorted(m["issue_type_counts"].items(), key=lambda kv: -kv[1]))
            print(f"  {name}: {dist}")


# ============================================================
# report 子命令（排名 + 维度对比 + 洞察，导出 JSON/Markdown）
# ============================================================

def _group_stats(group: list) -> dict:
    n = len(group)
    p = sum(1 for r in group if verdict_of(r) == "pass")
    e = sum(1 for r in group if verdict_of(r) == "hallucination")
    return {"count": n,
            "pass_count": p, "pass_rate": round(p / n * 100, 1) if n else 0,
            "error_count": e, "error_rate": round(e / n * 100, 1) if n else 0,
            "no_plan_count": sum(1 for r in group if verdict_of(r) == "no_plan"),
            "empty_count": sum(1 for r in group if verdict_of(r) == "empty_plan")}


def build_report(logs_result: str, logs_test: str) -> dict:
    data = aggregate(logs_result, logs_test)
    ranking = [{"model": name, "completion_rate": m["completion_rate"],
                "pass_rate": m["pass_rate"], "error_rate": m["error_rate"]}
               for name, m in data["models"].items()]
    ranking.sort(key=lambda x: (-x["pass_rate"], x["error_rate"]))

    # 题目维度分组：type（存在性/选择性）与 question_type（五题型），读合并视图元数据
    try:
        meta = db.load_metadata() or {}
    except Exception:
        meta = {}
    type_groups: dict = {}
    qtype_groups: dict = {}
    for r in load_results(logs_result):
        m = meta.get(r.get("question_id", "")) or {}
        type_groups.setdefault(m.get("type") or "未知", []).append(r)
        qtype_groups.setdefault(m.get("question_type") or "未知", []).append(r)

    warnings = [f"{r['model']} 错误率 {r['error_rate']}%，超过 10% 阈值"
                for r in ranking if r["error_rate"] > 10]

    insights = []
    if ranking:
        insights.append(f"{ranking[0]['model']} 在所有模型中通过率最高（{ranking[0]['pass_rate']}%）")
        insights.append(f"{ranking[-1]['model']} 在所有模型中通过率最低（{ranking[-1]['pass_rate']}%）")
    by_qt = {k: _group_stats(g) for k, g in qtype_groups.items()}
    if by_qt:
        order = sorted(by_qt.items(), key=lambda kv: -kv[1]["pass_rate"])
        insights.append(f"题型从易到难排序：{' > '.join(k for k, _ in order)}")
        insights.append(f"最容易：{order[0][0]}（{order[0][1]['pass_rate']}%）；"
                        f"最难：{order[-1][0]}（{order[-1][1]['pass_rate']}%）")
    by_type = {k: _group_stats(g) for k, g in type_groups.items()}
    if by_type:
        order = sorted(by_type.items(), key=lambda kv: -kv[1]["pass_rate"])
        insights.append(f"题目类型从易到难排序：{' > '.join(k for k, _ in order)}")

    return {
        "generated_at": datetime.now().isoformat(),
        "total_tests": data["total_tests"],
        "models_compared": [r["model"] for r in ranking],
        "model_ranking": ranking,
        "by_question_type": {"by_type": by_type, "by_question_type": by_qt},
        "hallucination_warning": warnings,
        "insights": insights,
    }


def run_report(logs_result: str, logs_test: str, report_dir: str):
    os.makedirs(report_dir, exist_ok=True)
    report = build_report(logs_result, logs_test)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = os.path.join(report_dir, f"summary_{ts}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    # 只保留最近 50 份 JSON 快照
    try:
        olds = sorted(f for f in os.listdir(report_dir)
                      if re.fullmatch(r"summary_\d{8}_\d{6}\.json", f))
        for name in olds[:-50]:
            os.remove(os.path.join(report_dir, name))
    except OSError:
        pass

    # Markdown
    lines = ["# Benchmark 统计报告\n",
             f"**生成时间**: {report['generated_at']}\n",
             f"**总测试数**: {report['total_tests']}\n",
             f"**参与模型**: {', '.join(report['models_compared']) or '无'}\n",
             "## 模型综合排名\n",
             "| 排名 | 模型 | 通过率 | 错误率 |", "|---|---|---|---|"]
    for i, r in enumerate(report["model_ranking"], 1):
        lines.append(f"| {i} | {r['model']} | {r['pass_rate']}% | {r['error_rate']}% |")
    for title, key in (("题型难度分析", "by_question_type"), ("题目类型分析", "by_type")):
        lines.append(f"\n## {title}\n")
        lines.append("| 维度 | 测试数 | 通过率 | 错误率 |")
        lines.append("|---|---|---|---|")
        dim = report["by_question_type"].get(key, {})
        for k, d in sorted(dim.items(), key=lambda kv: -kv[1]["pass_rate"]):
            lines.append(f"| {k} | {d['count']} | {d['pass_rate']}% | {d['error_rate']}% |")
    if report["hallucination_warning"]:
        lines.append("\n## 🚨 警告\n")
        lines += [f"- **{w}**" for w in report["hallucination_warning"]]
    lines.append("\n## 📊 洞察与结论\n")
    lines += [f"- {s}" for s in report["insights"]] or ["- 尚无测评数据"]
    md = "\n".join(lines) + "\n"

    md_path = os.path.join(report_dir, f"summary_{ts}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)

    print(md)
    print(f"报告已保存：\n  {json_path}\n  {md_path}")


# ============================================================
# CLI
# ============================================================

def main():
    ap = argparse.ArgumentParser(description="step5 测评与统计（eval / stats / report）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--railway-db", required=True, help="基础数据库路径（核查经停/站名用，只读）")
        p.add_argument("--prices-db", required=True, help="票价数据库路径（票价核查用，只读）")
        p.add_argument("--question-dir", required=True, help="题目目录（核查读 {qid}.db）")
        p.add_argument("--metadata", default=None,
                       help="题目元数据路径（默认 {question-dir}/metadata.json）")
        p.add_argument("--metadata-nl", default=None, help="NL 产物路径（可选）")
        p.add_argument("--env", default=None, help=".env 路径（默认脚本目录）")

    p_eval = sub.add_parser("eval", help="批量测评：测试记录 → 代码核查 → 结果落盘")
    common(p_eval)
    p_eval.add_argument("--logs-test", required=True, help="测试记录目录（输入）")
    p_eval.add_argument("--logs-result", required=True, help="测评结果目录（输出）")
    p_eval.add_argument("--ids", nargs="*", default=None,
                        help="仅测评指定记录文件名（默认全部未测评记录）")
    p_eval.add_argument("--re-evaluate", action="store_true", help="已测评的记录也重新测评")

    p_stats = sub.add_parser("stats", help="按模型聚合统计（verdict 分布/通过率/双调用）")
    common(p_stats)
    p_stats.add_argument("--logs-result", required=True, help="测评结果目录（输入）")
    p_stats.add_argument("--logs-test", required=True, help="测试记录目录（token/耗时溯源）")

    p_rep = sub.add_parser("report", help="生成排名/题型对比报告并导出 JSON + Markdown")
    common(p_rep)
    p_rep.add_argument("--logs-result", required=True, help="测评结果目录（输入）")
    p_rep.add_argument("--logs-test", required=True, help="测试记录目录（输入）")
    p_rep.add_argument("--report-dir", required=True, help="报告输出目录")

    args = ap.parse_args()
    if args.env:
        from common import config
        config.ENV.update(config.load_env(args.env))
    db.configure_paths(railway_db=args.railway_db, prices_db=args.prices_db,
                       question_dir=args.question_dir,
                       metadata=args.metadata, metadata_nl=args.metadata_nl)

    if args.cmd == "eval":
        run_eval(args.logs_test, args.logs_result, args.ids, args.re_evaluate)
    elif args.cmd == "stats":
        run_stats(args.logs_result, args.logs_test)
    else:
        run_report(args.logs_result, args.logs_test, args.report_dir)


if __name__ == "__main__":
    main()
