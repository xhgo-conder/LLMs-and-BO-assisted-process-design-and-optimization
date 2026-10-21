# -*- coding: utf-8 -*-
r"""
aspen_virtual_heat_pump_builder_global_v2.py

Aspen Plus RadFrac pseudo stream heat-pump builder.

Purpose
-------
This module packages the verified demo logic into reusable functions so it can be
called by the process-intensification agent or by a GUI action later.

Verified topology
-----------------
1. C1 stage N-1 liquid pseudo stream RBIN -> FRB(Flash2)
   FRB duty is used as the base reboiler-demand surrogate.
2. C1 stage 2 vapor pseudo stream TPIN -> KHP(Compr) -> HREB(Heater) -> HCOND(Heater)
   HREB duty is used as the heat-pump heat supplied to the reboiler surrogate.

Important Aspen automation details
----------------------------------
- Pseudo streams must be connected to RadFrac PS(OUT) first, then PSEUDO_STAGE can be written.
- Liquid pseudo stream normally does not need PSEUDO_PHAS0; Aspen defaults to liquid.
- Vapor pseudo stream should write PSEUDO_PHAS0 = "V" when the table is available.
- Compr outlet pressure radio/button is controlled by Input\OPT_SPEC, not by PRES itself.
  Write TYPE first, then OPT_SPEC="PRES", then PRES.

The main entry point is:
    build_column_heat_pump_virtual_scheme(aspen, ...)

This file intentionally does not import win32com at module import time; pass an
already-open Aspen COM document object from your existing pipeline.
"""

from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from typing import Any, Dict, Iterable, List, Optional, Tuple

BUILDER_VERSION = "aspen-virtual-heat-pump-builder-global-v6-dt7-signed-duty-temperature-gate"


def _norm_id(value: Any, max_len: int = 8) -> str:
    text = re.sub(r"[^A-Z0-9-]", "", str(value or "").upper())
    return text[:max_len] if max_len else text


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None:
            return default
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return default
        return out
    except Exception:
        return default


def _as_dict(obj: Any) -> dict:
    return obj if isinstance(obj, dict) else {}


