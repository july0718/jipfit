# 집핏 JSON 조회 전환 · 1~3단계 작업 기록

## 범위와 불변 조건

- PostgreSQL은 원본 데이터베이스이며 이 작업의 exporter는 읽기 전용입니다.
- FastAPI `/api/predict` 는 변경하지 않았습니다. 요청 시 HGB 추론을 유지합니다.
- 회원가입·로그인·관심단지·비교 저장은 기존 FastAPI 경로를 유지합니다.
- 구형 `/api/complexes`, `/api/trades` 엔드포인트는 삭제하지 않습니다.
- 집핏 단지 ID, 좌표, 과거 거래내역 및 말소·취소 상태를 임의 삭제하지 않습니다.
- 기존 `map-unverified-complexes.json` 지도 제외 정책을 그대로 유지합니다. 알 수 없는 존치 여부는 멸실로 단정하지 않습니다.
- 자치구별 지연 로딩, 마커 클러스터링, React 재렌더링 개편, 일일/월간 스케줄러는 후속 단계에서 다룹니다.

## 1단계: 기준점·백업·성능 측정

기준 Git commit: `fa03b30f2eeca4db8c3483600f6faecea69c6f92` (main, 작업 시작 당시).
작업 브랜치: `feature/json-snapshots-stage1-3`. 기존 main 또는 운영 DB는 변경하지 않습니다.

기존 API 응답 크기 및 왕복시간을 다음으로 측정할 수 있습니다.

```powershell
docker compose up -d --build --wait db api web
docker compose exec api python benchmark_web.py --origin http://web --repeat 5
docker compose exec api python verify_service.py http://web
```

측정치는 반드시 **변경 전 버전**과 **변경 후 버전**에서 같은 PC와 같은 DB를 대상으로 기록합니다.
DevTools Performance와 Network에서 초기 로딩 시간, `/api/complexes` 전송 크기, 클릭 지연, 마커 생성량을 별도로 측정합니다.
이 브랜치만으로는 기존 배포 환경에 접근하지 못하므로 측정 수치를 작성하지 않았습니다.
DB 백업은 운영 PC에서 별도로 `pg_dump` 등을 사용해 확인해야 합니다. Git 브랜치는 **코드 백업**일 뿐 DB 백업이 아닙니다.

## 2단계: PostgreSQL → JSON 생성 및 검증

```powershell
docker compose --profile snapshots run --rm --build snapshot
```

실행하면 `frontend/public/data/manifest.json`과 `releases/<timestamp>/` 하위에
25개 구의 `complexes/<district>.json`, `latest-trades/<district>.json`,
`transactions/<district>/<sha256(complex_id)>.json`이 생성됩니다.
트랜잭션은 단지별 파일 하나에 전체 면적군을 담고, **취소 거래도 보존**합니다.
원본 DB 데이터는 변경하지 않습니다. `manifest.json`은 전체 파일 검증 이후 마지막으로 전환됩니다.
검증기는 단지별 파일 존재·단지 ID·면적군 건수·전체 거래 건수를 확인합니다.

원본 DB에 `complexes.existence_status` 또는 `complexes.survival_status`가 있으면 해당 값을 그대로 공개합니다.
그 외에는 `existence_status:null`로 두며 기존 지도 제외 목록은 그대로 유지합니다.
**단지 존치 상태의 최종 DB 판정과 별도 감사 자료 통합은 이 브랜치에서 새로 확정하지 않습니다.**

## 3단계: React JSON 우선 조회

- 단지·좌표·지도 최근 거래·검색/정렬 입력은 JSON 스냅샷을 우선 조회합니다.
- 개별 단지 전체 거래내역 및 비교 창의 거래 조회는 단지별 JSON을 우선 조회합니다.
- `/api/predict`와 `/api/complex`, 계정 API는 그대로 유지합니다.
- `manifest.json`이 아직 404인 초기 상태에서만 기존 목록·거래 API로 대체합니다.
  스냅샷이 있는데 손상된 경우에는 조용히 최신 데이터처럼 표시하지 않고 오류를 알립니다.
- `frontend/public/data/`는 생성 산출물이므로 .gitignore 처리했습니다.

로컬 웹 컨테이너에는 생성된 JSON이 이미지 빌드 시 포함됩니다.
**따라서 JSON을 먼저 생성한 후 웹을 다시 빌드**해야 합니다.

```powershell
docker compose --profile snapshots run --rm --build snapshot
docker compose up -d --build --wait web
docker compose exec api python verify_service.py http://web
```

Cloudflare Pages의 정적 배포에도 `data/` 폴더를 포함하는 별도 배포 작업이 필요합니다.
이 브랜치의 `export-pages.ps1`은 `frontend/public/data/manifest.json`이 있으면 생성된 JSON 폴더도 `pages-upload/data/`에 복사합니다. 다만 실제 Cloudflare 업로드/배포 자동화는 아직 수행하지 않았습니다.

## 남은 검증 항목

1. 운영 DB 전체에서 exporter를 실행하고 manifest 총량과 실제 DB의 COUNT가 일치하는지 확인
2. 서울 25개 구에서 임의 단지를 골라 면적군별 전체 거래내역 및 취소 거래의 순서 확인
3. 전후 네트워크·렌더링 시간 비교 및 새 JSON의 총 크기·요청량 확인
4. 지도 미확정 목록과 존치 판정 테이블의 결합 방식 확정
5. Cloudflare 배포 방식 선택 및 JSON 갱신 자동화(후속 단계)

**이 문서는 구현 브랜치 설명이며 운영 환경에서 실행 완료를 의미하지 않습니다.**
