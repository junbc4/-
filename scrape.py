"""
scrape.py — 사이트별로 실제 브라우저를 띄워 렌더링한 뒤 공고를 수집한다.
Playwright(Chromium)를 쓰므로 정적/동적(SPA)/숨은 API 여부와 무관하게 동작한다.

사용:
    python scrape.py            # sites.yaml 읽어 data.json 생성
    python scrape.py --debug    # 각 사이트 원본 HTML을 debug/ 에 저장(선택자 튜닝용)
"""
import sys
import json
import argparse
from datetime import date, datetime, timezone
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

from extract import extract

ROOT = Path(__file__).resolve().parent
SITES_FILE = ROOT / "sites.yaml"
OUT_FILE = ROOT / "data.json"
DEBUG_DIR = ROOT / "debug"


def render_html(page, site):
    """한 사이트를 열고, 목록이 채워질 때까지 기다린 뒤 최종 HTML을 반환."""
    url = site["url"]
    page.goto(url, wait_until="domcontentloaded", timeout=45000)

    # SPA 대응: 목록 selector가 지정돼 있으면 그게 나타날 때까지 대기
    wait_for = site.get("wait_for")
    if wait_for:
        try:
            page.wait_for_selector(wait_for, timeout=20000)
        except Exception:
            pass  # 못 찾아도 일단 현재 HTML로 진행(로그로 확인 가능)
    else:
        # selector 미지정 시 네트워크가 잠잠해질 때까지 대기
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

    # 무한스크롤/지연로딩 사이트를 위한 여유
    page.wait_for_timeout(site.get("settle_ms", 1200))
    return page.content()


def scrape_all(debug=False):
    cfg = yaml.safe_load(SITES_FILE.read_text(encoding="utf-8"))
    sites = cfg.get("sites", [])
    today = date.today()
    all_rows, errors = [], []

    if debug:
        DEBUG_DIR.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0 Safari/537.36"),
            locale="ko-KR",
        )
        page = ctx.new_page()

        for site in sites:
            name = site.get("name", site["url"])
            try:
                html = render_html(page, site)
                if debug:
                    safe = "".join(c if c.isalnum() else "_" for c in name)
                    (DEBUG_DIR / f"{safe}.html").write_text(html, encoding="utf-8")

                rows = extract(
                    html=html,
                    base_url=site["url"],
                    source=name,
                    cfg=site,          # row/title/link/date/topics 등 있으면 사용
                    today=today,
                )
                print(f"[OK] {name}: {len(rows)}건")
                all_rows.extend(rows)
            except Exception as e:
                print(f"[ERR] {name}: {e}", file=sys.stderr)
                errors.append({"source": name, "error": str(e)})

        browser.close()

    # 중복 제거 (source+title+link)
    seen, deduped = set(), []
    for r in all_rows:
        key = (r["source"], r["title"], r["link"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)

    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(deduped),
        "errors": errors,
        "postings": deduped,
    }
    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n총 {len(deduped)}건 저장 → {OUT_FILE.name}"
          + (f" (오류 {len(errors)}건)" if errors else ""))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true", help="원본 HTML을 debug/ 에 저장")
    args = ap.parse_args()
    scrape_all(debug=args.debug)
