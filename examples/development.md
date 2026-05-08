## Code Style
- Language: Python 3.12+
- Formatter: ruff format
- Type hints: required for all public functions
- Max line length: 120

## Testing
- Framework: pytest
- Run: `pytest tests/ -x --tb=short`
- Coverage target: 80%+
- Integration tests: marked with @pytest.mark.integration

## Git Workflow
- Branch naming: feat/<ticket>-<description>
- Commit format: conventional commits
- PR required for merge to main
- Squash merge preferred
