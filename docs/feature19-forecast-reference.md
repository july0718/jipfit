# 기능 19 — 서울 통합 HGB 홀드아웃 오차 참고범위

## 목적과 중요한 제한

웹의 **예측 매매가** 그래프는 실제 단지·면적군의 3·6·12개월 HGB 예측값을 계속 표시한다.
이제 참고 음영에는 **광진구 과거 모델의 오차를 사용하지 않는다.**
서울 25개 구를 학습한 2025-04 기준 통합 HGB 모델에 대해 2025-04~2025-09 기준월을 홀드아웃으로 평가한 개별 오차만 사용한다.

이 결과는 **2025년 시점에 학습한 서울 통합 검증 모델**의 과거 외표본 분포다.
**현재 배포 중인 운영 모델의 검증된 신뢰구간이나 미래 가격의 확률을 뜻하지 않는다.**
모델 재학습 시점, 지표 개정, 시장환경 변화에 따라 성능·오차 분포가 달라질 수 있다.

## 산출과 표시 기준

- 검증 스크립트: `backend/evaluate_seoul_holdout.py`
- 검증 모델: `backend/train_seoul.py --cutoff 2025-04`로 생성한 서울 25개 구 학습 산출물
- 홀드아웃 기준월: 2025-04~2025-09 (6개월)
- 이용 가능한 과거 데이터 장부 시점: 2026-11, 거래 신고·모델 입력 2개월 시차 고려
- 대상: 각 월의 예측 가능 단지·면적군 중 실제 3·6·12개월 결과를 계산할 수 있는 표본
- 실제 오차 잔차: `actual_return_percent - predicted_return_percent` (%p)
- 음영 하단/상단: 잔차의 10/90백분위수를 현재 예측 수익률에 *참고용*으로 가산한 가격
- MAE: `mean(abs(predicted - actual))` (%p)
- Bias: `mean(predicted - actual)` (%p), 즉 잔차 평균의 음수
- 기간별 30건 미만, 자치구 25개 또는 홀드아웃 기준월 6개 미확보, 중복 샘플, 모델 출처 불명, 비유한값 등은 배포 실패

공개 JSON에는 **개별 단지 ID·실거래 원본·회원정보·개별 예측 오차를 포함하지 않는다.**
API가 기존 광진구 참고자료를 반환하는 경우에도 React는 이를 무시한다.

## 원본 분석 및 검증 결과 생성 (DB가 있는 PC에서 1회 실행)

VS Code 터미널에서 집핏 프로젝트 루트를 연다. 로컬 변경이 있으면 먼저 보호한다.

```powershell
git status -sb
git pull origin main

$reports = Join-Path (Get-Location) "backend\holdout-output"
New-Item -ItemType Directory -Force $reports | Out-Null

docker compose up -d --wait db
docker compose --profile updates build updater

# 이미 2025-04 cutoff 서울 25개 구 학습 검증 모델이 있다면 아래 학습은 생략 가능
docker compose --profile updates run --rm -v "${reports}:/audit" updater python train_seoul.py --cutoff 2025-04 --output /audit/seoul-2025-04.joblib

# 학습 모델·기존 PostgreSQL 원본 데이터를 읽기 전용으로 검증
docker compose --profile updates run --rm -v "${reports}:/audit" updater python evaluate_seoul_holdout.py --model /audit/seoul-2025-04.joblib --output-prefix /audit/seoul-holdout
```

생성되는 내부 검증 파일:

```text
backend/holdout-output/seoul-2025-04.joblib
backend/holdout-output/seoul-holdout-summary.csv
backend/holdout-output/seoul-holdout-district.csv
backend/holdout-output/seoul-holdout-month.csv
backend/holdout-output/seoul-holdout-detail.csv
backend/holdout-output/seoul-holdout-seoul-residuals.csv   # 개별 예측·실제값/오차, 공개 금지
backend/holdout-output/seoul-holdout-seoul-reference.json # 공개 가능 집계
```

`backend/holdout-output/` 폴더는 Git에서 제외했다. DB 변경, 학습 운영 모델 교체, 계정 데이터 수정은 하지 않는다.
검증이 실패하면 JSON 파일을 임의 수치로 작성하지 않는다.

## 정적 JSON 배포

검증 스크립트가 정상 종료하고 표본 수·결과를 검토한 뒤 **집계 JSON만** 공개 디렉터리에 복사한다.

```powershell
Copy-Item "$reports\seoul-holdout-seoul-reference.json" "frontend\public\metadata\seoul-forecast-reference.json"
python backend/seoul_reference.py frontend/public/metadata/seoul-forecast-reference.json

git add frontend/public/metadata/seoul-forecast-reference.json
git commit -m "data: publish audited Seoul HGB holdout reference"
git push origin main
```

GitHub Actions는 JSON 유효성을 검사한 후 Cloudflare Pages에 아래 파일을 제공한다.

```text
/metadata/seoul-forecast-reference.json
```

배포 후 브라우저에서 위 파일을 열어 `kind=seoul_hgb_holdout`, `training_cutoff=2025-04`, `calibrated_for_current_model=false`, 기간 3·6·12개월과 표본 수를 확인한다.

해당 파일이 **아직 없으면** 웹에서 참고 음영 선택이 비활성화되며, **광진구 오차를 대신 그리지 않는다.**
기존 실거래 그래프와 HGB 가격 예측은 계속 제공한다.

## 회귀 검사

```powershell
cd backend
python -m unittest test_seoul_reference -v
cd ../frontend
node --test forecastReference.test.mjs
npm run build
```

GitHub PR에서도 Node 테스트, Python 테스트, React 및 Docker 빌드를 검증한다.

## 향후 확장

현재 값은 **서울 전체 통합 표본의 분포**다. 자치구별 경험적 오차 범위가 필요한 경우 먼저 구별 표본 수와 편향을 확인한 뒤 독립 검증된 하위 표본의 자료를 추가해야 한다.
현재 운영 모델을 동일한 배포 시점/입력 시점에서 별도 검증하기 전까지는 ‘서울 통합 HGB 과거 검증 **참고범위**’로만 명명한다.
