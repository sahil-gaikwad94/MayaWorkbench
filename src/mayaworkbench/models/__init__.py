"""Provider-neutral model access.

Providers are adapters; the workbench owns prompts, schemas, validation, policy,
budgets, and evidence. No module here may read repository state or execute code.
"""
