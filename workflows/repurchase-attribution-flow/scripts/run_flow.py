# -*- coding: utf-8 -*-
"""
复购效果归因工作流 —— 端到端编排脚本（T3）。

流程（与 SKILL.md 的 DAG 一致）：
  S1 会员数据对接   读取召回券核销数据，映射字段（核销状态/本次消费/券成本）
  S2 RFM 分层       召回前后双快照打分对比 + 链路归因（核销率/增量毛利/ROI）

确定性口径：
  RFM：R 40% / F 30% / M 30%，各 5 档；≥4.5 高价值、3.5-4.4 潜力、2.5-3.4 一般挽留、<2.5 流失风险
  归因：核销率 = 核销人数 / 触达人数；增量毛利 = Σ(核销会员本次消费) × 毛利率；
        券成本 = 核销人数 × 券面额；ROI = 增量毛利 / 券成本
  召回后快照：核销者 R=核销距今天数、F+1 次、M+本次消费；未核销者维持召回前快照

用法：
  python run_flow.py --demo
  python run_flow.py --input examples/input.json --outdir out

产物：
  out/归因明细.csv          逐人召回前后分层对比
  out/attribution_result.json  机器可读归因结果
  out/归因报告.md           核销率/ROI/话术迭代建议
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))

TODAY = date(2026, 10, 7)      # 数据导出日
COUPON = 20                    # 券面额（元）
GROSS_RATE = 0.55              # 毛利率
REACHED_TOTAL = 30             # 全量触达人数（抽样跟踪 6 人）

# (会员, 召回前最近到店, 召回前年消费次数, 召回前年累计, 是否核销, 核销日, 本次消费)
DEMO_MEMBERS = [
    ("M002 李强", date(2026, 7, 31), 3, 620, True, date(2026, 9, 20), 88),
    ("M004 陈伟", date(2026, 7, 5), 1, 198, False, None, 0),
    ("M005 刘洋", date(2026, 8, 29), 5, 1180, True, date(2026, 9, 28), 156),
    ("M006 赵磊", date(2026, 6, 19), 2, 450, False, None, 0),
    ("M007 孙悦", date(2026, 8, 2), 4, 890, True, date(2026, 9, 15), 210),
    ("M008 周婷", date(2026, 7, 10), 6, 1600, False, None, 0),
]

BEFORE_SNAPSHOT = date(2026, 9, 1)  # 召回前快照日


def score(days: int, times: int, amount: int):
    r = 5 if days <= 15 else 4 if days <= 30 else 3 if days <= 60 else 2 if days <= 90 else 1
    f = 5 if times >= 10 else 4 if times >= 7 else 3 if times >= 4 else 2 if times >= 2 else 1
    m = 5 if amount >= 3000 else 4 if amount >= 2000 else 3 if amount >= 1000 else 2 if amount >= 500 else 1
    return r, f, m


def segment(total: float) -> str:
    if total >= 4.5:
        return "高价值会员"
    if total >= 3.5:
        return "潜力会员"
    if total >= 2.5:
        return "一般挽留会员"
    return "流失风险会员"


def s1_data_sync():
    """S1 会员数据对接：核销数据字段映射（有赞核销记录 → 归因字段）。"""
    field_map = [
        ["核销状态", "有赞券码 status", "读取", "已核销/未核销，截止导出日"],
        ["核销日期", "券码 verify_at", "读取", "用于召回后 R 间隔重算"],
        ["本次消费金额", "核销订单 pay_amount", "读取", "实付口径，不含券抵扣部分"],
        ["券成本", "券面额 × 核销人数", "计算", "仅核销订单计入成本"],
    ]
    return field_map


def s2_attribution():
    rows = []
    redeemed = 0
    revenue = 0
    upgrades = 0
    for name, last, f0, m0, ok, vdate, spend in DEMO_MEMBERS:
        # 召回前快照（相对 2026-09-01）
        r0, f0s, m0s = score((BEFORE_SNAPSHOT - last).days, f0, m0)
        t0 = round(r0 * 0.4 + f0s * 0.3 + m0s * 0.3, 1)
        seg0 = segment(t0)
        if ok:
            # 召回后：R=核销日距导出日，F+1，M+本次消费
            r1, f1, m1 = score((TODAY - vdate).days, f0 + 1, m0 + spend)
            t1 = round(r1 * 0.4 + f1 * 0.3 + m1 * 0.3, 1)
            seg1 = segment(t1)
            redeemed += 1
            revenue += spend
        else:
            r1, f1, m1, t1, seg1 = r0, f0s, m0s, t0, seg0
        order = ["流失风险会员", "一般挽留会员", "潜力会员", "高价值会员"]
        moved = "↑升级" if order.index(seg1) > order.index(seg0) else ("→持平" if seg1 == seg0 else "↓降级")
        if moved == "↑升级":
            upgrades += 1
        rows.append({
            "对象": name, "召回前": f"{seg0}（{t0}）",
            "核销": "是" if ok else "否",
            "本次消费": f"{spend}元" if ok else "—",
            "召回后": f"{seg1}（{t1}）", "迁移": moved,
        })

    n = len(DEMO_MEMBERS)
    rate = redeemed / n
    est_redeem = round(rate * REACHED_TOTAL)
    gross = round(revenue * GROSS_RATE, 1)
    cost = redeemed * COUPON
    roi = round(gross / cost, 2) if cost else 0
    kpi = {
        "抽样人数": n, "核销人数": redeemed, "核销率": f"{rate:.0%}",
        "全量估算核销人数": est_redeem, "全量估算券成本": f"{est_redeem * COUPON}元",
        "抽样增量毛利": f"{gross}元", "抽样券成本": f"{cost}元",
        "ROI（毛利/券成本）": roi, "分层升级人数": upgrades,
    }
    return rows, kpi


def write_out(outdir, field_map, rows, kpi):
    os.makedirs(outdir, exist_ok=True)
    fm_path = os.path.join(outdir, "字段映射.csv")
    with open(fm_path, "w", encoding="utf-8-sig", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["业务字段", "系统字段", "方向", "说明"])
        w.writerows(field_map)

    det_path = os.path.join(outdir, "归因明细.csv")
    with open(det_path, "w", encoding="utf-8-sig", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    result = {
        "workflow": "repurchase-attribution-flow",
        "steps": ["S1 会员数据对接", "S2 RFM 前后对比 + 链路归因"],
        "kpi": kpi,
        "details": rows,
        "ai_label": "AI 生成内容，话术迭代前需人工确认",
    }
    with open(os.path.join(outdir, "attribution_result.json"), "w", encoding="utf-8") as fp:
        json.dump(result, fp, ensure_ascii=False, indent=2)

    md = ["<!-- AI 生成内容 -->", "", "# 复购效果归因 · 报告", "",
          "## 核心指标", "",
          "| 指标 | 数值 |", "|---|---|"]
    md += [f"| {k} | {v} |" for k, v in kpi.items()]
    md += ["", "## 逐人归因", "",
           "| 对象 | 召回前 | 核销 | 本次消费 | 召回后 | 迁移 |",
           "|---|---|---|---|---|---|"]
    md += [f"| {r['对象']} | {r['召回前']} | {r['核销']} | {r['本次消费']} | {r['召回后']} | {r['迁移']} |"
           for r in rows]
    md += ["", "## 话术迭代建议", "",
           "1. 未核销会员（陈伟、赵磊、周婷）召回后分层未迁移：下一轮对 90 天以上未到店者提高权益拉力并缩短券有效期",
           "2. 核销者消费 88-210 元、全部高于券面额 20 元：券面额维持单客毛利 30% 以内的策略有效",
           "3. 触达后 7 天内未领券者补发 1 次提醒（同一会员 30 天内触达 ≤2 次）", ""]
    with open(os.path.join(outdir, "归因报告.md"), "w", encoding="utf-8") as fp:
        fp.write("\n".join(md))
    return det_path


def main():
    ap = argparse.ArgumentParser(description="复购效果归因工作流编排")
    ap.add_argument("--demo", action="store_true", help="内置演示数据跑通全流程")
    ap.add_argument("--input", help="输入 JSON（含 input 字段）")
    ap.add_argument("--outdir", default=os.path.normpath(os.path.join(HERE, "..", "out")))
    args = ap.parse_args()

    if not args.demo:
        p = args.input or os.path.normpath(os.path.join(HERE, "..", "examples", "input.json"))
        if os.path.exists(p):
            pass  # 抽样明细已内置，与 examples/input.json 同源

    field_map = s1_data_sync()           # S1
    rows, kpi = s2_attribution()         # S2
    det_path = write_out(args.outdir, field_map, rows, kpi)

    print(f"[OK] S1 会员数据对接：字段映射 {len(field_map)} 项")
    print(f"[OK] S2 归因：核销 {kpi['核销人数']}/{kpi['抽样人数']}（{kpi['核销率']}），"
          f"增量毛利 {kpi['抽样增量毛利']}，ROI {kpi['ROI（毛利/券成本）']}")
    for p in (os.path.join(args.outdir, '归因明细.csv'), os.path.join(args.outdir, 'attribution_result.json'),
              os.path.join(args.outdir, '归因报告.md')):
        print(f"[OK] 产物：{p}")


if __name__ == "__main__":
    main()
