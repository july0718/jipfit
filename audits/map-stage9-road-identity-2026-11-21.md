# 집핏 9차 — 폐쇄대장 도로명주소 역매칭과 추가 표제부 수집 준비 (2026-11-21)

## 입력·불변 원칙
- 8차 9,188개 지번별 건축HUB 원본 ZIP 및 8차 현행 건물목록(건축물 관리번호 기준)을 사용.
- 7차 최종 미확정 단지 **196개**를 대상으로, 8차 검토표의 고유 단지 ID를 보존.
- API 추가 조회 **0회**. 지도 제외 목록, PostgreSQL, 실거래, 예측, 좌표, 단지 ID **변경 없음**.

## 이번 추가 검증: 폐쇄대장 도로명주소 → 현행 건물 역매칭
과거 단지의 폐쇄말소대장 표제부에서 '새주소(newPlatPlc)'를 추출하고, 서울 9,188 지번 원본에서 구성한 현행 아파트 주거동의 도로명주소와 *도로명+건물번호까지 정확히* 비교했다. 괄호 속 법정동, 공백·구두점은 정규화하되 주소가 다르면 자동 연결하지 않는다.

| 검토 항목 | 결과 |
|---|---:|
| 미확정 단지 | 196 |
| 원지번에 폐쇄말소대장 레코드 있음 | 169 |
| 폐쇄대장에 도로명주소가 있음 | 137 |
| **과거/현재 도로명주소가 정확히 같은 단지** | **9** |
| **그중 다른 현행 지번에 연결된 단지** | **7** |
| 주소·기존 DB 준공연도까지 맞는 단지 | 1 |
| 현행 후보 동별 건물관리번호 레코드 | 47 |
| **최종 현존/멸실 판정 변경** | **0** |

### 주소 일치 후보 9개
| 과거 단지 | 현행 도로명주소 연결 후보 | 지번 관계 및 주의 |
|---|---|---|
| 국제 (대치동 612) | 대치 SK VIEW (1029) | 타지번, 새 건물일 수 있음 |
| 동신(바동) (도곡동 893-2) | 도곡쌍용예가 (933) | 타지번, 사용승인 차이/동 번호 확인 |
| 동산 (청담동 134) | 청담래미안로이뷰 (134) | 동일 지번, 종전명/리모델링 연결 필요 |
| 사당 (사당동 197) | 반석블레스 (1158) | 타지번, 신축 가능성 |
| 삼산 (합정동 437-18) | 합정한강아파트 (474) | 타지번, 종전 필지 확인 |
| 한신1차 (반포동 2-1) | 아크로리버파크 (2-12) | 타지번, 편입 필지·재건축 여부 |
| 한양 (반포동 32-5) | 반포 래미안아이파크 (1341) | 타지번, 옛 건물과 신축 건물 구분 |
| 디오엠프리미어 (방배동 808-3) | 지브 희담재 (808-3) | 동일 지번, 다른 신축 건물일 가능성 |
| 효창 (효창동 3-197) | 용산데시앙포레 (288) | 타지번, 변동이력 필요 |

**주의:** 도로명주소는 건물이 교체되더라도 유지될 수 있다. 이 9개는 *필지 연계 및 건물 동일성 원문을 확인할 우선 조사 후보*이며, 기존 건물이 현존한다는 뜻이 아니다.

### 후속 원문 조회 대상
- [P1 16개 원본 지번](./map-stage9-supplementary-targets-200.csv): 9개 과거 지번 + 7개 현행 지번, 중복 제거.
- P2 184개: 나머지 196개 단지의 종전 지번. 총 200개 지번.
- 원문 API (건축HUB 건축물대장정보):
  - `BldRgstHubService/getBrRecapTitleInfo` (총괄표제부)
  - `BldRgstHubService/getBrAtchJibunInfo` (부속지번)
- 최소 2회/지번이므로 **P1 최소 32회, 전체 최소 400회**. 페이지 추가·재시도는 더 많은 요청이 든다.
- 실제 인가받은 서비스 호출량은 공공데이터포털에서 확인. 스크립트 자체 `--daily-request-cap`은 별도 제한값이다.

## 재개 가능한 수집기
- `tools/jipfit_stage9_supplemental_collect.py`
- 기존 건축HUB 3종 수집과 **다른 출력 폴더** 사용: `$HOME\Downloads\jipfit_stage9_supplemental`
- 호출 한도/인증 오류에서 중단, 완료된 지번만 건너뜀, 지번별 2종의 **전체 페이지 조회가 완료된 뒤** JSON 저장
- 원본 데이터·DB·지도 쓰기 없음. **실제 두 API 조회/응답 검증은 아직 실행하지 않았음.**
- 유닛 테스트: `tools/test_jipfit_stage9_supplemental_collect.py`

### PowerShell 실행 예시
```powershell
# API 호출 없음
python tools/jipfit_stage9_supplemental_collect.py `
  --targets audits/map-stage9-supplementary-targets-200.csv `
  --results "$HOME\Downloads\jipfit_stage9_supplemental" `
  --scope p1 --dry-run

# 한 지번 시험 조회(실제 서비스 허용량을 확인한 후)
$env:DATA_GO_KR_API_KEY = Read-Host "공공데이터포털 디코딩 키"
python tools/jipfit_stage9_supplemental_collect.py `
  --targets audits/map-stage9-supplementary-targets-200.csv `
  --results "$HOME\Downloads\jipfit_stage9_supplemental" `
  --scope p1 --max-records 1 --daily-request-cap 100

# 성공 후 P1 전체 자동 이어받기
python tools/jipfit_stage9_supplemental_collect.py `
  --targets audits/map-stage9-supplementary-targets-200.csv `
  --results "$HOME\Downloads\jipfit_stage9_supplemental" `
  --scope p1 --max-records 0 --daily-request-cap 100
```

실제 공공데이터포털 키는 GitHub에 업로드하거나 메시지로 공유하지 않는다. `--daily-request-cap 100`은 승인량이 100회라는 진술이 아닌 예시이며, 실제 승인된 한도 아래에서만 설정한다.

## 감사 파일
- [196개 전체 9차 후속 표](./map-stage9-unresolved196-2026-11-21.csv)
- [9개 단지/47개 현행 건물 PK 후보](./map-stage9-same-road-candidates-2026-11-21.csv)
- [200 지번 우선 조회 순서](./map-stage9-supplementary-targets-200.csv)
- [8차 원본 품질검사 결과](./map-stage8-full9188-reverse-match-2026-11-21.md)

## 미완료
실제 P1의 총괄표제부·부속지번 원문 수집과 결과 해석, 원건물 동별 변동사항, 토지대장 합필·분할 이력, 재건축 편입 필지 조서 조회 및 최종 196개 판정은 **미완료**.

출처: [건축HUB 건축물대장정보 서비스](https://www.data.go.kr/data/15134735/openapi.do), [총괄표제부](https://www.hub.go.kr/portal/opn/tyb/idx-bdrg-totldr.do), [부속지번](https://www.hub.go.kr/portal/opn/tyb/idx-bdrg-anx.do).
