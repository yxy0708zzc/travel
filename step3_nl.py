"""
step3_nl.py —— 题目自然语言化：把结构化题面转成口语化购票需求，写回 metadata_nl.json

复刻参考项目工作流（语义 1:1）：
- 提示词 12 条生成要求（人数/座位等级隐式表达、评判标准与行为约束口语化带出）
- 存在性题按「基础题号 + 内容」分组：0_/1_ 配对共享同一份文案（保证除干扰外完全一致），
  不同题号即使内容相同也不合并
- 产物写独立文件 metadata_nl.json（不触碰 metadata.json），每题成功即落盘（中断不丢）

用法：
    # 批量并发生成（默认；跳过已有 nl 的组）
    python step3_nl.py --metadata question/metadata.json --metadata-nl question/metadata_nl.json ^
        --concurrency 5 [--ids q1 q2 ...] [--force]

    # 交互式逐组确认（[Enter]保存 [x]重新生成 [n]跳过）
    python step3_nl.py --metadata ... --metadata-nl ... --interactive

模型配置：--api-key/--model/--base-url 显式传入 > .env（NL_API_KEY/NL_MODEL→DEFAULT_MODEL、
NL_BASE_URL→DEFAULT_BASE_URL）。缺失时批量模式报错退出，交互模式提示补全。
"""

import argparse
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests

from common import config
from common import database as db

# ============================================================
# 提示词（题目语义契约的一部分，勿随意改动）
# ============================================================
NL_PROMPT_TEMPLATE = """你是一个想购买高铁票的真实用户。请根据下面的题目约束，生成一段自然、口语化的购票需求。

题目约束：
- 行程：{question}
- 人数：{people_count} 人
- 期望座位等级：{seat_label}{criterion_block}{constraint_block}

【生成要求】
1. 你可以以任何口气说，包括但不限于：尊敬、无奈、请求、无礼
2. 必须隐含"人数"和"座位等级"这两个约束，但不要用"买X张""买X等座""二等座"这种硬性指令式表达
3. 表达人数和座位等级要自然、口语化地带出
4. 座位等级：
  - class2:二等座,经济型,价格较低,不太舒适
  - class1:一等座,更舒适,价格中等
  - class0:特等座,可躺,十分舒适,价格更高
5. 必须保留出发站和到达站
6. 回答只能输出一个回答,不可输出多条
7. 不要自称"用户"或提及"题目/约束"，直接说出需求本身
8. 约束的语序要随机，可以任意布置约束的要求，口语词也可以有
9. 要求要适当隐讳
10. 不要涉及"座位挨着"。不必先从人数开始，座位等级等暗示也可以先提到。
11. 如果有评判标准（综合考虑/最快/最便宜/出发最晚/最早到达）,要用购票者口吻自然带出（如"综合考虑看看""越快越好""我要快点到""尽量晚点出发""尽量早点到"），不要用"约束:评判标准X"这种硬性指令；没有该标准就不要提。
12. 如果有行为约束（不允许换乘、不允许买短补长与额外购买）,要用购票者口吻自然带出（如"别整换乘那套""我不会换乘"等），不要用"约束:不许X"这种硬性指令；没有该约束就不要提。

[实例]:
1. 我们五个从北京南到上海虹桥，想找个比普通座舒服些、价钱又别太吓人的座儿，不会换乘，实惠点就行，帮忙看看？
2. 爸妈一起从成都东去西安北，我们年纪大了路上想躺一躺，看看有没有能躺的座位
"""

SEAT_LABELS = {"class2": "二等座", "class1": "一等座", "class0": "特等座"}
CRITERION_LABELS = {
    "comprehensive": "综合考虑", "fastest": "最快", "cheapest": "最便宜",
    "depart_latest": "出发最晚", "arrive_earliest": "最早到达",
}
CONSTRAINT_LABELS = {"no_transfer": "不允许换乘", "no_short_buy_extra": "不允许买短补长与额外购买"}
TYPE_LABELS = {"transfer": "换乘", "short_buy": "买短补长", "extra_front": "额外购买（前）",
               "extra_rear": "额外购买（后）", "mixed": "混合"}


