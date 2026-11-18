# scripts/handbook — 제출 문서 생성 스크립트

현행(v2) 제출물 두 가지를 다시 만드는 스크립트만 남아 있습니다.

| 스크립트 | 만드는 파일 | 필요한 것 |
|---|---|---|
| `build_migration_agent_deck.js` + `steps/` | `docs/migration-agent/migration-agent.pptx` (흐름 안내 덱) | node + `pptxgenjs` (`node_modules/`) |
| `build_kb_map.py` + `vendor/mermaid.min.js` | `docs/kb-map/index.html` (KB 데이터 지도, 한 파일) | 저장소 venv 파이썬 |

## 흐름 안내 덱 (`docs/migration-agent/migration-agent.pptx`)

프로젝트를 올리는 팀(사용자)과 담당자에게 "이렇게 흘러가고, 이것을 받아 이렇게 보면 됩니다" 를 보여 주는 20장짜리 덱입니다.
슬라이드와 발표자 노트는 그대로 사용자에게 나갑니다. 설정 키·파일 경로·함수 이름·종료 코드는 쓰지 않고(사용자가 내려받는
report.md / result.zip / changes.patch / errors.md 는 예외), 사용자가 화면과 보고서에서 보는 말(전체 진행, 취약점 해결,
「확인해 주세요」 …)을 씁니다.

| 쪽 | 내용 | 만드는 곳 |
|---|---|---|
| 1 | 표지 | `build_migration_agent_deck.js` |
| 2 | 전체 흐름 (12단계: ⑩ 최종 빌드 → ⑪ 취약점 조치 → ⑫ 마무리, 코드가 실행하는 순서) | `drawOverview` |
| 3 | 프로젝트에 따라 길이 달라집니다 (전체 진행 / 취약점 해결, 사내 프레임워크, 받지 않는 프로젝트) | `drawRoads` |
| 4–11 | ① 파드 준비 … ⑧~⑩ 빌드와 수정 | `steps/pod.js` … `steps/verify.js` |
| 12–15 | 수정 루프 네 장 | `drawLoop` / `drawLadder` / 표 |
| 16 | ⑪ 취약점 조치 | `steps/remediation.js` |
| 17 | ⑫ 마무리 | `steps/finalize.js` |
| 18 | 보고서는 이렇게 읽습니다 | `readingRows` |
| 19 | 취약점 조치 결과는 이렇게 봅니다 | `readingRows` |
| 20 | 지식베이스 | `drawKB` |

흐름도는 전부 **파워포인트 도형**(둥근 네모 + 선 + 글)이라 파워포인트에서 글자와 칸을 바로 고칠 수 있습니다(그림 파일 없음;
선은 칸에 붙어 있지 않아 칸을 옮기면 선도 옮겨야 합니다). 흐름도 함수는 `node(제목, 설명, 종류)` 로 칸을 놓고 `seg` / `route` 로
선을 긋습니다. 칸의 종류(색): `auto` 자동 처리, `build` Maven 빌드, `llm` LLM, `loop` 반복·사람 몫, `scan` 취약점 검사·조치,
`done` 끝, `ask` 질문·받는 것.

단계별 쪽은 `steps/<단계>.js` 한 파일이 한 장입니다(`title` / `notes` / `lines` / `draw(s, H)`). 그림은 `H.AREA` 안에만 그리고, 위의
단계 띠(`STEPS`, 열 칸)와 아래 글은 빌드 스크립트가 놓습니다. 코드가 바뀌면 그 단계의 파일을 고칩니다.

### 다시 만들고 확인하기

저장소 루트에서(`SK` = pptx 스킬 폴더, 이 PC 에서는
`/Users/acandiatale/.claude/skills/synced/f7ef29cf-cf4a-4485-b6d4-72876513888d_c9c548d6-117f-4a04-857f-5de6580c7abd/pptx`).
모든 명령은 디스크·시간 상한 래퍼 `scripts/bounded.py` 로 돌리고, 임시 파일은 `out/scratch/deck/` 에만 둡니다.

```bash
cd scripts/handbook && npm install pptxgenjs   # 처음 한 번
cd <repo>
B=".venv/bin/python scripts/bounded.py --file-mb 128 --seconds 600 --tmp-mb 1024 --"
# 1) 만들기 (APPLY_THEME_JS 를 빼면 오피스 기본 색으로 만들어진다. 내용은 같다)
(cd scripts/handbook && NODE_PATH=$PWD/node_modules APPLY_THEME_JS=$SK/scripts/apply_theme.js \
  ../../.venv/bin/python ../bounded.py --file-mb 128 --seconds 600 --tmp-mb 1024 -- \
  node build_migration_agent_deck.js ../../docs/migration-agent/migration-agent.pptx)
# 2) 구조 검사 — "All validations PASSED!" 가 나와야 한다
$B .venv/bin/python $SK/scripts/office/validate.py docs/migration-agent/migration-agent.pptx
# 3) 눈으로 보기: PDF → 쪽마다 JPG (soffice.py 는 저장소 venv 파이썬으로: 시스템 python3 에는 defusedxml 이 없다)
mkdir -p out/scratch/deck/check
$B .venv/bin/python $SK/scripts/office/soffice.py --headless --convert-to pdf --outdir out/scratch/deck/check docs/migration-agent/migration-agent.pptx
$B pdftoppm -r 75 -jpeg out/scratch/deck/check/migration-agent.pdf out/scratch/deck/check/s
```

JPG 를 한 장씩 열어 칸 밖으로 넘친 글, 글을 가로지르는 선, 슬라이드 밖으로 나간 칸, 선 위에 겹친 꼬리표를 찾습니다.
LibreOffice 는 굵은 글씨의 이모지(✅ ❌)를 빈 네모로 그리므로 칸 제목에는 이모지를 쓰지 않습니다. 한 단계만 고쳐서 빨리 보려면
`ONLY=intake,analyze` 를 앞에 붙여 다른 파일 이름으로 만듭니다.

## KB 데이터 지도 (`docs/kb-map/index.html`)

`docs/kb-map/kb-map.html` 이 원본이고, `build_kb_map.py` 가 Mermaid(`vendor/mermaid.min.js`, 11.x)를 그 안에 넣어
인터넷 없이 더블클릭으로 열리는 한 파일 `index.html` 을 씁니다. 원본을 고친 뒤 다시 실행하십시오.

```bash
.venv/bin/python scripts/handbook/build_kb_map.py            # vendor 의 Mermaid 사용
.venv/bin/python scripts/handbook/build_kb_map.py <mermaid.min.js>   # vendor 를 새 버전으로 바꾸면서
```
