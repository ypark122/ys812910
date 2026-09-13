"""Daily NTS public-document collector. Python 3.11+, standard library only."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ORIGIN = "https://taxlaw.nts.go.kr"
LIST_ACTION = "ASIPDI002PR01"
DETAIL_ACTION = "ASIQTB002PR01"
KST = dt.timezone(dt.timedelta(hours=9))
MAX_FEED_BYTES = 5 * 1024 * 1024
GROUPS = {
    "interpretations": ("question,question_gr", [f"001_{n:02}" for n in range(1, 5)]),
    "decisions": ("precedent,precedent_gr", [f"001_{n:02}" for n in range(5, 11)]),
}
# Preserve bookmarks for documents bundled in Android 0.1.0.
LEGACY_IDS = {str(200000000000000000 + n): f"nts:{n}" for n in
              (22920, 22917, 22919, 22915, 22895, 22913, 22921,
               22912, 22928, 22926, 22935, 22931, 22903, 22909)}


class CollectionError(Exception):
    pass


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if not self.hidden and tag in ("p", "div", "br", "tr", "li", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden and tag in ("p", "div", "tr", "li"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def clean(value):
    parser = PlainText()
    parser.feed(str(value or ""))
    return "\n".join(filter(None, (re.sub(r"\s+", " ", line).strip()
                                  for line in "".join(parser.parts).splitlines())))


def iso_date(value):
    s = re.sub(r"\D", "", str(value or ""))[:8]
    try:
        return dt.datetime.strptime(s, "%Y%m%d").date().isoformat()
    except ValueError as exc:
        raise CollectionError("Invalid source date") from exc


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


class Client:
    """Public website read actions only; fail closed on schema/count drift."""
    def __init__(self, delay=1.0):
        self.delay = delay
        self.last_request = 0.0

    def action(self, action, params):
        if action not in (LIST_ACTION, DETAIL_ACTION):
            raise CollectionError("Unsupported action")
        body = urllib.parse.urlencode({"actionId": action, "paramData": json.dumps(params)}).encode()
        for attempt in range(3):
            time.sleep(max(0, self.last_request + self.delay - time.monotonic()))
            self.last_request = time.monotonic()
            request = urllib.request.Request(ORIGIN + "/action.do", data=body, headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json", "User-Agent": "MaeilTaxPersonalCollector/0.2",
                "Referer": ORIGIN + "/qt/USEQTJ001M.do",
            })
            try:
                with urllib.request.urlopen(request, timeout=40) as response:
                    if urllib.parse.urlparse(response.url).netloc != "taxlaw.nts.go.kr":
                        raise CollectionError("Unexpected redirect")
                    raw = response.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise CollectionError("Source response exceeds limit")
                doc = json.loads(raw)
                if doc.get("status") != "SUCCESS" or action not in doc.get("data", {}):
                    raise CollectionError("Source action failed or schema changed")
                return doc["data"][action]
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise CollectionError(f"Source HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt == 2:
                    raise CollectionError("Source network request failed") from exc
            except (ValueError, TypeError) as exc:
                raise CollectionError("Invalid source JSON") from exc
            time.sleep(2 ** (attempt + 1))
        raise CollectionError("Source retries exhausted")

    def listing(self, group, start, end, page_size=50, max_pages=200):
        collection, codes = GROUPS[group]
        result = {}
        total = None
        session = None
        for page in range(1, max_pages + 1):
            params = dict(collectionName=collection, dcmClCdCtl=codes,
                          schDtBase="FRS_RGT_DTM", bltnStrtDt=start.replace("-", ""),
                          bltnEndDt=end.replace("-", ""), sortField="FRS_RGT_DTM/DESC",
                          startCount=page, viewCount=page_size, rltnStttCtl=[])
            if session:
                params["wnSessionUuid"] = session
            data = self.action(LIST_ACTION, params)
            try:
                facets = data["top"][0]["categoryMap"]["SUB_ID_CATEGORY"]
                if not isinstance(facets, list) or not isinstance(data["body"], list):
                    raise ValueError("Missing result list")
                reported = sum(int(x["count"]) for x in facets if x["name"] in codes)
                if reported < 0:
                    raise ValueError("Negative count")
                if total is None:
                    total = reported
                elif total != reported:
                    raise CollectionError("Result count changed during pagination; retry the run")
                sessions = data.get("wnSessionUuid") or []
                if sessions:
                    session = sessions[0].get("wnSessionUuid") or session
                rows = [x["dcm"] for x in data["body"]]
                for row in rows:
                    identity = str(row["DOC_ID"])
                    if not re.fullmatch(r"\d{18}", identity):
                        raise ValueError("Invalid document ID")
                    if row["SUB_ID_CATEGORY"] not in codes:
                        raise ValueError("Unexpected category")
                    if not start <= iso_date(row["FRS_RGT_DTM"]) <= end:
                        raise ValueError("Registration date filter was ignored")
                    if identity in result:
                        raise CollectionError("Repeated document in pagination; retry the run")
                    result[identity] = row
                if len(result) == total:
                    return list(result.values())
                if not rows or len(result) > total:
                    raise CollectionError("Incomplete or inconsistent source results")
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise CollectionError("Source list schema changed") from exc
        raise CollectionError("Pagination safety limit exceeded; narrow the date range")

    def detail(self, identity):
        result = self.action(DETAIL_ACTION, {"dcmDVO": {"ntstDcmId": identity}})
        if not isinstance(result.get("dcmDVO"), dict) or result["dcmDVO"].get("ntstDcmId") != identity:
            raise CollectionError("Detail missing or identity mismatch")
        return result


def normalize(row, detail):
    """Keep only public document content; omit operator IDs/internal metadata."""
    d = detail["dcmDVO"]
    code = d["ntstDcmClCd"]
    if code not in {f"{n:02}" for n in range(1, 11)}:
        raise CollectionError("Unknown document category")
    if code != row["SUB_ID_CATEGORY"].split("_")[-1]:
        raise CollectionError("List/detail category mismatch")
    full_text = "\n\n".join(clean(x.get("dcmFleByte")) for x in
                              detail.get("dcmHwpEditorDVOList", []) if x.get("dcmFleTy") == "html")
    return {
        "documentId": row["DOC_ID"],
        "kind": "해석례" if int(code) < 5 else "판례" if code == "09" else "결정례",
        "category": clean(row.get("LBL1_TTL")) or code,
        "title": clean(d.get("ntstDcmTtl")), "number": clean(d.get("ntstDcmDscmCntn")),
        "tax": clean(row.get("NTST_TLAW_CL_NM")) or "기타",
        "registeredDate": iso_date(row["FRS_RGT_DTM"]),
        "detailRegisteredDate": iso_date(d["frsRgtDtm"]),
        "producedDate": iso_date(d["ntstDcmRgtDt"]),
        "gist": clean(d.get("ntstDcmGistCntn")) or clean(row.get("GIST_CNTN")),
        "answer": clean(d.get("ntstDcmCntn")) or clean(d.get("ntstDcmRplyCntn")),
        "fullText": full_text,
        "group": str(d.get("ntstFareIntcGrpSn") or ""),
        "outcome": clean(row.get("NTST_DCM_DCS_CL_NM")),
    }


def excerpt(text, limit):
    return text if len(text) <= limit else text[:limit].rstrip() + "\n… [이하 원문에서 확인]"


def make_item(source):
    identity = source["documentId"]
    if not source["title"] or not source["number"] or not (source["gist"] or source["answer"]):
        raise CollectionError(f"Missing title/number/official summary: {identity}")
    # These are explicitly attributed extracts, not AI-produced legal analysis.
    result = {key: source[key] for key in ("kind", "tax", "title", "number", "registeredDate",
                                          "detailRegisteredDate", "producedDate", "group", "outcome")}
    result.update(
        id=LEGACY_IDS.get(identity, "nts:" + identity), sample=False,
        sourceUrl=ORIGIN + "/qt/USEQTA002P.do?ntstDcmId=" + identity,
        summary="[국세법령정보시스템 요지·답변 발췌]\n" + excerpt(source["gist"] or source["answer"], 2500),
        facts=("[상세 원문 앞부분 발췌 · 사실관계만 분리한 요약이 아닙니다]\n" + excerpt(source["fullText"], 1600)
               if source["fullText"] else "상세 원문 텍스트를 제공하지 않는 자료입니다. 원문 링크에서 확인해 주세요."),
        reason=("[공식 답변·결정·판결내용 발췌]\n" + excerpt(source["answer"], 2500)
                if source["answer"] else "판단 근거는 원문에서 확인해 주세요. 별도 해설을 생성하지 않았습니다."),
        practical="자동 수집한 공식 요지·본문 발췌입니다. 적용 요건과 예외는 원문을 확인해 주세요. 별도의 AI 실무 해설은 아직 제공하지 않습니다.",
        summaryMode="official-extract",
    )
    return result


def validate_feed(feed):
    if feed.get("schemaVersion") != 1 or not isinstance(feed.get("items"), list):
        raise CollectionError("Invalid feed schema")
    if len(feed["items"]) > 3000:
        raise CollectionError("Android 0.1.0 capacity: over 3,000 documents; app storage upgrade required")
    seen = set()
    for item in feed["items"]:
        for key in ("id", "title", "kind", "tax", "number", "registeredDate", "detailRegisteredDate",
                    "producedDate", "summary", "facts", "reason", "practical", "sourceUrl"):
            if not isinstance(item.get(key), str) or not item[key].strip() or len(item[key]) > 30000:
                raise CollectionError("Invalid feed field: " + key)
        if item["id"] in seen or item["kind"] not in ("해석례", "판례", "결정례") or type(item.get("sample")) is not bool:
            raise CollectionError("Invalid feed identity/type")
        seen.add(item["id"])
        for key in ("registeredDate", "detailRegisteredDate", "producedDate"):
            dt.date.fromisoformat(item[key])
        url = urllib.parse.urlparse(item["sourceUrl"])
        if url.scheme != "https" or url.hostname != "taxlaw.nts.go.kr" or url.username or url.port not in (None, 443):
            raise CollectionError("Invalid source URL")
    raw = encode(feed)
    # Leave a little room for the original 14 sample rows on older installations.
    if len(raw) > MAX_FEED_BYTES - 150000:
        raise CollectionError("Android 0.1.0 capacity: feed nearing 5MB; app storage upgrade required")
    return raw


def open_db(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.execute("CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, source TEXT NOT NULL, "
               "digest TEXT NOT NULL, item TEXT NOT NULL, checked_at TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS revisions (id TEXT, digest TEXT, source TEXT, "
               "observed_at TEXT, PRIMARY KEY(id,digest))")
    db.commit()
    return db


def default_range(db, end=None):
    end_date = dt.date.fromisoformat(end) if end else dt.datetime.now(KST).date()
    saved = db.execute("SELECT value FROM meta WHERE key='last_end'").fetchone()
    baseline = min(end_date, dt.date.fromisoformat(saved[0])) if saved else end_date
    return (baseline - dt.timedelta(days=7)).isoformat(), end_date.isoformat()


def collect(db, client, start, end, public, recheck=20):
    if start > end:
        raise CollectionError("Start date must not follow end date")
    dt.date.fromisoformat(start)
    dt.date.fromisoformat(end)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    public = Path(public)
    collected = {}
    counts = {}
    # All source requests finish before any persistent document changes.
    for group in GROUPS:
        rows = client.listing(group, start, end)
        counts[group] = len(rows)
        for row in rows:
            if row["DOC_ID"] in collected:
                raise CollectionError("Duplicate across document groups")
            collected[row["DOC_ID"]] = row
    pending = {}
    for index, (identity, row) in enumerate(collected.items(), 1):
        print(f"detail {index}/{len(collected)} {identity}", flush=True)
        pending[identity] = normalize(row, client.detail(identity))
    # Rotate through older known documents to detect body edits that do not move registration dates.
    old = db.execute("SELECT id,source FROM documents ORDER BY checked_at,id LIMIT ?",
                     (recheck + len(collected),)).fetchall()
    checked = 0
    for identity, raw in old:
        if identity in pending or checked >= recheck:
            continue
        previous = json.loads(raw)
        detail = client.detail(identity)
        row = {"DOC_ID": identity, "SUB_ID_CATEGORY": "001_" + detail["dcmDVO"]["ntstDcmClCd"],
               "FRS_RGT_DTM": previous["registeredDate"], "NTST_TLAW_CL_NM": previous["tax"],
               "LBL1_TTL": previous["category"], "GIST_CNTN": previous["gist"],
               "NTST_DCM_DCS_CL_NM": previous["outcome"]}
        pending[identity] = normalize(row, detail)
        checked += 1
    changed = []
    old_items = {identity: json.loads(item) for identity, item in db.execute("SELECT id,item FROM documents")}
    updates = []
    for identity, source in pending.items():
        digest = hashlib.sha256(encode(source)).hexdigest()
        item = make_item(source)
        existing = db.execute("SELECT digest FROM documents WHERE id=?", (identity,)).fetchone()
        if existing is None or existing[0] != digest:
            changed.append(identity)
        updates.append((identity, encode(source).decode(), digest, encode(item).decode(), now))
        old_items[identity] = item
    items = sorted(old_items.values(), key=lambda x: (x["registeredDate"], x["id"]), reverse=True)
    feed = {"schemaVersion": 1, "generatedAt": now, "coveredFrom": start, "coveredThrough": end,
            "summaryMode": "official-extract", "items": items}
    encoded = validate_feed(feed)
    status = {"ok": True, "lastSuccess": now, "coveredFrom": start, "coveredThrough": end,
              "listingCounts": counts, "changed": len(changed), "total": len(items),
              "olderRechecked": checked,
              "dateDiscrepancies": sum(x["registeredDate"] != x["detailRegisteredDate"] for x in items),
              "message": "수집 완료" if sum(counts.values()) else "정상 조회 완료: 조회 기간 등록 자료 없음"}
    # Failure before commit leaves all documents intact. Re-runs recover an interrupted export.
    with db:
        for update in updates:
            db.execute("INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?)", update)
            identity, source, digest, _, observed = update
            db.execute("INSERT OR IGNORE INTO revisions VALUES (?,?,?,?)", (identity, digest, source, observed))
        saved = db.execute("SELECT value FROM meta WHERE key='last_end'").fetchone()
        watermark = max(end, saved[0]) if saved else end
        db.execute("INSERT OR REPLACE INTO meta VALUES ('last_end',?)", (watermark,))
    atomic_write(public / "report.json", encoded)
    atomic_write(public / "status.json", encode(status))
    daily = {"schemaVersion": 1, "generatedAt": now,
             "items": [old_items[x] for x in changed]}
    atomic_write(public / "latest-changes.json", validate_feed(daily))
    atomic_write(public / "index.html", status_page(status))
    atomic_write(public / ".nojekyll", b"")
    return status


def status_page(status):
    text = html.escape(json.dumps(status, ensure_ascii=False, indent=2))
    return ("<!doctype html><html lang='ko'><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
            "<title>매일세법 보고서</title><body><h1>매일세법 보고서</h1>"
            "<p>국세청 공식 앱이 아닌 개인용 수집 보고서입니다. 공식 요지·본문 발췌를 제공합니다.</p>"
            "<p>앱 설정에 이 페이지 주소 뒤의 <b>report.json</b> 주소를 입력하세요.</p>"
            "<p><a href='report.json'>누적 보고서</a> · <a href='status.json'>수집 상태</a></p>"
            "<pre>" + text + "</pre></body></html>").encode("utf-8")


@contextlib.contextmanager
def run_lock(path):
    """OS lock is released even if the process crashes; no stale lock file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as file:
        if file.tell() == 0:
            file.write(b"0")
            file.flush()
        file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise CollectionError("Another collection is running") from exc
        try:
            yield
        finally:
            file.seek(0)
            if os.name == "nt":
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default="state/maeil.sqlite3")
    parser.add_argument("--public", default="public")
    parser.add_argument("--from", dest="start")
    parser.add_argument("--to", dest="end")
    parser.add_argument("--recheck", type=int, default=20)
    args = parser.parse_args()
    if not 0 <= args.recheck <= 200:
        parser.error("--recheck must be between 0 and 200")
    try:
        with run_lock(Path(args.state).with_suffix(".lock")):
            db = open_db(args.state)
            try:
                start, end = default_range(db, args.end)
                status = collect(db, Client(), args.start or start, end, args.public, args.recheck)
                print(json.dumps(status, ensure_ascii=False))
            except Exception as exc:
                # Never overwrite the last successful report with an error or empty payload.
                failure = {"ok": False, "attemptedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
                           "message": str(exc), "previousReportPreserved": True}
                atomic_write(Path(args.public) / "status.json", encode(failure))
                raise
            finally:
                db.close()
    except Exception as exc:
        print("Collection failed: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
