from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import AsyncGenerator, Dict, List

import docker
from docker.errors import APIError, DockerException, NotFound


class DockerUnavailable(Exception):
    pass


class DockerUpstreamError(Exception):
    pass


@dataclass
class ContainerInfo:
    name: str
    id: str
    status: str
    image: str

    def as_dict(self) -> Dict[str, str]:
        return {
            "name": self.name,
            "id": self.id,
            "status": self.status,
            "image": self.image,
        }


def _client():
    try:
        return docker.from_env()
    except DockerException as e:
        raise DockerUnavailable("Docker socket unavailable") from e


def list_containers() -> List[ContainerInfo]:
    cli = _client()
    try:
        containers = cli.containers.list(filters={"status": "running"})
    except DockerException as e:
        raise DockerUnavailable("Docker socket unavailable") from e

    out: List[ContainerInfo] = []
    for c in containers:
        try:
            img = ""
            try:
                tags = getattr(c.image, "tags", None) or []
                img = tags[0] if tags else (getattr(c.image, "short_id", "") or "")
            except Exception:
                img = ""
            out.append(
                ContainerInfo(
                    name=str(getattr(c, "name", "")),
                    id=str(getattr(c, "id", ""))[:12],
                    status=str(getattr(c, "status", "")),
                    image=str(img),
                )
            )
        except Exception:
            # Skip malformed entries; never leak raw docker object.
            continue
    return out


async def stream_logs(
    container_name: str, tail: int = 100
) -> AsyncGenerator[str, None]:
    cli = _client()
    try:
        container = cli.containers.get(container_name)
    except NotFound as e:
        raise e
    except APIError as e:
        raise DockerUpstreamError("Docker API error") from e
    except DockerException as e:
        raise DockerUnavailable("Docker socket unavailable") from e

    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue(maxsize=2000)
    stop = threading.Event()

    def _put(item):
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            # Drop lines under backpressure.
            pass

    def _worker() -> None:
        err: Exception | None = None
        try:
            stream = container.logs(
                stream=True,
                follow=True,
                tail=int(tail),
                stdout=True,
                stderr=True,
                timestamps=False,
            )
            buf = ""
            for chunk in stream:
                if stop.is_set():
                    break
                try:
                    text = chunk.decode("utf-8", errors="replace")
                except Exception:
                    continue
                buf += text
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    loop.call_soon_threadsafe(_put, line)
            if buf:
                loop.call_soon_threadsafe(_put, buf)
        except Exception as e:
            err = e
        finally:
            if err is not None:
                loop.call_soon_threadsafe(_put, err)
            loop.call_soon_threadsafe(_put, None)

    t = threading.Thread(target=_worker, name="log-stream", daemon=True)
    t.start()
    try:
        while True:
            item = await q.get()
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            yield str(item)
    finally:
        stop.set()
