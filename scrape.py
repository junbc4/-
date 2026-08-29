"""
scrape.py — GitHub Actions용 크롤러 (병렬화 버전).

[병렬화 원칙 — 결과물은 순차 버전과 동일, 시간만 단축]
 - 사이트를 '도메인별 그룹'으로 나눠, 서로 다른 도메인끼리만 동시에 처리
 - 같은 도메인(예: 강남 41개)은 한 워커가 '순차'로 처리 → 한 사이트에 동시다발 접속 안 함(차단/부하 방지)
 - 동시 워커 수 = WORKERS (기본 6)
 - 각 사이트의 렌더링/추출/재시도/차단감지 로직은 순차 버전과 완전히 동일
 - local:true 사이트는 건너뜀(집PC 담당). --local 이면 local + blocked.json 대상만.

사용:
    python scrape.py          # data.json + blocked.json 생성 (local 제외)
    python scrape.py --local  # local:true + blocked.json 대상 → data_local.json
    python scrape.py --debug  # 렌더링 HTML을 debug/ 에 저장
    python scrape.py --workers 8   # 동시 워커 수 조정(기본 6)
"""
import sys, json, argparse, threading
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict, OrderedDict
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from extract import extract

ROOT = Path(__file__).resolve().parent
SITES_FILE = ROOT / "sites.yaml"
BLOCK_HINTS = ["해외에서의", "접속을 제한", "보안 정책에 따라",
               "Access Denied", "Forbidden", "차단되었습니다"]

_print_lock = threading.Lock()
def log(msg):
    with _print_lock:
        print(msg, flush=True)


def _text_len(html):
    try:
        return len(BeautifulSoup(html, "html.parser").get_text(strip=True))
    except Exception:
        return len(html or "")

def _is_blocked(html):
    return any(h in (html or "") for h in BLOCK_HINTS)


def _attempt(browser, site, wait_until, goto_timeout, settle_ms):
    """1회 렌더링. 각 사이트마다 독립 컨텍스트(새 탭)."""
    ctx = browser.new_context(
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        locale="ko-KR")
    page = ctx.new_page()
    try:
        try:
            page.goto(site["url"], wait_until=wait_until, timeout=goto_timeout)
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
    """순차 버전과 동일한 렌더/추출/재시도/차단 판정."""
    base = site.get("settle_ms", 1500)
    html = _attempt(browser, site, "domcontentloaded", 30000, base)
    rows = extract(html=html or "", base_url=site["url"], source=site["name"], cfg=site, today=today)
    if _text_len(html) < 200 or _is_blocked(html):
        html2 = _attempt(browser, site, "networkidle", 50000, base + 2500)
        rows2 = extract(html=html2 or "", base_url=site["url"], source=site["name"], cfg=site, today=today)
        if _is_blocked(html2) or len(rows2) > len(rows):
            html, rows = html2, rows2
    blocked = _is_blocked(html) or (_text_len(html) < 100)
    return html, rows, blocked


def process_domain_group(pw_browser, sites_in_domain, today, debug, DEBUG_DIR):
    """한 도메인의 사이트들을 '순차'로 처리(같은 서버에 동시 접속 방지)."""
    out_rows, out_err, out_blocked = [], [], []
    for site in sites_in_domain:
        name = site["name"]
        try:
            html, rows, blocked = render_and_extract(pw_browser, site, today)
            if debug:
                safe = "".join(c if c.isalnum() else "_" for c in name)
                (DEBUG_DIR / f"{safe}.html").write_text(html or "", encoding="utf-8")
            if blocked and len(rows) == 0:
                out_blocked.append(name); log(f"[BLOCKED] {name}")
            else:
                log(f"[OK] {name}: {len(rows)}건")
            out_rows.extend(rows)
        except Exception as e:
            msg = str(e).splitlines()[0]
            log(f"[ERR] {name}: {msg}")
            out_err.append({"source": name, "error": msg})
    return out_rows, out_err, out_blocked


def scrape_all(debug=False, only_local=False, workers=6):
    cfg = yaml.safe_load(SITES_FILE.read_text(encoding="utf-8"))
    sites = cfg.get("sites", [])
    today = date.today()
    DEBUG_DIR = ROOT / "debug"
    if debug: DEBUG_DIR.mkdir(exist_ok=True)

    # 대상 선별(순차 버전과 동일 규칙)
    blocked_names = set()
    if only_local:
        bf = ROOT / "blocked.json"
        if bf.exists():
            try:
                blocked_names = set(json.loads(bf.read_text(encoding="utf-8")).get("blocked", []))
                log(f"[로컬] blocked.json 로드: {len(blocked_names)}곳")
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

    # 도메인별 그룹핑(같은 도메인은 한 그룹=순차 처리, 그룹끼리 병렬)
    groups = OrderedDict()
    for s in targets:
        host = urlsplit(s["url"]).netloc
        groups.setdefault(host, []).append(s)
    log(f"대상 {len(targets)}개 · 도메인 {len(groups)}개 · 동시 워커 {workers}")

    all_rows, errors, blocked_list = [], [], []
    lock = threading.Lock()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        def worker(host_sites):
            r, e, b = process_domain_group(browser, host_sites, today, debug, DEBUG_DIR)
            with lock:
                all_rows.extend(r); errors.extend(e); blocked_list.extend(b)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(worker, list(groups.values())))

        browser.close()

    # 중복 제거(순차 버전과 동일)
    seen, deduped = set(), []
    for r in all_rows:
        key = (r["source"], r["title"], r["link"])
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
        log(f"\n총 {len(deduped)}건 저장 · 차단감지 {len(set(blocked_list))}곳"
            + (f" (오류 {len(errors)}건)" if errors else ""))
        if blocked_list:
            log("차단(로컬 필요): " + ", ".join(sorted(set(blocked_list))))
    else:
        log(f"\n[로컬] 총 {len(deduped)}건 저장 → data_local.json")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    scrape_all(debug=args.debug, only_local=args.local, workers=args.workers)
