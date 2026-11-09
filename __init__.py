"""spawn-governor — admission control for delegation spend.

WHY
    Hermes lets a delegation backend choose a subagent's ROUTE. Metering plugins then tally what was
    spent, after the fact. Nothing stands in front of the spawn and answers "may this run, at this
    price, inside what is left of the envelope?" — so the only defence against a runaway fan-out is
    noticing it afterwards. This plugin is that gate.

WHERE IT SITS (verified against the running tree, not recalled)
    ``agent/delegation_provider.py`` — a ``DelegationProvider.route_task(task, config)`` may RAISE
    ``DelegationRouteError``, and the message becomes the ``delegate_task`` tool error the model
    reads. That is a sanctioned refusal at the boundary, before core spawns anything.
    ``task`` carries ``goal``, ``context``, ``index`` and this provider's own declared parameters —
    NOT token counts, NOT price, NOT usage. So an admission-time number is necessarily a FORECAST
    (from the goal/context text against a rate table). The real usage arrives later, once per
    physical provider call, at the ``post_api_request`` hook — that is the reconciliation leg.

WHY THE PAIRING IS THE POINT
    Forecast at the boundary, reconcile after it. The refusal is honest about being a forecast; the
    ledger is honest about what actually landed. Claiming "actual per-spawn cost" at admission time
    would be a false claim — do not make it.

KNOBS
    delegation.backends.spawn_governor.envelope_usd         0 disables the gate (observe-only)
    delegation.backends.spawn_governor.window               day | session | total
    delegation.backends.spawn_governor.rates                {model: usd_per_1k_blended}
    delegation.backends.spawn_governor.default_rate_per_1k  fallback when the model is unlisted
    delegation.backends.spawn_governor.estimate_chars_per_token   forecast divisor (default 4)
    delegation.backends.spawn_governor.on_breach            deny | warn

LEDGER
    $HERMES_HOME/logs/spawn-governor-ledger.jsonl — one line per reservation and per reconciled
    provider call: ``{"kind": "reservation"|"actual", "usd": ..., "model": ..., "ts": ...}``
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Mapping

from agent.delegation_provider import DelegationProvider, DelegationRouteError

PLUGIN_NAME = "spawn_governor"

# Closed-loop calibration bounds. A factor outside this range is not a calibration, it is a bug or
# a unit change — clamp rather than let one bad run move the gate's price by orders of magnitude.
_CALIBRATION_MIN_PAIRS = 2
_CALIBRATION_MIN = 0.25
_CALIBRATION_MAX = 4.0

# Route keys a provider may return; mirror the default backend's pass-through, minus the tier key
# the delegation section never carries.
_PASSTHROUGH = (
    "model", "provider", "base_url", "api_key", "api_mode",
    "request_overrides", "reasoning_effort", "fallback_providers",
)


def _home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def _ledger_path() -> Path:
    return _home() / "logs" / "spawn-governor-ledger.jsonl"


def _ledger_append(rec: dict[str, Any]) -> None:
    """Best-effort. A ledger that cannot be written must never block a spawn."""
    try:
        p = _ledger_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    except Exception:
        pass


def _spent_usd(window: str, session_id: str | None = None) -> float:
    """Sum reconciled ACTUAL spend (falling back to reservations when nothing reconciled yet)."""
    path = _ledger_path()
    if not path.exists():
        return 0.0
    now = time.time()
    horizon = {"day": now - 86_400, "session": 0.0}.get(window, 0.0)
    total = 0.0
    actual_seen = False
    reservations: list[tuple[float, float]] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                ts = float(r.get("ts") or 0)
                if window == "session" and session_id and r.get("session_id") not in (None, session_id):
                    continue
                if horizon and ts < horizon:
                    continue
                usd = float(r.get("usd") or 0)
                if r.get("kind") == "actual":
                    actual_seen = True
                    total += usd
                elif r.get("kind") == "reservation":
                    reservations.append((ts, usd))
    except Exception:
        return 0.0
    if not actual_seen:
        total = sum(u for _, u in reservations)
    return total


def _calibration() -> tuple[float, int]:
    """Closed-loop factor: what the provider ACTUALLY reported vs what we RESERVED, same window.

    The admission price is a forecast; the ledger is not. Dividing the two gives a factor this
    plugin applies to every later forecast, so the gate sharpens with use instead of repeating the
    same estimate forever. Clamped, and it needs at least ``_CALIBRATION_MIN_PAIRS`` reconciled
    calls before it is trusted — an uncalibrated box must not be steered by one lucky run.
    """
    path = _ledger_path()
    if not path.exists():
        return 1.0, 0
    reserved = actual = 0.0
    pairs = 0
    try:
        pending = 0.0
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                usd = float(r.get("usd") or 0)
                kind = r.get("kind")
                if kind == "reservation":
                    pending += usd
                elif kind == "actual" and pending > 0:
                    reserved += pending
                    actual += usd
                    pairs += 1
                    pending = 0.0
    except Exception:
        return 1.0, 0
    if pairs < _CALIBRATION_MIN_PAIRS or reserved <= 0:
        return 1.0, pairs
    return min(_CALIBRATION_MAX, max(_CALIBRATION_MIN, actual / reserved)), pairs


def _pick_downgrade(cfg: Mapping[str, Any], in_tokens: int, out_factor: float,
                    headroom: float, factor: float) -> tuple[dict[str, Any], float] | None:
    """First rung of the operator's ladder whose own forecast fits the headroom.

    Operator-ordered, not price-sorted: the ladder is a preference list the operator wrote, and a
    deterministic pick is one a reviewer can follow. A rung with no resolvable price is skipped —
    a downgrade to an unpriced route is a blind decision, the same defect the unpriced guard closes.
    """
    ladder = cfg.get("downgrade_ladder")
    if not isinstance(ladder, (list, tuple)):
        return None
    for rung in ladder:
        if not isinstance(rung, Mapping):
            continue
        # Emit ONLY members of core's ROUTE_KEYS (agent/delegation_provider.py:51): `_clean_route`
        # (tools/delegate_tool_routes.py:108) filters the answer to that set and drops the rest
        # SILENTLY — so a rung built from any other key would run on the primary route while the
        # receipt still said "downgraded". That is the $0 false-green class, on camera.
        route = {k: v for k, v in rung.items()
                 if (k in _PASSTHROUGH or k == "profile") and v not in (None, "")}
        if not route:
            continue
        rate = _rate(cfg, str(route.get("model") or ""))
        if rate <= 0:
            continue
        price = (in_tokens * (1.0 + out_factor) / 1000.0) * rate * factor
        if price <= headroom:
            return route, price
    return None


def _rate(cfg: Mapping[str, Any], model: str) -> float:
    rates = cfg.get("rates")
    if isinstance(rates, Mapping):
        for key in (model, (model or "").split("/")[-1]):
            if key and key in rates:
                try:
                    return float(rates[key])
                except Exception:
                    pass
    try:
        return float(cfg.get("default_rate_per_1k") or 0.0)
    except Exception:
        return 0.0


class SpawnGovernor(DelegationProvider):
    """Refuse a ``delegate_task`` spawn that would breach the spend envelope."""

    @property
    def name(self) -> str:
        return PLUGIN_NAME

    @property
    def display_name(self) -> str:
        return "Spawn Governor"

    @property
    def summary(self) -> str:
        return "Prices each spawn against an envelope and refuses it before it starts."

    def is_available(self) -> bool:
        # No network here by contract. Always available: the gate itself is local arithmetic.
        return True

    def task_parameters(self, config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        """One optional per-task declaration. Deterministic for a given config (cached prefix)."""
        return {
            "cost_ceiling_usd": {
                "type": "number",
                "description": (
                    "Optional ceiling for THIS task alone, in USD. The spawn is refused when the "
                    "forecast price exceeds it, independently of the rolling envelope."
                ),
            }
        }

    def description(self, config: Mapping[str, Any]) -> str:
        return (
            "Spawns are priced before they run. If a task would breach the configured spend "
            "envelope (or a per-task cost_ceiling_usd you pass) the spawn is refused and the "
            "refusal names the price and the remaining headroom. To proceed, free headroom by "
            "dropping a running child, or raise delegation.backends.spawn_governor.envelope_usd."
        )

    def route_task(self, task: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
        cfg = self.settings(config)
        route = {k: v for k, v in config.items() if k in _PASSTHROUGH and v not in (None, "")}
        model = str(route.get("model") or config.get("model") or "")

        try:
            envelope = float(cfg.get("envelope_usd") or 0.0)
        except Exception:
            envelope = 0.0
        window = str(cfg.get("window") or "day")
        breach_mode = str(cfg.get("on_breach") or "deny").lower()
        try:
            divisor = float(cfg.get("estimate_chars_per_token") or 4.0) or 4.0
        except Exception:
            divisor = 4.0

        text = f"{task.get('goal') or ''}\n{task.get('context') or ''}"
        in_tokens = max(1, int(len(text) / divisor))
        # A child replies roughly proportionally to what it was asked; use a stated, tunable factor
        # rather than a hidden one.
        try:
            out_factor = float(cfg.get("output_token_factor") or 1.0)
        except Exception:
            out_factor = 1.0
        rate = _rate(cfg, model)
        # A gate that cannot price must not pretend to. With an envelope armed, a spawn whose price
        # resolves to zero is a BLIND refusal decision, so it is refused as a configuration error
        # rather than silently allowed — the failure this plugin exists to prevent, in miniature.
        on_unpriced = str(cfg.get("on_unpriced") or "deny").lower()
        if envelope > 0 and rate <= 0 and on_unpriced != "allow":
            _ledger_append({
                "kind": "refused", "ts": time.time(), "model": model, "usd": 0.0,
                "reason": "unpriced", "window": window,
                "goal_chars": len(str(task.get('goal') or '')), "index": task.get("index"),
            })
            raise DelegationRouteError(
                "Spawn refused by spawn-governor: the envelope is armed but no price is known for "
                f"the route (model {model or '(inherited — the provider cannot see the parent route)'!r}), "
                "so this spawn could not be priced and was not allowed through on a zero. Set "
                "delegation.backends.spawn_governor.default_rate_per_1k (or a "
                f"rates[{model or '<model>'!r}] entry), or set on_unpriced: allow to price at zero "
                "deliberately."
            )
        forecast = (in_tokens * (1.0 + out_factor) / 1000.0) * rate
        # Closed loop: steer the forecast with what the provider actually reported last time.
        factor, pairs = _calibration()
        if factor != 1.0:
            forecast *= factor

        ledger: dict[str, Any] = {
            "kind": "reservation", "ts": time.time(), "model": model,
            "usd": round(forecast, 6), "in_tokens_est": in_tokens,
            "goal_chars": len(str(task.get('goal') or '')), "window": window,
            "index": task.get("index"),
            "calibration": round(factor, 4), "calibration_pairs": pairs,
        }

        # Per-task ceiling the model declared (type is not core-enforced; validate it ourselves).
        raw_ceiling = task.get("cost_ceiling_usd")
        if raw_ceiling is not None:
            try:
                ceiling = float(raw_ceiling)
            except Exception:
                raise DelegationRouteError(
                    f"cost_ceiling_usd must be a number in USD; got {raw_ceiling!r}. "
                    "Omit it or pass a number (e.g. 0.30)."
                )
            if forecast > ceiling:
                _ledger_append({**ledger, "kind": "refused", "reason": "task_ceiling",
                                "ceiling": ceiling})
                raise DelegationRouteError(
                    f"Spawn refused by spawn-governor: this task is forecast at "
                    f"${forecast:.4f} against the ${ceiling:.4f} ceiling you set for it. "
                    "Raise cost_ceiling_usd, or narrow the goal/context so the forecast fits."
                )

        if envelope > 0:
            spent = _spent_usd(window)
            headroom = max(0.0, envelope - spent)
            if spent + forecast > envelope:
                if breach_mode == "warn":
                    _ledger_append({**ledger, "kind": "overrun_allowed",
                                    "spent": round(spent, 6), "envelope": envelope})
                elif breach_mode == "downgrade":
                    picked = _pick_downgrade(cfg, in_tokens, out_factor, headroom, factor)
                    if picked is not None:
                        rung, rung_cost = picked
                        _ledger_append({
                            **ledger, "kind": "downgraded", "usd": round(rung_cost, 6),
                            "primary_forecast": round(forecast, 6),
                            "spent": round(spent, 6), "envelope": envelope,
                            "downgraded_to": rung.get("model"),
                        })
                        return {**rung, "label": (
                            f"spawn-governor: downgraded to {rung.get('model') or rung.get('profile')} — "
                            f"primary forecast ${forecast:.4f} vs ${headroom:.4f} headroom, "
                            f"downgrade ~${rung_cost:.4f}"
                        )}
                    _ledger_append({**ledger, "kind": "refused", "reason": "ladder_exhausted",
                                    "spent": round(spent, 6), "envelope": envelope})
                    raise DelegationRouteError(
                        f"Spawn refused by spawn-governor: forecast ${forecast:.4f} on top of "
                        f"${spent:.4f} already spent would breach the ${envelope:.4f} envelope, and no "
                        "rung of downgrade_ladder fits the remaining headroom "
                        f"(${headroom:.4f}). Free headroom, raise the envelope, add a cheaper rung, "
                        "or set on_breach: warn."
                    )
                else:
                    _ledger_append({**ledger, "kind": "refused", "reason": "envelope",
                                    "spent": round(spent, 6), "envelope": envelope})
                    raise DelegationRouteError(
                        f"Spawn refused by spawn-governor: forecast ${forecast:.4f} on top of "
                        f"${spent:.4f} already spent in this {window} window would breach the "
                        f"${envelope:.4f} envelope (${headroom:.4f} left). "
                        "Free headroom or raise delegation.backends.spawn_governor.envelope_usd."
                    )
        _ledger_append(ledger)
        return route


def _settings_from_config_file() -> dict[str, Any]:
    """The hook has no config argument, so read the plugin's own block once (best-effort)."""
    cached = getattr(_settings_from_config_file, "_cache", None)
    if cached is not None:
        return cached
    cfg: dict[str, Any] = {}
    try:
        path = _home() / "config.yaml"
        if path.exists():
            raw = path.read_text(encoding="utf-8")
            try:
                from ruamel.yaml import YAML  # shipped with Hermes
                loaded = YAML(typ="safe").load(raw) or {}
            except Exception:
                import yaml as _y  # type: ignore
                loaded = _y.safe_load(raw) or {}
            if isinstance(loaded, Mapping):
                delegation = loaded.get("delegation") or {}
                if isinstance(delegation, Mapping):
                    backends = delegation.get("backends") or {}
                    if isinstance(backends, Mapping):
                        own = backends.get(PLUGIN_NAME) or {}
                        if isinstance(own, Mapping):
                            cfg = dict(own)
    except Exception:
        cfg = {}
    _settings_from_config_file._cache = cfg  # type: ignore[attr-defined]
    return cfg