class NoRetryError(RuntimeError):
    """不可重试错误（鉴权/参数类 4xx）"""


def build_prompt(entry: dict) -> str:
    """题目元数据 → NL 提示词（评判标准/行为约束仅选择性题存在时携带）"""
    seat_label = SEAT_LABELS.get(entry.get("seat_type", "class2"), entry.get("seat_type", ""))
    cr = CRITERION_LABELS.get(entry.get("criterion"), "")
    cc = "、".join(filter(None, (CONSTRAINT_LABELS.get(c) for c in entry.get("constraints") or [])))
    return NL_PROMPT_TEMPLATE.format(
        question=entry.get("question", ""),
        people_count=entry.get("people_count", 2),
        seat_label=seat_label,
        criterion_block=f"\n- 评判标准：{cr}" if cr else "",
        constraint_block=f"\n- 行为约束：{cc}" if cc else "",
    )


def generate_nl(api_key: str, model: str, base_url: str, prompt: str,
                timeout: float = None, max_retries: int = 3) -> str:
    """调 LLM 生成一条自然语言。网络错误/429/5xx 指数退避重试；
    401/400 等参数类错误不重试直接失败；content 为空（内容全在思考链）显式报错。"""
    import time
    timeout = timeout or config.TEST_CHAT_TIMEOUT
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": config.NL_TEMPERATURE,
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    url = f"{base_url.rstrip('/')}/chat/completions"
    last_err: Exception = RuntimeError("未发起请求")
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
            if resp.status_code in (429, 500, 502, 503, 504):
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            if resp.status_code != 200:
                raise NoRetryError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
        except NoRetryError:
            raise
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                time.sleep(min(30.0, 2 ** attempt) + 1)
            continue
        if "error" in data:
            err = data["error"]
            raise RuntimeError(err.get("message", "API 调用失败") if isinstance(err, dict) else str(err))
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"API 响应缺少 choices: {str(data)[:200]}")
        content = ((choices[0].get("message") or {}).get("content") or "").strip()
        if not content:
            raise RuntimeError("模型返回为空（content 为空，内容可能全在 reasoning_content）")
        return content
    raise RuntimeError(f"重试 {max_retries} 次仍失败: {last_err}")


# ============================================================
# 目标分组：存在性 0_/1_ 配对共享，其余每题一组
# ============================================================

def _existence_base(qid: str) -> str:
    """0_34 / 1_34 → 34（去掉数字前缀）"""
    return re.sub(r"^[0-9]_", "", str(qid))


def group_targets(targets):
    """按「基础题号 + 内容（行程/人数/座位/评判标准/行为约束）」分组；
    返回 [{"qids": [...], "entry": 首条元数据}]，保持 metadata 首现顺序。"""
    groups, by_key = [], {}
    for qid, entry in targets:
        if entry.get("type") == "存在性":
            key = (_existence_base(qid), entry.get("question", ""),
                   entry.get("people_count", 2), entry.get("seat_type", "class2"),
                   CRITERION_LABELS.get(entry.get("criterion"), ""),
                   "、".join(filter(None, (CONSTRAINT_LABELS.get(c)
                                           for c in entry.get("constraints") or []))))
        else:
            key = (qid,)   # 非存在性：每题独立成组
        if key not in by_key:
            by_key[key] = {"qids": [], "entry": entry}
            groups.append(by_key[key])
        by_key[key]["qids"].append(qid)
    return groups


# ============================================================
# 批量并发生成
# ============================================================

