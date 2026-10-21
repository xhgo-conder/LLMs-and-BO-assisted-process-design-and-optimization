# -*- coding: utf-8 -*-
"""
process_separation_retrofit_agent.py

分离强化设计智能体模块。

定位：插入在“基础 ED 流程设计 / BO 优化”之后、“过程强化设计
（热泵、热集成、MVR 等换热强化）”之前。

设计原则：
1. 不改变 Stage 1/Stage 2 已生成的基础 ED 拓扑；
2. 只基于 ED_best 的 Aspen/BO 结构化结果生成分离改造支线；
3. 分离改造支线只负责变压与膜类方案，不负责热泵/热集成；
4. 每一个新分离方案必须输出统一的 process_data，才能继续进入后续换热强化模块；
5. 当前版本暂停 PSD 严格拓扑重建；EPSD 保留 ED 拓扑，并以 ED_best 为锚点联合优化原 ED 变量与 Flash 批准的分离塔压力；溶剂回收塔压力暂时固定；
6. 膜支线按安装位置拆分为产品精制、溶剂循环精制、塔间流股预处理与共沸对旁路膜验证；
7. 真正 Aspen 改写/严格验证在下一步细化。

GUI 兼容接口：
- make_separation_seed_message(process_data)
- chat_with_separation_retrofit_agent(user_message, chat_history, state_dict, api_key, base_url, model_name)
- build_separation_retrofit_analysis(process_data, state_dict=None)
- render_separation_retrofit_report(process_data, analysis=None)
- get_active_separation_process_data(state_dict)
- format_separation_retrofit_data(state_dict)
"""

from __future__ import annotations

import asyncio
import copy
import csv
import json
import math
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from openai import AsyncOpenAI
except Exception:  # 允许无 openai 包时仍能导入 GUI
    AsyncOpenAI = None

try:
    # PSD topology generation is now integrated into main_agent.py.
    # Do not require a separate psd_pressure_swing_topology_agent.py file.
    from main_agent import (
        PSD_TOPOLOGY_AGENT_VERSION,
        build_psd_topology_from_flash,
        build_psd_topology_report,
    )
except Exception:
    PSD_TOPOLOGY_AGENT_VERSION = "psd-pressure-swing-topology-agent-unavailable"
    build_psd_topology_from_flash = None
    build_psd_topology_report = None

AGENT_VERSION = "separation-retrofit-agent-v32-directional-cut-gate"
DEFAULT_MAX_PROMPT_JSON_CHARS = 18000

# GUI Flash2 screening defaults.  Nine *real* composition samples are sent to
# Aspen for every pressure curve; dense interpolation below is presentation-only.
GUI_FLASH_PRESSURE_GRID_KPA = [30.0, 50.0, 70.0, 100.0, 300.0, 900.0]
GUI_FLASH_X_GRID = [0.05, 0.10, 0.18, 0.30, 0.45, 0.60, 0.75, 0.85, 0.93]
GUI_FLASH_SOLVENT_TO_FEED_GRID = [0.5, 1.0, 2.0]
GUI_FLASH_VAPOR_FRACTION_GRID = [0.50]
GUI_FLASH_GAUSSIAN_SIGMA = 3.0
GUI_FLASH_DENSE_PLOT_POINTS = 720


# ============================================================
# 基础工具
# ============================================================

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


def _fmt(value: Any, ndigits: int = 2, unit: str = "") -> str:
    val = _safe_float(value, None)
    if val is None:
        return "NA"
    s = f"{val:.{ndigits}f}"
    return f"{s} {unit}".strip()


def _pct(value: Any, ndigits: int = 2) -> str:
    val = _safe_float(value, None)
    if val is None:
        return "NA"
    return f"{100.0 * val:.{ndigits}f}%"


def _safe_json_dumps(obj: Any, indent: int = 2) -> str:
    def default(o: Any) -> str:
        try:
            if hasattr(o, "item"):
                return o.item()
        except Exception:
            pass
        return str(o)

    try:
        return json.dumps(obj, ensure_ascii=False, indent=indent, default=default)
    except Exception:
        return str(obj)


def _truncate_text(text: str, max_chars: int = DEFAULT_MAX_PROMPT_JSON_CHARS) -> str:
    if not text or len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...【数据过长，已截断；请优先依据确定性候选和验证等级判断】"


def _clean_id(x: Any) -> str:
    return str(x if x is not None else "").strip().upper()


# ============================================================
# 分离方案短 ID 与兼容别名
# ============================================================
# 页面展示和用户输入统一使用短 ID：PSD1 / EPSD1 / MEM1...
# 旧版长 ID 仍作为输入别名兼容，避免历史 state 或旧对话无法选择。
def _normalize_case_id(value: Any) -> str:
    raw = str(value if value is not None else "").strip()
    if not raw:
        return ""
    up = re.sub(r"[^A-Z0-9]+", "_", raw.upper()).strip("_")

    if up in {"ED", "ED_BEST", "BASE", "BASE_CASE", "BASIC", "基准"}:
        return "ED_best"

    if up in {"PSD", "PSD1", "PSD_SOLVENT_FREE", "SOLVENT_FREE_PSD"} or up.startswith("PSD_SOLVENT_FREE_CANDIDATE") or up.startswith("PSD_CANDIDATE"):
        return "PSD1"

    if up in {"EPSD", "PED", "EPSD1", "PED1"} or up.startswith("EPSD_CANDIDATE") or up.startswith("PED_CANDIDATE"):
        return "EPSD1"

    mem_aliases = {
        "MEM": "MEM1",
        "MEM1": "MEM1",
        "MEM_PRODUCT": "MEM1",
        "MEM_CANDIDATE_PRODUCT_001": "MEM1",
        "MEM2": "MEM2",
        "MEM_RECYCLE": "MEM2",
        "MEM_CANDIDATE_RECYCLE_001": "MEM2",
        "MEM3": "MEM3",
        "MEM_INTERCOLUMN": "MEM3",
        "MEM_CANDIDATE_INTERCOLUMN_001": "MEM3",
        "MEM4": "MEM4",
        "MEM_AZEOTROPE_BYPASS": "MEM4",
        "MEM_CANDIDATE_AZEOTROPE_BYPASS_001": "MEM4",
    }
    if up in mem_aliases:
        return mem_aliases[up]
    if up.startswith("MEM_CANDIDATE_PRODUCT"):
        return "MEM1"
    if up.startswith("MEM_CANDIDATE_RECYCLE"):
        return "MEM2"
    if up.startswith("MEM_CANDIDATE_INTERCOLUMN"):
        return "MEM3"
    if up.startswith("MEM_CANDIDATE_AZEOTROPE"):
        return "MEM4"

    return raw


def _case_id_aliases(case_id: Any) -> List[str]:
    cid = str(case_id or "").strip()
    norm = _normalize_case_id(cid)
    aliases = {cid, norm}
    if norm == "PSD1":
        aliases.update({"PSD", "PSD1", "PSD_solvent_free", "solvent_free_PSD"})
    elif norm == "EPSD1":
        aliases.update({"EPSD", "PED", "EPSD1", "PED1"})
    elif norm == "MEM1":
        aliases.update({"MEM", "MEM_product", "MEM1"})
    elif norm == "MEM2":
        aliases.update({"MEM_recycle", "MEM2"})
    elif norm == "MEM3":
        aliases.update({"MEM_intercolumn", "MEM3"})
    elif norm == "MEM4":
        aliases.update({"MEM_azeotrope_bypass", "MEM4"})
    elif norm == "ED_best":
        aliases.update({"ED", "ED_best", "base", "基准"})
    return [a for a in aliases if str(a or "").strip()]


def _temperature_c(pack: Any) -> Optional[float]:
    if isinstance(pack, dict):
        if pack.get("C") is not None:
            return _safe_float(pack.get("C"))
        if pack.get("K") is not None:
            k = _safe_float(pack.get("K"))
            return None if k is None else k - 273.15
        raw = _safe_float(pack.get("raw"))
        if raw is None:
            return None
        return raw - 273.15 if raw > 200.0 else raw
    return _safe_float(pack)


def _column_id(col: dict) -> str:
    return _clean_id(col.get("id") or col.get("column"))


def _stream_id(stream: dict) -> str:
    return _clean_id(stream.get("id") or stream.get("stream"))


def _col_top_temp_c(col: dict) -> Optional[float]:
    return _temperature_c(_as_dict(col.get("temperatures")).get("top"))


def _col_bottom_temp_c(col: dict) -> Optional[float]:
    return _temperature_c(_as_dict(col.get("temperatures")).get("bottom"))


def _col_q_reb_kw(col: dict) -> Optional[float]:
    return _safe_float(_as_dict(col.get("duties")).get("reboiler_kw_abs"))


def _col_q_cond_kw(col: dict) -> Optional[float]:
    return _safe_float(_as_dict(col.get("duties")).get("condenser_kw_abs"))


def _component_names_from_state(state_dict: Optional[dict]) -> List[str]:
    state_dict = _as_dict(state_dict)
    comps = state_dict.get("components") or state_dict.get("component_ids") or []
    if isinstance(comps, dict):
        return [_clean_id(v) for v in comps.values() if v]
    return [_clean_id(x) for x in _as_list(comps) if x]


def _solvent_from_state(state_dict: Optional[dict]) -> str:
    return _clean_id(_as_dict(state_dict).get("solvent"))


def _non_solvent_components(state_dict: Optional[dict]) -> List[str]:
    comps = _component_names_from_state(state_dict)
    solvent = _solvent_from_state(state_dict)
    return [c for c in comps if c and c != solvent]


def _component_pairs(components: List[str]) -> List[List[str]]:
    comps = []
    for c in components:
        c = _clean_id(c)
        if c and c not in comps:
            comps.append(c)
    pairs = []
    for i in range(len(comps)):
        for j in range(i + 1, len(comps)):
            pairs.append([comps[i], comps[j]])
    return pairs


def _default_pressure_grid_kpa(process_family: str = "psd", state_dict: Optional[dict] = None) -> List[float]:
    """Return a generic pressure grid for Flash/PSD screening.

    泛化原则：优先读取用户/state 配置，不把某个物系的压力窗口写死到代码里。
    支持的覆盖字段：
    - state_dict["pressure_grid_kpa"]
    - state_dict["separation_pressure_grid_kpa"]
    - state_dict["separation_retrofit"]["pressure_grid_kpa"]
    - state_dict["pressure_swing_flash_config"]["pressure_grid_kpa"]
    """
    state = _as_dict(state_dict)
    pkg = _as_dict(state.get("separation_retrofit"))
    cfg = _as_dict(state.get("pressure_swing_flash_config"))
    candidates = [
        state.get("pressure_grid_kpa"),
        state.get("separation_pressure_grid_kpa"),
        pkg.get("pressure_grid_kpa"),
        cfg.get("pressure_grid_kpa"),
    ]
    for raw in candidates:
        vals = []
        for x in _as_list(raw):
            v = _safe_float(x, None)
            if v is not None and v > 0:
                vals.append(float(v))
        vals = sorted(dict.fromkeys(vals))
        if len(vals) >= 2:
            return vals

    family = str(process_family or "").lower()
    if family in {"psd", "solvent_free_psd"}:
        return [30.0, 50.0, 70.0, 100.0, 300.0, 600.0, 900.0]
    return [30.0, 50.0, 70.0, 100.0, 300.0, 600.0, 900.0]


def _pressure_grid_atm(grid_kpa: List[float]) -> List[float]:
    return [round(float(p) / 101.325, 4) for p in grid_kpa]


def _augment_pressure_grid_with_risk_bases(
        grid_kpa: List[float],
        risk_plan: Optional[dict],
        state_dict: Optional[dict] = None,
) -> List[float]:
    """Add vacuum points and unrepresented ED_best source-column pressures.

    All values are positive absolute pressures.  The term ``negative pressure``
    in user-facing text is interpreted as sub-atmospheric/vacuum operation.
    """
    state = _as_dict(state_dict)
    cfg = _as_dict(state.get("pressure_swing_flash_config"))
    include_vacuum = bool(cfg.get("include_vacuum_pressure_screening", state.get("include_vacuum_pressure_screening", True)))
    floor = _safe_float(cfg.get("minimum_absolute_pressure_kpa", state.get("minimum_absolute_pressure_kpa", 20.0)), 20.0) or 20.0
    vals = [float(v) for v in grid_kpa if _safe_float(v, None) is not None and float(v) >= float(floor)]
    if include_vacuum:
        vacuum = cfg.get("vacuum_pressure_grid_kpa") or state.get("vacuum_pressure_grid_kpa") or [30.0, 50.0, 70.0]
        vals.extend(float(v) for v in _as_list(vacuum) if _safe_float(v, None) is not None and float(floor) <= float(v) < 101.325)
    for ctx in _as_dict(_as_dict(risk_plan).get("pair_contexts")).values():
        p = _safe_float(_as_dict(ctx).get("reference_base_pressure_kpa"), None)
        if p is None or p < float(floor):
            continue
        if vals and min(abs(float(v) - p) / max(abs(p), 1e-12) for v in vals) <= 0.03:
            continue
        vals.append(float(p))
    return sorted(dict.fromkeys(round(float(v), 6) for v in vals))


def get_ed_process_data(state_dict: Optional[dict]) -> dict:
    """读取 Stage 2 ED 基准优化结果。"""
    state_dict = _as_dict(state_dict)
    return _as_dict(state_dict.get("bo_optimization"))


def get_separation_package(state_dict: Optional[dict]) -> dict:
    state_dict = _as_dict(state_dict)
    return _as_dict(state_dict.get("separation_retrofit"))


def get_active_separation_case(state_dict: Optional[dict]) -> dict:
    """返回当前选中的分离方案；如果没有选择，默认 ED_best。"""
    state_dict = _as_dict(state_dict)
    package = get_separation_package(state_dict)
    active_id = str(package.get("active_case_id") or "ED_best")
    for case in _as_list(package.get("cases")):
        if isinstance(case, dict) and str(case.get("case_id")) == active_id:
            return case

    ed_data = get_ed_process_data(state_dict)
    if ed_data:
        return {
            "case_id": "ED_best",
            "family": "ED",
            "verification_level": "aspen_strict" if ed_data.get("status") == "strictly_feasible" else "aspen_available",
            "process_data": ed_data,
            "metrics": _extract_case_metrics(ed_data),
        }
    return {}


def get_active_separation_process_data(state_dict: Optional[dict]) -> dict:
    case = get_active_separation_case(state_dict)
    process_data = _as_dict(case.get("process_data"))
    # Candidate stubs are not valid thermal-intensification inputs.
    if process_data and not process_data.get("source_process_ref") and process_data.get("columns"):
        return process_data
    if str(case.get("case_id") or "") == "ED_best":
        return get_ed_process_data(state_dict)
    # Unverified PSD/EPSD/MEM candidates should not overwrite active process_data.
    # Thermal page will still fall back to ED unless a validator later stores a full process_data.
    return get_ed_process_data(state_dict)


# ============================================================
# ED 数据摘要与约束边界识别
# ============================================================

def _extract_case_metrics(process_data: dict) -> dict:
    process_data = _as_dict(process_data)
    objective = _as_dict(process_data.get("objective"))
    metrics = {
        "status": process_data.get("status"),
        "tac": objective.get("tac") or process_data.get("tac") or process_data.get("best_tac"),
        "emissions": objective.get("emissions") or process_data.get("emissions"),
        "total_reboiler_kw": None,
        "total_condenser_kw": None,
        "solvent_recycle_purity": None,
        "all_products_feasible": process_data.get("status") == "strictly_feasible",
    }
    reb, cond = 0.0, 0.0
    has_reb, has_cond = False, False
    for col in _as_list(process_data.get("columns")):
        if not isinstance(col, dict):
            continue
        qreb = _col_q_reb_kw(col)
        qcond = _col_q_cond_kw(col)
        if qreb is not None:
            reb += abs(qreb)
            has_reb = True
        if qcond is not None:
            cond += abs(qcond)
            has_cond = True
    metrics["total_reboiler_kw"] = reb if has_reb else None
    metrics["total_condenser_kw"] = cond if has_cond else None

    # 尽量从 purities / product_metrics / constraints 中识别溶剂回收纯度。
    for key in ("purities", "product_purities", "stream_purities"):
        pur = _as_dict(process_data.get(key))
        for k, v in pur.items():
            uk = str(k).upper()
            if any(tok in uk for tok in ["SOLV", "SOLVENT", "EXTRACT", "EXTRACTANT", "RECYCLE", "RECYC"]):
                val = _safe_float(v)
                if val is not None:
                    metrics["solvent_recycle_purity"] = val
                    break
        if metrics["solvent_recycle_purity"] is not None:
            break
    return metrics


def _first_metric_number(*values) -> Optional[float]:
    for v in values:
        fv = _safe_float(v, None)
        if fv is not None:
            return float(fv)
    return None


def _baseline_tac_from_process_data(process_data: dict) -> Optional[float]:
    pd = _as_dict(process_data)
    obj = _as_dict(pd.get("objective"))
    cost = _as_dict(pd.get("cost_breakdown"))
    metrics = _as_dict(pd.get("metrics"))
    return _first_metric_number(
        obj.get("tac_usd_per_year"), obj.get("tac"), obj.get("TAC"),
        pd.get("tac_usd_per_year"), pd.get("tac"), pd.get("best_tac"),
        cost.get("tac_usd_per_year"), metrics.get("tac"), metrics.get("tac_usd_per_year"),
    )


def _baseline_emissions_from_process_data(process_data: dict) -> Optional[float]:
    pd = _as_dict(process_data)
    obj = _as_dict(pd.get("objective"))
    cost = _as_dict(pd.get("cost_breakdown"))
    metrics = _as_dict(pd.get("metrics"))
    return _first_metric_number(
        obj.get("emissions_kt_co2_per_year"), obj.get("emissions"), obj.get("GE"),
        pd.get("emissions_kt_co2_per_year"), pd.get("emissions"),
        cost.get("emissions_kt_co2_per_year"), metrics.get("emissions"), metrics.get("emissions_kt_co2_per_year"),
    )


def _make_epsd_engineering_guard(state_dict: dict, anchor_payload: Optional[dict] = None) -> dict:
    """Build the ED_best comparison policy for anchored EPSD joint BO.

    The saved ED result is used only as an audit reference. BO_optimizer must
    rebuild and evaluate the same anchor in the new Aspen session before any
    EPSD candidate is accepted.
    """
    state = _as_dict(state_dict)
    baseline_pd = copy.deepcopy(get_ed_process_data(state))
    baseline_metrics = _extract_case_metrics(baseline_pd)
    tac = _baseline_tac_from_process_data(baseline_pd) or _safe_float(baseline_metrics.get("tac"), None)
    emis = _baseline_emissions_from_process_data(baseline_pd) or _safe_float(baseline_metrics.get("emissions"), None)
    objective = _as_dict(baseline_pd.get("objective"))
    base_reb = _first_metric_number(
        objective.get("total_reboiler_kw_abs"),
        baseline_pd.get("total_reboiler_kw_abs"),
        _as_dict(baseline_pd.get("energy_summary")).get("total_reboiler_kw_abs"),
    )
    policy = _as_dict(state.get("epsd_engineering_guard"))
    return {
        "enabled": bool(policy.get("enabled", True)),
        "mode": "anchored_joint_bo_compare_to_rebuilt_ED_best",
        "baseline_case_id": "ED_best",
        "baseline_tac_usd_per_year": tac,
        "baseline_emissions_kt_co2_per_year": emis,
        "baseline_total_reboiler_kw_abs": base_reb,
        "baseline_metrics": baseline_metrics,
        "baseline_process_data": baseline_pd,
        "anchor_payload": copy.deepcopy(_as_dict(anchor_payload)),
        "anchor_reproduction_tolerance": float(policy.get("anchor_reproduction_tolerance", state.get("epsd_anchor_reproduction_tolerance", 0.02)) or 0.02),
        "min_tac_improvement_ratio": float(policy.get("min_tac_improvement_ratio", state.get("epsd_min_tac_improvement_ratio", 0.015)) or 0.015),
        "min_energy_improvement_ratio": float(policy.get("min_energy_improvement_ratio", state.get("epsd_min_energy_improvement_ratio", 0.03)) or 0.03),
        "accept_tac_worsening_ratio": float(policy.get("accept_tac_worsening_ratio", state.get("epsd_accept_tac_worsening_ratio", 0.0)) or 0.0),
        "accept_emissions_worsening_ratio": float(policy.get("accept_emissions_worsening_ratio", state.get("epsd_accept_emissions_worsening_ratio", 0.01)) or 0.01),
        "tradeoff_tac_worsening_ratio": float(policy.get("tradeoff_tac_worsening_ratio", state.get("epsd_tradeoff_tac_worsening_ratio", 0.02)) or 0.02),
        "tradeoff_emissions_worsening_ratio": float(policy.get("tradeoff_emissions_worsening_ratio", state.get("epsd_tradeoff_emissions_worsening_ratio", 0.02)) or 0.02),
    }


def _norm_bo_key_for_epsd(value: Any) -> str:
    return str(value or "").strip().upper()


def _iter_nested_dicts_for_epsd(obj: Any, max_depth: int = 4):
    """Yield shallow nested dicts likely to contain optimizer outputs."""
    seen = set()
    stack = [(obj, 0)]
    while stack:
        cur, depth = stack.pop()
        if not isinstance(cur, dict) or id(cur) in seen or depth > max_depth:
            continue
        seen.add(id(cur))
        yield cur
        for val in cur.values():
            if isinstance(val, dict):
                stack.append((val, depth + 1))


def _extract_epsd_anchor_params(state_dict: dict, topology: dict, bo_vars: dict, approved_pressure_keys: Optional[set] = None) -> dict:
    """Extract a complete ED_best anchor without midpoint substitution.

    Every original ED decision variable must be recovered from ED_best. Missing
    non-pressure variables block EPSD BO. Approved pressure variables may use the
    explicit ED/base pressure when the original ED search did not include pressure.
    """
    state = _as_dict(state_dict)
    topology = _as_dict(topology)
    bo_vars = _as_dict(bo_vars)
    approved_pressure_keys = {str(k).upper() for k in (approved_pressure_keys or set())}
    collected: Dict[str, float] = {}
    sources: Dict[str, str] = {}
    preferred_keys = (
        "best_params", "variables", "decision_variables", "x_best", "best_x",
        "best_variables", "selected_params", "selected_variables",
    )
    holders = [state]
    for key in (
        "bo_optimization", "process_data", "process_intensification_data", "stage3_result",
        "optimization_result", "bo_result", "bo_results", "final_bo_result", "best_bo_result",
        "bayes_opt_result", "final_result", "ED_best",
    ):
        val = state.get(key)
        if isinstance(val, dict):
            holders.append(val)
    package = _as_dict(state.get("separation_retrofit"))
    for case in _as_list(package.get("cases") or package.get("candidate_cases") or package.get("ranked_candidates")):
        if isinstance(case, dict) and _normalize_case_id(case.get("case_id")) == "ED_best":
            holders.append(case)
            if isinstance(case.get("process_data"), dict):
                holders.append(case.get("process_data"))

    for holder in holders:
        for d in _iter_nested_dicts_for_epsd(holder):
            for key in preferred_keys:
                val = d.get(key)
                if isinstance(val, dict):
                    for k, v in val.items():
                        kk = _norm_bo_key_for_epsd(k)
                        fv = _safe_float(v, None)
                        if kk and fv is not None:
                            collected[kk] = float(fv)
                            sources[kk] = f"{key}"

    def _column_rows(obj: Any):
        for d in _iter_nested_dicts_for_epsd(obj):
            cols = d.get("columns")
            if isinstance(cols, list):
                for col in cols:
                    if isinstance(col, dict):
                        yield col

    for holder in holders:
        for col in _column_rows(holder):
            cid = _norm_bo_key_for_epsd(col.get("id") or col.get("column") or col.get("name"))
            if not cid:
                continue
            settings = _as_dict(col.get("settings"))
            mapping = {
                "N": (settings.get("n_stages"), col.get("n_stages"), col.get("stages"), col.get("trays"), col.get("N"), col.get("NSTAGE")),
                "RR": (settings.get("reflux_ratio"), col.get("reflux_ratio"), col.get("rr"), col.get("RR"), col.get("BASIS_RR")),
                "D_FLOW": (settings.get("distillate_flow"), col.get("distillate_flow"), col.get("D_FLOW"), col.get("basis_d"), col.get("BASIS_D")),
                "PRESSURE": (settings.get("pressure_atm"), settings.get("pressure"), col.get("pressure_atm"), col.get("pressure")),
            }
            for suffix, values in mapping.items():
                for raw in values:
                    fv = _safe_float(raw, None)
                    if fv is not None:
                        key = f"{cid}_{suffix}"
                        collected.setdefault(key, float(fv))
                        sources.setdefault(key, "columns.settings")
                        break
            n = collected.get(f"{cid}_N")
            feed_stages = _as_dict(settings.get("feed_stages"))
            if n and n > 2 and feed_stages:
                feed_items = list(feed_stages.items())
                main_stages = [(_clean_id(k), _safe_float(v, None)) for k, v in feed_items if "SOLV" not in _clean_id(k)]
                solv_stages = [(_clean_id(k), _safe_float(v, None)) for k, v in feed_items if "SOLV" in _clean_id(k)]
                if main_stages and main_stages[0][1] is not None:
                    collected.setdefault(f"{cid}_FEED_NORM", max(0.02, min(0.98, float(main_stages[0][1]) / float(n))))
                    sources.setdefault(f"{cid}_FEED_NORM", "columns.settings.feed_stages")
                if solv_stages and solv_stages[0][1] is not None:
                    collected.setdefault(f"{cid}_SOLV_NORM", max(0.02, min(0.98, float(solv_stages[0][1]) / float(n))))
                    sources.setdefault(f"{cid}_SOLV_NORM", "columns.settings.feed_stages")

    # Explicit base pressure is allowed only for newly approved pressure variables.
    flash_opt = _flash_pressure_optimization_from_state(state)
    base_pressure_kpa = _safe_float(flash_opt.get("base_pressure_kpa"), 101.325) or 101.325
    base_pressure_atm = float(base_pressure_kpa) / 101.325
    for key in approved_pressure_keys:
        if key not in collected:
            collected[key] = base_pressure_atm
            sources[key] = "flash_base_pressure"

    required = []
    for raw_key in bo_vars.keys():
        key = _norm_bo_key_for_epsd(raw_key)
        if not key:
            continue
        if key.endswith("_PRESSURE") or key.endswith("_PRES"):
            if key in approved_pressure_keys:
                required.append(key)
        else:
            required.append(key)
    for key in sorted(approved_pressure_keys):
        if key not in required:
            required.append(key)
    missing = [k for k in required if k not in collected]
    params = {k: float(collected[k]) for k in required if k in collected}
    for key in list(params):
        if key.endswith("_N"):
            params[key] = float(int(round(params[key])))
    return {
        "params": params,
        "missing": missing,
        "sources": {k: sources.get(k, "unknown") for k in params},
        "required_keys": required,
    }


