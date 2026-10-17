# sbmig — JDK 21 / Spring Boot 4.1 migration agent

See `docs/ARCHITECTURE.md` (design), `docs/PLAN.md` (delivery plan), `docs/INTERFACES.md` (contracts), `docs/RUNBOOK.md` (operations).

```bash
uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"
cp config/artifacts.example.yml config/artifacts.yml   # fill in in-house coordinates
sbmig analyze path/to/project.zip
sbmig run path/to/project.zip --config config/artifacts.yml --out out/
sbmig kb list
```