def run_batch(groups, cfg: dict, concurrency: int) -> dict:
    """线程池并发生成（LLM 调用为 IO 密集）；每题成功即写 metadata_nl.json。
    返回 summary：{total: 题数, groups: 组数, generated/failed: 题数}（口径统一为题）。"""
    counts = {"generated": 0, "failed": 0, "skipped": 0}
    lock = threading.Lock()
    total_groups = len(groups)
    total_questions = sum(len(g["qids"]) for g in groups)

    def one(seq: int, g: dict) -> dict:
        qid = g["qids"][0]
        try:
            nl = generate_nl(cfg["api_key"], cfg["model"], cfg["base_url"],
                             build_prompt(g["entry"]))
            # 组内所有 qid 写同一份（0_/1_ 配对一致）
            db.update_metadata_nl({q: nl for q in g["qids"]})
            with lock:
                counts["generated"] += len(g["qids"])
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ✔（{seq}/{total_groups}）{qid} → {nl}")
            return {"question_id": qid, "ok": True, "error": None}
        except Exception as e:
            with lock:
                counts["failed"] += len(g["qids"])
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ✘（{seq}/{total_groups}）{qid} 生成失败: {e}")
            return {"question_id": qid, "ok": False, "error": str(e)}

    concurrency = max(1, min(int(concurrency or 1), 20))
    print(f"批量自然语言化开始：共 {total_groups} 组 / {total_questions} 题（模型 {cfg['model']}，"
          f"温度 {config.NL_TEMPERATURE}，并发 {concurrency}）")
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(as_completed([pool.submit(one, i, g) for i, g in enumerate(groups, 1)]))
    print(f"完成：生成 {counts['generated']} / 失败 {counts['failed']} / 跳过 {counts['skipped']}"
          f"（共 {total_questions} 题，{total_groups} 组）")
    return {"total": total_questions, "groups": total_groups, **counts}


# ============================================================
# 交互式逐组确认
# ============================================================

def run_interactive(groups, cfg: dict):
    """逐组生成 + 人工确认（[Enter]保存 [x]重新生成 [n]跳过 [Ctrl+C]中止）。
    已保存的不受中止影响（每题成功即落盘）。"""
    accepted, aborted = 0, False
    try:
        for idx, g in enumerate(groups, 1):
            entry, qids = g["entry"], g["qids"]
            print("-" * 60)
            print(f"[{idx}/{len(groups)}] 组: {', '.join(qids)}")
            line = (f"  {entry.get('question', '')} ｜ {entry.get('people_count', 2)} 人 ｜ "
                    f"{SEAT_LABELS.get(entry.get('seat_type', 'class2'), '')}")
            if entry.get("criterion"):
                line += f" ｜ 评判标准：{CRITERION_LABELS.get(entry['criterion'], '')}"
            if entry.get("constraints"):
                line += " ｜ 约束：" + "、".join(filter(None, (
                    CONSTRAINT_LABELS.get(c) for c in entry["constraints"])))
            print(line)
            if len(qids) > 1:
                print(f"  ⚠ 存在性配对：生成一份写回 {len(qids)} 题")

            while True:
                print("  生成中...")
                try:
                    nl = generate_nl(cfg["api_key"], cfg["model"], cfg["base_url"],
                                     build_prompt(entry))
                except KeyboardInterrupt:
                    aborted = True
                    raise
                except Exception as e:
                    print(f"  ❌ 生成失败: {e}")
                    if input("  [Enter] 跳过此组 | [r] 重试 | [Ctrl+C] 中止: ").strip().lower() != "r":
                        print("  ⏭ 已跳过此组")
                        break
                    continue
                print(f"  ✅ {nl}")
                choice = input("  [Enter] 保存  |  [x] 重新生成  |  [n] 跳过  |  [Ctrl+C] 中止\n  → ").strip()
                if choice == "":
                    db.update_metadata_nl({q: nl for q in qids})
                    accepted += len(qids)
                    print(f"  ✔ 已保存到 {len(qids)} 题")
                    break
                if choice.lower() == "x":
                    continue
                if choice.lower() == "n":
                    print("  ⏭ 已跳过此组")
                    break
                print("  无效输入，请按 Enter / x / n / Ctrl+C")
    except KeyboardInterrupt:
        aborted = True
        print("\n\n⚠️ 检测到 Ctrl+C，中止处理。")
    print(f"\n结果：已保存 {accepted} 题" + ("（已中止，未保存的不受影响）" if aborted else ""))


