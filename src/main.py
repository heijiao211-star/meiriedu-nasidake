"""纳斯达克 100 场外 QDII 代销额度雷达。

数据以基金公告为依据，只展示代销渠道（含支付宝）的申购/定投口径。
直销专属公告不会覆盖代销状态；支付宝 App 的即时可买状态仍以下单页为准。
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
from dataclasses import asdict, dataclass
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
# 公告接口对突发并发较敏感。所有请求至少相隔 0.35 秒启动；网络读取仍可重叠，
# 因此不会牺牲太多运行速度，却能显著降低 RemoteDisconnected 的概率。
REQUEST_PACE_SECONDS = 0.35
_request_pace_lock = threading.Lock()
_next_request_start = 0.0


class DataSourceError(RuntimeError):
    """一个外部数据源没有返回可用内容。"""


def pace_request() -> None:
    """在并发抓取时对公共公告源做全局节流。"""
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
    code: str
    name: str
    status: str
    limit: str | None
    channel: str
    announcement_id: str | None
    announcement_title: str | None
    announcement_date: str | None
    source_url: str | None
    confidence: str
    note: str | None = None


@dataclass(frozen=True)
class Change:
    code: str
    name: str
    kind: str
    before: FundState | None
    after: FundState


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


def request_text(url: str, *, headers: dict[str, str] | None = None, retries: int = 2) -> str:
    merged_headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, text/html, */*"}
    if headers:
        merged_headers.update(headers)
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            pace_request()
            request = Request(url, headers=merged_headers)
            with urlopen(request, timeout=15) as response:  # noqa: S310 - public, configured HTTPS endpoints
                body = response.read()
                # 东方财富部分公告接口会把 GBK/GB18030 正文错误标为 ISO-8859-1。
                # 先严格按 UTF-8、再按国标扩展编码解码，最后才信任响应头，避免
                # “额度数字可见、代销/直销中文字段乱码”导致渠道判断失真。
                declared_charset = response.headers.get_content_charset()
                candidates = ("utf-8", "gb18030", declared_charset)
                for charset in dict.fromkeys(charset for charset in candidates if charset):
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


def parse_json_or_jsonp(raw: str) -> Any:
    body = raw.strip()
    if body.startswith("{") or body.startswith("["):
        return json.loads(body)
    match = re.search(r"^[^(]+\((.*)\)\s*;?\s*$", body, flags=re.S)
    if not match:
        raise DataSourceError("数据源返回了无法识别的格式")
    return json.loads(match.group(1))


def normalise_name(value: str) -> str:
    return re.sub(r"\s+", "", value)


def discover_funds(settings: dict[str, Any]) -> list[Fund]:
    """从天天基金的全量基金代码表发现范围内基金，并合并人工补充项。

    代码表只承担“基金池发现”角色；真正状态来自公告正文。这样新基金发行后无需改代码。
    """
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
    discovered: dict[str, Fund] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 4:
            continue
        code, _pinyin, name, fund_type = (str(row[0]).strip(), str(row[1]), str(row[2]).strip(), str(row[3]).strip())
        compact_name = normalise_name(name)
        if not re.fullmatch(r"\d{6}", code) or code in excluded:
            continue
        is_qdii = "QDII" in compact_name.upper() or "QDII" in fund_type.upper()
        if not pattern.search(compact_name) or not is_qdii:
            continue
        # 直接在交易所买卖的 ETF 不属于本项目的场外定投范围；ETF 联接基金保留。
        # 基金代码表的类别常写作“指数型-海外股票”，故不能只依赖类别字段判断 ETF。
        if "ETF" in compact_name.upper() and "联接" not in compact_name:
            continue
        discovered[code] = Fund(code=code, name=name, fund_type=fund_type)

    for row in settings["universe"].get("pinned_funds", []):
        code = str(row["code"]).zfill(6)
        if code not in excluded:
            discovered[code] = Fund(code=code, name=str(row["name"]), fund_type=str(row.get("fund_type", "QDII")))

    min_fund_count = int(settings["safety"]["min_fund_count"])
    if len(discovered) < min_fund_count:
        raise DataSourceError(f"自动发现仅得到 {len(discovered)} 只基金，低于安全下限 {min_fund_count}；已停止本次推送")
    return sorted(discovered.values(), key=lambda item: (item.name, item.code))


def announcements_for(fund: Fund, settings: dict[str, Any]) -> list[dict[str, Any]]:
    source = settings["sources"]["announcement_list"]
    url = source["url"].format(code=fund.code, timestamp=int(time.time() * 1000))
    raw = request_text(url, headers={"Referer": source["referer"].format(code=fund.code)})
    payload = parse_json_or_jsonp(raw)
    if isinstance(payload, dict):
        rows = payload.get("Data", payload.get("data", []))
        if isinstance(rows, dict):
            rows = rows.get("List", rows.get("list", []))
    else:
        rows = []
    if not isinstance(rows, list):
        raise DataSourceError(f"{fund.code} 的公告列表格式异常")
    return [item for item in rows if isinstance(item, dict)]


def announcement_content(announcement_id: str, settings: dict[str, Any]) -> str:
    source = settings["sources"]["announcement_content"]
    # 公告正文接口偶发主动断开；正文是渠道识别的必要依据，因此多试几次，
    # 宁可保留上一次可信结果，也不以标题猜测代销额度。
    raw = request_text(source["url"].format(announcement_id=announcement_id), retries=4)
    payload = parse_json_or_jsonp(raw)
    if not isinstance(payload, dict):
        raise DataSourceError("公告正文格式异常")
    data = payload.get("data", payload.get("Data", {}))
    if not isinstance(data, dict):
        raise DataSourceError("公告正文为空")
    content = data.get("notice_content", data.get("NOTICE_CONTENT", ""))
    if not isinstance(content, str) or not content.strip():
        raise DataSourceError("公告正文为空")
    return content


def plain_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text).replace("\u3000", " ")
    return re.sub(r"\s+", " ", text).strip()


def is_relevant_announcement(title: str) -> bool:
    compact = normalise_name(title)
    has_business = any(token in compact for token in ("申购", "定期定额", "定投"))
    has_change = any(token in compact for token in ("暂停", "恢复", "调整", "限制", "开放"))
    # 节假日的临时闭市公告不能覆盖真正的申购额度状态。
    is_holiday_only = any(token in compact for token in ("节假日", "非交易日", "境外主要投资场所"))
    return has_business and has_change and not is_holiday_only


def is_direct_only_announcement(title: str) -> bool:
    """判断标题是否明确只调整直销渠道。

    直销专属公告不能代表支付宝等代销平台的状态。遇到这类公告时，继续回溯
    该基金最近一条适用于代销渠道的公告，而不是把直销额度误报给用户。
    """
    compact = normalise_name(title)
    has_direct = "直销" in compact
    has_agency = any(token in compact for token in ("代销", "销售机构"))
    return has_direct and not has_agency


def get_field(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def find_limit(text: str, fund_code: str) -> str | None:
    """从公告中保守提取金额；无法确定时宁可返回空，也不猜测。"""
    compact = normalise_name(text)
    positions = [match.start() for match in re.finditer(re.escape(fund_code), compact)]
    windows: list[str] = []
    for position in positions:
        windows.append(compact[max(0, position - 180) : position + 500])
    windows.append(compact)
    patterns = (
        r"(?:不应超过|不得超过|不超过|上限(?:为|调整为)?|限制金额(?:为)?)[^0-9]{0,24}([0-9][0-9,]*(?:\.\d+)?)\s*(?:人民币)?元",
        r"(?:单日|每日)[^。；]{0,70}?([0-9][0-9,]*(?:\.\d+)?)\s*(?:人民币)?元",
        r"(?:调整为|由[^。；]{0,50}?调整至)[^0-9]{0,24}([0-9][0-9,]*(?:\.\d+)?)\s*(?:人民币)?元",
    )
    for window in windows:
        for pattern in patterns:
            match = re.search(pattern, window)
            if not match:
                continue
            value = match.group(1).replace(",", "")
            try:
                amount = Decimal(value)
            except InvalidOperation:
                continue
            if amount >= 0:
                return format_amount(amount)
    return None


def find_agency_limit(text: str) -> str | None:
    """优先提取明确写给代销/销售机构的额度。

    不少公告会先列直销额度、再列代销额度。通用金额提取会误拿到前者，
    所以先在“代销机构/销售机构”附近寻找金额；无法定位时才使用公告统一口径。
    """
    compact = normalise_name(text)
    amount_patterns = (
        r"(?:不应超过|不得超过|不超过|上限(?:为|调整为)?|限制金额(?:为)?)[^0-9]{0,24}([0-9][0-9,]*(?:\.\d+)?)\s*(?:人民币)?元",
        r"(?:单日|每日)[^。；]{0,70}?([0-9][0-9,]*(?:\.\d+)?)\s*(?:人民币)?元",
        r"(?:调整为|由[^。；]{0,50}?调整至)[^0-9]{0,24}([0-9][0-9,]*(?:\.\d+)?)\s*(?:人民币)?元",
    )
    marker = re.compile(r"(?:通过)?(?:各)?(?:代销机构|销售机构)")
    for marker_match in marker.finditer(compact):
        # 公告里渠道说明到金额之间通常是一句完整限制条件；留出足够窗口，
        # 但不跨到下一条渠道规则，以免重新拾取直销金额。
        window = compact[marker_match.start() : marker_match.start() + 280]
        for pattern in amount_patterns:
            amount_match = re.search(pattern, window)
            if not amount_match:
                continue
            try:
                return format_amount(Decimal(amount_match.group(1).replace(",", "")))
            except InvalidOperation:
                continue
    return None


def format_amount(amount: Decimal) -> str:
    if amount == amount.to_integral():
        return f"¥{int(amount):,}"
    return f"¥{amount:,.2f}".rstrip("0").rstrip(".")


def classify_state(title: str, content: str, fund_code: str) -> tuple[str, str | None, str, str | None]:
    text = normalise_name(f"{title} {plain_text(content)}")
    agency_limit = find_agency_limit(text)
    # 没有单列代销条件时，公告的统一表述通常同样适用于代销渠道；但若明确
    # 单列代销条件，绝不能让先出现的直销金额覆盖它。
    limit = agency_limit or find_limit(text, fund_code)
    if re.search(r"恢复(?:办理)?(?:大额)?申购", text) or re.search(r"恢复(?:办理)?定期定额", text):
        status = "open"
    elif re.search(r"暂停(?:办理)?申购", text) and not re.search(r"暂停大额申购", text):
        status = "suspended"
    elif any(token in text for token in ("暂停大额申购", "限制大额申购", "限制申购金额", "调整大额申购")):
        status = "limited"
    else:
        status = "unknown"

    has_direct = "直销" in text
    has_agency = any(token in text for token in ("代销", "销售机构", "各代销"))
    if has_agency:
        channel = "代销渠道（含支付宝）"
    elif has_direct:
        channel = "代销口径（公告另列直销）"
    else:
        channel = "代销口径（公告统一）"

    note = None
    if status == "limited" and limit is None:
        note = "公告确认限额，但金额未能可靠提取"
    if status == "unknown":
        note = "公告标题无法可靠判定当前状态"
    return status, limit, channel, note


def source_url(announcement_id: str | None, settings: dict[str, Any]) -> str | None:
    if not announcement_id:
        return None
    return settings["sources"]["announcement_page"].format(announcement_id=announcement_id)


def unavailable_fund_state(fund: Fund, note: str, *, channel: str = "待公告核验") -> FundState:
    return FundState(
        code=fund.code,
        name=fund.name,
        status="unknown",
        limit=None,
        channel=channel,
        announcement_id=None,
        announcement_title=None,
        announcement_date=None,
        source_url=None,
        confidence="待下次复查",
        note=note,
    )


def fetch_fund_state(fund: Fund, settings: dict[str, Any]) -> FundState:
    rows = announcements_for(fund, settings)
    last_content_error: DataSourceError | None = None
    for row in rows:
        title = get_field(row, "TITLE", "title", "NoticeTitle") or ""
        if not is_relevant_announcement(title):
            continue
        if is_direct_only_announcement(title):
            # 只改变直销渠道的公告不影响支付宝等代销渠道，继续寻找上一条代销口径公告。
            continue
        announcement_id = get_field(row, "ID", "id", "ArtCode", "art_code")
        published_at = get_field(row, "PUBLISHDATEDesc", "PUBLISHDATE", "publishDate", "NOTICE_DATE")
        if not announcement_id:
            continue
        try:
            content = announcement_content(announcement_id, settings)
            status, limit, channel, note = classify_state(title, content, fund.code)
        except DataSourceError as exc:
            # 标题无法可靠判断直销/代销条件；继续回溯较早公告。若都失败，
            # 让上层保留历史快照，而不是用一条不完整状态覆盖它。
            last_content_error = exc
            continue
        return FundState(
            code=fund.code,
            name=fund.name,
            status=status,
            limit=limit,
            channel=channel,
            announcement_id=announcement_id,
            announcement_title=title,
            announcement_date=published_at,
            source_url=source_url(announcement_id, settings),
            confidence="公告标题+正文" if not note else "公告标题",
            note=note,
        )
    if last_content_error:
        raise DataSourceError(f"{fund.code} 未能读取任何可用公告正文：{last_content_error}")
    return unavailable_fund_state(fund, "未找到可判定当前限额的公告")


def serialise_state(item: FundState) -> dict[str, Any]:
    return asdict(item)


def deserialise_state(item: dict[str, Any]) -> FundState:
    return FundState(**item)


def compare_snapshots(previous: Iterable[FundState], current: Iterable[FundState]) -> list[Change]:
    old_by_code = {item.code: item for item in previous}
    changes: list[Change] = []
    for after in current:
        before = old_by_code.get(after.code)
        if before is None:
            changes.append(Change(after.code, after.name, "new", None, after))
            continue
        if after.status == "unknown":
            continue  # 不用一次解析失败覆盖可信的历史结果。
        if before.status != after.status:
            kind = "opened" if after.status == "open" else "suspended" if after.status == "suspended" else "status"
            changes.append(Change(after.code, after.name, kind, before, after))
        elif before.limit != after.limit:
            changes.append(Change(after.code, after.name, "limit", before, after))
        elif before.announcement_id != after.announcement_id and after.status != "unknown":
            changes.append(Change(after.code, after.name, "announcement", before, after))
    return changes


STATUS_META = {
    "open": ("正常开放", "#0d8a61", "#eaf8f1"),
    "limited": ("限额申购", "#b86b06", "#fff6e8"),
    "suspended": ("暂停申购", "#c53c4c", "#fff0f2"),
    "unknown": ("待核验", "#67758c", "#f2f5f8"),
}


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
        return "未见大额限制"
    return item.limit or "待核验"


def change_label(change: Change) -> str:
    before = change.before
    after = change.after
    if change.kind == "new":
        return "新增监控"
    if change.kind == "opened":
        return f"{STATUS_META.get(before.status, STATUS_META['unknown'])[0]} → 正常开放" if before else "恢复开放"
    if change.kind == "suspended":
        return "已暂停申购/定投"
    if change.kind == "limit":
        return f"{display_limit(before) if before else '—'} → {display_limit(after)}"
    return "公告更新，请查看状态"


def product_name(name: str) -> str:
    """将同一产品的人民币/美元及 A、C 等份额归为一个显示项。

    只在“状态、限额、渠道”完全一致时合并，避免把规则不同的份额混在一起。
    """
    result = normalise_name(name)
    result = re.sub(r"[（(]QDII(?:-LOF)?[）)]", "", result, flags=re.I)
    result = re.sub(r"(?:人民币|美元(?:现汇|现钞)?)", "", result)
    result = re.sub(r"[ACDEFI]$", "", result)
    return result.rstrip("- ") or name


def group_for_display(states: list[FundState]) -> list[tuple[str, list[FundState]]]:
    groups: dict[tuple[str, str, str | None, str], list[FundState]] = {}
    for item in states:
        key = (product_name(item.name), item.status, item.limit, item.channel)
        groups.setdefault(key, []).append(item)
    return sorted(
        ((key[0], members) for key, members in groups.items()),
        key=lambda item: ({"open": 0, "limited": 1, "suspended": 2, "unknown": 3}.get(item[1][0].status, 4), item[0]),
    )


def build_compact_message_html(states: list[FundState], changes: list[Change], now: datetime) -> str:
    """PushPlus 内容接近上限时使用的紧凑版，保证全量清单不会被截断。"""
    grouped = group_for_display(states)
    counts = {key: sum(item.status == key for item in states) for key in STATUS_META}
    rows: list[str] = []
    for display_name, members in grouped:
        item = members[0]
        labels = "、".join(member.code for member in members)
        share_label = f"{len(members)}份额" if len(members) > 1 else "1份额"
        status_label = STATUS_META.get(item.status, STATUS_META["unknown"])[0]
        rows.append(
            '<tr><td style="padding:9px 5px 9px 0;border-bottom:1px solid #edf0f4;vertical-align:top;">'
            f'<b style="color:#1b2942;font-size:13px;">{html.escape(display_name)}</b><br>'
            f'<span style="color:#7d899d;font-size:11px;">{html.escape(share_label)} · {html.escape(labels)} · {html.escape(item.channel)}</span>'
            '</td><td style="padding:9px 0;text-align:right;border-bottom:1px solid #edf0f4;vertical-align:top;white-space:nowrap;">'
            f'<b style="font-size:12px;color:#40516e;">{status_label}</b><br><span style="font-size:12px;color:#172844;">{html.escape(display_limit(item))}</span>'
            '</td></tr>'
        )
    change_text = "；".join(f"{change.name}：{change_label(change)}" for change in changes[:6])
    change_block = f'<div style="margin:12px 0;padding:9px 10px;background:#f4f7ff;color:#425d9d;font-size:12px;line-height:18px;border-radius:8px;">变化：{html.escape(change_text)}</div>' if change_text else ""
    summary = f'开放 {counts["open"]} · 限额 {counts["limited"]} · 暂停 {counts["suspended"]} · 待核验 {counts["unknown"]}'
    return f'''<div style="max-width:680px;margin:0 auto;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;color:#1b2942;">
  <div style="padding:18px;background:#182a51;color:white;border-radius:12px 12px 0 0;">
    <div style="font-size:18px;font-weight:800;">纳指100 场外基金代销额度雷达</div>
    <div style="margin-top:6px;font-size:12px;color:#c8d5f8;">{now.strftime("%Y年%m月%d日 %H:%M")}（北京时间）</div>
  </div>
  <div style="padding:14px;border:1px solid #e3e8f0;border-top:0;border-radius:0 0 12px 12px;">
    <div style="padding:9px 10px;background:#f5f7fa;border-radius:8px;font-size:12px;color:#516078;">{summary}</div>
    {change_block}
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:8px;border-collapse:collapse;">{''.join(rows)}</table>
    <div style="margin-top:10px;color:#8a95a5;font-size:10px;line-height:16px;">仅显示代销渠道（含支付宝）口径，直销专属公告已排除。支付宝实际可买状态以下单页为准。本消息不构成投资建议。</div>
  </div>
</div>'''


def build_message_html(
    states: list[FundState], changes: list[Change], now: datetime, *, is_digest: bool, initial: bool
) -> tuple[str, str]:
    grouped = group_for_display(states)
    counts = {key: sum(item.status == key for item in states) for key in STATUS_META}
    title_prefix = "首次建档" if initial else "每日汇总" if is_digest else "额度变动"
    title = f"纳指100代销额度｜{title_prefix}"
    date_label = now.strftime("%Y年%m月%d日 %H:%M")
    change_block = ""
    if changes:
        rows = "".join(
            (
                '<tr><td style="padding:10px 0;border-bottom:1px solid #eef1f5;vertical-align:top;">'
                f'<div style="font-weight:700;color:#18243a;font-size:14px;">{html.escape(change.name)}</div>'
                f'<div style="margin-top:3px;color:#76839a;font-size:12px;">{html.escape(change.code)} · {html.escape(change_label(change))}</div>'
                "</td></tr>"
            )
            for change in changes[:12]
        )
        more = "" if len(changes) <= 12 else f'<div style="color:#6c7890;font-size:12px;margin-top:8px;">另有 {len(changes) - 12} 项变动，详见下方完整清单</div>'
        change_block = (
            '<div style="margin:18px 0 14px;padding:15px 16px;border:1px solid #dce6fb;background:#f7f9ff;border-radius:12px;">'
            '<div style="font-size:13px;font-weight:800;color:#334f93;letter-spacing:.2px;">本次变化</div>'
            f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:4px;border-collapse:collapse;">{rows}</table>{more}</div>'
        )
    table_rows: list[str] = []
    for display_name, members in grouped:
        item = members[0]
        codes = "、".join(member.code for member in members)
        shares = f"{len(members)} 份额" if len(members) > 1 else "1 份额"
        notes = sorted({member.note for member in members if member.note})
        note = f'<div style="margin-top:3px;color:#9a6b17;font-size:11px;line-height:16px;">{html.escape("；".join(notes))}</div>' if notes else ""
        table_rows.append(
            '<tr>'
            '<td style="padding:12px 8px 12px 0;border-bottom:1px solid #eef1f5;vertical-align:top;">'
            f'<div style="font-weight:700;color:#18243a;font-size:13px;line-height:18px;">{html.escape(display_name)}</div>'
            f'<div style="margin-top:3px;color:#8390a5;font-size:11px;">{html.escape(shares)} · {html.escape(codes)} · {html.escape(item.channel)}</div>{note}'
            '</td>'
            '<td style="padding:12px 5px;border-bottom:1px solid #eef1f5;vertical-align:top;text-align:center;white-space:nowrap;">'
            f'{status_badge(item.status)}'
            '</td>'
            '<td style="padding:12px 0 12px 5px;border-bottom:1px solid #eef1f5;vertical-align:top;text-align:right;white-space:nowrap;">'
            f'<div style="font-size:13px;font-weight:800;color:#1f2d46;">{html.escape(display_limit(item))}</div>'
            '</td>'
            '</tr>'
        )
    summary = " · ".join(
        (
            f'<span style="color:#0d8a61;font-weight:800;">开放 {counts["open"]}</span>',
            f'<span style="color:#b86b06;font-weight:800;">限额 {counts["limited"]}</span>',
            f'<span style="color:#c53c4c;font-weight:800;">暂停 {counts["suspended"]}</span>',
            f'<span style="color:#67758c;font-weight:800;">待核验 {counts["unknown"]}</span>',
        )
    )
    message = f'''<div style="max-width:680px;margin:0 auto;background:#ffffff;color:#18243a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;">
  <div style="padding:24px 22px 22px;background:linear-gradient(135deg,#111b34 0%,#263e79 100%);border-radius:16px 16px 0 0;color:#ffffff;">
    <div style="font-size:12px;letter-spacing:1.2px;opacity:.74;">NASDAQ-100 · DISTRIBUTOR QUOTA RADAR</div>
    <div style="margin-top:8px;font-size:22px;font-weight:800;letter-spacing:.2px;">纳指100 场外基金代销额度雷达</div>
    <div style="margin-top:9px;font-size:12px;color:#cbd7fb;">{date_label}（北京时间）</div>
  </div>
  <div style="padding:18px 18px 22px;border:1px solid #e7ebf2;border-top:0;border-radius:0 0 16px 16px;">
    <div style="padding:12px 14px;border-radius:10px;background:#f6f8fc;color:#506078;font-size:13px;line-height:21px;">{summary}</div>
    {change_block}
    <div style="margin:16px 0 8px;font-size:13px;font-weight:800;color:#27344d;">完整清单</div>
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;table-layout:fixed;">
      <thead><tr>
        <th style="padding:8px 8px 8px 0;text-align:left;color:#8a96a9;font-size:11px;font-weight:700;">基金 / 代销口径</th>
        <th style="padding:8px 5px;text-align:center;color:#8a96a9;font-size:11px;font-weight:700;white-space:nowrap;">状态</th>
        <th style="padding:8px 0 8px 5px;text-align:right;color:#8a96a9;font-size:11px;font-weight:700;white-space:nowrap;">代销日上限</th>
      </tr></thead>
      <tbody>{''.join(table_rows)}</tbody>
    </table>
    <div style="margin-top:15px;padding-top:12px;border-top:1px solid #eef1f5;color:#8793a7;font-size:11px;line-height:18px;">
      同一基金中状态、额度和代销口径一致的不同份额已合并展示。只采用基金公告中适用于代销/销售机构的条件；直销专属公告不会覆盖本表。支付宝实际可买状态可能受渠道库存影响，请以下单页为准。<br>
      本消息仅作公开信息监控，不构成任何投资建议。
    </div>
  </div>
</div>'''
    # PushPlus 微信渠道的实名账号内容上限为 2 万字；保留余量应对新增基金与更长的基金名称。
    if len(message) > 18_500:
        message = build_compact_message_html(states, changes, now)
    return title, message


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
    except (HTTPError, URLError, TimeoutError) as exc:
        raise DataSourceError(f"PushPlus 推送失败：{exc}") from exc
    data = parse_json_or_jsonp(raw)
    if not isinstance(data, dict) or data.get("code") not in (200, "200"):
        message = data.get("msg") if isinstance(data, dict) else raw[:200]
        raise DataSourceError(f"PushPlus 返回失败：{message}")


def resolve_current_state(
    funds: list[Fund], settings: dict[str, Any], previous: list[FundState]
) -> tuple[list[FundState], list[str]]:
    previous_by_code = {item.code: item for item in previous}
    results: list[FundState] = []
    errors: list[str] = []
    workers = max(1, int(settings["safety"].get("max_parallel_requests", 4)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="announcement") as executor:
        pending = {executor.submit(fetch_fund_state, fund, settings): fund for fund in funds}
        for index, future in enumerate(as_completed(pending), start=1):
            fund = pending[future]
            try:
                results.append(future.result())
            except Exception as exc:  # 单只基金的网络/解析故障不应中断整批监控。
                errors.append(f"{fund.code} {fund.name}: {type(exc).__name__}: {exc}")
                if fund.code in previous_by_code:
                    results.append(previous_by_code[fund.code])
                else:
                    results.append(unavailable_fund_state(fund, "本次公告读取失败，已安排下次自动复查"))
            logging.info("已检查 %s/%s：%s", index, len(funds), fund.code)
    return sorted(results, key=lambda item: item.code), errors


def run(*, dry_run: bool, force_digest: bool) -> int:
    settings = load_json(CONFIG_PATH, None)
    if not isinstance(settings, dict):
        raise RuntimeError("缺少 config/settings.json")
    stored = load_json(STATE_PATH, {"version": 1, "funds": [], "last_digest_date": None})
    previous = [deserialise_state(item) for item in stored.get("funds", [])]
    now = datetime.now(BEIJING)
    funds = discover_funds(settings)
    current, errors = resolve_current_state(funds, settings, previous)
    max_failures = max(1, int(len(funds) * float(settings["safety"]["max_failure_ratio"])))
    if len(errors) > max_failures:
        raise DataSourceError(f"本次有 {len(errors)}/{len(funds)} 只基金读取失败，超过安全阈值 {max_failures}；没有覆盖历史状态")
    if not current:
        raise DataSourceError("没有获得任何基金状态；没有推送")

    initial = not previous
    changes = compare_snapshots(previous, current) if previous else [Change(item.code, item.name, "new", None, item) for item in current]
    is_digest = force_digest or stored.get("last_digest_date") != now.date().isoformat()
    should_push = initial or is_digest or bool(changes)
    # 首次建档时完整清单本身就是信息主体，不再额外堆叠几十条“新增监控”。
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
        logging.info("没有额度变动，且今日已发送日报；不重复推送")

    write_json(
        STATE_PATH,
        {
            "version": 1,
            "last_digest_date": now.date().isoformat() if is_digest else stored.get("last_digest_date"),
            "funds": [serialise_state(item) for item in current],
        },
    )
    if errors:
        logging.warning("本次有 %s 项读取异常，已保留可用的历史状态：\n%s", len(errors), "\n".join(errors))
    return 0


def render_saved_preview() -> int:
    """在不联网、不推送的前提下，重建最近一次消息卡片，便于校对排版。"""
    stored = load_json(STATE_PATH, {"funds": []})
    states = [deserialise_state(item) for item in stored.get("funds", [])]
    if not states:
        raise RuntimeError("没有可预览的历史状态，请先成功运行一次采集")
    _title, message = build_message_html(states, [], datetime.now(BEIJING), is_digest=True, initial=False)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(message, encoding="utf-8")
    logging.info("已生成离线预览：%s（%s 个字符）", REPORT_PATH, len(message))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="纳斯达克100场外QDII基金额度监控")
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

