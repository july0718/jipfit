# 시설 자료와 갱신

공식 배포용 빌드는 자치구별 월간 단지 JSON에 시설 요약을 합친다. 원본 거래 DB와 지도 존치 판정은 기존 자료를 사용한다. 각 배포의 자료 기준은 `/metadata/facility-sources.txt`에서 확인한다.

| 항목 | 원자료 및 초기 좌표 사본 |
|---|---|
| 총세대수·총주차대수 | [서울시 공동주택 아파트 정보](https://data.seoul.go.kr/dataList/OA-15818/S/1/datasetView.do), [초기 CSV 사본](https://github.com/impelfin/python/blob/b25864db858bd824885bae025cbbd6fc08399ed2/dash/data/seoul_house.csv) |
| 역명·노선·좌표 | [서울 교통빅데이터플랫폼](https://t-data.seoul.go.kr/category/dataviewopenapi.do?data_id=1036), [초기 좌표 및 가공 방법](https://github.com/daehyub71/seoul-subway-insight/blob/main/docs/STATION_COORDS.md). 초기 자료는 2026년 6월 말까지 관측된 역 중 좌표가 있는 역을 포함한다. |
| 학교명·학교급·좌표 | [나이스 학교기본정보](https://open.neis.go.kr/), [서울 학교 좌표 사본](https://github.com/khuda-data/6th-ML-team4/blob/ffe02075089f3799c4ad1e29a002ffc051d47fa4/데이터/school.csv). 서울 소재 초·중·고만 포함한다. |

세대당 주차대수는 총주차대수 ÷ 세대수다. 법정동과 정규화한 단지명이 같고 좌표 차이가 350 m 이내인 일대일 후보만 연결한다. 거리 지표는 자료에 포함된 시설과 단지 좌표 사이의 직선거리다.

GitHub Actions의 기존 월간 갱신 시 공개 아파트 CSV와 시설 자료를 갱신한다. `TDATA_SUBWAY_API_KEY`, `NEIS_API_KEY` 저장소 시크릿이 있으면 역 좌표와 학교 명부를 조회한다. 학교는 기존 좌표 자료와 이름·학교급·도로명주소가 일치해야 갱신 대상이다. 학교 신설·이전은 `school-anchors.json`에 검증한 좌표를 추가한 뒤 반영한다. 키가 없거나 응답이 불완전하면 직전 자료를 사용한다.
