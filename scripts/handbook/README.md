# scripts/handbook — 제출 문서 생성 스크립트

현행(v2) 제출물 두 가지를 다시 만드는 스크립트만 남아 있습니다.

| 스크립트 | 만드는 파일 | 필요한 것 |
|---|---|---|
| `build_migration_agent_deck.js` + `steps/` | `docs/migration-agent/migration-agent.pptx` (흐름 안내 덱) | node + `pptxgenjs` (`node_modules/`) |
| `build_kb_map.py` + `vendor/mermaid.min.js` | `docs/kb-map/index.html` (KB 데이터 지도, 한 파일) | 저장소 venv 파이썬 |

## 흐름 안내 덱 (`docs/migration-agent/migration-agent.pptx`)

담당자에게 "이렇게 흘러갑니다" 를 보여 주는 16장짜리 덱입니다: 전체 흐름 한 장, **단계마다 한 장**(파드 준비 · 접수 · 분석 · 계획 ·
자동 변환 · 구조 정리 · 구조 검토 · 빌드와 수정 · 마무리), 수정 루프 네 장, 지식베이스 한 장. `build_migration_agent_deck.js` 가 만들고, 흐름도 네 장은
**파워포인트 도형**(둥근 네모 + 선 + 글)으로 그리므로 파워포인트에서 글자와 칸을 바로 고칠 수 있습니다(그림 파일 없음;
선은 칸에 붙어 있지 않아 칸을 옮기면 선도 옮겨야 합니다). 설정 키·파일 이름 같은 세부 사항은 일부러 뺐습니다.

```bash
cd scripts/handbook && npm install pptxgenjs   # 처음 한 번
NODE_PATH=$PWD/node_modules APPLY_THEME_JS=<pptx 스킬>/scripts/apply_theme.js node build_migration_agent_deck.js ../../docs/migration-agent/migration-agent.pptx
```

`APPLY_THEME_JS` 를 주지 않으면 오피스 기본 색으로 만들어집니다(내용은 같습니다).

흐름도 함수(`drawOverview` / `drawLoop` / `drawLadder` / `drawKB`)는 `node(제목, 설명, 종류)` 로 칸을 놓고 `seg` / `route` 로 선을 긋습니다.

단계별 쪽은 `steps/<단계>.js` 한 파일이 한 장입니다(`title` / `notes` / `lines` / `draw(s, H)`). 그림은 `H.AREA` 안에만 그리고, 위의
단계 띠와 아래 글은 빌드 스크립트가 놓습니다. 칸에는 설정 키나 파일 경로를 쓰지 않고 발표자 노트(`notes`)에 적습니다. 한 단계만
고쳐서 빨리 보려면 `ONLY=intake,analyze` 를 앞에 붙여 다른 파일 이름으로 만듭니다. 코드가 바뀌면 그 단계의 파일을 고칩니다.

## KB 데이터 지도 (`docs/kb-map/index.html`)

`docs/kb-map/kb-map.html` 이 원본이고, `build_kb_map.py` 가 Mermaid(`vendor/mermaid.min.js`, 11.x)를 그 안에 넣어
인터넷 없이 더블클릭으로 열리는 한 파일 `index.html` 을 씁니다. 원본을 고친 뒤 다시 실행하십시오.

```bash
.venv/bin/python scripts/handbook/build_kb_map.py            # vendor 의 Mermaid 사용
.venv/bin/python scripts/handbook/build_kb_map.py <mermaid.min.js>   # vendor 를 새 버전으로 바꾸면서
```
