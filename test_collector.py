import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from collector import (Client, CollectionError, GROUPS, collect, open_db, default_range,
                       clean, normalize, make_item, validate_feed, iso_date)

ID = "200000000000022920"
ROW = {"DOC_ID": ID, "FRS_RGT_DTM": "20260908120000", "SUB_ID_CATEGORY": "001_01",
       "NTST_TLAW_CL_NM": "소득세", "LBL1_TTL": "사전답변", "NTST_DCM_DCS_CL_NM": "해당없음"}
DETAIL = {"dcmDVO": {"ntstDcmId": ID, "ntstDcmClCd": "01", "frsRgtDtm": "20260907120000",
                     "ntstDcmRgtDt": "20260825", "ntstDcmTtl": "테스트 문서", "ntstDcmDscmCntn": "테스트-001",
                     "ntstDcmGistCntn": "공식 요지", "ntstDcmCntn": "공식 답변"},
          "dcmHwpEditorDVOList": [{"dcmFleTy": "html", "dcmFleByte": "<p>사실관계</p><p>판단</p>"}]}


class FakeClient:
    def __init__(self, rows=None, detail=None, failure=False):
        self.rows = rows if rows is not None else [ROW]
        self.details = detail or DETAIL
        self.failure = failure

    def listing(self, group, start, end):
        if self.failure and group == "decisions":
            raise CollectionError("network failed")
        return copy.deepcopy(self.rows) if group == "interpretations" else []

    def detail(self, identity):
        return copy.deepcopy(self.details)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = open_db(self.root / "state.sqlite3")
        self.public = self.root / "public"

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def run_collect(self, client=None, **kwargs):
        return collect(self.db, client or FakeClient(), "2026-09-08", "2026-09-11", self.public, **kwargs)

    def test_idempotence_and_bookmark_compatibility(self):
        self.assertEqual(self.run_collect()["changed"], 1)
        self.assertEqual(self.run_collect()["changed"], 0)
        feed = json.loads((self.public / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(feed["items"][0]["id"], "nts:22920")
        self.assertFalse(feed["items"][0]["sample"])

    def test_changed_body_retains_revision(self):
        self.run_collect()
        changed = copy.deepcopy(DETAIL)
        changed["dcmDVO"]["ntstDcmCntn"] = "수정된 답변"
        self.assertEqual(self.run_collect(FakeClient(detail=changed))["changed"], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM revisions").fetchone()[0], 2)

    def test_second_category_failure_preserves_all_previous_data(self):
        self.run_collect()
        before = (self.public / "report.json").read_bytes()
        with self.assertRaises(CollectionError):
            self.run_collect(FakeClient(failure=True))
        self.assertEqual(before, (self.public / "report.json").read_bytes())
        self.assertEqual(self.db.execute("SELECT count(*) FROM revisions").fetchone()[0], 1)

    def test_zero_results_does_not_delete_previous_reports(self):
        self.run_collect()
        status = self.run_collect(FakeClient(rows=[]), recheck=0)
        self.assertEqual(status["total"], 1)
        self.assertEqual(status["listingCounts"]["interpretations"], 0)
        self.assertIn("등록 자료 없음", status["message"])

    def test_old_documents_are_rechecked_without_spurious_changes(self):
        self.run_collect()
        status = self.run_collect(FakeClient(rows=[]), recheck=1)
        self.assertEqual(status["changed"], 0)
        self.assertEqual(status["olderRechecked"], 1)

    def test_old_body_edits_are_found(self):
        self.run_collect()
        changed = copy.deepcopy(DETAIL)
        changed["dcmDVO"]["ntstDcmCntn"] = "옛 문서가 수정됨"
        status = self.run_collect(FakeClient(rows=[], detail=changed), recheck=1)
        self.assertEqual(status["changed"], 1)

    def test_discrepancies_are_not_silently_corrected(self):
        source = normalize(ROW, DETAIL)
        self.assertEqual(source["registeredDate"], "2026-09-08")
        self.assertEqual(source["detailRegisteredDate"], "2026-09-07")

    def test_decisions_are_supported(self):
        for code in ("05", "06", "07", "08", "10"):
            row, detail = copy.deepcopy(ROW), copy.deepcopy(DETAIL)
            row["SUB_ID_CATEGORY"] = "001_" + code
            detail["dcmDVO"]["ntstDcmClCd"] = code
            item = make_item(normalize(row, detail))
            self.assertEqual(item["kind"], "결정례")
            validate_feed({"schemaVersion": 1, "items": [item]})

    def test_markup_removed_and_entities_decoded(self):
        self.assertEqual(clean("<p>가 &amp; 나</p><script>bad()</script><p>다</p>"), "가 & 나\n다")

    def test_invalid_dates_fail(self):
        with self.assertRaises(CollectionError):
            iso_date("20260230")

    def test_missing_required_source_fails(self):
        source = normalize(ROW, DETAIL)
        source["number"] = ""
        with self.assertRaises(CollectionError):
            make_item(source)

    def test_no_operator_fields_are_archived(self):
        detail = copy.deepcopy(DETAIL)
        detail["dcmDVO"]["inptOptrMemNo"] = "SECRET_OPERATOR"
        self.assertNotIn("SECRET_OPERATOR", json.dumps(normalize(ROW, detail)))

    def test_missed_runs_are_backfilled_and_watermark_never_regresses(self):
        self.run_collect()
        self.assertEqual(default_range(self.db, "2026-10-11"), ("2026-09-04", "2026-10-11"))
        collect(self.db, FakeClient(rows=[]), "2026-09-01", "2026-09-02", self.public, recheck=0)
        self.assertEqual(self.db.execute("SELECT value FROM meta WHERE key='last_end'").fetchone()[0], "2026-09-11")

    def test_feed_capacity_fails_without_silent_truncation(self):
        item = make_item(normalize(ROW, DETAIL))
        with self.assertRaises(CollectionError):
            validate_feed({"schemaVersion": 1, "items": [dict(item, id=str(i)) for i in range(3001)]})


class PaginationTests(unittest.TestCase):
    def fake(self, pages):
        class PagingClient(Client):
            def action(self, action, params):
                self.last_params = params
                return pages[params["startCount"] - 1]
        return PagingClient(delay=0)

    def page(self, ids, total=2):
        return {"top": [{"categoryMap": {"SUB_ID_CATEGORY": [{"name": "001_01", "count": str(total)}]}}],
                "body": [{"dcm": dict(ROW, DOC_ID=identity)} for identity in ids]}

    def test_all_pages_are_collected(self):
        client = self.fake([self.page([ID]), self.page(["200000000000022921"])])
        self.assertEqual(len(client.listing("interpretations", "2026-09-08", "2026-09-08", page_size=1)), 2)
        self.assertEqual(client.last_params["schDtBase"], "FRS_RGT_DTM")

    def test_repeated_page_is_not_success(self):
        client = self.fake([self.page([ID]), self.page([ID])])
        with self.assertRaises(CollectionError):
            client.listing("interpretations", "2026-09-08", "2026-09-08", page_size=1)

    def test_count_drift_and_incomplete_results_fail(self):
        for page in (self.page([], 2), self.page(["200000000000022921"], 3)):
            client = self.fake([self.page([ID]), page])
            with self.assertRaises(CollectionError):
                client.listing("interpretations", "2026-09-08", "2026-09-08")

    def test_empty_success_is_distinct_from_missing_schema(self):
        self.assertEqual(self.fake([self.page([], 0)]).listing("interpretations", "2026-09-08", "2026-09-08"), [])
        with self.assertRaises(CollectionError):
            self.fake([{"body": []}]).listing("interpretations", "2026-09-08", "2026-09-08")

    def test_date_filter_must_be_honored(self):
        with self.assertRaises(CollectionError):
            self.fake([self.page([ID], 1)]).listing("interpretations", "2026-09-09", "2026-09-09")


if __name__ == "__main__":
    unittest.main()
