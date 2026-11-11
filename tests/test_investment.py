import json
import sqlite3
import subprocess
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from guard.core import Engine, Incoming, digest
from guard.history import build_investment_context
from guard.investment import (
    ENTRYPOINT_QUERY_TERMS,
    MARKETS,
    MAX_MODEL_CALLS,
    MAX_PUBLIC_PAGES,
    MAX_PUBLIC_REQUESTS,
    MAX_SECONDS,
    ResearchBudget,
    _allowed_hosts,
    _company_action_target,
    _market_hint,
    _model_targets,
    _queries,
    _rank_sources_for_targets,
    _resolve_target_markets,
    _source_entity_similarity,
    _targets,
    _valid_synthesis,
    entrypoint_for,
    extract_daily_bars,
    investment_capability_question,
    references_other_personal_data,
    related,
    run_isolated,
    run_research,
    technical_summary,
    verify_claims,
    verify_facts,
)
from guard.investment_catalogue import ENTRYPOINTS
from guard.model import ModelFailure, payload
from guard.public_web import PublicHTTP, PublicResearch, _extract_pdf_text

ROOT = Path(__file__).resolve().parents[1]


def _minimal_pdf(pages):
    def escape(value):
        return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    page_ids = [3 + index * 2 for index in range(len(pages))]
    font_id = 3 + len(pages) * 2
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + b" ".join(f"{value} 0 R".encode() for value in page_ids)
        + f"] /Count {len(pages)} >>".encode(),
    ]
    for page_id, text in zip(page_ids, pages):
        content_id = page_id + 1
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>".encode()
        )
        stream = f"BT /F1 12 Tf 36 760 Td ({escape(text)}) Tj ET".encode()
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    result = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, value in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{index} 0 obj\n".encode() + value + b"\nendobj\n")
    xref = len(result)
    result.extend(f"xref\n0 {len(offsets)}\n".encode())
    result.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(result)


def _public_pdf_fetcher(pdf):
    class Response:
        status = 200

        def __init__(self, body):
            self.body = body

        def getheader(self, name, default=""):
            return "application/pdf" if name.lower() == "content-type" else default

        def read1(self, size):
            value, self.body = self.body[:size], self.body[size:]
            return value

    class Connection:
        sock = None

        def request(self, *args, **kwargs):
            return None

        def getresponse(self):
            return Response(pdf)

        def close(self):
            return None

    return PublicHTTP(
        resolver=lambda *args: [(None, None, None, None, ("93.184.216.34", 443))],
        connection=lambda *args: Connection(),
    )


class InvestmentRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = time.time()
        self.policy = json.loads((ROOT / "config/policy.example.json").read_text())
        self.policy.update(enabled=True, general_enabled=True, group_id="group@chatroom", bot_instance_id="bot")
        snapshot = self.root / "snapshot.json"
        snapshot.write_text(json.dumps({"captured_at": self.now, "service": "healthy", "pressure": "normal"}))
        self.engine = Engine(self.policy, json.loads((ROOT / "knowledge/faq.json").read_text()),
            self.root / "state.sqlite", snapshot, lambda: self.now)
        self.sequence = 0

    def tearDown(self):
        self.engine.db.close()
        self.tmp.cleanup()

    def event(self, text, *, mentioned=True, group="group@chatroom", sender="member"):
        self.sequence += 1
        return Incoming(group, sender, str(self.sequence), text, mentioned, self.now)

    def test_catalog_has_all_fixed_entrypoints_and_specialized_routes(self):
        self.assertEqual(len(ENTRYPOINTS), 22)
        self.assertEqual(len({item["id"] for item in ENTRYPOINTS}), 22)
        self.assertEqual({item["id"] for item in ENTRYPOINTS}, set(ENTRYPOINT_QUERY_TERMS))
        self.assertTrue(related("分析美股 NVDA 的估值"))
        self.assertTrue(related("AAPL今天涨多少"))
        for item in ENTRYPOINTS:
            with self.subTest(entrypoint=item["id"]):
                self.assertTrue(related("/" + item["id"] + " NVDA"))
                self.assertEqual(entrypoint_for("/" + item["id"] + " NVDA"), item["id"])
        self.assertEqual(entrypoint_for("请精读这份2025年报"), "earnings-review")
        self.assertEqual(entrypoint_for("请比较这两份报告的逻辑漂移"), "thesis-drift")
        self.assertIn("latest company news", " ".join(_queries([{"target": "NVDA", "market": "US"}], "news-pulse")))
        self.assertIn("dividend history", " ".join(_queries([{"target": "0700.HK", "market": "HK"}], "income-investment")))

    def test_market_adapters_use_fixed_source_domains_and_currency_metadata(self):
        cases = (
            ("分析A股600519.SH估值", "A", "CNY", "cninfo.com.cn"),
            ("分析港股0700.HK估值", "HK", "HKD", "hkexnews.hk"),
            ("分析美股NVDA估值", "US", "USD", "sec.gov"),
            ("分析台股2330.TW估值", "TW", "TWD", "mops.twse.com.tw"),
        )
        for question, market, currency, domain in cases:
            with self.subTest(market=market):
                self.assertEqual(_market_hint(question), market)
                self.assertEqual(MARKETS[market]["currency"], currency)
                query = _queries([{"target": "TEST", "market": market}], "investment-research")
                self.assertTrue(any(domain in value for value in query))
                self.assertIn(domain, _allowed_hosts([{"target": "TEST", "market": market}]))

    def test_company_name_without_code_routes_and_is_extracted_from_natural_question(self):
        question = "真爱美家最近表现如何"
        self.assertTrue(related(question))
        self.assertEqual(_company_action_target(question), "真爱美家")
        self.assertEqual(_targets({}, question), [{"target": "真爱美家", "market": ""}])
        self.assertEqual(_targets({}, "分析美股NVDA估值"), [{"target": "NVDA", "market": "US"}])
        decision = self.engine.prepare(self.event(question))
        self.assertEqual(decision.intent, "investment")
        self.assertEqual(decision.model_input["question"], question)

    def test_missing_market_is_omitted_and_search_covers_supported_markets(self):
        question = "真爱美家还能加仓吗"
        targets = _targets({"targets": [{"company": "真爱美家", "market": "UNKNOWN"}]}, question)
        self.assertEqual(targets, [{"target": "真爱美家", "market": ""}])
        self.assertEqual(_model_targets(targets), [{"target": "真爱美家"}])
        query_text = " ".join(_queries(targets, "investment-research"))
        self.assertIn("真爱美家", query_text)
        self.assertNotIn("UNKNOWN", query_text)
        focused_query = " ".join(_queries(targets, "investment-research", "真爱美家股价走势，我持仓100股"))
        self.assertIn("股价", focused_query)
        self.assertIn("走势", focused_query)
        self.assertNotIn("100股", focused_query)
        self.assertNotIn("持仓", focused_query)
        allowed = _allowed_hosts(targets)
        for market in ("US", "HK", "A", "TW"):
            self.assertTrue(set(MARKETS[market]["hosts"]).issubset(allowed))

    def test_matching_company_sources_resolve_market_and_ambiguous_sources_do_not(self):
        targets = [{"target": "真爱美家", "market": "", "market_explicit": False}]
        mainland_source = {"title": "浙江真爱美家股份有限公司公告", "text": "真爱美家公告", "host": "www.cninfo.com.cn"}
        unrelated_source = {"title": "真爱科技股份有限公司", "text": "真爱科技年度业绩", "host": "www.cninfo.com.cn"}
        self.assertGreater(_source_entity_similarity("真爱美家", mainland_source),
            _source_entity_similarity("真爱美家", unrelated_source))
        self.assertEqual(_resolve_target_markets(targets, [mainland_source])[0]["market"], "A")
        ambiguous = [mainland_source, {**mainland_source, "host": "www.hkexnews.hk"}]
        self.assertEqual(_resolve_target_markets(targets, ambiguous)[0]["market"], "")

    def test_source_ranking_prefers_the_requested_company_topic(self):
        annual_report = {"title": "真爱美家年度报告", "text": "真爱美家主营业务及年度经营情况。",
            "url": "https://www.cninfo.com.cn/annual"}
        share_price = {"title": "真爱美家股价行情", "text": "真爱美家股价近期走势和行情。",
            "url": "https://www.cninfo.com.cn/price"}
        ranked = _rank_sources_for_targets([annual_report, share_price],
            [{"target": "真爱美家", "market": ""}], question="真爱美家股价近期怎么样")
        self.assertEqual(ranked[0], share_price)

    def test_company_research_prioritizes_matching_sources_without_unknown_market(self):
        calls = []
        query_log = []
        allowed_hosts = set()
        now = time.time()

        class Research:
            def collect_ephemeral(self, queries, urls, **kwargs):
                nonlocal allowed_hosts
                query_log.extend(queries)
                allowed_hosts = kwargs["allowed_hosts"]
                return {"state": "ok", "public_requests": 3, "public_pages": 2,
                    "as_of_bjt": "2026-11-11T15:00:00+08:00", "missing": [],
                    "sources": [
                        {"url": "https://www.cninfo.com.cn/other", "final_url": "https://www.cninfo.com.cn/other",
                         "title": "其他公司年度报告", "published_at": "2025-02-01", "fetched_at": now,
                         "kind": "text/plain", "text": "其他公司营业收入与现金流。"},
                        {"url": "https://www.cninfo.com.cn/target", "final_url": "https://www.cninfo.com.cn/target",
                         "title": "真爱美家年度报告", "published_at": "2025-02-02", "fetched_at": now,
                         "kind": "text/plain", "text": "真爱美家主营业务及年度经营情况。"},
                    ]}

        def model(data, key_file, api_base, timeout=None):
            calls.append(data)
            json.dumps(data, ensure_ascii=False)
            if data["phase"] == "investment_plan":
                return {"targets": []}, {"input_tokens": 1}
            if data["phase"] == "investment_extract":
                return {"facts": [], "claims": []}, {"input_tokens": 1}
            if data["phase"] == "investment_analysis":
                return {"scope": "service", "analysis": "经营结论需结合收入质量与风险。"}, {"input_tokens": 1}
            return {"scope": "service", "answer": "结论：优先核实经营质量，并关注估值与下行风险。"}, {"input_tokens": 1}

        result, _, _ = run_research({"question": "真爱美家最近表现如何"},
            None, "https://api.apiroute.asia/v1", model_call=model, research=Research())
        self.assertEqual(len(calls), 6)
        self.assertIn("真爱美家", " ".join(query_log))
        self.assertIn("表现", " ".join(query_log))
        self.assertIn("cninfo.com.cn", allowed_hosts)
        self.assertNotIn("UNKNOWN", json.dumps(calls, ensure_ascii=False))
        self.assertEqual(calls[1]["targets"], [{"target": "真爱美家", "market": "A"}])
        self.assertEqual(calls[1]["sources"][0]["title"], "真爱美家年度报告")
        self.assertNotIn("UNKNOWN", result["answer"])
        self.assertIn("真爱美家：A / CNY", result["answer"])

    def test_stock_question_routes_only_after_existing_group_and_mention_gates(self):
        decision = self.engine.prepare(self.event("分析美股 NVDA 的合理估值"))
        self.assertEqual(decision.intent, "investment")
        self.assertEqual(decision.model_input["domain"], "investment")
        self.assertEqual(decision.model_input["question"], "分析美股 NVDA 的合理估值")
        self.assertNotIn("group_context", decision.model_input)
        self.assertIsNone(self.engine.prepare(self.event("分析美股 NVDA 的估值", mentioned=False)).model_input)
        self.assertIsNone(self.engine.prepare(self.event("分析美股 NVDA 的估值", group="other@chatroom")).model_input)

    def test_stock_capability_question_gets_local_answer_not_company_research(self):
        question = "股票相关的内容你可以回答哪些"
        decision = self.engine.prepare(self.event(question))
        self.assertTrue(investment_capability_question(question))
        self.assertEqual(decision.category, "investment_capability")
        self.assertEqual(decision.intent, "investment")
        self.assertIsNone(decision.model_input)
        self.assertIn("A股、港股、美股和台股", decision.reply)
        self.assertFalse(investment_capability_question("分析美股NVDA的估值"))
        self.assertFalse(investment_capability_question("真爱美家还能加仓吗"))

    def test_investment_failure_code_is_sanitized_and_retained_with_delivery_metadata(self):
        event = self.event("分析美股NVDA的估值")
        self.assertTrue(self.engine.claim(event))
        self.assertTrue(self.engine.record_model_failure(event, "investment", "http_503"))
        event_digest = digest(event.group, event.message_id)
        row = self.engine.db.execute(
            "SELECT id,state,failure_code FROM deliveries WHERE id=?",
            (event_digest,),
        ).fetchone()
        self.assertEqual(row, (event_digest, "pending", "http_503"))
        self.assertNotEqual(row[0], event.message_id)

        unsafe_event = self.event("分析港股0700.HK")
        self.engine.claim(unsafe_event)
        self.engine.record_model_failure(unsafe_event, "investment", "invalid code")
        self.assertEqual(self.engine.db.execute(
            "SELECT failure_code FROM deliveries WHERE id=?",
            (digest(unsafe_event.group, unsafe_event.message_id),),
        ).fetchone()[0], "other_error")

        self.now += 8 * 86400
        self.engine.purge()
        self.assertEqual(self.engine.db.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)

    def test_existing_delivery_table_gets_failure_code_column_additively(self):
        legacy_path = self.root / "legacy-state.sqlite"
        legacy_db = sqlite3.connect(legacy_path)
        legacy_db.execute("CREATE TABLE deliveries(id TEXT PRIMARY KEY,state TEXT NOT NULL,at REAL NOT NULL)")
        legacy_db.execute("INSERT INTO deliveries VALUES ('legacy-digest','replied',?)", (self.now,))
        legacy_db.commit()
        legacy_db.close()

        engine = Engine(self.policy, json.loads((ROOT / "knowledge/faq.json").read_text()),
            legacy_path, self.root / "legacy-snapshot.json", lambda: self.now)
        try:
            columns = {row[1] for row in engine.db.execute("PRAGMA table_info(deliveries)")}
            self.assertIn("failure_code", columns)
            self.assertEqual(engine.db.execute("SELECT state FROM deliveries WHERE id='legacy-digest'").fetchone()[0], "replied")
            event = self.event("分析美股NVDA")
            self.assertTrue(engine.claim(event))
            self.assertTrue(engine.record_model_failure(event, "investment", "transport"))
            self.assertEqual(engine.db.execute("SELECT failure_code FROM deliveries WHERE id=?",
                (digest(event.group, event.message_id),)).fetchone()[0], "transport")
        finally:
            engine.db.close()

    def test_other_members_personal_financial_data_is_rejected_locally(self):
        decision = self.engine.prepare(self.event("比较我的持仓和example_person_b的成本，分析美股NVDA"))
        self.assertEqual(decision.intent, "investment")
        self.assertEqual(decision.category, "investment_privacy")
        self.assertIsNone(decision.model_input)

    def test_long_report_sources_are_allowed_but_investment_profile_is_not_saved(self):
        event = self.event("研究美股 NVDA 的估值")
        decision = self.engine.prepare(event)
        source_url = "https://sec.gov/Archives/edgar/data/1/10-k.htm"
        decision.public_urls.add(source_url)
        report = "结论与依据。\n" + ("证据。" * 350) + "\n来源：" + source_url
        result = self.engine.interpret(decision, {"scope": "service", "answer": report,
            "refs": ["investment_data"], "learning": "不得保存持仓档案。"})
        self.assertEqual(result.category, "investment")
        self.assertIn(source_url, result.reply)
        self.engine.delivered(event, result, "replied")
        self.assertEqual(self.engine.contexts, {})
        self.assertEqual(self.engine.last_domains[decision.subject][1], "investment")
        self.assertEqual(self.engine.db.execute("SELECT count(*) FROM knowledge").fetchone()[0], 0)