class AspenVirtualHeatPumpBuilder:
    """Small wrapper around Aspen Tree/Ports operations for this heat-pump structure."""

    def __init__(self, aspen: Any, logger: Optional[Any] = None):
        self.aspen = aspen
        self.log_lines: List[str] = []
        self.external_logger = logger

    # ------------------------------------------------------------------
    # Logging and Tree helpers
    # ------------------------------------------------------------------
    def log(self, msg: str) -> None:
        self.log_lines.append(str(msg))
        if callable(self.external_logger):
            try:
                self.external_logger(str(msg))
                return
            except Exception:
                pass
        print(msg)

    def node(self, path: str):
        try:
            return self.aspen.Tree.FindNode(path)
        except Exception:
            return None

    def get_val(self, path: str) -> Any:
        try:
            n = self.node(path)
            return n.Value if n is not None else None
        except Exception:
            return None

    def elements_names(self, path: str) -> List[str]:
        n = self.node(path)
        if n is None or not getattr(n, "Elements", None):
            return []
        out: List[str] = []
        try:
            for item in n.Elements:
                name = str(getattr(item, "Name", "") or "")
                if name:
                    out.append(name)
        except Exception:
            pass
        return out

    def get_or_create(self, parent_node: Any, child_name: str):
        if parent_node is None:
            return None
        try:
            return parent_node.Elements(child_name)
        except Exception:
            try:
                parent_node.Elements.Add(child_name)
                return parent_node.Elements(child_name)
            except Exception:
                return None

    def set_child_value(self, parent_path: str, child_name: str, value: Any) -> bool:
        parent = self.node(parent_path)
        child = self.get_or_create(parent, child_name)
        if child is None:
            return False
        try:
            child.Value = value
            rb = self.get_val(parent_path + "\\" + child_name)
            self.log(f"[WRITE] {parent_path}\\{child_name} = {value} ; readback={rb}")
            return True
        except Exception as exc:
            self.log(f"[WARN] write failed: {parent_path}\\{child_name} = {value}; {exc}")
            return False

    def write_table_entry(self, table_path: str, row_name: str, value: Any, required: bool = True) -> bool:
        """Write a stream-indexed table entry such as Input\PSEUDO_STAGE\RBIN."""
        row = _norm_id(row_name)
        table = self.node(table_path)
        if table is None:
            parent_path, table_name = table_path.rsplit("\\", 1)
            parent = self.node(parent_path)
            table = self.get_or_create(parent, table_name)
        if table is None:
            msg = f"[ERROR] table not found/created: {table_path}"
            self.log(msg)
            if required:
                raise RuntimeError(msg)
            return False

        try:
            item = table.Elements(row)
        except Exception:
            try:
                table.Elements.Add(row)
                item = table.Elements(row)
            except Exception:
                item = None
        if item is None:
            msg = f"[ERROR] table row not found/created: {table_path}\\{row}"
            self.log(msg)
            if required:
                raise RuntimeError(msg)
            return False

        try:
            item.Value = value
            rb = self.get_val(table_path + "\\" + row)
            self.log(f"[WRITE] {table_path}\\{row} = {value} ; readback={rb}")
            return True
        except Exception as exc:
            msg = f"[ERROR] table row write failed: {table_path}\\{row} = {value}; {exc}"
            self.log(msg)
            if required:
                raise RuntimeError(msg)
            return False

    # ------------------------------------------------------------------
    # Flowsheet construction helpers
    # ------------------------------------------------------------------
    def add_block(self, block_id: str, block_type: str) -> str:
        bid = _norm_id(block_id)
        if self.node(rf"\Data\Blocks\{bid}") is not None:
            self.log(f"[SKIP] Block exists: {bid}")
            return bid
        blocks = self.node(r"\Data\Blocks")
        if blocks is None:
            raise RuntimeError("Cannot find \\Data\\Blocks")
        blocks.Elements.Add(f"{bid}!{block_type}")
        self.log(f"[ADD] Block {bid}!{block_type}")
        return bid

    def add_stream(self, stream_id: str, stream_type: str = "MATERIAL") -> str:
        sid = _norm_id(stream_id)
        if self.node(rf"\Data\Streams\{sid}") is not None:
            self.log(f"[SKIP] Stream exists: {sid}")
            return sid
        streams = self.node(r"\Data\Streams")
        if streams is None:
            raise RuntimeError("Cannot find \\Data\\Streams")
        streams.Elements.Add(f"{sid}!{stream_type}")
        self.log(f"[ADD] Stream {sid}!{stream_type}")
        return sid

    def connect_stream_to_port(self, block_id: str, stream_id: str, port_candidates: List[str], role: str = "") -> bool:
        bid = _norm_id(block_id)
        sid = _norm_id(stream_id)
        for port in port_candidates:
            p = self.node(rf"\Data\Blocks\{bid}\Ports\{port}")
            if p is None:
                continue
            try:
                existing = {_norm_id(x) for x in self.elements_names(rf"\Data\Blocks\{bid}\Ports\{port}")}
                if sid not in existing:
                    p.Elements.Add(sid)
                suffix = f" ({role})" if role else ""
                self.log(f"[CONNECT] {sid} -> {bid}:{port}{suffix}")
                return True
            except Exception as exc:
                self.log(f"[WARN] port exists but connect failed: {sid} -> {bid}:{port}; {exc}")
        self.log(f"[WARN] connect failed: {sid} -> {bid}, candidate ports={port_candidates}")
        return False

    def connect_pseudo_stream_from_column(self, column_id: str, stream_id: str, port_candidates: Optional[List[str]] = None) -> bool:
        ports = port_candidates or [
            "PS(OUT)", "PSEUDO(OUT)", "P(OUT)", "P1(OUT)", "SP(OUT)", "SIDE(OUT)", "S(OUT)",
        ]
        return self.connect_stream_to_port(column_id, stream_id, ports, role="RadFrac pseudo stream source")

    def set_column_pseudo_stream(self, column_id: str, stream_id: str, stage: int, phase: Optional[str] = None) -> None:
        cid = _norm_id(column_id)
        sid = _norm_id(stream_id)
        if self.node(rf"\Data\Streams\{sid}") is None:
            raise RuntimeError(f"Pseudo stream must exist before PSEUDO_STAGE is written: {sid}")
        base = rf"\Data\Blocks\{cid}\Input"
        self.write_table_entry(base + r"\PSEUDO_STAGE", sid, int(stage), required=True)
        phase_u = str(phase or "").upper().strip()
        if phase_u == "V":
            self.write_table_entry(base + r"\PSEUDO_PHAS0", sid, "V", required=False)
        else:
            self.log(rf"[PSEUDO] {cid}: skip PSEUDO_PHAS0\{sid}; liquid/default phase.")

    # ------------------------------------------------------------------
    # Unit operation configuration
    # ------------------------------------------------------------------
    def add_flash2_reboiler_surrogate(
        self,
        flash_id: str,
        feed_stream: str,
        vapor_out: str,
        liquid_out: str,
        temperature_c: float,
        pressure: float,
    ) -> Dict[str, Any]:
        fid = self.add_block(flash_id, "Flash2")
        feed = self.add_stream(feed_stream)
        vap = self.add_stream(vapor_out)
        liq = self.add_stream(liquid_out)
        self.connect_stream_to_port(fid, feed, ["F(IN)", "FEED(IN)", "IN(IN)"], role="Flash2 feed from N-1 liquid pseudo stream")
        self.connect_stream_to_port(fid, vap, ["V(OUT)", "VAPOR(OUT)", "VAP(OUT)"], role="Flash2 vapor")
        self.connect_stream_to_port(fid, liq, ["L(OUT)", "LIQUID(OUT)", "LIQ(OUT)"], role="Flash2 liquid")
        base = rf"\Data\Blocks\{fid}\Input"
        self.set_child_value(base, "TEMP", float(temperature_c))
        self.set_child_value(base, "TEMP1", float(temperature_c))
        self.set_child_value(base, "PRES", float(pressure))
        self.set_child_value(base, "PRES1", float(pressure))
        return {"id": fid, "feed": feed, "vapor_out": vap, "liquid_out": liq, "temperature_C": temperature_c, "pressure": pressure}

    def _write_existing_or_create(self, block_id: str, candidates: List[str], value: Any, label: str, create_if_missing: bool = True) -> Optional[str]:
        bid = _norm_id(block_id)
        base = rf"\Data\Blocks\{bid}\Input"
        input_names = {_norm_id(x, max_len=0): x for x in self.elements_names(base)}
        for raw in candidates:
            real = input_names.get(_norm_id(raw, max_len=0))
            if real:
                path = base + "\\" + real
                try:
                    node = self.node(path)
                    node.Value = value
                    rb = self.get_val(path)
                    self.log(f"[WRITE] {label}: {path} = {value} ; readback={rb}")
                    return real
                except Exception as exc:
                    self.log(f"[WARN] {label} existing write failed: {path}={value}; {exc}")
        if create_if_missing:
            for raw in candidates:
                if self.set_child_value(base, raw, value):
                    return raw
        self.log(f"[WARN] {label}: no candidate accepted; candidates={candidates}, value={value}")
        return None

    def activate_compressor_outlet_spec(self, compressor_id: str, outlet_spec: str = "PRES") -> Optional[str]:
        """Activate the Compr outlet radio group, equivalent to clicking 'discharge pressure'."""
        bid = _norm_id(compressor_id)
        base = rf"\Data\Blocks\{bid}\Input"
        selector_names = ["OPT_SPEC", "OPT_DESIGN", "OUTLET_SPEC", "OUTSPEC", "PRES_SPEC", "PRESSURE_SPEC", "SPEC"]
        spec = str(outlet_spec or "PRES").upper().strip()
        token_map = {
            "PRES": ["PRES", "POUT", "P_OUT", "DISCHARGE-PRESSURE", "DISCHARGE_PRESSURE", "OUTLET-PRESSURE", "OUTLET_PRESSURE"],
            "DELP": ["DELP", "DP", "PRESSURE-INCREASE", "PRESSURE_INCREASE", "DELTA-P", "DELTA_P"],
            "PRATIO": ["PRATIO", "RATIO", "PRESSURE-RATIO", "PRESSURE_RATIO", "P-RATIO", "P_RATIO"],
            "POWER": ["POWER", "W", "WORK", "DUTY"],
        }
        tokens = token_map.get(spec, [spec])
        input_names = {_norm_id(x, max_len=0): x for x in self.elements_names(base)}
        for raw_sel in selector_names:
            sel = input_names.get(_norm_id(raw_sel, max_len=0))
            if not sel:
                continue
            path = base + "\\" + sel
            node = self.node(path)
            if node is None:
                continue
            for token in tokens:
                try:
                    node.Value = token
                    rb = self.get_val(path)
                    self.log(f"[WRITE] compressor outlet radio/select: {path} = {token} ; readback={rb}")
                    if rb is not None and str(rb).strip() != "":
                        return sel
                except Exception as exc:
                    self.log(f"[WARN] compressor outlet radio/select failed: {path}={token}; {exc}")
        # Last resort, create OPT_SPEC.
        for token in tokens:
            if self.set_child_value(base, "OPT_SPEC", token):
                return "OPT_SPEC"
        return None

    def add_compressor(
        self,
        compressor_id: str,
        feed_stream: str,
        out_stream: str,
        outlet_pressure: float,
        pressure_ratio: float = 2.0,
        calculation_type: str = "ISENTROPIC",
        outlet_spec: str = "PRES",
        isentropic_efficiency: float = 0.75,
        mechanical_efficiency: float = 0.95,
    ) -> Dict[str, Any]:
        kid = self.add_block(compressor_id, "Compr")
        feed = self.add_stream(feed_stream)
        out = self.add_stream(out_stream)
        self.connect_stream_to_port(kid, feed, ["F(IN)", "FEED(IN)", "IN(IN)", "V(IN)"], role="compressor feed from top vapor pseudo stream")
        self.connect_stream_to_port(kid, out, ["P(OUT)", "V(OUT)", "OUT(OUT)", "PRODUCT(OUT)", "F(OUT)"], role="compressor discharge")

        # Order matters: TYPE -> OPT_SPEC -> PRES.
        self._write_existing_or_create(kid, ["TYPE", "COMPTYPE", "COMP_TYPE", "CTYPE", "METHOD", "CALC_TYPE", "EFF_TYPE"], calculation_type, "compressor calculation TYPE")
        outlet_selector = self.activate_compressor_outlet_spec(kid, outlet_spec=outlet_spec)
        self._write_existing_or_create(kid, ["PRES", "PRES1", "P_OUT", "POUT", "OUTLET_PRESSURE", "DISCHARGE_PRESSURE"], float(outlet_pressure), "compressor discharge pressure")
        if str(outlet_spec).upper() in {"PRATIO", "RATIO", "PRESSURE_RATIO", "P_RATIO"}:
            self._write_existing_or_create(kid, ["PRATIO", "RATIO", "P_RATIO", "PRESSURE_RATIO"], float(pressure_ratio), "compressor pressure ratio")
        self._write_existing_or_create(kid, ["SEFF", "ISEN_EFF", "ISENTROPIC_EFF", "EFF_ISEN", "ETA_ISEN"], float(isentropic_efficiency), "compressor isentropic efficiency")
        self._write_existing_or_create(kid, ["MEFF", "MECH_EFF", "MECHANICAL_EFF", "ETA_MECH"], float(mechanical_efficiency), "compressor mechanical efficiency")
        return {
            "id": kid,
            "feed": feed,
            "out_stream": out,
            "outlet_pressure": outlet_pressure,
            "pressure_ratio": pressure_ratio,
            "outlet_spec_selector": outlet_selector,
            "calculation_type": calculation_type,
            "isentropic_efficiency": isentropic_efficiency,
            "mechanical_efficiency": mechanical_efficiency,
        }

    def add_heater(self, heater_id: str, feed_stream: str, out_stream: str, target_temperature_c: float, target_pressure: Optional[float], role: str) -> Dict[str, Any]:
        """Add an Aspen Heater used as a virtual cooler/heater.

        Important convention, verified in the GUI/Aspen debug chain:
        Heater pressure must not be set to the upstream/compressor pressure.
        For these virtual heat-exchanger blocks we write PRES/PRES1 = 0.0,
        i.e. zero pressure-drop / do-not-force-absolute-pressure surrogate.

        The incoming target_pressure argument is intentionally ignored and only
        kept in the returned metadata for traceability.
        """
        hid = self.add_block(heater_id, "Heater")
        feed = self.add_stream(feed_stream)
        out = self.add_stream(out_stream)
        self.connect_stream_to_port(hid, feed, ["F(IN)", "FEED(IN)", "IN(IN)"], role=f"{role} feed")
        self.connect_stream_to_port(hid, out, ["P(OUT)", "V(OUT)", "L(OUT)", "OUT(OUT)", "PRODUCT(OUT)"], role=f"{role} outlet")
        base = rf"\Data\Blocks\{hid}\Input"
        self.set_child_value(base, "TEMP", float(target_temperature_c))
        self.set_child_value(base, "TEMP1", float(target_temperature_c))
        # All virtual Heater blocks use zero pressure-drop convention.
        # Do not write compressor outlet pressure or hot-stream absolute pressure here.
        self.set_child_value(base, "PRES", 0.0)
        self.set_child_value(base, "PRES1", 0.0)
        return {
            "id": hid,
            "feed": feed,
            "out_stream": out,
            "target_temperature_C": target_temperature_c,
            "target_pressure_argument_ignored": target_pressure,
            "heater_pressure_written": 0.0,
            "pressure_convention": "PRES/PRES1=0.0 zero-pressure-drop surrogate for all virtual Heater blocks",
            "role": role,
        }

    # ------------------------------------------------------------------
    # Run / read results
    # ------------------------------------------------------------------
    def run_aspen(self, label: str = "heat-pump virtual scheme") -> None:
        self.log(f"[RUN] Aspen run start: {label}")
        engine = getattr(self.aspen, "Engine", None)
        if engine is None:
            raise RuntimeError("Aspen document has no Engine object")
        engine.Run2()
        self.log(f"[RUN] Aspen run finished: {label}")

    def read_first_float(self, paths: Iterable[str]) -> Optional[float]:
        for path in paths:
            val = _safe_float(self.get_val(path), None)
            if val is not None:
                return val
        return None

    def read_stream_flows(self, stream_id: str) -> Dict[str, float]:
        sid = _norm_id(stream_id)
        base = rf"\Data\Streams\{sid}\Output\MOLEFLOW\MIXED"
        node = self.node(base)
        flows: Dict[str, float] = {}
        if node is None or not getattr(node, "Elements", None):
            return flows
        try:
            for item in node.Elements:
                name = str(getattr(item, "Name", "") or "")
                val = _safe_float(getattr(item, "Value", None), 0.0) or 0.0
                if name:
                    flows[name.upper()] = val
        except Exception:
            pass
        return flows

    def snapshot_stream(self, stream_id: str) -> Dict[str, Any]:
        sid = _norm_id(stream_id)
        flows = self.read_stream_flows(sid)
        total = sum(flows.values())
        temp = self.read_first_float([rf"\Data\Streams\{sid}\Output\TEMP_OUT\MIXED", rf"\Data\Streams\{sid}\Output\TEMP\MIXED"])
        pres = self.read_first_float([rf"\Data\Streams\{sid}\Output\PRES_OUT\MIXED", rf"\Data\Streams\{sid}\Output\PRES\MIXED"])
        return {
            "id": sid,
            "temperature_raw": temp,
            "pressure_raw": pres,
            "total_molar_flow": total,
            "molar_flow": flows,
            "mole_fraction": {k: v / total for k, v in flows.items()} if total > 1e-12 else {},
        }

    def resolve_stream_temperature_for_approach(self, stream_id: str, fallback_c: float, role: str) -> Tuple[float, str]:
        """Resolve the actual virtual cold-side stream temperature used for ΔT.

        The heat-pump / heat-integration ΔT must reference the sink reboiler
        pseudo stream itself, not merely the screening bottom-temperature scalar.
        In the GUI naming this is the stream such as FGHPRB / FP_RB / A2_HP_RB:
        the RadFrac pseudo stream representing the tower-bottom liquid side.

        If Aspen has not populated the pseudo stream output yet, fall back to the
        supplied BO/screening temperature but keep the basis visible in logs and
        returned metadata.
        """
        sid = _norm_id(stream_id)
        candidates = [
            rf"\Data\Streams\{sid}\Output\TEMP_OUT\MIXED",
            rf"\Data\Streams\{sid}\Output\TEMP\MIXED",
            rf"\Data\Streams\{sid}\Output\TEMP",
            rf"\Data\Streams\{sid}\Input\TEMP\MIXED",
            rf"\Data\Streams\{sid}\Input\TEMP",
        ]
        temp = self.read_first_float(candidates)
        if temp is None:
            temp = float(fallback_c)
            basis = f"fallback:{float(fallback_c)}C"
        else:
            basis = f"stream:{sid}"
        self.log(f"[TEMP-BASIS] {role}: reference={basis}, T_ref={float(temp)} C")
        return float(temp), basis

    def scan_output_numeric(self, block_id: str, keywords: List[str], max_depth: int = 4) -> Optional[float]:
        bid = _norm_id(block_id)
        base = rf"\Data\Blocks\{bid}\Output"
        root = self.node(base)
        if root is None:
            return None
        keys = [str(k).upper() for k in keywords]
        hits: List[Tuple[str, float]] = []

        def walk(node: Any, path: str, depth: int) -> None:
            if depth > max_depth or node is None:
                return
            val = _safe_float(getattr(node, "Value", None), None)
            if val is not None and any(k in path.upper() for k in keys):
                hits.append((path, val))
            try:
                elements = getattr(node, "Elements", None)
                if elements:
                    for child in elements:
                        cname = str(getattr(child, "Name", "") or "")
                        if cname:
                            walk(child, path + "\\" + cname, depth + 1)
            except Exception:
                pass

        walk(root, base, 0)
        if hits:
            self.log(f"[FIND] {bid} output {keywords}: {hits[0][0]} = {hits[0][1]}")
            return hits[0][1]
        return None

    def read_block_duty(self, block_id: str) -> Optional[float]:
        bid = _norm_id(block_id)
        paths = [
            rf"\Data\Blocks\{bid}\Output\QCALC",
            rf"\Data\Blocks\{bid}\Output\DUTY",
            rf"\Data\Blocks\{bid}\Output\HEAT_DUTY",
            rf"\Data\Blocks\{bid}\Output\Q",
            rf"\Data\Blocks\{bid}\Output\NET_DUTY",
            rf"\Data\Blocks\{bid}\Output\QNET",
            rf"\Data\Blocks\{bid}\Output\B_DUTY",
            rf"\Data\Blocks\{bid}\Output\HX_DUTY",
        ]
        val = self.read_first_float(paths)
        if val is None:
            val = self.scan_output_numeric(bid, ["DUTY", "QCALC", "QNET", "HEAT", "HX"])
        self.log(f"[DUTY] {bid} duty_raw={val}")
        return val

    def read_compressor_power(self, block_id: str) -> Optional[float]:
        bid = _norm_id(block_id)
        paths = [
            rf"\Data\Blocks\{bid}\Output\WNET",
            rf"\Data\Blocks\{bid}\Output\POWER",
            rf"\Data\Blocks\{bid}\Output\BRAKE_POWER",
            rf"\Data\Blocks\{bid}\Output\W",
            rf"\Data\Blocks\{bid}\Output\WORK",
            rf"\Data\Blocks\{bid}\Output\WDOT",
            rf"\Data\Blocks\{bid}\Output\BPOWER",
        ]
        val = self.read_first_float(paths)
        if val is None:
            val = self.scan_output_numeric(bid, ["POWER", "WORK", "WNET", "WDOT", "BRAKE"])
        self.log(f"[POWER] {bid} power_raw={val}")
        return val


