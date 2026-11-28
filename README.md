# 집핏 프로토타입

## 전체 기능 상세표 (52개)

기능 번호는 기존 표를 따릅니다. 41번은 기존과 같이 비워 두었으며, 생애주기·운영 백업·예측 성능 기능을 50~53번에 추가했습니다.

- **구현**은 코드와 호출 경로가 있다는 뜻입니다. 운영 환경의 활성화·적재·검증 여부는 별도로 표시합니다.
- **로컬 구현 · GitHub 반영 전**은 운영 PC의 개발 브랜치에 구현되어 있으며 공개 저장소와 배포에는 별도 코드 반영이 필요한 기능입니다.
- **부분 구현 · 운영 절차 보류**는 일부 도구가 있으며 남은 절차가 보류된 기능입니다.

### A. 데이터베이스·AI 예측 핵심 기능 (1~13)

| 번호 | 기능 | 상세 내용 | 상태 |
|---:|---|---|---|
| 1 | 단지 정보 저장 | 단지명·주소·준공연도·좌표는 단지 테이블에, 면적군은 연결 테이블에 저장합니다. | **구현** |
| 2 | 실거래 내역 저장 | 계약일·금액·면적·층·해제 여부 등의 거래 원본 및 상태 보관 | **구현** |
| 3 | 면적군 연결 | 단지별 전용면적군을 실거래 및 대표면적에 연결 | **구현** |
| 4 | 모델 입력 생성 | 기준월 M의 M−19~M−2 거래와 공표된 금리·지역 가격지수로 입력을 만듭니다. 취소 및 학습 제외 거래는 빼고, 최근 12개월 거래가 3건 이상인 면적군을 대상으로 합니다. | **구현** |
| 5 | 운영 모델 로드 | FastAPI가 시작할 때 `backend/joint_model.joblib`을 로드하고 서울 25개 구가 모두 포함됐는지 검증합니다. | **구현** |
| 6 | 단지 상세 API | `GET /api/complex`: 주소·준공연도·면적군·거래 동기화 시점 제공 | **구현** |
| 7 | 실거래 조회 API | `GET /api/trades`: 선택 단지·면적군별 거래 내역·해제 상태 제공 | **구현** |
| 8 | 실시간 예측 API | `GET /api/predict`: 최신 모델 입력을 기반으로 3·6·12개월 예상가격·수익률 계산 | **구현** |
| 9 | React 단지·면적군 선택 | 지도·목록에서 단지를 선택하고 전용면적군을 변경해 조회 | **구현** |
| 10 | 실거래·예측 결과 표시 | 기준가격·전망가격·수익률·거래 내역을 화면에 제공 | **구현** |
| 11 | 로딩·오류·예측 불가 처리 | 요청 중·조회 실패·모델 입력 부재·재시도 상태 분리 | **구현** |
| 12 | 예측 재현·서비스 검증 | 동봉 기준값 또는 현재 DB와 모델로 API 예측값을 대조하고, 서비스 검사에서 거래·전망·동기화 시점을 확인합니다. | **구현** |
| 13 | 광진구 초기 데이터 적재 | 초기 단지·면적군·실거래·예측 입력을 DB로 적재하는 체계 | **구현** |

### B. 검색·지도·그래프·단지 비교 (14~20)

| 번호 | 기능 | 상세 내용 | 상태 |
|---:|---|---|---|
| 14 | 단지 목록 API | `GET /api/complexes`: 자치구·면적군·예측 가능 여부·정렬 정보 반환 | **구현** |
| 15 | 단지 검색 | 단지명·주소 검색과 자치구·전체 서울 범위 선택 | **구현** |
| 16 | 필터·정렬 | 가격·면적·준공연도·예측 가능 여부·거래건수·최근 거래·예상 상승률 필터/정렬 | **구현** |
| 17 | 지도 마커 표시 제어 | 네이버 지도·화면영역 기준 마커·20/100/전체 표시·클러스터링 | **구현** |
| 18 | 지도·목록·상세 연동 | 지도/목록 선택과 상세·실거래·예측 표시 동기화 | **구현** |
| 19 | 실거래·전망 그래프 | 월별 거래 중앙값·누적 변동률·3/6/12개월 전망·서울 HGB 과거 홀드아웃 참고 음영 | **구현(참고범위)** |
| 20 | 단지 비교 | 최대 4개 단지의 실거래·예상 차익·수익률·시설 정보를 동일 화면에서 비교 | **구현** |

