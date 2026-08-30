"""纳斯达克 100 场外 QDII 公开渠道实况雷达。

第一阶段只报告可公开核验的代销渠道当前交易页。当前接入渠道是天天基金；
支付宝、理财通及其他未接入渠道一律不推断。基金公告只保留为后续扩展的
交叉核验候选，不再用于判定某个销售平台“现在能否买”。
"""

from __future__ import annotations

import argparse
import html
import http.client
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "settings.json"
STATE_PATH = ROOT / "state" / "last_snapshot.json"
REPORT_PATH = ROOT / "reports" / "latest-report.html"
BEIJING = ZoneInfo("Asia/Shanghai")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# 公开交易页不是批量接口。全局节流同时保留少量并发，避免高频抓取触发源站保护。
REQUEST_PACE_SECONDS = 0.30
_request_pace_lock = threading.Lock()
_next_request_start = 0.0


class DataSourceError(RuntimeError):
    """一个外部数据源没有返回可用内容。"""


def pace_request() -> None:
    """在并发抓取时对公共渠道做全局节流。"""
    global _next_request_start
    with _request_pace_lock:
        now = time.monotonic()
        wait_seconds = max(0.0, _next_request_start - now)
        _next_request_start = max(now, _next_request_start) + REQUEST_PACE_SECONDS
    if wait_seconds:
        time.sleep(wait_seconds)


@dataclass(frozen=True)
class Fund:
    code: str
    name: str
    fund_type: str


@dataclass(frozen=True)
class FundState:
    """一只份额在一个明确销售渠道的当前页面状态。"""

    code: str
    name: str
    status: str
    limit: str | None
    dca_status: str
    channel: str
    source_url: str | None
    checked_at: str
    confidence: str
    note: str | None = None
    reference_status: str = "unknown"
    reference_title: str | None = None
    reference_date: str | None = None
    reference_url: str | None = None


@dataclass(frozen=True)
class Change:
    code: str
    name: str
    kind: str
    before: FundState | None
    after: FundState


STATUS_META = {
    "open": ("开放申购", "#087f5b", "#eaf8f1"),
    "limited": ("限额申购", "#ae6700", "#fff5e6"),
    "suspended": ("暂停申购", "#c53c4c", "#fff0f2"),
    "unknown": ("待核验", "#66748a", "#f1f4f8"),
}


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{path.name} 不是合法 JSON：{exc}") from exc


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def request_text(url: str, *, headers: dict[str, str] | None = None, retries: int = 3) -> str:
    merged_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if headers:
        merged_headers.update(headers)
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            pace_request()
            request = Request(url, headers=merged_headers)
            with urlopen(request, timeout=20) as response:  # noqa: S310 - configured public HTTPS endpoint
                body = response.read()
                declared_charset = response.headers.get_content_charset()
                # 基金网页常标 UTF-8，也有个别页面遗漏或错误标注编码。
                for charset in dict.fromkeys(value for value in ("utf-8", "gb18030", declared_charset) if value):
                    try:
                        return body.decode(charset)
                    except UnicodeDecodeError:
                        continue
                return body.decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise DataSourceError(f"请求失败：{url}（{last_error}）")


def normalise_name(value: str) -> str:
    return re.sub(r"\s+", "", value)


