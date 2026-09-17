#!/usr/bin/env python3
"""Call Context Agent using only Python's standard library (no Gemini key needed)."""

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4


def tenant_id(value):
    if value == "general" or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", value):
        raise argparse.ArgumentTypeError("Use a valid customer tenant ID; general is reserved.")
    return value


def positive_timeout(value):
    value = float(value)
    if not 0 < value < float("inf"):
        raise argparse.ArgumentTypeError("Timeout must be a positive finite number.")
    return value


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--base-url", default="http://localhost:8000")
    root.add_argument("--timeout", type=positive_timeout, default=240)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("health", help="Check API and storage readiness")
    for name, field, help_text in [
        ("put-document", "summary", "Create or REPLACE the tenant's entire document"),
        ("add-knowledge", "content", "Add new knowledge"),
        ("update-knowledge", "change", "Update existing knowledge"),
        ("set-instructions", "instructions", "Set the tenant's business instructions"),
        ("chat", "message", "Ask the agent a question"),
    ]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--tenant", type=tenant_id, required=True)
        if name == "put-document":
            command.add_argument("--title", required=True)
        if name == "chat":
            command.add_argument("--request-id", type=UUID, help="Defaults to a generated UUID")
            command.add_argument(
                "--user",
                dest="external_user_id",
                help="Stable user id; reusing it continues one conversation (enables memory).",
            )
            command.add_argument(
                "--channel", default="web", help="Channel label for the conversation (default web)."
            )
        source = command.add_mutually_exclusive_group(required=True)
        source.add_argument("--" + field, dest="text", help="Input text")
        source.add_argument("--file", type=Path, help="Read input text from a UTF-8 file")
    get_instr = commands.add_parser("get-instructions", help="Show tenant business instructions")
    get_instr.add_argument("--tenant", type=tenant_id, required=True)
    return root


def make_request(args):
    if args.command == "health":
        return "GET", "/health/ready", None
    if args.command == "get-instructions":
        return "GET", f"/api/v1/tenants/{args.tenant}/instructions", None
    content = args.file.read_text(encoding="utf-8") if args.file else args.text
    if not content.strip() or len(content.strip()) > 60000:
        raise ValueError("Input must contain between 1 and 60000 characters.")
    base = f"/api/v1/tenants/{args.tenant}"
    if args.command == "put-document":
        if not 1 <= len(args.title.strip()) <= 500:
            raise ValueError("Title must contain between 1 and 500 characters.")
        return "PUT", base + "/document", {"title": args.title, "summary": content}
    if args.command == "add-knowledge":
        return "POST", base + "/document/knowledge-units", {"content": content}
    if args.command == "update-knowledge":
        return "PATCH", base + "/document/knowledge-units", {"change": content}
    if args.command == "set-instructions":
        return "PUT", base + "/instructions", {"instructions": content}
    payload = {"message": content, "request_id": str(args.request_id or uuid4())}
    if args.external_user_id:
        payload["channel"] = args.channel
        payload["external_user_id"] = args.external_user_id
    return "POST", base + "/agent/messages", payload


def print_response(status, body):
    print(f"HTTP {status}", file=sys.stderr)
    try:
        print(json.dumps(json.loads(body), indent=2, ensure_ascii=False))
    except (ValueError, UnicodeError):
        print(body.decode("utf-8", errors="replace"))


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        method, path, payload = make_request(args)
        url = args.base_url.rstrip("/") + path
        if not url.startswith(("http://", "https://")):
            raise ValueError("Base URL must start with http:// or https://.")
        print(f"{method} {url}", file=sys.stderr)
        if payload and "request_id" in payload:
            print(f"Request ID: {payload['request_id']}", file=sys.stderr)
        request = Request(
            url,
            method=method,
            data=json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        # No automatic retries: mutations or tool calls might already have taken effect.
        with urlopen(request, timeout=args.timeout) as response:
            print_response(response.status, response.read())
        return 0
    except HTTPError as exc:
        print_response(exc.code, exc.read())
        return 1
    except (OSError, ValueError, URLError) as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