### C. 데이터 자동 갱신·모델 성능·배포 (21~29)

| 번호 | 기능 | 상세 내용 | 상태 |
|---:|---|---|---|
| 21 | 국토부 실거래 수집 | 국토부 API에서 자치구·계약월별 거래를 페이지 단위로 수집·검증해 적재합니다. 별도 과거 적재 도구는 구·월별 완료 지점에서 재개할 수 있습니다. | **구현** |
| 22 | 중복·정정·해제 반영 | 같은 조건의 거래 건수를 유지합니다. 취소·정정을 반영하고 대체된 원본을 이력에 남기며, 근거가 불명확한 누락·정정은 검토 대기에 둡니다. | **구현** |
| 23 | 일일 자동 실행 | 매일 06:00 KST 거래·좌표·개별 승인·미반영 생애주기 묶음·시설을 처리합니다. 실패는 10분 간격으로 최대 3회 재시도합니다. | **구현** |
| 24 | 모델 입력 갱신 | 거래·금리·지수로 구별 최신 기준월 입력을 게시합니다. 게시 장애는 체크포인트와 대기열로 재개합니다. | **구현** |
| 25 | 데이터 기준 시점 표시 | 거래 동기화 시간·모델 입력 기준월·예측 계산시점 제공 | **구현** |
| 26 | 예측 오차 진단 | 과거 평가 자료에서 기간별 MAE·편향을 보고, 서울 홀드아웃의 구·월별 오차·방향 적중률·평가 범위를 분석합니다. 운영 예측 추적은 53번을 참조하세요. | **구현** |
| 27 | 후보 모델 비교 검증 | 서울 통합·광진구 전이·가격 유지 등 비교 모델을 홀드아웃 표본에서 평가합니다. 운영 승격 관리는 28번을 참조하세요. | **구현** |
| 28 | 검증된 모델 교체 관리 | 후보 학습·홀드아웃 평가·API 검증 도구가 있습니다. 후보는 별도 파일로 저장하고, 운영 모델을 로드할 때 25개 구 범위를 검증합니다. 자동 승격·운영 롤백 절차는 보류 상태입니다. | **부분 구현 · 운영 절차 보류** |
| 29 | Docker 통합·배포 | 운영 PC의 PostgreSQL·FastAPI·Nginx·갱신기를 Compose로 구성하고, 공개 Pages Worker는 터널을 통해 FastAPI로 연결합니다. | **구현** |

### D. 회원·개인화 기능 (30~34)

| 번호 | 기능 | 상세 내용 | 상태 |
|---:|---|---|---|
| 30 | 회원가입·로그인·로그아웃 | 비밀번호 해시·세션 쿠키·로그인 시도 제한 등을 갖춘 계정 기능 | **구현** |
| 31 | 관심 단지 저장 | 사용자별 관심 단지와 면적군 저장·해제·재조회 | **구현** |
| 32 | 비교 조합 저장 | 서로 다른 2개 단지/면적군·선택 전망기간 조합 저장·복원 | **구현** |
| 33 | 개인 메모 | 단지별 메모와 저장한 비교 조합의 메모 관리 | **구현** |
| 34 | 관심 단지 갱신 알림 | 새 실거래·새 예측 기준월을 회원 화면에서 알림(이메일/푸시 아님) | **구현** |

### E. 서비스 지역 확장 (35)

| 번호 | 기능 | 상세 내용 | 상태 |
|---:|---|---|---|
| 35 | 서울 25개 구 확장 | 광진구 중심 서비스에서 서울 전체 실거래·지도·검색·예측·검증으로 확장 | **구현** |

### F. 추가 부동산·생활 정보 (36~39)

| 번호 | 기능 | 상세 내용 | 상태 |
|---:|---|---|---|
| 36 | 단지 총세대수 | 공개 공동주택 자료를 단지 식별·좌표로 연결해 세대수 표시; 월간 updater가 공공자료를 DB에 갱신 | **구현(매칭 단지)** |
| 37 | 세대당 주차대수 | 총 주차대수 ÷ 세대수 계산·비교·필터; 월간 updater가 공공자료를 DB에 갱신 | **구현(매칭 단지)** |
| 38 | 가까운 지하철역 | 역명·호선·좌표 간 직선거리·지도 및 거리 필터; 월간 updater가 공공자료를 DB에 갱신 | **구현(자료 보유 범위)** |
| 39 | 주변 학교 정보 | 초·중·고별 학교명·직선거리·지도·학교급별 필터; 월간 updater가 공공자료를 DB에 갱신 | **구현(자료 보유 범위)** |

