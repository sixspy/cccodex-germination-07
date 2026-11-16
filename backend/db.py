import os
import time

import psycopg
from psycopg.rows import dict_row

from metric import (
    JUDGE_FIRST,
    LEGACY,
    MoldJudgementError,
    judge_mold,
    measure,
    normalize_order,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS trials (
    id BIGSERIAL PRIMARY KEY,
    lot_code TEXT NOT NULL UNIQUE,
    crop_name TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS beds (
    id BIGSERIAL PRIMARY KEY,
    trial_id BIGINT NOT NULL REFERENCES trials(id),
    bed_no INTEGER NOT NULL,
    sown INTEGER NOT NULL,
    sprouted INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS beds_trial_bed_idx ON beds (trial_id, bed_no);

-- 霉态库。全库只有 _write_molds() 一处写它：先判定，再写入。
CREATE TABLE IF NOT EXISTS mold_states (
    trial_id BIGINT NOT NULL REFERENCES trials(id),
    bed_no INTEGER NOT NULL,
    moldy INTEGER NOT NULL,
    version INTEGER NOT NULL,
    judged_by TEXT NOT NULL,
    judge_role TEXT NOT NULL,
    judged_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (trial_id, bed_no)
);

-- 库这一层也拦先写后判：没走判定入口、没带上判定标记的写入，触发器直接拒。
CREATE OR REPLACE FUNCTION mold_states_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    bed RECORD;
BEGIN
    IF current_setting('germ.mold_order', true) IS DISTINCT FROM 'judge_first' THEN
        RAISE EXCEPTION '霉粒先写后判须拦：霉态库只接受先判定再写入，没有第二条写入路径';
    END IF;
    SELECT sown, sprouted INTO bed
    FROM beds WHERE trial_id = NEW.trial_id AND bed_no = NEW.bed_no;
    IF NOT FOUND THEN
        RAISE EXCEPTION '霉态写入找不到对应的床次';
    END IF;
    IF NEW.moldy < 0 OR bed.sprouted > bed.sown
       OR NEW.moldy > bed.sown OR bed.sprouted + NEW.moldy > bed.sown THEN
        RAISE EXCEPTION '霉态判定不过：发芽数与霉粒数加在一起超过了播种数';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS mold_states_guard ON mold_states;
CREATE TRIGGER mold_states_guard
BEFORE INSERT OR UPDATE ON mold_states
FOR EACH ROW EXECUTE FUNCTION mold_states_guard();

-- 争用说明。抢霉落败的一笔留在这里，霉态不动。
CREATE TABLE IF NOT EXISTS mold_contention_notes (
    id BIGSERIAL PRIMARY KEY,
    trial_id BIGINT NOT NULL REFERENCES trials(id),
    bed_no INTEGER,
    expected_version INTEGER,
    actual_version INTEGER,
    holder TEXT,
    inspector TEXT NOT NULL,
    judge_role TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS publications (
    id BIGSERIAL PRIMARY KEY,
    trial_id BIGINT NOT NULL REFERENCES trials(id),
    protocol_version TEXT NOT NULL,
    tested INTEGER NOT NULL,
    excluded_mold INTEGER NOT NULL,
    sprouted INTEGER NOT NULL,
    rate_bp INTEGER NOT NULL,
    note TEXT NOT NULL,
    published_by TEXT NOT NULL,
    published_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    mold_snapshot INTEGER
);
"""

# 旧库的 publications 没有出数时的霉态快照；补上，NULL 表示旧记录没记过。
COMPAT = """
ALTER TABLE publications ADD COLUMN IF NOT EXISTS mold_snapshot INTEGER;
"""


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def init_db():
    last_error = None
    for _ in range(30):
        try:
            with connect() as conn:
                conn.execute(SCHEMA)
                conn.execute(COMPAT)
                _absorb_legacy_mold_column(conn)
                count = conn.execute("SELECT COUNT(*) AS n FROM trials").fetchone()["n"]
                if count == 0:
                    _seed(conn)
                conn.commit()
            return
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(1)
    raise last_error


def _absorb_legacy_mold_column(conn):
    """旧库把霉粒写在 beds 上。搬进霉态库走同一个入口，然后把那一列拆掉。"""
    exists = conn.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'beds' AND column_name = 'moldy'
        """
    ).fetchone()
    if not exists:
        return
    legacy = conn.execute(
        "SELECT trial_id, bed_no, moldy FROM beds ORDER BY trial_id, bed_no"
    ).fetchall()
    conn.execute("ALTER TABLE beds DROP COLUMN moldy")
    for row in legacy:
        judge_and_write_mold(
            conn,
            row["trial_id"],
            row["bed_no"],
            row["moldy"],
            0,
            "migration",
            "system",
        )


def _add_trial(conn, lot_code, crop_name, status, beds):
    trial = conn.execute(
        "INSERT INTO trials (lot_code, crop_name, status) VALUES (%s, %s, %s) RETURNING id",
        (lot_code, crop_name, status),
    ).fetchone()
    for bed_no, sown, sprouted in beds:
        conn.execute(
            "INSERT INTO beds (trial_id, bed_no, sown, sprouted) VALUES (%s, %s, %s, %s)",
            (trial["id"], bed_no, sown, sprouted),
        )
    return trial["id"]


def _bed(conn, trial_id, bed_no):
    return conn.execute(
        "SELECT bed_no, sown, sprouted FROM beds WHERE trial_id = %s AND bed_no = %s",
        (trial_id, bed_no),
    ).fetchone()


def _lock_bed(conn, trial_id, bed_no, inspector, judge_role, moldy, expected_version):
    """加锁、读霉态、判定。只判定，不写库。"""
    bed = _bed(conn, trial_id, bed_no)
    if not bed:
        raise MoldJudgementError(f"批次 {trial_id} 没有 {bed_no} 床")
    # 主检和副检同时抢同一床霉粒时，靠这把钥匙排队；先拿批次钥匙，再拿床次钥匙。
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"mold-trial:{trial_id}",))
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s), %s)",
        (f"mold:{trial_id}", bed_no),
    )
    state = conn.execute(
        """
        SELECT moldy, version, judged_by, judge_role
        FROM mold_states WHERE trial_id = %s AND bed_no = %s FOR UPDATE
        """,
        (trial_id, bed_no),
    ).fetchone()
    # —— 判定：这一步不写任何表 ——
    return judge_mold(bed, state, moldy, expected_version, inspector, judge_role)


