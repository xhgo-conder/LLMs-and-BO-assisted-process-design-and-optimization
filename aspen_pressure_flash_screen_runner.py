# -*- coding: utf-8 -*-
"""
aspen_pressure_flash_screen_runner.py

Generic Aspen Flash2 pressure-sensitivity runner for the separation-retrofit stage.
"""

from __future__ import annotations

import copy
import csv
import json
import math
import os
import re
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

RUNNER_VERSION = "aspen-pressure-flash-screen-runner-v48-directional-cut-gate"
TERNARY_RUNNER_VERSION = RUNNER_VERSION

# A pressure-sensitive pair is not automatically an actionable pressure variable.
# The best screened pressure must move materially away from the rebuilt ED_best
# pressure and provide a non-trivial improvement over the nearest base-pressure
# Flash curve.  These are thermodynamic pre-screen gates; the final benefit is
# still verified by the anchored Aspen/BO run.
ACTIONABLE_BASE_PRESSURE_REL_TOL = 0.15
ACTIONABLE_MIN_QUALITY_GAIN = 0.03
DIRECTIONAL_MIN_ACCEPTABLE_QUALITY = -0.02
DIRECTIONAL_SOLVENT_RATIO_TOL = 0.03
DEFAULT_MAX_VALID_FLASH_TEMPERATURE_C = 350.0
DEFAULT_MIN_VALID_FLASH_TEMPERATURE_C = -100.0

# All pressures are absolute pressures.  "Negative pressure" in the GUI means
# sub-atmospheric/vacuum operation, never a negative absolute Aspen pressure.
ATMOSPHERIC_PRESSURE_KPA = 101.325
VACUUM_PRESSURE_THRESHOLD_KPA = 0.95 * ATMOSPHERIC_PRESSURE_KPA
MIN_ABSOLUTE_PRESSURE_KPA = 20.0
DEFAULT_VACUUM_PRESSURE_GRID_KPA = [30.0, 50.0, 70.0]
DEFAULT_PRESSURE_GRID_KPA = [30.0, 50.0, 70.0, 100.0, 300.0, 600.0, 900.0]
DEFAULT_X_GRID = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85,
                  0.90, 0.95]
DEFAULT_VAPOR_FRACTION_GRID = [0.10, 0.30, 0.50, 0.70, 0.90]
DEFAULT_TEMPERATURE_FALLBACK_GRID_C = [35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100, 110, 120, 130, 140]

# Fast engineering-screen defaults. These keep the staged strategy but increase
# the true composition sampling from 5 to 9 points so the GUI curves and BO
# recommendations are more stable while remaining much cheaper than the full grid.
FAST_PRESSURE_GRID_KPA = [30.0, 50.0, 70.0, 100.0, 300.0, 900.0]
FAST_X_GRID = [0.05, 0.10, 0.18, 0.30, 0.45, 0.60, 0.75, 0.85, 0.93]
FAST_VAPOR_FRACTION_GRID = [0.50]
FAST_SOLVENT_TO_FEED_GRID = [0.5, 1.0, 2.0]
FAST_TEMPERATURE_FALLBACK_GRID_C: List[float] = []

DEFAULT_SOLVENT_TO_FEED_GRID = [0.5, 1.0, 1.5, 2.0]
DEFAULT_BINARY_TRIPTYCH_PRESSURE_GRID_KPA = [30.0, 50.0, 70.0, 100.0, 300.0, 600.0, 900.0]
DEFAULT_BINARY_TRIPTYCH_X_GRID = [
    0.01, 0.02, 0.035, 0.05, 0.07, 0.09,
    0.12, 0.15, 0.18, 0.21, 0.24, 0.27,
    0.30, 0.33, 0.36, 0.39, 0.42, 0.45,
    0.48, 0.50, 0.52, 0.55, 0.58, 0.61,
    0.64, 0.67, 0.70, 0.73, 0.76, 0.79,
    0.82, 0.85, 0.88, 0.91, 0.93, 0.95,
    0.965, 0.98, 0.99,
]
DEFAULT_BINARY_TRIPTYCH_VAPOR_FRACTION_GRID = [0.08, 0.20, 0.35, 0.50, 0.65, 0.80, 0.92]
DEFAULT_BINARY_TRIPTYCH_PRESSURE_WRITE_MODE = "atm"


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def _as_dict(obj: Any) -> dict:
    return obj if isinstance(obj, dict) else {}


def _as_list(obj: Any) -> list:
    if obj is None:
        return []
    if isinstance(obj, list):
        return obj
    if isinstance(obj, tuple):
        return list(obj)
    return [obj]


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None:
            return default
        if isinstance(value, str) and not value.strip():
            return default
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return default
        return out
    except Exception:
        return default


def _pressure_regime_label(pressure_kpa: Any) -> str:
    p = _safe_float(pressure_kpa, None)
    if p is None or p <= 0:
        return "invalid"
    if p < VACUUM_PRESSURE_THRESHOLD_KPA:
        return "sub_atmospheric_vacuum"
    if p <= 1.05 * ATMOSPHERIC_PRESSURE_KPA:
        return "near_atmospheric"
    return "pressurized"


def _vacuum_inclusive_pressure_grid(
        values: Sequence[float],
        state_dict: Optional[dict] = None,
        pair_contexts: Optional[dict] = None,
) -> List[float]:
    """Return a positive-absolute-pressure grid including vacuum points.

    The default screen covers roughly 0.30, 0.49 and 0.69 atm in addition to
    atmospheric and positive-pressure points.  User configuration may disable
    this only explicitly.  Per-column ED_best pressures are retained when they
    are not already represented by a nearby grid point.
    """
    state = _as_dict(state_dict)
    cfg = _as_dict(state.get("pressure_swing_flash_config"))
    include_vacuum = bool(cfg.get("include_vacuum_pressure_screening", state.get("include_vacuum_pressure_screening", True)))
    floor = _safe_float(cfg.get("minimum_absolute_pressure_kpa", state.get("minimum_absolute_pressure_kpa", MIN_ABSOLUTE_PRESSURE_KPA)), MIN_ABSOLUTE_PRESSURE_KPA)
    floor = max(1.0, float(floor or MIN_ABSOLUTE_PRESSURE_KPA))

    raw: List[float] = []
    for value in values:
        p = _safe_float(value, None)
        if p is not None and p >= floor:
            raw.append(float(p))

    if include_vacuum:
        vacuum_values = cfg.get("vacuum_pressure_grid_kpa") or state.get("vacuum_pressure_grid_kpa") or DEFAULT_VACUUM_PRESSURE_GRID_KPA
        for value in _as_list(vacuum_values):
            p = _safe_float(value, None)
            if p is not None and floor <= p < ATMOSPHERIC_PRESSURE_KPA:
                raw.append(float(p))

    if bool(cfg.get("include_reference_base_pressures", True)):
        for ctx in _as_dict(pair_contexts).values():
            p = _safe_float(_as_dict(ctx).get("reference_base_pressure_kpa"), None)
            if p is None or p < floor:
                continue
            # Avoid redundant 100/101.325 kPa curves while retaining a genuinely
            # different vacuum ED_best anchor such as 0.39 or 0.61 atm.
            if raw and min(abs(float(x) - p) / max(abs(p), 1e-12) for x in raw) <= 0.03:
                continue
            raw.append(float(p))

    return sorted(dict.fromkeys(round(float(p), 6) for p in raw if p >= floor))


def _clean_id(value: Any, max_len: Optional[int] = None) -> str:
    text = re.sub(r"[^A-Z0-9_\-]", "", str(value or "").upper().strip())
    if max_len is not None:
        text = text[:max_len]
    return text


# Component identity bridge ---------------------------------------------------
#
# Internal BO/GUI names (for example ETOAC) are not always valid Aspen stream
# component IDs.  Aspen may require a registered ID such as ETHYL-01.  The
# optional component_aliases.json file keeps those two layers separate:
#   GUI/BO canonical id  -> bo_id      (used for state, plotting and summaries)
#   Aspen component row  -> aspen_id   (used when registering/writing/reading)
# This prevents Aspen from silently accepting a fake FLOW/MIXED/ETOAC row that
# has a nonzero readback but is ignored by Flash2 during the actual run.

_COMPONENT_ALIAS_CACHE: Optional[Dict[str, Dict[str, str]]] = None
_COMPONENT_ALIAS_RECORDS_CACHE: Optional[List[Dict[str, str]]] = None


def _alias_lookup_key(value: Any) -> str:
    ascii_key = _clean_id(value).replace("-", "").replace("_", "")
    if ascii_key:
        return ascii_key
    # Preserve non-ASCII aliases such as Chinese component names from
    # component_aliases.json.
    raw = str(value or "").strip().upper()
    return re.sub(r"[\s_\-]+", "", raw)


def _component_alias_file_candidates() -> List[str]:
    out: List[str] = []
    try:
        out.append(os.path.join(os.getcwd(), "component_aliases.json"))
    except Exception:
        pass
    try:
        out.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "component_aliases.json"))
    except Exception:
        pass
    # Useful for local tests in this ChatGPT sandbox; harmless in the real app.
    out.append(os.path.join("/mnt/data", "component_aliases.json"))
    return list(dict.fromkeys(out))


def _load_component_alias_records() -> List[Dict[str, str]]:
    global _COMPONENT_ALIAS_CACHE, _COMPONENT_ALIAS_RECORDS_CACHE
    if _COMPONENT_ALIAS_RECORDS_CACHE is not None:
        return _COMPONENT_ALIAS_RECORDS_CACHE

    records: List[Dict[str, str]] = []
    for path in _component_alias_file_candidates():
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                for alias, info in raw.items():
                    if not isinstance(info, dict):
                        continue
                    bo_id = _clean_id(info.get("bo_id") or alias)
                    aspen_id = _clean_id(info.get("aspen_id") or bo_id)
                    rec = {
                        "alias": str(alias),
                        "bo_id": bo_id,
                        "display_name": str(info.get("display_name") or bo_id),
                        "aspen_key": str(info.get("aspen_key") or ""),
                        "aspen_id": aspen_id,
                    }
                    records.append(rec)
            break
        except Exception:
            continue

    # Built-in fallbacks for common short names.  File-based entries override
    # these because the alias table is searched in order and may include the
    # project-specific Aspen ID.
    fallback = [
        {"alias": "MeOH", "bo_id": "METHANOL", "display_name": "methanol", "aspen_key": "", "aspen_id": "METHANOL"},
        {"alias": "EtOH", "bo_id": "ETHANOL", "display_name": "ethanol", "aspen_key": "", "aspen_id": "ETHANOL"},
        {"alias": "EAc", "bo_id": "ETOAC", "display_name": "ethyl acetate", "aspen_key": "", "aspen_id": "ETHYL-01"},
        {"alias": "EA", "bo_id": "ETOAC", "display_name": "ethyl acetate", "aspen_key": "", "aspen_id": "ETHYL-01"},
        {"alias": "ethyl acetate", "bo_id": "ETOAC", "display_name": "ethyl acetate", "aspen_key": "", "aspen_id": "ETHYL-01"},
        {"alias": "water", "bo_id": "WATER", "display_name": "water", "aspen_key": "", "aspen_id": "WATER"},
        {"alias": "tetrahydrofuran", "bo_id": "THF", "display_name": "THF", "aspen_key": "", "aspen_id": "THF"},
        {"alias": "dimethylsulfoxide", "bo_id": "DMSO", "display_name": "DMSO", "aspen_key": "", "aspen_id": "DMSO"},
        {"alias": "dimethylsulphoxide", "bo_id": "DMSO", "display_name": "DMSO", "aspen_key": "", "aspen_id": "DMSO"},
    ]
    records.extend(fallback)

    index: Dict[str, Dict[str, str]] = {}
    for rec in records:
        keys = [
            rec.get("alias"), rec.get("bo_id"), rec.get("display_name"),
            rec.get("aspen_key"), rec.get("aspen_id"),
        ]
        for key in keys:
            lk = _alias_lookup_key(key)
            if lk and lk not in index:
                index[lk] = rec
    _COMPONENT_ALIAS_CACHE = index
    _COMPONENT_ALIAS_RECORDS_CACHE = records
    return records


def _component_alias_record(name: Any) -> Optional[Dict[str, str]]:
    _load_component_alias_records()
    key = _alias_lookup_key(name)
    if not key:
        return None
    return (_COMPONENT_ALIAS_CACHE or {}).get(key)


def _canonical_component_name(name: Any) -> str:
    up = _clean_id(name)
    rec = _component_alias_record(name)
    if rec and rec.get("bo_id"):
        return _clean_id(rec.get("bo_id"))
    compact = up.replace("-", "").replace("_", "").replace(" ", "")
    aliases = {
        "MEOH": "METHANOL",
        "ETOH": "ETHANOL",
        "TETRAHYDROFURAN": "THF",
        "DIMETHYLSULFOXIDE": "DMSO",
        "DIMETHYLSULPHOXIDE": "DMSO",
    }
    return aliases.get(compact, up)


def _aspen_component_id(name: Any) -> str:
    rec = _component_alias_record(name)
    if rec and rec.get("aspen_id"):
        return _clean_id(rec.get("aspen_id"))
    return _canonical_component_name(name)


def _component_aliases_for_component(name: Any, *, prefer_aspen_first: bool = False) -> List[str]:
    bo = _canonical_component_name(name)
    values: List[str] = []
    for rec in _load_component_alias_records():
        if _clean_id(rec.get("bo_id")) == bo:
            values.extend([
                rec.get("aspen_id"), rec.get("bo_id"), rec.get("alias"),
                rec.get("display_name"), rec.get("aspen_key"),
            ])
    values.extend([name, bo, _aspen_component_id(bo), _clean_id(name)])
    clean_values = [_clean_id(v) for v in values if _clean_id(v)]
    # Aspen write/read should try the registered Aspen ID first.
    if prefer_aspen_first:
        asp = _aspen_component_id(bo)
        clean_values = [asp] + [v for v in clean_values if v != asp]
    return list(dict.fromkeys(clean_values))


def _component_name_matches(left: Any, right: Any) -> bool:
    if not left or not right:
        return False
    if _canonical_component_name(left) == _canonical_component_name(right):
        return True
    lkey = _alias_lookup_key(left)
    return bool(lkey and lkey in {_alias_lookup_key(x) for x in _component_aliases_for_component(right)})


def _component_pairs(components: Sequence[str]) -> List[List[str]]:
    seen: List[str] = []
    for c in components:
        c = _canonical_component_name(c)
        if c and c not in seen:
            seen.append(c)
    out: List[List[str]] = []
    for i in range(len(seen)):
        for j in range(i + 1, len(seen)):
            out.append([seen[i], seen[j]])
    return out


def _normalise_selected_pairs(components: Sequence[str], selected_pairs: Optional[Sequence[Sequence[str]]]) -> List[List[str]]:
    available_components = {_canonical_component_name(c) for c in components if c}
    if selected_pairs is None:
        return _component_pairs(components)
    # Preserve the process-cut orientation supplied by the separation agent.
    # Re-sorting through the global component order can turn ETHANOL/WATER into
    # WATER/ETHANOL and obscures which component belongs to the top product.
    out: List[List[str]] = []
    seen = set()
    for pair in selected_pairs:
        clean = [_canonical_component_name(c) for c in _as_list(pair)[:2] if c]
        if len(clean) != 2 or clean[0] == clean[1]:
            continue
        if any(c not in available_components for c in clean):
            continue
        key = tuple(sorted(clean))
        if key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out


def _normalise_pair_contexts(pair_contexts: Any) -> Dict[str, dict]:
    """Normalize optional risk-edge context keyed by unordered component pair."""
    out: Dict[str, dict] = {}
    if isinstance(pair_contexts, dict):
        items = pair_contexts.items()
    else:
        items = []
        for row in _as_list(pair_contexts):
            rd = _as_dict(row)
            pair = rd.get("pair") or str(rd.get("pair_key") or "").split("/")
            items.append(("/".join([_canonical_component_name(x) for x in _as_list(pair)[:2]]), rd))
    for raw_key, raw_ctx in items:
        ctx = copy.deepcopy(_as_dict(raw_ctx))
        pair = ctx.get("pair") or str(ctx.get("pair_key") or raw_key or "").split("/")
        vals = [_canonical_component_name(x) for x in _as_list(pair)[:2] if x]
        if len(vals) != 2 or vals[0] == vals[1]:
            continue
        key = "/".join(sorted(vals))
        bg = {}
        for comp, val in _as_dict(ctx.get("background_component_ratios")).items():
            c = _canonical_component_name(comp)
            v = _safe_float(val, None)
            if c and c not in vals and v is not None and v > 0:
                bg[c] = float(v)
        s = sum(bg.values())
        if s > 0:
            bg = {k: float(v) / s for k, v in bg.items()}
        ctx["pair"] = vals
        ctx["pair_key"] = "/".join(vals)
        ctx["background_component_ratios"] = bg
        ctx["background_to_pair_ratio"] = max(0.0, min(3.0, float(_safe_float(ctx.get("background_to_pair_ratio"), 0.0) or 0.0)))
        out[key] = ctx
    return out


def _pair_context_for(pair: Sequence[str], pair_contexts: Any) -> dict:
    vals = [_canonical_component_name(x) for x in _as_list(pair)[:2] if x]
    if len(vals) != 2:
        return {}
    return copy.deepcopy(_normalise_pair_contexts(pair_contexts).get("/".join(sorted(vals))) or {})


def estimate_pressure_flash_workload(payload: dict) -> dict:
    """Estimate Aspen Run2 calls before launching a screen.

    The minimum assumes the first P+VFRAC attempt succeeds.  The maximum uses
    every configured vapour-fraction and temperature-fallback value.
    """
    pd = _as_dict(payload)
    cases: List[dict] = []
    if isinstance(pd.get("payloads"), dict):
        for sub in pd["payloads"].values():
            cases.extend([_as_dict(c) for c in _as_list(_as_dict(sub).get("flash_cases"))])
    else:
        cases = [_as_dict(c) for c in _as_list(pd.get("flash_cases"))]

    composition_points = 0
    minimum_runs = 0
    maximum_runs = 0
    case_rows = []
    for idx, case in enumerate(cases, 1):
        nx = len(_as_list(case.get("composition_grid_x_first_component") or DEFAULT_X_GRID))
        vf = _as_list(case.get("vapor_fraction_grid") or case.get("fraction_grid") or case.get("vapor_fraction") or DEFAULT_VAPOR_FRACTION_GRID)
        nvf = max(1, len(vf))
        if "temperature_fallback_grid_C" in case:
            nt = len(_as_list(case.get("temperature_fallback_grid_C")))
        else:
            nt = 0
        composition_points += nx
        minimum_runs += nx
        maximum_runs += nx * (nvf + nt)
        case_rows.append({
            "case_index": idx,
            "pair": case.get("pair"),
            "pressure_kpa": case.get("pressure_kpa"),
            "with_solvent": bool(case.get("with_solvent")),
            "solvent_to_feed_ratio": case.get("solvent_to_feed_ratio"),
            "composition_points": nx,
            "vapor_fraction_attempts": nvf,
            "temperature_fallback_attempts": nt,
            "minimum_aspen_runs": nx,
            "maximum_aspen_runs": nx * (nvf + nt),
        })
    return {
        "case_count": len(cases),
        "composition_point_count": composition_points,
        "minimum_aspen_runs": minimum_runs,
        "maximum_aspen_runs": maximum_runs,
        "cases": case_rows,
    }


def _pressure_atm(kpa: float) -> float:
    return float(kpa) / 101.325


def _relative_volatility_binary(x: Optional[float], y: Optional[float], eps: float = 1e-12) -> Optional[float]:
    if x is None or y is None:
        return None
    if x <= eps or x >= 1.0 - eps or y <= eps or y >= 1.0 - eps:
        return None
    return float(y * (1.0 - x) / (x * (1.0 - y)))



def _temperature_pack(raw: Any, unit_hint: str = "AUTO") -> dict:
    val = _safe_float(raw, None)
    unit = str(unit_hint or "AUTO").strip().upper().replace("°", "")
    if val is None:
        return {"raw": None, "C": None, "K": None, "unit": unit or "unknown", "unit_source": "unavailable"}
    if unit in {"K", "KELVIN"}:
        return {"raw": val, "C": val - 273.15, "K": val, "unit": "K", "unit_source": "explicit_or_node"}
    if unit in {"C", "CELSIUS", "DEGC"}:
        return {"raw": val, "C": val, "K": val + 273.15, "unit": "C", "unit_source": "explicit_or_node"}
    # Aspen stream nodes occasionally omit UnitString while returning Kelvin.
    # Values above 250 are therefore interpreted as Kelvin only in AUTO mode;
    # an explicit user unit always wins.
    if 250.0 <= float(val) <= 2000.0:
        return {"raw": val, "C": val - 273.15, "K": val, "unit": "K", "unit_source": "auto_inferred_from_magnitude"}
    return {"raw": val, "C": val, "K": val + 273.15, "unit": "C", "unit_source": "auto_inferred_from_magnitude"}

def _json_default(obj: Any) -> Any:
    try:
        if hasattr(obj, "item"):
            return obj.item()
    except Exception:
        pass
    return str(obj)


def _safe_json_dumps(obj: Any, indent: int = 2) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=indent, default=_json_default)


def _aspen_tree_path(path: Any) -> str:
    p = str(path or "").strip().replace("/", "\\")
    while "\\\\" in p:
        p = p.replace("\\\\", "\\")
    if not p.startswith("\\"):
        p = "\\" + p
    return p


# ---------------------------------------------------------------------------
# State/payload builders
# ---------------------------------------------------------------------------

def components_from_state(state_dict: Optional[dict]) -> List[str]:
    state = _as_dict(state_dict)
    comps = state.get("components") or state.get("component_ids") or []
    if isinstance(comps, dict):
        comps = list(comps.values())
    return [_canonical_component_name(c) for c in _as_list(comps) if c]


def solvent_from_state(state_dict: Optional[dict]) -> str:
    state = _as_dict(state_dict)
    for key in ("solvent", "solvent_component", "extractant", "extractive_solvent"):
        if state.get(key):
            return _canonical_component_name(state.get(key))
    for pack_key in ("topology", "intent", "intent_targets"):
        pack = _as_dict(state.get(pack_key))
        for key in ("solvent", "solvent_component", "extractant"):
            if pack.get(key):
                return _canonical_component_name(pack.get(key))
    return ""


def non_solvent_components_from_state(state_dict: Optional[dict]) -> List[str]:
    comps = components_from_state(state_dict)
    solv = solvent_from_state(state_dict)
    return [c for c in comps if c and c != solv]


def feed_condition_from_state(state_dict: Optional[dict]) -> dict:
    state = _as_dict(state_dict)
    temp = _safe_float(
        state.get("feed_temperature_C")
        or state.get("temperature_C")
        or state.get("feed_temperature")
        or 25.0,
        25.0,
    )
    pres_kpa = _safe_float(
        state.get("pressure_kpa")
        or state.get("feed_pressure_kpa")
        or None,
        None,
    )
    if pres_kpa is None:
        pres_atm = _safe_float(state.get("pressure") or state.get("pressure_atm") or 1.0, 1.0)
        pres_kpa = float(pres_atm) * 101.325
    flow = _safe_float(state.get("feed_flow") or state.get("feed_flow_kmol_h") or 100.0, 100.0)
    return {
        "temperature_C": temp,
        "pressure_kpa": pres_kpa,
        "feed_flow_kmol_h": flow,
    }


def build_pressure_flash_payload_from_state(
        state_dict: Optional[dict],
        *,
        mode: str = "solvent_free_psd",
        pressure_grid_kpa: Optional[Sequence[float]] = None,
        x_grid: Optional[Sequence[float]] = None,
        solvent_to_feed_grid: Optional[Sequence[float]] = None,
        vapor_fraction_grid: Optional[Sequence[float]] = None,
        temperature_fallback_grid_C: Optional[Sequence[float]] = None,
        selected_pairs: Optional[Sequence[Sequence[str]]] = None,
        pair_contexts: Optional[Any] = None,
        scan_profile: str = "fine",
        include_solvent_free_baseline: bool = True,
) -> dict:
    state = _as_dict(state_dict)
    non_solvent = non_solvent_components_from_state(state)
    solvent = solvent_from_state(state)
    profile = str(scan_profile or "fine").strip().lower()
    use_fast = profile in {"fast", "coarse", "engineering", "screen"}
    pressure_default = FAST_PRESSURE_GRID_KPA if use_fast else DEFAULT_PRESSURE_GRID_KPA
    x_default = FAST_X_GRID if use_fast else DEFAULT_X_GRID
    sfr_default = FAST_SOLVENT_TO_FEED_GRID if use_fast else DEFAULT_SOLVENT_TO_FEED_GRID
    vf_default = FAST_VAPOR_FRACTION_GRID if use_fast else DEFAULT_VAPOR_FRACTION_GRID
    pressure_grid = [float(p) for p in (pressure_grid_kpa if pressure_grid_kpa is not None else pressure_default)]
    xs = [float(x) for x in (x_grid if x_grid is not None else x_default)]
    sfrs = [float(s) for s in (solvent_to_feed_grid if solvent_to_feed_grid is not None else sfr_default)]
    vf_grid = [float(v) for v in (vapor_fraction_grid if vapor_fraction_grid is not None else vf_default)]
    if temperature_fallback_grid_C is None:
        temp_fallback = list(FAST_TEMPERATURE_FALLBACK_GRID_C if use_fast else DEFAULT_TEMPERATURE_FALLBACK_GRID_C)
    else:
        temp_fallback = [float(t) for t in temperature_fallback_grid_C]
    pairs = _normalise_selected_pairs(non_solvent, selected_pairs)
    contexts = _normalise_pair_contexts(pair_contexts if pair_contexts is not None else state.get("pressure_screen_pair_contexts"))
    pressure_grid = _vacuum_inclusive_pressure_grid(pressure_grid, state, contexts)
    feed_condition = feed_condition_from_state(state)
    cfg = _as_dict(state.get("pressure_swing_flash_config"))
    max_valid_flash_temperature_C = _safe_float(
        cfg.get("max_valid_flash_temperature_C", state.get("max_valid_flash_temperature_C")),
        DEFAULT_MAX_VALID_FLASH_TEMPERATURE_C,
    ) or DEFAULT_MAX_VALID_FLASH_TEMPERATURE_C
    min_valid_flash_temperature_C = _safe_float(
        cfg.get("min_valid_flash_temperature_C", state.get("min_valid_flash_temperature_C")),
        DEFAULT_MIN_VALID_FLASH_TEMPERATURE_C,
    ) or DEFAULT_MIN_VALID_FLASH_TEMPERATURE_C

    flash_cases: List[dict] = []
    if include_solvent_free_baseline:
        for pair in pairs:
            for p_kpa in pressure_grid:
                pair_ctx = copy.deepcopy(contexts.get("/".join(sorted(pair))) or {})
                flash_cases.append({
                    "case_type": "binary_pressure_vle",
                    "pair": pair,
                    "risk_edge_context": pair_ctx,
                    "background_component_ratios": pair_ctx.get("background_component_ratios") or {},
                    "background_to_pair_ratio": pair_ctx.get("background_to_pair_ratio", 0.0),
                    "source_column_id": pair_ctx.get("column_id"),
                    "risk_role": pair_ctx.get("risk_role"),
                    "reference_base_pressure_kpa": pair_ctx.get("reference_base_pressure_kpa"),
                    "reference_pair_fraction_first_component": pair_ctx.get("reference_pair_fraction_first_component"),
                    "reference_solvent_to_feed_ratio": pair_ctx.get("reference_solvent_to_feed_ratio"),
                    "top_component": pair_ctx.get("top_component"),
                    "bottom_component": pair_ctx.get("bottom_component"),
                    "desired_direction": pair_ctx.get("desired_direction"),
                    "max_valid_flash_temperature_C": max_valid_flash_temperature_C,
                    "min_valid_flash_temperature_C": min_valid_flash_temperature_C,
                    "pressure_kpa": p_kpa,
                    "pressure_atm": round(_pressure_atm(p_kpa), 6),
                    "with_solvent": False,
                    "flash_block": "Flash2",
                    "flash_spec": "P_FRAC",
                    "vapor_fraction_grid": vf_grid,
                    "composition_grid_x_first_component": xs,
                    "temperature_fallback_grid_C": temp_fallback,
                })

    if mode != "solvent_free_psd" and solvent:
        for pair in pairs:
            pair_ctx = copy.deepcopy(contexts.get("/".join(sorted(pair))) or {})
            pair_sfrs = list(sfrs)
            ref_sfr = _safe_float(pair_ctx.get("reference_solvent_to_feed_ratio"), None)
            if ref_sfr is not None and ref_sfr > 0:
                pair_sfrs = sorted({round(max(0.05, float(ref_sfr) * factor), 6) for factor in (0.8, 1.0, 1.2)})
            for p_kpa in pressure_grid:
                for sfr in pair_sfrs:
                    flash_cases.append({
                        "case_type": "pseudo_ternary_with_solvent_pressure_vle",
                        "pair": pair,
                        "risk_edge_context": pair_ctx,
                        "background_component_ratios": pair_ctx.get("background_component_ratios") or {},
                        "background_to_pair_ratio": pair_ctx.get("background_to_pair_ratio", 0.0),
                        "source_column_id": pair_ctx.get("column_id"),
                        "risk_role": pair_ctx.get("risk_role"),
                        "reference_base_pressure_kpa": pair_ctx.get("reference_base_pressure_kpa"),
                        "pressure_kpa": p_kpa,
                        "pressure_atm": round(_pressure_atm(p_kpa), 6),
                        "with_solvent": True,
                        "solvent": solvent,
                        "solvent_to_feed_ratio": sfr,
                        "flash_block": "Flash2",
                        "flash_spec": "P_FRAC",
                        "vapor_fraction_grid": vf_grid,
                        "composition_grid_x_first_component": xs,
                        "temperature_fallback_grid_C": temp_fallback,
                    })

    return {
        "action": "run_aspen_flash2_pressure_sensitivity_screen",
        "runner_version": RUNNER_VERSION,
        "mode": mode,
        "scan_profile": profile,
        "components": non_solvent,
        "solvent": solvent if mode != "solvent_free_psd" else None,
        "pressure_grid_kpa": pressure_grid,
        "pressure_grid_atm": [round(_pressure_atm(p), 6) for p in pressure_grid],
        "vacuum_screening_enabled": any(p < VACUUM_PRESSURE_THRESHOLD_KPA for p in pressure_grid),
        "vacuum_pressure_points_kpa": [p for p in pressure_grid if p < VACUUM_PRESSURE_THRESHOLD_KPA],
        "pressure_basis": "positive_absolute_pressure_kpa",
        "vapor_fraction_grid": vf_grid,
        "feed_condition": feed_condition,
        "selected_pairs": pairs,
        "pair_contexts": contexts,
        "flash_case_count": len(flash_cases),
        "flash_cases": flash_cases,
    }


def build_binary_triptych_pressure_flash_payload_from_state(
        state_dict: Optional[dict],
        *,
        pressure_grid_kpa: Optional[Sequence[float]] = None,
        x_grid: Optional[Sequence[float]] = None,
        vapor_fraction_grid: Optional[Sequence[float]] = None,
        pressure_write_mode: str = DEFAULT_BINARY_TRIPTYCH_PRESSURE_WRITE_MODE,
) -> dict:
    """
    Build the canonical binary 1x3 y-x scan payload used by the integrated demo.

    This deliberately uses the native binary Flash2 scan, not the ternary-map
    projection. The pressure/composition/vapour-fraction grids mirror the
    single-pair academic plot so adding the 1x3 layout does not change the
    thermodynamic data source.
    """
    payload = build_pressure_flash_payload_from_state(
        state_dict,
        mode="solvent_free_psd",
        pressure_grid_kpa=pressure_grid_kpa or DEFAULT_BINARY_TRIPTYCH_PRESSURE_GRID_KPA,
        x_grid=x_grid or DEFAULT_BINARY_TRIPTYCH_X_GRID,
        vapor_fraction_grid=vapor_fraction_grid or DEFAULT_BINARY_TRIPTYCH_VAPOR_FRACTION_GRID,
    )
    payload["scan_profile"] = "binary_triptych_native_flash2_paper_grid"
    payload["recommended_pressure_write_mode"] = str(pressure_write_mode or DEFAULT_BINARY_TRIPTYCH_PRESSURE_WRITE_MODE).lower()
    payload["note"] = (
        "Use run_pressure_flash_payload(..., pressure_write_mode=payload['recommended_pressure_write_mode']) "
        "or run_binary_triptych_pressure_flash_screen() so the 1x3 figure uses the same pressure-write convention as the single-pair scan."
    )
    return payload


def build_fast_pressure_flash_payload_from_state(
        state_dict: Optional[dict],
        *,
        mode: str = "combined",
        pressure_grid_kpa: Optional[Sequence[float]] = None,
        x_grid: Optional[Sequence[float]] = None,
        solvent_to_feed_grid: Optional[Sequence[float]] = None,
        vapor_fraction_grid: Optional[Sequence[float]] = None,
        selected_pairs: Optional[Sequence[Sequence[str]]] = None,
) -> dict:
    """Build a sparse engineering screen with temperature fallback disabled."""
    kwargs = dict(
        pressure_grid_kpa=pressure_grid_kpa,
        x_grid=x_grid,
        solvent_to_feed_grid=solvent_to_feed_grid,
        vapor_fraction_grid=vapor_fraction_grid,
        temperature_fallback_grid_C=[],
        selected_pairs=selected_pairs,
        scan_profile="fast",
    )
    if mode == "combined":
        payload = {
            "action": "run_combined_aspen_flash2_pressure_sensitivity_screen",
            "runner_version": RUNNER_VERSION,
            "scan_profile": "fast",
            "feed_condition": feed_condition_from_state(state_dict),
            "payloads": {
                "PSD": build_pressure_flash_payload_from_state(
                    state_dict, mode="solvent_free_psd", include_solvent_free_baseline=True, **kwargs
                ),
                "EPSD": build_pressure_flash_payload_from_state(
                    state_dict, mode="extractive_pressure_swing", include_solvent_free_baseline=False, **kwargs
                ),
            },
        }
    else:
        payload = build_pressure_flash_payload_from_state(
            state_dict,
            mode=mode,
            include_solvent_free_baseline=(mode == "solvent_free_psd"),
            **kwargs,
        )
    payload["workload_estimate"] = estimate_pressure_flash_workload(payload)
    return payload


def build_combined_pressure_flash_payload_from_state(state_dict: Optional[dict]) -> dict:
    return {
        "runner_version": RUNNER_VERSION,
        "action": "run_combined_aspen_flash2_pressure_sensitivity_screen",
        "payloads": {
            "PSD": build_pressure_flash_payload_from_state(state_dict, mode="solvent_free_psd"),
            "EPSD": build_pressure_flash_payload_from_state(state_dict, mode="extractive_pressure_swing"),
        },
    }


# ---------------------------------------------------------------------------
# Aspen COM wrapper
# ---------------------------------------------------------------------------

class AspenPressureFlashScreenRunner:
    def __init__(
            self,
            aspen: Any,
            logger: Optional[Callable[[str], None]] = None,
            *,
            pressure_write_mode: str = "kpa",
            run_timeout_s: int = 90,
            flash_block_id: str = "PFL2",
            feed_stream_id: str = "PFFED",
            vapour_stream_id: str = "PFVAP",
            liquid_stream_id: str = "PFLIQ",
            total_non_solvent_flow: float = 100.0,
            default_temperature_C: float = 25.0,
            reuse_flash_unit: bool = True,
            enable_temperature_fallback: bool = True,
            temperature_fallback_grid_C: Optional[Sequence[float]] = None,
            log_level: str = "normal",
            cache_tree_nodes: bool = True,
            temperature_output_unit: str = "AUTO",
            composition_sum_tolerance: float = 0.03,
            overall_flow_balance_tolerance: float = 0.05,
            component_balance_tolerance: float = 0.10,
            rebuild_flash_on_component_change: bool = True,
    ) -> None:
        self.aspen = aspen
        self.logger = logger
        self.pressure_write_mode = str(pressure_write_mode or "kpa").lower()
        self.run_timeout_s = int(run_timeout_s)
        self.flash_block_id = _clean_id(flash_block_id, 8) or "PFL2"
        self.feed_stream_id = _clean_id(feed_stream_id, 8) or "PFFED"
        self.vapour_stream_id = _clean_id(vapour_stream_id, 8) or "PFVAP"
        self.liquid_stream_id = _clean_id(liquid_stream_id, 8) or "PFLIQ"
        self.total_non_solvent_flow = float(total_non_solvent_flow)
        self.default_temperature_C = float(default_temperature_C)
        self.reuse_flash_unit = bool(reuse_flash_unit)
        self.enable_temperature_fallback = bool(enable_temperature_fallback)
        self.temperature_fallback_grid_C = (
            [float(t) for t in temperature_fallback_grid_C]
            if temperature_fallback_grid_C is not None else list(DEFAULT_TEMPERATURE_FALLBACK_GRID_C)
        )
        self.log_level = str(log_level or "normal").strip().lower()
        self.cache_tree_nodes = bool(cache_tree_nodes)
        self._node_cache: Dict[str, Any] = {}
        self._flash_unit_ready = False
        self.temperature_output_unit = str(temperature_output_unit or "C").strip().upper()
        self.composition_sum_tolerance = max(1e-6, float(composition_sum_tolerance))
        self.overall_flow_balance_tolerance = max(1e-6, float(overall_flow_balance_tolerance))
        self.component_balance_tolerance = max(1e-6, float(component_balance_tolerance))
        self.rebuild_flash_on_component_change = bool(rebuild_flash_on_component_change)
        self._component_environment_signature: Tuple[str, ...] = tuple()
        self._active_flash_component_signature: Tuple[str, ...] = tuple()
        self._last_run_status: Dict[str, Any] = {}
        self._component_registry_snapshot: Dict[str, Any] = {}
        self.log_lines: List[str] = []

    def _log_is_enabled(self, text: str) -> bool:
        if self.log_level in {"debug", "verbose", "trace"}:
            return True
        if self.log_level in {"quiet", "error"}:
            return text.startswith(("[CASE-FAIL]", "[CASE-POINT-FAIL]", "[WARN]", "[PRESSURE-RANGE-WARN]"))
        noisy = (
            "[WRITE]", "[BO-FEED-WRITE]", "[BO-FEED-CLEAR]", "[BO-FEED-OK]",
            "[READ-STREAM-TABLE]", "[READ-STREAM-FRAC]", "[READ-FRAC-FROM-STREAM-FLOW]",
            "[FLASH-SPEC]", "[FLASH-SPEC-MODE]", "[CONNECT]", "[SKIP]",
        )
        return not text.startswith(noisy)

    def log(self, msg: str) -> None:
        text = str(msg)
        if not self._log_is_enabled(text):
            return
        self.log_lines.append(text)
        if callable(self.logger):
            try:
                self.logger(text)
            except Exception:
                pass
        else:
            print(text)

    def invalidate_node_cache(self, prefix: Optional[str] = None) -> None:
        if prefix is None:
            self._node_cache.clear()
            return
        aprefix = _aspen_tree_path(prefix).upper()
        for key in list(self._node_cache):
            if key.upper().startswith(aprefix):
                self._node_cache.pop(key, None)

    def node(self, path: str) -> Any:
        apath = _aspen_tree_path(path)
        if self.cache_tree_nodes and apath in self._node_cache:
            return self._node_cache[apath]
        try:
            node = self.aspen.Tree.FindNode(apath)
            if self.cache_tree_nodes and node is not None:
                self._node_cache[apath] = node
            return node
        except Exception:
            return None

    def _pressure_value_for_aspen(self, pressure_kpa: float) -> float:
        return _pressure_atm(pressure_kpa) if self.pressure_write_mode == "atm" else float(pressure_kpa)

    def elements_names(self, path: str) -> List[str]:
        n = self.node(path)
        if n is None or not getattr(n, "Elements", None):
            return []
        out: List[str] = []
        try:
            for item in n.Elements:
                nm = str(getattr(item, "Name", "") or "")
                if nm:
                    out.append(nm)
        except Exception:
            pass
        return out

    def get_or_create(self, parent: Any, child_name: str) -> Any:
        if parent is None:
            return None
        try:
            return parent.Elements(child_name)
        except Exception:
            pass
        try:
            parent.Elements.Add(child_name)
            return parent.Elements(child_name)
        except Exception:
            pass
        try:
            parent.Elements.InsertRow(0, 0)
            parent.Elements.SetLabel(0, 0, False, child_name)
            return parent.Elements(child_name)
        except Exception:
            return None

    def ensure_table_row(self, table_path: str, row_name: str) -> Any:
        row_name = _clean_id(row_name)
        table = self.node(table_path)
        if table is None:
            return None
        try:
            return table.Elements(row_name)
        except Exception:
            pass
        attempts = [
            lambda: table.Elements.Add(row_name),
            lambda: table.Elements.InsertRow(0, 0),
            lambda: table.Elements.InsertRow(0),
        ]
        for fn in attempts:
            try:
                fn()
                try:
                    table.Elements.SetLabel(0, 0, False, row_name)
                except Exception:
                    try:
                        table.Elements.SetLabel(0, row_name)
                    except Exception:
                        pass
                try:
                    return table.Elements(row_name)
                except Exception:
                    try:
                        for item in table.Elements:
                            # For input table creation we must use the exact Aspen row id.
                            # Do not treat aliases such as ETOAC and ETHYL-01 as the same row here:
                            # an old GUI alias row can be accepted by the COM tree but ignored by Flash2.
                            nm = _clean_id(str(getattr(item, "Name", "") or ""))
                            if nm == row_name:
                                return item
                    except Exception:
                        pass
            except Exception:
                continue
        return None

    def set_value(self, path: str, value: Any, *, required: bool = False) -> bool:
        n = self.node(path)
        if n is None:
            if required:
                raise RuntimeError(f"Aspen path not found: {path}")
            self.log(f"[WARN] path not found: {path}")
            return False
        try:
            n.Value = value
            self.log(f"[WRITE] {path} = {value}")
            return True
        except Exception as exc:
            if required:
                raise RuntimeError(f"Cannot write Aspen path: {path}={value}; {exc}") from exc
            self.log(f"[WARN] write failed: {path}={value}; {exc}")
            return False

    def set_first_existing(self, path_values: Sequence[Tuple[str, Any]], *, required: bool = False) -> bool:
        for path, value in path_values:
            n = self.node(path)
            if n is None:
                continue
            try:
                n.Value = value
                self.log(f"[WRITE] {path} = {value}")
                return True
            except Exception as exc:
                self.log(f"[WARN] write candidate failed: {path}={value}; {exc}")
        if required:
            raise RuntimeError("None of the Aspen write candidates succeeded: " + "; ".join(p for p, _ in path_values))
        self.log("[WARN] no write candidate succeeded: " + "; ".join(p for p, _ in path_values))
        return False

    def get_value(self, path: str) -> Any:
        try:
            n = self.node(path)
            return n.Value if n is not None else None
        except Exception:
            return None


    def _registered_component_ids(self) -> List[str]:
        return [_canonical_component_name(x) for x in self._registered_aspen_component_ids_exact()]

    def _registered_aspen_component_ids_exact(self) -> List[str]:
        node = self.node(r"\Data\Components\Specifications\Input\TYPE")
        out: List[str] = []
        if node is None or not getattr(node, "Elements", None):
            return out
        try:
            for item in node.Elements:
                for attr in ("Label", "Name", "Value"):
                    try:
                        vv = _clean_id(getattr(item, attr, None))
                    except Exception:
                        vv = ""
                    if vv:
                        out.append(vv)
        except Exception:
            pass
        return list(dict.fromkeys(out))

    def _aspen_component_register_id(self, comp: str) -> str:
        return _aspen_component_id(comp)


    def ensure_components_registered(self, components: Sequence[str], *, thermo_method: Optional[str] = None) -> dict:
        clean_components: List[str] = []
        for c in components:
            cc = _canonical_component_name(c)
            if cc and cc not in clean_components:
                clean_components.append(cc)
        if not clean_components:
            return dict(self._component_registry_snapshot)

        requested = {comp: self._aspen_component_register_id(comp) for comp in clean_components}
        comp_node = self.node(r"\Data\Components\Specifications\Input\TYPE")
        added: List[str] = []
        if comp_node is None:
            raise RuntimeError("Aspen component TYPE table not found; component registration cannot be verified.")

        existing_exact = set(self._registered_aspen_component_ids_exact())
        for comp in reversed(clean_components):
            reg_id = requested[comp]
            if reg_id in existing_exact:
                self.log(f"[SKIP] component registered: {reg_id}")
                continue
            try:
                comp_node.Elements.InsertRow(0, 0)
                comp_node.Elements.SetLabel(0, 0, False, reg_id)
                added.append(reg_id)
                self.log(f"[COMP] registered {comp} as {reg_id}")
            except Exception as exc:
                self.log(f"[WARN] component registration failed for {comp} as {reg_id}; {exc}")

        method = str(thermo_method or "").strip().upper()
        method_written = False
        if method:
            for path in (
                    r"\Data\Properties\Specifications\Input\GBASEOPSET",
                    r"\Data\Properties\Specifications\Input\GOPSETNAME",
            ):
                n = self.node(path)
                if n is not None:
                    try:
                        n.Value = method
                        method_written = True
                        self.log(f"[PROP] {path} = {method}")
                        break
                    except Exception as exc:
                        self.log(f"[WARN] property method write failed: {path}={method}; {exc}")

        if added:
            self.invalidate_node_cache(r"\Data\Components")
            self.invalidate_node_cache(r"\Data\Properties")
            # Component tables on already-created streams/blocks are position-sensitive.
            # Rebuild the temporary Flash2 unit after any component-set expansion.
            if self.rebuild_flash_on_component_change and self._flash_unit_ready:
                self._flash_unit_ready = False
                self._active_flash_component_signature = tuple()
                self.log("[COMP-CHANGE] component registry expanded; temporary Flash2 will be rebuilt")

        exact_after = self._registered_aspen_component_ids_exact()
        exact_set = set(exact_after)
        missing = [reg_id for reg_id in requested.values() if reg_id not in exact_set]
        if missing:
            raise RuntimeError(
                "Aspen component registration verification failed. Missing exact IDs: "
                + ", ".join(missing)
                + f"; registered={exact_after}"
            )

        union_ids = sorted(set(self._component_environment_signature).union(requested.values()))
        new_signature = tuple(union_ids)
        if (
            self.rebuild_flash_on_component_change
            and self._active_flash_component_signature
            and new_signature != self._active_flash_component_signature
        ):
            self._flash_unit_ready = False
            self._active_flash_component_signature = tuple()
        self._component_environment_signature = new_signature
        self._component_registry_snapshot = {
            "requested": requested,
            "registered_exact_ids": exact_after,
            "missing_exact_ids": missing,
            "added_exact_ids": added,
            "thermo_method": method or None,
            "thermo_method_written": method_written,
            "component_signature": list(new_signature),
            "verified": not missing,
        }
        return dict(self._component_registry_snapshot)

    def bo_get_or_create(self, parent_node: Any, child_name: str) -> Any:
        if parent_node is None:
            return None
        try:
            return parent_node.Elements(child_name)
        except Exception:
            pass
        try:
            parent_node.Elements.Add(child_name)
            return parent_node.Elements(child_name)
        except Exception:
            return None

    def bo_set_val_robust(self, parent_node: Any, child_name: str, value: Any) -> bool:
        node = self.bo_get_or_create(parent_node, child_name)
        if node is None:
            return False
        try:
            node.Value = value
            return True
        except Exception:
            return False

    def bo_force_populate_stream(self, stream_id: str, temp: float, pres: float, flow_dict: Dict[str, float]) -> Dict[
        str, Any]:
        sid = _clean_id(stream_id, 8)
        in_node = self.node(rf"\Data\Streams\{sid}\Input")
        if in_node is None:
            raise RuntimeError(f"BO-style stream population failed: stream input node not found: {sid}")

        temp_node = self.bo_get_or_create(in_node, "TEMP")
        pres_node = self.bo_get_or_create(in_node, "PRES")
        self.bo_set_val_robust(temp_node, "MIXED", float(temp))
        self.bo_set_val_robust(pres_node, "MIXED", float(pres))

        flow_node = self.bo_get_or_create(in_node, "FLOW")
        mixed_node = self.bo_get_or_create(flow_node, "MIXED")
        if mixed_node is None:
            raise RuntimeError(f"BO-style stream population failed: FLOW/MIXED node not creatable for {sid}")

        self.clear_stream_component_flows(sid)

        written: Dict[str, str] = {}
        failed: Dict[str, str] = {}
        for comp, val in flow_dict.items():
            comp_c = _canonical_component_name(comp)
            target_id = self.resolve_component_id(mixed_node, comp_c)
            comp_node = self.bo_get_or_create(mixed_node, target_id)
            if comp_node is None:
                failed[comp_c] = f"cannot create FLOW/MIXED/{target_id}"
                continue
            try:
                comp_node.Value = float(val)
                written[comp_c] = target_id
                self.log(f"[BO-FEED-WRITE] {sid}: FLOW/MIXED/{target_id} = {float(val):.8g}")
            except Exception as exc:
                failed[comp_c] = f"write failed FLOW/MIXED/{target_id}: {exc}"

        try:
            self._write_stream_total_flow(sid, sum(float(v or 0.0) for v in flow_dict.values()))
        except Exception:
            pass

        readback: Dict[str, Optional[float]] = {}
        for comp in flow_dict:
            comp_c = _canonical_component_name(comp)
            val = None
            candidates = self._component_aliases_for_write(comp_c)
            if written.get(comp_c):
                candidates.append(written[comp_c])
            for cid in candidates:
                raw = self.get_value(rf"\Data\Streams\{sid}\Input\FLOW\\MIXED\{cid}")
                if raw is not None:
                    val = _safe_float(raw, None)
                    if val is not None:
                        break
            if val is None and mixed_node is not None and getattr(mixed_node, "Elements", None):
                try:
                    aliases = {_canonical_component_name(a) for a in candidates}
                    for item in mixed_node.Elements:
                        nm = str(getattr(item, "Name", "") or "")
                        if _canonical_component_name(nm) in aliases:
                            val = _safe_float(getattr(item, "Value", None), None)
                            break
                except Exception:
                    pass
            readback[comp_c] = val

        good = {k: v for k, v in readback.items() if v is not None and abs(float(v)) > 1e-14}
        if len(good) < 2:
            debug = {
                "input_children": self.elements_names(rf"\Data\Streams\{sid}\Input"),
                "flow_children": self.elements_names(rf"\Data\Streams\{sid}\Input\FLOW"),
                "mixed_children": self.elements_names(rf"\Data\Streams\{sid}\Input\FLOW\\MIXED"),
                "written": written,
                "failed": failed,
                "readback": readback,
                "requested_flows": flow_dict,
            }
            raise RuntimeError(
                "BO-style feed stream write verification failed; Aspen would run with blank feed. "
                + _safe_json_dumps(debug, indent=2)
            )

        self.log(f"[BO-FEED-OK] {sid}: readback={good}")
        return {"written": written, "failed": failed, "readback": readback, "good": good}

    def remove_named_flowsheet_object(self, parent_path: str, obj_id: str) -> None:
        parent = self.node(parent_path)
        if parent is None:
            return
        oid = _clean_id(obj_id, 8)
        try:
            if self.node(parent_path + "\\" + oid) is not None:
                parent.Elements.Remove(oid)
                self.invalidate_node_cache(parent_path + "\\" + oid)
                self.log(f"[REMOVE] {parent_path}\\{oid}")
        except Exception:
            pass

    def add_block(self, block_id: str, block_type: str = "Flash2") -> str:
        bid = _clean_id(block_id, 8)
        if self.node(rf"\Data\Blocks\{bid}") is not None:
            self.log(f"[SKIP] block exists: {bid}")
            return bid
        blocks = self.node(r"\Data\Blocks")
        if blocks is None:
            raise RuntimeError("Cannot find \\Data\\Blocks")
        blocks.Elements.Add(f"{bid}!{block_type}")
        self.log(f"[ADD] block {bid}!{block_type}")
        return bid

    def add_stream(self, stream_id: str, stream_type: str = "MATERIAL") -> str:
        sid = _clean_id(stream_id, 8)
        if self.node(rf"\Data\Streams\{sid}") is not None:
            self.log(f"[SKIP] stream exists: {sid}")
            return sid
        streams = self.node(r"\Data\Streams")
        if streams is None:
            raise RuntimeError("Cannot find \\Data\\Streams")
        streams.Elements.Add(f"{sid}!{stream_type}")
        self.log(f"[ADD] stream {sid}!{stream_type}")
        return sid

    def connect_stream_to_port(self, block_id: str, stream_id: str, port_candidates: Sequence[str],
                               role: str = "") -> bool:
        bid = _clean_id(block_id, 8)
        sid = _clean_id(stream_id, 8)
        for port in port_candidates:
            p = self.node(rf"\Data\Blocks\{bid}\Ports\{port}")
            if p is None:
                continue
            try:
                existing = {_clean_id(x) for x in self.elements_names(rf"\Data\Blocks\{bid}\Ports\{port}")}
                if sid not in existing:
                    p.Elements.Add(sid)
                self.log(f"[CONNECT] {sid} -> {bid}:{port} {role}".rstrip())
                return True
            except Exception as exc:
                self.log(f"[WARN] connect failed candidate: {sid}->{bid}:{port}; {exc}")
        self.log(f"[WARN] no port candidate connected for {sid}->{bid}; candidates={list(port_candidates)}")
        return False


    def ensure_flash_unit(self) -> Tuple[str, str, str, str]:
        bid = _clean_id(self.flash_block_id, 8) or "PFL2"
        feed = _clean_id(self.feed_stream_id, 8) or "PFFED"
        vap = _clean_id(self.vapour_stream_id, 8) or "PFVAP"
        liq = _clean_id(self.liquid_stream_id, 8) or "PFLIQ"
        expected_signature = tuple(self._component_environment_signature)

        signature_matches = (
            not self.rebuild_flash_on_component_change
            or not expected_signature
            or expected_signature == self._active_flash_component_signature
        )
        if self.reuse_flash_unit and self._flash_unit_ready and signature_matches:
            if (
                self.node(rf"\Data\Blocks\{bid}") is not None
                and self.node(rf"\Data\Streams\{feed}") is not None
                and self.node(rf"\Data\Streams\{vap}") is not None
                and self.node(rf"\Data\Streams\{liq}") is not None
            ):
                return bid, feed, vap, liq
            self._flash_unit_ready = False

        if self._flash_unit_ready and not signature_matches:
            self.log(
                f"[FLASH-REBUILD] component signature changed: "
                f"{self._active_flash_component_signature} -> {expected_signature}"
            )
        for sid in (feed, vap, liq):
            self.remove_named_flowsheet_object(r"\Data\Streams", sid)
        self.remove_named_flowsheet_object(r"\Data\Blocks", bid)
        self.invalidate_node_cache()

        blocks = self.node(r"\Data\Blocks")
        streams = self.node(r"\Data\Streams")
        if blocks is None or streams is None:
            raise RuntimeError("Cannot find Aspen Data/Blocks or Data/Streams")

        try:
            blocks.Elements.Add(f"{bid}!Flash2")
            self.log(f"[BO-ADD] block {bid}!Flash2")
        except Exception as exc:
            if self.node(rf"\Data\Blocks\{bid}") is None:
                raise RuntimeError(f"Cannot create Flash2 block {bid}: {exc}") from exc

        for sid in (feed, vap, liq):
            try:
                streams.Elements.Add(f"{sid}!MATERIAL")
                self.log(f"[BO-ADD] stream {sid}!MATERIAL")
            except Exception as exc:
                if self.node(rf"\Data\Streams\{sid}") is None:
                    raise RuntimeError(f"Cannot create material stream {sid}: {exc}") from exc

        connected = [
            self.connect_stream_to_port(bid, feed, ["F(IN)", "IN", "FEED", "FEED(IN)"], role="Flash2 feed"),
            self.connect_stream_to_port(bid, vap, ["V(OUT)", "VAPOR(OUT)", "VAPOUR(OUT)", "V"], role="Flash2 vapour"),
            self.connect_stream_to_port(bid, liq, ["L(OUT)", "LIQUID(OUT)", "LIQ(OUT)", "L"], role="Flash2 liquid"),
        ]
        if not all(connected):
            raise RuntimeError(f"Temporary Flash2 port connection failed: {connected}")
        self._flash_unit_ready = True
        self._active_flash_component_signature = expected_signature
        return bid, feed, vap, liq

    def resolve_component_id(self, mixed_node: Any, component: str) -> str:
        comp = _canonical_component_name(component)
        target = _aspen_component_id(comp)
        # Strict write rule:
        # Always write to the registered Aspen component id.  Do not reuse an
        # existing alias row such as ETOAC when the registered component row is
        # ETHYL-01.  Aspen can preserve alias rows in the stream input table,
        # and those rows may read back nonzero but still be ignored by Flash2.
        if mixed_node is not None and getattr(mixed_node, "Elements", None):
            try:
                for item in mixed_node.Elements:
                    name = _clean_id(str(getattr(item, "Name", "") or ""))
                    if name == target:
                        return name
            except Exception:
                pass
        return target

    def clear_stream_component_flows(self, stream_id: str) -> None:
        sid = _clean_id(stream_id, 8)
        cleared = 0
        # Clear stale component rows before every Flash point.  This is essential
        # when a previous GUI alias row (e.g. ETOAC) coexists with the Aspen
        # registered row (e.g. ETHYL-01); otherwise the stream may contain a
        # hidden invalid component amount and Flash2 can return zero outlet flow.
        for parent in (
                rf"\Data\Streams\{sid}\Input\FLOW\\MIXED",
                rf"\Data\Streams\{sid}\Input\MOLEFLOW\\MIXED",
                rf"\Data\Streams\{sid}\Input\MOLE-FLOW\\MIXED",
                rf"\Data\Streams\{sid}\Input\MASSFLOW\\MIXED",
                rf"\Data\Streams\{sid}\Input\FRAC\MIXED",
                rf"\Data\Streams\{sid}\Input\MOLEFRAC\MIXED",
                rf"\Data\Streams\{sid}\Input\MOLE-FRAC\MIXED",
        ):
            node = self.node(parent)
            if node is None or not getattr(node, "Elements", None):
                continue
            try:
                for item in node.Elements:
                    try:
                        item.Value = 0.0
                        cleared += 1
                    except Exception:
                        pass
            except Exception:
                pass
        if cleared:
            self.log(f"[BO-FEED-CLEAR] {sid}: cleared {cleared} stale component input cells")

    def _component_aliases_for_write(self, comp: str) -> List[str]:
        return _component_aliases_for_component(comp, prefer_aspen_first=True)

    def _write_first_existing_or_created(self, path_candidates: Sequence[str], value: float, *,
                                         row_name: Optional[str] = None) -> Optional[str]:
        for path in path_candidates:
            n = self.node(path)
            if n is None and row_name:
                parent_path = path.rsplit("\\", 1)[0]
                n = self.ensure_table_row(parent_path, row_name)
            if n is None:
                continue
            try:
                n.Value = float(value)
                self.log(f"[WRITE] {path} = {float(value):.8g}")
                return path
            except Exception as exc:
                self.log(f"[WARN] write candidate failed: {path}={value}; {exc}")
        return None

    def _write_stream_total_flow(self, sid: str, total_flow: float) -> Optional[str]:
        candidates = [
            rf"\Data\Streams\{sid}\Input\TOTFLOW\\MIXED",
            rf"\Data\Streams\{sid}\Input\TOTFLOW",
            rf"\Data\Streams\{sid}\Input\TOTAL_FLOW\\MIXED",
            rf"\Data\Streams\{sid}\Input\TOTAL_FLOW",
        ]
        return self._write_first_existing_or_created(candidates, total_flow)

    def _write_component_amount(self, sid: str, comp: str, value: float, *, kind: str = "FLOW") -> Optional[str]:
        comp = _canonical_component_name(comp)
        aliases = self._component_aliases_for_write(comp)
        paths: List[str] = []
        for cid in aliases:
            if kind.upper() == "FLOW":
                paths.extend([
                    rf"\Data\Streams\{sid}\Input\FLOW\\MIXED\{cid}",
                    rf"\Data\Streams\{sid}\Input\MOLEFLOW\\MIXED\{cid}",
                    rf"\Data\Streams\{sid}\Input\MOLE-FLOW\\MIXED\{cid}",
                ])
            else:
                paths.extend([
                    rf"\Data\Streams\{sid}\Input\FRAC\MIXED\{cid}",
                    rf"\Data\Streams\{sid}\Input\MOLEFRAC\MIXED\{cid}",
                    rf"\Data\Streams\{sid}\Input\MOLE-FRAC\MIXED\{cid}",
                ])
        for cid in aliases:
            if kind.upper() == "FLOW":
                for parent in (
                        rf"\Data\Streams\{sid}\Input\FLOW\\MIXED",
                        rf"\Data\Streams\{sid}\Input\MOLEFLOW\\MIXED",
                        rf"\Data\Streams\{sid}\Input\MOLE-FLOW\\MIXED",
                ):
                    self.ensure_table_row(parent, cid)
            else:
                for parent in (
                        rf"\Data\Streams\{sid}\Input\FRAC\MIXED",
                        rf"\Data\Streams\{sid}\Input\MOLEFRAC\MIXED",
                        rf"\Data\Streams\{sid}\Input\MOLE-FRAC\MIXED",
                ):
                    self.ensure_table_row(parent, cid)
        return self._write_first_existing_or_created(paths, value, row_name=comp)

    def set_feed_stream(
            self,
            stream_id: str,
            flow_dict: Dict[str, float],
            *,
            temperature_C: Optional[float] = None,
            pressure_kpa: Optional[float] = None,
    ) -> None:
        sid = _clean_id(stream_id, 8)
        temp_c = self.default_temperature_C if temperature_C is None else float(temperature_C)
        pres_kpa = 101.325 if pressure_kpa is None else float(pressure_kpa)
        pressure_value = self._pressure_value_for_aspen(pres_kpa)

        total_flow = sum(float(v or 0.0) for v in flow_dict.values())
        if total_flow <= 0:
            raise RuntimeError(f"Feed stream {sid} has non-positive total flow: {flow_dict}")

        self.bo_force_populate_stream(sid, temp_c, pressure_value, flow_dict)

    def set_flash2_spec_mode(self, block_id: str, mode: str = "PV") -> None:
        bid = _clean_id(block_id, 8)
        mode = str(mode or "PV").strip().upper()
        path = rf"\Data\Blocks\{bid}\Input\SPEC_OPT"
        node = self.node(path)
        if node is None:
            children = self.elements_names(rf"\Data\Blocks\{bid}\Input")
            raise RuntimeError(f"Flash2 SPEC_OPT node not found for {bid}. Input children={children}")
        before = None
        try:
            before = node.Value
        except Exception:
            pass
        node.Value = mode
        try:
            after = node.Value
        except Exception:
            after = mode
        self.log(f"[FLASH-SPEC-MODE] {bid}: SPEC_OPT {before!r} -> {after!r}")

    def set_flash_block(
            self,
            block_id: str,
            *,
            pressure_kpa: float,
            vapor_fraction: float = 0.5,
    ) -> None:
        bid = _clean_id(block_id, 8)
        pressure_value = self._pressure_value_for_aspen(pressure_kpa)
        frac = max(1e-6, min(0.999999, float(vapor_fraction)))

        self.set_flash2_spec_mode(bid, "PV")

        for inactive in (
                rf"\Data\Blocks\{bid}\Input\TEMP",
                rf"\Data\Blocks\{bid}\Input\TEMP\MIXED",
                rf"\Data\Blocks\{bid}\Input\DUTY",
                rf"\Data\Blocks\{bid}\Input\DUTY\MIXED",
        ):
            n = self.node(inactive)
            if n is not None:
                try:
                    n.Value = None
                except Exception:
                    pass

        self.set_first_existing([
            (rf"\Data\Blocks\{bid}\Input\PRES", pressure_value),
            (rf"\Data\Blocks\{bid}\Input\PRES\MIXED", pressure_value),
            (rf"\Data\Blocks\{bid}\Input\FLASH_PRES", pressure_value),
        ], required=True)

        candidates = [
            rf"\Data\Blocks\{bid}\Input\VFRAC",
            rf"\Data\Blocks\{bid}\Input\VFRAC\MIXED",
            rf"\Data\Blocks\{bid}\Input\FRAC",
            rf"\Data\Blocks\{bid}\Input\FRAC\MIXED",
            rf"\Data\Blocks\{bid}\Input\VAPFRAC",
            rf"\Data\Blocks\{bid}\Input\VAPFRAC\MIXED",
            rf"\Data\Blocks\{bid}\Input\VF",
        ]
        for path in candidates:
            n = self.node(path)
            if n is None:
                continue
            try:
                n.Value = frac
                self.log(f"[FLASH-SPEC] {bid}: SPEC_OPT=PV; PRES={pressure_value:g}; VFRAC={frac:g} via {path}")
                return
            except Exception as exc:
                self.log(f"[FLASH-SPEC-WARN] vapor fraction write failed: {path}={frac}; {exc}")

        children = self.elements_names(rf"\Data\Blocks\{bid}\Input")
        raise RuntimeError(f"Flash2 vapor-fraction specification is not writable for {bid}. Input children={children}")


    def run_aspen(self, label: str = "Flash2 pressure screen") -> None:
        self.log(f"[RUN] {label}")
        self.aspen.Engine.Run2(1)
        self._aspen_run_count = int(getattr(self, "_aspen_run_count", 0)) + 1
        t0 = time.time()
        while True:
            try:
                running = bool(self.aspen.Engine.IsRunning)
            except Exception:
                running = False
            if not running:
                break
            if time.time() - t0 > self.run_timeout_s:
                try:
                    self.aspen.Engine.Stop()
                except Exception:
                    pass
                raise TimeoutError(f"Aspen run timeout after {self.run_timeout_s}s: {label}")
            time.sleep(0.5)
        self._last_run_status = self.read_aspen_run_status(self.flash_block_id)
        if self._last_run_status.get("explicit_failure"):
            self.log(f"[ASPEN-STATUS-FAIL] {self._last_run_status}")

    def _recursive_find_component_value(self, node: Any, aliases: Sequence[str], max_depth: int = 4) -> Optional[float]:
        if node is None or max_depth < 0:
            return None
        alias_set = {_canonical_component_name(a) for a in aliases if a}

        try:
            nm = str(getattr(node, "Name", "") or "")
            if _canonical_component_name(nm) in alias_set:
                val = _safe_float(getattr(node, "Value", None), None)
                if val is not None:
                    return val
        except Exception:
            pass

        try:
            elems = getattr(node, "Elements", None)
            if elems is None:
                return None
            for item in elems:
                val = self._recursive_find_component_value(item, aliases, max_depth=max_depth - 1)
                if val is not None:
                    return val
        except Exception:
            return None
        return None

    def _read_component_table(self, stream_id: str, table_names: Sequence[str], components: Sequence[str]) -> Dict[
        str, Optional[float]]:
        sid = _clean_id(stream_id, 8)
        out: Dict[str, Optional[float]] = {}
        for comp in components:
            comp_canon = _canonical_component_name(comp)
            val = None
            aliases = list(
                dict.fromkeys([comp, comp_canon, _clean_id(comp)] + self._component_aliases_for_write(comp_canon)))
            for table in table_names:
                table = str(table or "").strip()
                if not table:
                    continue
                for cid in aliases:
                    for path in (
                            rf"\Data\Streams\{sid}\Output\{table}\MIXED\{cid}",
                            rf"\Data\Streams\{sid}\Output\{table}\{cid}",
                            rf"\Data\Streams\{sid}\Output\{table}\MIXED\{_clean_id(cid)}",
                            rf"\Data\Streams\{sid}\Output\{table}\{_clean_id(cid)}",
                    ):
                        raw = self.get_value(path)
                        if raw is not None:
                            val = _safe_float(raw, None)
                            if val is not None:
                                break
                    if val is not None:
                        break
                if val is not None:
                    break

                table_node = self.node(rf"\Data\Streams\{sid}\Output\{table}")
                val = self._recursive_find_component_value(table_node, aliases, max_depth=5)
                if val is not None:
                    self.log(f"[READ-STREAM-TABLE] {sid}.Output.{table}.{comp_canon} = {val}")
                    break
            out[comp_canon] = val
        return out

    def _phase_table_order(self, stream_id: str, phase_hint: Optional[str] = None) -> List[str]:
        sid = _clean_id(stream_id, 8)
        phase = (phase_hint or "").strip().lower()
        if phase.startswith("vap") or "VAP" in sid or sid.endswith("V"):
            return ["Y", "MOLEFRAC", "MOLEFRAC2", "MOLE-FRAC", "MOLEFRAC_OUT", "MOLFRAC", "FRAC"]
        if phase.startswith("liq") or "LIQ" in sid or sid.endswith("L"):
            return ["X", "X1", "X2", "MOLEFRAC", "MOLEFRAC2", "MOLE-FRAC", "MOLEFRAC_OUT", "MOLFRAC", "FRAC"]
        return ["MOLEFRAC", "MOLEFRAC2", "MOLE-FRAC", "MOLEFRAC_OUT", "MOLFRAC", "FRAC", "X", "Y"]

    def read_stream_molefrac(self, stream_id: str, components: Sequence[str], phase_hint: Optional[str] = None) -> Dict[
        str, float]:
        sid = _clean_id(stream_id, 8)
        comps = [_canonical_component_name(c) for c in components]
        frac = self._read_component_table(sid, self._phase_table_order(sid, phase_hint), comps)
        if any(v is not None and abs(float(v)) > 1e-14 for v in frac.values()):
            out = {c: float(frac.get(c) or 0.0) for c in comps}
            s = sum(out.values())
            self.log(f"[READ-STREAM-FRAC] {sid} phase={phase_hint or 'auto'} frac={out} sum={s:g}")
            return out

        flows = self._read_component_table(sid, ["MOLEFLOW", "MOLE-FLOW", "MOLEFLMX", "FLOW", "MASSFLOW", "MASS-FLOW"],
                                           comps)
        total = sum(float(v or 0.0) for v in flows.values())
        if total > 1e-14:
            norm = {c: float(flows.get(c) or 0.0) / total for c in comps}
            self.log(f"[READ-FRAC-FROM-STREAM-FLOW] {sid}: {norm}")
            return norm

        output_children = self.elements_names(rf"\Data\Streams\{sid}\Output")
        detail_children = {}
        for table in ("X", "Y", "MOLEFRAC", "MOLEFLOW"):
            detail_children[table] = self.elements_names(rf"\Data\Streams\{sid}\Output\{table}")
        self.log(
            f"[READ-WARN] no stream composition read for {sid}; output children={output_children}; table children={detail_children}")
        return {c: 0.0 for c in comps}


    def read_stream_temperature(self, stream_id: str) -> dict:
        sid = _clean_id(stream_id, 8)
        for path in (
                rf"\Data\Streams\{sid}\Output\TEMP_OUT\MIXED",
                rf"\Data\Streams\{sid}\Output\TEMP\MIXED",
                rf"\Data\Streams\{sid}\Output\TEMP_OUT",
                rf"\Data\Streams\{sid}\Output\TEMP",
        ):
            node = self.node(path)
            if node is None:
                continue
            try:
                raw = node.Value
            except Exception:
                raw = None
            if raw is None:
                continue
            unit = self.temperature_output_unit
            for attr in ("UnitString", "Units", "Unit"):
                try:
                    candidate = str(getattr(node, attr, "") or "").strip().upper()
                except Exception:
                    candidate = ""
                if "K" == candidate or "KELVIN" in candidate:
                    unit = "K"
                    break
                if "C" in candidate:
                    unit = "C"
                    break
            pack = _temperature_pack(raw, unit_hint=unit)
            pack["source_path"] = path
            return pack
        return {"raw": None, "C": None, "K": None, "unit": "unknown", "unit_source": "unavailable"}

    def read_aspen_run_status(self, block_id: str) -> dict:
        bid = _clean_id(block_id, 8)
        candidates = (
            rf"\Data\Blocks\{bid}\Output\BLKSTAT",
            rf"\Data\Blocks\{bid}\Output\STATUS",
            rf"\Data\Blocks\{bid}\Output\CONVSTAT",
            r"\Data\Results Summary\Run-Status\Output\RUN_STATUS",
            r"\Data\Results Summary\Run-Status\Output\STATUS",
            r"\Data\Results Summary\Run Status\Output\STATUS",
        )
        values = []
        for path in candidates:
            raw = self.get_value(path)
            if raw is not None and str(raw).strip() != "":
                values.append({"path": path, "value": raw})
        joined = " | ".join(str(v.get("value") or "").upper() for v in values)
        failure_tokens = ("ERROR", "FAIL", "NOT CONVERGED", "INFEAS", "TERMINAT", "FATAL")
        explicit_failure = any(tok in joined for tok in failure_tokens)
        return {
            "available": bool(values),
            "explicit_failure": explicit_failure,
            "values": values,
        }

    def validate_flash_point_integrity(
            self,
            *,
            components: Sequence[str],
            input_flows: Dict[str, float],
            liquid_flow: float,
            vapour_flow: float,
            liquid_mole_fraction: Dict[str, float],
            vapour_mole_fraction: Dict[str, float],
            aspen_status: Optional[dict] = None,
    ) -> dict:
        comps = [_canonical_component_name(c) for c in components]
        feed = {_canonical_component_name(k): float(v or 0.0) for k, v in input_flows.items()}
        liq = {_canonical_component_name(k): float(v or 0.0) for k, v in liquid_mole_fraction.items()}
        vap = {_canonical_component_name(k): float(v or 0.0) for k, v in vapour_mole_fraction.items()}
        feed_total = sum(feed.values())
        outlet_total = float(liquid_flow or 0.0) + float(vapour_flow or 0.0)
        liquid_sum = sum(liq.get(c, 0.0) for c in comps)
        vapour_sum = sum(vap.get(c, 0.0) for c in comps)
        flow_balance_error = (
            abs(outlet_total - feed_total) / max(abs(feed_total), 1e-12)
            if feed_total > 0 else None
        )
        component_balance_errors: Dict[str, Optional[float]] = {}
        missing_fed_components: List[str] = []
        for comp in comps:
            fin = float(feed.get(comp, 0.0))
            fout = float(liquid_flow or 0.0) * liq.get(comp, 0.0) + float(vapour_flow or 0.0) * vap.get(comp, 0.0)
            if fin > 1e-10:
                err = abs(fout - fin) / max(abs(fin), 1e-12)
                component_balance_errors[comp] = err
                if liq.get(comp, 0.0) <= 1e-12 and vap.get(comp, 0.0) <= 1e-12:
                    missing_fed_components.append(comp)
            else:
                component_balance_errors[comp] = None

        issues: List[str] = []
        if liquid_flow <= 1e-12 or vapour_flow <= 1e-12:
            issues.append("zero_or_single_phase_outlet")
        if abs(liquid_sum - 1.0) > self.composition_sum_tolerance:
            issues.append("liquid_composition_sum_invalid")
        if abs(vapour_sum - 1.0) > self.composition_sum_tolerance:
            issues.append("vapour_composition_sum_invalid")
        if flow_balance_error is not None and flow_balance_error > self.overall_flow_balance_tolerance:
            issues.append("overall_flow_balance_failed")
        if any(
            err is not None and err > self.component_balance_tolerance
            for err in component_balance_errors.values()
        ):
            issues.append("component_balance_failed")
        if missing_fed_components:
            issues.append("fed_component_missing_from_both_outlets")
        status = _as_dict(aspen_status)
        if status.get("explicit_failure"):
            issues.append("aspen_reported_failure")

        return {
            "valid": not issues,
            "issues": issues,
            "liquid_composition_sum": liquid_sum,
            "vapour_composition_sum": vapour_sum,
            "feed_total_flow": feed_total,
            "outlet_total_flow": outlet_total,
            "overall_flow_balance_relative_error": flow_balance_error,
            "component_balance_relative_errors": component_balance_errors,
            "missing_fed_components": missing_fed_components,
            "tolerances": {
                "composition_sum": self.composition_sum_tolerance,
                "overall_flow_balance": self.overall_flow_balance_tolerance,
                "component_balance": self.component_balance_tolerance,
            },
            "aspen_status": status,
        }

    def read_stream_total_flow(self, stream_id: str) -> float:
        sid = _clean_id(stream_id, 8)
        paths = [
            rf"\Data\Streams\{sid}\Output\TOT_FLOW\\MIXED",
            rf"\Data\Streams\{sid}\Output\TOT_FLOW",
            rf"\Data\Streams\{sid}\Output\MOLEFLOW\\MIXED",
            rf"\Data\Streams\{sid}\Output\MOLEFLOW",
            rf"\Data\Streams\{sid}\Output\VAP_FLOW\\MIXED",
            rf"\Data\Streams\{sid}\Output\VAP_FLOW",
            rf"\Data\Streams\{sid}\Output\LIQ_FLOW\\MIXED",
            rf"\Data\Streams\{sid}\Output\LIQ_FLOW",
        ]
        for path in paths:
            val = _safe_float(self.get_value(path), None)
            if val is not None and abs(val) > 1e-14:
                return float(val)
        total = 0.0
        for table in ("MOLEFLOW", "MOLEFLMX", "MASSFLOW", "MASSFLMX"):
            node = self.node(rf"\Data\Streams\{sid}\Output\{table}")
            if node is None or not getattr(node, "Elements", None):
                continue
            try:
                for item in node.Elements:
                    v = _safe_float(getattr(item, "Value", None), None)
                    if v is not None and v > 0:
                        total += float(v)
            except Exception:
                pass
            if total > 1e-14:
                return total
        return 0.0

    def read_block_vfrac_out(self, block_id: str, vap_stream: str = "PFVAP") -> Optional[float]:
        bid = _clean_id(block_id, 8)
        sid = _clean_id(vap_stream, 8)
        for path in (
                rf"\Data\Blocks\{bid}\Output\VFRAC",
                rf"\Data\Blocks\{bid}\Output\B_VFRAC",
                rf"\Data\Streams\{sid}\Output\VFRAC_OUT",
                rf"\Data\Streams\{sid}\Output\B_VFRAC",
                rf"\Data\Streams\{sid}\Output\RES_VFRAC",
        ):
            val = _safe_float(self.get_value(path), None)
            if val is not None:
                return float(val)
        return None

    def set_flash_block_temperature(self, block_id: str, *, pressure_kpa: float, temperature_raw: float) -> None:
        bid = _clean_id(block_id, 8)
        pressure_value = self._pressure_value_for_aspen(pressure_kpa)
        self.set_value(rf"\Data\Blocks\{bid}\Input\PRES", pressure_value)
        for path in (
                rf"\Data\Blocks\{bid}\Input\TEMP",
                rf"\Data\Blocks\{bid}\Input\TEMP\MIXED",
        ):
            n = self.node(path)
            if n is None:
                continue
            try:
                n.Value = float(temperature_raw)
                self.log(f"[FLASH-SPEC] {bid}: P + TEMP = {pressure_value:g}, {float(temperature_raw):g} via {path}")
                return
            except Exception:
                pass
        input_node = self.node(rf"\Data\Blocks\{bid}\Input")
        node = self.bo_get_or_create(input_node, "TEMP")
        if node is not None:
            try:
                node.Value = float(temperature_raw)
                self.log(
                    f"[FLASH-SPEC] {bid}: P + TEMP = {pressure_value:g}, {float(temperature_raw):g} via created TEMP")
                return
            except Exception:
                pass
        raise RuntimeError(f"Flash2 temperature specification is not writable for {bid}")

    # ------------------------------
    # Core scan
    # ------------------------------

    def build_flow_dict_for_point(self, case: dict, x_first: float, total_non_solvent_flow: Optional[float] = None) -> \
    Dict[str, float]:
        pair = [_canonical_component_name(c) for c in _as_list(case.get("pair"))[:2]]
        if len(pair) != 2:
            raise ValueError(f"Flash case must contain a binary pair: {case}")
        total = float(total_non_solvent_flow or self.total_non_solvent_flow)
        x = max(0.0, min(1.0, float(x_first)))

        # v45: preserve the actual multicomponent background around the selected
        # risk edge.  ``x`` still scans the normalized pair composition, while
        # other non-solvent components remain at a fixed ratio derived from the
        # ED_best feed to the corresponding column.
        bg_ratios = {}
        for comp, val in _as_dict(case.get("background_component_ratios")).items():
            c = _canonical_component_name(comp)
            v = _safe_float(val, None)
            if c and c not in pair and v is not None and v > 0:
                bg_ratios[c] = float(v)
        bg_sum = sum(bg_ratios.values())
        if bg_sum > 0:
            bg_ratios = {k: v / bg_sum for k, v in bg_ratios.items()}
        bg_to_pair = max(0.0, min(3.0, float(_safe_float(case.get("background_to_pair_ratio"), 0.0) or 0.0)))
        pair_total = total / (1.0 + bg_to_pair) if bg_to_pair > 0 and bg_ratios else total
        bg_total = max(0.0, total - pair_total)

        flows = {
            pair[0]: x * pair_total,
            pair[1]: (1.0 - x) * pair_total,
        }
        for comp, ratio in bg_ratios.items():
            flows[comp] = bg_total * float(ratio)

        solvent = _canonical_component_name(case.get("solvent"))
        if case.get("with_solvent") and solvent:
            sfr = _safe_float(case.get("solvent_to_feed_ratio"), 1.0) or 1.0
            flows[solvent] = float(sfr) * total
        return flows

    def pair_normalized_first_fraction(self, comp_dict: Dict[str, float], pair: Sequence[str]) -> Optional[float]:
        c1 = _canonical_component_name(pair[0])
        c2 = _canonical_component_name(pair[1])
        v1 = _safe_float(comp_dict.get(c1), 0.0) or 0.0
        v2 = _safe_float(comp_dict.get(c2), 0.0) or 0.0
        den = v1 + v2
        if den <= 1e-15:
            return None
        return float(v1 / den)


    def _record_point_from_outlets(
            self,
            *,
            points: List[dict],
            pair: Sequence[str],
            components_to_read: Sequence[str],
            liq: str,
            vap: str,
            x0: float,
            spec_label: str,
            spec_value: Any,
            flows: Dict[str, float],
            attempts: List[dict],
    ) -> bool:
        liq_flow = self.read_stream_total_flow(liq)
        vap_flow = self.read_stream_total_flow(vap)
        liq_comp = self.read_stream_molefrac(liq, components_to_read, phase_hint="liquid")
        vap_comp = self.read_stream_molefrac(vap, components_to_read, phase_hint="vapor")
        x_pair = self.pair_normalized_first_fraction(liq_comp, pair)
        y_pair = self.pair_normalized_first_fraction(vap_comp, pair)
        temp = self.read_stream_temperature(liq)
        aspen_status = self.read_aspen_run_status(self.flash_block_id)
        integrity = self.validate_flash_point_integrity(
            components=components_to_read,
            input_flows=flows,
            liquid_flow=liq_flow,
            vapour_flow=vap_flow,
            liquid_mole_fraction=liq_comp,
            vapour_mole_fraction=vap_comp,
            aspen_status=aspen_status,
        )
        attempt = {
            spec_label: spec_value,
            "x": x_pair,
            "y": y_pair,
            "temperature": temp,
            "liquid_flow": liq_flow,
            "vapour_flow": vap_flow,
            "vfrac_out": (vap_flow / (vap_flow + liq_flow)) if (vap_flow + liq_flow) > 1e-14 else None,
            "liquid_mole_fraction": liq_comp,
            "vapour_mole_fraction": vap_comp,
            "data_integrity": integrity,
        }
        attempts.append(attempt)
        if not integrity.get("valid"):
            self.log(
                f"[POINT-QUALITY-FAIL] {spec_label}={spec_value}; "
                f"issues={integrity.get('issues')}; pair={list(pair)}"
            )
            return False
        if x_pair is None or y_pair is None:
            self.log(f"[NO-XY] {spec_label}={spec_value}; x={x_pair}, y={y_pair}")
            return False
        alpha = _relative_volatility_binary(x_pair, y_pair)
        points.append({
            "feed_x_first_component": x0,
            "spec_type": spec_label,
            "spec_value": spec_value,
            "x": x_pair,
            "y": y_pair,
            "alpha": alpha,
            "y_minus_x": float(y_pair - x_pair),
            "liquid_mole_fraction": liq_comp,
            "vapour_mole_fraction": vap_comp,
            "temperature": temp,
            "liquid_flow": liq_flow,
            "vapour_flow": vap_flow,
            "vfrac_out": attempt.get("vfrac_out"),
            "input_flows": flows,
            "data_integrity": integrity,
            "spec_attempts": attempts,
        })
        return True

    def run_flash_case(self, case: dict, *, feed_condition: Optional[dict] = None) -> dict:
        bid, feed, vap, liq = self.ensure_flash_unit()
        pair = [_canonical_component_name(c) for c in _as_list(case.get("pair"))[:2]]
        if len(pair) != 2:
            raise ValueError(f"Invalid Flash case pair: {case.get('pair')}")
        pressure_kpa = _safe_float(case.get("pressure_kpa"), None)
        if pressure_kpa is None:
            patm = _safe_float(case.get("pressure_atm"), 1.0) or 1.0
            pressure_kpa = patm * 101.325
        x_grid = _as_list(case.get("composition_grid_x_first_component") or DEFAULT_X_GRID)
        fcond = _as_dict(feed_condition)
        base_temp_c = _safe_float(fcond.get("temperature_C"), self.default_temperature_C) or self.default_temperature_C
        feed_pres_kpa = _safe_float(fcond.get("pressure_kpa"), 101.325) or 101.325
        frac_grid = _as_list(case.get("vapor_fraction_grid") or case.get("fraction_grid") or case.get(
            "vapor_fraction") or DEFAULT_VAPOR_FRACTION_GRID)
        frac_grid = [max(1e-6, min(0.999999, float(_safe_float(v, 0.5) or 0.5))) for v in frac_grid]
        components_to_read = list(pair)
        for comp in _as_dict(case.get("background_component_ratios")).keys():
            c = _canonical_component_name(comp)
            if c and c not in components_to_read:
                components_to_read.append(c)
        solvent = _canonical_component_name(case.get("solvent"))
        if case.get("with_solvent") and solvent and solvent not in components_to_read:
            components_to_read.append(solvent)
        points: List[dict] = []
        failures: List[dict] = []
        for x_raw in x_grid:
            x0 = _safe_float(x_raw, None)
            if x0 is None:
                continue
            try:
                flows = self.build_flow_dict_for_point(case, x0)
                point_added = False
                attempts: List[dict] = []
                self.set_feed_stream(feed, flows, temperature_C=base_temp_c, pressure_kpa=feed_pres_kpa)
                for frac_try in frac_grid:
                    self.set_flash_block(bid, pressure_kpa=pressure_kpa, vapor_fraction=frac_try)
                    self.run_aspen(
                        f"{case.get('case_type', 'flash')} pair={pair} feedT={base_temp_c:g}C feedP={feed_pres_kpa:g}kPa blockP={pressure_kpa:g}kPa z={x0:g} frac={frac_try:g}")
                    if self._record_point_from_outlets(
                            points=points, pair=pair, components_to_read=components_to_read,
                            liq=liq, vap=vap, x0=x0, spec_label="block_fraction_vapour_default",
                            spec_value=frac_try, flows=flows, attempts=attempts,
                    ):
                        point_added = True
                        break
                if not point_added:
                    # Some systems/components fail with Flash2 P+VFRAC even when a
                    # valid two-phase point exists.  Retry a conservative P+T scan
                    # before declaring the point invalid.
                    if "temperature_fallback_grid_C" in case:
                        fallback_grid = _as_list(case.get("temperature_fallback_grid_C"))
                    elif self.enable_temperature_fallback:
                        fallback_grid = list(self.temperature_fallback_grid_C)
                    else:
                        fallback_grid = []
                    for temp_try in fallback_grid:
                        tval = _safe_float(temp_try, None)
                        if tval is None:
                            continue
                        try:
                            self.set_flash_block_temperature(bid, pressure_kpa=pressure_kpa, temperature_raw=tval)
                            self.run_aspen(
                                f"{case.get('case_type', 'flash')} pair={pair} feedT={base_temp_c:g}C feedP={feed_pres_kpa:g}kPa blockP={pressure_kpa:g}kPa z={x0:g} temp={tval:g}C fallback")
                            if self._record_point_from_outlets(
                                    points=points, pair=pair, components_to_read=components_to_read,
                                    liq=liq, vap=vap, x0=x0, spec_label="temperature_C_fallback",
                                    spec_value=tval, flows=flows, attempts=attempts,
                            ):
                                point_added = True
                                self.log(
                                    f"[CASE-POINT-FALLBACK-OK] pair={pair} blockP={pressure_kpa:g} z={x0:g}; T={tval:g}C")
                                break
                        except Exception as fallback_exc:
                            attempts.append({"temperature_C_fallback": tval, "error": repr(fallback_exc)})
                    if not point_added:
                        failures.append({"x": x0, "input_flows": flows, "attempts": attempts,
                                         "error": "no valid y-x point across block pressure/fraction/temperature scan"})
                        self.log(
                            f"[CASE-POINT-NO-VLE] pair={pair} blockP={pressure_kpa:g} feed z={x0:g}; no valid y-x across fraction/temperature grid")
            except Exception as exc:
                failures.append({"x": x_raw, "error": repr(exc)})
                self.log(f"[CASE-POINT-FAIL] pair={pair} blockP={pressure_kpa:g} z={x_raw}; {exc}")
        expected_point_count = len([x for x in x_grid if _safe_float(x, None) is not None])
        if len(points) == expected_point_count and not failures:
            case_status = "completed"
        elif points:
            case_status = "partial_invalid_points"
        else:
            case_status = "failed_no_valid_points"
        issue_counts: Dict[str, int] = {}
        for failure in failures:
            for attempt in _as_list(_as_dict(failure).get("attempts")):
                for issue in _as_list(_as_dict(_as_dict(attempt).get("data_integrity")).get("issues")):
                    issue_counts[str(issue)] = issue_counts.get(str(issue), 0) + 1
        return {
            "case_type": case.get("case_type") or (
                "pseudo_ternary_with_solvent_pressure_vle" if case.get("with_solvent") else "binary_pressure_vle"),
            "pair": pair,
            "pressure_kpa": pressure_kpa,
            "pressure_atm": round(_pressure_atm(pressure_kpa), 6),
            "with_solvent": bool(case.get("with_solvent") or case.get("solvent")),
            "solvent": solvent if case.get("with_solvent") else None,
            "solvent_to_feed_ratio": case.get("solvent_to_feed_ratio"),
            "source_column_id": case.get("source_column_id"),
            "risk_role": case.get("risk_role"),
            "top_component": case.get("top_component") or _as_dict(case.get("risk_edge_context")).get("top_component"),
            "bottom_component": case.get("bottom_component") or _as_dict(case.get("risk_edge_context")).get("bottom_component"),
            "desired_direction": case.get("desired_direction") or _as_dict(case.get("risk_edge_context")).get("desired_direction"),
            "reference_base_pressure_kpa": case.get("reference_base_pressure_kpa") or _as_dict(case.get("risk_edge_context")).get("reference_base_pressure_kpa"),
            "reference_pair_fraction_first_component": case.get("reference_pair_fraction_first_component") or _as_dict(case.get("risk_edge_context")).get("reference_pair_fraction_first_component"),
            "reference_solvent_to_feed_ratio": case.get("reference_solvent_to_feed_ratio") or _as_dict(case.get("risk_edge_context")).get("reference_solvent_to_feed_ratio"),
            "max_valid_flash_temperature_C": case.get("max_valid_flash_temperature_C", DEFAULT_MAX_VALID_FLASH_TEMPERATURE_C),
            "min_valid_flash_temperature_C": case.get("min_valid_flash_temperature_C", DEFAULT_MIN_VALID_FLASH_TEMPERATURE_C),
            "risk_edge_context": copy.deepcopy(_as_dict(case.get("risk_edge_context"))),
            "background_component_ratios": copy.deepcopy(_as_dict(case.get("background_component_ratios"))),
            "background_to_pair_ratio": _safe_float(case.get("background_to_pair_ratio"), 0.0) or 0.0,
            "status": case_status,
            "points": points,
            "failed_points": failures,
            "expected_point_count": expected_point_count,
            "converged_points": len(points),
            "valid_point_count": len(points),
            "failed_point_count": len(failures),
            "data_quality": {
                "valid": case_status == "completed",
                "usable_for_curve_analysis": len(points) >= 2,
                "issue_counts": issue_counts,
            },
            "scan_method": "fixed_feed_TP_vary_composition_block_pressure_fraction_read_outlet_streams",
        }


    def run_payload(self, payload: dict) -> dict:
        payload = _as_dict(payload)
        if payload.get("payloads") and isinstance(payload.get("payloads"), dict):
            cases = []
            for label, sub in payload["payloads"].items():
                for c in _as_list(_as_dict(sub).get("flash_cases")):
                    cc = copy.deepcopy(c)
                    cc["route_payload_label"] = label
                    cases.append(cc)
            feed_condition = feed_condition_from_payload_or_state(payload, None)
        else:
            cases = _as_list(payload.get("flash_cases"))
            feed_condition = feed_condition_from_payload_or_state(payload, None)
        if not cases:
            raise ValueError("Payload contains no flash_cases.")

        comps_to_register: List[str] = []
        for c in cases:
            for comp in _as_list(_as_dict(c).get("pair"))[:2]:
                cc = _canonical_component_name(comp)
                if cc and cc not in comps_to_register:
                    comps_to_register.append(cc)
            solv = _canonical_component_name(_as_dict(c).get("solvent"))
            if _as_dict(c).get("with_solvent") and solv and solv not in comps_to_register:
                comps_to_register.append(solv)
        thermo_method = payload.get("thermo_method") or payload.get("property_method") or "NRTL"
        registry = self.ensure_components_registered(comps_to_register, thermo_method=thermo_method)

        self.log(f"[START] {RUNNER_VERSION}; flash_cases={len(cases)}; components={comps_to_register}")
        result_cases: List[dict] = []
        failed_cases: List[dict] = []
        for idx, case in enumerate(cases, 1):
            try:
                self.log(f"[CASE {idx}/{len(cases)}] {case.get('case_type')} pair={case.get('pair')} P={case.get('pressure_kpa')}")
                out = self.run_flash_case(case, feed_condition=feed_condition)
                result_cases.append(out)
            except Exception as exc:
                failed_cases.append({"case_index": idx, "case": case, "error": str(exc)})
                self.log(f"[CASE-FAIL] index={idx}; {exc}")

        results = {
            "runner_version": RUNNER_VERSION,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "case_count": len(result_cases),
            "cases": result_cases,
            "failed_cases": failed_cases,
            "log": list(self.log_lines),
            "scan_profile": payload.get("scan_profile"),
            "recommended_pressure_write_mode": payload.get("recommended_pressure_write_mode"),
            "workload_estimate": estimate_pressure_flash_workload(payload),
            "actual_aspen_run_count": int(getattr(self, "_aspen_run_count", 0)),
            "component_registry": registry,
        }
        return finalize_pressure_screen_results(results)

# ---------------------------------------------------------------------------
# Curve analysis helpers
# ---------------------------------------------------------------------------

def _sorted_valid_points(case: dict) -> List[dict]:
    """Return ordered x-y points for binary or pseudo-ternary Flash cases.

    Earlier versions only accepted explicit point['x']/point['y'].  Some
    Aspen Flash2 branches, especially solvent/pseudo-ternary scans, store full
    liquid/vapour composition dictionaries instead.  Ignoring those points can
    erase an actual y=x crossing from the screen plot and from the pressure
    sensitivity logic.  This helper now resolves x/y from the case pair first,
    then recomputes y-x and alpha consistently.
    """
    cd = _as_dict(case)
    pair = [_canonical_component_name(x) for x in _as_list(cd.get("pair"))[:2]]
    pts = []
    for p in _as_list(cd.get("points")):
        pd = _as_dict(p)
        x = _safe_float(pd.get("x"), None)
        y = _safe_float(pd.get("y"), None)
        if (x is None or y is None) and len(pair) == 2:
            x = _plot_pair_fraction(
                pd.get("liquid_ternary_x")
                or pd.get("liquid_mole_fraction")
                or pd.get("x_liquid")
                or {},
                pair,
            )
            y = _plot_pair_fraction(
                pd.get("vapour_ternary_y")
                or pd.get("vapor_ternary_y")
                or pd.get("vapour_mole_fraction")
                or pd.get("vapor_mole_fraction")
                or pd.get("y_vapor")
                or {},
                pair,
            )
        if x is None or y is None:
            continue
        x = float(x); y = float(y)
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            continue
        q = copy.deepcopy(pd)
        q["x"] = x
        q["y"] = y
        q["alpha"] = _safe_float(pd.get("alpha"), None)
        if q["alpha"] is None:
            q["alpha"] = _relative_volatility_binary(x, y)
        q["y_minus_x"] = _safe_float(pd.get("y_minus_x"), y - x)
        pts.append(q)
    pts.sort(key=lambda d: d["x"])
    return pts


def _mean(values: Sequence[Optional[float]]) -> Optional[float]:
    vv = [float(v) for v in values if v is not None]
    if not vv:
        return None
    return sum(vv) / float(len(vv))


def _minmax(values: Sequence[Optional[float]]) -> Tuple[Optional[float], Optional[float]]:
    vv = [float(v) for v in values if v is not None]
    if not vv:
        return None, None
    return min(vv), max(vv)


def _interp_crossing_x(points: Sequence[dict]) -> Optional[float]:
    if len(points) < 2:
        return None
    prev = None
    for p in points:
        d = _safe_float(_as_dict(p).get("y_minus_x"), None)
        x = _safe_float(_as_dict(p).get("x"), None)
        if d is None or x is None:
            continue
        if abs(d) <= 1e-9:
            return float(x)
        if prev is not None:
            x0, d0 = prev
            if d0 == 0:
                return float(x0)
            if (d0 < 0 < d) or (d0 > 0 > d):
                if abs(d - d0) <= 1e-12:
                    return float((x0 + x) / 2.0)
                frac = (0.0 - d0) / (d - d0)
                return float(x0 + frac * (x - x0))
        prev = (x, d)
    return None


def _risk_edge_direction_context(case: dict) -> dict:
    cd = _as_dict(case)
    ctx = _as_dict(cd.get("risk_edge_context"))
    pair = [_canonical_component_name(x) for x in _as_list(cd.get("pair"))[:2] if x]
    top = _canonical_component_name(cd.get("top_component") or ctx.get("top_component"))
    bottom = _canonical_component_name(cd.get("bottom_component") or ctx.get("bottom_component"))
    sign = None
    if len(pair) == 2 and top and bottom:
        if pair[0] == top and pair[1] == bottom:
            sign = 1.0
        elif pair[0] == bottom and pair[1] == top:
            sign = -1.0
    ref_x = _safe_float(
        cd.get("reference_pair_fraction_first_component")
        or ctx.get("reference_pair_fraction_first_component"),
        None,
    )
    if ref_x is None and len(pair) == 2:
        flows = _as_dict(ctx.get("reference_feed_molar_flow") or cd.get("reference_feed_molar_flow"))
        a = _safe_float(flows.get(pair[0]), 0.0) or 0.0
        b = _safe_float(flows.get(pair[1]), 0.0) or 0.0
        if a + b > 1e-12:
            ref_x = float(a / (a + b))
    return {
        "pair": pair,
        "top_component": top or None,
        "bottom_component": bottom or None,
        "orientation_sign": sign,
        "direction_context_known": sign is not None,
        "reference_pair_fraction_first_component": ref_x,
        "desired_direction": cd.get("desired_direction") or ctx.get("desired_direction") or "top_component_more_volatile",
        "reference_solvent_to_feed_ratio": _safe_float(
            cd.get("reference_solvent_to_feed_ratio") or ctx.get("reference_solvent_to_feed_ratio"), None
        ),
    }


def _temperature_c_for_screen(point: dict) -> Optional[float]:
    pack = _as_dict(_as_dict(point).get("temperature"))
    c = _safe_float(pack.get("C"), None)
    raw = _safe_float(pack.get("raw"), None)
    unit = str(pack.get("unit") or "").upper()
    source = str(pack.get("source_path") or "").upper()
    # Backward compatibility for v47 result files: PFLIQ values around 300-900
    # were Kelvin but were labelled as Celsius when UnitString was absent.
    if raw is not None and unit == "C" and raw >= 250.0 and "PFLIQ" in source:
        return float(raw - 273.15)
    return c


def _weighted_mean_pairs(values: Sequence[Tuple[float, float]]) -> Optional[float]:
    rows = [(float(v), max(0.0, float(w))) for v, w in values if v is not None and w is not None]
    den = sum(w for _, w in rows)
    if not rows or den <= 1e-15:
        return None
    return float(sum(v * w for v, w in rows) / den)


def _curve_case_metrics(case: dict) -> dict:
    cd = _as_dict(case)
    pts = _sorted_valid_points(cd)
    direction = _risk_edge_direction_context(cd)
    pair = direction.get("pair") or [str(x) for x in _as_list(cd.get("pair"))[:2]]
    alphas = [_safe_float(p.get("alpha"), None) for p in pts]
    ymx = [_safe_float(p.get("y_minus_x"), None) for p in pts]
    xvals = [_safe_float(p.get("x"), None) for p in pts]
    yvals = [_safe_float(p.get("y"), None) for p in pts]
    a_min, a_max = _minmax(alphas)
    crossing_x = _interp_crossing_x(pts)
    mean_abs_gap = _mean([abs(v) if v is not None else None for v in ymx])
    max_abs_gap = max([abs(v) for v in ymx if v is not None], default=None)
    alpha_mean = _mean(alphas)

    min_temp = _safe_float(cd.get("min_valid_flash_temperature_C"), DEFAULT_MIN_VALID_FLASH_TEMPERATURE_C)
    max_temp = _safe_float(cd.get("max_valid_flash_temperature_C"), DEFAULT_MAX_VALID_FLASH_TEMPERATURE_C)
    engineering_pts: List[dict] = []
    excluded_temperature_points = 0
    temperatures_C: List[float] = []
    for point in pts:
        tc = _temperature_c_for_screen(point)
        if tc is not None:
            temperatures_C.append(float(tc))
            if (min_temp is not None and tc < float(min_temp)) or (max_temp is not None and tc > float(max_temp)):
                excluded_temperature_points += 1
                continue
        engineering_pts.append(point)

    ref_x = _safe_float(direction.get("reference_pair_fraction_first_component"), None)
    sign = _safe_float(direction.get("orientation_sign"), None)
    gap_rows: List[Tuple[float, float]] = []
    log_alpha_rows: List[Tuple[float, float]] = []
    reversal_rows: List[Tuple[float, float]] = []
    for point in engineering_pts:
        x = _safe_float(point.get("x"), None)
        y = _safe_float(point.get("y"), None)
        alpha = _safe_float(point.get("alpha"), None)
        if x is None or y is None:
            continue
        weight = 1.0
        if ref_x is not None:
            weight = 0.35 + 0.65 * math.exp(-0.5 * ((float(x) - float(ref_x)) / 0.18) ** 2)
        if sign is not None:
            signed_gap = float(sign) * (float(y) - float(x))
            gap_rows.append((signed_gap, weight))
            if alpha is not None and alpha > 1e-12:
                alpha_tb = float(alpha) if sign > 0 else 1.0 / float(alpha)
                log_a = math.log(max(alpha_tb, 1e-12))
                log_alpha_rows.append((log_a, weight))
                reversal_rows.append((1.0 if alpha_tb < 1.0 else 0.0, weight))

    directional_gap_mean = _weighted_mean_pairs(gap_rows)
    directional_log_alpha_mean = _weighted_mean_pairs(log_alpha_rows)
    volatility_order_reversed_fraction = _weighted_mean_pairs(reversal_rows)
    directional_alpha_geomean = (
        math.exp(float(directional_log_alpha_mean)) if directional_log_alpha_mean is not None else None
    )
    directional_quality = None
    if sign is not None and (directional_gap_mean is not None or directional_log_alpha_mean is not None):
        gap_term = math.tanh(float(directional_gap_mean or 0.0) / 0.05)
        alpha_term = math.tanh(float(directional_log_alpha_mean or 0.0) / math.log(1.35))
        directional_quality = 0.60 * gap_term + 0.40 * alpha_term

    slope_segments = []
    for i in range(1, len(pts)):
        x0, y0 = pts[i - 1]["x"], pts[i - 1]["y"]
        x1, y1 = pts[i]["x"], pts[i]["y"]
        if abs(x1 - x0) > 1e-12:
            slope_segments.append((y1 - y0) / (x1 - x0))
    slope_mean = _mean(slope_segments)
    monotonic_non_decreasing = all(pts[i]["y"] >= pts[i - 1]["y"] - 1e-9 for i in range(1, len(pts))) if len(pts) >= 2 else None
    volatility_hint = None
    if directional_alpha_geomean is not None and direction.get("top_component") and direction.get("bottom_component"):
        if directional_alpha_geomean > 1.05:
            volatility_hint = f"{direction.get('top_component')} is favored toward the vapor/top side"
        elif directional_alpha_geomean < 0.95:
            volatility_hint = f"volatility order is adverse: {direction.get('bottom_component')} is favored toward vapor"
        else:
            volatility_hint = "top/bottom relative volatility is close to unity"
    elif alpha_mean is not None and len(pair) >= 2:
        if alpha_mean > 1.05:
            volatility_hint = f"{pair[0]} appears more volatile than {pair[1]}"
        elif alpha_mean < 0.95:
            volatility_hint = f"{pair[1]} appears more volatile than {pair[0]}"
        else:
            volatility_hint = "relative volatility is close to unity"

    return {
        "pair": pair,
        "pair_key": "/".join(pair),
        "source_column_id": cd.get("source_column_id") or _as_dict(cd.get("risk_edge_context")).get("column_id"),
        "risk_role": cd.get("risk_role") or _as_dict(cd.get("risk_edge_context")).get("risk_role"),
        "top_component": direction.get("top_component"),
        "bottom_component": direction.get("bottom_component"),
        "desired_direction": direction.get("desired_direction"),
        "orientation_sign": sign,
        "direction_context_known": bool(direction.get("direction_context_known")),
        "reference_pair_fraction_first_component": ref_x,
        "reference_solvent_to_feed_ratio": direction.get("reference_solvent_to_feed_ratio"),
        "reference_base_pressure_kpa": _safe_float(
            cd.get("reference_base_pressure_kpa") or _as_dict(cd.get("risk_edge_context")).get("reference_base_pressure_kpa"), None
        ),
        "pressure_kpa": _safe_float(cd.get("pressure_kpa"), None),
        "point_count": len(pts),
        "engineering_valid_point_count": len(engineering_pts),
        "engineering_excluded_temperature_point_count": int(excluded_temperature_points),
        "engineering_temperature_valid": bool(len(engineering_pts) >= 2),
        "temperature_min_C": min(temperatures_C) if temperatures_C else None,
        "temperature_max_C": max(temperatures_C) if temperatures_C else None,
        "max_valid_flash_temperature_C": max_temp,
        "min_valid_flash_temperature_C": min_temp,
        "crossing_x": crossing_x,
        "crossing_detected": crossing_x is not None,
        "alpha_mean": alpha_mean,
        "alpha_min": a_min,
        "alpha_max": a_max,
        "mean_abs_y_minus_x": mean_abs_gap,
        "max_abs_y_minus_x": max_abs_gap,
        "directional_gap_mean": directional_gap_mean,
        "directional_log_alpha_mean": directional_log_alpha_mean,
        "directional_alpha_geomean": directional_alpha_geomean,
        "directional_quality": directional_quality,
        "volatility_order_reversed_fraction": volatility_order_reversed_fraction,
        "y_minus_x_sign_at_low_x": ymx[0] if ymx else None,
        "y_minus_x_sign_at_high_x": ymx[-1] if ymx else None,
        "monotonic_non_decreasing": monotonic_non_decreasing,
        "slope_mean": slope_mean,
        "first_x": xvals[0] if xvals else None,
        "last_x": xvals[-1] if xvals else None,
        "first_y": yvals[0] if yvals else None,
        "last_y": yvals[-1] if yvals else None,
        "volatility_hint": volatility_hint,
        "raw_case": cd,
    }

def _pair_curve_analysis(case_metrics: Sequence[dict]) -> dict:
    grouped: Dict[str, List[dict]] = {}
    for m in case_metrics:
        key = str(_as_dict(m).get("pair_key") or "")
        grouped.setdefault(key, []).append(_as_dict(m))
    pair_summaries = []
    for key, rows in grouped.items():
        rows = sorted(rows, key=lambda r: (_safe_float(r.get("pressure_kpa"), 0.0) or 0.0))
        low = rows[0]
        high = rows[-1]
        low_cross = _safe_float(low.get("crossing_x"), None)
        high_cross = _safe_float(high.get("crossing_x"), None)
        crossing_shift = None
        if low_cross is not None and high_cross is not None:
            crossing_shift = high_cross - low_cross
        alpha_mean_change = None
        if _safe_float(low.get("alpha_mean"), None) is not None and _safe_float(high.get("alpha_mean"),
                                                                                None) is not None:
            alpha_mean_change = _safe_float(high.get("alpha_mean"), 0.0) - _safe_float(low.get("alpha_mean"), 0.0)
        gap_change = None
        if _safe_float(low.get("mean_abs_y_minus_x"), None) is not None and _safe_float(high.get("mean_abs_y_minus_x"),
                                                                                        None) is not None:
            gap_change = _safe_float(high.get("mean_abs_y_minus_x"), 0.0) - _safe_float(low.get("mean_abs_y_minus_x"),
                                                                                        0.0)
        score = 0.0
        if crossing_shift is not None:
            score += min(abs(crossing_shift) * 100.0, 50.0)
        if alpha_mean_change is not None:
            score += min(abs(alpha_mean_change) * 20.0, 30.0)
        if gap_change is not None:
            score += min(abs(gap_change) * 100.0, 20.0)
        guidance = []
        route_hint = "weak_pressure_sensitivity"
        if low_cross is not None or high_cross is not None:
            guidance.append("azeotrope-like y=x crossing appears in at least one pressure level")
        if crossing_shift is not None:
            guidance.append(f"crossing shifts by {crossing_shift:+.4f} in x when pressure increases")
            if abs(crossing_shift) >= 0.05:
                route_hint = "strong_pressure_sensitive_crossing"
        if route_hint == "weak_pressure_sensitivity" and alpha_mean_change is not None and abs(
                alpha_mean_change) >= 0.10:
            route_hint = "pressure_sensitive_relative_volatility"
            guidance.append(f"mean relative volatility changes by {alpha_mean_change:+.4f}")
        if route_hint == "weak_pressure_sensitivity":
            alpha_band = [r.get("alpha_mean") for r in rows if r.get("alpha_mean") is not None]
            if alpha_band and min(alpha_band) > 1.2:
                route_hint = "ordinary_distillation_favorable"
                guidance.append("no clear crossing but volatility gap stays comfortably above unity")
        if not guidance:
            guidance.append("pressure effect appears limited within the scanned range")
        recommended = route_hint in {"strong_pressure_sensitive_crossing", "pressure_sensitive_relative_volatility"}
        pair_summaries.append({
            "pair_key": key,
            "pair": _as_list(low.get("pair")),
            "pressure_levels_kpa": [_safe_float(r.get("pressure_kpa"), None) for r in rows],
            "low_pressure_case": low,
            "high_pressure_case": high,
            "crossing_shift": crossing_shift,
            "alpha_mean_change": alpha_mean_change,
            "mean_abs_y_minus_x_change": gap_change,
            "pressure_sensitivity_score": score,
            "route_hint": route_hint,
            "recommended_for_psd_screen": recommended,
            "guidance": guidance,
        })
    pair_summaries.sort(key=lambda d: (_safe_float(d.get("pressure_sensitivity_score"), 0.0) or 0.0), reverse=True)
    return {
        "pair_summaries": pair_summaries,
        "recommended_priority_pairs": [p.get("pair_key") for p in pair_summaries if
                                       p.get("recommended_for_psd_screen")],
    }


def build_flash_curve_analysis(results: dict) -> dict:
    rd = _as_dict(results)
    cases = _as_list(rd.get("cases"))
    case_metrics = []
    excluded_cases = []
    for c in cases:
        metric = _curve_case_metrics(c)
        if int(_safe_float(metric.get("point_count"), 0) or 0) >= 2:
            case_metrics.append(metric)
        else:
            excluded_cases.append({
                "pair": _as_dict(c).get("pair"),
                "pressure_kpa": _as_dict(c).get("pressure_kpa"),
                "with_solvent": bool(_as_dict(c).get("with_solvent")),
                "reason": "fewer_than_two_valid_quality_checked_points",
            })
    pair_analysis = _pair_curve_analysis(case_metrics)
    summary_lines = []
    for item in _as_list(pair_analysis.get("pair_summaries"))[:5]:
        item = _as_dict(item)
        pair_key = item.get("pair_key") or "UNKNOWN"
        hint = item.get("route_hint") or "unknown"
        cross = _safe_float(item.get("crossing_shift"), None)
        alpha_change = _safe_float(item.get("alpha_mean_change"), None)
        parts = [f"{pair_key}: {hint}"]
        if cross is not None:
            parts.append(f"Δx_cross={cross:+.4f}")
        if alpha_change is not None:
            parts.append(f"Δalpha_mean={alpha_change:+.4f}")
        summary_lines.append("; ".join(parts))
    return {
        "case_metrics": case_metrics,
        "excluded_cases": excluded_cases,
        "pair_summaries": pair_analysis.get("pair_summaries", []),
        "recommended_priority_pairs": pair_analysis.get("recommended_priority_pairs", []),
        "summary_lines": summary_lines,
    }


# ---------------------------------------------------------------------------
# Flash-derived pressure-range inference for BO
# ---------------------------------------------------------------------------


def _interp_y_from_metric_points(metric: dict, x_grid: Sequence[float]) -> List[Optional[float]]:
    pts = []
    raw_case = _as_dict(metric.get("raw_case"))
    if raw_case:
        pts = [(p["x"], p["y"]) for p in _sorted_valid_points(raw_case)]
    if not pts:
        # Fallback: no raw case was preserved; a sparse reconstruction is still
        # better than treating the curve as absent.
        first_x = _safe_float(metric.get("first_x"), None)
        first_y = _safe_float(metric.get("first_y"), None)
        last_x = _safe_float(metric.get("last_x"), None)
        last_y = _safe_float(metric.get("last_y"), None)
        if first_x is not None and first_y is not None and last_x is not None and last_y is not None:
            pts = [(float(first_x), float(first_y)), (float(last_x), float(last_y))]
    pts = sorted((float(x), float(y)) for x, y in pts if x is not None and y is not None)
    if len(pts) < 2:
        return [None for _ in x_grid]
    out: List[Optional[float]] = []
    for xq in x_grid:
        xq = float(xq)
        if xq < pts[0][0] or xq > pts[-1][0]:
            out.append(None)
            continue
        yq = None
        for (x0, y0), (x1, y1) in zip(pts[:-1], pts[1:]):
            if x0 <= xq <= x1 and abs(x1 - x0) > 1e-12:
                t = (xq - x0) / (x1 - x0)
                yq = y0 + t * (y1 - y0)
                break
        out.append(yq)
    return out


def _median(values: Sequence[Optional[float]]) -> Optional[float]:
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    if n % 2:
        return vals[mid]
    return 0.5 * (vals[mid - 1] + vals[mid])


def _robust_abs_log_alpha_from_metric(metric: dict) -> Optional[float]:
    """Median |ln(alpha)| over interior composition points.

    Arithmetic mean alpha is strongly distorted by points close to x=0 or x=1.
    Pressure screening needs the change of the central VLE curve, not endpoint
    singularities.
    """
    m = _as_dict(metric)
    raw_case = _as_dict(m.get("raw_case"))
    vals: List[float] = []
    for p in _sorted_valid_points(raw_case):
        x = _safe_float(p.get("x"), None)
        a = _safe_float(p.get("alpha"), None)
        if x is None or a is None or a <= 1e-12:
            continue
        if 0.08 <= x <= 0.92:
            vals.append(abs(math.log(float(a))))
    med = _median(vals)
    if med is not None:
        return float(med)
    a = _safe_float(m.get("alpha_mean"), None)
    if a is not None and a > 1e-12:
        return abs(math.log(float(a)))
    return None


def _pressure_curve_spread_stats(clean: Sequence[dict]) -> Tuple[float, float]:
    """Return mean and maximum vertical curve spread on a common x-grid."""
    rows = [_as_dict(r) for r in clean]
    if len(rows) < 2:
        return 0.0, 0.0
    grid = [0.10, 0.18, 0.30, 0.45, 0.60, 0.75, 0.85, 0.90]
    curves = [_interp_y_from_metric_points(r, grid) for r in rows]
    spreads: List[float] = []
    for j in range(len(grid)):
        vals = [c[j] for c in curves if c[j] is not None]
        if len(vals) >= 2:
            spreads.append(max(vals) - min(vals))
    if not spreads:
        return 0.0, 0.0
    return float(sum(spreads) / len(spreads)), float(max(spreads))


def _pressure_curve_spread(clean: Sequence[dict]) -> float:
    """Mean vertical spread of y-x curves across pressure on a common x-grid."""
    mean_spread, _ = _pressure_curve_spread_stats(clean)
    return float(mean_spread)


def _pressure_quality_from_case_metric(metric: dict) -> Optional[float]:
    """Direction-aware separation usefulness at one pressure.

    For a process cut, ``top_component`` must become more volatile than
    ``bottom_component``.  Therefore alpha<1 is adverse rather than another
    large absolute separation signal.  Legacy cases without cut orientation
    retain the older unsigned metric only as a diagnostic fallback.
    """
    m = _as_dict(metric)
    directional = _safe_float(m.get("directional_quality"), None)
    if directional is not None and bool(m.get("direction_context_known")):
        return float(directional)
    gap = _safe_float(m.get("mean_abs_y_minus_x"), None)
    alpha = _safe_float(m.get("alpha_mean"), None)
    score = 0.0
    used = False
    if gap is not None:
        score += 0.65 * max(0.0, min(float(gap), 0.35)) / 0.35
        used = True
    if alpha is not None and alpha > 1e-12:
        score += 0.35 * max(0.0, min(abs(math.log(float(alpha))), math.log(3.0))) / math.log(3.0)
        used = True
    return float(score) if used else None

def _pressure_neighbor_window(pressures: Sequence[float], best_p: float, base_p: float) -> Tuple[float, float]:
    vals = sorted({float(p) for p in pressures if _safe_float(p, None) is not None and float(p) > 0})
    if not vals:
        return max(30.0, 0.8 * base_p), max(150.0, 1.2 * base_p)
    idx = min(range(len(vals)), key=lambda i: abs(vals[i] - float(best_p)))
    if len(vals) == 1:
        low = max(1.0, vals[0] * 0.85)
        high = vals[0] * 1.15
    else:
        if best_p > base_p * 1.20:
            # 加压有效：只取最佳压力附近的高压侧窗口，避免把常压/低压也丢进 BO。
            low = vals[max(0, idx - 1)]
            high = vals[min(len(vals) - 1, idx + 1)]
            if idx == len(vals) - 1:
                high = vals[-1]
            low = max(low, min(vals[-1], base_p))
        elif best_p < base_p * 0.80:
            # 减压有效：只取低压侧窗口。
            low = vals[max(0, idx - 1)]
            high = vals[min(len(vals) - 1, idx + 1)]
            if idx == 0:
                low = vals[0]
            high = min(high, max(vals[0], base_p))
        else:
            low = vals[max(0, idx - 1)]
            high = vals[min(len(vals) - 1, idx + 1)]
    if low >= high:
        low = max(1.0, best_p * 0.90)
        high = max(low + 1e-6, best_p * 1.10)
    return float(low), float(high)


def _pressure_sensitivity_decision(rows: Sequence[dict], *, base_pressure_kpa: float = 101.325) -> dict:
    """Separate pressure sensitivity from favorable process-cut direction.

    Pressure may move a y-x curve strongly while reversing the volatility order.
    Such a case is thermodynamically pressure-sensitive but is not an actionable
    pressure direction for the retained ED cut.
    """
    clean_all: List[dict] = []
    for row in rows:
        rr = _as_dict(row)
        p = _safe_float(rr.get("pressure_kpa"), None)
        if p is None or p <= 0:
            continue
        q = _pressure_quality_from_case_metric(rr)
        clean_all.append({
            "pressure_kpa": float(p),
            "quality": q,
            "directional_quality": _safe_float(rr.get("directional_quality"), None),
            "directional_gap_mean": _safe_float(rr.get("directional_gap_mean"), None),
            "directional_log_alpha_mean": _safe_float(rr.get("directional_log_alpha_mean"), None),
            "directional_alpha_geomean": _safe_float(rr.get("directional_alpha_geomean"), None),
            "volatility_order_reversed_fraction": _safe_float(rr.get("volatility_order_reversed_fraction"), None),
            "direction_context_known": bool(rr.get("direction_context_known")),
            "top_component": rr.get("top_component"),
            "bottom_component": rr.get("bottom_component"),
            "alpha_mean": _safe_float(rr.get("alpha_mean"), None),
            "robust_abs_log_alpha": _robust_abs_log_alpha_from_metric(rr),
            "mean_abs_y_minus_x": _safe_float(rr.get("mean_abs_y_minus_x"), None),
            "crossing_x": _safe_float(rr.get("crossing_x"), None),
            "crossing_detected": bool(rr.get("crossing_detected")),
            "point_count": int(_safe_float(rr.get("point_count"), 0) or 0),
            "engineering_valid_point_count": int(_safe_float(rr.get("engineering_valid_point_count"), rr.get("point_count")) or 0),
            "engineering_temperature_valid": bool(rr.get("engineering_temperature_valid", True)),
            "temperature_min_C": _safe_float(rr.get("temperature_min_C"), None),
            "temperature_max_C": _safe_float(rr.get("temperature_max_C"), None),
            "max_valid_flash_temperature_C": _safe_float(rr.get("max_valid_flash_temperature_C"), DEFAULT_MAX_VALID_FLASH_TEMPERATURE_C),
            "raw_case": _as_dict(rr.get("raw_case")),
            "solvent_to_feed_ratio": rr.get("solvent_to_feed_ratio"),
            "reference_solvent_to_feed_ratio": _safe_float(rr.get("reference_solvent_to_feed_ratio"), None),
            "reference_base_pressure_kpa": _safe_float(
                rr.get("reference_base_pressure_kpa")
                or _as_dict(rr.get("raw_case")).get("reference_base_pressure_kpa")
                or _as_dict(_as_dict(rr.get("raw_case")).get("risk_edge_context")).get("reference_base_pressure_kpa"),
                None,
            ),
        })
    if len(clean_all) < 2:
        return {
            "should_optimize_pressure": False,
            "material_pressure_change": False,
            "actionable_pressure_benefit": False,
            "base_pressure_is_best": False,
            "direction": "insufficient_flash_data",
            "sensitivity_score": 0.0,
            "reason": "有效压力点少于 2 个，不能从 Flash 结果推导变压范围。",
        }

    # Deduplicate pressure levels before both magnitude and directional tests.
    by_p: Dict[float, dict] = {}
    for row in clean_all:
        p = row["pressure_kpa"]
        rank = (
            int(row.get("engineering_valid_point_count") or 0),
            int(row.get("point_count") or 0),
        )
        old = by_p.get(p)
        old_rank = (
            int(old.get("engineering_valid_point_count") or 0),
            int(old.get("point_count") or 0),
        ) if old else (-1, -1)
        if old is None or rank > old_rank:
            by_p[p] = row
    clean_all = [by_p[p] for p in sorted(by_p)]
    clean = [r for r in clean_all if r.get("engineering_temperature_valid") and int(r.get("engineering_valid_point_count") or 0) >= 2]

    base_candidates = [_safe_float(row.get("reference_base_pressure_kpa"), None) for row in clean_all]
    base_candidates = [float(v) for v in base_candidates if v is not None and v > 0]
    effective_base_p = sorted(base_candidates)[len(base_candidates) // 2] if base_candidates else float(base_pressure_kpa)

    if len(clean) < 2:
        return {
            "should_optimize_pressure": False,
            "material_pressure_change": False,
            "actionable_pressure_benefit": False,
            "base_pressure_is_best": False,
            "direction": "engineering_temperature_gate_failed",
            "sensitivity_score": 0.0,
            "base_pressure_kpa": effective_base_p,
            "pressure_metrics": clean_all,
            "excluded_pressure_metrics": [r for r in clean_all if r not in clean],
            "reason": "压力曲线虽有 Aspen 收敛点，但工程温度门后有效压力层少于 2 个；不生成塔压 BO 变量。",
        }

    qualities = [row["quality"] for row in clean if row.get("quality") is not None]
    gaps = [row["mean_abs_y_minus_x"] for row in clean if row.get("mean_abs_y_minus_x") is not None]
    robust_logs = [row["robust_abs_log_alpha"] for row in clean if row.get("robust_abs_log_alpha") is not None]
    crossings = [
        row["crossing_x"] for row in clean
        if row.get("crossing_x") is not None and 0.08 < float(row.get("crossing_x")) < 0.92
    ]
    q_range = (max(qualities) - min(qualities)) if len(qualities) >= 2 else 0.0
    gap_range = (max(gaps) - min(gaps)) if len(gaps) >= 2 else 0.0
    alpha_log_range = (max(robust_logs) - min(robust_logs)) if len(robust_logs) >= 2 else 0.0
    crossing_range = (max(crossings) - min(crossings)) if len(crossings) >= 2 else 0.0
    curve_spread, curve_spread_max = _pressure_curve_spread_stats(clean)
    mean_gap_level = float(sum(gaps) / len(gaps)) if gaps else 0.0
    relative_curve_spread = curve_spread / max(mean_gap_level, 0.03)

    evidence = {
        "mean_curve_spread": bool(curve_spread >= 0.025 and relative_curve_spread >= 0.12),
        "maximum_curve_spread": bool(curve_spread_max >= 0.045),
        "quality_change": bool(q_range >= 0.060),
        "yx_gap_change": bool(gap_range >= 0.030),
        "robust_alpha_change": bool(alpha_log_range >= 0.100),
        "interior_crossing_shift": bool(crossing_range >= 0.055),
    }
    evidence_count = sum(1 for value in evidence.values() if value)
    strong_signal = bool(
        curve_spread_max >= 0.065 or q_range >= 0.100 or gap_range >= 0.050
        or alpha_log_range >= 0.180 or crossing_range >= 0.090
    )
    material_change = bool(strong_signal or evidence_count >= 2)
    raw_score = (
        0.34 * max(0.0, min(curve_spread / 0.045, 1.0))
        + 0.18 * max(0.0, min(curve_spread_max / 0.075, 1.0))
        + 0.16 * max(0.0, min(q_range / 0.10, 1.0))
        + 0.12 * max(0.0, min(gap_range / 0.050, 1.0))
        + 0.12 * max(0.0, min(alpha_log_range / 0.18, 1.0))
        + 0.08 * max(0.0, min(crossing_range / 0.09, 1.0))
    )
    sensitivity_score = float(raw_score if material_change else min(raw_score, 0.33))

    direction_known = any(bool(r.get("direction_context_known")) for r in clean)
    best = max(clean, key=lambda row: _safe_float(row.get("quality"), -9.0) or -9.0)
    best_p = float(best["pressure_kpa"])
    base_ref = min(clean, key=lambda row: abs(float(row["pressure_kpa"]) - effective_base_p))
    base_ref_p = float(base_ref["pressure_kpa"])
    best_q = _safe_float(best.get("quality"), None)
    base_q = _safe_float(base_ref.get("quality"), None)
    quality_gain = float(best_q) - float(base_q) if best_q is not None and base_q is not None else 0.0
    pressure_shift_ratio = abs(best_p - effective_base_p) / max(abs(effective_base_p), 1e-12)
    nearest_base_ratio = abs(base_ref_p - effective_base_p) / max(abs(effective_base_p), 1e-12)
    best_is_nearest_base_sample = abs(best_p - base_ref_p) <= max(1e-9, 1e-6 * max(best_p, base_ref_p, 1.0))
    base_pressure_is_best = bool(best_is_nearest_base_sample or pressure_shift_ratio <= ACTIONABLE_BASE_PRESSURE_REL_TOL)
    best_directional_quality = _safe_float(best.get("directional_quality"), best_q)
    best_log_alpha = _safe_float(best.get("directional_log_alpha_mean"), None)
    best_reversed_fraction = _safe_float(best.get("volatility_order_reversed_fraction"), None)
    direction_is_favorable = bool(
        (not direction_known)
        or (
            best_directional_quality is not None
            and best_directional_quality >= DIRECTIONAL_MIN_ACCEPTABLE_QUALITY
            and (best_log_alpha is None or best_log_alpha >= -0.03)
            and (best_reversed_fraction is None or best_reversed_fraction <= 0.55)
        )
    )
    actionable = bool(
        material_change and not base_pressure_is_best
        and quality_gain >= ACTIONABLE_MIN_QUALITY_GAIN and direction_is_favorable
    )

    common = {
        "top_component": best.get("top_component") or base_ref.get("top_component"),
        "bottom_component": best.get("bottom_component") or base_ref.get("bottom_component"),
        "direction_context_known": bool(direction_known),
        "directional_gate_applied": bool(direction_known),
        "direction_is_favorable": bool(direction_is_favorable),
        "best_pressure_kpa": best_p,
        "best_pressure_atm": float(best_p) / ATMOSPHERIC_PRESSURE_KPA,
        "best_pressure_regime": _pressure_regime_label(best_p),
        "vacuum_candidate": bool(best_p < VACUUM_PRESSURE_THRESHOLD_KPA),
        "best_quality": best_q,
        "best_directional_quality": best_directional_quality,
        "best_directional_log_alpha_mean": best_log_alpha,
        "best_directional_alpha_geomean": best.get("directional_alpha_geomean"),
        "best_directional_gap_mean": best.get("directional_gap_mean"),
        "best_volatility_order_reversed_fraction": best_reversed_fraction,
        "base_pressure_kpa": float(effective_base_p),
        "base_pressure_atm": float(effective_base_p) / ATMOSPHERIC_PRESSURE_KPA,
        "base_pressure_regime": _pressure_regime_label(effective_base_p),
        "base_reference_pressure_kpa": base_ref_p,
        "base_reference_quality": base_q,
        "base_reference_directional_quality": base_ref.get("directional_quality"),
        "base_reference_distance_ratio": float(nearest_base_ratio),
        "quality_improvement_vs_base": float(quality_gain),
        "best_pressure_shift_ratio": float(pressure_shift_ratio),
        "base_pressure_is_best": bool(base_pressure_is_best),
        "actionable_pressure_benefit": bool(actionable),
        "actionable_base_pressure_rel_tol": float(ACTIONABLE_BASE_PRESSURE_REL_TOL),
        "actionable_min_quality_gain": float(ACTIONABLE_MIN_QUALITY_GAIN),
        "sensitivity_score": sensitivity_score,
        "raw_sensitivity_score": float(raw_score),
        "quality_range": float(q_range),
        "gap_range": float(gap_range),
        "alpha_log_range": float(alpha_log_range),
        "crossing_range": float(crossing_range),
        "curve_spread": float(curve_spread),
        "curve_spread_max": float(curve_spread_max),
        "relative_curve_spread": float(relative_curve_spread),
        "material_evidence": evidence,
        "material_evidence_count": int(evidence_count),
        "material_pressure_change": bool(material_change),
        "pressure_metrics": clean,
        "excluded_pressure_metrics": [r for r in clean_all if r not in clean],
    }
    hold_bounds_kpa = [round(effective_base_p * 0.98, 4), round(effective_base_p * 1.02, 4)]
    hold_bounds_atm = [round(x / ATMOSPHERIC_PRESSURE_KPA, 6) for x in hold_bounds_kpa]

    if not material_change:
        return {**common, "should_optimize_pressure": False, "direction": "hold_near_base_pressure",
                "recommended_bounds_kpa": hold_bounds_kpa, "recommended_bounds_atm": hold_bounds_atm,
                "reason": "多压力曲线存在性证据不足；该风险边不参与塔压 BO。"}
    if direction_known and not direction_is_favorable:
        return {**common, "should_optimize_pressure": False, "direction": "reject_adverse_volatility_order",
                "recommended_bounds_kpa": hold_bounds_kpa, "recommended_bounds_atm": hold_bounds_atm,
                "reason": "压力变化显著，但最佳绝对偏离对应错误的塔顶/塔底挥发性方向；不新增塔压 BO 变量。"}
    if base_pressure_is_best:
        return {**common, "should_optimize_pressure": False, "direction": "hold_at_ED_best_pressure",
                "recommended_bounds_kpa": hold_bounds_kpa, "recommended_bounds_atm": hold_bounds_atm,
                "reason": f"风险边具有压敏性，但 ED_best 基准压力 {effective_base_p:g} kPa 附近已是方向感知后的最佳点。"}
    if quality_gain < ACTIONABLE_MIN_QUALITY_GAIN:
        return {**common, "should_optimize_pressure": False, "direction": "hold_at_ED_best_pressure",
                "recommended_bounds_kpa": hold_bounds_kpa, "recommended_bounds_atm": hold_bounds_atm,
                "reason": f"方向正确的最佳压力相对基准收益仅 {quality_gain:.4f}，低于阈值 {ACTIONABLE_MIN_QUALITY_GAIN:.4f}。"}

    low, high = _pressure_neighbor_window([row["pressure_kpa"] for row in clean], best_p, effective_base_p)
    if best_p > effective_base_p * 1.20:
        direction, cn = "pressurize", "加压"
    elif best_p < effective_base_p * 0.80:
        direction, cn = "depressurize", "减压"
    else:
        direction, cn = "narrow_near_best_pressure", "围绕最佳压力窄范围扫描"
    return {
        **common,
        "should_optimize_pressure": True,
        "direction": direction,
        "recommended_bounds_kpa": [round(low, 4), round(high, 4)],
        "recommended_bounds_atm": [round(low / ATMOSPHERIC_PRESSURE_KPA, 6), round(high / ATMOSPHERIC_PRESSURE_KPA, 6)],
        "reason": (
            f"方向感知 Flash2 显示 {common.get('top_component')}/{common.get('bottom_component')} "
            f"在 {best_p:g} kPa 对目标切割更有利，较 ED_best 品质增益={quality_gain:.4f}；建议{cn}，"
            "并由 RadFrac 单塔连续验证确定最终可行走廊。"
        ),
    }

def _risk_edge_metadata_map(results: dict) -> Dict[Tuple[str, str], dict]:
    rd = _as_dict(results)
    plan = _as_dict(rd.get("risk_edge_plan"))
    if not plan:
        plan = _as_dict(_as_dict(rd.get("separation_agent_handoff")).get("risk_edge_plan"))
    out: Dict[Tuple[str, str], dict] = {}
    for cut in _as_list(plan.get("cuts")):
        cut = _as_dict(cut)
        cid = str(cut.get("column_id") or "").upper()
        top_set = {_canonical_component_name(x) for x in _as_list(cut.get("top_components")) if x}
        bottom_set = {_canonical_component_name(x) for x in _as_list(cut.get("bottom_components")) if x}
        for edge in _as_list(cut.get("selected_edges") or cut.get("candidate_edges")):
            edge = _as_dict(edge)
            pair = [_canonical_component_name(x) for x in _as_list(edge.get("pair"))[:2] if x]
            if len(pair) != 2:
                continue
            top = next((x for x in pair if x in top_set), pair[0])
            bottom = next((x for x in pair if x in bottom_set and x != top), pair[1])
            ctx = copy.deepcopy(_as_dict(edge.get("context")))
            ctx.update({
                "column_id": cid or edge.get("column_id"),
                "top_component": top,
                "bottom_component": bottom,
                "desired_direction": "top_component_more_volatile",
                "risk_role": edge.get("risk_role"),
            })
            key = (cid, "/".join(sorted(pair)))
            old = out.get(key)
            if old is None or str(edge.get("risk_role")) == "primary":
                out[key] = ctx
    for key, ctx0 in _as_dict(plan.get("pair_contexts")).items():
        ctx = copy.deepcopy(_as_dict(ctx0))
        pair = [_canonical_component_name(x) for x in _as_list(ctx.get("pair"))[:2] if x]
        if len(pair) != 2:
            pair = [_canonical_component_name(x) for x in str(key).split("/")[:2] if x]
        if len(pair) != 2:
            continue
        cid = str(ctx.get("column_id") or "").upper()
        map_key = (cid, "/".join(sorted(pair)))
        out.setdefault(map_key, ctx)
    return out


def build_flash_pressure_optimization_recommendations(results: dict, *, base_pressure_kpa: float = 101.325) -> dict:
    """Build directional, ED_best-anchored pressure candidates.

    S/F perturbations are robustness checks only.  The recommendation itself is
    selected from the curve whose solvent ratio is closest to ED_best, rather
    than from whichever solvent ratio happens to have the largest sensitivity.
    """
    rd = _as_dict(results)
    edge_meta = _risk_edge_metadata_map(rd)
    case_metrics: List[dict] = []
    for c in _as_list(rd.get("cases")):
        cd = copy.deepcopy(_as_dict(c))
        pair0 = [_canonical_component_name(x) for x in _as_list(cd.get("pair"))[:2] if x]
        cid0 = str(cd.get("source_column_id") or _as_dict(cd.get("risk_edge_context")).get("column_id") or "").upper()
        meta = edge_meta.get((cid0, "/".join(sorted(pair0)))) if len(pair0) == 2 else None
        if meta:
            ctx = copy.deepcopy(_as_dict(cd.get("risk_edge_context")))
            for key, value in _as_dict(meta).items():
                if ctx.get(key) is None:
                    ctx[key] = copy.deepcopy(value)
            cd["risk_edge_context"] = ctx
            for key in (
                "top_component", "bottom_component", "desired_direction",
                "reference_base_pressure_kpa", "reference_pair_fraction_first_component",
                "reference_solvent_to_feed_ratio",
            ):
                if cd.get(key) is None and ctx.get(key) is not None:
                    cd[key] = copy.deepcopy(ctx.get(key))
        try:
            m = _curve_case_metrics(cd)
        except Exception:
            continue
        if (_safe_float(m.get("point_count"), 0) or 0) < 2:
            continue
        m["with_solvent"] = bool(cd.get("with_solvent") or cd.get("solvent") or "solvent" in str(cd.get("case_type", "")).lower())
        m["solvent"] = cd.get("solvent")
        m["case_type"] = cd.get("case_type")
        m["solvent_to_feed_ratio"] = _safe_float(cd.get("solvent_to_feed_ratio"), None)
        case_metrics.append(m)

    subgroups: Dict[Tuple[str, str, Optional[float], str], List[dict]] = {}
    for m in case_metrics:
        pair_key = str(m.get("pair_key") or "")
        if not pair_key:
            continue
        mode = "EPSD" if m.get("with_solvent") else "PSD"
        sf = _safe_float(m.get("solvent_to_feed_ratio"), None) if mode == "EPSD" else None
        sf_key = round(float(sf), 8) if sf is not None else None
        column_id = str(m.get("source_column_id") or "")
        subgroups.setdefault((mode, pair_key, sf_key, column_id), []).append(m)

    evaluated: Dict[Tuple[str, str, str], List[dict]] = {}
    for (mode, pair_key, sf, column_id), rows in subgroups.items():
        decision = _pressure_sensitivity_decision(rows, base_pressure_kpa=base_pressure_kpa)
        decision["solvent_to_feed_ratio"] = sf
        decision["reference_solvent_to_feed_ratio"] = _median([
            _safe_float(_as_dict(r).get("reference_solvent_to_feed_ratio"), None) for r in rows
        ])
        decision["source_column_id"] = column_id or None
        decision["risk_role"] = _as_dict(rows[0]).get("risk_role") if rows else None
        evaluated.setdefault((mode, pair_key, column_id), []).append(decision)

    pair_recs: List[dict] = []
    for (mode, pair_key, column_id), decisions in evaluated.items():
        reference_sf = _median([_safe_float(d.get("reference_solvent_to_feed_ratio"), None) for d in decisions])
        if mode == "EPSD" and reference_sf is not None:
            selected = min(
                decisions,
                key=lambda d: abs((_safe_float(d.get("solvent_to_feed_ratio"), reference_sf) or reference_sf) - reference_sf),
            )
            selection_basis = "closest_to_ED_best_solvent_ratio"
        else:
            selected = max(decisions, key=lambda d: _safe_float(d.get("sensitivity_score"), 0.0) or 0.0)
            selection_basis = "single_or_solvent_free_curve"
        rec = copy.deepcopy(selected)

        def side(d: dict) -> str:
            bp = _safe_float(d.get("base_pressure_kpa"), base_pressure_kpa) or base_pressure_kpa
            pp = _safe_float(d.get("best_pressure_kpa"), bp) or bp
            if pp > bp * 1.20:
                return "pressurize"
            if pp < bp * 0.80:
                return "depressurize"
            return "near_base"

        material_direction_rows = [
            d for d in decisions
            if d.get("material_pressure_change")
            and (_safe_float(d.get("quality_improvement_vs_base"), 0.0) or 0.0) >= 0.5 * ACTIONABLE_MIN_QUALITY_GAIN
            and d.get("direction_is_favorable", True)
        ]
        directional_sides = {side(d) for d in material_direction_rows if side(d) != "near_base"}
        direction_robust = len(directional_sides) <= 1
        if mode == "EPSD" and rec.get("should_optimize_pressure") and not direction_robust:
            rec["should_optimize_pressure"] = False
            rec["actionable_pressure_benefit"] = False
            rec["direction"] = "pressure_direction_not_robust_to_solvent_ratio"
            rec["reason"] = (
                "ED_best S/F 附近不同固定溶剂比给出相反变压方向；"
                "压敏性存在，但不能据此生成硬 BO 压力窗口。"
            )

        rows0 = subgroups.get((mode, pair_key, rec.get("solvent_to_feed_ratio"), column_id), [])
        pair = _as_list(_as_dict(rows0[0]).get("pair"))[:2] if rows0 else pair_key.split("/")
        rec.update({
            "mode": mode,
            "pair_key": pair_key,
            "pair": pair,
            "source_column_id": column_id or rec.get("source_column_id"),
            "risk_role": rec.get("risk_role"),
            "reference_solvent_to_feed_ratio": reference_sf,
            "solvent_ratio_selection_basis": selection_basis,
            "direction_robust_to_solvent_ratio": bool(direction_robust),
            "source": "directional_flash_yx_alpha_fixed_solvent_ratio",
            "unit": "kPa_and_atm",
            "ratio_screening": [
                {
                    "solvent_to_feed_ratio": d.get("solvent_to_feed_ratio"),
                    "reference_solvent_to_feed_ratio": d.get("reference_solvent_to_feed_ratio"),
                    "should_optimize_pressure": bool(d.get("should_optimize_pressure")),
                    "material_pressure_change": bool(d.get("material_pressure_change")),
                    "direction_is_favorable": bool(d.get("direction_is_favorable", True)),
                    "best_pressure_kpa": d.get("best_pressure_kpa"),
                    "direction": d.get("direction"),
                    "best_directional_quality": d.get("best_directional_quality"),
                    "quality_improvement_vs_base": d.get("quality_improvement_vs_base"),
                    "sensitivity_score": d.get("sensitivity_score"),
                    "raw_sensitivity_score": d.get("raw_sensitivity_score"),
                    "reason": d.get("reason"),
                }
                for d in sorted(decisions, key=lambda x: (_safe_float(x.get("solvent_to_feed_ratio"), -1.0) or -1.0))
            ],
        })
        pair_recs.append(rec)

    pair_recs.sort(key=lambda d: (
        0 if d.get("should_optimize_pressure") else 1,
        -(_safe_float(d.get("sensitivity_score"), 0.0) or 0.0),
        str(d.get("pair_key") or ""),
    ))
    summary_lines: List[str] = []
    for r in pair_recs:
        key = f"{r.get('mode')} {r.get('pair_key')}"
        sf = r.get("solvent_to_feed_ratio")
        sf_text = f" | S/F={sf:g}" if sf is not None else ""
        if r.get("should_optimize_pressure"):
            b = r.get("recommended_bounds_kpa") or []
            regime = " | vacuum" if _safe_float(r.get("best_pressure_kpa"), 1e9) < VACUUM_PRESSURE_THRESHOLD_KPA else ""
            summary_lines.append(
                f"{key}: {r.get('direction')} | P_candidate={b[0]:g}-{b[1]:g} kPa{regime}{sf_text} "
                f"| directional_gain={_safe_float(r.get('quality_improvement_vs_base'),0):.3f}"
            )
        else:
            summary_lines.append(f"{key}: skip pressure BO{sf_text} | {r.get('reason')} | score={_safe_float(r.get('sensitivity_score'),0):.3f}")

    return {
        "status": "ready" if pair_recs else "no_valid_pair_pressure_data",
        "base_pressure_kpa": base_pressure_kpa,
        "pair_recommendations": pair_recs,
        "recommended_priority_pairs": [r.get("pair_key") for r in pair_recs if r.get("should_optimize_pressure")],
        "summary_lines": summary_lines,
        "decision_rule": {
            "sensitivity_vs_direction": "曲线变化幅度只证明压敏性；塔压方向必须按 top/bottom 目标切割的有符号 alpha 与 y-x 判断。",
            "base_optimum_gate": "若方向感知后的最佳点位于 ED_best 附近，固定该塔压力。",
            "epsd_reference_ratio": "ED_best S/F 决定主推荐；0.8x/1.2x S/F 仅用于方向稳健性检查。",
            "engineering_gate": "超出工程温度范围的压力层不生成塔压候选。",
            "radfrac_confirmation": "Flash 只生成候选方向；最终 BO 窗口由单塔 RadFrac 连续可行域追踪确定。",
        },
    }

def feed_condition_from_payload_or_state(payload: Optional[dict], state_dict: Optional[dict]) -> dict:
    payload = _as_dict(payload)
    cond = _as_dict(payload.get("feed_condition"))
    if cond:
        return cond
    return feed_condition_from_state(state_dict)




# ---------------------------------------------------------------------------
# Separation-page final plot bridge
# ---------------------------------------------------------------------------

def _separation_page_artifact_dir(out_dir: Optional[str] = None) -> str:
    """Return the fixed output directory for separation-page plots.

    This mirrors the BO live/final plot idea: the runner owns the latest plot
    artifacts, writes them to stable filenames, and the GUI only displays the
    saved path.  Stable filenames avoid accumulating PNG files during repeated
    refreshes.
    """
    if out_dir is None:
        out_dir = os.path.join(os.getcwd(), "gradio_runtime_artifacts")
    out_dir = os.path.abspath(os.path.expanduser(os.path.expandvars(str(out_dir))))
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


def _plot_pair_key(case: dict) -> str:
    pair = [_canonical_component_name(x) for x in _as_list(_as_dict(case).get("pair"))[:2]]
    return "/".join(pair) if len(pair) == 2 else ""


def _plot_pair_fraction(comp: dict, pair: Sequence[str]) -> Optional[float]:
    if len(pair) < 2:
        return None
    c1 = _canonical_component_name(pair[0])
    c2 = _canonical_component_name(pair[1])
    d = _as_dict(comp)
    v1 = _safe_float(d.get(c1), None)
    v2 = _safe_float(d.get(c2), None)
    if v1 is None:
        # Fallback: keys may have already been saved as raw Aspen IDs.
        for k, v in d.items():
            if _canonical_component_name(k) == c1:
                v1 = _safe_float(v, None)
                break
    if v2 is None:
        for k, v in d.items():
            if _canonical_component_name(k) == c2:
                v2 = _safe_float(v, None)
                break
    if v1 is None or v2 is None:
        return None
    den = float(v1) + float(v2)
    if den <= 1e-14:
        return None
    return float(v1) / den


def _plot_points_for_pair(case: dict) -> List[Tuple[float, float]]:
    """Extract liquid/vapour x-y points from binary or ternary runner results."""
    cd = _as_dict(case)
    pair = [_canonical_component_name(x) for x in _as_list(cd.get("pair"))[:2]]
    if len(pair) != 2:
        return []
    points: List[Tuple[float, float]] = []
    for raw in _as_list(cd.get("points")):
        p = _as_dict(raw)
        x = _safe_float(p.get("x"), None)
        y = _safe_float(p.get("y"), None)
        if x is None or y is None:
            # Ternary/pseudo-ternary Flash results store full compositions.
            x = _plot_pair_fraction(p.get("liquid_ternary_x") or p.get("liquid_mole_fraction") or {}, pair)
            y = _plot_pair_fraction(p.get("vapour_ternary_y") or p.get("vapour_mole_fraction") or {}, pair)
        if x is None or y is None:
            continue
        if 0.0 <= float(x) <= 1.0 and 0.0 <= float(y) <= 1.0:
            points.append((float(x), float(y)))
    points.sort(key=lambda t: t[0])
    return points


def _plot_crossing_x(points: Sequence[Tuple[float, float]]) -> Optional[float]:
    pts = [(float(x), float(y)) for x, y in points if x is not None and y is not None]
    if len(pts) < 2:
        return None
    vals = [(x, y - x) for x, y in pts]
    for (x0, d0), (x1, d1) in zip(vals[:-1], vals[1:]):
        if abs(d0) < 1e-8:
            return float(x0)
        if d0 * d1 < 0 and abs(d1 - d0) > 1e-12:
            return float(x0 - d0 * (x1 - x0) / (d1 - d0))
    return None


def _plot_component_pairs_from_results(results: dict, max_pairs: int = 3) -> List[str]:
    order: List[str] = []
    analysis = _as_dict(_as_dict(results).get("curve_analysis"))
    for item in _as_list(analysis.get("pair_summaries")):
        key = str(_as_dict(item).get("pair_key") or "").strip()
        if key and key not in order:
            order.append(key)
    for case in _as_list(_as_dict(results).get("cases")):
        key = _plot_pair_key(_as_dict(case))
        if key and key not in order:
            order.append(key)
    return order[:max_pairs]



def _group_plot_cases_by_pressure(results: dict, pair_key: str) -> Dict[float, List[Tuple[float, float]]]:
    """Return one consolidated y-x point set per pressure for a component pair.

    Flash payloads may create several cases with the same pair/pressure
    (different vapour fraction seeds, solvent ratios, or retry branches).  The
    separation-page overview should show *pressure trends*, not every internal
    runner case.  Therefore this helper first prefers native binary VLE cases
    and then merges duplicate points at the same pressure.
    """
    rd = _as_dict(results)
    all_cases = [
        _as_dict(c) for c in _as_list(rd.get("cases"))
        if _plot_pair_key(_as_dict(c)) == pair_key and _plot_points_for_pair(_as_dict(c))
    ]
    if not all_cases:
        return {}

    binary_cases = [
        c for c in all_cases
        if str(c.get("case_type") or "").lower() == "binary_pressure_vle"
        and not bool(c.get("with_solvent"))
    ]
    selected = binary_cases or all_cases

    grouped: Dict[float, List[Tuple[float, float]]] = {}
    for case in selected:
        p_kpa = _safe_float(case.get("pressure_kpa"), None)
        if p_kpa is None:
            # Keep unknown-pressure data separate but deterministic.
            p_kpa = -1.0
        grouped.setdefault(float(p_kpa), []).extend(_plot_points_for_pair(case))
    return grouped


def _aggregate_xy_points(points: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """Collapse repeated x values and return monotonic x-y data.

    Duplicate x values appear when the runner has several internal cases at the
    same pressure.  Averaging y at identical x keeps the display readable while
    preserving the pressure-level trend.
    """
    buckets: Dict[float, List[float]] = {}
    for x, y in points:
        xf = _safe_float(x, None)
        yf = _safe_float(y, None)
        if xf is None or yf is None:
            continue
        if not (0.0 <= xf <= 1.0 and 0.0 <= yf <= 1.0):
            continue
        key = round(float(xf), 5)
        buckets.setdefault(key, []).append(float(yf))
    out: List[Tuple[float, float]] = []
    for x in sorted(buckets):
        vals = buckets[x]
        if not vals:
            continue
        out.append((float(x), float(sum(vals) / len(vals))))
    # Add physical endpoints only when they are not already present.
    if not out or out[0][0] > 1e-8:
        out.insert(0, (0.0, 0.0))
    if out[-1][0] < 1.0 - 1e-8:
        out.append((1.0, 1.0))
    return out


def _pressure_grid_for_demo_plot(results: Optional[dict] = None, state_dict: Optional[dict] = None) -> List[float]:
    rd = _as_dict(results)
    grid = rd.get("pressure_grid_kpa") or rd.get("pressures_kpa")
    if not grid:
        sd = _as_dict(state_dict)
        grid = (
            sd.get("pressure_grid_kpa")
            or sd.get("separation_pressure_grid_kpa")
            or _as_dict(sd.get("separation_retrofit")).get("pressure_grid_kpa")
            or _as_dict(sd.get("pressure_swing_flash_config")).get("pressure_grid_kpa")
        )
    vals: List[float] = []
    for p in _as_list(grid):
        v = _safe_float(p, None)
        if v is not None and v > 0 and v not in vals:
            vals.append(float(v))
    return vals[:4] or [100.0, 300.0, 600.0, 900.0]


def _pair_keys_for_demo_plot(results: Optional[dict] = None, state_dict: Optional[dict] = None) -> List[str]:
    keys = _plot_component_pairs_from_results(_as_dict(results), max_pairs=3) if results else []
    if keys:
        return keys[:3]
    comps = non_solvent_components_from_state(_as_dict(state_dict)) if state_dict else []
    pairs = _component_pairs(comps)
    keys = ["/".join(p) for p in pairs[:3]]
    return keys or ["A/B", "B/C", "A/C"]


def _draw_demo_reference_triptych(fig, axes, pair_keys: Sequence[str], pressure_grid: Sequence[float]) -> None:
    """Draw the old demo-style y-x triptych without using it as page output."""
    import numpy as np
    xs = np.linspace(0.0, 1.0, 90)
    for ax, pair_key in zip(axes, pair_keys):
        ax.plot([0, 1], [0, 1], linestyle="--", linewidth=1.0, label="y = x")
        for idx, p in enumerate(list(pressure_grid)[:4]):
            amp = 0.18 - 0.035 * idx
            ys = xs + amp * np.sin(np.pi * xs) * (1.0 - 0.15 * idx)
            ys = np.clip(ys, 0.0, 1.0)
            ax.plot(xs, ys, linewidth=1.35, label=f"{float(p):g} kPa")
        if "/" in pair_key:
            c1, c2 = pair_key.split("/", 1)
        else:
            c1, c2 = pair_key, "B"
        ax.set_title(f"{c1} / {c2}", fontsize=12)
        ax.set_xlabel(f"x({c1}) liquid", fontsize=10.5)
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.tick_params(direction="in", labelsize=9.5)
        ax.grid(alpha=0.18)
        for spine in ax.spines.values():
            spine.set_linewidth(0.9)
    if axes:
        axes[0].set_ylabel("y vapor", fontsize=10.5)


def save_separation_page_demo_reference_yx_triptych_plot(
        results: Optional[dict] = None,
        *,
        state_dict: Optional[dict] = None,
        out_dir: Optional[str] = None,
        filename: str = "separation_pressure_screen_yx_triptych_demo_reference.png",
        logger: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """Save the old demo/test y-x triptych into an archive folder.

    This plot is intentionally *not* used by the GUI display path.  It is kept
    only as a demo/reference artifact for comparison and debugging.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:
        if callable(logger):
            logger(f"[PLOT-WARN] matplotlib unavailable; cannot save demo y-x reference: {exc}")
        return None

    base_dir = _separation_page_artifact_dir(out_dir)
    demo_dir = os.path.join(base_dir, "separation_demo_reference")
    os.makedirs(demo_dir, exist_ok=True)
    out_path = os.path.join(demo_dir, filename)
    pair_keys = _pair_keys_for_demo_plot(results, state_dict=state_dict)[:3]
    pressure_grid = _pressure_grid_for_demo_plot(results, state_dict=state_dict)
    n = max(1, min(3, len(pair_keys)))

    plt.rcParams["font.family"] = "Times New Roman"
    plt.rcParams["mathtext.fontset"] = "stix"
    fig, axes = plt.subplots(1, n, figsize=(4.8 * n, 4.0), dpi=220, sharex=True, sharey=True)
    if n == 1:
        axes = [axes]
    else:
        axes = list(axes)
    _draw_demo_reference_triptych(fig, axes, pair_keys[:n], pressure_grid)
    handles, labels = axes[0].get_legend_handles_labels() if axes else ([], [])
    if handles:
        fig.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.5, 0.985),
                   ncol=min(5, len(labels)), frameon=False, fontsize=8.5)
    fig.suptitle("Demo reference y-x triptych (not displayed)", fontsize=13, y=1.03)
    fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.88))
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    msg = f"[PLOT] save non-displayed demo y-x reference: {out_path}"
    if callable(logger):
        logger(msg)
    else:
        try:
            print(msg, flush=True)
        except Exception:
            pass
    return out_path


def save_separation_page_yx_triptych_plot(
        results: dict,
        *,
        out_dir: Optional[str] = None,
        filename: str = "separation_pressure_screen_yx_triptych.png",
        logger: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """Save the clean separation-page y-x triptych from runner results.

    This page figure is runner-owned and overwritten after each Flash2 run.  It
    shows one consolidated curve per pressure for each component pair.  Internal
    duplicate runner cases are merged so the legend remains readable.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:
        if callable(logger):
            logger(f"[PLOT-WARN] matplotlib unavailable; cannot save y-x triptych: {exc}")
        return None

    rd = _as_dict(_as_dict(results).get("binary_triptych_results") or results)
    out_dir = _separation_page_artifact_dir(out_dir)
    out_path = os.path.join(out_dir, filename)
    pair_keys = _plot_component_pairs_from_results(rd, max_pairs=3)
    if not pair_keys:
        pair_keys = ["A/B", "A/C", "B/C"]

    n = max(1, min(3, len(pair_keys)))
    plt.rcParams["font.family"] = "Times New Roman"
    plt.rcParams["mathtext.fontset"] = "stix"
    fig, axes = plt.subplots(1, n, figsize=(4.9 * n, 4.15), dpi=220, sharex=True, sharey=True)
    if n == 1:
        axes = [axes]
    else:
        axes = list(axes)

    plotted_any = False
    legend_by_label: Dict[str, Any] = {}

    for ax, pair_key in zip(axes, pair_keys[:n]):
        diag, = ax.plot([0.0, 1.0], [0.0, 1.0], linestyle="--", linewidth=1.0, label="y = x")
        legend_by_label.setdefault("y = x", diag)

        pressure_groups = _group_plot_cases_by_pressure(rd, pair_key)
        pressure_items = sorted(pressure_groups.items(), key=lambda kv: kv[0])
        for p_kpa, raw_points in pressure_items:
            pts = _aggregate_xy_points(raw_points)
            if len(pts) < 3:
                continue
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            label = f"{p_kpa:g} kPa" if p_kpa >= 0 else "unknown P"
            line, = ax.plot(xs, ys, linewidth=1.55, label=label)
            legend_by_label.setdefault(label, line)
            # Sparse markers: enough to show sampled Aspen points without clutter.
            step = max(1, len(pts) // 8)
            marker_pts = pts[1:-1:step]
            if marker_pts:
                ax.plot([p[0] for p in marker_pts], [p[1] for p in marker_pts],
                        marker="o", linestyle="none", markersize=2.4, color=line.get_color())
            cross = _plot_crossing_x(pts)
            if cross is not None and 0.02 < cross < 0.98:
                ax.scatter([cross], [cross], s=34, marker="s", facecolors="white", edgecolors="black", linewidths=0.8, zorder=6)
            plotted_any = True

        if not pressure_items:
            ax.text(
                0.05, 0.92,
                "No valid Flash2 y-x data\nfor this pair",
                transform=ax.transAxes,
                fontsize=9,
                va="top",
                bbox=dict(boxstyle="round", fc="white", ec="0.7"),
            )

        if "/" in pair_key:
            c1, c2 = pair_key.split("/", 1)
        else:
            c1, c2 = pair_key, "B"
        ax.set_title(f"{c1} / {c2}", fontsize=12)
        ax.set_xlabel(f"x({c1}) liquid", fontsize=10.5)
        ax.set_xlim(0.0, 1.0)
        ax.set_ylim(0.0, 1.0)
        ax.tick_params(direction="in", labelsize=9.5)
        ax.grid(alpha=0.16)
        for spine in ax.spines.values():
            spine.set_linewidth(0.9)

    if axes:
        axes[0].set_ylabel("y vapor", fontsize=10.5)

    labels = list(legend_by_label.keys())
    handles = [legend_by_label[k] for k in labels]
    if handles:
        # One shared legend prevents the repeated-pressure legend columns that
        # made the previous figure unreadable.
        fig.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.5, 0.985),
                   ncol=min(6, len(labels)), frameon=False, fontsize=8.5)

    title = "Pressure-swing y-x triptych from Aspen Flash2" if plotted_any else "Pressure-swing y-x triptych: no valid Flash2 points"
    fig.suptitle(title, fontsize=13, y=1.04)
    fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.86))
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    if callable(logger):
        logger(f"[PLOT] overwrite clean separation y-x triptych: {out_path}")
    else:
        try:
            print(f"[PLOT] overwrite clean separation y-x triptych: {out_path}", flush=True)
        except Exception:
            pass
    return out_path


def save_separation_page_ternary_plot(
        results: dict,
        *,
        out_dir: Optional[str] = None,
        filename: str = "separation_pressure_screen_ternary.png",
        logger: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """Save a fixed-name ternary trend image when ternary data is available."""
    out_dir = _separation_page_artifact_dir(out_dir)
    fixed_path = os.path.join(out_dir, filename)
    try:
        path = plot_ternary_phase_trend_diagram(results, out_dir=out_dir)
        if path and os.path.exists(path):
            try:
                import shutil
                shutil.copyfile(path, fixed_path)
                try:
                    os.remove(path)
                except Exception:
                    pass
                if callable(logger):
                    logger(f"[PLOT] overwrite separation ternary plot: {fixed_path}")
                return fixed_path
            except Exception:
                return path
    except Exception as exc:
        if callable(logger):
            logger(f"[PLOT-WARN] ternary plot skipped: {exc}")
    return None


def save_separation_page_final_plots(
        state_dict: dict,
        results: dict,
        *,
        out_dir: Optional[str] = None,
        logger: Optional[Callable[[str], None]] = None,
) -> Dict[str, str]:
    """Generate all final separation-page plots and write their paths to state.

    This is the single runner-owned plot bridge used by the GUI.  It should be
    called after every successful or partial Flash2 screen, and it overwrites
    the latest plot files rather than producing timestamped refresh artifacts.
    """
    state = _as_dict(state_dict)
    plot_paths: Dict[str, str] = {}
    yx = save_separation_page_yx_triptych_plot(results, out_dir=out_dir, logger=logger)
    if yx:
        plot_paths["yx_triptych"] = yx
    demo_yx = save_separation_page_demo_reference_yx_triptych_plot(
        results,
        state_dict=state,
        out_dir=out_dir,
        logger=logger,
    )
    if demo_yx:
        # Saved for demo/testing comparison only; GUI display code must not use this key.
        plot_paths["demo_reference_yx_triptych"] = demo_yx
    ternary = save_separation_page_ternary_plot(results, out_dir=out_dir, logger=logger)
    if ternary:
        plot_paths["ternary"] = ternary

    pkg = _as_dict(state.get("separation_retrofit"))
    old_paths = _as_dict(pkg.get("plot_paths"))
    old_paths.update(plot_paths)
    pkg["plot_paths"] = old_paths
    pkg["runner_plot_owner"] = True
    pkg["runner_plot_version"] = RUNNER_VERSION
    pkg["runner_plot_updated_at"] = datetime.now().isoformat(timespec="seconds")
    state["separation_retrofit"] = pkg
    return plot_paths


# ---------------------------------------------------------------------------
# Public orchestration helpers
# ---------------------------------------------------------------------------

def open_aspen_document(
        template_path: str,
        *,
        aspen_version: str = "Apwn.Document.40.0",
        visible: bool = False,
        suppress_dialogs: bool = True,
) -> Any:
    try:
        import win32com.client as win32  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            "win32com is not available. Run this on Windows with pywin32 and Aspen Plus installed.") from exc

    template_path = os.path.abspath(os.path.expanduser(os.path.expandvars(str(template_path or ""))))
    try:
        print(f"[ASPEN_OPEN] requested template_path={template_path}", flush=True)
    except Exception:
        pass
    if not os.path.isfile(template_path):
        raise FileNotFoundError(f"Aspen template file not found: {template_path}")

    try:
        print(f"[ASPEN_OPEN] DispatchEx({aspen_version})", flush=True)
    except Exception:
        pass
    aspen = win32.DispatchEx(aspen_version)
    lower = template_path.lower()
    try:
        if lower.endswith((".bkp", ".aprbkp", ".apwz")):
            try:
                print("[ASPEN_OPEN] InitFromArchive2", flush=True)
            except Exception:
                pass
            aspen.InitFromArchive2(template_path)
        else:
            try:
                print("[ASPEN_OPEN] InitFromFile2", flush=True)
            except Exception:
                pass
            aspen.InitFromFile2(template_path)
        try:
            print("[ASPEN_OPEN] Aspen document opened", flush=True)
        except Exception:
            pass
    except Exception as exc:
        try:
            aspen.Close(False)
        except Exception:
            pass
        raise RuntimeError(
            "Aspen 无法打开模板文件。请确认路径是绝对路径、文件未被占用、Aspen 版本可打开该文件，"
            f"并避免把模板放在需要权限/同步锁的目录。template_path={template_path}; 原始错误={exc}"
        ) from exc

    try:
        aspen.Visible = bool(visible)
    except Exception:
        pass
    try:
        aspen.SuppressDialogs = 1 if suppress_dialogs else 0
    except Exception:
        pass
    return aspen



def _case_quality_key(case: dict) -> str:
    cd = _as_dict(case)
    mode = "EPSD" if bool(cd.get("with_solvent") or cd.get("solvent")) else "PSD"
    pair = "/".join(_canonical_component_name(x) for x in _as_list(cd.get("pair"))[:2])
    sfr = cd.get("solvent_to_feed_ratio")
    return f"{mode}:{pair}" + (f":SFR={sfr}" if mode == "EPSD" else "")


def build_pressure_screen_data_quality(results: dict) -> dict:
    cases = [_as_dict(c) for c in _as_list(_as_dict(results).get("cases"))]
    valid_points = sum(int(_safe_float(c.get("valid_point_count"), len(_as_list(c.get("points")))) or 0) for c in cases)
    failed_points = sum(int(_safe_float(c.get("failed_point_count"), len(_as_list(c.get("failed_points")))) or 0) for c in cases)
    complete_cases = sum(1 for c in cases if str(c.get("status") or "") == "completed")
    partial_cases = sum(1 for c in cases if str(c.get("status") or "").startswith("partial"))
    failed_cases = sum(1 for c in cases if str(c.get("status") or "").startswith("failed"))
    execution_failure_count = len(_as_list(_as_dict(results).get("failed_cases")))
    groups: Dict[str, dict] = {}
    for c in cases:
        key = _case_quality_key(c)
        row = groups.setdefault(key, {
            "group_key": key,
            "mode": "EPSD" if bool(c.get("with_solvent") or c.get("solvent")) else "PSD",
            "pair": _as_list(c.get("pair"))[:2],
            "solvent_to_feed_ratio": c.get("solvent_to_feed_ratio"),
            "case_count": 0,
            "valid_point_count": 0,
            "failed_point_count": 0,
            "usable_pressure_levels_kpa": [],
            "blocked_pressure_levels_kpa": [],
            "issue_counts": {},
        })
        row["case_count"] += 1
        vp = int(_safe_float(c.get("valid_point_count"), len(_as_list(c.get("points")))) or 0)
        fp = int(_safe_float(c.get("failed_point_count"), len(_as_list(c.get("failed_points")))) or 0)
        row["valid_point_count"] += vp
        row["failed_point_count"] += fp
        p = _safe_float(c.get("pressure_kpa"), None)
        if p is not None:
            (row["usable_pressure_levels_kpa"] if vp >= 2 else row["blocked_pressure_levels_kpa"]).append(float(p))
        for issue, count in _as_dict(_as_dict(c.get("data_quality")).get("issue_counts")).items():
            row["issue_counts"][issue] = row["issue_counts"].get(issue, 0) + int(count or 0)
    group_rows = list(groups.values())
    for row in group_rows:
        row["usable_for_pressure_inference"] = len(set(row["usable_pressure_levels_kpa"])) >= 2
        row["usable_pressure_levels_kpa"] = sorted(set(row["usable_pressure_levels_kpa"]))
        row["blocked_pressure_levels_kpa"] = sorted(set(row["blocked_pressure_levels_kpa"]))
    total_expected = valid_points + failed_points
    usable_groups = [r for r in group_rows if r.get("usable_for_pressure_inference")]
    return {
        "schema_version": "pressure-screen-data-quality-v1",
        "status": (
            "validated" if cases and failed_points == 0
            else "partial_validated" if valid_points > 0
            else "blocked_no_valid_points"
        ),
        "valid_point_count": valid_points,
        "invalid_point_count": failed_points,
        "expected_point_count": total_expected,
        "valid_point_fraction": (valid_points / total_expected) if total_expected else 0.0,
        "completed_case_count": complete_cases,
        "partial_case_count": partial_cases,
        "failed_case_count": failed_cases,
        "execution_failure_count": execution_failure_count,
        "usable_group_count": len(usable_groups),
        "groups": group_rows,
    }


def build_separation_agent_handoff(results: dict) -> dict:
    rd = _as_dict(results)
    quality = _as_dict(rd.get("data_quality")) or build_pressure_screen_data_quality(rd)
    pressure_opt = copy.deepcopy(_as_dict(rd.get("pressure_optimization")))
    valid_recs = []
    for rec in _as_list(pressure_opt.get("pair_recommendations")):
        rr = copy.deepcopy(_as_dict(rec))
        metrics = _as_list(rr.get("pressure_metrics"))
        valid_levels = [m for m in metrics if int(_safe_float(_as_dict(m).get("point_count"), 0) or 0) >= 2]
        if len({float(_safe_float(_as_dict(m).get("pressure_kpa"), 0.0) or 0.0) for m in valid_levels}) >= 2:
            rr["quality_verified"] = True
            rr["pressure_metrics"] = [
                {
                    "pressure_kpa": _safe_float(_as_dict(m).get("pressure_kpa"), None),
                    "quality": _safe_float(_as_dict(m).get("quality"), None),
                    "alpha_mean": _safe_float(_as_dict(m).get("alpha_mean"), None),
                    "mean_abs_y_minus_x": _safe_float(_as_dict(m).get("mean_abs_y_minus_x"), None),
                    "crossing_x": _safe_float(_as_dict(m).get("crossing_x"), None),
                    "point_count": int(_safe_float(_as_dict(m).get("point_count"), 0) or 0),
                }
                for m in valid_levels
            ]
            valid_recs.append(rr)
    pressure_opt["pair_recommendations"] = valid_recs
    pressure_opt["recommended_priority_pairs"] = [
        r.get("pair_key") for r in valid_recs if r.get("should_optimize_pressure")
    ]
    pressure_opt["status"] = "ready" if valid_recs else "no_valid_pair_pressure_data"

    psd = [r for r in valid_recs if str(r.get("mode") or "").upper() == "PSD"]
    epsd = [r for r in valid_recs if str(r.get("mode") or "").upper() == "EPSD"]
    psd_score = sum(_safe_float(r.get("sensitivity_score"), 0.0) or 0.0 for r in psd if r.get("should_optimize_pressure"))
    epsd_score = sum(_safe_float(r.get("sensitivity_score"), 0.0) or 0.0 for r in epsd if r.get("should_optimize_pressure"))
    if epsd_score > psd_score * 1.05 and epsd_score > 0:
        route = "EPSD"
    elif psd_score > 0:
        route = "PSD"
    else:
        route = "NO_PRESSURE_ROUTE_YET"
    blocked_groups = [r for r in _as_list(quality.get("groups")) if not _as_dict(r).get("usable_for_pressure_inference")]
    return {
        "schema_version": "pressure-screen-separation-agent-handoff-v1",
        "source_runner_version": rd.get("runner_version") or RUNNER_VERSION,
        "created_at": rd.get("created_at") or datetime.now().isoformat(timespec="seconds"),
        "status": "ready" if valid_recs else "blocked",
        "can_enter_separation_agent": bool(valid_recs),
        "recommended_route": route,
        "data_quality_status": quality.get("status"),
        "pressure_optimization": pressure_opt,
        "validated_pair_recommendations": valid_recs,
        "blocked_groups": blocked_groups,
        "selected_pairs_for_epsd": rd.get("selected_pairs_for_epsd"),
        "consumer_contract": {
            "full_results_state_key": "pressure_swing_flash_results",
            "pressure_bounds_state_key": "flash_pressure_optimization",
            "compact_handoff_state_key": "pressure_screen_handoff",
            "rule": "Downstream separation logic must use only quality_verified pair recommendations.",
        },
        "artifact_paths": copy.deepcopy(_as_dict(rd.get("plot_paths"))),
    }


def finalize_pressure_screen_results(results: dict) -> dict:
    rd = _as_dict(results)
    quality = build_pressure_screen_data_quality(rd)
    rd["data_quality"] = quality
    rd["valid_point_count"] = quality.get("valid_point_count", 0)
    rd["invalid_point_count"] = quality.get("invalid_point_count", 0)
    rd["failed_case_count"] = (
        quality.get("failed_case_count", 0)
        + quality.get("partial_case_count", 0)
        + quality.get("execution_failure_count", 0)
    )
    if quality.get("valid_point_count", 0) <= 0:
        rd["status"] = "failed_data_quality"
    elif quality.get("invalid_point_count", 0) > 0 or quality.get("execution_failure_count", 0) > 0:
        rd["status"] = "partial_invalid_data"
    else:
        rd["status"] = "completed"
    rd["curve_analysis"] = build_flash_curve_analysis(rd)
    rd["pressure_optimization"] = build_flash_pressure_optimization_recommendations(rd)
    rd["separation_agent_handoff"] = build_separation_agent_handoff(rd)
    # Keep the legacy state handoff clean as well.
    rd["pressure_optimization"] = copy.deepcopy(
        _as_dict(rd["separation_agent_handoff"]).get("pressure_optimization")
    )
    return rd


def _merge_flash_stage_results(stage_results: Sequence[Tuple[str, dict]], *, scan_profile: str) -> dict:
    """Merge sequential PSD/EPSD runs performed by the same runner instance."""
    cases: List[dict] = []
    failed_cases: List[dict] = []
    logs: List[str] = []
    workload = {
        "case_count": 0,
        "composition_point_count": 0,
        "minimum_aspen_runs": 0,
        "maximum_aspen_runs": 0,
        "stages": [],
    }
    last_actual_run_count = 0
    runner_version = RUNNER_VERSION
    created_at = datetime.now().isoformat(timespec="seconds")
    registry_snapshots = []
    for label, result in stage_results:
        rd = _as_dict(result)
        runner_version = str(rd.get("runner_version") or runner_version)
        created_at = str(rd.get("created_at") or created_at)
        if rd.get("component_registry"):
            registry_snapshots.append(copy.deepcopy(rd.get("component_registry")))
        for case in _as_list(rd.get("cases")):
            cc = copy.deepcopy(_as_dict(case))
            cc["route_payload_label"] = label
            cases.append(cc)
        for failure in _as_list(rd.get("failed_cases")):
            ff = copy.deepcopy(_as_dict(failure))
            ff["route_payload_label"] = label
            failed_cases.append(ff)
        logs = _as_list(rd.get("log")) or logs
        last_actual_run_count = max(last_actual_run_count, int(_safe_float(rd.get("actual_aspen_run_count"), 0) or 0))
        w = _as_dict(rd.get("workload_estimate"))
        stage_w = {
            "label": label,
            "case_count": int(_safe_float(w.get("case_count"), 0) or 0),
            "composition_point_count": int(_safe_float(w.get("composition_point_count"), 0) or 0),
            "minimum_aspen_runs": int(_safe_float(w.get("minimum_aspen_runs"), 0) or 0),
            "maximum_aspen_runs": int(_safe_float(w.get("maximum_aspen_runs"), 0) or 0),
        }
        workload["stages"].append(stage_w)
        for key in ("case_count", "composition_point_count", "minimum_aspen_runs", "maximum_aspen_runs"):
            workload[key] += stage_w[key]

    merged = {
        "runner_version": runner_version,
        "created_at": created_at,
        "scan_profile": scan_profile,
        "execution_strategy": "sequential_psd_then_selected_pair_epsd",
        "case_count": len(cases),
        "cases": cases,
        "failed_cases": failed_cases,
        "log": logs,
        "workload_estimate": workload,
        "actual_aspen_run_count": last_actual_run_count,
        "component_registry": registry_snapshots[-1] if registry_snapshots else {},
    }
    return finalize_pressure_screen_results(merged)


def _priority_pairs_from_results(results: dict, *, limit: int = 2) -> List[List[str]]:
    summaries = _as_list(_as_dict(_as_dict(results).get("curve_analysis")).get("pair_summaries"))
    selected: List[List[str]] = []
    for summary in summaries:
        sd = _as_dict(summary)
        low = _as_dict(sd.get("low_pressure_case"))
        high = _as_dict(sd.get("high_pressure_case"))
        if int(_safe_float(low.get("point_count"), 0) or 0) < 2 or int(_safe_float(high.get("point_count"), 0) or 0) < 2:
            continue
        pair = [_canonical_component_name(c) for c in _as_list(sd.get("pair"))[:2]]
        if len(pair) == 2 and pair not in selected:
            selected.append(pair)
        if len(selected) >= max(1, int(limit)):
            break
    if selected:
        return selected
    # No quality-verified pair: do not push an arbitrary pair into EPSD.
    return []


def update_state_with_flash_results(state_dict: dict, results: dict, *,
                                    clear_stale_retrofit_package: bool = True) -> dict:
    state = _as_dict(state_dict)
    finalized = finalize_pressure_screen_results(results)
    state["pressure_swing_flash_results"] = finalized
    pressure_opt = copy.deepcopy(_as_dict(finalized.get("pressure_optimization")))
    state["flash_pressure_optimization"] = pressure_opt
    handoff = copy.deepcopy(_as_dict(finalized.get("separation_agent_handoff")))
    state["pressure_screen_handoff"] = handoff
    state["separation_agent_input"] = {
        "schema_version": "separation-agent-input-v1",
        "pressure_screen": handoff,
        "source_state_keys": [
            "pressure_swing_flash_results",
            "flash_pressure_optimization",
            "pressure_screen_handoff",
        ],
    }
    if clear_stale_retrofit_package and "separation_retrofit" in state:
        state.pop("separation_retrofit", None)
    return state

def run_pressure_flash_screen_from_state(
        state_dict: dict,
        *,
        aspen: Any = None,
        template_path: Optional[str] = None,
        aspen_version: str = "Apwn.Document.40.0",
        mode: str = "combined",
        pressure_write_mode: str = "kpa",
        pressure_grid_kpa: Optional[Sequence[float]] = None,
        x_grid: Optional[Sequence[float]] = None,
        solvent_to_feed_grid: Optional[Sequence[float]] = None,
        vapor_fraction_grid: Optional[Sequence[float]] = None,
        temperature_fallback_grid_C: Optional[Sequence[float]] = None,
        selected_pairs: Optional[Sequence[Sequence[str]]] = None,
        pair_contexts: Optional[Any] = None,
        scan_profile: str = "fast",
        adaptive_pair_limit: int = 2,
        run_native_triptych: Optional[bool] = None,
        save_plots: bool = True,
        visible: bool = False,
        suppress_dialogs: bool = True,
        logger: Optional[Callable[[str], None]] = None,
        close_aspen: bool = False,
        **runner_options: Any,
) -> Tuple[dict, dict]:
    if aspen is None:
        if not template_path:
            template_path = (
                    _as_dict(state_dict).get("template_path")
                    or _as_dict(state_dict).get("aspen_template_path")
                    or "Simulation 1.bkp"
            )
        aspen = open_aspen_document(
            str(template_path),
            aspen_version=aspen_version,
            visible=visible,
            suppress_dialogs=suppress_dialogs,
        )

    profile = str(scan_profile or "fast").strip().lower()
    use_fast = profile in {"fast", "coarse", "engineering", "screen"}
    build_kwargs = dict(
        pressure_grid_kpa=pressure_grid_kpa,
        x_grid=x_grid,
        solvent_to_feed_grid=solvent_to_feed_grid,
        vapor_fraction_grid=vapor_fraction_grid,
        temperature_fallback_grid_C=([] if use_fast and temperature_fallback_grid_C is None else temperature_fallback_grid_C),
        selected_pairs=selected_pairs,
        pair_contexts=pair_contexts,
        scan_profile=profile,
    )
    if mode == "combined":
        payload = {
            "action": "run_combined_aspen_flash2_pressure_sensitivity_screen",
            "runner_version": RUNNER_VERSION,
            "scan_profile": profile,
            "feed_condition": feed_condition_from_state(state_dict),
            "payloads": {
                "PSD": build_pressure_flash_payload_from_state(
                    state_dict, mode="solvent_free_psd", include_solvent_free_baseline=True, **build_kwargs
                ),
                "EPSD": build_pressure_flash_payload_from_state(
                    state_dict, mode="extractive_pressure_swing", include_solvent_free_baseline=False, **build_kwargs
                ),
            },
        }
    else:
        payload = build_pressure_flash_payload_from_state(
            state_dict,
            mode=mode,
            include_solvent_free_baseline=(mode == "solvent_free_psd"),
            **build_kwargs,
        )
    payload["thermo_method"] = (
        _as_dict(state_dict).get("thermo_method")
        or _as_dict(state_dict).get("property_method")
        or "NRTL"
    )
    payload["workload_estimate"] = estimate_pressure_flash_workload(payload)
    if callable(logger):
        w = payload["workload_estimate"]
        logger(
            f"[FLASH-WORKLOAD] profile={profile}; cases={w['case_count']}; "
            f"composition_points={w['composition_point_count']}; "
            f"Aspen runs estimated={w['minimum_aspen_runs']}..{w['maximum_aspen_runs']}"
        )

    if use_fast:
        runner_options.setdefault("enable_temperature_fallback", False)
        runner_options.setdefault("reuse_flash_unit", True)
        runner_options.setdefault("cache_tree_nodes", True)
        runner_options.setdefault("log_level", "normal")
    runner = AspenPressureFlashScreenRunner(
        aspen,
        logger=logger,
        pressure_write_mode=pressure_write_mode,
        default_temperature_C=feed_condition_from_state(state_dict).get("temperature_C") or 25.0,
        **runner_options,
    )
    # v40: preserve the same raw component-name registration path used by the GUI.
    # In particular, ETOAC is submitted to Aspen as "ethyl acetate"; Aspen then
    # decides the internal component row ID. No component_aliases.json edit is required.
    runner._component_registration_names = _component_registration_names_from_state(state_dict)
    runner._component_registration_specs = _component_registration_specs_from_state(state_dict)
    if use_fast and mode == "combined" and int(adaptive_pair_limit or 0) > 0 and selected_pairs is None:
        # Register the complete component environment, including solvent, before
        # creating the temporary Flash2. This prevents component-table index drift
        # between PSD and EPSD while still allowing the unit itself to be reused.
        runner.ensure_components_registered(
            components_from_state(state_dict),
            thermo_method=payload.get("thermo_method") or "NRTL",
        )
        psd_payload = _as_dict(_as_dict(payload.get("payloads")).get("PSD"))
        psd_workload = estimate_pressure_flash_workload(psd_payload)
        if callable(logger):
            logger(
                f"[FAST-STAGE PSD] cases={psd_workload['case_count']}; "
                f"Aspen runs={psd_workload['minimum_aspen_runs']}..{psd_workload['maximum_aspen_runs']}"
            )
        psd_results = runner.run_payload(psd_payload)
        priority_pairs = _priority_pairs_from_results(psd_results, limit=int(adaptive_pair_limit))
        if callable(logger):
            logger(f"[FAST-PAIR-SELECT] selected for EPSD: {priority_pairs}")
        epsd_payload = build_pressure_flash_payload_from_state(
            state_dict,
            mode="extractive_pressure_swing",
            pressure_grid_kpa=pressure_grid_kpa,
            x_grid=x_grid,
            solvent_to_feed_grid=solvent_to_feed_grid,
            vapor_fraction_grid=vapor_fraction_grid,
            temperature_fallback_grid_C=([] if temperature_fallback_grid_C is None else temperature_fallback_grid_C),
            selected_pairs=priority_pairs,
            scan_profile=profile,
            include_solvent_free_baseline=False,
        )
        epsd_workload = estimate_pressure_flash_workload(epsd_payload)
        if callable(logger):
            logger(
                f"[FAST-STAGE EPSD] pairs={priority_pairs}; cases={epsd_workload['case_count']}; "
                f"Aspen runs={epsd_workload['minimum_aspen_runs']}..{epsd_workload['maximum_aspen_runs']}"
            )
        if epsd_workload["case_count"] > 0:
            epsd_results = runner.run_payload(epsd_payload)
            results = _merge_flash_stage_results(
                [("PSD", psd_results), ("EPSD", epsd_results)], scan_profile=profile
            )
        else:
            results = _merge_flash_stage_results([("PSD", psd_results)], scan_profile=profile)
        results["selected_pairs_for_epsd"] = priority_pairs
    else:
        results = runner.run_payload(payload)

    # v34: the separation-page y-x plot and PSD pressure windows should use the
    # same native binary-triptych scan that was validated in the demo.  The
    # combined scan is still kept for EPSD/solvent cases, but the displayed
    # triptych and solvent-free PSD sequence logic are driven by this native
    # binary scan to avoid losing y=x crossings through the generic combined path.
    triptych_results = {}
    if run_native_triptych is None:
        run_native_triptych = not use_fast
    try:
        if mode == "combined" and bool(run_native_triptych):
            if callable(logger):
                logger("[TRIPTYCH] running native binary y-x triptych scan for page plot and PSD sequence inference")
            triptych_results = run_binary_triptych_pressure_flash_screen(
                state_dict,
                aspen=aspen,
                template_path=template_path,
                aspen_version=aspen_version,
                pressure_grid_kpa=pressure_grid_kpa or DEFAULT_PRESSURE_GRID_KPA,
                pressure_write_mode=DEFAULT_BINARY_TRIPTYCH_PRESSURE_WRITE_MODE,
                logger=logger,
                close_aspen=False,
                **runner_options,
            )
            results["binary_triptych_results"] = triptych_results
            # Merge native PSD binary cases with solvent/EPSD cases for the BO
            # pressure-window handoff.  PSD must use the native triptych; EPSD
            # still uses solvent-containing cases from the combined scan.
            solvent_cases = [c for c in _as_list(results.get("cases")) if bool(_as_dict(c).get("with_solvent") or _as_dict(c).get("solvent") or "solvent" in str(_as_dict(c).get("case_type", "")).lower())]
            hybrid = copy.deepcopy(results)
            hybrid["cases"] = _as_list(_as_dict(triptych_results).get("cases")) + solvent_cases
            hybrid["pressure_optimization"] = build_flash_pressure_optimization_recommendations(hybrid)
            results["pressure_optimization"] = hybrid["pressure_optimization"]
            if callable(logger):
                for line in _as_list(_as_dict(results.get("pressure_optimization")).get("summary_lines")):
                    logger(f"[PRESSURE-BO-RANGE-NATIVE] {line}")
    except Exception as exc:
        if callable(logger):
            logger(f"[TRIPTYCH-WARN] native binary triptych scan failed; fallback to combined result: {exc}")
        else:
            try:
                print(f"[TRIPTYCH-WARN] native binary triptych scan failed; fallback to combined result: {exc}", flush=True)
            except Exception:
                pass

    results = finalize_pressure_screen_results(results)
    update_state_with_flash_results(state_dict, results)
    runner_plot_paths: Dict[str, str] = {}
    try:
        if save_plots:
            runner_plot_paths = save_separation_page_final_plots(state_dict, results, logger=logger)
            if runner_plot_paths:
                results.setdefault("plot_paths", {}).update(runner_plot_paths)
                results["separation_agent_handoff"] = build_separation_agent_handoff(results)
                results["separation_agent_handoff"].setdefault("artifact_paths", {}).update(runner_plot_paths)
                update_state_with_flash_results(
                    state_dict, results, clear_stale_retrofit_package=False
                )
    except Exception as exc:
        if callable(logger):
            logger(f"[PLOT-WARN] failed to save runner-owned separation plots: {exc}")
        else:
            try:
                print(f"[PLOT-WARN] failed to save runner-owned separation plots: {exc}", flush=True)
            except Exception:
                pass

    if close_aspen:
        try:
            aspen.Close(False)
        except Exception:
            pass
        try:
            aspen.Quit()
        except Exception:
            pass

    return state_dict, results


def run_pressure_flash_payload(
        payload: dict,
        *,
        aspen: Any = None,
        template_path: Optional[str] = None,
        aspen_version: str = "Apwn.Document.40.0",
        pressure_write_mode: str = "kpa",
        logger: Optional[Callable[[str], None]] = None,
        close_aspen: bool = False,
        **runner_options: Any,
) -> dict:
    if aspen is None:
        if not template_path:
            raise ValueError("template_path is required when aspen is not provided.")
        aspen = open_aspen_document(str(template_path), aspen_version=aspen_version)

    runner = AspenPressureFlashScreenRunner(
        aspen,
        logger=logger,
        pressure_write_mode=pressure_write_mode,
        **runner_options,
    )
    results = runner.run_payload(payload)

    if close_aspen:
        try:
            aspen.Close(False)
        except Exception:
            pass
        try:
            aspen.Quit()
        except Exception:
            pass
    return results


def run_binary_triptych_pressure_flash_screen(
        state_dict: dict,
        *,
        aspen: Any = None,
        template_path: Optional[str] = None,
        aspen_version: str = "Apwn.Document.40.0",
        pressure_grid_kpa: Optional[Sequence[float]] = None,
        x_grid: Optional[Sequence[float]] = None,
        vapor_fraction_grid: Optional[Sequence[float]] = None,
        pressure_write_mode: str = DEFAULT_BINARY_TRIPTYCH_PRESSURE_WRITE_MODE,
        logger: Optional[Callable[[str], None]] = None,
        close_aspen: bool = False,
        **runner_options: Any,
) -> dict:
    """
    Run the native binary all-pair scan used for the 1x3 y-x figure.

    The helper exists to prevent the common mistake of generating a 1x3 plot
    from a different scan path or a different Aspen pressure-write convention.
    """
    payload = build_binary_triptych_pressure_flash_payload_from_state(
        state_dict,
        pressure_grid_kpa=pressure_grid_kpa,
        x_grid=x_grid,
        vapor_fraction_grid=vapor_fraction_grid,
        pressure_write_mode=pressure_write_mode,
    )
    return run_pressure_flash_payload(
        payload,
        aspen=aspen,
        template_path=template_path,
        aspen_version=aspen_version,
        pressure_write_mode=str(pressure_write_mode or DEFAULT_BINARY_TRIPTYCH_PRESSURE_WRITE_MODE).lower(),
        logger=logger,
        close_aspen=close_aspen,
        **runner_options,
    )


def save_flash_results_json(results: dict, out_dir: str = "/mnt/data",
                            prefix: str = "pressure_swing_flash_results") -> str:
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(out_dir, f"{prefix}_{ts}.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_safe_json_dumps(results, indent=2))
    return path


def save_flash_curve_analysis_markdown(results: dict, out_dir: str = ".",
                                       prefix: str = "pressure_swing_flash_analysis") -> str:
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(out_dir, f"{prefix}_{ts}.md")
    analysis = _as_dict(_as_dict(results).get("curve_analysis"))
    lines = ["# Aspen Flash curve analysis", ""]
    for s in _as_list(analysis.get("summary_lines")):
        lines.append(f"- {s}")
    lines.append("")
    lines.append("## Pair summaries")
    lines.append("")
    for item in _as_list(analysis.get("pair_summaries")):
        item = _as_dict(item)
        lines.append(f"### {item.get('pair_key')}")
        lines.append(f"- route_hint: {item.get('route_hint')}")
        lines.append(f"- recommended_for_psd_screen: {item.get('recommended_for_psd_screen')}")
        lines.append(f"- pressure_sensitivity_score: {item.get('pressure_sensitivity_score')}")
        lines.append(f"- crossing_shift: {item.get('crossing_shift')}")
        lines.append(f"- alpha_mean_change: {item.get('alpha_mean_change')}")
        for g in _as_list(item.get('guidance')):
            lines.append(f"  - {g}")
        lines.append("")
    with open(path, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))
    return path


# ---------------------------------------------------------------------------
# Integrated ternary Flash-derived phase-map and topology-proxy tools
# ---------------------------------------------------------------------------

TERNARY_DEFAULT_COMPONENTS = ["METHANOL", "THF", "ETHANOL"]
TERNARY_DEFAULT_PRESSURE_GRID_KPA = [30.0, 50.0, 70.0, 101.325, 300.0]
TERNARY_DEFAULT_VAPOR_FRACTION_GRID = [0.10]
TERNARY_DEFAULT_GRID_DIVISIONS = 5
TERNARY_DEFAULT_TOTAL_FLOW = 100.0


def _ternary_clean_component(name: Any) -> str:
    canon = _canonical_component_name(name)
    return canon if canon else str(name or "").upper().strip().replace(" ", "").replace("-", "_")


def _ternary_json_default(obj: Any) -> Any:
    try:
        if hasattr(obj, "item"):
            return obj.item()
    except Exception:
        pass
    return str(obj)


def save_json(obj: dict, out_dir: str, prefix: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(out_dir, f"{prefix}_{ts}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=_ternary_json_default)
    return path


def _ternary_mean(values: Sequence[float]) -> Optional[float]:
    vals = [float(v) for v in values if v is not None and not math.isnan(float(v))]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _ternary_norm(vec: Sequence[float]) -> float:
    return math.sqrt(sum(float(v) ** 2 for v in vec))


def _ternary_normalize_triplet(values: Dict[str, float], components: Sequence[str]) -> Optional[Dict[str, float]]:
    comps = [_ternary_clean_component(c) for c in components]
    vals = {c: float(values.get(c) or 0.0) for c in comps}
    s = sum(vals.values())
    if s <= 1e-15:
        return None
    return {c: vals[c] / s for c in comps}


def _ternary_triplet_to_list(values: Dict[str, float], components: Sequence[str]) -> List[float]:
    comps = [_ternary_clean_component(c) for c in components]
    return [float(values.get(c) or 0.0) for c in comps]


def ternary_grid(
        divisions: int = TERNARY_DEFAULT_GRID_DIVISIONS,
        min_fraction: float = 0.0,
        min_component_fraction: Optional[float] = None,
) -> List[Tuple[float, float, float, Tuple[int, int, int]]]:
    n = max(2, int(divisions))
    min_frac = float(min_component_fraction if min_component_fraction is not None else min_fraction)
    out: List[Tuple[float, float, float, Tuple[int, int, int]]] = []
    for i in range(n + 1):
        for j in range(n + 1 - i):
            k = n - i - j
            a, b, c = i / n, j / n, k / n
            if min(a, b, c) + 1e-12 < min_frac:
                continue
            out.append((a, b, c, (i, j, k)))
    return out


def ternary_to_xy(a: float, b: float, c: float) -> Tuple[float, float]:
    return b + 0.5 * c, (math.sqrt(3.0) / 2.0) * c


def dict_to_xy(comp_dict: Dict[str, float], components: Sequence[str]) -> Tuple[float, float]:
    a, b, c = _ternary_triplet_to_list(comp_dict, components)
    return ternary_to_xy(a, b, c)


def _dominant_key(d: Dict[str, float]) -> Optional[str]:
    if not d:
        return None
    return max(d, key=lambda k: float(d.get(k) or 0.0))


def _component_delta(y: Dict[str, float], x: Dict[str, float], components: Sequence[str]) -> Dict[str, float]:
    return {_ternary_clean_component(c): float(y.get(_ternary_clean_component(c)) or 0.0) - float(
        x.get(_ternary_clean_component(c)) or 0.0) for c in components}


def _k_values(y: Dict[str, float], x: Dict[str, float], components: Sequence[str], eps: float = 1e-12) -> Dict[
    str, Optional[float]]:
    out: Dict[str, Optional[float]] = {}
    for c in components:
        cc = _ternary_clean_component(c)
        xv = float(x.get(cc) or 0.0)
        yv = float(y.get(cc) or 0.0)
        out[cc] = (yv / xv) if xv > eps else None
    return out


def _dominant_k_component(k_values: Dict[str, Optional[float]]) -> Optional[str]:
    vals = {k: v for k, v in k_values.items() if v is not None}
    if not vals:
        return None
    return max(vals, key=lambda k: float(vals[k] or 0.0))


class AspenTernaryFlashRunner:
    def __init__(
            self,
            aspen: Any,
            *,
            components: Sequence[str] = TERNARY_DEFAULT_COMPONENTS,
            pressure_write_mode: str = "atm",
            total_flow: float = TERNARY_DEFAULT_TOTAL_FLOW,
            feed_temperature_C: float = 25.0,
            feed_pressure_kpa: float = 101.325,
            logger: Optional[Callable[[str], None]] = None,
            base_runner_options: Optional[dict] = None,
    ) -> None:
        comps = [_ternary_clean_component(c) for c in components]
        if len(comps) != 3:
            raise ValueError("Ternary runner requires exactly three plotted components.")
        self.components = comps
        self.aspen = aspen
        self.total_flow = float(total_flow)
        self.feed_temperature_C = float(feed_temperature_C)
        self.feed_pressure_kpa = float(feed_pressure_kpa)
        self.logger = logger
        self.log_lines: List[str] = []
        opts = dict(base_runner_options or {})
        self.base = AspenPressureFlashScreenRunner(
            aspen,
            logger=self.log,
            pressure_write_mode=pressure_write_mode,
            total_non_solvent_flow=total_flow,
            default_temperature_C=feed_temperature_C,
            **opts,
        )

    def log(self, msg: str) -> None:
        text = str(msg)
        self.log_lines.append(text)
        if callable(self.logger):
            self.logger(text)
        else:
            print(text)

    def _build_flow_dict(self, z: Sequence[float], *, solvent: Optional[str] = None,
                         solvent_to_feed_ratio: float = 0.0) -> Dict[str, float]:
        flows = {c: float(v) * self.total_flow for c, v in zip(self.components, z)}
        solv = _ternary_clean_component(solvent) if solvent else ""
        if solv and float(solvent_to_feed_ratio or 0.0) > 0:
            flows[solv] = self.total_flow * float(solvent_to_feed_ratio)
        return flows

    def _record_point(
            self,
            *,
            pressure_kpa: float,
            vapor_fraction: float,
            feed_z: Dict[str, float],
            grid_index: Tuple[int, int, int],
            liq_stream: str,
            vap_stream: str,
            flows: Dict[str, float],
            with_solvent: bool = False,
            solvent: Optional[str] = None,
            solvent_to_feed_ratio: float = 0.0,
    ) -> dict:
        read_components = list(self.components)
        if with_solvent and solvent:
            solv = _ternary_clean_component(solvent)
            if solv not in read_components:
                read_components.append(solv)
        liq_flow = self.base.read_stream_total_flow(liq_stream)
        vap_flow = self.base.read_stream_total_flow(vap_stream)
        x_raw = self.base.read_stream_molefrac(liq_stream, read_components, phase_hint="liquid")
        y_raw = self.base.read_stream_molefrac(vap_stream, read_components, phase_hint="vapor")
        x = _ternary_normalize_triplet(x_raw, self.components)
        y = _ternary_normalize_triplet(y_raw, self.components)
        two_phase = liq_flow > 1e-12 and vap_flow > 1e-12 and x is not None and y is not None
        if not two_phase:
            return {
                "status": "no_two_phase_or_no_composition",
                "pressure_kpa": pressure_kpa,
                "vapor_fraction_spec": vapor_fraction,
                "feed_z": feed_z,
                "grid_index": list(grid_index),
                "input_flows": flows,
                "liquid_flow": liq_flow,
                "vapour_flow": vap_flow,
                "liquid_mole_fraction_raw": x_raw,
                "vapour_mole_fraction_raw": y_raw,
            }
        delta = _component_delta(y, x, self.components)
        k = _k_values(y, x, self.components)
        dx = [delta[c] for c in self.components]
        tie_length = _ternary_norm(dx)
        residue_direction = {c: -delta[c] for c in self.components}
        return {
            "status": "two_phase",
            "pressure_kpa": pressure_kpa,
            "vapor_fraction_spec": vapor_fraction,
            "feed_z": feed_z,
            "grid_index": list(grid_index),
            "input_flows": flows,
            "liquid_flow": liq_flow,
            "vapour_flow": vap_flow,
            "vfrac_out": vap_flow / (vap_flow + liq_flow) if (vap_flow + liq_flow) > 1e-14 else None,
            "liquid_mole_fraction_raw": x_raw,
            "vapour_mole_fraction_raw": y_raw,
            "liquid_ternary_x": x,
            "vapour_ternary_y": y,
            "tie_line_delta_y_minus_x": delta,
            "tie_line_length": tie_length,
            "k_values": k,
            "dominant_vapour_enrichment_component": _dominant_key(delta),
            "dominant_k_component": _dominant_k_component(k),
            "azeotrope_like_gap_norm": tie_length,
            "residue_direction_proxy_x_minus_y": residue_direction,
            "temperature": self.base.read_stream_temperature(liq_stream),
            "with_solvent": bool(with_solvent),
            "solvent": _ternary_clean_component(solvent) if solvent else None,
            "solvent_to_feed_ratio": solvent_to_feed_ratio if with_solvent else None,
        }

    def run(
            self,
            *,
            pressure_grid_kpa: Sequence[float] = TERNARY_DEFAULT_PRESSURE_GRID_KPA,
            vapor_fraction_grid: Sequence[float] = TERNARY_DEFAULT_VAPOR_FRACTION_GRID,
            grid_divisions: int = TERNARY_DEFAULT_GRID_DIVISIONS,
            min_component_fraction: float = 0.0,
            thermo_method: str = "NRTL",
            solvent: Optional[str] = None,
            solvent_to_feed_ratio: float = 0.0,
    ) -> dict:
        with_solvent = bool(solvent and float(solvent_to_feed_ratio or 0.0) > 0)
        reg_components = list(self.components)
        if with_solvent:
            solv = _ternary_clean_component(solvent)
            if solv not in reg_components:
                reg_components.append(solv)
        self.base.ensure_components_registered(reg_components, thermo_method=thermo_method)
        grid = ternary_grid(grid_divisions, min_component_fraction=min_component_fraction)
        cases = []
        failed_cases = []
        self.log(
            f"[TERNARY-START] {TERNARY_RUNNER_VERSION}; components={self.components}; pressures={list(pressure_grid_kpa)}; grid_points={len(grid)}")
        for p_idx, p_kpa in enumerate([float(p) for p in pressure_grid_kpa], 1):
            bid, feed, vap, liq = self.base.ensure_flash_unit()
            points = []
            failed_points = []
            for a, b, c, idx in grid:
                feed_z = {self.components[0]: a, self.components[1]: b, self.components[2]: c}
                flows = self._build_flow_dict([a, b, c], solvent=solvent, solvent_to_feed_ratio=solvent_to_feed_ratio)
                point_added = False
                attempts = []
                try:
                    self.base.set_feed_stream(feed, flows, temperature_C=self.feed_temperature_C,
                                              pressure_kpa=self.feed_pressure_kpa)
                    for vf in [float(v) for v in vapor_fraction_grid]:
                        self.base.set_flash_block(bid, pressure_kpa=p_kpa, vapor_fraction=vf)
                        self.base.run_aspen(f"ternary_flash P={p_kpa:g} kPa z=({a:g},{b:g},{c:g}) vf={vf:g}")
                        rec = self._record_point(
                            pressure_kpa=p_kpa,
                            vapor_fraction=vf,
                            feed_z=feed_z,
                            grid_index=idx,
                            liq_stream=liq,
                            vap_stream=vap,
                            flows=flows,
                            with_solvent=with_solvent,
                            solvent=solvent,
                            solvent_to_feed_ratio=solvent_to_feed_ratio,
                        )
                        attempts.append(rec)
                        if rec.get("status") == "two_phase":
                            points.append(rec)
                            point_added = True
                            break
                    if not point_added:
                        failed_points.append({"feed_z": feed_z, "grid_index": list(idx), "attempts": attempts})
                except Exception as exc:
                    failed_points.append({"feed_z": feed_z, "grid_index": list(idx), "error": repr(exc)})
                    self.log(f"[TERNARY-POINT-FAIL] P={p_kpa:g}; z={feed_z}; {exc}")
            case = {
                "case_type": "ternary_flash_phase_map",
                "components": list(self.components),
                "pressure_kpa": p_kpa,
                "pressure_atm": p_kpa / 101.325,
                "feed_temperature_C": self.feed_temperature_C,
                "feed_pressure_kpa": self.feed_pressure_kpa,
                "vapor_fraction_grid": list(vapor_fraction_grid),
                "grid_divisions": grid_divisions,
                "min_component_fraction": min_component_fraction,
                "with_solvent": with_solvent,
                "solvent": _ternary_clean_component(solvent) if solvent else None,
                "solvent_to_feed_ratio": solvent_to_feed_ratio if with_solvent else None,
                "points": points,
                "failed_points": failed_points,
                "valid_point_count": len(points),
                "failed_point_count": len(failed_points),
            }
            cases.append(case)
            self.log(f"[TERNARY-CASE] P={p_kpa:g} kPa; valid={len(points)}; failed={len(failed_points)}")
        results = {
            "runner_version": TERNARY_RUNNER_VERSION,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "status": "completed" if any(
                c.get("valid_point_count", 0) for c in cases) else "failed_no_valid_ternary_points",
            "components": list(self.components),
            "case_count": len(cases),
            "cases": cases,
            "log": list(self.log_lines),
        }
        results["ternary_analysis"] = analyze_ternary_flash_results(results)
        return results


def _centroid(points: List[Dict[str, float]], components: Sequence[str]) -> Optional[Dict[str, float]]:
    if not points:
        return None
    out = {c: 0.0 for c in components}
    for p in points:
        for c in components:
            out[c] += float(p.get(c) or 0.0)
    n = float(len(points))
    return {c: out[c] / n for c in components}


def _boundary_proxy_segments(points: List[dict], components: Sequence[str]) -> List[dict]:
    by_idx = {tuple(p.get("grid_index")): p for p in points if p.get("grid_index") is not None}
    segs: List[dict] = []
    neighbor_steps = [(1, -1, 0), (1, 0, -1), (0, 1, -1)]
    for idx, p in by_idx.items():
        dom = p.get("dominant_k_component") or p.get("dominant_vapour_enrichment_component")
        for step in neighbor_steps:
            nb = (idx[0] + step[0], idx[1] + step[1], idx[2] + step[2])
            q = by_idx.get(nb)
            if not q:
                continue
            qdom = q.get("dominant_k_component") or q.get("dominant_vapour_enrichment_component")
            if dom and qdom and dom != qdom:
                segs.append({
                    "from_feed_z": p.get("feed_z"),
                    "to_feed_z": q.get("feed_z"),
                    "from_dominant": dom,
                    "to_dominant": qdom,
                    "interpretation": "dominant volatility/enrichment region changes across this grid edge; treat as boundary proxy, not rigorous distillation boundary",
                })
    return segs


def analyze_ternary_flash_results(results: dict) -> dict:
    components = [_ternary_clean_component(c) for c in results.get("components", [])]
    case_summaries = []
    for case in results.get("cases", []):
        pts = [p for p in case.get("points", []) if p.get("status") == "two_phase"]
        liq_pts = [p.get("liquid_ternary_x") for p in pts if p.get("liquid_ternary_x")]
        vap_pts = [p.get("vapour_ternary_y") for p in pts if p.get("vapour_ternary_y")]
        feed_pts = [p.get("feed_z") for p in pts if p.get("feed_z")]
        tie_lengths = [float(p.get("tie_line_length") or 0.0) for p in pts]
        gaps_sorted = sorted(pts, key=lambda p: float(p.get("azeotrope_like_gap_norm") or 1e9))
        enrichment_counts: Dict[str, int] = {}
        k_counts: Dict[str, int] = {}
        for p in pts:
            d = p.get("dominant_vapour_enrichment_component")
            k = p.get("dominant_k_component")
            if d:
                enrichment_counts[d] = enrichment_counts.get(d, 0) + 1
            if k:
                k_counts[k] = k_counts.get(k, 0) + 1
        boundary_proxy = _boundary_proxy_segments(pts, components)
        case_summaries.append({
            "pressure_kpa": case.get("pressure_kpa"),
            "valid_point_count": len(pts),
            "grid_total_count": len(pts) + len(case.get("failed_points", [])),
            "two_phase_fraction": len(pts) / max(1, len(pts) + len(case.get("failed_points", []))),
            "feed_centroid": _centroid(feed_pts, components),
            "liquid_centroid": _centroid(liq_pts, components),
            "vapour_centroid": _centroid(vap_pts, components),
            "mean_tie_line_length": _ternary_mean(tie_lengths),
            "max_tie_line_length": max(tie_lengths) if tie_lengths else None,
            "dominant_vapour_enrichment_counts": enrichment_counts,
            "dominant_k_component_counts": k_counts,
            "azeotrope_like_candidates": [
                {
                    "feed_z": p.get("feed_z"),
                    "liquid_ternary_x": p.get("liquid_ternary_x"),
                    "vapour_ternary_y": p.get("vapour_ternary_y"),
                    "gap_norm": p.get("azeotrope_like_gap_norm"),
                    "vapor_fraction_spec": p.get("vapor_fraction_spec"),
                }
                for p in gaps_sorted[:5]
            ],
            "boundary_proxy_segment_count": len(boundary_proxy),
            "boundary_proxy_segments": boundary_proxy[:100],
        })
    pressure_shift = []
    if len(results.get("cases", [])) >= 2:
        cases = sorted(results["cases"], key=lambda c: float(c.get("pressure_kpa") or 0.0))
        low, high = cases[0], cases[-1]
        low_map = {tuple(p.get("grid_index")): p for p in low.get("points", []) if p.get("status") == "two_phase"}
        high_map = {tuple(p.get("grid_index")): p for p in high.get("points", []) if p.get("status") == "two_phase"}
        common = sorted(set(low_map) & set(high_map))
        shifts = []
        flips = 0
        for idx in common:
            lp, hp = low_map[idx], high_map[idx]
            ly = lp.get("vapour_ternary_y") or {}
            hy = hp.get("vapour_ternary_y") or {}
            lx = lp.get("liquid_ternary_x") or {}
            hx = hp.get("liquid_ternary_x") or {}
            vap_shift = {c: float(hy.get(c) or 0.0) - float(ly.get(c) or 0.0) for c in components}
            liq_shift = {c: float(hx.get(c) or 0.0) - float(lx.get(c) or 0.0) for c in components}
            low_dom = lp.get("dominant_k_component")
            high_dom = hp.get("dominant_k_component")
            if low_dom != high_dom:
                flips += 1
            shifts.append({
                "grid_index": list(idx),
                "feed_z": lp.get("feed_z"),
                "vapour_shift_high_minus_low": vap_shift,
                "liquid_shift_high_minus_low": liq_shift,
                "tie_line_length_change": float(hp.get("tie_line_length") or 0.0) - float(
                    lp.get("tie_line_length") or 0.0),
                "dominant_k_low": low_dom,
                "dominant_k_high": high_dom,
            })
        if shifts:
            mean_vap_shift = {c: _ternary_mean([s["vapour_shift_high_minus_low"][c] for s in shifts]) for c in
                              components}
            mean_liq_shift = {c: _ternary_mean([s["liquid_shift_high_minus_low"][c] for s in shifts]) for c in
                              components}
            mean_tie_change = _ternary_mean([s["tie_line_length_change"] for s in shifts])
            pressure_shift.append({
                "low_pressure_kpa": low.get("pressure_kpa"),
                "high_pressure_kpa": high.get("pressure_kpa"),
                "common_grid_points": len(common),
                "dominant_region_flip_count": flips,
                "dominant_region_flip_fraction": flips / max(1, len(common)),
                "mean_vapour_composition_shift": mean_vap_shift,
                "mean_liquid_composition_shift": mean_liq_shift,
                "mean_tie_line_length_change": mean_tie_change,
                "point_shifts": shifts,
            })
    summary_lines = []
    for cs in case_summaries:
        summary_lines.append(
            f"P={cs.get('pressure_kpa')} kPa: two_phase={cs.get('valid_point_count')}/{cs.get('grid_total_count')}, "
            f"mean_tie={cs.get('mean_tie_line_length')}, boundary_proxy={cs.get('boundary_proxy_segment_count')}"
        )
    if pressure_shift:
        ps = pressure_shift[0]
        summary_lines.append(
            f"Pressure comparison {ps.get('low_pressure_kpa')}→{ps.get('high_pressure_kpa')} kPa: "
            f"dominant-region flips={ps.get('dominant_region_flip_count')}/{ps.get('common_grid_points')}, "
            f"mean tie-line change={ps.get('mean_tie_line_length_change')}"
        )
    return {
        "method": "flash_derived_ternary_phase_map_plus_route_A_topology_proxy",
        "limitations": [
            "Boundary proxy is derived from Flash tie-line/enrichment-region changes and is not a rigorous residue-curve or distillation-boundary calculation.",
            "Use this for PSD/EPSD screening; strict route validation should use RadFrac and/or Aspen Property Analysis residue curves.",
        ],
        "case_summaries": case_summaries,
        "pressure_shift": pressure_shift,
        "summary_lines": summary_lines,
    }


def _draw_ternary_base(ax: Any, components: Sequence[str], title: str = "") -> None:
    import matplotlib.pyplot as plt
    tri = [ternary_to_xy(1, 0, 0), ternary_to_xy(0, 1, 0), ternary_to_xy(0, 0, 1), ternary_to_xy(1, 0, 0)]
    ax.plot([p[0] for p in tri], [p[1] for p in tri], linewidth=1.2)
    ax.text(-0.04, -0.04, components[0], ha="right", va="top", fontsize=10)
    ax.text(1.04, -0.04, components[1], ha="left", va="top", fontsize=10)
    ax.text(0.5, math.sqrt(3) / 2 + 0.04, components[2], ha="center", va="bottom", fontsize=10)
    ax.set_aspect("equal")
    ax.set_xlim(-0.08, 1.08)
    ax.set_ylim(-0.08, math.sqrt(3) / 2 + 0.10)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    if title:
        ax.set_title(title, fontsize=12)


def save_point_csv(results: dict, out_dir: str = ".") -> str:
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(out_dir, f"ternary_flash_points_{ts}.csv")
    comps = [_ternary_clean_component(c) for c in results.get("components", [])]
    fields = ["pressure_kpa", "vapor_fraction_spec", "status"]
    for prefix in ("feed", "liq", "vap"):
        fields.extend([f"{prefix}_{c}" for c in comps])
    fields.extend([
        "tie_line_length",
        "azeotrope_like_gap_norm",
        "dominant_k_component",
        "dominant_vapour_enrichment_component",
    ])
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for case in results.get("cases", []):
            for p in case.get("points", []):
                p = _as_dict(p)
                row = {
                    "pressure_kpa": case.get("pressure_kpa"),
                    "vapor_fraction_spec": p.get("vapor_fraction_spec"),
                    "status": p.get("status"),
                }
                for c in comps:
                    row[f"feed_{c}"] = (p.get("feed_z") or {}).get(c)
                    row[f"liq_{c}"] = (p.get("liquid_ternary_x") or {}).get(c)
                    row[f"vap_{c}"] = (p.get("vapour_ternary_y") or {}).get(c)
                row["tie_line_length"] = p.get("tie_line_length")
                row["azeotrope_like_gap_norm"] = p.get("azeotrope_like_gap_norm")
                row["dominant_k_component"] = p.get("dominant_k_component")
                row["dominant_vapour_enrichment_component"] = p.get("dominant_vapour_enrichment_component")
                writer.writerow(row)
    return path


def run_ternary_flash_screen(
        aspen: Any,
        *,
        components: Sequence[str] = TERNARY_DEFAULT_COMPONENTS,
        pressure_grid_kpa: Sequence[float] = TERNARY_DEFAULT_PRESSURE_GRID_KPA,
        vapor_fraction_grid: Sequence[float] = TERNARY_DEFAULT_VAPOR_FRACTION_GRID,
        grid_divisions: int = TERNARY_DEFAULT_GRID_DIVISIONS,
        min_component_fraction: float = 0.0,
        feed_temperature_C: float = 25.0,
        feed_pressure_kpa: float = 101.325,
        pressure_write_mode: str = "atm",
        thermo_method: str = "NRTL",
        solvent: Optional[str] = None,
        solvent_to_feed_ratio: float = 0.0,
        logger: Optional[Callable[[str], None]] = None,
) -> dict:
    runner = AspenTernaryFlashRunner(
        aspen,
        components=components,
        pressure_write_mode=pressure_write_mode,
        feed_temperature_C=feed_temperature_C,
        feed_pressure_kpa=feed_pressure_kpa,
        logger=logger,
    )
    return runner.run(
        pressure_grid_kpa=pressure_grid_kpa,
        vapor_fraction_grid=vapor_fraction_grid,
        grid_divisions=grid_divisions,
        min_component_fraction=min_component_fraction,
        thermo_method=thermo_method,
        solvent=solvent,
        solvent_to_feed_ratio=solvent_to_feed_ratio,
    )


def ternary_components_from_state(state_dict: Optional[dict]) -> List[str]:
    comps = non_solvent_components_from_state(state_dict)
    if len(comps) >= 3:
        return comps[:3]
    fallback = [_canonical_component_name(c) for c in TERNARY_DEFAULT_COMPONENTS]
    return fallback[:3]


def update_state_with_ternary_flash_results(state_dict: dict, results: dict) -> dict:
    state = _as_dict(state_dict)
    state["ternary_flash_results"] = results
    if "separation_retrofit" in state:
        state.pop("separation_retrofit", None)
    return state


def run_ternary_flash_screen_from_state(
        state_dict: dict,
        *,
        aspen: Any = None,
        template_path: Optional[str] = None,
        aspen_version: str = "Apwn.Document.40.0",
        components: Optional[Sequence[str]] = None,
        pressure_grid_kpa: Optional[Sequence[float]] = None,
        vapor_fraction_grid: Optional[Sequence[float]] = None,
        grid_divisions: int = TERNARY_DEFAULT_GRID_DIVISIONS,
        min_component_fraction: float = 0.0,
        pressure_write_mode: str = "atm",
        thermo_method: str = "NRTL",
        solvent: Optional[str] = None,
        solvent_to_feed_ratio: float = 0.0,
        logger: Optional[Callable[[str], None]] = None,
        close_aspen: bool = False,
        **runner_options: Any,
) -> Tuple[dict, dict]:
    if aspen is None:
        if not template_path:
            template_path = (
                    _as_dict(state_dict).get("template_path")
                    or _as_dict(state_dict).get("aspen_template_path")
                    or "Simulation 1.bkp"
            )
        aspen = open_aspen_document(str(template_path), aspen_version=aspen_version)

    cond = feed_condition_from_state(state_dict)
    comps = [_canonical_component_name(c) for c in (components or ternary_components_from_state(state_dict))]
    results = run_ternary_flash_screen(
        aspen,
        components=comps,
        pressure_grid_kpa=pressure_grid_kpa or TERNARY_DEFAULT_PRESSURE_GRID_KPA,
        vapor_fraction_grid=vapor_fraction_grid or TERNARY_DEFAULT_VAPOR_FRACTION_GRID,
        grid_divisions=grid_divisions,
        min_component_fraction=min_component_fraction,
        feed_temperature_C=float(cond.get("temperature_C") or 25.0),
        feed_pressure_kpa=float(cond.get("pressure_kpa") or 101.325),
        pressure_write_mode=pressure_write_mode,
        thermo_method=thermo_method,
        solvent=solvent,
        solvent_to_feed_ratio=solvent_to_feed_ratio,
        logger=logger,
        **runner_options,
    )
    update_state_with_ternary_flash_results(state_dict, results)

    if close_aspen:
        try:
            aspen.Close(False)
        except Exception:
            pass
        try:
            aspen.Quit()
        except Exception:
            pass

    return state_dict, results


# ---------------------------------------------------------------------------
# v21 readable topology plots: paper-aligned unidistribution/univolatility/RCM proxy
# ---------------------------------------------------------------------------

def _two_phase_points(case: dict) -> List[dict]:
    return [p for p in _as_list(_as_dict(case).get("points")) if _as_dict(p).get("status") == "two_phase"]


def _safe_ratio(num: Any, den: Any, default: Optional[float] = None) -> Optional[float]:
    n = _safe_float(num, None)
    d = _safe_float(den, None)
    if n is None or d is None or abs(d) <= 1e-14:
        return default
    val = n / d
    if math.isnan(val) or math.isinf(val):
        return default
    return float(val)


def _ternary_k_values(point: dict, components: Sequence[str]) -> Dict[str, Optional[float]]:
    x = _as_dict(_as_dict(point).get("liquid_ternary_x"))
    y = _as_dict(_as_dict(point).get("vapour_ternary_y"))
    return {c: _safe_ratio(y.get(c), x.get(c), None) for c in components}


def _xy_values_for_case(case: dict, components: Sequence[str]) -> Tuple[List[float], List[float], List[dict]]:
    pts = _two_phase_points(case)
    xs, ys, valid = [], [], []
    for p in pts:
        z = _as_dict(p).get("feed_z") or {}
        tx, ty = dict_to_xy(z, components)
        if tx is None or ty is None:
            continue
        xs.append(float(tx))
        ys.append(float(ty))
        valid.append(p)
    return xs, ys, valid


def _scalar_values_for_points(points: Sequence[dict], components: Sequence[str], scalar_kind: str, key: Any) -> List[
    float]:
    vals: List[float] = []
    for p in points:
        kvals = _ternary_k_values(p, components)
        val = None
        if scalar_kind == "K":
            comp = str(key)
            val = kvals.get(comp)
        elif scalar_kind == "alpha":
            c1, c2 = key
            val = _safe_ratio(kvals.get(c1), kvals.get(c2), None)
        elif scalar_kind == "gap":
            val = _safe_float(_as_dict(p).get("azeotrope_like_gap_norm"), None)
        elif scalar_kind == "tie":
            val = _safe_float(_as_dict(p).get("tie_line_length"), None)
        vals.append(float(val) if val is not None else float("nan"))
    return vals


def _scalar_value_for_point(point: dict, scalar_kind: str, key: Any, components: Optional[Sequence[str]] = None) -> \
Optional[float]:
    pd = _as_dict(point)
    kind = str(scalar_kind or "").lower()

    if kind == "alpha":
        try:
            c1, c2 = key
        except Exception:
            return None
        x = _as_dict(pd.get("liquid_ternary_x"))
        y = _as_dict(pd.get("vapour_ternary_y"))
        x1 = _safe_float(x.get(c1), None)
        x2 = _safe_float(x.get(c2), None)
        y1 = _safe_float(y.get(c1), None)
        y2 = _safe_float(y.get(c2), None)
        if None in (x1, x2, y1, y2):
            return None
        return _safe_ratio(_safe_ratio(y1, x1, None), _safe_ratio(y2, x2, None), None)

    if kind == "k":
        comp = str(key)
        x = _as_dict(pd.get("liquid_ternary_x"))
        y = _as_dict(pd.get("vapour_ternary_y"))
        return _safe_ratio(y.get(comp), x.get(comp), None)

    if kind == "gap":
        return _safe_float(pd.get("azeotrope_like_gap_norm", pd.get("gap_norm")), None)

    if kind == "tie":
        return _safe_float(pd.get("tie_line_length"), None)

    if components:
        vals = _scalar_values_for_points([pd], components, scalar_kind, key)
        if not vals or math.isnan(vals[0]):
            return None
        return vals[0]
    return None


def _xy_distance(p0: Sequence[float], p1: Sequence[float]) -> float:
    """Euclidean distance in the ternary plot coordinate system."""
    try:
        return math.sqrt((float(p0[0]) - float(p1[0])) ** 2 + (float(p0[1]) - float(p1[1])) ** 2)
    except Exception:
        return float("inf")


def _clean_xy_polyline(points: Sequence[Sequence[float]], *, min_step: float = 1e-10) -> List[Tuple[float, float]]:
    """Remove NaN and duplicate-neighbour coordinates from a polyline."""
    out: List[Tuple[float, float]] = []
    for pt in points:
        if pt is None or len(pt) < 2:
            continue
        x = _safe_float(pt[0], None)
        y = _safe_float(pt[1], None)
        if x is None or y is None or math.isnan(x) or math.isnan(y):
            continue
        xy = (float(x), float(y))
        if not out or _xy_distance(out[-1], xy) > min_step:
            out.append(xy)
    return out


def _connect_xy_segments(
        segments: Sequence[Sequence[Sequence[float]]],
        *,
        max_gap: float = 0.035,
        min_segment_points: int = 2,
) -> List[List[Tuple[float, float]]]:
    """
    Merge neighbouring contour fragments into longer continuous polylines.

    Matplotlib's tricontour can return many short segments when the ternary grid
    is sparse or the contour runs along triangle edges.  For publication figures
    we keep the computed contour geometry but join only very close segment ends,
    so the visual line is continuous without inventing long artificial branches.
    """
    remaining: List[List[Tuple[float, float]]] = []
    for seg in segments:
        pts = _clean_xy_polyline(seg)
        if len(pts) >= min_segment_points:
            remaining.append(pts)

    if not remaining:
        return []

    def seg_length(seg: Sequence[Tuple[float, float]]) -> float:
        return sum(_xy_distance(seg[i - 1], seg[i]) for i in range(1, len(seg)))

    polylines: List[List[Tuple[float, float]]] = []
    while remaining:
        # Start from the longest segment.  This avoids a small boundary fragment
        # controlling the ordering of the main phase-boundary contour.
        idx0 = max(range(len(remaining)), key=lambda i: seg_length(remaining[i]))
        line = remaining.pop(idx0)

        improved = True
        while improved and remaining:
            improved = False
            best = None
            line_start, line_end = line[0], line[-1]
            for i, seg in enumerate(remaining):
                candidates = [
                    ("append", False, _xy_distance(line_end, seg[0])),
                    ("append", True, _xy_distance(line_end, seg[-1])),
                    ("prepend", False, _xy_distance(line_start, seg[-1])),
                    ("prepend", True, _xy_distance(line_start, seg[0])),
                ]
                for mode, reverse, gap in candidates:
                    if gap <= max_gap and (best is None or gap < best[0]):
                        best = (gap, i, mode, reverse)
            if best is not None:
                _, i, mode, reverse = best
                seg = remaining.pop(i)
                if reverse:
                    seg = list(reversed(seg))
                if mode == "append":
                    if _xy_distance(line[-1], seg[0]) <= 1e-10:
                        line.extend(seg[1:])
                    else:
                        line.extend(seg)
                else:
                    if _xy_distance(seg[-1], line[0]) <= 1e-10:
                        line = seg[:-1] + line
                    else:
                        line = seg + line
                improved = True

        polylines.append(_clean_xy_polyline(line))

    # Draw longer curves first in legends/figures.
    polylines.sort(key=seg_length, reverse=True)
    return polylines


def _tricontour_segments(
        ax: Any,
        xs: Sequence[float],
        ys: Sequence[float],
        vals: Sequence[float],
        *,
        level: float,
) -> List[List[Tuple[float, float]]]:
    """Return raw contour segments from tricontour without leaving artists on ax."""
    import numpy as np

    finite = [(x, y, v) for x, y, v in zip(xs, ys, vals) if v is not None and np.isfinite(v)]
    if len(finite) < 4:
        return []
    fx, fy, fv = zip(*finite)
    if min(fv) > level or max(fv) < level:
        return []

    try:
        cs = ax.tricontour(fx, fy, fv, levels=[level], colors=["none"], linewidths=0.0, alpha=0.0)
    except Exception:
        return []

    segments: List[List[Tuple[float, float]]] = []
    try:
        if getattr(cs, "allsegs", None):
            for arr in cs.allsegs[0]:
                pts = _clean_xy_polyline(arr)
                if len(pts) >= 2:
                    segments.append(pts)
    finally:
        try:
            for coll in cs.collections:
                coll.remove()
        except Exception:
            pass
    return segments


def _add_tricontour_level(
        ax: Any,
        xs: Sequence[float],
        ys: Sequence[float],
        vals: Sequence[float],
        *,
        level: float,
        color: Any,
        linestyle: str,
        linewidth: float,
        label: str,
        alpha: float = 0.95,
        connect_segments: bool = True,
        connect_gap: float = 0.035,
) -> bool:
    """Add a ternary scalar contour, optionally merging small line gaps."""
    segments = _tricontour_segments(ax, xs, ys, vals, level=level)
    if not segments:
        return False

    polylines = _connect_xy_segments(segments, max_gap=connect_gap) if connect_segments else [
        _clean_xy_polyline(seg) for seg in segments
    ]
    polylines = [line for line in polylines if len(line) >= 2]
    if not polylines:
        return False

    for line in polylines:
        ax.plot(
            [p[0] for p in line],
            [p[1] for p in line],
            color=color,
            linestyle=linestyle,
            linewidth=linewidth,
            alpha=alpha,
            solid_capstyle="round",
            solid_joinstyle="round",
        )

    handles, labels = ax.get_legend_handles_labels()
    if label not in labels:
        ax.plot([], [], color=color, linestyle=linestyle, linewidth=linewidth, label=label, alpha=alpha)
    return True

def _pair_color_lookup(components: Sequence[str]) -> Dict[Tuple[str, str], str]:
    palette = ["tab:purple", "tab:brown", "tab:pink", "tab:gray", "tab:olive", "tab:cyan"]
    pairs = []
    for i in range(len(components)):
        for j in range(i + 1, len(components)):
            pairs.append((components[i], components[j]))
    return {pair: palette[i % len(palette)] for i, pair in enumerate(pairs)}


def _plot_gap_panel(ax: Any, case: dict, components: Sequence[str], *, title: str) -> None:
    import numpy as np
    import matplotlib.pyplot as plt
    _draw_ternary_base(ax, components, title=title)
    xs, ys, pts = _xy_values_for_case(case, components)
    if not pts:
        ax.text(0.5, 0.45, "No two-phase ternary Flash points", ha="center", va="center", fontsize=10)
        return
    gap_vals = _scalar_values_for_points(pts, components, "gap", None)
    sc = ax.scatter(xs, ys, c=gap_vals, s=35, cmap="viridis_r", edgecolors="white", linewidths=0.3, alpha=0.95)
    cb = plt.colorbar(sc, ax=ax, fraction=0.046, pad=0.02)
    cb.set_label("||y-x||", fontsize=10)
    cb.ax.tick_params(labelsize=9)

    pair_colors = _pair_color_lookup(components)
    for pair, color in pair_colors.items():
        vals = _scalar_values_for_points(pts, components, "alpha", pair)
        _add_tricontour_level(ax, xs, ys, vals, level=1.0, color=color, linestyle="-", linewidth=1.5,
                              label=f"α{pair[0]}/{pair[1]}=1", alpha=0.9)

    ranked = sorted(pts, key=lambda p: float(_as_dict(p).get("azeotrope_like_gap_norm") or 1e9))
    for rank, p in enumerate(ranked[:3], start=1):
        z = _as_dict(p).get("feed_z") or {}
        px, py = dict_to_xy(z, components)
        ax.scatter([px], [py], s=100, marker="*", c="gold", edgecolors="black", linewidths=0.6, zorder=7)

    ax.legend(loc="lower left", fontsize=9, frameon=False)


def plot_ternary_case(case: dict, out_dir: str = ".") -> str:
    import matplotlib.pyplot as plt
    plt.rcParams['font.family'] = 'Times New Roman'
    plt.rcParams['mathtext.fontset'] = 'stix'

    os.makedirs(out_dir, exist_ok=True)
    components = [_ternary_clean_component(c) for c in case.get("components", [])]
    p_kpa = float(case.get("pressure_kpa") or 0.0)

    fig, ax = plt.subplots(1, 1, figsize=(6.5, 5.5), dpi=300)
    _plot_gap_panel(ax, case, components, title=f"Flash-derived screening map @ {p_kpa:g} kPa")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(out_dir, f"ternary_topology_map_{int(round(p_kpa))}kPa_{ts}.png")
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


def _select_pressure_pair_for_screen(results: dict) -> Tuple[Optional[dict], Optional[dict], dict]:
    cases = sorted(
        _as_list(results.get("cases")),
        key=lambda c: (_safe_float(_as_dict(c).get("pressure_kpa"), 0.0) or 0.0),
    )
    if len(cases) < 2:
        return None, None, {}

    best: Optional[Tuple[dict, dict, dict]] = None
    components = [_ternary_clean_component(c) for c in results.get("components", [])]

    for i in range(len(cases) - 1):
        for j in range(i + 1, len(cases)):
            low = _as_dict(cases[i])
            high = _as_dict(cases[j])
            low_pts = {
                tuple(_as_dict(p).get("grid_index") or []): _as_dict(p)
                for p in _as_list(low.get("points"))
                if _as_dict(p).get("status") == "two_phase"
            }
            high_pts = {
                tuple(_as_dict(p).get("grid_index") or []): _as_dict(p)
                for p in _as_list(high.get("points"))
                if _as_dict(p).get("status") == "two_phase"
            }
            common = sorted(set(low_pts) & set(high_pts))
            if len(common) < 5:
                continue

            magnitudes: List[float] = []
            flip_count = 0
            for idx in common:
                lp = low_pts[idx]
                hp = high_pts[idx]
                ly = _as_dict(lp.get("vapour_ternary_y"))
                hy = _as_dict(hp.get("vapour_ternary_y"))
                shift = {c: float(hy.get(c) or 0.0) - float(ly.get(c) or 0.0) for c in components}
                magnitudes.append(math.sqrt(sum(v * v for v in shift.values())))
                if lp.get("dominant_k_component") != hp.get("dominant_k_component"):
                    flip_count += 1

            pressure_gap = abs(
                (_safe_float(high.get("pressure_kpa"), 0.0) or 0.0)
                - (_safe_float(low.get("pressure_kpa"), 0.0) or 0.0)
            )
            flip_fraction = flip_count / float(len(common)) if common else 0.0
            mean_shift = _mean(magnitudes) or 0.0
            score = mean_shift * 140.0 + flip_fraction * 30.0 + min(pressure_gap / 15.0, 15.0)
            meta = {
                "screen_score": score,
                "common_count": len(common),
                "flip_fraction": flip_fraction,
                "mean_shift": mean_shift,
            }
            if best is None or score > best[2]["screen_score"]:
                best = (low, high, meta)

    if best is None:
        return (
            _as_dict(cases[0]),
            _as_dict(cases[-1]),
            {"screen_score": None, "common_count": 0, "flip_fraction": 0.0, "mean_shift": 0.0},
        )
    return best


def _candidate_point_for_pair(case: dict, pair: Tuple[str, str]) -> Optional[dict]:
    best: Optional[Tuple[float, dict]] = None
    for p in _two_phase_points(case):
        pd = _as_dict(p)
        alpha = _scalar_value_for_point(pd, "alpha", pair)
        if alpha is None:
            continue
        gap = _safe_float(pd.get("azeotrope_like_gap_norm", pd.get("gap_norm")), 1.0) or 1.0
        score = abs(float(alpha) - 1.0) + 0.35 * gap
        if best is None or score < best[0]:
            best = (score, pd)
    return None if best is None else best[1]


def plot_pressure_comparison(results: dict, out_dir: str = ".") -> Optional[str]:
    import matplotlib.pyplot as plt
    try:
        import numpy as np
    except Exception:
        np = None

    plt.rcParams['font.family'] = 'Times New Roman'
    plt.rcParams['mathtext.fontset'] = 'stix'

    os.makedirs(out_dir, exist_ok=True)
    analysis = _as_dict(results.get("ternary_analysis"))
    pair_summaries = _as_list(analysis.get("pair_summaries"))
    if not pair_summaries:
        return None

    primary = _as_dict(pair_summaries[0])
    pair = tuple(_as_list(primary.get("pair")))
    if len(pair) != 2:
        return None

    cases = sorted(_as_list(results.get("cases")),
                   key=lambda c: (_safe_float(_as_dict(c).get("pressure_kpa"), 0.0) or 0.0))
    if not cases:
        return None

    components = [_ternary_clean_component(c) for c in results.get("components", [])]

    fig, ax = plt.subplots(1, 1, figsize=(6.5, 5.5), dpi=300)
    p_min = float(_as_dict(cases[0]).get("pressure_kpa") or 0.0)
    p_max = float(_as_dict(cases[-1]).get("pressure_kpa") or 0.0)
    _draw_ternary_base(ax, components,
                       title=f"Merged ternary PSD map ({p_min:g}–{p_max:g} kPa): α{pair[0]}/{pair[1]} = 1")

    base_xs, base_ys, base_points = _xy_values_for_case(_as_dict(cases[0]), components)
    if base_points:
        ax.scatter(base_xs, base_ys, s=5, c="0.75", alpha=0.3, edgecolors="none")

    if np is not None:
        color_list = [plt.cm.plasma(v) for v in np.linspace(0.10, 0.92, len(cases))]
    else:
        color_list = [None] * len(cases)

    for idx, case in enumerate(cases):
        case_d = _as_dict(case)
        p_kpa = float(case_d.get("pressure_kpa") or 0.0)
        xs, ys, pts = _xy_values_for_case(case_d, components)
        if not pts:
            continue
        alpha_vals = _scalar_values_for_points(pts, components, "alpha", pair)
        _add_tricontour_level(
            ax, xs, ys, alpha_vals,
            level=1.0, color=color_list[idx], linestyle="-", linewidth=2.5,
            label=f"{p_kpa:g} kPa", alpha=0.95,
        )

    candidate_specs = [
        (_candidate_point_for_pair(_as_dict(cases[0]), pair), color_list[0]),
        (_candidate_point_for_pair(_as_dict(cases[-1]), pair), color_list[-1]),
    ]
    for cand, color in candidate_specs:
        if cand:
            px, py = dict_to_xy(_as_dict(cand.get("feed_z")), components)
            ax.scatter([px], [py], s=120, marker="*", c=[color], edgecolors="black", linewidths=0.7, zorder=7)

    ax.legend(loc="upper right", fontsize=10, frameon=False)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(out_dir, f"ternary_pressure_swing_merged_map_{ts}.png")
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path



def _composition_xy_from_point(point: dict, field: str, components: Sequence[str]) -> Optional[Tuple[float, float]]:
    comp = _as_dict(_as_dict(point).get(field))
    if not comp:
        return None
    try:
        return dict_to_xy(comp, components)
    except Exception:
        return None


def _ordered_nearest_xy_path(
        points: Sequence[Tuple[float, float]],
        *,
        max_gap: float = 0.12,
) -> List[List[Tuple[float, float]]]:
    """
    Build visually continuous paths through unordered ternary endpoint clouds.

    This is for plotting flash-derived phase-diagram envelopes.  It does not
    replace rigorous phase-boundary tracing, but it prevents the figure from
    drawing random zig-zag connections between distant equilibrium endpoints.
    """
    clean: List[Tuple[float, float]] = []
    seen = set()
    for pt in points:
        xy = _clean_xy_polyline([pt])
        if not xy:
            continue
        key = (round(xy[0][0], 8), round(xy[0][1], 8))
        if key in seen:
            continue
        seen.add(key)
        clean.append(xy[0])
    if len(clean) < 2:
        return []

    unused = set(range(len(clean)))
    paths: List[List[Tuple[float, float]]] = []
    while unused:
        # Start at the left/lower outermost remaining point to make the line
        # direction stable between runs.
        start = min(unused, key=lambda i: (clean[i][0], clean[i][1]))
        unused.remove(start)
        path = [clean[start]]
        while unused:
            last = path[-1]
            nxt = min(unused, key=lambda i: _xy_distance(last, clean[i]))
            gap = _xy_distance(last, clean[nxt])
            if gap > max_gap and len(path) >= 2:
                break
            unused.remove(nxt)
            path.append(clean[nxt])
        if len(path) >= 2:
            paths.append(path)

    paths.sort(key=lambda line: sum(_xy_distance(line[i - 1], line[i]) for i in range(1, len(line))), reverse=True)
    return paths


def _evenly_spaced_points(points: Sequence[dict], max_count: int) -> List[dict]:
    pts = list(points)
    if max_count <= 0 or len(pts) <= max_count:
        return pts
    if max_count == 1:
        return [pts[len(pts) // 2]]
    out = []
    for i in range(max_count):
        idx = round(i * (len(pts) - 1) / (max_count - 1))
        out.append(pts[int(idx)])
    return out


def _polyline_length(points: Sequence[Sequence[float]]) -> float:
    pts = _clean_xy_polyline(points)
    if len(pts) < 2:
        return 0.0
    return sum(_xy_distance(pts[i - 1], pts[i]) for i in range(1, len(pts)))


def _ternary_upper_y_limit(x: float) -> float:
    """Upper boundary y value of the equilateral ternary triangle at plot-coordinate x."""
    x = max(0.0, min(1.0, float(x)))
    return math.sqrt(3.0) * x if x <= 0.5 else math.sqrt(3.0) * (1.0 - x)


def _clip_xy_to_ternary(x: float, y: float, *, margin: float = 0.002) -> Tuple[float, float]:
    """Keep smoothed display coordinates inside the ternary triangle."""
    xx = max(margin, min(1.0 - margin, float(x)))
    yy_max = max(margin, _ternary_upper_y_limit(xx) - margin)
    yy = max(margin, min(float(y), yy_max))
    return xx, yy


def _percentile(values: Sequence[float], q: float) -> Optional[float]:
    vals = sorted(float(v) for v in values if v is not None and not math.isnan(float(v)))
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    q = max(0.0, min(1.0, float(q)))
    pos = q * (len(vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    frac = pos - lo
    return vals[lo] * (1.0 - frac) + vals[hi] * frac


def _moving_average_xy(points: Sequence[Tuple[float, float]], passes: int = 2) -> List[Tuple[float, float]]:
    """Light smoothing that preserves the two endpoints."""
    pts = _clean_xy_polyline(points, min_step=1e-7)
    if len(pts) < 4:
        return pts
    passes = max(0, int(passes))
    for _ in range(passes):
        new_pts = [pts[0]]
        for i in range(1, len(pts) - 1):
            x = 0.25 * pts[i - 1][0] + 0.50 * pts[i][0] + 0.25 * pts[i + 1][0]
            y = 0.25 * pts[i - 1][1] + 0.50 * pts[i][1] + 0.25 * pts[i + 1][1]
            new_pts.append(_clip_xy_to_ternary(x, y))
        new_pts.append(pts[-1])
        pts = _clean_xy_polyline(new_pts, min_step=1e-7)
    return pts


def _catmull_rom_spline_xy(
        points: Sequence[Tuple[float, float]],
        *,
        samples_per_segment: int = 16,
) -> List[Tuple[float, float]]:
    """
    Smooth a display polyline without requiring scipy.

    The curve is used only for visual trend display.  It does not alter the
    underlying Aspen flash results or the CSV/JSON data.
    """
    pts = _clean_xy_polyline(points, min_step=1e-7)
    if len(pts) < 3:
        return pts
    samples = max(4, int(samples_per_segment))
    ext = [pts[0]] + pts + [pts[-1]]
    out: List[Tuple[float, float]] = []
    for i in range(1, len(ext) - 2):
        p0, p1, p2, p3 = ext[i - 1], ext[i], ext[i + 1], ext[i + 2]
        for j in range(samples):
            t = j / float(samples)
            t2 = t * t
            t3 = t2 * t
            x = 0.5 * (
                (2.0 * p1[0])
                + (-p0[0] + p2[0]) * t
                + (2.0 * p0[0] - 5.0 * p1[0] + 4.0 * p2[0] - p3[0]) * t2
                + (-p0[0] + 3.0 * p1[0] - 3.0 * p2[0] + p3[0]) * t3
            )
            y = 0.5 * (
                (2.0 * p1[1])
                + (-p0[1] + p2[1]) * t
                + (2.0 * p0[1] - 5.0 * p1[1] + 4.0 * p2[1] - p3[1]) * t2
                + (-p0[1] + 3.0 * p1[1] - 3.0 * p2[1] + p3[1]) * t3
            )
            out.append(_clip_xy_to_ternary(x, y))
    out.append(_clip_xy_to_ternary(*pts[-1]))
    return _clean_xy_polyline(out, min_step=1e-7)


def _smooth_y_envelope_from_cloud(
        points: Sequence[Tuple[float, float]],
        *,
        bin_count: int = 16,
        y_quantile: float = 0.72,
        smooth_passes: int = 3,
        samples_per_segment: int = 18,
        min_points_per_bin: int = 1,
) -> List[Tuple[float, float]]:
    """
    Convert a noisy ternary endpoint cloud into one smooth y=f(x) trend curve.

    This is deliberately simpler than a rigorous phase-envelope tracing method:
    points are binned along the ternary x-coordinate, a high-percentile y value
    is retained in each bin, and the resulting envelope is smoothed.  This gives
    a clean pressure-shift figure while keeping the trend anchored in the Aspen
    flash endpoints.
    """
    pts = _clean_xy_polyline(points, min_step=1e-7)
    if len(pts) < 3:
        return pts

    # Deduplicate after rounding so dense repeated grid points do not overweight
    # the trend line.
    unique: Dict[Tuple[int, int], Tuple[float, float]] = {}
    for x, y in pts:
        xx, yy = _clip_xy_to_ternary(x, y)
        unique[(round(xx, 5), round(yy, 5))] = (xx, yy)
    pts = sorted(unique.values(), key=lambda p: (p[0], p[1]))
    if len(pts) < 3:
        return pts

    xs = [p[0] for p in pts]
    x_min, x_max = min(xs), max(xs)
    if x_max - x_min < 1e-4:
        return _catmull_rom_spline_xy(_moving_average_xy(pts, passes=smooth_passes),
                                      samples_per_segment=samples_per_segment)

    bin_count = max(5, min(int(bin_count), 32, len(pts)))
    width = (x_max - x_min) / float(bin_count)
    anchors: List[Tuple[float, float]] = []
    for i in range(bin_count):
        lo = x_min + i * width
        hi = x_min + (i + 1) * width if i < bin_count - 1 else x_max + 1e-12
        group = [p for p in pts if lo <= p[0] < hi]
        if len(group) < min_points_per_bin:
            continue
        gx = sum(p[0] for p in group) / len(group)
        gy = _percentile([p[1] for p in group], y_quantile)
        if gy is None:
            continue
        anchors.append(_clip_xy_to_ternary(gx, gy))

    if len(anchors) < 3:
        # Fallback: principal-axis trend from the previous simplified method.
        return _trend_xy_path(pts, smooth_bins=min(12, len(pts)))

    # Preserve physical range endpoints but use the smoothed high-percentile
    # envelope in the interior.
    left_band = sorted(pts, key=lambda p: p[0])[:max(2, len(pts) // 12)]
    right_band = sorted(pts, key=lambda p: p[0])[-max(2, len(pts) // 12):]
    left_anchor = _clip_xy_to_ternary(
        min(p[0] for p in left_band),
        _percentile([p[1] for p in left_band], min(0.50, y_quantile)) or left_band[0][1],
    )
    right_anchor = _clip_xy_to_ternary(
        max(p[0] for p in right_band),
        _percentile([p[1] for p in right_band], min(0.50, y_quantile)) or right_band[-1][1],
    )
    anchors = [left_anchor] + anchors + [right_anchor]
    anchors = sorted(_clean_xy_polyline(anchors, min_step=1e-5), key=lambda p: p[0])
    anchors = _moving_average_xy(anchors, passes=smooth_passes)
    return _catmull_rom_spline_xy(anchors, samples_per_segment=samples_per_segment)


def _trend_xy_path(
        points: Sequence[Tuple[float, float]],
        *,
        smooth_bins: int = 12,
) -> List[Tuple[float, float]]:
    """
    Fallback trend path from a point cloud using principal-axis sorting.

    The main v29 plot uses _smooth_y_envelope_from_cloud for cleaner curves.
    This function is retained for compatibility and rare near-vertical clouds.
    """
    pts = _clean_xy_polyline(points, min_step=1e-7)
    if len(pts) < 2:
        return pts
    mx = sum(p[0] for p in pts) / len(pts)
    my = sum(p[1] for p in pts) / len(pts)
    sxx = sum((p[0] - mx) ** 2 for p in pts) / len(pts)
    syy = sum((p[1] - my) ** 2 for p in pts) / len(pts)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pts) / len(pts)
    angle = 0.5 * math.atan2(2.0 * sxy, sxx - syy) if abs(sxx - syy) + abs(sxy) > 1e-14 else 0.0
    ux, uy = math.cos(angle), math.sin(angle)
    ordered = sorted(pts, key=lambda p: (p[0] - mx) * ux + (p[1] - my) * uy)
    n_bins = max(2, min(int(smooth_bins), len(ordered)))
    group_size = max(1, math.ceil(len(ordered) / n_bins))
    grouped: List[Tuple[float, float]] = []
    for i in range(0, len(ordered), group_size):
        grp = ordered[i:i + group_size]
        grouped.append(_clip_xy_to_ternary(
            sum(p[0] for p in grp) / len(grp),
            sum(p[1] for p in grp) / len(grp),
        ))
    return _catmull_rom_spline_xy(_moving_average_xy(grouped, passes=2), samples_per_segment=16)


def _heatmap_reference_pressure_palette(n: int) -> List[str]:
    """
    Muted lavender-to-magenta palette matching the supplied heatmap reference.
    """
    base = ["#E8E3EC", "#CBB8D8", "#D07AB6", "#C22C7A"]
    if n <= len(base):
        return base[:n]
    # Linear interpolation in RGB for unusual pressure grids.
    def hex_to_rgb(h: str) -> Tuple[int, int, int]:
        h = h.lstrip("#")
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    def rgb_to_hex(rgb: Tuple[float, float, float]) -> str:
        return "#" + "".join(f"{max(0, min(255, int(round(v)))):02X}" for v in rgb)
    rgbs = [hex_to_rgb(h) for h in base]
    out = []
    for i in range(n):
        t = i / max(1, n - 1)
        pos = t * (len(rgbs) - 1)
        lo = int(math.floor(pos))
        hi = min(len(rgbs) - 1, int(math.ceil(pos)))
        f = pos - lo
        rgb = tuple(rgbs[lo][k] * (1.0 - f) + rgbs[hi][k] * f for k in range(3))
        out.append(rgb_to_hex(rgb))
    return out


def plot_ternary_phase_diagram(
        results: dict,
        out_dir: str = ".",
        *,
        boundary_source: str = "liquid",
        show_vapour_boundary: bool = False,
        show_raw_points: bool = False,
        show_tie_lines: bool = False,
        max_tie_lines_per_pressure: int = 3,
        curve_bin_count: int = 15,
        curve_y_quantile: float = 0.72,
        curve_smooth_passes: int = 4,
        samples_per_segment: int = 20,
        linewidth: float = 2.6,
) -> Optional[str]:
    """
    Plot an ultra-simplified ternary phase-trend diagram.

    Default behavior is intentionally minimal:
      * one smooth pressure-trend curve per pressure;
      * heatmap-like lavender-to-magenta colors;
      * no dense tie-lines, no raw zig-zag endpoint cloud, no annotation box.

    The displayed curves are smoothed high-percentile envelopes of the selected
    phase endpoints.  They are for visual trend communication; the raw Aspen
    flash results remain in the JSON output.
    """
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    os.makedirs(out_dir, exist_ok=True)
    rd = _as_dict(results)
    components = [_ternary_clean_component(c) for c in _as_list(rd.get("components"))]
    components = [c for c in components if c]
    if len(components) != 3:
        return None

    cases = sorted(
        _as_list(rd.get("cases")),
        key=lambda c: (_safe_float(_as_dict(c).get("pressure_kpa"), 0.0) or 0.0),
    )
    cases = [c for c in cases if _two_phase_points(_as_dict(c))]
    if not cases:
        return None

    plt.rcParams["font.family"] = "Times New Roman"
    plt.rcParams["mathtext.fontset"] = "stix"

    fig, ax = plt.subplots(1, 1, figsize=(6.5, 5.5), dpi=300)
    fig.patch.set_facecolor("#FBFAFC")
    ax.set_facecolor("#FBFAFC")

    p_min = float(_as_dict(cases[0]).get("pressure_kpa") or 0.0)
    p_max = float(_as_dict(cases[-1]).get("pressure_kpa") or 0.0)
    _draw_ternary_base(
        ax,
        components,
        title=f"Simplified ternary phase-trend diagram ({p_min:g}–{p_max:g} kPa)",
    )

    # Restyle the triangle outline to a restrained academic blue.
    for line in ax.lines:
        try:
            line.set_color("#1F6FAE")
            line.set_linewidth(1.15)
        except Exception:
            pass

    colors = _heatmap_reference_pressure_palette(len(cases))
    pressure_handles = []
    plotted_any = False
    boundary_source = str(boundary_source or "liquid").lower().strip()

    for idx, case in enumerate(cases):
        case_d = _as_dict(case)
        p_kpa = _safe_float(case_d.get("pressure_kpa"), None)
        pts = _two_phase_points(case_d)
        color = colors[idx]

        liq_xy: List[Tuple[float, float]] = []
        vap_xy: List[Tuple[float, float]] = []
        tie_specs: List[Tuple[Tuple[float, float], Tuple[float, float], float]] = []
        for pnt in pts:
            pd = _as_dict(pnt)
            lxy = _composition_xy_from_point(pd, "liquid_ternary_x", components)
            vxy = _composition_xy_from_point(pd, "vapour_ternary_y", components)
            if lxy is not None:
                liq_xy.append(lxy)
            if vxy is not None:
                vap_xy.append(vxy)
            if lxy is not None and vxy is not None:
                gap = _safe_float(pd.get("tie_line_length"), 0.0) or 0.0
                tie_specs.append((lxy, vxy, gap))

        if boundary_source in {"vapour", "vapor"}:
            main_cloud = vap_xy
        elif boundary_source == "combined":
            main_cloud = liq_xy + vap_xy
        else:
            main_cloud = liq_xy

        if len(main_cloud) < 3:
            continue

        main_path = _smooth_y_envelope_from_cloud(
            main_cloud,
            bin_count=curve_bin_count,
            y_quantile=curve_y_quantile,
            smooth_passes=curve_smooth_passes,
            samples_per_segment=samples_per_segment,
        )
        if len(main_path) >= 2:
            ax.plot(
                [pt[0] for pt in main_path], [pt[1] for pt in main_path],
                color=color, linestyle="-", linewidth=linewidth, alpha=0.96,
                solid_capstyle="round", solid_joinstyle="round", zorder=5,
            )
            plotted_any = True

        if show_vapour_boundary and len(vap_xy) >= 3:
            vap_path = _smooth_y_envelope_from_cloud(
                vap_xy,
                bin_count=curve_bin_count,
                y_quantile=curve_y_quantile,
                smooth_passes=curve_smooth_passes,
                samples_per_segment=samples_per_segment,
            )
            if len(vap_path) >= 2:
                ax.plot(
                    [pt[0] for pt in vap_path], [pt[1] for pt in vap_path],
                    color=color, linestyle="--", linewidth=max(1.0, linewidth * 0.58), alpha=0.42,
                    solid_capstyle="round", solid_joinstyle="round", zorder=4,
                )

        if show_raw_points:
            xs = [p[0] for p in main_cloud]
            ys = [p[1] for p in main_cloud]
            ax.scatter(xs, ys, s=5, color=color, alpha=0.14, edgecolors="none", zorder=2)

        if show_tie_lines and tie_specs:
            tie_specs = sorted(tie_specs, key=lambda item: item[2], reverse=True)
            selected = _evenly_spaced_points([
                {"lxy": lxy, "vxy": vxy, "gap": gap} for lxy, vxy, gap in tie_specs if gap > 1e-8
            ], max_tie_lines_per_pressure)
            for item in selected:
                lxy = item["lxy"]
                vxy = item["vxy"]
                ax.plot(
                    [lxy[0], vxy[0]], [lxy[1], vxy[1]],
                    color=color, linestyle=":", linewidth=0.7, alpha=0.16,
                    solid_capstyle="round", zorder=3,
                )

        pressure_handles.append(Line2D([0], [0], color=color, lw=linewidth, label=f"{p_kpa:g} kPa"))

    if not plotted_any:
        plt.close(fig)
        return None

    ax.legend(handles=pressure_handles, loc="upper right", frameon=False, fontsize=10.5, handlelength=2.4)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(out_dir, f"ternary_phase_diagram_trend_smooth_{ts}.png")
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    return path


def plot_ternary_phase_trend_diagram(results: dict, out_dir: str = ".", **kwargs: Any) -> Optional[str]:
    """Compatibility alias with an explicit name for the simplified ternary plot."""
    return plot_ternary_phase_diagram(results, out_dir=out_dir, **kwargs)


def _infer_controlling_pair_from_ternary_results(results: dict, components: Sequence[str]) -> Optional[Tuple[str, str]]:
    """Infer the most relevant binary pair for ternary boundary visualization."""
    analysis = _as_dict(_as_dict(results).get("ternary_analysis"))
    for item in _as_list(analysis.get("pair_summaries")):
        pair = tuple(_ternary_clean_component(c) for c in _as_list(_as_dict(item).get("pair"))[:2])
        if len(pair) == 2 and pair[0] and pair[1]:
            return pair  # type: ignore[return-value]
    comps = [_ternary_clean_component(c) for c in components if _ternary_clean_component(c)]
    if len(comps) >= 2:
        return (comps[0], comps[1])
    return None


def _log_alpha_values_for_case(case: dict, components: Sequence[str], pair: Tuple[str, str]) -> Tuple[List[float], List[float], List[float], List[dict]]:
    """Return feed-grid xy and log10(alpha_pair) values for one ternary pressure case."""
    xs, ys, pts = _xy_values_for_case(_as_dict(case), components)
    raw_alpha = _scalar_values_for_points(pts, components, "alpha", pair)
    out_x: List[float] = []
    out_y: List[float] = []
    out_v: List[float] = []
    out_p: List[dict] = []
    for x, y, a, p in zip(xs, ys, raw_alpha, pts):
        av = _safe_float(a, None)
        if av is None or av <= 0.0 or math.isnan(av) or math.isinf(av):
            continue
        out_x.append(float(x))
        out_y.append(float(y))
        out_v.append(math.log10(float(av)))
        out_p.append(_as_dict(p))
    return out_x, out_y, out_v, out_p


def _boundary_diverging_cmap():
    """Diverging version of the supplied heatmap palette: purple -> near-white -> magenta."""
    import matplotlib.colors as mcolors
    return mcolors.LinearSegmentedColormap.from_list(
        "lavender_magenta_boundary",
        ["#7A5AA6", "#E8E3EC", "#C22C7A"],
        N=256,
    )


def _boundary_pressure_palette(n: int) -> List[str]:
    """Line palette matching the heatmap reference, from pale lavender to magenta."""
    return _heatmap_reference_pressure_palette(n)


def _alpha_levels_from_results(cases: Sequence[dict], components: Sequence[str], pair: Tuple[str, str]) -> List[float]:
    vals: List[float] = []
    for case in cases:
        _, _, v, _ = _log_alpha_values_for_case(_as_dict(case), components, pair)
        vals.extend([x for x in v if x is not None and not math.isnan(x) and not math.isinf(x)])
    if not vals:
        return [-0.3, -0.15, -0.05, 0.0, 0.05, 0.15, 0.3]
    abs_lim = max(abs(min(vals)), abs(max(vals)), 0.06)
    abs_lim = min(max(abs_lim, 0.10), 0.85)
    return [-abs_lim, -0.60 * abs_lim, -0.30 * abs_lim, -0.12 * abs_lim, 0.0,
            0.12 * abs_lim, 0.30 * abs_lim, 0.60 * abs_lim, abs_lim]


def _ternary_component_pairs(components: Sequence[str]) -> List[Tuple[str, str]]:
    comps = [_ternary_clean_component(c) for c in components if _ternary_clean_component(c)]
    pairs: List[Tuple[str, str]] = []
    for i in range(len(comps)):
        for j in range(i + 1, len(comps)):
            pairs.append((comps[i], comps[j]))
    return pairs


def _pair_display_color_map(pairs: Sequence[Tuple[str, str]]) -> Dict[Tuple[str, str], str]:
    palette = ["#5A3E99", "#B12A90", "#2C8C7A", "#D17A00", "#B74D3A", "#6B6B6B"]
    out: Dict[Tuple[str, str], str] = {}
    for i, pair in enumerate(pairs):
        out[tuple(pair)] = palette[i % len(palette)]
    return out


def _pressure_linestyle_cycle(n: int) -> List[str]:
    styles = ["-", "--", "-.", ":"]
    return [styles[i % len(styles)] for i in range(max(1, n))]


def _pairs_with_alpha_boundary(results: dict, components: Sequence[str]) -> List[Tuple[str, str]]:
    rd = _as_dict(results)
    cases = [_as_dict(c) for c in _as_list(rd.get("cases")) if _two_phase_points(_as_dict(c))]
    found: List[Tuple[str, str]] = []
    for pair in _ternary_component_pairs(components):
        ok = False
        for case in cases:
            _, _, vals, _ = _log_alpha_values_for_case(case, components, pair)
            if len(vals) >= 4 and min(vals) <= 0.0 <= max(vals):
                ok = True
                break
        if ok:
            found.append(pair)
    return found


def _contour_polylines(
        ax: Any,
        xs: Sequence[float],
        ys: Sequence[float],
        vals: Sequence[float],
        *,
        level: float = 0.0,
        connect_segments: bool = True,
        connect_gap: float = 0.035,
) -> List[List[Tuple[float, float]]]:
    segments = _tricontour_segments(ax, xs, ys, vals, level=level)
    if not segments:
        return []
    polylines = _connect_xy_segments(segments, max_gap=connect_gap) if connect_segments else [
        _clean_xy_polyline(seg) for seg in segments
    ]
    return [line for line in polylines if len(line) >= 2]


def _triangle_xy_vertices() -> List[Tuple[float, float]]:
    h = math.sqrt(3.0) / 2.0
    return [(0.0, 0.0), (1.0, 0.0), (0.5, h)]


def _cross2d(a: Sequence[float], b: Sequence[float]) -> float:
    return float(a[0]) * float(b[1]) - float(a[1]) * float(b[0])


def _ray_segment_intersection(p: Tuple[float, float], d: Tuple[float, float], a: Tuple[float, float], b: Tuple[float, float]) -> Optional[Tuple[float, Tuple[float, float]]]:
    r = (float(d[0]), float(d[1]))
    s = (float(b[0] - a[0]), float(b[1] - a[1]))
    denom = _cross2d(r, s)
    if abs(denom) <= 1e-12:
        return None
    ap = (float(a[0] - p[0]), float(a[1] - p[1]))
    t = _cross2d(ap, s) / denom
    u = _cross2d(ap, r) / denom
    if t >= 0.0 and -1e-9 <= u <= 1.0 + 1e-9:
        return t, (p[0] + t * r[0], p[1] + t * r[1])
    return None


def _ray_to_triangle_intersection(p: Tuple[float, float], d: Tuple[float, float]) -> Optional[Tuple[float, float]]:
    tri = _triangle_xy_vertices()
    edges = [(tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])]
    hits = []
    for a, b in edges:
        hit = _ray_segment_intersection(p, d, a, b)
        if hit is None:
            continue
        t, q = hit
        if t > 1e-9:
            hits.append((t, q))
    if not hits:
        return None
    hits.sort(key=lambda item: item[0])
    return hits[0][1]


def _extend_polyline_to_triangle(line: Sequence[Tuple[float, float]], *, max_extension: float = 0.35) -> List[Tuple[float, float]]:
    pts = _clean_xy_polyline(line)
    if len(pts) < 2:
        return pts
    out = list(pts)

    def extend_end(points: List[Tuple[float, float]], at_start: bool) -> List[Tuple[float, float]]:
        if len(points) < 2:
            return points
        if at_start:
            p0, p1 = points[0], points[1]
            d = (p0[0] - p1[0], p0[1] - p1[1])
            base = p0
        else:
            p0, p1 = points[-1], points[-2]
            d = (p0[0] - p1[0], p0[1] - p1[1])
            base = p0
        norm = math.hypot(d[0], d[1])
        if norm <= 1e-12:
            return points
        d = (d[0] / norm, d[1] / norm)
        hit = _ray_to_triangle_intersection(base, d)
        if hit is None:
            return points
        if _xy_distance(base, hit) > max_extension:
            return points
        if _xy_distance(base, hit) <= 1e-5:
            return points
        if at_start:
            return [hit] + points
        return points + [hit]

    out = extend_end(out, True)
    out = extend_end(out, False)
    return _clean_xy_polyline(out)


def plot_ternary_separation_boundary_panels(
        results: dict,
        out_dir: str = ".",
        *,
        pairs: Optional[Sequence[Sequence[str]]] = None,
        max_pressures: int = 4,
        show_sample_points: bool = False,
        show_candidate_star: bool = False,
        show_guidance_text: bool = True,
        show_fill: bool = False,
        fill_pair: Optional[Sequence[str]] = None,
        contour_linewidth: float = 1.9,
        extend_to_boundary: bool = True,
) -> Optional[str]:
    """
    Plot pressure-by-pressure ternary boundary panels with all pressure-sensitive binary pairs.

    Default reading mode:
      * one panel = one pressure;
      * one colored α=1 contour per pressure-sensitive binary pair;
      * optional fill only for a chosen pair if requested explicitly.
    """
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    os.makedirs(out_dir, exist_ok=True)
    rd = _as_dict(results)
    components = [_ternary_clean_component(c) for c in _as_list(rd.get("components"))]
    components = [c for c in components if c]
    if len(components) != 3:
        return None

    cases = sorted(
        [_as_dict(c) for c in _as_list(rd.get("cases")) if _two_phase_points(_as_dict(c))],
        key=lambda c: (_safe_float(c.get("pressure_kpa"), 0.0) or 0.0),
    )
    if not cases:
        return None
    cases = cases[:max(1, int(max_pressures))]

    if pairs:
        pair_list = []
        for pair in pairs:
            pair_tuple = tuple(_ternary_clean_component(c) for c in _as_list(pair)[:2])
            if len(pair_tuple) == 2 and pair_tuple[0] and pair_tuple[1] and pair_tuple not in pair_list:
                pair_list.append(pair_tuple)
    else:
        pair_list = _pairs_with_alpha_boundary(rd, components)
        if not pair_list:
            pair_list = _ternary_component_pairs(components)
    pair_colors = _pair_display_color_map(pair_list)

    fill_pair_tuple = None
    if show_fill:
        if fill_pair:
            cand = tuple(_ternary_clean_component(c) for c in _as_list(fill_pair)[:2])
            if len(cand) == 2 and cand[0] and cand[1]:
                fill_pair_tuple = cand
        if fill_pair_tuple is None and pair_list:
            fill_pair_tuple = pair_list[0]

    plt.rcParams["font.family"] = "Times New Roman"
    plt.rcParams["mathtext.fontset"] = "stix"

    n = len(cases)
    if n <= 2:
        nrows, ncols = 1, n
    else:
        nrows, ncols = 2, int(math.ceil(n / 2.0))
    fig, axes = plt.subplots(nrows, ncols, figsize=(4.4 * ncols, 4.0 * nrows), dpi=300)
    if not isinstance(axes, (list, tuple)):
        try:
            axes_flat = list(axes.ravel())
        except Exception:
            axes_flat = [axes]
    else:
        axes_flat = list(axes)

    fig.patch.set_facecolor("#FBFAFC")
    mappable = None
    plotted_any = False
    fill_levels = _alpha_levels_from_results(cases, components, fill_pair_tuple) if fill_pair_tuple else None
    cmap = _boundary_diverging_cmap() if fill_pair_tuple else None

    for ax, case in zip(axes_flat, cases):
        ax.set_facecolor("#FBFAFC")
        p_kpa = _safe_float(case.get("pressure_kpa"), None)
        _draw_ternary_base(ax, components, title=f"{p_kpa:g} kPa" if p_kpa is not None else "unknown P")
        for line in ax.lines:
            try:
                line.set_color("#1F6FAE")
                line.set_linewidth(1.05)
            except Exception:
                pass

        if fill_pair_tuple:
            xs, ys, vals, _ = _log_alpha_values_for_case(case, components, fill_pair_tuple)
            if len(vals) >= 4 and min(vals) < max(vals):
                try:
                    cf = ax.tricontourf(xs, ys, vals, levels=fill_levels, cmap=cmap, extend="both", alpha=0.30)
                    mappable = cf
                except Exception:
                    pass
            if show_sample_points and xs:
                ax.scatter(xs, ys, s=4, color="white", alpha=0.35, edgecolors="none", zorder=3)

        for pair in pair_list:
            xs, ys, vals, _ = _log_alpha_values_for_case(case, components, pair)
            if len(vals) < 4 or not (min(vals) <= 0.0 <= max(vals)):
                continue
            polylines = _contour_polylines(ax, xs, ys, vals, level=0.0, connect_segments=True, connect_gap=0.045)
            if extend_to_boundary:
                polylines = [_extend_polyline_to_triangle(line) for line in polylines]
            polylines = [line for line in polylines if len(line) >= 2]
            if not polylines:
                continue
            for line in polylines:
                ax.plot([p[0] for p in line], [p[1] for p in line], color=pair_colors[pair], linestyle='-',
                        linewidth=contour_linewidth, alpha=0.96, solid_capstyle='round', solid_joinstyle='round')
            plotted_any = True
            if show_candidate_star:
                cand = _candidate_point_for_pair(case, pair)
                if cand:
                    px, py = dict_to_xy(_as_dict(cand.get("feed_z")), components)
                    ax.scatter([px], [py], s=58, marker='*', color=pair_colors[pair], edgecolors='black',
                               linewidths=0.45, zorder=9)

    for ax in axes_flat[len(cases):]:
        ax.axis("off")

    handles = [Line2D([0], [0], color=pair_colors[p], lw=2.2, label=f"α({p[0]}/{p[1]}) = 1") for p in pair_list]
    fig.suptitle("Ternary separation-boundary panels: all pressure-sensitive binary pairs", fontsize=14, y=0.985)
    fig.legend(handles=handles, loc="upper center", ncol=min(len(handles), 3), frameon=False, fontsize=10.0,
               bbox_to_anchor=(0.5, 0.965))
    if mappable is not None and fill_pair_tuple:
        cbar = fig.colorbar(mappable, ax=axes_flat[:len(cases)], shrink=0.76, pad=0.025, aspect=28)
        cbar.set_label(f"log10(α({fill_pair_tuple[0]}/{fill_pair_tuple[1]}))", fontsize=10.0)
        cbar.ax.tick_params(labelsize=8.5, direction='in')
    if show_guidance_text:
        note = "Each panel is one pressure. Colored lines are α=1 boundaries for all binary pairs that show a contour in the sampled ternary domain."
        fig.text(0.5, 0.025, note, ha='center', va='bottom', fontsize=9.0)
    if not plotted_any:
        plt.close(fig)
        return None

    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(out_dir, f"ternary_separation_boundary_panels_allpairs_{ts}.png")
    fig.tight_layout(rect=(0.0, 0.05, 1.0, 0.93))
    fig.savefig(path, bbox_inches='tight', facecolor=fig.get_facecolor())
    plt.close(fig)
    return path


def _aggregate_pair_log_alpha_across_pressures(
        cases: Sequence[dict],
        components: Sequence[str],
        pair: Tuple[str, str],
) -> Tuple[List[float], List[float], List[float]]:
    """Aggregate log10(alpha) for one pair across pressures on common ternary feed points."""
    bucket: Dict[Tuple[int, ...], dict] = {}
    fallback_idx = 0
    for case in cases:
        xs, ys, vals, pts = _log_alpha_values_for_case(_as_dict(case), components, pair)
        for x, y, v, pt in zip(xs, ys, vals, pts):
            idx = tuple(_as_list(_as_dict(pt).get('grid_index')))
            if not idx:
                idx = (fallback_idx,)
                fallback_idx += 1
            rec = bucket.setdefault(idx, {'x': float(x), 'y': float(y), 'vals': []})
            rec['vals'].append(float(v))
    out_x, out_y, out_v = [], [], []
    for rec in bucket.values():
        vals = [float(v) for v in rec.get('vals', []) if v is not None and not math.isnan(v) and not math.isinf(v)]
        if not vals:
            continue
        out_x.append(float(rec['x']))
        out_y.append(float(rec['y']))
        out_v.append(sum(vals) / len(vals))
    return out_x, out_y, out_v


def _polyline_point_and_angle(line: Sequence[Tuple[float, float]], frac: float = 0.55) -> Optional[Tuple[float, float, float]]:
    pts = _clean_xy_polyline(line)
    if len(pts) < 2:
        return None
    seglens = [_xy_distance(pts[i - 1], pts[i]) for i in range(1, len(pts))]
    total = sum(seglens)
    if total <= 1e-12:
        p0, p1 = pts[0], pts[-1]
        ang = math.degrees(math.atan2(p1[1] - p0[1], p1[0] - p0[0]))
        return p0[0], p0[1], ang
    target = max(0.0, min(1.0, frac)) * total
    acc = 0.0
    for i in range(1, len(pts)):
        seg = seglens[i - 1]
        if acc + seg >= target:
            t = 0.0 if seg <= 1e-12 else (target - acc) / seg
            x = pts[i - 1][0] + t * (pts[i][0] - pts[i - 1][0])
            y = pts[i - 1][1] + t * (pts[i][1] - pts[i - 1][1])
            ang = math.degrees(math.atan2(pts[i][1] - pts[i - 1][1], pts[i][0] - pts[i - 1][0]))
            return x, y, ang
        acc += seg
    p0, p1 = pts[-2], pts[-1]
    ang = math.degrees(math.atan2(p1[1] - p0[1], p1[0] - p0[0]))
    return p1[0], p1[1], ang


def _label_boundary_pressure(ax: Any, line: Sequence[Tuple[float, float]], text: str, color: Any, frac: float) -> None:
    from matplotlib import patheffects as pe
    pa = _polyline_point_and_angle(line, frac=frac)
    if pa is None:
        return
    x, y, ang = pa
    # Offset labels slightly toward the triangle centroid to reduce overlap with boundaries and edges.
    dx = math.cos(math.radians(ang))
    dy = math.sin(math.radians(ang))
    nx, ny = -dy, dx
    cx, cy = 0.5, math.sqrt(3.0) / 6.0
    sign = 1.0 if ((cx - x) * nx + (cy - y) * ny) >= 0.0 else -1.0
    x += sign * 0.012 * nx
    y += sign * 0.012 * ny
    try:
        txt = ax.text(x, y, text, fontsize=8.8, color=color, ha='center', va='center',
                      rotation=ang, rotation_mode='anchor', zorder=9)
        txt.set_path_effects([
            pe.withStroke(linewidth=2.0, foreground='white', alpha=0.96),
        ])
    except Exception:
        ax.text(x, y, text, fontsize=8.8, color=color, ha='center', va='center', zorder=9)


def _inset_triangle_vertices(scale: float = 0.91) -> List[Tuple[float, float]]:
    tri = _triangle_xy_vertices()
    cx, cy = 0.5, math.sqrt(3.0) / 6.0
    return [(cx + scale * (x - cx), cy + scale * (y - cy)) for x, y in tri]


def _clip_contourset_to_polygon(contourset: Any, ax: Any, polygon_xy: Sequence[Tuple[float, float]]) -> None:
    """Clip a Matplotlib contourf result to a polygon path."""
    try:
        from matplotlib.patches import Polygon
        patch = Polygon(list(polygon_xy), closed=True, transform=ax.transData)
        if hasattr(contourset, 'collections'):
            for coll in contourset.collections:
                try:
                    coll.set_clip_path(patch)
                except Exception:
                    pass
    except Exception:
        pass


def _regularize_fill_points(
        xs: Sequence[float],
        ys: Sequence[float],
        vals: Sequence[float],
        *,
        inset_scale: float = 0.91,
        edge_samples: int = 9,
) -> Tuple[List[float], List[float], List[float]]:
    """
    Add synthetic anchor points on a uniformly inset triangle so the filled field
    approaches the phase-diagram boundary with a nearly uniform gap.
    """
    base = [
        (float(x), float(y), float(v))
        for x, y, v in zip(xs, ys, vals)
        if x is not None and y is not None and v is not None and not math.isnan(float(v)) and not math.isinf(float(v))
    ]
    if len(base) < 3:
        return list(xs), list(ys), list(vals)

    out = list(base)
    tri = _inset_triangle_vertices(scale=inset_scale)
    pts = tri + [tri[0]]
    edge_samples = max(2, int(edge_samples))

    def nearest_val(qx: float, qy: float) -> float:
        best = min(base, key=lambda item: (item[0] - qx) ** 2 + (item[1] - qy) ** 2)
        return float(best[2])

    # Inset triangle vertices and edge samples.
    for i in range(3):
        p0 = pts[i]
        p1 = pts[i + 1]
        for k in range(edge_samples):
            t = k / float(edge_samples - 1)
            qx = p0[0] + t * (p1[0] - p0[0])
            qy = p0[1] + t * (p1[1] - p0[1])
            out.append((qx, qy, nearest_val(qx, qy)))

    # Add centroid anchor with mean value for smoother center fill.
    cx, cy = 0.5, math.sqrt(3.0) / 6.0
    out.append((cx, cy, sum(v for _, _, v in base) / len(base)))

    out_x = [p[0] for p in out]
    out_y = [p[1] for p in out]
    out_v = [p[2] for p in out]
    return out_x, out_y, out_v


def _ternary_inset_xy(x: float, y: float, scale: float = 0.88) -> Tuple[float, float]:
    cx, cy = 0.5, math.sqrt(3.0) / 6.0
    return cx + float(scale) * (float(x) - cx), cy + float(scale) * (float(y) - cy)


def _ternary_regular_grid_xy(n: int, *, scale: float = 1.0) -> Tuple[List[float], List[float]]:
    xs: List[float] = []
    ys: List[float] = []
    n = max(2, int(n))
    for i in range(n + 1):
        for j in range(n + 1 - i):
            k = n - i - j
            a = i / n
            b = j / n
            c = k / n
            x, y = ternary_to_xy(a, b, c)
            if abs(scale - 1.0) > 1e-12:
                x, y = _ternary_inset_xy(x, y, scale=scale)
            xs.append(float(x))
            ys.append(float(y))
    return xs, ys


def _idw_interpolate_xy(
        sample_x: Sequence[float],
        sample_y: Sequence[float],
        sample_v: Sequence[float],
        query_x: Sequence[float],
        query_y: Sequence[float],
        *,
        power: float = 2.0,
        neighbors: int = 18,
) -> List[float]:
    finite_samples = [
        (float(x), float(y), float(v))
        for x, y, v in zip(sample_x, sample_y, sample_v)
        if x is not None and y is not None and v is not None and not math.isnan(float(v)) and not math.isinf(float(v))
    ]
    if not finite_samples:
        return [float('nan')] * len(list(query_x))
    neighbors = max(1, min(int(neighbors), len(finite_samples)))
    out: List[float] = []
    for qx, qy in zip(query_x, query_y):
        distances = []
        for sx, sy, sv in finite_samples:
            d2 = (sx - float(qx)) ** 2 + (sy - float(qy)) ** 2
            distances.append((d2, sv))
        distances.sort(key=lambda item: item[0])
        if distances[0][0] <= 1e-16:
            out.append(float(distances[0][1]))
            continue
        use = distances[:neighbors]
        weights = [1.0 / (max(d2, 1e-16) ** (power / 2.0)) for d2, _ in use]
        value = sum(w * v for w, (_, v) in zip(weights, use)) / sum(weights)
        out.append(float(value))
    return out


def _resample_polyline_uniform(
        line: Sequence[Tuple[float, float]],
        *,
        n_points: int,
) -> List[Tuple[float, float]]:
    """Resample a polyline approximately uniformly along arc length."""
    pts = _clean_xy_polyline(line, min_step=1e-8)
    if len(pts) < 2:
        return pts
    n_points = max(2, int(n_points))
    seg_lengths = [0.0]
    for i in range(1, len(pts)):
        seg_lengths.append(seg_lengths[-1] + _xy_distance(pts[i - 1], pts[i]))
    total = seg_lengths[-1]
    if total <= 1e-12:
        return [pts[0]] * n_points
    out: List[Tuple[float, float]] = []
    targets = [total * i / float(n_points - 1) for i in range(n_points)]
    j = 1
    for t in targets:
        while j < len(seg_lengths) - 1 and seg_lengths[j] < t:
            j += 1
        t0, t1 = seg_lengths[j - 1], seg_lengths[j]
        p0, p1 = pts[j - 1], pts[j]
        frac = 0.0 if t1 <= t0 + 1e-12 else (t - t0) / (t1 - t0)
        x = p0[0] + frac * (p1[0] - p0[0])
        y = p0[1] + frac * (p1[1] - p0[1])
        out.append((x, y))
    return _clean_xy_polyline(out, min_step=1e-8)


def _smooth_boundary_polyline(
        line: Sequence[Tuple[float, float]],
        *,
        smooth_passes: int = 4,
        samples_per_segment: int = 14,
) -> List[Tuple[float, float]]:
    """Restore a visibly smooth internal contour by resampling + stronger display smoothing."""
    pts = _clean_xy_polyline(line, min_step=1e-7)
    if len(pts) < 3:
        return pts
    # First regularize point spacing; the IDW contour extraction tends to produce
    # uneven short-wavelength wiggles if the raw nodes are used directly.
    approx_n = max(18, min(72, int(_polyline_length(pts) / 0.012)))
    pts = _resample_polyline_uniform(pts, n_points=approx_n)
    pts = _moving_average_xy(pts, passes=smooth_passes)
    pts = _catmull_rom_spline_xy(pts, samples_per_segment=samples_per_segment)
    pts = _moving_average_xy(pts, passes=1)
    return _clean_xy_polyline(pts, min_step=1e-7)


def _label_contour_lines(ax: Any, contour_set: Any, label: str, color: Any) -> None:
    """Directly label α=1 contour lines without a legend box."""
    try:
        labels = ax.clabel(
            contour_set,
            contour_set.levels,
            fmt={0.0: label},
            inline=True,
            fontsize=7.8,
            colors=[color],
            inline_spacing=4,
        )
        for t in labels:
            try:
                t.set_rotation_mode('anchor')
                t.set_bbox(dict(facecolor='none', edgecolor='none', pad=0.0))
            except Exception:
                pass
    except Exception:
        pass


def _render_pair_boundary_overlay_on_ax(
        ax: Any,
        *,
        components: Sequence[str],
        pair: Tuple[str, str],
        cases: Sequence[dict],
        fill_levels: Sequence[float],
        cmap: Any,
        pressure_colors: Sequence[Any],
        extend_to_boundary: bool,
        show_candidate_shift: bool,
        fill_inset_scale: float = 0.88,
        fill_grid_n: int = 75,
        boundary_grid_n: int = 95,
        idw_power: float = 2.0,
        idw_neighbors: int = 18,
) -> Tuple[Optional[Any], bool]:
    import matplotlib.tri as mtri

    ax.set_facecolor("#FBFAFC")
    _draw_ternary_base(ax, components, title=f"α({pair[0]}/{pair[1]}) = 1")
    for line in ax.lines:
        try:
            line.set_color("#1F6FAE")
            line.set_linewidth(1.10)
        except Exception:
            pass

    # Filled field: evaluate on a deterministic inset triangle to force a uniform gap from the outer frame.
    xs_fill_s, ys_fill_s, vals_fill_s = _aggregate_pair_log_alpha_across_pressures(cases, components, pair)
    fill_x, fill_y = _ternary_regular_grid_xy(fill_grid_n, scale=fill_inset_scale)
    fill_v = _idw_interpolate_xy(xs_fill_s, ys_fill_s, vals_fill_s, fill_x, fill_y,
                                 power=idw_power, neighbors=idw_neighbors)
    mappable = None
    plotted_any = False
    finite_fill = [float(v) for v in fill_v if v is not None and not math.isnan(float(v)) and not math.isinf(float(v))]
    if len(finite_fill) >= 4 and min(finite_fill) < max(finite_fill):
        try:
            fill_tri = mtri.Triangulation(fill_x, fill_y)
            cf = ax.tricontourf(fill_tri, fill_v, levels=fill_levels, cmap=cmap, extend='both', alpha=0.86)
            mappable = cf
        except Exception:
            pass

    # Boundary lines: use the old direct triangulated contour source, not the IDW grid.
    # The IDW grid is retained only for the filled field.  Direct contours from the
    # original Aspen sample points reproduce the earlier smoother boundary topology
    # and avoid the high-frequency wiggles introduced by full-grid IDW contours.
    for idx, case in enumerate(cases):
        p_kpa = _safe_float(_as_dict(case).get('pressure_kpa'), None)
        color = pressure_colors[idx]
        xs, ys, vals, _ = _log_alpha_values_for_case(_as_dict(case), components, pair)
        vals_finite = [float(v) for v in vals if v is not None and not math.isnan(float(v)) and not math.isinf(float(v))]
        if len(vals_finite) < 4 or not (min(vals_finite) <= 0.0 <= max(vals_finite)):
            continue

        # This is the old pathway: tricontour on raw sampled ternary points,
        # segment connection, then display-only smoothing.
        polylines = _contour_polylines(ax, xs, ys, vals, level=0.0, connect_segments=True, connect_gap=0.045)
        if not polylines:
            continue
        if extend_to_boundary:
            polylines = [_extend_polyline_to_triangle(line) for line in polylines]

        smoothed: List[List[Tuple[float, float]]] = []
        for line in polylines:
            # Fewer resampled points than v39 and stronger moving average gives a
            # visually smooth engineering boundary similar to the previous version.
            sline = _smooth_boundary_polyline(line, smooth_passes=5, samples_per_segment=18)
            if extend_to_boundary:
                sline = _extend_polyline_to_triangle(sline)
            if len(sline) >= 2:
                smoothed.append(sline)
        if not smoothed:
            continue
        smoothed.sort(key=lambda line: _polyline_length(line), reverse=True)

        for line in smoothed:
            ax.plot([p[0] for p in line], [p[1] for p in line], color=color, linestyle='-', linewidth=2.15,
                    alpha=0.96, solid_capstyle='round', solid_joinstyle='round', zorder=6)
            plotted_any = True
        if p_kpa is not None:
            _label_boundary_pressure(ax, smoothed[0], f"{p_kpa:g}", color, 0.56)
        if show_candidate_shift:
            cand = _candidate_point_for_pair(_as_dict(case), pair)
            if cand:
                px, py = dict_to_xy(_as_dict(cand.get('feed_z')), components)
                ax.scatter([px], [py], s=34, marker='*', color=color, edgecolors='black', linewidths=0.35, zorder=7)
    return mappable, plotted_any


def plot_ternary_separation_boundary_overlay(
        results: dict,
        out_dir: str = ".",
        *,
        pairs: Optional[Sequence[Sequence[str]]] = None,
        max_pressures: int = 4,
        show_candidate_shift: bool = False,
        extend_to_boundary: bool = True,
        fill_inset_scale: float = 0.88,
        fill_grid_n: int = 75,
        boundary_grid_n: int = 95,
        idw_power: float = 2.0,
        idw_neighbors: int = 18,
) -> Optional[str]:
    """
    Generate ternary α=1 boundary overlays using an IDW-smoothed rendering model
    validated in the standalone hard-coded plotting demo.

    Key design choices:
      1) the filled heat map is evaluated on a deterministic inset triangle so the
         gap to the outer ternary frame is uniform across all three sides;
      2) α=1 boundaries are evaluated on a full-triangle grid so the contours can
         reach the outer blue frame naturally;
      3) each binary pair is also saved as a separate PNG in addition to a
         triptych overview.
    """
    import matplotlib.pyplot as plt

    os.makedirs(out_dir, exist_ok=True)
    rd = _as_dict(results)
    components = [_ternary_clean_component(c) for c in _as_list(rd.get("components"))]
    components = [c for c in components if c]
    if len(components) != 3:
        return None
    cases = sorted(
        [_as_dict(c) for c in _as_list(rd.get("cases")) if _two_phase_points(_as_dict(c))],
        key=lambda c: (_safe_float(c.get("pressure_kpa"), 0.0) or 0.0),
    )[:max(1, int(max_pressures))]
    if not cases:
        return None

    if pairs:
        pair_list: List[Tuple[str, str]] = []
        for pair in pairs:
            pair_tuple = tuple(_ternary_clean_component(c) for c in _as_list(pair)[:2])
            if len(pair_tuple) == 2 and pair_tuple[0] and pair_tuple[1] and pair_tuple not in pair_list:
                pair_list.append(pair_tuple)
    else:
        pair_list = _ternary_component_pairs(components)
    pair_list = pair_list[:3]
    if not pair_list:
        return None

    plt.rcParams["font.family"] = "Times New Roman"
    plt.rcParams["mathtext.fontset"] = "stix"
    pressure_colors = _boundary_pressure_palette(len(cases))
    cmap = _boundary_diverging_cmap()

    global_abs = 0.10
    for pair in pair_list:
        _, _, vals_fill = _aggregate_pair_log_alpha_across_pressures(cases, components, pair)
        finite_vals = [float(v) for v in vals_fill if v is not None and not math.isnan(float(v)) and not math.isinf(float(v))]
        if finite_vals:
            global_abs = max(global_abs, max(abs(min(finite_vals)), abs(max(finite_vals))))
    global_abs = min(max(global_abs, 0.12), 0.85)
    fill_levels = [
        -global_abs, -0.60 * global_abs, -0.30 * global_abs, -0.12 * global_abs,
        0.0,
        0.12 * global_abs, 0.30 * global_abs, 0.60 * global_abs, global_abs,
    ]

    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    generated_pair_paths: List[str] = []
    any_plotted = False

    for pair in pair_list:
        fig_i, ax_i = plt.subplots(1, 1, figsize=(5.25, 5.20), dpi=300)
        fig_i.patch.set_facecolor("#FBFAFC")
        mappable_i, plotted_i = _render_pair_boundary_overlay_on_ax(
            ax_i,
            components=components,
            pair=pair,
            cases=cases,
            fill_levels=fill_levels,
            cmap=cmap,
            pressure_colors=pressure_colors,
            extend_to_boundary=extend_to_boundary,
            show_candidate_shift=show_candidate_shift,
            fill_inset_scale=fill_inset_scale,
            fill_grid_n=fill_grid_n,
            boundary_grid_n=boundary_grid_n,
            idw_power=idw_power,
            idw_neighbors=idw_neighbors,
        )
        if plotted_i:
            any_plotted = True
            if mappable_i is not None:
                cbar = fig_i.colorbar(mappable_i, ax=ax_i, orientation='horizontal', fraction=0.060, pad=0.11, aspect=34)
                cbar.set_label("IDW-smoothed mean log10(α) field; fixed inset fill region", fontsize=9.0)
                cbar.ax.tick_params(labelsize=7.8, direction='in')
            fig_i.subplots_adjust(top=0.86, bottom=0.17, left=0.06, right=0.97)
            pair_key = f"{pair[0]}_{pair[1]}"
            pair_path = os.path.join(out_dir, f"ternary_alpha_boundary_overlay_{pair_key}_{ts}.png")
            fig_i.savefig(pair_path, bbox_inches='tight', facecolor=fig_i.get_facecolor())
            generated_pair_paths.append(pair_path)
        plt.close(fig_i)

    if not any_plotted:
        return None

    n = len(pair_list)
    fig, axes = plt.subplots(1, n, figsize=(4.8 * n, 4.95), dpi=300)
    if n == 1:
        axes = [axes]
    else:
        try:
            axes = list(axes.ravel())
        except Exception:
            axes = list(axes)
    fig.patch.set_facecolor("#FBFAFC")
    last_mappable = None
    for ax, pair in zip(axes, pair_list):
        ax.set_facecolor("#FBFAFC")
        mappable, plotted = _render_pair_boundary_overlay_on_ax(
            ax,
            components=components,
            pair=pair,
            cases=cases,
            fill_levels=fill_levels,
            cmap=cmap,
            pressure_colors=pressure_colors,
            extend_to_boundary=extend_to_boundary,
            show_candidate_shift=show_candidate_shift,
            fill_inset_scale=fill_inset_scale,
            fill_grid_n=fill_grid_n,
            boundary_grid_n=boundary_grid_n,
            idw_power=idw_power,
            idw_neighbors=idw_neighbors,
        )
        if mappable is not None:
            last_mappable = mappable
        any_plotted = any_plotted or plotted
    p_min = float(_as_dict(cases[0]).get('pressure_kpa') or 0.0)
    p_max = float(_as_dict(cases[-1]).get('pressure_kpa') or 0.0)
    fig.suptitle(f"Ternary separation-boundary overlay ({p_min:g}–{p_max:g} kPa)", fontsize=14, y=0.97)
    if last_mappable is not None:
        cbar = fig.colorbar(last_mappable, ax=axes, orientation='horizontal', fraction=0.055, pad=0.11, aspect=42)
        cbar.set_label("IDW-smoothed mean log10(α) field; fixed inset fill region", fontsize=9.4)
        cbar.ax.tick_params(labelsize=8.0, direction='in')
    fig.subplots_adjust(top=0.84, bottom=0.18, left=0.025, right=0.985, wspace=0.11)
    combined_path = os.path.join(out_dir, f"ternary_alpha_boundary_overlay_triptych_{ts}.png")
    fig.savefig(combined_path, bbox_inches='tight', facecolor=fig.get_facecolor())
    plt.close(fig)

    manifest_path = os.path.join(out_dir, f"ternary_alpha_boundary_overlay_manifest_{ts}.txt")
    with open(manifest_path, 'w', encoding='utf-8') as mf:
        mf.write("Per-pair overlay PNGs:\n")
        for p in generated_pair_paths:
            mf.write(p + "\n")
        mf.write("\nCombined overview PNG:\n")
        mf.write(combined_path + "\n")


    return combined_path

def _component_value_from_ternary_composition(comp_dict: Any, component: str) -> Optional[float]:
    """Read a component value from a ternary composition dict with canonical name matching."""
    d = _as_dict(comp_dict)
    if not d:
        return None
    target = _ternary_clean_component(component)
    if not target:
        return None

    # Fast path: exact key.
    val = _safe_float(d.get(target), None)
    if val is not None:
        return val

    # Robust path: Aspen/component aliases and mixed key casing.
    for key, raw in d.items():
        if _ternary_clean_component(key) == target:
            return _safe_float(raw, None)
    return None


def _pair_normalized_fraction_from_ternary(
        comp_dict: Any,
        pair: Sequence[str],
        *,
        eps: float = 1e-12,
) -> Optional[float]:
    """
    Project a ternary liquid/vapour composition onto one binary pair.

    For pair (i, j), return z_i / (z_i + z_j).  This is used only for the
    diagnostic projected y-x figure, not for the alpha=1 boundary calculation.
    Missing component keys are handled conservatively: if both pair components
    are absent the projection is invalid; if only one is absent it is treated as
    zero after canonical-name matching.
    """
    pair_clean = [_ternary_clean_component(c) for c in _as_list(pair)[:2]]
    if len(pair_clean) != 2 or not pair_clean[0] or not pair_clean[1]:
        return None

    v0 = _component_value_from_ternary_composition(comp_dict, pair_clean[0])
    v1 = _component_value_from_ternary_composition(comp_dict, pair_clean[1])
    if v0 is None and v1 is None:
        return None
    a = 0.0 if v0 is None else max(0.0, float(v0))
    b = 0.0 if v1 is None else max(0.0, float(v1))
    den = a + b
    if den <= eps:
        return None
    return max(0.0, min(1.0, a / den))

def plot_ternary_yx_projection(results: dict, out_dir: str = ".") -> Optional[str]:
    """
    Plot a 1x3 diagnostic y-x projection from ternary Flash results.

    Each panel projects ternary liquid/vapour compositions onto one binary pair:
        x_i' = x_i / (x_i + x_j),  y_i' = y_i / (y_i + y_j)

    This is not a replacement for a rigorous residue-curve map. It is a compact
    screening plot for pressure-sensitive volatility/topology changes after the
    merged ternary pressure map has been generated.
    """
    import matplotlib.pyplot as plt

    os.makedirs(out_dir, exist_ok=True)
    rd = _as_dict(results)
    components = [_ternary_clean_component(c) for c in _as_list(rd.get("components"))]
    components = [c for c in components if c]
    if len(components) < 3:
        return None

    pairs: List[Tuple[str, str]] = []
    for i in range(len(components)):
        for j in range(i + 1, len(components)):
            pairs.append((components[i], components[j]))
    pairs = pairs[:3]
    if not pairs:
        return None

    cases = sorted(
        _as_list(rd.get("cases")),
        key=lambda c: (_safe_float(_as_dict(c).get("pressure_kpa"), 0.0) or 0.0),
    )
    if not cases:
        return None

    plt.rcParams["font.family"] = "Times New Roman"
    plt.rcParams["mathtext.fontset"] = "stix"

    fig, axes = plt.subplots(1, len(pairs), figsize=(5.0 * len(pairs), 4.4), dpi=300, sharex=True, sharey=True)
    if len(pairs) == 1:
        axes = [axes]

    plotted_any = False
    for ax, pair in zip(axes, pairs):
        ax.plot([0.0, 1.0], [0.0, 1.0], linestyle="--", linewidth=1.0, label="y = x")

        for case in cases:
            case_d = _as_dict(case)
            p_kpa = _safe_float(case_d.get("pressure_kpa"), None)
            pts_xy: List[Tuple[float, float]] = []
            for point in _two_phase_points(case_d):
                pd = _as_dict(point)
                x_proj = _pair_normalized_fraction_from_ternary(pd.get("liquid_ternary_x") or {}, pair)
                y_proj = _pair_normalized_fraction_from_ternary(pd.get("vapour_ternary_y") or {}, pair)
                if x_proj is None or y_proj is None:
                    continue
                pts_xy.append((x_proj, y_proj))

            if not pts_xy:
                continue
            pts_xy.sort(key=lambda t: t[0])
            xs = [t[0] for t in pts_xy]
            ys = [t[1] for t in pts_xy]
            label = f"{p_kpa:g} kPa" if p_kpa is not None else "unknown P"
            ax.plot(xs, ys, marker="o", markersize=2.2, linewidth=1.2, label=label)
            plotted_any = True

        ax.set_title(f"{pair[0]} / {pair[1]}", fontsize=12)
        ax.set_xlabel(f"x'({pair[0]})", fontsize=11)
        ax.set_xlim(0.0, 1.0)
        ax.set_ylim(0.0, 1.0)
        ax.tick_params(direction="in", labelsize=10)
        for spine in ax.spines.values():
            spine.set_linewidth(0.9)

    axes[0].set_ylabel("y' of first component", fontsize=11)
    handles, labels = axes[-1].get_legend_handles_labels()
    if handles:
        # Keep one consolidated legend to avoid repeated labels across the 1x3 panel.
        fig.legend(handles, labels, loc="upper center", ncol=min(len(labels), 5), frameon=False, fontsize=10)

    fig.suptitle("Ternary Flash projected y-x diagnostics", fontsize=13, y=1.03)
    fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.94))

    if not plotted_any:
        plt.close(fig)
        return None

    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(out_dir, f"ternary_projected_yx_1x3_{ts}.png")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


def save_ternary_summary_markdown(results: dict, out_dir: str = ".") -> str:
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(out_dir, f"ternary_flash_analysis_summary_{ts}.md")
    analysis = results.get("ternary_analysis", {})
    lines = ["# Flash-derived ternary thermodynamic-topology screening", ""]
    lines.append("## Plot-reading logic")
    lines.append("- y-x plots provide the clearest local pressure-sensitivity evidence for each component pair.")
    lines.append(
        "- Ternary maps follow the conceptual-design logic of ED literature: unidistribution lines Ki=1, univolatility lines αij=1, and an RCM-like vector dx/dh=x-y.")
    lines.append(
        "- Lines are generated from Flash-derived x/y data, so they are screening proxies, not strict residue-curve or distillation-boundary calculations.")
    lines.append("")
    lines.append("## Overall summary")
    for line in analysis.get("summary_lines", []):
        lines.append(f"- {line}")
    lines.append("")
    lines.append("## Pressure-case summaries")
    for cs in analysis.get("case_summaries", []):
        cs = _as_dict(cs)
        lines.append(f"### P = {cs.get('pressure_kpa')} kPa")
        lines.append(f"- two_phase_fraction: {cs.get('two_phase_fraction')}")
        lines.append(f"- mean_tie_line_length: {cs.get('mean_tie_line_length')}")
        lines.append(f"- max_tie_line_length: {cs.get('max_tie_line_length')}")
        lines.append(f"- dominant_k_component_counts: {cs.get('dominant_k_component_counts')}")
        lines.append(f"- boundary_proxy_segment_count: {cs.get('boundary_proxy_segment_count')}")
        cands = _as_list(cs.get("azeotrope_like_candidates"))
        if cands:
            lines.append("- low-driving-force candidates, ranked by smallest ||y-x||:")
            for i, cand in enumerate(cands[:5], start=1):
                lines.append(
                    f"  - #{i}: gap={cand.get('gap_norm')}; feed_z={cand.get('feed_z')}; x={cand.get('liquid_ternary_x')}; y={cand.get('vapour_ternary_y')}")
        lines.append("")
    ps_list = _as_list(analysis.get("pressure_shift"))
    if ps_list:
        ps = _as_dict(ps_list[0])
        lines.append("## Pressure-shift guidance")
        lines.append(f"- common_grid_points: {ps.get('common_grid_points')}")
        lines.append(f"- dominant_region_flip_count: {ps.get('dominant_region_flip_count')}")
        lines.append(f"- dominant_region_flip_fraction: {ps.get('dominant_region_flip_fraction')}")
        lines.append(f"- mean_vapour_composition_shift: {ps.get('mean_vapour_composition_shift')}")
        lines.append(f"- mean_tie_line_length_change: {ps.get('mean_tie_line_length_change')}")
        lines.append(
            "- engineering reading: prioritize zones where αij=1 curves move, vapor composition shift is large, and low-driving-force points concentrate.")
        lines.append("")
    lines.append("## Method limitation")
    for line in analysis.get("limitations", []):
        lines.append(f"- {line}")
    lines.append(
        "- For final PSD/EPSD feasibility, validate selected routes with RadFrac and, if available, Aspen Property Analysis residue-curve/distillation-boundary tools.")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def _case_pair_key(case: dict) -> str:
    pair = [_canonical_component_name(x) for x in _as_list(_as_dict(case).get("pair"))[:2]]
    return "/".join(pair) if len(pair) == 2 else ""


def _binary_pair_order_from_results(results: dict, pair_summaries: Sequence[dict]) -> List[dict]:
    """Return pair summaries in native scan/input order instead of score-sorted order."""
    rd = _as_dict(results)
    summary_by_key = {str(_as_dict(p).get("pair_key") or ""): _as_dict(p) for p in pair_summaries}
    ordered_keys: List[str] = []

    # Preferred order: first appearance of cases. build_pressure_flash_payload_from_state
    # creates all pressures for pair 1, then pair 2, then pair 3, preserving component order.
    for case in _as_list(rd.get("cases")):
        key = _case_pair_key(case)
        if key and key in summary_by_key and key not in ordered_keys:
            ordered_keys.append(key)

    # Fallback: any remaining summaries keep their existing analysis order.
    for ps in pair_summaries:
        key = str(_as_dict(ps).get("pair_key") or "")
        if key and key not in ordered_keys:
            ordered_keys.append(key)

    return [summary_by_key[k] for k in ordered_keys if k in summary_by_key]


def plot_binary_yx_with_metrics(results: dict, out_dir: str = ".", max_pairs: int = 3) -> Optional[str]:
    import matplotlib.pyplot as plt

    plt.rcParams['font.family'] = 'Times New Roman'
    plt.rcParams['mathtext.fontset'] = 'stix'

    analysis = _as_dict(_as_dict(results).get("curve_analysis"))
    pair_summaries = _as_list(analysis.get("pair_summaries"))
    if not pair_summaries:
        return None

    os.makedirs(out_dir, exist_ok=True)
    selected = _binary_pair_order_from_results(results, pair_summaries)[:max_pairs]
    fig, axes = plt.subplots(1, len(selected), figsize=(4.5 * len(selected), 4.0), dpi=300)
    if len(selected) == 1:
        axes = [axes]

    cases = _as_list(_as_dict(results).get("cases"))
    for ax, ps_raw in zip(axes, selected):
        ps = _as_dict(ps_raw)
        pair_key = str(ps.get("pair_key") or "")
        pair_cases = [
            c for c in cases
            if "/".join([str(x) for x in _as_list(_as_dict(c).get("pair"))[:2]]) == pair_key
        ]
        pair_cases = sorted(pair_cases, key=lambda c: float(_as_dict(c).get("pressure_kpa") or 0.0))
        ax.plot([0, 1], [0, 1], linestyle="--", color="0.45", linewidth=1.0, label="y = x")

        for case in pair_cases:
            cd = _as_dict(case)
            p_kpa = float(cd.get("pressure_kpa") or 0.0)
            pts = _sorted_valid_points(cd)
            if not pts:
                continue

            raw_xs = [float(pt.get("x") or 0.0) for pt in pts]
            raw_ys = [float(pt.get("y") or 0.0) for pt in pts]
            ext_xs = [0.0] + raw_xs + [1.0]
            ext_ys = [0.0] + raw_ys + [1.0]

            line = ax.plot(ext_xs, ext_ys, linestyle="-", linewidth=1.5, label=f"{p_kpa:g} kPa")
            color = line[0].get_color()
            ax.plot(raw_xs, raw_ys, marker="o", linestyle="none", markersize=4, color=color)

            cross = _interp_crossing_x(pts)
            if cross is not None:
                ax.scatter([cross], [cross], s=40, marker="s", c="black", zorder=5)

        c1, c2 = pair_key.split('/')
        ax.set_title(f"{c1} / {c2}", fontsize=12)
        ax.set_xlabel(f"x({c1}) liquid", fontsize=11)
        ax.set_ylabel(f"y({c1}) vapor", fontsize=11)
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.tick_params(direction="in", labelsize=10)
        ax.grid(alpha=0.2)
        ax.legend(loc="lower right", fontsize=9, frameon=False)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(out_dir, f"binary_yx_engineering_{ts}.png")
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


def save_binary_crossing_table_csv(results: dict, out_dir: str = ".", prefix: str = "binary_yx_crossing_table") -> str:
    """Save per-case y=x crossing diagnostics for checking azeotrope markers."""
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(out_dir, f"{prefix}_{ts}.csv")
    fields = [
        "pair_key", "pressure_kpa", "point_count", "crossing_detected", "crossing_x",
        "min_y_minus_x", "max_y_minus_x", "first_x", "last_x", "scan_profile",
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for case in _as_list(_as_dict(results).get("cases")):
            cd = _as_dict(case)
            pts = _sorted_valid_points(cd)
            gaps = [_safe_float(p.get("y_minus_x"), None) for p in pts]
            gaps = [g for g in gaps if g is not None]
            cross = _interp_crossing_x(pts)
            writer.writerow({
                "pair_key": _case_pair_key(cd),
                "pressure_kpa": _safe_float(cd.get("pressure_kpa"), None),
                "point_count": len(pts),
                "crossing_detected": cross is not None,
                "crossing_x": cross,
                "min_y_minus_x": min(gaps) if gaps else None,
                "max_y_minus_x": max(gaps) if gaps else None,
                "first_x": pts[0].get("x") if pts else None,
                "last_x": pts[-1].get("x") if pts else None,
                "scan_profile": _as_dict(results).get("scan_profile") or _as_dict(results).get("payload_scan_profile"),
            })
    return path


# ===========================================================================
# v39 component-ID resolution override
# ===========================================================================
# The previous built-in EAc mapping forced ETOAC -> ETHYL-01 even when that row
# was not the ethyl-acetate component used by the active Aspen template.  v39
# resolves the Aspen row from the template first.  A guessed row is never added
# for EAc.  When the template does not already contain an unambiguous match, the
# caller must provide the exact ID through component_aliases.json,
# state_dict["component_alias_overrides"], or demo --eac-aspen-id.

_LEGACY_BLOCKED_ASPEN_IDS = {"ETOAC": {"ETHYL-01"}}
_V39_ORIGINAL_BUILD_PRESSURE_PAYLOAD = build_pressure_flash_payload_from_state
_V39_ORIGINAL_BUILD_COMBINED_PAYLOAD = build_combined_pressure_flash_payload_from_state
_V39_ORIGINAL_RUN_PAYLOAD = AspenPressureFlashScreenRunner.run_payload


def _v39_normalized_id(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _v39_as_list_of_ids(value: Any) -> List[str]:
    out: List[str] = []
    for item in _as_list(value):
        cid = _clean_id(item)
        if cid and cid not in out:
            out.append(cid)
    return out


def _v39_component_alias_records() -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    loaded_path = None
    for path in _component_alias_file_candidates():
        if not path or not os.path.exists(path):
            continue
        try:
            raw = json.loads(open(path, "r", encoding="utf-8").read())
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        loaded_path = path
        for alias, info in raw.items():
            if not isinstance(info, dict):
                continue
            bo_id = _clean_id(info.get("bo_id") or alias)
            explicit_id = _clean_id(info.get("aspen_id"))
            candidates = _v39_as_list_of_ids(info.get("aspen_candidates"))
            if explicit_id and explicit_id not in candidates:
                candidates.insert(0, explicit_id)
            records.append({
                "alias": str(alias),
                "bo_id": bo_id,
                "display_name": str(info.get("display_name") or bo_id),
                "aspen_key": str(info.get("aspen_key") or ""),
                "aspen_id": explicit_id,
                "aspen_candidates": candidates,
                "source": f"file:{path}",
                "explicit_aspen_id": bool(explicit_id),
            })
        break

    # EAc intentionally has no built-in exact Aspen ID.  These strings are only
    # used to match rows already present in the template; they are never inserted.
    fallback: List[Dict[str, Any]] = [
        {"alias": "MeOH", "bo_id": "METHANOL", "display_name": "METHANOL", "aspen_id": "METHANOL", "aspen_candidates": ["METHANOL"], "source": "builtin", "explicit_aspen_id": True},
        {"alias": "EtOH", "bo_id": "ETHANOL", "display_name": "ETHANOL", "aspen_id": "ETHANOL", "aspen_candidates": ["ETHANOL"], "source": "builtin", "explicit_aspen_id": True},
        {"alias": "EAc", "bo_id": "ETOAC", "display_name": "EAc", "aspen_id": "", "aspen_candidates": ["EAC", "ETAC", "ETOAC", "ETHYLAC", "ETHYL-AC", "ETHYLACET", "ETHYL-ACET", "ETHYLACETATE", "ETHYL-ACETATE"], "source": "builtin-template-match", "explicit_aspen_id": False},
        {"alias": "EA", "bo_id": "ETOAC", "display_name": "EAc", "aspen_id": "", "aspen_candidates": ["EAC", "ETAC", "ETOAC", "ETHYLAC", "ETHYL-AC", "ETHYLACET", "ETHYL-ACET", "ETHYLACETATE", "ETHYL-ACETATE"], "source": "builtin-template-match", "explicit_aspen_id": False},
        {"alias": "ethyl acetate", "bo_id": "ETOAC", "display_name": "EAc", "aspen_id": "", "aspen_candidates": ["EAC", "ETAC", "ETOAC", "ETHYLAC", "ETHYL-AC", "ETHYLACET", "ETHYL-ACET", "ETHYLACETATE", "ETHYL-ACETATE"], "source": "builtin-template-match", "explicit_aspen_id": False},
        {"alias": "water", "bo_id": "WATER", "display_name": "WATER", "aspen_id": "WATER", "aspen_candidates": ["WATER", "H2O"], "source": "builtin", "explicit_aspen_id": True},
        {"alias": "tetrahydrofuran", "bo_id": "THF", "display_name": "THF", "aspen_id": "THF", "aspen_candidates": ["THF"], "source": "builtin", "explicit_aspen_id": True},
        {"alias": "dimethylsulfoxide", "bo_id": "DMSO", "display_name": "DMSO", "aspen_id": "DMSO", "aspen_candidates": ["DMSO"], "source": "builtin", "explicit_aspen_id": True},
        {"alias": "dimethylsulphoxide", "bo_id": "DMSO", "display_name": "DMSO", "aspen_id": "DMSO", "aspen_candidates": ["DMSO"], "source": "builtin", "explicit_aspen_id": True},
    ]
    records.extend(fallback)
    return records


def _load_component_alias_records() -> List[Dict[str, Any]]:
    global _COMPONENT_ALIAS_CACHE, _COMPONENT_ALIAS_RECORDS_CACHE
    if _COMPONENT_ALIAS_RECORDS_CACHE is not None and any("aspen_candidates" in r for r in _COMPONENT_ALIAS_RECORDS_CACHE):
        return _COMPONENT_ALIAS_RECORDS_CACHE  # type: ignore[return-value]
    records = _v39_component_alias_records()
    index: Dict[str, Dict[str, Any]] = {}
    for rec in records:
        values = [rec.get("alias"), rec.get("bo_id"), rec.get("display_name"), rec.get("aspen_key"), rec.get("aspen_id")]
        values.extend(_as_list(rec.get("aspen_candidates")))
        for value in values:
            key = _alias_lookup_key(value)
            if key and key not in index:
                index[key] = rec
    _COMPONENT_ALIAS_CACHE = index  # type: ignore[assignment]
    _COMPONENT_ALIAS_RECORDS_CACHE = records  # type: ignore[assignment]
    return records


def _component_aspen_candidates(name: Any) -> List[str]:
    bo = _canonical_component_name(name)
    values: List[str] = []
    for rec in _load_component_alias_records():
        if _clean_id(rec.get("bo_id")) != bo:
            continue
        values.extend(_v39_as_list_of_ids(rec.get("aspen_id")))
        values.extend(_v39_as_list_of_ids(rec.get("aspen_candidates")))
        values.extend(_v39_as_list_of_ids([rec.get("aspen_key"), rec.get("alias"), rec.get("display_name")]))
    values.extend(_v39_as_list_of_ids([name, bo]))
    blocked = _LEGACY_BLOCKED_ASPEN_IDS.get(bo, set())
    return [x for x in list(dict.fromkeys(values)) if x not in blocked]


def _component_explicit_aspen_id(name: Any, overrides: Optional[dict] = None) -> str:
    bo = _canonical_component_name(name)
    raw = _as_dict(overrides).get(bo) or _as_dict(overrides).get(str(name))
    if isinstance(raw, dict):
        explicit = _clean_id(raw.get("aspen_id"))
    else:
        explicit = _clean_id(raw)
    if explicit:
        return explicit
    rec = _component_alias_record(name)
    if rec and rec.get("explicit_aspen_id"):
        explicit = _clean_id(rec.get("aspen_id"))
        if explicit in _LEGACY_BLOCKED_ASPEN_IDS.get(bo, set()):
            return ""
        return explicit
    return ""


def _aspen_component_id(name: Any) -> str:
    # This function is only a preview without an Aspen template.  ETOAC is kept
    # unresolved rather than being incorrectly advertised as ETHYL-01.
    explicit = _component_explicit_aspen_id(name)
    return explicit or _canonical_component_name(name)


def _component_aliases_for_component(name: Any, *, prefer_aspen_first: bool = False) -> List[str]:
    bo = _canonical_component_name(name)
    values = _component_aspen_candidates(name)
    explicit = _component_explicit_aspen_id(name)
    if explicit:
        values = [explicit] + [x for x in values if x != explicit]
    values.extend(_v39_as_list_of_ids([bo, name]))
    return list(dict.fromkeys(values))


def _component_overrides_from_state(state_dict: Optional[dict]) -> Dict[str, dict]:
    state = _as_dict(state_dict)
    out: Dict[str, dict] = {}
    for raw_key, raw_val in _as_dict(state.get("component_alias_overrides")).items():
        bo = _canonical_component_name(raw_key)
        if isinstance(raw_val, dict):
            out[bo] = {
                "aspen_id": _clean_id(raw_val.get("aspen_id")),
                "aspen_candidates": _v39_as_list_of_ids(raw_val.get("aspen_candidates")),
            }
        else:
            out[bo] = {"aspen_id": _clean_id(raw_val), "aspen_candidates": []}
    for raw_key, meta in _as_dict(state.get("components_meta")).items():
        if not isinstance(meta, dict):
            continue
        bo = _canonical_component_name(meta.get("bo_id") or raw_key)
        row = out.setdefault(bo, {"aspen_id": "", "aspen_candidates": []})
        explicit = _clean_id(meta.get("aspen_id"))
        if explicit:
            row["aspen_id"] = explicit
        row["aspen_candidates"] = list(dict.fromkeys(row.get("aspen_candidates", []) + _v39_as_list_of_ids(meta.get("aspen_candidates"))))
    return out


def _v39_payload_with_component_overrides(state_dict: Optional[dict], payload: dict) -> dict:
    overrides = _component_overrides_from_state(state_dict)
    if overrides:
        payload["component_alias_overrides"] = overrides
    return payload


def build_pressure_flash_payload_from_state(state_dict: Optional[dict], *args, **kwargs) -> dict:
    return _v39_payload_with_component_overrides(state_dict, _V39_ORIGINAL_BUILD_PRESSURE_PAYLOAD(state_dict, *args, **kwargs))


def build_combined_pressure_flash_payload_from_state(state_dict: Optional[dict]) -> dict:
    return _v39_payload_with_component_overrides(state_dict, _V39_ORIGINAL_BUILD_COMBINED_PAYLOAD(state_dict))


def _v39_override_candidates(self: AspenPressureFlashScreenRunner, comp: str) -> Tuple[str, List[str]]:
    bo = _canonical_component_name(comp)
    overrides = _as_dict(getattr(self, "_component_id_overrides", {}))
    raw = overrides.get(bo)
    explicit = ""
    extra: List[str] = []
    if isinstance(raw, dict):
        explicit = _clean_id(raw.get("aspen_id"))
        extra = _v39_as_list_of_ids(raw.get("aspen_candidates"))
    elif raw:
        explicit = _clean_id(raw)
    candidates = []
    if explicit:
        candidates.append(explicit)
    candidates.extend(extra)
    candidates.extend(_component_aspen_candidates(bo))
    blocked = _LEGACY_BLOCKED_ASPEN_IDS.get(bo, set())
    if explicit in blocked:
        # Explicit project mapping is authoritative and may intentionally use a
        # legacy-looking row.  Only the built-in guess is blocked.
        blocked = set()
    candidates = [x for x in list(dict.fromkeys(candidates)) if x and x not in blocked]
    return explicit, candidates


def _v39_resolve_registered_id(self: AspenPressureFlashScreenRunner, comp: str, registered: Sequence[str]) -> Tuple[Optional[str], dict]:
    bo = _canonical_component_name(comp)
    explicit, candidates = _v39_override_candidates(self, bo)
    exact = list(dict.fromkeys(_clean_id(x) for x in registered if _clean_id(x)))
    exact_set = set(exact)
    if explicit and explicit in exact_set:
        return explicit, {"source": "explicit_existing", "candidates": candidates}
    candidate_norms = {_v39_normalized_id(x) for x in candidates if x}
    matches = [rid for rid in exact if _v39_normalized_id(rid) in candidate_norms]
    if len(matches) == 1:
        return matches[0], {"source": "template_alias_match", "candidates": candidates}
    if len(matches) > 1:
        raise RuntimeError(
            f"Ambiguous Aspen component mapping for {bo}: matches={matches}. "
            "Set an exact aspen_id in component_aliases.json or component_alias_overrides."
        )
    return None, {"source": "unresolved", "candidates": candidates, "explicit": explicit}


def _v39_ensure_components_registered(self: AspenPressureFlashScreenRunner, components: Sequence[str], *, thermo_method: Optional[str] = None) -> dict:
    clean: List[str] = []
    for raw in components:
        bo = _canonical_component_name(raw)
        if bo and bo not in clean:
            clean.append(bo)
    if not clean:
        return dict(getattr(self, "_component_registry_snapshot", {}))

    comp_node = self.node(r"\Data\Components\Specifications\Input\TYPE")
    if comp_node is None:
        raise RuntimeError("Aspen component TYPE table not found; component IDs cannot be resolved.")

    existing = self._registered_aspen_component_ids_exact()
    resolved: Dict[str, str] = {}
    resolution: Dict[str, dict] = {}
    to_add: Dict[str, str] = {}
    for bo in clean:
        rid, detail = _v39_resolve_registered_id(self, bo, existing)
        if rid:
            resolved[bo] = rid
            resolution[bo] = detail
            continue
        explicit, candidates = _v39_override_candidates(self, bo)
        if explicit:
            to_add[bo] = explicit
            resolution[bo] = {"source": "explicit_to_register", "candidates": candidates}
            continue
        raise RuntimeError(
            f"Aspen ID unresolved for {bo}. Registered IDs={existing}. "
            f"Template-match candidates={candidates}. The legacy automatic mapping "
            "ETOAC->ETHYL-01 is disabled. Register the correct component in the template "
            "or provide component_alias_overrides / component_aliases.json with an exact aspen_id."
        )

    added: List[str] = []
    for bo, rid in reversed(list(to_add.items())):
        if rid in existing:
            resolved[bo] = rid
            continue
        try:
            comp_node.Elements.InsertRow(0, 0)
            comp_node.Elements.SetLabel(0, 0, False, rid)
            added.append(rid)
            self.log(f"[COMP] explicitly registered {bo} as {rid}")
        except Exception as exc:
            raise RuntimeError(f"Cannot explicitly register Aspen component {bo} as {rid}: {exc}") from exc

    method = str(thermo_method or "").strip().upper()
    method_written = False
    if method:
        for path in (r"\Data\Properties\Specifications\Input\GBASEOPSET", r"\Data\Properties\Specifications\Input\GOPSETNAME"):
            node = self.node(path)
            if node is None:
                continue
            try:
                node.Value = method
                method_written = True
                self.log(f"[PROP] {path} = {method}")
                break
            except Exception as exc:
                self.log(f"[WARN] property method write failed: {path}={method}; {exc}")

    if added:
        self.invalidate_node_cache(r"\Data\Components")
        self.invalidate_node_cache(r"\Data\Properties")
        if self.rebuild_flash_on_component_change and getattr(self, "_flash_unit_ready", False):
            self._flash_unit_ready = False
            self._active_flash_component_signature = tuple()

    after = self._registered_aspen_component_ids_exact()
    for bo, rid in to_add.items():
        if rid not in after:
            raise RuntimeError(f"Aspen rejected explicit component ID {rid} for {bo}; registered IDs={after}")
        resolved[bo] = rid

    self._resolved_aspen_component_ids = dict(resolved)
    signature = tuple(sorted(resolved.values()))
    if self.rebuild_flash_on_component_change and getattr(self, "_active_flash_component_signature", tuple()) and signature != self._active_flash_component_signature:
        self._flash_unit_ready = False
        self._active_flash_component_signature = tuple()
    self._component_environment_signature = signature
    snapshot = {
        "requested_bo_ids": clean,
        "resolved_mapping": dict(resolved),
        "resolution": resolution,
        "registered_exact_ids": after,
        "added_exact_ids": added,
        "blocked_legacy_ids": {k: sorted(v) for k, v in _LEGACY_BLOCKED_ASPEN_IDS.items()},
        "thermo_method": method or None,
        "thermo_method_written": method_written,
        "component_signature": list(signature),
        "verified": len(resolved) == len(clean),
    }
    self._component_registry_snapshot = snapshot
    return dict(snapshot)


def _v39_aspen_component_register_id(self: AspenPressureFlashScreenRunner, comp: str) -> str:
    bo = _canonical_component_name(comp)
    return _as_dict(getattr(self, "_resolved_aspen_component_ids", {})).get(bo) or _component_explicit_aspen_id(bo, getattr(self, "_component_id_overrides", {})) or bo


def _v39_component_aliases_for_write(self: AspenPressureFlashScreenRunner, comp: str) -> List[str]:
    bo = _canonical_component_name(comp)
    resolved = _as_dict(getattr(self, "_resolved_aspen_component_ids", {})).get(bo)
    values = [resolved] if resolved else []
    values.extend(_component_aliases_for_component(bo, prefer_aspen_first=True))
    return [x for x in list(dict.fromkeys(_clean_id(v) for v in values if _clean_id(v)))]


def _v39_resolve_component_id(self: AspenPressureFlashScreenRunner, mixed_node: Any, component: str) -> str:
    bo = _canonical_component_name(component)
    resolved = _as_dict(getattr(self, "_resolved_aspen_component_ids", {})).get(bo)
    if not resolved:
        raise RuntimeError(f"Aspen component row for {bo} was not resolved before stream writing.")
    return _clean_id(resolved)


def _v39_run_payload(self: AspenPressureFlashScreenRunner, payload: dict) -> dict:
    pd = _as_dict(payload)
    overrides = dict(_as_dict(pd.get("component_alias_overrides")))
    for sub in _as_dict(pd.get("payloads")).values():
        for key, value in _as_dict(_as_dict(sub).get("component_alias_overrides")).items():
            overrides.setdefault(key, value)
    self._component_id_overrides = overrides
    results = _V39_ORIGINAL_RUN_PAYLOAD(self, payload)
    results["component_registry"] = copy.deepcopy(getattr(self, "_component_registry_snapshot", {}))
    results["component_mapping"] = copy.deepcopy(getattr(self, "_resolved_aspen_component_ids", {}))
    return results


AspenPressureFlashScreenRunner.ensure_components_registered = _v39_ensure_components_registered
AspenPressureFlashScreenRunner._aspen_component_register_id = _v39_aspen_component_register_id
AspenPressureFlashScreenRunner._component_aliases_for_write = _v39_component_aliases_for_write
AspenPressureFlashScreenRunner.resolve_component_id = _v39_resolve_component_id
AspenPressureFlashScreenRunner.run_payload = _v39_run_payload

# Reset caches populated by the v38 definitions, if any importer called them
# before reaching this override block.
_COMPONENT_ALIAS_CACHE = None
_COMPONENT_ALIAS_RECORDS_CACHE = None


# ===========================================================================
# v41 registered-ID submission override
# ===========================================================================
# The Aspen TYPE table accepts only short registered component IDs (less than
# nine characters). The GUI may accept a long common name such as
# "ethyl acetate", but its parser resolves that name before TYPE.SetLabel is
# called. This bridge reproduces the resolved registration step directly:
# ETOAC -> ETHYL-01. The literal GUI name remains in state metadata only.
# External alias-file edits are not required.

_V40_ORIGINAL_BUILD_PRESSURE_PAYLOAD = build_pressure_flash_payload_from_state
_V40_ORIGINAL_BUILD_COMBINED_PAYLOAD = build_combined_pressure_flash_payload_from_state
_V40_ORIGINAL_RUN_PAYLOAD = AspenPressureFlashScreenRunner.run_payload

_V40_GUI_REGISTRATION_DEFAULTS: Dict[str, str] = {
    "ETOAC": "ETHYL-01",
    "ETHANOL": "ETHANOL",
    "WATER": "WATER",
    "DMSO": "DMSO",
}


def _v40_raw_registration_name(value: Any) -> str:
    return str(value or "").strip()


def _component_registration_names_from_state(state_dict: Optional[dict]) -> Dict[str, str]:
    state = _as_dict(state_dict)
    out: Dict[str, str] = dict(_V40_GUI_REGISTRATION_DEFAULTS)
    meta_pack = _as_dict(state.get("components_meta"))
    for raw_key, raw_meta in meta_pack.items():
        meta = _as_dict(raw_meta)
        bo = _canonical_component_name(meta.get("bo_id") or raw_key)
        # raw_name/display_name are user-facing names, not TYPE-table IDs.
        # Only an explicit registration_name may override the built-in short ID.
        registration_id = _clean_id(meta.get("registration_name"))
        if bo and registration_id:
            if len(registration_id) > 8:
                raise ValueError(
                    f"registration_name {registration_id!r} for {bo} exceeds Aspen's 8-character ID limit"
                )
            out[bo] = registration_id

    # TYPE.SetLabel is an ID field with an eight-character limit. Keep literal
    # GUI names only in state metadata and submit resolved registration IDs.
    out["ETOAC"] = "ETHYL-01"
    return out


def components_from_state_raw(state_dict: Optional[dict]) -> List[str]:
    state = _as_dict(state_dict)
    comps = state.get("components") or state.get("component_ids") or []
    if isinstance(comps, dict):
        comps = list(comps.values())
    return [str(c).strip() for c in _as_list(comps) if str(c or "").strip()]


def _v40_payload_with_registration_names(state_dict: Optional[dict], payload: dict) -> dict:
    payload["component_registration_names"] = _component_registration_names_from_state(state_dict)
    return payload


def build_pressure_flash_payload_from_state(state_dict: Optional[dict], *args, **kwargs) -> dict:
    payload = _V40_ORIGINAL_BUILD_PRESSURE_PAYLOAD(state_dict, *args, **kwargs)
    return _v40_payload_with_registration_names(state_dict, payload)


def build_combined_pressure_flash_payload_from_state(state_dict: Optional[dict]) -> dict:
    payload = _V40_ORIGINAL_BUILD_COMBINED_PAYLOAD(state_dict)
    return _v40_payload_with_registration_names(state_dict, payload)


def _v40_exact_element_names(node: Any) -> List[str]:
    out: List[str] = []
    if node is None or not getattr(node, "Elements", None):
        return out
    try:
        for item in node.Elements:
            name = _clean_id(getattr(item, "Name", None))
            if name and name not in out:
                out.append(name)
    except Exception:
        pass
    return out


def _v40_registration_query(self: AspenPressureFlashScreenRunner, bo: str) -> str:
    names = _as_dict(getattr(self, "_component_registration_names", {}))
    query = _v40_raw_registration_name(names.get(bo))
    return query or _V40_GUI_REGISTRATION_DEFAULTS.get(bo) or bo


def _v40_existing_row_match(bo: str, row_ids: Sequence[str], registration_query: str) -> Optional[str]:
    exact = [_clean_id(x) for x in row_ids if _clean_id(x)]
    if bo in exact:
        return bo
    norms = {
        _v39_normalized_id(bo),
        _v39_normalized_id(registration_query),
    }
    # These are matching spellings only; registration still uses the literal GUI
    # name and does not depend on an aliases file.
    if bo == "ETOAC":
        norms.update({
            "ETHYLACETATE", "ETOAC", "EAC", "ETAC", "EA",
        })
    matches = [rid for rid in exact if _v39_normalized_id(rid) in norms]
    return matches[0] if len(matches) == 1 else None


def _v40_write_property_method(self: AspenPressureFlashScreenRunner, thermo_method: Optional[str]) -> Tuple[str, bool]:
    method = str(thermo_method or "").strip().upper()
    written = False
    if method:
        for path in (
            r"\Data\Properties\Specifications\Input\GBASEOPSET",
            r"\Data\Properties\Specifications\Input\GOPSETNAME",
        ):
            node = self.node(path)
            if node is None:
                continue
            try:
                node.Value = method
                written = True
                self.log(f"[PROP] {path} = {method}")
                break
            except Exception as exc:
                self.log(f"[WARN] property method write failed: {path}={method}; {exc}")
    return method, written


def _v40_ensure_components_registered(
    self: AspenPressureFlashScreenRunner,
    components: Sequence[str],
    *,
    thermo_method: Optional[str] = None,
) -> dict:
    clean: List[str] = []
    for raw in components:
        bo = _canonical_component_name(raw)
        if bo and bo not in clean:
            clean.append(bo)
    if not clean:
        return dict(getattr(self, "_component_registry_snapshot", {}))

    comp_node = self.node(r"\Data\Components\Specifications\Input\TYPE")
    if comp_node is None:
        raise RuntimeError("Aspen component TYPE table not found; component registration cannot proceed.")

    existing = self._registered_aspen_component_ids_exact()
    resolved: Dict[str, str] = dict(_as_dict(getattr(self, "_resolved_aspen_component_ids", {})))
    resolution: Dict[str, dict] = dict(_as_dict(_as_dict(getattr(self, "_component_registry_snapshot", {})).get("resolution")))
    submitted: List[dict] = []
    submitted_bo_ids = set(_as_list(getattr(self, "_submitted_component_bo_ids", [])))

    # Register one component at a time so a newly created Aspen row can be
    # associated with the exact GUI input that produced it.
    for bo in reversed(clean):
        query = _v40_registration_query(self, bo)
        if bo in submitted_bo_ids:
            row = dict(_as_dict(resolution.get(bo)))
            row.setdefault("source", "already_submitted_waiting_for_stream_row")
            row.setdefault("registration_query", query)
            resolution[bo] = row
            continue
        before = self._registered_aspen_component_ids_exact()
        existing_match = _v40_existing_row_match(bo, before, query)
        if existing_match:
            resolved[bo] = existing_match
            resolution[bo] = {
                "source": "existing_component_row",
                "registration_query": query,
                "resolved_id": existing_match,
            }
            continue

        try:
            comp_node.Elements.InsertRow(0, 0)
            if len(query) > 8:
                raise RuntimeError(
                    f"Aspen registration ID {query!r} for {bo} exceeds the TYPE-table "
                    "limit of 8 characters."
                )
            comp_node.Elements.SetLabel(0, 0, False, query)
            self.log(f"[COMP-REGISTER-ID] submitted {bo} as {query!r}")
        except Exception as exc:
            raise RuntimeError(f"Cannot submit Aspen component name {query!r} for {bo}: {exc}") from exc

        self.invalidate_node_cache(r"\Data\Components")
        after = self._registered_aspen_component_ids_exact()
        new_rows = [rid for rid in after if rid not in set(before)]
        detected = None
        if len(new_rows) == 1:
            detected = new_rows[0]
        if detected is None:
            detected = _v40_existing_row_match(bo, after, query)
        if detected:
            resolved[bo] = detected
            source = "new_registry_row"
        else:
            # Some Aspen versions do not expose TYPE.Elements until stream tables
            # are generated. Resolution is intentionally deferred to FLOW/MIXED.
            source = "deferred_to_stream_component_table"
        resolution[bo] = {
            "source": source,
            "registration_query": query,
            "resolved_id": detected,
            "registry_rows_before": before,
            "registry_rows_after": after,
        }
        submitted.append({"bo_id": bo, "registration_query": query, "detected_id": detected})
        submitted_bo_ids.add(bo)
        existing = after

    self._submitted_component_bo_ids = sorted(submitted_bo_ids)
    method, method_written = _v40_write_property_method(self, thermo_method)

    if submitted:
        self.invalidate_node_cache(r"\Data\Components")
        self.invalidate_node_cache(r"\Data\Properties")
        if self.rebuild_flash_on_component_change and getattr(self, "_flash_unit_ready", False):
            self._flash_unit_ready = False
            self._active_flash_component_signature = tuple()
            self.log("[COMP-CHANGE] registered component IDs submitted; temporary Flash2 will be rebuilt")

    self._resolved_aspen_component_ids = resolved
    registration_names = {
        bo: _v40_registration_query(self, bo) for bo in clean
    }
    signature_tokens = [resolved.get(bo) or registration_names[bo] for bo in clean]
    signature = tuple(sorted(str(x) for x in signature_tokens if x))
    self._component_environment_signature = signature
    snapshot = {
        "requested_bo_ids": clean,
        "registration_mode": "resolved_registered_id_then_stream_row_verification",
        "registration_names": registration_names,
        "resolved_mapping": dict(resolved),
        "resolution": resolution,
        "submitted": submitted,
        "submitted_bo_ids": sorted(submitted_bo_ids),
        "registered_exact_ids": self._registered_aspen_component_ids_exact(),
        "thermo_method": method or None,
        "thermo_method_written": method_written,
        "component_signature": list(signature),
        "verified": all(bo in resolved for bo in clean),
        "deferred_bo_ids": [bo for bo in clean if bo not in resolved],
    }
    self._component_registry_snapshot = snapshot
    return dict(snapshot)


def _v40_commit_resolved_component(
    self: AspenPressureFlashScreenRunner,
    bo: str,
    row_id: str,
    *,
    source: str,
) -> str:
    rid = _clean_id(row_id)
    mapping = dict(_as_dict(getattr(self, "_resolved_aspen_component_ids", {})))
    mapping[bo] = rid
    self._resolved_aspen_component_ids = mapping
    snapshot = dict(_as_dict(getattr(self, "_component_registry_snapshot", {})))
    snapshot["resolved_mapping"] = dict(mapping)
    deferred = [x for x in _as_list(snapshot.get("deferred_bo_ids")) if x != bo]
    snapshot["deferred_bo_ids"] = deferred
    snapshot["verified"] = not deferred
    resolution = dict(_as_dict(snapshot.get("resolution")))
    row = dict(_as_dict(resolution.get(bo)))
    row.update({"source": source, "resolved_id": rid})
    resolution[bo] = row
    snapshot["resolution"] = resolution
    self._component_registry_snapshot = snapshot
    self.log(f"[COMP-ROW-RESOLVED] {bo} -> {rid} via {source}")
    return rid


def _v40_resolve_component_id(self: AspenPressureFlashScreenRunner, mixed_node: Any, component: str) -> str:
    bo = _canonical_component_name(component)
    row_ids = _v40_exact_element_names(mixed_node)
    if not row_ids:
        raise RuntimeError(
            f"Aspen stream component table is empty after registering {bo} by GUI name. "
            "The component search/registration did not populate FLOW/MIXED; no fake row will be created."
        )

    mapping = dict(_as_dict(getattr(self, "_resolved_aspen_component_ids", {})))
    current = _clean_id(mapping.get(bo))
    if current and current in row_ids:
        return current

    query = _v40_registration_query(self, bo)
    matched = _v40_existing_row_match(bo, row_ids, query)
    if matched:
        return _v40_commit_resolved_component(self, bo, matched, source="stream_name_match")

    # Known exact component rows are removed first. For this demo ETOAC is the
    # only name-resolved component, so the single remaining Aspen row is its
    # actual internal ID (for example an Aspen-generated short ID).
    assigned_other = {
        _clean_id(rid) for other_bo, rid in mapping.items()
        if other_bo != bo and _clean_id(rid)
    }
    known_other = {
        other_bo for other_bo in _V40_GUI_REGISTRATION_DEFAULTS
        if other_bo != bo
    }
    remaining = [rid for rid in row_ids if rid not in assigned_other and rid not in known_other]
    if len(remaining) == 1:
        return _v40_commit_resolved_component(self, bo, remaining[0], source="single_unassigned_stream_row")

    raise RuntimeError(
        f"Aspen component row remains ambiguous for {bo} after submitting {query!r}. "
        f"FLOW/MIXED rows={row_ids}; resolved_mapping={mapping}; remaining={remaining}."
    )


def _v40_component_aliases_for_write(self: AspenPressureFlashScreenRunner, comp: str) -> List[str]:
    bo = _canonical_component_name(comp)
    mapping = _as_dict(getattr(self, "_resolved_aspen_component_ids", {}))
    values: List[str] = []
    if mapping.get(bo):
        values.append(mapping.get(bo))
    values.extend([bo, _v40_registration_query(self, bo)])
    return list(dict.fromkeys(_clean_id(v) for v in values if _clean_id(v)))


def _v40_run_payload(self: AspenPressureFlashScreenRunner, payload: dict) -> dict:
    pd = _as_dict(payload)
    registration_names = dict(_as_dict(pd.get("component_registration_names")))
    for sub in _as_dict(pd.get("payloads")).values():
        for key, value in _as_dict(_as_dict(sub).get("component_registration_names")).items():
            registration_names.setdefault(_canonical_component_name(key), str(value))
    if registration_names:
        self._component_registration_names = {
            _canonical_component_name(k): str(v).strip()
            for k, v in registration_names.items() if str(v or "").strip()
        }
    results = _V40_ORIGINAL_RUN_PAYLOAD(self, payload)
    results["component_registry"] = copy.deepcopy(getattr(self, "_component_registry_snapshot", {}))
    results["component_mapping"] = copy.deepcopy(getattr(self, "_resolved_aspen_component_ids", {}))
    return results


AspenPressureFlashScreenRunner.ensure_components_registered = _v40_ensure_components_registered
AspenPressureFlashScreenRunner.resolve_component_id = _v40_resolve_component_id
AspenPressureFlashScreenRunner._component_aliases_for_write = _v40_component_aliases_for_write
AspenPressureFlashScreenRunner.run_payload = _v40_run_payload



# ===========================================================================
# v42 Aspen-key / stream-ID bridge
# ===========================================================================
# Root-cause correction:
#   components_meta[BO_ID]["aspen_key"] is the value used to register/search the
#   databank component (prefer CAS, e.g. 141-78-6), while
#   components_meta[BO_ID]["aspen_id"] is the component row used in streams
#   (e.g. ETHYL-01).  v41 incorrectly inserted the stream ID into the TYPE table.
#   Aspen accepted the row label but did not bind it to the ethyl-acetate databank
#   record, so every Flash2 case containing ETOAC returned zero outlet flow.

_V42_PREVIOUS_BUILD_PRESSURE_PAYLOAD = build_pressure_flash_payload_from_state
_V42_PREVIOUS_BUILD_COMBINED_PAYLOAD = build_combined_pressure_flash_payload_from_state
_V42_PREVIOUS_RUN_PAYLOAD = AspenPressureFlashScreenRunner.run_payload

_V42_COMPONENT_DEFAULTS: Dict[str, Dict[str, str]] = {
    "ETOAC": {
        "raw_name": "ethyl acetate",
        "display_name": "EAc",
        "aspen_key": "141-78-6",
        "aspen_id": "ETHYL-01",
    },
    "ETHANOL": {
        "raw_name": "EtOH",
        "display_name": "EtOH",
        "aspen_key": "ETHANOL",
        "aspen_id": "ETHANOL",
    },
    "WATER": {
        "raw_name": "water",
        "display_name": "water",
        "aspen_key": "WATER",
        "aspen_id": "WATER",
    },
    "DMSO": {
        "raw_name": "DMSO",
        "display_name": "DMSO",
        "aspen_key": "DMSO",
        "aspen_id": "DMSO",
    },
}


def _v42_clean_registration_key(value: Any) -> str:
    """Preserve a valid Aspen registration key, including CAS hyphens."""
    return re.sub(r"[^A-Za-z0-9_\-]", "", str(value or "").strip()).upper()


def _component_registration_specs_from_state(state_dict: Optional[dict]) -> Dict[str, Dict[str, str]]:
    state = _as_dict(state_dict)
    specs: Dict[str, Dict[str, str]] = copy.deepcopy(_V42_COMPONENT_DEFAULTS)
    meta_pack = _as_dict(state.get("components_meta"))

    # components_meta is the same contract used by the GUI and BO optimizer:
    # aspen_key for registration, aspen_id for stream read/write.
    for raw_key, raw_meta in meta_pack.items():
        meta = _as_dict(raw_meta)
        bo = _canonical_component_name(meta.get("bo_id") or raw_key)
        if not bo:
            continue
        row = dict(specs.get(bo, {}))
        row["raw_name"] = str(meta.get("raw_name") or row.get("raw_name") or raw_key).strip()
        row["display_name"] = str(meta.get("display_name") or row.get("display_name") or bo).strip()
        row["aspen_key"] = _v42_clean_registration_key(
            meta.get("aspen_key") or row.get("aspen_key") or bo
        )
        row["aspen_id"] = _clean_id(meta.get("aspen_id") or row.get("aspen_id") or bo)
        specs[bo] = row

    for raw in components_from_state_raw(state):
        bo = _canonical_component_name(raw)
        if not bo:
            continue
        specs.setdefault(bo, {
            "raw_name": str(raw),
            "display_name": str(raw),
            "aspen_key": _v42_clean_registration_key(bo),
            "aspen_id": _clean_id(bo),
        })

    # Explicitly preserve the GUI-resolved ethyl-acetate contract even when the
    # demo is copied without an external aliases file.
    eac = dict(specs.get("ETOAC", {}))
    eac["raw_name"] = str(eac.get("raw_name") or "ethyl acetate")
    eac["display_name"] = str(eac.get("display_name") or "EAc")
    eac["aspen_key"] = _v42_clean_registration_key(eac.get("aspen_key") or "141-78-6")
    eac["aspen_id"] = _clean_id(eac.get("aspen_id") or "ETHYL-01")
    specs["ETOAC"] = eac
    return specs


def _component_registration_names_from_state(state_dict: Optional[dict]) -> Dict[str, str]:
    """Compatibility field: now contains databank registration keys, not stream IDs."""
    return {
        bo: str(spec.get("aspen_key") or bo)
        for bo, spec in _component_registration_specs_from_state(state_dict).items()
    }


def _v42_attach_component_specs(state_dict: Optional[dict], payload: dict) -> dict:
    specs = _component_registration_specs_from_state(state_dict)
    payload["component_registration_specs"] = copy.deepcopy(specs)
    payload["component_registration_names"] = {
        bo: spec.get("aspen_key") for bo, spec in specs.items()
    }
    return payload


def build_pressure_flash_payload_from_state(state_dict: Optional[dict], *args, **kwargs) -> dict:
    payload = _V42_PREVIOUS_BUILD_PRESSURE_PAYLOAD(state_dict, *args, **kwargs)
    return _v42_attach_component_specs(state_dict, payload)


def build_combined_pressure_flash_payload_from_state(state_dict: Optional[dict]) -> dict:
    payload = _V42_PREVIOUS_BUILD_COMBINED_PAYLOAD(state_dict)
    return _v42_attach_component_specs(state_dict, payload)


def _v42_registration_spec(self: AspenPressureFlashScreenRunner, bo: str) -> Dict[str, str]:
    bo = _canonical_component_name(bo)
    specs = _as_dict(getattr(self, "_component_registration_specs", {}))
    row = dict(_as_dict(specs.get(bo)))
    default = _as_dict(_V42_COMPONENT_DEFAULTS.get(bo))
    return {
        "raw_name": str(row.get("raw_name") or default.get("raw_name") or bo),
        "display_name": str(row.get("display_name") or default.get("display_name") or bo),
        "aspen_key": _v42_clean_registration_key(
            row.get("aspen_key") or default.get("aspen_key") or bo
        ),
        "aspen_id": _clean_id(row.get("aspen_id") or default.get("aspen_id") or bo),
    }


def _v42_registration_key(self: AspenPressureFlashScreenRunner, bo: str) -> str:
    return _v42_registration_spec(self, bo).get("aspen_key") or _clean_id(bo)


def _v42_expected_stream_id(self: AspenPressureFlashScreenRunner, bo: str) -> str:
    return _v42_registration_spec(self, bo).get("aspen_id") or _clean_id(bo)


def _v42_ensure_components_registered(
    self: AspenPressureFlashScreenRunner,
    components: Sequence[str],
    *,
    thermo_method: Optional[str] = None,
) -> dict:
    clean: List[str] = []
    for raw in components:
        bo = _canonical_component_name(raw)
        if bo and bo not in clean:
            clean.append(bo)
    if not clean:
        return dict(getattr(self, "_component_registry_snapshot", {}))

    comp_node = self.node(r"\Data\Components\Specifications\Input\TYPE")
    if comp_node is None:
        raise RuntimeError("Aspen component TYPE table not found; component registration cannot proceed.")

    resolved = dict(_as_dict(getattr(self, "_resolved_aspen_component_ids", {})))
    resolution = dict(_as_dict(_as_dict(getattr(self, "_component_registry_snapshot", {})).get("resolution")))
    submitted_bo_ids = set(_as_list(getattr(self, "_submitted_component_bo_ids", [])))
    submitted: List[dict] = []

    for bo in reversed(clean):
        spec = _v42_registration_spec(self, bo)
        reg_key = spec["aspen_key"]
        stream_id = spec["aspen_id"]
        if not reg_key:
            raise RuntimeError(f"Aspen registration key is empty for {bo}.")
        if len(reg_key) > 8:
            raise RuntimeError(
                f"Aspen registration key {reg_key!r} for {bo} exceeds the TYPE-table "
                "limit of 8 characters. Use components_meta[BO_ID]['aspen_key'], preferably CAS."
            )

        before = self._registered_aspen_component_ids_exact()
        before_set = set(before)
        if reg_key not in before_set and stream_id not in before_set:
            try:
                comp_node.Elements.InsertRow(0, 0)
                comp_node.Elements.SetLabel(0, 0, False, reg_key)
                self.log(
                    f"[COMP-REGISTER-KEY] {bo}: aspen_key={reg_key!r}; "
                    f"stream_id={stream_id!r}; raw={spec['raw_name']!r}"
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Cannot register Aspen component {bo} with aspen_key={reg_key!r}: {exc}"
                ) from exc
            self.invalidate_node_cache(r"\Data\Components")

        after = self._registered_aspen_component_ids_exact()
        after_set = set(after)
        registered = reg_key in after_set or stream_id in after_set
        if not registered:
            raise RuntimeError(
                f"Aspen registration verification failed for {bo}: aspen_key={reg_key!r}, "
                f"stream_id={stream_id!r}, TYPE rows={after}."
            )

        # TYPE registration and stream I/O are intentionally separate. Do not
        # replace ETHYL-01 with the CAS row label 141-78-6.
        resolved[bo] = stream_id
        resolution[bo] = {
            "source": "components_meta_aspen_key_and_aspen_id",
            "raw_name": spec["raw_name"],
            "registration_key": reg_key,
            "stream_component_id": stream_id,
            "registry_rows_before": before,
            "registry_rows_after": after,
            "registration_verified": registered,
        }
        if bo not in submitted_bo_ids:
            submitted.append({
                "bo_id": bo,
                "registration_key": reg_key,
                "stream_component_id": stream_id,
            })
        submitted_bo_ids.add(bo)

    self._submitted_component_bo_ids = sorted(submitted_bo_ids)
    method, method_written = _v40_write_property_method(self, thermo_method)

    if submitted:
        self.invalidate_node_cache(r"\Data\Components")
        self.invalidate_node_cache(r"\Data\Properties")
        if self.rebuild_flash_on_component_change and getattr(self, "_flash_unit_ready", False):
            self._flash_unit_ready = False
            self._active_flash_component_signature = tuple()
            self.log("[COMP-CHANGE] databank registration keys added; temporary Flash2 will be rebuilt")

    self._resolved_aspen_component_ids = resolved
    all_specs = dict(_as_dict(getattr(self, "_component_registration_specs", {})))
    signature = tuple(sorted(
        _clean_id(_as_dict(spec).get("aspen_id") or bo)
        for bo, spec in all_specs.items()
        if _clean_id(_as_dict(spec).get("aspen_id") or bo)
    ))
    self._component_environment_signature = signature
    snapshot = {
        "requested_bo_ids": sorted(set(_as_list(_as_dict(getattr(self, "_component_registry_snapshot", {})).get("requested_bo_ids"))).union(clean)),
        "registration_mode": "aspen_key_for_TYPE__aspen_id_for_stream_IO",
        "registration_specs": copy.deepcopy(all_specs),
        "resolved_mapping": dict(resolved),
        "resolution": resolution,
        "submitted": submitted,
        "submitted_bo_ids": sorted(submitted_bo_ids),
        "registered_exact_ids": self._registered_aspen_component_ids_exact(),
        "thermo_method": method or None,
        "thermo_method_written": method_written,
        "component_signature": list(signature),
        "verified": all(bo in resolved for bo in clean),
        "deferred_bo_ids": [],
    }
    self._component_registry_snapshot = snapshot
    return dict(snapshot)


def _v42_resolve_component_id(self: AspenPressureFlashScreenRunner, mixed_node: Any, component: str) -> str:
    bo = _canonical_component_name(component)
    row_ids = _v40_exact_element_names(mixed_node)
    if not row_ids:
        raise RuntimeError(
            f"Aspen FLOW/MIXED component table is empty for {bo} after databank registration."
        )

    spec = _v42_registration_spec(self, bo)
    mapping = dict(_as_dict(getattr(self, "_resolved_aspen_component_ids", {})))
    direct_candidates = [
        _clean_id(mapping.get(bo)),
        _clean_id(spec.get("aspen_id")),
        _clean_id(spec.get("aspen_key")),
        _clean_id(bo),
    ]
    for candidate in direct_candidates:
        if candidate and candidate in row_ids:
            return _v40_commit_resolved_component(
                self, bo, candidate, source="exact_stream_row_from_aspen_key_bridge"
            )

    target_norms = {
        _v39_normalized_id(x) for x in direct_candidates if x
    }
    target_norms.add(_v39_normalized_id(spec.get("raw_name")))
    target_norms.add(_v39_normalized_id(spec.get("display_name")))
    if bo == "ETOAC":
        target_norms.update({"ETHYL01", "141786", "ETOAC", "ETHYLACETATE"})
    matches = [rid for rid in row_ids if _v39_normalized_id(rid) in target_norms]
    if len(matches) == 1:
        return _v40_commit_resolved_component(
            self, bo, matches[0], source="normalized_stream_row_from_aspen_key_bridge"
        )

    assigned_other = {
        _clean_id(rid) for other_bo, rid in mapping.items()
        if other_bo != bo and _clean_id(rid)
    }
    expected_other = {
        _clean_id(_v42_registration_spec(self, other_bo).get("aspen_id"))
        for other_bo in _as_dict(getattr(self, "_component_registration_specs", {}))
        if other_bo != bo
    }
    remaining = [rid for rid in row_ids if rid not in assigned_other and rid not in expected_other]
    if len(remaining) == 1:
        return _v40_commit_resolved_component(
            self, bo, remaining[0], source="single_unassigned_stream_row_aspen_key_bridge"
        )

    raise RuntimeError(
        f"Aspen stream row unresolved for {bo}. aspen_key={spec.get('aspen_key')!r}; "
        f"expected_aspen_id={spec.get('aspen_id')!r}; FLOW/MIXED rows={row_ids}; "
        f"matches={matches}; remaining={remaining}."
    )


def _v42_component_aliases_for_write(self: AspenPressureFlashScreenRunner, comp: str) -> List[str]:
    bo = _canonical_component_name(comp)
    spec = _v42_registration_spec(self, bo)
    mapping = _as_dict(getattr(self, "_resolved_aspen_component_ids", {}))
    values = [
        mapping.get(bo),
        spec.get("aspen_id"),
        spec.get("aspen_key"),
        bo,
    ]
    return list(dict.fromkeys(_clean_id(v) for v in values if _clean_id(v)))


def _v42_run_payload(self: AspenPressureFlashScreenRunner, payload: dict) -> dict:
    pd = _as_dict(payload)
    specs = copy.deepcopy(_as_dict(pd.get("component_registration_specs")))
    for sub in _as_dict(pd.get("payloads")).values():
        for key, value in _as_dict(_as_dict(sub).get("component_registration_specs")).items():
            specs.setdefault(_canonical_component_name(key), copy.deepcopy(value))
    if specs:
        self._component_registration_specs = {
            _canonical_component_name(k): dict(_as_dict(v))
            for k, v in specs.items() if _canonical_component_name(k)
        }
        self._component_registration_names = {
            bo: _v42_clean_registration_key(_as_dict(spec).get("aspen_key") or bo)
            for bo, spec in self._component_registration_specs.items()
        }
    results = _V42_PREVIOUS_RUN_PAYLOAD(self, payload)
    results["component_registry"] = copy.deepcopy(getattr(self, "_component_registry_snapshot", {}))
    results["component_mapping"] = copy.deepcopy(getattr(self, "_resolved_aspen_component_ids", {}))
    return results


AspenPressureFlashScreenRunner.ensure_components_registered = _v42_ensure_components_registered
AspenPressureFlashScreenRunner.resolve_component_id = _v42_resolve_component_id
AspenPressureFlashScreenRunner._component_aliases_for_write = _v42_component_aliases_for_write
AspenPressureFlashScreenRunner.run_payload = _v42_run_payload

__all__ = [
    "RUNNER_VERSION",
    "_component_aspen_candidates",
    "_component_overrides_from_state",
    "build_pressure_screen_data_quality",
    "build_separation_agent_handoff",
    "finalize_pressure_screen_results",
    "build_binary_triptych_pressure_flash_payload_from_state",
    "run_binary_triptych_pressure_flash_screen",
    "save_separation_page_demo_reference_yx_triptych_plot",
    "save_binary_crossing_table_csv",
    "DEFAULT_BINARY_TRIPTYCH_PRESSURE_GRID_KPA",
    "DEFAULT_BINARY_TRIPTYCH_X_GRID",
    "DEFAULT_BINARY_TRIPTYCH_VAPOR_FRACTION_GRID",
    "DEFAULT_BINARY_TRIPTYCH_PRESSURE_WRITE_MODE",
    "save_ternary_summary_markdown",
    "save_point_csv",
    "save_json",
    "plot_pressure_comparison",
    "plot_ternary_phase_diagram",
    "plot_binary_yx_with_metrics",
    "plot_ternary_case",
    "analyze_ternary_flash_results",
    "ternary_components_from_state",
    "update_state_with_ternary_flash_results",
    "run_ternary_flash_screen_from_state",
    "run_ternary_flash_screen",
    "AspenTernaryFlashRunner",
    "TERNARY_RUNNER_VERSION",
    "DEFAULT_VAPOR_FRACTION_GRID",
    "AspenPressureFlashScreenRunner",
    "build_pressure_flash_payload_from_state",
    "build_combined_pressure_flash_payload_from_state",
    "build_fast_pressure_flash_payload_from_state",
    "estimate_pressure_flash_workload",
    "FAST_PRESSURE_GRID_KPA",
    "FAST_X_GRID",
    "FAST_VAPOR_FRACTION_GRID",
    "FAST_SOLVENT_TO_FEED_GRID",
    "open_aspen_document",
    "run_pressure_flash_screen_from_state",
    "run_pressure_flash_payload",
    "save_flash_results_json",
    "save_flash_curve_analysis_markdown",
    "update_state_with_flash_results",
]