"""
process_intensification_agent_smart_v14.py

过程强化设计智能体独立模块 v14：仅负责直接热集成、热泵及二者联合方案。

能力边界：
1. 跨塔冷凝器或溶剂回收塔底部高温溶剂物流—再沸器直接热集成筛选与 Aspen 虚拟流股验证；
2. 单塔 MVR、外循环闭式热泵和跨塔热泵筛选；
3. P1/P2/P3 分组展示，每组独立推荐类内 Top3，不生成跨技术总分；P2 的类内分以其所含 P3 热泵分为基线，并增加热集成增益，避免联合方案因评分口径不同而低于其纯热泵子方案；
4. 显式生成“直接热集成 + 热泵”联合方案，覆盖同一再沸器和不同再沸器供热；
5. 将 BO/Aspen 优化后的塔温度与热负荷转化为可执行验证方案；支持对话命令“执行 P2 TopN”，在同一 BO 最优 Aspen 副本中依次运行 HINT 与热泵并进行联合经济评价。

不在本智能体内处理压力调整、多效精馏、进料预热或背景过程 pinch 网络设计；这些任务应由对应智能体负责。

与 GUI 的兼容接口：
- make_intensification_seed_message(process_data)
- chat_with_intensification_agent(user_message, chat_history, state_dict, api_key, base_url, model_name)
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

try:
    from openai import AsyncOpenAI
except Exception:
    AsyncOpenAI = None

try:
    from aspen_virtual_heat_pump_builder import (
        build_cross_column_heat_pump_virtual_scheme,
        make_cross_column_heat_pump_validation_payload,
    )
except Exception:
    build_cross_column_heat_pump_virtual_scheme = None
    make_cross_column_heat_pump_validation_payload = None

try:
    from aspen_virtual_heat_integration_builder import (
        build_direct_heat_integration_virtual_scheme,
        make_direct_heat_integration_validation_payload,
    )
except Exception:
    build_direct_heat_integration_virtual_scheme = None
    make_direct_heat_integration_validation_payload = None


# ============================================================
# 可调工程参数
# ============================================================

DEFAULT_DT_MIN_C = 7.0
DEFAULT_CARNOT_EFFICIENCY = 0.45
DEFAULT_MAX_PROMPT_JSON_CHARS = 18000

# Strict Aspen heat-pump gate defaults.  These do not change P1/P2/P3 screening
# scores; they only decide whether an executed pseudo-stream case represents
# genuine compressor-driven heat release and may enter economic ranking.
DEFAULT_HP_MIN_DISCHARGE_MARGIN_C = 0.5
DEFAULT_HP_MIN_HEAT_RELEASE_KW = 25.0
DEFAULT_HP_MIN_SINK_COVERAGE = 0.05
DEFAULT_HP_MIN_SOURCE_RECOVERY_FRACTION = 0.05
DEFAULT_HP_MAX_SOURCE_OVERDRAW_FRACTION = 0.05
DEFAULT_HP_MAX_HREB_TARGET_ERROR_C = 2.0
DEFAULT_HP_DUTY_SIGN_TOLERANCE_KW = 0.05
DEFAULT_HP_MAX_PRESSURE_RATIO = 4.0

# 技术层级仅用于分组展示，不换算为跨技术“全局优先分”。
# 每层最多推荐 3 个类内高分方案，由用户明确选择 P1/P2/P3 的 TopN 进入严格验证。
PRIORITY_TIER_DIRECT_HI = 1
PRIORITY_TIER_HYBRID = 2
PRIORITY_TIER_HEAT_PUMP = 3
TOP_RECOMMENDATIONS_PER_TIER = 3


# ============================================================
# 基础工具函数
# ============================================================

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


def _safe_round(value: Any, ndigits: int = 2) -> Any:
    val = _safe_float(value, None)
    if val is None:
        return None
    return round(val, ndigits)


def _fmt(value: Any, ndigits: int = 2, unit: str = "") -> str:
    val = _safe_float(value, None)
    if val is None:
        return "NA"
    text = f"{val:.{ndigits}f}"
    if unit:
        text += f" {unit}"
    return text


def _pct(value: Any, ndigits: int = 1) -> str:
    val = _safe_float(value, None)
    if val is None:
        return "NA"
    return f"{100.0 * val:.{ndigits}f}%"


def _truncate_text(text: str, max_chars: int = DEFAULT_MAX_PROMPT_JSON_CHARS) -> str:
    if not text:
        return text
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...【数据过长，已截断；优先使用摘要、筛选结果和关键塔数据】"


def _get_process_data_from_state(state_dict: Optional[dict]) -> dict:
    if not state_dict:
        return {}
    return state_dict.get("bo_optimization") or {}


def _temperature_c(pack: Any) -> Optional[float]:
    """兼容 BO 数据里的 {C, K, raw} 温度包。"""
    if isinstance(pack, dict):
        if pack.get("C") is not None:
            return _safe_float(pack.get("C"))
        if pack.get("K") is not None:
            k = _safe_float(pack.get("K"))
            return None if k is None else k - 273.15
        raw = _safe_float(pack.get("raw"))
        # Aspen 输出可能已经是 C，也可能是 K；这里按常见温度区间做弱判断。
        if raw is None:
            return None
        return raw - 273.15 if raw > 200.0 else raw
    return _safe_float(pack)


def _column_id(col: dict) -> str:
    return str(col.get("id") or col.get("column") or "UNKNOWN").upper().strip()


def _column_role(col: dict) -> str:
    return str(col.get("role") or "ordinary").strip()


def _col_top_temp_c(col: dict) -> Optional[float]:
    temps = col.get("temperatures") or {}
    return _temperature_c(temps.get("top"))


def _col_bottom_temp_c(col: dict) -> Optional[float]:
    temps = col.get("temperatures") or {}
    return _temperature_c(temps.get("bottom"))


def _col_q_reb_kw(col: dict) -> Optional[float]:
    return _safe_float((col.get("duties") or {}).get("reboiler_kw_abs"))


def _col_q_cond_kw(col: dict) -> Optional[float]:
    return _safe_float((col.get("duties") or {}).get("condenser_kw_abs"))


def _col_settings(col: dict) -> dict:
    return col.get("settings") or {}


def _col_n_stages(col: dict, default: int = 30) -> int:
    settings = _col_settings(col)
    n = _safe_float(settings.get("n_stages") or col.get("n_stages"), default)
    try:
        return max(2, int(round(float(n))))
    except Exception:
        return default


def _column_lookup(process_data: dict) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for col in process_data.get("columns", []) or []:
        cid = _column_id(col)
        if cid and cid != "UNKNOWN":
            out[cid] = col
    return out


def _first_value(mapping: Dict[str, Any], keys: List[str]) -> Any:
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        if key in mapping and mapping.get(key) not in (None, ""):
            return mapping.get(key)
    return None


def _is_solvent_recovery_column(col: Dict[str, Any]) -> bool:
    """Identify solvent-recovery columns without hard-coding a specific column ID."""
    text = " ".join([
        str(col.get("role") or ""),
        str(col.get("service") or ""),
        str(col.get("description") or ""),
        str(col.get("name") or ""),
    ]).lower()
    exact_roles = {
        "solvent_recovery", "solvent-recovery", "solvent recovery",
        "entrainer_recovery", "entrainer-recovery", "entrainer recovery",
        "extractive_solvent_recovery", "溶剂回收", "萃取剂回收",
    }
    role = str(col.get("role") or "").lower().strip()
    if role in exact_roles:
        return True
    return (
        ("solvent" in text or "entrainer" in text or "溶剂" in text or "萃取剂" in text)
        and ("recover" in text or "回收" in text)
    )


def _bottom_stream_metadata(col: Dict[str, Any]) -> Dict[str, Any]:
    """Read a solvent-recovery bottom-product stream from several compatible schemas.

    The agent never invents a heat duty.  It accepts an explicit recoverable-heat
    value, or a heat-capacity flow rate from which sink-specific sensible heat can
    be calculated.  Aspen validation can still use a real stream ID or a bottom
    liquid pseudo stream when screening heat-load data are missing.
    """
    candidates: List[Dict[str, Any]] = []
    direct_keys = [
        "solvent_bottom_stream", "bottom_solvent_stream", "bottom_product",
        "bottoms_product", "bottom_stream", "bottoms_stream",
    ]
    for key in direct_keys:
        val = col.get(key)
        if isinstance(val, dict):
            candidates.append(dict(val))
        elif isinstance(val, str) and val.strip():
            candidates.append({"id": val})

    for parent_key in ["products", "product_streams", "outlet_streams", "streams", "outputs"]:
        parent = col.get(parent_key)
        if not isinstance(parent, dict):
            continue
        for key in ["bottom", "bottoms", "BOTTOM", "BOTTOMS", "solvent", "recovered_solvent"]:
            val = parent.get(key)
            if isinstance(val, dict):
                candidates.append(dict(val))
            elif isinstance(val, str) and val.strip():
                candidates.append({"id": val})

    settings = _col_settings(col)
    fallback_id = _first_value(settings, [
        "bottom_stream_id", "bottoms_stream_id", "bottom_product_stream",
        "solvent_bottom_stream_id", "recovered_solvent_stream_id",
    ])
    if fallback_id:
        candidates.append({"id": fallback_id})

    meta: Dict[str, Any] = {}
    for item in candidates:
        for key, value in item.items():
            if key not in meta or meta.get(key) in (None, ""):
                if value not in (None, ""):
                    meta[key] = value
    temps = col.get("temperatures") or {}
    duties = col.get("duties") or {}

    stream_id = _first_value(meta, ["id", "stream_id", "name", "aspen_id", "stream"])
    temperature_c = None
    for value in [
        _first_value(meta, ["temperature_C", "temperature_c", "temp_C", "temp_c", "temperature", "temp"]),
        temps.get("bottom_product"), temps.get("bottom_stream"), temps.get("bottoms_stream"),
        temps.get("solvent_bottom"), temps.get("recovered_solvent"), temps.get("bottom"),
    ]:
        temperature_c = _temperature_c(value)
        if temperature_c is not None:
            break

    q_explicit = None
    q_keys = [
        "available_heat_kw_abs", "recoverable_heat_kw_abs", "sensible_heat_kw_abs",
        "cooling_duty_kw_abs", "heat_available_kw", "recoverable_heat_kw",
        "available_kw_abs", "cooling_duty_kw",
    ]
    for mapping in [meta, duties]:
        q_explicit = _safe_float(_first_value(mapping, q_keys), None)
        if q_explicit is not None:
            q_explicit = abs(q_explicit)
            break

    cp_flow = _safe_float(_first_value(meta, [
        "cp_flow_kw_per_C", "heat_capacity_flow_kw_per_C", "CP_kw_per_C",
        "mcp_kw_per_C", "heat_capacity_rate_kw_per_C",
    ]), None)
    if cp_flow is None:
        mass_flow = _safe_float(_first_value(meta, ["mass_flow_kg_s", "flow_kg_s"]), None)
        cp_mass = _safe_float(_first_value(meta, ["cp_kj_kg_K", "cp_kj_per_kg_K"]), None)
        if mass_flow is not None and cp_mass is not None:
            cp_flow = mass_flow * cp_mass
    if cp_flow is None:
        molar_flow = _safe_float(_first_value(meta, ["molar_flow_kmol_h", "flow_kmol_h"]), None)
        cp_molar = _safe_float(_first_value(meta, ["cp_kj_kmol_K", "cp_kj_per_kmol_K"]), None)
        if molar_flow is not None and cp_molar is not None:
            cp_flow = molar_flow * cp_molar / 3600.0

    minimum_outlet_c = _temperature_c(_first_value(meta, [
        "minimum_outlet_temperature_C", "min_outlet_temperature_C",
        "cooling_target_temperature_C", "target_temperature_C", "return_temperature_C",
    ]))

    cid = _column_id(col)
    source_mode = "existing_hot_stream" if stream_id else "column_stage"
    return {
        "source_key": f"{cid}:solvent_bottom",
        "source_type": "solvent_recovery_bottom_stream",
        "source_column": cid,
        "source_role": _column_role(col),
        "source_mode": source_mode,
        "existing_hot_stream_id": str(stream_id).upper().strip() if stream_id else None,
        "source_hot_stage": max(1, _col_n_stages(col) - 1),
        "source_hot_phase": "L",
        "temperature_C": temperature_c,
        "explicit_available_heat_kw": q_explicit,
        "cp_flow_kw_per_C": cp_flow,
        "minimum_outlet_temperature_C": minimum_outlet_c,
        "metadata_found": bool(meta),
        "heat_basis": (
            "explicit recoverable/cooling duty" if q_explicit is not None
            else "CP-flow sensible-heat estimate" if cp_flow is not None
            else "Aspen HINT duty required"
        ),
    }


def _direct_heat_sources(process_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    sources: List[Dict[str, Any]] = []
    for col in process_data.get("columns", []) or []:
        cid = _column_id(col)
        top_t = _col_top_temp_c(col)
        q_cond = _col_q_cond_kw(col)
        if top_t is not None and q_cond is not None and q_cond > 0:
            sources.append({
                "source_key": f"{cid}:condenser",
                "source_type": "column_condenser",
                "source_column": cid,
                "source_role": _column_role(col),
                "source_mode": "column_top_vapor",
                "existing_hot_stream_id": None,
                "source_hot_stage": 2,
                "source_hot_phase": "V",
                "temperature_C": top_t,
                "explicit_available_heat_kw": q_cond,
                "cp_flow_kw_per_C": None,
                "minimum_outlet_temperature_C": None,
                "heat_basis": "column condenser duty",
            })
        if _is_solvent_recovery_column(col):
            bottom = _bottom_stream_metadata(col)
            if bottom.get("temperature_C") is not None:
                sources.append(bottom)
    return sources


def _source_available_heat_for_sink(
    source: Dict[str, Any],
    sink_temperature_c: float,
    dt_min_c: float,
) -> Tuple[Optional[float], str]:
    """Return sink-specific recoverable heat without reusing a false fixed duty.

    Condenser latent duty is effectively independent of the selected sink once
    the temperature condition is satisfied.  A solvent-bottom sensible stream is
    different: its recoverable duty changes with the required outlet temperature.
    Therefore a single explicit kW value may only be reused across sinks when a
    reference outlet temperature is also available, allowing a CP-flow estimate.
    Otherwise Aspen HINT must quantify each match separately.
    """
    explicit = _safe_float(source.get("explicit_available_heat_kw"), None)
    cp_flow = _safe_float(source.get("cp_flow_kw_per_C"), None)
    hot_t = _safe_float(source.get("temperature_C"), None)
    source_type = str(source.get("source_type") or "column_condenser").lower().strip()
    sensible_source = source_type == "solvent_recovery_bottom_stream"
    min_out = _safe_float(source.get("minimum_outlet_temperature_C"), None)

    target = float(sink_temperature_c) + float(dt_min_c)
    if min_out is not None:
        target = max(target, min_out)

    inferred_cp_from_explicit = False
    if sensible_source and (cp_flow is None or cp_flow <= 0) and explicit is not None:
        # Interpret explicit heat as the recoverable duty down to the supplied
        # minimum/reference outlet temperature.  Without that temperature basis,
        # the same explicit duty must not be copied to every sink.
        if hot_t is not None and min_out is not None and hot_t > min_out + 1e-9:
            cp_flow = explicit / (hot_t - min_out)
            inferred_cp_from_explicit = True
        else:
            return None, (
                "sensible-stream explicit duty has no reference outlet temperature; "
                "sink-specific Aspen HINT duty required"
            )

    q_sensible = None
    if cp_flow is not None and cp_flow > 0 and hot_t is not None:
        q_sensible = cp_flow * max(0.0, hot_t - target)

    if sensible_source:
        if q_sensible is None:
            return None, "missing sink-specific CP-flow/enthalpy basis; Aspen HINT validation required"
        if explicit is not None:
            basis = (
                "explicit duty converted to CP-flow using reference outlet, then cooled to sink+DTmin"
                if inferred_cp_from_explicit else
                "min(explicit duty, CP-flow sensible heat to sink+DTmin)"
            )
            return min(explicit, q_sensible), basis
        return q_sensible, "CP-flow sensible heat to sink+DTmin"

    # Condenser/top-vapor latent heat: the column condenser duty is a valid
    # sink-independent upper bound after the temperature feasibility check.
    if explicit is not None and explicit >= 0:
        return explicit, str(source.get("heat_basis") or "column condenser duty")
    return None, "missing condenser duty; Aspen HINT validation required"


def _score_direct_heat_exchange(
    hot_temperature_c: float,
    cold_temperature_c: float,
    available_kw: Optional[float],
    required_kw: float,
    dt_min_c: float,
    exchanger_count: int = 1,
    source_type: str = "column_condenser",
) -> Dict[str, Any]:
    """Score compressor-free hot-stream -> reboiler matches with the original P1 formula.

    The P1 score remains unchanged:
    - temperature quality: 25;
    - sink coverage: 45;
    - source utilization: 25;
    - configuration simplicity: 5.

    Only the preliminary heat-duty calculation is corrected.  Condenser latent
    duty may be treated as a fixed upper bound after the temperature gate, while
    solvent-bottom sensible heat must be calculated for the selected sink outlet
    target.  If CP-flow/enthalpy data are unavailable, no fictitious energy-saving
    kW is generated and Aspen HINT is required.
    """
    driving_force = float(hot_temperature_c) - float(cold_temperature_c)
    margin = driving_force - float(dt_min_c)
    q_req = max(0.0, float(required_kw))
    q_avail = None if available_kw is None else max(0.0, float(available_kw))
    matched = min(q_avail, q_req) if q_avail is not None else None
    sink_cov = matched / q_req if matched is not None and q_req > 0 else None
    source_util = matched / q_avail if matched is not None and q_avail > 0 else None

    sensible_source = str(source_type or "").lower().strip() == "solvent_recovery_bottom_stream"
    specific_recoverable_heat_per_cp = max(0.0, margin) if sensible_source else None
    required_cp_flow_for_full_cover = (
        q_req / specific_recoverable_heat_per_cp
        if sensible_source and specific_recoverable_heat_per_cp and q_req > 0 else None
    )

    if margin < 0:
        score = max(0.0, 20.0 + 2.0 * margin)
        feasibility = "不优先"
        reason = f"实际热端与冷端温差不足 {dt_min_c:g} °C。"
    elif matched is None:
        # Keep the original P1 pending-data score.  The corrected duty model only
        # prevents one fixed sensible-heat value from being copied to every sink.
        temperature_quality = 20.0 * math.exp(-max(0.0, margin) / 35.0)
        score = min(39.0, 15.0 + temperature_quality)
        feasibility = "数据不足"
        reason = (
            "温度满足 DTmin，但缺少该热阱对应的可回收热负荷或 CP-flow；"
            "不估算节能 kW，需用 Aspen HINT duty 定量。"
        )
    elif matched <= 0 or q_req <= 0:
        score = 0.0
        feasibility = "不优先"
        reason = "可利用热量或再沸需求为零。"
    else:
        # Original P1 scoring parameters are intentionally retained.
        temperature_score = 25.0 * math.exp(-max(0.0, margin) / 35.0)
        sink_score = 45.0 * min(1.0, sink_cov or 0.0)
        source_score = 25.0 * min(1.0, source_util or 0.0)
        simplicity_score = 5.0 / max(1, int(exchanger_count))
        score = min(100.0, temperature_score + sink_score + source_score + simplicity_score)
        if (sink_cov or 0.0) >= 0.50 and score >= 70.0:
            feasibility = "优先"
        elif (sink_cov or 0.0) >= 0.20 and score >= 50.0:
            feasibility = "可选"
        else:
            feasibility = "边界"
        reason = "满足统一 DTmin；按温度品位、再沸覆盖率、热源利用率和单一换热器简洁度评分。"

    return {
        "score": round(score, 1),
        "feasibility": feasibility,
        "reason": reason,
        "temperature_driving_force_C": round(driving_force, 3),
        "surplus_after_dtmin_C": round(margin, 3),
        "matched_kw_abs": None if matched is None else round(matched, 3),
        "gross_hot_utility_saving_kw": None if matched is None else round(matched, 3),
        "specific_recoverable_heat_per_unit_CP_kw_per_kw_per_C": (
            None if specific_recoverable_heat_per_cp is None else round(specific_recoverable_heat_per_cp, 3)
        ),
        "required_CP_flow_for_full_sink_coverage_kw_per_C": (
            None if required_cp_flow_for_full_cover is None else round(required_cp_flow_for_full_cover, 5)
        ),
        "cold_duty_coverage": None if sink_cov is None else round(sink_cov, 4),
        "hot_duty_coverage": None if source_util is None else round(source_util, 4),
        "scoring_standard": "unified_direct_heat_exchange_v1",
        "calculation_standard": "sink_specific_heat_availability_v2",
        "score_components": {
            "temperature_quality_max": 25.0,
            "sink_coverage_max": 45.0,
            "source_utilization_max": 25.0,
            "configuration_simplicity_max": 5.0,
        },
    }

def _score_heat_pump_match(
    temperature_lift_c: float,
    cop_heating: float,
    sink_coverage: float,
    source_coverage: float,
    same_column: bool,
    max_lift_c: float = 80.0,
    min_cop: float = 1.5,
) -> Dict[str, Any]:
    """Unified score for single-column, cross-column and residual heat pumps.

    The paper's placement criteria are translated into one normalized scale:
    meet the heat load, use available source heat, minimize temperature lift,
    maximize COP, and prefer the simpler one-column configuration when equal.
    """
    lift = max(0.0, float(temperature_lift_c))
    cop = max(0.0, float(cop_heating))
    sink_cov = max(0.0, min(1.0, float(sink_coverage)))
    source_cov = max(0.0, min(1.0, float(source_coverage)))
    sink_score = 35.0 * sink_cov
    source_score = 20.0 * source_cov
    cop_quality = max(0.0, min(1.0, (cop - min_cop) / max(1e-9, 5.0 - min_cop)))
    cop_score = 25.0 * cop_quality
    lift_quality = math.exp(-lift / 35.0) if lift <= max_lift_c else 0.0
    lift_score = 15.0 * lift_quality
    # Every current candidate uses one hot source and one cold sink; therefore
    # each needs the minimum evaporator/condenser pair and receives the same
    # configuration score.  Future multi-stream matches should reduce this term.
    simplicity_score = 5.0
    score = min(100.0, sink_score + source_score + cop_score + lift_score + simplicity_score)
    return {
        "score": round(score, 1),
        "scoring_standard": "unified_heat_pump_match_v1",
        "score_components": {
            "sink_coverage": round(sink_score, 3),
            "source_utilization": round(source_score, 3),
            "cop_quality": round(cop_score, 3),
            "minimum_temperature_lift": round(lift_score, 3),
            "configuration_simplicity": round(simplicity_score, 3),
        },
    }


def _priority_metadata(
    candidate: Dict[str, Any],
    technology_class: str,
    priority_eligible: bool = True,
) -> Dict[str, Any]:
    """Attach P1/P2/P3 grouping metadata without creating a cross-technology score.

    ``score`` / ``technical_score`` is only comparable inside one tier.  The
    three tiers are shown separately and each tier exposes at most three
    recommendations.  The user, not an artificial global score, chooses which
    tier/rank enters strict Aspen validation.
    """
    technical = max(0.0, min(100.0, _safe_float(candidate.get("score"), 0.0) or 0.0))
    cls = str(technology_class or "").strip().lower()
    if cls == "direct_heat_integration":
        tier = PRIORITY_TIER_DIRECT_HI
        label = "P1 直接热集成"
    elif cls == "hybrid":
        tier = PRIORITY_TIER_HYBRID
        label = "P2 热集成+热泵"
    else:
        tier = PRIORITY_TIER_HEAT_PUMP
        label = "P3 纯热泵"

    candidate["technical_score"] = round(technical, 1)
    candidate["within_class_score"] = round(technical, 1)
    candidate["priority_tier"] = tier
    candidate["priority_class"] = label
    candidate["priority_eligible"] = bool(priority_eligible)
    candidate.pop("global_priority_score", None)
    candidate["score_semantics"] = (
        "technical_score is comparable only within the same P1/P2/P3 tier; "
        "no cross-technology global score is generated"
    )
    return candidate


def _heat_pump_operability_constraints(process_data: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """Read optional equipment minimum-load constraints without hard-coding them.

    Accepted locations are process_data['intensification_constraints'] and
    process_data['heat_pump_constraints'].  Missing values stay ``None`` and
    therefore become explicit validation requirements rather than invented
    screening constants.
    """
    merged: Dict[str, Any] = {}
    for key in ["intensification_constraints", "heat_pump_constraints"]:
        pack = process_data.get(key) or {}
        if isinstance(pack, dict):
            merged.update(pack)
    return {
        "min_hp_duty_kw": _safe_float(merged.get("min_hp_duty_kw"), None),
        "min_hp_source_branch_fraction": _safe_float(
            merged.get("min_hp_source_branch_fraction")
            or merged.get("min_mvr_source_split_fraction"),
            None,
        ),
        "min_hp_residual_sink_fraction": _safe_float(
            merged.get("min_hp_residual_sink_fraction"), None
        ),
    }


def _evaluate_hybrid_minimum_load(
    *,
    hp_hot_kw: float,
    residual_sink_fraction: float,
    source_branch_fraction: float,
    constraints: Dict[str, Optional[float]],
) -> Dict[str, Any]:
    """Evaluate explicit user/equipment minimum-load limits for a P2 heat pump."""
    violations: List[str] = []
    min_duty = constraints.get("min_hp_duty_kw")
    min_branch = constraints.get("min_hp_source_branch_fraction")
    min_residual = constraints.get("min_hp_residual_sink_fraction")
    if min_duty is not None and hp_hot_kw + 1e-9 < min_duty:
        violations.append(f"Q_HP={hp_hot_kw:.3f} kW < min_hp_duty_kw={min_duty:.3f} kW")
    if min_branch is not None and source_branch_fraction + 1e-9 < min_branch:
        violations.append(
            f"source_branch_fraction={source_branch_fraction:.4f} < "
            f"min_hp_source_branch_fraction={min_branch:.4f}"
        )
    if min_residual is not None and residual_sink_fraction + 1e-9 < min_residual:
        violations.append(
            f"residual_sink_fraction={residual_sink_fraction:.4f} < "
            f"min_hp_residual_sink_fraction={min_residual:.4f}"
        )
    known = any(v is not None for v in constraints.values())
    return {
        "constraints_available": known,
        "minimum_load_passed": None if not known else not violations,
        "violations": violations,
        "constraints": constraints,
    }


def _fallback_cross_column_heat_pump_payload(
    source_column: Dict[str, Any],
    sink_column: Dict[str, Any],
    candidate: Optional[Dict[str, Any]] = None,
    pressure_ratio: float = 2.0,
    reboiler_approach_c: float = DEFAULT_DT_MIN_C,
) -> Dict[str, Any]:
    candidate = candidate or {}
    src_id = _column_id(source_column) if source_column else str(candidate.get("source_column") or "C1").upper()
    sink_id = _column_id(sink_column) if sink_column else str(candidate.get("sink_column") or "C1").upper()
    src_n = _col_n_stages(source_column or {})
    sink_n = _col_n_stages(sink_column or {})
    src_top_t = _col_top_temp_c(source_column or {})
    sink_bot_t = _col_bottom_temp_c(sink_column or {})
    if src_top_t is None:
        src_top_t = _safe_float(candidate.get("source_top_temperature_C"))
    if sink_bot_t is None:
        sink_bot_t = _safe_float(candidate.get("sink_bottom_temperature_C"))
    return {
        "callable": "build_cross_column_heat_pump_virtual_scheme(aspen, ...)",
        "source_column_id": src_id,
        "sink_column_id": sink_id,
        "required_inputs": {
            "source_n_stages": src_n,
            "sink_n_stages": sink_n,
            "source_top_temperature_C": src_top_t,
            "sink_bottom_temperature_C": sink_bot_t,
            "source_top_pressure": "process_data source column pressure if available; otherwise Aspen/feed pressure fallback",
            "pressure_ratio": pressure_ratio,
            "reboiler_approach_C": reboiler_approach_c,
        },
        "aspen_structure": [
            f"{sink_id} stage {sink_n - 1} liquid/default pseudo stream -> Flash2 reboiler surrogate",
            f"{src_id} stage 2 vapor pseudo stream -> Compressor -> HREB -> HCOND",
            "Compressor: TYPE=ISENTROPIC, OPT_SPEC=PRES, PRES=source_top_pressure*pressure_ratio, SEFF=0.75, MEFF=0.95",
            f"HREB TEMP={sink_id} bottom temperature + {reboiler_approach_c:g} °C",
            f"HCOND TEMP={src_id} top temperature, PRES=0；读取 abs(HCOND duty) 作为热泵后仍需处理的源塔冷凝器负荷。",
        ],
        "energy_interpretation": {
            "base_reboiler_surrogate": "abs(Flash2 duty on sink reboiler liquid pseudo stream)",
            "heat_supplied_by_heat_pump": "abs(HREB duty)",
            "external_reboiler_after_heat_pump": "max(0, abs(Flash2 duty) - abs(HREB duty))",
            "compressor_electricity": "Compr power/work output path, active Aspen unit set",
            "residual_source_condenser_load": "abs(HCOND duty)，不能默认塔顶冷凝负荷为 0。",
        },
    }


def make_global_heat_pump_validation_payload(
    process_data: dict,
    candidate: Dict[str, Any],
    pressure_ratio: float = 2.0,
    reboiler_approach_c: float = DEFAULT_DT_MIN_C,
) -> Dict[str, Any]:
    lookup = _column_lookup(process_data)
    src_col = lookup.get(str(candidate.get("source_column") or "").upper().strip(), {})
    sink_col = lookup.get(str(candidate.get("sink_column") or "").upper().strip(), {})
    if make_cross_column_heat_pump_validation_payload is not None:
        try:
            return make_cross_column_heat_pump_validation_payload(
                src_col, sink_col, candidate=candidate,
                pressure_ratio=pressure_ratio, reboiler_approach_c=reboiler_approach_c,
            )
        except Exception:
            pass
    return _fallback_cross_column_heat_pump_payload(
        src_col, sink_col, candidate=candidate,
        pressure_ratio=pressure_ratio, reboiler_approach_c=reboiler_approach_c,
    )



def _fallback_direct_heat_integration_payload(
    source_column: Dict[str, Any],
    sink_column: Dict[str, Any],
    candidate: Optional[Dict[str, Any]] = None,
    dt_min_c: float = DEFAULT_DT_MIN_C,
) -> Dict[str, Any]:
    """Fallback payload for condenser or solvent-bottom direct integration."""
    candidate = candidate or {}
    src_id = _column_id(source_column) if source_column else str(candidate.get("hot_source_column") or "C1").upper()
    sink_id = _column_id(sink_column) if sink_column else str(candidate.get("cold_sink_column") or "C1").upper()
    src_n = _col_n_stages(source_column or {})
    sink_n = _col_n_stages(sink_column or {})
    source_mode = str(candidate.get("source_mode") or "column_top_vapor")
    source_hot_stage = int(candidate.get("source_hot_stage") or (2 if source_mode == "column_top_vapor" else max(1, src_n - 1)))
    source_hot_phase = str(candidate.get("source_hot_phase") or ("V" if source_mode == "column_top_vapor" else "L"))
    source_hot_t = _safe_float(candidate.get("hot_temperature_C"), None)
    if source_hot_t is None:
        source_hot_t = _col_top_temp_c(source_column or {}) if source_mode == "column_top_vapor" else _col_bottom_temp_c(source_column or {})
    sink_bot_t = _col_bottom_temp_c(sink_column or {})
    if sink_bot_t is None:
        sink_bot_t = _safe_float(candidate.get("cold_temperature_C"))
    existing_hot_stream_id = candidate.get("existing_hot_stream_id")
    source_desc = (
        f"existing hot stream {existing_hot_stream_id}" if source_mode == "existing_hot_stream"
        else f"{src_id} stage {source_hot_stage} {source_hot_phase} pseudo stream"
    )
    return {
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
            "sink_bottom_temperature_C": sink_bot_t,
            "source_hot_stage": source_hot_stage,
            "source_hot_phase": source_hot_phase,
            "sink_reboiler_liquid_stage": max(1, sink_n - 1),
            "dt_min_C": dt_min_c,
            "source_hot_outlet_target_C": None,
            "screening_source_hot_outlet_target_C": (sink_bot_t + dt_min_c) if sink_bot_t is not None else None,
            "flash_pressure": 0.0,
        },
        "aspen_structure": [
            f"{sink_id} stage {max(1, sink_n - 1)} liquid/default pseudo stream -> Flash2 reboiler surrogate",
            f"{source_desc} -> HINT(Heater used as cooler)",
            "No compressor; read abs(Flash2 duty) and abs(HINT duty), then compute direct heat coverage in Python.",
        ],
        "energy_interpretation": {
            "base_reboiler_surrogate": "abs(Flash2 duty on sink reboiler liquid pseudo stream)",
            "available_hot_stream_heat": "abs(HINT duty) after cooling source hot stream to sink_bottom+DTmin",
            "integrated_heat": "min(abs(Flash2 duty), abs(HINT duty), optional source/sink limits)",
            "external_reboiler_after_heat_integration": "max(0, abs(Flash2 duty)-integrated_heat)",
        },
    }


def make_cross_column_heat_integration_validation_payload(
    process_data: dict,
    candidate: Dict[str, Any],
    dt_min_c: float = DEFAULT_DT_MIN_C,
) -> Dict[str, Any]:
    """Create an Aspen payload for condenser or solvent-bottom direct integration."""
    lookup = _column_lookup(process_data)
    src_col = lookup.get(str(candidate.get("hot_source_column") or candidate.get("source_column") or "").upper().strip(), {})
    sink_col = lookup.get(str(candidate.get("cold_sink_column") or candidate.get("sink_column") or "").upper().strip(), {})
    source_mode = str(candidate.get("source_mode") or "column_top_vapor")
    source_hot_stage = int(candidate.get("source_hot_stage") or (2 if source_mode == "column_top_vapor" else max(1, _col_n_stages(src_col) - 1)))
    source_hot_phase = str(candidate.get("source_hot_phase") or ("V" if source_mode == "column_top_vapor" else "L"))
    existing_hot_stream_id = candidate.get("existing_hot_stream_id")
    payload_candidate = {
        **candidate,
        "source_mode": source_mode,
        "existing_hot_stream_id": existing_hot_stream_id,
        "source_hot_temperature_C": candidate.get("hot_temperature_C"),
        "sink_bottom_temperature_C": candidate.get("cold_temperature_C"),
        "source_available_heat_kw": candidate.get("available_kw_abs"),
        "sink_reboiler_kw_abs": candidate.get("required_kw_abs"),
        "source_hot_stage": source_hot_stage,
        "source_hot_phase": source_hot_phase,
        "dt_min_C": dt_min_c,
    }
    if make_direct_heat_integration_validation_payload is not None:
        try:
            return make_direct_heat_integration_validation_payload(
                src_col, sink_col, candidate=payload_candidate, dt_min_c=dt_min_c
            )
        except Exception:
            pass
    fallback = _fallback_direct_heat_integration_payload(src_col, sink_col, candidate=payload_candidate, dt_min_c=dt_min_c)
    req = fallback.setdefault("required_inputs", {})
    req.update({
        "source_mode": source_mode,
        "existing_hot_stream_id": existing_hot_stream_id,
        "source_hot_stage": source_hot_stage,
        "source_hot_phase": source_hot_phase,
    })
    return fallback


def _col_feed_temperatures(col: dict) -> Dict[str, Optional[float]]:
    temps = (col.get("temperatures") or {}).get("feed_streams") or {}
    return {str(k): _temperature_c(v) for k, v in temps.items()}


def _quality_label(score: float) -> str:
    if score >= 80:
        return "优先"
    if score >= 60:
        return "可选"
    if score >= 40:
        return "边界"
    return "不优先"


# ============================================================
# 内置知识：后续可被 RAG 替换或增强
# ============================================================

BUILTIN_HEAT_PUMP_RAG_CONTEXT = """
【内置热集成—热泵规则库 v2】
1. 全局节能筛选必须先做无需压缩机的直接热集成；技术优先级是硬约束，不允许高 COP 或高类内分数的热泵越过温度可行的直接换热。
2. 直接热集成的热源池同时包含塔顶冷凝热和溶剂回收塔底高温溶剂物流；所有热源统一按 DTmin=7 °C、再沸覆盖率、热源利用率和换热器数量计算类内技术分。温度可行但热负荷缺失时仍保持 P1，先用 Aspen HINT 定量。
3. 单塔 MVR/机械蒸汽再压缩通过压缩塔顶蒸汽，使其冷凝温度高于再沸器供热温度；适合温升较小、负荷匹配且塔顶蒸汽适合压缩的场景。
4. 外循环闭式热泵使用外部工质在低温端吸热、在高温端放热，适用于过程蒸汽不适合直接压缩但温度窗口仍合理的场景。
5. 热泵评分必须基于热集成后的残余热源和残余热阱；按 physical source_key/sink_key 扣除直接集成热量，塔底溶剂显热与同塔冷凝热不得误判为同一热源。
6. 联合方案需同时考虑：同一再沸器由直接热集成与热泵补热，以及两者分别服务不同再沸器。
7. 文献方法要求同时检查温度与热负荷，并优先减少参与流股/换热器数量；热泵优先采用满足目标的最小温升。
8. 经济判断必须同时考虑蒸汽削减、冷却负荷削减、压缩机电耗、设备投资和可操作性风险。
""".strip()

# ============================================================
# 数据摘要



# ============================================================
# 数据摘要
# ============================================================

def format_process_data_summary(process_data: dict) -> str:
    """将 BO 返回的数据包压缩成过程强化 Agent 可直接阅读的摘要。"""
    if not process_data:
        return "暂无 BO 优化结果数据。请先完成阶段 2：建立分离过程。"

    status = process_data.get("status", "unknown")
    obj = process_data.get("objective", {}) or {}
    lines = [
        f"BO 数据状态: {status}",
        f"选中迭代: {process_data.get('selected_iteration')}",
        f"TAC: {_fmt(obj.get('tac_usd_per_year'), 2, 'USD/yr')}",
        f"CO2: {_fmt(obj.get('emissions_kt_co2_per_year'), 4, 'kt/yr')}",
        f"总再沸负荷: {_fmt(obj.get('total_reboiler_kw_abs'), 3, 'kW')}",
        f"总冷凝负荷: {_fmt(obj.get('total_condenser_kw_abs'), 3, 'kW')}",
        "",
        "各塔关键数据：",
    ]

    columns = process_data.get("columns", []) or []
    if not columns:
        lines.append("- 未收到 columns 字段；无法读取单塔温度、负荷和塔设置。")

    for col in columns:
        cid = _column_id(col)
        settings = _col_settings(col)
        top_t = _col_top_temp_c(col)
        bot_t = _col_bottom_temp_c(col)
        q_reb = _col_q_reb_kw(col)
        q_cond = _col_q_cond_kw(col)
        feed_ts = _col_feed_temperatures(col)
        feed_desc = [f"{sid}:{_fmt(t, 2, '°C')}" for sid, t in feed_ts.items()]
        lines.append(
            f"- {cid} [{_column_role(col)}]: "
            f"N={_fmt(settings.get('n_stages'), 0)}, "
            f"RR={_fmt(settings.get('reflux_ratio'), 4)}, "
            f"D={_fmt(settings.get('distillate_flow'), 4)}, "
            f"feed_stage={settings.get('feed_stages')}, "
            f"T_top={_fmt(top_t, 2, '°C')}, "
            f"T_bottom={_fmt(bot_t, 2, '°C')}, "
            f"Q_reb={_fmt(q_reb, 3, 'kW')}, "
            f"Q_cond={_fmt(q_cond, 3, 'kW')}, "
            f"Feed_T=[{'; '.join(feed_desc)}]"
        )

    return "\n".join(lines)


# ============================================================
# 热泵 / 热集成确定性筛选
# ============================================================

def estimate_heating_cop(
    evaporating_temp_c: float,
    condensing_temp_c: float,
    carnot_efficiency: float = DEFAULT_CARNOT_EFFICIENCY,
) -> Optional[float]:
    """
    估算供热型热泵 COP。

    COP_heating,ideal = T_hot / (T_hot - T_cold)
    COP_real ≈ eta_carnot * COP_ideal

    这里只用于早期方案筛选，不替代压缩机严格模型。
    """
    t_cold_k = _safe_float(evaporating_temp_c)
    t_hot_k = _safe_float(condensing_temp_c)
    if t_cold_k is None or t_hot_k is None:
        return None
    t_cold_k += 273.15
    t_hot_k += 273.15
    if t_cold_k <= 0 or t_hot_k <= 0 or t_hot_k <= t_cold_k:
        return None
    cop_ideal = t_hot_k / (t_hot_k - t_cold_k)
    cop = carnot_efficiency * cop_ideal
    # 筛选阶段防止极小温差导致非现实超大 COP。
    return max(1.05, min(cop, 12.0))


def analyze_single_column_heat_pumps(
    process_data: dict,
    dt_min_c: float = DEFAULT_DT_MIN_C,
    carnot_efficiency: float = DEFAULT_CARNOT_EFFICIENCY,
) -> List[Dict[str, Any]]:
    """筛选每座塔的单塔 MVR / 外循环热泵候选。"""
    candidates: List[Dict[str, Any]] = []
    for col in process_data.get("columns", []) or []:
        cid = _column_id(col)
        role = _column_role(col)
        t_top = _col_top_temp_c(col)
        t_bottom = _col_bottom_temp_c(col)
        q_reb = _col_q_reb_kw(col)
        q_cond = _col_q_cond_kw(col)

        missing = []
        if t_top is None:
            missing.append("塔顶温度")
        if t_bottom is None:
            missing.append("塔釜温度")
        if q_reb is None:
            missing.append("再沸器负荷")
        if q_cond is None:
            missing.append("冷凝器负荷")
        if missing:
            candidates.append({
                "column": cid,
                "role": role,
                "technology": "MVR/闭式热泵",
                "feasibility": "数据不足",
                "score": 0,
                "missing_data": missing,
                "reason": f"缺少 {', '.join(missing)}，无法判断温升和负荷匹配。",
            })
            continue

        # 要把热量送入塔釜再沸器，热泵冷凝端温度至少要高于塔釜温度 dt_min。
        hp_evap_c = t_top
        hp_cond_c = t_bottom + dt_min_c
        effective_lift_c = hp_cond_c - hp_evap_c
        cop = estimate_heating_cop(hp_evap_c, hp_cond_c, carnot_efficiency)

        if cop is None or effective_lift_c <= 0 or q_reb <= 0 or q_cond <= 0:
            candidates.append({
                "column": cid,
                "role": role,
                "technology": "MVR/闭式热泵",
                "feasibility": "不优先",
                "score": 10,
                "top_temperature_C": t_top,
                "bottom_temperature_C": t_bottom,
                "effective_lift_C": effective_lift_c,
                "reason": "温度或负荷数据不满足基本热泵筛选条件。",
            })
            continue

        # 热泵能量衡算：Q_hot = COP * W, Q_cold = Q_hot - W。
        # 若只使用本塔冷凝热，则 Q_cold 不能超过 Q_cond。
        max_reboiler_by_source = q_cond / max(1e-9, (1.0 - 1.0 / cop))
        covered_reboiler_kw = min(q_reb, max_reboiler_by_source)
        compressor_kw = covered_reboiler_kw / cop
        required_source_kw = covered_reboiler_kw - compressor_kw
        condenser_utility_reduction_kw = min(q_cond, required_source_kw)
        reboiler_coverage = covered_reboiler_kw / q_reb if q_reb > 0 else 0.0
        condenser_coverage = condenser_utility_reduction_kw / q_cond if q_cond > 0 else 0.0

        score_pack = _score_heat_pump_match(
            effective_lift_c, cop, reboiler_coverage, condenser_coverage, same_column=True
        )
        score = score_pack["score"]

        if effective_lift_c <= 25 and cop >= 3.0 and reboiler_coverage >= 0.5:
            feasibility = "优先"
        elif effective_lift_c <= 45 and cop >= 2.0 and reboiler_coverage >= 0.3:
            feasibility = "可选"
        elif effective_lift_c <= 65 and cop >= 1.5:
            feasibility = "边界"
        else:
            feasibility = "不优先"

        if score >= 80:
            feasibility = "优先"
        elif score >= 60 and feasibility not in ["优先"]:
            feasibility = "可选"
        elif score < 40:
            feasibility = "不优先"

        candidates.append({
            "column": cid,
            "role": role,
            "technology": "单塔 MVR / 外循环闭式热泵",
            "feasibility": feasibility,
            "score": score,
            "scoring_standard": score_pack.get("scoring_standard"),
            "score_components": score_pack.get("score_components"),
            "top_temperature_C": t_top,
            "bottom_temperature_C": t_bottom,
            "dt_min_C": dt_min_c,
            "heat_pump_evaporating_temperature_C": hp_evap_c,
            "heat_pump_condensing_temperature_C": hp_cond_c,
            "effective_lift_C": effective_lift_c,
            "estimated_COP_heating": round(cop, 3),
            "reboiler_kw_abs": q_reb,
            "condenser_kw_abs": q_cond,
            "covered_reboiler_kw": round(covered_reboiler_kw, 3),
            "reboiler_coverage": round(reboiler_coverage, 4),
            "condenser_utility_reduction_kw": round(condenser_utility_reduction_kw, 3),
            "condenser_coverage": round(condenser_coverage, 4),
            "estimated_compressor_kw": round(compressor_kw, 3),
            "required_source_heat_kw": round(required_source_kw, 3),
            "aspen_validation_actions": [
                f"复制 {cid} 基准塔模型，新增压缩机/热泵回路。",
                f"将 {cid} 塔顶冷凝热作为热泵蒸发端热源，塔釜再沸器作为热泵冷凝端热阱。",
                "在 Aspen 中扫描压缩比或热泵冷凝温度，约束再沸器满足 DTmin。",
                "比较节省蒸汽、增加电耗、冷却水削减与 TAC/CO2 变化。",
            ],
            "risks": [
                "该 COP 为筛选估算，不代表压缩机严格性能。",
                "MVR 需要检查塔顶蒸汽是否适合直接压缩：腐蚀性、两相夹带、可凝性和压缩后温度。",
                "若塔顶产品温度敏感，需要检查压缩导致的热降解和控制稳定性。",
            ],
        })

    for item in candidates:
        _priority_metadata(item, "heat_pump", priority_eligible=True)
    candidates.sort(
        key=lambda x: _safe_float(x.get("technical_score", x.get("score")), 0.0) or 0.0,
        reverse=True,
    )
    return candidates




def _direct_heat_integration_is_temperature_feasible(candidate: Dict[str, Any]) -> bool:
    """Temperature-feasible direct recovery is always a P1 option.

    Missing heat duty does not demote the option below a compressor-based heat
    pump.  It only means the P1 option must be quantified with Aspen HINT before
    residual duties and hybrid portfolios can be calculated.
    """
    surplus = _safe_float(candidate.get("surplus_after_dtmin_C"), None)
    return (
        surplus is not None
        and surplus >= 0.0
        and candidate.get("feasibility") != "不优先"
    )


def _direct_heat_integration_is_feasible(candidate: Dict[str, Any]) -> bool:
    """Quantified P1 direct recovery that can debit the heat-duty ledger."""
    matched = _safe_float(candidate.get("matched_kw_abs"), 0.0) or 0.0
    return (
        _direct_heat_integration_is_temperature_feasible(candidate)
        and matched > 0.0
        and candidate.get("feasibility") != "数据不足"
    )


def _evaluate_heat_pump_after_heat_integration(
    heat_pump: Dict[str, Any],
    heat_integration: Dict[str, Any],
    require_overlap: bool = True,
) -> Optional[Dict[str, Any]]:
    """Apply direct heat integration first, then recalculate the residual heat-pump duty.

    The direct exchanger may compete with the heat pump for the same condenser
    source, the same reboiler sink, both, or neither.  Source and sink duties are
    reduced before the heat-pump balance is evaluated, preventing double counting.
    """
    if not _direct_heat_integration_is_feasible(heat_integration):
        return None

    hi_source = str(heat_integration.get("hot_source_column") or "").upper().strip()
    hi_sink = str(heat_integration.get("cold_sink_column") or "").upper().strip()
    hp_source = str(heat_pump.get("source_column") or "").upper().strip()
    hp_sink = str(heat_pump.get("sink_column") or "").upper().strip()
    hi_source_key = str(heat_integration.get("hot_source_key") or f"{hi_source}:condenser").upper().strip()
    hp_source_key = str(heat_pump.get("source_key") or f"{hp_source}:condenser").upper().strip()
    hi_sink_key = str(heat_integration.get("cold_sink_key") or f"{hi_sink}:reboiler").upper().strip()
    hp_sink_key = str(heat_pump.get("sink_key") or f"{hp_sink}:reboiler").upper().strip()
    same_source = bool(hi_source_key and hp_source_key and hi_source_key == hp_source_key)
    same_sink = bool(hi_sink_key and hp_sink_key and hi_sink_key == hp_sink_key)
    if require_overlap and not (same_source or same_sink):
        return None

    direct_kw = max(0.0, _safe_float(heat_integration.get("matched_kw_abs"), 0.0) or 0.0)
    hp_source_kw = max(0.0, _safe_float(heat_pump.get("source_condenser_kw_abs"), 0.0) or 0.0)
    hp_sink_kw = max(0.0, _safe_float(heat_pump.get("sink_reboiler_kw_abs"), 0.0) or 0.0)
    cop = _safe_float(heat_pump.get("estimated_COP_heating"), None)

    residual_source_kw = max(0.0, hp_source_kw - direct_kw) if same_source else hp_source_kw
    residual_sink_kw = max(0.0, hp_sink_kw - direct_kw) if same_sink else hp_sink_kw

    hp_hot_kw = 0.0
    hp_cold_kw = 0.0
    compressor_kw = 0.0
    if cop is not None and cop > 1.0 and residual_source_kw > 0.0 and residual_sink_kw > 0.0:
        max_hot_by_source = residual_source_kw / max(1e-9, 1.0 - 1.0 / cop)
        hp_hot_kw = min(residual_sink_kw, max_hot_by_source)
        compressor_kw = hp_hot_kw / cop
        hp_cold_kw = max(0.0, hp_hot_kw - compressor_kw)

    source_overlap_fraction = direct_kw / hp_source_kw if same_source and hp_source_kw > 0 else 0.0
    sink_overlap_fraction = direct_kw / hp_sink_kw if same_sink and hp_sink_kw > 0 else 0.0
    overlap_fraction = max(0.0, min(1.0, max(source_overlap_fraction, sink_overlap_fraction)))

    raw_score = _safe_float(
        heat_pump.get("score_without_heat_integration", heat_pump.get("score")), 0.0
    ) or 0.0
    lift_c = _safe_float(heat_pump.get("temperature_lift_C"), 999.0) or 999.0
    residual_reboiler_coverage = hp_hot_kw / residual_sink_kw if residual_sink_kw > 0 else 0.0
    incremental_original_sink_coverage = hp_hot_kw / hp_sink_kw if hp_sink_kw > 0 else 0.0
    residual_source_coverage = hp_cold_kw / residual_source_kw if residual_source_kw > 0 else 0.0
    residual_score_pack = _score_heat_pump_match(
        lift_c, cop or 0.0, incremental_original_sink_coverage, residual_source_coverage,
        same_column=bool(heat_pump.get("same_column")),
    )
    residual_thermodynamic_score = _safe_float(residual_score_pack.get("score"), 0.0) or 0.0

    # A pure heat-pump option is deliberately penalized when a compressor-free
    # exchanger has already claimed the same source or sink.  The corresponding
    # hybrid candidate retains the direct-integration benefit and is scored separately.
    priority_penalty = 35.0 * overlap_fraction
    adjusted_standalone_score = max(0.0, min(raw_score, residual_thermodynamic_score) - priority_penalty)

    return {
        "same_source": same_source,
        "same_sink": same_sink,
        "heat_integration_source_key": hi_source_key,
        "heat_pump_source_key": hp_source_key,
        "heat_integration_sink_key": hi_sink_key,
        "heat_pump_sink_key": hp_sink_key,
        "direct_heat_integration_kw": direct_kw,
        "residual_source_condenser_kw": residual_source_kw,
        "residual_sink_reboiler_kw": residual_sink_kw,
        "heat_pump_heat_after_integration_kw": hp_hot_kw,
        "heat_pump_source_heat_after_integration_kw": hp_cold_kw,
        "compressor_power_after_integration_kw": compressor_kw,
        "heat_pump_reboiler_coverage_after_integration": residual_reboiler_coverage,
        "heat_pump_incremental_coverage_of_original_sink": incremental_original_sink_coverage,
        "source_overlap_fraction": source_overlap_fraction,
        "sink_overlap_fraction": sink_overlap_fraction,
        "overlap_fraction": overlap_fraction,
        "priority_penalty": priority_penalty,
        "adjusted_standalone_heat_pump_score": round(adjusted_standalone_score, 1),
        "residual_heat_pump_scoring_standard": residual_score_pack.get("scoring_standard"),
        "residual_heat_pump_score_components": residual_score_pack.get("score_components"),
        "combined_steam_saving_kw": direct_kw + hp_hot_kw,
    }


def _apply_heat_integration_priority_to_heat_pump_candidate(
    candidate: Dict[str, Any],
    heat_integration_candidates: Optional[List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Make direct integration part of every downstream heat-pump score."""
    candidate = dict(candidate)
    raw_score = _safe_float(candidate.get("score"), 0.0) or 0.0
    candidate["score_without_heat_integration"] = round(raw_score, 1)
    candidate["score_components_without_heat_integration"] = candidate.get("score_components")
    candidate["feasibility_without_heat_integration"] = candidate.get("feasibility")
    candidate["screening_priority_order"] = 3
    candidate["screening_priority_basis"] = "standalone heat pump; evaluated only after compressor-free heat integration"

    evaluations: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for hi in heat_integration_candidates or []:
        ev = _evaluate_heat_pump_after_heat_integration(candidate, hi, require_overlap=True)
        if ev is not None:
            evaluations.append((hi, ev))

    if not evaluations:
        candidate["heat_integration_priority_applied"] = False
        return candidate

    # The preceding exchanger is selected by total recovered heat, then by the
    # direct duty itself.  This represents the best heat-integration-first path
    # that shares a source or sink with the heat-pump option.
    hi, ev = max(
        evaluations,
        key=lambda pair: (
            _safe_float(pair[1].get("combined_steam_saving_kw"), 0.0) or 0.0,
            _safe_float(pair[1].get("direct_heat_integration_kw"), 0.0) or 0.0,
            _safe_float(pair[0].get("score"), 0.0) or 0.0,
        ),
    )
    candidate["heat_integration_priority_applied"] = True
    candidate["preceding_heat_integration"] = {
        "hot_source_column": hi.get("hot_source_column"),
        "hot_source_key": hi.get("hot_source_key"),
        "hot_source_type": hi.get("hot_source_type"),
        "cold_sink_column": hi.get("cold_sink_column"),
        "cold_sink_key": hi.get("cold_sink_key"),
        "matched_kw_abs": hi.get("matched_kw_abs"),
        "score": hi.get("score"),
        "same_source_as_heat_pump": ev.get("same_source"),
        "same_sink_as_heat_pump": ev.get("same_sink"),
    }
    candidate["direct_heat_integration_kw_before_heat_pump"] = round(ev["direct_heat_integration_kw"], 3)
    candidate["residual_source_condenser_kw_after_heat_integration"] = round(ev["residual_source_condenser_kw"], 3)
    candidate["residual_sink_reboiler_kw_after_heat_integration"] = round(ev["residual_sink_reboiler_kw"], 3)
    candidate["covered_reboiler_kw_after_heat_integration"] = round(ev["heat_pump_heat_after_integration_kw"], 3)
    candidate["reboiler_coverage_after_heat_integration"] = round(ev["heat_pump_reboiler_coverage_after_integration"], 4)
    candidate["incremental_reboiler_coverage_of_original_after_heat_integration"] = round(
        ev["heat_pump_incremental_coverage_of_original_sink"], 4
    )
    candidate["estimated_compressor_kw_after_heat_integration"] = round(ev["compressor_power_after_integration_kw"], 3)
    candidate["heat_integration_priority_penalty"] = round(ev["priority_penalty"], 1)
    candidate["score"] = ev["adjusted_standalone_heat_pump_score"]
    candidate["scoring_standard"] = ev.get("residual_heat_pump_scoring_standard", candidate.get("scoring_standard"))
    candidate["score_components"] = ev.get("residual_heat_pump_score_components", candidate.get("score_components"))

    exact_pair = bool(ev.get("same_source") and ev.get("same_sink"))
    if exact_pair:
        candidate["score"] = min(_safe_float(candidate.get("score"), 0.0) or 0.0, 15.0)
        candidate["feasibility"] = "不优先"
        candidate["reason"] = (
            "同一热源—热阱组合已满足直接热集成温差，压缩机方案被直接换热支配；"
            "仅在直接换热受材料、控制或设备布置限制时保留为备选。"
        )
    elif ev["heat_pump_heat_after_integration_kw"] <= 1e-9:
        candidate["feasibility"] = "不优先"
        candidate["reason"] = "热集成先行后已无剩余热源或再沸需求，纯热泵方案不再产生增量节能。"
    else:
        original_reason = str(candidate.get("reason") or "")
        candidate["reason"] = (
            f"先执行 {hi.get('hot_source_column')}→{hi.get('cold_sink_column')} 直接热集成，"
            f"再按剩余热源/热阱评价热泵。{original_reason}"
        )
        if (_safe_float(candidate.get("score"), 0.0) or 0.0) < 40.0:
            candidate["feasibility"] = "不优先"
        elif (_safe_float(candidate.get("score"), 0.0) or 0.0) < 60.0 and candidate.get("feasibility") == "优先":
            candidate["feasibility"] = "边界"
    return candidate


