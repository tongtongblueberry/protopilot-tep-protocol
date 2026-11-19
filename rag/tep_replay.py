"""Replay visible paired TEP traces and export a separate, as-of operator view.

Uses Python's standard library. This reads a saved simulation trace and does not
register a procedure, infer recorded alarms, or issue a control action.
"""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path


ARMS = ("keep", "lower")
SIGNALS = ("reactor_temp_C", "reactor_pressure_kPag", "product_flow_m3_h")
COLUMNS = ("time_h",) + tuple(f"{arm}_{signal}" for arm in ARMS for signal in SIGNALS)
CLOCK_TOLERANCE_H = 1e-9
REFERENCE_LIMITS = {
    "reactor_temp_C": {"upper": 150.0, "unit": "degC", "tag": "XMEAS(9)"},
    "reactor_pressure_kPag": {"upper": 2895.0, "unit": "kPa(g)", "tag": "XMEAS(7)"},
}
REFERENCE_SOURCE = {
    "uri": "https://users.abo.fi/khaggblo/RS/Downs.pdf#page=5",
    "locator": "Downs and Vogel (1993), p. 249, Table 6, normal operating high limits",
    "scope": "Benchmark reference ranges; recorded alarm settings are not supplied.",
}


def read_trace(path):
    """Select visible fields, including when an evaluator CSV has extra columns."""
    with Path(path).open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        names = reader.fieldnames or []
        if len(names) != len(set(names)) or not set(COLUMNS).issubset(names):
            raise ValueError("CSV requires unique headers and all visible trace columns")
        rows = []
        for line, raw in enumerate(reader, 2):
            try:
                row = {name: float(raw[name]) for name in COLUMNS}
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Missing or nonnumeric visible value at line {line}") from exc
            if not all(math.isfinite(value) for value in row.values()):
                raise ValueError(f"Nonfinite visible value at line {line}")
            if row["time_h"] < 0 or (rows and row["time_h"] <= rows[-1]["time_h"]):
                raise ValueError("Sample times must be nonnegative and strictly increasing")
            rows.append(row)
    if len(rows) < 2:
        raise ValueError("At least two visible samples are required")
    return rows


def integrate(rows, column):
    return math.fsum(
        (right["time_h"] - left["time_h"]) * (left[column] + right[column]) / 2
        for left, right in zip(rows, rows[1:])
    )


def screen_range(rows, column, upper):
    outside = [row[column] > upper for row in rows]
    first = next((row["time_h"] for row, above in zip(rows, outside) if above), None)
    return {
        "samples_above_upper": sum(outside),
        "upward_crossings_between_samples": sum(
            not previous and current for previous, current in zip(outside, outside[1:])
        ),
        "first_sample_above_upper_h": first,
        "maximum": max(row[column] for row in rows),
    }


def comparison(rows, decision_h):
    if not math.isfinite(decision_h):
        raise ValueError("Decision time must be finite")
    matching = [i for i, row in enumerate(rows)
                if abs(row["time_h"] - decision_h) <= CLOCK_TOLERANCE_H]
    if len(matching) != 1 or matching[0] == len(rows) - 1:
        raise ValueError("Decision time must match one recorded sample before the final sample")
    index = matching[0]
    window = rows[index:]
    duration = window[-1]["time_h"] - window[0]["time_h"]
    arms = {}
    for arm in ARMS:
        stats = {}
        for signal in SIGNALS:
            column = f"{arm}_{signal}"
            values = [row[column] for row in window]
            stats[signal] = {"min": min(values), "max": max(values), "end": values[-1],
                             "mean": integrate(window, column) / duration}
        stats["product_volume_m3"] = integrate(window, f"{arm}_product_flow_m3_h")
        stats["reference_range_screen_entire_record"] = {
            signal: screen_range(rows, f"{arm}_{signal}", definition["upper"])
            for signal, definition in REFERENCE_LIMITS.items()
        }
        arms[arm] = stats
    prefix_equal = all(row[f"keep_{signal}"] == row[f"lower_{signal}"]
                       for row in rows[:index + 1] for signal in SIGNALS)
    keep, lower = arms["keep"], arms["lower"]
    return {
        "origin": "tep_replay", "analysis_scope": "saved visible trace comparison",
        "n_samples_per_arm": len(rows),
        "record_window_h": [rows[0]["time_h"], rows[-1]["time_h"]],
        "postdecision_window_h": [window[0]["time_h"], window[-1]["time_h"]],
        "integration_method": "trapezoidal integral over recorded times, including decision boundary",
        "clock_tolerance_h": CLOCK_TOLERANCE_H,
        "visible_predecision_samples": index + 1,
        "visible_predecision_values_equal": prefix_equal,
        "reference_source": REFERENCE_SOURCE, "reference_limits": REFERENCE_LIMITS,
        "arms": arms,
        "lower_minus_keep": {
            "product_volume_m3": lower["product_volume_m3"] - keep["product_volume_m3"],
            "product_volume_litres": 1000 * (lower["product_volume_m3"] - keep["product_volume_m3"]),
            "max_reactor_temp_C": lower["reactor_temp_C"]["max"] - keep["reactor_temp_C"]["max"],
            "max_reactor_pressure_kPag": lower["reactor_pressure_kPag"]["max"] - keep["reactor_pressure_kPag"]["max"],
        },
        "recorded_alarm_history": "UNKNOWN",
        "measurement_quality_flags": "NOT_RECORDED",
        "limits": [
            "Only sampled visible values are screened; between-sample extrema are unknown.",
            "The input has no alarm lifecycle or quality flags; range screening does not establish alarms.",
            "Prefix equality is checked on the six exported signals; full-state pairing requires the generating experiment.",
            "Results describe this saved pair and do not establish an optimal control action.",
        ],
    }


