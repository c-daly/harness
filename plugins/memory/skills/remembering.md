---
name: remembering
description: When and how to write memories
---

Write memories for durable preferences, corrections, and decisions that should
persist across sessions.

Guidelines:
- One fact per entry: each memory captures a single, specific observation.
- Use `subject=user` for cross-cutting preferences (applies to all projects).
- Use `subject=<project-name>` for project-specific context.
- Choose `entry_type` to reflect the nature of the memory:
  - `user` -- personal preferences, corrections, behavioral rules
  - `feedback` -- explicit feedback or critique from the user
  - `project` -- project state, decisions, blockers
  - `reference` -- external facts, links, or reference material
- Write the `description` as a one-line summary; put detail in `body`.
- Do NOT duplicate an existing entry (name+entry_type is unique; the store is append-only).
