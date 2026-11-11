"""Isolated worker: no persistent research cache and no child network workers."""
import json
import socket
import sys
from pathlib import Path

from .investment import run_research
from .model import call
from .public_web import PinnedHTTPS, PublicHTTP, PublicResearch


def main():
    raw = sys.stdin.read(128 * 1024 + 1)
    if len(raw) > 128 * 1024:
        raise ValueError("input_too_large")
    request = json.loads(raw)
    if not isinstance(request, dict) or set(request) != {"data", "key_file", "api_base"}:
        raise ValueError("invalid_worker_request")
    data = request["data"]
    if not isinstance(data, dict) or request["api_base"] != "https://api.apiroute.asia/v1":
        raise ValueError("invalid_worker_scope")
    fetcher = PublicHTTP(resolver=socket.getaddrinfo, connection=PinnedHTTPS).get
    research = PublicResearch(cache=None, fetch=fetcher, key_file=Path(request["key_file"]))
    result, usages, evidence = run_research(data, Path(request["key_file"]), request["api_base"],
        model_call=call, research=research)
    sys.stdout.write(json.dumps({"result": result, "usages": usages, "evidence": evidence}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError):
        sys.stdout.write(json.dumps({"result": {"failure": "investment_worker_failed"}, "usages": [],
            "evidence": ["investment_data"]}, ensure_ascii=False))