def operator_view(rows, arm, cutoff_h):
    """The returned object contains no later values or other-arm observations."""
    if arm not in ARMS or not math.isfinite(cutoff_h):
        raise ValueError("Specify a valid arm and a finite cutoff time")
    if cutoff_h > rows[-1]["time_h"] + CLOCK_TOLERANCE_H:
        raise ValueError("Cutoff time is after the available trace")
    eligible = [row for row in rows if row["time_h"] <= cutoff_h + CLOCK_TOLERANCE_H]
    if not eligible:
        raise ValueError("No sample is available by the cutoff time")
    sample = eligible[-1]
    observations = [
        {"tag": definition["tag"], "value": sample[f"{arm}_{signal}"],
         "unit": definition["unit"], "sampled_at_sim_h": sample["time_h"],
         "quality": "NOT_RECORDED"}
        for signal, definition in REFERENCE_LIMITS.items()
    ]
    return {
        "origin": "tep_replay", "arm": arm, "cutoff_sim_h": cutoff_h,
        "clock_tolerance_h": CLOCK_TOLERANCE_H,
        "observations": observations,
        "missing_required_tags": ["XMEAS(8)", "XMEAS(21)"],
        "recorded_alarm_history": "UNKNOWN",
        "procedure_registration": "NOT_REGISTERED",
        "query_en": (
            f"At simulation hour {sample['time_h']:.9g}, reactor temperature is "
            f"{sample[f'{arm}_reactor_temp_C']:.6g} degC and reactor pressure is "
            f"{sample[f'{arm}_reactor_pressure_kPag']:.6g} kPa gauge. "
            "Reactor level, reactor cooling water outlet temperature, measurement quality "
            "and recorded alarm history are unavailable in this view. "
            "Which observations and procedure applicability checks are required before giving guidance?"
        ),
    }


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path, help="Paired visible CSV, with time_h and keep_/lower_ columns")
    parser.add_argument("--decision-h", type=float, default=2.0, help="Recorded boundary for paired outcome integration")
    parser.add_argument("--output", type=Path, help="Save the retrospective paired outcome JSON")
    parser.add_argument("--operator-output", type=Path, help="Save only the selected arm's as-of observations and query")
    parser.add_argument("--arm", choices=ARMS, default="keep")
    parser.add_argument("--as-of-h", type=float, help="Operator-view cutoff; defaults to decision time")
    args = parser.parse_args()
    outputs = [path.resolve() for path in (args.output, args.operator_output) if path]
    if args.csv.resolve() in outputs or len(outputs) != len(set(outputs)):
        parser.error("Use distinct input, comparison output and operator output paths")
    try:
        rows = read_trace(args.csv)
        summary = comparison(rows, args.decision_h)
        view = operator_view(rows, args.arm, args.as_of_h if args.as_of_h is not None else args.decision_h)
        summary["input_sha256"] = hashlib.sha256(args.csv.read_bytes()).hexdigest()
        if args.operator_output:
            write_json(args.operator_output, view)
        if args.output:
            write_json(args.output, summary)
        else:
            print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))
    except (OSError, ValueError, csv.Error) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