def build_column_heat_pump_virtual_scheme(
    aspen: Any,
    column_id: str,
    n_stages: int,
    top_temperature_c: float,
    bottom_temperature_c: float,
    top_pressure: Optional[float] = None,
    pressure_ratio: float = 2.0,
    compressor_outlet_pressure: Optional[float] = None,
    reboiler_approach_c: float = 7.0,
    final_condenser_target_c: Optional[float] = None,
    final_condenser_pressure: Optional[float] = 0.0,
    reboiler_liquid_stage: Optional[int] = None,
    top_vapor_stage: int = 2,
    flash_pressure: float = 0.0,
    column_pseudo_ports: Optional[List[str]] = None,
    ids: Optional[Dict[str, str]] = None,
    run: bool = True,
    logger: Optional[Any] = None,
) -> Dict[str, Any]:
    """Build the virtual heat-pump scheme on an already-open Aspen document.

    Parameters are intentionally explicit so the process-intensification agent can
    call this from BO process_data without re-optimizing Aspen.
    """
    ids = {**{
        "reboiler_flash": "FRB",
        "reboiler_pseudo": "RBIN",
        "reboiler_vapor": "RBV",
        "reboiler_liquid": "RBL",
        "top_vapor_pseudo": "TPIN",
        "compressor": "KHP",
        "compressor_out": "KOUT",
        "reboiler_supply_heater": "HREB",
        "reboiler_supply_out": "HPHOT",
        "final_condenser": "HCOND",
        "final_condenser_out": "HPCOND",
    }, **(ids or {})}

    b = AspenVirtualHeatPumpBuilder(aspen, logger=logger)
    cid = _norm_id(column_id)
    n = max(2, int(n_stages))
    rb_stage = int(reboiler_liquid_stage) if reboiler_liquid_stage is not None else max(1, n - 1)
    tv_stage = max(1, min(int(top_vapor_stage), n))

    top_t = float(top_temperature_c)
    bot_t = float(bottom_temperature_c)
    if compressor_outlet_pressure is None:
        base_p = _safe_float(top_pressure, None)
        if base_p is None or base_p <= 0:
            base_p = 1.01325
        compressor_outlet_pressure = base_p * float(pressure_ratio)
    hreb_target = bot_t + float(reboiler_approach_c)
    hcond_target = top_t if final_condenser_target_c is None else float(final_condenser_target_c)

    b.log(f"[BUILDER] {BUILDER_VERSION}")
    b.log(f"[SCHEME] column={cid}, RBIN stage={rb_stage} liquid/default, TPIN stage={tv_stage} vapor")

    # 1) Reboiler surrogate: pseudo liquid -> Flash2
    rb_feed = b.add_stream(ids["reboiler_pseudo"])
    rb_info = b.add_flash2_reboiler_surrogate(
        ids["reboiler_flash"], rb_feed, ids["reboiler_vapor"], ids["reboiler_liquid"], bot_t, float(flash_pressure)
    )
    if not b.connect_pseudo_stream_from_column(cid, rb_feed, column_pseudo_ports):
        raise RuntimeError(f"Cannot connect {rb_feed} to {cid} pseudo output port.")
    b.set_column_pseudo_stream(cid, rb_feed, rb_stage, phase=None)

    # Critical timing fix:
    # The tower-bottom liquid pseudo stream temperature is not reliable immediately
    # after assigning PSEUDO_STAGE.  Aspen only populates its Output\TEMP after a
    # run.  If we read it too early, the old code fell back to bottom_temperature_c,
    # which could make HREB TEMP 20-40 C higher than the actual pseudo stream.
    if run:
        try:
            b.run_aspen(f"populate {rb_feed} reboiler-liquid pseudo temperature before HREB target")
        except Exception as exc:
            b.log(f"[WARN] preliminary pseudo-temperature run failed; fallback may be used: {exc}")

    rb_reference_temperature_c, rb_reference_basis = b.resolve_stream_temperature_for_approach(
        rb_feed, bot_t, "HREB ΔT reference from reboiler liquid pseudo stream AFTER preliminary run"
    )
    # Co-current assumption: hot stream leaving HREB is controlled against the
    # actual tower-bottom liquid pseudo stream temperature, not the scalar
    # bottom_temperature_c from screening data. Counter-current integration can
    # recover more heat; this virtual model deliberately keeps the conservative
    # co-current reference.
    hreb_target = rb_reference_temperature_c + float(reboiler_approach_c)

    # 2) Top vapor: pseudo vapor -> compressor -> heater -> heater
    top_feed = b.add_stream(ids["top_vapor_pseudo"])
    comp = b.add_compressor(
        compressor_id=ids["compressor"],
        feed_stream=top_feed,
        out_stream=ids["compressor_out"],
        outlet_pressure=float(compressor_outlet_pressure),
        pressure_ratio=float(pressure_ratio),
        calculation_type="ISENTROPIC",
        outlet_spec="PRES",
        isentropic_efficiency=0.75,
        mechanical_efficiency=0.95,
    )
    hreb = b.add_heater(ids["reboiler_supply_heater"], comp["out_stream"], ids["reboiler_supply_out"], hreb_target, 0.0, "heat release to reboiler surrogate")
    hcond = b.add_heater(
        ids["final_condenser"],
        hreb["out_stream"],
        ids["final_condenser_out"],
        hcond_target,
        final_condenser_pressure,
        "final condenser surrogate after heat release; residual source-condenser duty"
    )
    if not b.connect_pseudo_stream_from_column(cid, top_feed, column_pseudo_ports):
        raise RuntimeError(f"Cannot connect {top_feed} to {cid} pseudo output port.")
    b.set_column_pseudo_stream(cid, top_feed, tv_stage, phase="V")

    if run:
        b.run_aspen("RadFrac pseudo streams + reboiler Flash2 + heat-pump chain")

    rb_snapshot = b.snapshot_stream(rb_feed)
    tp_snapshot = b.snapshot_stream(top_feed)
    compressor_out_snapshot = b.snapshot_stream(comp["out_stream"])
    hreb_out_snapshot = b.snapshot_stream(hreb["out_stream"])
    hcond_out_snapshot = b.snapshot_stream(hcond["out_stream"])
    frb_duty = b.read_block_duty(ids["reboiler_flash"]) if run else None
    hreb_duty = b.read_block_duty(ids["reboiler_supply_heater"]) if run else None
    hcond_duty = b.read_block_duty(ids["final_condenser"]) if run else None
    comp_power = b.read_compressor_power(ids["compressor"]) if run else None

    # Aspen Heater convention: positive duty adds heat to the material stream;
    # negative duty removes heat.  A real heat-pump reboiler match therefore
    # requires HREB duty < 0.  Never convert a positive HREB duty into heat
    # supplied merely by taking abs().
    reboiler_demand = abs(frb_duty) if frb_duty is not None else None
    heat_release = abs(hreb_duty) if hreb_duty is not None and hreb_duty < 0 else 0.0
    external_reboiler = None
    if reboiler_demand is not None:
        external_reboiler = max(0.0, reboiler_demand - heat_release)
        b.log(f"[BALANCE] external_reboiler_raw = {reboiler_demand} - {heat_release} = {external_reboiler}")

    compressor_discharge_t = _safe_float(compressor_out_snapshot.get("temperature_raw"), None)
    discharge_margin_c = None if compressor_discharge_t is None else compressor_discharge_t - hreb_target
    physical_gate = {
        "hreb_is_heat_releasing": None if hreb_duty is None else bool(hreb_duty < 0),
        "hcond_is_nonheating": None if hcond_duty is None else bool(hcond_duty <= 0),
        "compressor_discharge_temperature_raw": compressor_discharge_t,
        "hreb_target_temperature_C": hreb_target,
        "compressor_discharge_margin_C": discharge_margin_c,
        "compressor_discharge_hotter_than_hreb_target": None if discharge_margin_c is None else bool(discharge_margin_c > 0),
    }

    return {
        "version": BUILDER_VERSION,
        "column": cid,
        "status": "built_and_run" if run else "built_not_run",
        "temperature_basis": {
            "top_temperature_C": top_t,
            "bottom_temperature_C_input": bot_t,
            "reboiler_liquid_pseudo_reference_stream": rb_feed,
            "reboiler_liquid_pseudo_reference_temperature_C": rb_reference_temperature_c,
            "reboiler_liquid_pseudo_reference_basis": rb_reference_basis,
            "reboiler_approach_C": float(reboiler_approach_c),
            "reboiler_supply_heater_target_C": hreb_target,
            "final_condenser_target_C": hcond_target,
            "final_condenser_pressure": final_condenser_pressure,
        },
        "pseudo_streams": {
            "reboiler_liquid": {"stream": rb_feed, "stage": rb_stage, "phase": "default liquid", "path": rf"\Data\Blocks\{cid}\Input\PSEUDO_STAGE\{rb_feed}"},
            "top_vapor": {"stream": top_feed, "stage": tv_stage, "phase": "V", "stage_path": rf"\Data\Blocks\{cid}\Input\PSEUDO_STAGE\{top_feed}", "phase_path": rf"\Data\Blocks\{cid}\Input\PSEUDO_PHAS0\{top_feed}"},
        },
        "blocks": {"reboiler_flash": rb_info, "compressor": comp, "reboiler_supply_heater": hreb, "final_condenser": hcond},
        "streams": {
            "reboiler_pseudo_snapshot": rb_snapshot,
            "top_vapor_pseudo_snapshot": tp_snapshot,
            "compressor_out_snapshot": compressor_out_snapshot,
            "hreb_out_snapshot": hreb_out_snapshot,
            "hcond_out_snapshot": hcond_out_snapshot,
        },
        "physical_heat_exchange_gate": physical_gate,
        "energy_balance_raw": {
            "reboiler_flash_duty_raw": frb_duty,
            "heat_pump_heat_to_reboiler_raw_signed_HREB": hreb_duty,
            "heat_pump_heat_to_reboiler_raw_abs_HREB": heat_release,
            "heat_pump_hreb_absolute_duty_raw": abs(hreb_duty) if hreb_duty is not None else None,
            "external_reboiler_after_heat_pump_raw": external_reboiler,
            "final_condenser_duty_raw": hcond_duty,
            "final_condenser_duty_raw_signed_HCOND": hcond_duty,
            "final_condenser_residual_cooling_raw_abs": abs(hcond_duty) if hcond_duty is not None else None,
            "compressor_power_raw": comp_power,
            "note": "Raw values follow the active Aspen unit set. HREB is credited as heat release only when its signed Heater duty is negative; positive HREB duty means the block is heating the compressed stream and is not a valid heat-pump match. HCOND remains a virtual-branch diagnostic.",
        },
        "log": b.log_lines,
    }



