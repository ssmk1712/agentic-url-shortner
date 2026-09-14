.PHONY: install test compile validate run scenario-greenfield scenario-brownfield scenario-ambiguous clean

install:
	python -m pip install -r requirements.txt

test:
	pytest -q

compile:
	python -m compileall -q app agentic tests

validate: test compile
	python -m agentic.run_scenario greenfield --approve-release
	python -m agentic.run_scenario brownfield --approve-release

run:
	uvicorn app.main:app --reload

scenario-greenfield:
	python -m agentic.run_scenario greenfield --approve-release

scenario-brownfield:
	python -m agentic.run_scenario brownfield --approve-release

scenario-ambiguous:
	python -m agentic.run_scenario ambiguous --approve-release

clean:
	rm -rf .pytest_cache app/__pycache__ agentic/__pycache__ tests/__pycache__ data
	find artifacts/runs -type f ! -name '.gitkeep' -delete 2>/dev/null || true