# ============================================================
# CLI
# ============================================================

def main():
    ap = argparse.ArgumentParser(description="step3 题目自然语言化")
    ap.add_argument("--metadata", required=True, help="题目元数据路径（metadata.json）")
    ap.add_argument("--metadata-nl", required=True, help="NL 产物路径（metadata_nl.json）")
    ap.add_argument("--ids", nargs="*", default=None,
                    help="仅处理指定题目（存在性配对组会整组处理；默认处理所有缺 nl 的组）")
    ap.add_argument("--force", action="store_true", help="强制重新生成已保存 nl 的题目")
    ap.add_argument("--concurrency", type=int, default=5, help="批量模式并发数 1~20，默认 5")
    ap.add_argument("--interactive", action="store_true", help="交互式逐组确认（默认批量并发）")
    ap.add_argument("--api-key", default="", help="API Key（未填读 .env NL_API_KEY）")
    ap.add_argument("--model", default="", help="模型名（未填读 .env NL_MODEL→DEFAULT_MODEL）")
    ap.add_argument("--base-url", default="", help="API Base URL（未填读 .env NL_BASE_URL→DEFAULT_BASE_URL）")
    ap.add_argument("--env", default=None, help=".env 路径（默认脚本目录）")
    args = ap.parse_args()

    if args.env:
        config.ENV.update(config.load_env(args.env))
    cfg = {
        "model": args.model.strip() or config.env_get("NL_MODEL", "DEFAULT_MODEL"),
        "base_url": args.base_url.strip() or config.env_get("NL_BASE_URL", "DEFAULT_BASE_URL"),
        "api_key": args.api_key.strip() or config.env_get("NL_API_KEY"),
    }
    missing = [n for n, v in (("模型名", cfg["model"]), ("Base URL", cfg["base_url"]),
                              ("API Key", cfg["api_key"])) if not v]
    if missing:
        sys.exit(f"❌ 模型配置缺失：{'、'.join(missing)}（命令行传入或写 .env，系统不默认任何平台）")

    db.configure_paths(metadata=args.metadata, metadata_nl=args.metadata_nl)
    metadata = db.load_metadata()   # 已合并 NL 独立产物（避免"默认跳过"判断失效）

    targets = [(qid, m) for qid, m in metadata.items()
               if isinstance(m, dict) and m.get("question")]
    if not targets:
        sys.exit("❌ 没有找到含 question 字段的题目")
    groups = group_targets(targets)

    # 目标筛选：--ids → 整组处理；--force → 全部；默认 → 组内任一缺 nl 则整组处理
    if args.ids:
        qset = set(args.ids)
        gone = [q for q in qset if q not in metadata]
        if gone:
            sys.exit(f"❌ 指定题目不存在于 metadata: {', '.join(gone)}")
        target_groups = [g for g in groups if qset & set(g["qids"])]
    elif args.force:
        target_groups = groups
    else:
        merged_nl = {qid: m.get("nl_question") for qid, m in targets}
        target_groups = [g for g in groups if not all(merged_nl.get(q) for q in g["qids"])]
        skipped = len(groups) - len(target_groups)
        if skipped:
            print(f"⏭ 跳过 {skipped} 组已有自然语言（--force 可强制重新生成）")
    if not target_groups:
        print("✅ 所有题目都已有自然语言，无需生成")
        return

    if args.interactive:
        run_interactive(target_groups, cfg)
    else:
        run_batch(target_groups, cfg, args.concurrency)


if __name__ == "__main__":
    main()
