"""
scrape.py — GitHub Actions용 크롤러 (해외 IP).
 - local: true 사이트(해외차단, 예: 성동·성북)는 건너뜀 → 집PC(run_local.py)가 담당
 - 사이트마다 새 탭 열고 닫아 충돌 방지
 - 로딩 실패 또는 '추출 0건'이면 더 오래 기다려 재시도(은평류 지연로딩 대응)

사용:
    python scrape.py          # data.json 생성 (local 사이트 제외)
    python scrape.py --debug  # 렌더링 HTML을 debug/ 에 저장
"""
import sys, json, argparse
from datetime import date, datetime, timezone
from pathlib import Path

import yaml
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from extract import extract

ROOT = Path(__file__).resolve().parent
SITES_FILE = ROOT / "sites.yaml"
OUT_FILE = ROOT / "data.json"
DEBUG_DIR = ROOT / "debug"


def _text_len(html):
    try:
        return len(BeautifulSoup(html, "html.parser").get_text(strip=True))
    except Exception:
        return len(html or "")


def _attempt(browser, site, wait_until, goto_timeout, settle_ms):
    ctx = browser.new_context(
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        locale="ko-KR",
    )
    page = ctx.new_page()
    try:
        try:
            page.goto(site["url"], wait_until=wait_until, timeout=goto_timeout)
        except Exception:
            pass
        if site.get("wait_for"):
            try:
                page.wait_for_selector(site["wait_for"], timeout=12000)
            except Exception:
                pass
        page.wait_for_timeout(settle_ms)
        try:
            return page.content()
        except Exception:
            return ""
    finally:
        try: page.close()
        except Exception: pass
        try: ctx.close()
        except Exception: pass


def render_and_extract(browser, site, today):
    """렌더 → 추출. 내용부족/0건이면 networkidle로 1회 재시도."""
    base = site.get("settle_ms", 1500)
    html = _attempt(browser, site, "domcontentloaded", 35000, base)
    rows = extract(html=html or "", base_url=site["url"], source=site["name"], cfg=site, today=today)
    if _text_len(html) < 200 or len(rows) == 0:
        html2 = _attempt(browser, site, "networkidle", 60000, base + 3000)
        rows2 = extract(html=html2 or "", base_url=site["url"], source=site["name"], cfg=site, today=today)
        if len(rows2) > len(rows):
            return html2, rows2
    return html, rows


def scrape_all(debug=False, only_local=False):
    cfg = yaml.safe_load(SITES_FILE.read_text(encoding="utf-8"))
    sites = cfg.get("sites", [])
    today = date.today()
    all_rows, errors = [], []
    if debug:
        DEBUG_DIR.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for site in sites:
            is_local = bool(site.get("local"))
            # GitHub 실행: local 사이트 건너뜀 / 집PC 실행: local 사이트만
            if only_local and not is_local:
                continue
            if not only_local and is_local:
                continue
            name = site.setdefault("name", site["url"])
            try:
                html, rows = render_and_extract(browser, site, today)
                if debug:
                    safe = "".join(c if c.isalnum() else "_" for c in name)
                    (DEBUG_DIR / f"{safe}.html").write_text(html or "", encoding="utf-8")
                print(f"[OK] {name}: {len(rows)}건", flush=True)
                all_rows.extend(rows)
            except Exception as e:
                msg = str(e).splitlines()[0]
                print(f"[ERR] {name}: {msg}", flush=True)
                errors.append({"source": name, "error": msg})
        browser.close()

    seen, deduped = set(), []
    for r in all_rows:
        key = (r["source"], r["title"], r["link"])
        if key in seen: continue
        seen.add(key); deduped.append(r)

    out = ROOT / ("data_local.json" if only_local else "data.json")
    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(deduped), "errors": errors, "postings": deduped,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n총 {len(deduped)}건 저장 → {out.name}"
          + (f" (오류 {len(errors)}건)" if errors else ""), flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--local", action="store_true", help="local:true 사이트만 수집 → data_local.json")
    args = ap.parse_args()
    scrape_all(debug=args.debug, only_local=args.local)
