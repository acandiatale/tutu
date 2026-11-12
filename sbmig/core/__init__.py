"""모든 패키지가 함께 쓰는 기본 정의(models, config, workspace, interfaces, stage, diag)를 모아 둔 패키지
모듈. 다른 패키지는 모두 이것을 import한다.
모델, 설정, 플러그인 인터페이스처럼 다른 코드가 의존하는 정의를 바꾸면 docs/INTERFACES.md에 기록한다.

Shared contracts: models, config, workspace, interfaces. Every other package depends on this one only."""
