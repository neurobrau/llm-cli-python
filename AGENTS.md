# NeuroBräu CLI — Python

## Project
Educational multi-provider LLM CLI application.

## Stack
- Python 3.14+
- Standard library for CLI argument parsing
- Official provider SDKs for OpenAI and Anthropic
- Ollama local HTTP API
- pytest for testing

## Engineering rules
- Use the src-layout package structure.
- Keep provider implementations separate from CLI logic.
- Use type hints.
- Keep functions small and testable.
- Never hardcode API keys or secrets.
- Never commit .env files.
- Mock external API calls in unit tests.
- Do not introduce LangChain or agent frameworks.
- Do not commit or push changes without approval.
- Explain significant architectural decisions.

## Workflow
Implement one task at a time.
Run relevant tests after each change.
Report changed files and test results.
