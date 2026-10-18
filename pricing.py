#!/usr/bin/env python3
"""Leads 广场定价 v0（2026-10-13，W1-D4）。

公式（见方案）：price = base × 新鲜度系数 × 质检分系数 × (1 + 同类转化溢价) × 独家系数

- base：类目基价（USD）
- 新鲜度系数：7 天线性衰减（与质检一致）
- 质检分系数：qa>=75 → 1.2；60<=qa<75 → 1.0；<60 不上架
- 同类转化溢价：category_cvr 有数据时 (1 + cvr*2)，样本<30 仅内部参考
- 独家系数：exclusive lead 7 天独占 → 5.0
"""
from datetime import datetime, timezone

BASE_PRICE = {
    "ai_automation_intent": 0.80,
    "funding_signal": 0.60,
    "funding_hiring_signal": 0.60,
    "devtool_signal": 0.40,
}
EXCLUSIVE_MULTIPLIER = 5.0
MIN_LIST_QA = 60.0


def freshness(age_hours: float) -> float:
    return max(0.0, 1.0 - age_hours / 168.0)


def qa_coef(qa_score: float) -> float:
    if qa_score >= 75: return 1.2
    if qa_score >= 60: return 1.0
    return 0.0  # 不上架


def price_of(category: str, age_hours: float, qa_score: float,
             category_cvr: float | None = None, exclusive: bool = False) -> float | None:
    """返回 USD 价格；不上架返回 None。"""
    qc = qa_coef(qa_score)
    if qc == 0.0: return None
    base = BASE_PRICE.get(category, 0.40)
    cvr_coef = (1 + category_cvr * 2) if category_cvr else 1.0
    excl = EXCLUSIVE_MULTIPLIER if exclusive else 1.0
    return round(base * freshness(age_hours) * qc * cvr_coef * excl, 2)


def age_hours_of(captured_at: str) -> float:
    try:
        cap = datetime.fromisoformat(captured_at)
    except Exception:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - cap).total_seconds() / 3600)