def analyze_global_heat_pump_candidates(
    process_data: dict,
    dt_approach_c: float = DEFAULT_DT_MIN_C,
    carnot_efficiency: float = DEFAULT_CARNOT_EFFICIENCY,
    max_lift_c: float = 80.0,
    min_cop: float = 1.5,
    min_reboiler_coverage: float = 0.05,
    pressure_ratio: float = 2.0,
    heat_integration_candidates: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Enumerate global heat pumps after compressor-free heat integration is screened first."""
    if heat_integration_candidates is None:
        # Keep this public function safe when called outside build_intensification_analysis.
        heat_integration_candidates = analyze_cross_column_heat_integration(process_data, dt_approach_c)
    columns = [c for c in (process_data.get("columns", []) or []) if isinstance(c, dict)]
    candidates: List[Dict[str, Any]] = []

    for source_col in columns:
        src_id = _column_id(source_col)
        src_role = _column_role(source_col)
        t_source = _col_top_temp_c(source_col)
        q_source_cond = _col_q_cond_kw(source_col)

        for sink_col in columns:
            sink_id = _column_id(sink_col)
            sink_role = _column_role(sink_col)
            t_sink = _col_bottom_temp_c(sink_col)
            q_sink_reb = _col_q_reb_kw(sink_col)
            same_column = src_id == sink_id

            missing = []
            if t_source is None:
                missing.append("source top temperature")
            if t_sink is None:
                missing.append("sink bottom temperature")
            if q_source_cond is None:
                missing.append("source condenser duty")
            if q_sink_reb is None:
                missing.append("sink reboiler duty")
            if missing:
                candidates.append({
                    "technology": "全局塔顶蒸汽热泵",
                    "feasibility": "数据不足",
                    "score": 0.0,
                    "score_without_heat_integration": 0.0,
                    "screening_priority_order": 3,
                    "source_column": src_id,
                    "source_key": f"{src_id}:condenser",
                    "sink_column": sink_id,
                    "sink_key": f"{sink_id}:reboiler",
                    "same_column": same_column,
                    "source_role": src_role,
                    "sink_role": sink_role,
                    "missing_data": missing,
                    "reason": "缺少 " + ", ".join(missing) + "，不能计算温升/COP/负荷覆盖。",
                })
                continue

            hot_target_c = float(t_sink) + float(dt_approach_c)
            lift_c = hot_target_c - float(t_source)
            cop = estimate_heating_cop(float(t_source), hot_target_c, carnot_efficiency)
            score_pack: Dict[str, Any] = {"scoring_standard": "unified_heat_pump_match_v1", "score_components": {}}

            if lift_c <= 0:
                feasibility = "不优先"
                score = 20.0
                reason = "热源温度已满足直接换热，必须优先进入热集成候选，不应先上压缩式热泵。"
                covered_reboiler_kw = 0.0
                compressor_kw = None
                required_source_kw = None
                reboiler_coverage = 0.0
                source_coverage = 0.0
            elif cop is None or q_source_cond <= 0 or q_sink_reb <= 0:
                feasibility = "不优先"
                score = 10.0
                reason = "温度或负荷不满足热泵筛选的基本条件。"
                covered_reboiler_kw = 0.0
                compressor_kw = None
                required_source_kw = None
                reboiler_coverage = 0.0
                source_coverage = 0.0
            else:
                max_hot_by_source = q_source_cond / max(1e-9, (1.0 - 1.0 / cop))
                covered_reboiler_kw = min(q_sink_reb, max_hot_by_source)
                compressor_kw = covered_reboiler_kw / cop
                required_source_kw = max(0.0, covered_reboiler_kw - compressor_kw)
                reboiler_coverage = covered_reboiler_kw / q_sink_reb if q_sink_reb > 0 else 0.0
                source_coverage = required_source_kw / q_source_cond if q_source_cond > 0 else 0.0

                score_pack = _score_heat_pump_match(
                    lift_c, cop, reboiler_coverage, source_coverage, same_column,
                    max_lift_c=max_lift_c, min_cop=min_cop,
                )
                score = score_pack["score"]

                if lift_c > max_lift_c:
                    feasibility = "不优先"
                    reason = f"温升 {lift_c:.1f} °C 超过筛选上限 {max_lift_c:.1f} °C，COP 和压缩比风险偏大。"
                elif cop < min_cop:
                    feasibility = "不优先"
                    reason = f"估算 COP={cop:.2f} 低于筛选下限 {min_cop:.2f}。"
                elif reboiler_coverage < min_reboiler_coverage:
                    feasibility = "不优先"
                    reason = "可覆盖的目标再沸负荷太小，工程价值不足。"
                elif lift_c <= 25 and cop >= 3.0 and reboiler_coverage >= 0.5:
                    feasibility = "优先"
                    reason = "温升小、COP 较高且再沸覆盖率较高；仅在热集成先行后仍有剩余热源/热阱时优先验证。"
                elif lift_c <= 45 and cop >= 2.0 and reboiler_coverage >= 0.25:
                    feasibility = "可选"
                    reason = "温升和负荷覆盖处于可验证区间，需要在热集成残余负荷基础上进一步确认。"
                else:
                    feasibility = "边界"
                    reason = "热泵可形成温度级别，但 COP、负荷覆盖或压缩比存在边界风险。"

                if score >= 80 and feasibility != "不优先":
                    feasibility = "优先"
                elif score < 40:
                    feasibility = "不优先"

            candidate: Dict[str, Any] = {
                "technology": "全局塔顶蒸汽热泵",
                "feasibility": feasibility,
                "score": round(float(score), 1),
                "scoring_standard": score_pack.get("scoring_standard"),
                "score_components": score_pack.get("score_components"),
                "source_column": src_id,
                "source_key": f"{src_id}:condenser",
                "source_type": "column_condenser",
                "sink_column": sink_id,
                "sink_key": f"{sink_id}:reboiler",
                "same_column": same_column,
                "source_role": src_role,
                "sink_role": sink_role,
                "source_top_temperature_C": round(float(t_source), 3),
                "sink_bottom_temperature_C": round(float(t_sink), 3),
                "hot_target_temperature_C": round(float(hot_target_c), 3),
                "dt_approach_C": dt_approach_c,
                "temperature_lift_C": round(float(lift_c), 3),
                "estimated_COP_heating": round(float(cop), 3) if cop is not None else None,
                "source_condenser_kw_abs": q_source_cond,
                "sink_reboiler_kw_abs": q_sink_reb,
                "covered_reboiler_kw": round(float(covered_reboiler_kw), 3),
                "reboiler_coverage": round(float(reboiler_coverage), 4),
                "required_source_heat_kw": round(float(required_source_kw), 3) if required_source_kw is not None else None,
                "source_condenser_coverage": round(float(source_coverage), 4),
                "estimated_compressor_kw": round(float(compressor_kw), 3) if compressor_kw is not None else None,
                "residual_reboiler_kw": round(max(0.0, float(q_sink_reb) - float(covered_reboiler_kw)), 3) if q_sink_reb is not None else None,
                "pressure_ratio_screening": pressure_ratio,
                "reason": reason,
                "aspen_validation_actions": [
                    "先核查所有无压缩机直接热集成候选，并按 physical source_key/sink_key 锁定已占用的热源与再沸热阱。",
                    f"若仍有剩余负荷，在复制的 Aspen 文件中创建 {src_id} stage 2 气相 pseudo stream 和 {sink_id} stage N-1 液相 pseudo stream。",
                    f"搭建 {src_id} top vapor -> Compressor -> HREB({sink_id} bottom+DT) -> HCOND({src_id} top)。",
                    "读取 Flash2/HREB/HCOND/Compressor，检查热源、热阱与冷却负荷均未和直接热集成重复计数。",
                    "比较直接热集成、联合方案和纯热泵的 TAC/CO2。",
                ],
                "risks": [
                    "热泵评分已经扣除优先直接热集成占用的热源/热阱；不能再用原始完整负荷重复计算收益。",
                    "COP 仍为筛选估算，压缩比、排气温度和两相夹带需 Aspen 严格验证。",
                    "联合方案需要统一热量台账，防止同一物理热源同时供给换热器和热泵蒸发端。",
                ],
            }
            candidate["aspen_validation_payload"] = make_global_heat_pump_validation_payload(
                process_data, candidate, pressure_ratio=pressure_ratio, reboiler_approach_c=dt_approach_c,
            )
            candidate = _apply_heat_integration_priority_to_heat_pump_candidate(
                candidate, heat_integration_candidates
            )
            _priority_metadata(candidate, "heat_pump", priority_eligible=True)
            candidates.append(candidate)

    candidates.sort(
        key=lambda x: _safe_float(x.get("technical_score", x.get("score")), 0.0) or 0.0,
        reverse=True,
    )
    return candidates

def analyze_cross_column_heat_integration(
    process_data: dict,
    dt_min_c: float = DEFAULT_DT_MIN_C,
) -> List[Dict[str, Any]]:
    """Screen all quantified hot streams against column reboilers with one score.

    Hot-source pool:
    1) column condenser/top vapor latent heat;
    2) hot solvent bottom product from a solvent-recovery column.

    Both source types use the identical temperature/load/simplicity score.
    """
    columns = [c for c in (process_data.get("columns", []) or []) if isinstance(c, dict)]
    sources = _direct_heat_sources(process_data)
    candidates: List[Dict[str, Any]] = []

    for source in sources:
        hot_id = str(source.get("source_column") or "UNKNOWN").upper().strip()
        hot_t = _safe_float(source.get("temperature_C"), None)
        if hot_t is None:
            continue

        for cold_col in columns:
            cold_id = _column_id(cold_col)
            # This intelligent agent handles cross-column recovery.  Recycling a
            # column's own bottom product to its own reboiler would be circular.
            if hot_id == cold_id:
                continue
            cold_t = _col_bottom_temp_c(cold_col)
            q_cold = _col_q_reb_kw(cold_col)
            if cold_t is None or q_cold is None or q_cold <= 0:
                continue

            source_type = str(source.get("source_type") or "column_condenser")
            q_hot, heat_basis = _source_available_heat_for_sink(source, cold_t, dt_min_c)
            scored = _score_direct_heat_exchange(
                hot_temperature_c=hot_t,
                cold_temperature_c=cold_t,
                available_kw=q_hot,
                required_kw=q_cold,
                dt_min_c=dt_min_c,
                exchanger_count=1,
                source_type=source_type,
            )

            is_solvent_bottom = source_type == "solvent_recovery_bottom_stream"
            technology = (
                "溶剂回收塔底溶剂物流—再沸器直接热集成"
                if is_solvent_bottom else "跨塔冷凝器—再沸器直接热集成"
            )
            stream_id = source.get("existing_hot_stream_id")
            hot_source_desc = (
                f"{hot_id} bottom solvent stream {stream_id}" if stream_id
                else f"{hot_id} bottom liquid pseudo stream"
            ) if is_solvent_bottom else f"{hot_id} condenser/top-vapor pseudo stream"

            if is_solvent_bottom:
                actions = [
                    f"用 {cold_id} 第 N-1 块塔板液相 pseudo stream 建 Flash2，以 abs(Flash2 duty) 表示再沸需求。",
                    (
                        f"将 Aspen 现有塔底溶剂物流 {stream_id} 接入 HINT"
                        if stream_id else
                        f"由 {hot_id} 第 N-1 块塔板液相 pseudo stream 建立塔底溶剂热源并接入 HINT"
                    ) + f"，出口温度设为 {cold_id} 再沸液相参考温度 + {dt_min_c:g} °C。",
                    "读取 abs(HINT duty) 作为塔底溶剂物流在 7 °C 最小温差下的实际可回收显热。",
                    "Python 后处理 integrated_heat=min(abs(Flash2 duty), abs(HINT duty), screening limits)，再按统一评分函数回填覆盖率与分数。",
                    "检查溶剂回收塔底产品的允许最低送出温度、黏度、结晶/凝固和下游操作温度约束。",
                ]
            else:
                actions = [
                    f"用 {cold_id} 第 N-1 块塔板液相 pseudo stream 建 Flash2，以 abs(Flash2 duty) 表示再沸需求。",
                    f"用 {hot_id} 第 2 块塔板气相 pseudo stream 建 HINT，出口温度设为 {cold_id} 再沸液相参考温度 + {dt_min_c:g} °C。",
                    "不加压缩机；读取 abs(HINT duty) 作为可直接利用的冷凝热。",
                    "Python 后处理 integrated_heat=min(abs(Flash2 duty), abs(HINT duty))，并计算剩余外部再沸负荷。",
                ]

            candidate: Dict[str, Any] = {
                "hot_source_column": hot_id,
                "hot_source_key": source.get("source_key"),
                "hot_source_type": source_type,
                "cold_sink_column": cold_id,
                "cold_sink_key": f"{cold_id}:reboiler",
                "technology": technology,
                "source_mode": source.get("source_mode"),
                "existing_hot_stream_id": stream_id,
                "source_hot_stage": source.get("source_hot_stage"),
                "source_hot_phase": source.get("source_hot_phase"),
                "feasibility": scored.get("feasibility"),
                "score": scored.get("score"),
                "hot_source": hot_source_desc,
                "cold_sink": f"{cold_id} reboiler surrogate",
                "hot_temperature_C": hot_t,
                "cold_temperature_C": cold_t,
                "dt_min_C": float(dt_min_c),
                "temperature_driving_force_C": scored.get("temperature_driving_force_C"),
                "surplus_after_dtmin_C": scored.get("surplus_after_dtmin_C"),
                "available_kw_abs": q_hot,
                "available_heat_basis": heat_basis,
                "required_kw_abs": q_cold,
                "matched_kw_abs": scored.get("matched_kw_abs"),
                "gross_hot_utility_saving_kw": scored.get("gross_hot_utility_saving_kw"),
                "specific_recoverable_heat_per_unit_CP_kw_per_kw_per_C": scored.get("specific_recoverable_heat_per_unit_CP_kw_per_kw_per_C"),
                "required_CP_flow_for_full_sink_coverage_kw_per_C": scored.get("required_CP_flow_for_full_sink_coverage_kw_per_C"),
                "cold_duty_coverage": scored.get("cold_duty_coverage"),
                "hot_duty_coverage": scored.get("hot_duty_coverage"),
                "scoring_standard": scored.get("scoring_standard"),
                "calculation_standard": scored.get("calculation_standard"),
                "score_components": scored.get("score_components"),
                "reason": scored.get("reason"),
                "aspen_validation_actions": actions,
            }
            candidate["aspen_validation_payload"] = make_cross_column_heat_integration_validation_payload(
                process_data, candidate, dt_min_c=dt_min_c
            )
            _priority_metadata(
                candidate,
                "direct_heat_integration",
                priority_eligible=_direct_heat_integration_is_temperature_feasible(candidate),
            )
            candidates.append(candidate)

    candidates.sort(
        key=lambda x: (
            0 if _direct_heat_integration_is_temperature_feasible(x) else 1,
            -(_safe_float(x.get("technical_score", x.get("score")), 0.0) or 0.0),
        )
    )
    return candidates


def analyze_heat_integration_heat_pump_hybrids(
    process_data: dict,
    heat_integration_candidates: List[Dict[str, Any]],
    heat_pump_candidates: List[Dict[str, Any]],
    max_candidates: int = 40,
) -> List[Dict[str, Any]]:
    """Enumerate direct-HI-first + heat-pump portfolios for same/different reboilers.

    Direct heat integration is always applied first.  The heat pump can then:
    1) supply the residual duty of the same reboiler; or
    2) supply a different reboiler while the direct exchanger serves another sink.

    Shared condenser sources and shared reboiler sinks are explicitly debited so
    the combined score cannot count the same heat twice.
    """
    hybrids: List[Dict[str, Any]] = []
    feasible_hi = [x for x in heat_integration_candidates or [] if _direct_heat_integration_is_feasible(x)]
    operability_constraints = _heat_pump_operability_constraints(process_data)
    objective = process_data.get("objective") or {}
    total_process_reboiler_kw = _safe_float(objective.get("total_reboiler_kw_abs"), None)
    if total_process_reboiler_kw is None or total_process_reboiler_kw <= 0:
        total_process_reboiler_kw = sum((_col_q_reb_kw(c) or 0.0) for c in (process_data.get("columns") or []))

    for hi in feasible_hi:
        for hp in heat_pump_candidates or []:
            cop = _safe_float(hp.get("estimated_COP_heating"), None)
            lift_c = _safe_float(hp.get("temperature_lift_C"), None)
            if cop is None or cop < 1.5 or lift_c is None or lift_c <= 0.0 or lift_c > 80.0:
                continue
            if hp.get("feasibility") == "数据不足":
                continue

            # The exact same source-sink pair is already thermodynamically served
            # by a direct exchanger and therefore should not be hybridized with a compressor.
            hi_source_key = str(hi.get("hot_source_key") or f"{hi.get('hot_source_column')}:condenser").upper().strip()
            hp_source_key = str(hp.get("source_key") or f"{hp.get('source_column')}:condenser").upper().strip()
            hi_sink_key = str(hi.get("cold_sink_key") or f"{hi.get('cold_sink_column')}:reboiler").upper().strip()
            hp_sink_key = str(hp.get("sink_key") or f"{hp.get('sink_column')}:reboiler").upper().strip()
            exact_same_pair = hi_source_key == hp_source_key and hi_sink_key == hp_sink_key
            if exact_same_pair:
                continue

            ev = _evaluate_heat_pump_after_heat_integration(hp, hi, require_overlap=False)
            if ev is None or (_safe_float(ev.get("heat_pump_heat_after_integration_kw"), 0.0) or 0.0) <= 1e-9:
                continue

            hi_source = str(hi.get("hot_source_column") or "").upper().strip()
            hi_sink = str(hi.get("cold_sink_column") or "").upper().strip()
            hp_source = str(hp.get("source_column") or "").upper().strip()
            hp_sink = str(hp.get("sink_column") or "").upper().strip()
            same_reboiler = hi_sink_key == hp_sink_key
            same_source = hi_source_key == hp_source_key

            direct_kw = _safe_float(ev.get("direct_heat_integration_kw"), 0.0) or 0.0
            hp_hot_kw = _safe_float(ev.get("heat_pump_heat_after_integration_kw"), 0.0) or 0.0
            hp_cold_kw = _safe_float(ev.get("heat_pump_source_heat_after_integration_kw"), 0.0) or 0.0
            comp_kw = _safe_float(ev.get("compressor_power_after_integration_kw"), 0.0) or 0.0
            total_steam_saved_kw = direct_kw + hp_hot_kw

            hi_sink_demand = _safe_float(hi.get("required_kw_abs"), 0.0) or 0.0
            hp_sink_demand = _safe_float(hp.get("sink_reboiler_kw_abs"), 0.0) or 0.0
            total_reboiler_basis_kw = hp_sink_demand if same_reboiler else hi_sink_demand + hp_sink_demand
            total_reboiler_coverage = (
                total_steam_saved_kw / total_reboiler_basis_kw if total_reboiler_basis_kw > 0 else 0.0
            )

            hi_source_available = _safe_float(hi.get("available_kw_abs"), 0.0) or 0.0
            hp_source_available = _safe_float(hp.get("source_condenser_kw_abs"), 0.0) or 0.0
            total_source_basis_kw = hp_source_available if same_source else hi_source_available + hp_source_available
            total_cooling_recovered_kw = direct_kw + hp_cold_kw
            total_source_coverage = (
                total_cooling_recovered_kw / total_source_basis_kw if total_source_basis_kw > 0 else 0.0
            )
            direct_share = direct_kw / total_steam_saved_kw if total_steam_saved_kw > 0 else 0.0

            # P2 is a portfolio, not simply "P1 Top1 + P3 Top1".  If direct
            # integration nearly fills the same reboiler, the heat pump must be
            # sized to the residual duty and, for process-vapor MVR, only a
            # fraction of the source vapor may enter the compressor.
            residual_source_kw = _safe_float(ev.get("residual_source_condenser_kw"), 0.0) or 0.0
            residual_sink_kw = _safe_float(ev.get("residual_sink_reboiler_kw"), 0.0) or 0.0
            residual_sink_fraction = residual_sink_kw / hp_sink_demand if hp_sink_demand > 0 else 0.0
            hp_source_branch_fraction = hp_cold_kw / residual_source_kw if residual_source_kw > 0 else 0.0
            hp_source_branch_fraction = max(0.0, min(1.0, hp_source_branch_fraction))
            hp_incremental_sink_fraction = hp_hot_kw / hp_sink_demand if hp_sink_demand > 0 else 0.0
            hp_sizing_mode = (
                "同一再沸器残余负荷定容 + 顶汽分流/旁路"
                if same_reboiler and hp_source_branch_fraction < 0.999
                else "同一再沸器残余负荷定容"
                if same_reboiler
                else "不同再沸器独立定容"
            )
            min_load = _evaluate_hybrid_minimum_load(
                hp_hot_kw=hp_hot_kw,
                residual_sink_fraction=residual_sink_fraction,
                source_branch_fraction=hp_source_branch_fraction,
                constraints=operability_constraints,
            )

            # P2 的类内技术分必须表达“在该 P3 热泵基础上增加直接热回收”的增益，
            # 不能再用一套与 P3 完全不同的绝对加权和，否则会出现联合节能更多、
            # 数值却低于其纯热泵子方案的假象。这里保留一套 portfolio diagnostics，
            # 但最终 P2 score 以 constituent P3 score 为基线，并将剩余分数空间按
            # 实际热集成贡献、全流程增量节汽、电耗替代和拓扑独立性进行提升。
            system_steam_saving_fraction = (
                total_steam_saved_kw / total_process_reboiler_kw
                if total_process_reboiler_kw and total_process_reboiler_kw > 0 else 0.0
            )
            system_saving_score = 30.0 * min(1.0, system_steam_saving_fraction)
            cooling_score = 15.0 * min(1.0, total_source_coverage)
            direct_priority_score = 10.0 * min(1.0, direct_share)
            cop_score = 15.0 * max(0.0, min(1.0, (cop - 1.0) / 4.0))
            lift_score = 10.0 * max(0.0, min(1.0, 1.0 - lift_c / 80.0))
            incremental_scale_score = 15.0 * math.sqrt(max(0.0, min(1.0, hp_incremental_sink_fraction)))
            topology_score = 5.0 if same_reboiler and hp_source_branch_fraction >= 0.999 else 3.0
            portfolio_performance_score = round(
                min(100.0, system_saving_score + cooling_score + direct_priority_score + cop_score
                    + lift_score + incremental_scale_score + topology_score),
                1,
            )

            hp_constituent_score = max(0.0, min(100.0, _safe_float(
                hp.get("score_without_heat_integration", hp.get("technical_score", hp.get("score"))), 0.0
            ) or 0.0))
            hp_standalone_hot_kw = max(0.0, _safe_float(hp.get("covered_reboiler_kw"), 0.0) or 0.0)
            hp_standalone_comp_kw = max(0.0, _safe_float(hp.get("estimated_compressor_kw"), 0.0) or 0.0)
            direct_system_fraction = (
                direct_kw / total_process_reboiler_kw
                if total_process_reboiler_kw and total_process_reboiler_kw > 0 else 0.0
            )
            incremental_steam_kw = max(0.0, total_steam_saved_kw - hp_standalone_hot_kw)
            incremental_steam_fraction = (
                incremental_steam_kw / total_process_reboiler_kw
                if total_process_reboiler_kw and total_process_reboiler_kw > 0 else 0.0
            )
            avoided_compressor_kw_equiv = min(direct_kw, hp_standalone_hot_kw) / max(cop, 1e-9)
            avoided_power_fraction = (
                avoided_compressor_kw_equiv / hp_standalone_comp_kw
                if hp_standalone_comp_kw > 1e-9 else 0.0
            )
            topology_quality = 1.0 if (not same_source and not same_reboiler) else 0.70
            hybrid_gain_fraction = min(0.65, max(0.05,
                0.08
                + 0.22 * min(1.0, direct_system_fraction / 0.25)
                + 0.18 * min(1.0, incremental_steam_fraction / 0.25)
                + 0.10 * min(1.0, avoided_power_fraction)
                + 0.07 * topology_quality
            ))
            anchored_score = hp_constituent_score + (100.0 - hp_constituent_score) * hybrid_gain_fraction
            # 只要 Q_HI>0 且联合结构没有违反最小负荷约束，P2 就不能低于其 P3 子方案；
            # 若设备约束不通过，则仍由 feasibility 降级，而不是伪装为高可操作性方案。
            score = round(min(100.0, max(portfolio_performance_score, anchored_score)), 1)

            if total_reboiler_coverage >= 0.50 and cop >= 2.0 and direct_share >= 0.15:
                feasibility = "优先"
            elif total_reboiler_coverage >= 0.25 and cop >= 1.7:
                feasibility = "可选"
            else:
                feasibility = "边界"
            if score < 40.0 or min_load.get("minimum_load_passed") is False:
                feasibility = "不优先"

            reboiler_mode = "同一再沸器联合供热" if same_reboiler else "不同再沸器协同供热"
            source_mode = "共享冷凝热源" if same_source else "独立冷凝热源"
            hybrid = {
                "technology": "直接热集成 + 热泵联合方案",
                "category": "hybrid_heat_integration_heat_pump",
                "feasibility": feasibility,
                "score": score,
                "screening_priority_order": 2,
                "screening_priority_basis": "direct heat integration first; heat pump only covers residual/other reboiler duty",
                "heat_integration_source_column": hi_source,
                "heat_integration_source_key": hi_source_key,
                "heat_integration_source_type": hi.get("hot_source_type"),
                "heat_integration_sink_column": hi_sink,
                "heat_integration_sink_key": hi_sink_key,
                "heat_pump_source_column": hp_source,
                "heat_pump_source_key": hp_source_key,
                "heat_pump_sink_column": hp_sink,
                "heat_pump_sink_key": hp_sink_key,
                "same_reboiler": same_reboiler,
                "same_condenser_source": same_source,
                "reboiler_mode": reboiler_mode,
                "source_mode": source_mode,
                "direct_heat_integration_kw": round(direct_kw, 3),
                "heat_pump_heat_after_integration_kw": round(hp_hot_kw, 3),
                "heat_pump_source_heat_after_integration_kw": round(hp_cold_kw, 3),
                "estimated_compressor_kw_after_integration": round(comp_kw, 3),
                "total_steam_saving_kw": round(total_steam_saved_kw, 3),
                "total_reboiler_basis_kw": round(total_reboiler_basis_kw, 3),
                "total_reboiler_coverage": round(total_reboiler_coverage, 4),
                "system_steam_saving_fraction": round(system_steam_saving_fraction, 4),
                "total_cooling_recovered_kw": round(total_cooling_recovered_kw, 3),
                "total_source_condenser_basis_kw": round(total_source_basis_kw, 3),
                "total_source_coverage": round(total_source_coverage, 4),
                "direct_heat_share": round(direct_share, 4),
                "heat_pump_temperature_lift_C": hp.get("temperature_lift_C"),
                "estimated_COP_heating": hp.get("estimated_COP_heating"),
                "residual_hp_source_condenser_kw": round(ev.get("residual_source_condenser_kw", 0.0), 3),
                "residual_hp_sink_reboiler_kw": round(ev.get("residual_sink_reboiler_kw", 0.0), 3),
                "residual_sink_fraction": round(residual_sink_fraction, 4),
                "heat_pump_incremental_sink_fraction": round(hp_incremental_sink_fraction, 4),
                "heat_pump_source_branch_fraction": round(hp_source_branch_fraction, 4),
                "heat_pump_sizing_mode": hp_sizing_mode,
                "minimum_load_check": min_load,
                "strict_thermodynamic_validation_ready": min_load.get("minimum_load_passed") is not False,
                "mechanical_operability_confirmed": min_load.get("minimum_load_passed"),
                "score_components": {
                    "constituent_P3_score": round(hp_constituent_score, 3),
                    "hybrid_gain_fraction_of_remaining_headroom": round(hybrid_gain_fraction, 4),
                    "anchored_P2_score": round(anchored_score, 3),
                    "portfolio_performance_diagnostic": round(portfolio_performance_score, 3),
                    "overall_process_steam_saving_diagnostic": round(system_saving_score, 3),
                    "total_source_utilization_diagnostic": round(cooling_score, 3),
                    "direct_heat_share_diagnostic": round(direct_priority_score, 3),
                    "cop_quality_diagnostic": round(cop_score, 3),
                    "minimum_temperature_lift_diagnostic": round(lift_score, 3),
                    "incremental_heat_pump_scale_diagnostic": round(incremental_scale_score, 3),
                    "configuration_simplicity_diagnostic": round(topology_score, 3),
                },
                "scoring_standard": "quantified_hybrid_score_anchored_to_constituent_P3_v2",
                "score_semantics_detail": (
                    "P2 score starts from its constituent P3 technical score and uses verified direct-heat contribution "
                    "to convert part of the remaining headroom to 100; portfolio diagnostics remain available separately."
                ),
                "heat_integration_candidate": {
                    "hot_source_column": hi_source,
                    "hot_source_key": hi_source_key,
                    "hot_source_type": hi.get("hot_source_type"),
                    "cold_sink_column": hi_sink,
                    "cold_sink_key": hi_sink_key,
                    "matched_kw_abs": hi.get("matched_kw_abs"),
                    "score": hi.get("score"),
                },
                "heat_pump_candidate": {
                    "source_column": hp_source,
                    "sink_column": hp_sink,
                    "score_without_heat_integration": hp.get("score_without_heat_integration", hp.get("score")),
                    "temperature_lift_C": hp.get("temperature_lift_C"),
                    "estimated_COP_heating": hp.get("estimated_COP_heating"),
                },
                "reason": (
                    f"先由 {hi_source} 冷凝热直接供给 {hi_sink} 再沸器，再由 {hp_source} 顶汽热泵供给 "
                    f"{hp_sink} 的剩余/独立再沸负荷；模式为{reboiler_mode}、{source_mode}。"
                ),
                "aspen_validation_actions": [
                    f"步骤 1：先建立并验证 {hi_source} → {hi_sink} 直接热集成，读取 integrated_heat。",
                    "步骤 2：从共享热源冷凝负荷和/或共享再沸需求中扣除 integrated_heat，形成残余热量台账。",
                    f"步骤 3：热泵供热目标设为 Q_HP,target={hp_hot_kw:.3f} kW，而不是原始完整再沸负荷。",
                    f"步骤 4：若采用顶汽 MVR，仅将约 {hp_source_branch_fraction:.4f} 的可用源汽送入压缩机，其余旁路至常规冷凝器；闭式热泵则按该负荷调节工质循环量。",
                    "步骤 5：若同一再沸器供热，约束 Q_HI + Q_HP ≤ Q_reboiler；若不同再沸器，分别核算两座塔的剩余外部蒸汽。",
                    "步骤 6：检查压缩机最小稳定负荷/最小分流比；未提供设备约束时只能完成热力学严格验证，不能声称机械可操作性已确认。",
                    "步骤 7：统一计算蒸汽削减、冷却削减、电耗、设备投资和 TAC/CO2，禁止重复计入同一冷凝热。",
                ],
            }
            _priority_metadata(hybrid, "hybrid", priority_eligible=True)
            hybrids.append(hybrid)

    hybrids.sort(
        key=lambda x: (
            _safe_float(x.get("technical_score", x.get("score")), 0.0) or 0.0,
            _safe_float(x.get("total_steam_saving_kw"), 0.0) or 0.0,
        ),
        reverse=True,
    )
    return hybrids[:max_candidates]


def analyze_provisional_heat_integration_heat_pump_hybrids(
    process_data: dict,
    pending_heat_integration_candidates: List[Dict[str, Any]],
    heat_pump_candidates: List[Dict[str, Any]],
    max_candidates: int = 40,
) -> List[Dict[str, Any]]:
    """Create P2 structural recommendations before the P1 HINT duty is known.

    P2 is a screening tier, not a strict simulation result.  A temperature-feasible
    P1 option may therefore be paired with a thermodynamically feasible heat pump
    before ``integrated_heat`` is available.  The routine never invents the direct
    duty or the residual heat-pump size:

    - different source + different sink: the standalone heat-pump sizing remains a
      valid preliminary value, while the direct duty stays pending;
    - shared sink: Q_HP must be recalculated from Q_reboiler - Q_HI;
    - shared source: the heat-pump source capacity must be recalculated after Q_HI;
    - exact same source/sink pair is excluded because a compressor is redundant when
      that pair already satisfies direct-exchange temperature feasibility.

    The provisional score is comparable only among provisional P2 candidates.  It
    combines the known P3 match quality, P1 temperature-grade quality, physical
    independence of the two matches, and the potential importance of the P1 sink.
    """
    hybrids: List[Dict[str, Any]] = []
    pending_hi = [
        x for x in (pending_heat_integration_candidates or [])
        if _direct_heat_integration_is_temperature_feasible(x)
        and not _direct_heat_integration_is_feasible(x)
    ]
    objective = process_data.get("objective") or {}
    total_process_reboiler_kw = _safe_float(objective.get("total_reboiler_kw_abs"), None)
    if total_process_reboiler_kw is None or total_process_reboiler_kw <= 0:
        total_process_reboiler_kw = sum(
            (_col_q_reb_kw(c) or 0.0) for c in (process_data.get("columns") or [])
        )

    for hi in pending_hi:
        hi_source = str(hi.get("hot_source_column") or "").upper().strip()
        hi_sink = str(hi.get("cold_sink_column") or "").upper().strip()
        hi_source_key = str(
            hi.get("hot_source_key") or f"{hi_source}:condenser"
        ).upper().strip()
        hi_sink_key = str(
            hi.get("cold_sink_key") or f"{hi_sink}:reboiler"
        ).upper().strip()
        margin = max(0.0, _safe_float(hi.get("surplus_after_dtmin_C"), 999.0) or 999.0)
        hi_temperature_quality = math.exp(-margin / 35.0)
        hi_sink_demand = max(0.0, _safe_float(hi.get("required_kw_abs"), 0.0) or 0.0)
        hi_sink_potential_fraction = (
            hi_sink_demand / total_process_reboiler_kw
            if total_process_reboiler_kw and total_process_reboiler_kw > 0 else 0.0
        )

        for hp in heat_pump_candidates or []:
            cop = _safe_float(hp.get("estimated_COP_heating"), None)
            lift_c = _safe_float(hp.get("temperature_lift_C"), None)
            if cop is None or cop < 1.5 or lift_c is None or lift_c <= 0.0 or lift_c > 80.0:
                continue
            if hp.get("feasibility") == "数据不足":
                continue

            hp_source = str(hp.get("source_column") or "").upper().strip()
            hp_sink = str(hp.get("sink_column") or "").upper().strip()
            hp_source_key = str(
                hp.get("source_key") or f"{hp_source}:condenser"
            ).upper().strip()
            hp_sink_key = str(
                hp.get("sink_key") or f"{hp_sink}:reboiler"
            ).upper().strip()
            same_source = hi_source_key == hp_source_key
            same_sink = hi_sink_key == hp_sink_key
            if same_source and same_sink:
                continue

            resource_independent = not same_source and not same_sink
            if resource_independent:
                topology_quality = 1.0
                condition = (
                    "P1 与 HP 使用不同物理热源并服务不同再沸器；"
                    "HINT 前可保留热泵独立定容值，P1 热量仍待定量。"
                )
            elif same_sink and not same_source:
                topology_quality = 0.60
                condition = (
                    "两者共享同一再沸热阱；HINT 后必须按 "
                    "Q_HP=min(Q_reboiler-Q_HI, source limit) 重新定容。"
                )
            elif same_source and not same_sink:
                topology_quality = 0.50
                condition = (
                    "两者共享同一物理热源；HINT 后必须先扣除 Q_HI，"
                    "再用剩余热源计算热泵供热上限。"
                )
            else:
                topology_quality = 0.40
                condition = "存在共享资源，严格负荷必须在 HINT 后重算。"

            hp_score = max(0.0, min(100.0, _safe_float(
                hp.get("score_without_heat_integration", hp.get("technical_score", hp.get("score"))),
                0.0,
            ) or 0.0))
            # HINT 前不能虚构 Q_HI，但 P2 已经包含一个温度可行、无需压缩机的 P1 结构。
            # 因此条件 P2 分数以其 P3 子方案为基线，并将剩余 100-score 空间按
            # P1 温度品位、资源独立性和潜在热阱重要性转换为联合增益。
            # 这样只用于 P2 类内排序，不是跨技术全局分，也不会声称未知的节能量已定量。
            sink_potential_quality = max(0.0, min(1.0, hi_sink_potential_fraction / 0.50))
            hybrid_gain_fraction = min(0.60, max(0.05,
                0.10
                + 0.15 * max(0.0, min(1.0, hi_temperature_quality))
                + 0.20 * topology_quality
                + 0.15 * sink_potential_quality
            ))
            score = round(
                min(100.0, hp_score + (100.0 - hp_score) * hybrid_gain_fraction),
                1,
            )
            hp_quality_component = hp_score
            hi_temperature_component = (100.0 - hp_score) * 0.15 * max(0.0, min(1.0, hi_temperature_quality))
            topology_component = (100.0 - hp_score) * 0.20 * topology_quality
            hi_sink_potential_component = (100.0 - hp_score) * 0.15 * sink_potential_quality

            if resource_independent and score >= 75.0:
                feasibility = "条件优先"
            elif score >= 60.0:
                feasibility = "条件可选"
            else:
                feasibility = "条件边界"

            hp_hot_upper = max(0.0, _safe_float(hp.get("covered_reboiler_kw"), 0.0) or 0.0)
            hp_cold_upper = max(0.0, _safe_float(hp.get("required_source_heat_kw"), 0.0) or 0.0)
            hp_power_upper = max(0.0, _safe_float(hp.get("estimated_compressor_kw"), 0.0) or 0.0)
            hp_target_preliminary = hp_hot_upper if resource_independent else None
            hp_power_preliminary = hp_power_upper if resource_independent else None

            reboiler_mode = "同一再沸器联合供热" if same_sink else "不同再沸器协同供热"
            source_mode = "共享物理热源" if same_source else "独立物理热源"
            hybrid: Dict[str, Any] = {
                "technology": "直接热集成 + 热泵联合方案（HINT 前条件推荐）",
                "category": "hybrid_heat_integration_heat_pump",
                "feasibility": feasibility,
                "score": score,
                "provisional_hybrid": True,
                "quantification_status": "pending_P1_HINT_duty",
                "screening_priority_order": 2,
                "screening_priority_basis": (
                    "P2 structural screening before HINT; exact direct duty, residual ledger and final HP size are not invented"
                ),
                "heat_integration_source_column": hi_source,
                "heat_integration_source_key": hi_source_key,
                "heat_integration_source_type": hi.get("hot_source_type"),
                "heat_integration_sink_column": hi_sink,
                "heat_integration_sink_key": hi_sink_key,
                "heat_pump_source_column": hp_source,
                "heat_pump_source_key": hp_source_key,
                "heat_pump_sink_column": hp_sink,
                "heat_pump_sink_key": hp_sink_key,
                "same_reboiler": same_sink,
                "same_condenser_source": same_source,
                "resource_independent": resource_independent,
                "reboiler_mode": reboiler_mode,
                "source_mode": source_mode,
                "direct_heat_integration_kw": None,
                "heat_pump_heat_after_integration_kw": hp_target_preliminary,
                "heat_pump_source_heat_after_integration_kw": (
                    hp_cold_upper if resource_independent else None
                ),
                "estimated_compressor_kw_after_integration": hp_power_preliminary,
                "heat_pump_heat_upper_bound_kw": round(hp_hot_upper, 3),
                "heat_pump_source_heat_upper_bound_kw": round(hp_cold_upper, 3),
                "compressor_power_upper_bound_kw": round(hp_power_upper, 3),
                "total_steam_saving_kw": None,
                "total_reboiler_coverage": None,
                "heat_pump_temperature_lift_C": hp.get("temperature_lift_C"),
                "estimated_COP_heating": hp.get("estimated_COP_heating"),
                "strict_thermodynamic_validation_ready": False,
                "mechanical_operability_confirmed": None,
                "conditional_sizing_rule": condition,
                "score_components": {
                    "constituent_P3_score": round(hp_quality_component, 3),
                    "hybrid_gain_fraction_of_remaining_headroom": round(hybrid_gain_fraction, 4),
                    "P1_temperature_grade_uplift": round(hi_temperature_component, 3),
                    "resource_independence_uplift": round(topology_component, 3),
                    "P1_sink_potential_uplift": round(hi_sink_potential_component, 3),
                },
                "scoring_standard": "provisional_hybrid_score_anchored_to_constituent_P3_v2",
                "score_semantics_detail": (
                    "Pending P2 score is anchored above its constituent P3 score because a temperature-feasible compressor-free P1 is added; "
                    "the uplift ranks structures only and is replaced by actual HINT/HP results after execution."
                ),
                "heat_integration_candidate": {
                    "hot_source_column": hi_source,
                    "hot_source_key": hi_source_key,
                    "hot_source_type": hi.get("hot_source_type"),
                    "cold_sink_column": hi_sink,
                    "cold_sink_key": hi_sink_key,
                    "surplus_after_dtmin_C": hi.get("surplus_after_dtmin_C"),
                    "required_kw_abs": hi.get("required_kw_abs"),
                    "matched_kw_abs": None,
                },
                "heat_pump_candidate": {
                    "source_column": hp_source,
                    "source_key": hp_source_key,
                    "sink_column": hp_sink,
                    "sink_key": hp_sink_key,
                    "temperature_lift_C": hp.get("temperature_lift_C"),
                    "estimated_COP_heating": hp.get("estimated_COP_heating"),
                    "covered_reboiler_kw": hp.get("covered_reboiler_kw"),
                    "estimated_compressor_kw": hp.get("estimated_compressor_kw"),
                },
                "aspen_validation_actions": [
                    "步骤 1：先执行所选 P1 HINT，读取 actual integrated_heat。",
                    "步骤 2：用 actual integrated_heat 更新 physical source_key/sink_key 热量台账。",
                    "步骤 3：若共享再沸器，按残余热阱重新定容热泵；若共享热源，按残余热源重新计算供热上限。",
                    "步骤 4：若热源和热阱均独立，可保留当前热泵独立定容值，但总收益仍需加入实际 Q_HI 后重算。",
                    "步骤 5：重新运行 quantified P2 评分；当前条件分不得作为严格结果。",
                ],
            }
            _priority_metadata(hybrid, "hybrid", priority_eligible=True)
            hybrids.append(hybrid)

    hybrids.sort(
        key=lambda x: (
            _safe_float(x.get("technical_score", x.get("score")), 0.0) or 0.0,
            1 if x.get("resource_independent") else 0,
            _safe_float(x.get("heat_pump_heat_upper_bound_kw"), 0.0) or 0.0,
        ),
        reverse=True,
    )
    return hybrids[:max_candidates]


def _heat_integration_priority_order(candidate: Dict[str, Any]) -> int:
    """Any temperature-feasible compressor-free recovery is a P1 option."""
    return PRIORITY_TIER_DIRECT_HI if _direct_heat_integration_is_temperature_feasible(candidate) else 9

def build_intensification_analysis(
    process_data: dict,
    dt_min_c: float = DEFAULT_DT_MIN_C,
    carnot_efficiency: float = DEFAULT_CARNOT_EFFICIENCY,
) -> Dict[str, Any]:
    """Build the deterministic package with hard technology precedence.

    A temperature-feasible direct exchanger is a P1 option even when its heat
    load is not yet quantified.  Quantified P1 options create fully calculated P2
    portfolios; pending P1 options still create conditional P2 structural
    recommendations, but they cannot invent integrated heat, residual duties or
    final compressor size before Aspen HINT provides the recoverable duty.
    """
    # 1) Screen direct integration first.
    hx = analyze_cross_column_heat_integration(process_data, dt_min_c)
    priority_hx = [item for item in hx if _direct_heat_integration_is_temperature_feasible(item)]
    feasible_hx = [item for item in priority_hx if _direct_heat_integration_is_feasible(item)]
    pending_hx = [item for item in priority_hx if not _direct_heat_integration_is_feasible(item)]

    # 2) Re-score heat pumps only with quantified direct duties debited.
    global_hp = analyze_global_heat_pump_candidates(
        process_data,
        dt_min_c,
        carnot_efficiency,
        heat_integration_candidates=feasible_hx,
    )

    # Unquantified P1 options cannot yet debit kW, but they must gate any heat
    # pump competing for the same physical source or reboiler sink.
    for hp_item in global_hp:
        hp_source_key = str(hp_item.get("source_key") or f"{hp_item.get('source_column')}:condenser").upper().strip()
        hp_sink_key = str(hp_item.get("sink_key") or f"{hp_item.get('sink_column')}:reboiler").upper().strip()
        conflicts = []
        for hi in pending_hx:
            hi_source_key = str(hi.get("hot_source_key") or f"{hi.get('hot_source_column')}:condenser").upper().strip()
            hi_sink_key = str(hi.get("cold_sink_key") or f"{hi.get('cold_sink_column')}:reboiler").upper().strip()
            if hi_source_key == hp_source_key or hi_sink_key == hp_sink_key:
                conflicts.append({
                    "hot_source_key": hi_source_key,
                    "cold_sink_key": hi_sink_key,
                    "technical_score": hi.get("technical_score", hi.get("score")),
                    "missing_basis": hi.get("available_heat_basis"),
                })
        if conflicts:
            hp_item["screening_gate"] = "blocked_pending_P1_direct_heat_integration_validation"
            hp_item["pending_direct_heat_integration"] = conflicts
            hp_item["final_selection_allowed"] = False
            hp_item["reason"] = (
                "存在温度可行但热负荷尚未量化的 P1 直接热集成方案，与本热泵共享热源或再沸热阱；"
                "必须先完成 HINT duty 验证并计算残余负荷，当前热泵仅保留为 P3 备选。 "
                + str(hp_item.get("reason") or "")
            )
        else:
            hp_item["final_selection_allowed"] = True
        _priority_metadata(hp_item, "heat_pump", priority_eligible=True)

    # 3) P2 has two screening states:
    #    - quantified P2 after HINT, with a strict residual heat ledger;
    #    - provisional P2 before HINT, which recommends the physical pairing but
    #      never invents Q_HI, residual sink/source duty or final compressor size.
    quantified_hybrid = analyze_heat_integration_heat_pump_hybrids(
        process_data, feasible_hx, global_hp
    )
    provisional_hybrid = analyze_provisional_heat_integration_heat_pump_hybrids(
        process_data, pending_hx, global_hp
    )
    hybrid = quantified_hybrid + provisional_hybrid
    hybrid.sort(
        key=lambda x: (
            _safe_float(x.get("technical_score", x.get("score")), 0.0) or 0.0,
            0 if not x.get("provisional_hybrid") else -1,
        ),
        reverse=True,
    )

    # Backward-compatible single-column list with hard-priority metadata.
    hp = analyze_single_column_heat_pumps(process_data, dt_min_c, carnot_efficiency)
    global_by_pair = {
        (str(x.get("source_column") or "").upper(), str(x.get("sink_column") or "").upper()): x
        for x in global_hp
    }
    for item in hp:
        cid = str(item.get("column") or "").upper()
        g = global_by_pair.get((cid, cid))
        if not g:
            _priority_metadata(item, "heat_pump", priority_eligible=True)
            continue
        item["score_without_heat_integration"] = item.get("score")
        item["score"] = g.get("score")
        item["feasibility"] = g.get("feasibility", item.get("feasibility"))
        item["heat_integration_priority_applied"] = g.get("heat_integration_priority_applied", False)
        for key in [
            "preceding_heat_integration",
            "direct_heat_integration_kw_before_heat_pump",
            "residual_source_condenser_kw_after_heat_integration",
            "residual_sink_reboiler_kw_after_heat_integration",
            "covered_reboiler_kw_after_heat_integration",
            "reboiler_coverage_after_heat_integration",
            "incremental_reboiler_coverage_of_original_after_heat_integration",
            "estimated_compressor_kw_after_heat_integration",
            "heat_integration_priority_penalty",
            "screening_gate",
            "pending_direct_heat_integration",
            "final_selection_allowed",
        ]:
            if key in g:
                item[key] = g[key]
        _priority_metadata(item, "heat_pump", priority_eligible=True)

    p1_recommendations = [
        {**item, "category": "cross_column_heat_integration", "screening_priority_order": PRIORITY_TIER_DIRECT_HI}
        for item in priority_hx[:TOP_RECOMMENDATIONS_PER_TIER]
    ]
    p2_recommendations = [
        {**item, "category": "hybrid_heat_integration_heat_pump", "screening_priority_order": PRIORITY_TIER_HYBRID}
        for item in hybrid[:TOP_RECOMMENDATIONS_PER_TIER]
    ]
    p3_recommendations = [
        {**item, "category": "global_heat_pump", "screening_priority_order": PRIORITY_TIER_HEAT_PUMP}
        for item in global_hp[:TOP_RECOMMENDATIONS_PER_TIER]
    ]
    # Backward-compatible flattened view, but it is a grouped display sequence,
    # not a cross-technology ranking.
    ranked: List[Dict[str, Any]] = p1_recommendations + p2_recommendations + p3_recommendations

    obj = process_data.get("objective", {}) or {}
    total_reb = _safe_float(obj.get("total_reboiler_kw_abs"), 0.0) or 0.0
    total_cond = _safe_float(obj.get("total_condenser_kw_abs"), 0.0) or 0.0
    bottleneck_columns = []
    for col in process_data.get("columns", []) or []:
        q_reb = _col_q_reb_kw(col) or 0.0
        q_cond = _col_q_cond_kw(col) or 0.0
        bottleneck_columns.append({
            "column": _column_id(col),
            "role": _column_role(col),
            "reboiler_kw_abs": q_reb,
            "condenser_kw_abs": q_cond,
            "share_of_total_reboiler": q_reb / total_reb if total_reb > 0 else None,
            "share_of_total_condenser": q_cond / total_cond if total_cond > 0 else None,
        })
    bottleneck_columns.sort(
        key=lambda x: _safe_float(x.get("reboiler_kw_abs"), 0.0) or 0.0,
        reverse=True,
    )

    return {
        "assumptions": {
            "dt_min_C": dt_min_c,
            "carnot_efficiency": carnot_efficiency,
            "cop_model": "COP_heating ≈ eta_carnot * T_hot_K / (T_hot_K - T_cold_K)",
            "screening_only": True,
            "agent_scope": [
                "direct condenser or solvent-bottom-stream to reboiler heat integration",
                "single-column and cross-column heat pumps",
                "direct heat integration + heat pump hybrid portfolios",
            ],
            "global_screening_order": [
                "P1 temperature-feasible direct hot-stream to reboiler heat integration",
                "P2 quantified or conditional direct heat integration + heat pump portfolio",
                "P3 standalone heat pump",
            ],
            "ranking_rule": (
                "P1/P2/P3 are displayed as separate candidate groups; each group is sorted only by its own "
                "technical score and exposes at most three recommendations for user-selected strict validation"
            ),
            "score_interpretation": (
                "technical_score/score is comparable only inside P1, P2, or P3; "
                "each tier independently recommends its top three candidates"
            ),
            "pending_HI_rule": (
                "temperature-feasible but unquantified direct integration blocks final selection of any heat pump "
                "sharing its physical hot source or reboiler sink until Aspen HINT duty is read"
            ),
            "double_counting_rule": "direct duty is debited by physical source_key and sink_key; solvent-bottom heat and the same column condenser are distinct resources",
            "hybrid_scope": "pairwise portfolio screening; pending P1 creates conditional P2 structures, while quantified P1 debits physical resources and sizes same-sink heat pumps to the residual duty",
            "hybrid_heat_pump_sizing_rule": "Q_HP,target equals the residual/independent sink duty; process-vapor MVR compresses only the required source branch, while the remainder bypasses to the conventional condenser",
            "minimum_load_rule": "no arbitrary minimum heat-pump load is hard-coded; optional equipment limits are read from process_data constraints and otherwise remain a strict-validation requirement",
            "direct_heat_exchange_score": "unified_direct_heat_exchange_v1: temperature quality 25 + sink coverage 45 + source utilization 25 + configuration simplicity 5; within P1 only",
            "direct_heat_exchange_calculation": "sink-specific heat availability v2: condenser latent duty may remain fixed after the DTmin gate; solvent-bottom sensible heat is recalculated for each sink from CP-flow/reference enthalpy and T_out=max(T_sink+DTmin,T_min_out); otherwise HINT duty is required",
            "heat_pump_score": "unified_heat_pump_match_v1: sink coverage 35 + source utilization 20 + COP 25 + minimum lift 15 + minimum stream/exchanger configuration 5; within P3 only",
            "hybrid_score": "P2 score is anchored to the constituent P3 score and increases within the remaining headroom to 100. Pending P2 uses P1 temperature grade/resource independence/sink potential; quantified P2 uses actual direct heat, incremental steam saving, avoided compressor work and topology. Portfolio diagnostics are retained separately.",
            "solvent_bottom_data_contract": "prefer existing stream ID plus recoverable heat duty or CP-flow; otherwise Aspen HINT duty is required, but the temperature-feasible option remains P1",
            "excluded_scope": [
                "pressure adjustment",
                "multi-effect distillation",
                "feed preheating",
                "background-process pinch network design",
            ],
        },
        "data_status": process_data.get("status", "missing") if process_data else "missing",
        "energy_bottlenecks": bottleneck_columns,
        "cross_column_heat_integration_candidates": hx,
        "temperature_feasible_cross_column_heat_integration_candidates": priority_hx,
        "feasible_cross_column_heat_integration_candidates": feasible_hx,
        "pending_quantification_cross_column_heat_integration_candidates": pending_hx,
        "hybrid_heat_integration_heat_pump_candidates": hybrid,
        "quantified_hybrid_heat_integration_heat_pump_candidates": quantified_hybrid,
        "provisional_hybrid_heat_integration_heat_pump_candidates": provisional_hybrid,
        "global_heat_pump_candidates": global_hp,
        "single_column_heat_pump_candidates": hp,
        "tier_recommendations": {
            "P1": p1_recommendations,
            "P2": p2_recommendations,
            "P3": p3_recommendations,
        },
        "ranked_options": ranked,
    }


# ============================================================
# 报告渲染

# ============================================================
# 报告渲染
# ============================================================

def _priority_header(opt: Dict[str, Any]) -> str:
    feas = opt.get("feasibility") or _quality_label(_safe_float(opt.get("technical_score", opt.get("score")), 0.0) or 0.0)
    tier = opt.get("priority_tier")
    cls = opt.get("priority_class")
    technical = opt.get("technical_score", opt.get("score"))
    if tier is not None:
        return f"[{cls or ('P' + str(tier))} | 类内技术分={_fmt(technical, 1)} | 类内判断={feas}]"
    return f"[{feas} | 类内技术分={_fmt(technical, 1)}]"


def _render_top_options(options: List[Dict[str, Any]], limit: int = 5) -> str:
    if not options:
        return "暂无候选。"
    lines = []
    for idx, opt in enumerate(options[:limit], 1):
        cat = opt.get("category", opt.get("technology", "option"))
        tech = opt.get("technology", cat)
        header = _priority_header(opt)
        gate = "；需先完成 P1 HINT 定量" if opt.get("screening_gate") else ""
        if cat == "hybrid_heat_integration_heat_pump":
            if opt.get("provisional_hybrid"):
                hp_target = opt.get("heat_pump_heat_after_integration_kw")
                hp_upper = opt.get("heat_pump_heat_upper_bound_kw")
                hp_desc = (
                    f"HP可先按独立负荷约 {_fmt(hp_target, 2, 'kW')} 预筛"
                    if hp_target is not None
                    else f"HP严格负荷待 HINT 回填（当前独立上限 {_fmt(hp_upper, 2, 'kW')}）"
                )
                lines.append(
                    f"{idx}. {header} {tech}: "
                    f"HI {opt.get('heat_integration_source_key') or opt.get('heat_integration_source_column')}→{opt.get('heat_integration_sink_column')} + "
                    f"HP {opt.get('heat_pump_source_column')}→{opt.get('heat_pump_sink_column')}，"
                    f"{opt.get('reboiler_mode')}，{opt.get('source_mode')}；"
                    f"HI热量待 HINT 定量，{hp_desc}，COP≈{_fmt(opt.get('estimated_COP_heating'), 2)}。"
                )
            else:
                lines.append(
                    f"{idx}. {header} {tech}: "
                    f"HI {opt.get('heat_integration_source_key') or opt.get('heat_integration_source_column')}→{opt.get('heat_integration_sink_column')} + "
                    f"HP {opt.get('heat_pump_source_column')}→{opt.get('heat_pump_sink_column')}，"
                    f"{opt.get('reboiler_mode')}，"
                    f"总蒸汽削减={_fmt(opt.get('total_steam_saving_kw'), 2, 'kW')} "
                    f"({_pct(opt.get('total_reboiler_coverage'))})，"
                    f"压缩功≈{_fmt(opt.get('estimated_compressor_kw_after_integration'), 2, 'kW')}。"
                )
        elif cat == "global_heat_pump":
            after_hi = bool(opt.get("heat_integration_priority_applied"))
            covered = opt.get("covered_reboiler_kw_after_heat_integration") if after_hi else opt.get("covered_reboiler_kw")
            coverage = opt.get("reboiler_coverage_after_heat_integration") if after_hi else opt.get("reboiler_coverage")
            compressor = opt.get("estimated_compressor_kw_after_heat_integration") if after_hi else opt.get("estimated_compressor_kw")
            suffix = "（热集成后残余负荷）" if after_hi else ""
            lines.append(
                f"{idx}. {header} {tech}: "
                f"{opt.get('source_column')} 顶汽 → {opt.get('sink_column')} 再沸器，"
                f"温升={_fmt(opt.get('temperature_lift_C'), 2, '°C')}，"
                f"COP≈{_fmt(opt.get('estimated_COP_heating'), 2)}，"
                f"覆盖再沸={_fmt(covered, 2, 'kW')} ({_pct(coverage)}){suffix}，"
                f"压缩功≈{_fmt(compressor, 2, 'kW')}{gate}。"
            )
        elif cat == "heat_pump":
            after_hi = bool(opt.get("heat_integration_priority_applied"))
            covered = opt.get("covered_reboiler_kw_after_heat_integration") if after_hi else opt.get("covered_reboiler_kw")
            coverage = opt.get("reboiler_coverage_after_heat_integration") if after_hi else opt.get("reboiler_coverage")
            compressor = opt.get("estimated_compressor_kw_after_heat_integration") if after_hi else opt.get("estimated_compressor_kw")
            lines.append(
                f"{idx}. {header} {tech}: {opt.get('column')}，"
                f"有效温升={_fmt(opt.get('effective_lift_C'), 2, '°C')}，"
                f"COP≈{_fmt(opt.get('estimated_COP_heating'), 2)}，"
                f"可覆盖再沸={_fmt(covered, 2, 'kW')} ({_pct(coverage)})，"
                f"估算压缩功={_fmt(compressor, 2, 'kW')}{gate}。"
            )
        elif cat == "cross_column_heat_integration":
            if opt.get("matched_kw_abs") is None:
                potential = opt.get("specific_recoverable_heat_per_unit_CP_kw_per_kw_per_C")
                cp_req = opt.get("required_CP_flow_for_full_sink_coverage_kw_per_C")
                potential_desc = (
                    f"单位 CP 可回收热量={_fmt(potential, 2, 'kW/(kW/K)')}，"
                    f"满覆盖所需 CP-flow≈{_fmt(cp_req, 2, 'kW/K')}"
                    if potential is not None else "缺少热负荷/CP-flow，不能给出节能 kW"
                )
                lines.append(
                    f"{idx}. {header} {tech}: "
                    f"{opt.get('hot_source')} → {opt.get('cold_sink')}，"
                    f"ΔT余量={_fmt(opt.get('surplus_after_dtmin_C'), 2, '°C')}，"
                    f"{potential_desc}；实际节能待 HINT。"
                )
            else:
                lines.append(
                    f"{idx}. {header} {tech}: "
                    f"{opt.get('hot_source')} → {opt.get('cold_sink')}，"
                    f"热负荷节省={_fmt(opt.get('gross_hot_utility_saving_kw'), 2, 'kW')}，"
                    f"再沸覆盖={_pct(opt.get('cold_duty_coverage'))}，"
                    f"热源利用={_pct(opt.get('hot_duty_coverage'))}，"
                    f"ΔT余量={_fmt(opt.get('surplus_after_dtmin_C'), 2, '°C')}。"
                )
        else:
            lines.append(f"{idx}. {header} {tech}: {opt}")
    return "\n".join(lines)


def _render_tier_recommendations(analysis: Dict[str, Any]) -> str:
    tiers = analysis.get("tier_recommendations") or {}
    p1 = tiers.get("P1") or []
    p2 = tiers.get("P2") or []
    p3 = tiers.get("P3") or []
    return (
        "**P1：直接热集成 Top3**\n"
        + _render_top_options(p1, limit=TOP_RECOMMENDATIONS_PER_TIER)
        + "\n\n**P2：热集成 + 热泵 Top3**\n"
        + _render_top_options(p2, limit=TOP_RECOMMENDATIONS_PER_TIER)
        + "\n\n**P3：纯热泵 Top3**\n"
        + _render_top_options(p3, limit=TOP_RECOMMENDATIONS_PER_TIER)
    )


def render_heat_integration_report(
    process_data: dict,
    user_message: str = "",
    analysis: Optional[Dict[str, Any]] = None,
) -> str:
    """输出仅包含直接热集成、热泵及联合方案的确定性报告。"""
    if not process_data:
        return "当前还没有 BO 优化后的过程强化数据包。请先完成阶段 2：建立分离过程。"

    analysis = analysis or build_intensification_analysis(process_data)
    data_status = analysis.get("data_status", "unknown")
    caution = ""
    if data_status != "strictly_feasible":
        caution = "\n⚠️ 注意：当前 BO 数据包不是 strictly_feasible，以下结果只能作为方案筛选，不应作为最终工程结论。\n"

    bottleneck_lines = []
    for b in (analysis.get("energy_bottlenecks") or [])[:5]:
        bottleneck_lines.append(
            f"- {b.get('column')} [{b.get('role')}]: "
            f"Qreb={_fmt(b.get('reboiler_kw_abs'), 2, 'kW')} ({_pct(b.get('share_of_total_reboiler'))}), "
            f"Qcond={_fmt(b.get('condenser_kw_abs'), 2, 'kW')} ({_pct(b.get('share_of_total_condenser'))})"
        )

    top_options = analysis.get("ranked_options") or []
    hp_options = analysis.get("single_column_heat_pump_candidates") or []
    global_hp_options = analysis.get("global_heat_pump_candidates") or []
    hx_options = analysis.get("cross_column_heat_integration_candidates") or []
    hybrid_options = analysis.get("hybrid_heat_integration_heat_pump_candidates") or []

    next_actions = [
        "1. 先验证满足 7 °C DTmin 的无压缩机直接热集成，热源同时检查塔顶冷凝热和溶剂回收塔底溶剂显热。",
        "2. 从共享热源和共享热阱中扣除已集成热量，再评价热泵残余负荷。",
        "3. 分别验证同一再沸器联合供热与不同再沸器协同供热，禁止重复计算冷凝热。",
        "4. 对热泵扫描压缩比和再沸端 approach，并检查排气温度、HREB、HCOND 与压缩功。",
        "5. 用统一经济基准比较 Base / 直接热集成 / 联合方案 / 纯热泵的 TAC 与 CO2。",
    ]

    return (
        "## 热集成—热泵节能方案\n"
        f"{caution}\n"
        "### 1. 数据可用性与排序规则\n"
        f"- BO 数据状态：{data_status}\n"
        f"- DTmin={analysis.get('assumptions', {}).get('dt_min_C')} °C；"
        f"Carnot 效率={analysis.get('assumptions', {}).get('carnot_efficiency')}。\n"
        "- 候选按 P1 直接热集成、P2 热集成+热泵、P3 纯热泵分组展示。\n"
        "- 不生成跨技术全局优先分；P1、P2、P3 各自按类内技术分推荐前三，由用户指定某层 TopN 进入严格验证。\n"
        "- 温度满足 7 °C DTmin 但缺少热负荷的直接热集成仍属于 P1，先做 HINT 定量，不得被高 COP 热泵越级。\n"
        "- 热泵评分使用热集成后的残余物理热源与残余再沸需求，不使用完整负荷重复计分。\n\n"
        "### 2. 能耗瓶颈识别\n"
        f"{chr(10).join(bottleneck_lines) if bottleneck_lines else '暂无可用塔负荷数据。'}\n\n"
        "### 3. P1/P2/P3 分层推荐（每层最多 3 个）\n"
        f"{_render_tier_recommendations(analysis)}\n\n"
        "### 4. 直接热集成候选（冷凝热 + 溶剂塔底显热）\n"
        f"{_render_top_options([{**x, 'category': 'cross_column_heat_integration'} for x in hx_options], limit=6)}\n\n"
        "### 5. 热集成 + 热泵联合候选\n"
        f"{_render_top_options([{**x, 'category': 'hybrid_heat_integration_heat_pump'} for x in hybrid_options], limit=6)}\n\n"
        "### 6. 热集成后残余负荷的全局热泵候选\n"
        f"{_render_top_options([{**x, 'category': 'global_heat_pump'} for x in global_hp_options], limit=5)}\n\n"
        "### 7. 单塔 MVR / 闭式热泵参考\n"
        f"{_render_top_options([{**x, 'category': 'heat_pump'} for x in hp_options], limit=5)}\n\n"
        "### 8. 下一步 Aspen 验证动作\n"
        f"{chr(10).join(next_actions)}"
    )

# ============================================================
# v4: 仅热集成与热泵的意图路由与聚焦回答
# ============================================================

# ============================================================
# v3: 热集成优先的意图路由与聚焦回答
# ============================================================

def classify_intensification_intent(user_message: str) -> str:
    """Route only direct-heat-integration and heat-pump tasks inside this agent."""
    text = str(user_message or "").lower()
    zh = str(user_message or "")
    p1_hit = bool(re.search(r"\bp\s*1\b", text)) or "第一层" in zh
    p2_hit = bool(re.search(r"\bp\s*2\b", text)) or any(k in zh for k in ["联合方案", "联合热泵", "第二层"])
    p3_hit = bool(re.search(r"\bp\s*3\b", text)) or "第三层" in zh
    heat_pump_hit = p2_hit or p3_hit or any(k in text for k in ["mvr", "heat pump", "compress", "vapor recompression", "top vapor"]) or any(k in zh for k in ["热泵", "压缩", "蒸汽再压缩", "机械再压缩", "顶汽", "塔顶气"])
    direct_hi_hit = p1_hit or p2_hit or any(k in text for k in ["heat integration", "direct heat", "coupling", "heat-integrated", "heat integrated"]) or any(k in zh for k in ["热集成", "直接换热", "跨塔", "热耦合"])
    validation_hit = any(k in text for k in ["aspen", "validate", "validation", "verify", "verification", "build", "payload", "top1", "top 1"]) or any(k in zh for k in ["验证", "怎么建", "步骤", "落地", "模拟", "搭建", "构建", "payload", "top1", "top 1"])
    execution_hit = any(k in text for k in ["execute", "run now", "run validation", "run aspen", "really run", "perform"]) or any(k in zh for k in ["执行", "运行", "启动", "实跑", "真正跑", "开始跑", "启动aspen", "启动 Aspen", "自动搭建", "直接搭建", "调用aspen", "调用 Aspen"])

    if p2_hit and validation_hit and execution_hit:
        return "hybrid_execution"
    if p2_hit and validation_hit:
        return "hybrid_validation"
    if p1_hit and validation_hit:
        return "direct_hi_validation"
    if p3_hit and validation_hit and execution_hit:
        return "heat_pump_execution"
    if p3_hit and validation_hit:
        return "heat_pump_validation"
    if heat_pump_hit and validation_hit and execution_hit:
        return "heat_pump_execution"
    if heat_pump_hit and validation_hit:
        return "heat_pump_validation"
    if direct_hi_hit and validation_hit:
        return "direct_hi_validation"
    if heat_pump_hit:
        return "heat_pump"
    if direct_hi_hit:
        return "cross_column"

    outside_scope = (
        any(k in text for k in ["multi-effect", "multiple effect", "pressure adjustment", "feed preheat", "pinch", "gcc", "grand composite"])
        or any(k in zh for k in ["多效", "压力调整", "进料预热", "夹点", "总组合曲线", "组合曲线", "问题表"])
    )
    if outside_scope:
        return "out_of_scope"
    if any(k in text for k in ["bottleneck", "ranking", "priority", "compare"]) or any(k in zh for k in ["瓶颈", "优先", "排序", "比较", "哪个方案"]):
        return "ranking"
    if any(k in text for k in ["aspen", "validate", "verification", "steps"]) or any(k in zh for k in ["验证", "怎么建", "步骤", "落地", "模拟"]):
        return "validation"
    return "overview"

def _one_line_option(opt: dict) -> str:
    tech = opt.get("technology") or opt.get("category") or "方案"
    if opt.get("category") == "hybrid_heat_integration_heat_pump" or opt.get("heat_integration_source_column"):
        target = (
            f"HI {opt.get('heat_integration_source_key') or opt.get('heat_integration_source_column')}→{opt.get('heat_integration_sink_column')} + "
            f"HP {opt.get('heat_pump_source_column')}→{opt.get('heat_pump_sink_column')}"
        )
    elif opt.get("source_column") and opt.get("sink_column"):
        target = f"{opt.get('source_column')} 顶汽 → {opt.get('sink_column')} 再沸器"
    elif opt.get("column"):
        target = opt.get("column")
    elif opt.get("hot_source_column") and opt.get("cold_sink_column"):
        target = f"{opt.get('hot_source') or opt.get('hot_source_column')} → {opt.get('cold_sink') or (str(opt.get('cold_sink_column')) + ' reboiler')}"
    else:
        target = opt.get("cold_sink") or opt.get("hot_source") or "未指定对象"
    extras = []
    for key, label, unit in [
        ("temperature_lift_C", "温升", "°C"),
        ("heat_pump_temperature_lift_C", "HP温升", "°C"),
        ("effective_lift_C", "温升", "°C"),
        ("estimated_COP_heating", "COP", ""),
        ("reboiler_coverage_after_heat_integration", "残余再沸覆盖", ""),
        ("incremental_reboiler_coverage_of_original_after_heat_integration", "原再沸增量覆盖", ""),
        ("reboiler_coverage", "再沸覆盖", ""),
        ("total_reboiler_coverage", "联合覆盖", ""),
        ("surplus_after_dtmin_C", "DT余量", "°C"),
        ("matched_kw_abs", "热负荷节省", "kW"),
        ("total_steam_saving_kw", "总蒸汽削减", "kW"),
        ("estimated_compressor_kw_after_integration", "压缩功", "kW"),
        ("heat_pump_source_branch_fraction", "源汽分支", ""),
        ("residual_sink_fraction", "残余热阱比例", ""),
        ("temperature_driving_force_C", "温差", "°C"),
    ]:
        if key == "reboiler_coverage" and opt.get("reboiler_coverage_after_heat_integration") is not None:
            continue
        if key in opt and opt.get(key) is not None:
            val = opt.get(key)
            if "coverage" in key or key in {"heat_pump_source_branch_fraction", "residual_sink_fraction"}:
                extras.append(f"{label}={_pct(val)}")
            else:
                extras.append(f"{label}={_fmt(val, 2, unit)}")
    if opt.get("matched_kw_abs") is None and opt.get("category") == "cross_column_heat_integration":
        extras.append("热负荷节省=待HINT定量")
        if opt.get("specific_recoverable_heat_per_unit_CP_kw_per_kw_per_C") is not None:
            extras.append(
                "单位CP可回收热量="
                + _fmt(opt.get("specific_recoverable_heat_per_unit_CP_kw_per_kw_per_C"), 2, "kW/(kW/K)")
            )
        if opt.get("required_CP_flow_for_full_sink_coverage_kw_per_C") is not None:
            extras.append(
                "满覆盖所需CP-flow="
                + _fmt(opt.get("required_CP_flow_for_full_sink_coverage_kw_per_C"), 2, "kW/K")
            )
    if opt.get("reboiler_mode"):
        extras.insert(0, str(opt.get("reboiler_mode")))
    if opt.get("provisional_hybrid"):
        if opt.get("resource_independent"):
            extras.append("P2条件推荐：HI热量待HINT回填，HP独立定容不受该P1占用")
        else:
            extras.append("P2条件推荐：HI热量与残余HP负荷待HINT回填")
        if opt.get("heat_pump_heat_upper_bound_kw") is not None:
            label = "HP预筛负荷" if opt.get("resource_independent") else "HP独立负荷上限"
            extras.append(f"{label}={_fmt(opt.get('heat_pump_heat_upper_bound_kw'), 2, 'kW')}")
    if opt.get("screening_gate"):
        extras.append("P3最终选择被P1 HINT验证锁定")
    return f"- {target}: {tech}，{_priority_header(opt)}" + ("；" + "，".join(extras) if extras else "")


def _top_energy_bottleneck_lines(analysis: dict, limit: int = 3) -> List[str]:
    lines = []
    for b in (analysis.get("energy_bottlenecks") or [])[:limit]:
        lines.append(
            f"- {b.get('column')} [{b.get('role')}]: Qreb={_fmt(b.get('reboiler_kw_abs'), 2, 'kW')} "
            f"({_pct(b.get('share_of_total_reboiler'))})，Qcond={_fmt(b.get('condenser_kw_abs'), 2, 'kW')} "
            f"({_pct(b.get('share_of_total_condenser'))})"
        )
    return lines



def _extract_requested_top_n(user_message: str, default: int = 1, max_n: int = 3) -> int:
    text = str(user_message or "")
    m = re.search(r"top\s*([1-9])", text, flags=re.IGNORECASE)
    if not m:
        m = re.search(r"前\s*([一二三四五六七八九1-9])\s*个", text)
    if m:
        raw = m.group(1)
        cn = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        try:
            val = cn[raw] if raw in cn else int(raw)
            return max(1, min(max_n, val))
        except Exception:
            pass
    if any(k in text for k in ["全部", "前三", "3个", "三候选"]):
        return min(max_n, 3)
    return default


def _candidate_builder_kwargs(candidate: Dict[str, Any]) -> Dict[str, Any]:
    payload = candidate.get("aspen_validation_payload") or {}
    req = payload.get("required_inputs") or {}
    return {
        "source_column_id": payload.get("source_column_id") or candidate.get("source_column"),
        "sink_column_id": payload.get("sink_column_id") or candidate.get("sink_column"),
        "source_n_stages": req.get("source_n_stages"),
        "sink_n_stages": req.get("sink_n_stages"),
        "source_top_temperature_c": req.get("source_top_temperature_C") or candidate.get("source_top_temperature_C"),
        "sink_bottom_temperature_c": req.get("sink_bottom_temperature_C") or candidate.get("sink_bottom_temperature_C"),
        "pressure_ratio": req.get("pressure_ratio", candidate.get("pressure_ratio_screening", 2.0)),
        "reboiler_approach_c": req.get("reboiler_approach_C", candidate.get("dt_approach_C", DEFAULT_DT_MIN_C)),
        "final_condenser_target_c": req.get("source_top_temperature_C") or candidate.get("source_top_temperature_C"),
        "final_condenser_pressure": 0.0,
        "run": True,
    }



def _direct_hi_candidate_builder_kwargs(candidate: Dict[str, Any]) -> Dict[str, Any]:
    payload = candidate.get("aspen_validation_payload") or {}
    req = payload.get("required_inputs") or {}
    return {
        "source_mode": req.get("source_mode", candidate.get("source_mode", "column_top_vapor")),
        "existing_hot_stream_id": req.get("existing_hot_stream_id") or candidate.get("existing_hot_stream_id"),
        "source_column_id": payload.get("source_column_id") or candidate.get("hot_source_column"),
        "sink_column_id": payload.get("sink_column_id") or candidate.get("cold_sink_column"),
        "source_n_stages": req.get("source_n_stages"),
        "sink_n_stages": req.get("sink_n_stages"),
        "source_hot_temperature_c": req.get("source_hot_temperature_C") or candidate.get("hot_temperature_C"),
        "sink_bottom_temperature_c": req.get("sink_bottom_temperature_C") or candidate.get("cold_temperature_C"),
        "source_hot_stage": req.get("source_hot_stage", 2),
        "source_hot_phase": req.get("source_hot_phase", "V"),
        "sink_reboiler_liquid_stage": req.get("sink_reboiler_liquid_stage"),
        "source_hot_outlet_target_c": req.get("source_hot_outlet_target_C"),
        "dt_min_c": req.get("dt_min_C", DEFAULT_DT_MIN_C),
        "flash_pressure": req.get("flash_pressure", 0.0),
        "source_available_heat_kw": candidate.get("available_kw_abs"),
        "sink_reboiler_kw_abs": candidate.get("required_kw_abs"),
        "run": True,
    }


def _render_direct_heat_integration_validation_reply(process_data: dict, user_message: str, analysis: dict) -> str:
    """Render deterministic Aspen payload for direct cross-column heat integration."""
    hx = [
        x for x in (analysis.get("cross_column_heat_integration_candidates") or [])
        if _direct_heat_integration_is_temperature_feasible(x)
    ]
    if not hx:
        return "没有满足 7 °C DTmin 的直接热集成候选。冷凝热源需要塔顶温度/冷凝负荷；溶剂塔底热源至少需要塔底温度，缺少可回收热负荷时应通过 Aspen HINT duty 完成定量。"
    n = _extract_requested_top_n(user_message, default=1, max_n=min(3, len(hx)))
    lines = [
        f"## 直接热集成 Aspen 虚拟流股验证 payload（Top {n}）",
        "逻辑：冷端用 N-1 液相 pseudo stream + Flash2 模拟再沸器；热源可为塔顶冷凝蒸汽或溶剂回收塔底高温溶剂物流，统一接入 HINT 并按 7 °C 最小温差计算；不加压缩机。",
    ]
    for idx, cand in enumerate(hx[:n], 1):
        payload = cand.get("aspen_validation_payload") or make_cross_column_heat_integration_validation_payload(process_data, cand)
        cand["aspen_validation_payload"] = payload
        kwargs = _direct_hi_candidate_builder_kwargs(cand)
        lines.append(f"\n### 候选 {idx}: {cand.get('hot_source_column')} → {cand.get('cold_sink_column')}")
        lines.append(_one_line_option(cand))
        lines.append(f"- 温差余量 after DTmin = {_fmt(cand.get('surplus_after_dtmin_C'), 2, '°C')}；筛选匹配热量 = {_fmt(cand.get('matched_kw_abs'), 2, 'kW')}；冷端覆盖 = {_pct(cand.get('cold_duty_coverage'))}。")
        lines.append("\n**Aspen 结构**")
        lines.extend(f"- {x}" for x in (payload.get("aspen_structure") or [])[:6])
        lines.append("\n```python")
        lines.append("from aspen_virtual_heat_integration_builder import build_direct_heat_integration_virtual_scheme")
        lines.append("result = build_direct_heat_integration_virtual_scheme(")
        lines.append("    aspen,")
        for k, v in kwargs.items():
            lines.append(f"    {k}={v!r},")
        lines.append(")")
        lines.append("```")
        lines.append("\n**payload**")
        lines.append("```json")
        lines.append(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        lines.append("```")
    lines.append("\n读取结果时看四项：abs(Flash2 duty)=基准再沸需求；abs(HINT duty)=高温流股可供热；integrated_heat=min(两者)；external_reboiler_after_heat_integration=热集成后的剩余外部再沸。")
    return "\n".join(lines)

def _render_hybrid_validation_reply(process_data: dict, user_message: str, analysis: dict) -> str:
    """Render a sequential payload for a user-selected quantified or provisional P2 candidate."""
    tiers = analysis.get("tier_recommendations") or {}
    hybrids = tiers.get("P2") or analysis.get("hybrid_heat_integration_heat_pump_candidates") or []
    if not hybrids:
        return (
            "当前没有 P2 联合结构。只有在不存在温度可行的 P1，或不存在可用热泵配对时才应出现该结果；"
            "若 P1 只是待 HINT 定量，系统仍应给出条件 P2。"
        )
    rank = _extract_requested_top_rank(user_message, default=1, max_n=min(3, len(hybrids)))
    cand = hybrids[rank - 1]
    hi = cand.get("heat_integration_candidate") or {}
    hp = cand.get("heat_pump_candidate") or {}

    # Pending P1 candidates are intentionally included here: selecting a provisional
    # P2 means strict validation starts with its embedded P1 HINT, not that P2 is rejected.
    hi_pool = (
        analysis.get("temperature_feasible_cross_column_heat_integration_candidates")
        or analysis.get("cross_column_heat_integration_candidates")
        or []
    )
    hi_source_key_norm = str(cand.get("heat_integration_source_key") or "").upper().strip()
    hi_sink_key_norm = str(cand.get("heat_integration_sink_key") or "").upper().strip()
    hp_source_key_norm = str(cand.get("heat_pump_source_key") or "").upper().strip()
    hp_sink_key_norm = str(cand.get("heat_pump_sink_key") or "").upper().strip()
    hi_full = next((
        x for x in hi_pool
        if str(x.get("hot_source_key") or "").upper().strip() == hi_source_key_norm
        and str(x.get("cold_sink_key") or "").upper().strip() == hi_sink_key_norm
    ), hi)
    hp_full = next((
        x for x in (analysis.get("global_heat_pump_candidates") or [])
        if str(x.get("source_key") or "").upper().strip() == hp_source_key_norm
        and str(x.get("sink_key") or "").upper().strip() == hp_sink_key_norm
    ), hp)
    hi_payload = hi_full.get("aspen_validation_payload") or make_cross_column_heat_integration_validation_payload(process_data, hi_full)
    hp_payload = hp_full.get("aspen_validation_payload") or make_global_heat_pump_validation_payload(process_data, hp_full)

    if cand.get("provisional_hybrid"):
        independent = bool(cand.get("resource_independent"))
        same_sink = bool(cand.get("same_reboiler"))
        same_source = bool(cand.get("same_condenser_source"))
        hp_upper = _safe_float(cand.get("heat_pump_heat_upper_bound_kw"), 0.0) or 0.0
        power_upper = _safe_float(cand.get("compressor_power_upper_bound_kw"), 0.0) or 0.0
        if independent:
            hp_target_formula = f"Q_HP,hot = {hp_upper:.3f} kW preliminary; unchanged by Q_HI because source and sink are independent"
            source_formula = "Use the standalone HP source branch; recompute only if strict Aspen duties differ from screening values"
        elif same_sink and not same_source:
            hp_target_formula = "Q_HP,hot = min(max(0, Q_reboiler - Q_HI), Q_HP,max_by_source)"
            source_formula = "f_split = Q_HP,cold / Q_HP_source_available after Q_HI is known"
        elif same_source and not same_sink:
            hp_target_formula = "Q_HP,hot = min(Q_HP_sink, (Q_HP_source - Q_HI)/(1-1/COP))"
            source_formula = "f_split is calculated from the source heat remaining after direct integration"
        else:
            hp_target_formula = "Rebuild the residual source/sink ledger after Q_HI; no numeric HP target is valid before HINT"
            source_formula = "Source branch remains conditional"

        lines = [
            f"## P2 Top{rank} 条件联合方案顺序验证 payload",
            _one_line_option(cand),
            "",
            "**为什么现在可以推荐、但还不能给最终负荷**",
            "P2 Top3 是结构预筛。该方案的热源—热阱组合已经明确，但 P1 的 integrated_heat 尚未读取，因此总蒸汽削减、残余热阱和最终压缩机负荷不能伪造。",
            "",
            "**严格调用链**",
            "1. 运行下方直接热集成 HINT payload，读取 actual integrated_heat。",
            "2. 将 actual integrated_heat 写回该 P1 候选，更新 physical source_key/sink_key 台账。",
            f"3. 热泵定容规则：{hp_target_formula}。",
            f"4. 源侧分流规则：{source_formula}。",
            "5. 重新执行 quantified P2 评分；只有回填后的分数、蒸汽削减和压缩功才是严格 P2 结果。",
            "",
            "**直接热集成 payload**",
            "```json",
            json.dumps(hi_payload, ensure_ascii=False, indent=2, default=str),
            "```",
            "",
            "**热泵基础 payload（定容条件待 HINT 回填）**",
            "```json",
            json.dumps({
                "base_heat_pump_payload": hp_payload,
                "provisional_only": True,
                "resource_independent": independent,
                "same_reboiler": same_sink,
                "same_physical_source": same_source,
                "standalone_heat_pump_hot_duty_upper_bound_kw": hp_upper,
                "standalone_compressor_power_upper_bound_kw": power_upper,
                "heat_pump_target_rule_after_HINT": hp_target_formula,
                "source_branch_rule_after_HINT": source_formula,
                "conditional_sizing_rule": cand.get("conditional_sizing_rule"),
            }, ensure_ascii=False, indent=2, default=str),
            "```",
        ]
        return "\n".join(lines)

    branch = _safe_float(cand.get("heat_pump_source_branch_fraction"), 0.0) or 0.0
    target = _safe_float(cand.get("heat_pump_heat_after_integration_kw"), 0.0) or 0.0
    residual = _safe_float(cand.get("residual_hp_sink_reboiler_kw"), 0.0) or 0.0
    min_load = cand.get("minimum_load_check") or {}
    partial_mvr = branch < 0.999
    lines = [
        f"## P2 Top{rank} 联合方案顺序验证 payload",
        _one_line_option(cand),
        "",
        "**严格调用链**",
        "1. 先运行直接热集成 HINT，读取实际 integrated_heat；不得直接使用筛选估算替代。",
        "2. 更新共享热源和再沸热阱台账，重新计算残余负荷。",
        f"3. 将热泵供热目标设为 {target:.3f} kW，当前残余热阱为 {residual:.3f} kW；热泵不按原始完整再沸负荷定容。",
        f"4. 当前所需源侧分支比例约为 {branch:.4f}。",
    ]
    if partial_mvr:
        lines.append(
            "5. 若采用顶汽 MVR，必须在压缩机前设置源汽分流/旁路：仅该比例进入压缩机，其余返回常规冷凝器。"
            "当前 heat-pump builder 使用完整 pseudo stream，不能把其结果冒充为该 P2 的严格结果；需要 splitter wrapper 后才能自动实跑。"
        )
    else:
        lines.append("5. 当前源侧接近全流量，可直接进入现有热泵 builder 的严格热力学验证。")
    if min_load.get("constraints_available"):
        lines.append(
            "6. 已读取设备最小负荷约束："
            + ("通过。" if min_load.get("minimum_load_passed") else "不通过：" + "; ".join(min_load.get("violations") or []))
        )
    else:
        lines.append("6. 未提供压缩机最小稳定负荷/最小分流比；Aspen 可验证热力学，但机械可操作性仍需设备约束。")
    lines.extend([
        "",
        "**直接热集成 payload**",
        "```json",
        json.dumps(hi_payload, ensure_ascii=False, indent=2, default=str),
        "```",
        "",
        "**热泵子回路 payload（须使用残余负荷与源侧分支）**",
        "```json",
        json.dumps({
            "base_heat_pump_payload": hp_payload,
            "heat_pump_target_hot_kw": target,
            "residual_sink_reboiler_kw": residual,
            "source_branch_fraction": branch,
            "source_bypass_fraction": max(0.0, 1.0 - branch),
            "requires_source_splitter_for_mvr": partial_mvr,
            "minimum_load_check": min_load,
        }, ensure_ascii=False, indent=2, default=str),
        "```",
    ])
    return "\n".join(lines)


def _render_heat_pump_validation_reply(process_data: dict, user_message: str, analysis: dict) -> str:
    global_hp = [x for x in (analysis.get("global_heat_pump_candidates") or []) if x.get("feasibility") != "数据不足"]
    if not global_hp:
        return "没有可生成 Aspen 热泵验证 payload 的全局热泵候选。请先确认 process_data['columns'] 中有塔顶/塔底温度和冷凝/再沸负荷。"

    n = _extract_requested_top_n(user_message, default=1, max_n=3)
    selected = global_hp[:n]
    lines = [
        "## 全局热泵 Aspen 验证入口",
        "该对话框先生成确定性验证 payload 和 builder 调用参数；真正运行 Aspen 时，需要执行层传入已经打开的 Aspen COM document，并建议在复制文件上运行。",
    ]
    for idx, cand in enumerate(selected, 1):
        payload = cand.get("aspen_validation_payload") or make_global_heat_pump_validation_payload(process_data, cand)
        cand["aspen_validation_payload"] = payload
        kwargs = _candidate_builder_kwargs(cand)
        lines.append(f"\n### Top {idx}: {cand.get('source_column')} 顶汽 → {cand.get('sink_column')} 再沸器")
        lines.append(_one_line_option(cand))
        lines.append(
            f"- 热目标温度={_fmt(cand.get('hot_target_temperature_C'), 2, '°C')}；"
            f"估算压缩功={_fmt(cand.get('estimated_compressor_kw'), 2, 'kW')}；"
            f"剩余外部再沸={_fmt(cand.get('residual_reboiler_kw'), 2, 'kW')}。"
        )
        lines.append("- Aspen 结构：" + "；".join(str(x) for x in (payload.get("aspen_structure") or [])[:5]))
        lines.append("\n```python")
        lines.append("from aspen_virtual_heat_pump_builder import build_cross_column_heat_pump_virtual_scheme")
        lines.append("result = build_cross_column_heat_pump_virtual_scheme(")
        lines.append("    aspen,")
        for k, v in kwargs.items():
            lines.append(f"    {k}={v!r},")
        lines.append(")")
        lines.append("```")
        lines.append("\n**payload**")
        lines.append("```json")
        lines.append(json.dumps(payload, ensure_ascii=False, indent=2))
        lines.append("```")
    lines.append("\n执行结果读取时至少认四项：Flash2 duty≈基准再沸需求，HREB duty≈热泵供热，HCOND duty≈热泵后源塔剩余冷凝器负荷，Compressor power≈新增电耗；raw 单位必须按 Aspen 当前 unit set 再换算成 kW。")
    return "\n".join(lines)


# ============================================================
# Aspen 执行层：Top 热泵候选实跑 + 经济参数扫描
# ============================================================

def _extract_requested_top_rank(user_message: str, default: int = 1, max_n: int = 3) -> int:
    """解析“Top1/Top 2/第3个”等候选序号。"""
    text = str(user_message or "")
    m = re.search(r"top\s*([1-9])", text, flags=re.IGNORECASE)
    if not m:
        m = re.search(r"第\s*([一二三四五六七八九1-9])\s*个", text)
    if not m:
        return default
    raw = m.group(1)
    cn = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    try:
        val = cn[raw] if raw in cn else int(raw)
        return max(1, min(max_n, val))
    except Exception:
        return default


def _parse_number_list_after_key(text: str, keys: List[str]) -> Optional[List[float]]:
    """从消息中解析形如 pressure_ratio=1.8,2.0 或 approach=8/10/12 的列表。"""
    raw = str(text or "")
    for key in keys:
        pattern = rf"{re.escape(key)}\s*[:=：]\s*([0-9.,，/\s-]+)"
        m = re.search(pattern, raw, flags=re.IGNORECASE)
        if not m:
            continue
        nums = re.findall(r"[-+]?\d+(?:\.\d+)?", m.group(1))
        vals = []
        for x in nums:
            v = _safe_float(x, None)
            if v is not None:
                vals.append(float(v))
        if vals:
            return vals
    return None


def _heat_pump_scan_grid(user_message: str) -> Tuple[List[float], List[float]]:
    """默认经济参数扫描网格。用户可在对话中用 pressure_ratio=... / approach=... 覆盖。"""
    pr = _parse_number_list_after_key(user_message, ["pressure_ratio", "pr", "压缩比", "压力比"])
    ap = _parse_number_list_after_key(user_message, ["approach", "dt", "dtmin", "温差", "传热温差"])
    if not pr:
        # Keep the economic pressure-ratio scan within the practical range.
        # Low-PR points remain for diagnostics and are rejected by the signed-duty
        # and discharge-temperature gates when the compressor lift is insufficient.
        # Ratios above 4.0 are excluded because they are rarely competitive after
        # compressor electricity and equipment cost are included.
        pr = [1.4, 1.6, 1.8, 2.0, 2.4, 2.8, 3.2, 3.6, 4.0]
    if not ap:
        # All heat exchangers use the unified minimum approach of 7 C.
        ap = [7.0, 8.0, 10.0, 12.0, 15.0]
    # Apply the same engineering ceiling to both the default grid and user-provided
    # overrides so an accidental request cannot reintroduce uneconomic high-PR cases.
    pr = sorted({
        round(float(x), 4)
        for x in pr
        if _safe_float(x, None) is not None
        and 1.01 <= float(x) <= DEFAULT_HP_MAX_PRESSURE_RATIO
    })
    ap = sorted({round(float(x), 4) for x in ap if _safe_float(x, None) is not None and DEFAULT_DT_MIN_C <= float(x) <= 35.0})
    return pr[:16] or [2.0], ap[:9] or [DEFAULT_DT_MIN_C]


def _economic_basis_for_heat_pump(process_data: dict, state_dict: Optional[dict] = None) -> Tuple[Dict[str, float], List[str]]:
    """读取热泵经济优化参数；缺项时使用明确标注的默认值。"""
    state_dict = state_dict or {}
    basis = dict((process_data.get("economic_basis") or {}) if isinstance(process_data, dict) else {})
    basis.update(state_dict.get("heat_pump_economic_basis") or {})
    notes: List[str] = []

    annual_hours = _safe_float(basis.get("annual_hours"), 8000.0) or 8000.0
    steam_price = _safe_float(basis.get("steam_price_usd_per_kw_h"), 0.056) or 0.056
    cooling_price = _safe_float(basis.get("cooling_price_usd_per_kw_h"), 0.00057) or 0.00057
    electricity_price = _safe_float(basis.get("electricity_price_usd_per_kw_h"), None)
    if electricity_price is None or electricity_price <= 0:
        # 原 BO 基准电价常为 0。热泵经济性必须计入电耗，因此给出可覆盖的默认值并在结果中说明。
        electricity_price = 0.085
        notes.append("BO economic_basis 中 electricity_price_usd_per_kw_h 缺失或为 0；热泵执行优化临时使用 0.085 USD/kWh，可在 state_dict['heat_pump_economic_basis'] 中覆盖。")

    capex_per_kw = _safe_float(basis.get("heat_pump_compressor_capex_usd_per_kw"), 0.0) or 0.0
    hx_capex_per_kw = _safe_float(basis.get("direct_heat_exchanger_capex_usd_per_kw"), 0.0) or 0.0
    annualization_years = _safe_float(basis.get("capex_annualization_years"), 3.0) or 3.0
    emission_factor = _safe_float(basis.get("emission_factor_kt_co2_per_kw_year_equiv"), 2.450 * annual_hours / 1000.0) or (2.450 * annual_hours / 1000.0)

    return {
        "annual_hours": float(annual_hours),
        "steam_price_usd_per_kw_h": float(steam_price),
        "cooling_price_usd_per_kw_h": float(cooling_price),
        "electricity_price_usd_per_kw_h": float(electricity_price),
        "heat_pump_compressor_capex_usd_per_kw": float(capex_per_kw),
        "direct_heat_exchanger_capex_usd_per_kw": float(hx_capex_per_kw),
        "capex_annualization_years": float(annualization_years),
        "emission_factor_kt_co2_per_kw_year_equiv": float(emission_factor),
    }, notes


def _infer_kw_from_raw(raw_value: Any, reference_kw: Optional[float] = None) -> Tuple[Optional[float], str]:
    """将 Aspen raw duty/power 尝试换算为 kW。优先用 BO 数据中的 kW 量级做判别。"""
    val = _safe_float(raw_value, None)
    if val is None:
        return None, "missing"
    aval = abs(float(val))
    ref = _safe_float(reference_kw, None)
    choices = [(aval, "raw_as_kW"), (aval / 1000.0, "raw_W_to_kW"), (aval * 1000.0, "raw_MW_to_kW")]
    if ref is not None and ref > 1e-9:
        def score(item):
            x, _ = item
            if x <= 0:
                return 1e9
            return abs(math.log(max(x, 1e-12) / max(ref, 1e-12)))
        best = min(choices, key=score)
        return best[0], best[1] + f"; reference={ref:.6g} kW"
    if aval > 10000:
        return aval / 1000.0, "raw_W_to_kW_by_magnitude"
    return aval, "raw_as_kW_by_magnitude"


def _infer_signed_kw_from_raw(raw_value: Any, reference_kw: Optional[float] = None) -> Tuple[Optional[float], str]:
    """Infer kW while preserving the Aspen sign convention."""
    raw = _safe_float(raw_value, None)
    if raw is None:
        return None, "missing"
    magnitude_kw, basis = _infer_kw_from_raw(raw, reference_kw)
    if magnitude_kw is None:
        return None, basis
    sign = -1.0 if raw < 0 else (1.0 if raw > 0 else 0.0)
    return sign * magnitude_kw, basis + "; sign_preserved"


def _heat_pump_validation_constraints(process_data: dict, state_dict: Optional[dict] = None) -> Dict[str, float]:
    """Resolve configurable gates for executed heat-pump cases."""
    merged: Dict[str, Any] = {}
    for pack in [
        (process_data or {}).get("intensification_constraints"),
        (process_data or {}).get("heat_pump_validation_constraints"),
        (state_dict or {}).get("intensification_constraints"),
        (state_dict or {}).get("heat_pump_validation_constraints"),
    ]:
        if isinstance(pack, dict):
            merged.update(pack)
    return {
        "min_discharge_margin_C": max(0.0, _safe_float(merged.get("min_discharge_margin_C"), DEFAULT_HP_MIN_DISCHARGE_MARGIN_C) or DEFAULT_HP_MIN_DISCHARGE_MARGIN_C),
        "min_heat_release_kw": max(0.0, _safe_float(merged.get("min_heat_release_kw", merged.get("min_hp_duty_kw")), DEFAULT_HP_MIN_HEAT_RELEASE_KW) or DEFAULT_HP_MIN_HEAT_RELEASE_KW),
        "min_sink_coverage": max(0.0, min(1.0, _safe_float(merged.get("min_sink_coverage", merged.get("min_hp_residual_sink_fraction")), DEFAULT_HP_MIN_SINK_COVERAGE) or DEFAULT_HP_MIN_SINK_COVERAGE)),
        "min_source_recovery_fraction": max(0.0, min(1.0, _safe_float(merged.get("min_source_recovery_fraction", merged.get("min_hp_source_branch_fraction")), DEFAULT_HP_MIN_SOURCE_RECOVERY_FRACTION) or DEFAULT_HP_MIN_SOURCE_RECOVERY_FRACTION)),
        "max_source_overdraw_fraction": max(0.0, _safe_float(merged.get("max_source_overdraw_fraction"), DEFAULT_HP_MAX_SOURCE_OVERDRAW_FRACTION) or DEFAULT_HP_MAX_SOURCE_OVERDRAW_FRACTION),
        "max_hreb_target_error_C": max(0.1, _safe_float(merged.get("max_hreb_target_error_C"), DEFAULT_HP_MAX_HREB_TARGET_ERROR_C) or DEFAULT_HP_MAX_HREB_TARGET_ERROR_C),
        "duty_sign_tolerance_kw": max(0.0, _safe_float(merged.get("duty_sign_tolerance_kw"), DEFAULT_HP_DUTY_SIGN_TOLERANCE_KW) or DEFAULT_HP_DUTY_SIGN_TOLERANCE_KW),
    }


def _score_heat_pump_execution_case(
    result: Dict[str, Any],
    candidate: Dict[str, Any],
    economic_basis: Dict[str, float],
    validation_constraints: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Score an Aspen heat-pump run only after strict physical heat-exchange gates.

    A case may enter economic ranking only when the compressed vapor is genuinely
    hotter than the HREB target and HREB removes heat from the compressed stream.
    Positive HREB Heater duty is rejected instead of being converted to useful heat
    with ``abs()``.  The virtual HCOND duty remains diagnostic; the physical source
    condenser reduction is anchored by ``Q_source = Q_hot - W``.
    """
    eb = result.get("energy_balance_raw") or {}
    streams = result.get("streams") or {}
    tb = result.get("temperature_basis") or {}
    gates = dict(validation_constraints or {})
    if not gates:
        gates = _heat_pump_validation_constraints({}, {})

    sink_reb_ref = _safe_float(candidate.get("sink_reboiler_kw_abs"), None)
    src_cond_ref = _safe_float(candidate.get("source_condenser_kw_abs"), None)
    est_comp_ref = _safe_float(candidate.get("estimated_compressor_kw"), None)

    reb_surrogate_kw, reb_surrogate_unit = _infer_kw_from_raw(
        eb.get("reboiler_flash_duty_raw"), sink_reb_ref
    )
    hreb_signed_kw, hreb_unit = _infer_signed_kw_from_raw(
        eb.get("heat_pump_heat_to_reboiler_raw_signed_HREB"),
        candidate.get("covered_reboiler_kw"),
    )
    residual_surrogate_kw, residual_surrogate_unit = _infer_kw_from_raw(
        eb.get("external_reboiler_after_heat_pump_raw"),
        candidate.get("residual_reboiler_kw"),
    )
    hcond_signed_kw, hcond_unit = _infer_signed_kw_from_raw(
        eb.get("final_condenser_duty_raw_signed_HCOND", eb.get("final_condenser_duty_raw")),
        src_cond_ref,
    )
    comp_kw, comp_unit = _infer_kw_from_raw(
        eb.get("compressor_power_raw"), est_comp_ref
    )

    fallback_notes: List[str] = []
    rejection_reasons: List[str] = []
    if sink_reb_ref is not None and sink_reb_ref > 0:
        base_reboiler_kw = float(sink_reb_ref)
        base_reboiler_basis = "BO/Aspen actual column reboiler duty"
    else:
        base_reboiler_kw = _safe_float(reb_surrogate_kw, 0.0) or 0.0
        base_reboiler_basis = "Flash2 virtual surrogate fallback"
        fallback_notes.append("缺少 sink_reboiler_kw_abs，使用 Flash2 duty 作为再沸基准。")

    src_cond_ref = max(0.0, _safe_float(src_cond_ref, 0.0) or 0.0)
    comp_kw = max(0.0, _safe_float(comp_kw, 0.0) or 0.0)
    duty_tol = max(0.0, _safe_float(gates.get("duty_sign_tolerance_kw"), DEFAULT_HP_DUTY_SIGN_TOLERANCE_KW) or 0.0)

    # HREB must be a cooler/condensing heat-release block under Aspen Heater sign convention.
    if hreb_signed_kw is None:
        heat_raw_kw = 0.0
        rejection_reasons.append("缺少 HREB signed duty；需要 heat-pump builder v6 或更高版本。")
        hreb_is_heat_releasing = False
    else:
        hreb_is_heat_releasing = hreb_signed_kw < -duty_tol
        heat_raw_kw = abs(hreb_signed_kw) if hreb_is_heat_releasing else 0.0
        if not hreb_is_heat_releasing:
            rejection_reasons.append(
                f"HREB signed duty={hreb_signed_kw:.3f} kW，不是负值放热；该参数点实际在加热压缩物流。"
            )

    # Read actual stream temperatures after Aspen convergence.
    compressor_out_t = _temperature_c((streams.get("compressor_out_snapshot") or {}).get("temperature_raw"))
    hreb_out_t = _temperature_c((streams.get("hreb_out_snapshot") or {}).get("temperature_raw"))
    hcond_out_t = _temperature_c((streams.get("hcond_out_snapshot") or {}).get("temperature_raw"))
    hreb_target_t = _safe_float(tb.get("reboiler_supply_heater_target_C"), None)
    min_discharge_margin = max(0.0, _safe_float(gates.get("min_discharge_margin_C"), DEFAULT_HP_MIN_DISCHARGE_MARGIN_C) or 0.0)
    discharge_margin = None
    if compressor_out_t is None or hreb_target_t is None:
        rejection_reasons.append("缺少压缩机排气温度或 HREB 目标温度，无法确认真实温升。")
    else:
        discharge_margin = compressor_out_t - hreb_target_t
        if discharge_margin < min_discharge_margin:
            rejection_reasons.append(
                f"压缩机排气温度裕量仅 {discharge_margin:.3f} °C，低于门槛 {min_discharge_margin:.3f} °C。"
            )

    hreb_target_error = None
    max_target_error = max(0.1, _safe_float(gates.get("max_hreb_target_error_C"), DEFAULT_HP_MAX_HREB_TARGET_ERROR_C) or DEFAULT_HP_MAX_HREB_TARGET_ERROR_C)
    if hreb_out_t is not None and hreb_target_t is not None:
        hreb_target_error = abs(hreb_out_t - hreb_target_t)
        if hreb_target_error > max_target_error:
            rejection_reasons.append(
                f"HREB 出口未达到设定温度，误差 {hreb_target_error:.3f} °C > {max_target_error:.3f} °C。"
            )
    else:
        rejection_reasons.append("缺少 HREB 出口温度，无法确认换热器温度规格已满足。")

    if hcond_signed_kw is not None and hcond_signed_kw > duty_tol:
        rejection_reasons.append(
            f"HCOND signed duty={hcond_signed_kw:.3f} kW 为正，末端冷凝块实际在加热物流。"
        )

    heat_supplied_kw = min(base_reboiler_kw, heat_raw_kw) if base_reboiler_kw > 0 else heat_raw_kw
    steam_saved_kw = heat_supplied_kw
    residual_kw = max(0.0, base_reboiler_kw - steam_saved_kw)
    source_heat_recovered_uncapped_kw = heat_supplied_kw - comp_kw
    actual_cop = heat_supplied_kw / comp_kw if comp_kw > 1e-12 else None

    min_heat_release = max(
        max(0.0, _safe_float(gates.get("min_heat_release_kw"), DEFAULT_HP_MIN_HEAT_RELEASE_KW) or 0.0),
        max(0.0, _safe_float(gates.get("min_sink_coverage"), DEFAULT_HP_MIN_SINK_COVERAGE) or 0.0) * max(0.0, base_reboiler_kw),
    )
    if heat_supplied_kw < min_heat_release:
        rejection_reasons.append(
            f"真实 HREB 放热 {heat_supplied_kw:.3f} kW 低于换热负荷门槛 {min_heat_release:.3f} kW。"
        )
    if comp_kw <= duty_tol:
        rejection_reasons.append("压缩机功耗未得到正值，不能视为真实压缩式热泵运行。")
    if actual_cop is None or actual_cop <= 1.05:
        rejection_reasons.append(f"实际供热 COP={actual_cop}，未满足 COP>1.05。")
    if source_heat_recovered_uncapped_kw <= 0:
        rejection_reasons.append("Q_HREB-Wcomp<=0，未从源塔回收正的冷凝热。")

    min_source_fraction = max(0.0, _safe_float(gates.get("min_source_recovery_fraction"), DEFAULT_HP_MIN_SOURCE_RECOVERY_FRACTION) or 0.0)
    max_overdraw_fraction = max(0.0, _safe_float(gates.get("max_source_overdraw_fraction"), DEFAULT_HP_MAX_SOURCE_OVERDRAW_FRACTION) or 0.0)
    source_branch_fraction = None
    if src_cond_ref > 0:
        source_branch_fraction = source_heat_recovered_uncapped_kw / src_cond_ref
        if source_branch_fraction < min_source_fraction:
            rejection_reasons.append(
                f"源侧回收比例 {source_branch_fraction:.3%} 低于门槛 {min_source_fraction:.3%}。"
            )
        if source_branch_fraction > 1.0 + max_overdraw_fraction:
            rejection_reasons.append(
                f"源侧所需热量为原冷凝负荷的 {source_branch_fraction:.3%}，超过允许上限。"
            )
    else:
        rejection_reasons.append("缺少实际源塔冷凝负荷，无法执行源侧负荷门控。")

    physical_valid = result.get("status") == "built_and_run" and not rejection_reasons
    cooling_saved_kw = min(src_cond_ref, max(0.0, source_heat_recovered_uncapped_kw)) if physical_valid else 0.0
    final_cond_kw = max(0.0, src_cond_ref - cooling_saved_kw)
    additional_cooling_kw = 0.0
    cooling_delta_kw = cooling_saved_kw

    hcond_aux_kw = abs(hcond_signed_kw) if hcond_signed_kw is not None else None
    hcond_abs_error_kw = None
    hcond_relative_error = None
    hcond_consistent = None
    if hcond_aux_kw is not None:
        hcond_abs_error_kw = abs(float(hcond_aux_kw) - final_cond_kw)
        denom = max(1.0, src_cond_ref, final_cond_kw)
        hcond_relative_error = hcond_abs_error_kw / denom
        hcond_consistent = hcond_relative_error <= 0.25
        if not hcond_consistent:
            fallback_notes.append("虚拟 HCOND duty 与锚定能量衡算不闭合，仅保留为诊断值。")

    annual_hours = economic_basis["annual_hours"]
    steam_saving = steam_saved_kw * economic_basis["steam_price_usd_per_kw_h"] * annual_hours if physical_valid else 0.0
    cooling_saving = cooling_saved_kw * economic_basis["cooling_price_usd_per_kw_h"] * annual_hours if physical_valid else 0.0
    electricity_cost = comp_kw * economic_basis["electricity_price_usd_per_kw_h"] * annual_hours if physical_valid else 0.0
    annualized_capex = 0.0
    if physical_valid and economic_basis.get("heat_pump_compressor_capex_usd_per_kw", 0.0) > 0:
        annualized_capex = comp_kw * economic_basis["heat_pump_compressor_capex_usd_per_kw"] / max(1e-9, economic_basis["capex_annualization_years"])
    ungated_objective = steam_saving + cooling_saving - electricity_cost - annualized_capex
    objective = ungated_objective if physical_valid else -1e99
    co2_reduction = steam_saved_kw * economic_basis["emission_factor_kt_co2_per_kw_year_equiv"] if physical_valid else 0.0

    return {
        "status": result.get("status"),
        "feasible_for_ranking": bool(physical_valid),
        "physical_heat_exchange_valid": bool(physical_valid),
        "rejection_reasons": rejection_reasons,
        "validation_constraints": gates,
        "pressure_ratio": result.get("pressure_ratio"),
        "reboiler_approach_C": result.get("reboiler_approach_C"),
        "base_reboiler_kw": base_reboiler_kw,
        "base_reboiler_basis": base_reboiler_basis,
        "reboiler_flash_surrogate_kw": _safe_float(reb_surrogate_kw, None),
        "hreb_signed_duty_kw": hreb_signed_kw,
        "hreb_is_heat_releasing": hreb_is_heat_releasing,
        "heat_supplied_raw_kw": heat_raw_kw,
        "heat_supplied_kw": heat_supplied_kw,
        "minimum_heat_release_gate_kw": min_heat_release,
        "residual_reboiler_kw": residual_kw,
        "residual_reboiler_surrogate_kw": _safe_float(residual_surrogate_kw, None),
        "source_condenser_baseline_kw": src_cond_ref,
        "source_heat_recovered_kw": cooling_saved_kw,
        "source_heat_recovered_uncapped_kw": source_heat_recovered_uncapped_kw,
        "source_branch_fraction": source_branch_fraction,
        "final_condenser_kw": final_cond_kw,
        "hcond_signed_duty_kw": hcond_signed_kw,
        "hcond_auxiliary_duty_kw": hcond_aux_kw,
        "hcond_expected_from_anchored_balance_kw": final_cond_kw,
        "hcond_balance_abs_error_kw": hcond_abs_error_kw,
        "hcond_balance_relative_error": hcond_relative_error,
        "hcond_balance_consistent": hcond_consistent,
        "compressor_power_kw": comp_kw,
        "compressor_discharge_temperature_C": compressor_out_t,
        "hreb_target_temperature_C": hreb_target_t,
        "compressor_discharge_margin_C": discharge_margin,
        "hreb_outlet_temperature_C": hreb_out_t,
        "hreb_target_error_C": hreb_target_error,
        "hcond_outlet_temperature_C": hcond_out_t,
        "actual_COP_heating": actual_cop,
        "steam_saved_kw": steam_saved_kw if physical_valid else 0.0,
        "cooling_delta_kw": cooling_delta_kw,
        "cooling_saved_kw": cooling_saved_kw,
        "additional_cooling_kw": additional_cooling_kw,
        "annual_steam_saving_usd_per_year": steam_saving,
        "annual_cooling_saving_usd_per_year": cooling_saving,
        "annual_electricity_cost_usd_per_year": electricity_cost,
        "annualized_heat_pump_capex_usd_per_year": annualized_capex,
        "annual_net_saving_before_physical_gate_usd_per_year": ungated_objective,
        "annual_net_saving_usd_per_year": objective,
        "steam_co2_reduction_kt_per_year": co2_reduction,
        "unit_inference": {
            "reboiler_flash_surrogate": reb_surrogate_unit,
            "hreb_signed_duty": hreb_unit,
            "residual_reboiler_surrogate": residual_surrogate_unit,
            "hcond_signed_duty": hcond_unit,
            "compressor_power": comp_unit,
        },
        "fallback_notes": fallback_notes,
        "raw_result": result,
    }

