# scripts/handbook — 핸드북 제출물 생성 스크립트

`docs/handbook/sbmig-reference.xlsx`(표 레퍼런스)와 `docs/handbook/sbmig-overview.pptx`(발표 자료)를 코드에서 다시 만드는
스크립트입니다. 코드·설정·지식 파일이 바뀌면 엑셀을 다시 생성하십시오(표는 전부 추출본입니다).

```bash
# 1) 표 데이터 추출 (저장소 venv — sbmig를 import 합니다)
.venv/bin/python scripts/handbook/extract_reference.py out/handbook-data.json
# 2) 엑셀 생성 (openpyxl이 있는 파이썬) + LibreOffice 재계산(선택)
python3 scripts/handbook/build_xlsx.py out/handbook-data.json docs/handbook/sbmig-reference.xlsx
# 3) PPT 생성 (node + pptxgenjs; 테마 색상은 APPLY_THEME_JS=<apply_theme.js 경로>를 주면 적용)
cd scripts/handbook && npm install pptxgenjs && node build_deck.js ../../docs/handbook/sbmig-overview.pptx
```

시트 구성과 추출 출처는 엑셀의 `목차` 시트와 각 시트 2행에 적혀 있습니다. 마크다운 핸드북과 코드의 불일치는
`tests/docs/test_handbook.py`가 검사합니다.
