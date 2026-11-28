# 월간 멸실·재건축 판정 운영 안내

updater의 월간 작업이 매달 1일 06:00(KST)에 K-apt 갱신과 검증 메타데이터 반영을 마친 뒤 모든 단지의 존치를 판정합니다. 확정 판정은 바로 DB에 반영되고, 정적 메타데이터는 07:45 Pages 작업이 다시 배포합니다. **운영자가 할 일은 보류 단지를 판정해 반영하는 것입니다.**

설계는 저장소 루트의 `plan-lifecycle-auto.md`, 코드는 `backend/lifecycle_check/`와 `backend/lifecycle_admin.py`에 있습니다.

## 반영되는 것

| 판정 | DB | 지도 |
| :--- | :--- | :--- |
| 새 단지 현존 | 현재 판정 `present` | 변화 없음 |
| 대장 말소 자동 확정 | `demolished`, 효력일, 학습 제외 갱신 | 마커 제거 |
| 기존 멸실 단지의 후속 연결 | 후속 ID 추가(효력일 유지) | 변화 없음 |
| 보류 | 보류 코드만 기록 | 변화 없음 |
| 효력일 이후 거래가 있는 새 확정 | `말소후거래` 보류 | 변화 없음 |

자동 판정은 멸실을 현존으로 되돌리지 않습니다. 그런 신호는 `review.csv`에만 올립니다.

## 자료 폴더 `lifecycle-data/` (Git에 올리지 않음)

| 경로 | 내용 | 보존 |
| :--- | :--- | :--- |
| `reference/` | 전환 때 복사한 기준 자료(2026-11-26). updater에는 읽기 전용 | 영구 |
| `monthly/<YYYY-MM>/` | 그 달에 받은 대장 | 최근 2개월 |
| `batches/monthly-<날짜>/` | `manifest.json`, `results.jsonl`, `proposals.jsonl`, `fingerprints.json` | 영구 |
| `reports/monthly-<날짜>/` | `report.md`, `auto_confirmed.csv`, `review.csv`, `holds.csv` | 영구 |

`reference/`와 DB의 `complex_lifecycle_*` 표는 다음 달 판정의 기준이므로 함께 백업합니다.

## 설정 (`.env`)

| 변수 | 기본값 | 뜻 |
| :--- | ---: | :--- |
| `JIPFIT_LIFECYCLE_AUTO` | `0` | `1`일 때만 월간 작업이 판정합니다 |
| `JIPFIT_LIFECYCLE_MAX_AUTO` | `20` | 한 달 자동 확정 한도. 넘으면 그 달 확정을 모두 `대량확정보류`로 둡니다 |
| `JIPFIT_LIFECYCLE_COLLECT_MINUTES` | `90` | 한 번의 시도에서 수집에 쓰는 시간 |
| `JIPFIT_LIFECYCLE_READ_ONCE` | `1` | 한 번의 실행은 같은 캐시 파일을 디스크에서 한 번만 읽습니다. `0`은 문제 해결용으로, 매번 다시 읽습니다 |

인증키는 `MOLIT_SERVICE_KEY`를 씁니다.

## 전환 (한 번)

저장소 루트에서 실행합니다.

### 0. 먼저 확인

- `MOLIT_SERVICE_KEY`가 다음 네 가지 data.go.kr API에 모두 활용 신청된 키인지 확인합니다: 건축HUB **건축물대장**(`BldRgstHubService`), **폐쇄말소대장**(`ShtRgstHubService`), **건축인허가**(`ArchPmsHubService`, 철거·멸실), 국토부 **아파트 매매 실거래가 상세**(`RTMSDataSvcAptTradeDev`, 새 법정동 코드 확인).
- 한 달 조회가 대장 종류마다 약 9,300건(합계 약 28,000건)입니다. 각 API의 하루 호출 한도가 이보다 큰지 확인합니다.
- Docker에 메모리 여유가 있는지 확인합니다. 판정 중 updater는 약 1.2GB를 씁니다(2026-11-28 거래 145만 행, 판정만 측정: 1,114MB. 파일 한 번 읽기를 끄면 455MB).
- 디스크에 1.5GB 이상의 여유가 필요합니다(기준 자료 약 560MB와 월별 캐시).

### 1. DB 전체 백업