def _write_molds(conn, trial_id, judged_entries, inspector, judge_role):
    """霉态库的唯一写入口。

    只有整笔判定全部通过之后才会走到这里；调用方要么是单床入口，要么是整笔入口。
    代码里没有第二条写 mold_states 的路；库里的触发器还要求这一笔先打过判定标记。
    """
    conn.execute("SET LOCAL germ.mold_order = 'judge_first'")
    written = []
    for bed_no, judged in judged_entries:
        written.append(
            conn.execute(
                """
                INSERT INTO mold_states (trial_id, bed_no, moldy, version, judged_by, judge_role)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (trial_id, bed_no) DO UPDATE SET
                    moldy = EXCLUDED.moldy,
                    version = EXCLUDED.version,
                    judged_by = EXCLUDED.judged_by,
                    judge_role = EXCLUDED.judge_role,
                    judged_at = NOW()
                RETURNING bed_no, moldy, version, judged_by, judge_role, judged_at
                """,
                (trial_id, bed_no, judged["moldy"], judged["version"], inspector, judge_role),
            ).fetchone()
        )
    return written


def judge_and_write_mold(
    conn, trial_id, bed_no, moldy, expected_version, inspector, judge_role, order=JUDGE_FIRST
):
    """单床入口：先加锁、再判定，判定通过才写入。"""
    normalize_order(order)
    judged = _lock_bed(conn, trial_id, bed_no, inspector, judge_role, moldy, expected_version)
    return _write_molds(conn, trial_id, [(bed_no, judged)], inspector, judge_role)[0]


def judge_and_write_batch(conn, trial_id, entries, inspector, judge_role, order=JUDGE_FIRST):
    """整笔先判定、再写入。任何一床判不过，整笔回滚，一床都不许落库。"""
    normalize_order(order)
    judged_entries = []
    for entry in sorted(entries, key=lambda item: item["bed_no"]):
        judged = _lock_bed(
            conn,
            trial_id,
            entry["bed_no"],
            inspector,
            judge_role,
            entry["moldy"],
            entry["expected_version"],
        )
        judged_entries.append((entry["bed_no"], judged))
    # 整笔判定全部通过，才开始写入。
    return _write_molds(conn, trial_id, judged_entries, inspector, judge_role)