def _pair_id_prefix(source_column_id: str, sink_column_id: str) -> str:
    """Create a short Aspen-safe prefix for cross-column virtual heat-pump IDs."""
    src = _norm_id(source_column_id, max_len=3) or "S"
    snk = _norm_id(sink_column_id, max_len=3) or "R"
    raw = re.sub(r"[^A-Z0-9]", "", f"{src}{snk}")
    return raw[:4] or "HP"


def build_cross_column_heat_pump_virtual_scheme(
    aspen: Any,
    source_column_id: str,
    sink_column_id: str,
    source_n_stages: int,
    sink_n_stages: int,
    source_top_temperature_c: float,
    sink_bottom_temperature_c: float,
    source_top_pressure: Optional[float] = None,
    pressure_ratio: float = 2.0,
    compressor_outlet_pressure: Optional[float] = None,
    reboiler_approach_c: float = 7.0,
    final_condenser_target_c: Optional[float] = None,
    final_condenser_pressure: Optional[float] = 0.0,
    sink_reboiler_liquid_stage: Optional[int] = None,
    source_top_vapor_stage: int = 2,
    flash_pressure: float = 0.0,
    column_pseudo_ports: Optional[List[str]] = None,
    ids: Optional[Dict[str, str]] = None,
    run: bool = True,
    logger: Optional[Any] = None,
    compressor_efficiency: float = 0.75,
    mechanical_efficiency: float = 0.95,
    reboiler_approach_C: Optional[float] = None,
    **extra_options: Any,
) -> Dict[str, Any]:
    """Build a global heat-pump scheme between any source tower top vapor and any sink reboiler.

    Source side:
        source_column_id stage 2 vapor pseudo stream -> Compr -> HREB -> HCOND

    Sink side:
        sink_column_id stage N-1 liquid/default pseudo stream -> Flash2

    This generalizes the single-column MVR demo. When source_column_id == sink_column_id,
    it reduces to the original same-column heat-pump structure.

    Compatibility notes
    -------------------
    - reboiler_approach_C is accepted as an alias of reboiler_approach_c because
      earlier agent snippets used the capital-C name.
    - compressor_efficiency / mechanical_efficiency are explicit knobs for
      sensitivity and economic optimization; older versions hard-coded 0.75/0.95.
    - final_condenser_pressure defaults to 0.0, matching the verified heater convention
      used to compute residual condenser duty at source_top_temperature_c after HREB.
    """
    if reboiler_approach_C is not None:
        reboiler_approach_c = float(reboiler_approach_C)
    if extra_options:
        # Keep forward compatibility without failing on harmless future flags.
        pass

    prefix = _pair_id_prefix(source_column_id, sink_column_id)
    default_ids = {
        "reboiler_flash": f"F{prefix}R",
        "reboiler_pseudo": f"R{prefix}IN",
        "reboiler_vapor": f"R{prefix}V",
        "reboiler_liquid": f"R{prefix}L",
        "top_vapor_pseudo": f"T{prefix}IN",
        "compressor": f"K{prefix}",
        "compressor_out": f"K{prefix}O",
        "reboiler_supply_heater": f"H{prefix}R",
        "reboiler_supply_out": f"H{prefix}O",
        "final_condenser": f"H{prefix}C",
        "final_condenser_out": f"H{prefix}D",
    }
    ids = {**default_ids, **(ids or {})}

    b = AspenVirtualHeatPumpBuilder(aspen, logger=logger)
    src_id = _norm_id(source_column_id)
    sink_id = _norm_id(sink_column_id)
    src_n = max(2, int(source_n_stages))
    sink_n = max(2, int(sink_n_stages))
    rb_stage = int(sink_reboiler_liquid_stage) if sink_reboiler_liquid_stage is not None else max(1, sink_n - 1)
    tv_stage = max(1, min(int(source_top_vapor_stage), src_n))

    top_t = float(source_top_temperature_c)
    bot_t = float(sink_bottom_temperature_c)
    if compressor_outlet_pressure is None:
        base_p = _safe_float(source_top_pressure, None)
        if base_p is None or base_p <= 0:
            base_p = 1.01325
        compressor_outlet_pressure = base_p * float(pressure_ratio)
    hreb_target = bot_t + float(reboiler_approach_c)
    hcond_target = top_t if final_condenser_target_c is None else float(final_condenser_target_c)

    b.log(f"[BUILDER] {BUILDER_VERSION}")
    b.log(f"[GLOBAL-SCHEME] source={src_id} stage={tv_stage} vapor -> sink={sink_id} stage={rb_stage} liquid/default")

    # 1) Sink reboiler surrogate: sink column pseudo liquid -> Flash2.
    rb_feed = b.add_stream(ids["reboiler_pseudo"])
    rb_info = b.add_flash2_reboiler_surrogate(
        ids["reboiler_flash"], rb_feed, ids["reboiler_vapor"], ids["reboiler_liquid"], bot_t, float(flash_pressure)
    )
    if not b.connect_pseudo_stream_from_column(sink_id, rb_feed, column_pseudo_ports):
        raise RuntimeError(f"Cannot connect {rb_feed} to {sink_id} pseudo output port.")
    b.set_column_pseudo_stream(sink_id, rb_feed, rb_stage, phase=None)

    # Critical timing fix:
    # The sink reboiler pseudo stream must be populated by Aspen before it is
    # used as the temperature reference.  Otherwise HREB can silently use the
    # screening scalar sink_bottom_temperature_c and become much hotter than
    # the actual tower-bottom liquid pseudo stream.
    if run:
        try:
            b.run_aspen(f"populate {rb_feed} sink-reboiler pseudo temperature before HREB target")
        except Exception as exc:
            b.log(f"[WARN] preliminary pseudo-temperature run failed; fallback may be used: {exc}")

    rb_reference_temperature_c, rb_reference_basis = b.resolve_stream_temperature_for_approach(
        rb_feed, bot_t, "HREB ΔT reference from sink reboiler liquid pseudo stream AFTER preliminary run"
    )
    # Co-current assumption: use the actual sink reboiler pseudo liquid stream
    # temperature as the ΔT reference.  This fixes the previous use of the
    # screening scalar sink_bottom_temperature_c.
    hreb_target = rb_reference_temperature_c + float(reboiler_approach_c)

    # 2) Source top vapor: source column pseudo vapor -> compressor -> heat release -> final condenser.
    top_feed = b.add_stream(ids["top_vapor_pseudo"])
    comp = b.add_compressor(
        compressor_id=ids["compressor"],
        feed_stream=top_feed,
        out_stream=ids["compressor_out"],
        outlet_pressure=float(compressor_outlet_pressure),
        pressure_ratio=float(pressure_ratio),
        calculation_type="ISENTROPIC",
        outlet_spec="PRES",
        isentropic_efficiency=float(compressor_efficiency),
        mechanical_efficiency=float(mechanical_efficiency),
    )
    hreb = b.add_heater(
        ids["reboiler_supply_heater"], comp["out_stream"], ids["reboiler_supply_out"], hreb_target,
        0.0, f"heat release from {src_id} top vapor to {sink_id} reboiler surrogate"
    )
    hcond = b.add_heater(
        ids["final_condenser"], hreb["out_stream"], ids["final_condenser_out"], hcond_target,
        final_condenser_pressure, "final condenser surrogate after heat release; residual source-condenser duty"
    )
    if not b.connect_pseudo_stream_from_column(src_id, top_feed, column_pseudo_ports):
        raise RuntimeError(f"Cannot connect {top_feed} to {src_id} pseudo output port.")
    b.set_column_pseudo_stream(src_id, top_feed, tv_stage, phase="V")

    if run:
        b.run_aspen(f"global heat pump: {src_id} top vapor -> {sink_id} reboiler")

    rb_snapshot = b.snapshot_stream(rb_feed)
    tp_snapshot = b.snapshot_stream(top_feed)
    compressor_out_snapshot = b.snapshot_stream(comp["out_stream"])
    hreb_out_snapshot = b.snapshot_stream(hreb["out_stream"])
    hcond_out_snapshot = b.snapshot_stream(hcond["out_stream"])
    frb_duty = b.read_block_duty(ids["reboiler_flash"]) if run else None
    hreb_duty = b.read_block_duty(ids["reboiler_supply_heater"]) if run else None
    hcond_duty = b.read_block_duty(ids["final_condenser"]) if run else None
    comp_power = b.read_compressor_power(ids["compressor"]) if run else None

    # Aspen Heater convention: positive duty adds heat to the material stream;
    # negative duty removes heat.  A real heat-pump reboiler match therefore
    # requires HREB duty < 0.  Never convert a positive HREB duty into heat
    # supplied merely by taking abs().
    reboiler_demand = abs(frb_duty) if frb_duty is not None else None
    heat_release = abs(hreb_duty) if hreb_duty is not None and hreb_duty < 0 else 0.0
    external_reboiler = None
    if reboiler_demand is not None:
        external_reboiler = max(0.0, reboiler_demand - heat_release)
        b.log(f"[BALANCE] external_reboiler_raw = {reboiler_demand} - {heat_release} = {external_reboiler}")

    compressor_discharge_t = _safe_float(compressor_out_snapshot.get("temperature_raw"), None)
    discharge_margin_c = None if compressor_discharge_t is None else compressor_discharge_t - hreb_target
    physical_gate = {
        "hreb_is_heat_releasing": None if hreb_duty is None else bool(hreb_duty < 0),
        "hcond_is_nonheating": None if hcond_duty is None else bool(hcond_duty <= 0),
        "compressor_discharge_temperature_raw": compressor_discharge_t,
        "hreb_target_temperature_C": hreb_target,
        "compressor_discharge_margin_C": discharge_margin_c,
        "compressor_discharge_hotter_than_hreb_target": None if discharge_margin_c is None else bool(discharge_margin_c > 0),
    }

    return {
        "version": BUILDER_VERSION,
        "status": "built_and_run" if run else "built_not_run",
        "source_column": src_id,
        "sink_column": sink_id,
        "temperature_basis": {
            "source_top_temperature_C": top_t,
            "sink_bottom_temperature_C_input": bot_t,
            "sink_reboiler_liquid_pseudo_reference_stream": rb_feed,
            "sink_reboiler_liquid_pseudo_reference_temperature_C": rb_reference_temperature_c,
            "sink_reboiler_liquid_pseudo_reference_basis": rb_reference_basis,
            "reboiler_approach_C": float(reboiler_approach_c),
            "reboiler_supply_heater_target_C": hreb_target,
            "final_condenser_target_C": hcond_target,
        },
        "pressure_basis": {
            "source_top_pressure": source_top_pressure,
            "pressure_ratio": pressure_ratio,
            "compressor_outlet_pressure": compressor_outlet_pressure,
            "final_condenser_pressure": final_condenser_pressure,
        },
        "compressor_efficiency_basis": {
            "isentropic_efficiency": compressor_efficiency,
            "mechanical_efficiency": mechanical_efficiency,
        },
        "pseudo_streams": {
            "sink_reboiler_liquid": {
                "column": sink_id,
                "stream": rb_feed,
                "stage": rb_stage,
                "phase": "default liquid",
                "path": rf"\Data\Blocks\{sink_id}\Input\PSEUDO_STAGE\{rb_feed}",
            },
            "source_top_vapor": {
                "column": src_id,
                "stream": top_feed,
                "stage": tv_stage,
                "phase": "V",
                "stage_path": rf"\Data\Blocks\{src_id}\Input\PSEUDO_STAGE\{top_feed}",
                "phase_path": rf"\Data\Blocks\{src_id}\Input\PSEUDO_PHAS0\{top_feed}",
            },
        },
        "blocks": {"reboiler_flash": rb_info, "compressor": comp, "reboiler_supply_heater": hreb, "final_condenser": hcond},
        "streams": {
            "reboiler_pseudo_snapshot": rb_snapshot,
            "top_vapor_pseudo_snapshot": tp_snapshot,
            "compressor_out_snapshot": compressor_out_snapshot,
            "hreb_out_snapshot": hreb_out_snapshot,
            "hcond_out_snapshot": hcond_out_snapshot,
        },
        "physical_heat_exchange_gate": physical_gate,
        "energy_balance_raw": {
            "reboiler_flash_duty_raw": frb_duty,
            "heat_pump_heat_to_reboiler_raw_signed_HREB": hreb_duty,
            "heat_pump_heat_to_reboiler_raw_abs_HREB": heat_release,
            "heat_pump_hreb_absolute_duty_raw": abs(hreb_duty) if hreb_duty is not None else None,
            "external_reboiler_after_heat_pump_raw": external_reboiler,
            "final_condenser_duty_raw": hcond_duty,
            "final_condenser_duty_raw_signed_HCOND": hcond_duty,
            "final_condenser_residual_cooling_raw_abs": abs(hcond_duty) if hcond_duty is not None else None,
            "compressor_power_raw": comp_power,
            "note": "Raw values follow the active Aspen unit set. HREB is credited as heat release only when its signed Heater duty is negative; positive HREB duty means the block is heating the compressed stream and is not a valid heat-pump match. HCOND remains a virtual-branch diagnostic.",
        },
        "log": b.log_lines,
    }

