"""缺报推演引擎。

规则：
- 第一轮：每组前 20 人；显著则停止，完成率高者胜。
- 第二轮：第一轮不显著时，用每组全部 40 人，同样双侧 Fisher、阈值 0.025。
- 缺报（status 为 NULL）者的完成状态在两轮间必须一致：
  同一人前 20 名缺报时，第一轮和第二轮取同一状态。
- 枚举所有可能的缺报赋值，汇总可能结论集合及每种结论的一组代表性两轮完成数。
"""
from dataclasses import dataclass, field

from .stats import decide

ROUND1_SIZE = 20
GROUP_SIZE = 40
GROUPS = ("A", "B")


@dataclass
class Scenario:
    """一组缺报赋值下的完整推演结果。"""
    r1_a: int
    r1_b: int
    r1_p: object
    r2_a: int | None = None
    r2_b: int | None = None
    r2_p: object | None = None
    conclusion: str = ""  # 'A' / 'B' / 'NONE'
    note: str = ""


@dataclass
class GroupFacts:
    group: str
    r1_fixed_done: int = 0
    r1_missing: int = 0
    tail_fixed_done: int = 0      # 第 21-40 名中已报完成
    tail_missing: int = 0         # 第 21-40 名中缺报
    r2_fixed_done: int = 0
    r2_missing: int = 0
    reported: int = 0


@dataclass
class Analysis:
    facts: dict = field(default_factory=dict)
    possible: set = field(default_factory=set)          # {'A','B','NONE'}
    examples: dict = field(default_factory=dict)        # 结论 -> Scenario
    total_scenarios: int = 0
    unique_conclusion: str | None = None

    @property
    def can_issue(self):
        return len(self.possible) == 1


def _group_facts(rows, group):
    f = GroupFacts(group=group)
    for r in rows:
        if r["group_name"] != group:
            continue
        idx = r["seq"]
        in_round1 = idx <= ROUND1_SIZE
        if r["status"] is None:
            if in_round1:
                f.r1_missing += 1
            else:
                f.tail_missing += 1
        else:
            f.reported += 1
            if r["status"] == 1:
                if in_round1:
                    f.r1_fixed_done += 1
                else:
                    f.tail_fixed_done += 1
    f.r2_fixed_done = f.r1_fixed_done + f.tail_fixed_done
    f.r2_missing = f.r1_missing + f.tail_missing
    return f


def analyze(rows):
    """rows: [{id, group_name, seq, status(0/1/None)}, ...]，按入组顺序。"""
    facts = {g: _group_facts(rows, g) for g in GROUPS}
    fa, fb = facts["A"], facts["B"]
    result = Analysis(facts=facts)

    # 组内联合枚举：(k1 前20缺报中完成数, kt 后20缺报中完成数)
    # 第二轮缺报完成数 k2 = k1 + kt，天然保证同一人两轮状态一致。
    for ka1 in range(fa.r1_missing + 1):
        for kat in range(fa.tail_missing + 1):
            a1 = fa.r1_fixed_done + ka1
            a2 = fa.r2_fixed_done + ka1 + kat
            for kb1 in range(fb.r1_missing + 1):
                sig1, winner1, p1 = decide(
                    a1, fb.r1_fixed_done + kb1, ROUND1_SIZE, ROUND1_SIZE
                )
                if sig1:
                    sc = Scenario(
                        r1_a=a1, r1_b=fb.r1_fixed_done + kb1, r1_p=p1,
                        conclusion=winner1,
                        note="第一轮已显著，停止，不进入第二轮",
                    )
                else:
                    for kbt in range(fb.tail_missing + 1):
                        b1 = fb.r1_fixed_done + kb1
                        b2 = fb.r2_fixed_done + kb1 + kbt
                        sig2, winner2, p2 = decide(
                            a2, b2, GROUP_SIZE, GROUP_SIZE
                        )
                        sc = Scenario(
                            r1_a=a1, r1_b=b1, r1_p=p1,
                            r2_a=a2, r2_b=b2, r2_p=p2,
                            conclusion=winner2 if sig2 else "NONE",
                            note=("第二轮显著" if sig2
                                  else "两轮均不显著，无胜出"),
                        )
                        _collect(result, sc)
                    continue
                _collect(result, sc)

    result.unique_conclusion = next(iter(result.possible)) if result.can_issue else None
    return result


def _collect(result, sc):
    result.total_scenarios += 1
    result.possible.add(sc.conclusion)
    # 每种结论保留一组可得到它的两轮分组完成数
    if sc.conclusion not in result.examples:
        result.examples[sc.conclusion] = sc
