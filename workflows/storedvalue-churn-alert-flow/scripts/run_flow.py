# -*- coding: utf-8 -*-
"""
储值流失预警工作流 —— 端到端编排脚本（T3）。

流程（与 SKILL.md 的 DAG 一致）：
  S1 RFM 分层（储值视角）   解析储值会员快照，按统一口径打分
  S2 预警规则引擎（本脚本确定性承担）
     红色预警：储值余额 ≥500 元 且 最近到店超过 60 天
     黄色预警：储值余额 <500 元 但 最近到店超过 90 天
     其余：正常观察
     风险敞口 = 预警名单储值余额合计

用法：
  python run_flow.py --demo
  python run_flow.py --input examples/input.json --outdir out

产物：
  out/预警名单.csv          逐人预警等级/风险敞口/挽留方案
  out/churn_alert_result.json  机器可读结果
  out/预警报告.md           预警汇总与挽留方案
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

SNAPSHOT = "2026-10-07"

# (会员, 储值余额, 最近到店距快照天数, 年消费次数, 年累计消费)
DEMO_MEMBERS = [
    ("M002 李强", 1200, 68, 3, 620),
    ("M004 陈伟", 80, 95, 1, 198),
    ("M008 周婷", 300, 52, 6, 1600),
    ("M003 张敏", 800, 15, 8, 2450),
    ("M009 吴倩", 2100, 78, 4, 980),
]

LINE_PAT = re.compile(
    r"(?P<name>M\d+\s*\S+)：储值余额(?P<sv>\d+)元，最近到店\d{4}-\d{2}-\d{2}（(?P<r>\d+)天前），"
    r"年消费(?P<f>\d+)次，年累计消费(?P<m>\d+)元"
)


def alert_level(stored: int, days: int) -> str:
    if stored >= 500 and days > 60:
        return "红色预警"
    if stored < 500 and days > 90:
        return "黄色预警"
    return "正常观察"


def retention_plan(name: str, stored: int, days: int, level: str) -> str:
    if level == "红色预警":
        return (f"店长 3 天内企微一对一致电 {name}，先关怀不促销；到店礼 ≤ 单客毛利 30%"
                f"（余额 {stored} 元，可抵扣项目优先），7 天内无回应升级短信 + 专属券")
    if level == "黄色预警":
        return f"批量触达 {name}：20 元以内到店券 + 到店理由，观察 30 天"
    return f"暂不打扰，纳入下期常规观察（{name}）"


def parse(text: str):
    rows = []
    for m in LINE_PAT.finditer(text or ""):
        rows.append((m.group("name"), int(m.group("sv")), int(m.group("r")),
                     int(m.group("f")), int(m.group("m"))))
    return rows


def run(rows):
    details = []
    for name, sv, days, f, m in rows:
        level = alert_level(sv, days)
        details.append({
            "对象": name, "储值余额": f"{sv}元", "未到店天数": f"{days}天",
            "年消费次数": f"{f}次", "年累计消费": f"{m}元",
            "预警等级": level,
            "风险敞口": f"{sv}元" if level != "正常观察" else "—",
            "挽留方案": retention_plan(name, sv, days, level),
        })
    alerted = [d for d in details if d["预警等级"] != "正常观察"]
    exposure = sum(int(d["风险敞口"].rstrip("元")) for d in alerted)
    kpi = {
        "快照日期": SNAPSHOT,
        "会员总数": len(details),
        "红色预警": sum(1 for d in details if d["预警等级"] == "红色预警"),
        "黄色预警": sum(1 for d in details if d["预警等级"] == "黄色预警"),
        "风险敞口合计": f"{exposure}元",
    }
    return details, kpi


def write_out(outdir, details, kpi):
    os.makedirs(outdir, exist_ok=True)
    csv_path = os.path.join(outdir, "预警名单.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=list(details[0].keys()))
        w.writeheader()
        w.writerows(details)

    result = {
        "workflow": "storedvalue-churn-alert-flow",
        "steps": ["S1 RFM 分层（储值视角）", "S2 预警规则引擎"],
        "rules": "红色：余额≥500元且>60天未到店；黄色：余额<500元但>90天未到店",
        "kpi": kpi,
        "details": details,
        "ai_label": "AI 生成内容，挽留动作执行前需人工确认",
    }
    with open(os.path.join(outdir, "churn_alert_result.json"), "w", encoding="utf-8") as fp:
        json.dump(result, fp, ensure_ascii=False, indent=2)

    md = ["<!-- AI 生成内容 -->", "", "# 储值流失预警 · 报告", "",
          f"- 快照日期：{SNAPSHOT}；预警规则：余额 ≥500 元且 >60 天未到店为红色，"
          "余额 <500 元但 >90 天未到店为黄色", "",
          "| 指标 | 数值 |", "|---|---|"]
    md += [f"| {k} | {v} |" for k, v in kpi.items()]
    md += ["", "## 预警名单", "",
           "| 对象 | 余额 | 未到店 | 等级 | 挽留方案 |", "|---|---|---|---|---|"]
    md += [f"| {d['对象']} | {d['储值余额']} | {d['未到店天数']} | {d['预警等级']} | {d['挽留方案']} |"
           for d in details if d["预警等级"] != "正常观察"]
    md += ["", "## 人工确认点", "",
           "1. 一对一致电话术发送前由店长确认",
           "2. 专属券面额按毛利红线（单客毛利 30% 以内）复核",
           "3. 7 天无回应后的升级动作需再次确认", ""]
    with open(os.path.join(outdir, "预警报告.md"), "w", encoding="utf-8") as fp:
        fp.write("\n".join(md))
    return csv_path


def main():
    ap = argparse.ArgumentParser(description="储值流失预警工作流编排")
    ap.add_argument("--demo", action="store_true", help="内置演示数据跑通全流程")
    ap.add_argument("--input", help="输入 JSON（含 input 字段）")
    ap.add_argument("--outdir", default=os.path.normpath(os.path.join(HERE, "..", "out")))
    args = ap.parse_args()

    text = None
    if not args.demo:
        p = args.input or os.path.normpath(os.path.join(HERE, "..", "examples", "input.json"))
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fp:
                text = json.load(fp).get("input")
    rows = parse(text) if text else DEMO_MEMBERS
    if not rows:
        rows = DEMO_MEMBERS

    details, kpi = run(rows)
    csv_path = write_out(args.outdir, details, kpi)

    print(f"[OK] 快照 {kpi['会员总数']} 名储值会员：红色 {kpi['红色预警']} 人、"
          f"黄色 {kpi['黄色预警']} 人，风险敞口 {kpi['风险敞口合计']}")
    for d in details:
        if d["预警等级"] != "正常观察":
            print(f"  [{d['预警等级']}] {d['对象']}：余额 {d['储值余额']}，{d['未到店天数']}未到店")
    print(f"[OK] 产物：{csv_path}")


if __name__ == "__main__":
    main()
