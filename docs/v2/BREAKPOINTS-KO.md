# 브레이크포인트 안내 — 흐름을 따라가며 볼 곳 (커밋 `024d379`, 2026-10-29 기준)

줄 번호는 커밋이 바뀌면 밀립니다. **함수 이름**이 안정적인 기준이니 IDE 에서 함수로 찾아 걸어도 됩니다.
실행 방법(오프라인, LLM 은 재생 파일로 대신):

```
python -m sbmig.cli run fixtures/projects/servlet-nonstandard-dirs --config config/artifacts.local-e2e.yml --config fixtures/projects/servlet-nonstandard-dirs/sbmig.e2e.yml
```

`JAVA21_HOME` 이 필요합니다. 기본 엔진(LangGraph)도 노드를 같은 스레드에서 돌리므로 브레이크포인트가 그대로 걸립니다. 콜스택을 단순하게 보려면
설정에 `loop: {engine: inline}`. Ant 픽스처는 `fixtures/projects/ant-webapp`.

**추천 순서**: `orchestrator.py:1027` `_run_step` 에 조건 브레이크포인트(`step.id == "..."`)를 하나 걸어 단계별로 따라가고, 루프는
`fixloop.py:666`(라운드 빌드)과 `fixloop.py:1604`(케이스마다 어느 길로 가나) 두 곳이면 충분합니다.

## 0. 진입
| 위치 | 설명 |
|---|---|
| `sbmig/cli.py:290` `run()` | CLI 진입. 설정 합치기·run dir 생성 |
| `sbmig/loop/orchestrator.py:365` `Orchestrator.run()` | 한 실행의 시작. `self.cfg`, `self.ws`(작업 공간) |
| `sbmig/graph/engine.py:175` | 그래프 노드가 단계 하나를 실행 → `orchestrator.py:790` `step_phase()` → `:1004` `_run_phase()` → `:1027` `_run_step()` (step 분기, `step.id` 로 현재 단계 확인) |

## 1. 접수 · 분석 · 계획
| 위치 | 설명 |
|---|---|
| `orchestrator.py:547` `_step_intake()` | 작업 사본, git 스냅샷, preflight(mvn, JDK 21) |
| `orchestrator.py:574` `_check_build_tool()` → `:599` `_synthesize_ant_pom()` | Maven 인지, Ant 면 pom 합성(v2.1j) |
| `sbmig/analyze/ant.py:1086` `describe_ant()` / `:1326` `synthesize_pom()` | build.xml 해석, pom.xml 작성 |
| `sbmig/analyze/jar_coords.py:558` `resolve_jar()` | jar 한 개의 좌표 사다리(pom.properties → 이름 표 → sha1) |
| `orchestrator.py:698` `step_analyze()` → `sbmig/analyze/analyzer.py:230` `analyze_path()` | `self.profile` 완성(케이스 종류, Java 버전, 라이브러리) |
| `orchestrator.py:729` `step_plan()` → `sbmig/plan/planner.py:549` `build_plan()` | step 목록. `report.plan` 에 뒤에 뭐가 도는지 전부 보임 |

## 2. 자동 변환 (OpenRewrite 1회)
| 위치 | 설명 |
|---|---|
| `orchestrator.py:1062` `_step_rewrite()` | `step.recipes` = 돌릴 레시피 id |
| `orchestrator.py:1123` `_run_rewrite_once()` | 실제 호출 |
| `sbmig/rewrite/runner.py:336` `OpenRewriteRunner.run()` | `mvn rewrite:run` 조립·실행, 바뀐 파일 목록 |