def _on_post_api_request(**payload: Any) -> None:
    """Reconciliation leg: fold the real usage of one physical provider call into the ledger.

    This is the half admission cannot know: at spawn time the price is a forecast, here it is what
    the provider actually reported. Reserved spawns are NOT matched 1:1 to calls (core owns the
    lifecycle), so the ledger keeps both streams and ``_spent_usd`` prefers reconciled actuals.
    """
    try:
        usage = payload.get("usage") or payload.get("token_usage") or {}
        if not isinstance(usage, Mapping):
            usage = {}
        in_tok = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        out_tok = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        if not (in_tok or out_tok):
            return None
        model = str(payload.get("response_model") or payload.get("model") or "")
        cfg = _settings_from_config_file()
        rate = _rate(cfg, model)
        if rate <= 0:
            # No price known: record the tokens anyway so the ledger is never silently empty.
            _ledger_append({
                "kind": "actual_unpriced", "ts": time.time(), "model": model,
                "in_tokens": in_tok, "out_tokens": out_tok, "usd": 0.0,
                "session_id": payload.get("session_id"),
            })
            return None
        usd = ((in_tok + out_tok) / 1000.0) * rate
        _ledger_append({
            "kind": "actual", "ts": time.time(), "model": model, "usd": round(usd, 6),
            "in_tokens": in_tok, "out_tokens": out_tok,
            "session_id": payload.get("session_id"),
            "provider": payload.get("provider"),
        })
        return None
    except Exception:
        return None


def register(ctx: Any) -> None:
    ctx.register_delegation_provider(SpawnGovernor())
    ctx.register_hook("post_api_request", _on_post_api_request)