### G. 운영·데이터 검증·최적화 (40, 42~53)

| 번호 | 기능 | 상세 내용 | 상태 |
|---:|---|---|---|
| 40 | 서울 단지 좌표 수집·저장 | 기존 좌표 자료를 보존하고 신규·주소 변경 단지의 좌표를 검증 자료 또는 네이버 지오코딩으로 보완합니다. 좌표가 없는 단지도 목록·거래로 조회할 수 있습니다. | **구현** |
| 42 | 단지 지도 마커 표시 | 현재 지도 범위에서 유효한 좌표와 실거래 표시가격이 있는 단지를 표시합니다. 확정 멸실 단지는 마커에서 제외합니다. | **구현** |
| 43 | 과거 거래 보존 | 멸실 확정 후에도 기존 단지 ID와 거래 이력을 조회할 수 있습니다. 거래 정정 시에는 원본을 정정 이력에 보관합니다. | **구현** |
| 44 | 공개 조회 JSON 스냅샷 | 단지·최근 거래·전체 거래 이력을 검증된 읽기 전용 스냅샷으로 내보낼 수 있습니다. 현재 화면의 목록·거래·시설 조회는 실시간 API를 사용합니다. | **선택 도구 구현** |
| 45 | JSON 스냅샷 배포·복원 | 해시 객체·버전 매니페스트를 사용하는 선택적 R2 배포와 롤백 도구가 있습니다. 기본 Compose 갱신기에 연결된 경로는 아닙니다. | **선택 도구 구현 · 운영 미확인** |
| 46 | React/지도 조회 최적화 | 자치구별 요청·목록 페이지네이션·영역/줌 기준 마커·중복 렌더 절감 | **코드 구현** |
| 47 | 단지·시설 정보 정기 갱신 | 매월 1일 06:00 KST에 K-apt 점검·검증 메타데이터·시설 원자료를 갱신합니다. 일간 작업은 신규 검증 파일·좌표와 단지별 시설 계산을 반영합니다. 역·학교 갱신에는 출처별 키가 필요합니다. | **구현 · 출처별 설정 필요** |
| 48 | 서울 통합 HGB 홀드아웃 검증 | 2025-04 학습 검증 모델의 2025-04~09 홀드아웃에서 오차를 집계하고, 3·6·12개월 오차의 10·90백분위를 참고용 JSON으로 제공합니다. | **검증 집계 포함** |
| 49 | GitHub에서 Cloudflare Pages 배포 | main 푸시 또는 수동 실행으로 React·Worker·홀드아웃 집계 JSON을 Pages에 배포합니다. 백엔드 이미지는 운영 PC에서 별도로 갱신합니다. | **워크플로 구현** |
| 50 | 월간 생애주기 판정·반영 | 전환 후 자동 실행을 켜면 매월 1일 06:00 KST에 대장과 기준 자료를 대조해 존치·멸실·보류를 기록합니다. 일간 갱신은 미반영 묶음을 다시 처리합니다. [운영 안내](docs/lifecycle-monthly.md) | **구현 · 자동 실행 설정 필요** |
| 51 | 운영 백업·복원 | 계정을 포함한 DB 전체와 생애주기 자료·검증 메타데이터·환경 설정을 같은 백업에 묶습니다. 월간 판정 잠금을 확인하고 완료된 백업만 최종 폴더명으로 바꿉니다. | **로컬 구현 · GitHub 반영 전** |
| 52 | 예측 성능 화면·집계 API | 머리글의 예측 성능 화면과 `GET /api/performance`에서 과거 홀드아웃 집계와 운영 예측 추적을 확인할 수 있습니다. 과거 지표는 평가 결과를 가져온 뒤 표시합니다. | **로컬 구현 · GitHub 반영 전** |
| 53 | 운영 예측 기록·실제 결과 채점 | 일간 작업에서 각 면적군의 전망을 기준월별로 저장하고, 목표기간과 거래 신고 여유 2개월이 지나면 실제 결과를 채점합니다. | **로컬 구현 · GitHub 반영 전** |

