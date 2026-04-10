"""
core/interpolation.py - 情绪-阈值连续插值函数

v3.0: 将离散的情绪-阈值映射改为线性插值，消除边界效应。
"""


def interpolate_seal_threshold(sentiment_score: float) -> float:
    """
    将离散的情绪-封单阈值映射改为线性插值，消除边界效应。

    锚点（保持与原有阈值在整数点一致）：
    - 情绪 10.0 → 2000万
    - 情绪 8.0  → 3000万
    - 情绪 7.0  → 5000万
    - 情绪 5.5  → 8000万
    - 情绪 4.0  → 1.0亿
    - 情绪 2.5  → 1.5亿
    - 情绪 1.0  → 2.0亿
    """
    anchors = [
        (10.0, 2e7),
        (8.0, 3e7),
        (7.0, 5e7),
        (5.5, 8e7),
        (4.0, 1e8),
        (2.5, 1.5e8),
        (1.0, 2e8),
    ]

    if sentiment_score >= anchors[0][0]:
        return anchors[0][1]
    if sentiment_score <= anchors[-1][0]:
        return anchors[-1][1]

    for i in range(len(anchors) - 1):
        high_score, high_threshold = anchors[i]
        low_score, low_threshold = anchors[i + 1]
        if low_score <= sentiment_score <= high_score:
            ratio = (sentiment_score - low_score) / (high_score - low_score)
            return low_threshold + (high_threshold - low_threshold) * ratio

    return 1e8  # fallback


def interpolate_sector_requirements(sentiment_score: float):
    """
    v3.0: 连续化板块效应要求（扫板用）。
    返回 (required_sectors, required_leading)。
    """
    # 情绪10→(0,0), 情绪7→(2,1), 情绪5.5→(3,2), 情绪4→(4,3), 情绪2.5→(4,3)
    anchors = [
        (10.0, 0, 0),
        (8.0, 0, 0),
        (7.0, 2, 1),
        (5.5, 3, 2),
        (4.0, 4, 3),
        (2.5, 4, 3),
    ]

    if sentiment_score >= anchors[0][0]:
        return (anchors[0][1], anchors[0][2])
    if sentiment_score <= anchors[-1][0]:
        return (anchors[-1][1], anchors[-1][2])

    for i in range(len(anchors) - 1):
        high_score, high_s, high_l = anchors[i]
        low_score, low_s, low_l = anchors[i + 1]
        if low_score <= sentiment_score <= high_score:
            ratio = (sentiment_score - low_score) / (high_score - low_score)
            req_s = int(round(low_s + (high_s - low_s) * ratio))
            req_l = int(round(low_l + (high_l - low_l) * ratio))
            return (req_s, req_l)

    return (3, 2)  # fallback