## 3. 1차 컴파일 → 오류 목록의 첫 레코드
| 위치 | 설명 |
|---|---|
| `orchestrator.py` `_step_compile()` | `mvn clean compile` 1회 — v2.1o 부터 `plan.compile_after_rewrite: true` 일 때만 계획에 들어간다(기본 false: 재구성 전 빌드 없음) |
| `orchestrator.py:1245` | `self.errors.add_build(res, "compile", …)` — 오류 목록이 처음 채워지는 줄 |
| `sbmig/build/runner.py:137` `MavenRunner.run()` → `sbmig/build/mvn.py:216` | Maven 자식 프로세스 기동(Popen) |
| `sbmig/build/log_parser.py:944` `parse_maven_log()` | 로그 → BuildError(L1/L2 서명) |
| `sbmig/errors/entries.py:448` `ErrorList.add_build()` | 케이스 id, 이전 오류와의 해결/신규 판정 |

## 4. 구조 정리와 구조 검토
| 위치 | 설명 |
|---|---|
| `orchestrator.py:1285` `_step_restructure()` | 모드(layout / boot_pom / msa_pom) 결정 |
| `sbmig/restructure/restructurer.py:342` `Restructurer.apply()` | 재구성 진입 |
| `restructurer.py:416` `_layout()` / `:2681` `_populate_front()` / `:3899` `_boot_in_place()` / `:4011` `_msa()` | 분리 본체 / JSP·정적 리소스 / Boot as-is / MSA |
| `restructurer.py:376` `_jsp_jakarta()` | JSP javax → jakarta (모든 모드 끝) |
| `orchestrator.py:1383` `_step_review()` → `sbmig/review/reviewer.py:209` `review()` | LLM 구조 검토. `:290` `_ask()` 형식 재시도, `:455` `_apply_retry()` 적용 실패 재시도 |

## 5. 검증 빌드 → 수정 루프
| 위치 | 설명 |
|---|---|
| `orchestrator.py:1780` `_step_verify()` | `mvn clean package` 첫 빌드. 실패하면 루프로 |
| `orchestrator.py:1876` `_verify_fix_loop()` | **루프 진입점**. `first` = 실패한 빌드 |
| `orchestrator.py:1818` `_final_build()` | 최종 빌드(`_step_verify` 안의 내부 함수) |
| `sbmig/loop/fixloop.py:609` `BuildFixLoop.run()` | 라운드 루프 본체 |
| `fixloop.py:666` | 라운드마다 재빌드하는 줄(첫 라운드는 실패 빌드 재사용) |
| `fixloop.py:670` → `:1079` `_score()` | 직전 라운드 시도 채점, 악화면 되돌림(`:1907` `_rollback_previous()`) |
| `fixloop.py:805` `_after_scoring()` | 채점 뒤 resolved 처리 |
| `fixloop.py:1238` `_kb_match()` / `:1256` `_decide()` | 케이스별 KB 조회 / 트리아지 결정(재사용·보강·신규·차단) |
| `fixloop.py:1604` `_script_attempt()` | 사다리 분기: 저장 스크립트(`:2143` `_run_script()`) 또는 LLM(`:1669` `_llm_try()` → `:1978` `_ask_llm()`) |
| `fixloop.py:2181` `_apply_edits()` | LLM 의 edits 적용 |
| `fixloop.py:2089` `_store_script()` / `:2589` `_credit_script()` | 스크립트 저장 / 실적 반영 |

## 6. 바깥 호출
| 위치 | 설명 |
|---|---|
| `sbmig/fix/fixer.py:415` `propose_script()` / `:601` `verify_guide()` | 프롬프트 조립 → 호출 → 파싱 / 운영자 가이드 |
| `sbmig/fix/company.py:199` | 게이트웨이 `requests.post` 한 줄(`payload` 가 보내는 messages) |
| `sbmig/script/runner.py:214` | 수정 스크립트 자식 프로세스 기동 |
| `sbmig/errors/cases.py:363` `best()` / `:387` `add_script()` / `:477` `credit()` / `:609` `record_run()` | KB 읽기/쓰기 |

## 7. 마무리
| 위치 | 설명 |
|---|---|
| `orchestrator.py:2335` `_finalize()` | 최종 빌드 결과 반영, 보고서(`sbmig/report/writer.py:43` `write_report()`)·KB 기록·`SBMIG-RESULT` |
