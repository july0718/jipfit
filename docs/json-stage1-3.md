# 집핏 JSON 조회 전환 1~3단계

> 이전 실험 기록입니다. 현재 운영 구현은 [DB 조회 및 프런트엔드 최적화](./json-stage4-6.md)를 따릅니다.

## 기준과 데이터 경로

- 기준 커밋은 `fa03b30f2eeca4db8c3483600f6faecea69c6f92`입니다. 기준 버전과 PR 버전은 같은 PostgreSQL 데이터와 모델 입력 시점을 사용해 비교합니다.
- PR의 기존 기본 모델 파일은 광진구 학습본이었습니다. 기존에 학습한 2026년 11월 서울 25개 구 통합 HGB로 교체했습니다. API와 스냅샷이 같은 모델 파일을 읽고, 초기화 시 25개 구 범위를 확인합니다.
- PostgreSQL은 원본입니다. 내보내기는 `REPEATABLE READ READ ONLY` 트랜잭션으로 실행하며 실거래, 취소 표시, 과거 거래를 읽습니다.
- 단지 상세, 계정, 관심단지, 메모 및 비교 저장은 서버 API를 사용합니다. 단지 선택과 비교 창은 서버에서 3·6·12개월 예측을 계산합니다. 스냅샷의 수익률은 목록 정렬에 사용합니다.
- 지도 표시는 기존 지도 제외 목록과 마커 표시 조건을 적용합니다.

## 서울 25개 구 JSON 생성 및 검증

이미 서울 전체 데이터가 들어 있는 Docker Compose 프로젝트를 사용합니다. `docker compose ls`로 기존 프로젝트와 볼륨을 먼저 확인합니다. 별도 체크아웃에서 실행한다면 기존 프로젝트에 연결되는지 확인한 뒤 실행합니다.

```powershell
docker compose --profile snapshots run --rm --build snapshot
docker compose up -d --build --wait web
docker compose exec api python verify_service.py http://web
```

내보내기는 구별 단지 및 최근 가격 파일과 단지별 전체 이력 파일을 `frontend/public/data/releases/<timestamp>/`에 생성합니다. 거래 파일에는 전체 면적군과 취소 거래가 들어갑니다. 같은 DB 스냅샷에서 25개 구 면적군 키, 전체·취소 건수, 각 거래 필드와 정렬 순서를 모두 검증한 뒤 `manifest.json`을 교체합니다. 생성된 JSON은 Git에서 제외합니다.

디스크에 저장된 파일만 다시 확인하려면 다음을 실행합니다.

```powershell
python backend/check_public_json.py --root frontend/public/data
```

## React 및 전후 검증

```powershell
Push-Location frontend
npm install --include=dev --no-audit --no-fund
npm run build
node --test staticData.test.mjs
Pop-Location
python -m unittest discover -s backend -p test_public_json.py -v
```

브라우저에서 지역·검색·세 가지 정렬, 지도 마커, 면적군 선택, 취소 포함 거래 더 보기, 비교 창을 확인합니다. Network에서 단지·거래 파일과 실시간 `/api/predict` 호출을 확인합니다. 별도의 테스트 계정으로 로그인·로그아웃, 관심단지, 메모, 비교 저장·복원을 확인합니다. 스냅샷이 없는 상태에서 매니페스트 404와 API 조회 경로도 확인합니다.

같은 PC에서 기존 웹과 PR 웹을 동일한 DB 및 동일한 서울 통합 HGB를 사용하는 API에 연결한 뒤 비교합니다. 이렇게 하면 모델 교체가 조회 성능 비교에 영향을 주지 않습니다.

```powershell
python backend/benchmark_web.py --baseline-origin http://127.0.0.1:5174 --snapshot-origin http://127.0.0.1:5173 --complex-id "<예측 가능 단지 ID>" --area-group 60 --repeat 7
```

지정한 면적군이 실제 예측 가능한지 확인합니다. 스크립트는 첫 목록, 선택한 면적군의 전체 거래, 실시간 예측 각각의 응답 바이트·요청 수·중앙값·최대를 기록합니다. 브라우저 초기 렌더링과 클릭 응답의 시간은 브라우저 Performance에서 따로 기록합니다. 4단계 적용 후 첫 지도 화면은 매니페스트와 선택한 구의 파일 두 개를 요청합니다. 전체 운영 흐름은 [4~6단계 문서](./json-stage4-6.md)를 참조합니다.

## Pages와 현재 검증 상태

- Pages 배포는 main 푸시나 `deploy-pages.yml` 수동 실행으로 합니다. GitHub 기반 배포에서 서울 데이터가 포함된 정적 JSON은 별도 입력으로 제공해야 합니다.
- 로컬에서 JSON 검증기 4개와 프론트엔드 데이터 전송 테스트 4개를 통과했습니다. 테스트는 가상 데이터·응답을 사용합니다.
- 통합 HGB를 직접 로드해 저장소 표본 10건의 3·6·12개월 추론에서 유한한 결과를 확인했습니다. 이 테스트는 API 호출을 포함하지 않습니다.
- 실제 운영 DB를 대상으로 한 전체 생성·정합성 검사, 브라우저 기능 회귀 테스트, 전후 성능 비교는 미검증입니다. 통합 검증이 끝난 뒤 병합 여부를 결정합니다.

