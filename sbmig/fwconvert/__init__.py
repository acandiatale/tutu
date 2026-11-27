"""사내 프레임워크 프로젝트를 Spring Boot 프로젝트로 전환하는 패키지. 변환 규칙과 생성기를 둔다.

Rules and module contract: docs/FW-CONVERT.md. Nothing here is called from a run yet; the modules are functions
with tests. The framework's name never appears in this package: rules carry the token ``{Fw}`` and
``rules.load_rules`` fills it in."""
