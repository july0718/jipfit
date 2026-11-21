# 집핏 4~6단계 검증 및 운영

## 웹 조회 경로

| 동작 | 데이터 조회 |
|---|---|
| 첫 광진구 화면 | 매니페스트와 광진구 단지·최근 거래 JSON |
| 특정 구 선택 | 해당 구의 JSON. 이미 방문한 구는 현재 탭의 캐시 사용 |
| 전체 지역 지도 | 화면 경계와 겹치는 구의 JSON. 축소 시 여러 구를 함께 로딩 |
| 서울 전체 검색·목록 | 사용자가 전체 지역에서 검색하거나 전체 목록 버튼을 누를 때 25개 구 조회 |
| 관심단지·메모·저장 비교 열기 | 항목이 속한 구를 필요한 시점에 조회 |
| 거래 이력 | 단지별 전체 JSON을 읽고 표는 20행씩 표시 |
| 예측·인증·개인화 | 기존 FastAPI 호출 |

네이버 지도는 화면 경계 안의 단지만 표시합니다. 줌 15 미만에서 가까운 단지를 묶고, 클러스터를 누르면 확대합니다. 줌 15 이상에서 개별 마커를 표시합니다. 기존 20·100·전체 옵션은 표시 핀 수에 적용합니다. 지도는 같은 인스턴스를 사용하고 바뀐 핀만 갱신합니다. 지도 가격은 면적군의 최근 유효 실거래가를 표시합니다.

## 실행과 자동 운영

기존 PostgreSQL 볼륨을 사용하는 집핏 Compose 프로젝트에서 실행합니다. Git으로 추적하지 않는 `.env`에 국토부 키를 설정하고, 월간 검증 데이터는 `verified-metadata/`에 둡니다.

```powershell
docker compose --profile public-updates run --rm --build public-updater python -u run_public_updates.py --publish-only
docker compose up -d --build web
docker compose --profile public-updates up -d --build public-updater
docker compose logs -f public-updater
```

`--publish-only`는 기존 DB를 읽어 스냅샷만 검증·생성합니다. 상주 서비스는 시작 시와 이후 매일 한국시간 06시에 실행합니다. 구별 수집 실패 시 공개 매니페스트를 유지하고 한 시간 후 재시도합니다. 구별 DB 반영이 성공한 구는 같은 날 재시도에서 건너뜁니다. 이 서비스를 사용할 때 별도로 실행 중인 기존 `updater`는 중지합니다.

국토부 조회는 기존 신고 기간 재조회와 다운타임 따라잡기 로직을 사용합니다. 유효 거래에서 제외된 정정 전 행은 같은 DB 트랜잭션에서 `trade_revisions`에 보관합니다. 취소 표시가 포함된 현재 거래는 단지 JSON에 들어갑니다. 기존 신고 기간 바깥의 뒤늦은 변경을 검증하려면 해당 계약월을 별도로 재검토해야 합니다.

월간 작업은 검증을 마친 변경 목록을 `YYYY-MM.json`으로 받습니다. 새 단지는 일일 수집에서 DB에 등록되며, 월간 일정을 기다리지 않고 `new-<고유 배치명>.json`으로 검증 결과를 반영할 수 있습니다. 증빙이 아직 준비되지 않은 월은 로그에 대기 상태를 남깁니다. 월간 검증 증빙을 생산하거나 자동 판정하는 작업은 별도 입력에 의존합니다.

배치 예시는 다음과 같습니다. `batch`는 파일명에서 확장자를 뺀 값입니다. `expected_address`는 적용 전 DB 주소와 일치해야 합니다. 존치 상태는 `active`, `demolished`, `unconfirmed`를 사용합니다. 멸실·미확정 판정은 지도 표시 조건에만 적용합니다.

```json
{
  "batch": "2026-11",
  "complexes": [{
    "complex_id": "<PostgreSQL 단지 ID>",
    "expected_address": "<현재 PostgreSQL 주소>",
    "evidence": "<검증된 원본과 판정 기록 식별자>",
    "changes": {"existence_status": "active", "latitude": 37.55, "longitude": 127.1}
  }]
}
```

검증된 단지 정보는 `name`, `address`, `built`도 갱신할 수 있습니다. 좌표는 위도·경도를 함께 기록합니다. 적용 완료 배치는 원본으로 보관하고 추후 정정은 새 배치로 전달합니다.

## Cloudflare 게시 및 복구

Cloudflare Pages 프로젝트에서 비공개 R2 버킷을 `PUBLIC_SNAPSHOTS` 이름으로 연결한 뒤 재배포합니다. 스케줄러의 `.env`에 같은 버킷을 지정하는 `JIPFIT_PUBLISH_R2=1`, `R2_ENDPOINT`, `R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`를 설정합니다. 별도의 Preview 버킷을 사용하면 PR 검증이 운영 데이터 버전에 영향을 주지 않습니다.

공개용 스냅샷은 같은 읽기 전용 DB 스냅샷에서 전체 정합성을 검사합니다. 발행 파일의 SHA-256을 경로로 사용해 동일한 파일을 재사용하고 새 객체만 R2에 올립니다. 검증 단계는 전체 DB를 읽고 임시 파일을 다시 만들기 때문에 전송 최적화와 DB 검사 비용은 별개입니다. 공개 매니페스트는 참조 파일 업로드와 보관용 매니페스트 작성 이후에 교체합니다. 브라우저는 객체를 캐시하고 매니페스트는 재사용하지 않습니다.

이전 버전으로 복구할 경우 스케줄러를 정지한 뒤 이전에 검증·게시된 버전 식별자를 지정합니다.

```powershell
docker compose --profile public-updates stop public-updater
docker compose --profile public-updates run --rm public-updater python -u run_public_updates.py --rollback <VERSION>
```

## 통합 검증 항목

```powershell
node --test frontend/staticData.test.mjs
python -m unittest discover -s backend -p test_public_json.py -v
python backend/check_public_json.py --root frontend/public/data
```

웹 성능 측정은 같은 DB와 서울 통합 HGB에 연결한 기존 웹과 후보 웹을 실행하고 [1~3단계 문서](./json-stage1-3.md)의 벤치마크 명령을 사용합니다. 브라우저에서 초기 목록 표시 시간, 이동·확대 중 프레임, 장기 거래 목록 페이지 전환, 지도 마커 수를 비교합니다. 로그인·로그아웃, 다른 구 관심단지·비교 복원, 3·6·12개월 정상 응답과 시점 표기, 취소 포함 거래 이력, 멸실 단지의 지도 제외와 이력 조회를 함께 확인합니다.

기존 JSON 전환 버전의 첫 목록은 51개 데이터 요청이었습니다. 후보는 테스트 응답 기준 3개를 요청하며 해당 구 재방문은 추가 요청이 없습니다. 이 수치는 데이터 요청 수를 비교한 것으로 실제 로딩 시간 실측은 아닙니다. 같은 운영 데이터를 쓰는 브라우저·DB 측정 완료 후 병합 여부를 판단합니다.
