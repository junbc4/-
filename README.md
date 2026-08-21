# 도서관·사서 채용 모아보기

여러 문화재단·구립도서관 사이트에 흩어진 채용 공고를 자동 수집해 한 페이지에서 보여주는 도구.
정적 사이트든 JavaScript로 그려지는 동적(SPA) 사이트든 **실제 브라우저로 렌더링해서 수집**하므로 대상 사이트를 가리지 않는다.

## 구성

```
├── index.html        # 화면 (data.json 을 읽어 표시)
├── data.json         # 수집 결과 (첫 크롤링 실행 시 자동 생성)
├── scrape.py         # Playwright 크롤러 진입점
├── extract.py        # HTML → 공고 추출/분류/마감일 파싱 (테스트 완료)
├── sites.yaml        # 수집 대상 사이트 목록 (여기에 추가만 하면 됨)
├── requirements.txt
└── .github/workflows/scrape.yml   # 하루 3회 자동 수집 + Pages 배포
```

## 동작 방식

```
sites.yaml (대상 목록)
      │
      ▼
scrape.py ──> Playwright(Chromium)로 각 사이트 렌더링
      │            (JS 실행까지 끝난 최종 HTML 확보)
      ▼
extract.py ─> 목록 추출 → '채용'만 분류 → 마감일 파싱 → 중복 제거
      │
      ▼
  data.json  ──>  index.html 이 읽어 화면에 표시
```

## 배포 (무료, 서버 불필요)

1. 이 폴더를 GitHub 저장소로 올린다. (`.github/workflows/scrape.yml` 경로 유지)
2. 저장소 **Settings → Pages → Source: GitHub Actions** 로 설정.
3. **Actions** 탭에서 `scrape-and-deploy` 워크플로를 한 번 수동 실행(Run workflow).
4. 실행이 끝나면 `data.json` 이 생성되고 Pages 주소로 사이트가 뜬다.
5. 이후 하루 3회(한국시간 07·12·18시경) 자동 갱신된다. 주기는 `scrape.yml` 의 `cron` 에서 조정.

## 사이트 추가

`sites.yaml` 에 블록 하나만 추가하면 된다. 대부분 `name` + `url` 로 충분하다.

```yaml
  - name: 동작문화재단
    url: https://www.idfac.or.kr/bbs/board.php?bo_table=job
    topics: [사서, 채용, 기간제]   # (선택) 이 키워드 포함 글만
```

추출이 부정확하면 선택자를 직접 지정한다. 선택자는 아래로 알아낸다.

```bash
pip install -r requirements.txt
python -m playwright install chromium
python scrape.py --debug      # debug/기관명.html 저장 → 목록 행 구조 확인
```

```yaml
  - name: 어떤도서관
    url: https://example.or.kr/notice
    row:   "table.bbs tbody tr"   # 목록 한 줄의 선택자
    title: "td.subject a"
    date:  "td.date"
    wait_for: "table.bbs tbody tr" # SPA면 이 요소가 뜰 때까지 대기
```

## 검증 상태 (정직하게)

| 항목 | 상태 |
|---|---|
| 추출/분류/마감일 파싱 로직 (`extract.py`) | ✅ 픽스처로 테스트 완료 (메뉴 60개+ 섞인 경우 필터 포함) |
| Playwright 렌더링 파이프라인 (`scrape.py`) | ⚠️ 코드는 표준 방식이나 실사이트 실측 미완료 |
| 사이트별 `wait_for`/선택자 | ⚠️ 첫 실행 후 튜닝 필요 |

### "실시간"에 대하여
이 도구는 스스로 실시간으로 도는 게 아니라, GitHub Actions가 정해진 주기(기본 하루 3회)에 크롤러를 실행해 `data.json`을 갱신한다.
"실시간에 가까운 자동 갱신"이며 주기는 `scrape.yml`의 `cron`으로 조정한다.

### 제공된 37개 사이트 현황
- **바로 수집 예상(서버 렌더링)**: 종로·광진·강북·서대문·은평·강남(4관)·강동 도서관, 동작문화재단(채용), dfmc·sscmc·성북·영등포문화재단, 구로·중구·마포·강서 구청 등
- **SPA(Playwright로 처리, 첫 실행 후 튜닝 권장)**: 종로·서초·관악·금천·도봉문화재단, 성동문화재단(채용), 강북 brms 등
- **외부 채용 플랫폼(Incruit, 개별 확인 권장)**: 송파·강동문화재단
- **⚠️ 주소 교체 필요(그대로면 0건)** — 제공된 링크가 메인/폐지 페이지:
  `caci.or.kr/home/ko/main`(메인), `jnfac.or.kr/`(메인), `yslibrary.or.kr/`(메인),
  `nowonarts.kr/.../recruit.php`(**404 폐지 확인됨**) → 실제 목록 주소로 교체
- **중복 제외**: `gwangjinlib .../index.do`, `sdfac .../main.do`, `library.gangnam.go.kr/` (각각 목록 주소로 대체)

> 확인된 사실: 종로구립도서관·광진정보도서관은 **현재 채용글 0건**(휴관·행사 공지뿐).
> 배포 직후 화면이 비어도 오류가 아니라 "현재 해당 기관에 도서관·사서 채용 없음"일 수 있음.

### 기관명 주의
`sscmc / dfmc / l4d / caci / gcfac / gfac / yfac / gbcmc` 등 일부 이름은 도메인 기준 **추정**. `sites.yaml`에서 실제 기관명으로 수정할 것.

- `sites.yaml` 의 `wait_for` 값은 일반적인 게시판 구조를 가정한 추정치다.
  첫 실행에서 수집이 0건이거나 이상하면 `--debug` 로 실제 HTML을 열어 선택자를 맞춰야 한다.
- 채용/공지 분류는 제목 키워드 기반 **휴리스틱**이라 오탐·누락이 있을 수 있다.
  `extract.py` 의 `RECRUIT_KW` / `PROGRAM_KW` 로 조정한다.
- 마감일은 제목 표기에서 추정하며, 연도 미표기 건은 `deadlineEstimated: true` 로 표시된다.

## 대응이 어려운 예외

- 로그인/본인인증이 필요한 게시판
- CAPTCHA가 걸린 페이지
- 목록이 iframe 내부에 있거나 스크롤 단위로만 로드되는 경우 (→ `settle_ms` 상향 또는 별도 처리)