```powershell
docker compose -f compose.yaml -f compose.pages.yaml exec -T db pg_dump -U jipfit -d jipfit -Fc -f /tmp/before_lifecycle_auto.dump
```

```powershell
docker compose -f compose.yaml -f compose.pages.yaml cp db:/tmp/before_lifecycle_auto.dump .
```

### 2. 기준 자료 복사

updater를 다시 만들기 **전에** PC에서 실행합니다(Python 표준 라이브러리만 씁니다). `<jipfit_lifecycle>`은 검증기 저장소 경로입니다. 검증기 폴더를 읽는 단계는 이것뿐입니다.

```powershell
python backend/lifecycle_admin.py import-reference --from <jipfit_lifecycle>/source_cache --to lifecycle-data/reference
```

판정이 읽는 폴더만 복사하고 `lifecycle-data/reference/MANIFEST.json`에 파일 목록과 SHA-256을 남깁니다. 약 33,000개 파일, 560MB입니다.

### 3. updater 재빌드 (06:00 전후는 피함)

`.env`의 `JIPFIT_LIFECYCLE_AUTO`는 아직 넣지 않습니다.

```powershell
docker compose -f compose.yaml -f compose.pages.yaml --profile updates up -d --build --no-deps updater
```

### 4. 지문 저장

```powershell
docker compose -f compose.yaml -f compose.pages.yaml --profile updates exec updater python lifecycle_admin.py seed-fingerprints --as-of 2026-11-26
```

`지문 9,219개 지번 저장`과 `원본 지문과 같습니다`가 나와야 합니다.

### 5. 리허설 (반영 없음)

```powershell
docker compose -f compose.yaml -f compose.pages.yaml --profile updates exec updater python lifecycle_admin.py run --dry-run --offline
```

기준 자료만으로 판정해 `lifecycle-data/reports/rehearsal-<오늘>/`에 보고서를 씁니다. 자동 확정 0곳, 결과는 기준선 뒤 등록된 새 단지 몇 건이어야 합니다(2026-11-28 백업 기준 2곳, 모두 `수집미완`).

### 6. 실제 수집 리허설 (선택)

```powershell
docker compose -f compose.yaml -f compose.pages.yaml --profile updates exec updater python lifecycle_admin.py run --dry-run
```

이번 달 캐시를 채웁니다. 시간 한도로 남은 조회가 있으면 같은 명령을 다시 실행합니다. 7일 안에 시작한 캐시만 이어 씁니다.

### 7. 자동 실행 켜기

`.env`에 `JIPFIT_LIFECYCLE_AUTO=1`을 넣고 updater를 다시 올립니다.

```powershell
docker compose -f compose.yaml -f compose.pages.yaml --profile updates up -d --no-deps updater
```

- 이번 달의 월간 작업 기록이 아직 완료 상태가 아니면 올리자마자 이번 달 판정이 시작될 수 있습니다.
- 이번 달 월간 작업이 이미 끝났으면 다음 달 1일부터 자동입니다. 이번 달 판정을 바로 반영하려면 다음 명령을 실행합니다.

```powershell
docker compose -f compose.yaml -f compose.pages.yaml --profile updates exec updater python lifecycle_admin.py run
```

`run`은 자동 작업과 같은 실행 기록(`complex_lifecycle_runs`)을 써서 같은 달을 두 번 반영하지 않습니다.

## 매달 확인

```powershell
docker compose -f compose.yaml -f compose.pages.yaml logs --tail 100 updater
```

```text
Lifecycle monthly-20261201: 대상 9,290 / 수집 완료 / 현존 확인 12 / 자동 확정 3 / 보류 5(운영자 확인 4) / 후속 연결 2
Lifecycle 파일 읽기: 디스크 31,632회(파일 31,632개) · 재사용 93,145회 · 최대 메모리 1,114MB · 판정 3분 6초
```

`파일 읽기` 줄은 실행(자동 시도, `run`, `run --dry-run`, `seed-fingerprints`)이 끝날 때 한 번 나옵니다.

- `디스크`와 `파일` 수가 같으면 파일마다 한 번만 읽은 것입니다. `재사용`은 읽어 둔 내용을 다시 쓴 횟수입니다.
- `최대 메모리`는 이번 실행만이 아니라 그 프로세스가 시작한 뒤의 최댓값입니다.
- `판정`은 수집을 뺀 판정 시간입니다. 판정하지 않은 실행(수집 재시도, 이미 반영한 달, 지문 저장)에는 없습니다.

