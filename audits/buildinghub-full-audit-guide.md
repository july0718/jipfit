# 서울 9,188개 지번 건축HUB 전수조사 — 안전 재개형 실행 안내

2026-11-21 기준. 집핏 7·8단계의 439개 판정/지도 표시 작업과 **별개로**, 서울 25개 구 전체 9,188개 고유 지번에서 이미 수집한 JSON을 재사용하면서 나머지 지번을 조사하는 프로그램입니다.

## 구성 및 전제

- 새 코드: \`tools/jipfit_buildinghub_remaining.py\` (Python 3.10+, 표준 라이브러리만 사용)
- 단위 테스트: \`tools/test_buildinghub_remaining.py\`
- **기존 로컬 원본 스크립트**: \`C:\Users\july0\Downloads\jipfit_buildinghub_full.ps1\` (GitHub에는 없고 이 새 코드로 대체하려는 별도 파일)
- 전체 목록: \`C:\Users\july0\Downloads\jipfit_full_targets.csv\` (현재 GitHub 미포함)
- 기존 결과: \`C:\Users\july0\Downloads\jipfit_buildinghub_results\<법정동코드_대지구분_본번_부번>.json\` (현재 GitHub 미포함)
- 위 파일들은 **사용자 PC에만 있음**. ChatGPT/GitHub 저장소에서 직접 결과 수를 셀 수 없고, API 인증키 없이 원격으로 조사를 실행할 수 없습니다.
- 9,188개 전체 지번에서 종전 좌표 누락 표본 2,598개가 모두 성공한 것이라면 남은 표본은 6,590개. 실제 분모/기조사/미조사 건수는 **dry-run 결과가 유일한 기준**입니다.
- 건축물대장 \`BldRgstHubService/getBrTitleInfo\`; 폐쇄말소대장 \`ShtRgstHubService/getSrTitleInfo\`; 철거멸실관리대장 \`ArchPmsHubService/getApDemolExtngMgmRgstInfo\`.

## 안전장치

1. CSV의 5자리 시군구코드·5자리 법정동코드·대지구분·4자리 본번/부번으로 지번별 결과 파일명을 만듭니다. 예: \`11710_10700_0_0171_0000.json\`.
2. 같은 이름의 기존 **유효 JSON**이 있고 실패 표시가 없으면 재호출하지 않습니다. **주의:** 원본 \`jipfit_buildinghub_full.ps1\` 파일의 정확한 JSON 스키마를 직접 확보하지 못했으므로, 최초 dry-run의 기존 완료 집계를 기존 스크립트 통계와 비교하고 **반드시 기존 JSON 몇 건을 직접 확인**합니다. JSON 문법이 유효한데도 3개 서비스 중 일부만 저장된 파일은 자동으로 완전성을 입증할 수 없습니다.
3. 각 지번은 3개 서비스의 모든 페이지가 성공하고 \`totalCount\`가 맞을 때 **하나의 JSON으로 원자적 저장**합니다. 중간에 실패하면 불완전한 결과를 저장하지 않습니다.
4. API 본문의 \`resultCode=22\`(일일 한도)·\`resultCode=23\`(초당 한도), HTTP 429, 한도 초과 메시지 감지 시 배치 전체 중단. **한도를 우회·회피하기 위한 키 전환/자동 폭주 재시도 없음.**
5. 인증·권한 오류(HTTP 401·403 등)는 별도로 전체 중단. 일반 서버 오류·네트워크 오류는 제한적 재시도(기본 3회) 후 해당 지번 실패로 남기고 다음 지번 진행. 연속 10개 실패면 전체 중단.
6. API 호출 사이 기본 350ms 간격; 일일 로컬 호출 상한을 \`--daily-request-cap\`으로 지정할 수 있으며 이 설정은 여러 번 실행해도 한국시간 날짜별로 기록됩니다. **승인된 실제 서비스별 한도를 공공데이터포털에서 확인하고 그보다 낮게 설정**해야 합니다. 이미 다른 프로그램에서 사용한 호출량은 로컬 카운터에 포함되지 않습니다.
7. 상태 파일은 기존 JSON 결과와 충돌하지 않도록 \`jipfit_buildinghub_results\.state\` 하위에 저장. 기존 잘못된 JSON에 재수집을 성공한 경우에도 이전 파일은 \`.state\backups\`로 복사해 보존합니다.
8. 입력 CSV에 특수 블록지번(\`BL-3-1\`, \`가-238\` 등)이나 코드 누락이 있다면 임의로 환산하지 않습니다. 기본적으로 조회 전 중지하며, 정제 주소로 수동 변환 후 재실행해야 합니다.
9. DB/거래/좌표/지도상태는 변경하지 않으며, 결과 수집 뒤 존치 상태는 별도의 검증 절차에서 판정합니다. 말소 기록만으로 멸실 확정 금지.

## 로컬에서 한꺼번에 git pull한 뒤 실행

프로젝트 루트(\`jipfit\`) PowerShell 7 기준입니다.

\`\`\`powershell
python -m unittest -v tools/test_buildinghub_remaining.py

$targets = Join-Path $HOME 'Downloads\jipfit_full_targets.csv'
$results = Join-Path $HOME 'Downloads\jipfit_buildinghub_results'

# API를 한 번도 호출하지 않고 9,188개 중 기존 결과와 신규 대상 건수를 집계
python tools/jipfit_buildinghub_remaining.py --targets $targets --results $results --dry-run
\`\`\`

실행 준비가 되면 공공데이터포털의 **Decoding 인증키**를 PowerShell에서 대화형으로 입력합니다(코드·GitHub·명령 기록에 하드코딩하지 않음).

\`\`\`powershell
$secureKey = Read-Host '공공데이터포털 Decoding 인증키' -AsSecureString
$env:DATA_GO_KR_API_KEY = [System.Net.NetworkCredential]::new('', $secureKey).Password

# 예시: 실제 승인된 일일 호출한도보다 낮은 자체 상한을 선택할 것
$myDailyCap = 300
python tools/jipfit_buildinghub_remaining.py --targets $targets --results $results --max-records 200 --daily-request-cap $myDailyCap
\`\`\`

\`300\`은 **기술 예시일 뿐 공공데이터포털이 허용하는 실제 한도를 뜻하지 않습니다.** 사전에 본인 계정의 API 활용현황·한도를 확인하십시오. 페이지가 여러 개인 지번은 3회보다 더 많은 API 호출을 사용합니다. 로컬 카운터는 이 스크립트에서 발생한 호출만 추적합니다.

dry-run 결과와 기존 완료 건수가 일치하고 최초 200개 배치가 정상 검증되면 전체 잔여분을 대상으로 실행할 수 있습니다. 완료되거나 제한에 걸릴 때까지 **현재 실행 내에서만** 진행하며 백그라운드나 다음 날 자동 실행은 하지 않습니다.

\`\`\`powershell
python tools/jipfit_buildinghub_remaining.py --targets $targets --results $results --max-records 0 --daily-request-cap $myDailyCap

# 배치 완료 또는 제한 시 요약 확인
Get-Content (Join-Path $results '.state\last-run-summary.json') -Encoding UTF8
\`\`\`

- 한도에 걸리면 종료코드 3과 \`stop_reason\`을 기록하고 **즉시 중단**합니다. 다음 허용 시점에 동일 명령을 다시 실행하면 정상 JSON이 존재하는 지번은 건너뛰고 시작합니다.
- 일반 재시도 실패가 있으면 \`.state\last-run-errors.csv\`에 기록하고 종료코드 2를 반환합니다.
- 입력 전체가 9,188개가 아니면 요청을 보내지 않고 중단합니다. 의도적으로 다른 표본을 검사하는 테스트에서만 \`--expected-total 0\`을 사용하십시오.
- 유효하지 않은 지번을 확인한 후 \`--allow-invalid-targets\`로 일시적으로 건너뛸 수 있지만, 이는 **전수조사 완료가 아닙니다**. 반드시 \`.state\invalid-targets.csv\`를 추후 해결해야 합니다.
- \`.state\audit.lock\`이 남았다면 실행 중인 조사 프로세스가 정말 없는지 확인한 뒤에만 수동으로 제거합니다.

## 신규 JSON 형식 및 감사 후처리

신규 JSON에는 \`schema_version=jipfit-buildinghub-audit-v2\`, \`parcel\`, \`building_registry\`, \`closed_registry\`, \`demolition_registry\`의 원본 항목과 페이지 수·총건수를 보존합니다. 기존 PowerShell 스크립트의 결과 스키마는 아직 직접 검증하지 못했습니다. 따라서 **구형/신형 JSON을 합산하여 지도 복구 여부를 바로 확정하지 말고 먼저 양쪽 스키마의 정규화 변환기를 검증**해야 합니다.

전수조회 후 단계: (a) 지번별 3종 조회 완결성 검증 → (b) 동별 건물관리번호·폐쇄/멸실 사유·현재 주소 매칭 → (c) 현존/멸실 확정/미확정 판정 → (d) 지도 제외 목록만 별도 변경. 과거 거래내역·단지ID 삭제 금지.