**19번 참고범위 주의:** 서울 통합 HGB **2025년 4월 학습 검증 모델**의 2025년 4~9월 홀드아웃 잔차를 활용한 참고 음영입니다. 최신 운영 모델의 신뢰구간 또는 80% 포함확률을 보장하지 않습니다. 집계 JSON: `frontend/public/metadata/seoul-forecast-reference.json`.

**지도 표시 기준:** 좌표와 유효한 실거래 표시가격이 있는 단지를 표시합니다. 과거 실거래 내역은 보존합니다.

**남은 운영 확인:** 28번 모델 승격·롤백 절차, 선택적 스냅샷 운영 여부, 시설·생애주기 설정을 확인합니다. 51~53번은 로컬 개발 브랜치의 코드 반영 후 운영 환경에서 확인합니다. 상세 근거는 `docs/feature19-forecast-reference.md`, `docs/json-stage4-6.md`, `backend/facilities_seed/README.md` 참고.

## 로드맵 5단계 · 공개 서비스 통합 검증

팀원 PC에서는 `git clone https://github.com/july0718/jipfit.git` 후 `cd jipfit`으로 이동해서 아래의 광진구 지도 버전 실행 절차를 따릅니다. 팀원 PC는 자체 DB와 로컬 화면으로 확인하고, 다음 공개 검사는 운영 DB를 사용하는 **서버 PC**에서 실행합니다. 프론트엔드는 main 푸시 후 GitHub Actions가 기존 Pages 프로젝트로 배포합니다.

```powershell
docker compose up -d --build --wait api web
docker compose exec api python verify_service.py http://web
docker compose exec api python verify_service.py https://jipfit-48v.pages.dev
docker compose logs --tail 30 updater
```

검사기는 화면 파일·비로그인 응답·지도 좌표·전체 면적군 수, 예측 가능/불가 면적군 각각의 거래 이력, 3·6·12개월 API 전망, DB의 최근 수집 성공 시각과 상세 API 시각의 일치를 확인합니다. 수집을 실행하지 않은 신규 DB에서는 시각 대조를 완료할 수 없습니다. 운영 로그에서 가장 최근 예정된 한국시간 06시 작업이 성공했는지도 확인합니다.

공개 화면에서는 별도의 검수 계정으로 회원가입 후 지도에서 단지를 선택하고, 다른 단지까지 비교에 담아 면적군과 전망 기간을 지정합니다. 관심 단지·비교 조합·메모를 저장하고 로그아웃한 뒤 재로그인합니다. 저장 조합을 열어 두 면적군·기간·메모가 복원되는지 확인합니다. 상세 화면의 거래 동기화 시각은 한국시간으로 표시되므로 검사 결과와 같은 시점인지 비교합니다.

## 광진구 지도 버전 실행