def make_virtual_heat_pump_validation_payload(
    column: Dict[str, Any],
    candidate: Optional[Dict[str, Any]] = None,
    pressure_ratio: float = 2.0,
    reboiler_approach_c: float = 7.0,
) -> Dict[str, Any]:
    """Create a no-COM payload that the agent can display before Aspen execution."""
    candidate = candidate or {}
    settings = _as_dict(column.get("settings"))
    temps = _as_dict(column.get("temperatures"))

    def temp_c(pack: Any) -> Optional[float]:
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

    cid = _norm_id(column.get("id") or column.get("column") or candidate.get("column") or "C1")
    n = int(_safe_float(settings.get("n_stages") or column.get("n_stages"), 30) or 30)
    top_t = temp_c(temps.get("top"))
    bot_t = temp_c(temps.get("bottom"))
    if top_t is None:
        top_t = _safe_float(candidate.get("top_temperature_C"), None)
    if bot_t is None:
        bot_t = _safe_float(candidate.get("bottom_temperature_C"), None)

    return {
        "builder_version": BUILDER_VERSION,
        "callable": "build_column_heat_pump_virtual_scheme(aspen, ...)",
        "column_id": cid,
        "required_inputs": {
            "n_stages": n,
            "top_temperature_C": top_t,
            "bottom_temperature_C": bot_t,
            "top_pressure": "process_data column pressure if available; otherwise Aspen/feed pressure fallback",
            "pressure_ratio": pressure_ratio,
            "reboiler_approach_C": reboiler_approach_c,
        },
        "aspen_structure": [
            f"{cid} stage {n - 1} liquid/default pseudo stream RBIN -> FRB(Flash2)",
            f"{cid} stage 2 vapor pseudo stream TPIN -> KHP(Compr)",
            "KHP(Compr): TYPE=ISENTROPIC, OPT_SPEC=PRES, PRES=top_pressure*pressure_ratio, SEFF=0.75, MEFF=0.95",
            "KHP outlet KOUT -> HREB(Heater), HREB TEMP=reboiler_pseudo_stream_temperature+approach",
            "HREB outlet HPHOT -> HCOND(Heater), HCOND TEMP=top_temperature, Heater PRES/PRES1=0 zero-pressure-drop surrogate; HCOND duty = residual condenser load",
        ],
        "energy_interpretation": {
            "base_reboiler_surrogate": "abs(FRB duty)",
            "heat_supplied_by_heat_pump": "abs(HREB duty)",
            "external_reboiler_after_heat_pump": "max(0, abs(FRB duty) - abs(HREB duty))",
            "compressor_electricity": "KHP power/work output path, active Aspen unit set",
            "residual_source_condenser_load": "abs(HCOND duty); do not assume top condenser load becomes zero after HREB",
        },
    }


