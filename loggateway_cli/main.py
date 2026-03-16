from __future__ import annotations

import asyncio
import datetime as _dt
import inspect
import json
import os
import re
from pathlib import Path
from typing import Optional

import httpx
import typer
import websockets
import click
from rich.console import Console
from rich.table import Table
from rich.text import Text


console = Console()
app = typer.Typer(add_completion=True, no_args_is_help=True)
auth_app = typer.Typer(no_args_is_help=True)
app.add_typer(auth_app, name="auth")


def _config_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "logcli"
        return Path.home() / "AppData" / "Roaming" / "logcli"
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "logcli"
    return Path.home() / ".config" / "logcli"


def _config_path() -> Path:
    return _config_dir() / "config.json"


def load_config() -> dict:
    p = _config_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def save_config(data: dict) -> None:
    d = _config_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = _config_path()
    p.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def _ts() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _join_url(base: str, path: str) -> str:
    base = base.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def _to_ws_url(http_url: str) -> str:
    if http_url.startswith("ws://") or http_url.startswith("wss://"):
        return http_url
    if http_url.startswith("https://"):
        return "wss://" + http_url[len("https://") :]
    if http_url.startswith("http://"):
        return "ws://" + http_url[len("http://") :]
    return "ws://" + http_url


def _resolve_server_and_key(
    server: Optional[str], key: Optional[str]
) -> tuple[str, str]:
    cfg = load_config()
    s = (server or cfg.get("server") or "http://localhost:8080").strip()
    k = (key or cfg.get("key") or "").strip()
    return s, k


def _require_key(key: str) -> None:
    if not key:
        raise typer.BadParameter("Missing API key (use --key or `logcli auth set`)")


def _fetch_container_names(server: str, key: str) -> list[str]:
    if not key:
        return []
    url = _join_url(server, "/api/containers")
    headers = {"Authorization": f"Bearer {key}"}
    try:
        with httpx.Client(timeout=1.5) as client:
            r = client.get(url, headers=headers)
        if r.status_code != 200:
            return []
        data = r.json()
        if not isinstance(data, list):
            return []
        names: list[str] = []
        for c in data:
            n = c.get("name") if isinstance(c, dict) else None
            if isinstance(n, str) and n:
                names.append(n)
        return names
    except Exception:
        return []


try:
    from click.shell_completion import CompletionItem as _CompletionItem  # type: ignore
except Exception:  # pragma: no cover
    _CompletionItem = None


def _shell_complete_container(
    ctx: click.Context, param: click.Parameter, incomplete: str
):
    _ = (param,)
    params = getattr(ctx, "params", {}) or {}
    s, k = _resolve_server_and_key(params.get("server"), params.get("key"))
    names = _fetch_container_names(s, k)
    out = [n for n in names if not incomplete or n.startswith(incomplete)]
    if _CompletionItem is None:
        return out
    return [_CompletionItem(n) for n in out]


@auth_app.command("set")
def auth_set(
    server: str = typer.Option(..., help="Server base URL (http://host:port)"),
    key: str = typer.Option(..., help="API key (Bearer token)"),
):
    cfg = load_config()
    cfg["server"] = server.rstrip("/")
    cfg["key"] = key.strip()
    save_config(cfg)
    console.print(f"Saved credentials to [bold]{_config_path()}[/bold]")


@auth_app.command("show")
def auth_show():
    cfg = load_config()
    server = (cfg.get("server") or "").strip()
    key = (cfg.get("key") or "").strip()
    if not server and not key:
        console.print(f"No config found at [bold]{_config_path()}[/bold]")
        raise typer.Exit(0)
    console.print(f"config: [bold]{_config_path()}[/bold]")
    if server:
        console.print(f"server: {server}")
    console.print(f"key: {'(set)' if key else '(missing)'}")


@app.command("containers")
def containers(
    server: Optional[str] = typer.Option(
        None, help="Server base URL (defaults to config or http://localhost:8080)"
    ),
    key: Optional[str] = typer.Option(None, help="API key (defaults to config)"),
):
    server, key = _resolve_server_and_key(server, key)
    _require_key(key)

    url = _join_url(server, "/api/containers")
    headers = {"Authorization": f"Bearer {key}"}
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.get(url, headers=headers)
    except Exception as e:
        console.print(f"[red]error:[/red] failed to call server: {e}")
        raise typer.Exit(1)

    if r.status_code != 200:
        try:
            data = r.json()
            msg = data.get("error") if isinstance(data, dict) else None
            msg = msg or r.text
        except Exception:
            msg = r.text
        console.print(f"[red]error:[/red] {msg}")
        raise typer.Exit(1)

    data = r.json()
    if not isinstance(data, list) or not data:
        console.print("(no allowed running containers)")
        raise typer.Exit(0)

    t = Table(show_header=True, header_style="bold")
    t.add_column("NAME", style="cyan", no_wrap=True)
    t.add_column("STATUS", style="magenta")
    t.add_column("IMAGE")
    t.add_column("ID", style="dim")
    for c in data:
        if not isinstance(c, dict):
            continue
        t.add_row(
            str(c.get("name", "")),
            str(c.get("status", "")),
            str(c.get("image", "")),
            str(c.get("id", "")),
        )
    console.print(t)


async def _ws_logs(ws_url: str, key: str, tail: int, grep: Optional[str]) -> int:
    pattern = re.compile(grep) if grep else None
    headers = [("Authorization", f"Bearer {key}")]
    auth_msg = {"type": "auth", "token": key, "tail": int(tail)}

    connect_kwargs: dict = {"max_size": 2**22}
    try:
        sig = inspect.signature(websockets.connect)
        if "additional_headers" in sig.parameters:
            connect_kwargs["additional_headers"] = headers
        elif "extra_headers" in sig.parameters:
            connect_kwargs["extra_headers"] = headers
    except Exception:
        pass

    try:
        async with websockets.connect(ws_url, **connect_kwargs) as ws:
            await ws.send(json.dumps(auth_msg))
            async for message in ws:
                try:
                    data = json.loads(message)
                except Exception:
                    continue
                if data.get("type") == "error":
                    console.print(f"[red]error:[/red] {data.get('message')}")
                    return 1
                if data.get("type") != "log":
                    continue
                line = data.get("line")
                if not isinstance(line, str):
                    continue
                if pattern and not pattern.search(line):
                    continue
                console.print(Text(_ts(), style="dim"), line)
    except KeyboardInterrupt:
        return 0
    except Exception as e:
        console.print(f"[red]error:[/red] websocket failed: {e}")
        return 1
    return 0


@app.command("logs")
def logs(
    container: str = typer.Argument(
        ..., help="Container name", shell_complete=_shell_complete_container
    ),
    server: Optional[str] = typer.Option(
        None, help="Server base URL (defaults to config or http://localhost:8080)"
    ),
    key: Optional[str] = typer.Option(None, help="API key (defaults to config)"),
    tail: int = typer.Option(100, help="Show last N lines before follow"),
    grep: Optional[str] = typer.Option(None, help="Regex filter applied client-side"),
):
    server, key = _resolve_server_and_key(server, key)
    _require_key(key)

    base = server.rstrip("/")
    ws_base = _to_ws_url(base)
    ws_url = _join_url(ws_base, f"/api/logs/{container}") + f"?tail={int(tail)}"
    raise typer.Exit(
        asyncio.run(_ws_logs(ws_url=ws_url, key=key, tail=tail, grep=grep))
    )


@app.callback()
def _completion_hint():
    """Docker Log Gateway CLI."""


def main() -> int:
    app()
    return 0
