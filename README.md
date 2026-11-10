# FortyGuard API smoke test

This script tests the documented environmental-parameters workflow:

1. Submit one `POST /v1/env_params` request.
2. Poll `GET /v1/status/{activity_id}` until the task completes or fails.
3. Check that the completed result contains `metadata` and `locations`, with
   `parameters` either at the result level or nested within a location.

The script uses Matplotlib for plotting. It does not send a request by default.
A successful request may consume credits. The
API docs list 2,110 credits per request for Basic/Startup plans; verify the current
cost for your account before proceeding.

## Run a dry run

From PowerShell in this folder:

```powershell
uv run python main.py
```

## Run one live test

Put the key in `.env` as `FORTYGUARD_API_KEY=...`. The script reads that variable
from `.env` (or from the current environment). Do not put the key in source code,
commit it, or paste it into chat. `.env` is ignored by Git.

```powershell
uv run python main.py --execute --confirm-credits
```

The default test uses the documented New York example coordinates and
`2024-07-15 14:00`. Override these values as needed:

```powershell
uv run python main.py --latitude 33.4484 --longitude -112.074 --temperature 35 `
  --date 2025-09-20 --time 14:00 --execute --confirm-credits
```

The test waits up to five minutes by default. If it times out after submission,
it prints the activity ID and tells you not to submit a duplicate. The script
redacts the API key if it appears in an API response.

## View an existing result

Fetch and pretty-print a task's current status and result without submitting a
new analysis or consuming additional analysis credits:

```powershell
uv run python main.py --activity-id 092f0771-28a5-4195-a86f-eccc3b5663a9
```

## Plot an existing result

Create a PNG with a thermal-indicator chart, a parameter-by-time heatmap, and a
point-level location view:

```powershell
uv run python main.py --activity-id 092f0771-28a5-4195-a86f-eccc3b5663a9 --plot
```

By default, the image is saved as `fortiguard_result.png` in the current folder.
Choose another output path with `--plot-output`. The API result has only one
sampled location, so the map marks the point rather than inventing a continuous
spatial heat surface. Heatmap colors are normalized independently per parameter;
the labels show the original values.