Docker Desktop을 실행한 뒤 VSCode에서 compose.yaml이 들어 있는 jipfit 폴더를 엽니다. 처음 받은 폴더에서는 아래 명령으로 .env를 준비하고 POSTGRES_PASSWORD에 본인의 로컬 DB 비밀번호를 입력해 저장합니다.

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }
code .env
```

```powershell
docker compose up -d --build --wait web
```

기존 1단지 버전의 Docker 볼륨을 처음 확장할 때는 아래 명령으로 광진구 자료를 추가합니다. 새 DB는 최초 시작 시 자동으로 적재합니다.

```powershell
docker compose exec -T db psql -U jipfit -d jipfit -f upgrade.sql
```

http://127.0.0.1:5173 에서 현대프라임을 검색하고 60㎡ 면적군을 선택합니다. 2026-11 샘플은 기준가 169,000만 원과 3·6·12개월 예상 매매가 187,465만 원, 197,398만 원, 212,545만 원을 표시합니다. 선택할 때마다 DB 입력으로 다시 추론합니다.
## 2단계 · PostgreSQL 샘플 DB
Docker Desktop을 실행하고 이 폴더에서 다음 명령을 실행합니다. 최초 설치 시 .env.example을 .env로 복사하고 POSTGRES_PASSWORD에 로컬 비밀번호를 설정합니다. 이미 .env가 있으면 그대로 사용합니다.

```powershell
docker compose up -d --wait db
docker compose logs db
docker compose exec db psql -U jipfit -d jipfit -c "SELECT count(*) FROM trades;"
docker compose exec db psql -U jipfit -d jipfit -c "SELECT month, area_group, count12 FROM model_inputs;"
```

pgAdmin 또는 DataGrip 접속 정보는 127.0.0.1, 포트 55432, DB와 사용자 이름은 모두 jipfit입니다. 데이터는 Docker 볼륨에 보존됩니다. 초기화 SQL은 빈 볼륨의 첫 시작 시 실행됩니다.

현대프라임 60㎡ 면적군(대표면적 59.82㎡)의 정제 실거래 972건과 2026-11 모델 입력 1행을 담았습니다. 거래금액 단위는 만원입니다. 출처는 stage7_updated의 clean_transactions.csv, latest_model_inputs.csv, web/data.json, web/coordinates.json입니다. 좌표는 기존 지도 표시 위치의 근삿값입니다.

backend/db/init.sql에서 스키마와 적재 순서를, backend/db/sample에서 입력 자료를 확인할 수 있습니다.


## 3단계 · FastAPI 실시간 예측

프로젝트 루트에서 API를 빌드하고 실행합니다.

```powershell
docker compose up -d --build --wait api
Invoke-RestMethod http://127.0.0.1:8000/api/complexes
```

브라우저에서 http://127.0.0.1:8000/docs 를 열고 GET /api/predict의 Try it out을 선택합니다. complex_id에 서울특별시 광진구 구의동|631-1|현대프라임, area_group에 60을 넣고 Execute를 누릅니다.

main.py는 요청마다 선택한 단지·면적군의 최근 월 입력을 PostgreSQL에서 조회합니다. inference.py는 서버 시작 시 로드한 학습 모델로 기간별 예측을 계산합니다. 기준가는 DB의 로그 ㎡당 기준가를 원래 단위로 환산해 대표면적과 곱한 값입니다. 현재 샘플은 2026년 11월 입력이며, 입력 갱신은 별도 단계입니다.

## 4단계 · React 예측 화면

프로젝트 루트에서 실행합니다.

```powershell
docker compose up -d --build --wait web
```

http://127.0.0.1:5173 에서 현대프라임 60㎡ 면적군을 선택하면 기준가와 기간별 예측을 확인할 수 있습니다. 브라우저 개발자 도구의 Network에서 단지를 누를 때마다 새로 전송되는 /api/predict 요청을 확인할 수 있습니다. 화면은 frontend/app.jsx, 스타일은 frontend/style.css에 있습니다. 웹 컨테이너는 /api/ 요청을 FastAPI로 전달합니다.


## 전체 로드맵 4단계 · 광진구 지도

2026-11 스냅샷에는 253개 단지의 주소 좌표, 734개 면적군, 실거래 29,036건이 있습니다. 76개 단지의 110개 면적군에 모델 입력이 있습니다. 단지 또는 마커를 선택하면 예측 가능한 첫 면적군으로 요청하고, 면적을 변경하면 다시 계산합니다. 월 입력이 없는 경우 화면에 사유를 표시합니다.

네이버 지도는 frontend/config.js의 브라우저용 Client ID를 사용합니다. 로컬 주소에서 지도 인증이나 네트워크 연결이 실패하면 좌표 위치도와 단지 목록에서 계속 선택할 수 있습니다. 좌표는 기존 지도에서 복원한 주소 위치입니다.

backend/db/gwangjin은 기존 광진구 자료에서 추출한 단지·면적군·실거래·입력 CSV입니다. 예측 면적군에는 원모델의 대표면적을, 나머지에는 해당 면적군 실거래 면적의 중앙값을 사용합니다. upgrade.sql은 하나의 트랜잭션으로 실행하며, 재실행해도 같은 거래를 추가 적재하지 않습니다.

## 5단계 · 실거래 갱신과 검증

공공데이터포털의 국토교통부 아파트 매매 실거래가 자료(15126469) 활용 신청 후 로컬 .env의 MOLIT_SERVICE_KEY에 일반 인증키(Decoding)를 저장합니다. 광진구 데이터가 준비된 상태에서 실행합니다.

```powershell
docker compose --profile updates up -d --build --wait web updater
docker compose exec api python validate.py
docker compose logs --tail 30 updater
docker compose exec db psql -U jipfit -d jipfit -c "SELECT * FROM refresh_runs ORDER BY day DESC LIMIT 5;"
```

갱신 서비스는 최초 기동 시 당일 작업을 시도하고 이후 매일 한국시간 06시에 실행합니다. 컨테이너가 실행 중이어야 합니다.
- **일일 실거래·신규 단지·좌표·개별 승인(`new-*.json`)**: 매일 KST 06:00 슬롯에서 최근 계약월 4개(`M-3 … M`)와 순환 과거월(구별 하루 1개 계약월), 미좌표·주소변경 대기 단지(회차당 최대 50개) 및 `new-*.json` 승인 파일을 처리하고, 마지막에 단지별 시설(`complex_facilities`)을 다시 계산합니다. 일시 장애 시 10분 간격으로 최대 3회(최초 포함 최대 4회) 재시도하며, 같은 슬롯에서 이미 완료된 구·계약월(`daily_trade_month_checkpoints`)과 처리된 주소는 건너뜁니다.
- **월간 작업 순서**: ① K-apt 기본정보 점검 → ② `verified-metadata/` 반영 → ③ 시설 공공자료 갱신 → ④ 멸실·재건축 판정입니다. ③이 실패해도 ④는 실행합니다.
- **시설 공공자료(세대수·주차·역·학교)**: updater를 시작할 때 DB 표가 비어 있으면 `backend/facilities_seed/`의 초기 사본을 적재합니다. 월간 작업은 출처별 공공자료를 받아 DB를 갱신합니다. 역·학교는 `.env`의 `TDATA_SUBWAY_API_KEY`, `NEIS_API_KEY`가 필요하고, 키가 없으면 저장된 자료를 유지합니다. 바로 갱신하는 방법은 [시설 자료 안내](backend/facilities_seed/README.md)를 따릅니다.
- **월간 K-apt 기본정보 점검·멸실·재건축 판정**: 매월 첫 가용 KST 06:00 정기 슬롯에서 구별 기존 `kapt_verified_links`를 보존한 채 이름·주소·준공연도 변경 후보(`complex_metadata_candidates`)를 탐지한 뒤, `JIPFIT_LIFECYCLE_AUTO=1`이면 모든 단지의 존치를 판정해 반영합니다. 일시 장애 시 30분 간격으로 최대 3회(최초 포함 최대 4회) 재시도하며, 날이 바뀌거나 재기동되어도 같은 달의 시도 횟수는 초기화되지 않습니다. 한도 소진 또는 미확정 후보(`EMPTY_DETAIL`, 후보 보류)는 다음 월간 정기 점검에서 새 자료와 다시 대조합니다.
- **보류 조회·승인·재개 방법**:
  - 단지 식별 보류: `SELECT * FROM refresh_identity_pending WHERE status IN ('pending', 'invalid_decision');`로 조회합니다. 일반 별칭이나 신규 단지 확정은 `refresh_identity_decisions(district, source_id, target_id)`에 기록하고, 같은 지번·이름을 재사용하는 재건축 분리는 `refresh_identity_period_decisions(district, source_id, built, valid_from, valid_to, target_id, ...)`에 적용 구간과 구분 준공연도(`built`), 고유 접미사 ID(`issue_rebuild_complex_id`, 예: `<원천키>#2026`)를 등록합니다. 잘못된 결정을 수정한 뒤에는 `UPDATE refresh_identity_pending SET status='pending' WHERE status='invalid_decision' AND source_id=...;`로 재개합니다.
  - 거래 누락·정정 보류: `refresh_missing_trades` 및 `refresh_revision_pending`에서 원본과 후보를 비교하고, 실제 삭제·대체가 확인된 건만 `UPDATE refresh_missing_trades SET approved=true WHERE trade_id=...;`로 승인합니다. 승인된 과거 계약월은 다음 일일 실행에서 자동 재처리합니다. 일시 장애는 같은 일일 슬롯에서 10분 간격으로 재시도합니다. 빈 거래 응답 등 안전하게 대조할 수 없는 승인 건은 `refresh_approval_failures`에 기록하고 1·3·7·14일 간격으로 재확인합니다.
  - 월간 기본정보 후보 승인: `SELECT * FROM complex_metadata_candidates WHERE status IN ('pending', 'held');`로 주소·준공연도 변경 후보를 확인한 뒤, `verified-metadata/YYYY-MM.json` 또는 `verified-metadata/new-*.json`에 `complex_id`, `expected_address`(필요 시 `expected_name`, `expected_built`), `evidence`, `changes`, 후보를 승인할 때 `expected_name`, `expected_built`, `candidate_id` 또는 `candidate_ids`와 필드별 `field_evidence`를 담아 배치합니다. 주소만 정정되면 기존 좌표가 즉시 비워지고 `complex_geocode_pending`을 통해 새 주소 좌표가 보완되며, 검증 좌표쌍(`latitude`, `longitude`)을 함께 넣으면 재조회 없이 즉시 반영됩니다. 준공연도 변경이나 예측 입력 창(`M-19 … M-2`) 내 거래 소속 이동은 건수 변화가 0건이어도 `refresh_publish_pending`을 통해 구별 모델 입력 재계산으로 이어집니다.
