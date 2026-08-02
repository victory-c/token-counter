# AI Coding Agent Token Burn Report — 2026-07-01 to 2026-07-31

## Summary

- Total tokens: 57,018,891
- Input: 4,873,500
- Output: 703,137
- Cache creation: 661,099
- Cache read: 50,781,155
- Reasoning: 0
- API-equivalent cost: $18.19
- Subscription cash paid: $80.00
- Estimated subscription value multiple: 0.23x

## By Provider

| Provider | Tokens | API-Equivalent Cost | Events | Confidence |
|---|---:|---:|---:|---|
| gemini | 47,625,353 | $18.09 | 1,021 | exact_from_provider_log |
| claude_code | 9,393,538 | $0.10 | 94 | exact_from_local_log |

## By Model

| Provider | Model | Input | Output | Cache R | Cache W | Reason | Tokens | Cost |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| gemini | gemini-3.6-flash | 4,840,194 | 592,829 | 42,117,802 | 0 | 0 | 47,550,825 | $18.02 |
| claude_code | claude-sonnet-5 | 184 | 108,939 | 8,608,582 | 651,051 | 0 | 9,368,756 | $0.00 |
| gemini | gemini-3.6-flash-high | 33,118 | 1,351 | 40,059 | 0 | 0 | 74,528 | $0.07 |
| claude_code | claude-opus-5 | 2 | 9 | 7,370 | 5,505 | 0 | 12,886 | $0.00 |
| claude_code | claude-opus-4-8 | 2 | 9 | 7,342 | 4,543 | 0 | 11,896 | $0.10 |

## Top Projects

| Provider | Project | Tokens | Cost | Sessions |
|---|---|---:|---:|---:|
| claude_code | ~/hermes-workspace/hermes-agent-pr-68524 | 7,378,822 | $0.00 | 1 |
| claude_code | /tmp/bay-area-venture-map-review | 1,989,934 | $0.00 | 2 |
| claude_code | ~/hermes-workspace | 24,782 | $0.10 | 2 |

## Top 10 Token-Burning Sessions

| Provider | Project | Session | Tokens | Cost |
|---|---|---|---:|---:|
| claude_code | ~/hermes-workspace/hermes-agent-pr-68524 | `e326e63f-d95` | 7,378,822 | $0.00 |
| gemini |  | `73e362b0-731` | 4,502,157 | $1.21 |
| gemini |  | `2369d23c-495` | 4,232,292 | $1.20 |
| gemini |  | `09f85c95-ba8` | 4,133,066 | $1.23 |
| gemini |  | `d3a621c7-9d7` | 3,022,013 | $0.86 |
| gemini |  | `1340ffa9-80c` | 2,611,447 | $0.85 |
| gemini |  | `83d78a16-4e7` | 2,524,891 | $0.89 |
| gemini |  | `9360877b-7e2` | 2,070,261 | $0.64 |
| gemini |  | `26fe8210-037` | 2,039,168 | $0.67 |
| gemini |  | `c8732b6e-6be` | 2,021,720 | $0.87 |

## By Task

| Task | Tokens | Cost | Dominant model | Min class |
|---|---:|---:|---|---|
| unclassified | 57,018,891 | $18.19 | gemini-3.6-flash (83%) | — |

⚠ = dominant model is in a heavier class than the task typically needs.

## Notes

Confidence labels reflect data provenance:
`exact_from_local_log` (Claude Code, Codex), `exact_from_provider_log` (Gemini API metadata), `manual_import` (Cursor / hand-imported), `estimated_from_session_summary` (Codex cumulative-totals fallback).

Subscription value multiple is the API-equivalent retail cost divided by subscription cash paid. It is **not** vendor cost.
