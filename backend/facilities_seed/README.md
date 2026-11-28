# 시설 자료와 갱신

updater가 공공자료를 DB에 갱신하고, 화면은 `/api/facilities`로 읽는다. 화면의 "자료 기준" 문구는 DB의 `facility_sources`다. 이 폴더의 JSON은 DB 표가 비어 있을 때 updater가 처음 적재하는 초기 사본(시드)이다.

| 항목 | 원자료 및 초기 좌표 사본 |
|---|---|
| 총세대수·총주차대수 | [서울시 공동주택 아파트 정보](https://data.seoul.go.kr/dataList/OA-15818/S/1/datasetView.do), [초기 CSV 사본](https://github.com/impelfin/python/blob/b25864db858bd824885bae025cbbd6fc08399ed2/dash/data/seoul_house.csv) |
| 역명·노선·좌표 | [서울 교통빅데이터플랫폼](https://t-data.seoul.go.kr/category/dataviewopenapi.do?data_id=1036), [초기 좌표 및 가공 방법](https://github.com/daehyub71/seoul-subway-insight/blob/main/docs/STATION_COORDS.md). 초기 자료는 2026년 6월 말까지 관측된 역 중 좌표가 있는 역을 포함한다. |
| 학교명·학교급·좌표 | [나이스 학교기본정보](https://open.neis.go.kr/), [서울 학교 좌표 사본](https://github.com/khuda-data/6th-ML-team4/blob/ffe02075089f3799c4ad1e29a002ffc051d47fa4/데이터/school.csv). 서울 소재 초·중·고만 포함한다. |

세대당 주차대수는 총주차대수 ÷ 세대수다. 법정동과 정규화한 단지명이 같고 좌표 차이가 350 m 이내인 일대일 후보만 연결한다. 거리 지표는 자료에 포함된 시설과 단지 좌표 사이의 직선거리다. 거리가 같으면 원자료에서 먼저 나온 시설을 고른다.

## 갱신

| 시점 | 하는 일 |
| :--- | :--- |
| updater 시작 | 비어 있는 표를 이 폴더의 사본으로 채우고, `complex_facilities`가 비었으면 한 번 계산한다 |
| 월간(1일, 검증 메타데이터 뒤·멸실 판정 앞) | 출처별 공공자료를 받아 통째로 교체한다. 그 달에 성공한 출처는 다시 받지 않는다 |
| 일간(마지막 단계) | 새 단지·좌표·주소 변경을 반영해 `complex_facilities`를 다시 계산한다 |

- 키는 `.env`의 `TDATA_SUBWAY_API_KEY`(역), `NEIS_API_KEY`(학교)다. 공동주택은 키가 필요 없다.
- 키가 없으면 그 출처는 저장된 자료를 유지한다. 요청이 실패하거나 받은 행이 저장된 행의 70% 미만이어도 유지하고, 월간 재시도에서 다시 받는다.

바로 갱신할 때는 먼저 반영 없이 받은 행 수만 확인한다.

```powershell
docker compose -f compose.yaml -f compose.pages.yaml --profile updates exec updater python facilities.py refresh --dry-run
```

`--dry-run`을 빼면 반영한다. 원자료는 그대로 두고 다시 계산만 하려면 `refresh` 대신 `sync`를 쓴다.

## 신설·이전 학교

학교는 `facility_school_anchors`의 이름·학교급·도로명주소가 일치해야 갱신 대상이다. 학교가 새로 생기거나 옮기면 검증한 좌표를 넣은 뒤 반영한다.

```powershell
docker compose -f compose.yaml -f compose.pages.yaml exec db psql -U jipfit -d jipfit -c "INSERT INTO facility_school_anchors VALUES ('<학교명>', '초등학교', '<NEIS 도로명주소>', 37.5, 127.0);"
```

반영은 다음 월간 갱신 때 한다. 이번 달에 이미 학교를 갱신했다면 `facility_sources`의 `schools` 행에서 `refreshed_month`를 비운 뒤 `facilities.py refresh`를 실행한다.

```powershell
docker compose -f compose.yaml -f compose.pages.yaml exec db psql -U jipfit -d jipfit -c "UPDATE facility_sources SET refreshed_month = NULL WHERE source = 'schools';"
```
