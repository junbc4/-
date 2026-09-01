"""
scrape.py — GitHub Actions용 크롤러 (멀티프로세스 병렬).

[병렬화 원칙 — 결과물은 순차와 동일, 시간만 단축]
 - 사이트를 '도메인별 그룹'으로 나눔. 한 도메인은 한 프로세스가 '순차'로 처리
   (같은 서버에 동시 접속하지 않음 → 차단/부하 방지)
 - 서로 다른 도메인 그룹을 여러 '프로세스'로 나눠 병렬 처리
   (Playwright sync는 스레드 공유가 금지되므로 '프로세스' 병렬만 안전)
 - 각 사이트의 렌더/추출/재시도/차단감지 로직은 순차 버전과 완전히 동일
 - local:true 는 GitHub에서 제외. --local 이면 local + blocked.json 대상만(순차).

사용:
    python scrape.py               # data.json + blocked.json (local 제외)
    python scrape.py --local       # local + blocked.json 대상 → data_local.json (순차)
    python scrape.py --workers 6   # 병렬 프로세스 수(기본 6)
    python scrape.py --debug       # 렌더링 HTML을 debug/ 에 저장
"""
import sys, json, argparse
import multiprocessing as mp
from collections import OrderedDict
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from extract import extract

ROOT = Path(__file__).resolve().parent
SITES_FILE = ROOT / "sites.yaml"
DEBUG_DIR = ROOT / "debug"
BLOCK_HINTS = ["해외에서의", "접속을 제한", "보안 정책에 따라",
               "Access Denied", "Forbidden", "차단되었습니다"]


def _text_len(html):
    try:
        return len(BeautifulSoup(html, "html.parser").get_text(strip=True))
    except Exception:
        return len(html or "")

def _is_blocked(html):
    return any(h in (html or "") for h in BLOCK_HINTS)


def _attempt(browser, site, wait_until, goto_timeout, settle_ms):
    ctx = browser.new_context(
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        locale="ko-KR")
    page = ctx.new_page()
    try:
        try:
            page.goto(site["url"], wait_until=wait_until, timeout=30000)
        except Exception:
            pass
        if site.get("wait_for"):
            try: page.wait_for_selector(site["wait_for"], timeout=10000)
            except Exception: pass
        page.wait_for_timeout(settle_ms)
        try: return page.content()
        except Exception: return ""
    finally:
        try: page.close()
        except Exception: pass
        try: ctx.close()
        except Exception: pass


def render_and_extract(browser, site, today):
    base = site.get("settle_ms", 1500)
    html = _attempt(browser, site, "domcontentloaded", 20000, base)
    tl = _text_len(html)
    # 차단 메시지 감지 or 사실상 빈 페이지(<100) → 재시도 없이 즉시 차단 처리
    if _is_blocked(html) or tl < 100:
        return html, [], True
    rows = extract(html=html or "", base_url=site["url"], source=site["name"], cfg=site, today=today)
    # 내용이 어정쩡(100~200)할 때만 짧게 1회 재시도(느린 로딩 대비)
    if 100 <= tl < 200:
        html2 = _attempt(browser, site, "networkidle", 15000, base + 1500)
        if _is_blocked(html2) or _text_len(html2) < 100:
            return html2, [], True
        rows2 = extract(html=html2 or "", base_url=site["url"], source=site["name"], cfg=site, today=today)
        if len(rows2) > len(rows):
            html, rows = html2, rows2
    blocked = _is_blocked(html) or (_text_len(html) < 100)
    return html, rows, blocked