- **멸실·재건축 판정**: 판정 코드는 `backend/lifecycle_check/`에 있고 기본값(`JIPFIT_LIFECYCLE_AUTO=0`)에서는 실행하지 않습니다. 판정 보류(`pending`)는 기존 상태와 가격 마커 표시 자격을 그대로 유지하며, 근거·기준일·버전을 갖춘 멸실 확정(`demolished`) 단지만 과거 거래·상세·관심단지를 보존한 채 지도 가격 마커에서 제외합니다. 운영자는 보류 단지만 처리합니다. 처음 켜는 절차, 보류 처리, 되돌리기는 [월간 판정 운영 안내](docs/lifecycle-monthly.md)를 따릅니다. 월간 `lifecycle_provider`는 같은 판정을 두 번 반영하지 않도록 빈 제공자(`empty_lifecycle_provider`)로 둡니다. 판정 한 번은 같은 캐시 파일을 디스크에서 한 번만 읽습니다. 문제 해결용으로 `.env`에 `JIPFIT_LIFECYCLE_READ_ONCE=0`을 넣고 updater를 다시 올리면 이를 끕니다(기본값 `1`). 수집은 API별로 초당 `JIPFIT_LIFECYCLE_RATE`회(기본 `2`) 이하로 호출하고, HTTP 429를 받으면 `JIPFIT_LIFECYCLE_RATE_MIN`(기본 `1`)까지 스스로 낮춥니다.

