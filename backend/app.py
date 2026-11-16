import json
import os

from bottle import Bottle, request, response, run

from auth import authenticate, issue_token, read_user
from db import (
    connect,
    contention_notes,
    init_db,
    judge_and_write_batch,
    judge_and_write_mold,
    mold_board,
    mold_total,
    publish,
    record_contention,
)
from metric import (
    CURRENT,
    EXCLUDE_NAME,
    JUDGE_FIRST,
    MOLD_POLICY,
    MoldContention,
    MoldJudgementError,
    MoldOrderError,
    rate_label,
)

app = Bottle()


def _json(payload, status=200):
    response.status = status
    response.content_type = "application/json; charset=utf-8"
    return json.dumps(payload, ensure_ascii=False, default=str)


def _user():
    return read_user(request.headers.get("Authorization"))


def _stamp(value):
    return value.isoformat(sep=" ", timespec="minutes") if value else None


def _body():
    try:
        return json.loads(request.body.read() or b"{}")
    except json.JSONDecodeError:
        raise ValueError("请求不是合法的 JSON")


def _mold_payload(conn, trial_id):
    rows = mold_board(conn, trial_id)
    for row in rows:
        row["judged_at"] = _stamp(row["judged_at"])
    notes = contention_notes(conn, trial_id)
    for note in notes:
        note["created_at"] = _stamp(note["created_at"])
    return {
        "beds": rows,
        "mold_total": mold_total(conn, trial_id),
        "exclude_name": EXCLUDE_NAME,
        "policy": MOLD_POLICY,
        "write_order": JUDGE_FIRST,
        "contention_notes": notes,
    }


def _trial_payload(conn, trial_id):
    trial = conn.execute(
        "SELECT id, lot_code, crop_name, status FROM trials WHERE id = %s",
        (trial_id,),
    ).fetchone()
    if not trial:
        return None
    mold = _mold_payload(conn, trial_id)
    publications = conn.execute(
        """
        SELECT id, protocol_version, tested, excluded_mold, sprouted, rate_bp, note,
               published_by, published_at, mold_snapshot
        FROM publications WHERE trial_id = %s ORDER BY id DESC
        """,
        (trial_id,),
    ).fetchall()
    for row in publications:
        row["rate_label"] = rate_label(row["rate_bp"])
        row["published_at"] = _stamp(row["published_at"])
        row["current"] = row["protocol_version"] == CURRENT
        # 分母详情里的剔除名和霉态快照，跟霉粒列表同一来源。
        row["exclude_name"] = EXCLUDE_NAME
        # 旧库补列之前的记录没有快照，不能假装对得上。
        row["mold_matches_board"] = (
            row["mold_snapshot"] == mold["mold_total"] if row["mold_snapshot"] is not None else None
        )
    return {"trial": trial, "mold": mold, "publications": publications}


@app.get("/api/health")
def health():
    with connect() as conn:
        conn.execute("SELECT 1")
    return _json({"status": "ok", "service": "germination-protocol"})


@app.post("/api/login")
def login():
    try:
        body = _body()
    except ValueError as exc:
        return _json({"detail": str(exc)}, 400)
    user = authenticate(str(body.get("username", "")).strip(), str(body.get("password", "")))
    if not user:
        return _json({"detail": "用户名或密码错误"}, 401)
    return _json(
        {
            "access_token": issue_token(user),
            "username": user["username"],
            "role": user["role"],
            "title": user["title"],
        }
    )


@app.get("/api/trials")
def list_trials():
    if not _user():
        return _json({"detail": "未登录"}, 401)
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, lot_code, crop_name, status FROM trials ORDER BY id"
        ).fetchall()
    return _json(rows)


@app.get("/api/trials/<trial_id:int>")
def trial_detail(trial_id):
    if not _user():
        return _json({"detail": "未登录"}, 401)
    with connect() as conn:
        payload = _trial_payload(conn, trial_id)
    if not payload:
        return _json({"detail": "没有这个批次"}, 404)
    return _json(payload)


