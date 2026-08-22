"""
scrape.py — 사이트별 브라우저 렌더링 후 공고 수집.

기능:
 - 사이트마다 새 탭을 열고 닫아 이동 충돌 방지
 - 로딩 실패/내용 부족 시 더 오래 기다려 재시도
 - proxy: true 사이트는 '한국 공개 프록시'를 경유해 접속(해외 IP 차단 우회)
   · 공개 프록시 목록을 실행 시 자동 수집
   · 프록시로 실패하면 프록시 없이 한 번 더 시도(안전장치)

사용:
    python scrape.py          # data.json 생성
    python scrape.py --debug  # 렌더링 HTML을 debug/ 에 저장
"""
import sys
import json
import argparse
import urllib.request
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

# 차단 안내 판별용(프록시 통과 실패 감지)
BLOCK_HINTS = ["해외에서의", "접속을 제한", "보안 정책에 따라"]


def fetch_kr_proxies(limit=15):
    """공개된 한국(KR) 프록시 목록을 여러 소스에서 수집. 실패해도 빈 리스트."""
    sources = [
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    ]
    # 위 소스는 국가 구분이 없어 전부 시도용. 국가필터 소스도 추가:
    kr_sources = [
        "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/countries/KR/data.txt",
    ]
    proxies = []
    # 1) KR 전용 소스 우선
    for url in kr_sources:
        try:
            data = urllib.request.urlopen(url, timeout=15).read().decode("utf-8", "ignore")
            for line in data.splitlines():
                line = line.strip()
                # 형식: http://ip:port  또는 ip:port
                if not line:
                    continue
                if line.startswith("http"):
                    proxies.append(line)
                elif ":" in line:
                    proxies.append("http://" + line)
        except Exception as e:
            print(f"[proxy] KR 소스 실패: {e}", flush=True)
    # 중복 제거, 상한
    seen, uniq = set(), []
    for p in proxies:
        if p in seen:
            continue
        seen.add(p)
        uniq.append(p)
        if len(uniq) >= limit:
            break
    print(f"[proxy] 한국 프록시 {len(uniq)}개 확보", flush=True)
    return uniq


def _is_blocked(html):
    return any(h in (html or "") for h in BLOCK_HINTS)


def _attempt(browser, site, wait_until, goto_timeout, settle_ms, proxy=None):
    """1회 렌더링 시도. proxy 지정 시 그 프록시로 새 컨텍스트를 만든다."""
    ctx_args = dict(
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"),
        locale="ko-KR",
    )
    if proxy:
        ctx_args["proxy"] = {"server": proxy}
    ctx = browser.new_context(**ctx_args)
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
        try:
            page.close()
        except Exception:
            pass
        try:
            ctx.close()
        except Exception:
            pass


def _text_len(html):
    try:
        return len(BeautifulSoup(html, "html.parser").get_text(strip=True))
    except Exception:
        return len(html or "")


def render_html(browser, site, kr_proxies):
    base_settle = site.get("settle_ms", 1500)

    # 프록시 대상 사이트: 한국 프록시를 순서대로 시도
    if site.get("proxy") and kr_proxies:
        for i, px in enumerate(kr_proxies):
            html = _attempt(browser, site, "domcontentloaded", 30000, base_settle, proxy=px)
            if _text_len(html) >= 200 and not _is_blocked(html):
                print(f"[proxy] {site['name']}: 프록시 #{i+1} 성공", flush=True)
                return html
            # 최대 8개까지만 시도(시간 절약)
            if i >= 7:
                break
        print(f"[proxy] {site['name']}: 프록시 모두 실패 → 직접 접속 시도", flush=True)

    # 일반 접속(또는 프록시 실패 후 폴백)
    html = _attempt(browser, site, "domcontentloaded", 35000, base_settle)
    if _text_len(html) < 200 or _is_blocked(html):
        html2 = _attempt(browser, site, "networkidle", 60000, base_settle + 3000)
        if _text_len(html2) > _text_len(html):
            html = html2
    return html


def scrape_all(debug=False):
    cfg = yaml.safe_load(SITES_FILE.read_text(encoding="utf-8"))
    sites = cfg.get("sites", [])
    today = date.today()
    all_rows, errors = [], []

    # proxy 대상이 하나라도 있으면 프록시 목록 준비
    need_proxy = any(s.get("proxy") for s in sites)
    kr_proxies = fetch_kr_proxies() if need_proxy else []

    if debug:
        DEBUG_DIR.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for site in sites:
            name = site.get("name", site["url"])
            try:
                html = render_html(browser, site, kr_proxies)
                if debug:
                    safe = "".join(c if c.isalnum() else "_" for c in name)
                    (DEBUG_DIR / f"{safe}.html").write_text(html or "", encoding="utf-8")
                rows = extract(html=html or "", base_url=site["url"],
                               source=name, cfg=site, today=today)
                blocked = " (차단감지)" if _is_blocked(html) else ""
                print(f"[OK] {name}: {len(rows)}건{blocked}", flush=True)
                all_rows.extend(rows)
            except Exception as e:
                msg = str(e).splitlines()[0]
                print(f"[ERR] {name}: {msg}", flush=True)
                errors.append({"source": name, "error": msg})
        browser.close()

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
          + (f" (오류 {len(errors)}건)" if errors else ""), flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    scrape_all(debug=args.debug)
