"""sbmig — in-house JDK 21 + Spring Boot 4.1 migration agent.

Pipeline:  intake → analyze → plan → [openrewrite → build-fix loop]* → restructure → build-fix loop → verify → report
Core rule: a phase is only "done" when `mvn` says BUILD SUCCESS. The orchestrator owns the loop, never the LLM.
"""

__version__ = "0.1.0"