def _judge_body(body):
    bed_no = body.get("bed_no")
    moldy = body.get("moldy")
    expected = body.get("expected_version")
    if not isinstance(bed_no, int) or isinstance(bed_no, bool):
        raise MoldJudgementError("bed_no 必须是整数床号")
    if not isinstance(moldy, int) or isinstance(moldy, bool):
        raise MoldJudgementError("moldy 必须是整数的霉粒数")
    if not isinstance(expected, int) or isinstance(expected, bool):
        raise MoldJudgementError("expected_version 必须是整数：先读霉粒列表再判定")
    return bed_no, moldy, expected


def _contended(trial_id, exc, user):
    """抢霉落败：霉态不动，只留争用说明；说明单独提交，不跟失败那笔一起回滚。"""
    with connect() as conn:
        record_contention(conn, trial_id, exc, user["username"], user["title"])
        conn.commit()
    return _json(
        {
            "detail": str(exc),
            "contention": True,
            "bed_no": exc.bed_no,
            "expected_version": exc.expected_version,
            "actual_version": exc.actual_version,
            "holder": exc.holder,
            "write_order": JUDGE_FIRST,
        },
        409,
    )


def _judge_write(trial_id, handle):
    """判定写入的公共外壳：权限、批次存在、异常一律按“拦下 / 回滚 / 争用”分开答。

    成功后返回整份批次详情，霉粒列表和出数记录来自同一次提交后的快照。
    """
    user = _user()
    if not user:
        return _json({"detail": "未登录"}, 401)
    if user["role"] != "writer":
        return _json({"detail": "见证员不能判定霉粒"}, 403)
    try:
        with connect() as conn:
            exists = conn.execute("SELECT id FROM trials WHERE id = %s", (trial_id,)).fetchone()
            if not exists:
                return _json({"detail": "没有这个批次"}, 404)
            handle(conn, user)
            payload = _trial_payload(conn, trial_id)
            conn.commit()
    except MoldContention as exc:
        return _contended(trial_id, exc, user)
    except MoldOrderError as exc:
        return _json({"detail": str(exc), "blocked": True, "write_order": JUDGE_FIRST}, 400)
    except MoldJudgementError as exc:
        return _json({"detail": str(exc), "rolled_back": True}, 400)
    except ValueError as exc:
        return _json({"detail": str(exc)}, 400)
    return _json(payload)


@app.post("/api/trials/<trial_id:int>/mold")
def judge_mold_bed(trial_id):
    def handle(conn, user):
        body = _body()
        order = body.get("order", JUDGE_FIRST)
        bed_no, moldy, expected = _judge_body(body)
        judge_and_write_mold(
            conn, trial_id, bed_no, moldy, expected, user["username"], user["title"], order
        )

    return _judge_write(trial_id, handle)


@app.post("/api/trials/<trial_id:int>/mold/batch")
def judge_mold_batch(trial_id):
    def handle(conn, user):
        body = _body()
        order = body.get("order", JUDGE_FIRST)
        entries = body.get("beds")
        if not isinstance(entries, list) or not entries:
            raise MoldJudgementError("整笔判定至少要有一床")
        parsed = []
        seen = set()
        for entry in entries:
            bed_no, moldy, expected = _judge_body(entry or {})
            if bed_no in seen:
                raise MoldJudgementError(f"{bed_no} 床在同一笔里出现了两次")
            seen.add(bed_no)
            parsed.append({"bed_no": bed_no, "moldy": moldy, "expected_version": expected})
        judge_and_write_batch(conn, trial_id, parsed, user["username"], user["title"], order)

    return _judge_write(trial_id, handle)


@app.post("/api/trials/<trial_id:int>/publish")
def publish_current(trial_id):
    user = _user()
    if not user:
        return _json({"detail": "未登录"}, 401)
    if user["role"] != "writer":
        return _json({"detail": "见证员不能出数"}, 403)
    try:
        with connect() as conn:
            exists = conn.execute("SELECT id FROM trials WHERE id = %s", (trial_id,)).fetchone()
            if not exists:
                return _json({"detail": "没有这个批次"}, 404)
            publish(conn, trial_id, user["username"], CURRENT)
            payload = _trial_payload(conn, trial_id)
            conn.commit()
    except ValueError as exc:
        return _json({"detail": str(exc)}, 400)
    return _json(payload)


init_db()


if __name__ == "__main__":
    run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), server="waitress")
