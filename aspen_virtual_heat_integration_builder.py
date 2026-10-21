# -*- coding: utf-8 -*-
r"""
aspen_virtual_heat_integration_builder.py

Direct heat-integration virtual builder for the process-intensification agent.

This module complements the verified heat-pump demo/builder.  The heat-pump
builder uses:
    sink column N-1 liquid pseudo stream -> Flash2 reboiler surrogate
    source column top vapor pseudo stream -> Compr -> HREB -> HCOND

Direct heat integration removes the compressor and treats the high-temperature
process stream as the hot side of a virtual exchanger:
    sink column N-1 liquid pseudo stream -> Flash2 reboiler surrogate
    source hot stream / source column pseudo stream -> HINT(Heater used as cooler)

The two Aspen blocks are not forced to share one heat stream.  The simulation
reads both duties and performs the heat-balance in Python:
    integrated_heat = min(abs(FRB duty), abs(HINT duty), optional source/sink limits)
    external_reboiler_after_HI = max(0, abs(FRB duty) - integrated_heat)
    residual_hot_stream_cooling = max(0, hot_source_limit - integrated_heat)

Why this structure is deliberate
--------------------------------
1. It follows the proven pseudo-stream rule: create/connect the material stream
   to RadFrac PS(OUT) first, then write Input\PSEUDO_STAGE\stream.
2. It avoids hand-writing pseudo stream composition/flow.  Aspen generates the
   pseudo stream from the converged column.
3. It is consistent with pinch/GCC methodology: the agent first identifies a
   hot source and cold sink; the builder then validates temperature feasibility
   and duty coverage in Aspen.

Main entry points
-----------------
- build_direct_heat_integration_virtual_scheme(...)
- make_direct_heat_integration_validation_payload(...)

Pass an already-open Aspen COM document.  This module intentionally does not
import win32com.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional

try:
    from aspen_virtual_heat_pump_builder import (
        AspenVirtualHeatPumpBuilder,
        _norm_id,
        _safe_float,
        _pair_id_prefix,
    )
except Exception as exc:  # pragma: no cover - only hit if the companion file is absent
    raise ImportError(
        "aspen_virtual_heat_integration_builder.py requires "
        "aspen_virtual_heat_pump_builder.py in the same project folder."
    ) from exc

BUILDER_VERSION = "aspen-virtual-direct-heat-integration-v6-dt7-solvent-bottom"
DEFAULT_DT_MIN_C = 7.0


# ---------------------------------------------------------------------------
# Small data helpers, kept local so the agent can build payloads without COM.
# ---------------------------------------------------------------------------

def _as_dict(obj: Any) -> dict:
    return obj if isinstance(obj, dict) else {}


def _temp_c(pack: Any) -> Optional[float]:
    """Read a BO/Aspen temperature pack as Celsius."""
    if isinstance(pack, dict):
        if pack.get("C") is not None:
            return _safe_float(pack.get("C"), None)
        if pack.get("K") is not None:
            k = _safe_float(pack.get("K"), None)
            return None if k is None else k - 273.15
        raw = _safe_float(pack.get("raw"), None)
        if raw is None:
            return None
        return raw - 273.15 if raw >= 260.0 else raw
    return _safe_float(pack, None)


def _q_abs_kw(col: Dict[str, Any], key: str) -> Optional[float]:
    return _safe_float((_as_dict(col.get("duties"))).get(key), None)


def _n_stages(col: Dict[str, Any], default: int = 30) -> int:
    settings = _as_dict(col.get("settings"))
    n = _safe_float(settings.get("n_stages") or col.get("n_stages"), default)
    try:
        return max(2, int(round(float(n))))
    except Exception:
        return default


def _column_id(col: Dict[str, Any], fallback: str = "C1") -> str:
    return _norm_id(col.get("id") or col.get("column") or fallback)


def _safe_min_positive(*values: Optional[float]) -> Optional[float]:
    vals = [float(v) for v in values if v is not None and math.isfinite(float(v)) and float(v) >= 0]
    return min(vals) if vals else None


# ---------------------------------------------------------------------------
# Payload for the process-intensification agent / GUI.
# ---------------------------------------------------------------------------

def make_direct_heat_integration_validation_payload(
    source_column: Dict[str, Any],
    sink_column: Dict[str, Any],
    candidate: Optional[Dict[str, Any]] = None,
    dt_min_c: float = DEFAULT_DT_MIN_C,
) -> Dict[str, Any]:
    """Create a no-COM payload for direct hot-source -> sink-reboiler validation.

    Intended candidate categories:
    - cross_column_heat_integration: high-temperature source column condenser/top vapor
      drives low-temperature sink column reboiler.
    - background_hot_to_reboiler: an existing hot background process stream drives
      a column reboiler.
    """
    candidate = candidate or {}
    source_mode = str(candidate.get("source_mode") or "column_top_vapor").lower().strip()
    existing_hot_stream_id = candidate.get("existing_hot_stream_id")
    src_settings = _as_dict(source_column.get("settings"))
    sink_settings = _as_dict(sink_column.get("settings"))
    src_temps = _as_dict(source_column.get("temperatures"))
    sink_temps = _as_dict(sink_column.get("temperatures"))

    src_id = _norm_id(
        source_column.get("id")
        or source_column.get("column")
        or candidate.get("source_column")
        or candidate.get("hot_source_column")
        or "C1"
    )
    sink_id = _norm_id(
        sink_column.get("id")
        or sink_column.get("column")
        or candidate.get("sink_column")
        or candidate.get("cold_sink_column")
        or "C1"
    )
    src_n = int(_safe_float(src_settings.get("n_stages") or source_column.get("n_stages"), 30) or 30)
    sink_n = int(_safe_float(sink_settings.get("n_stages") or sink_column.get("n_stages"), 30) or 30)

    source_hot_t = _safe_float(candidate.get("source_hot_temperature_C"), None)
    if source_hot_t is None:
        if source_mode == "column_top_vapor":
            source_hot_t = _temp_c(src_temps.get("top"))
        else:
            source_hot_t = (
                _temp_c(src_temps.get("bottom_product"))
                or _temp_c(src_temps.get("bottom_stream"))
                or _temp_c(src_temps.get("bottom"))
            )
    if source_hot_t is None:
        source_hot_t = _safe_float(candidate.get("source_top_temperature_C"), None)

    sink_bottom_t = _temp_c(sink_temps.get("bottom"))
    if sink_bottom_t is None:
        sink_bottom_t = _safe_float(candidate.get("sink_bottom_temperature_C"), None)

    source_q = _safe_float(
        candidate.get("source_available_heat_kw")
        or candidate.get("available_kw_abs")
        or candidate.get("source_condenser_kw_abs")
        or _q_abs_kw(source_column, "condenser_kw_abs"),
        None,
    )
    sink_q = _safe_float(
        candidate.get("sink_reboiler_kw_abs")
        or candidate.get("sink_required_heat_kw")
        or _q_abs_kw(sink_column, "reboiler_kw_abs"),
        None,
    )

    target_hot_out = None
    if sink_bottom_t is not None:
        target_hot_out = sink_bottom_t + float(dt_min_c)

    margin = None
    if source_hot_t is not None and sink_bottom_t is not None:
        margin = source_hot_t - sink_bottom_t - float(dt_min_c)

    source_stage = int(candidate.get("source_hot_stage") or (2 if source_mode == "column_top_vapor" else max(1, src_n - 1)))
    source_phase = str(candidate.get("source_hot_phase") or ("V" if source_mode == "column_top_vapor" else "L"))
    if source_mode == "existing_hot_stream":
        hot_structure = f"existing Aspen hot stream {existing_hot_stream_id} -> HINT(Heater used as cooler)"
    else:
        hot_structure = f"{src_id} stage {source_stage} {source_phase} pseudo stream -> HINT(Heater used as cooler)"

    return {
        "builder_version": BUILDER_VERSION,
        "callable": "build_direct_heat_integration_virtual_scheme(aspen, ...)",
        "source_column_id": src_id,
        "sink_column_id": sink_id,
        "required_inputs": {
            "source_mode": source_mode,
            "existing_hot_stream_id": existing_hot_stream_id,
            "source_column_id": src_id,
            "sink_column_id": sink_id,
            "source_n_stages": src_n,
            "sink_n_stages": sink_n,
            "source_hot_temperature_C": source_hot_t,
            "sink_bottom_temperature_C": sink_bottom_t,
            "source_hot_stage": source_stage,
            "source_hot_phase": source_phase,
            "sink_reboiler_liquid_stage": int(candidate.get("sink_reboiler_liquid_stage") or max(1, sink_n - 1)),
            "dt_min_C": float(dt_min_c),
            "source_hot_outlet_target_C": None,
            "screening_source_hot_outlet_target_C": target_hot_out,
            "flash_pressure": _safe_float(candidate.get("flash_pressure"), 0.0),
        },
        "screening": {
            "temperature_margin_C": margin,
            "source_available_heat_kw": source_q,
            "sink_reboiler_kw_abs": sink_q,
            "max_integrated_heat_kw_screening": _safe_min_positive(source_q, sink_q),
            "feasible_by_temperature": None if margin is None else margin >= 0,
        },
        "aspen_structure": [
            f"{sink_id} stage {max(1, sink_n - 1)} liquid/default pseudo stream -> Flash2 reboiler surrogate",
            hot_structure,
            f"HINT outlet TEMP is resolved after the preliminary run as the actual {sink_id} reboiler-liquid pseudo temperature + DTmin; abs(HINT duty) = available direct heat at usable temperature",
            "No compressor is used; Python reads abs(Flash2 duty) and abs(HINT duty) and computes coverage.",
        ],
        "energy_interpretation": {
            "base_reboiler_surrogate": "abs(Flash2 duty on sink reboiler liquid pseudo stream)",
            "available_hot_stream_heat": "abs(HINT duty) after cooling source hot stream to sink_bottom+DTmin",
            "integrated_heat": "min(abs(Flash2 duty), abs(HINT duty), optional source/sink screening limits)",
            "external_reboiler_after_heat_integration": "max(0, abs(Flash2 duty)-integrated_heat)",
            "residual_hot_stream_cooling": "max(0, source_available_heat_kw-integrated_heat) if source_available_heat_kw is provided",
        },
    }


# ---------------------------------------------------------------------------
# Aspen virtual scheme builder.
# ---------------------------------------------------------------------------

def build_direct_heat_integration_virtual_scheme(
    aspen: Any,
    source_column_id: Optional[str],
    sink_column_id: str,
    source_n_stages: Optional[int],
    sink_n_stages: int,
    source_hot_temperature_c: float,
    sink_bottom_temperature_c: float,
    source_mode: str = "column_top_vapor",
    existing_hot_stream_id: Optional[str] = None,
    source_hot_pressure: Optional[float] = None,
    source_hot_stage: int = 2,
    source_hot_phase: str = "V",
    sink_reboiler_liquid_stage: Optional[int] = None,
    source_hot_outlet_target_c: Optional[float] = None,
    dt_min_c: float = DEFAULT_DT_MIN_C,
    flash_pressure: float = 0.0,
    source_available_heat_kw: Optional[float] = None,
    sink_reboiler_kw_abs: Optional[float] = None,
    column_pseudo_ports: Optional[List[str]] = None,
    ids: Optional[Dict[str, str]] = None,
    run: bool = True,
    logger: Optional[Any] = None,
    **extra_options: Any,
) -> Dict[str, Any]:
    """Build a direct heat-integration validation structure in Aspen.

    Parameters
    ----------
    source_mode:
        "column_top_vapor"  : create a hot vapor pseudo stream from source_column_id.
        "column_stage"      : create a pseudo stream from source_column_id at source_hot_stage/source_hot_phase.
        "existing_hot_stream": use an already-existing Aspen material stream as the hot source.

    High-temperature stream simulation
    ----------------------------------
    - If the source is a distillation condenser/top vapor, it is not manually
      populated.  The stream is connected to the source RadFrac PS(OUT), then
      Input\\PSEUDO_STAGE\\stream and, for vapor, Input\\PSEUDO_PHAS0\\stream = V
      are written.  Aspen generates composition/flow after the column runs.
    - If the source is a background process stream or a solvent-recovery bottom
      product stream, use source_mode="existing_hot_stream" and pass
      existing_hot_stream_id. The builder places a Heater/Cooler on that stream
      and reads the cooling duty at the unified 7 C minimum approach.
    """
    if extra_options:
        # Forward-compatible: ignore harmless future flags.
        pass

    src_id = _norm_id(source_column_id or "SRC")
    sink_id = _norm_id(sink_column_id)
    sink_n = max(2, int(sink_n_stages))
    src_n = max(2, int(source_n_stages or 2))
    rb_stage = int(sink_reboiler_liquid_stage) if sink_reboiler_liquid_stage is not None else max(1, sink_n - 1)
    hot_stage = max(1, min(int(source_hot_stage), src_n))
    hot_phase = str(source_hot_phase or "V").upper().strip()

    mode = str(source_mode or "column_top_vapor").lower().strip()
    target_temperature_was_explicit = source_hot_outlet_target_c is not None
    if source_hot_outlet_target_c is None:
        # Temporary fallback until the sink reboiler pseudo stream is connected.
        # The final default is recalculated from the actual sink pseudo stream
        # temperature below.
        source_hot_outlet_target_c = float(sink_bottom_temperature_c) + float(dt_min_c)
    source_hot_outlet_target_c = float(source_hot_outlet_target_c)

    if float(source_hot_temperature_c) < source_hot_outlet_target_c:
        # Do not fail: Aspen may still run if actual stream temperature differs,
        # but make the issue visible in the result/log.
        temperature_warning = (
            f"source_hot_temperature_c={source_hot_temperature_c} is below "
            f"target hot outlet/sink approach={source_hot_outlet_target_c}; "
            "direct heat integration is thermodynamically not favored."
        )
    else:
        temperature_warning = None

    prefix = _pair_id_prefix(src_id, sink_id)
    default_ids = {
        "reboiler_flash": f"F{prefix}R",
        "reboiler_pseudo": f"R{prefix}IN",
        "reboiler_vapor": f"R{prefix}V",
        "reboiler_liquid": f"R{prefix}L",
        "hot_pseudo": f"H{prefix}IN",
        "hot_cooler": f"H{prefix}X",
        "hot_out": f"H{prefix}O",
    }
    ids = {**default_ids, **(ids or {})}

    b = AspenVirtualHeatPumpBuilder(aspen, logger=logger)
    b.log(f"[BUILDER] {BUILDER_VERSION}")
    b.log(f"[DIRECT-HI] source_mode={mode}; source={src_id}; sink={sink_id}")
    # Warning is logged after the actual sink pseudo-stream temperature reference is resolved.

    # 1) Sink reboiler surrogate: sink column N-1 liquid/default pseudo -> Flash2.
    rb_feed = b.add_stream(ids["reboiler_pseudo"])
    rb_info = b.add_flash2_reboiler_surrogate(
        flash_id=ids["reboiler_flash"],
        feed_stream=rb_feed,
        vapor_out=ids["reboiler_vapor"],
        liquid_out=ids["reboiler_liquid"],
        temperature_c=float(sink_bottom_temperature_c),
        pressure=float(flash_pressure),
    )
    if not b.connect_pseudo_stream_from_column(sink_id, rb_feed, column_pseudo_ports):
        raise RuntimeError(f"Cannot connect {rb_feed} to {sink_id} pseudo output port.")
    b.set_column_pseudo_stream(sink_id, rb_feed, rb_stage, phase=None)

    # 2) Hot source side: existing process stream OR source column pseudo stream.
    # Create/connect the hot pseudo BEFORE reading any pseudo-stream temperature.
    # Aspen only populates Output\TEMP for RadFrac pseudo streams after an Engine.Run2().
    hot_source_is_pseudo = mode in {"column_top_vapor", "column_stage", "column_pseudo"}
    if mode == "existing_hot_stream":
        if not existing_hot_stream_id:
            raise ValueError("existing_hot_stream_id is required when source_mode='existing_hot_stream'.")
        hot_feed = _norm_id(existing_hot_stream_id)
        if b.node(rf"\Data\Streams\{hot_feed}") is None:
            raise RuntimeError(f"Existing hot stream not found in Aspen: {hot_feed}")
        b.log(f"[HOT-SOURCE] use existing stream {hot_feed}; no composition/flow is written.")
    elif hot_source_is_pseudo:
        hot_feed = b.add_stream(ids["hot_pseudo"])
        if not b.connect_pseudo_stream_from_column(src_id, hot_feed, column_pseudo_ports):
            raise RuntimeError(f"Cannot connect {hot_feed} to {src_id} pseudo output port.")
        # For condenser/top vapor use vapor phase; for liquid side-draw/background
        # column stream, pass source_hot_phase='L' or None.
        phase_to_write = "V" if hot_phase == "V" else None
        b.set_column_pseudo_stream(src_id, hot_feed, hot_stage, phase=phase_to_write)
        b.log(f"[HOT-SOURCE] {src_id} stage={hot_stage} phase={hot_phase or 'default'} -> {hot_feed}")
    else:
        raise ValueError(
            "source_mode must be one of: 'column_top_vapor', 'column_stage', 'column_pseudo', 'existing_hot_stream'."
        )

    # Critical timing fix, same logic as the successful heat-pump builder:
    # run once after pseudo-stream assignment so FGHIRB/HI_RB and optional hot pseudo
    # have real Output\TEMP values.  Only then set the hot-side Heater target.
    preliminary_run_status = "not_run"
    if run:
        try:
            b.run_aspen(f"populate {rb_feed} and {hot_feed} pseudo temperatures before direct-HI target")
            preliminary_run_status = "completed"
        except Exception as exc:
            preliminary_run_status = f"failed: {exc}"
            b.log(f"[WARN] preliminary direct-HI pseudo-temperature run failed; fallback may be used: {exc}")

    rb_reference_temperature_c, rb_reference_basis = b.resolve_stream_temperature_for_approach(
        rb_feed,
        float(sink_bottom_temperature_c),
        "direct-HI ΔT reference from sink reboiler liquid pseudo stream AFTER preliminary run",
    )
    hot_reference_temperature_c = float(source_hot_temperature_c)
    hot_reference_basis = "source_hot_temperature_C_input"
    if hot_source_is_pseudo:
        hot_reference_temperature_c, hot_reference_basis = b.resolve_stream_temperature_for_approach(
            hot_feed,
            float(source_hot_temperature_c),
            "direct-HI hot-source pseudo stream temperature AFTER preliminary run",
        )

    if not target_temperature_was_explicit:
        # Co-current conservative reference: cool the hot stream down to the actual
        # sink reboiler-liquid pseudo temperature + DTmin. Counter-current designs
        # can recover more heat; this virtual validation deliberately starts here.
        source_hot_outlet_target_c = rb_reference_temperature_c + float(dt_min_c)

    if hot_reference_temperature_c < source_hot_outlet_target_c:
        temperature_warning = (
            f"hot_reference_temperature_c={hot_reference_temperature_c} ({hot_reference_basis}) is below "
            f"target hot outlet/pseudo-stream approach={source_hot_outlet_target_c}; "
            "direct heat integration is thermodynamically not favored."
        )
    else:
        temperature_warning = None
    if temperature_warning:
        b.log(f"[WARN] {temperature_warning}")

    # Heater used as the hot-side cooler.  In Aspen sign convention its duty is
    # usually negative for cooling; use abs(duty) in post-processing.
    hot_cooler = b.add_heater(
        heater_id=ids["hot_cooler"],
        feed_stream=hot_feed,
        out_stream=ids["hot_out"],
        target_temperature_c=source_hot_outlet_target_c,
        target_pressure=0.0,
        role=f"direct heat source cooling for {sink_id} reboiler surrogate",
    )

    if run:
        b.run_aspen("direct heat-integration virtual scheme")

    frb_duty = b.read_block_duty(ids["reboiler_flash"]) if run else None
    hot_duty = b.read_block_duty(ids["hot_cooler"]) if run else None

    reboiler_demand = abs(frb_duty) if frb_duty is not None else None
    hot_available_from_aspen = abs(hot_duty) if hot_duty is not None else None
    screening_source_limit = _safe_float(source_available_heat_kw, None)
    screening_sink_limit = _safe_float(sink_reboiler_kw_abs, None)

    integrated_heat = _safe_min_positive(
        reboiler_demand,
        hot_available_from_aspen,
        screening_source_limit,
        screening_sink_limit,
    )
    external_reboiler = None
    if reboiler_demand is not None and integrated_heat is not None:
        external_reboiler = max(0.0, reboiler_demand - integrated_heat)

    residual_hot_cooling = None
    if screening_source_limit is not None and integrated_heat is not None:
        residual_hot_cooling = max(0.0, screening_source_limit - integrated_heat)

    rb_snapshot = b.snapshot_stream(rb_feed) if run else None
    hot_feed_snapshot = b.snapshot_stream(hot_feed) if run else None
    hot_out_snapshot = b.snapshot_stream(ids["hot_out"]) if run else None

    return {
        "version": BUILDER_VERSION,
        "status": "built_and_run" if run else "built_not_run",
        "source_mode": mode,
        "source_column": src_id if hot_source_is_pseudo else None,
        "sink_column": sink_id,
        "temperature_basis": {
            "source_hot_temperature_C_input": float(source_hot_temperature_c),
            "source_hot_reference_stream": hot_feed,
            "source_hot_reference_temperature_C": hot_reference_temperature_c,
            "source_hot_reference_basis": hot_reference_basis,
            "sink_bottom_temperature_C_input": float(sink_bottom_temperature_c),
            "sink_reboiler_liquid_pseudo_reference_stream": rb_feed,
            "sink_reboiler_liquid_pseudo_reference_temperature_C": rb_reference_temperature_c,
            "sink_reboiler_liquid_pseudo_reference_basis": rb_reference_basis,
            "preliminary_pseudo_temperature_run_status": preliminary_run_status,
            "dt_min_C": float(dt_min_c),
            "hot_cooler_target_C": source_hot_outlet_target_c,
            "target_temperature_was_explicit": bool(target_temperature_was_explicit),
            "temperature_margin_C_hot_reference_vs_pseudo_reference": float(hot_reference_temperature_c) - float(rb_reference_temperature_c) - float(dt_min_c),
            "warning": temperature_warning,
        },
        "pseudo_streams": {
            "sink_reboiler_liquid": {
                "stream": rb_feed,
                "stage": rb_stage,
                "phase": "default liquid",
                "path": rf"\Data\Blocks\{sink_id}\Input\PSEUDO_STAGE\{rb_feed}",
            },
            "source_hot": {
                "stream": hot_feed,
                "stage": hot_stage if hot_source_is_pseudo else None,
                "phase": hot_phase if hot_source_is_pseudo else "existing process stream",
                "stage_path": rf"\Data\Blocks\{src_id}\Input\PSEUDO_STAGE\{hot_feed}" if hot_source_is_pseudo else None,
                "phase_path": rf"\Data\Blocks\{src_id}\Input\PSEUDO_PHAS0\{hot_feed}" if hot_source_is_pseudo and hot_phase == "V" else None,
            },
        },
        "blocks": {
            "reboiler_flash": rb_info,
            "hot_source_cooler": hot_cooler,
        },
        "streams": {
            "reboiler_pseudo_snapshot": rb_snapshot,
            "hot_source_in_snapshot": hot_feed_snapshot,
            "hot_source_out_snapshot": hot_out_snapshot,
        },
        "energy_balance_raw": {
            "reboiler_flash_duty_raw": frb_duty,
            "reboiler_demand_raw_abs": reboiler_demand,
            "hot_source_cooler_duty_raw": hot_duty,
            "available_hot_stream_heat_raw_abs": hot_available_from_aspen,
            "source_available_heat_kw_screening_limit": screening_source_limit,
            "sink_reboiler_kw_abs_screening_limit": screening_sink_limit,
            "integrated_heat_raw": integrated_heat,
            "external_reboiler_after_heat_integration_raw": external_reboiler,
            "residual_hot_stream_cooling_raw": residual_hot_cooling,
            "note": (
                "Raw values follow Aspen active unit set and sign convention. "
                "The hot-side Heater is used as a cooler; use abs(duty) as available heat. "
                "Duties are not internally coupled by Aspen; Python computes coverage."
            ),
        },
        "log": b.log_lines,
    }


# Convenience alias with a shorter name for GUI/action handlers.
build_direct_hi_virtual_scheme = build_direct_heat_integration_virtual_scheme


__all__ = [
    "BUILDER_VERSION",
    "make_direct_heat_integration_validation_payload",
    "build_direct_heat_integration_virtual_scheme",
    "build_direct_hi_virtual_scheme",
]