def _epsd_local_bounds(anchor: float, original_bounds: Any, key: str, policy: dict) -> List[float]:
    if not isinstance(original_bounds, (list, tuple)) or len(original_bounds) != 2:
        raise ValueError(f"EPSD 局部边界缺少原始范围: {key}={original_bounds}")
    lo = _safe_float(original_bounds[0], None)
    hi = _safe_float(original_bounds[1], None)
    if lo is None or hi is None or lo >= hi:
        raise ValueError(f"EPSD 局部边界无效: {key}={original_bounds}")
    a = float(anchor)
    ku = str(key).upper()
    if ku.endswith("_N"):
        low = max(lo, math.floor(a * float(policy.get("n_low_ratio", 0.85))))
        high = min(hi, math.ceil(a * float(policy.get("n_high_ratio", 1.20))))
        if high - low < 2:
            low = max(lo, math.floor(a - 2))
            high = min(hi, math.ceil(a + 2))
    elif ku.endswith("_RR"):
        low = max(lo, a * float(policy.get("rr_low_ratio", 0.75)))
        high = min(hi, a * float(policy.get("rr_high_ratio", 1.30)))
    elif ku.endswith("_D_FLOW"):
        low = max(lo, a * float(policy.get("d_low_ratio", 0.85)))
        high = min(hi, a * float(policy.get("d_high_ratio", 1.15)))
    elif ku.endswith("_FEED_NORM") or ku.endswith("_SOLV_NORM"):
        delta = float(policy.get("stage_norm_delta", 0.12))
        low = max(lo, a - delta)
        high = min(hi, a + delta)
    elif "FLOW" in ku and "SOLV" in ku:
        low = max(lo, a * float(policy.get("solvent_flow_low_ratio", 0.70)))
        high = min(hi, a * float(policy.get("solvent_flow_high_ratio", 1.30)))
    else:
        ratio = float(policy.get("other_relative_span", 0.20))
        span = max(abs(a) * ratio, (hi - lo) * 0.05)
        low = max(lo, a - span)
        high = min(hi, a + span)
    if high <= low:
        raise ValueError(f"EPSD 锚点局部范围退化: {key}, anchor={a}, original={original_bounds}, local={[low, high]}")
    return [float(low), float(high)]


def _build_epsd_anchor_joint_space(state_dict: dict, topology: dict, base_bo_vars: dict, case_vars: dict) -> dict:
    approved_pressure_keys = {str(k).upper() for k in case_vars if str(k).upper().endswith("_PRESSURE")}
    if not approved_pressure_keys:
        raise ValueError("EPSD 候选没有通过 Flash 材料性筛选的塔压变量。")
    anchor_info = _extract_epsd_anchor_params(state_dict, topology, base_bo_vars, approved_pressure_keys)
    if anchor_info.get("missing"):
        raise ValueError(
            "ED_best 锚点参数不完整，禁止用区间中点代替。缺失变量: "
            + ", ".join(anchor_info.get("missing") or [])
        )
    anchor_params = _as_dict(anchor_info.get("params"))
    policy = _as_dict(_as_dict(state_dict).get("epsd_anchor_policy"))
    local_vars = {}
    for raw_key, original in _as_dict(base_bo_vars).items():
        key = _norm_bo_key_for_epsd(raw_key)
        if not key or key.endswith("_PRESSURE") or key.endswith("_PRES"):
            continue
        local_vars[key] = _epsd_local_bounds(anchor_params[key], original, key, policy)
    for raw_key, bounds in _as_dict(case_vars).items():
        key = _norm_bo_key_for_epsd(raw_key)
        if key in approved_pressure_keys:
            local_vars[key] = [float(bounds[0]), float(bounds[1])]
    return {
        "bo_variables": local_vars,
        "anchor_params": anchor_params,
        "anchor_sources": anchor_info.get("sources") or {},
        "approved_pressure_keys": sorted(approved_pressure_keys),
        "policy": {
            "mode": "ED_best_anchored_local_joint_optimization",
            "all_original_ed_variables_enabled": True,
            "only_flash_approved_pressure_variables_enabled": True,
            "solvent_recovery_pressure_fixed_unless_explicitly_approved": True,
            "trust_region": {
                "N": [policy.get("n_low_ratio", 0.85), policy.get("n_high_ratio", 1.20)],
                "RR": [policy.get("rr_low_ratio", 0.75), policy.get("rr_high_ratio", 1.30)],
                "D_FLOW": [policy.get("d_low_ratio", 0.85), policy.get("d_high_ratio", 1.15)],
                "FEED/SOLV_NORM_delta": policy.get("stage_norm_delta", 0.12),
                "SOLV_FLOW": [policy.get("solvent_flow_low_ratio", 0.70), policy.get("solvent_flow_high_ratio", 1.30)],
            },
        },
    }


def _light_process_stub(process_data: dict, *, retrofit_family: str = "", status: str = "candidate") -> dict:
    """Small process_data placeholder for unverified retrofit candidates.

    Do not copy the full ED/BO process_data into every candidate; large BO
    histories and matrices can freeze Gradio after Stage 2.
    """
    process_data = _as_dict(process_data)
    return {
        "status": status,
        "retrofit_parent": "ED_best",
        "retrofit_family": retrofit_family,
        "source_process_ref": "state_dict.bo_optimization",
        "column_count": len(_as_list(process_data.get("columns"))),
        "stream_count": len(_as_list(process_data.get("streams"))),
        "metrics": _extract_case_metrics(process_data),
        "note": "candidate stub only; strict Aspen/shortcut validation must create full process_data before thermal intensification",
    }


def _compact_case_for_display(case: dict, *, include_payload: bool = True) -> dict:
    """Remove large embedded process_data from display JSON."""
    case = _as_dict(case)
    out = {}
    for k, v in case.items():
        if k == "process_data":
            pd = _as_dict(v)
            out["process_data_summary"] = {
                "status": pd.get("status"),
                "retrofit_family": pd.get("retrofit_family"),
                "column_count": len(_as_list(pd.get("columns"))) if pd.get("columns") is not None else pd.get("column_count"),
                "stream_count": len(_as_list(pd.get("streams"))) if pd.get("streams") is not None else pd.get("stream_count"),
                "source_process_ref": pd.get("source_process_ref"),
            }
        elif k == "verification_payload" and include_payload:
            out[k] = _compact_payload_for_display(v)
        elif k == "verification_payload":
            out["verification_payload_summary"] = _payload_summary(v)
        else:
            out[k] = v
    return out


def _payload_summary(payload: Any) -> dict:
    payload = _as_dict(payload)
    if not payload:
        return {}
    flash_cases = _as_list(payload.get("flash_cases"))
    nested = _as_dict(payload.get("flash_screen"))
    nested_cases = _as_list(nested.get("flash_cases"))
    return {
        "action": payload.get("action"),
        "mode": payload.get("mode") or nested.get("mode"),
        "flash_case_count": payload.get("flash_case_count") or nested.get("flash_case_count") or len(flash_cases) or len(nested_cases),
        "pressure_grid_kpa": payload.get("pressure_grid_kpa") or nested.get("pressure_grid_kpa"),
        "components": payload.get("components") or nested.get("components"),
        "solvent": payload.get("solvent") or nested.get("solvent"),
    }


def _compact_payload_for_display(payload: Any, max_cases: int = 6) -> Any:
    payload = _as_dict(payload)
    if not payload:
        return payload
    out = dict(payload)
    if "flash_cases" in out:
        cases = _as_list(out.get("flash_cases"))
        out["flash_case_count"] = out.get("flash_case_count") or len(cases)
        out["flash_cases_preview"] = cases[:max_cases]
        out.pop("flash_cases", None)
    if isinstance(out.get("flash_screen"), dict):
        fs = dict(out["flash_screen"])
        cases = _as_list(fs.get("flash_cases"))
        fs["flash_case_count"] = fs.get("flash_case_count") or len(cases)
        fs["flash_cases_preview"] = cases[:max_cases]
        fs.pop("flash_cases", None)
        out["flash_screen"] = fs
    return out


def compact_separation_package_for_display(package: dict) -> dict:
    package = _as_dict(package)
    if not package:
        return {}
    out = {}
    for k, v in package.items():
        if k == "base_case":
            out[k] = _compact_case_for_display(v, include_payload=False)
        elif k in {"cases", "ranked_candidates"}:
            out[k] = [_compact_case_for_display(c) for c in _as_list(v)]
        else:
            out[k] = v
    return out



def _constraint_warnings(process_data: dict) -> List[dict]:
    """从 BO 数据中提取最薄弱的分离约束，用于膜 polishing / 变压支线定位。"""
    out: List[dict] = []
    process_data = _as_dict(process_data)

    for source_key in ("constraint_gaps", "constraint_vector", "best_constraints", "constraints"):
        items = process_data.get(source_key)
        if isinstance(items, dict):
            iterable = items.items()
        elif isinstance(items, list):
            iterable = enumerate(items)
        else:
            continue
        for k, v in iterable:
            if isinstance(v, dict):
                name = str(v.get("name") or v.get("constraint") or k)
                gap = _safe_float(v.get("gap") if v.get("gap") is not None else v.get("value"), None)
                stream = v.get("stream") or v.get("product_stream")
                component = v.get("component") or v.get("target_component")
            else:
                name = str(k)
                gap = _safe_float(v, None)
                stream, component = None, None
            if gap is not None and gap > 0:
                out.append({
                    "name": name,
                    "gap": gap,
                    "stream": _clean_id(stream),
                    "component": _clean_id(component),
                    "source": source_key,
                })
    out.sort(key=lambda x: _safe_float(x.get("gap"), 0.0), reverse=True)
    return out


def _column_summary(process_data: dict) -> List[dict]:
    rows = []
    for col in _as_list(_as_dict(process_data).get("columns")):
        if not isinstance(col, dict):
            continue
        cid = _column_id(col)
        if not cid:
            continue
        rows.append({
            "id": cid,
            "role": col.get("role") or col.get("topology_role") or "unknown",
            "top_temperature_C": _col_top_temp_c(col),
            "bottom_temperature_C": _col_bottom_temp_c(col),
            "reboiler_kw_abs": _col_q_reb_kw(col),
            "condenser_kw_abs": _col_q_cond_kw(col),
            "pressure": _as_dict(col.get("settings")).get("pressure") or col.get("pressure"),
        })
    return rows


# ============================================================
# 分离强化候选生成
# ============================================================

def _make_ed_case(process_data: dict) -> dict:
    # Store only a reference/summary here. The real ED process_data remains in
    # state_dict["bo_optimization"] and is retrieved by get_active_* fallback.
    return {
        "case_id": "ED_best",
        "family": "ED",
        "technology": "baseline_extract_distillation",
        "verification_level": "aspen_strict" if _as_dict(process_data).get("status") == "strictly_feasible" else "aspen_available",
        "process_data": _light_process_stub(process_data, retrofit_family="baseline_ED", status=_as_dict(process_data).get("status") or "available"),
        "metrics": _extract_case_metrics(process_data),
        "description": "基础萃取精馏 BO/Aspen 优化结果，作为后续分离强化与换热强化的基准；完整数据保留在 state_dict['bo_optimization']。",
        "can_enter_thermal_intensification": bool(_as_dict(process_data).get("columns")),
    }



def _infer_pressure_bounds_for_column(col_row: dict, idx: int, solvent: str = "") -> Tuple[float, float]:
    cid = _clean_id(col_row.get("id"))
    role = str(col_row.get("role") or "").lower()
    bottom_t = _safe_float(col_row.get("bottom_temperature_C"), None)

    # C3 / solvent recovery 通常优先允许减压，以降低高沸点萃取剂回收温度。
    if "REC" in cid or "SOLVENT" in role or (solvent and idx >= 2):
        if bottom_t is not None and bottom_t >= 150:
            return 0.05, 1.0
        return 0.1, 2.0

    # 前两塔用于相对挥发度/共沸边界移动，给中等压力窗口。
    if idx == 0:
        return 0.5, 3.0
    if idx == 1:
        return 0.5, 5.0
    return 0.2, 2.0



def _flash_pressure_optimization_from_state(state_dict: Optional[dict]) -> dict:
    state = _as_dict(state_dict)
    opt = _as_dict(state.get("flash_pressure_optimization"))
    if opt:
        return opt
    opt = _as_dict(_as_dict(state.get("pressure_swing_flash_results")).get("pressure_optimization"))
    return opt


def _pair_key(pair: Any) -> str:
    vals = [_canonical_component_name(x) for x in _as_list(pair)[:2]]
    return "/".join(vals) if len(vals) == 2 and vals[0] and vals[1] else ""


def _material_pressure_recommendation(rec: dict) -> dict:
    """Normalize pressure recommendations and enforce the actionable-base gate.

    This is deliberately duplicated as a downstream guard so stale cached Flash
    results cannot create ``*_PRESSURE`` variables after the runner policy has
    been upgraded.
    """
    r = copy.deepcopy(_as_dict(rec))
    if not r:
        return r
    if not bool(r.get("quality_verified", True)):
        r["should_optimize_pressure"] = False
        r["material_pressure_change"] = False
        r["actionable_pressure_benefit"] = False
        return r

    # New-runner explicit decision has priority.
    if r.get("base_pressure_is_best") is True or r.get("actionable_pressure_benefit") is False:
        r["should_optimize_pressure"] = False
        r["actionable_pressure_benefit"] = False
        r["direction"] = "hold_at_ED_best_pressure"
        if not str(r.get("reason") or "").strip():
            r["reason"] = "ED_best 基准压力已是最佳筛选点，不加入塔压 BO。"
        return r

    explicit = r.get("material_pressure_change")
    if explicit is not None:
        if not bool(explicit):
            r["should_optimize_pressure"] = False
            r["actionable_pressure_benefit"] = False
            return r

        # Backward-compatible derivation for cached v45 recommendations that
        # expose materiality but not the new actionable fields.
        base_p = _safe_float(r.get("base_pressure_kpa"), None)
        best_p = _safe_float(r.get("best_pressure_kpa"), None)
        base_q = _safe_float(r.get("base_reference_quality"), None)
        best_q = _safe_float(r.get("best_quality"), None)
        if base_p is not None and best_p is not None and base_p > 0:
            shift = abs(float(best_p) - float(base_p)) / float(base_p)
            gain = (
                float(best_q) - float(base_q)
                if best_q is not None and base_q is not None else None
            )
            if shift <= 0.15 or (gain is not None and gain < 0.02):
                r["base_pressure_is_best"] = bool(shift <= 0.15)
                r["actionable_pressure_benefit"] = False
                r["should_optimize_pressure"] = False
                r["direction"] = "hold_at_ED_best_pressure"
                r["reason"] = (
                    "该风险边具有压敏性，但相对 ED_best 基准压力没有达到可执行收益门，"
                    "不加入塔压 BO。"
                )
                return r
        r.setdefault("actionable_pressure_benefit", bool(r.get("should_optimize_pressure")))
        return r

    # Legacy materiality re-screen.
    curve = _safe_float(r.get("curve_spread"), 0.0) or 0.0
    curve_max = _safe_float(r.get("curve_spread_max"), 0.0) or 0.0
    qrange = _safe_float(r.get("quality_range"), 0.0) or 0.0
    gap = _safe_float(r.get("gap_range"), 0.0) or 0.0
    alpha = _safe_float(r.get("alpha_log_range"), 0.0) or 0.0
    crossing = _safe_float(r.get("crossing_range"), 0.0) or 0.0
    metrics = _as_list(r.get("pressure_metrics"))
    gaps = [_safe_float(_as_dict(m).get("mean_abs_y_minus_x"), None) for m in metrics]
    gaps = [value for value in gaps if value is not None]
    mean_gap = sum(gaps) / len(gaps) if gaps else 0.0
    rel_curve = curve / max(mean_gap, 0.03)

    evidence = [
        curve >= 0.025 and rel_curve >= 0.12,
        curve_max >= 0.045,
        qrange >= 0.060,
        gap >= 0.030,
        alpha >= 0.100,
        crossing >= 0.055,
    ]
    strong = (
        curve_max >= 0.065 or qrange >= 0.100 or gap >= 0.050
        or alpha >= 0.180 or crossing >= 0.090
    )
    material = bool(strong or sum(bool(value) for value in evidence) >= 2)
    r["material_pressure_change"] = material
    r["material_evidence_count"] = sum(bool(value) for value in evidence)
    r["relative_curve_spread"] = rel_curve
    if not material:
        r["should_optimize_pressure"] = False
        r["actionable_pressure_benefit"] = False
        r["direction"] = "hold_near_base_pressure"
        r["sensitivity_score"] = min(_safe_float(r.get("sensitivity_score"), 0.0) or 0.0, 0.33)
        r["reason"] = "多压力 y-x 曲线变化未达到材料性阈值，该组分对不参与塔压 BO。"
    return r

def _flash_pressure_pair_recommendations(
    state_dict: Optional[dict],
    *,
    mode: Optional[str] = None,
    only_optimized: bool = False,
) -> List[dict]:
    opt = _flash_pressure_optimization_from_state(state_dict)
    rows: List[dict] = []
    for raw in _as_list(opt.get("pair_recommendations")):
        r = _material_pressure_recommendation(_as_dict(raw))
        if mode and str(r.get("mode") or "").upper() != str(mode).upper():
            continue
        if only_optimized and not bool(r.get("should_optimize_pressure")):
            continue
        rows.append(r)
    rows.sort(key=lambda d: _safe_float(d.get("sensitivity_score"), 0.0) or 0.0, reverse=True)
    return rows


def _find_flash_pressure_recommendation_for_pair(
    state_dict: Optional[dict],
    pair: Any,
    *,
    mode: str = "PSD",
) -> dict:
    target = _pair_key(pair)
    if not target:
        return {}
    target_set = set(target.split("/"))
    # First try exact order, then unordered pair match.
    for r in _flash_pressure_pair_recommendations(state_dict, mode=mode):
        if str(r.get("pair_key") or "") == target:
            return r
    for r in _flash_pressure_pair_recommendations(state_dict, mode=mode):
        if set(str(r.get("pair_key") or "").split("/")) == target_set:
            return r
    return {}


def _bounds_atm_from_recommendation(rec: dict) -> Optional[List[float]]:
    vals = []
    for x in _as_list(_as_dict(rec).get("recommended_bounds_atm"))[:2]:
        v = _safe_float(x, None)
        if v is not None and v > 0:
            vals.append(float(v))
    if len(vals) != 2:
        raw_kpa = []
        for x in _as_list(_as_dict(rec).get("recommended_bounds_kpa"))[:2]:
            v = _safe_float(x, None)
            if v is not None and v > 0:
                raw_kpa.append(float(v))
        if len(raw_kpa) == 2:
            vals = [raw_kpa[0] / 101.325, raw_kpa[1] / 101.325]
    if len(vals) == 2 and vals[0] < vals[1]:
        return [round(vals[0], 6), round(vals[1], 6)]
    return None


def _has_ready_flash_pressure_ranges(state_dict: Optional[dict]) -> bool:
    opt = _flash_pressure_optimization_from_state(state_dict)
    return str(opt.get("status") or "").lower() in {"ready", "completed"} and bool(_as_list(opt.get("pair_recommendations")))


def _pressure_case_has_optimized_bounds(case: dict) -> bool:
    for k in _as_dict(_as_dict(case).get("new_variables")).keys():
        if str(k).upper().endswith("_PRESSURE"):
            return True
    return False


def _pressure_handoff_from_case(case: dict) -> dict:
    """Return BO-facing bounds keyed by Aspen block ID."""
    block_bounds = {}
    disabled_keys = []
    for k, v in _as_dict(_as_dict(case).get("new_variables")).items():
        ku = str(k or "").upper().strip()
        if not ku.endswith("_PRESSURE"):
            continue
        b = _aspen_block_id(ku.rsplit("_", 1)[0])
        bounds = _normalize_bo_bounds(v)
        if len(bounds) == 2 and bounds[0] < bounds[1]:
            block_bounds[b] = bounds
        else:
            disabled_keys.append(ku)
    return {
        "source": "flash_derived_pressure_window",
        "case_id": case.get("case_id"),
        "block_pressure_bounds_atm": block_bounds,
        "disabled_pressure_keys": disabled_keys,
        "note": "Only pressure variables supported by Flash y-x/alpha sensitivity are passed to BO.",
    }



def _risk_edge_unordered_key(pair: Any) -> str:
    vals = [_canonical_component_name(x) for x in _as_list(pair)[:2] if x]
    return "/".join(sorted(vals)) if len(vals) == 2 and vals[0] != vals[1] else ""


def _process_columns_by_id(process_data: dict) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for col in _as_list(_as_dict(process_data).get("columns")):
        if not isinstance(col, dict):
            continue
        cid = _clean_id(col.get("id") or col.get("column") or col.get("name"))
        if cid:
            out[cid] = col
    return out


def _snapshot_component_value(snapshot: Any, component: str, field: str = "mole_fraction") -> float:
    snap = _as_dict(snapshot)
    comp = _canonical_component_name(component)
    pack = _as_dict(snap.get(field))
    for key, val in pack.items():
        if _canonical_component_name(key) == comp:
            return float(_safe_float(val, 0.0) or 0.0)
    return 0.0


def _column_feed_flows(column: dict) -> Dict[str, float]:
    out: Dict[str, float] = {}
    feeds = _as_list(_as_dict(_as_dict(column).get("streams")).get("feeds"))
    for snap in feeds:
        for comp, val in _as_dict(_as_dict(snap).get("molar_flow")).items():
            c = _canonical_component_name(comp)
            v = _safe_float(val, None)
            if c and v is not None and v > 0:
                out[c] = out.get(c, 0.0) + float(v)
    return out


def _cut_split_spec(block: dict, remaining_components: Sequence[str], feed_flows: Optional[dict] = None) -> dict:
    block = _as_dict(block)
    split = _as_dict(block.get("split_spec"))
    path = _as_dict(block.get("path_constraints"))
    remaining = [_canonical_component_name(c) for c in remaining_components if c]
    top = [
        _canonical_component_name(x) for x in _as_list(
            split.get("top_products")
            or split.get("required_top_components")
            or path.get("required_top_components")
            or block.get("target_light_keys")
            or []
        ) if x
    ]
    bottom = [
        _canonical_component_name(x) for x in _as_list(
            split.get("bottom_products")
            or split.get("required_bottom_components")
            or path.get("required_bottom_components")
            or block.get("target_heavy_keys")
            or []
        ) if x
    ]
    top = [c for c in top if c in remaining]
    bottom = [c for c in bottom if c in remaining and c not in top]
    if not bottom:
        present = [c for c in remaining if (_safe_float(_as_dict(feed_flows).get(c), 0.0) or 0.0) > 1e-9]
        basis = present or remaining
        bottom = [c for c in basis if c not in top]
    return {
        "top_components": list(dict.fromkeys(top)),
        "bottom_components": list(dict.fromkeys(bottom)),
        "product_side": str(split.get("product_side") or path.get("product_side") or block.get("product_side") or "top").lower(),
        "role": str(split.get("role") or path.get("role") or block.get("role") or ""),
    }


