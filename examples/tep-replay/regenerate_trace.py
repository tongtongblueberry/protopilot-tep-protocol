"""Compare two production targets during the same TEP cooling-water disturbance."""

import argparse
import hashlib
import json
import platform
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path

import numpy as np
import pandas as pd
from tep_studio.ui.config import DisturbanceActivation, ScenarioConfig, StepTestSpec
from tep_studio.ui.service import mode_default_setpoints, run_scenario


COMMIT = "852f0c69ab974540750d61bd1dbfafd56bfd80bb"
DECISION_H = 2.0
HORIZON_H = 8.0
DECREASE_M3_H = 0.10
SIGNALS = {
    "reactor_temp_C": "measurement.reactor_temperature",
    "reactor_pressure_kPag": "measurement.reactor_pressure",
    "separator_temp_C": "measurement.separator_temperature",
    "separator_level_pct": "measurement.separator_level",
    "product_flow_m3_h": "measurement.stripper_underflow",
    "cooling_valve_pct": "implemented_action.reactor_cooling_water_valve",
}


def summarize(window):
    time = window["time_end"].to_numpy()
    duration = time[-1] - time[0]
    totals = {}
    for short, tag in SIGNALS.items():
        values = window[tag].to_numpy()
        totals[short] = {
            "mean": float(np.trapezoid(values, time) / duration),
            "min": float(values.min()),
            "max": float(values.max()),
            "end": float(values[-1]),
        }
    totals["product_volume_m3"] = float(np.trapezoid(window[SIGNALS["product_flow_m3_h"]], time))
    return totals


def run(csv_path):
    target = mode_default_setpoints("mode1")["production_rate"]
    hold = StepTestSpec("setpoint", "production_rate", target, target, DECISION_H)
    base = ScenarioConfig(
        mode="mode1",
        loop_type="closed",
        horizon=HORIZON_H,
        control_interval=0.01,
        record_every=1,
        solver_method="RK4",
        fixed_step=0.0005,
        seed=1431655765.0,
        disturbances=(DisturbanceActivation("idv_04", start_time=1.0),),
        step_test=hold,
    )
    changed = replace(base, step_test=replace(hold, step_value=target - DECREASE_M3_H))
    result_a, result_b, repeat = [run_scenario(cfg, run_id="matched") for cfg in (base, changed, base)]
    a, b, again = [result.to_frame() for result in (result_a, result_b, repeat)]
    starts = a["time_start"].to_numpy()
    first_change_interval = int(np.flatnonzero(starts >= DECISION_H)[0])
    activation = float(a.loc[a["disturbance.idv_04"].eq(1), "time_start"].iloc[0])
    decision = float(starts[first_change_interval])
    if first_change_interval < 1 or not np.isclose(a["time_end"].iat[first_change_interval - 1], decision):
        raise RuntimeError("Cannot find the final common observation immediately before the decision.")
    same_prefix = a.iloc[:first_change_interval].equals(b.iloc[:first_change_interval])
    same_disturbances = a.filter(regex=r"^disturbance\.").equals(b.filter(regex=r"^disturbance\."))
    same_grid = a["time_end"].equals(b["time_end"])
    complete = all(abs(r.final_time - HORIZON_H) < 1e-8 for r in (result_a, result_b))
    if not (a.equals(again) and same_prefix and same_grid and same_disturbances and complete):
        raise RuntimeError(f"Full-window paired comparison failed: repeat={a.equals(again)}, "
                           f"prefix={same_prefix}, grid={same_grid}, inputs={same_disturbances}, "
                           f"end_times={(result_a.final_time, result_b.final_time)}")
    windows = [frame.iloc[first_change_interval - 1:] for frame in (a, b)]
    stats_a, stats_b = (summarize(window) for window in windows)
    trace = pd.DataFrame({"time_h": a["time_end"], "idv_04": a["disturbance.idv_04"]})
    for name, frame in (("keep", a), ("lower", b)):
        for short, tag in SIGNALS.items():
            trace[f"{name}_{short}"] = frame[tag]
    trace_bytes = trace.to_csv(index=False, float_format="%.17g", lineterminator="\n").encode()
    if csv_path is not None:
        csv_path.write_bytes(trace_bytes)
    return {
        "model": {"project": "khalidlabs/tep-studio", "source_commit": COMMIT,
                  "python": platform.python_version(), "machine": platform.machine(),
                  "packages": {p: version(p) for p in ("tep-studio", "numpy", "scipy", "pandas")}},
        "settings_keep": base.to_dict(),
        "lower_target_m3_h": changed.step_test.step_value,
        "actual_fault_start_h": activation, "actual_first_changed_target_start_h": decision,
        "window_h": [decision, float(a["time_end"].iat[-1])],
        "checks": {"all_rows_repeat_exactly": a.equals(again), "predecision_rows_equal": same_prefix,
                   "predecision_rows": first_change_interval, "columns_compared": len(a.columns),
                   "entire_external_disturbance_command_equal": same_disturbances,
                   "all_sample_times_equal": same_grid, "total_rows_per_run": len(a),
                   "postdecision_intervals": len(windows[0])-1},
        "shutdown": {"keep": result_a.terminated, "lower": result_b.terminated},
        "postdecision_keep": stats_a, "postdecision_lower": stats_b,
        "trace_sha256": hashlib.sha256(trace_bytes).hexdigest(),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, help="Write the paired signal histories for plotting.")
    args = parser.parse_args()
    print(json.dumps(run(args.csv), indent=2, ensure_ascii=False, allow_nan=False))