def make_cross_column_heat_pump_validation_payload(
    source_column: Dict[str, Any],
    sink_column: Dict[str, Any],
    candidate: Optional[Dict[str, Any]] = None,
    pressure_ratio: float = 2.0,
    reboiler_approach_c: float = 7.0,
) -> Dict[str, Any]:
    """Create a no-COM payload for global source-top-vapor -> sink-reboiler heat pump validation."""
    candidate = candidate or {}

    def temp_c(pack: Any) -> Optional[float]:
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

    src_settings = _as_dict(source_column.get("settings"))
    sink_settings = _as_dict(sink_column.get("settings"))
    src_temps = _as_dict(source_column.get("temperatures"))
    sink_temps = _as_dict(sink_column.get("temperatures"))

    src_id = _norm_id(source_column.get("id") or source_column.get("column") or candidate.get("source_column") or candidate.get("source_top_vapor_column") or "C1")
    sink_id = _norm_id(sink_column.get("id") or sink_column.get("column") or candidate.get("sink_column") or candidate.get("sink_reboiler_column") or "C1")
    src_n = int(_safe_float(src_settings.get("n_stages") or source_column.get("n_stages"), 30) or 30)
    sink_n = int(_safe_float(sink_settings.get("n_stages") or sink_column.get("n_stages"), 30) or 30)
    src_top_t = temp_c(src_temps.get("top"))
    sink_bot_t = temp_c(sink_temps.get("bottom"))
    if src_top_t is None:
        src_top_t = _safe_float(candidate.get("source_top_temperature_C"), None)
    if sink_bot_t is None:
        sink_bot_t = _safe_float(candidate.get("sink_bottom_temperature_C"), None)

    return {
        "builder_version": BUILDER_VERSION,
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
            f"{src_id} stage 2 vapor pseudo stream -> Compr",
            "Compr: TYPE=ISENTROPIC, OPT_SPEC=PRES, PRES=source_top_pressure*pressure_ratio, SEFF=0.75, MEFF=0.95",
            f"Compr outlet -> HREB(Heater), HREB TEMP={sink_id} reboiler pseudo stream temperature + approach",
            f"HREB outlet -> HCOND(Heater), HCOND TEMP={src_id} top temperature or specified condenser outlet temperature, Heater PRES/PRES1=0 zero-pressure-drop surrogate; HCOND duty = residual source-condenser load",
        ],
        "energy_interpretation": {
            "base_reboiler_surrogate": "abs(Flash2 duty on sink reboiler liquid pseudo stream)",
            "heat_supplied_by_heat_pump": "abs(HREB duty)",
            "external_reboiler_after_heat_pump": "max(0, abs(Flash2 duty) - abs(HREB duty))",
            "compressor_electricity": "Compr power/work output path, active Aspen unit set",
            "residual_source_condenser_load": "abs(HCOND duty); do not assume top condenser load becomes zero after HREB",
        },
    }
