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

## 흐름 안내 덱 (`docs/migration-agent/migration-agent.pptx`, 2026-10-29)

담당자에게 "이렇게 흘러갑니다" 만 보여 주는 10장짜리 덱입니다. `build_migration_agent_deck.js` 가 만들고, 그림은
`docs/migration-agent/diagrams/flow-*.mmd`(네모칸마다 굵은 제목 + 작은 설명)를 mermaid-cli 로 렌더링한 PNG 입니다.
설정 키·파일 이름 같은 세부 사항은 일부러 뺐습니다. 세부 설계까지 담은 37장짜리 버전은 커밋 `a4c3827` 에 있습니다
(그 그림 `diagrams/v2-*.mmd` 는 그대로 남겨 두었습니다).

```bash
cd docs/migration-agent/diagrams && for f in flow-*.mmd; do npx -y -p @mermaid-js/mermaid-cli mmdc -i "$f" -o "${f%.mmd}.png" -b white -s 3 -c flow-config.json; done
cd scripts/handbook && NODE_PATH=$PWD/node_modules APPLY_THEME_JS=<pptx 스킬>/scripts/apply_theme.js node build_migration_agent_deck.js ../../docs/migration-agent/migration-agent.pptx
```

mermaid-cli 는 SVG 를 뷰포트 폭에 맞춰 줄이므로, 그림이 가로로 길수록 슬라이드에서 글씨가 작아집니다. 네모칸 설명은 한 줄로,
한 줄에 네 칸 이하로 두고 줄이 길어지면 `subgraph` 로 줄을 나눕니다(줄 사이 링크는 `~~~`, 줄을 가로지르는 화살표는 방향을 깨뜨림).
