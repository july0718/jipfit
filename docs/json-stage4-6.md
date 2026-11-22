# 집핏 DB 조회 및 프런트엔드 최적화

## 현재 데이터 흐름

React는 FastAPI에 자치구, 단지 목록, 거래 이력, 3·6·12개월 예측을 요청합니다. FastAPI는 기존 PostgreSQL을 조회하며 예측 API는 서울 통합 HGB를 사용합니다. Cloudflare Pages는 프런트엔드를 제공하고 `/api/` 요청을 기존 FastAPI 서버로 전달합니다.

| 동작 | 적용 방식 |
|---|---|
| 첫 광진구 화면 | DB에서 구 목록과 광진구 단지·가격·정렬 데이터를 조회 |
| 특정 구 선택 | 요청한 구의 단지만 조회하고 같은 탭에서 응답 재사용 |
| 전체 지역 지도 | 화면 경계와 겹치는 구의 데이터 조회 |
| 거래 이력 | 선택한 단지와 면적군을 DB로 조회하고 표는 20행씩 표시 |
| 관심단지·메모·저장 비교 | 기존 계정 API를 사용하고 관련 단지는 필요한 시점에 조회 |

지도는 화면 안의 단지만 표시하고, 줌 15 미만에서 인접 단지를 클러스터로 묶습니다. 마커를 재사용하고 React에서 불필요한 다시 렌더링을 줄입니다. 지역 선택 시 특정 상황에서 지도 중심이 잘못 이동하는 문제는 별도 후속 작업입니다.

`GET /api/districts`가 아직 없는 이전 API 컨테이너와도 연결할 수 있습니다. 그 경우 기존 전체 목록 API를 한 번 호출한 다음 구별로 걸러냅니다. **서버를 최신 버전으로 재시작한 뒤 구별 전송량과 속도를 측정**합니다. 이전 JSON 스냅샷의 측정치는 현재 DB 방식의 성능 수치에 적용되지 않습니다.

## DB 갱신

기존 PostgreSQL 볼륨을 사용하는 Compose 프로젝트에서 다음 서비스를 실행합니다.

```powershell
docker compose --profile updates up -d --build updater
docker compose logs -f updater
```

스케줄러는 시작 시와 이후 매일 한국시간 06시에 거래·모델 입력을 갱신합니다. 갱신 실패 시 한 시간 후 다시 시도합니다. 정정 전 원본 행은 `trade_revisions`에 보관하고, 현재 거래 응답에도 취소 상태를 포함합니다.

단지 정보와 좌표는 근거가 확인된 변경 목록으로 반영합니다. 입력 파일을 `verified-metadata/YYYY-MM.json`에 두고, 신규 단지용 변경 파일은 `new-<고유 배치명>.json`으로 둡니다. 항목마다 `complex_id`, `expected_address`, `evidence`, `changes`가 필요합니다. 주소가 일치하는 경우에만 반영합니다. 지도는 좌표와 유효한 실거래 표시가격이 있는 단지를 표시합니다.

## 확인할 항목

- `GET /api/districts` 응답 및 첫 화면의 `GET /api/complexes?include_sort=true&district=광진구` 요청
- 지도 이동과 확대·축소, 구 변경, 검색, 거래내역 페이지 이동
- 로그인·로그아웃, 다른 구 관심단지와 비교 복원, 실시간 3·6·12개월 예측
- 실제 운영 DB 스케줄러 갱신 로그

```powershell
node --test frontend/staticData.test.mjs
```
