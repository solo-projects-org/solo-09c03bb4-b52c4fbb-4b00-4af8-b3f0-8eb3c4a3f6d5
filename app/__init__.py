"""Flask 应用：底表补报/更正、缺报推演、签发与失效管理。"""
from flask import (
    Flask, Response, flash, g, redirect, render_template, request, url_for,
)

from . import db
from .engine import GROUPS, GROUP_SIZE, ROUND1_SIZE, analyze
from .stats import ALPHA

CONCLUSION_LABEL = {"A": "A 版胜出", "B": "B 版胜出", "NONE": "无胜出"}


def create_app():
    app = Flask(__name__)
    app.secret_key = "dev-secret-change-in-production"

    @app.teardown_appcontext
    def _close(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    def conn():
        if "db" not in g:
            c = db.get_db()
            db.init_db(c)
            g.db = c
        return g.db

    def current_analysis():
        return analyze([dict(r) for r in db.all_visitors(conn())])

    @app.route("/")
    def index():
        c = conn()
        analysis = current_analysis()
        return render_template(
            "index.html",
            analysis=analysis,
            alpha=float(ALPHA),
            issuances=db.list_issuances(c),
            corrections=db.list_corrections(c),
            groups=GROUPS,
            group_size=GROUP_SIZE,
            round1_size=ROUND1_SIZE,
            conclusion_label=CONCLUSION_LABEL,
            visitors=db.all_visitors(c),
        )

    @app.route("/upload", methods=["POST"])
    def upload():
        f = request.files.get("csv_file")
        if f is None or not f.filename:
            flash("请先选择 CSV 文件", "error")
            return redirect(url_for("index"))
        try:
            records = db.parse_csv(f.read())
            before = analyze([dict(r) for r in db.all_visitors(conn())])
            before_unique = before.unique_conclusion
            # 更正与签发失效在同一事务：任何一步失败整体回滚，原状不变
            changes = db.apply_corrections(conn(), records, commit=False)
            after = analyze([dict(r) for r in db.all_visitors(conn())])
            invalidated = 0
            # 原结论不再是当前唯一可能（可能集合变化或不再唯一），旧有效签发即失效
            if (before_unique is not None
                    and after.possible != {before_unique}):
                # 原唯一结论不再是唯一可能：旧签发随更正一并失效
                invalidated = db.invalidate_open_issuances(
                    conn(),
                    f"更正后可能结论变为 {sorted(after.possible)}，"
                    f"原结论 {before_unique} 不再是唯一可能",
                )
            conn().commit()
        except (ValueError, db.sqlite3.Error) as exc:
            conn().rollback()  # 双保险：确保写入失败保留原状
            flash(f"上传被拒绝，底表未改动：{exc}", "error")
            return redirect(url_for("index"))

        if not changes:
            flash("CSV 处理完成：所有记录与底表相同，无实际变更。", "info")
        else:
            flash(
                f"已写入 {len(changes)} 条补报/更正；"
                + (f"{invalidated} 份旧签发已标记失效。" if invalidated
                   else "当前有效签发不受影响。"),
                "ok",
            )
        return redirect(url_for("index"))

    @app.route("/issue", methods=["POST"])
    def issue():
        conclusion = request.form.get("conclusion", "")
        if conclusion not in CONCLUSION_LABEL:
            flash("非法结论值。", "error")
            return redirect(url_for("index"))
        # 按最新底表重新核实：所见结论仍是唯一可能才签发
        analysis = current_analysis()
        if not analysis.can_issue or analysis.unique_conclusion != conclusion:
            flash(
                "签发被拒绝：按最新底表，该结论已不是唯一可能 "
                f"（当前可能结论：{sorted(analysis.possible)}）。",
                "error",
            )
            return redirect(url_for("index"))
        try:
            iid = db.issue(conn(), conclusion)
        except db.sqlite3.Error as exc:
            conn().rollback()
            flash(f"签发写入失败，已保留原状：{exc}", "error")
            return redirect(url_for("index"))
        flash(f"已签发第 {iid} 号结论：{CONCLUSION_LABEL[conclusion]}。", "ok")
        return redirect(url_for("index"))

    @app.route("/roster.csv")
    def roster_template():
        rows = ["id,status"]
        for r in db.all_visitors(conn()):
            st = "" if r["status"] is None else r["status"]
            rows.append(f"{r['id']},{st}")
        return Response(
            "\n".join(rows) + "\n",
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=roster.csv"},
        )

    return app
