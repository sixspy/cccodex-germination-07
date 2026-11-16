CURRENT = "v2"
LEGACY = "v1"

# 霉粒清洗的写入顺序只有一种：先判定、再写入。
# 接口和页面传这个值；入口处不接受任何“先写后判”的说法。
JUDGE_FIRST = "judge_first"
JUDGE_FIRST_ALIASES = frozenset({JUDGE_FIRST, "judge_then_write", "先判定再写入"})
WRITE_FIRST_ALIASES = frozenset(
    {"write_first", "write_then_judge", "先写后判", "先写再判", "先写后判霉态"}
)

MOLD_POLICY = (
    "霉态判定与霉粒写入同一入口：先判定再写入。判定不过整笔回滚，重判通过后才允许写入；"
    "霉态库只认这一条路径，禁止双路写入，霉粒先写后判一律拦下。"
)
EXCLUDE_NAME = "剔除霉粒"


class MoldJudgementError(ValueError):
    """判定没通过：整笔回滚，不得留下任何霉态写入。"""


class MoldOrderError(MoldJudgementError):
    """先写后判：在入口处拦下，先于任何写入。"""


class MoldContention(MoldJudgementError):
    """霉粒争用：先到的一笔已经写入，后到的不覆盖，只留下争用说明。"""

    def __init__(
        self,
        message,
        *,
        bed_no=None,
        expected_version=None,
        actual_version=None,
        holder=None,
    ):
        super().__init__(message)
        self.bed_no = bed_no
        self.expected_version = expected_version
        self.actual_version = actual_version
        self.holder = holder


def normalize_order(order) -> str:
    """把写入顺序收口成唯一一种；先写后判在入口处直接拦下。"""
    text = str(order or "").strip()
    if text in JUDGE_FIRST_ALIASES:
        return JUDGE_FIRST
    if text in WRITE_FIRST_ALIASES:
        raise MoldOrderError(
            "霉粒先写后判须拦：霉态判定与霉粒写入必须同一入口，先判定再写入"
        )
    raise MoldOrderError(
        f"不认识的霉粒写入顺序“{text or '空'}”：只允许先判定再写入"
    )


def rate_label(basis_points: int) -> str:
    whole = basis_points // 100
    frac = basis_points % 100
    return f"{whole}.{frac:02d}%"


def validate_counts(bed: dict, moldy: int) -> None:
    """一床的播种、发芽、霉粒必须是同一笔判定里对得上的数。"""
    if moldy < 0:
        raise MoldJudgementError(f"{bed['bed_no']} 床的霉粒数不能为负")
    if bed["sprouted"] < 0:
        raise MoldJudgementError(f"{bed['bed_no']} 床的发芽数不能为负")
    if bed["sprouted"] > bed["sown"]:
        raise MoldJudgementError("某一床的发芽数超过了播种数")
    if moldy > bed["sown"]:
        raise MoldJudgementError("某一床的霉粒数超过了播种数")
    if bed["sprouted"] + moldy > bed["sown"]:
        raise MoldJudgementError("发芽数与霉粒数加在一起超过了播种数")


def judge_mold(bed, state, moldy, expected_version, inspector, judge_role):
    """只判定，不写入。

    判定通过才返回新的霉态；判定不通过直接抛错，由入口整笔回滚。
    霉粒写库只能发生在这一函数返回之后，所以不存在“先写后判”的路径。
    """
    validate_counts(bed, moldy)
    if isinstance(expected_version, bool) or not isinstance(expected_version, int):
        raise MoldJudgementError("缺少霉态版本：先读霉粒列表，再判定写入")
    current_version = state["version"] if state else 0
    if expected_version != current_version:
        holder = (
            f"{state['judged_by']}（{state['judge_role']}）" if state else "还没有人判定"
        )
        raise MoldContention(
            f"霉粒争用说明：{inspector}（{judge_role}）读到第 {expected_version} 版，"
            f"判定时霉态已是第 {current_version} 版，先到留的是 {holder}；"
            "后到不覆盖，请重读霉粒列表再判定",
            bed_no=bed["bed_no"],
            expected_version=expected_version,
            actual_version=current_version,
            holder=holder,
        )
    return {"moldy": moldy, "version": current_version + 1}


def measure(beds: list[dict], molds: dict[int, int], protocol: str):
    """按霉粒列表里的霉态出数；没有霉态的床不许出数。"""
    sown = 0
    sprouted = 0
    moldy = 0
    for bed in beds:
        if bed["bed_no"] not in molds:
            raise MoldJudgementError(
                f"{bed['bed_no']} 床在霉粒列表里没有霉态，先判定再出数"
            )
        bed_moldy = molds[bed["bed_no"]]
        validate_counts(bed, bed_moldy)
        sown += bed["sown"]
        sprouted += bed["sprouted"]
        moldy += bed_moldy
    if protocol == LEGACY:
        if sown <= 0:
            raise MoldJudgementError("旧口径的分母是零")
        basis = (sprouted * 10000) // sown
        return {
            "protocol_version": LEGACY,
            "tested": sown,
            "excluded_mold": 0,
            "sprouted": sprouted,
            "rate_bp": basis,
            "note": "旧口径把霉粒留在分母里",
        }
    tested = sown - moldy
    if tested <= 0:
        raise MoldJudgementError("剔除霉粒后没有可计种子")
    if sprouted > tested:
        raise MoldJudgementError("发芽数大于剔除霉粒后的分母")
    basis = (sprouted * 10000) // tested
    return {
        "protocol_version": CURRENT,
        "tested": tested,
        "excluded_mold": moldy,
        "sprouted": sprouted,
        "rate_bp": basis,
        "note": "现行口径的分母是播种粒数减去霉粒数",
    }
