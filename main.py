import argparse
import json
import math
import os
import sys
import time
from datetime import date
from numbers import Real
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


API_BASE_URL = "https://api.fortyguard.com/v1"
DOCUMENTED_ENV_PARAMS_COST = 2110


class ApiTestError(Exception):
    pass


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run a credit-gated FortyGuard environmental-parameters API smoke test."
    )
    parser.add_argument("--execute", action="store_true", help="Submit the API request.")
    parser.add_argument(
        "--confirm-credits",
        action="store_true",
        help="Acknowledge that a successful request may consume API credits.",
    )
    parser.add_argument(
        "--activity-id",
        help="Fetch and print an existing task result without submitting a new request.",
    )
    parser.add_argument(
        "--plot",
        action="store_true",
        help="Create a chart and point-level heatmap PNG for --activity-id.",
    )
    parser.add_argument(
        "--plot-output",
        default="fortiguard_result.png",
        help="PNG output path for --plot (default: fortiguard_result.png).",
    )
    parser.add_argument("--latitude", type=float, default=40.7128)
    parser.add_argument("--longitude", type=float, default=-74.0060)
    parser.add_argument("--temperature", type=float, default=32.5)
    parser.add_argument("--date", default="2024-07-15", help="Date in YYYY-MM-DD format.")
    parser.add_argument("--time", default="14:00", help="Time in 24-hour HH:MM format.")
    parser.add_argument("--timeout", type=int, default=300, help="Maximum wait for completion.")
    parser.add_argument("--poll-interval", type=int, default=5)
    return parser.parse_args()


def validate_args(args):
    if not -90 <= args.latitude <= 90:
        raise ApiTestError("--latitude must be between -90 and 90.")
    if not -180 <= args.longitude <= 180:
        raise ApiTestError("--longitude must be between -180 and 180.")
    try:
        parsed_date = date.fromisoformat(args.date)
        if parsed_date.isoformat() != args.date:
            raise ValueError
    except ValueError as exc:
        raise ApiTestError("--date must be a valid YYYY-MM-DD date.") from exc
    try:
        hour, minute = (int(part) for part in args.time.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError as exc:
        raise ApiTestError("--time must be a valid 24-hour HH:MM time.") from exc
    if args.timeout <= 0 or args.poll_interval <= 0:
        raise ApiTestError("--timeout and --poll-interval must be positive.")


def request_json(url, api_key, method="GET", payload=None, timeout=30):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"api-key": api_key, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method)

    try:
        with urlopen(request, timeout=timeout) as response:
            status_code = response.status
            response_body = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        status_code = exc.code
        response_body = exc.read().decode("utf-8", errors="replace")
    except (TimeoutError, URLError, OSError) as exc:
        raise ApiTestError(f"Network request failed: {exc}") from exc

    response_body = response_body.replace(api_key, "[REDACTED]")
    try:
        decoded = json.loads(response_body) if response_body else {}
    except json.JSONDecodeError as exc:
        excerpt = response_body[:300].replace("\n", " ")
        raise ApiTestError(
            f"API returned non-JSON data (HTTP {status_code}): {excerpt}"
        ) from exc
    return status_code, decoded


def response_data(response):
    if not isinstance(response, dict):
        raise ApiTestError("API response was not a JSON object.")
    data = response.get("data", response)
    if not isinstance(data, dict):
        raise ApiTestError("API response did not contain an object in its data field.")
    return data


def submit_task(api_key, args):
    payload = {
        "latitude": args.latitude,
        "longitude": args.longitude,
        "temperature": args.temperature,
        "date_time": {
            "start_date": args.date,
            "start_time": args.time,
            "filter_type": 1,
        },
    }
    status_code, response = request_json(
        f"{API_BASE_URL}/env_params",
        api_key,
        method="POST",
        payload=payload,
    )
    if not 200 <= status_code < 300:
        raise ApiTestError(f"Submit failed with HTTP {status_code}: {response}")

    data = response_data(response)
    activity_id = data.get("activity_id")
    if not activity_id:
        raise ApiTestError(
            "Submit returned success but no data.activity_id was present; "
            f"response fields: {', '.join(data.keys()) or '(none)'}"
        )
    return str(activity_id)


def check_status(api_key, activity_id):
    encoded_id = quote(activity_id, safe="")
    return request_json(
        f"{API_BASE_URL}/status/{encoded_id}",
        api_key,
        timeout=30,
    )