```powershell
docker compose -f compose.yaml -f compose.pages.yaml exec db psql -U jipfit -d jipfit -c "SELECT month, status, attempts, remaining_calls, batch, last_error FROM complex_lifecycle_runs ORDER BY month DESC LIMIT 3;"
```

첫 몇 달은 `reports/monthly-<날짜>/auto_confirmed.csv`를 훑어보는 것을 권합니다(필수 아님). 첫 실제 실행에서는 기준 자료의 K-apt 정보와 정비사업 페이지가 2026-11-26에 멈춰 있으므로, 새 단지 보류가 늘어나는지도 확인합니다.

## 보류 처리

```powershell
docker compose -f compose.yaml -f compose.pages.yaml --profile updates exec updater python lifecycle_admin.py holds
```

열린 보류를 출력하고 `lifecycle-data/reports/holds-<오늘>.csv`로 저장합니다. `수집미완`은 `--all`을 붙여야 보입니다.

| 보류 코드 | 뜻 | 조치 |
| :--- | :--- | :--- |
| `수집미완` | 대장을 다 받지 못함 | 없음. 다음 달 자동 재판정 |
| `식별실패` | 주소로 지번키를 만들지 못함 | 주소(구·지번) 확인 후 대장을 직접 보고 `resolve` |
| `기록없음` | 현행·폐쇄 주거동 기록이 모두 없음 | 건축HUB·K-apt 확인 후 `resolve` |
| `일부존치` | 같은 연도의 현행·폐쇄 주거동이 함께 있음 | 남은 동이 단지 본체면 `present` |
| `범위미확인` | 말소 동수·세대수·총괄표제부가 맞지 않음 | 단지 전체가 말소됐는지 확인 |
| `말소후거래` | 마지막 계약이 말소일 이후 | 계약 성격 확인. 멸실이면 효력일 없이 `--trade-caution-from` |
| `철거후거래` | 철거 기록이 마지막 계약보다 앞섬 | 위와 같음 |
| `단기말소` | 사용승인 5년 안 말소 | 대장 재발급인지 확인. 대부분 `present` |
| `타지번잔존` | 다른 지번에 같은 이름·연도 주거동이나 K-apt 단지 | 다른 지번 이전 등록인지 확인 |
| `대량확정보류` | 자동 확정이 한도를 넘음 | `auto_confirmed.csv`를 보고 `accept` |

### 판정 반영 (`resolve`)

먼저 `--dry-run`으로 바뀔 존치·효력일·학습 제외·마커를 확인합니다.

```powershell
docker compose -f compose.yaml -f compose.pages.yaml --profile updates exec updater python lifecycle_admin.py resolve "<complex_id>" --existence present --evidence "<확인한 근거>" --dry-run
```

```powershell
docker compose -f compose.yaml -f compose.pages.yaml --profile updates exec updater python lifecycle_admin.py resolve "<complex_id>" --existence demolished --evidence "<확인한 근거>" --effective-date 2026-11-30 --successor-id "<후속 complex_id>"
```

- `--precision day|month|year`는 `--effective-date`와 함께 씁니다(기본 `day`).
- 효력일 이후 계약이 있으면 거절합니다. `--effective-date` 없이 `--trade-caution-from <날짜>`를 쓰면 그 날짜부터의 계약을 학습에서 뺍니다.
- 효력일이 있는 멸실 단지를 다시 `demolished`로 판정할 때 `--effective-date`를 빼면 효력일이 지워집니다(명령이 알려 줍니다).
- 다음 달부터 이 판정이 유지됩니다. `present`로 둔 단지는 새 말소 기록이 생길 때만 다시 판정합니다.

### 대량확정보류 (`accept`)

```powershell
docker compose -f compose.yaml -f compose.pages.yaml --profile updates exec updater python lifecycle_admin.py accept --batch monthly-20261201 --all --dry-run
```

`--all` 대신 단지 ID를 하나 이상 적을 수 있습니다. 그사이 판정이 바뀐 단지는 건너뛰고, 효력일 이후 거래가 있으면 `말소후거래` 보류가 됩니다.

