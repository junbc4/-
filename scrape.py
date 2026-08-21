"""
scrape.py — 사이트별로 실제 브라우저를 띄워 렌더링한 뒤 공고를 수집한다.
Playwright(Chromium)로 정적/동적(SPA) 모두 대응.

수정판: 사이트마다 새 페이지(탭)를 열고 닫아 '이동 충돌'을 제거.
       goto가 리다이렉트/지연으로 실패해도 현재 내용으로 시도하고,
       한 사이트 오류가 다른 사이트에 영향 주지 않도록 격리.

사용:
    python scrape.py           # sites.yaml 읽어 data.json 생성
    python scrape.py --debug   # 각 사이트 원본 HTML을 debug/ 에 저장
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


def render_html(ctx, site):
    """
    사이트마다 '새 페이지'를 열어 렌더링 후 HTML 반환하고 페이지를 닫는다.
    - goto가 리다이렉트/지연으로 예외를 던져도 현재 페이지 내용으로 진행
    - 새 탭을 쓰므로 앞 사이트의 늦은 리다이렉트가 다음 사이트를 방해하지 않음
    """
    page = ctx.new_page()
    try:
        try:
            page.goto(site["url"], wait_until="domcontentloaded", timeout=40000)
        except Exception:
            pass  # 리다이렉트/타임아웃이어도 현재 내용으로 시도

        wait_for = site.get("wait_for")
        if wait_for:
            try:
                page.wait_for_selector(wait_for, timeout=12000)
            except Exception:
                pass
        else:
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

        page.wait_for_timeout(site.get("settle_ms", 1200))
        try:
            return page.content()
        except Exception:
            return ""
    finally:
        try:
            page.close()
        except Exception:
            pass


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

        for site in sites:
            name = site.get("name", site["url"])
            try:
                html = render_html(ctx, site)
                if debug:
                    safe = "".join(c if c.isalnum() else "_" for c in name)
                    (DEBUG_DIR / f"{safe}.html").write_text(html or "", encoding="utf-8")

                rows = extract(
                    html=html or "",
                    base_url=site["url"],
                    source=name,
                    cfg=site,
                    today=today,
                )
                print(f"[OK] {name}: {len(rows)}건", flush=True)
                all_rows.extend(rows)
            except Exception as e:
                # 한 줄 요약만 (긴 call log 생략)
                msg = str(e).splitlines()[0]
                print(f"[ERR] {name}: {msg}", flush=True)
                errors.append({"source": name, "error": msg})

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
        # 항상 현재 시각 기록 → 실행할 때마다 '확인 시각'이 갱신됨
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(deduped),
        "errors": errors,
        "postings": deduped,
    }
    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n총 {len(deduped)}건 저장 → {OUT_FILE.name}"
          + (f" (오류 {len(errors)}건)" if errors else ""), flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true", help="원본 HTML을 debug/ 에 저장")
    args = ap.parse_args()
    scrape_all(debug=args.debug)
