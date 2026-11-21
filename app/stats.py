"""统计检验：双侧 Fisher 精确检验（纯标准库，分数精确计算）。"""
from fractions import Fraction
from functools import lru_cache
from math import comb

ALPHA = Fraction(1, 40)  # 0.025，显著性阈值（含等号）


@lru_cache(maxsize=None)
def fisher_two_sided(a, b, n1, n2):
    """2x2 列联表双侧 Fisher 检验。

    a, b: 两组各自的完成人数；n1, n2: 两组总人数。
    返回 Fraction（精确 p 值）。固定边际：完成总数 c=a+b，未完成总数其余。
    """
    if not (0 <= a <= n1 and 0 <= b <= n2):
        raise ValueError("单元格取值超出边际范围")
    c = a + b
    total = n1 + n2

    def prob(x):
        # x: 组1完成数；超几何概率
        return Fraction(
            comb(n1, x) * comb(n2, c - x),
            comb(total, c),
        )

    lo = max(0, c - n2)
    hi = min(n1, c)
    p_obs = prob(a)
    p_value = Fraction(0, 1)
    for x in range(lo, hi + 1):
        px = prob(x)
        if px <= p_obs:  # 双侧：累加概率不大于观测表的所有表
            p_value += px
    return p_value


def significant(p_value):
    return p_value <= ALPHA


def decide(a, b, n1, n2):
    """返回 (是否显著, 胜出组 'A'/'B'/None, 精确 p)。"""
    p = fisher_two_sided(a, b, n1, n2)
    if significant(p):
        return True, ("A" if a / n1 > b / n2 else "B"), p
    return False, None, p


def precompute_tables(sizes):
    """预热指定 (n1,n2) 下所有 (a,b) 组合的缓存。"""
    for n1, n2 in sizes:
        for a in range(n1 + 1):
            for b in range(n2 + 1):
                fisher_two_sided(a, b, n1, n2)
