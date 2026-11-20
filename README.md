# 집핏 프로토타입

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

갱신 서비스는 최초 기동 시 당일 작업을 시도하고 이후 매일 한국시간 06시에 실행합니다. 실패하면 한 시간 뒤 재시도합니다. 컨테이너가 실행 중이어야 합니다. 최근 계약월 4개를 다시 조회하고, 중단 기간이 길면 누락된 달도 수집합니다. 이 범위보다 오래된 뒤늦은 정정은 별도 재수집 대상입니다. 중복 거래는 건수까지 보존하며 취소·정정 차이를 반영합니다. 신규 단지에 좌표가 없으면 목록에서 선택할 수 있습니다.

공개된 금리·광진구 매매가격지수를 확인한 뒤 모델 입력을 다시 계산합니다. 모델의 거래 시차는 2개월입니다. 예를 들어 11월 기준 예측에는 9월 계약까지 사용합니다. 수집·지표 확인·입력 저장을 모두 마친 날만 refresh_runs에 기록합니다. 앱은 가장 최근 공개 월에 입력이 있는 면적군을 예측합니다.

validate.py는 동봉한 기준 자료의 입력과 예측값을 재현한 뒤 기존 시계열 평가 표본의 수익률 오차를 퍼센트포인트(pp)로 집계합니다. 이 결과는 과거 표본 평가입니다.

### 공개 서버에서 실행하기

상시 실행할 Docker 서버에서 도메인의 DNS를 서버로 연결하고 80·443 포트 수신을 허용합니다. .env의 JIPFIT_DOMAIN에 공개 호스트 이름을 설정하고 네이버 지도 애플리케이션에도 서비스 주소를 등록합니다. 준비된 서버에서만 아래 명령을 실행합니다.

```sh
docker compose -f compose.yaml -f compose.deploy.yaml --profile updates up -d --build --wait
```

게이트웨이가 HTTPS를 제공하며 브라우저의 API 요청은 같은 도메인으로 전달됩니다. 데이터는 서버의 PostgreSQL 볼륨에 보존됩니다.

### Cloudflare Pages로 발표 버전 배포하기

React와 API 전달 코드는 Pages에 올리고, FastAPI·PostgreSQL·updater는 현재 Docker 호스트에서 실행합니다. PowerShell에서 다음 명령으로 현재 실행 중인 웹 빌드를 내보내고 발표용 Quick Tunnel을 엽니다.

```powershell
.\export-pages.ps1
docker compose -f compose.yaml -f compose.pages.yaml --profile updates up -d web updater tunnel
docker compose -f compose.yaml -f compose.pages.yaml logs --tail 50 tunnel
```

Cloudflare Pages에서 Direct Upload 프로젝트를 만들고 pages-upload 폴더를 업로드합니다. Settings의 Variables and Secrets에서 Production 텍스트 변수 API_ORIGIN에 로그의 https://…trycloudflare.com 주소를 저장하고 다시 배포합니다. Pages 주소의 /api/complexes에서 JSON이 보이고 단지 선택 후 예측 표가 뜨는지 확인합니다. 네이버 지도 Web 서비스 URL에도 새 Pages 주소를 등록합니다.

Quick Tunnel 주소는 터널 재생성 시 바뀔 수 있습니다. 상시 운영 시 Cloudflare에서 관리하는 고정 API 도메인을 Tunnel로 연결하고 API_ORIGIN을 해당 HTTPS 주소로 설정합니다. Docker 호스트를 끄면 Pages 화면에 예측 서버 연결 오류가 표시됩니다.
