#!/usr/bin/env python3
"""Sync BLOFIN_* from Documents credential files into .env (no secret output)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ENV_PATH = ROOT / ".env"
CRED_PATHS = [
    Path.home() / "Documents" / "1B Blofin API.txt",
    Path.home() / "Documents" / "Blofin API 2 1B.txt",
    Path.home() / "OneDrive" / "Documents" / "1B Blofin API.txt",
    Path.home() / "OneDrive" / "Documents" / "Blofin API 2 1B.txt",
]


def parse_cred_file(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    out: dict[str, str] = {}
    patterns = [
        (r"(?i)^(?:api[_ -]?key|access[_ -]?key)\s*[:=]\s*(.+)$", "key"),
        (r"(?i)^(?:secret(?:\s*key)?|api[_ -]?secret|access[_ -]?secret)\s*[:=]\s*(.+)$", "secret"),
        (r"(?i)^(?:passphrase|password|pass)\s*[:=]\s*(.+)$", "pass"),
    ]
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for pat, key in patterns:
            m = re.match(pat, line)
            if m:
                out[key] = m.group(1).strip().strip("\"'")
    if len(out) < 3:
        for line in text.splitlines():
            parts = re.split(r"[\t,|]+", line.strip())
            if len(parts) >= 3 and all(len(p) > 8 for p in parts[:3]):
                out.setdefault("key", parts[0].strip().strip("\"'"))
                out.setdefault("secret", parts[1].strip().strip("\"'"))
                out.setdefault("pass", parts[2].strip().strip("\"'"))
    return out


def read_env() -> dict[str, str]:
    if not ENV_PATH.is_file():
        return {}
    vals: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        vals[k.strip()] = v.strip().strip("\"'")
    return vals


def write_env_updates(updates: dict[str, str]) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    keys = {
        "BLOFIN_API_KEY": updates.get("key"),
        "BLOFIN_SECRET": updates.get("secret"),
        "BLOFIN_PASSPHRASE": updates.get("pass"),
    }
    seen = set()
    new_lines: list[str] = []
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            k = line.split("=", 1)[0].strip()
            if k in keys and keys[k]:
                new_lines.append(f"{k}={keys[k]}")
                seen.add(k)
                continue
        new_lines.append(line)
    for k, v in keys.items():
        if v and k not in seen:
            new_lines.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(new_lines) + ("\n" if new_lines else ""), encoding="utf-8")


def test_api(key: str, secret: str, passphrase: str, *, demo: bool) -> tuple[bool, str]:
    from blofin_http import BlofinHttp

    h = BlofinHttp(key, secret, passphrase, demo=demo)
    try:
        data = h.get_balance("futures")
        if data is None:
            return False, "empty response"
        return True, "ok"
    except Exception as e:
        return False, str(e)[:200]


def main() -> int:
    all_creds: list[tuple[str, dict[str, str]]] = []
    for p in CRED_PATHS:
        if p.is_file():
            parsed = parse_cred_file(p)
            if len(parsed) >= 3:
                all_creds.append((str(p), parsed))
    if not all_creds:
        print("No complete credential file found in Documents", file=sys.stderr)
        return 1

    env = read_env()
    mode = (env.get("BLOFIN_MODE") or env.get("MODE") or "live").strip().lower()
    demo = mode == "demo"

    candidates: list[tuple[str, dict[str, str]]] = []
    if env.get("BLOFIN_API_KEY") and env.get("BLOFIN_SECRET") and env.get("BLOFIN_PASSPHRASE"):
        candidates.append(
            (
                ".env",
                {
                    "key": env["BLOFIN_API_KEY"],
                    "secret": env["BLOFIN_SECRET"],
                    "pass": env["BLOFIN_PASSPHRASE"],
                },
            )
        )
    for source, parsed in all_creds:
        candidates.append((source, parsed))

    working_src = ""
    working: dict[str, str] | None = None
    last_err = ""
    for source, c in candidates:
        ok, msg = test_api(c["key"], c["secret"], c["pass"], demo=demo)
        if ok:
            working_src = source
            working = c
            break
        last_err = msg

    if not working:
        # Retry live if demo failed (or vice versa)
        alt_demo = not demo
        for source, c in candidates:
            ok, msg = test_api(c["key"], c["secret"], c["pass"], demo=alt_demo)
            if ok:
                working_src = source
                working = c
                demo = alt_demo
                break
            last_err = msg

    if not working:
        print(f"API test failed: {last_err}", file=sys.stderr)
        return 2

    mode_label = "demo" if demo else "live"
    if (
        env.get("BLOFIN_API_KEY") == working["key"]
        and env.get("BLOFIN_SECRET") == working["secret"]
        and env.get("BLOFIN_PASSPHRASE") == working["pass"]
    ):
        print(f"BLOFIN credentials OK ({mode_label}), no .env change needed")
        return 0

    write_env_updates(working)
    print(f"Updated .env BLOFIN_* from {working_src} — API OK ({mode_label})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