def _column_reference_pressure_kpa(column: dict) -> Optional[float]:
    """Recover the ED_best column pressure without assuming one fixed schema.

    Aspen process-data snapshots in this project commonly expose stream
    ``pressure_raw`` in bar (1.01325 bar at atmospheric pressure).  The stream
    fallback prevents a valid ED_best pressure from being lost when the column
    settings dictionary does not carry an explicit pressure field.
    """
    col = _as_dict(column)
    mappings = [
        col,
        _as_dict(col.get("settings")),
        _as_dict(col.get("operating_conditions")),
        _as_dict(col.get("conditions")),
    ]
    exact_kpa = (
        "pressure_kpa", "operating_pressure_kpa", "column_pressure_kpa",
        "top_pressure_kpa", "P_KPA", "PRES_KPA",
    )
    exact_atm = (
        "pressure_atm", "operating_pressure_atm", "column_pressure_atm",
        "top_pressure_atm", "P_ATM", "PRES_ATM",
    )
    exact_bar = (
        "pressure_bar", "operating_pressure_bar", "column_pressure_bar",
        "top_pressure_bar", "P_BAR", "PRES_BAR",
    )
    for mapping in mappings:
        for key in exact_kpa:
            value = _safe_float(mapping.get(key), None)
            if value is not None and value > 0:
                return float(value)
        for key in exact_atm:
            value = _safe_float(mapping.get(key), None)
            if value is not None and value > 0:
                return float(value) * 101.325
        for key in exact_bar:
            value = _safe_float(mapping.get(key), None)
            if value is not None and value > 0:
                return float(value) * 100.0

    for mapping in mappings:
        unit = str(mapping.get("pressure_unit") or mapping.get("P_unit") or "").strip().lower()
        for key in ("pressure", "operating_pressure", "column_pressure", "top_pressure", "PRES"):
            value = _safe_float(mapping.get(key), None)
            if value is None or value <= 0:
                continue
            if "kpa" in unit:
                return float(value)
            if "bar" in unit:
                return float(value) * 100.0
            if "atm" in unit:
                return float(value) * 101.325
            return float(value) if float(value) > 20.0 else float(value) * 101.325

    streams = _as_dict(col.get("streams"))
    snapshots: List[dict] = []
    for key in ("top", "bottom"):
        snap = _as_dict(streams.get(key))
        if snap:
            snapshots.append(snap)
    snapshots.extend(_as_dict(x) for x in _as_list(streams.get("feeds")) if isinstance(x, dict))
    recovered: List[float] = []
    for snap in snapshots:
        for key in ("pressure_kpa", "P_KPA", "PRES_KPA"):
            value = _safe_float(snap.get(key), None)
            if value is not None and value > 0:
                recovered.append(float(value))
        for key in ("pressure_bar", "P_BAR", "PRES_BAR"):
            value = _safe_float(snap.get(key), None)
            if value is not None and value > 0:
                recovered.append(float(value) * 100.0)
        for key in ("pressure_atm", "P_ATM", "PRES_ATM"):
            value = _safe_float(snap.get(key), None)
            if value is not None and value > 0:
                recovered.append(float(value) * 101.325)
        raw = _safe_float(snap.get("pressure_raw"), None)
        if raw is None or raw <= 0:
            continue
        unit = str(snap.get("pressure_unit") or snap.get("unit") or "").strip().lower()
        if "kpa" in unit:
            recovered.append(float(raw))
        elif "atm" in unit:
            recovered.append(float(raw) * 101.325)
        elif "bar" in unit:
            recovered.append(float(raw) * 100.0)
        elif float(raw) > 20.0:
            recovered.append(float(raw))
        else:
            recovered.append(float(raw) * 100.0)
    recovered = sorted(v for v in recovered if 1.0 <= v <= 50000.0)
    return float(recovered[len(recovered) // 2]) if recovered else None


def _risk_edge_context_for_column(
    column: dict,
    pair: Sequence[str],
    solvent: str = "",
    *,
    top_component: Optional[str] = None,
    bottom_component: Optional[str] = None,
) -> dict:
    pair_c = [_canonical_component_name(x) for x in _as_list(pair)[:2] if x]
    top_c = _canonical_component_name(top_component) if top_component else (pair_c[0] if pair_c else "")
    bottom_c = _canonical_component_name(bottom_component) if bottom_component else (pair_c[1] if len(pair_c) > 1 else "")
    feed_flows = _column_feed_flows(column)
    pair_total = sum(max(0.0, feed_flows.get(c, 0.0)) for c in pair_c)
    bg = {
        c: max(0.0, v) for c, v in feed_flows.items()
        if c not in pair_c and c != _canonical_component_name(solvent) and v > 1e-12
    }
    bg_total = sum(bg.values())
    bg_ratios = {c: v / bg_total for c, v in bg.items()} if bg_total > 0 else {}
    non_solvent_total = pair_total + bg_total
    solvent_flow = max(0.0, feed_flows.get(_canonical_component_name(solvent), 0.0)) if solvent else 0.0
    reference_x_first = None
    if len(pair_c) == 2 and pair_total > 1e-12:
        reference_x_first = max(0.0, feed_flows.get(pair_c[0], 0.0)) / pair_total
    return {
        "background_component_ratios": bg_ratios,
        "background_to_pair_ratio": (bg_total / pair_total) if pair_total > 1e-12 else 0.0,
        "reference_non_solvent_feed_molar_flow": non_solvent_total,
        "reference_solvent_to_feed_ratio": (solvent_flow / non_solvent_total) if non_solvent_total > 1e-12 else None,
        "reference_feed_molar_flow": feed_flows,
        "reference_pair_fraction_first_component": reference_x_first,
        "reference_base_pressure_kpa": _column_reference_pressure_kpa(column),
        "top_component": top_c,
        "bottom_component": bottom_c,
        "desired_direction": "top_component_more_volatile",
        "pair_order_is_top_bottom": bool(len(pair_c) == 2 and pair_c[0] == top_c and pair_c[1] == bottom_c),
    }

def _column_risk_edge_score(column: dict, pair: Sequence[str], warnings: Sequence[dict]) -> dict:
    pair_c = [_canonical_component_name(x) for x in _as_list(pair)[:2] if x]
    if len(pair_c) != 2:
        return {"score": 0.0}
    top_comp, bottom_comp = pair_c
    streams = _as_dict(_as_dict(column).get("streams"))
    top_snap = _as_dict(streams.get("top"))
    bottom_snap = _as_dict(streams.get("bottom"))
    feed_flows = _column_feed_flows(column)
    total_feed = sum(feed_flows.values())
    top_contamination = _snapshot_component_value(top_snap, bottom_comp)
    bottom_contamination = _snapshot_component_value(bottom_snap, top_comp)
    feed_balance = min(feed_flows.get(top_comp, 0.0), feed_flows.get(bottom_comp, 0.0)) / max(total_feed, 1e-12)
    warning_score = 0.0
    for row in warnings:
        rd = _as_dict(row)
        comp = _canonical_component_name(rd.get("component"))
        name = str(rd.get("name") or "").upper()
        if comp in pair_c or any(c in name for c in pair_c):
            warning_score += max(0.0, _safe_float(rd.get("gap"), 0.0) or 0.0)
    score = 6.0 * top_contamination + 3.0 * bottom_contamination + 0.25 * feed_balance + 4.0 * warning_score
    return {
        "score": float(score),
        "top_contamination": float(top_contamination),
        "bottom_contamination": float(bottom_contamination),
        "feed_balance": float(feed_balance),
        "constraint_warning_score": float(warning_score),
    }


def _is_recovery_column_for_cut_risk(block: dict, idx: int, total: int, solvent: str = "") -> bool:
    """Stricter recovery-column classifier for cut extraction.

    Extractive columns legitimately carry solvent in their bottoms, so solvent
    presence alone must not classify C1/C2 as recovery columns.
    """
    b = _as_dict(block)
    cid = _clean_id(b.get("id") or b.get("name") or b.get("column"))
    role = " ".join([
        str(b.get("role") or ""), str(b.get("topology_role") or ""),
        str(b.get("function") or ""), str(_as_dict(b.get("split_spec")).get("role") or ""),
        str(_as_dict(b.get("path_constraints")).get("role") or ""), cid,
    ]).upper()
    if any(token in role for token in ("SOLVENT_RECOVERY", "SOLVENT RECOVERY", "SOLVREC", "RECYCLE_COLUMN", "RECOVERY_COLUMN")):
        return True
    if cid in {"C3", "T3", "SRC", "SOLVREC"} and total >= 3 and idx == total - 1:
        return True
    if total >= 3 and idx == total - 1:
        split = _as_dict(b.get("split_spec"))
        bottom = {_canonical_component_name(x) for x in _as_list(split.get("bottom_products")) if x}
        if solvent and _canonical_component_name(solvent) in bottom:
            return True
    return False


def build_pressure_cut_risk_edge_plan(process_data: dict, state_dict: Optional[dict] = None) -> dict:
    """Map the retained ED topology to N-1 pressure-screening cut risk edges.

    One separation column creates one cut.  Only cross-cut component pairs are
    eligible for pressure screening; other components remain in the Flash feed
    as a fixed multicomponent background derived from the ED_best column feed.
    """
    state = _as_dict(state_dict)
    process_data = _as_dict(process_data)
    solvent = _canonical_component_name(_solvent_from_state(state))
    non_solvent = [_canonical_component_name(c) for c in _non_solvent_components(state)]
    topology = _as_dict(state.get("topology"))
    blocks = [copy.deepcopy(_as_dict(b)) for b in _as_list(topology.get("blocks")) if isinstance(b, dict)]
    col_map = _process_columns_by_id(process_data)
    if not blocks:
        blocks = [copy.deepcopy(c) for c in col_map.values()]
    warnings = _constraint_warnings(process_data)
    remaining = list(dict.fromkeys(non_solvent))
    max_cuts = max(0, len(remaining) - 1)
    cuts: List[dict] = []
    selected_pairs: List[List[str]] = []
    pair_contexts: Dict[str, dict] = {}

    for idx, block in enumerate(blocks):
        if len(cuts) >= max_cuts or len(remaining) < 2:
            break
        cid = _clean_id(block.get("id") or block.get("name") or block.get("column"))
        col = copy.deepcopy(col_map.get(cid) or {})
        merged = copy.deepcopy(block)
        for key in ("role", "topology_role", "split_spec", "path_constraints", "settings"):
            if not merged.get(key) and col.get(key) is not None:
                merged[key] = copy.deepcopy(col.get(key))
        if _is_recovery_column_for_cut_risk(merged, idx, len(blocks), solvent):
            continue
        feed_flows = _column_feed_flows(col)
        split = _cut_split_spec(merged, remaining, feed_flows)
        top = [c for c in split.get("top_components") or [] if c and c != solvent]
        bottom = [c for c in split.get("bottom_components") or [] if c and c != solvent]
        if not top or not bottom:
            continue
        candidates = []
        for light in top:
            for heavy in bottom:
                metrics = _column_risk_edge_score(col, [light, heavy], warnings)
                context = _risk_edge_context_for_column(
                    col, [light, heavy], solvent,
                    top_component=light, bottom_component=heavy,
                )
                candidates.append({
                    "pair": [light, heavy],
                    "pair_key": f"{light}/{heavy}",
                    "unordered_pair_key": _risk_edge_unordered_key([light, heavy]),
                    "risk_score": metrics.get("score", 0.0),
                    "risk_metrics": metrics,
                    "context": context,
                })
        candidates.sort(key=lambda r: _safe_float(r.get("risk_score"), 0.0) or 0.0, reverse=True)
        retained = []
        if candidates:
            retained.append(candidates[0])
            if len(candidates) > 1:
                s0 = _safe_float(candidates[0].get("risk_score"), 0.0) or 0.0
                s1 = _safe_float(candidates[1].get("risk_score"), 0.0) or 0.0
                uncertain = s0 <= 1e-8
                close = s1 >= 0.85 * max(s0, 1e-12)
                if uncertain or close:
                    retained.append(candidates[1])
        edges = []
        for eidx, row in enumerate(retained):
            role = "primary" if eidx == 0 else "backup"
            edge = copy.deepcopy(row)
            edge["risk_role"] = role
            edge["column_id"] = cid
            edge["cut_index"] = len(cuts) + 1
            edges.append(edge)
            pair = list(edge.get("pair") or [])
            if pair and pair not in selected_pairs:
                selected_pairs.append(pair)
            ukey = str(edge.get("unordered_pair_key") or "")
            if ukey:
                ctx = copy.deepcopy(_as_dict(edge.get("context")))
                ctx.update({
                    "pair": pair,
                    "pair_key": edge.get("pair_key"),
                    "column_id": cid,
                    "cut_index": len(cuts) + 1,
                    "risk_role": role,
                    "risk_score": edge.get("risk_score"),
                })
                old = pair_contexts.get(ukey)
                if old is None or (_safe_float(ctx.get("risk_score"), 0.0) or 0.0) > (_safe_float(old.get("risk_score"), 0.0) or 0.0):
                    pair_contexts[ukey] = ctx
        cuts.append({
            "column_id": cid,
            "cut_index": len(cuts) + 1,
            "top_components": top,
            "bottom_components": bottom,
            "candidate_edges": candidates,
            "selected_edges": edges,
            "downstream_components": bottom,
        })
        # Standard retained-ED sequence passes the bottom side to the next
        # separation column.  Explicit split data therefore determines the next
        # remaining component set without enumerating all binary combinations.
        remaining = list(dict.fromkeys(bottom))

    return {
        "schema_version": "pressure-cut-risk-edge-v1",
        "non_solvent_component_count": len(non_solvent),
        "expected_cut_count": max_cuts,
        "identified_cut_count": len(cuts),
        "cuts": cuts,
        "selected_pairs": selected_pairs,
        "pair_contexts": pair_contexts,
        "other_component_policy": "Other non-solvent components are retained as a fixed ED_best column-feed background; they do not create independent pressure variables unless they form a selected cross-cut risk edge.",
    }


def _risk_edge_plan_from_state(state_dict: Optional[dict], process_data: Optional[dict] = None) -> dict:
    state = _as_dict(state_dict)
    cached = _as_dict(state.get("pressure_screen_risk_edges"))
    if cached.get("cuts"):
        contexts = []
        for cut in _as_list(cached.get("cuts")):
            for edge in _as_list(_as_dict(cut).get("selected_edges")):
                contexts.append(_as_dict(_as_dict(edge).get("context")))
        cache_is_directional = bool(contexts) and all(
            ctx.get("top_component") and ctx.get("bottom_component")
            and _safe_float(ctx.get("reference_base_pressure_kpa"), None) is not None
            for ctx in contexts
        )
        if cache_is_directional:
            return cached
    # Rebuild legacy cached plans so the new top/bottom orientation and actual
    # ED_best column pressures cannot be bypassed by an old GUI state object.
    pd = _as_dict(process_data) or get_ed_process_data(state)
    return build_pressure_cut_risk_edge_plan(pd, state)


def _filter_recommendations_to_risk_edges(rows: Sequence[dict], risk_plan: dict) -> List[dict]:
    allowed = {
        _risk_edge_unordered_key(p) for p in _as_list(_as_dict(risk_plan).get("selected_pairs"))
        if _risk_edge_unordered_key(p)
    }
    if not allowed:
        return []
    return [r for r in rows if _risk_edge_unordered_key(_as_dict(r).get("pair") or str(_as_dict(r).get("pair_key") or "").split("/")) in allowed]


def _build_pressure_flash_screen_payload(
    process_data: dict,
    state_dict: Optional[dict] = None,
    *,
    mode: str = "solvent_free_psd",
) -> dict:
    """Build a cut-aware Flash2 screen payload from the retained ED topology."""
    state = _as_dict(state_dict)
    non_solvent = _non_solvent_components(state)
    solvent = _solvent_from_state(state)
    risk_plan = _risk_edge_plan_from_state(state, process_data)
    pressure_grid_kpa = _augment_pressure_grid_with_risk_bases(
        _default_pressure_grid_kpa(mode, state), risk_plan, state
    )
    pairs = _as_list(risk_plan.get("selected_pairs")) or _component_pairs(non_solvent)
    contexts = _as_dict(risk_plan.get("pair_contexts"))
    flash_cases = []
    x_grid = [0.05, 0.10, 0.18, 0.30, 0.45, 0.60, 0.75, 0.85, 0.93]
    flash_cfg = _as_dict(state.get("pressure_swing_flash_config"))
    max_valid_flash_temperature_C = _safe_float(
        flash_cfg.get("max_valid_flash_temperature_C", state.get("max_valid_flash_temperature_C")), 350.0
    ) or 350.0
    min_valid_flash_temperature_C = _safe_float(
        flash_cfg.get("min_valid_flash_temperature_C", state.get("min_valid_flash_temperature_C")), -100.0
    ) or -100.0

    for pair in pairs:
        ctx = copy.deepcopy(contexts.get(_risk_edge_unordered_key(pair)) or {})
        for p_kpa in pressure_grid_kpa:
            flash_cases.append({
                "case_type": "binary_pressure_vle",
                "pair": pair,
                "pressure_kpa": p_kpa,
                "pressure_atm": round(p_kpa / 101.325, 4),
                "flash_block": "Flash2",
                "duty": 0.0,
                "composition_grid_x_first_component": x_grid,
                "source_column_id": ctx.get("column_id"),
                "risk_role": ctx.get("risk_role"),
                "reference_base_pressure_kpa": ctx.get("reference_base_pressure_kpa"),
                "reference_pair_fraction_first_component": ctx.get("reference_pair_fraction_first_component"),
                "reference_solvent_to_feed_ratio": ctx.get("reference_solvent_to_feed_ratio"),
                "top_component": ctx.get("top_component"),
                "bottom_component": ctx.get("bottom_component"),
                "desired_direction": ctx.get("desired_direction"),
                "risk_edge_context": copy.deepcopy(ctx),
                "max_valid_flash_temperature_C": max_valid_flash_temperature_C,
                "min_valid_flash_temperature_C": min_valid_flash_temperature_C,
                "background_component_ratios": ctx.get("background_component_ratios") or {},
                "background_to_pair_ratio": ctx.get("background_to_pair_ratio", 0.0),
                "outputs": [
                    "vapour_mole_fraction", "liquid_mole_fraction", "temperature",
                    "relative_volatility_alpha", "azeotrope_indicator_y_minus_x",
                ],
            })

    if mode != "solvent_free_psd" and solvent:
        for pair in pairs:
            ctx = copy.deepcopy(contexts.get(_risk_edge_unordered_key(pair)) or {})
            reference_sfr = _safe_float(ctx.get("reference_solvent_to_feed_ratio"), None)
            sfr_grid = [0.5, 1.0, 2.0]
            if reference_sfr is not None and reference_sfr > 0:
                sfr_grid = sorted({round(max(0.1, reference_sfr * f), 4) for f in (0.8, 1.0, 1.2)})
            for p_kpa in pressure_grid_kpa:
                for sfr in sfr_grid:
                    flash_cases.append({
                        "case_type": "multicomponent_risk_edge_with_solvent_pressure_vle",
                        "pair": pair,
                        "solvent": solvent,
                        "solvent_to_feed_ratio": sfr,
                        "pressure_kpa": p_kpa,
                        "pressure_atm": round(p_kpa / 101.325, 4),
                        "flash_block": "Flash2",
                        "duty": 0.0,
                        "composition_grid_x_first_component": x_grid,
                        "source_column_id": ctx.get("column_id"),
                        "risk_role": ctx.get("risk_role"),
                        "reference_base_pressure_kpa": ctx.get("reference_base_pressure_kpa"),
                        "reference_pair_fraction_first_component": ctx.get("reference_pair_fraction_first_component"),
                        "reference_solvent_to_feed_ratio": ctx.get("reference_solvent_to_feed_ratio"),
                        "top_component": ctx.get("top_component"),
                        "bottom_component": ctx.get("bottom_component"),
                        "desired_direction": ctx.get("desired_direction"),
                        "risk_edge_context": copy.deepcopy(ctx),
                        "max_valid_flash_temperature_C": max_valid_flash_temperature_C,
                        "min_valid_flash_temperature_C": min_valid_flash_temperature_C,
                        "background_component_ratios": ctx.get("background_component_ratios") or {},
                        "background_to_pair_ratio": ctx.get("background_to_pair_ratio", 0.0),
                        "outputs": [
                            "vapour_mole_fraction", "liquid_mole_fraction",
                            "relative_volatility_alpha_with_solvent", "solvent_loss_to_vapour",
                        ],
                    })

    return {
        "action": "run_aspen_flash2_pressure_sensitivity_screen",
        "mode": mode,
        "thermodynamic_model_hint": "NRTL_or_user_selected_activity_model",
        "pressure_grid_kpa": pressure_grid_kpa,
        "pressure_grid_atm": _pressure_grid_atm(pressure_grid_kpa),
        "components": non_solvent,
        "solvent": solvent if mode != "solvent_free_psd" else None,
        "selected_pairs": pairs,
        "risk_edge_plan": risk_plan,
        "pair_contexts": contexts,
        "flash_case_count": len(flash_cases),
        "flash_cases": flash_cases,
        "decision_rules": {
            "cut_rule": "Only cross-cut risk edges from the retained ED topology are screened; expected count is N-1 cuts.",
            "background_rule": "Other components remain at fixed ED_best column-feed ratios during pair-composition scanning.",
            "pressure_rule": "A column receives a pressure variable only if its primary or retained backup risk edge passes both the material-change and actionable-benefit gates.",
            "vacuum_rule": "The default screen includes positive absolute pressures below 1 atm (30/50/70 kPa); no zero or negative absolute pressure is written to Aspen.",
        },
    }


def _unordered_pair_key(pair: Any) -> str:
    vals = [_canonical_component_name(x) for x in _as_list(pair)[:2] if x]
    return "/".join(sorted(vals)) if len(vals) == 2 else ""


def _psd_rec_score_map(state_dict: Optional[dict]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for r in _flash_pressure_pair_recommendations(state_dict, mode="PSD"):
        key = _unordered_pair_key(r.get("pair") or str(r.get("pair_key") or "").split("/"))
        if not key:
            continue
        old = out.get(key)
        if old is None or (_safe_float(r.get("sensitivity_score"), 0.0) or 0.0) > (_safe_float(old.get("sensitivity_score"), 0.0) or 0.0):
            out[key] = r
    return out


def _rank_psd_separation_sequences(state_dict: Optional[dict]) -> List[dict]:
    """Enumerate initial solvent-free PSD component orders from Flash results.

    PSD changes the actual distillation sequence after removing the solvent;
    therefore pressure-sensitive binary pairs should determine the initial
    serial-column order.  EPSD intentionally does not use this helper because it
    keeps the ED topology.
    """
    comps = _non_solvent_components(state_dict)
    if len(comps) < 2:
        return []
    rec_map = _psd_rec_score_map(state_dict)
    try:
        from itertools import permutations
    except Exception:
        permutations = None
    perms = list(permutations(comps)) if permutations else [tuple(comps)]
    # Limit factorial growth for later generalized tests.
    if len(perms) > 120:
        perms = perms[:120]
    candidates = []
    for order in perms:
        split_pairs = []
        score = 0.0
        pressure_supported = 0
        skipped = 0
        for i in range(len(order) - 1):
            pair = [order[i], order[i + 1]]
            rec = rec_map.get(_unordered_pair_key(pair), {})
            bounds = _bounds_atm_from_recommendation(rec) if bool(rec.get("should_optimize_pressure")) else None
            pair_score = _safe_float(rec.get("sensitivity_score"), 0.0) or 0.0
            if bounds:
                pressure_supported += 1
                score += pair_score + 0.15
            else:
                skipped += 1
                score += 0.10 * pair_score
            split_pairs.append({
                "pair": pair,
                "pair_key": _pair_key(pair),
                "unordered_pair_key": _unordered_pair_key(pair),
                "pressure_rec": rec,
                "pressure_bounds_atm": bounds,
                "pressure_bounds_kpa": rec.get("recommended_bounds_kpa") if rec else None,
                "direction": rec.get("direction") if rec else "hold_near_base_pressure",
                "should_optimize_pressure": bool(bounds),
                "sensitivity_score": pair_score,
            })
        # Prefer sequences with more pressure-supported splits, then higher score.
        candidates.append({
            "ordered_components": list(order),
            "split_pairs": split_pairs,
            "pressure_supported_count": pressure_supported,
            "skipped_pair_count": skipped,
            "sequence_score": round(float(score + 0.25 * pressure_supported - 0.10 * skipped), 6),
            "selection_rule": "maximize Flash-derived pressure-sensitive adjacent splits; do not use skipped pairs as pressure variables",
        })
    candidates.sort(key=lambda d: (-int(d.get("pressure_supported_count") or 0), -(_safe_float(d.get("sequence_score"), 0.0) or 0.0), "/".join(d.get("ordered_components") or [])))
    # Remove duplicate reverse-equivalent lists only when they have identical split set/score.
    unique = []
    seen = set()
    for c in candidates:
        key = tuple(c.get("ordered_components") or [])
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)
    return unique[:12]


def _selected_psd_sequence(state_dict: Optional[dict], case: Optional[dict] = None) -> dict:
    case = _as_dict(case)
    seq = _as_dict(case.get("selected_psd_sequence"))
    if seq:
        return seq
    seqs = _rank_psd_separation_sequences(state_dict)
    return _as_dict(seqs[0]) if seqs else {}

def _build_solvent_free_psd_candidate(process_data: dict, state_dict: Optional[dict] = None) -> dict:
    non_solvent = _non_solvent_components(state_dict)
    if len(non_solvent) < 2:
        return {}
    has_flash_ranges = _has_ready_flash_pressure_ranges(state_dict)
    seq_candidates = _rank_psd_separation_sequences(state_dict) if has_flash_ranges else []
    selected_seq = _as_dict(seq_candidates[0]) if seq_candidates else {
        "ordered_components": non_solvent,
        "split_pairs": [{"pair": [non_solvent[i], non_solvent[i + 1]], "pair_key": _pair_key([non_solvent[i], non_solvent[i + 1]])} for i in range(len(non_solvent) - 1)],
        "sequence_score": 0.0,
        "selection_rule": "input component order; pending Flash-derived PSD sequence ranking",
    }
    split_pairs = _as_list(selected_seq.get("split_pairs"))
    n_cols = max(1, len(non_solvent) - 1)
    modified_units = [f"PSDC{i + 1}" for i in range(n_cols)]
    variables = {}
    reasons = [
        "无溶剂 PSD 会改变分离序列；本候选先根据 Flash2 y-x 压敏对枚举并选择 PSD 串联初始顺序。",
        "塔压变量只来自所选序列中 Flash2 判定压敏的相邻组分对；弱压敏对不生成 *_PRESSURE。",
    ]
    sensitive_pairs = []
    skipped_pairs = []
    for i, cid in enumerate(modified_units):
        sp = _as_dict(split_pairs[i]) if i < len(split_pairs) else {}
        pair = _as_list(sp.get("pair"))[:2]
        rec = _as_dict(sp.get("pressure_rec")) or (_find_flash_pressure_recommendation_for_pair(state_dict, pair, mode="PSD") if has_flash_ranges else {})
        bounds = _bounds_atm_from_recommendation(rec) if bool(rec.get("should_optimize_pressure")) else None
        if bounds:
            variables[f"{cid}_PRESSURE"] = bounds
            sensitive_pairs.append({
                "block": cid,
                "pair": pair,
                "pair_key": _pair_key(pair),
                "direction": rec.get("direction"),
                "bounds_atm": bounds,
                "bounds_kpa": rec.get("recommended_bounds_kpa"),
                "sensitivity_score": rec.get("sensitivity_score"),
                "reason": rec.get("reason"),
            })
            reasons.append(f"{cid}: 所选 PSD 序列相邻对 `{_pair_key(pair)}` 进入压力 BO，范围={bounds[0]:.4g}-{bounds[1]:.4g} atm。")
        else:
            skipped_pairs.append({
                "block": cid,
                "pair": pair,
                "pair_key": _pair_key(pair),
                "reason": rec.get("reason") if rec else "尚无该组分对的 Flash 压敏结论。",
            })
            if rec:
                reasons.append(f"{cid}: `{_pair_key(pair)}` Flash y-x 基本不随压力变化，不加入塔压 BO。")
        variables[f"{cid}_N"] = [30, 140]
        variables[f"{cid}_RR"] = [0.2, 15.0]
        variables[f"{cid}_D_FLOW"] = [0.05, 0.95]
        variables[f"{cid}_FEED_NORM"] = [0.05, 0.95]

    pseudo_pd = _light_process_stub(process_data, retrofit_family="solvent_free_pressure_swing_distillation", status="flash_screen_required_before_aspen_psd")
    priority = 0.20
    verification = "flash_screen_required"
    if has_flash_ranges:
        if sensitive_pairs:
            priority = 0.88 if len(non_solvent) >= 3 else 0.76
            verification = "flash_derived_pressure_bounds_and_psd_sequence_ready"
        else:
            priority = 0.05
            verification = "flash_indicates_no_useful_psd_pressure_swing"

    return {
        "case_id": "PSD1",
        "family": "pressure_retrofit",
        "technology": "solvent_free_pressure_swing_distillation",
        "subtype": "PSD_without_extractant",
        "verification_level": verification,
        "base_case": "ED_best",
        "modified_units": modified_units,
        "removed_or_disabled_items": ["external_solvent_feed", "solvent_recycle_spec", "solvent_recovery_constraint_for_this_branch"],
        "components_scope": non_solvent,
        "selected_psd_sequence": selected_seq,
        "psd_sequence_candidates": seq_candidates[:6],
        "new_variables": variables,
        "pressure_pair_assignment": {
            "sensitive_pairs": sensitive_pairs,
            "skipped_pairs": skipped_pairs,
            "requires_flash_before_pressure_bo": not has_flash_ranges,
            "sequence_selection": selected_seq,
        },
        "process_data": pseudo_pd,
        "metrics": _extract_case_metrics(process_data),
        "priority": priority,
        "objective": ["avoid_extractant", "derive_psd_sequence_from_flash", "verify_pressure_sensitive_azeotrope_split", "reduce_solvent_recovery_burden"],
        "reasoning": reasons + [
            "PSD 与 EPSD 不同：PSD 去掉萃取剂后需要重新确定分离顺序；EPSD 保留 ED 拓扑，不额外枚举分离序列。",
            "若某一对组分的 y-x 曲线随压力基本重合，例如甲醇/乙醇这类弱压敏对，则该对不会生成塔压变量。",
            "该支线不继承原萃取剂/溶剂循环纯度约束，但必须重新满足所有非溶剂产品纯度和收率。",
        ],
        "verification_payload": _build_pressure_flash_screen_payload(process_data, state_dict, mode="solvent_free_psd"),
        "can_enter_thermal_intensification": False,
    }





def _epsd_pressure_bounds_for_ed_column(state_dict: dict, rec: dict) -> Optional[List[float]]:
    """Map a Flash pressure recommendation to a conservative EPSD column window.

    EPSD/PED keeps the original ED topology and solvent loop.  Therefore the
    Flash-derived high/low pressure trend is treated as a *directional signal*,
    not as a direct RadFrac pressure prescription.  This function converts the
    signal into a bounded polishing window that can plausibly improve ED without
    forcing the column into a PSD-like pressure regime.

    Defaults:
    - safe EPSD envelope: 0.30 to 4.00 atm (positive absolute pressure)
    - depressurize: retain the Flash-derived vacuum window inside that envelope
    - pressurize: retain the Flash-derived high-pressure window inside that envelope
    - near-best: overlap with the user safe EPSD window
    """
    rec = _as_dict(rec)
    if not bool(rec.get("should_optimize_pressure")):
        return None

    score = _safe_float(rec.get("sensitivity_score"), 0.0) or 0.0
    min_score = _safe_float(_as_dict(state_dict).get("min_epsd_pressure_sensitivity_score"), 0.32) or 0.32
    if score < min_score:
        return None

    source_bounds = _bounds_atm_from_recommendation(rec)
    if not source_bounds:
        return None

    state = _as_dict(state_dict)
    base_atm = _safe_float(state.get("epsd_base_pressure_atm"), None)
    if base_atm is None:
        base_atm = _safe_float(_as_dict(state.get("bo_optimization")).get("base_pressure_atm"), 1.0) or 1.0

    min_atm = _safe_float(state.get("min_epsd_pressure_atm"), 0.30) or 0.30
    configured_max = _safe_float(state.get("max_epsd_pressure_atm"), None)
    hard_max_atm = _safe_float(state.get("hard_max_epsd_pressure_atm"), 10.0) or 10.0

    direction = str(rec.get("direction") or "").lower()
    src_lo, src_hi = float(source_bounds[0]), float(source_bounds[1])

    # Do not silently cut a validated high-pressure Flash window at 4 atm.
    # When the user has not supplied an explicit engineering cap, retain the
    # full evidence-based window up to a generic absolute-pressure safety cap.
    max_atm = float(configured_max) if configured_max is not None else max(4.0, src_hi)
    min_atm = max(0.20, float(min_atm))
    max_atm = min(float(hard_max_atm), max(float(min_atm) + 0.10, float(max_atm)))

    if "depress" in direction or "减压" in direction:
        # Preserve the evidence-based vacuum window instead of expanding it back
        # to atmospheric pressure.  Example: 30-50 kPa -> 0.30-0.49 atm.
        lo = max(min_atm, src_lo)
        hi = min(max_atm, float(base_atm), src_hi)
        if hi <= lo:
            lo = max(min_atm, min(src_lo, float(base_atm) * 0.80))
            hi = min(max_atm, float(base_atm), max(src_hi, lo + 0.10))
    elif "press" in direction or "加压" in direction:
        # Likewise keep the Flash high-pressure neighborhood, clipped only by
        # the explicit EPSD safety envelope.
        lo = max(min_atm, float(base_atm), src_lo)
        hi = min(max_atm, src_hi)
        if hi <= lo:
            lo = max(min_atm, min(float(base_atm), max_atm - 0.10))
            hi = min(max_atm, max(src_hi, lo + 0.10))
    else:
        lo = max(min_atm, min(src_lo, float(base_atm) * 0.90))
        hi = min(max_atm, max(src_hi, float(base_atm) * 1.10))
        if hi <= lo:
            lo, hi = max(min_atm, float(base_atm) * 0.90), min(max_atm, float(base_atm) * 1.10)

    if hi - lo < 0.05:
        return None
    return [round(float(lo), 6), round(float(hi), 6)]

def _is_epsd_solvent_recovery_column(col: dict, idx: int, total: int, solvent: str = "") -> bool:
    """Identify the solvent recovery column in the retained-ED EPSD flowsheet.

    This intentionally uses topology role first and then robust fallbacks.  In
    most three-column ED/EPSD flowsheets the last column recovers the high-boiling
    solvent and may benefit from pressure reduction, so it must not be excluded
    from EPSD pressure mapping.
    """
    col = _as_dict(col)
    cid = _clean_id(col.get("id") or col.get("column") or col.get("name"))
    role = " ".join([
        str(col.get("role") or ""),
        str(col.get("topology_role") or ""),
        str(col.get("function") or ""),
        str(_as_dict(col.get("split_spec")).get("role") or ""),
        str(_as_dict(col.get("path_constraints")).get("role") or ""),
        cid,
    ]).upper()
    solvent_u = str(solvent or "").upper().strip()
    if "SOLVENT" in role and ("REC" in role or "RECOVERY" in role or "RECY" in role):
        return True
    if "RECOVERY" in role or "RECYCLE" in role or "SOLVREC" in role:
        return True
    if solvent_u:
        for holder in (_as_dict(col.get("split_spec")), _as_dict(col.get("path_constraints")), _as_dict(col.get("settings"))):
            for key in ("bottom_products", "required_bottom_components", "target_heavy_keys", "recycle_components"):
                vals = [str(x).upper().strip() for x in _as_list(holder.get(key))]
                if solvent_u in vals:
                    return True
    # Conservative fallback for the standard ED/EPSD three-column topology: T3/C3
    # is the solvent recovery column.  This matches the paper-style EPSD layout,
    # where all column pressures are optimized and the recovery column is reduced
    # in pressure to mitigate high-boiling-solvent recovery duty.
    if total >= 3 and idx == total - 1:
        return True
    if cid.upper() in {"C3", "T3", "DC3", "SRC", "SOLVREC"}:
        return True
    return False


def _state_bounds_list(state: dict, *keys: str) -> Optional[List[float]]:
    state = _as_dict(state)
    pkg = _as_dict(state.get("separation_retrofit"))
    cfg = _as_dict(state.get("epsd_pressure_policy"))
    holders = [state, pkg, cfg]
    for holder in holders:
        for key in keys:
            raw = holder.get(key) if isinstance(holder, dict) else None
            vals = []
            for x in _as_list(raw)[:2]:
                v = _safe_float(x, None)
                if v is not None and v > 0:
                    vals.append(float(v))
            if len(vals) == 2 and vals[0] < vals[1]:
                return [round(vals[0], 6), round(vals[1], 6)]
    return None


def _epsd_solvent_recovery_pressure_bounds(state_dict: dict, col: dict) -> List[float]:
    """Pressure BO window for the solvent recovery column in EPSD.

    The recovery column is energy-driven rather than binary-pair-driven.  The
    ACS IECR 2025 EPSD example treats all three column pressures as optimization
    variables and specifically reduces the solvent recovery column pressure for
    a high-boiling extractant.  We therefore add a guarded low-pressure window
    for the recovery tower instead of excluding it.
    """
    state = _as_dict(state_dict)
    user_bounds = _state_bounds_list(
        state,
        "epsd_solvent_recovery_pressure_bounds_atm",
        "solvent_recovery_pressure_bounds_atm",
        "C3_PRESSURE_BOUNDS_ATM",
    )
    if user_bounds:
        return user_bounds

    settings = _as_dict(_as_dict(col).get("settings"))
    base_atm = (
        _safe_float(settings.get("pressure_atm"), None)
        or _safe_float(_as_dict(col).get("pressure_atm"), None)
        or _safe_float(settings.get("pressure"), None)
        or _safe_float(_as_dict(col).get("pressure"), None)
        or 1.0
    )
    bottom_t = (
        _safe_float(_as_dict(col).get("bottom_temperature_C"), None)
        or _safe_float(settings.get("bottom_temperature_C"), None)
        or _safe_float(settings.get("bottom_temperature"), None)
    )

    # Default window: allow vacuum recovery around the paper-style 0.5 atm point,
    # while keeping the upper end close to atmospheric pressure so the optimizer
    # can choose no effective pressure reduction if it is not beneficial.
    lo = _safe_float(state.get("min_epsd_recovery_pressure_atm"), 0.30) or 0.30
    hi = _safe_float(state.get("max_epsd_recovery_pressure_atm"), 1.00) or 1.00
    if bottom_t is not None and bottom_t >= 180.0:
        lo = min(lo, 0.20)
    # If the current ED already runs the recovery column below 1 atm, keep a
    # local neighborhood but do not exceed the safe EPSD recovery envelope.
    if base_atm and base_atm < 0.95:
        lo = min(lo, max(0.20, 0.70 * float(base_atm)))
        hi = min(hi, max(0.60, 1.35 * float(base_atm)))
    lo = max(0.05, float(lo))
    hi = max(float(lo) + 0.10, float(hi))
    return [round(lo, 6), round(hi, 6)]


def _build_pressure_retrofit_candidate(process_data: dict, state_dict: Optional[dict] = None) -> dict:
    state_dict = _as_dict(state_dict)
    columns = _column_summary(process_data)
    variables = {}
    modified_units = []
    reasons = []
    solvent = _solvent_from_state(state_dict)
    has_flash_ranges = _has_ready_flash_pressure_ranges(state_dict)
    risk_plan = _risk_edge_plan_from_state(state_dict, process_data)
    raw_epsd_recs = _filter_recommendations_to_risk_edges(
        _flash_pressure_pair_recommendations(state_dict, mode="EPSD", only_optimized=True), risk_plan
    ) if has_flash_ranges else []
    rec_by_pair = {}
    rec_by_column_pair = {}
    for rec in raw_epsd_recs:
        rd = _as_dict(rec)
        key = _risk_edge_unordered_key(rd.get("pair") or str(rd.get("pair_key") or "").split("/"))
        column_id = _clean_id(rd.get("source_column_id"))
        if key:
            old_rec = rec_by_pair.get(key)
            if old_rec is None or (_safe_float(rd.get("sensitivity_score"), 0.0) or 0.0) > (_safe_float(_as_dict(old_rec).get("sensitivity_score"), 0.0) or 0.0):
                rec_by_pair[key] = rd
            if column_id:
                ckey = (column_id, key)
                old_col_rec = rec_by_column_pair.get(ckey)
                if old_col_rec is None or (_safe_float(rd.get("sensitivity_score"), 0.0) or 0.0) > (_safe_float(_as_dict(old_col_rec).get("sensitivity_score"), 0.0) or 0.0):
                    rec_by_column_pair[ckey] = rd

    separation_columns = []
    recovery_columns = []
    total_cols = len(columns)
    for idx, col in enumerate(columns):
        cid = _clean_id(col.get("id"))
        if not cid:
            continue
        if _is_epsd_solvent_recovery_column(col, idx, total_cols, solvent):
            recovery_columns.append(col)
        else:
            separation_columns.append(col)

    cuts_by_column = {
        _clean_id(c.get("column_id")): _as_dict(c)
        for c in _as_list(risk_plan.get("cuts")) if _clean_id(_as_dict(c).get("column_id"))
    }
    epsd_recs = []
    mapped_edges = []
    for col in separation_columns:
        cid = _clean_id(col.get("id"))
        cut = _as_dict(cuts_by_column.get(cid))
        approved = []
        for edge in _as_list(cut.get("selected_edges")):
            ed = _as_dict(edge)
            edge_key = str(ed.get("unordered_pair_key") or _risk_edge_unordered_key(ed.get("pair")))
            rec = _as_dict(rec_by_column_pair.get((cid, edge_key)) or rec_by_pair.get(edge_key))
            if not rec:
                continue
            bounds = _epsd_pressure_bounds_for_ed_column(state_dict, rec)
            if bounds:
                approved.append((rec, bounds, ed))
        approved.sort(key=lambda row: _safe_float(_as_dict(row[0]).get("sensitivity_score"), 0.0) or 0.0, reverse=True)
        if approved:
            rec, bounds, edge = approved[0]
            variables[f"{cid}_PRESSURE"] = [float(bounds[0]), float(bounds[1])]
            modified_units.append(cid)
            rr = dict(rec)
            rr["ed_column_bounds_atm"] = bounds
            rr["mapped_column_id"] = cid
            rr["risk_role"] = edge.get("risk_role")
            epsd_recs.append(rr)
            mapped_edges.append({"column_id": cid, "edge": edge, "recommendation": rr})
            reasons.append(
                f"{cid}: 风险边 `{edge.get('pair_key')}` ({edge.get('risk_role')}) 通过含溶剂材料性压敏筛选，"
                f"P_BO={float(bounds[0]):.4g}-{float(bounds[1]):.4g} atm；其余跨边组分仅作为 ED_best 多组分背景。"
            )
        elif has_flash_ranges:
            selected_edge_names = [str(_as_dict(e).get("pair_key") or "") for e in _as_list(cut.get("selected_edges"))]
            reasons.append(
                f"{cid}: 切割风险边 {selected_edge_names or ['未识别']} 未通过材料性压敏或相对 ED_best 的可执行收益门，不加入该塔塔压 BO。"
            )

    # Current project boundary: solvent-recovery pressure is held at ED_best.
    # Its benefit depends on steam grade/price, vacuum cost and condenser utility,
    # so it must not enter the thermodynamic pressure-sensitivity BO at this stage.
    for col in recovery_columns:
        cid = _clean_id(col.get("id"))
        if cid:
            reasons.append(
                f"{cid}: 溶剂回收塔压力固定为 ED_best；当前不加入 *_PRESSURE，"
                "待后续公用工程价格/蒸汽等级/真空费用情景分析后再独立筛选。"
            )

    pseudo_pd = _light_process_stub(process_data, retrofit_family="pressure_swing_retrofit", status="candidate_from_ED")
    priority = 0.20
    verification = "flash_plus_aspen_rerun_required"
    if has_flash_ranges:
        if variables:
            priority = 0.68
            verification = "flash_material_pressure_bounds_ready"
        else:
            priority = 0.05
            verification = "flash_indicates_no_reliable_epsd_pressure_window"
            reasons.append(
                "含溶剂 Flash2 没有给出达到材料性阈值的 EPSD 分离塔压力窗口；"
                "本轮不强行变压，溶剂回收塔压力也保持 ED_best。"
            )
    else:
        reasons.append("尚未执行 Flash2，不能推导分离塔 EPSD/PED 的压敏方向；溶剂回收塔压力仍固定为 ED_best。")

    return {
        "case_id": "EPSD1",
        "family": "pressure_retrofit",
        "technology": "extractive_pressure_swing_distillation",
        "subtype": "ED_with_flash_approved_pressure_and_fixed_solvent_recovery_pressure",
        "verification_level": verification,
        "base_case": "ED_best",
        "modified_units": modified_units,
        "new_variables": variables,
        "pressure_pair_assignment": {
            "mapped_flash_pairs": epsd_recs,
            "mapped_risk_edges": mapped_edges,
            "risk_edge_plan": risk_plan,
            "fixed_recovery_pressure_columns": [_clean_id(c.get("id")) for c in recovery_columns],
            "skipped_raw_epsd_pairs": [r for r in raw_epsd_recs if _risk_edge_unordered_key(_as_dict(r).get("pair") or str(_as_dict(r).get("pair_key") or "").split("/")) not in {str(_as_dict(x).get("edge", {}).get("unordered_pair_key") or "") for x in mapped_edges}],
            "requires_flash_before_pressure_bo": not has_flash_ranges,
            "policy": "EPSD keeps ED topology; one pressure decision is mapped per actual N-1 process cut from its primary/backup cross-cut risk edge. Other components remain fixed multicomponent background. Solvent-recovery pressure remains fixed at ED_best.",
        },
        "process_data": pseudo_pd,
        "metrics": _extract_case_metrics(process_data),
        "priority": priority,
        "objective": ["retain_extractant", "jointly_optimize_ED_variables_around_ED_best", "pressure_polishing_if_flash_supported", "keep_solvent_recovery_pressure_fixed", "avoid_unphysical_high_pressure_ED_mapping"],
        "reasoning": reasons,
        "verification_payload": {
            "action": "flash_screen_then_copy_ED_best_and_guard_column_pressures",
            "flash_screen": _build_pressure_flash_screen_payload(process_data, state_dict, mode="extractive_pressure_swing"),
            "risk_edge_plan": risk_plan,
            "ed_pressure_policy": {
                "default_min_epsd_pressure_atm": 0.30,
                "default_max_epsd_pressure_atm": 9.00,
                "solvent_recovery_pressure_policy": "fixed_at_ED_best_pending_utility_price_analysis",
                "mapping": "pressurize/depressurize windows apply only to Flash-approved separation columns; solvent recovery pressure is not a BO variable",
            },
        },
        "can_enter_thermal_intensification": False,
    }

def _guess_product_streams(process_data: dict) -> List[dict]:
    streams = []
    for s in _as_list(_as_dict(process_data).get("streams")):
        if not isinstance(s, dict):
            continue
        sid = _stream_id(s)
        if not sid:
            continue
        up = sid.upper()
        if any(token in up for token in ["DC", "DIST", "TOP", "PROD", "PRODUCT"]):
            streams.append(s)
    return streams


def _guess_recycle_streams(process_data: dict, state_dict: Optional[dict] = None) -> List[dict]:
    solvent = _solvent_from_state(state_dict)
    streams = []
    for s in _as_list(_as_dict(process_data).get("streams")):
        if not isinstance(s, dict):
            continue
        sid = _stream_id(s)
        up = sid.upper()
        if any(token in up for token in ["REC", "RECYCLE", "SOLV", "BC3", "BOTTOM"]):
            streams.append(s)
        elif solvent and solvent in up:
            streams.append(s)
    return streams


def _guess_intercolumn_streams(process_data: dict) -> List[dict]:
    """识别塔间物流：这类膜用于进入下一塔前的预处理，而不是最终产品 polishing。"""
    out = []
    for s in _as_list(_as_dict(process_data).get("streams")):
        if not isinstance(s, dict):
            continue
        sid = _stream_id(s)
        if not sid:
            continue
        up = sid.upper()
        source = _clean_id(s.get("source") or s.get("source_block") or s.get("from") or s.get("source_port"))
        target = _clean_id(s.get("target") or s.get("target_block") or s.get("to") or s.get("target_port"))
        looks_like_transfer = any(tok in up for tok in ["C1B", "C1BC2", "W1", "W2", "FEEDC2", "FEEDC3", "TOC2", "TOC3", "INTER"])
        two_columns = bool(re.search(r"C\d", source) and re.search(r"C\d", target) and source != target)
        if looks_like_transfer or two_columns:
            out.append(s)
    return out


def _build_membrane_candidates(process_data: dict, state_dict: Optional[dict] = None) -> List[dict]:
    warnings = _constraint_warnings(process_data)
    product_streams = _guess_product_streams(process_data)
    recycle_streams = _guess_recycle_streams(process_data, state_dict)
    intercolumn_streams = _guess_intercolumn_streams(process_data)
    comps = _component_names_from_state(state_dict)
    solvent = _solvent_from_state(state_dict)
    out: List[dict] = []

    # 产品 polishing：针对最薄弱产品流，默认以 shortcut 膜模型验证。
    target_stream = ""
    target_component = ""
    impurity_components: List[str] = []
    for w in warnings:
        n = str(w.get("name") or "").upper()
        if "PURITY" in n or "RECOVERY" in n:
            target_stream = _clean_id(w.get("stream"))
            target_component = _clean_id(w.get("component"))
            break
    if not target_stream and product_streams:
        target_stream = _stream_id(product_streams[0])
    if not target_component and comps:
        target_component = comps[0]
    if comps:
        impurity_components = [c for c in comps if c != target_component and c != solvent][:3]

    pseudo_pd = _light_process_stub(process_data, retrofit_family="membrane_retrofit", status="shortcut_required")

    if target_stream:
        out.append({
            "case_id": "MEM1",
            "family": "membrane_retrofit",
            "technology": "membrane_retrofit",
            "subtype": "product_polishing",
            "installation_position": "after_product_condenser_or_product_cooler",
            "improvement_mechanism": "选择性移除产品中的痕量杂质，提高产品纯度；若成功，可反向降低原塔回流比或塔板冗余。",
            "verification_level": "shortcut_model_required",
            "base_case": "ED_best",
            "target_stream": target_stream,
            "target_component": target_component,
            "impurity_components": impurity_components,
            "screening_variables": {
                "stage_cut": [0.02, 0.15],
                "selectivity": [10, 100],
                "max_product_loss_fraction": 0.02,
            },
            "process_data": copy.deepcopy(pseudo_pd),
            "metrics": _extract_case_metrics(process_data),
            "priority": 0.70,
            "objective": ["product_polishing", "reduce_reflux_or_tray_demand"],
            "reasoning": [
                "ED 后产品流若只差少量杂质，膜 polishing 可作为低扰动后处理。",
                "当前版本只做 shortcut 筛选；需要膜选择性/通量数据后才能做严格结论。",
            ],
            "verification_payload": {
                "action": "run_shortcut_membrane_polishing",
                "feed_stream": target_stream,
                "target_component": target_component,
                "impurity_components": impurity_components,
                "stage_cut_grid": [0.02, 0.05, 0.10, 0.15],
                "selectivity_grid": [10, 20, 50, 100],
            },
            "can_enter_thermal_intensification": False,
        })

    # 溶剂循环 polishing：用于任意萃取剂 recycle 达到目标纯度的 last mile。
    recycle_stream = _stream_id(recycle_streams[0]) if recycle_streams else ""
    if recycle_stream or solvent:
        out.append({
            "case_id": "MEM2",
            "family": "membrane_retrofit",
            "technology": "membrane_retrofit",
            "subtype": "solvent_recycle_polishing",
            "installation_position": "before_solvent_recycle_return_or_after_solvent_recovery_bottom",
            "improvement_mechanism": "从萃取剂循环流中脱除痕量轻组分/夹带杂质，提高循环溶剂纯度并降低新鲜溶剂补充。",
            "verification_level": "shortcut_model_required",
            "base_case": "ED_best",
            "target_stream": recycle_stream or "SOLVENT_RECYCLE",
            "target_component": solvent or "SOLVENT",
            "impurity_components": [c for c in comps if c and c != solvent][:3],
            "screening_variables": {
                "stage_cut": [0.005, 0.10],
                "selectivity": [20, 200],
                "max_solvent_loss_fraction": 0.005,
            },
            "process_data": copy.deepcopy(pseudo_pd),
            "metrics": _extract_case_metrics(process_data),
            "priority": 0.68,
            "objective": ["solvent_recycle_purity>=0.9999", "reduce_fresh_solvent_makeup"],
            "reasoning": [
                "溶剂循环只差少量杂质时，膜/渗透汽化 polishing 可作为塔后精制支线。",
                "需检查溶剂黏度、膜污染、目标杂质挥发性和膜数据可信度。",
            ],
            "verification_payload": {
                "action": "run_shortcut_membrane_recycle_polishing",
                "feed_stream": recycle_stream or "SOLVENT_RECYCLE",
                "target_component": solvent or "SOLVENT",
                "stage_cut_grid": [0.005, 0.02, 0.05, 0.10],
                "selectivity_grid": [20, 50, 100, 200],
            },
            "can_enter_thermal_intensification": False,
        })
    # 塔间流股预处理：降低下一塔进料中的关键杂质负荷，改善下游塔可行域。
    inter_stream = _stream_id(intercolumn_streams[0]) if intercolumn_streams else ""
    if inter_stream:
        out.append({
            "case_id": "MEM3",
            "family": "membrane_retrofit",
            "technology": "membrane_retrofit",
            "subtype": "intercolumn_feed_conditioning",
            "installation_position": "between_columns_before_downstream_feed",
            "verification_level": "shortcut_then_downstream_aspen_rerun_required",
            "base_case": "ED_best",
            "target_stream": inter_stream,
            "target_component": target_component or (comps[0] if comps else "TARGET"),
            "impurity_components": impurity_components,
            "screening_variables": {
                "stage_cut": [0.02, 0.20],
                "selectivity": [5, 80],
                "max_target_loss_fraction": 0.03,
            },
            "process_data": copy.deepcopy(pseudo_pd),
            "metrics": _extract_case_metrics(process_data),
            "priority": 0.66,
            "objective": ["reduce_downstream_column_load", "improve_feasibility_margin", "stabilize_product_split"],
            "improvement_mechanism": "在塔间物流进入下一塔前选择性移除干扰组分，降低下游塔纯度/收率约束压力；通过后续 RadFrac 重跑确认是否可减少 RR/N。",
            "reasoning": [
                "塔间膜不是最终产品精制，而是分离负荷再分配。",
                "shortcut 通过后必须重跑下游塔，否则不能判断整体收率和能耗。",
            ],
            "verification_payload": {
                "action": "run_shortcut_membrane_intercolumn_conditioning_then_rerun_downstream_columns",
                "feed_stream": inter_stream,
                "stage_cut_grid": [0.02, 0.05, 0.10, 0.15, 0.20],
                "selectivity_grid": [5, 10, 20, 50, 80],
                "rerun_scope": "downstream_columns_only_if_possible_else_full_flowsheet",
            },
            "can_enter_thermal_intensification": False,
        })

    # 共沸对/近共沸对旁路膜：作为 ED 或 PSD 前后的局部破共沸支线。
    pairs = _component_pairs([c for c in comps if c != solvent])
    if pairs:
        out.append({
            "case_id": "MEM4",
            "family": "membrane_retrofit",
            "technology": "membrane_retrofit",
            "subtype": "azeotrope_pair_bypass",
            "installation_position": "side_stream_or_recycle_bypass_around_difficult_split",
            "verification_level": "literature_or_experimental_selectivity_required",
            "base_case": "ED_best",
            "target_pairs": pairs[:3],
            "screening_variables": {
                "stage_cut": [0.01, 0.20],
                "selectivity": [10, 200],
                "max_energy_penalty_kw": "to_be_estimated",
            },
            "process_data": copy.deepcopy(pseudo_pd),
            "metrics": _extract_case_metrics(process_data),
            "priority": 0.58,
            "objective": ["break_or_weaken_azeotrope", "reduce_solvent_or_pressure_swing_intensity"],
            "improvement_mechanism": "对难分共沸对建立侧线/循环旁路膜，利用膜选择性先改变局部组成，再返回塔系，从而降低塔内相平衡约束强度。",
            "reasoning": [
                "该方案最依赖膜材料数据；没有选择性和通量数据时只能作为概念候选。",
                "适合用于任意难分共沸对或近共沸对的局部破共沸筛选。",
            ],
            "verification_payload": {
                "action": "require_membrane_material_data_then_shortcut_bypass_scan",
                "target_pairs": pairs[:3],
                "stage_cut_grid": [0.01, 0.03, 0.05, 0.10, 0.20],
                "selectivity_grid": [10, 20, 50, 100, 200],
                "required_data": ["permeance", "selectivity", "temperature_limit", "solvent_compatibility", "fouling_risk"],
            },
            "can_enter_thermal_intensification": False,
        })

    return out





def _canonical_component_name(name: Any) -> str:
    """仅做字符串标准化，不内置任何体系热力学数据。"""
    return _clean_id(name)


def _feed_fractions_from_state(state_dict: Optional[dict]) -> dict:
    state_dict = _as_dict(state_dict)
    comps = _as_list(state_dict.get("components"))
    fracs = _as_list(state_dict.get("fractions"))
    out = {}
    if comps and fracs and len(comps) == len(fracs):
        for c, f in zip(comps, fracs):
            out[_canonical_component_name(c)] = _safe_float(f, 0.0) or 0.0
    else:
        for i in range(1, 8):
            c = state_dict.get(f"comp{i}") or state_dict.get(f"component{i}")
            f = state_dict.get(f"frac{i}") or state_dict.get(f"fraction{i}")
            if c is not None and f is not None:
                out[_canonical_component_name(c)] = _safe_float(f, 0.0) or 0.0
    s = sum(out.values())
    if s > 0:
        out = {k: v / s for k, v in out.items()}
    return out


def _flash_results_from_state(state_dict: Optional[dict]) -> List[dict]:
    """读取真实 Flash2 结果；不返回任何硬编码热力学数据。

    推荐写入格式：
    state_dict["pressure_swing_flash_results"] = {
        "cases": [
            {
              "case_type": "binary_pressure_vle",
              "pair": ["A", "B"],
              "pressure_kpa": 100,
              "with_solvent": false,
              "points": [{"x": 0.1, "y": 0.2}, ...]
            }
        ]
    }

    也兼容 points 中使用 liquid/vapour 或 x_i/y_i 字段。
    """
    state_dict = _as_dict(state_dict)
    containers = [
        state_dict.get("pressure_swing_flash_results"),
        state_dict.get("separation_flash_results"),
        state_dict.get("flash_results"),
        _as_dict(state_dict.get("separation_retrofit")).get("pressure_swing_flash_results"),
    ]
    cases: List[dict] = []
    for pack in containers:
        if not pack:
            continue
        if isinstance(pack, list):
            cases.extend([c for c in pack if isinstance(c, dict)])
        elif isinstance(pack, dict):
            raw_cases = pack.get("cases") or pack.get("flash_cases") or pack.get("results")
            if isinstance(raw_cases, list):
                cases.extend([c for c in raw_cases if isinstance(c, dict)])
            elif pack.get("pair") or pack.get("points"):
                cases.append(pack)
    return cases


def _point_xy_for_pair(point: dict, pair: List[str]) -> Tuple[Optional[float], Optional[float]]:
    p = _as_dict(point)
    c1 = _canonical_component_name(pair[0]) if pair else "A"

    for x_key, y_key in (("x", "y"), ("x1", "y1"), ("liquid_x", "vapour_y"), ("liquid", "vapour")):
        x = p.get(x_key)
        y = p.get(y_key)
        if isinstance(x, dict):
            x = x.get(c1) or x.get(pair[0])
        if isinstance(y, dict):
            y = y.get(c1) or y.get(pair[0])
        xf = _safe_float(x, None)
        yf = _safe_float(y, None)
        if xf is not None and yf is not None:
            return xf, yf

    liquid = _as_dict(p.get("liquid_mole_fraction") or p.get("liquid_composition") or p.get("x_composition"))
    vapour = _as_dict(p.get("vapour_mole_fraction") or p.get("vapor_mole_fraction") or p.get("y_composition"))
    if liquid and vapour:
        x = _safe_float(liquid.get(c1) or liquid.get(pair[0]), None)
        y = _safe_float(vapour.get(c1) or vapour.get(pair[0]), None)
        if x is not None and y is not None:
            return x, y
    return None, None


def _relative_volatility_from_xy(x: float, y: float, eps: float = 1e-12) -> Optional[float]:
    """二元体系相对挥发度：
    alpha_12 = (y1/x1) / (y2/x2) = y1(1-x1)/(x1(1-y1))
    """
    if x is None or y is None:
        return None
    if x <= eps or x >= 1.0 - eps or y <= eps or y >= 1.0 - eps:
        return None
    return (y * (1.0 - x)) / (x * (1.0 - y))


def _interpolate_azeotrope_x(points: List[Tuple[float, float]]) -> List[float]:
    """从 y-x 数据中通过 y-x=0 的符号变化线性插值寻找二元共沸组成。"""
    pts = sorted([(float(x), float(y)) for x, y in points if x is not None and y is not None], key=lambda t: t[0])
    roots: List[float] = []
    prev = None
    for x, y in pts:
        f = y - x
        if abs(f) < 1e-4:
            roots.append(x)
        if prev is not None:
            x0, f0 = prev
            if f0 * f < 0:
                denom = f - f0
                xr = x0 - f0 * (x - x0) / denom if abs(denom) > 1e-12 else 0.5 * (x + x0)
                roots.append(max(0.0, min(1.0, xr)))
        prev = (x, f)
    # 去重
    out = []
    for r in roots:
        if not any(abs(r - q) < 1e-3 for q in out):
            out.append(r)
    return out


def _analyze_flash_cases(flash_cases: List[dict], components: List[str]) -> dict:
    """把真实 Flash2 结果转成通用压敏指标。

    指标全部由公式计算，不依赖具体物系经验表。
    """
    grouped: Dict[Tuple[str, str, float, bool], List[Tuple[float, float]]] = {}
    alpha_records: List[dict] = []
    yx_deviation_records: List[float] = []

    for case in flash_cases:
        pair = [_canonical_component_name(x) for x in _as_list(case.get("pair"))[:2]]
        if len(pair) != 2:
            continue
        p_kpa = _safe_float(case.get("pressure_kpa") or case.get("pressure") or case.get("P_kPa"), None)
        if p_kpa is None:
            p_atm = _safe_float(case.get("pressure_atm"), None)
            p_kpa = None if p_atm is None else p_atm * 101.325
        if p_kpa is None:
            continue
        with_solvent = bool(case.get("with_solvent") or case.get("solvent") or "solvent" in str(case.get("case_type", "")).lower())
        key = (pair[0], pair[1], round(float(p_kpa), 6), with_solvent)
        points = _as_list(case.get("points") or case.get("vle_points") or case.get("results"))
        xy_points = []
        for pt in points:
            if not isinstance(pt, dict):
                continue
            x, y = _point_xy_for_pair(pt, pair)
            if x is None or y is None:
                continue
            xy_points.append((x, y))
            alpha = _relative_volatility_from_xy(x, y)
            if alpha is not None:
                alpha_records.append({"pair": pair, "pressure_kpa": p_kpa, "with_solvent": with_solvent, "x": x, "y": y, "alpha": alpha})
                yx_deviation_records.append(abs(y - x))
        if xy_points:
            grouped.setdefault(key, []).extend(xy_points)

    azeotrope_records: List[dict] = []
    for (a, b, p, with_solvent), pts in grouped.items():
        roots = _interpolate_azeotrope_x(pts)
        for xr in roots:
            comp = {c: 0.0 for c in components}
            comp[a] = xr
            comp[b] = 1.0 - xr
            azeotrope_records.append({
                "pair": [a, b],
                "pressure_kpa": p,
                "with_solvent": with_solvent,
                "x_first_component": xr,
                "composition": comp,
            })

    # 同一共沸对不同压力的组成移动量
    shift_by_pair: Dict[Tuple[str, str, bool], float] = {}
    pair_groups: Dict[Tuple[str, str, bool], List[float]] = {}
    for rec in azeotrope_records:
        pair = tuple(rec["pair"]) + (bool(rec.get("with_solvent")),)
        pair_groups.setdefault(pair, []).append(float(rec["x_first_component"]))
    for key, xs in pair_groups.items():
        if len(xs) >= 2:
            shift_by_pair[key] = max(xs) - min(xs)

    max_azeotrope_shift = max(shift_by_pair.values()) if shift_by_pair else 0.0

    # alpha 压敏：同一 pair/with_solvent 下 log(alpha) 随压力/组成的变化范围
    alpha_values = [abs(math.log(max(1e-12, r["alpha"]))) for r in alpha_records if _safe_float(r.get("alpha"), None) is not None]
    alpha_log_range = (max(alpha_values) - min(alpha_values)) if len(alpha_values) >= 2 else 0.0
    mean_yx_deviation = sum(yx_deviation_records) / len(yx_deviation_records) if yx_deviation_records else 0.0

    # 为三元图构造每个压力下的“Flash-derived azeotrope locus”。
    # 它是压敏筛选图，不等同于严格 RCM 精馏边界；严格边界后续由 RCM/RadFrac 验证。
    boundaries = []
    if len(components) >= 3 and azeotrope_records:
        by_p: Dict[Tuple[float, bool], List[dict]] = {}
        for rec in azeotrope_records:
            by_p.setdefault((float(rec["pressure_kpa"]), bool(rec.get("with_solvent"))), []).append(rec)
        for (p, with_solvent), recs in sorted(by_p.items()):
            if len(recs) >= 2:
                boundaries.append({
                    "pressure_kpa": p,
                    "pressure_atm": round(p / 101.325, 4),
                    "with_solvent": with_solvent,
                    "azeotrope_points": [
                        {"pair": "-".join(r["pair"]), "composition": r["composition"]}
                        for r in recs[:3]
                    ],
                    "boundary_type": "flash_azeotrope_locus_not_rigorous_rcm_boundary",
                })

    psd_component = max(0.0, min(1.0, max_azeotrope_shift / 0.10))
    alpha_component = max(0.0, min(1.0, alpha_log_range / math.log(2.0))) if alpha_log_range else 0.0
    yx_component = max(0.0, min(1.0, mean_yx_deviation / 0.20)) if mean_yx_deviation else 0.0
    generic_score = 0.50 * psd_component + 0.30 * alpha_component + 0.20 * yx_component

    return {
        "azeotrope_records": azeotrope_records,
        "alpha_records": alpha_records,
        "boundaries": boundaries,
        "metrics": {
            "max_azeotrope_shift": max_azeotrope_shift,
            "alpha_log_range": alpha_log_range,
            "mean_yx_deviation": mean_yx_deviation,
            "generic_pressure_score": generic_score,
        },
        "formula": {
            "relative_volatility": "alpha_ij=(y_i/x_i)/(y_j/x_j)=y_i(1-x_i)/(x_i(1-y_i))",
            "azeotrope_condition": "y_i-x_i=0 或 alpha_ij=1",
            "azeotrope_shift": "Delta_x_az=max_P(x_az(P))-min_P(x_az(P))",
            "pressure_score": "0.50*clip(Delta_x_az/0.10)+0.30*clip(range(|ln(alpha)|)/ln2)+0.20*clip(mean(|y-x|)/0.20)",
        },
    }


def build_pressure_swing_screen(process_data: dict, state_dict: Optional[dict] = None) -> dict:
    """通用变压支线路由。

    不内置任何特定物系数据；若没有真实 Flash2 结果，只输出待执行 payload。
    若 state_dict 中已有 Flash2 结果，则用相对挥发度、y-x 交点、共沸点迁移量进行 PSD/EPSD 路由。
    """
    components = [_canonical_component_name(c) for c in _non_solvent_components(state_dict)]
    solvent = _canonical_component_name(_solvent_from_state(state_dict))
    feed = _feed_fractions_from_state(state_dict)
    flash_cases = _flash_results_from_state(state_dict)

    if not flash_cases:
        payload_psd = _build_pressure_flash_screen_payload(process_data, state_dict, mode="solvent_free_psd")
        payload_epsd = _build_pressure_flash_screen_payload(process_data, state_dict, mode="extractive_pressure_swing")
        return {
            "status": "requires_aspen_flash_execution",
            "source": "generic_formula_no_hardcoded_data",
            "components": components[:3],
            "solvent": solvent or None,
            "feed_composition": feed,
            "pressure_grid_kpa": _default_pressure_grid_kpa("psd", state_dict),
            "pressure_grid_atm": _pressure_grid_atm(_default_pressure_grid_kpa("psd", state_dict)),
            "boundaries": [],
            "scores": {"PSD": None, "EPSD": None},
            "recommended_route": "PENDING_FLASH",
            "recommended_summary": "尚未执行 Aspen Flash2；需先根据 payload 生成多压力 VLE 数据，再由公式判断 PSD 或 EPSD。",
            "decision_formula": {
                "relative_volatility": "alpha_ij=(y_i/x_i)/(y_j/x_j)",
                "azeotrope_condition": "y_i=x_i 或 alpha_ij=1",
                "pressure_sensitive": "max_P(x_az)-min_P(x_az) > threshold",
                "route": "若无溶剂压力得分高且可跨越边界→PSD；若含溶剂得分显著更高或无溶剂不敏感→EPSD",
            },
            "flash_payloads": {
                "PSD": payload_psd,
                "EPSD": payload_epsd,
            },
        }

    # 拆分无溶剂与含溶剂结果，分别评分
    psd_cases = []
    epsd_cases = []
    for c in flash_cases:
        with_solvent = bool(c.get("with_solvent") or c.get("solvent") or "solvent" in str(c.get("case_type", "")).lower())
        (epsd_cases if with_solvent else psd_cases).append(c)

    psd_analysis = _analyze_flash_cases(psd_cases, components[:3]) if psd_cases else {"metrics": {"generic_pressure_score": 0.0}, "boundaries": []}
    epsd_analysis = _analyze_flash_cases(epsd_cases, components[:3]) if epsd_cases else {"metrics": {"generic_pressure_score": 0.0}, "boundaries": []}

    psd_score = _safe_float(_as_dict(psd_analysis.get("metrics")).get("generic_pressure_score"), 0.0) or 0.0
    epsd_score = _safe_float(_as_dict(epsd_analysis.get("metrics")).get("generic_pressure_score"), 0.0) or 0.0

    # 有溶剂基准流程但无含溶剂 Flash 时，不人为给 EPSD 加分；只给待补充提示。
    if epsd_cases and solvent:
        # 若含溶剂数据展示了更强 alpha 偏离或共沸迁移，EPSD 自然胜出。
        pass

    pressure_opt = _flash_pressure_optimization_from_state(state_dict)
    risk_plan = _risk_edge_plan_from_state(state_dict, process_data)
    psd_pressure_recs = _filter_recommendations_to_risk_edges(
        _flash_pressure_pair_recommendations(state_dict, mode="PSD", only_optimized=True), risk_plan
    )
    epsd_pressure_recs = _filter_recommendations_to_risk_edges(
        _flash_pressure_pair_recommendations(state_dict, mode="EPSD", only_optimized=True), risk_plan
    )
    # Route selection uses only material pressure recommendations. The maximum
    # pair score is used instead of a sum, so several weak pairs cannot outvote
    # one genuinely pressure-sensitive split.
    psd_pressure_score = max([(_safe_float(r.get("sensitivity_score"), 0.0) or 0.0) for r in psd_pressure_recs] or [0.0])
    epsd_pressure_score = max([(_safe_float(r.get("sensitivity_score"), 0.0) or 0.0) for r in epsd_pressure_recs] or [0.0])

    if pressure_opt:
        if not psd_pressure_recs and not epsd_pressure_recs:
            recommendation = "NO_PRESSURE_ROUTE_YET"
            summary = "严格材料性筛选后没有组分对保留塔压变量；不建议进入 PSD/EPSD 变压支线。"
        elif epsd_pressure_recs and (not psd_pressure_recs or epsd_pressure_score > psd_pressure_score * 1.05):
            recommendation = "EPSD"
            summary = "固定溶剂比下的含溶剂 Flash2 显示更强材料性压力效应，优先推荐 EPSD/PED。"
        elif psd_pressure_recs:
            recommendation = "PSD"
            summary = "严格筛选后仍有无溶剂组分对显示材料性压力效应，推荐 PSD 支线。"
        else:
            recommendation = "EPSD"
            summary = "仅含溶剂组分对通过材料性压力筛选，推荐 EPSD/PED 支线。"
    else:
        threshold = 0.35
        if max(psd_score, epsd_score) < threshold:
            recommendation = "NO_PRESSURE_ROUTE_YET"
            summary = "Flash2 结果尚未显示足够压敏性；优先保留 ED，并考虑膜或其他分离强化候选。"
        elif psd_score >= epsd_score:
            recommendation = "PSD"
            summary = "无溶剂 Flash2 初步得分更高；尚需材料性压力筛选确认。"
        else:
            recommendation = "EPSD"
            summary = "含溶剂 Flash2 初步得分更高；尚需固定 S/F 的材料性压力筛选确认。"

    boundaries = psd_analysis.get("boundaries") or epsd_analysis.get("boundaries") or []
    return {
        "status": "flash_analyzed",
        "source": "aspen_flash2_formula_evaluation",
        "components": components[:3],
        "solvent": solvent or None,
        "feed_composition": feed,
        "pressure_grid_kpa": sorted({float(c.get("pressure_kpa")) for c in flash_cases if _safe_float(c.get("pressure_kpa"), None) is not None}),
        "pressure_grid_atm": [],
        "boundaries": boundaries,
        "scores": {
            "PSD": psd_pressure_score if pressure_opt else psd_score,
            "EPSD": epsd_pressure_score if pressure_opt else epsd_score,
            "PSD_generic": psd_score,
            "EPSD_generic": epsd_score,
            "PSD_pressure_window": psd_pressure_score,
            "EPSD_pressure_window": epsd_pressure_score,
        },
        "pressure_optimization": pressure_opt,
        "risk_edge_plan": risk_plan,
        "recommended_route": recommendation,
        "recommended_summary": summary,
        "psd_analysis": psd_analysis,
        "epsd_analysis": epsd_analysis,
        "reasoning": [
            "所有判断均由 Flash2 的 y-x 数据、相对挥发度和共沸点随压力迁移量计算得到。",
            "塔压 BO 范围不再使用默认全网格，而是只采用 Flash 显示压敏的组分对及其最佳压力邻域。",
            "只有至少两个独立压力变化信号达到材料性阈值，或一个信号极强时，才生成塔压优化变量。",
            "EPSD 在固定 S/F 比率下比较压力，禁止把溶剂比变化误计为压力效应。",
            "推荐路线只决定进入 PSD 或 EPSD 支线，不直接替代后续严格模拟。",
        ],
    }


def _ternary_to_xy(comp: dict, order: Optional[List[str]] = None) -> Tuple[float, float]:
    order = order or ["A", "B", "C"]
    a = _safe_float(comp.get(order[0]), 0.0) or 0.0
    b = _safe_float(comp.get(order[1]), 0.0) or 0.0
    c = _safe_float(comp.get(order[2]), 0.0) or 0.0
    s = a + b + c
    if s <= 0:
        return 0.0, 0.0
    a, b, c = a / s, b / s, c / s
    x = b + 0.5 * c
    y = (math.sqrt(3.0) / 2.0) * c
    return x, y


def _separation_artifact_dir(out_dir: Optional[str] = None) -> str:
    """分离页图像统一输出目录。

    这里固定放在当前项目运行目录下，避免 Gradio allowed_paths 拒绝显示；
    文件名采用固定名覆盖写入，防止刷新分离页时持续堆积 PNG。
    """
    if out_dir is None:
        out_dir = os.path.join(os.getcwd(), "gradio_runtime_artifacts")
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


def save_pressure_swing_ternary_plot(screen: dict, out_dir: Optional[str] = None) -> Optional[str]:
    """保存三元筛选图；固定文件名覆盖，保留给导出/调试使用。

    分离页展示图已经切换为 y-x 三联图；该函数仍保留，避免外部调用断裂。
    """
    screen = _as_dict(screen)
    comps = screen.get("components") or ["A", "B", "C"]
    if len(comps) < 3:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None

    out_dir = _separation_artifact_dir(out_dir)
    out_path = os.path.join(out_dir, "separation_pressure_screen_ternary.png")

    fig = plt.figure(figsize=(8, 7), dpi=160)
    ax = fig.add_subplot(111)
    ax.set_aspect("equal")

    tri = [(0, 0), (1, 0), (0.5, math.sqrt(3.0) / 2.0), (0, 0)]
    ax.plot([p[0] for p in tri], [p[1] for p in tri], linewidth=1.5)
    ax.text(-0.04, -0.04, comps[0], fontsize=11)
    ax.text(1.02, -0.04, comps[1], fontsize=11, ha="right")
    ax.text(0.5, math.sqrt(3.0) / 2.0 + 0.04, comps[2], fontsize=11, ha="center")

    boundaries = _as_list(screen.get("boundaries"))
    for bd in boundaries:
        pts = _as_list(bd.get("azeotrope_points"))
        if len(pts) < 2:
            continue
        xy_points = []
        for item in pts[:3]:
            comp = _as_dict(item).get("composition") or {}
            xy_points.append(_ternary_to_xy(comp, comps[:3]))
        if len(xy_points) >= 2:
            ax.plot([p[0] for p in xy_points], [p[1] for p in xy_points], linestyle='-', linewidth=1.8, alpha=0.9,
                    label=f"Flash locus {int(_safe_float(bd.get('pressure_kpa'),0) or 0)} kPa")
            ax.scatter([p[0] for p in xy_points], [p[1] for p in xy_points], s=28)

    feed = _as_dict(screen.get("feed_composition"))
    if feed:
        fx, fy = _ternary_to_xy(feed, comps[:3])
        ax.scatter([fx], [fy], marker='*', s=170)
        ax.text(fx, fy - 0.035, "Feed", fontsize=10, ha="center")

    rec = screen.get('recommended_route') or 'NA'
    status = screen.get('status') or 'unknown'
    note = "Flash-derived azeotrope locus" if boundaries else "PENDING_FLASH: no Flash2 VLE results yet"
    ax.text(0.02, 0.96, f"Route: {rec}\nStatus: {status}\n{note}",
            transform=ax.transAxes, fontsize=9, va='top',
            bbox=dict(boxstyle='round', fc='white', ec='0.6'))

    ax.set_xlim(-0.08, 1.08)
    ax.set_ylim(-0.08, math.sqrt(3.0) / 2.0 + 0.10)
    ax.axis('off')
    if boundaries:
        ax.legend(loc='lower center', bbox_to_anchor=(0.5, -0.08), ncol=2, fontsize=8, frameon=False)
    fig.tight_layout()
    fig.savefig(out_path, dpi=160)
    plt.close(fig)
    try:
        print(f"[SEP_PLOT] overwrite ternary plot: {out_path}", flush=True)
    except Exception:
        pass
    return out_path


def _pair_key_from_case(case: dict) -> str:
    pair = [_canonical_component_name(x) for x in _as_list(_as_dict(case).get("pair"))[:2]]
    return "/".join(pair) if len(pair) == 2 else ""


def _unique_component_pairs_from_screen(screen: dict) -> List[List[str]]:
    comps = [_canonical_component_name(c) for c in _as_list(_as_dict(screen).get("components"))]
    comps = [c for c in comps if c]
    pairs: List[List[str]] = []
    for i in range(len(comps)):
        for j in range(i + 1, len(comps)):
            pairs.append([comps[i], comps[j]])
    return pairs[:3]


def _case_points_xy(case: dict) -> List[Tuple[float, float]]:
    pair = [_canonical_component_name(x) for x in _as_list(_as_dict(case).get("pair"))[:2]]
    if len(pair) != 2:
        return []
    out: List[Tuple[float, float]] = []
    for point in _as_list(_as_dict(case).get("points")):
        x, y = _point_xy_for_pair(_as_dict(point), pair)
        if x is None or y is None:
            continue
        if 0.0 <= float(x) <= 1.0 and 0.0 <= float(y) <= 1.0:
            out.append((float(x), float(y)))
    out.sort(key=lambda t: t[0])
    return out


def _pressure_screen_case_mode(case: dict) -> str:
    case = _as_dict(case)
    return "EPSD" if bool(case.get("with_solvent") or case.get("solvent")) else "PSD"


def _pressure_screen_recommendation(results: dict, pair_key: str, mode: str = "PSD") -> dict:
    target = str(pair_key or "").upper()
    for raw in _as_list(_as_dict(results.get("pressure_optimization")).get("pair_recommendations")):
        rec = _as_dict(raw)
        if str(rec.get("mode") or "").upper() != str(mode or "").upper():
            continue
        key = str(rec.get("pair_key") or _pair_key(rec.get("pair"))).upper()
        if key == target and rec.get("quality_verified") is not False:
            return rec
    return {}


def _pressure_direction_display_cases(results: dict, pair_key: str) -> Tuple[List[dict], dict, str]:
    """Return the exact cases used by the GUI pressure-direction panel.

    EPSD curves are preferred because they contain the real solvent/background
    environment used to qualify a tower pressure.  The selected fixed S/F ratio
    and source column from the recommendation are respected.  Legacy PSD cases
    are used only as a fallback when no validated EPSD curve is available.
    """
    all_cases = _flash_results_from_state({"pressure_swing_flash_results": results})
    target = str(pair_key or "").upper()

    def _matching(mode: str) -> Tuple[List[dict], dict]:
        rec = _pressure_screen_recommendation(results, pair_key, mode=mode)
        rows = [
            _as_dict(c) for c in all_cases
            if _pressure_screen_case_mode(_as_dict(c)) == mode
            and _pair_key_from_case(_as_dict(c)).upper() == target
        ]
        if mode == "EPSD" and rows:
            sf = _safe_float(rec.get("solvent_to_feed_ratio"), None)
            cid = str(rec.get("source_column_id") or "").upper().strip()
            if sf is not None:
                same_sf = [
                    c for c in rows
                    if _safe_float(c.get("solvent_to_feed_ratio"), None) is not None
                    and abs(float(_safe_float(c.get("solvent_to_feed_ratio"), 0.0)) - float(sf)) <= 1.0e-8
                ]
                if same_sf:
                    rows = same_sf
            if cid:
                same_col = [c for c in rows if str(c.get("source_column_id") or "").upper().strip() == cid]
                if same_col:
                    rows = same_col
        rows.sort(key=lambda c: _safe_float(c.get("pressure_kpa"), 0.0) or 0.0)
        return rows, rec

    epsd_rows, epsd_rec = _matching("EPSD")
    if epsd_rows:
        return epsd_rows, epsd_rec, "EPSD"
    psd_rows, psd_rec = _matching("PSD")
    return psd_rows, psd_rec, "PSD"


def _gaussian_kernel_smooth(values: List[float], sigma: float = GUI_FLASH_GAUSSIAN_SIGMA) -> List[float]:
    try:
        import numpy as np
        arr = np.asarray(values, dtype=float)
        if arr.size <= 2:
            return arr.tolist()
        sigma = max(0.25, float(sigma))
        radius = max(2, int(round(4.0 * sigma)))
        idx = np.arange(-radius, radius + 1, dtype=float)
        kernel = np.exp(-0.5 * (idx / sigma) ** 2)
        kernel /= kernel.sum()
        padded = np.pad(arr, (radius, radius), mode="reflect")
        return np.convolve(padded, kernel, mode="same")[radius:-radius].tolist()
    except Exception:
        return list(values)


def _monotone_cubic_curve(points: List[Tuple[float, float]], dense_count: int = GUI_FLASH_DENSE_PLOT_POINTS) -> Tuple[List[float], List[float]]:
    """Fritsch-Carlson monotone cubic interpolation for display only."""
    pts = sorted({(float(x), float(y)) for x, y in points if x is not None and y is not None})
    if len(pts) < 3:
        return [x for x, _ in pts], [y for _, y in pts]
    try:
        import numpy as np
        xs = np.asarray([p[0] for p in pts], dtype=float)
        ys = np.asarray([p[1] for p in pts], dtype=float)
        n = xs.size
        h = np.diff(xs)
        delta = np.diff(ys) / h
        slopes = np.zeros(n, dtype=float)
        slopes[0] = delta[0]
        slopes[-1] = delta[-1]
        for i in range(1, n - 1):
            if delta[i - 1] == 0.0 or delta[i] == 0.0 or delta[i - 1] * delta[i] <= 0.0:
                slopes[i] = 0.0
            else:
                w1 = 2.0 * h[i] + h[i - 1]
                w2 = h[i] + 2.0 * h[i - 1]
                slopes[i] = (w1 + w2) / (w1 / delta[i - 1] + w2 / delta[i])
        dense_x = np.linspace(float(xs.min()), float(xs.max()), max(240, int(dense_count)))
        dense_y = np.empty_like(dense_x)
        seg = 0
        for k, x in enumerate(dense_x):
            while seg < n - 2 and x > xs[seg + 1]:
                seg += 1
            width = xs[seg + 1] - xs[seg]
            t = 0.0 if width == 0.0 else (x - xs[seg]) / width
            h00 = 2.0 * t**3 - 3.0 * t**2 + 1.0
            h10 = t**3 - 2.0 * t**2 + t
            h01 = -2.0 * t**3 + 3.0 * t**2
            h11 = t**3 - t**2
            dense_y[k] = (
                h00 * ys[seg] + h10 * width * slopes[seg]
                + h01 * ys[seg + 1] + h11 * width * slopes[seg + 1]
            )
        return dense_x.tolist(), dense_y.tolist()
    except Exception:
        return [x for x, _ in pts], [y for _, y in pts]


def _pressure_screen_smoothed_curve(points: List[Tuple[float, float]], sigma: float = GUI_FLASH_GAUSSIAN_SIGMA) -> Tuple[List[float], List[float]]:
    dense_x, dense_y = _monotone_cubic_curve(points)
    if len(dense_x) < 3:
        return dense_x, dense_y
    smooth_y = _gaussian_kernel_smooth(dense_y, sigma=sigma)
    if smooth_y:
        smooth_y[0] = dense_y[0]
        smooth_y[-1] = dense_y[-1]
    return dense_x, [max(0.0, min(1.0, float(v))) for v in smooth_y]


def _interpolate_curve_y(xs: List[float], ys: List[float], x_query: float) -> Optional[float]:
    if not xs or not ys:
        return None
    try:
        import numpy as np
        return float(np.interp(float(x_query), xs, ys))
    except Exception:
        return None


def _pressure_screen_marker(case: dict, rec: dict, sigma: float = GUI_FLASH_GAUSSIAN_SIGMA) -> Tuple[Optional[float], Optional[float], str]:
    """One black square per pressure curve.

    Use the azeotrope-like crossing when available; otherwise use the maximum
    absolute y-x separation on that curve.  This fixes the old arbitrary x≈0.5
    marker and keeps the selection physically interpretable.
    """
    pts = _case_points_xy(case)
    if len(pts) < 2:
        return None, None, ""
    pressure = _safe_float(_as_dict(case).get("pressure_kpa"), None)
    crossing = None
    for row in _as_list(_as_dict(rec).get("pressure_metrics")):
        row = _as_dict(row)
        rp = _safe_float(row.get("pressure_kpa"), None)
        if pressure is not None and rp is not None and abs(pressure - rp) < 1e-8:
            crossing = _safe_float(row.get("crossing_x"), None)
            break
    if crossing is None:
        roots = _interpolate_azeotrope_x(pts)
        crossing = roots[0] if roots else None
    sx, sy = _pressure_screen_smoothed_curve(pts, sigma=sigma)
    if crossing is not None and pts[0][0] <= crossing <= pts[-1][0]:
        y_cross = _interpolate_curve_y(sx, sy, crossing)
        if y_cross is not None:
            return float(crossing), float(y_cross), "azeotrope-like crossing"
    if sx and sy:
        idx = max(range(len(sx)), key=lambda i: abs(float(sy[i]) - float(sx[i])))
        return float(sx[idx]), float(sy[idx]), "maximum |y-x|"
    best = max(pts, key=lambda xy: abs(xy[1] - xy[0]))
    return float(best[0]), float(best[1]), "maximum |y-x|"


def _pressure_direction_text(rec: dict) -> str:
    rec = _as_dict(rec)
    direction = str(rec.get("direction") or "").lower()
    if "narrow" in direction or "near_best" in direction:
        action = "Search near best pressure"
    elif "depress" in direction:
        action = "Decrease pressure (vacuum)"
    elif direction.startswith("pressurize") or direction == "increase_pressure":
        action = "Increase pressure"
    elif rec.get("should_optimize_pressure"):
        action = "Optimize pressure locally"
    else:
        action = "Hold near base pressure"
    best = _safe_float(rec.get("best_pressure_kpa"), None)
    bounds = _as_list(rec.get("recommended_bounds_kpa"))
    details = []
    if best is not None:
        details.append(f"best = {best:g} kPa")
    if len(bounds) >= 2:
        details.append(f"P_BO = {_safe_float(bounds[0], 0.0):g}-{_safe_float(bounds[1], 0.0):g} kPa")
    return action + (("\n" + "; ".join(details)) if details else "")


def _configure_pressure_plot_font(plt) -> None:
    try:
        from matplotlib import font_manager
        names = {f.name for f in font_manager.fontManager.ttflist}
        plt.rcParams["font.family"] = "Times New Roman" if "Times New Roman" in names else "DejaVu Serif"
    except Exception:
        plt.rcParams["font.family"] = "DejaVu Serif"
    plt.rcParams["mathtext.fontset"] = "stix"
    plt.rcParams["axes.unicode_minus"] = False


def _draw_pressure_direction_panel(ax, results: dict, pair_key: str, sigma: float = GUI_FLASH_GAUSSIAN_SIGMA) -> dict:
    pair_cases, rec, display_mode = _pressure_direction_display_cases(results, pair_key)
    best_pressure = _safe_float(rec.get("best_pressure_kpa"), None)

    diag, = ax.plot([0.0, 1.0], [0.0, 1.0], linestyle="--", linewidth=1.1, label="y = x")
    handles = [diag]
    labels = ["y = x"]
    marker_added = False
    marker_count = 0
    main_marker_type = ""
    curve_count = 0
    for case in pair_cases:
        pts = _case_points_xy(case)
        if len(pts) < 2:
            continue
        p_kpa = _safe_float(_as_dict(case).get("pressure_kpa"), None)
        sx, sy = _pressure_screen_smoothed_curve(pts, sigma=sigma)
        line, = ax.plot(sx, sy, linewidth=2.25, alpha=0.97, label=f"{p_kpa:g} kPa" if p_kpa is not None else "P=NA")
        ax.plot(
            [p[0] for p in pts], [p[1] for p in pts], linestyle="none", marker="o", markersize=4.3,
            markerfacecolor="white", markeredgewidth=1.1, color=line.get_color(),
        )
        handles.append(line)
        labels.append(f"{p_kpa:g} kPa" if p_kpa is not None else "P=NA")
        curve_count += 1
        mx, my, marker_type = _pressure_screen_marker(case, rec, sigma=sigma)
        if mx is not None and my is not None:
            is_best = best_pressure is not None and p_kpa is not None and abs(best_pressure - p_kpa) < 1e-8
            marker, = ax.plot(
                [mx], [my], linestyle="none", marker="s", markersize=8.2 if is_best else 6.8,
                markerfacecolor="black", markeredgecolor="white", markeredgewidth=0.8, color="black", zorder=7,
                label="Selected indicator" if not marker_added else None,
            )
            marker_count += 1
            if not marker_added:
                handles.append(marker)
                labels.append("Selected indicator")
                marker_added = True
            if is_best:
                main_marker_type = marker_type

    if "/" in pair_key:
        c1, c2 = pair_key.split("/", 1)
    else:
        c1, c2 = pair_key, "B"
    ax.set_xlim(0.0, 1.0)
    ax.set_ylim(0.0, 1.0)
    ax.set_xlabel(f"x({c1}) liquid")
    ax.set_ylabel(f"y({c1}) vapor")
    ax.set_title(f"{c1} / {c2}", fontsize=13.0)
    ax.grid(alpha=0.16, linewidth=0.8)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    if curve_count:
        message = _pressure_direction_text(rec)
        if main_marker_type:
            message += f"\nmain marker = {main_marker_type}"
        message += f"\nreal x points = {len(_case_points_xy(pair_cases[0])) if pair_cases else 0}; black squares = {marker_count}"
        ax.text(
            0.03, 0.97, message, transform=ax.transAxes, ha="left", va="top", fontsize=8.6,
            bbox=dict(boxstyle="round,pad=0.32", facecolor="white", alpha=0.86, edgecolor="0.75"),
        )
        ax.legend(handles, labels, loc="lower right", fontsize=8.0, frameon=False, handlelength=2.5)
    else:
        ax.text(0.5, 0.54, "No validated y-x points", ha="center", va="center", fontsize=11)
        ax.text(0.5, 0.45, "Check Aspen component registration / Flash data", ha="center", va="center", fontsize=9)
    return {
        "pair_key": pair_key,
        "display_mode": display_mode,
        "curve_count": curve_count,
        "marker_count": marker_count,
        "best_pressure_kpa": best_pressure,
        "recommendation": rec,
    }


def _pressure_direction_csv_path(out_dir: str) -> str:
    base = os.path.abspath(str(out_dir))
    if os.path.basename(base).lower() == "figures":
        data_dir = os.path.join(os.path.dirname(base), "data")
    else:
        data_dir = base
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "pressure_direction_yx_plot_data.csv")


def _export_pressure_direction_plot_csv(
    results: dict,
    pair_order: Sequence[str],
    out_dir: str,
    sigma: float = GUI_FLASH_GAUSSIAN_SIGMA,
) -> Optional[str]:
    """Export every raw and smoothed point used by the unchanged GUI figure."""
    rows: List[dict] = []
    for pair_key in pair_order:
        pair_cases, rec, display_mode = _pressure_direction_display_cases(results, pair_key)
        best_p = _safe_float(rec.get("best_pressure_kpa"), None)
        for curve_index, case in enumerate(pair_cases):
            case = _as_dict(case)
            pressure_kpa = _safe_float(case.get("pressure_kpa"), None)
            pressure_atm = float(pressure_kpa) / 101.325 if pressure_kpa is not None else None
            sf = _safe_float(case.get("solvent_to_feed_ratio"), None)
            common = {
                "display_mode": display_mode,
                "pair_key": pair_key,
                "source_column_id": case.get("source_column_id") or rec.get("source_column_id"),
                "risk_role": case.get("risk_role") or rec.get("risk_role"),
                "top_component": case.get("top_component") or rec.get("top_component") or _as_dict(case.get("risk_edge_context")).get("top_component"),
                "bottom_component": case.get("bottom_component") or rec.get("bottom_component") or _as_dict(case.get("risk_edge_context")).get("bottom_component"),
                "solvent": case.get("solvent"),
                "solvent_to_feed_ratio": sf,
                "reference_solvent_to_feed_ratio": case.get("reference_solvent_to_feed_ratio") or rec.get("reference_solvent_to_feed_ratio") or _as_dict(case.get("risk_edge_context")).get("reference_solvent_to_feed_ratio"),
                "pressure_kpa": pressure_kpa,
                "pressure_atm": pressure_atm,
                "curve_index": curve_index,
                "is_best_pressure": int(
                    best_p is not None and pressure_kpa is not None and abs(float(best_p) - float(pressure_kpa)) <= 1.0e-8
                ),
            }
            raw_pts = _case_points_xy(case)
            for idx, (xv, yv) in enumerate(raw_pts):
                rows.append({**common, "point_kind": "raw_aspen", "point_index": idx, "x_liquid": xv, "y_vapor": yv, "is_selected_indicator": 0, "marker_type": ""})
            sx, sy = _pressure_screen_smoothed_curve(raw_pts, sigma=sigma)
            for idx, (xv, yv) in enumerate(zip(sx, sy)):
                rows.append({**common, "point_kind": "smoothed_plot", "point_index": idx, "x_liquid": xv, "y_vapor": yv, "is_selected_indicator": 0, "marker_type": ""})
            mx, my, marker_type = _pressure_screen_marker(case, rec, sigma=sigma)
            if mx is not None and my is not None:
                rows.append({**common, "point_kind": "selected_indicator", "point_index": 0, "x_liquid": mx, "y_vapor": my, "is_selected_indicator": 1, "marker_type": marker_type})

    if not rows:
        return None
    path = _pressure_direction_csv_path(out_dir)
    fieldnames = [
        "display_mode", "pair_key", "source_column_id", "risk_role", "top_component", "bottom_component", "solvent",
        "solvent_to_feed_ratio", "reference_solvent_to_feed_ratio", "pressure_kpa", "pressure_atm", "curve_index",
        "point_kind", "point_index", "x_liquid", "y_vapor",
        "is_selected_indicator", "is_best_pressure", "marker_type",
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


def save_pressure_direction_gui_artifacts(
    state_dict: Optional[dict],
    results: Optional[dict],
    out_dir: Optional[str] = None,
    sigma: float = GUI_FLASH_GAUSSIAN_SIGMA,
) -> dict:
    """Save one total PNG and one independent PNG per binary PSD pair."""
    results = _as_dict(results)
    state_dict = _as_dict(state_dict)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return {}
    _configure_pressure_plot_font(plt)
    out_dir = _separation_artifact_dir(out_dir)

    all_cases = _flash_results_from_state({"pressure_swing_flash_results": results})
    epsd_cases = [c for c in all_cases if _pressure_screen_case_mode(c) == "EPSD"]
    psd_cases = [c for c in all_cases if _pressure_screen_case_mode(c) == "PSD"]
    pair_order: List[str] = []
    for case in (epsd_cases or psd_cases):
        key = _pair_key_from_case(case)
        if key and key not in pair_order:
            pair_order.append(key)
    if not pair_order:
        non_solvent = _non_solvent_components(state_dict)
        pair_order = ["/".join(p) for p in _component_pairs(non_solvent)[:3]]
    pair_order = pair_order[:3]
    if not pair_order:
        return {}

    panel_rows = []
    fig, axes = plt.subplots(1, len(pair_order), figsize=(6.65 * len(pair_order), 5.4), squeeze=False)
    for ax, pair_key in zip(axes[0], pair_order):
        meta = _draw_pressure_direction_panel(ax, results, pair_key, sigma=sigma)
        safe_pair = re.sub(r"[^A-Za-z0-9]+", "_", pair_key).strip("_").lower()
        panel_path = os.path.join(out_dir, f"pressure_direction_{safe_pair}.png")
        single_fig, single_ax = plt.subplots(1, 1, figsize=(6.65, 5.4))
        _draw_pressure_direction_panel(single_ax, results, pair_key, sigma=sigma)
        single_fig.tight_layout()
        single_fig.savefig(panel_path, dpi=260, bbox_inches="tight")
        plt.close(single_fig)
        meta["path"] = panel_path
        meta["caption"] = pair_key
        panel_rows.append(meta)

    fig.suptitle("Pressure-direction screening", fontsize=16)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    summary_path = os.path.join(out_dir, "pressure_direction_summary.png")
    fig.savefig(summary_path, dpi=260, bbox_inches="tight")
    plt.close(fig)
    csv_path = _export_pressure_direction_plot_csv(results, pair_order, out_dir, sigma=sigma)
    return {
        "pressure_direction_summary_png": summary_path,
        "pressure_direction_panel_pngs": panel_rows,
        "pressure_direction_yx_csv": csv_path,
        # Compatibility key used by existing GUI bindings.
        "yx_triptych": summary_path,
    }


def save_pressure_swing_yx_triptych_plot(
    state_dict: Optional[dict],
    screen: Optional[dict] = None,
    out_dir: Optional[str] = None,
) -> Optional[str]:
    """Compatibility wrapper; final GUI figures are generated from raw runner results."""
    results = _as_dict(_as_dict(state_dict).get("pressure_swing_flash_results"))
    artifacts = save_pressure_direction_gui_artifacts(state_dict, results, out_dir=out_dir)
    return artifacts.get("pressure_direction_summary_png") if artifacts else None


def render_pressure_swing_screen_summary(screen: dict) -> str:
    screen = _as_dict(screen)
    rec = screen.get("recommended_route") or "NA"
    scores = _as_dict(screen.get("scores"))
    lines = [
        f"### 变压支线 Flash 筛选建议",
        f"- Flash 推荐路线：**{rec}**",
        f"- 当前执行策略：**EPSD1 优先验证；PSD 暂停设计**",
        f"- PSD score：**{_fmt(scores.get('PSD'), 2)}**；EPSD score：**{_fmt(scores.get('EPSD'), 2)}**",
        f"- 状态：`{screen.get('status', 'unknown')}`",
        f"- 说明：{screen.get('recommended_summary') or 'NA'}",
    ]
    pressure_opt = _as_dict(screen.get("pressure_optimization"))
    recs = _as_list(pressure_opt.get("pair_recommendations"))
    if recs:
        lines.append("")
        lines.append("#### Flash 推导的塔压趋势与 EPSD1 压力映射")
        lines.append("- 仅将通过材料性筛选的组分对映射为塔压 BO 变量；固定 S/F 下压力曲线近似重合的组分对会被剔除。")
        for r in recs[:8]:
            r = _as_dict(r)
            pair_key = r.get("pair_key") or "NA"
            mode = r.get("mode") or "NA"
            score = _fmt(r.get("sensitivity_score"), 3)
            if r.get("should_optimize_pressure"):
                b = _as_list(r.get("recommended_bounds_kpa"))
                direction = r.get("direction") or "optimize"
                if len(b) >= 2:
                    lines.append(f"- `{mode} {pair_key}`：**{direction}**，BO 范围 `{_fmt(b[0],1,'kPa')}–{_fmt(b[1],1,'kPa')}`，score={score}")
                else:
                    lines.append(f"- `{mode} {pair_key}`：**{direction}**，score={score}")
            else:
                lines.append(f"- `{mode} {pair_key}`：不建议变压；{r.get('reason') or '压敏性不足'}")
    reasons = _as_list(screen.get("reasoning"))
    if reasons:
        lines.append("- 判断依据：")
        for r in reasons[:5]:
            lines.append(f"  - {r}")
    return "\n".join(lines)


def get_separation_page_artifacts(state_dict: Optional[dict]) -> Tuple[str, Optional[str]]:
    state_dict = _as_dict(state_dict)
    package = get_separation_package(state_dict) or ensure_separation_retrofit_package(state_dict)
    screen = _as_dict(package.get("pressure_swing_screen"))
    summary = render_pressure_swing_screen_summary(screen) if screen else "### 变压支线 Flash 筛选建议\n暂无结果。"

    plot_paths = _as_dict(package.get("plot_paths"))
    result_paths = _as_dict(_as_dict(state_dict.get("pressure_swing_flash_results")).get("plot_paths"))
    for source in (plot_paths, result_paths):
        for key in ("pressure_direction_summary_png", "pressure_direction_summary", "yx_triptych", "binary_yx", "ternary"):
            path = source.get(key)
            if path and os.path.exists(str(path)):
                return summary, str(path)
    return summary, None


def get_separation_page_gui_artifacts(state_dict: Optional[dict]) -> Tuple[str, Optional[str], List[Tuple[str, str]]]:
    """Return summary, total PNG, and independent panel PNGs for Gradio."""
    state = _as_dict(state_dict)
    summary, main_path = get_separation_page_artifacts(state)
    package = get_separation_package(state)
    sources = [
        _as_dict(package.get("plot_paths")),
        _as_dict(_as_dict(state.get("pressure_swing_flash_results")).get("plot_paths")),
        _as_dict(state.get("pressure_screen_gui_display")),
    ]
    gallery: List[Tuple[str, str]] = []
    seen = set()
    for source in sources:
        rows = source.get("pressure_direction_panel_pngs") or source.get("panel_figures_png") or []
        for raw in _as_list(rows):
            if isinstance(raw, dict):
                path = raw.get("path")
                caption = raw.get("caption") or raw.get("pair_key") or raw.get("display_pair") or "pressure pair"
            elif isinstance(raw, (list, tuple)) and raw:
                path = raw[0]
                caption = raw[1] if len(raw) > 1 else "pressure pair"
            else:
                path = raw
                caption = "pressure pair"
            if not path:
                continue
            path = os.path.abspath(str(path))
            if path in seen or not os.path.exists(path):
                continue
            seen.add(path)
            gallery.append((path, str(caption)))
    return summary, main_path, gallery


def build_separation_retrofit_analysis(process_data: dict, state_dict: Optional[dict] = None) -> dict:
    """生成 ED 后分离强化候选，不做换热强化。"""
    process_data = _as_dict(process_data)
    if not process_data:
        return {
            "agent_version": AGENT_VERSION,
            "status": "no_process_data",
            "cases": [],
            "ranked_candidates": [],
            "message": "尚无 ED/BO 结构化结果。请先完成基础流程设计与参数优化。",
        }

    ed_case = _make_ed_case(process_data)
    pressure_swing_screen = build_pressure_swing_screen(process_data, state_dict)
    # v20 policy: PSD topology reconstruction is paused.  The Flash screen may
    # still say that a solvent-free PSD route is thermodynamically attractive,
    # but the executable branch for this iteration is EPSD/PED only.
    pressure = _build_pressure_retrofit_candidate(process_data, state_dict)
    membranes = _build_membrane_candidates(process_data, state_dict)
    candidates = [pressure] + membranes

    rec = str(_as_dict(pressure_swing_screen).get("recommended_route") or "").upper()
    if rec == "PSD":
        pressure["priority"] = max(_safe_float(pressure.get("priority"), 0.0) or 0.0, 0.60 if pressure.get("new_variables") else 0.25)
        pressure["route_recommendation"] = "epsd_first_even_if_flash_prefers_psd"
        pressure.setdefault("reasoning", []).append(
            "当前阶段暂停 PSD 拓扑重建；先验证 EPSD/PED 是否能在保留 ED 拓扑下通过受限塔压窗口获得改进。"
        )
    elif rec == "EPSD":
        pressure["priority"] = (_safe_float(pressure.get("priority"), 0.0) or 0.0) + 0.12
        pressure["route_recommendation"] = "recommended_by_flash_screen"

    candidates = [c for c in candidates if isinstance(c, dict) and _safe_float(c.get("priority"), 0.0) > 0]
    candidates.sort(key=lambda x: _safe_float(x.get("priority"), 0.0), reverse=True)

    return {
        "agent_version": AGENT_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "status": "ready",
        "base_case": ed_case,
        "cases": [ed_case] + candidates,
        "ranked_candidates": candidates,
        "column_summary": _column_summary(process_data),
        "constraint_warnings": _constraint_warnings(process_data)[:12],
        "pressure_swing_screen": pressure_swing_screen,
        "routing_rule": "分离强化候选必须先完成 Aspen/shortcut 验证，形成新的 process_data 后，再进入过程强化设计。",
    }


def ensure_separation_retrofit_package(state_dict: Optional[dict]) -> dict:
    state_dict = _as_dict(state_dict)
    process_data = get_ed_process_data(state_dict)
    old_pkg = get_separation_package(state_dict)
    old_plot_paths = _as_dict(old_pkg.get("plot_paths"))
    old_runner_plot_owner = bool(old_pkg.get("runner_plot_owner"))
    old_runner_plot_version = old_pkg.get("runner_plot_version")
    old_runner_plot_updated_at = old_pkg.get("runner_plot_updated_at")

    analysis = build_separation_retrofit_analysis(process_data, state_dict)
    active_case_id = _normalize_case_id(old_pkg.get("active_case_id") or "ED_best")
    available_ids = [str(c.get("case_id")) for c in _as_list(analysis.get("cases")) if isinstance(c, dict)]
    if _normalize_case_id(active_case_id) not in {_normalize_case_id(x) for x in available_ids}:
        active_case_id = "ED_best"
    analysis["active_case_id"] = active_case_id

    # Runner owns separation-page final plots, like BO owns its final figures.
    # Rebuilding the analysis package must not erase paths written by the runner.
    if old_plot_paths:
        analysis["plot_paths"] = old_plot_paths
    if old_runner_plot_owner:
        analysis["runner_plot_owner"] = True
    if old_runner_plot_version:
        analysis["runner_plot_version"] = old_runner_plot_version
    if old_runner_plot_updated_at:
        analysis["runner_plot_updated_at"] = old_runner_plot_updated_at

    state_dict["separation_retrofit"] = analysis
    return analysis


# ============================================================
# 激活 / 选择分离方案
# ============================================================

def _find_case_by_id(package: dict, case_id: str) -> dict:
    target_raw = str(case_id or "").strip()
    target_norm = _normalize_case_id(target_raw)
    target_keys = {target_raw.lower(), str(target_norm).lower()}

    # 精确 ID / 兼容别名优先。
    for case in _as_list(_as_dict(package).get("cases")):
        if not isinstance(case, dict):
            continue
        cid = str(case.get("case_id") or "")
        case_keys = {str(a).lower() for a in _case_id_aliases(cid)}
        if target_keys & case_keys:
            return case

    # 宽松匹配：用户说 PSD/EPSD/MEM/ED。
    target_low = target_raw.lower()
    target_norm_low = str(target_norm).lower()
    for case in _as_list(_as_dict(package).get("cases")):
        if not isinstance(case, dict):
            continue
        cid = str(case.get("case_id") or "").lower()
        fam = str(case.get("family") or "").lower()
        tech = str(case.get("technology") or "").lower()
        subtype = str(case.get("subtype") or "").lower()
        hay = " ".join([cid, fam, tech, subtype])
        if target_low and (target_low in hay or target_norm_low in hay):
            return case
    return {}


def activate_separation_case(state_dict: Optional[dict], requested_case_id: str) -> Tuple[dict, str]:
    """把某个分离方案设为后续换热强化输入。"""
    state_dict = _as_dict(state_dict)
    package = ensure_separation_retrofit_package(state_dict)
    if _normalize_case_id(requested_case_id) == "PSD1":
        package["active_case_id"] = "EPSD1" if _find_case_by_id(package, "EPSD1") else "ED_best"
        state_dict["separation_retrofit"] = package
        return state_dict, "PSD1 当前已暂停：先完成 EPSD1 的受限塔压优化验证；EPSD 成功后再恢复 PSD 拓扑与循环联立设计。"
    case = _find_case_by_id(package, requested_case_id)
    if not case:
        return state_dict, f"未找到分离方案：{requested_case_id}。可用方案：" + ", ".join(str(c.get("case_id")) for c in _as_list(package.get("cases")) if isinstance(c, dict))

    cid = str(case.get("case_id") or "ED_best")
    package["active_case_id"] = cid
    state_dict["separation_retrofit"] = package

    level = str(case.get("verification_level") or "unknown")
    if case.get("can_enter_thermal_intensification") or cid == "ED_best" or level in {"aspen_strict", "aspen_verified"}:
        msg = f"已将 `{cid}` 设为后续过程强化输入。验证等级：{level}。"
    else:
        msg = (
            f"已暂时选中 `{cid}`，但验证等级为 `{level}`。"
            "该候选还需要先完成分离支线验证，严格 Aspen 结果出来后再进入换热强化更合理。"
        )
    return state_dict, msg


# ============================================================
# 报告与本地回复
# ============================================================

def _candidate_line(case: dict, idx: int = 0) -> str:
    cid = case.get("case_id") or "UNKNOWN"
    tech = case.get("technology") or case.get("family") or "unknown"
    priority = _safe_float(case.get("priority"), None)
    level = case.get("verification_level") or "unknown"
    units = ", ".join(_as_list(case.get("modified_units"))) or case.get("target_stream") or "—"
    ptxt = "NA" if priority is None else f"{priority:.2f}"
    prefix = f"{idx}. " if idx else ""
    return f"{prefix}`{cid}` | {tech} | priority={ptxt} | 验证等级={level} | 改造位置={units}"


def render_separation_retrofit_report(process_data: dict, analysis: Optional[dict] = None, state_dict: Optional[dict] = None) -> str:
    if not process_data:
        return "当前还没有基础 ED/BO 结果。请先完成基础流程设计与参数优化。"
    analysis = analysis or build_separation_retrofit_analysis(process_data, state_dict)
    active_id = _as_dict(state_dict).get("separation_retrofit", {}).get("active_case_id") if state_dict else analysis.get("active_case_id", "ED_best")
    lines = [
        "# 分离强化设计报告",
        "",
        f"- Agent version: `{analysis.get('agent_version', AGENT_VERSION)}`",
        f"- Base status: `{_as_dict(process_data).get('status', 'unknown')}`",
        f"- Active separation case: `{active_id or 'ED_best'}`",
        "",
        "## 1. 总体策略",
        "基础 ED 流程作为基准；分离强化页先做变压支线 Flash 筛选。本轮暂停 PSD 拓扑重建，优先验证 EPSD1 的受限塔压微调是否能带来改进，并同步保留多位置膜候选。分离方案验证后才能进入后续热泵或热集成优化。",
        "",
        "## 2. 塔数据摘要",
    ]
    cols = _as_list(analysis.get("column_summary"))
    if not cols:
        lines.append("暂无 columns 数据。")
    else:
        lines.append("| 塔 | 角色 | 顶温/°C | 釜温/°C | 再沸/kW | 冷凝/kW |")
        lines.append("|---|---|---:|---:|---:|---:|")
        for c in cols:
            lines.append(
                f"| {c.get('id')} | {c.get('role')} | {_fmt(c.get('top_temperature_C'))} | {_fmt(c.get('bottom_temperature_C'))} | "
                f"{_fmt(c.get('reboiler_kw_abs'))} | {_fmt(c.get('condenser_kw_abs'))} |"
            )

    lines.extend(["", "## 3. 变压支线推荐"])
    screen = _as_dict(analysis.get("pressure_swing_screen"))
    if not screen:
        lines.append("暂无变压支线筛选结果。")
    else:
        lines.append(f"- 推荐路线：**{screen.get('recommended_route') or 'NA'}**")
        lines.append(f"- 说明：{screen.get('recommended_summary') or 'NA'}")
        for r in _as_list(screen.get("reasoning"))[:4]:
            lines.append(f"  - {r}")

    active_case = get_active_separation_case(state_dict) if state_dict else {}
    topo_report = str(_as_dict(active_case).get("generated_topology_report") or "")
    if topo_report:
        lines.extend(["", "## 4. PSD 拓扑智能体输出", topo_report])
    lines.extend(["", "## 5. 候选分离改造方案"])
    ranked = _as_list(analysis.get("ranked_candidates"))
    if not ranked:
        lines.append("暂无候选。")
    else:
        for i, cand in enumerate(ranked, 1):
            lines.append(_candidate_line(cand, i))
            for r in _as_list(cand.get("reasoning"))[:3]:
                lines.append(f"   - {r}")

    warnings = _as_list(analysis.get("constraint_warnings"))
    if warnings:
        lines.extend(["", "## 5. 约束薄弱点"])
        for w in warnings[:8]:
            lines.append(f"- {w.get('name')}: gap={_fmt(w.get('gap'), 4)} stream={w.get('stream') or 'NA'} component={w.get('component') or 'NA'}")

    lines.extend([
        "",
        "## 6. 后续验证规则",
        "- `solvent_free_pressure_swing_distillation`：先用 Flash2 在多压力下验证共沸组成/相对挥发度变化，再新建无溶剂 PSD 严格流程；该分支不继承溶剂循环约束。",
        "- `extractive_pressure_swing_distillation`：保留 ED 萃取剂，先做含溶剂 Flash2 压敏筛选，再复制 ED_best 扫描/优化塔压，并严格检查溶剂循环。",
        "- `membrane_retrofit`：必须标明膜安装位置；先用 shortcut 膜模型扫描 stage cut/selectivity；只有 shortcut 通过且膜数据可信时再进入严格 Aspen/自定义膜模块。",
        "- 只有验证后的分离方案 process_data 才应进入过程强化设计页做热泵/热集成详细优化。",
    ])
    return "\n".join(lines)


def make_separation_seed_message(process_data: dict) -> str:
    if not process_data:
        return "阶段 2 尚未返回 ED/BO 结构化结果，分离强化设计暂不可用。"
    analysis = build_separation_retrofit_analysis(process_data)
    ranked = _as_list(analysis.get("ranked_candidates"))
    lines = [
        "✅ 基础 ED 流程数据已传入分离强化智能体。",
        "",
        "当前分离强化支线先做 **Flash 筛选**。本轮暂停 PSD 拓扑重建，优先验证 **EPSD1**：保留 ED 拓扑，只给通过 Flash 压敏证据的塔加入受限塔压微调窗口。",
        "",
        "可用候选：",
    ]
    if ranked:
        for i, cand in enumerate(ranked[:5], 1):
            lines.append(_candidate_line(cand, i))
    else:
        lines.append("暂无候选，请检查 ED 结果中是否包含 columns/streams/constraints 数据。")
    lines.extend([
        "",
        "你可以输入：`执行 Aspen Flash2 压敏筛选`、`选择 EPSD1`、`执行方案验证`、`执行当前方案 BO 优化`、`查看验证载荷`。PSD1 本轮暂停，EPSD1 成功后再恢复。",
    ])
    return "\n".join(lines)


def format_separation_retrofit_data(state_dict: Optional[dict]) -> str:
    state_dict = _as_dict(state_dict)
    package = get_separation_package(state_dict) or ensure_separation_retrofit_package(state_dict)
    if not package:
        return "暂无分离强化数据包。"
    compact = compact_separation_package_for_display(package)
    text = _safe_json_dumps(compact, indent=2)
    # Gradio Textbox 会因超大 JSON 卡顿；保留摘要，完整状态仍在 state_dict 内部。
    max_chars = 45000
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...【分离强化数据包过长，GUI 仅显示前 45000 字；完整数据仍保留在 state_dict 中。】"
    return text


# ============================================================
# LLM 问答接口
# ============================================================

def _content_to_plain_text(content: Any, _depth: int = 0) -> str:
    """Collapse Gradio/OpenAI multimodal text blocks to plain Markdown.

    Some Gradio versions return message content as `[{'text': ..., 'type': 'text'}]`.
    If we simply call `str(content)`, the previous message becomes literal
    Python-list characters after the next response.  This helper recursively
    extracts the real text and also handles already-stringified block lists.
    """
    if _depth > 5:
        return str(content or "")
    if content is None:
        return ""
    if isinstance(content, str):
        s = content
        ss = s.strip()
        if (ss.startswith("[") and ss.endswith("]")) or (ss.startswith("{") and ss.endswith("}")):
            try:
                import ast
                parsed = ast.literal_eval(ss)
                if parsed is not content:
                    return _content_to_plain_text(parsed, _depth + 1)
            except Exception:
                pass
        return s
    if isinstance(content, dict):
        if "text" in content:
            return _content_to_plain_text(content.get("text"), _depth + 1)
        if "content" in content:
            return _content_to_plain_text(content.get("content"), _depth + 1)
        return "\n".join(f"{k}: {_content_to_plain_text(v, _depth + 1)}" for k, v in content.items() if v is not None)
    if isinstance(content, (list, tuple)):
        parts = []
        for item in content:
            t = _content_to_plain_text(item, _depth + 1).strip()
            if t:
                parts.append(t)
        return "\n".join(parts)
    return str(content)


def _normalize_chat_history(chat_history: Optional[list]) -> list:
    if not chat_history:
        return []
    out = []
    for item in chat_history:
        if isinstance(item, dict) and item.get("role") in {"user", "assistant", "system"}:
            out.append({"role": item.get("role"), "content": _content_to_plain_text(item.get("content"))})
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            if item[0]:
                out.append({"role": "user", "content": _content_to_plain_text(item[0])})
            if item[1]:
                out.append({"role": "assistant", "content": _content_to_plain_text(item[1])})
    return out


def _append(history: list, role: str, content: str) -> list:
    out = _normalize_chat_history(history)
    out.append({"role": role, "content": content})
    return out


def _extract_requested_case_id(text: str) -> Optional[str]:
    raw = str(text or "").strip()
    low = raw.lower()
    if not any(k in low for k in ["选择", "激活", "使用", "切换", "执行", "运行", "启动", "优化", "重跑", "验证", "select", "activate", "use", "run", "execute", "start", "optimize", "verify"]):
        return None
    # 优先抓精确/短 case id。
    m = re.search(r"(ED[_-]?best|PSD\s*1|EPSD\s*1|PED\s*1|MEM\s*[1-4]|PSD_solvent_free_candidate_\d+|EPSD_candidate_\d+|PSD_candidate_\d+|MEM_candidate_[A-Za-z0-9_]+)", raw, flags=re.I)
    if m:
        return _normalize_case_id(m.group(1).replace(" ", ""))
    if "无溶剂" in low or "不需要溶剂" in low or "solvent-free" in low:
        return "PSD1"
    if "epsd" in low or "ped" in low or "保留溶剂" in low:
        return "EPSD1"
    if "psd" in low or "变压" in low:
        return "PSD1"
    if "mem" in low or "膜" in low:
        return "MEM1"
    if "ed" in low or "基准" in low:
        return "ED_best"
    return None


def build_local_separation_reply(user_message: str, state_dict: Optional[dict]) -> Tuple[str, dict]:
    state_dict = _as_dict(state_dict)
    package = ensure_separation_retrofit_package(state_dict)
    process_data = get_ed_process_data(state_dict)
    msg = str(user_message or "").strip()

    requested = _extract_requested_case_id(msg)
    if requested:
        state_dict, result = activate_separation_case(state_dict, requested)
        package = get_separation_package(state_dict)
        active = get_active_separation_case(state_dict)
        tech = str(active.get("technology") or active.get("subtype") or "").lower()
        extra = ""
        if ("solvent_free" in tech or "without_extractant" in tech or str(active.get("case_id") or "").upper().startswith("PSD")):
            try:
                topo = _ensure_psd_generated_topology(state_dict, active)
                extra = "\n\n已由 PSD 拓扑智能体生成新的无溶剂变压精馏循环拓扑，基础流程页将切换显示该 PSD 拓扑。"
                if not _has_ready_flash_pressure_ranges(state_dict):
                    extra += "\n\n注意：当前未检测到完整 Flash 压敏结果，拓扑可用于页面预览；正式 BO 前仍建议先执行 Aspen Flash2 压敏筛选。"
                if build_psd_topology_report is not None:
                    extra += "\n\n" + build_psd_topology_report(topo)
            except Exception as exc:
                extra = f"\n\nPSD 拓扑智能体暂未生成拓扑：{exc}"
        elif "pressure_swing" in tech or "extractive_pressure" in tech or str(active.get("case_id") or "").upper().startswith("EPSD"):
            extra = "\n\nEPSD/PED 保留基础 ED 拓扑，只改变塔压搜索窗口，因此基础流程图不重绘。"
        return result + "\n\n当前活动方案：\n" + _candidate_line(active) + extra, state_dict

    lower = msg.lower()
    if any(k in lower for k in ["payload", "载荷", "验证", "json"]):
        payloads = [
            {
                "case_id": c.get("case_id"),
                "technology": c.get("technology"),
                "verification_level": c.get("verification_level"),
                "verification_payload": c.get("verification_payload"),
            }
            for c in _as_list(package.get("ranked_candidates"))
            if isinstance(c, dict)
        ]
        return "以下是分离强化候选验证载荷：\n\n```json\n" + _safe_json_dumps(payloads, indent=2) + "\n```", state_dict

    if any(k in lower for k in ["报告", "总结", "候选", "方案", "分析", "生成"]):
        return render_separation_retrofit_report(process_data, package, state_dict), state_dict

    if "pending_flash" in lower or ("flash" in lower and not _flash_results_from_state(state_dict)):
        screen = _as_dict(package.get("pressure_swing_screen"))
        msg_lines = [
            "当前状态是 `PENDING_FLASH`：尚未取得真实 Aspen Flash2 的 VLE 结果。",
            "",
            "这里不能根据经验或文献常识直接推断 PSD/EPSD 可行性；必须先执行 Flash2 扫描，拿到 y-x 数据后再用公式计算相对挥发度、共沸点和压力迁移量。",
            "",
            render_pressure_swing_screen_summary(screen),
            "",
            "当前三元图只显示组分顶点和进料点；不会伪造共沸线或精馏边界。",
        ]
        return "\n".join(msg_lines), state_dict

    active = get_active_separation_case(state_dict)
    ranked = _as_list(package.get("ranked_candidates"))
    lines = [
        "当前分离强化数据包已生成。",
        "",
        f"活动方案：{_candidate_line(active)}",
        "",
        "候选优先级：",
    ]
    for i, cand in enumerate(ranked[:6], 1):
        lines.append(_candidate_line(cand, i))
    lines.append("\n输入 `执行 Aspen Flash2 压敏筛选` 可在对话中调用 Aspen；输入 `选择 PSD1` 或 `选择 EPSD1` 可切换分离候选；输入 `执行方案验证` 会直接启动当前方案的 Aspen/BO 验证。")
    return "\n".join(lines), state_dict


async def generate_separation_response(
    user_message: str,
    state_dict: Optional[dict],
    api_key: str,
    base_url: str,
    model_name: str,
    chat_history: Optional[List[Dict[str, str]]] = None,
) -> Tuple[str, dict]:
    """分离强化回复。执行/选择类请求先走确定性逻辑；一般解释可选 LLM。"""
    state_dict = _as_dict(state_dict)
    local_reply, state_dict = build_local_separation_reply(user_message, state_dict)

    # 选择、验证载荷、报告等请求由确定性逻辑完成，不让 LLM 改写 payload。
    lower = str(user_message or "").lower()
    if any(k in lower for k in ["选择", "激活", "使用", "payload", "载荷", "验证", "报告", "生成", "pending_flash"]):
        return local_reply, state_dict
    if "flash" in lower and not _flash_results_from_state(state_dict):
        return local_reply, state_dict

    if not api_key or AsyncOpenAI is None:
        return local_reply, state_dict

    package = ensure_separation_retrofit_package(state_dict)
    recent = []
    for item in _normalize_chat_history(chat_history)[-6:]:
        if item.get("role") in {"user", "assistant"}:
            recent.append(f"{item.get('role')}: {str(item.get('content') or '')[:900]}")

    system_prompt = """你是化工分离强化设计智能体。你的职责只包括 ED 后的分离支线改造：变压和膜类方案。
严格限制：
1. 不讨论热泵、热集成、MVR 的详细设计，这些属于下一阶段过程强化；
2. 变压分支必须区分无溶剂 PSD 与保留萃取剂 EPSD/PED；
3. 无溶剂 PSD 不能继承溶剂循环约束，但必须重新严格验证所有产品纯度/收率；
4. 膜方案必须标明安装位置：产品精制、溶剂循环精制、塔间预处理或共沸旁路；
5. 不直接声称膜方案严格可行，除非有验证结果；
6. 输出必须区分 Flash2 压敏筛选、Aspen 可重跑验证、shortcut 筛选、概念候选；
7. 当状态为 PENDING_FLASH 或没有真实 Flash2 结果时，严禁基于经验预测共沸点、压敏程度或 PSD/EPSD 可行性，只能提示需要执行 Flash2。"""
    user_prompt = "\n\n".join([
        f"用户问题：{user_message}",
        "【最近对话】",
        "\n".join(recent) if recent else "无",
        "【分离强化确定性数据包】",
        _truncate_text(_safe_json_dumps(package, indent=2)),
        "【确定性本地回答】",
        local_reply,
    ])
    try:
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        resp = await client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.12,
        )
        return resp.choices[0].message.content or local_reply, state_dict
    except Exception as exc:
        return f"[分离强化 LLM 响应失败] {exc}\n\n以下返回确定性结果：\n\n{local_reply}", state_dict




# ============================================================
# 分离方案 -> BO 重优化桥接
# ============================================================

def _aspen_block_id(value: Any, max_len: int = 8) -> str:
    """Aspen block id: keep letters/digits/hyphen only, no underscore."""
    text = re.sub(r"[^A-Z0-9-]", "", str(value or "").upper().strip())
    return text[:max_len] or "C1"


def _normalize_bo_bounds(bounds: Any) -> list:
    xs = _as_list(bounds)
    if len(xs) >= 2:
        lo = _safe_float(xs[0], None)
        hi = _safe_float(xs[1], None)
        if lo is not None and hi is not None:
            if hi < lo:
                lo, hi = hi, lo
            return [float(lo), float(hi)]
    return [0.0, 1.0]


def _case_variables_for_bo(case: dict, valid_blocks: Optional[List[str]] = None) -> dict:
    valid = {_aspen_block_id(x) for x in _as_list(valid_blocks)} if valid_blocks else None
    out = {}
    for raw_key, raw_bounds in _as_dict(case.get("new_variables")).items():
        key = str(raw_key or "").upper().strip()
        key = key.replace("_D_OR_B_FLOW", "_D_FLOW")
        parts = key.split("_")
        if len(parts) < 2:
            continue
        block = _aspen_block_id(parts[0])
        suffix = "_".join(parts[1:]).upper()
        # 兼容旧 PSD_C1_* 写法：PSD_C1_N -> PSDC1_N。
        if block == "PSD" and len(parts) >= 3 and parts[1].upper().startswith("C"):
            block = _aspen_block_id("PSD" + parts[1])
            suffix = "_".join(parts[2:]).upper()
        if valid is not None and block not in valid:
            continue
        if suffix == "D_OR_B_FLOW":
            suffix = "D_FLOW"
        if suffix not in {"PRESSURE", "N", "RR", "D_FLOW", "FEED_NORM", "SOLV_NORM"} and not ("SOLV" in suffix and "FLOW" in suffix):
            continue
        out[f"{block}_{suffix}"] = _normalize_bo_bounds(raw_bounds)
    return out


def _filtered_non_solvent_state(state_dict: dict, non_solvent: List[str]) -> dict:
    state = copy.deepcopy(_as_dict(state_dict))
    solvent = _solvent_from_state(state_dict)
    non_solvent = [_clean_id(c) for c in non_solvent if _clean_id(c)]
    state["components"] = non_solvent
    state["solvent"] = ""
    state["component_ids"] = non_solvent

    feed = copy.deepcopy(_as_dict(state.get("feed_conditions")))
    fracs = _as_dict(feed.get("fractions"))
    if fracs:
        kept = {}
        for c in non_solvent:
            # 允许原始 key 大小写不同。
            val = None
            for k, v in fracs.items():
                if _clean_id(k) == c:
                    val = _safe_float(v, 0.0)
                    break
            kept[c] = float(val or 0.0)
        total = sum(kept.values())
        if total > 0:
            kept = {k: v / total for k, v in kept.items()}
        else:
            kept = {c: 1.0 / max(1, len(non_solvent)) for c in non_solvent}
        feed["fractions"] = kept
        state["feed_conditions"] = feed
    if isinstance(state.get("components_meta"), dict):
        state["components_meta"] = {k: v for k, v in state["components_meta"].items() if _clean_id(k) in set(non_solvent)}
    if isinstance(state.get("component_aliases"), dict) and solvent:
        state["component_aliases"] = {k: v for k, v in state["component_aliases"].items() if _clean_id(k) != solvent and _clean_id(v) != solvent}
    return state


def _build_solvent_free_psd_topology_for_bo(state_dict: dict, case: dict) -> dict:
    """Build a new recycle-coupled PSD topology through the dedicated PSD agent.

    PSD is not a simple reordering of the ED columns.  Removing the extractant
    changes the ternary topology/recycle logic, so the topology must be generated
    as a new process graph before BO.
    """
    if build_psd_topology_from_flash is None:
        raise RuntimeError("main_agent.py 中的 PSD 拓扑智能体未能导入，无法为 PSD 生成新拓扑。")
    topology = build_psd_topology_from_flash(state_dict, case)
    if not _as_dict(topology).get("bo_variables"):
        raise RuntimeError("PSD 拓扑智能体未生成 bo_variables，不能启动 BO。")
    if not any(str(k).upper().endswith("_PRESSURE") for k in _as_dict(topology.get("bo_variables"))):
        raise RuntimeError("PSD 拓扑智能体没有得到 Flash 支持的塔压变量；不建议启动 PSD 压力 BO。")
    return topology


def get_separation_topology_for_base_display(state_dict: Optional[dict]) -> dict:
    """Return topology that the base page should render after separation actions.

    PSD must render its generated recycle-coupled topology.  If the user selects
    PSD1 before BO, create/store the topology lazily so the base page can switch
    immediately.  EPSD/PED keeps rendering the original ED topology.
    """
    state = _as_dict(state_dict)
    package = get_separation_package(state)
    active = get_active_separation_case(state)
    cid = str(active.get("case_id") or package.get("active_case_id") or "")
    tech = str(active.get("technology") or active.get("subtype") or "").lower()
    is_psd = ("solvent_free" in tech) or ("without_extractant" in tech) or cid.upper().startswith("PSD")
    if is_psd:
        topo = _as_dict(active.get("generated_topology") or active.get("psd_generated_topology"))
        if topo:
            return topo
        overlays = _as_dict(package.get("topology_overlays"))
        topo = _as_dict(overlays.get(cid))
        if topo:
            return topo
        try:
            # Lazy generation for display.  This is the same topology handoff used
            # later by BO, so the base-page figure and optimizer input stay aligned.
            topo = _ensure_psd_generated_topology(state, active)
            if topo:
                return topo
        except Exception as exc:
            try:
                print(f"[SEP_TOPO][WARN] PSD topology display generation failed for {cid}: {exc}", flush=True)
            except Exception:
                pass
    return _as_dict(state.get("topology"))


def _ensure_psd_generated_topology(state_dict: dict, case: dict) -> dict:
    """Generate/store PSD topology for display and BO handoff."""
    package = ensure_separation_retrofit_package(state_dict)
    topology = _as_dict(case.get("generated_topology") or case.get("psd_generated_topology"))
    if not topology:
        topology = _build_solvent_free_psd_topology_for_bo(state_dict, case)
        case["generated_topology"] = topology
        case["psd_topology_agent_version"] = topology.get("agent_version")
        if build_psd_topology_report is not None:
            case["generated_topology_report"] = build_psd_topology_report(topology)
    package.setdefault("topology_overlays", {})[str(case.get("case_id") or "PSD")] = topology
    state_dict["separation_retrofit"] = package
    return topology


def _build_epsd_topology_for_bo(state_dict: dict, case: dict) -> dict:
    state = copy.deepcopy(_as_dict(state_dict))
    topology = copy.deepcopy(_as_dict(state.get("topology")))
    if not topology:
        raise ValueError("当前 state_dict 中没有 topology，无法复制 ED_best 进入 EPSD/PED 联合 BO。")
    blocks = [_aspen_block_id(_as_dict(b).get("id") or _as_dict(b).get("name")) for b in _as_list(topology.get("blocks"))]
    original_bo_vars = copy.deepcopy(_as_dict(topology.get("bo_variables")))
    if not original_bo_vars:
        raise ValueError("ED_best topology 中没有原始 bo_variables，无法进行锚点联合优化。")
    case_vars = _case_variables_for_bo(case, blocks)
    joint = _build_epsd_anchor_joint_space(state, topology, original_bo_vars, case_vars)
    bo_vars = _as_dict(joint.get("bo_variables"))
    if not any(str(k).upper().endswith("_PRESSURE") for k in bo_vars):
        raise ValueError("EPSD 候选没有由 Flash 推导出的有效 *_PRESSURE 搜索变量。")
    topology.pop("fixed_bo_params", None)
    topology.pop("epsd_fixed_param_policy", None)
    topology["bo_variables"] = bo_vars
    topology["epsd_pressure_only_first"] = False
    topology["epsd_anchor_params"] = copy.deepcopy(joint.get("anchor_params") or {})
    topology["epsd_anchor_optimization"] = {
        "enabled": True,
        "mode": "ED_best_anchored_local_joint_optimization",
        "anchor_params": copy.deepcopy(joint.get("anchor_params") or {}),
        "anchor_sources": copy.deepcopy(joint.get("anchor_sources") or {}),
        "approved_pressure_keys": list(joint.get("approved_pressure_keys") or []),
        "local_bound_policy": copy.deepcopy(joint.get("policy") or {}),
        "require_same_session_anchor_reproduction": True,
        "forbid_midpoint_substitution": True,
    }
    approved_keys = list(joint.get("approved_pressure_keys") or [])
    anchor_params = copy.deepcopy(joint.get("anchor_params") or {})
    topology["epsd_pressure_continuation"] = {
        "enabled": True,
        "mode": "single_column_continuation_then_joint_bo",
        "approved_pressure_keys": approved_keys,
        "anchor_pressures_atm": {k: anchor_params.get(k) for k in approved_keys},
        "flash_pressure_bounds_atm": {k: copy.deepcopy(bo_vars.get(k)) for k in approved_keys},
        "continuation_fractions": [0.12, 0.25, 0.45, 0.70, 1.00],
        "repair_trials_per_pressure": 6,
        "minimum_realized_pressure_change_atm": 0.05,
        "minimum_realized_pressure_change_ratio": 0.08,
        "require_strict_feasible_single_column_corridor": True,
        "require_pressure_realization_for_epsd_classification": True,
        "disable_ed_refine_fallback_as_epsd": True,
    }
    topology["retrofit_family"] = "extractive_pressure_swing_distillation"
    topology["source_case_id"] = case.get("case_id")
    topology["pressure_optimization_handoff"] = _pressure_handoff_from_case(case)
    return topology


def is_separation_bo_execution_command(text: str) -> bool:
    """Return True for separation-page commands that should start Aspen/BO.

    v18 fix:
    - Accept compact Chinese phrases such as ``执行当前BO方案优化`` and
      ``执行当前方案BO优化``.
    - Keep Flash and pure payload/report commands out of this branch.
    - Do not require a long candidate id; the active case can be used.
    """
    raw0 = str(text or "").strip()
    raw = raw0.lower()
    compact = re.sub(r"\s+", "", raw)
    if not compact:
        return False

    # Flash commands are handled by the Flash runner branch.
    if any(k in compact for k in ["flash", "flash2", "闪蒸", "压敏", "pressureflash"]):
        return False

    # Pure read-only commands must not start BO.
    readonly_tokens = ["查看", "显示", "展示", "载荷", "payload", "json", "报告", "生成分离强化报告"]
    if any(k in compact for k in readonly_tokens) and not any(k in compact for k in ["执行", "运行", "启动", "run", "execute", "start", "bo", "优化", "验证"]):
        return False
    if any(k in compact for k in ["查看验证载荷", "显示验证载荷", "展示验证载荷", "查看payload", "查看json"]):
        return False

    direct_phrases = [
        "执行方案验证", "执行验证", "执行当前方案", "执行当前方案优化", "执行当前方案bo优化",
        "执行当前bo方案优化", "执行当前bo优化", "执行当前优化", "执行当前方案aspen验证",
        "执行当前方案严格验证", "执行当前方案bo", "执行bo", "启动bo", "运行bo", "重新bo",
        "执行psd1", "执行epsd1", "运行psd1", "运行epsd1", "启动psd1", "启动epsd1",
        "psd1bo", "epsd1bo", "psd1优化", "epsd1优化", "psd1验证", "epsd1验证",
    ]
    if any(p in compact for p in direct_phrases):
        return True

    has_execute = any(k in compact for k in ["执行", "运行", "启动", "重跑", "重新", "run", "execute", "start"])
    has_optimize = any(k in compact for k in ["bo", "贝叶斯", "优化", "重优化", "参数", "radfrac", "aspen"])
    has_verify = any(k in compact for k in ["验证", "严格验证", "方案验证", "aspen验证", "严格"])
    has_case = any(k in compact for k in ["方案", "candidate", "psd", "epsd", "ped", "mem", "膜", "变压", "当前", "该方案", "ed_best", "基准", "psd1", "epsd1", "mem1"])

    return bool(has_execute and (has_optimize or has_verify or has_case) and (has_case or has_verify or has_optimize))


def prepare_separation_bo_state(state_dict: Optional[dict], user_message: str = "") -> Tuple[dict, dict, dict, str]:
    """把用户在分离页选择的方案转换成 BO_optimizer 可运行 state。

    返回: display_state, bo_state, case, message。
    display_state 用于 GUI 状态；bo_state 是传给 run_stage3_optimization 的深拷贝。
    """
    display_state = _as_dict(state_dict)
    package = ensure_separation_retrofit_package(display_state)
    requested = _extract_requested_case_id(user_message or "")
    if not requested:
        low = str(user_message or "").lower()
        ranked = [c for c in _as_list(package.get("ranked_candidates")) if isinstance(c, dict)]
        if any(k in low for k in ["推荐", "top1", "top 1", "最优"]):
            requested = str(_as_dict(ranked[0]).get("case_id") or package.get("active_case_id") or "ED_best") if ranked else "ED_best"
        else:
            requested = str(package.get("active_case_id") or "ED_best")
    display_state, select_msg = activate_separation_case(display_state, requested)
    package = get_separation_package(display_state)
    case = get_active_separation_case(display_state)
    case_id = str(case.get("case_id") or "ED_best")
    technology = str(case.get("technology") or case.get("family") or "").lower()

    if requested and _normalize_case_id(requested) == "PSD1":
        raise ValueError("PSD1 当前暂停执行。请先选择 EPSD1 并完成受限塔压 BO：`选择 EPSD1` → `执行当前方案 BO 优化`。")

    if case_id != "ED_best" and "membrane" in technology:
        raise ValueError("膜方案目前只有 shortcut/概念验证载荷，尚未接入可由 BO_optimizer 直接重跑的 Aspen 膜模块。请先补充膜选择性、通量和安装位置模型。")

    bo_state = copy.deepcopy(display_state)
    bo_state.setdefault("separation_bo_runs", {})
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_case = re.sub(r"[^A-Za-z0-9_-]", "_", case_id)[:60]
    bo_state["bo_export_dir"] = os.path.abspath(os.path.join("bo_results_export", f"{safe_case}_{timestamp}"))
    bo_state["separation_bo_context"] = {
        "case_id": case_id,
        "technology": technology,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "source": "separation_page_chat",
    }

    if case_id == "ED_best":
        # 基准 ED 直接沿用现有 topology/solvent/components。
        if not _as_dict(bo_state.get("topology")).get("bo_variables"):
            raise ValueError("ED_best topology 中没有 bo_variables，无法启动 BO。")
        msg = f"{select_msg}\n已准备基准 ED_best 的 BO 重跑输入。"
        return display_state, bo_state, case, msg

    if "solvent_free" in technology or "without_extractant" in technology or str(case.get("subtype") or "").lower().startswith("psd_without"):
        if not _has_ready_flash_pressure_ranges(display_state):
            raise ValueError("必须先执行 Aspen Flash2 压敏筛选，再根据 y-x/alpha 结果推导 PSD 塔压 BO 范围。")
        if not _pressure_case_has_optimized_bounds(case):
            raise ValueError("Flash 结果没有给出可用于 PSD 的压敏组分对；不建议启动无溶剂 PSD 塔压 BO。")
        non_solvent = _non_solvent_components(display_state)
        psd_topology = _ensure_psd_generated_topology(display_state, case)
        bo_state = _filtered_non_solvent_state(bo_state, non_solvent)
        bo_state["topology"] = copy.deepcopy(psd_topology)
        bo_state["flash_pressure_optimization"] = _flash_pressure_optimization_from_state(display_state)
        bo_state["flash_pressure_optimization"]["bo_handoff"] = _pressure_handoff_from_case(case)
        msg = (
            f"{select_msg}\n已把 `{case_id}` 转换为无溶剂 PSD BO 输入：组件已过滤为非溶剂组分，"
            "且只对 Flash 判定压敏的塔加入压力变量；弱压敏组分对不会变压。"
        )
        return display_state, bo_state, case, msg

    if "pressure_swing" in technology or "extractive_pressure" in technology or case_id.upper().startswith("EPSD"):
        if not _has_ready_flash_pressure_ranges(display_state):
            raise ValueError("必须先执行 Aspen Flash2 压敏筛选，再根据含溶剂 y-x/alpha 结果推导 EPSD/PED 塔压 BO 范围。")
        if not _pressure_case_has_optimized_bounds(case):
            raise ValueError("Flash 结果没有给出可用于 EPSD/PED 的压敏组分对；不建议启动塔压 BO。")
        bo_state["topology"] = _build_epsd_topology_for_bo(display_state, case)
        bo_state["epsd_anchor_optimization"] = copy.deepcopy(_as_dict(bo_state["topology"].get("epsd_anchor_optimization")))
        bo_state["flash_pressure_optimization"] = _flash_pressure_optimization_from_state(display_state)
        bo_state["flash_pressure_optimization"]["bo_handoff"] = _pressure_handoff_from_case(case)
        bo_state["epsd_engineering_guard"] = _make_epsd_engineering_guard(display_state, bo_state.get("epsd_anchor_optimization"))
        bo_state.setdefault("intent_targets", {})
        bo_state["intent_targets"]["progress_emit_stride"] = int(_safe_float(bo_state.get("epsd_progress_emit_stride"), 1) or 1)
        bo_state["intent_targets"]["progress_contour_stride"] = int(_safe_float(bo_state.get("epsd_progress_contour_stride"), 2) or 2)
        msg = (
            f"{select_msg}\n已把 `{case_id}` 转换为 ED_best 锚定的 EPSD/PED 局部联合 BO："
            "原 ED 的塔板数、回流比、采出量、进料位置和溶剂流量全部在锚点邻域重新优化，"
            "仅 Flash 材料性筛选通过的塔压被新增为变量；启动后先在同一 Aspen 会话复现 ED_best，"
            "再将所有 EPSD 候选与该锚点比较，未达到材料性改善则保留诊断并回退。"
        )
        return display_state, bo_state, case, msg

    raise ValueError(f"当前方案 `{case_id}` 的 technology=`{technology}` 尚未定义 BO 转换规则。")


def store_separation_bo_result(
    state_dict: Optional[dict],
    case_id: str,
    process_data: dict,
    *,
    bo_log: str = "",
    contour_plot_path: Optional[str] = None,
    curve_plot_path: Optional[str] = None,
) -> dict:
    """把某个分离方案的 BO 结果写回 separation_retrofit.cases。"""
    state_dict = _as_dict(state_dict)
    package = ensure_separation_retrofit_package(state_dict)
    cid = str(case_id or package.get("active_case_id") or "ED_best")
    case = _find_case_by_id(package, cid)
    if not case:
        return state_dict
    pdata = _as_dict(process_data)
    rollback = bool(_as_dict(pdata.get("epsd_bo_rollback")))
    anchor_comparison = _as_dict(pdata.get("epsd_anchor_comparison"))
    has_columns = bool(_as_list(pdata.get("columns")))
    case["process_data"] = pdata if pdata else case.get("process_data")
    case["metrics"] = _extract_case_metrics(pdata) if pdata else case.get("metrics")
    if rollback:
        case["verification_level"] = "rolled_back_to_ED_best_after_anchor_comparison"
        case["can_enter_thermal_intensification"] = False
    else:
        accepted = str(anchor_comparison.get("decision_class") or "").startswith("auto_accept")
        case["verification_level"] = (
            "aspen_bo_optimized_vs_ED_anchor" if has_columns and accepted
            else "aspen_bo_optimized" if has_columns
            else "bo_run_no_structured_columns"
        )
        case["can_enter_thermal_intensification"] = has_columns
    case["bo_result"] = {
        "status": pdata.get("status") or "available" if pdata else "no_process_data",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "contour_plot_path": contour_plot_path,
        "curve_plot_path": curve_plot_path,
        "rollback_applied": rollback,
        "rollback": _as_dict(pdata.get("epsd_bo_rollback")),
        "anchor_reproduction": _as_dict(pdata.get("epsd_anchor_reproduction")),
        "anchor_comparison": anchor_comparison,
        "log_tail": "\n".join(str(bo_log or "").splitlines()[-30:]),
    }
    # If EPSD is worse, keep diagnostics on EPSD1 but return the active process
    # to ED_best so later thermal/flexibility pages do not consume a regressive flow.
    package["active_case_id"] = "ED_best" if rollback else cid
    package["last_bo_case_id"] = cid
    package["cases"] = [case if str(_as_dict(c).get("case_id")) == cid else c for c in _as_list(package.get("cases"))]
    ranked = []
    for c in _as_list(package.get("ranked_candidates")):
        if isinstance(c, dict) and str(c.get("case_id")) == cid:
            ranked.append(case)
        else:
            ranked.append(c)
    package["ranked_candidates"] = ranked
    state_dict["separation_retrofit"] = package
    state_dict.setdefault("separation_bo_results", {})[cid] = {
        "process_data": pdata,
        "contour_plot_path": contour_plot_path,
        "curve_plot_path": curve_plot_path,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    return state_dict

def _is_flash_execution_command(text: str) -> bool:
    """确定性识别：用户明确要求在分离页执行 Aspen Flash2 压敏筛选。"""
    raw = str(text or "").strip().lower()
    if not raw:
        return False
    has_execute = any(k in raw for k in [
        "执行", "运行", "开始", "启动", "调用", "run", "execute", "start"
    ])
    has_flash = any(k in raw for k in [
        "flash2", "flash", "闪蒸", "压敏", "pressure flash", "aspen flash"
    ])
    # 注意：用户说“执行某方案/启动 BO”时也常会带 Aspen 字样；
    # Flash 入口必须只响应明确的 flash/闪蒸/压敏命令，避免误把 BO 重跑当成 Flash。
    return has_execute and has_flash




def is_flash_execution_command(text: str) -> bool:
    """Public wrapper used by GUI_APP.py to route Flash commands before LLM/BO branches."""
    return _is_flash_execution_command(text)


def _infer_flash_mode_from_text(text: str) -> str:
    raw = str(text or "").lower()
    if any(k in raw for k in ["无溶剂", "solvent-free", "solvent_free", "psd"]):
        if not any(k in raw for k in ["epsd", "ped", "保留溶剂", "含溶剂", "萃取"]):
            return "solvent_free_psd"
    if any(k in raw for k in ["epsd", "ped", "保留溶剂", "含溶剂", "萃取"]):
        return "extractive_pressure_swing"
    return "combined"



def _resolve_aspen_template_path(state_dict: Optional[dict], explicit_path: Optional[str] = None) -> str:
    """Return an absolute Aspen template path with clear diagnostics.

    Aspen COM is much less tolerant of relative paths than Python.  A relative
    `Simulation 1.bkp` can pass Python-side checks in one working directory but
    still fail inside `InitFromArchive2` with OLE error 2041 / 无法打开文件.
    """
    state = _as_dict(state_dict)
    candidates = [
        explicit_path,
        state.get("template_path"),
        state.get("aspen_template_path"),
        _as_dict(state.get("bo_optimization")).get("template_path"),
        _as_dict(state.get("runtime_paths")).get("template_path"),
        "Simulation 1.bkp",
    ]
    checked: List[str] = []
    for cand in candidates:
        if not cand:
            continue
        raw = str(cand).strip().strip('"').strip("'")
        if not raw:
            continue
        expanded = os.path.abspath(os.path.expanduser(os.path.expandvars(raw)))
        checked.append(expanded)
        if os.path.isfile(expanded):
            return expanded
    raise FileNotFoundError(
        "Aspen 模板文件不存在或不可访问。请在 state_dict['template_path'] 或 "
        "state_dict['aspen_template_path'] 中写入 .bkp/.apw/.aprbkp 的绝对路径；"
        f"已检查路径: {checked}"
    )

def _run_pressure_flash_screen_sync(state_dict: dict, user_message: str = "") -> Tuple[dict, dict, str]:
    """Execute the GUI-owned 9-point Flash2 pressure screen in one run folder."""
    state_dict = _as_dict(state_dict)
    try:
        print(f"[SEP_FLASH] _run_pressure_flash_screen_sync entered; user_message={user_message!r}", flush=True)
    except Exception:
        pass
    log_lines: List[str] = []

    def _logger(msg: str) -> None:
        if msg is not None:
            line = str(msg)
            log_lines.append(line)
            try:
                print(line, flush=True)
            except Exception:
                pass

    need_uninit = False
    try:
        try:
            import pythoncom  # type: ignore
            pythoncom.CoInitialize()
            need_uninit = True
        except Exception:
            pythoncom = None  # noqa: F841

        from aspen_pressure_flash_screen_runner import run_pressure_flash_screen_from_state
        template_path = _resolve_aspen_template_path(state_dict)
        state_dict["template_path"] = template_path
        state_dict["aspen_template_path"] = template_path
        mode = _infer_flash_mode_from_text(user_message)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_root = os.path.abspath(str(state_dict.get("pressure_screen_output_root") or os.path.join(os.getcwd(), "gradio_runtime_artifacts", "pressure_screen_runs")))
        run_dir = os.path.join(output_root, f"run_{stamp}")
        figures_dir = os.path.join(run_dir, "figures")
        raw_dir = os.path.join(run_dir, "raw")
        data_dir = os.path.join(run_dir, "data")
        for folder in (figures_dir, raw_dir, data_dir):
            os.makedirs(folder, exist_ok=True)

        ed_process_data = get_ed_process_data(state_dict)
        risk_plan = build_pressure_cut_risk_edge_plan(ed_process_data, state_dict)
        pressure_grid = _default_pressure_grid_kpa("psd", state_dict)
        if not any(k in state_dict for k in ("pressure_grid_kpa", "separation_pressure_grid_kpa")):
            pressure_grid = list(GUI_FLASH_PRESSURE_GRID_KPA)
        pressure_grid = _augment_pressure_grid_with_risk_bases(pressure_grid, risk_plan, state_dict)
        selected_pairs = _as_list(risk_plan.get("selected_pairs"))
        pair_contexts = _as_dict(risk_plan.get("pair_contexts"))
        state_dict["pressure_screen_risk_edges"] = risk_plan
        state_dict["pressure_screen_pair_contexts"] = pair_contexts

        _logger(f"[FLASH] Aspen template path = {template_path}")
        _logger(f"[FLASH] GUI profile: mode={mode}; pressures={pressure_grid}; true x points={GUI_FLASH_X_GRID}")
        _logger(f"[FLASH-RISK-EDGE] cuts={risk_plan.get('identified_cut_count')}/{risk_plan.get('expected_cut_count')}; selected_pairs={selected_pairs}")
        updated_state, results = run_pressure_flash_screen_from_state(
            state_dict=state_dict,
            template_path=template_path,
            mode=mode,
            pressure_write_mode="kpa",
            pressure_grid_kpa=pressure_grid,
            x_grid=list(GUI_FLASH_X_GRID),
            solvent_to_feed_grid=list(GUI_FLASH_SOLVENT_TO_FEED_GRID),
            vapor_fraction_grid=list(GUI_FLASH_VAPOR_FRACTION_GRID),
            temperature_fallback_grid_C=[],
            selected_pairs=selected_pairs or None,
            pair_contexts=pair_contexts or None,
            scan_profile="fast",
            adaptive_pair_limit=0 if selected_pairs else 2,
            run_native_triptych=False,
            save_plots=False,
            logger=_logger,
            close_aspen=True,
            enable_temperature_fallback=False,
            reuse_flash_unit=True,
            cache_tree_nodes=True,
            log_level="normal",
            temperature_output_unit="C",
            composition_sum_tolerance=0.03,
            overall_flow_balance_tolerance=0.05,
            component_balance_tolerance=0.10,
            rebuild_flash_on_component_change=True,
        )
        updated_state = _as_dict(updated_state)
        results = _as_dict(results)
        results["risk_edge_plan"] = risk_plan
        updated_state["pressure_screen_risk_edges"] = risk_plan
        updated_state["pressure_screen_pair_contexts"] = pair_contexts
        updated_state["pressure_swing_flash_results"] = results
        updated_state["flash_pressure_optimization"] = _as_dict(results.get("pressure_optimization"))
        if results.get("separation_agent_handoff"):
            updated_state["pressure_screen_handoff"] = results.get("separation_agent_handoff")

        plot_paths = save_pressure_direction_gui_artifacts(updated_state, results, out_dir=figures_dir)
        plot_paths["run_directory"] = run_dir
        results["plot_paths"] = plot_paths

        recommendations = [
            _as_dict(r) for r in _as_list(_as_dict(results.get("pressure_optimization")).get("pair_recommendations"))
            if _as_dict(r).get("quality_verified") is not False
        ]
        recommendations.sort(key=lambda r: _safe_float(r.get("sensitivity_score"), 0.0) or 0.0, reverse=True)
        gui_display = {
            "schema_version": "pressure-screen-gui-display-v1",
            "status": results.get("status"),
            "run_directory": run_dir,
            "true_composition_grid": list(GUI_FLASH_X_GRID),
            "main_figure_png": plot_paths.get("pressure_direction_summary_png"),
            "panel_figures_png": plot_paths.get("pressure_direction_panel_pngs") or [],
            "yx_plot_data_csv": plot_paths.get("pressure_direction_yx_csv"),
            "top_recommendation": recommendations[0] if recommendations else None,
            "all_recommendations": recommendations,
            "risk_edge_plan": risk_plan,
            "bo_source_state_key": "flash_pressure_optimization",
        }
        updated_state["pressure_screen_gui_display"] = gui_display
        results["gui_display"] = gui_display

        package = ensure_separation_retrofit_package(updated_state)
        package["plot_paths"] = plot_paths
        package["runner_plot_owner"] = True
        package["runner_plot_version"] = results.get("runner_version") or "gui-pressure-screen-v1"
        package["runner_plot_updated_at"] = datetime.now().isoformat(timespec="seconds")
        updated_state["separation_retrofit"] = package

        with open(os.path.join(raw_dir, "pressure_screen_results_full.json"), "w", encoding="utf-8") as f:
            f.write(_safe_json_dumps(results, indent=2))
        with open(os.path.join(raw_dir, "pressure_screen_state_full.json"), "w", encoding="utf-8") as f:
            f.write(_safe_json_dumps(updated_state, indent=2))
        with open(os.path.join(data_dir, "pressure_screen_gui_display.json"), "w", encoding="utf-8") as f:
            f.write(_safe_json_dumps(gui_display, indent=2))

        _logger(f"[FLASH] GUI artifacts saved to {run_dir}")
        return updated_state, results, "\n".join(log_lines)

    finally:
        if need_uninit:
            try:
                pythoncom.CoUninitialize()  # type: ignore[name-defined]
            except Exception:
                pass


def _build_flash_execution_reply(results: dict, log_text: str, state_dict: dict) -> str:
    results = _as_dict(results)
    package = ensure_separation_retrofit_package(state_dict)
    screen = _as_dict(package.get("pressure_swing_screen"))
    status = results.get("status") or "unknown"
    case_count = len(_as_list(results.get("cases")))
    rec = screen.get("recommended_route") or "PENDING_FLASH"
    scores = _as_dict(screen.get("scores"))
    psd_score = _fmt(scores.get("PSD"), 3)
    epsd_score = _fmt(scores.get("EPSD"), 3)

    lines = [
        "✅ Aspen Flash2 压敏筛选已完成，并已刷新分离强化页图像与 EPSD 优先验证建议。",
        "",
        f"- Flash status: `{status}`",
        f"- Flash cases: `{case_count}`",
        f"- Flash 推荐路线: **{rec}**",
        "- 当前执行策略: **先做 EPSD1；PSD 暂停到 EPSD 成功后再恢复**",
        f"- PSD score: `{psd_score}`",
        f"- EPSD score: `{epsd_score}`",
        "",
        "页面右侧/上方的 y-x 图会随本次结果刷新；筛选同时覆盖真空（30/50/70 kPa 绝压）、常压和正压。含溶剂 Flash 只决定哪些分离塔压力可进入 EPSD1；进入 BO 后，以 ED_best 为锚点联合优化原 ED 变量与获批压力，溶剂回收塔压力固定。",
    ]
    pressure_opt = _as_dict(screen.get("pressure_optimization"))
    rec_lines = _as_list(pressure_opt.get("summary_lines"))
    if rec_lines:
        lines.extend(["", "Flash 推导的压力 BO 建议："])
        for line in rec_lines[:8]:
            lines.append(f"- {line}")
    if log_text:
        preview = "\n".join(str(log_text).splitlines()[-12:])
        lines.extend(["", "最近运行日志：", "```text", preview, "```"])
    lines.extend([
        "",
        "下一步可以输入：",
        "- `选择 EPSD1`",
        "- `查看验证载荷`",
    ])
    return "\n".join(lines)


async def run_aspen_flash_from_separation_chat(
    user_message: str,
    state_dict: Optional[dict],
) -> Tuple[str, dict]:
    """供 chat_with_separation_retrofit_agent 调用的异步执行入口。"""
    state_dict = _as_dict(state_dict)
    try:
        print(f"[SEP_FLASH] async entry matched; user_message={user_message!r}", flush=True)
    except Exception:
        pass

    def _worker():
        try:
            updated_state, results, log_text = _run_pressure_flash_screen_sync(state_dict, user_message)
            return updated_state, results, log_text, None
        except Exception as exc:
            return state_dict, {}, "", exc

    updated_state, results, log_text, error = await asyncio.to_thread(_worker)
    if error is not None:
        updated_state = _as_dict(updated_state)
        updated_state["pressure_swing_flash_results"] = {
            "status": "failed",
            "error": str(error),
            "cases": [],
        }
        updated_state.pop("separation_retrofit", None)
        ensure_separation_retrofit_package(updated_state)
        hint = ""
        err_text = str(error)
        if "无法打开文件" in err_text or "FileNotFound" in err_text or "No such file" in err_text or "不存在" in err_text:
            hint = (
                "\n\n排查重点：Aspen 模板路径没有传入绝对路径，或 .bkp 文件不在 GUI 当前工作目录。"
                "请把 state_dict['template_path'] / state_dict['aspen_template_path'] 设为实际 .bkp 的绝对路径，"
                "并确认文件未被其他 Aspen 实例锁定。"
            )
        reply = (
            "❌ Aspen Flash2 压敏筛选失败。\n\n"
            f"错误信息：`{error}`"
            f"{hint}\n\n"
            "当前仍保持 Flash 未完成/失败状态；分离页不会根据失败结果推断 PSD/EPSD 可行性。"
        )
        return reply, updated_state

    return _build_flash_execution_reply(results, log_text, updated_state), updated_state




async def chat_with_separation_retrofit_agent(
    user_message: str,
    chat_history: Optional[list],
    state_dict: Optional[dict],
    api_key: str,
    base_url: str,
    model_name: str,
):
    """GUI 流式兼容入口：yield (history, cleared_text, state_dict)。

    与过程强化页保持一致：普通问题走解释/报告；明确的执行命令可在对话中触发
    Aspen Flash2 压敏筛选，并由 GUI 包装层刷新图像和数据包。
    """
    history = _normalize_chat_history(chat_history)
    text = str(user_message or "").strip()
    if not text:
        yield history, "", _as_dict(state_dict)
        return

    history = _append(history, "user", text)

    if _is_flash_execution_command(text):
        running = _append(
            history,
            "assistant",
            "正在调用 Aspen 执行 Flash2 压敏筛选。该操作会打开/运行 Aspen，刷新分离强化页 y-x 三联图，并根据 Flash 结果推导塔压 BO 范围..."
        )
        yield running, "", _as_dict(state_dict)

        reply, new_state = await run_aspen_flash_from_separation_chat(text, state_dict)
        final_history = history + [{"role": "assistant", "content": reply}]
        yield final_history, "", new_state
        return

    thinking = _append(history, "assistant", "正在生成分离强化候选/验证载荷...")
    yield thinking, "", _as_dict(state_dict)

    reply, new_state = await generate_separation_response(
        user_message=text,
        state_dict=state_dict,
        api_key=api_key,
        base_url=base_url,
        model_name=model_name,
        chat_history=history,
    )
    final_history = history + [{"role": "assistant", "content": reply}]
    yield final_history, "", new_state