공개된 금리·광진구 매매가격지수를 확인한 뒤 모델 입력을 다시 계산합니다. 모델의 거래 시차는 2개월입니다. 예를 들어 11월 기준 예측에는 9월 계약까지 사용합니다. 수집·지표 확인·입력 저장을 모두 마친 날만 refresh_runs에 기록합니다. 앱은 가장 최근 공개 월에 입력이 있는 면적군을 예측합니다.

validate.py는 동봉한 기준 자료의 입력과 예측값을 재현한 뒤 기존 시계열 평가 표본의 수익률 오차를 퍼센트포인트(pp)로 집계합니다. 이 결과는 과거 표본 평가입니다.

### 공개 서버에서 실행하기

상시 실행할 Docker 서버에서 도메인의 DNS를 서버로 연결하고 80·443 포트 수신을 허용합니다. .env의 JIPFIT_DOMAIN에 공개 호스트 이름을 설정하고 네이버 지도 애플리케이션에도 서비스 주소를 등록합니다. 준비된 서버에서만 아래 명령을 실행합니다.

```sh
docker compose -f compose.yaml -f compose.deploy.yaml --profile updates up -d --build --wait
```

게이트웨이가 HTTPS를 제공하며 브라우저의 API 요청은 같은 도메인으로 전달됩니다. 데이터는 서버의 PostgreSQL 볼륨에 보존됩니다.

### Cloudflare Pages로 발표 버전 배포하기

React와 API 전달 코드는 Pages에 올리고, FastAPI·PostgreSQL·updater는 현재 Docker 호스트에서 실행합니다. Pages 배포는 main 푸시나 GitHub의 `deploy-pages.yml` 수동 실행으로 합니다(저장소 시크릿 `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`과 변수 `CLOUDFLARE_PAGES_PROJECT` 필요). 발표용 Quick Tunnel은 PowerShell에서 다음 명령으로 엽니다.