def validate_result(response):
    data = response_data(response)
    result = data.get("result", data)
    if not isinstance(result, dict):
        raise ApiTestError("Completed response did not contain a result object.")

    expected_fields = {"metadata", "locations"}
    missing = expected_fields.difference(result)
    if missing:
        raise ApiTestError(
            "Task completed, but the documented environmental result fields "
            f"were missing: {', '.join(sorted(missing))}. "
            f"Returned fields: {', '.join(result.keys()) or '(none)'}"
        )

    locations = result["locations"]
    if not isinstance(locations, list):
        raise ApiTestError("Completed result field 'locations' was not a list.")
    has_location_parameters = any(
        isinstance(location, dict) and "parameters" in location
        for location in locations
    )
    if "parameters" not in result and not has_location_parameters:
        raise ApiTestError(
            "Completed result did not include 'parameters' at the result level "
            "or within any location. "
            f"Result fields: {', '.join(result.keys()) or '(none)'}"
        )
    print(
        "PASS: completed result includes metadata and locations, with "
        "environmental parameters."
    )


def load_api_key():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    try:
        with open(env_path, encoding="utf-8-sig") as env_file:
            for line in env_file:
                name, separator, value = line.strip().partition("=")
                if separator and name.strip() == "FORTYGUARD_API_KEY":
                    return value.strip().strip("'\"") or None
    except FileNotFoundError:
        return None
    return None


def numeric_series(parameters):
    series = {}
    if not isinstance(parameters, dict):
        return series
    for name, values in parameters.items():
        if not isinstance(values, list):
            values = [values]
        numeric_values = [
            float(value)
            if isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)
            else float("nan")
            for value in values
        ]
        if any(math.isfinite(value) for value in numeric_values):
            series[name] = numeric_values
    return series


