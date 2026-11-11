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
#    apply_theme.js(pptx 스킬의 scripts/)가 jszip을 찾도록 NODE_PATH를 이 디렉터리의 node_modules로:
#    APPLY_THEME_JS=<…>/pptx/scripts/apply_theme.js NODE_PATH=$PWD/node_modules node build_deck.js ../../docs/handbook/sbmig-overview.pptx
```

엑셀의 '에러카탈로그' 시트는 `docs/ERROR-HANDLING.md`의 표를, 나머지 시트는 코드·설정·지식 파일을 그대로 읽습니다. 시트 설명의 개수(핸들러 N종, 레시피 N개 …)는 추출한 행 수로 계산합니다. PPT의 숫자(테스트 수, 픽스처 수, 핸들러·레시피·어드바이저리 수, 설정 키 수)는 `build_deck.js`에 직접 적혀 있으므로 라운드마다 고칩니다(round 10: 2,192 / 32 / 32 · 50 · 67 / 212).

시트 구성과 추출 출처는 엑셀의 `목차` 시트와 각 시트 2행에 적혀 있습니다. 마크다운 핸드북과 코드의 불일치는
`tests/docs/test_handbook.py`가 검사합니다.

## 흐름 안내 덱 (`docs/migration-agent/migration-agent.pptx`, 2026-11-11)

담당자에게 "이렇게 흘러갑니다" 를 보여 주는 16장짜리 덱입니다: 전체 흐름 한 장, **단계마다 한 장**(파드 준비 · 접수 · 분석 · 계획 ·
자동 변환 · 구조 정리 · 구조 검토 · 빌드와 수정 · 마무리), 수정 루프 네 장, 지식베이스 한 장. `build_migration_agent_deck.js` 가 만들고, 흐름도 네 장은
**파워포인트 도형**(둥근 네모 + 선 + 글)으로 그리므로 파워포인트에서 글자와 칸을 바로 고칠 수 있습니다(그림 파일 없음;
선은 칸에 붙어 있지 않아 칸을 옮기면 선도 옮겨야 합니다). 설정 키·파일 이름 같은 세부 사항은 일부러 뺐습니다.
세부 설계까지 담은 37장짜리 버전(Mermaid 그림)은 커밋 `a4c3827` 에 있습니다.

```bash
cd scripts/handbook && NODE_PATH=$PWD/node_modules APPLY_THEME_JS=<pptx 스킬>/scripts/apply_theme.js node build_migration_agent_deck.js ../../docs/migration-agent/migration-agent.pptx
```

흐름도 함수(`drawOverview` / `drawLoop` / `drawLadder` / `drawKB`)는 `node(제목, 설명, 종류)` 로 칸을 놓고 `seg` / `route` 로 선을 긋습니다.

단계별 쪽은 `steps/<단계>.js` 한 파일이 한 장입니다(`title` / `notes` / `lines` / `draw(s, H)`). 그림은 `H.AREA` 안에만 그리고, 위의
단계 띠와 아래 글은 빌드 스크립트가 놓습니다. 칸에는 설정 키나 파일 경로를 쓰지 않고 발표자 노트(`notes`)에 적습니다. 한 단계만
고쳐서 빨리 보려면 `ONLY=intake,analyze` 를 앞에 붙여 다른 파일 이름으로 만듭니다. 코드가 바뀌면 그 단계의 파일을 고칩니다.
