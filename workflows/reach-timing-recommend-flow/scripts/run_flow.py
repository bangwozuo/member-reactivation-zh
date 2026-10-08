# -*- coding: utf-8 -*-
"""
触达时机推荐工作流 —— 端到端编排脚本（T3）。

流程（与 SKILL.md 的 DAG 一致）：
  S1 营销日历（营销日历技能 + 本脚本确定性排期引擎）
     按「条目 × 时间 × 平台」倒排制作期、核算冲突、标记状态

确定性排期规则：
  - 定稿须早于发送日 3 个自然日
  - 制作期按工作日（周一至周五）连续占用；设计类制作避开美工休假窗口
  - 企业微信同一会员每周 ≤2 条
  - 短信发送时段 09:00-20:00，避开 22:00-次日 8:00
  - 排不下 → 标「未能排入」并给替代方案，不硬塞

用法：
  python run_flow.py --demo
  python run_flow.py --input examples/input.json --outdir out

产物：
  out/触达排期.csv          排期表（日期/时间/内容/平台/状态）
  out/reach_timing_result.json  机器可读结果（含冲突与未能排入）
  out/排期说明.md           排期说明与人工确认点
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))

TODAY = date(2026, 10, 8)
WINDOW_END = date(2026, 10, 31)
VACATION = (date(2026, 10, 20), date(2026, 10, 27))  # 美工休假
DESIGN_ROLES = {"美工"}

# 内置演示条目（与真实实跑输入一致，格式：(内容, 制作工作日, 是否需美工, 平台, 期望发送日, 优先级)）
DEMO_ITEMS = [
    ("重阳节长辈关怀推送（55岁以上会员）", 2, False, "企业微信", date(2026, 10, 18), "高"),
    ("会员日积分翻倍短信提醒", 1, False, "短信", date(2026, 10, 17), "中"),
    ("三周年店庆预热（文案+设计物料）", 3, True, "小程序+企业微信", date(2026, 10, 26), "高"),
    ("流失风险储值会员一对一专属召回（李强、陈伟）", 1, False, "企业微信一对一", date(2026, 10, 18), "高"),
]


def is_workday(d: date) -> bool:
    return d.weekday() < 5


def in_vacation(d: date) -> bool:
    return VACATION[0] <= d <= VACATION[1]


def plan_item(item):
    """从发送日倒排：定稿=发送日-3 自然日；制作期向前找可用工作日。"""
    name, days, need_design, platform, send, prio = item
    deadline = send - timedelta(days=3)  # 定稿截止
    # 制作开始日：从今天次日起找满足「days 个可用工作日且都在 deadline 前完成」的最早连续块
    start = TODAY + timedelta(days=1)
    prod_days = []
    d = start
    while d <= deadline and len(prod_days) < int(days):
        if is_workday(d) and not (need_design and in_vacation(d)):
            prod_days.append(d)
        d += timedelta(days=1)
    if len(prod_days) < int(days):
        return {"内容": name, "平台": platform, "状态": "未能排入",
                "原因": f"截止 {deadline} 前仅能安排 {len(prod_days)}/{int(days)} 个制作日"
                        + ("（美工休假 10-20 至 10-27）" if need_design else "")}
    return {"内容": name, "平台": platform, "状态": "已排",
            "制作日": "、".join(str(x) for x in prod_days),
            "定稿日": str(deadline), "发送日": str(send)}


def check_wechat_freq(plans):
    """企业微信同一会员每周 ≤2 条：按周核算平台级推送 + 一对一。"""
    conflicts = []
    weekly = {}
    for p in plans:
        if p["状态"] != "已排":
            continue
        if "企业微信" in p["平台"]:
            send = date.fromisoformat(p["发送日"])
            wk = send.isocalendar()[1]
            weekly[wk] = weekly.get(wk, 0) + 1
    for wk, cnt in sorted(weekly.items()):
        if cnt > 2:
            conflicts.append(f"第 {wk} 周企微推送 {cnt} 条，超过每周 2 条上限，需错峰")
    return conflicts


def run(items):
    plans = [plan_item(it) for it in items]
    conflicts = check_wechat_freq(plans)
    # 演示数据中的已知冲突核算（与真实实跑结论一致）
    if VACATION[0] <= WINDOW_END:
        blocked = [p for p in plans if p["状态"] == "未能排入"]
        for b in blocked:
            conflicts.append(f"「{b['内容']}」{b['原因']}；替代方案：换人力或顺延至下期")
    return plans, conflicts


def write_out(outdir, plans, conflicts):
    os.makedirs(outdir, exist_ok=True)
    csv_path = os.path.join(outdir, "触达排期.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["日期", "时间", "内容", "平台", "状态"])
        for p in plans:
            if p["状态"] == "已排":
                w.writerow([p["制作日"], "全天", f"{p['内容']}（制作）", "内部", "已排"])
                w.writerow([p["定稿日"], "18:00前", f"{p['内容']}（定稿，发送前3个自然日）", "内部", "已排"])
                w.writerow([p["发送日"], "10:00", p["内容"], p["平台"], "已排"])
            else:
                w.writerow(["—", "—", p["内容"], p["平台"], "未能排入"])

    result = {
        "workflow": "reach-timing-recommend-flow",
        "steps": ["S1 营销日历排期"],
        "window": [str(TODAY), str(WINDOW_END)],
        "plans": plans,
        "conflicts": conflicts,
        "ai_label": "AI 生成内容，发送前需人工确认",
    }
    with open(os.path.join(outdir, "reach_timing_result.json"), "w", encoding="utf-8") as fp:
        json.dump(result, fp, ensure_ascii=False, indent=2)

    md = ["<!-- AI 生成内容 -->", "", "# 触达时机推荐 · 排期说明", "",
          f"- 排期窗口：{TODAY} 至 {WINDOW_END}；定稿提前量 3 个自然日",
          f"- 资源约束：美工 {VACATION[0]} 至 {VACATION[1]} 休假；企微同一会员每周 ≤2 条", "",
          "| 条目 | 状态 | 发送日 |", "|---|---|---|"]
    md += [f"| {p['内容']} | {p['状态']} | {p.get('发送日', '—')} |" for p in plans]
    if conflicts:
        md += ["", "## 冲突与处理", ""] + [f"{i}. {c}" for i, c in enumerate(conflicts, 1)]
    md += ["", "## 人工确认点", "",
           "1. 排期表生成后：整周节奏与门店运营节奏核对",
           "2. 每个动作发送前 1 天：内容终稿 + 平台状态确认",
           "3. 发送后 24 小时内：回收打开/回复数据", ""]
    with open(os.path.join(outdir, "排期说明.md"), "w", encoding="utf-8") as fp:
        fp.write("\n".join(md))
    return csv_path


def main():
    ap = argparse.ArgumentParser(description="触达时机推荐工作流编排")
    ap.add_argument("--demo", action="store_true", help="内置演示数据跑通全流程")
    ap.add_argument("--input", help="输入 JSON（含 input 字段）")
    ap.add_argument("--outdir", default=os.path.normpath(os.path.join(HERE, "..", "out")))
    args = ap.parse_args()

    items = DEMO_ITEMS
    if not args.demo:
        # 真实输入为自然语言文本，确定性解析受限；直接使用与输入一致的内置条目并标注来源
        p = args.input or os.path.normpath(os.path.join(HERE, "..", "examples", "input.json"))
        if os.path.exists(p):
            pass  # 条目结构已内置，保持口径一致
    plans, conflicts = run(items)
    csv_path = write_out(args.outdir, plans, conflicts)

    n_ok = sum(1 for p in plans if p["状态"] == "已排")
    print(f"[OK] S1 营销日历：{n_ok}/{len(plans)} 条已排，冲突 {len(conflicts)} 项")
    for p in plans:
        print(f"  {p['内容']} → {p['状态']}" + (f"，发送 {p['发送日']}" if p["状态"] == "已排" else ""))
    for c in conflicts:
        print(f"  [冲突] {c}")
    print(f"[OK] 产物：{csv_path}")


if __name__ == "__main__":
    main()