> **다음 달 1일 전에 처리하세요.** 처리하지 않은 `대량확정보류` 단지는 다음 달에 다시 판정합니다. 그달의 확정 수가 한도 이하이면 **자동 반영됩니다.** 반영하면 안 되는 단지는 `resolve --existence present`로 정합니다.

반영 결과는 공개 API에 바로 보입니다. 정적 메타데이터를 바로 바꾸려면 GitHub에서 `deploy-pages.yml`을 수동 실행합니다.

## 되돌리기

```powershell
docker compose -f compose.yaml -f compose.pages.yaml --profile updates exec updater python lifecycle_admin.py revert --batch monthly-20261201 --complex "<complex_id>" --dry-run
```

`--complex`를 빼면 그 묶음이 바꾼 모든 단지를 되돌립니다. 묶음 직전의 확정 판정으로 정정합니다.

- 직전 확정이 없던 단지(새 단지)는 알리기만 합니다. 잘못 멸실 처리됐으면 `resolve --existence present`를 씁니다.
- `accept`나 `resolve`로 넣은 판정, 묶음 뒤에 다시 바뀐 단지는 건너뜁니다. `resolve`로 바로잡습니다.
- 지문은 되돌리지 않습니다. 다음 달이 지금 대장 기록으로 다시 비교합니다.
- 큰 사고는 1단계의 `pg_dump` 백업으로 복원합니다.

## 문제가 생겼을 때

| 로그·상태 | 뜻 | 조치 |
| :--- | :--- | :--- |
| `lifecycle:collecting` | 수집이 덜 끝남 | 30분 뒤 자동 재시도. 4번째 시도는 받은 자료로 판정합니다 |
| `collecting`인데 재시도가 끝남 | 중간에 오류로 중단 | `last_error` 확인 후 `lifecycle_admin.py run` |
| `judged` | 묶음은 만들었고 반영이 남음 | 일일 작업이 이어서 반영합니다 |
| `lifecycle:reference`, `lifecycle:key` | 기준 자료나 인증키가 없음 | 전환 2단계, `.env` 확인 |
| `lifecycle:locked` | 다른 판정이 실행 중 | 끝난 뒤 다시 실행 |
| `lifecycle:month-changed` | 월말에 수집하다 달이 바뀜 | 새 달의 실행이 판정합니다 |
| `Lifecycle ...: rejected` | 묶음 파일이 손상됨 | 묶음은 반영되지 않습니다. 원인을 확인해야 합니다 |
| 메모리 부족이나 읽어 둔 파일이 의심됨 | 파일 한 번 읽기 문제 | `.env`에 `JIPFIT_LIFECYCLE_READ_ONCE=0`을 넣고 updater만 다시 올립니다(아래). 판정 결과는 같고 느려집니다 |

- API가 크게 실패한 달에는 4번째 시도가 받은 자료로 판정해, 기존 보류의 코드가 `수집미완`으로 바뀌고 `holds` 기본 목록에서 빠질 수 있습니다. `remaining_calls`가 크면 `holds --all`로 확인합니다.
- 응답 형식 오류 원문은 인증키를 가린 채 `monthly/<YYYY-MM>/format_errors/`에 남습니다.
- 한 달을 건너뛰려면 `complex_lifecycle_runs`에서 그 달 상태를 `disabled`로 둡니다.
- **월간 실행 중에는 `import-reference`를 하지 않습니다.** 실행은 한 번 읽은 기준 자료를 다시 확인하지 않습니다.

### 파일 한 번 읽기 끄기

`.env`에 `JIPFIT_LIFECYCLE_READ_ONCE=0`을 넣고 updater만 다시 올립니다(05:30~07:30 KST는 피함). 되돌릴 때는 그 줄을 지우고 같은 명령을 실행합니다.

```powershell
docker compose -f compose.yaml -f compose.pages.yaml --profile updates up -d --no-deps updater
```

수동 실행 한 번만 끄려면 다시 올리지 않고 `exec` 뒤에 `-e JIPFIT_LIFECYCLE_READ_ONCE=0`을 붙입니다.

```powershell
docker compose -f compose.yaml -f compose.pages.yaml --profile updates exec -e JIPFIT_LIFECYCLE_READ_ONCE=0 updater python lifecycle_admin.py run --dry-run
```