def record_contention(conn, trial_id, exc, inspector, judge_role):
    """争用落败的一笔只留说明，不碰霉态。"""
    conn.execute(
        """
        INSERT INTO mold_contention_notes (
            trial_id, bed_no, expected_version, actual_version, holder,
            inspector, judge_role, note
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            trial_id,
            getattr(exc, "bed_no", None),
            getattr(exc, "expected_version", None),
            getattr(exc, "actual_version", None),
            getattr(exc, "holder", None),
            inspector,
            judge_role,
            str(exc),
        ),
    )


def mold_board(conn, trial_id):
    """霉粒列表与分母详情共用这一个来源：霉态库。"""
    return conn.execute(
        """
        SELECT b.bed_no, b.sown, b.sprouted,
               m.moldy, m.version, m.judged_by, m.judge_role, m.judged_at
        FROM beds b
        LEFT JOIN mold_states m ON m.trial_id = b.trial_id AND m.bed_no = b.bed_no
        WHERE b.trial_id = %s
        ORDER BY b.bed_no
        """,
        (trial_id,),
    ).fetchall()


def mold_total(conn, trial_id):
    row = conn.execute(
        "SELECT COALESCE(SUM(moldy), 0) AS n FROM mold_states WHERE trial_id = %s",
        (trial_id,),
    ).fetchone()
    return row["n"]


def contention_notes(conn, trial_id):
    return conn.execute(
        """
        SELECT bed_no, expected_version, actual_version, holder,
               inspector, judge_role, note, created_at
        FROM mold_contention_notes WHERE trial_id = %s ORDER BY id DESC
        """,
        (trial_id,),
    ).fetchall()


def publish(conn, trial_id: int, username: str, protocol: str):
    # 出数时锁住霉粒列表：跟判定写入同一把批次钥匙，分母详情不会读到写一半的霉态。
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"mold-trial:{trial_id}",))
    beds = conn.execute(
        "SELECT bed_no, sown, sprouted FROM beds WHERE trial_id = %s ORDER BY bed_no",
        (trial_id,),
    ).fetchall()
    if not beds:
        raise ValueError("这个批次还没有床次")
    states = conn.execute(
        "SELECT bed_no, moldy FROM mold_states WHERE trial_id = %s",
        (trial_id,),
    ).fetchall()
    molds = {row["bed_no"]: row["moldy"] for row in states}
    result = measure(beds, molds, protocol)
    row = conn.execute(
        """
        INSERT INTO publications (
            trial_id, protocol_version, tested, excluded_mold, sprouted, rate_bp, note,
            published_by, mold_snapshot
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            trial_id,
            result["protocol_version"],
            result["tested"],
            result["excluded_mold"],
            result["sprouted"],
            result["rate_bp"],
            result["note"],
            username,
            sum(molds.values()),
        ),
    ).fetchone()
    if protocol != LEGACY:
        conn.execute("UPDATE trials SET status = %s WHERE id = %s", ("已按现行口径出数", trial_id))
    return row["id"]


def _seed(conn):
    wheat = _add_trial(
        conn,
        "WH-2408",
        "小麦",
        "已按现行口径出数",
        [(1, 100, 92), (2, 100, 88)],
    )
    for bed_no, moldy in ((1, 3), (2, 5)):
        judge_and_write_mold(conn, wheat, bed_no, moldy, 0, "primary", "主检")
    publish(conn, wheat, "admin", "v2")
    rice = _add_trial(
        conn,
        "RC-2411",
        "水稻",
        "待按现行口径重算",
        [(1, 80, 70), (2, 80, 64)],
    )
    for bed_no, moldy in ((1, 6), (2, 4)):
        judge_and_write_mold(conn, rice, bed_no, moldy, 0, "primary", "主检")
    publish(conn, rice, "admin", LEGACY)
    conn.execute("UPDATE trials SET status = %s WHERE id = %s", ("待按现行口径重算", rice))
