import sys
import unittest
from http.client import RemoteDisconnected
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import main  # noqa: E402


class QuotaParserTests(unittest.TestCase):
    def test_network_disconnect_becomes_a_retryable_data_error(self):
        with patch.object(main, "urlopen", side_effect=RemoteDisconnected("source closed connection")):
            with self.assertRaises(main.DataSourceError):
                main.request_text("https://example.test/notice", retries=1)

    def test_limited_announcement_extracts_limit(self):
        content = """
        <p>自2026年8月20日起，投资人通过各代销机构申购本基金，
        单日单个基金账户单笔或累计申购及定期定额投资金额不应超过1,000元。</p>
        """
        status, limit, channel, note = main.classify_state("调整大额申购及定期定额投资业务", content, "019441")
        self.assertEqual(status, "limited")
        self.assertEqual(limit, "¥1,000")
        self.assertEqual(channel, "代销渠道（支付宝通常适用）")
        self.assertIsNone(note)

    def test_suspension_is_not_mistaken_for_a_limit(self):
        status, limit, _channel, _note = main.classify_state(
            "暂停申购及定期定额投资业务的公告", "自即日起暂停办理申购及定期定额投资业务。", "015299"
        )
        self.assertEqual(status, "suspended")
        self.assertIsNone(limit)

    def test_holiday_notice_is_not_a_quota_event(self):
        self.assertFalse(main.is_relevant_announcement("境外主要投资场所节假日暂停申购及定期定额申购业务的公告"))


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


class PresentationTests(unittest.TestCase):
    def test_html_escapes_fund_name_and_shows_change(self):
        before = main.FundState("000834", "旧基金", "limited", "¥100", "代销渠道（支付宝通常适用）", "AN1", "公告", "2026-08-01", None, "公告标题+正文")
        after = main.FundState("000834", "测试<script>", "limited", "¥1,000", "代销渠道（支付宝通常适用）", "AN2", "公告", "2026-08-02", None, "公告标题+正文")
        change = main.Change("000834", after.name, "limit", before, after)
        _title, body = main.build_message_html(
            [after], [change], datetime(2026, 8, 29, 8, 37, tzinfo=main.BEIJING), is_digest=True, initial=False
        )
        self.assertIn("¥100 → ¥1,000", body)
        self.assertIn("测试&lt;script&gt;", body)
        self.assertNotIn("测试<script>", body)


if __name__ == "__main__":
    unittest.main()