def plain_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text).replace("\u3000", " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_json_or_jsonp(raw: str) -> Any:
    body = raw.strip()
    if body.startswith("{") or body.startswith("["):
        return json.loads(body)
    match = re.search(r"^[^(]+\((.*)\)\s*;?\s*$", body, flags=re.S)
    if not match:
        raise DataSourceError("数据源返回了无法识别的格式")
    return json.loads(match.group(1))


def get_field(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def is_relevant_reference_title(title: str) -> bool:
    """只把明确涉及申购或定投变更的公告用于背景核验。"""
    compact = normalise_name(title)
    has_business = any(token in compact for token in ("申购", "定期定额", "定投"))
    has_change = any(token in compact for token in ("暂停", "恢复", "调整", "限制", "开放"))
    is_holiday_only = any(token in compact for token in ("节假日", "非交易日", "境外主要投资场所"))
    return has_business and has_change and not is_holiday_only


def is_direct_only_reference(title: str) -> bool:
    """直销专属公告不能用来质疑或覆盖代销渠道的当前页面。"""
    compact = normalise_name(title)
    has_direct = "直销" in compact
    has_broader_scope = any(token in compact for token in ("代销", "销售机构", "所有渠道"))
    return has_direct and not has_broader_scope


def status_from_reference_title(title: str) -> str:
    """公告仅按标题的当前动作归类，避免正文引用历史状态造成反向误判。"""
    compact = normalise_name(title)
    if "暂停大额申购" in compact or any(token in compact for token in ("限制大额申购", "调整大额申购", "限制申购金额")):
        return "limited"
    if "暂停" in compact and "申购" in compact:
        return "suspended"
    if "恢复" in compact and any(token in compact for token in ("申购", "定期定额", "定投")):
        return "open"
    return "unknown"


def announcement_reference_for(fund: Fund, settings: dict[str, Any]) -> tuple[str, str | None, str | None, str | None]:
    """读取最近一条适用于非直销口径的公告，作为来源独立的背景核验。"""
    source = settings["sources"]["announcement_list"]
    url = source["url"].format(code=fund.code, timestamp=int(time.time() * 1000))
    raw = request_text(url, headers={"Referer": source["referer"].format(code=fund.code)})
    payload = parse_json_or_jsonp(raw)
    if not isinstance(payload, dict):
        raise DataSourceError("公告列表格式异常")
    rows = payload.get("Data", payload.get("data", []))
    if isinstance(rows, dict):
        rows = rows.get("List", rows.get("list", []))
    if not isinstance(rows, list):
        raise DataSourceError("公告列表没有可用条目")
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = get_field(row, "TITLE", "title", "NoticeTitle") or ""
        if not is_relevant_reference_title(title) or is_direct_only_reference(title):
            continue
        status = status_from_reference_title(title)
        if status == "unknown":
            continue
        announcement_id = get_field(row, "ID", "id", "ArtCode", "art_code")
        published_at = get_field(row, "PUBLISHDATEDesc", "PUBLISHDATE", "publishDate", "NOTICE_DATE")
        page = settings["sources"]["announcement_page"].format(announcement_id=announcement_id) if announcement_id else None
        return status, title, published_at, page
    return "unknown", None, None, None


def discover_funds(settings: dict[str, Any]) -> list[Fund]:
    """从公开基金代码表生成纳斯达克 100 场外 QDII 基金池。"""
    discovery = settings["sources"]["fund_code_table"]
    raw = request_text(discovery["url"], headers={"Referer": discovery["referer"]})
    match = re.search(r"(?:var\s+\w+\s*=\s*)?(\[.*\])\s*;?\s*$", raw, flags=re.S)
    if not match:
        raise DataSourceError("基金代码表格式已变化，无法自动发现基金")
    try:
        rows = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise DataSourceError("基金代码表不是可读取的数组") from exc

    pattern = re.compile(settings["universe"]["name_pattern"])
    excluded = set(settings["universe"].get("exclude_codes", []))
    required_codes = {str(code).zfill(6) for code in settings["universe"].get("required_codes", [])}
    discovered: dict[str, Fund] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 4:
            continue
        code, _pinyin, name, fund_type = (str(row[0]).strip(), str(row[1]), str(row[2]).strip(), str(row[3]).strip())
        compact_name = normalise_name(name)
        if not re.fullmatch(r"\d{6}", code) or code in excluded:
            continue
        # 代码表偶尔省略基金名称中的 “QDII” 标记。例如 160213 国泰纳斯达克
        # 100 指数在代码表中只标为“指数型-海外股票”。海外类型同样可作为场外
        # QDII 候选的公开分类依据，不能仅因缺少字样而漏掉。
        is_qdii = "QDII" in compact_name.upper() or "QDII" in fund_type.upper()
        is_overseas_fund = "海外" in compact_name or "海外" in fund_type
        if not pattern.search(compact_name) or not (is_qdii or is_overseas_fund):
            continue
        # 场内 ETF 不属于定投的场外基金范围；ETF 联接基金保留。
        if "ETF" in compact_name.upper() and "联接" not in compact_name:
            continue
        discovered[code] = Fund(code=code, name=name, fund_type=fund_type)

    for row in settings["universe"].get("pinned_funds", []):
        code = str(row["code"]).zfill(6)
        if code not in excluded:
            discovered[code] = Fund(code=code, name=str(row["name"]), fund_type=str(row.get("fund_type", "QDII")))

    # 经过人工审计的现有范围必须完整。上游代码表、筛选规则或命名变化若导致
    # 任一已知基金消失，宁可让本次任务失败，也不发送“看似正常”的残缺清单。
    missing_required = sorted(required_codes - set(discovered))
    if missing_required:
        raise DataSourceError(
            "基金池完整性校验失败，缺少已审计基金代码：" + "、".join(missing_required) + "；本次不推送"
        )

    min_fund_count = int(settings["safety"]["min_fund_count"])
    if len(discovered) < min_fund_count:
        raise DataSourceError(f"自动发现仅得到 {len(discovered)} 只基金，低于安全下限 {min_fund_count}；已停止本次推送")
    return sorted(discovered.values(), key=lambda item: (item.name, item.code))


def extract_table_value(page: str, label: str) -> str | None:
    """读取天天基金详情页中某个表格字段紧随其后的值。"""
    pattern = re.compile(
        rf"<td\b[^>]*>\s*{re.escape(label)}\s*</td>\s*<td\b[^>]*>(.*?)</td>",
        flags=re.I | re.S,
    )
    match = pattern.search(page)
    if not match:
        return None
    value = plain_text(match.group(1))
    return value or None


def status_from_tiantian(value: str | None) -> str:
    """将天天基金页面的申购文案映射为内部状态，不从公告推断。"""
    compact = normalise_name(value or "")
    if any(token in compact for token in ("暂停", "封闭", "停止")):
        return "suspended"
    if any(token in compact for token in ("限大额", "限额", "限制")):
        return "limited"
    if "开放" in compact:
        return "open"
    return "unknown"


def dca_from_tiantian(value: str | None) -> str:
    compact = normalise_name(value or "")
    if not compact or compact in {"---", "—"}:
        return "待核验"
    if "不支持" in compact or "暂停" in compact:
        return "不可定投"
    if "支持" in compact or "开放" in compact:
        return "可定投"
    return value or "待核验"


def format_amount(amount: Decimal) -> str:
    if amount == amount.to_integral():
        return f"¥{int(amount):,}"
    return f"¥{amount:,.2f}".rstrip("0").rstrip(".")


def limit_from_tiantian(value: str | None) -> str | None:
    compact = normalise_name(value or "")
    if not compact or compact in {"---", "—"} or "无限额" in compact:
        return None
    match = re.search(r"([0-9][0-9,]*(?:\.\d+)?)\s*元", compact)
    if not match:
        return None
    try:
        return format_amount(Decimal(match.group(1).replace(",", "")))
    except InvalidOperation:
        return None


def parse_tiantian_page(page: str) -> tuple[str, str | None, str, str | None]:
    """解析公开渠道页面，不把遗留限额数字误当成仍可申购。"""
    purchase_text = extract_table_value(page, "申购状态")
    dca_text = extract_table_value(page, "定投状态")
    limit_text = extract_table_value(page, "日累计申购限额")
    status = status_from_tiantian(purchase_text)
    limit = limit_from_tiantian(limit_text)
    note = None
    if status == "unknown":
        note = f"未能识别页面申购状态（原文：{purchase_text or '空'}）"
    elif status == "limited" and limit is None:
        note = "页面显示限额申购，但未给出可识别的日累计金额"
    elif status == "suspended" and limit is not None:
        # 页面可能保留上一次限额字段；暂停时该金额不能代表现在可以购买。
        note = "页面虽保留日限额字段，但当前已暂停申购，不展示为可买额度"
    return status, limit, dca_from_tiantian(dca_text), note


def unavailable_fund_state(fund: Fund, note: str, checked_at: str) -> FundState:
    return FundState(
        code=fund.code,
        name=fund.name,
        status="unknown",
        limit=None,
        dca_status="待核验",
        channel="天天基金",
        source_url=None,
        checked_at=checked_at,
        confidence="读取失败",
        note=note,
    )


def fetch_tiantian_state(fund: Fund, settings: dict[str, Any], checked_at: str) -> FundState:
    source = settings["sources"]["tiantian"]
    url = source["url"].format(code=fund.code)
    page = request_text(url, headers={"Referer": source["referer"]})
    if fund.code not in page:
        raise DataSourceError("详情页未包含目标基金代码")
    status, limit, dca_status, note = parse_tiantian_page(page)
    return FundState(
        code=fund.code,
        name=fund.name,
        status=status,
        limit=limit,
        dca_status=dca_status,
        channel=source["channel"],
        source_url=url,
        checked_at=checked_at,
        confidence="公开交易页" if status != "unknown" else "页面待核验",
        note=note,
    )


def fetch_fund_state(fund: Fund, settings: dict[str, Any], checked_at: str) -> FundState:
    """主渠道为天天基金；公告仅为独立的背景核验，不影响主渠道结论。"""
    state = fetch_tiantian_state(fund, settings, checked_at)
    try:
        reference_status, reference_title, reference_date, reference_url = announcement_reference_for(fund, settings)
    except DataSourceError as exc:
        logging.warning("%s 公告交叉核验暂不可用：%s", fund.code, exc)
        return state
    return replace(
        state,
        reference_status=reference_status,
        reference_title=reference_title,
        reference_date=reference_date,
        reference_url=reference_url,
    )


def serialise_state(item: FundState) -> dict[str, Any]:
    return asdict(item)


def deserialise_state(item: dict[str, Any]) -> FundState:
    allowed = {field.name for field in fields(FundState)}
    values = {key: value for key, value in item.items() if key in allowed}
    values.setdefault("dca_status", "待核验")
    values.setdefault("channel", "天天基金")
    values.setdefault("source_url", None)
    values.setdefault("checked_at", "")
    values.setdefault("confidence", "历史快照")
    values.setdefault("note", None)
    return FundState(**values)


def compare_snapshots(previous: Iterable[FundState], current: Iterable[FundState]) -> list[Change]:
    old_by_code = {item.code: item for item in previous}
    changes: list[Change] = []
    for after in current:
        before = old_by_code.get(after.code)
        if before is None:
            changes.append(Change(after.code, after.name, "new", None, after))
            continue
        if after.status == "unknown":
            continue  # 单次读取失败不覆盖历史状态，也不制造变动消息。
        if before.status != after.status:
            changes.append(Change(after.code, after.name, "status", before, after))
        elif before.limit != after.limit:
            changes.append(Change(after.code, after.name, "limit", before, after))
        elif before.dca_status != after.dca_status:
            changes.append(Change(after.code, after.name, "dca", before, after))
        elif has_reference_conflict(before) != has_reference_conflict(after):
            changes.append(Change(after.code, after.name, "reference", before, after))
    return changes


def status_badge(status: str) -> str:
    label, color, background = STATUS_META.get(status, STATUS_META["unknown"])
    return (
        f'<span style="display:inline-block;padding:3px 8px;border-radius:999px;'
        f'background:{background};color:{color};font-size:12px;font-weight:700;line-height:18px;">{label}</span>'
    )


def display_limit(item: FundState) -> str:
    if item.status == "suspended":
        return "—"
    if item.status == "open" and item.limit is None:
        return "无限额"
    return item.limit or "待核验"


def reference_label(item: FundState) -> str:
    """只描述是否与公告背景一致，绝不以公告替换渠道页面状态。"""
    if item.reference_status == "unknown":
        return "公告待核验"
    if item.reference_status == item.status:
        return "公告一致"
    return "公告有差异"


def has_reference_conflict(item: FundState) -> bool:
    return item.reference_status != "unknown" and item.reference_status != item.status


def change_label(change: Change) -> str:
    before = change.before
    after = change.after
    if change.kind == "new":
        return "新增监控"
    if change.kind == "status":
        before_label = STATUS_META.get(before.status, STATUS_META["unknown"])[0] if before else "待核验"
        after_label = STATUS_META.get(after.status, STATUS_META["unknown"])[0]
        return f"{before_label} → {after_label}"
    if change.kind == "limit":
        return f"{display_limit(before) if before else '—'} → {display_limit(after)}"
    if change.kind == "dca":
        return f"定投：{before.dca_status if before else '待核验'} → {after.dca_status}"
    if change.kind == "reference":
        return f"公告核验：{reference_label(before) if before else '待核验'} → {reference_label(after)}"
    return "状态更新，请查看渠道页面"


def product_name(name: str) -> str:
    """同规则的 A/C、人民币/美元份额可以在卡片中合并展示。"""
    result = normalise_name(name)
    result = re.sub(r"[（(]QDII(?:-LOF)?[）)]", "", result, flags=re.I)
    result = re.sub(r"(?:人民币|美元(?:现汇|现钞)?)", "", result)
    result = re.sub(r"[ACDEFI]$", "", result)
    return result.rstrip("- ") or name


def display_sort_key(item: FundState) -> tuple[int, Decimal, str, str]:
    """让可定投且日上限更高的基金优先出现，便于快速找到可操作额度。"""
    active = item.status in {"open", "limited"}
    can_dca = item.dca_status == "可定投"
    if active and can_dca:
        tier = 0
        if item.status == "open" and item.limit is None:
            # 页面明确开放且没有限额时，展示为无限额，应位于所有有限额度之前。
            amount_rank = Decimal("-Infinity")
        elif item.limit:
            try:
                amount_rank = -Decimal(item.limit.removeprefix("¥").replace(",", ""))
            except InvalidOperation:
                amount_rank = Decimal("Infinity")
        else:
            # 限额但页面没有可识别金额时，不能假装额度很高。
            amount_rank = Decimal("Infinity")
    elif active:
        tier = 1
        amount_rank = Decimal("Infinity")
    elif item.status == "suspended":
        tier = 2
        amount_rank = Decimal("Infinity")
    else:
        tier = 3
        amount_rank = Decimal("Infinity")
    return tier, amount_rank, product_name(item.name), item.code


def group_for_display(states: list[FundState]) -> list[tuple[str, list[FundState]]]:
    groups: dict[tuple[str, str, str | None, str, str, str], list[FundState]] = {}
    for item in states:
        key = (product_name(item.name), item.status, item.limit, item.dca_status, item.channel, item.reference_status)
        groups.setdefault(key, []).append(item)
    return sorted(
        ((key[0], members) for key, members in groups.items()),
        key=lambda item: display_sort_key(item[1][0]),
    )


def build_message_html(
    states: list[FundState], changes: list[Change], now: datetime, *, is_digest: bool, initial: bool
) -> tuple[str, str]:
    grouped = group_for_display(states)
    counts = {key: sum(item.status == key for item in states) for key in STATUS_META}
    reference_counts = {
        "consistent": sum(item.reference_status != "unknown" and item.reference_status == item.status for item in states),
        "conflict": sum(has_reference_conflict(item) for item in states),
        "unknown": sum(item.reference_status == "unknown" for item in states),
    }
    title_prefix = "首次建档" if initial else "每日汇总" if is_digest else "状态变动"
    title = f"纳指100公开渠道｜{title_prefix}"
    date_label = now.strftime("%Y年%m月%d日 %H:%M")
    change_block = ""
    if changes:
        rows = "".join(
            (
                '<tr><td style="padding:10px 0;border-bottom:1px solid #e8edf4;vertical-align:top;">'
                f'<div style="font-weight:760;color:#15233b;font-size:14px;">{html.escape(change.name)}</div>'
                f'<div style="margin-top:3px;color:#72819a;font-size:12px;">{html.escape(change.code)} · {html.escape(change_label(change))}</div>'
                "</td></tr>"
            )
            for change in changes[:12]
        )
        more = "" if len(changes) <= 12 else f'<div style="color:#657690;font-size:12px;margin-top:8px;">另有 {len(changes) - 12} 项变动，详见完整清单</div>'
        change_block = (
            '<div style="margin:18px 0 14px;padding:15px 16px;border:1px solid #d8e4fb;background:#f6f9ff;border-radius:12px;">'
            '<div style="font-size:13px;font-weight:800;color:#2c5499;letter-spacing:.2px;">本次变化</div>'
            f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:4px;border-collapse:collapse;">{rows}</table>{more}</div>'
        )
    table_rows: list[str] = []
    for display_name, members in grouped:
        item = members[0]
        codes = "、".join(member.code for member in members)
        shares = f"{len(members)} 份额" if len(members) > 1 else "1 份额"
        notes = sorted({member.note for member in members if member.note})
        reference_note = reference_label(item)
        note = f'<div style="margin-top:3px;color:#9b6b18;font-size:11px;line-height:16px;">{html.escape("；".join(notes))}</div>' if notes else ""
        table_rows.append(
            '<tr>'
            '<td style="padding:13px 8px 13px 0;border-bottom:1px solid #e9eef5;vertical-align:top;">'
            f'<div style="font-weight:760;color:#182740;font-size:13px;line-height:19px;">{html.escape(display_name)}</div>'
            f'<div style="margin-top:3px;color:#8290a5;font-size:11px;line-height:17px;">{html.escape(shares)} · {html.escape(codes)} · {html.escape(item.channel)} · {html.escape(reference_note)}</div>{note}'
            '</td>'
            '<td style="padding:13px 5px;border-bottom:1px solid #e9eef5;vertical-align:top;text-align:center;white-space:nowrap;">'
            f'{status_badge(item.status)}<div style="margin-top:5px;color:#697993;font-size:11px;">定投：{html.escape(item.dca_status)}</div>'
            '</td>'
            '<td style="padding:13px 0 13px 5px;border-bottom:1px solid #e9eef5;vertical-align:top;text-align:right;white-space:nowrap;">'
            f'<div style="font-size:13px;font-weight:800;color:#1f2e48;">{html.escape(display_limit(item))}</div>'
            '</td>'
            '</tr>'
        )
    summary = " · ".join(
        (
            f'<span style="color:#087f5b;font-weight:800;">开放 {counts["open"]}</span>',
            f'<span style="color:#ae6700;font-weight:800;">限额 {counts["limited"]}</span>',
            f'<span style="color:#c53c4c;font-weight:800;">暂停 {counts["suspended"]}</span>',
            f'<span style="color:#67758c;font-weight:800;">待核验 {counts["unknown"]}</span>',
        )
    )
    message = f'''<div style="max-width:680px;margin:0 auto;background:#ffffff;color:#18243a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;">
  <div style="padding:24px 22px 22px;background:linear-gradient(135deg,#101a31 0%,#254883 100%);border-radius:16px 16px 0 0;color:#ffffff;">
    <div style="font-size:12px;letter-spacing:1.3px;opacity:.75;">NASDAQ-100 · PUBLIC DISTRIBUTOR MONITOR</div>
    <div style="margin-top:8px;font-size:22px;font-weight:800;letter-spacing:.2px;">纳指100 公开渠道实况雷达</div>
    <div style="margin-top:9px;font-size:12px;color:#cbd9fb;">{date_label}（北京时间） · 主渠道：天天基金</div>
  </div>
  <div style="padding:18px 18px 22px;border:1px solid #e3eaf4;border-top:0;border-radius:0 0 16px 16px;">
    <div style="padding:12px 14px;border-radius:10px;background:#f4f7fc;color:#50617b;font-size:13px;line-height:21px;">{summary}</div>
    <div style="margin-top:8px;color:#718199;font-size:11px;line-height:17px;">公告交叉核验：一致 {reference_counts["consistent"]} · 有差异 {reference_counts["conflict"]} · 待核验 {reference_counts["unknown"]}</div>
    {change_block}
    <div style="margin:16px 0 3px;font-size:13px;font-weight:800;color:#26364f;">完整清单</div>
    <div style="margin:0 0 8px;color:#718199;font-size:11px;line-height:17px;">可定投基金优先，按该渠道日申购上限从高到低排列；无限额排最前。</div>
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;table-layout:fixed;">
      <thead><tr>
        <th style="padding:8px 8px 8px 0;text-align:left;color:#8996a9;font-size:11px;font-weight:700;">基金 / 已核验渠道</th>
        <th style="padding:8px 5px;text-align:center;color:#8996a9;font-size:11px;font-weight:700;white-space:nowrap;">申购 / 定投</th>
        <th style="padding:8px 0 8px 5px;text-align:right;color:#8996a9;font-size:11px;font-weight:700;white-space:nowrap;">日申购上限</th>
      </tr></thead>
      <tbody>{''.join(table_rows)}</tbody>
    </table>
    <div style="margin-top:15px;padding-top:12px;border-top:1px solid #e9eef5;color:#7f8ea4;font-size:11px;line-height:18px;">
      主状态来自天天基金公开交易详情页；基金公司适用公告只用于交叉核验，不会覆盖渠道页面。支付宝、理财通及其他未接入渠道不在本卡中推断。暂停申购时，即使网页仍保留历史限额数字，也会显示“—”。<br>
      本消息只作公开信息提醒，不构成投资建议。
    </div>
  </div>
</div>'''
    if len(message) > 18_500:
        message = build_compact_message_html(states, changes, now)
    return title, message


def build_compact_message_html(states: list[FundState], changes: list[Change], now: datetime) -> str:
    """当完整视觉卡片过长时，换用保留全量信息的紧凑版。"""
    grouped = group_for_display(states)
    counts = {key: sum(item.status == key for item in states) for key in STATUS_META}
    reference_counts = {
        "consistent": sum(item.reference_status != "unknown" and item.reference_status == item.status for item in states),
        "conflict": sum(has_reference_conflict(item) for item in states),
        "unknown": sum(item.reference_status == "unknown" for item in states),
    }
    change_text = "；".join(f"{change.name}：{change_label(change)}" for change in changes[:6])
    change_block = (
        f'<div style="margin:10px 0;padding:8px 10px;background:#f3f7ff;color:#36558f;font-size:12px;line-height:18px;border-radius:7px;">变化：{html.escape(change_text)}</div>'
        if change_text
        else ""
    )
    rows: list[str] = []
    for display_name, members in grouped:
        item = members[0]
        codes = "、".join(member.code for member in members)
        label = STATUS_META.get(item.status, STATUS_META["unknown"])[0]
        rows.append(
            '<tr><td style="padding:8px 5px 8px 0;border-bottom:1px solid #edf0f4;vertical-align:top;">'
            f'<b style="font-size:12px;color:#1b2942;">{html.escape(display_name)}</b><br>'
            f'<span style="font-size:10px;color:#7d899d;">{html.escape(codes)} · 天天基金 · {html.escape(reference_label(item))}</span>'
            '</td><td style="padding:8px 0;border-bottom:1px solid #edf0f4;text-align:right;vertical-align:top;white-space:nowrap;">'
            f'<b style="font-size:11px;color:#40516e;">{label}</b><br>'
            f'<span style="font-size:11px;color:#172844;">定投 {html.escape(item.dca_status)} · {html.escape(display_limit(item))}</span>'
            '</td></tr>'
        )
    summary = f'开放 {counts["open"]} · 限额 {counts["limited"]} · 暂停 {counts["suspended"]} · 待核验 {counts["unknown"]}'
    message = f'''<div style="max-width:680px;margin:0 auto;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;color:#1b2942;">
  <div style="padding:18px;background:#182f5d;color:#fff;border-radius:12px 12px 0 0;">
    <div style="font-size:17px;font-weight:800;">纳指100 公开渠道实况雷达</div>
    <div style="margin-top:6px;font-size:11px;color:#c8d7fb;">{now.strftime("%Y年%m月%d日 %H:%M")}（北京时间） · 天天基金</div>
  </div>
  <div style="padding:13px;border:1px solid #e3e8f0;border-top:0;border-radius:0 0 12px 12px;">
    <div style="padding:8px 10px;background:#f5f7fa;border-radius:8px;font-size:12px;color:#516078;">{summary}</div>
    <div style="margin-top:7px;color:#718199;font-size:10px;">公告核验：一致 {reference_counts["consistent"]} · 差异 {reference_counts["conflict"]} · 待核验 {reference_counts["unknown"]}</div>
    {change_block}
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:8px;border-collapse:collapse;">{''.join(rows)}</table>
    <div style="margin-top:10px;color:#8a95a5;font-size:10px;line-height:16px;">主状态为天天基金公开交易页；公告只作交叉核验，不覆盖渠道结论。支付宝、理财通及其他未接入渠道不作推断；暂停时不显示任何可买额度。本消息不构成投资建议。</div>
  </div>
</div>'''
    if len(message) > 18_500:
        raise RuntimeError("紧凑版推送仍超过安全长度；请缩小基金范围")
    return message


def send_pushplus(title: str, content: str, token: str) -> None:
    payload = json.dumps(
        {"token": token, "title": title, "content": content, "template": "html", "channel": "wechat"},
        ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        "https://www.pushplus.plus/send",
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urlopen(request, timeout=25) as response:  # noqa: S310 - fixed PushPlus API endpoint
            raw = response.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        raise DataSourceError(f"PushPlus 推送失败：{exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DataSourceError("PushPlus 返回了无法识别的内容") from exc
    if not isinstance(data, dict) or data.get("code") not in (200, "200"):
        message = data.get("msg") if isinstance(data, dict) else raw[:200]
        raise DataSourceError(f"PushPlus 返回失败：{message}")


def resolve_current_state(
    funds: list[Fund], settings: dict[str, Any], previous: list[FundState], checked_at: str
) -> tuple[list[FundState], list[str]]:
    previous_by_code = {item.code: item for item in previous}
    results: list[FundState] = []
    errors: list[str] = []
    workers = max(1, int(settings["safety"].get("max_parallel_requests", 2)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="public-source") as executor:
        pending = {executor.submit(fetch_fund_state, fund, settings, checked_at): fund for fund in funds}
        for index, future in enumerate(as_completed(pending), start=1):
            fund = pending[future]
            try:
                results.append(future.result())
            except Exception as exc:  # 单只页面故障不能让整批误报为暂停。
                errors.append(f"{fund.code} {fund.name}: {type(exc).__name__}: {exc}")
                if fund.code in previous_by_code:
                    results.append(previous_by_code[fund.code])
                else:
                    results.append(unavailable_fund_state(fund, "本次渠道页面读取失败，已安排下次复查", checked_at))
            logging.info("已检查 %s/%s：%s", index, len(funds), fund.code)
    return sorted(results, key=lambda item: item.code), errors


def run(*, dry_run: bool, force_digest: bool) -> int:
    settings = load_json(CONFIG_PATH, None)
    if not isinstance(settings, dict):
        raise RuntimeError("缺少 config/settings.json")
    stored = load_json(STATE_PATH, {"version": 2, "funds": [], "last_digest_date": None})
    # 公告版快照没有渠道页面字段，不能与新版混比；首个成功结果将重新建档。
    previous = [deserialise_state(item) for item in stored.get("funds", [])] if stored.get("version") == 2 else []
    now = datetime.now(BEIJING)
    checked_at = now.isoformat(timespec="seconds")
    funds = discover_funds(settings)
    current, errors = resolve_current_state(funds, settings, previous, checked_at)
    max_failures = max(1, int(len(funds) * float(settings["safety"]["max_failure_ratio"])))
    if not previous and errors:
        raise DataSourceError(f"首次渠道建档有 {len(errors)}/{len(funds)} 只基金页面读取失败；本次不推送，等待下次完整复查")
    if len(errors) > max_failures:
        raise DataSourceError(f"本次有 {len(errors)}/{len(funds)} 只基金页面读取失败，超过安全阈值 {max_failures}；没有覆盖历史状态")
    if not current:
        raise DataSourceError("没有获得任何基金状态；没有推送")

    initial = not previous
    changes = compare_snapshots(previous, current) if previous else [Change(item.code, item.name, "new", None, item) for item in current]
    is_digest = force_digest or stored.get("last_digest_date") != now.date().isoformat()
    should_push = initial or is_digest or bool(changes)
    display_changes = [] if initial else changes
    title, message = build_message_html(current, display_changes, now, is_digest=is_digest, initial=initial)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(message, encoding="utf-8")

    if should_push and not dry_run:
        token = os.getenv("PUSHPLUS_TOKEN", "").strip()
        if not token:
            raise RuntimeError("需要 GitHub Secret：PUSHPLUS_TOKEN；本次没有发送推送")
        send_pushplus(title, message, token)
        logging.info("PushPlus 推送成功：%s", title)
    elif should_push:
        logging.info("试运行：已生成推送预览，但未发送")
    else:
        logging.info("渠道状态未变化，且今日已发送日报；不重复推送")

    write_json(
        STATE_PATH,
        {
            "version": 2,
            "source_model": "public-distributor-channel",
            "last_digest_date": now.date().isoformat() if is_digest else stored.get("last_digest_date"),
            "funds": [serialise_state(item) for item in current],
        },
    )
    if errors:
        logging.warning("本次有 %s 项页面读取异常，已保留可用的历史状态：\n%s", len(errors), "\n".join(errors))
    return 0


def render_saved_preview() -> int:
    """在不联网、不推送的前提下，重建最近一次消息卡片。"""
    stored = load_json(STATE_PATH, {"funds": []})
    if stored.get("version") != 2:
        raise RuntimeError("现有快照属于旧公告版；请先成功完成一次公开渠道建档")
    states = [deserialise_state(item) for item in stored.get("funds", [])]
    if not states:
        raise RuntimeError("没有可预览的历史状态，请先成功运行一次采集")
    _title, message = build_message_html(states, [], datetime.now(BEIJING), is_digest=True, initial=False)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(message, encoding="utf-8")
    logging.info("已生成离线预览：%s（%s 个字符）", REPORT_PATH, len(message))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="纳斯达克100场外 QDII 公开渠道监控")
    parser.add_argument("--dry-run", action="store_true", help="只生成 reports/latest-report.html，不调用 PushPlus")
    parser.add_argument("--force-digest", action="store_true", help="无论当天是否已推送日报，都生成一条完整日报")
    parser.add_argument("--preview", action="store_true", help="用最近一次保存的状态离线生成消息预览")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    try:
        if args.preview:
            return render_saved_preview()
        return run(dry_run=args.dry_run, force_digest=args.force_digest)
    except (DataSourceError, RuntimeError) as exc:
        logging.error("运行终止：%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
