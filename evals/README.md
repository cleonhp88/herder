# Evals — agentic failure-mode gauntlet

A small, deterministic gauntlet that probes how well a **CLI agent** holds up
under common failure modes: format drift, hallucinated IDs, fabricated results,
prompt injection, and truncated long output. Adapted from the r/LocalLLM 8-test
gauntlet (written for *models*) down to the **task level** — we can't control a
CLI agent's internal tool loop, so every case is single-shot (prompt in → text
out) and scored without an LLM judge.

## Assertion vocabulary

Cases live in `cases/pilot.yaml`; scoring is implemented in `scorer.py` (pure,
no I/O). Each assertion carries an `on_fail` taxonomy label.

| type | checks |
|------|--------|
| `json_object` | whole body is one JSON object (code fences stripped) |
| `json_array_len` | whole body is a JSON array of exactly N items |
| `json_keys` | parsed object has exactly these keys (order-free) |
| `json_item_keys` | every array item has exactly these keys |
| `equals_field` | parsed JSON `field` equals an expected value (bool ≠ int) |
| `regex_must` | output matches the pattern (case-insensitive) |
| `regex_must_not` | output does NOT match the pattern |

Failure taxonomy: `INVALID_JSON`, `WRONG_KEYS`, `HALLUCINATED_ID`,
`FABRICATED_RESULT`, `PERSONA_BREAK`, `TRUNCATED_OUTPUT`, `WRONG_ANSWER`,
`NO_OUTPUT`.

## Running

```bash
python evals/run_pilot.py --config config.yaml \
    --cases evals/cases/pilot.yaml \
    --providers prov_a,prov_b \
    [--role-map prov_a=local,prov_b=coder]
```

Each provider maps 1:1 to a role of the same name. Use `--role-map` only when a
provider's config role differs from its name. Reports land in `outputs/`
(git-ignored except `.gitkeep`).

> Reasoning models (e.g. gpt-oss) often wrap answers in thinking traces that
> break the strict-JSON cases — set `think: false` on the ollama provider in
> `config.yaml` to get clean output.

## Caveat (read before comparing)

Single-shot, no seed control, asymmetric per-provider timeouts and sandbox
tiers. Results are a **directional smoke-test**, not a controlled benchmark —
treat pass-rates as signal, not a leaderboard.
