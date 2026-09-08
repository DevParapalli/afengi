"""Dump the live /decide stream from the deployed app to a file -- local only.

Sends ONE real request to the running app and writes the response to a
file next to this script. No gateway/LLM/DB is touched directly by the script --
it just talks HTTP to the app, which does the rest.

Streaming (default): reads the Server-Sent Events response and writes each frame
as ``ts: <arrival>\nevent: <name>\ndata: <json>\n\n`` -- the ts: line is the
client-side arrival time, handy for eyeballing keep_alive heartbeat gaps. Output
goes to frames.txt.

--no-stream: hits the plain JSON path instead and writes the single JSON
response (pretty-printed, with a ts: header) to frame.txt.

The request body is the REQUEST const below; override the whole body with either
``--json '<json>'`` or ``--json-file <path>``. The "stream" flag is set
automatically (true, or false with --no-stream).

Point it at any agent with ``--url`` (used verbatim -- include the full path,
e.g. another agent on another port) or edit BASE_URL.

Timeouts are first-class so you can exercise the server heartbeat (a keep_alive
frame every ~15s of agent silence) and client read-timeout behaviour: raise
--read-timeout above the heartbeat to watch keep_alive frames arrive, or drop it
below the agent's response time to force a client-side timeout (whatever streamed
so far is still written).

Run::

    uv run scripts/dump_frames.py
    uv run scripts/dump_frames.py --no-stream                  # sync -> frame.txt
    uv run scripts/dump_frames.py --url http://localhost:9000/decide  # another agent
    uv run scripts/dump_frames.py --json-file myrequest.json --out myrequest.txt
    uv run scripts/dump_frames.py --read-timeout 5             # force a timeout
    uv run scripts/dump_frames.py --read-timeout none          # wait forever
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import requests

# --- Defaults (the edit surface) --------------------------------------------

# Full URL of the app endpoint that returns the frames (used verbatim).
BASE_URL = "http://localhost:8182/decide"

# Auth: X-User-Id yields an authenticated "user" identity (no allowlist needed).
# Swap for {"X-API-KEY": "<key>"} if the deployment validates against api_keys.
HEADERS = {"X-User-Id": "dump-frames"}

# The input sent to the app. Override at runtime with --json / --json-file.
# The "stream" flag is forced on before sending (off with --no-stream).
REQUEST: dict = {
    "text": "Where is my order ORD-118422?",
    "contextId": "ctx-ORD-118422",
    "metadata": {
        "category": "Orders",
        "coreIssue": "Status",
        "reference": "REQ-4471",
        "summary": "Customer asking for order status",
        "contextExtracted": {"orderId": "ORD-118422"},
    },
}

# Output files (written next to this script) and default (connect, read) timeouts
# in seconds. read=None waits forever (heartbeats keep the socket busy).
OUTPUT_NAME = "frames.txt"  # streaming
SYNC_OUTPUT_NAME = "frame.txt"  # --no-stream (single JSON response)
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT: float | None = 300.0


def _parse_timeout(value: str) -> float | None:
    """--read-timeout: 'none'/'0'/'' -> no timeout, else a float (seconds)."""
    if value.strip().lower() in {"none", "0", ""}:
        return None
    return float(value)


def _load_body(args: argparse.Namespace, stream: bool) -> dict:
    """Resolve the request body: --json / --json-file override the REQUEST const."""
    if args.json is not None:
        body = json.loads(args.json)
    elif args.json_file is not None:
        body = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
    else:
        body = copy.deepcopy(REQUEST)
    # stream=True -> SSE frames; stream=False -> one JSON response.
    body["stream"] = stream
    return body


def _now() -> str:
    """Frame arrival timestamp (UTC ISO-8601), written as the ts: line."""
    return datetime.now(UTC).isoformat()


def _run_stream(url, body, headers, timeout, out_path) -> tuple[int | None, int]:
    """Read SSE frames, prefix each with a ts: line, write to file + stdout.

    Splits the byte stream on the SSE blank-line separator so each frame is
    timestamped when it arrives. A ReadTimeout keeps whatever streamed so far.
    """
    frames = 0
    with requests.post(url, json=body, headers=headers, stream=True, timeout=timeout) as resp:
        resp.encoding = "utf-8"
        with out_path.open("w", encoding="utf-8", newline="") as fh:
            buffer = ""

            def emit(frame: str) -> None:
                nonlocal frames
                block = f"ts: {_now()}\n{frame}\n\n"
                fh.write(block)
                fh.flush()
                sys.stdout.write(block)
                sys.stdout.flush()
                frames += 1

            try:
                for chunk in resp.iter_content(chunk_size=None):
                    if not chunk:
                        continue
                    buffer += chunk.decode("utf-8", errors="replace")
                    while "\n\n" in buffer:  # one complete SSE frame
                        frame, buffer = buffer.split("\n\n", 1)
                        if frame.strip():
                            emit(frame)
            except requests.exceptions.ReadTimeout:
                print("\n[read timeout -- partial dump kept]", file=sys.stderr)
            if buffer.strip():  # trailing frame with no terminating blank line
                emit(buffer.rstrip("\n"))
        return resp.status_code, frames


def _run_sync(url, body, headers, timeout, out_path) -> tuple[int | None, int]:
    """Sync SendMessage: write the single JSON response (pretty) with a ts: line."""
    resp = requests.post(url, json=body, headers=headers, timeout=timeout)
    try:
        text = json.dumps(resp.json(), indent=2, ensure_ascii=False)
    except ValueError:
        text = resp.text
    block = f"ts: {_now()}\n{text}\n"
    out_path.write_text(block, encoding="utf-8")
    sys.stdout.write(block)
    return resp.status_code, 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--json", help="Inline JSON request body (overrides the const).")
    src.add_argument("--json-file", help="Path to a JSON file with the request body.")
    parser.add_argument(
        "--url",
        default=BASE_URL,
        help=f"Full URL to POST to, used as-is (default {BASE_URL}).",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Hit the sync SendMessage endpoint (one JSON response) instead of "
        "streaming; default output becomes frame.txt.",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output file name (default frames.txt, or frame.txt with --no-stream).",
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=CONNECT_TIMEOUT,
        help=f"Connect timeout in seconds (default {CONNECT_TIMEOUT}).",
    )
    parser.add_argument(
        "--read-timeout",
        type=_parse_timeout,
        default=READ_TIMEOUT,
        help=f"Read timeout in seconds; 'none' waits forever (default {READ_TIMEOUT}).",
    )
    args = parser.parse_args()

    stream = not args.no_stream
    body = _load_body(args, stream)
    url = args.url
    out_name = args.out or (SYNC_OUTPUT_NAME if args.no_stream else OUTPUT_NAME)
    out_path = Path(__file__).resolve().parent / out_name
    accept = "application/json" if args.no_stream else "text/event-stream"
    headers = {"Content-Type": "application/json", "Accept": accept, **HEADERS}
    timeout = (args.connect_timeout, args.read_timeout)

    print(
        f"POST {url}  stream={stream}  read-timeout={args.read_timeout}",
        file=sys.stderr,
    )
    try:
        if args.no_stream:
            status, frames = _run_sync(url, body, headers, timeout, out_path)
        else:
            status, frames = _run_stream(url, body, headers, timeout, out_path)
    except requests.exceptions.ConnectTimeout:
        print(
            f"Connect timed out after {args.connect_timeout}s to {url}.",
            file=sys.stderr,
        )
        return 2
    except requests.exceptions.ConnectionError as exc:
        print(f"Could not reach {url}: {exc}", file=sys.stderr)
        print(f"Is the app running on {args.url}?", file=sys.stderr)
        return 2
    except requests.exceptions.ReadTimeout:
        print(f"Read timed out after {args.read_timeout}s.", file=sys.stderr)
        return 2

    print(f"\nstatus={status}  frames={frames}  ->  {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
