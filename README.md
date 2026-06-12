# harness

Personal event-sourced, multi-model agent harness. Working name.
Design: vault/10-projects/harness/2026-06-10-harness-design.md

## Importing a Claude Code plugin

Convert a Claude Code (CC) format plugin to a native harness plugin:

```
harness import <path-to-cc-plugin-root>
```

This emits a native plugin tree alongside an `IMPORT-REPORT.md` that lists
every rewrite, degradation, drop, hook flag, MCP refusal, and skipped file.
Secrets are never echoed. The output is deterministic across runs.
