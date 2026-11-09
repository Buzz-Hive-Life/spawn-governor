# spawn-governor

**Admission control for delegation spend.** Hermes lets a delegation backend pick a subagent's
*route*. Metering plugins then tally what was *spent*. Neither stands in front of the spawn and
answers **"may this run, at this price, inside what is left of the envelope?"** — so a runaway
fan-out is only ever noticed afterwards. This plugin is that gate.

## What it does

- **Prices the spawn before it runs.** `route_task()` forecasts a cost from the task's
  `goal` + `context` (chars ÷ `estimate_chars_per_token`) against a `rates` table.
- **Refuses it, with the arithmetic.** On breach it raises `DelegationRouteError`, so the refusal
  is the `delegate_task` error the model reads — naming the forecast, the spend so far, and the
  headroom left. Nothing is spawned.
- **Honours a per-task ceiling.** The model may pass `cost_ceiling_usd` on a task; a spawn whose
  forecast exceeds it is refused independently of the rolling envelope.
- **Reconciles after the fact.** A `post_api_request` hook folds the *real* usage of each physical
  provider call into a JSONL ledger, so the forecast is auditable against what actually landed.
- **Closed loop.** The ledger's reconciled/forecast ratio becomes a **calibration factor** applied
  to every later forecast (clamped to `0.25–4.0`, and ignored until at least 2 calls have been
  reconciled). The gate sharpens with use instead of repeating one guess forever. Measured by the
  shipped harness, on a 4,000-char goal against a $0.14/1k route: `$0.2800` at factor `1.00`, still
  `$0.2800` at one reconciled pair (the deadband), then `$1.1200` at factor `4.00` once two pairs
  reconcile — the ratio was 6.00 and the clamp held it at 4.00.
- **Deny *or downgrade*.** With `on_breach: downgrade` the gate does not stop at refusing: it walks
  `downgrade_ladder` and **takes the first admissible cheaper rung** whose own forecast fits the
  remaining headroom, recording the swap in the ledger and in the child's label. It refuses only
  when no rung fits. Routing plugins pick a route and cannot price it; metering plugins price it and
  cannot act on it — acting on the price, with an alternative, is the point.
- **Never allows a spawn on a zero price.** An armed envelope with no resolvable rate for the route
  is refused as a configuration error (`on_unpriced: deny`), because a gate that cannot price is a
  gate pretending. The unpriced guard applies to ladder rungs too.

## Measured, not claimed

`hermes plugins validate` → **15/15 checks OK, 0 warnings, isolation `host`** (re-run at this pinned
tree; run it yourself — it is the same gate catalogue CI runs).

The verification harness is `tests/core_path_demo.py` — run it against a Hermes checkout and it prints
the whole story (arm · refusal · downgrade · loop · honesty stamp). It imports two core internals on
purpose so it can drive core's *own* resolution rather than a private harness; it is a development
script and never runs inside Hermes, and it lives under `tests/` for exactly that reason.

**Proven through core's own pre-spawn path** (`tools/delegate_tool_routes.resolve_task_routes`, the
function `delegate_task` calls at `tools/delegate_tool.py:511` — not a private harness):

| arm | config | result |
|---|---|---|
| engine selection | `delegation.backend: spawn_governor` | core resolves our provider **by name** |
| armed, unpriced | `envelope_usd: 0.30`, no rate resolvable | **refused** — a spawn is never allowed through on a zero price |
| 3 tasks, one over | `envelope_usd: 0.30`, priced route | task 1 **refused**, tasks 0 and 2 **routed** — siblings unaffected |
| refusal receipt | — | `{"task_index": 1, "status": "error", "error": "Task 1: Spawn refused…", "route": {…, "refused": true}}` |
| **downgrade** | `on_breach: downgrade` + a priced `downgrade_ladder` | task **routed on the cheaper rung** — label `spawn-governor: downgraded to cheap — primary forecast $2.8000 vs $0.3000 headroom, downgrade ~$0.0200`, ledger `downgraded` |
| ladder exhausted | same, headroom below every rung | **refused** |
| ladder rung unpriced | same, rung has no rate | rung **skipped**, then **refused** (no blind downgrade) |

Plus five admission arms exercised directly: allow · per-task ceiling refusal · envelope refusal ·
observe-only (`envelope_usd: 0`) · `on_breach: warn` (allows and records an `overrun_allowed` line).

> **Sibling isolation is core's, not ours.** `resolve_task_routes` documents it: a refused task "is
> refused alone and its siblings still run." The gate inherits that — one expensive spawn dies, the
> rest of the fan-out completes. That is the beat worth filming.

## Install

```
hermes plugins install <repo-url> --ref <commit>
```

Then select it and set an envelope in `config.yaml`:

```yaml
delegation:
  backend: spawn_governor
  backends:
    spawn_governor:
      envelope_usd: 2.50
      window: day            # day | session | total
      on_breach: deny        # deny | downgrade | warn
      on_unpriced: deny      # deny | allow — an armed envelope never passes a spawn priced at 0
      estimate_chars_per_token: 4
      rates:
        deepseek/deepseek-v4.1-flash: 0.14
      # optional: instead of refusing, take the first rung that fits the headroom
      # on_breach: downgrade
      # downgrade_ladder:
      #   - {model: deepseek/deepseek-v4.1-flash}
      #   - {model: <a-cheaper-route>}
      default_rate_per_1k: 0.0
```

Set `envelope_usd: 0` for **observe-only**: every spawn is priced and recorded, nothing is refused.

## Where it sits, and why the claim is worded the way it is

The delegation provider interface (`agent/delegation_provider.py`) hands `route_task()` a task's
`goal`, `context`, `index` and the provider's own declared parameters — **no token counts, no price,
no usage**, and the interface has no post-run method. So:

- the admission number **is a forecast**, priced from the goal text against your rate table;
- **actual** spend arrives later, once per physical provider call, at `post_api_request`.

The pairing is the product: *forecast at the boundary, reconcile after it*. This plugin does **not**
and cannot claim an "actual per-spawn cost" — that would be false at admission time.

## Disclosure (admission rule 13)

- **Writes to disk.** A JSONL ledger at `$HERMES_HOME/logs/spawn-governor-ledger.jsonl` (one line
  per reservation, refusal and reconciled provider call). Nothing leaves the machine.
- **Reads.** `$HERMES_HOME/config.yaml`, once, for this plugin's own `delegation.backends.spawn_governor`
  block.
- **Network calls.** None. `is_available()` is local arithmetic by contract.
- **Shell commands / background processes.** None.
- **Credentials.** None stored, none read. This plugin never touches an API key.

## License

MIT — see `LICENSE`.
