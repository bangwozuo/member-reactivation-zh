# -*- coding: utf-8 -*-
"""
RFM 分层 —— 确定性打分脚本（T1）。

口径（与 prompt.txt / SKILL.md 完全一致）：
  R 最近到店间隔（权重 40%）：≤15 天=5，16-30=4，31-60=3，61-90=2，>90=1
  F 年消费次数（权重 30%）：≥10 次=5，7-9=4，4-6=3，2-3=2，≤1=1
  M 年累计消费金额（权重 30%）：≥3000 元=5，2000-2999=4，1000-1999=3，500-999=2，<500=1
  总分 = R×0.4 + F×0.3 + M×0.3，保留 1 位小数
  分层：≥4.5 高价值会员；3.5-4.4 潜力会员；2.5-3.4 一般挽留会员；<2.5 流失风险会员

用法：
  python rfm_segmentation.py --demo
  python rfm_segmentation.py --input examples/input.json --outdir out

产物（写入 out/）：
  out/评分明细.csv     逐条 R/F/M 原始分、加权过程、总分、分层
  out/分层汇总.csv     各层数量、占比、建议动作
  out/rfm_result.json  机器可读结果
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

WEIGHTS = {"R": 0.4, "F": 0.3, "M": 0.3}
THRESHOLDS = [(4.5, "高价值会员"), (3.5, "潜力会员"), (2.5, "一般挽留会员"), (None, "流失风险会员")]
ACTIONS = {
    "高价值会员": "优先维护：专属服务、新品优先体验，暂不打扰性推送",
    "潜力会员": "升级培育：储值加赠引导，提升消费频次",
    "一般挽留会员": "中度触达：发送 20 元以内到店券，激活回店",
    "流失风险会员": "重点召回：强权益召回券 + 到店理由，30 天无回应降低频率",
}

DEMO_TEXT = (
    "门店会员RFM原始数据（近一年消费，导出于2026-10-07）：\n"
    "1. 会员M001 王芳：最近到店5天前（2026-10-02），年消费12次，年累计消费3860元，储值余额520元\n"
    "2. 会员M002 李强：最近到店68天前（2026-07-31），年消费3次，年累计消费620元，无储值\n"
    "3. 会员M003 张敏：最近到店15天前（2026-09-23），年消费8次，年累计消费2450元，储值余额800元\n"
    "4. 会员M004 陈伟：最近到店95天前（2026-07-05），年消费1次，年累计消费198元，无储值\n"
    "5. 会员M005 刘洋：最近到店40天前（2026-08-29），年消费5次，年累计消费1180元，储值余额150元"
)

PAT = re.compile(
    r"(?P<name>会员[A-Z]\d+\s*\S+?)[：:].*?最近到店(?P<r>\d+)天前.*?"
    r"年消费(?P<f>\d+)次.*?年累计消费(?P<m>\d+)元(?:，储值余额(?P<sv>\d+)元)?"
)


def score_r(days: int) -> int:
    if days <= 15:
        return 5
    if days <= 30:
        return 4
    if days <= 60:
        return 3
    if days <= 90:
        return 2
    return 1


def score_f(times: int) -> int:
    if times >= 10:
        return 5
    if times >= 7:
        return 4
    if times >= 4:
        return 3
    if times >= 2:
        return 2
    return 1


def score_m(amount: int) -> int:
    if amount >= 3000:
        return 5
    if amount >= 2000:
        return 4
    if amount >= 1000:
        return 3
    if amount >= 500:
        return 2
    return 1


def segment(total: float) -> str:
    for th, name in THRESHOLDS:
        if th is None or total >= th:
            return name
    return "流失风险会员"


def parse_records(text: str):
    rows = []
    for m in PAT.finditer(text or ""):
        rows.append({
            "name": m.group("name").strip(),
            "r_days": int(m.group("r")),
            "f_times": int(m.group("f")),
            "m_amount": int(m.group("m")),
            "stored": int(m.group("sv")) if m.group("sv") else 0,
        })
    return rows


def run(records_text: str):
    rows = parse_records(records_text)
    details, buckets = [], {}
    for i, rec in enumerate(rows, 1):
        r, f, m = score_r(rec["r_days"]), score_f(rec["f_times"]), score_m(rec["m_amount"])
        total = round(r * WEIGHTS["R"] + f * WEIGHTS["F"] + m * WEIGHTS["M"], 1)
        seg = segment(total)
        buckets.setdefault(seg, []).append(rec["name"])
        details.append({
            "#": str(i), "对象": rec["name"],
            "维度A": f"R={r}（{rec['r_days']}天）/ F={f}（{rec['f_times']}次）/ M={m}（{rec['m_amount']}元）",
            "维度B": f"加权：{r}×0.4+{f}×0.3+{m}×0.3",
            "总分": f"{total}", "分层": seg,
            "_r_days": rec["r_days"], "_f_times": rec["f_times"],
            "_m_amount": rec["m_amount"], "_stored": rec["stored"],
        })
    n = len(details) or 1
    summary = []
    for _, seg in THRESHOLDS:
        members = buckets.get(seg, [])
        if not members:
            continue
        summary.append({
            "层级": seg, "数量": str(len(members)),
            "占比": f"{round(len(members) / n * 100)}%",
            "典型特征": "、".join(members),
            "建议动作": ACTIONS[seg],
        })
    return details, summary


def write_out(outdir, details, summary):
    os.makedirs(outdir, exist_ok=True)
    det_path = os.path.join(outdir, "评分明细.csv")
    with open(det_path, "w", encoding="utf-8-sig", newline="") as fp:
        cols = ["#", "对象", "维度A", "维度B", "总分", "分层"]
        w = csv.DictWriter(fp, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(details)

    sum_path = os.path.join(outdir, "分层汇总.csv")
    with open(sum_path, "w", encoding="utf-8-sig", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=["层级", "数量", "占比", "典型特征", "建议动作"])
        w.writeheader()
        w.writerows(summary)

    result = {
        "weights": WEIGHTS,
        "thresholds_text": "总分≥4.5 高价值会员；3.5-4.4 潜力会员；2.5-3.4 一般挽留会员；<2.5 流失风险会员",
        "count": len(details),
        "details": details,
        "summary": summary,
        "ai_label": "AI 生成内容，外发前需人工审核",
    }
    with open(os.path.join(outdir, "rfm_result.json"), "w", encoding="utf-8") as fp:
        json.dump(result, fp, ensure_ascii=False, indent=2)
    return det_path, sum_path


def main():
    ap = argparse.ArgumentParser(description="RFM 分层确定性打分")
    ap.add_argument("--demo", action="store_true", help="内置演示数据跑通全流程")
    ap.add_argument("--input", help="输入 JSON（含 records 字段）")
    ap.add_argument("--outdir", default=os.path.normpath(os.path.join(HERE, "..", "out")))
    args = ap.parse_args()

    text = DEMO_TEXT
    if not args.demo and args.input:
        with open(args.input, encoding="utf-8") as fp:
            data = json.load(fp)
        text = data.get("records") if isinstance(data.get("records"), str) else "\n".join(
            data.get("records", []))
    elif not args.demo:
        demo_path = os.path.normpath(os.path.join(HERE, "..", "examples", "input.json"))
        if os.path.exists(demo_path):
            with open(demo_path, encoding="utf-8") as fp:
                data = json.load(fp)
            text = data.get("records") if isinstance(data.get("records"), str) else "\n".join(
                data.get("records", []))

    details, summary = run(text)
    if not details:
        print("[错误] 未能从输入解析出任何记录", file=sys.stderr)
        sys.exit(1)

    det_path, sum_path = write_out(args.outdir, details, summary)
    print(f"[OK] 解析 {len(details)} 条记录，分 {len(summary)} 层")
    for row in summary:
        print(f"  {row['层级']}: {row['数量']} 人（{row['占比']}）")
    print(f"[OK] 产物：{det_path}")
    print(f"[OK] 产物：{sum_path}")


if __name__ == "__main__":
    main()
