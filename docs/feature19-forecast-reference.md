# 기능 19 — 실거래·전망 그래프와 검증 오차 참고범위

## 구현 범위

- 과거 실거래(계약 해제 제외)의 월별 중앙값 차트와 누적 변화율은 기존 동작을 유지한다.
- 실시간 HGB 전망은 현재 기준가와 3·6·12개월 예상 가격을 점 및 점선으로 표시한다.
- 사용자가 **광진구 과거 검증 오차 참고범위 표시**를 선택하면, 과거 검증 표본에서 얻은 잔차의 10~90백분위에 대응하는 가격대를 음영으로 덧그린다.
- 당시 검증에 사용한 모델과 현재 서울 통합 운영 HGB는 동일한 모델이 아니다. 이 음영을 현재 모델의 **신뢰구간, 확률적 예측구간, 서울 지역별 검증 오차**로 해석해서는 안 된다.

## 출처와 수식

출처: `backend/audit.csv.gz` 및 `MODEL_EVALUATION.md`의 광진구 과거 검증. 테스트는 `backend/validate.py`가 동일한 감사 자료를 읽는다.

기간 `h`별 잔차(단위: %p):

```text
e_h = 실제 상승률(%) - 당시 예측 상승률(%)
q10_h = percentile(e_h, 10)
q90_h = percentile(e_h, 90)
```

현재 모델의 예측 수익률 `r_h` 및 기준가 `P_0`에 대한 *참고용 시각화 좌표*:

```text
참고 하단가 = max(0, P_0 × [1 + (r_h + q10_h)/100])
참고 상단가 = max(0, P_0 × [1 + (r_h + q90_h)/100])
```

현재 모델의 오차 범위를 검증하거나 보정한 값이 아니라, **다른 과거 모델의 오차를 시각적으로 재사용한 참고값**이다. 확률적 해석 금지. 가격은 만 원 단위, 오차는 %p 단위다. 표본은 3·6·12개월 각각 독립 집계한다.

## API

기존 `GET /api/predict?complex_id=...&area_group=...` 응답의 계약을 확장한다. 기존 `forecasts` 배열은 불변이며, 새로운 선택 필드 `historical_reference`를 반환한다.

```json
{
  "historical_reference": {
    "kind": "gwangjin_historical_model",
    "scope": "광진구 과거 모델 평가",
    "calibrated_for_current_model": false,
    "horizons": [
      {"months": 3, "sample_count": 1510, "p10_pp": "실제 자료에서 계산", "p90_pp": "실제 자료에서 계산"}
    ]
  }
}
```

예시는 응답 필드의 구조 설명용이며, 표본 수와 분위수는 감사 자료에서 계산한다. 파일이 없거나 값이 불완전하면 `historical_reference: null`을 응답하고 **기본 예측 API는 계속 작동**하도록 했다. 프론트엔드에서는 참고 대역 대신 ‘검증 자료가 제공되지 않는다’는 설명을 표시한다.

## 검증

```powershell
cd frontend
node --test forecastReference.test.mjs
npm run build

cd ../backend
python -m unittest test_reference_band -v
```

최종 수동 검증: 3·6·12개월 전망이 표시되는 단지에서 체크박스를 전환하고, 오차 표본 수와 %p 값·단위 및 음영을 확인한다. 예측 입력이 없는 단지는 기존 거래 차트가 정상 표시돼야 한다.

## 후속 과제

정식 서울 통합 모델의 예측 구간을 주장하려면 **동일한 모델 계열·학습 시점·평가 대상에 대해 시간 누수 없는 서울 25개 구 외표본 잔차 자료**를 수집하고, 기간·지역별 표본 수와 포함률을 독립 검증해야 한다. 그런 자료가 준비되기 전까지 현재 밴드는 ‘광진구 과거 참고범위’만 뜻한다.

## Cloudflare 정적 JSON 제공(가시성 버그 수정)

이전 구현은 API 서버가 최신 `historical_reference` 필드를 반환하지 않으면 차트 아래의 체크박스조차 나타나지 않았다. 사용자에게 기능이 보이지 않는 문제가 있었으므로, `main` 푸시 배포 단계에서 `backend/audit.csv.gz`를 직접 검증하고 다음 정적 파일을 만든다.

```text
/metadata/forecast-reference.json
```

`tools/export_historical_reference.py`는 기존 과거 감사 자료를 읽기 전용으로 처리하여 10·90백분위 오차를 계산한다. React는 라이브 API 응답에 해당 자료가 없을 경우 이 정적 JSON을 한 번만 불러온다. 이를 통해 API 컨테이너를 당장 재배포하지 않아도 공개 Cloudflare Pages에서 **참고 음영**을 표시할 수 있다. 백엔드/DB/학습 모델을 수정하지 않는다.

체크박스 자체는 항상 표시하며, 데이터가 없으면 비활성화 상태와 원인을 표시한다.

배포 검증: `https://jipfit-48v.pages.dev/metadata/forecast-reference.json`에서 `kind=gwangjin_historical_model`과 `calibrated_for_current_model=false` 확인. 프론트엔드의 ‘예측 매매가’ 그래프에서 체크박스를 켜고 참고 음영을 확인한다. 이 정적 데이터 역시 **현재 서울 통합 HGB의 검증된 예측 신뢰구간이 아니다.**
