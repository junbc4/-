"""
extract.py — 렌더링된 HTML에서 공고를 추출하는 순수 로직.
Playwright(브라우저)와 분리되어 있어 픽스처로 단독 테스트 가능.
"""
import re
from datetime import date, datetime
from urllib.parse import urljoin
from bs4 import BeautifulSoup

# ---- 채용 판별 키워드 ----
RECRUIT_KW = ["채용", "공채", "임용", "근로자", "기간제",
              "공무직", "인턴", "위촉", "구인", "충원", "선발"]
# '모집'은 프로그램 모집과 헷갈리므로 별도 취급
AMBIGUOUS_KW = ["모집"]
# '모집'이 이 단어들과 함께 오면 채용이 아니라 프로그램/행사 공지로 본다
PROGRAM_KW = ["참가자", "수강생", "수강", "신청자", "회원", "동아리",
              "자원봉사자", "봉사자", "강좌", "프로그램", "체험", "공모전",
              "작가", "이용자", "독서", "강사 초빙 안내"]


def classify(title: str) -> str:
    """
    제목 기반 채용/공지 분류 (휴리스틱).
    - 명확한 채용 키워드가 있으면 '채용'
    - '모집'만 있고 프로그램성 단어가 섞이면 '공지'로 강등
    ※ 100% 정확하지 않음: 첫 운영 후 오탐/누락 보며 키워드 조정 필요.
    """
    if any(k in title for k in RECRUIT_KW):
        return "채용"
    if any(k in title for k in AMBIGUOUS_KW):
        if any(p in title for p in PROGRAM_KW):
            return "공지"
        return "채용"
    return "공지"


# ---- 도서관/사서 채용 관련성 필터 ----
# '사서'가 다른 단어에 우연히 포함된 오탐(자사서비스, 공사서류 등)
FALSE_SASEO = ["자사서", "공사서", "봉사서", "인사서", "검사서", "조사서",
               "보고서", "심사서", "각서", "계약서", "감사서", "수사서"]
# 채용이 아닌 프로그램/행사/안내성 (도서관/사서가 들어가도 제외)
NONJOB_KW = ["사물함", "서포터즈", "참여자", "수강생", "자원봉사", "봉사자",
             "동아리", "독서회", "공모전", "전시", "강좌", "체험", "회원 모집",
             "이용자", "반납", "휴관", "연장 안내", "중단안내", "신착", "북큐레이션",
             "희망도서", "독서의 달", "이벤트", "안내", "행사", "주차", "DVD",
             "VOD", "리터러시", "인문학", "순회사서",
             "SI 영업", "영업", "풀스택", "개발자", "MARC", "마케팅", "영업직"]
# 이미 끝난 채용의 결과 발표 (지원 불가) → 제외
RESULT_KW = ["합격자", "합격 발표", "합격자 발표", "최종합격", "서류합격",
             "합격자 공고", "면접 대상자", "선정 결과", "결과 발표", "결과발표"]
RECRUIT_ALL = RECRUIT_KW + AMBIGUOUS_KW


def is_library_job(title: str) -> bool:
    """도서관/사서 '채용'글만 True. (프로그램·안내·결과발표·오탐 제외)"""
    t = title
    # 1) 도서관/사서 관련성 (사서 오탐 단어 제거 후 판정)
    t_clean = t
    for f in FALSE_SASEO:
        t_clean = t_clean.replace(f, "")
    if not ("도서관" in t or "사서" in t_clean):
        return False
    # 2) 채용성 키워드 필요
    if not any(k in t for k in RECRUIT_ALL):
        return False
    # 3) 프로그램/행사/안내성 제외
    if any(k in t for k in NONJOB_KW):
        return False
    # 4) 결과 발표 제외 (지원 불가)
    if any(k in t for k in RESULT_KW):
        return False
    return True


DATE_RE = re.compile(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})")
SHORT_DATE_RE = re.compile(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{1,2})\b")


def find_posted(text: str):
    """행 텍스트에서 등록일(YYYY-MM-DD)을 찾아 반환. 없으면 None.
    공고번호(예: 2026-45-1)를 날짜로 오인하지 않도록, 유효한 날짜를 만날 때까지
    모든 후보를 검사한다."""
    for m in DATE_RE.finditer(text):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            continue  # 45월 같은 공고번호는 건너뜀
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            continue
    for m in SHORT_DATE_RE.finditer(text):  # 25.08.03 형태
        yy, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            continue
        y = 2000 + yy if yy < 80 else 1900 + yy
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            continue
    return None


# ---- 마감일 파싱: 제목에 흔히 박히는 한국식 표기 ----
# 예) ~08.03 / ~8/4까지 / ~2.28(토) / ~2026.02.28 / 8/4까지 / ~2.26 12:00 마감
DEADLINE_PATTERNS = [
    re.compile(r"~\s*(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})"),   # ~2026.02.28
    re.compile(r"~\s*(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{1,2}))?"),  # ~08.03 / ~2.28
    re.compile(r"(\d{1,2})[./](\d{1,2})\s*까지"),                  # 8/4까지
]