def create_plot(result, activity_id, output_path):
    try:
        import matplotlib
        from matplotlib.ticker import FuncFormatter, MaxNLocator

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError as exc:
        raise ApiTestError(
            "Matplotlib is not installed. Run `uv sync` to install project dependencies."
        ) from exc

    locations = result.get("locations")
    if not isinstance(locations, list) or not locations:
        raise ApiTestError("No locations are available to plot.")

    metadata = result.get("metadata", {})
    timestamps = metadata.get("timestamps", []) if isinstance(metadata, dict) else []
    plot_series = []
    for location in locations:
        if not isinstance(location, dict):
            continue
        plot_series.append(numeric_series(location.get("parameters", {})))
    if not plot_series:
        raise ApiTestError("No environmental parameter values are available to plot.")

    keys = list(dict.fromkeys(key for series in plot_series for key in series))
    time_count = max((len(series.get(key, [])) for series in plot_series for key in keys), default=0)
    if time_count == 0:
        raise ApiTestError("No numeric environmental parameter values are available to plot.")

    values = np.full((len(keys), time_count), np.nan)
    for row, key in enumerate(keys):
        for series in plot_series:
            row_values = series.get(key, [])
            values[row, : len(row_values)] = np.where(
                np.isfinite(values[row, : len(row_values)]),
                values[row, : len(row_values)],
                row_values,
            )

    normalized = np.full_like(values, np.nan)
    for row, row_values in enumerate(values):
        finite = np.isfinite(row_values)
        if finite.any():
            low = np.nanmin(row_values)
            high = np.nanmax(row_values)
            normalized[row, finite] = (
                0.5 if math.isclose(low, high) else (row_values[finite] - low) / (high - low)
            )

    fig = plt.figure(figsize=(16, 9), constrained_layout=True)
    grid = fig.add_gridspec(2, 2, width_ratios=(1, 1.5), height_ratios=(1.1, 0.9))
    map_axis = fig.add_subplot(grid[0, 0])
    temperature_axis = fig.add_subplot(grid[1, 0])
    heatmap_axis = fig.add_subplot(grid[:, 1])

    valid_locations = [
        location
        for location in locations
        if isinstance(location, dict)
        and isinstance(location.get("lat"), Real)
        and isinstance(location.get("lon"), Real)
    ]
    if not valid_locations:
        raise ApiTestError("No valid latitude/longitude coordinates are available to plot.")

    heat_values = []
    for location in valid_locations:
        parameters = location.get("parameters", {})
        heat_index = parameters.get("heat_index_celsius") if isinstance(parameters, dict) else None
        if isinstance(heat_index, list):
            heat_index = next(
                (value for value in heat_index if isinstance(value, Real) and math.isfinite(value)),
                None,
            )
        if not isinstance(heat_index, Real) or isinstance(heat_index, bool):
            heat_index = location.get("temperature")
        heat_values.append(float(heat_index) if isinstance(heat_index, Real) else float("nan"))

    scatter = map_axis.scatter(
        [location["lon"] for location in valid_locations],
        [location["lat"] for location in valid_locations],
        c=heat_values,
        cmap="inferno",
        s=180,
        edgecolors="white",
        linewidths=1.2,
    )
    for location, value in zip(valid_locations, heat_values):
        if math.isfinite(value):
            map_axis.annotate(
                f"{value:.1f} C",
                (location["lon"], location["lat"]),
                xytext=(8, 8),
                textcoords="offset points",
                color="#202020",
                fontsize=9,
            )
    map_axis.set_title("Sampled locations (not interpolated)")
    map_axis.set_xlabel("Longitude")
    map_axis.set_ylabel("Latitude")
    map_axis.xaxis.set_major_locator(MaxNLocator(nbins=4))
    map_axis.yaxis.set_major_locator(MaxNLocator(nbins=4))
    map_axis.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:.4f}"))
    map_axis.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:.4f}"))
    map_axis.grid(alpha=0.25)
    if len(valid_locations) == 1:
        map_axis.set_xlim(valid_locations[0]["lon"] - 0.01, valid_locations[0]["lon"] + 0.01)
        map_axis.set_ylim(valid_locations[0]["lat"] - 0.01, valid_locations[0]["lat"] + 0.01)
    map_colorbar = fig.colorbar(scatter, ax=map_axis, shrink=0.8)
    map_colorbar.set_label("Heat index C (or reference temperature)")

    temperature_keys = (
        "heat_index_celsius",
        "apparent_temperature_celsius",
        "wet_bulb_temperature_celsius",
    )
    labels = []
    temperature_values = []
    for location in valid_locations:
        parameters = location.get("parameters", {})
        if not isinstance(parameters, dict):
            continue
        for key in temperature_keys:
            sample = parameters.get(key)
            if isinstance(sample, list):
                sample = next(
                    (value for value in sample if isinstance(value, Real) and math.isfinite(value)),
                    None,
                )
            if isinstance(sample, Real) and not isinstance(sample, bool):
                labels.append(key.replace("_celsius", "").replace("_", " "))
                temperature_values.append(float(sample))
    if temperature_values:
        temperature_axis.barh(labels, temperature_values, color="#e76f51")
        temperature_axis.set_xlabel("Temperature (C)")
        temperature_axis.set_title("Thermal indicators")
        temperature_axis.grid(axis="x", alpha=0.25)
    else:
        temperature_axis.text(0.5, 0.5, "No temperature indicators returned", ha="center", va="center")
        temperature_axis.set_axis_off()

    image = heatmap_axis.imshow(
        normalized,
        aspect="auto",
        interpolation="nearest",
        cmap="magma",
        vmin=0,
        vmax=1,
    )
    time_labels = []
    for index in range(time_count):
        label = str(timestamps[index]) if index < len(timestamps) else f"Sample {index + 1}"
        try:
            label = date.fromisoformat(label[:10]).strftime("%Y-%m-%d") + "\n" + label[11:16]
        except ValueError:
            pass
        time_labels.append(label)
    heatmap_axis.set_xticks(
        range(time_count),
        labels=time_labels,
        rotation=45 if time_count > 1 else 0,
        ha="right" if time_count > 1 else "center",
    )
    heatmap_axis.set_yticks(range(len(keys)), labels=[key.replace("_", " ") for key in keys])
    heatmap_axis.set_title("Environmental parameter heatmap")
    heatmap_axis.set_xlabel("Timestamp")
    heatmap_axis.set_ylabel("Parameter")
    for row in range(values.shape[0]):
        for column in range(values.shape[1]):
            value = values[row, column]
            if math.isfinite(value):
                heatmap_axis.text(
                    column,
                    row,
                    f"{value:g}",
                    ha="center",
                    va="center",
                    color="white" if normalized[row, column] < 0.65 else "black",
                    fontsize=8,
                )
    colorbar = fig.colorbar(image, ax=heatmap_axis, shrink=0.85)
    colorbar.set_label("Relative scale within each parameter")
    fig.suptitle(
        f"FortyGuard environmental results\nActivity {activity_id}",
        fontsize=15,
    )
    fig.text(
        0.5,
        -0.01,
        "Heatmap colors are normalized separately per parameter; cell labels show raw values. "
        "The location view shows sampled locations only, without spatial interpolation.",
        ha="center",
        fontsize=9,
    )
    try:
        fig.savefig(output_path, dpi=160, bbox_inches="tight")
    except OSError as exc:
        raise ApiTestError(f"Could not save chart to '{output_path}': {exc}") from exc
    finally:
        plt.close(fig)