```powershell
docker compose -f compose.yaml -f compose.pages.yaml --profile updates up -d web updater tunnel
docker compose -f compose.yaml -f compose.pages.yaml logs --tail 50 tunnel
```

Pages 프로젝트 Settings의 Variables and Secrets에서 Production 텍스트 변수 API_ORIGIN에 로그의 https://…trycloudflare.com 주소를 저장하고 `deploy-pages.yml`을 다시 실행합니다. Pages 주소의 /api/complexes에서 JSON이 보이고 단지 선택 후 예측 표가 뜨는지 확인합니다. 네이버 지도 Web 서비스 URL에도 새 Pages 주소를 등록합니다.

Quick Tunnel 주소는 터널 재생성 시 바뀔 수 있습니다. 상시 운영 시 Cloudflare에서 관리하는 고정 API 도메인을 Tunnel로 연결하고 API_ORIGIN을 해당 HTTPS 주소로 설정합니다. Docker 호스트를 끄면 Pages 화면에 예측 서버 연결 오류가 표시됩니다.

## 서울 시범 운영 · 광진구와 성동구

기존 광진구 단지 ID와 회원 자료를 유지합니다. 성동구(11200) 최초 수집은 모델 입력을 만들기 위해 기준월 포함 20개월을 조회합니다. 이후 두 구는 독립된 수집 기록을 사용합니다. 광진구와 성동구가 동일한 구형 수집기를 공유하지 않도록 **기존 updater를 먼저 중지**하고 다음 순서로 전환합니다.

```powershell
docker compose --profile updates stop updater
docker compose --profile updates build api web updater
docker compose up -d --wait api web
docker compose --profile updates run --rm updater python refresh.py --once --district 11200
docker compose --profile updates up -d updater
docker compose exec api python verify_service.py http://web --district 11215
docker compose exec api python verify_service.py http://web --district 11200
```

성동구 전망에는 성동구 매매가격지수와 광진구에서 학습한 모델을 사용합니다. 지역별 오차 검증은 6-3에서 수행합니다. 기존 학습 산출물의 입력 이름을 유지하면서 지수 값은 요청한 지역에 맞춰 생성합니다. 좌표가 없는 성동구 단지는 목록에서 조회하며, 행당동 380 서울숲리버뷰자이는 주소 일치 시 확인된 대표 좌표를 연결합니다. 지도·목록으로 광진구와 성동구 후보를 한 개씩 담아 비교 창에서 각 단지의 기준월과 3·6·12개월 전망을 확인합니다.

### 6-3 광진구·성동구 예측 모델 비교

저장소 루트에서 실행하면 성동구 월별 실거래를 수집하고 두 구의 과거 자료로 모델을 다시 학습한다. 중간에 수집이 끊기면 같은 명령으로 이어받을 수 있다.

```powershell
$auditDir = Join-Path $env:LOCALAPPDATA 'jipfit-district-audit'
New-Item -ItemType Directory -Force $auditDir | Out-Null
docker compose --profile updates build updater
docker compose --profile updates run --rm --no-deps -v "${auditDir}:/audit" updater python -u evaluate_districts.py --cache /audit
```

검증 기준월은 2025년 4~9월이다. 2025년 4월 시점에 관측 가능한 목표값으로 학습한 뒤 같은 단지·면적군·기준월에서 광진구 학습 모델, 두 구 통합 모델, 성동구 전용 모델, 가격 유지 기준을 비교한다. 실거래 공시 시차는 2개월로 가정한다. 성동구 지수는 지역별 매매가격지수 원자료의 성동구 열을 사용하고 공개 시점은 같은 통계의 광진구 일정과 맞췄다. 과거 거래는 현재 확보한 이력으로 재구성하므로 당시의 신고·정정 상태까지 재현하지는 않는다.

결과 폴더의 `district_scores.csv`에 구·기간별 평가 표본 수, 상승률 MAE와 평균 편향(%p)이 기록된다. `district_coverage.csv`에는 수집 기간 내 확인된 단지·면적군 중 기준월별 예측 입력을 만들 수 있는 비율이 기록된다. 출력된 후보는 여섯 기준월에 대한 잠정 결론이며 운영 모델을 교체하려면 별도 기간에서도 확인해야 한다.