def _open_optimized_aspen_from_bo_state(state_dict: dict, process_data: dict, log_lines: List[str]):
    """用 BO 最优参数重建一个独立 Aspen 会话。返回 optimizer，调用方负责 close。"""
    from BO_optimizer import DynamicBOOptimizer_SingleSession

    template_path = (state_dict or {}).get("template_path") or (state_dict or {}).get("aspen_template_path") or "Simulation 1.bkp"
    optimizer = DynamicBOOptimizer_SingleSession(state_dict or {}, template_path=template_path)
    log_lines.append(f"[BASE] open template and rebuild BO optimum: {os.path.abspath(template_path)}")
    optimizer.build_and_connect()
    best_params = (process_data or {}).get("best_params") or {}
    if not best_params:
        raise RuntimeError("process_data['best_params'] 为空，无法根据 BO 最优点重建 Aspen 基准流程。")
    log_lines.append(f"[BASE] apply best_params with {len(best_params)} variables")
    optimizer.evaluate_dynamic(**best_params)
    return optimizer


def _resolve_hybrid_execution_subcandidates(
    analysis: Dict[str, Any],
    hybrid_candidate: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Resolve the full P1/P3 records embedded in a compact P2 recommendation."""
    hi_source_key = str(hybrid_candidate.get("heat_integration_source_key") or "").upper().strip()
    hi_sink_key = str(hybrid_candidate.get("heat_integration_sink_key") or "").upper().strip()
    hp_source_key = str(hybrid_candidate.get("heat_pump_source_key") or "").upper().strip()
    hp_sink_key = str(hybrid_candidate.get("heat_pump_sink_key") or "").upper().strip()

    hi_pool = (
        analysis.get("temperature_feasible_cross_column_heat_integration_candidates")
        or analysis.get("cross_column_heat_integration_candidates")
        or []
    )
    hp_pool = analysis.get("global_heat_pump_candidates") or []
    hi_full = next((
        x for x in hi_pool
        if str(x.get("hot_source_key") or "").upper().strip() == hi_source_key
        and str(x.get("cold_sink_key") or "").upper().strip() == hi_sink_key
    ), hybrid_candidate.get("heat_integration_candidate") or {})
    hp_full = next((
        x for x in hp_pool
        if str(x.get("source_key") or "").upper().strip() == hp_source_key
        and str(x.get("sink_key") or "").upper().strip() == hp_sink_key
    ), hybrid_candidate.get("heat_pump_candidate") or {})
    return dict(hi_full or {}), dict(hp_full or {})


def _score_direct_heat_integration_execution_case(
    result: Dict[str, Any],
    candidate: Dict[str, Any],
    economic_basis: Dict[str, float],
) -> Dict[str, Any]:
    """Convert HINT duties to a strict direct-HI utility/economic result.

    The original P1 screening score is untouched.  This function only corrects
    strict utility accounting:
    - actual sink reboiler duty from BO/Aspen is the steam baseline;
    - Flash2 duty is retained as a virtual diagnostic, not substituted for Qreb;
    - condenser heat sources receive cooling credit directly;
    - a solvent-bottom sensible stream receives cooling credit only when an
      explicit baseline cooler duty is supplied.  Otherwise HINT still saves
      steam, but no unverified cold-utility saving is invented.
    """
    eb = result.get("energy_balance_raw") or {}
    sink_ref = _safe_float(candidate.get("required_kw_abs"), None)
    source_ref = _safe_float(candidate.get("available_kw_abs"), None)

    reb_surrogate_kw, base_unit = _infer_kw_from_raw(
        eb.get("reboiler_demand_raw_abs", eb.get("reboiler_flash_duty_raw")), sink_ref
    )
    hot_available_kw, hot_unit = _infer_kw_from_raw(
        eb.get("available_hot_stream_heat_raw_abs", eb.get("hot_source_cooler_duty_raw")),
        source_ref or sink_ref,
    )
    integrated_raw_kw, integrated_unit = _infer_kw_from_raw(
        eb.get("integrated_heat_raw"), sink_ref
    )
    residual_surrogate_kw, residual_unit = _infer_kw_from_raw(
        eb.get("external_reboiler_after_heat_integration_raw"), None
    )

    fallback_notes: List[str] = []
    if sink_ref is not None and sink_ref > 0:
        base_reboiler_kw = float(sink_ref)
        base_reboiler_basis = "BO/Aspen actual column reboiler duty"
    else:
        base_reboiler_kw = max(0.0, _safe_float(reb_surrogate_kw, 0.0) or 0.0)
        base_reboiler_basis = "Flash2 virtual surrogate fallback"
        fallback_notes.append(
            "缺少 required_kw_abs，才使用 Flash2 duty 作为再沸基准；严格比较应补充真实塔再沸负荷。"
        )

    hot_available_kw = _safe_float(hot_available_kw, None)
    integrated_raw_kw = _safe_float(integrated_raw_kw, None)
    if integrated_raw_kw is None and hot_available_kw is not None:
        integrated_raw_kw = hot_available_kw
        fallback_notes.append("integrated_heat_raw 未读到，使用 HINT available-hot duty。")
    if integrated_raw_kw is None:
        integrated_raw_kw = 0.0
        fallback_notes.append("HINT duty 未读到，严格热集成量按 0 处理。")

    integrated_kw = max(0.0, float(integrated_raw_kw))
    if hot_available_kw is not None:
        integrated_kw = min(integrated_kw, max(0.0, float(hot_available_kw)))
    if base_reboiler_kw > 0:
        integrated_kw = min(integrated_kw, base_reboiler_kw)
    residual_kw = max(0.0, base_reboiler_kw - integrated_kw)

    source_type = str(candidate.get("hot_source_type") or "column_condenser").lower().strip()
    source_baseline_cooling_kw = _safe_float(
        candidate.get("source_baseline_cooling_kw_abs", candidate.get("baseline_cooling_kw_abs")),
        None,
    )
    if source_type == "column_condenser":
        condenser_baseline = _safe_float(source_ref, None)
        cooling_saved_kw = min(
            integrated_kw,
            max(0.0, condenser_baseline) if condenser_baseline is not None else integrated_kw,
        )
        cooling_credit_basis = "displaced source-column condenser duty"
    elif source_baseline_cooling_kw is not None and source_baseline_cooling_kw > 0:
        cooling_saved_kw = min(integrated_kw, source_baseline_cooling_kw)
        cooling_credit_basis = "explicit baseline cooler duty on sensible hot stream"
    else:
        cooling_saved_kw = 0.0
        cooling_credit_basis = "not credited: solvent-bottom/background cooler baseline missing"
        fallback_notes.append(
            "塔底溶剂显热可减少再沸蒸汽，但 BO 数据未给出其原有冷却器负荷；"
            "因此不把 integrated_heat 自动计作冷却公用工程削减。"
        )

    annual_hours = economic_basis["annual_hours"]
    steam_saving = integrated_kw * economic_basis["steam_price_usd_per_kw_h"] * annual_hours
    cooling_saving = cooling_saved_kw * economic_basis["cooling_price_usd_per_kw_h"] * annual_hours
    hx_capex = 0.0
    if economic_basis.get("direct_heat_exchanger_capex_usd_per_kw", 0.0) > 0:
        hx_capex = (
            integrated_kw * economic_basis["direct_heat_exchanger_capex_usd_per_kw"]
            / max(1e-9, economic_basis["capex_annualization_years"])
        )
    net_saving = steam_saving + cooling_saving - hx_capex
    co2_reduction = integrated_kw * economic_basis["emission_factor_kt_co2_per_kw_year_equiv"]
    feasible = result.get("status") == "built_and_run" and integrated_kw > 1e-9

    return {
        "status": result.get("status"),
        "feasible_for_ranking": bool(feasible),
        "base_reboiler_kw": base_reboiler_kw,
        "base_reboiler_basis": base_reboiler_basis,
        "reboiler_flash_surrogate_kw": _safe_float(reb_surrogate_kw, None),
        "hot_available_kw": max(0.0, hot_available_kw) if hot_available_kw is not None else None,
        "integrated_heat_raw_kw": max(0.0, integrated_raw_kw),
        "integrated_heat_kw": integrated_kw,
        "residual_reboiler_kw": residual_kw,
        "residual_reboiler_surrogate_kw": _safe_float(residual_surrogate_kw, None),
        "cooling_saved_kw": cooling_saved_kw,
        "cooling_credit_basis": cooling_credit_basis,
        "annual_steam_saving_usd_per_year": steam_saving,
        "annual_cooling_saving_usd_per_year": cooling_saving,
        "annualized_direct_hx_capex_usd_per_year": hx_capex,
        "annual_net_saving_usd_per_year": net_saving,
        "steam_co2_reduction_kt_per_year": co2_reduction,
        "unit_inference": {
            "reboiler_flash_surrogate": base_unit,
            "hot_available": hot_unit,
            "integrated_heat": integrated_unit,
            "residual_reboiler_surrogate": residual_unit,
        },
        "fallback_notes": fallback_notes,
        "raw_result": result,
    }

def _score_hybrid_execution_case(
    hi_scored: Dict[str, Any],
    hp_scored: Dict[str, Any],
    hybrid_candidate: Dict[str, Any],
    hp_candidate: Dict[str, Any],
    process_data: Dict[str, Any],
) -> Dict[str, Any]:
    """Combine independent P1 + P3 actual results without double counting."""
    hi_kw = max(0.0, _safe_float(hi_scored.get("integrated_heat_kw"), 0.0) or 0.0)
    hp_kw = max(0.0, _safe_float(hp_scored.get("steam_saved_kw"), 0.0) or 0.0)
    total_steam_kw = hi_kw + hp_kw
    total_cooling_delta_kw = (
        max(0.0, _safe_float(hi_scored.get("cooling_saved_kw"), 0.0) or 0.0)
        + max(0.0, _safe_float(hp_scored.get("cooling_saved_kw"), 0.0) or 0.0)
    )
    compressor_kw = max(0.0, _safe_float(hp_scored.get("compressor_power_kw"), 0.0) or 0.0)
    annual_net = (
        (_safe_float(hi_scored.get("annual_net_saving_usd_per_year"), 0.0) or 0.0)
        + (_safe_float(hp_scored.get("annual_net_saving_usd_per_year"), 0.0) or 0.0)
    )
    co2 = (
        (_safe_float(hi_scored.get("steam_co2_reduction_kt_per_year"), 0.0) or 0.0)
        + (_safe_float(hp_scored.get("steam_co2_reduction_kt_per_year"), 0.0) or 0.0)
    )
    objective = process_data.get("objective") or {}
    total_reboiler = _safe_float(objective.get("total_reboiler_kw_abs"), None)
    if total_reboiler is None or total_reboiler <= 0:
        total_reboiler = sum((_col_q_reb_kw(c) or 0.0) for c in (process_data.get("columns") or []))
    process_coverage = total_steam_kw / total_reboiler if total_reboiler and total_reboiler > 0 else 0.0

    hp_base_score = max(0.0, min(100.0, _safe_float(
        hp_candidate.get("score_without_heat_integration", hp_candidate.get("technical_score", hp_candidate.get("score"))), 0.0
    ) or 0.0))
    direct_fraction = hi_kw / total_reboiler if total_reboiler and total_reboiler > 0 else 0.0
    hp_standalone_kw = max(0.0, _safe_float(hp_candidate.get("covered_reboiler_kw"), 0.0) or 0.0)
    incremental_fraction = (
        max(0.0, total_steam_kw - hp_standalone_kw) / total_reboiler
        if total_reboiler and total_reboiler > 0 else 0.0
    )
    gain_fraction = min(0.65, max(0.05,
        0.10
        + 0.30 * min(1.0, direct_fraction / 0.25)
        + 0.20 * min(1.0, incremental_fraction / 0.25)
    ))
    strict_technical_score = round(
        min(100.0, hp_base_score + (100.0 - hp_base_score) * gain_fraction), 1
    )
    feasible = bool(hi_scored.get("feasible_for_ranking") and hp_scored.get("feasible_for_ranking"))
    ranked_annual_net = annual_net if feasible else -1e99
    return {
        "status": "combined_strict_result" if feasible else "combined_infeasible",
        "feasible_for_ranking": feasible,
        "pressure_ratio": hp_scored.get("pressure_ratio"),
        "reboiler_approach_C": hp_scored.get("reboiler_approach_C"),
        "direct_heat_integration_kw": hi_kw,
        "heat_pump_steam_saving_kw": hp_kw,
        "total_steam_saving_kw": total_steam_kw,
        "overall_process_reboiler_coverage": process_coverage,
        "total_cooling_delta_kw": total_cooling_delta_kw,
        "compressor_power_kw": compressor_kw,
        "annual_net_saving_before_physical_gate_usd_per_year": annual_net,
        "annual_net_saving_usd_per_year": ranked_annual_net,
        "steam_co2_reduction_kt_per_year": co2,
        "constituent_P3_score": hp_base_score,
        "strict_P2_technical_score": strict_technical_score,
        "hybrid_gain_fraction_of_remaining_headroom": gain_fraction,
        "heat_integration_result": hi_scored,
        "heat_pump_result": hp_scored,
        "heat_pump_rejection_reasons": hp_scored.get("rejection_reasons") or [],
    }


def execute_hybrid_validation_and_economic_optimization(
    state_dict: Optional[dict],
    user_message: str = "执行 P2 Top1",
) -> Dict[str, Any]:
    """Execute selected P2 in one BO-optimum Aspen copy: HINT first, then HP scan."""
    started = time.time()
    state_dict = state_dict or {}
    process_data = _get_process_data_from_state(state_dict)
    if not process_data:
        return {"status": "failed", "error": "state_dict 中没有 bo_optimization / process_data，无法执行 P2。"}
    if build_direct_heat_integration_virtual_scheme is None:
        return {"status": "failed", "error": "无法导入 direct heat-integration builder。"}
    if build_cross_column_heat_pump_virtual_scheme is None:
        return {"status": "failed", "error": "无法导入 heat-pump builder。"}

    analysis = build_intensification_analysis(process_data)
    candidates = (analysis.get("tier_recommendations") or {}).get("P2") or []
    if not candidates:
        return {"status": "failed", "error": "当前没有可执行的 P2 候选。"}
    rank = _extract_requested_top_rank(user_message, default=1, max_n=min(3, len(candidates)))
    candidate = dict(candidates[rank - 1])

    # 当前自动严格执行层只接受物理热源和热阱均独立的组合。共享资源组合必须先按
    # HINT 结果定容并在 MVR 前实现 splitter/bypass，否则完整 pseudo stream 会过供热。
    resource_independent = bool(candidate.get("resource_independent"))
    if not resource_independent:
        return {
            "status": "requires_splitter_or_residual_sizing",
            "candidate_rank": rank,
            "candidate": candidate,
            "error": (
                "所选 P2 共享热源或再沸热阱。现有 heat-pump builder 使用完整塔顶 pseudo stream，"
                "必须先实现源汽 splitter/bypass 或闭式热泵循环量定容后才能称为严格自动实跑。"
            ),
        }

    hi_candidate, hp_candidate = _resolve_hybrid_execution_subcandidates(analysis, candidate)
    hi_payload = hi_candidate.get("aspen_validation_payload") or make_cross_column_heat_integration_validation_payload(
        process_data, hi_candidate, dt_min_c=DEFAULT_DT_MIN_C
    )
    hp_payload = hp_candidate.get("aspen_validation_payload") or make_global_heat_pump_validation_payload(
        process_data, hp_candidate
    )
    hi_candidate["aspen_validation_payload"] = hi_payload
    hp_candidate["aspen_validation_payload"] = hp_payload
    hi_kwargs = _direct_hi_candidate_builder_kwargs(hi_candidate)
    hp_kwargs = _candidate_builder_kwargs(hp_candidate)
    pressure_ratios, approaches = _heat_pump_scan_grid(user_message)
    economic_basis, basis_notes = _economic_basis_for_heat_pump(process_data, state_dict)
    validation_constraints = _heat_pump_validation_constraints(process_data, state_dict)

    evaluations: List[Dict[str, Any]] = []
    logs: List[str] = []
    for pr in pressure_ratios:
        for ap in approaches:
            optimizer = None
            case_log: List[str] = []
            try:
                optimizer = _open_optimized_aspen_from_bo_state(state_dict, process_data, case_log)
                aspen = optimizer.aspen

                # Strict order: P1 HINT is built and run first in this same Aspen copy.
                hi_run_kwargs = dict(hi_kwargs)
                hi_run_kwargs["dt_min_c"] = float(DEFAULT_DT_MIN_C)
                hi_run_kwargs["run"] = True
                hi_result = build_direct_heat_integration_virtual_scheme(aspen, **hi_run_kwargs)
                hi_scored = _score_direct_heat_integration_execution_case(
                    hi_result, hi_candidate, economic_basis
                )
                if not hi_scored.get("feasible_for_ranking"):
                    raise RuntimeError("P1 HINT 未得到正的 integrated_heat，停止该 P2 参数点。")

                # Resource-independent P2: actual HINT duty does not change the HP sink/source ledger.
                hp_run_kwargs = dict(hp_kwargs)
                hp_run_kwargs["pressure_ratio"] = float(pr)
                hp_run_kwargs["reboiler_approach_c"] = float(ap)
                hp_run_kwargs["run"] = True
                hp_result = build_cross_column_heat_pump_virtual_scheme(aspen, **hp_run_kwargs)
                hp_result["pressure_ratio"] = float(pr)
                hp_result["reboiler_approach_C"] = float(ap)
                hp_scored = _score_heat_pump_execution_case(hp_result, hp_candidate, economic_basis, validation_constraints)
                combined = _score_hybrid_execution_case(
                    hi_scored, hp_scored, candidate, hp_candidate, process_data
                )
                combined["case_log_tail"] = (
                    case_log + (hi_result.get("log") or []) + (hp_result.get("log") or [])
                )[-120:]
                evaluations.append(combined)
                if combined.get("feasible_for_ranking"):
                    logs.append(
                        f"[P2 CASE OK] PR={pr}, approach={ap}: Q_HI={hi_scored['integrated_heat_kw']:.3f} kW, "
                        f"Q_HP={hp_scored['steam_saved_kw']:.3f} kW, net={combined['annual_net_saving_usd_per_year']:.2f} USD/yr"
                    )
                else:
                    logs.append(
                        f"[P2 GATE REJECT] PR={pr}, approach={ap}: "
                        + "; ".join(hp_scored.get("rejection_reasons") or ["unknown physical gate failure"])
                    )
            except Exception as exc:
                evaluations.append({
                    "status": "failed",
                    "feasible_for_ranking": False,
                    "pressure_ratio": float(pr),
                    "reboiler_approach_C": float(ap),
                    "annual_net_saving_usd_per_year": -1e99,
                    "error": str(exc),
                    "traceback_tail": traceback.format_exc()[-3000:],
                    "case_log_tail": case_log[-100:],
                })
                logs.append(f"[P2 CASE FAIL] PR={pr}, approach={ap}: {exc}")
            finally:
                if optimizer is not None:
                    try:
                        optimizer.close()
                    except Exception:
                        pass

    feasible = [
        x for x in evaluations
        if x.get("feasible_for_ranking")
        and math.isfinite(_safe_float(x.get("annual_net_saving_usd_per_year"), float("nan")))
    ]
    best = max(feasible, key=lambda x: x.get("annual_net_saving_usd_per_year", -1e99)) if feasible else None
    return {
        "status": "completed" if best else "completed_without_feasible_case",
        "candidate_rank": rank,
        "candidate": candidate,
        "heat_integration_candidate": hi_candidate,
        "heat_pump_candidate": hp_candidate,
        "heat_integration_payload": hi_payload,
        "heat_pump_payload": hp_payload,
        "execution_order": ["rebuild_BO_optimum", "run_P1_HINT", "run_P3_subloop_in_same_Aspen_copy", "combined_economic_scoring"],
        "pressure_ratio_grid": pressure_ratios,
        "reboiler_approach_grid_C": approaches,
        "economic_basis": economic_basis,
        "economic_basis_notes": basis_notes,
        "heat_pump_validation_constraints": validation_constraints,
        "best": best,
        "evaluations": sorted(evaluations, key=lambda x: x.get("annual_net_saving_usd_per_year", -1e99), reverse=True),
        "logs": logs,
        "elapsed_s": time.time() - started,
    }


def render_hybrid_execution_result(execution: Dict[str, Any]) -> str:
    """Render actual P2 HINT + HP execution result for the GUI chat."""
    if not execution or execution.get("status") in {"failed", "requires_splitter_or_residual_sizing"}:
        title = "## P2 联合方案执行受阻" if (execution or {}).get("status") == "requires_splitter_or_residual_sizing" else "## P2 联合方案执行失败"
        return title + "\n" + str((execution or {}).get("error") or "未知错误")

    cand = execution.get("candidate") or {}
    best = execution.get("best") or {}
    lines = [
        "## P2 联合方案已执行：同一 Aspen 副本内 HINT → 热泵",
        f"候选：P2 Top{execution.get('candidate_rank')}，HI {cand.get('heat_integration_source_key')}→{cand.get('heat_integration_sink_column')} + HP {cand.get('heat_pump_source_column')}→{cand.get('heat_pump_sink_column')}",
        f"执行顺序：{' → '.join(execution.get('execution_order') or [])}",
        f"扫描网格：pressure_ratio={execution.get('pressure_ratio_grid')}；approach={execution.get('reboiler_approach_grid_C')} °C",
    ]
    if execution.get("economic_basis_notes"):
        lines.append("\n**经济基准说明**")
        lines.extend(f"- {x}" for x in execution.get("economic_basis_notes") or [])
    if not best:
        lines.append("\n没有参数组合通过真实换热门控。低压力比点即使 Aspen 收敛，也不会进入经济排序。")
        for item in (execution.get("evaluations") or [])[:3]:
            reasons = item.get("heat_pump_rejection_reasons") or (item.get("heat_pump_result") or {}).get("rejection_reasons") or []
            detail = "; ".join(reasons[:4]) or item.get("error") or item.get("status")
            lines.append(f"- PR={item.get('pressure_ratio')}, approach={item.get('reboiler_approach_C')}: {detail}")
        return "\n".join(lines)

    hi = best.get("heat_integration_result") or {}
    hp = best.get("heat_pump_result") or {}
    lines.extend([
        "\n### 联合经济最优参数",
        f"- pressure_ratio = {_fmt(best.get('pressure_ratio'), 3)}",
        f"- 热泵最小换热温差 = {_fmt(best.get('reboiler_approach_C'), 2, '°C')}；HINT DTmin 固定为 {_fmt(DEFAULT_DT_MIN_C, 1, '°C')}",
        f"- HINT 实际供热 = {_fmt(best.get('direct_heat_integration_kw'), 2, 'kW')}",
        f"- 热泵实际节汽 = {_fmt(best.get('heat_pump_steam_saving_kw'), 2, 'kW')}",
        f"- 联合总节汽 = {_fmt(best.get('total_steam_saving_kw'), 2, 'kW')}（全流程再沸覆盖 {_pct(best.get('overall_process_reboiler_coverage'))}）",
        f"- 压缩机功耗 = {_fmt(best.get('compressor_power_kw'), 2, 'kW')}",
        f"- 冷却公用工程削减 = {_fmt(best.get('total_cooling_delta_kw'), 2, 'kW')}",
        f"- 年净节省 = {_fmt(best.get('annual_net_saving_usd_per_year'), 0, 'USD/yr')}",
        f"- 蒸汽 CO2 减排 = {_fmt(best.get('steam_co2_reduction_kt_per_year'), 3, 'kt/yr')}",
        f"- 所含 P3 技术分 = {_fmt(best.get('constituent_P3_score'), 1)}；严格 P2 技术分 = {_fmt(best.get('strict_P2_technical_score'), 1)}",
        "\n### 关键严格读数",
        f"- HINT 后目标再沸器剩余负荷 = {_fmt(hi.get('residual_reboiler_kw'), 2, 'kW')}",
        f"- 热泵后目标再沸器剩余负荷 = {_fmt(hp.get('residual_reboiler_kw'), 2, 'kW')}",
        f"- 热泵后源塔物理剩余冷凝负荷 = {_fmt(hp.get('final_condenser_kw'), 2, 'kW')}",
        f"- 虚拟支路 HCOND duty（仅诊断） = {_fmt(hp.get('hcond_auxiliary_duty_kw'), 2, 'kW')}",
        f"- 压缩机排气温度 = {_fmt(hp.get('compressor_discharge_temperature_C'), 2, '°C')}；HREB目标 = {_fmt(hp.get('hreb_target_temperature_C'), 2, '°C')}；裕量 = {_fmt(hp.get('compressor_discharge_margin_C'), 2, '°C')}",
        f"- HREB signed duty = {_fmt(hp.get('hreb_signed_duty_kw'), 2, 'kW')}；真实放热门槛 = {_fmt(hp.get('minimum_heat_release_gate_kw'), 2, 'kW')}",
        f"- 源汽等效分流比例 = {_pct(hp.get('source_branch_fraction'), 1)}",
    ])
    lines.append("\n### 参数扫描结果 Top 5")
    lines.append("| 排名 | PR | approach °C | Q_HI kW | Q_HP kW | Wcomp kW | 总节汽 kW | P2分 | 年净节省 USD/yr |")
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for i, item in enumerate((execution.get("evaluations") or [])[:5], 1):
        lines.append(
            f"| {i} | {_fmt(item.get('pressure_ratio'), 3)} | {_fmt(item.get('reboiler_approach_C'), 2)} | "
            f"{_fmt(item.get('direct_heat_integration_kw'), 1)} | {_fmt(item.get('heat_pump_steam_saving_kw'), 1)} | "
            f"{_fmt(item.get('compressor_power_kw'), 1)} | {_fmt(item.get('total_steam_saving_kw'), 1)} | "
            f"{_fmt(item.get('strict_P2_technical_score'), 1)} | {_fmt(item.get('annual_net_saving_usd_per_year'), 0)} |"
        )
    lines.append(f"\n执行耗时记录：{_fmt(execution.get('elapsed_s'), 1, 's')}。")
    return "\n".join(lines)

def execute_heat_pump_validation_and_economic_optimization(
    state_dict: Optional[dict],
    user_message: str = "执行 Top1 热泵验证",
) -> Dict[str, Any]:
    """在复制/重建的 BO 最优 Aspen 流程上执行 Top 热泵验证，并按年净节省排序参数组合。"""
    started = time.time()
    state_dict = state_dict or {}
    process_data = _get_process_data_from_state(state_dict)
    if not process_data:
        return {"status": "failed", "error": "state_dict 中没有 bo_optimization / process_data，无法执行热泵验证。"}
    if build_cross_column_heat_pump_virtual_scheme is None:
        return {"status": "failed", "error": "无法导入 build_cross_column_heat_pump_virtual_scheme，请确认 aspen_virtual_heat_pump_builder.py 在同一目录。"}

    analysis = build_intensification_analysis(process_data)
    candidates = [x for x in (analysis.get("global_heat_pump_candidates") or []) if x.get("feasibility") != "数据不足"]
    if not candidates:
        return {"status": "failed", "error": "没有可执行的全局热泵候选。"}

    rank = _extract_requested_top_rank(user_message, default=1, max_n=min(3, len(candidates)))
    candidate = candidates[rank - 1]
    payload = candidate.get("aspen_validation_payload") or make_global_heat_pump_validation_payload(process_data, candidate)
    candidate["aspen_validation_payload"] = payload
    kwargs = _candidate_builder_kwargs(candidate)
    pressure_ratios, approaches = _heat_pump_scan_grid(user_message)
    economic_basis, basis_notes = _economic_basis_for_heat_pump(process_data, state_dict)
    validation_constraints = _heat_pump_validation_constraints(process_data, state_dict)

    logs: List[str] = []
    evaluations: List[Dict[str, Any]] = []
    for pr in pressure_ratios:
        for ap in approaches:
            case_log: List[str] = []
            optimizer = None
            try:
                optimizer = _open_optimized_aspen_from_bo_state(state_dict, process_data, case_log)
                run_kwargs = dict(kwargs)
                run_kwargs["pressure_ratio"] = float(pr)
                run_kwargs["reboiler_approach_c"] = float(ap)
                run_kwargs["run"] = True
                # builder 只接受 lower-case 参数名。
                result = build_cross_column_heat_pump_virtual_scheme(optimizer.aspen, **run_kwargs)
                result["pressure_ratio"] = float(pr)
                result["reboiler_approach_C"] = float(ap)
                scored = _score_heat_pump_execution_case(result, candidate, economic_basis, validation_constraints)
                scored["case_log_tail"] = (case_log + (result.get("log") or []))[-80:]
                evaluations.append(scored)
                if scored.get("feasible_for_ranking"):
                    logs.append(f"[CASE OK] PR={pr}, approach={ap}: net_saving={scored['annual_net_saving_usd_per_year']:.2f} USD/yr")
                else:
                    logs.append(f"[GATE REJECT] PR={pr}, approach={ap}: " + "; ".join(scored.get("rejection_reasons") or []))
            except Exception as exc:
                err = {
                    "status": "failed",
                    "feasible_for_ranking": False,
                    "pressure_ratio": float(pr),
                    "reboiler_approach_C": float(ap),
                    "annual_net_saving_usd_per_year": -1e99,
                    "error": str(exc),
                    "traceback_tail": traceback.format_exc()[-2500:],
                    "case_log_tail": case_log[-80:],
                }
                evaluations.append(err)
                logs.append(f"[CASE FAIL] PR={pr}, approach={ap}: {exc}")
            finally:
                if optimizer is not None:
                    try:
                        optimizer.close()
                    except Exception:
                        pass

    feasible = [x for x in evaluations if x.get("feasible_for_ranking") and math.isfinite(_safe_float(x.get("annual_net_saving_usd_per_year"), float("nan")))]
    best = max(feasible, key=lambda x: x.get("annual_net_saving_usd_per_year", -1e99)) if feasible else None
    return {
        "status": "completed" if best else "completed_without_feasible_case",
        "candidate_rank": rank,
        "candidate": candidate,
        "payload": payload,
        "pressure_ratio_grid": pressure_ratios,
        "reboiler_approach_grid_C": approaches,
        "economic_basis": economic_basis,
        "economic_basis_notes": basis_notes,
        "heat_pump_validation_constraints": validation_constraints,
        "best": best,
        "evaluations": sorted(evaluations, key=lambda x: x.get("annual_net_saving_usd_per_year", -1e99), reverse=True),
        "logs": logs,
        "elapsed_s": time.time() - started,
    }


def render_heat_pump_execution_result(execution: Dict[str, Any]) -> str:
    """将实际执行 + 经济优化结果渲染为对话框回复。"""
    if not execution or execution.get("status") == "failed":
        return "## Top 热泵执行失败\n" + str((execution or {}).get("error") or "未知错误")

    cand = execution.get("candidate") or {}
    best = execution.get("best") or {}
    lines = [
        "## Top 热泵已执行：Aspen 虚拟流股验证 + 经济参数优化",
        f"候选：Top {execution.get('candidate_rank')}，{cand.get('source_column')} 顶汽 → {cand.get('sink_column')} 再沸器",
        f"扫描网格：pressure_ratio={execution.get('pressure_ratio_grid')}；reboiler_approach_C={execution.get('reboiler_approach_grid_C')}",
    ]
    if execution.get("economic_basis_notes"):
        lines.append("\n**经济基准说明**")
        lines.extend(f"- {x}" for x in execution.get("economic_basis_notes") or [])
    lines.append("\n**经济基准**")
    lines.append("```json")
    lines.append(json.dumps(execution.get("economic_basis") or {}, ensure_ascii=False, indent=2))
    lines.append("```")

    if not best:
        lines.append("\n没有参数组合通过真实换热门控；请查看 HREB signed duty、排气温度裕量和换热负荷门槛。")
        for item in (execution.get("evaluations") or [])[:3]:
            detail = "; ".join((item.get("rejection_reasons") or [])[:4]) or item.get("error") or item.get("status")
            lines.append(f"- PR={item.get('pressure_ratio')}, approach={item.get('reboiler_approach_C')}: {detail}")
        return "\n".join(lines)

    lines.append("\n### 经济最优参数")
    lines.append(f"- pressure_ratio = {_fmt(best.get('pressure_ratio'), 3)}")
    lines.append(f"- reboiler_approach_C = {_fmt(best.get('reboiler_approach_C'), 2, '°C')}")
    lines.append(f"- HREB 供热 = {_fmt(best.get('heat_supplied_kw'), 2, 'kW')}")
    lines.append(f"- 剩余外部再沸 = {_fmt(best.get('residual_reboiler_kw'), 2, 'kW')}")
    lines.append(f"- 压缩机功耗 = {_fmt(best.get('compressor_power_kw'), 2, 'kW')}")
    lines.append(f"- 压缩机排气温度 = {_fmt(best.get('compressor_discharge_temperature_C'), 2, '°C')}；HREB目标 = {_fmt(best.get('hreb_target_temperature_C'), 2, '°C')}；裕量 = {_fmt(best.get('compressor_discharge_margin_C'), 2, '°C')}")
    lines.append(f"- HREB signed duty = {_fmt(best.get('hreb_signed_duty_kw'), 2, 'kW')}；真实放热门槛 = {_fmt(best.get('minimum_heat_release_gate_kw'), 2, 'kW')}")
    lines.append(f"- 源汽等效分流比例 = {_pct(best.get('source_branch_fraction'), 1)}")
    lines.append(f"- 原源塔冷凝负荷 = {_fmt(best.get('source_condenser_baseline_kw'), 2, 'kW')}")
    lines.append(f"- 热泵后源塔物理剩余冷凝负荷 = {_fmt(best.get('final_condenser_kw'), 2, 'kW')}")
    lines.append(f"- 虚拟支路 HCOND duty（仅诊断） = {_fmt(best.get('hcond_auxiliary_duty_kw'), 2, 'kW')}")
    lines.append(f"- 冷却公用工程削减 = {_fmt(best.get('cooling_saved_kw'), 2, 'kW')}")
    lines.append(f"- 热源侧回收热量 Qhot-W = {_fmt(best.get('source_heat_recovered_kw'), 2, 'kW')}")
    lines.append(f"- 年净节省 = {_fmt(best.get('annual_net_saving_usd_per_year'), 0, 'USD/yr')}")
    lines.append(f"- 蒸汽 CO2 减排 = {_fmt(best.get('steam_co2_reduction_kt_per_year'), 3, 'kt/yr')}")

    lines.append("\n### 参数扫描结果 Top 5")
    lines.append("| 排名 | pressure_ratio | approach °C | HREB kW | Wcomp kW | HCOND kW | cooling Δ kW | residual reb kW | net saving USD/yr |")
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for i, item in enumerate((execution.get("evaluations") or [])[:5], 1):
        lines.append(
            f"| {i} | {_fmt(item.get('pressure_ratio'), 3)} | {_fmt(item.get('reboiler_approach_C'), 2)} | "
            f"{_fmt(item.get('heat_supplied_kw'), 1)} | {_fmt(item.get('compressor_power_kw'), 1)} | "
            f"{_fmt(item.get('final_condenser_kw'), 1)} | {_fmt(item.get('cooling_delta_kw'), 1)} | "
            f"{_fmt(item.get('residual_reboiler_kw'), 1)} | {_fmt(item.get('annual_net_saving_usd_per_year'), 0)} |"
        )

    if best.get("fallback_notes"):
        lines.append("\n**读数兜底说明**")
        lines.extend(f"- {x}" for x in best.get("fallback_notes") or [])
    lines.append("\n**单位判断**")
    lines.append("```json")
    lines.append(json.dumps(best.get("unit_inference") or {}, ensure_ascii=False, indent=2))
    lines.append("```")

    raw = best.get("raw_result") or {}
    lines.append("\n**Aspen builder 状态**")
    lines.append(f"- status: {raw.get('status')}")
    eb = raw.get("energy_balance_raw") or {}
    lines.append("```json")
    lines.append(json.dumps(eb, ensure_ascii=False, indent=2, default=str))
    lines.append("```")
    lines.append(f"\n执行耗时记录：{_fmt(execution.get('elapsed_s'), 1, 's')}。")
    return "\n".join(lines)

def render_focused_intensification_reply(process_data: dict, user_message: str, analysis: Optional[dict] = None) -> str:
    """Deterministic focused reply for direct heat integration and heat pumps only."""
    if not process_data:
        return "当前还没有 BO 优化后的过程强化数据包。请先完成阶段 2：建立分离过程。"

    analysis = analysis or build_intensification_analysis(process_data)
    intent = classify_intensification_intent(user_message)
    status = analysis.get("data_status", "unknown")
    warning = ""
    if status != "strictly_feasible":
        warning = "\n⚠️ 当前数据包不是 strictly_feasible，以下只能作为候选筛选，不能当作最终工程结论。\n"

    bottlenecks = _top_energy_bottleneck_lines(analysis)
    hp = analysis.get("single_column_heat_pump_candidates") or []
    global_hp = analysis.get("global_heat_pump_candidates") or []
    hx = analysis.get("cross_column_heat_integration_candidates") or []
    priority_hx = analysis.get("temperature_feasible_cross_column_heat_integration_candidates") or []
    feasible_hx = analysis.get("feasible_cross_column_heat_integration_candidates") or []
    pending_hx = analysis.get("pending_quantification_cross_column_heat_integration_candidates") or []
    hybrid = analysis.get("hybrid_heat_integration_heat_pump_candidates") or []
    ranked = analysis.get("ranked_options") or []

    lines = ["## 热集成—热泵设计判断", warning.strip()]
    if bottlenecks:
        lines.append("**能耗瓶颈**\n" + "\n".join(bottlenecks))

    if intent == "out_of_scope":
        lines.append("\n该请求不属于本智能体范围。本智能体仅处理直接热集成、热泵及二者联合方案；压力调整、多效精馏、进料预热和背景过程 pinch 网络应转交对应智能体。")
        return "\n".join(x for x in lines if x)

    if intent == "hybrid_execution":
        return (
            "## 已识别为 P2 联合执行请求\n"
            "系统将根据 BO 最优参数重建独立 Aspen 会话，在同一流程副本中先运行 P1 HINT，再运行所含热泵子回路并联合核算节汽、电耗、冷却负荷与年净节省。"
        )
    if intent == "heat_pump_execution":
        return (
            "## 已识别为热泵执行请求\n"
            "若候选带有 preceding_heat_integration，必须先在同一复制流程中建立直接热集成并扣除共享热量，再运行热泵子回路；否则经济收益会重复计数。"
        )
    if intent == "hybrid_validation":
        return _render_hybrid_validation_reply(process_data, user_message, analysis)
    if intent == "heat_pump_validation":
        return _render_heat_pump_validation_reply(process_data, user_message, analysis)
    if intent == "direct_hi_validation":
        return _render_direct_heat_integration_validation_reply(process_data, user_message, analysis)

    if intent == "heat_pump":
        lines.append("\n### 直接判断：先热集成，再评价热泵")
        if priority_hx:
            lines.append("**P1：无压缩机直接热集成（类内 Top3）**")
            for opt in priority_hx[:TOP_RECOMMENDATIONS_PER_TIER]:
                lines.append(_one_line_option({**opt, "category": "cross_column_heat_integration"}))
            if pending_hx:
                lines.append("- 其中温度可行但热负荷缺失的方案仍保持 P1；必须先读取 Aspen HINT duty，相关 P3 热泵不得提前定案。")
        else:
            lines.append("- 当前没有满足 7 °C DTmin 的直接热集成候选，才可直接进入 P3 热泵筛选。")

        if hybrid:
            lines.append("\n**P2：热集成 + 热泵联合方案（类内 Top3）**")
            for opt in hybrid[:TOP_RECOMMENDATIONS_PER_TIER]:
                lines.append(_one_line_option(opt))
            lines.append("- 联合评分覆盖同一再沸器补热和不同再沸器协同供热，并扣除共享冷凝热/再沸需求。")

        if global_hp:
            lines.append("\n**P3：纯热泵候选（类内 Top3）**")
            for opt in global_hp[:TOP_RECOMMENDATIONS_PER_TIER]:
                lines.append(_one_line_option(opt))
            best = global_hp[0]
            if best.get("heat_integration_priority_applied"):
                pre = best.get("preceding_heat_integration") or {}
                lines.append(
                    f"- 当前热泵 Top1 已按 {pre.get('hot_source_column')}→{pre.get('cold_sink_column')} 热集成先行修正；"
                    f"原始分={_fmt(best.get('score_without_heat_integration'), 1)}，修正后={_fmt(best.get('score'), 1)}。"
                )
            lines.append("- 联合方案必须按 HI → residual ledger → HP 的顺序验证。")
        if hp:
            lines.append("\n**单塔 MVR / 闭式热泵参考**")
            lines.append(_one_line_option({**hp[0], "category": "heat_pump"}))
        if not priority_hx and not hybrid and not global_hp and not hp:
            lines.append("没有足够温度/负荷数据。至少需要每塔塔顶温度、塔釜温度、冷凝器负荷和再沸器负荷。")

    elif intent == "cross_column":
        lines.append("\n### 直接跨塔热集成")
        if hx:
            for opt in hx[:6]:
                lines.append(_one_line_option({**opt, "category": "cross_column_heat_integration"}))
            lines.append("\n所有满足 7 °C DTmin 的直接换热候选都进入 P1 类内排序；热负荷已量化者可扣减热量台账，负荷缺失者先用 Aspen HINT 定量，但不会降到热泵之后。")
        else:
            lines.append("没有发现可比较的塔顶冷凝热/溶剂回收塔底显热—再沸器窗口。")
        if hybrid:
            lines.append("\n**直接热集成后的热泵补充方案**")
            for opt in hybrid[:3]:
                lines.append(_one_line_option(opt))

    elif intent == "validation":
        lines.append("\n### Aspen 严格验证候选")
        lines.append(_render_tier_recommendations(analysis))
        lines.append("\n请明确指定‘验证 P1 TopN’、‘验证 P2 TopN’或‘验证 P3 TopN’；系统不会跨层自动替用户选择。")

    elif intent == "ranking":
        lines.append("\n### P1/P2/P3 类内 Top3")
        lines.append(_render_tier_recommendations(analysis))
        lines.append("\n三层不做数值混排；用户可明确选择‘验证 P1/P2/P3 TopN’。")

    else:
        lines.append("\n### P1/P2/P3 类内推荐")
        lines.append(_render_tier_recommendations(analysis))
        lines.append("\n默认调用链：P1 温度筛选 → P2 条件结构推荐 → 用户选层/TopN → HINT 定量 → 残余热源/热阱台账 → P2 严格重评分；P3 独立保留。")

    return "\n".join(x for x in lines if x)

# ============================================================
# RAG 接口

# ============================================================
# RAG 接口
# ============================================================

def retrieve_intensification_rag_context(
    query: str,
    process_data: dict,
    max_items: int = 5,
) -> str:
    """
    过程强化 RAG 检索接口。

    当前版本返回内置规则库，保证不接外部向量库也能工作。
    后续可以替换为：
    - 文献向量库检索；
    - 案例库检索；
    - Aspen 历史方案数据库；
    - 规则库 JSON/YAML 检索。
    """
    return BUILTIN_HEAT_PUMP_RAG_CONTEXT


# ============================================================
# Prompt 构建
# ============================================================

def build_intensification_system_prompt(rag_context: str = "") -> str:
    rag_rule = ""
    if rag_context:
        rag_rule = "\n9. 若提供了【RAG/规则库资料】，必须区分资料依据与 BO/Aspen 数据依据；资料不能覆盖数据包中的硬数据。"

    return f"""你是化工分离过程的热集成—热泵智能体，仅负责直接热集成、热泵/MVR及二者联合方案。

回答风格要求：
1. 先回答用户当前问题，不要每次机械输出完整报告。
2. 数字结论必须来自【确定性筛选结果】或【BO/Aspen 数据包】；没有数据就说缺失。
3. P1 无需压缩机的直接热集成、P2 热集成+热泵联合方案、P3 纯热泵必须分组展示。禁止跨层比较 score，也不得构造跨技术全局分数；每层独立推荐前三，并由用户指定某层 TopN 进入严格验证。
4. 温度满足 7 °C DTmin 的直接热集成即属于 P1。即使可回收热负荷尚未量化，也必须先做 Aspen HINT 验证；与其共享热源或再沸热阱的 P3 热泵只能作为待定备选。
5. 热泵必须使用热集成先行后的剩余物理热源、剩余再沸需求进行统一评分，并报告温升、COP、电耗和覆盖率。P2 类内分以其所含 P3 热泵分为基线，加入直接热集成增益后不得因评分口径不同而低于该 P3 子方案；这仍不是跨层全局分。
6. 联合方案必须区分“同一再沸器联合供热”和“不同再沸器协同供热”。HINT 前输出条件 P2 并标明共享资源；HINT 后才允许声称共享热源/共享热阱已定量扣除。执行命令“执行 P2 TopN”时必须在同一 BO 最优 Aspen 副本中先 HINT、后热泵，并统一核算联合经济性。
7. 对直接热集成必须同时检查塔顶冷凝热与溶剂回收塔底溶剂显热，并说明热源、热阱、7 °C DTmin 余量、匹配热量和剩余外部再沸负荷；不讨论压力调整、多效精馏、进料预热或背景过程 pinch 网络设计。
8. 结尾给 Aspen 验证动作或下一步数据需求；不要泛泛讨论。{rag_rule}

输出必须包含：直接判断、关键依据、热量台账/防重复计数、风险限制、下一步验证。
"""

def build_intensification_user_prompt(
    user_message: str,
    process_data: dict,
    deterministic_analysis: dict,
    rag_context: str = "",
    max_json_chars: int = DEFAULT_MAX_PROMPT_JSON_CHARS,
    chat_history: Optional[List[Dict[str, str]]] = None,
) -> str:
    context_summary = format_process_data_summary(process_data)
    analysis_json = json.dumps(deterministic_analysis, ensure_ascii=False, indent=2)
    data_json = json.dumps(process_data, ensure_ascii=False, indent=2)
    intent = classify_intensification_intent(user_message)
    recent = []
    for item in (chat_history or [])[-6:]:
        if isinstance(item, dict):
            role = item.get("role")
            content = str(item.get("content") or "")[:900]
            if role in {"user", "assistant"} and content:
                recent.append(f"{role}: {content}")

    blocks = [
        f"用户问题：{user_message}",
        f"识别到的回答焦点：{intent}",
        "【最近对话】",
        "\n".join(recent) if recent else "无",
        "【过程强化数据摘要】",
        context_summary,
        "【确定性筛选结果】",
        _truncate_text(analysis_json, max_json_chars),
        "【完整 BO/Aspen 结构化数据包】",
        _truncate_text(data_json, max_json_chars),
    ]
    if rag_context:
        blocks.extend(["【RAG/规则库资料】", rag_context])
    return "\n\n".join(blocks)


# ============================================================
# 本地回复与 LLM 调用
# ============================================================

def build_local_intensification_reply(process_data: dict, user_message: str = "") -> str:
    if not process_data:
        return "当前还没有 BO 优化后的过程强化数据包。请先完成阶段 2：建立分离过程。"
    analysis = build_intensification_analysis(process_data)
    return render_focused_intensification_reply(process_data, user_message, analysis)


async def generate_intensification_response(
    user_message: str,
    process_data: dict,
    api_key: str,
    base_url: str,
    model_name: str,
    enable_rag: bool = True,
    chat_history: Optional[List[Dict[str, str]]] = None,
    state_dict: Optional[dict] = None,
) -> str:
    """生成热集成—热泵智能体回复；先做确定性筛选，LLM 仅解释结果。"""
    if not process_data:
        return "当前还没有 BO 优化后的过程强化数据包。请先完成阶段 2：建立分离过程。"

    deterministic_analysis = build_intensification_analysis(process_data)
    local_report = render_focused_intensification_reply(process_data, user_message, deterministic_analysis)
    intent = classify_intensification_intent(user_message)
    rag_context = retrieve_intensification_rag_context(user_message, process_data) if enable_rag else ""

    # Aspen 验证 payload 必须保持确定性，不交给 LLM 重写。
    if intent in {"heat_pump_validation", "direct_hi_validation", "hybrid_validation"}:
        return local_report

    # 执行型请求原则上由 GUI 聊天入口异步 to_thread 处理；这里作为非 GUI 调用兜底。
    if intent == "hybrid_execution":
        execution = await asyncio.to_thread(execute_hybrid_validation_and_economic_optimization, state_dict or {}, user_message)
        return render_hybrid_execution_result(execution)
    if intent == "heat_pump_execution":
        execution = await asyncio.to_thread(execute_heat_pump_validation_and_economic_optimization, state_dict or {}, user_message)
        return render_heat_pump_execution_result(execution)

    if not api_key or not str(api_key).strip() or AsyncOpenAI is None:
        return local_report

    system_prompt = build_intensification_system_prompt(rag_context)
    user_prompt = build_intensification_user_prompt(
        user_message=user_message,
        process_data=process_data,
        deterministic_analysis=deterministic_analysis,
        rag_context=rag_context,
        chat_history=chat_history,
    )

    try:
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        response = await client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.15,
        )
        return response.choices[0].message.content
    except Exception as e:
        return (
            f"[强化智能体 LLM 响应失败] {str(e)}\n\n"
            "以下返回确定性聚焦筛选结果：\n\n"
            + local_report
        )


# ============================================================
# GUI 兼容接口
# ============================================================

def make_intensification_seed_message(process_data: dict) -> str:
    """阶段 2 完成后自动写入聊天框的初始消息。"""
    if not process_data:
        return "阶段 2 已结束，但没有收到结构化过程强化数据包。请查看阶段 2 日志。"

    status = process_data.get("status", "unknown")
    if status == "strictly_feasible":
        prefix = "✅ 阶段 2 帕累托最优解已锁定，BO 优化结果已传入过程强化智能体。"
    elif status == "closest_available":
        prefix = "⚠️ 阶段 2 未得到严格可行解，但已将最接近可行域的数据点传入过程强化智能体，后续节能判断需谨慎。"
    else:
        prefix = "⚠️ 阶段 2 已结束，过程强化数据包状态异常，请结合日志判断。"

    analysis = build_intensification_analysis(process_data)
    top_text = _render_tier_recommendations(analysis)

    return (
        prefix
        + "\n\n"
        + format_process_data_summary(process_data)
        + "\n\n【强化候选初筛】\n"
        + top_text
        + "\n\n可以直接提问：查看直接热集成优先排序、比较同一/不同再沸器的热集成+热泵联合方案，或生成 Aspen 验证步骤；输入“验证 P1 Top1”可输出直接热集成 payload，输入“验证 P2 Top1”可输出联合方案顺序 payload，输入“执行 P2 Top1”可在同一 Aspen 副本中启动 HINT+热泵联合验证，输入“验证 P3 Top1”可输出纯热泵参数。"
    )


async def chat_with_intensification_agent(
    user_message: str,
    chat_history: Optional[List[Dict[str, str]]],
    state_dict: Optional[dict],
    api_key: str,
    base_url: str,
    model_name: str,
):
    """
    Gradio 聊天入口。

    保持与 GUI 事件绑定兼容：
    inputs=[chat_input, chatbot, current_topology_state, api_key_input, base_url_input, model_input]
    outputs=[chatbot, chat_input]
    """
    if chat_history is None:
        chat_history = []

    msg = str(user_message or "").strip()
    if not msg:
        yield chat_history, ""
        return

    chat_history.append({"role": "user", "content": msg})
    yield chat_history, ""

    process_data = _get_process_data_from_state(state_dict)
    intent = classify_intensification_intent(msg)

    if intent == "hybrid_execution":
        chat_history.append({
            "role": "assistant",
            "content": "已进入 P2 执行分支：将根据 BO 最优参数重建 Aspen 流程，在同一副本中先运行 HINT，再扫描热泵 pressure_ratio / approach，并按联合年净节省选择最优。",
        })
        yield chat_history, ""
        execution = await asyncio.to_thread(execute_hybrid_validation_and_economic_optimization, state_dict or {}, msg)
        chat_history[-1] = {"role": "assistant", "content": render_hybrid_execution_result(execution)}
        yield chat_history, ""
        return

    if intent == "heat_pump_execution":
        chat_history.append({
            "role": "assistant",
            "content": "已进入执行分支：将根据 BO 最优参数重建 Aspen 流程，扫描 Top 热泵 pressure_ratio / approach，并按年净节省选择经济最优。",
        })
        yield chat_history, ""
        execution = await asyncio.to_thread(execute_heat_pump_validation_and_economic_optimization, state_dict or {}, msg)
        chat_history[-1] = {"role": "assistant", "content": render_heat_pump_execution_result(execution)}
        yield chat_history, ""
        return

    bot_reply = await generate_intensification_response(
        user_message=msg,
        process_data=process_data,
        api_key=api_key,
        base_url=base_url,
        model_name=model_name,
        enable_rag=True,
        chat_history=chat_history,
        state_dict=state_dict,
    )

    chat_history.append({"role": "assistant", "content": bot_reply})
    yield chat_history, ""


# ============================================================
# 可选 CLI 调试入口
# ============================================================

if __name__ == "__main__":
    # 用于快速语法和本地函数测试。实际运行由 GUI_APP.py 调用。
    demo_data = {
        "status": "strictly_feasible",
        "objective": {
            "total_reboiler_kw_abs": 1000.0,
            "total_condenser_kw_abs": 950.0,
        },
        "columns": [
            {
                "id": "C1",
                "role": "ordinary",
                "settings": {"n_stages": 30, "reflux_ratio": 2.0, "distillate_flow": 40, "feed_stages": {"FEED": 15}},
                "temperatures": {"top": {"C": 65}, "bottom": {"C": 95}, "feed_streams": {"FEED": {"C": 30}}},
                "duties": {"reboiler_kw_abs": 500, "condenser_kw_abs": 480},
            },
            {
                "id": "C2",
                "role": "extractive",
                "settings": {"n_stages": 40, "reflux_ratio": 3.0, "distillate_flow": 30, "feed_stages": {"C1B": 22}},
                "temperatures": {"top": {"C": 78}, "bottom": {"C": 120}, "feed_streams": {"C1B": {"C": 85}}},
                "duties": {"reboiler_kw_abs": 500, "condenser_kw_abs": 470},
            },
        ],
    }
    print(build_local_intensification_reply(demo_data))