def run_test(api_key, args):
    print("Submitting one environmental-parameters request...")
    print(
        f"Location: {args.latitude}, {args.longitude}; "
        f"time: {args.date} {args.time}; reference temperature: {args.temperature} C"
    )
    activity_id = submit_task(api_key, args)
    print(f"PASS: request accepted (activity ID: {activity_id}).")

    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        status_code, response = check_status(api_key, activity_id)
        if status_code == 404:
            # The API docs note that a new activity may briefly be unavailable.
            print("Status not available yet (HTTP 404); retrying after a short wait.")
        elif not 200 <= status_code < 300:
            raise ApiTestError(f"Status check failed with HTTP {status_code}: {response}")
        else:
            data = response_data(response)
            status = str(data.get("status", "")).strip().lower()
            if status in {"completed", "succeeded", "success"}:
                validate_result(response)
                print("PASS: API task completed successfully.")
                return
            if status in {"failed", "error"}:
                raise ApiTestError(
                    f"API task failed (activity ID: {activity_id}); "
                    f"response fields: {', '.join(data.keys())}"
                )
            if not status:
                raise ApiTestError(
                    "Status response did not include data.status; "
                    f"response fields: {', '.join(data.keys()) or '(none)'}"
                )
            print(f"Task status: {status}")
        time.sleep(min(args.poll_interval, max(0, deadline - time.monotonic())))

    raise ApiTestError(
        f"Timed out after {args.timeout}s while waiting for activity {activity_id}. "
        "Use the activity ID to check its status; do not submit a duplicate request."
    )


def show_activity(api_key, activity_id):
    status_code, response = check_status(api_key, activity_id)
    if not 200 <= status_code < 300:
        raise ApiTestError(f"Status check failed with HTTP {status_code}: {response}")

    data = response_data(response)
    print(f"Activity status: {data.get('status', 'unknown')}")
    result = data.get("result")
    if result is None:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return

    print("Result:")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return result


def main():
    args = parse_args()
    try:
        validate_args(args)
        if args.activity_id and args.execute:
            raise ApiTestError("--activity-id cannot be combined with --execute.")

        if args.activity_id:
            api_key = load_api_key()
            if not api_key:
                raise ApiTestError(
                    "Set FORTYGUARD_API_KEY in .env or the environment; do not put "
                    "the key in this script or share it in chat."
                )
            result = show_activity(api_key, args.activity_id)
            if args.plot:
                if not isinstance(result, dict):
                    raise ApiTestError("Task result is not available for plotting.")
                create_plot(result, args.activity_id, args.plot_output)
                print(f"PASS: chart saved to {args.plot_output}")
            return 0
        if args.plot:
            raise ApiTestError("--plot requires --activity-id.")

        if not args.execute:
            print("Dry run only; no API request was sent.")
            print(
                f"Would submit POST {API_BASE_URL}/env_params and poll "
                f"GET {API_BASE_URL}/status/{{activity_id}}."
            )
            print(
                f"The docs list a cost of {DOCUMENTED_ENV_PARAMS_COST:,} credits "
                "per successful request for API Basic/Startup; check your plan "
                "and balance because pricing may differ."
            )
            print("Use --execute --confirm-credits to send the request.")
            return 0

        if not args.confirm_credits:
            raise ApiTestError(
                "Execution is credit-gated. Add --confirm-credits only after "
                "checking your plan and available balance."
            )
        api_key = load_api_key()
        if not api_key:
            raise ApiTestError(
                "Set FORTYGUARD_API_KEY in .env or the environment; do not put "
                "the key in this script or share it in chat."
            )
        run_test(api_key, args)
        return 0
    except ApiTestError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
