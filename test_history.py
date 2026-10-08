"""测试历史：解析 MI 的明细报表，支持按任意日期范围筛选。

为什么需要这个模块
------------------
`TestPointSummary` 返回的是**聚合结果**，23 列里没有序列号也没有时间戳。
日期是在 MI 那边聚合时就消化掉的，所以拿到手之后无法按别的日期重新切分——
换一个时间段就必须重新向 MI 请求一次。这导致离线快照只能有固定的日期范围。

`FunctionalTestAndRepairHistory` 给的是**明细**：每行一次测试，带
`TestStartTime` / `TestEndTime` / `TestPassed`。一次把每台机器的全部历史拉下来，
之后就能在本地（或不能访问 MI 的服务器上）按任意日期筛选、重新聚合。

⚠️ 两种良率的分母不同，不可混用：
  - TestPointSummary  良率 = 1 − 失败的测试点执行次数 ÷ 总测试点执行次数（量级十万）
  - 本模块（场次级）  良率 = 1 − 失败场次 ÷ 总测试场次（量级百）
两个都对，但含义不同。本模块产出的结果里带 level="run" 标记，展示时必须标明。
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

# ---------------------------------------------------------------------------
# 解析 MI 的返回结构
#
# MI 有两种形状，解析前必须先判断（用错会误报"0 行"，其实有数据）：
#   A) data.data                  -> 纯二维数组，列名靠外部常量（TestPointSummary）
#   B) data.resultSet[i]          -> 自带 columns[].name + rows（其余报表）
#      data.tableResultSet        -> 同上，但只有单张表（SerialNumberLog）
# 本模块只处理 B。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Table:
    """一张带列名的表。"""
    name: str
    columns: list[str]
    rows: list[list[Any]]

    def index(self, column: str) -> int:
        try:
            return self.columns.index(column)
        except ValueError:
            raise KeyError(
                f"{self.name} 没有列 {column!r}。实际列名：{self.columns}") from None

    def get(self, row: Sequence[Any], column: str) -> Any:
        """按列名取值。

        列名拼错时直接抛 KeyError，而不是静默返回 None——后者会让 bug
        藏到很后面才暴露（算出来的良率莫名其妙是 0，但不报错）。
        """
        i = self.index(column)
        return row[i] if i < len(row) else None

    def has(self, column: str) -> bool:
        return column in self.columns


def parse_tables(obj: Any) -> list[Table]:
    """把 MI 的结构 B 响应拆成若干 Table。空返回给空列表，不报错。"""
    if not isinstance(obj, dict):
        return []
    data = obj.get("data")
    if not isinstance(data, dict):
        return []

    out: list[Table] = []
    for key in ("resultSet", "tableResultSet", "resultSets"):
        v = data.get(key)
        items: list[Any]
        if isinstance(v, dict):
            items = [v]
        elif isinstance(v, list):
            items = v
        else:
            continue
        for i, t in enumerate(items):
            if not isinstance(t, dict):
                continue
            cols = [str(c.get("name", "")) for c in (t.get("columns") or [])]
            rows = t.get("rows") or t.get("data") or t.get("aaData") or []
            if not isinstance(rows, list):
                rows = []
            out.append(Table(name=f"{key}[{i}]", columns=cols,
                             rows=[r for r in rows if isinstance(r, (list, tuple))]))
    return out


# ---------------------------------------------------------------------------
# 时间解析
# ---------------------------------------------------------------------------

_RE_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?")
_RE_US = re.compile(
    r"^(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AP]M)?",
    re.I)
_NULLISH = {"", "[null]", "[blank]", "null", "none", "n/a", "[n/a]", "-"}


def parse_dt(v: Any) -> dt.datetime | None:
    """解析 MI 的各种时间写法，解析不了返回 None（不抛异常）。

    实测遇到过三种：
      '2026-09-14 08:40:05'        FunctionalTestAndRepairHistory
      '2026-09-14 11:00:25.000'    ChildSerialNumberTestHistory（带毫秒）
      '9/11/2026 12:00:00 AM'      Mfg Date（美式，12 小时制）
    """
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v
    s = str(v).strip()
    if not s or s.lower() in _NULLISH:
        return None

    m = _RE_ISO.match(s)
    if m:
        y, mo, d, h, mi, se = m.groups()
        return dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(se or 0))

    m = _RE_US.match(s)
    if m:
        mo, d, y, h, mi, se, ampm = m.groups()
        hh = int(h) % 12
        if ampm and ampm.upper() == "PM":
            hh += 12
        elif not ampm:
            hh = int(h)
        return dt.datetime(int(y), int(mo), int(d), hh, int(mi), int(se or 0))
    return None


def _as_dt(v: Any) -> dt.datetime | None:
    """把用户给的边界值转成 datetime。支持 date、datetime、字符串。"""
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v
    if isinstance(v, dt.date):
        return dt.datetime(v.year, v.month, v.day)
    return parse_dt(v)


# ---------------------------------------------------------------------------
# 测试场次
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TestRun:
    """一次测试场次（一台机器跑一遍某个测试程序）。"""
    serial_number: str
    product: str
    revision: str
    criteria: str
    started: dt.datetime | None
    ended: dt.datetime | None
    passed: bool
    test_count: int
    category: str
    station: str
    factory: str

    @property
    def duration_min(self) -> float | None:
        if self.started and self.ended and self.ended >= self.started:
            return (self.ended - self.started).total_seconds() / 60.0
        return None


def _pick_table(tables: list[Table], *required: str) -> Table | None:
    """挑出含指定列的那张表。resultSet 里表的顺序不保证，所以按列名认。"""
    for t in tables:
        if all(t.has(c) for c in required):
            return t
    return None


def to_runs(obj: Any) -> list[TestRun]:
    """从 FunctionalTestAndRepairHistory 的响应里取出测试场次。"""
    tables = parse_tables(obj)
    t = _pick_table(tables, "SerialNumber", "TestStartTime", "TestPassed")
    if t is None:
        return []

    runs: list[TestRun] = []
    for r in t.rows:
        cnt = str(t.get(r, "TestCount") or "").strip()
        runs.append(TestRun(
            serial_number=str(t.get(r, "SerialNumber") or "").strip(),
            product=str(t.get(r, "ProductNumber") or "").strip(),
            revision=str(t.get(r, "ProductNumberRevisionState") or "").strip(),
            criteria=str(t.get(r, "TestCriteria") or "").strip(),
            started=parse_dt(t.get(r, "TestStartTime")),
            ended=parse_dt(t.get(r, "TestEndTime")),
            # 'Y'/'N'。只把明确的 Y 当通过，其他一律算失败，避免把未知当好。
            passed=str(t.get(r, "TestPassed") or "").strip().upper() == "Y",
            test_count=int(cnt) if cnt.isdigit() else 0,
            category=str(t.get(r, "TestCategoryID") or "").strip(),
            station=str(t.get(r, "TestStationID") or "").strip(),
            factory=str(t.get(r, "Factory") or "").strip(),
        ))
    return runs


@dataclass(frozen=True)
class Repair:
    serial_number: str
    test_started: dt.datetime | None
    started: dt.datetime | None
    ended: dt.datetime | None
    fault: str
    fault_area: str
    position: str
    action: str


def to_repairs(obj: Any) -> list[Repair]:
    """从同一个响应里取出维修记录（resultSet[1]）。没有就给空列表。"""
    tables = parse_tables(obj)
    t = _pick_table(tables, "SerialNumber", "RepairStartTime")
    if t is None:
        return []
    out = []
    for r in t.rows:
        out.append(Repair(
            serial_number=str(t.get(r, "SerialNumber") or "").strip(),
            test_started=parse_dt(t.get(r, "TestStartTime")),
            started=parse_dt(t.get(r, "RepairStartTime")),
            ended=parse_dt(t.get(r, "RepairEndTime")),
            fault=str(t.get(r, "FaultName") or "").strip(),
            fault_area=str(t.get(r, "FaultAreaName") or "").strip(),
            position=str(t.get(r, "PositionID") or "").strip(),
            action=str(t.get(r, "RepairActionName") or "").strip(),
        ))
    return out


# ---------------------------------------------------------------------------
# ★ 按任意条件筛选——这是解锁日期框的关键能力
# ---------------------------------------------------------------------------

def filter_runs(runs: Iterable[TestRun], *,
                from_date: Any = None, to_date: Any = None,
                product: str | None = None,
                serial_numbers: Iterable[str] | None = None,
                categories: Iterable[str] | None = None,
                ) -> list[TestRun]:
    """按日期/产品/序列号/类别筛选。全部条件可选，不给就是不筛。

    日期区间**含左不含右**（[from, to)），这样相邻的两个区间不会把
    边界上的记录各算一次。
    """
    lo, hi = _as_dt(from_date), _as_dt(to_date)
    sns = {s.strip().upper() for s in serial_numbers} if serial_numbers else None
    cats = {str(c).strip() for c in categories} if categories else None
    prod = product.strip() if product else None

    out = []
    for r in runs:
        if lo is not None and (r.started is None or r.started < lo):
            continue
        if hi is not None and (r.started is None or r.started >= hi):
            continue
        if prod is not None and r.product != prod:
            continue
        if sns is not None and r.serial_number.upper() not in sns:
            continue
        if cats is not None and r.category not in cats:
            continue
        out.append(r)
    return out


def date_span(runs: Iterable[TestRun]) -> tuple[dt.datetime | None, dt.datetime | None]:
    """数据实际覆盖的时间范围。前端用它提示用户能查到哪一段。

    下界取最早的开始时间，上界取最晚的**结束**时间。上界刻意用结束时间而不是
    开始时间：筛选是含左不含右的，如果上界等于最后一条的开始时间，
    用户把结束日期设成满量程反而会把最后一条排除掉。
    """
    rs = list(runs)
    starts = [r.started for r in rs if r.started is not None]
    if not starts:
        return (None, None)
    ends = [r.ended for r in rs if r.ended is not None]
    return (min(starts), max(ends + starts))


# ---------------------------------------------------------------------------
# 场次级良率
# ---------------------------------------------------------------------------

def run_yield(runs: Iterable[TestRun], *, first_only: bool = False) -> dict[str, Any]:
    """按测试场次算良率。

    first_only=True 只统计每台的第一次测试，即 FPY
    （First Pass Yield，首次通过率——衡量"一次做对"的能力）。

    返回里的 level="run" 是刻意加的：提醒调用方这是**场次级**良率，
    和 TestPointSummary 的测试点级良率分母不同，两者不可混用。
    """
    rs = [r for r in runs if not first_only or r.test_count == 1]
    n = len(rs)
    failed = sum(1 for r in rs if not r.passed)
    return {
        "level": "run",
        "first_only": first_only,
        "n_runs": n,
        "n_passed": n - failed,
        "n_failed": failed,
        "yield_pct": (n - failed) / n * 100.0 if n else None,
    }


# ---------------------------------------------------------------------------
# 时间趋势——有了时间戳才能做的事
# ---------------------------------------------------------------------------

def _bucket_of(d: dt.datetime, by: str) -> str:
    if by == "day":
        return d.strftime("%Y-%m-%d")
    if by == "week":
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"
    if by == "month":
        return d.strftime("%Y-%m")
    if by == "hour":
        return d.strftime("%Y-%m-%d %H:00")
    raise ValueError(f"不支持的时间粒度 {by!r}，可用：hour/day/week/month")


def trend(runs: Iterable[TestRun], *, by: str = "day",
          first_only: bool = False) -> list[dict[str, Any]]:
    """按时间粒度统计良率趋势，结果按时间正序。

    first_only=True 只统计每台的第一次测试，即 FPY 趋势。
    本项目对外只展示 FPY 口径，所以接口层固定传 True。
    """
    groups: dict[str, list[TestRun]] = {}
    for r in runs:
        if r.started is None:
            continue
        if first_only and r.test_count != 1:
            continue
        groups.setdefault(_bucket_of(r.started, by), []).append(r)

    out = []
    for bucket in sorted(groups):
        rs = groups[bucket]
        failed = sum(1 for r in rs if not r.passed)
        out.append({
            "bucket": bucket,
            "n_runs": len(rs),
            "n_passed": len(rs) - failed,
            "n_failed": failed,
            "yield_pct": (len(rs) - failed) / len(rs) * 100.0 if rs else None,
            "n_serials": len({r.serial_number for r in rs}),
        })
    return out