def _process_sites(sites_chunk, today, debug):
    """한 프로세스: 자기 브라우저를 띄워 배정된 사이트들을 순차 처리."""
    rows_all, errors, blocked = [], [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for site in sites_chunk:
            name = site["name"]
            try:
                html, rows, blk = render_and_extract(browser, site, today)
                if debug:
                    safe = "".join(c if c.isalnum() else "_" for c in name)
                    (DEBUG_DIR / f"{safe}.html").write_text(html or "", encoding="utf-8")
                if blk and len(rows) == 0:
                    blocked.append(name); print(f"[BLOCKED] {name}", flush=True)
                else:
                    print(f"[OK] {name}: {len(rows)}건", flush=True)
                rows_all.extend(rows)
            except Exception as e:
                msg = str(e).splitlines()[0]
                print(f"[ERR] {name}: {msg}", flush=True)
                errors.append({"source": name, "error": msg})
        browser.close()
    return rows_all, errors, blocked


def _worker(args):
    sites_chunk, today_iso, debug = args
    return _process_sites(sites_chunk, date.fromisoformat(today_iso), debug)


def _balance_chunks(groups, n):
    """도메인 그룹(통째)을 n개 프로세스에 크기 균형있게 배분. 도메인은 쪼개지 않음."""
    loads = [0]*n
    chunks = [[] for _ in range(n)]
    for host, gsites in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        i = loads.index(min(loads))       # 가장 한가한 워커에 배정
        chunks[i].extend(gsites)
        loads[i] += len(gsites)
    return [c for c in chunks if c]


def scrape_all(debug=False, only_local=False, workers=6):
    cfg = yaml.safe_load(SITES_FILE.read_text(encoding="utf-8"))
    sites = cfg.get("sites", [])
    today = date.today()
    if debug: DEBUG_DIR.mkdir(exist_ok=True)

    blocked_names = set()
    if only_local:
        bf = ROOT / "blocked.json"
        if bf.exists():
            try:
                blocked_names = set(json.loads(bf.read_text(encoding="utf-8")).get("blocked", []))
                print(f"[로컬] blocked.json 로드: {len(blocked_names)}곳", flush=True)
            except Exception:
                pass

    targets = []
    for s in sites:
        s.setdefault("name", s["url"])
        is_local = bool(s.get("local"))
        if only_local:
            if is_local or s["name"] in blocked_names: targets.append(s)
        else:
            if not is_local: targets.append(s)

    all_rows, errors, blocked_list = [], [], []

    if only_local:
        # 로컬(수십 개)은 순차 처리 — Windows 호환·단순
        print(f"[로컬] 대상 {len(targets)}개 순차 수집", flush=True)
        all_rows, errors, blocked_list = _process_sites(targets, today, debug)
    else:
        # 도메인 그룹핑 → 프로세스 병렬
        groups = OrderedDict()
        for s in targets:
            groups.setdefault(urlsplit(s["url"]).netloc, []).append(s)
        n = max(1, min(workers, len(groups)))
        chunks = _balance_chunks(groups, n)
        print(f"대상 {len(targets)}개 · 도메인 {len(groups)}개 · 프로세스 {len(chunks)}", flush=True)
        today_iso = today.isoformat()
        ctx = mp.get_context("spawn")
        with ctx.Pool(processes=len(chunks)) as pool:
            for r, e, b in pool.map(_worker, [(c, today_iso, debug) for c in chunks]):
                all_rows.extend(r); errors.extend(e); blocked_list.extend(b)

    # 제목 정규화 기준 중복 제거(같은 공고가 여러 도서관 사이트에 올라와도 1건)
    import re as _re
    def _norm(t):
        return _re.sub(r"\s+", "", _re.sub(r"[^\w가-힣]", "", t or "")).lower()
    seen, deduped = set(), []
    for r in all_rows:
        key = _norm(r["title"])
        if key in seen: continue
        seen.add(key); deduped.append(r)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = ROOT / ("data_local.json" if only_local else "data.json")
    out.write_text(json.dumps({
        "generatedAt": now, "count": len(deduped),
        "errors": errors, "postings": deduped,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if not only_local:
        (ROOT / "blocked.json").write_text(json.dumps({
            "generatedAt": now, "blocked": sorted(set(blocked_list)),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n총 {len(deduped)}건 저장 · 차단감지 {len(set(blocked_list))}곳"
              + (f" (오류 {len(errors)}건)" if errors else ""), flush=True)
        if blocked_list:
            print("차단(로컬 필요): " + ", ".join(sorted(set(blocked_list))), flush=True)
    else:
        print(f"\n[로컬] 총 {len(deduped)}건 저장 → data_local.json", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    scrape_all(debug=args.debug, only_local=args.local, workers=args.workers)
