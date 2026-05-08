# Example Knowledge Files

This directory contains example L1 knowledge files that demonstrate
the layered memory system's structure.

## File Naming Convention

Use lowercase-hyphenated names that clearly indicate the domain:
- `infrastructure.md` — servers, databases, deployments
- `api-conventions.md` — REST patterns, auth, error handling
- `development.md` — coding standards, tools, workflows
- `user-preferences.md` — personal preferences, habits

## Structure

Each file should use `## Second-level Headings` to create searchable sections:

```markdown
## Section Title
- Fact 1
- Fact 2
- Configuration detail

## Another Section
- More facts...
```

The `recall_knowledge` tool searches within `## sections`, so good
heading structure directly improves search accuracy.
