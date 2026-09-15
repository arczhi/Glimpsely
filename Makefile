.PHONY: ci lint test demo test-llm clean

lint:
	.venv/bin/python -m ruff check glimpsely tests scripts

test:
	.venv/bin/python -m pytest tests/ -q

ci: lint test

demo:
	.venv/bin/python -m glimpsely.main demo

test-llm:
	.venv/bin/python -m glimpsely.main test-llm

clean:
	rm -rf data glimpsely/__pycache__ tests/__pycache__