def parse_deadline(title: str, today: date = None):
    """
    제목에서 마감일을 추정. (title이 곧 마감의 유일한 근거인 경우가 많음)
    반환: (isoDate|None, is_estimate:bool)
    연도 미표기 시 올해로 보되, 이미 지난 날짜면 내년으로 보정(추정).
    """
    today = today or date.today()
    for i, pat in enumerate(DEADLINE_PATTERNS):
        m = pat.search(title)
        if not m:
            continue
        g = m.groups()
        try:
            if i == 0:  # 연도 포함
                y, mo, d = int(g[0]), int(g[1]), int(g[2])
                return date(y, mo, d).isoformat(), False
            elif i == 1:  # ~M.D  또는 ~M.D.??  (첫 두 숫자를 월/일로)
                a, b, c = g
                if c:  # 세 토막이면 앞이 연도 두자리일 수 있으나 드묾 → M/D로 처리
                    mo, d = int(a), int(b)
                else:
                    mo, d = int(a), int(b)
                cand = date(today.year, mo, d)
                if cand < today:
                    cand = date(today.year + 1, mo, d)
                return cand.isoformat(), True
            else:  # M/D까지
                mo, d = int(g[0]), int(g[1])
                cand = date(today.year, mo, d)
                if cand < today:
                    cand = date(today.year + 1, mo, d)
                return cand.isoformat(), True
        except ValueError:
            continue
    return None, False


def _clean(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip()


def extract_with_selectors(soup, base_url, cfg):
    """config에 selector가 있으면 정확 추출."""
    rows = soup.select(cfg["row"])
    out = []
    for r in rows:
        a = r.select_one(cfg.get("link") or "a[href]")
        if not a:
            continue
        title_el = r.select_one(cfg["title"]) if cfg.get("title") else a
        title = _clean(title_el.get_text())
        if not title:
            continue
        href = a.get("href", "")
        link = urljoin(base_url, href) if href else base_url
        posted = None
        if cfg.get("date"):
            de = r.select_one(cfg["date"])
            if de:
                posted = find_posted(de.get_text())
        if not posted:
            posted = find_posted(r.get_text(" ", strip=True))
        out.append({"title": title, "link": link, "posted": posted})
    return out


def _has_date(text):
    return bool(DATE_RE.search(text) or re.search(r"\b\d{1,2}[.\-/]\d{1,2}\b", text))


def extract_generic(soup, base_url):
    """
    설정 없이도 동작하는 일반 추출.
    한국 게시판 대부분인 <tr>/<li> 반복 구조를 노려 제목·링크·날짜를 뽑는다.
    메뉴(nav)를 목록으로 오인하지 않도록, '날짜가 있거나 채용/모집 키워드가
    있는 행'만 후보로 남긴다.
    """
    kw = RECRUIT_KW + AMBIGUOUS_KW
    best = []
    for container_tag in ("tr", "li"):
        items = soup.find_all(container_tag)
        picked = []
        for it in items:
            a = it.find("a", href=True)
            if not a:
                continue
            title = _clean(a.get_text())
            if len(title) < 6 or len(title) > 160:
                continue
            if re.fullmatch(r"[\d\s<>«»‹›]+", title):
                continue
            row_text = it.get_text(" ", strip=True)
            # 핵심 필터: 날짜가 있거나 채용 키워드가 있는 행만
            if not (_has_date(row_text) or any(k in title for k in kw)):
                continue
            href = a["href"]
            link = urljoin(base_url, href) if not href.startswith(("javascript:", "#")) else base_url
            posted = find_posted(row_text)
            picked.append({"title": title, "link": link, "posted": posted})
        if len(picked) > len(best):
            best = picked

    # 폴백: 표/리스트로 못 잡으면 div/article 카드형 목록을 시도(성동류 SPA)
    if len(best) < 2:
        card_picked = []
        for a in soup.find_all("a", href=True):
            title = _clean(a.get_text())
            if len(title) < 6 or len(title) > 160:
                continue
            if re.fullmatch(r"[\d\s<>«»‹›]+", title):
                continue
            # a 주변 3단계 조상 블록에서 날짜 탐색
            block = a
            ctx_text = ""
            for _ in range(3):
                block = block.parent
                if block is None:
                    break
                ctx_text = block.get_text(" ", strip=True)
                if _has_date(ctx_text):
                    break
            if not (_has_date(ctx_text) or any(k in title for k in kw)):
                continue
            href = a["href"]
            link = urljoin(base_url, href) if not href.startswith(("javascript:", "#")) else base_url
            card_picked.append({"title": title, "link": link, "posted": find_posted(ctx_text)})
        if len(card_picked) > len(best):
            best = card_picked

    seen, dedup = set(), []
    for p in best:
        if p["title"] in seen:
            continue
        seen.add(p["title"])
        dedup.append(p)
    return dedup


def extract(html, base_url, source, cfg=None, today=None):
    """
    최종 진입점. 한 사이트의 HTML을 받아 공고 리스트(dict)를 반환.
    cfg에 selector가 있으면 정확 추출, 없으면 generic.
    """
    today = today or date.today()
    soup = BeautifulSoup(html, "html.parser")
    cfg = cfg or {}
    if cfg.get("row"):
        raw = extract_with_selectors(soup, base_url, cfg)
    else:
        raw = extract_generic(soup, base_url)

    # 전역 필터: 도서관/사서 '채용'글만 남김 (프로그램·안내·결과발표·오탐 제외)
    results = []
    for r in raw:
        title = r["title"]
        if not is_library_job(title):
            continue
        category = classify(title)
        deadline, est = parse_deadline(title, today)
        results.append({
            "source": source,
            "sourceUrl": base_url,
            "title": title,
            "category": category,
            "posted": r["posted"],
            "deadline": deadline,
            "deadlineEstimated": est,
            "link": r["link"],
        })
    return results
