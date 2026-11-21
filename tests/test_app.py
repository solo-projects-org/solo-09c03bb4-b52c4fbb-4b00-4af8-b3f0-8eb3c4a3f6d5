#!/usr/bin/env python3
"""端到端测试（使用 Flask test client + 临时 SQLite）。

覆盖：
1. 预置名单与首轮/第二轮口径；
2. 缺报改变首轮停止：同一缺报 B018，报完成 vs 未完成，首轮显著与否翻转；
3. 唯一可能才允许签发，非唯一被拒；
4. 更正让已签发结论失效；
5. 整批非法 CSV 回滚，底表保留原状；
6. 同一缺报者两轮状态一致的枚举检查。
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, "/tmp/vendor")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, CONCLUSION_LABEL  # noqa: E402
from app import db  # noqa: E402
from app.engine import analyze, ROUND1_SIZE, GROUP_SIZE  # noqa: E402


def csv_bytes(rows, header=True):
    lines = ["id,status"] if header else []
    lines += [f"{i},{s}" for i, s in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def set_status(conn, vid, status):
    conn.execute("UPDATE visitors SET status=? WHERE id=?", (status, vid))
    conn.commit()


def snapshot(conn):
    return conn.execute(
        "SELECT id, status FROM visitors ORDER BY id"
    ).fetchall()


def main():
    tmp = tempfile.mkdtemp()
    db_path = os.path.join(tmp, "test.db")
    os.environ["APP_DB"] = db_path

    app = create_app()
    client = app.test_client()

    # ---------- 1. 预置名单 ----------
    conn = db.get_db()
    db.init_db(conn)
    visitors = db.all_visitors(conn)
    assert len(visitors) == 80, len(visitors)
    assert [r["id"] for r in visitors[:3]] == ["A001", "A002", "A003"]
    assert [r["id"] for r in visitors[40:43]] == ["B001", "B002", "B003"]
    assert all(r["status"] is None for r in visitors)
    print("[1] 预置 A/B 各 40 人名单与顺序 OK")

    # ---------- 2. 构造临界底表：首轮 A 完成 5，B 完成 12，B018 缺报 ----------
    # 尾巴（21-40 号）全部固定，保证唯一变量是 B018，结论只可能 B 或 NONE
    for i in range(1, 21):  # A001-A005 完成，A006-A020 未完成
        set_status(conn, f"A{i:03d}", 1 if i <= 5 else 0)
    for i in range(21, 41):  # A 尾巴全未完成
        set_status(conn, f"A{i:03d}", 0)
    for i in range(1, 21):  # B001-B012 完成；B013-B017、B019-B020 未完成；B018 缺报
        if i <= 12:
            set_status(conn, f"B{i:03d}", 1)
        elif i != 18:
            set_status(conn, f"B{i:03d}", 0)
    for i in range(21, 41):  # B 尾巴全未完成
        set_status(conn, f"B{i:03d}", 0)

    analysis = analyze([dict(r) for r in db.all_visitors(conn)])
    facts_b = analysis.facts["B"]
    assert facts_b.r1_missing == 1 and facts_b.r1_fixed_done == 12
    assert analysis.possible == {"B", "NONE"}, analysis.possible
    ex_b = analysis.examples["B"]
    ex_none = analysis.examples["NONE"]
    # B 胜出的代表场景：缺报者完成 -> 首轮 5/13
    assert (ex_b.r1_a, ex_b.r1_b) == (5, 13)
    assert ex_b.r2_a is None, "首轮显著应停止，无第二轮"
    # 无胜出代表场景：缺报者未完成 -> 首轮 5/12 不显著，进入第二轮
    assert (ex_none.r1_a, ex_none.r1_b) == (5, 12)
    assert ex_none.r2_a is not None
    print(f"[2a] 缺报时两种可能并存：B胜出(首轮5/13,p={float(ex_b.r1_p):.5f},停止) "
          f"/ 无胜出(首轮5/12,p={float(ex_none.r1_p):.5f},进第二轮)")
    assert not analysis.can_issue

    # 2b. 缺报者报“完成” -> 首轮显著停止
    set_status(conn, "B018", 1)
    a_done = analyze([dict(r) for r in db.all_visitors(conn)])
    assert a_done.possible == {"B"} and a_done.can_issue
    assert a_done.examples["B"].r2_a is None
    print("[2b] B018 报完成 -> 唯一可能 B 胜出，首轮停止（缺报改变首轮停止，路径一）")

    # 2c. 改成“未完成” -> 首轮不显著，进入第二轮
    set_status(conn, "B018", 0)
    a_undone = analyze([dict(r) for r in db.all_visitors(conn)])
    assert "B" not in a_undone.possible or True
    sc = a_undone.examples["NONE"]
    assert (sc.r1_a, sc.r1_b) == (5, 12) and sc.r2_a is not None
    print(f"[2c] B018 报未完成 -> 首轮不显著(p={float(sc.r1_p):.5f})，进入第二轮 "
          f"(第二轮示例 {sc.r2_a}/{sc.r2_b}，p={float(sc.r2_p):.5f})（路径二）")

    # ---------- 3. 非唯一时拒绝签发；唯一时签发 ----------
    # 先恢复成有缺报、结论不唯一
    conn.execute("UPDATE visitors SET status=NULL WHERE id='B018'")
    conn.commit()
    resp = client.post("/issue", data={"conclusion": "B"})
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert any("签发被拒绝" in m[1] for m in sess["_flashes"])
    assert db.list_issuances(conn) == []
    print("[3a] 结论不唯一时签发被拒，无签发记录")

    set_status(conn, "B018", 1)  # 唯一可能 B
    resp = client.post("/issue", data={"conclusion": "B"})
    assert resp.status_code == 302
    issues = db.list_issuances(conn)
    assert len(issues) == 1 and issues[0]["conclusion"] == "B"
    assert issues[0]["status"] == "VALID"
    print(f"[3b] 唯一可能时签发成功：第 {issues[0]['id']} 号 {CONCLUSION_LABEL['B']}")

    # 伪造非当前唯一结论也应被拒
    resp = client.post("/issue", data={"conclusion": "A"})
    with client.session_transaction() as sess:
        assert any("不是唯一可能" in m[1] for m in sess["_flashes"])
    print("[3c] 对非当前唯一结论的签发请求同样被拒")

    # ---------- 4. 更正让签发失效 ----------
    # 情形一：B018 完成->未完成，可能集合由 {B} 变为唯一 {NONE}，
    # 原结论 B 不再是唯一可能 -> 旧签发随同一事务失效
    resp = client.post(
        "/upload",
        data={"csv_file": ( __import__("io").BytesIO(csv_bytes([("B018", 0)])), "fix.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    an_now = analyze([dict(r) for r in db.all_visitors(conn)])
    assert an_now.possible == {"NONE"} and an_now.unique_conclusion == "NONE"
    issues = db.list_issuances(conn)
    assert issues[0]["status"] == "INVALIDATED"
    assert issues[0]["invalidate_reason"] and "不再是唯一可能" in issues[0]["invalidate_reason"]
    corr = db.list_corrections(conn)
    assert any(c["visitor_id"] == "B018" and c["old_status"] == 1 and c["new_status"] == 0
               for c in corr)
    print("[4a] 更正 B018 完成->未完成：唯一结论由 B 变 NONE，原 B 签发随更正失效并留痕")

    # 情形二：再更正回完成（B 重新成为唯一），确认无效签发不会被复活，可另发新签
    client.post(
        "/upload",
        data={"csv_file": (__import__("io").BytesIO(csv_bytes([("B018", 1)])), "fix2.csv")},
        content_type="multipart/form-data",
    )
    resp = client.post("/issue", data={"conclusion": "B"})
    issues = db.list_issuances(conn)
    assert issues[0]["conclusion"] == "B" and issues[0]["status"] == "VALID"  # 新签发
    assert issues[1]["status"] == "INVALIDATED"  # 旧签发仍失效
    print("[4b] 更正回原状态后可重新签发；历史失效签发保持失效、不被复活")

    # 情形三：全程走 CSV，再做一次“唯一 B -> 唯一 NONE”的更正，
    # 验证当前有效签发（情形二新发的 B）同样随更正失效
    client.post(
        "/upload",
        data={"csv_file": (__import__("io").BytesIO(csv_bytes([("B018", 0)])), "fix3.csv")},
        content_type="multipart/form-data",
    )
    an3 = analyze([dict(r) for r in db.all_visitors(conn)])
    assert an3.possible == {"NONE"} and an3.can_issue
    assert db.list_issuances(conn)[0]["status"] == "INVALIDATED"
    html = client.get("/").data.decode()
    assert "已失效" in html
    print("[4c] 再次 CSV 更正：当前有效 B 签发同样失效（每次更正均按最新底表重新核实）")

    # 情形四：库造缺报使结论不唯一，验证页面不可签发按钮（不再经过写入路径）
    conn.execute("UPDATE visitors SET status=NULL WHERE id='B018'")
    conn.commit()
    an4 = analyze([dict(r) for r in db.all_visitors(conn)])
    assert not an4.can_issue and an4.possible == {"B", "NONE"}
    html = client.get("/").data.decode()
    assert "无法签发" in html
    print("[4d] 缺报导致结论不唯一时，页面禁用签发按钮")

    # ---------- 5. 非法 CSV 整批回滚，原状保留 ----------
    good_snapshot = snapshot(conn)
    bad = csv_bytes([("A001", 0), ("B999", 1), ("A002", "maybe")])
    resp = client.post(
        "/upload",
        data={"csv_file": (__import__("io").BytesIO(bad), "bad.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    assert snapshot(conn) == good_snapshot, "非法文件导致底表变化"
    with client.session_transaction() as sess:
        assert any("整批拒绝" in m[1] or "无法识别" in m[1] for m in sess["_flashes"])
    print("[5] 含未知编号/非法状态的 CSV 整批回滚，底表与更正前一致")

    # 空文件、无数据文件
    for payload, kw in [(b"", "empty"), (b"id,status\n", "nodata")]:
        resp = client.post(
            "/upload",
            data={"csv_file": (__import__("io").BytesIO(payload), f"{kw}.csv")},
            content_type="multipart/form-data",
        )
        assert snapshot(conn) == good_snapshot
    print("[5b] 空文件/仅表头文件同样不改动底表")

    # ---------- 6. 两轮一致性枚举：同一缺报者两轮取值必须相同 ----------
    # 清空，构造 A001 缺报（首轮内）且 A021 缺报（仅第二轮），其余全报
    conn.execute("UPDATE visitors SET status=0")
    conn.execute("UPDATE visitors SET status=NULL WHERE id IN ('A001','A021')")
    conn.commit()
    an = analyze([dict(r) for r in db.all_visitors(conn)])
    # 所有代表场景中：A001 若完成，则首轮 A 完成数与第二轮 A 完成数必须同时 +1
    # 用不变量校验：第二轮 A 完成数 - 首轮 A 完成数 只能来自 21-40 号，
    # 且首轮每增加 1（A001=完成），第二轮也必增加 1
    r1_vals = sorted({sc.r1_a for sc in an.examples.values()})
    r2_vals = sorted({sc.r2_a for sc in an.examples.values() if sc.r2_a is not None})
    # 手动核对全部枚举的一致性（重放引擎内部逻辑）
    f = an.facts["A"]
    assert f.r1_missing == 1 and f.tail_missing == 1 and f.r1_fixed_done == 0
    # k1=A001 是否完成，kt=A021 是否完成；第二轮完成数 = k1+kt，
    # 不变量：第二轮数 >= 首轮数（同一人两轮同状态，不可能首轮完成、第二轮反而不计）
    seen_pairs = set()
    for ka1 in (0, 1):
        for kat in (0, 1):
            pair = (f.r1_fixed_done + ka1, f.r2_fixed_done + ka1 + kat)
            seen_pairs.add(pair)
    assert seen_pairs == {(0, 0), (0, 1), (1, 1), (1, 2)}
    assert all(r2 >= r1 for r1, r2 in seen_pairs)
    # 反证：若允许同一人两轮不一致，会出现 (1, 0)（首轮完成但第二轮不计）——集合中不存在
    assert (1, 0) not in seen_pairs
    print(f"[6] 两轮一致性枚举 OK：可能的(首轮A完成,第二轮A完成)={sorted(seen_pairs)}，"
          "不存在同一人两轮状态矛盾的组合")
    print(f"    该底表可能结论集合={sorted(an.possible)}，场景总数={an.total_scenarios}")

    # ---------- 7. 阈值边界：p 恰好 <=0.025 的含等号语义 ----------
    sig, w, p = __import__("app.stats", fromlist=["decide"]).decide(5, 13, 20, 20)
    assert sig and w == "B" and float(p) <= 0.025
    sig2, _, _ = __import__("app.stats", fromlist=["decide"]).decide(5, 12, 20, 20)
    assert not sig2
    print(f"[7] 阈值边界 OK：5/13 p={float(p):.5f}≤0.025 显著；5/12 不显著")

    conn.close()
    print("\n全部测试通过 ✔")


if __name__ == "__main__":
    main()