class InvestmentPrivacyTests(unittest.TestCase):
    def test_history_is_filtered_by_raw_sender_before_anonymization(self):
        rows = (
            SimpleNamespace(id="m1", sender="member-a", text="我持有AAPL，成本100美元", truncated=False),
            SimpleNamespace(id="m2", sender="member-b", text="我持有MSFT，预算5000美元", truncated=False),
            SimpleNamespace(id="m3", sender="member-a", text="我持有NVDA；example_person_b持有TSLA", truncated=False),
            SimpleNamespace(id="m4", sender="member-a", text="周末去哪里吃饭", truncated=False),
        )
        context = build_investment_context(rows, "current", "member-a", excluded_aliases=["example_person_b"])
        self.assertEqual(len(context), 1)
        self.assertEqual(context[0]["speaker"], "提问者")
        self.assertIn("AAPL", context[0]["text"])
        self.assertNotIn("MSFT", json.dumps(context, ensure_ascii=False))
        self.assertNotIn("NVDA", json.dumps(context, ensure_ascii=False))
        self.assertNotIn("member-a", json.dumps(context, ensure_ascii=False))
        self.assertTrue(references_other_personal_data("比较我和朋友的预算", []))


class InvestmentEvidenceTests(unittest.TestCase):
    def test_public_fetch_is_ephemeral_bounded_and_market_host_filtered(self):
        searches = []
        fetched = []
        def search(queries, timeout):
            searches.append((queries, timeout))
            return [
                {"url": "https://sec.gov/Archives/edgar/data/1/filing.htm", "title": "Filing", "snippet": ""},
                {"url": "https://untrusted.example/filing", "title": "Untrusted", "snippet": ""},
            ]
        def fetch(url, timeout):
            fetched.append(url)
            return {"url": url, "kind": "text/plain", "body": "Public filing FY2025 revenue 100."}
        research = PublicResearch(cache=None, fetch=fetch, search=search, clock=lambda: 1794283200)
        proof = research.collect_ephemeral(["NVDA latest annual report", "NVDA SEC filing"],
            max_pages=9, max_requests=10, allowed_hosts={"sec.gov"})
        self.assertEqual(len(searches), 1)
        self.assertEqual(fetched, ["https://sec.gov/Archives/edgar/data/1/filing.htm"])
        self.assertEqual(proof["public_requests"], 2)
        self.assertEqual(proof["public_pages"], 1)
        self.assertFalse(proof["persistent_cache_used"])
        self.assertEqual(proof["state"], "ok")

    def test_pdf_extraction_enforces_page_character_and_deadline_limits(self):
        pdf = _minimal_pdf(["First page Revenue 100.", "Second page Net income 25."])
        page_limited = _extract_pdf_text(pdf, deadline=time.monotonic() + 5, max_pages=1)
        self.assertEqual(page_limited["pdf_page_count"], 2)
        self.assertEqual(page_limited["pdf_pages_extracted"], 1)
        self.assertTrue(page_limited["pdf_truncated"])
        self.assertNotIn("Second page", page_limited["text"])

        char_limited = _extract_pdf_text(_minimal_pdf(["Revenue 100 million USD FY2025.", "Revenue 200."]),
            deadline=time.monotonic() + 5, max_chars=12)
        self.assertEqual(len(char_limited["text"]), 12)
        self.assertTrue(char_limited["pdf_truncated"])
        with self.assertRaises(TimeoutError):
            _extract_pdf_text(pdf, deadline=time.monotonic() - 1)

    def test_public_fetch_worker_timeout_is_a_safe_timeout(self):
        with patch("guard.public_web.subprocess.run", side_effect=subprocess.TimeoutExpired(["python"], 3)), \
                self.assertRaisesRegex(TimeoutError, "public_worker_deadline"):
            PublicHTTP().get("https://example.com/report.pdf", timeout=3)

    def test_ephemeral_research_marks_partial_pdf_coverage_and_keeps_title(self):
        pages = ["NVDA FY2025 Revenue 100."] + ["Other report page."] * 40
        fetcher = _public_pdf_fetcher(_minimal_pdf(pages))
        research = PublicResearch(cache=None, fetch=fetcher.get,
            search=lambda queries, timeout: [{"url": "https://sec.gov/Archives/edgar/data/1/annual.pdf",
                                              "title": "NVDA FY2025 annual report"}])
        proof = research.collect_ephemeral(["NVDA FY2025 annual report"],
            max_pages=1, max_requests=2, allowed_hosts={"sec.gov"})
        self.assertEqual(proof["state"], "partial")
        source = proof["sources"][0]
        self.assertEqual(source["title"], "NVDA FY2025 annual report")
        self.assertEqual(source["pdf_page_count"], 41)
        self.assertEqual(source["pdf_pages_extracted"], 40)
        self.assertTrue(source["pdf_truncated"])

    def test_pdf_financial_facts_reach_the_research_answer(self):
        pdf = _minimal_pdf(["NVDA FY2025 " + "X" * 3200 + "Revenue 100 million USD. " + "Y" * 3500])
        fetcher = _public_pdf_fetcher(pdf)
        research = PublicResearch(
            cache=None,
            fetch=fetcher.get,
            search=lambda queries, timeout: [{
                "url": "https://sec.gov/Archives/edgar/data/1/annual-report.pdf",
                "title": "NVDA FY2025 annual report",
            }],
        )
        calls = []

        def model(data, *args, **kwargs):
            calls.append(data)
            phase = data["phase"]
            if phase == "investment_plan":
                return {"targets": [{"company": "NVIDIA", "ticker": "NVDA", "market": "US"}]}, {}
            if phase == "investment_extract":
                return {"facts": [{"name": "Revenue", "value": "100", "unit": "million",
                    "currency": "USD", "period": "FY2025", "source_ids": ["S1"]}], "claims": []}, {}
            if phase == "investment_analysis":
                return {"scope": "service", "analysis": "FY2025 revenue was 100 million USD [S1]."}, {}
            return {"scope": "service", "answer": "结论：FY2025 revenue was 100 million USD [S1]."}, {}

        result, _, _ = run_research({"question": "分析美股NVDA的年度表现"}, None,
            "https://api.apiroute.asia/v1", model_call=model, research=research)
        self.assertEqual(len(calls), MAX_MODEL_CALLS)
        self.assertIn("FY2025 revenue was 100 million USD", result["answer"])
        self.assertIn("| Revenue | 100 USD million | FY2025 | 单一来源 | [S1] sec.gov |", result["answer"])
        self.assertIn("NVDA FY2025 annual report", result["answer"])
        self.assertIn("资料状态：部分覆盖", result["answer"])
        self.assertIn("PDF正文解析1/1页（受限截断）", result["answer"])

    def test_ephemeral_fetch_never_exceeds_ten_total_public_requests(self):
        fetched = []
        def search(queries, timeout):
            return [{"url": f"https://sec.gov/Archives/edgar/data/1/{index}.htm", "title": str(index), "snippet": ""}
                    for index in range(12)]
        def fetch(url, timeout):
            fetched.append(url)
            return {"url": url, "kind": "text/plain", "body": "Public filing text."}
        proof = PublicResearch(cache=None, fetch=fetch, search=search).collect_ephemeral(
            ["NVDA latest annual report"], max_pages=9, max_requests=10, allowed_hosts={"sec.gov"})
        self.assertEqual(proof["public_requests"], 10)
        self.assertEqual(proof["public_pages"], 9)
        self.assertEqual(len(fetched), 9)

    def test_key_facts_require_literal_value_period_and_independent_sources(self):
        sources = [
            {"id": "S1", "host": "sec.gov", "text": "Revenue was 100 billion USD for FY2025."},
            {"id": "S2", "host": "stockanalysis.com", "text": "Revenue 100 billion USD, FY2025."},
        ]
        facts = verify_facts([{"name": "Revenue", "value": "100", "unit": "billion", "currency": "USD",
            "period": "FY2025", "source_ids": ["S1", "S2"]},
            {"name": "Net income", "value": "999", "unit": "million", "currency": "USD",
             "period": "FY2025", "source_ids": ["S1"]}], sources)
        self.assertEqual(len(facts), 2)
        self.assertTrue(all("双源一致" in item["cross_check"] for item in facts))
        self.assertIsInstance(json.dumps(facts, ensure_ascii=False), str)
        missing_period = verify_facts([{"name": "Revenue", "value": "100", "unit": "billion", "currency": "USD",
            "period": "FY2024", "source_ids": ["S1"]}], sources)
        self.assertEqual(missing_period, [])
        wrong_currency = verify_facts([{"name": "Revenue", "value": "100", "unit": "billion", "currency": "CNY",
            "period": "FY2025", "source_ids": ["S1"]}], sources)
        wrong_unit = verify_facts([{"name": "Revenue", "value": "100", "unit": "million", "currency": "USD",
            "period": "FY2025", "source_ids": ["S1"]}], sources)
        wrong_metric = verify_facts([{"name": "Net income", "value": "100", "unit": "billion", "currency": "USD",
            "period": "FY2025", "source_ids": ["S1"]}], sources)
        self.assertEqual(wrong_currency, [])
        self.assertEqual(wrong_unit, [])
        self.assertEqual(wrong_metric, [])

    def test_synthesis_rejects_unverified_numbers_and_unknown_source_ids(self):
        sources = [{"id": "S1", "text": "Revenue 100 billion USD FY2025."}]
        targets = [{"target": "NVDA", "market": "US"}]
        facts = [{"value": "100"}]
        self.assertTrue(_valid_synthesis("Revenue was 100 billion [S1] in FY2025.", sources, targets, facts))
        self.assertFalse(_valid_synthesis("Revenue was 999 billion [S1].", sources, targets, facts))
        self.assertFalse(_valid_synthesis("Revenue was 100 billion [S99].", sources, targets, facts))
        self.assertTrue(_valid_synthesis("My position has 50 shares.", sources, targets, facts, {50}))

    def test_qualitative_claims_need_a_verbatim_source_quote(self):
        sources = [{"id": "S1", "text": "The company sells GPU computing products."}]
        claims = verify_claims([
            {"claim": "The company sells GPU products.", "source_id": "S1",
             "evidence_quote": "The company sells GPU computing products."},
            {"claim": "The company dominates the market.", "source_id": "S1",
             "evidence_quote": "It is the undisputed market leader in all segments."},
            {"claim": "Unknown source.", "source_id": "S9",
             "evidence_quote": "The company sells GPU computing products."},
        ], sources)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["source_id"], "S1")

    def test_daily_series_requires_structured_valid_ohlc_and_backtest_is_not_claimed(self):
        rows = []
        for index in range(50):
            close = 100 + index
            rows.append({"date": (date(2025, 1, 1) + timedelta(days=index)).isoformat(),
                "open": close, "high": close + 1, "low": close - 1, "close": close})
        source = {"id": "S1", "kind": "application/json", "text": json.dumps({"data": rows})}
        bars, source_id = extract_daily_bars([source], "2026-12-31")
        summary = technical_summary(bars, source_id, True, "2025-03-01")
        self.assertEqual(len(bars), 50)
        self.assertEqual(summary["state"], "validated_single_source")
        self.assertEqual(summary["sma20"], "139.50")
        self.assertEqual(summary["sma50"], "124.50")
        self.assertEqual(summary["return20_pct"], "15.50")
        self.assertIn("unsupported", summary["backtest"])
        stale = technical_summary(bars, source_id, True, "2026-11-11")
        self.assertEqual(stale["state"], "unsupported")
        prose = {"id": "S2", "kind": "text/html", "text": "Close values 1 2 3 4"}
        self.assertEqual(extract_daily_bars([prose])[0], [])

    def test_budget_and_prompt_keep_tools_closed(self):
        budget = ResearchBudget()
        for _ in range(MAX_MODEL_CALLS):
            budget.reserve_model()
        with self.assertRaises(ModelFailure):
            budget.reserve_model()
        self.assertEqual((MAX_PUBLIC_REQUESTS, MAX_PUBLIC_PAGES, MAX_SECONDS), (10, 9, 90))
        request = payload({"domain": "investment", "phase": "investment_plan"})
        self.assertEqual(request["tools"], [])
        self.assertFalse(request["store"])
        self.assertIn("investment_plan", request["instructions"])

    def test_orchestrator_uses_six_calls_and_never_searches_personal_position(self):
        calls = []
        query_log = []
        now = time.time()
        class Research:
            def collect_ephemeral(self, queries, urls, **kwargs):
                query_log.extend(queries)
                return {"state": "ok", "public_requests": 3, "public_pages": 2,
                    "as_of_bjt": "2026-11-11T15:00:00+08:00", "missing": [],
                    "sources": [
                        {"url": "https://sec.gov/Archives/edgar/data/1/filing.htm", "final_url": "https://sec.gov/Archives/edgar/data/1/filing.htm",
                         "title": "NVDA SEC Filing", "published_at": "2025-02-01", "fetched_at": now,
                         "kind": "text/plain", "text": "NVDA Revenue 100 billion USD FY2025. The company sells GPU computing products."},
                        {"url": "https://stockanalysis.com/stocks/nvda/financials/", "final_url": "https://stockanalysis.com/stocks/nvda/financials/",
                         "title": "NVDA Financials", "published_at": "2025-02-02", "fetched_at": now,
                         "kind": "text/plain", "text": "NVDA Revenue 100 billion USD FY2025. The company sells GPU computing products."},
                    ]}
        def model(data, key_file, api_base, timeout=None):
            calls.append(data)
            json.dumps(data, ensure_ascii=False)
            if data["phase"] == "investment_plan":
                return {"targets": [{"company": "NVIDIA", "ticker": "NVDA", "market": "US"}]}, {"input_tokens": 1}
            if data["phase"] == "investment_extract":
                return {"facts": [{"name": "Revenue", "value": "100", "unit": "billion", "currency": "USD",
                    "period": "FY2025", "source_ids": ["S1", "S2"]}],
                    "claims": [{"claim": "The company sells GPU computing products.", "source_id": "S1",
                                "evidence_quote": "The company sells GPU computing products."}]}, {"input_tokens": 1}
            if data["phase"] == "investment_analysis":
                return {"scope": "service", "analysis": "基于来源的分析。"}, {"input_tokens": 1}
            return {"scope": "service", "answer": "结论：估值需结合现金流和反方风险；关键收入见[S1]。"}, {"input_tokens": 1}

        question = "请分析NVDA美股，我持仓100股，成本100美元，风险偏好中等"
        own_context = [{"speaker": "提问者", "text": "持有NVDA，预算2000美元"}]
        result, usages, proof = run_research({"question": question, "personal_context": own_context},
            None, "https://api.apiroute.asia/v1", model_call=model, research=Research())
        self.assertEqual(len(calls), 6)
        self.assertEqual(len(usages), 6)
        self.assertEqual(proof, ["investment_data"])
        self.assertTrue(all("100股" not in query and "成本" not in query for query in query_log))
        self.assertNotIn("personal_context", calls[2])
        self.assertNotIn("personal_context", calls[3])
        self.assertEqual(calls[2]["verified_claims"][0]["source_id"], "S1")
        self.assertEqual(calls[4]["personal_context"], own_context)
        self.assertNotIn("100股", calls[2]["question"])
        self.assertIn("USD", result["answer"])
        self.assertIn("双源一致", result["answer"])
        self.assertIn("https://sec.gov/Archives/edgar/data/1/filing.htm", result["answer"])

    def test_worker_starts_in_isolation_and_stops_before_network_when_key_is_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            key_path = Path(folder) / "missing-key"
            result, usages, evidence = run_isolated({"question": "分析美股NVDA估值", "web_urls": []},
                key_path, "https://api.apiroute.asia/v1")
        self.assertEqual(result.get("failure"), "key_unavailable")
        self.assertEqual(usages, [])
        self.assertEqual(evidence, ["investment_data"])

    def test_isolated_worker_hard_timeout_is_fixed_at_ninety_seconds(self):
        with patch("guard.investment.subprocess.run", side_effect=subprocess.TimeoutExpired(["python"], 90)) as run:
            result, usages, evidence = run_isolated({"question": "AAPL估值"}, "/unused/key", "https://api.apiroute.asia/v1")
        self.assertEqual(result["failure"], "investment_deadline")
        self.assertEqual(usages, [])
        self.assertEqual(evidence, ["investment_data"])
        self.assertEqual(run.call_args.kwargs["timeout"], MAX_SECONDS)
        self.assertEqual(run.call_args.kwargs["env"]["PYTHONDONTWRITEBYTECODE"], "1")

    def test_missing_public_sources_returns_explicit_unsupported_without_synthesis(self):
        calls = []
        class MissingResearch:
            def collect_ephemeral(self, *args, **kwargs):
                return {"state": "unavailable", "sources": [], "missing": ["search_unavailable"],
                    "public_requests": 1, "public_pages": 0, "as_of_bjt": "2026-11-11T15:00:00+08:00"}
        def model(data, *args, **kwargs):
            calls.append(data)
            return {"targets": [{"company": "NVIDIA", "ticker": "NVDA", "market": "US"}]}, {}
        result, usages, _ = run_research({"question": "分析美股NVDA技术指标"}, None,
            "https://api.apiroute.asia/v1", model_call=model, research=MissingResearch())
        self.assertIn("没有找到足够可靠且与目标匹配", result["answer"])
        self.assertIn("技术指标和回测均不支持", result["answer"])
        self.assertIn("证券代码不是必填项", result["answer"])
        self.assertNotIn("UNKNOWN", result["answer"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(usages), 1)


if __name__ == "__main__":
    unittest.main()
