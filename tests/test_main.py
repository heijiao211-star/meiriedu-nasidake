import sys
import unittest
from datetime import datetime
from http.client import RemoteDisconnected
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import main  # noqa: E402


class NetworkTests(unittest.TestCase):
    def test_network_disconnect_becomes_a_retryable_data_error(self):
        with patch.object(main, "urlopen", side_effect=RemoteDisconnected("source closed connection")):
            with self.assertRaises(main.DataSourceError):
                main.request_text("https://example.test/page", retries=1)


class TiantianParserTests(unittest.TestCase):
    def test_paused_page_wins_over_a_displayed_historical_limit(self):
        page = """
        <table><tr><td>申购状态</td><td>暂停申购</td><td>定投状态</td><td>不支持</td></tr></table>
        <table><tr><td>日累计申购限额</td><td>100.00元</td></tr></table>
        """
        status, limit, dca_status, note = main.parse_tiantian_page(page)
        self.assertEqual(status, "suspended")
        self.assertEqual(limit, "¥100")
        self.assertEqual(dca_status, "不可定投")
        self.assertIn("当前已暂停申购", note)
        state = main.FundState("015299", "华夏测试A", status, limit, dca_status, "天天基金", None, "", "公开交易页", note)
        self.assertEqual(main.display_limit(state), "—")

    def test_limited_page_extracts_channel_quota_and_dca_status(self):
        page = """
        <table><tr><td class="th">申购状态</td><td class="w135">限大额</td>
        <td class="th">定投状态</td><td class="w135">支持</td></tr>
        <tr><td>日累计申购限额</td><td>10.00元</td></tr></table>
        """
        status, limit, dca_status, note = main.parse_tiantian_page(page)
        self.assertEqual((status, limit, dca_status, note), ("limited", "¥10", "可定投", None))

    def test_open_page_without_cap_is_unlimited(self):
        page = """
        <table><tr><td>申购状态</td><td>开放申购</td><td>定投状态</td><td>支持</td></tr>
        <tr><td>日累计申购限额</td><td>无限额</td></tr></table>
        """
        status, limit, dca_status, _note = main.parse_tiantian_page(page)
        state = main.FundState("000001", "测试基金", status, limit, dca_status, "天天基金", None, "", "公开交易页")
        self.assertEqual(status, "open")
        self.assertEqual(main.display_limit(state), "无限额")


class AnnouncementReferenceTests(unittest.TestCase):
    def test_direct_only_notice_is_not_used_as_channel_cross_check(self):
        self.assertTrue(main.is_direct_only_reference("关于在基金管理人直销电子交易平台暂停申购业务的公告"))
        self.assertFalse(main.is_direct_only_reference("关于暂停各销售机构申购业务的公告"))

    def test_full_pause_and_large_purchase_limit_are_distinguished(self):
        self.assertEqual(main.status_from_reference_title("关于暂停申购及定期定额投资业务的公告"), "suspended")
        self.assertEqual(main.status_from_reference_title("关于调整大额申购及定期定额投资业务的公告"), "limited")


class DiscoveryTests(unittest.TestCase):
    def test_discovery_keeps_off_exchange_qdii_and_excludes_etf(self):
        settings = {
            "sources": {"fund_code_table": {"url": "https://example.test/funds", "referer": "https://example.test/"}},
            "universe": {"name_pattern": "纳斯达克100", "exclude_codes": [], "pinned_funds": []},
            "safety": {"min_fund_count": 1},
        }
        source = '''var r = [
          ["000834","dc","大成纳斯达克100ETF联接(QDII)A","指数型-海外股票","dc"],
          ["513300","hx","华夏纳斯达克100ETF(QDII)","指数型-海外股票","hx"],
          ["000001","x","普通基金","混合型-偏股","x"]
        ];'''
        with patch.object(main, "request_text", return_value=source):
            funds = main.discover_funds(settings)
        self.assertEqual([(item.code, item.name) for item in funds], [("000834", "大成纳斯达克100ETF联接(QDII)A")])


class SnapshotAndPresentationTests(unittest.TestCase):
    def test_status_and_dca_changes_are_detected(self):
        before = main.FundState("000834", "测试基金", "limited", "¥10", "可定投", "天天基金", None, "", "公开交易页")
        after = main.FundState("000834", "测试基金", "suspended", "¥10", "不可定投", "天天基金", None, "", "公开交易页")
        change = main.compare_snapshots([before], [after])[0]
        self.assertEqual(change.kind, "status")
        self.assertEqual(main.change_label(change), "限额申购 → 暂停申购")

    def test_message_names_only_the_verified_channel(self):
        state = main.FundState("000834", "测试<script>", "limited", "¥10", "可定投", "天天基金", None, "", "公开交易页")
        title, body = main.build_message_html(
            [state], [], datetime(2026, 8, 29, 8, 37, tzinfo=main.BEIJING), is_digest=True, initial=False
        )
        self.assertEqual(title, "纳指100公开渠道｜每日汇总")
        self.assertIn("天天基金", body)
        self.assertIn("支付宝、理财通及其他未接入渠道不在本卡中推断", body)
        self.assertIn("测试&lt;script&gt;", body)
        self.assertNotIn("测试<script>", body)

    def test_reference_conflict_is_explicit_without_overriding_channel_state(self):
        state = main.FundState(
            "015299", "华夏测试", "suspended", "¥100", "不可定投", "天天基金", None, "", "公开交易页",
            reference_status="limited", reference_title="旧的限额公告"
        )
        self.assertTrue(main.has_reference_conflict(state))
        self.assertEqual(main.reference_label(state), "公告有差异")
        self.assertEqual(main.display_limit(state), "—")


if __name__ == "__main__":
    unittest.main()
