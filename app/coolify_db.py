from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import docker
from docker.errors import DockerException


@dataclass
class CoolifyResource:
    project: str
    environment: str
    app: str = ""
    service: str = ""


@dataclass
class CoolifyConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    container_name: str = ""


@dataclass
class CoolifyProject:
    name: str
    environment: str
    services: List[str] = field(default_factory=list)


class CoolifyDBManager:
    def __init__(self):
        self._projects: List[CoolifyProject] = []
        self._resource_map: Dict[str, CoolifyResource] = {}
        self._config: Optional[CoolifyConfig] = None
        self._lock = threading.RLock()
        self._refresh_interval = 60
        self._stop_event = threading.Event()
        self._refresh_thread: Optional[threading.Thread] = None

    @property
    def is_configured(self) -> bool:
        return self._config is not None

    @property
    def projects(self) -> List[CoolifyProject]:
        with self._lock:
            return list(self._projects)

    def load_config_from_file(self) -> Optional[CoolifyConfig]:
        # Check for manual override via environment variable
        db_url = os.getenv("COOLIFY_DB_URL", "").strip()
        if db_url:
            try:
                from urllib.parse import urlparse

                u = urlparse(db_url)
                if u.scheme == "postgresql" and u.hostname and u.username and u.path:
                    return CoolifyConfig(
                        host=u.hostname,
                        port=int(u.port or 5432),
                        user=u.username,
                        password=u.password or "",
                        database=(u.path or "/").lstrip("/") or "coolify",
                    )
            except Exception:
                pass

        config_path = "/data/coolify_db_config.json"
        try:
            import json
            from pathlib import Path

            if Path(config_path).exists():
                data = json.loads(Path(config_path).read_text())
                return CoolifyConfig(
                    host=data.get("host", ""),
                    port=data.get("port", 5432),
                    user=data.get("user", ""),
                    password=data.get("password", ""),
                    database=data.get("database", "coolify"),
                    container_name=data.get("container_name", ""),
                )
        except Exception:
            pass
        return None

    def save_config_to_file(self, config: CoolifyConfig) -> None:
        config_path = "/data/coolify_db_config.json"
        try:
            import json
            from pathlib import Path

            Path(config_path).parent.mkdir(parents=True, exist_ok=True)
            Path(config_path).write_text(
                json.dumps(
                    {
                        "host": config.host,
                        "port": config.port,
                        "user": config.user,
                        "password": config.password,
                        "database": config.database,
                        "container_name": config.container_name,
                    }
                )
            )
        except Exception:
            pass

    def _docker_client(self):
        docker_socket = os.getenv("DOCKER_SOCKET", "/var/run/docker.sock")
        return docker.DockerClient(base_url=f"unix://{docker_socket}")

    def _resolve_db_container(self) -> Optional["docker.models.containers.Container"]:
        if not self._config or not self._config.container_name:
            return None
        try:
            cli = self._docker_client()
            return cli.containers.get(self._config.container_name)
        except Exception:
            return None

    def _psql_rows(self, sql: str) -> List[str]:
        """Execute SQL via `docker exec psql` inside the Coolify DB container."""
        if not self._config:
            return []
        container = self._resolve_db_container()
        if not container:
            return []

        cmd = [
            "psql",
            "-U",
            self._config.user,
            "-d",
            self._config.database,
            "-t",
            "-A",
            "-F",
            "|",
            "-c",
            sql,
        ]
        try:
            res = container.exec_run(cmd, environment={"PGPASSWORD": self._config.password})
        except Exception:
            return []
        if getattr(res, "exit_code", 1) != 0:
            return []

        out = getattr(res, "output", b"")
        try:
            text = out.decode("utf-8", errors="replace")
        except Exception:
            return []
        rows = [r for r in (text or "").splitlines() if r.strip()]
        return rows

    def detect_coolify_db(self) -> Optional[CoolifyConfig]:
        try:
            cli = self._docker_client()
        except DockerException:
            return None

        try:
            containers = cli.containers.list()
        except DockerException:
            return None

        db_container = None
        for c in containers:
            name = (c.name or "").lower()
            if "coolify-db" in name or "coolify-postgres" in name:
                db_container = c
                break
            if "postgres" in name and "coolify" in name:
                db_container = c
                break

        if not db_container:
            return None

        try:
            env_vars = db_container.attrs.get("Config", {}).get("Env", [])
            config = {}
            for env in env_vars:
                if "=" in env:
                    key, value = env.split("=", 1)
                    config[key] = value

            host = ""
            networks = db_container.attrs.get("NetworkSettings", {}).get("Networks", {})
            for net_name, net_info in networks.items():
                ip = net_info.get("IPAddress", "")
                if ip:
                    host = ip
                    break

            if not host:
                host = db_container.name.replace("/", "")

            return CoolifyConfig(
                host=host,
                port=int(config.get("POSTGRES_PORT", "5432")),
                user=config.get("POSTGRES_USER", "coolify"),
                password=config.get("POSTGRES_PASSWORD", ""),
                database=config.get("POSTGRES_DB", "coolify"),
                container_name=db_container.name,
            )
        except Exception:
            return None

    def ping_db(self, config: CoolifyConfig) -> bool:
        # Prefer exec'ing psql inside the DB container. This works even when
        # the app is not on the same Docker network as Coolify.
        prev = self._config
        try:
            self._config = config
            rows = self._psql_rows("SELECT 1")
            if rows and rows[0].strip() == "1":
                return True
        except Exception:
            pass
        finally:
            self._config = prev

        # Fallback: TCP connect (only works if reachable).
        try:
            import psycopg2

            conn = psycopg2.connect(
                host=config.host,
                port=config.port,
                user=config.user,
                password=config.password,
                database=config.database,
                connect_timeout=5,
            )
            conn.close()
            return True
        except Exception:
            return False

    def fetch_resources(
        self,
    ) -> tuple[List[CoolifyProject], Dict[str, CoolifyResource]]:
        if not self._config:
            return [], {}

        projects_map: Dict[str, Dict[str, List[str]]] = {}
        resource_map: Dict[str, CoolifyResource] = {}

        # Build project + environment list
        for row in self._psql_rows(
            """
            SELECT p.name, e.name
            FROM projects p
            JOIN environments e ON e.project_id = p.id
            """.strip()
        ):
            parts = row.split("|")
            if len(parts) != 2:
                continue
            proj_name, env_name = parts
            key = f"{proj_name}|{env_name}"
            projects_map.setdefault(
                key,
                {"project": proj_name, "environment": env_name, "services": []},
            )

        # Services
        for row in self._psql_rows(
            """
            SELECT s.name, p.name, e.name
            FROM services s
            JOIN environments e ON s.environment_id = e.id
            JOIN projects p ON e.project_id = p.id
            """.strip()
        ):
            parts = row.split("|")
            if len(parts) != 3:
                continue
            service_name, proj_name, env_name = parts
            key = f"{proj_name}|{env_name}"
            if key in projects_map:
                projects_map[key]["services"].append(service_name)
            resource_map[service_name.lower()] = CoolifyResource(
                project=proj_name, environment=env_name, service=service_name
            )

        # Applications
        for row in self._psql_rows(
            """
            SELECT a.name, p.name, e.name
            FROM applications a
            JOIN environments e ON a.environment_id = e.id
            JOIN projects p ON e.project_id = p.id
            """.strip()
        ):
            parts = row.split("|")
            if len(parts) != 3:
                continue
            app_name, proj_name, env_name = parts
            key = f"{proj_name}|{env_name}"
            if key in projects_map:
                projects_map[key]["services"].append(app_name)
            resource_map[app_name.lower()] = CoolifyResource(
                project=proj_name, environment=env_name, app=app_name
            )

        projects: List[CoolifyProject] = []
        for data in projects_map.values():
            projects.append(
                CoolifyProject(
                    name=data["project"],
                    environment=data["environment"],
                    services=data["services"],
                )
            )

        return projects, resource_map

    def initialize(self) -> tuple[bool, str]:
        config = self.load_config_from_file()

        if config:
            if self.ping_db(config):
                self._config = config
                self._load_resources()
                self._start_background_refresh()
                return True, "Connected to Coolify DB from saved config"
            else:
                return False, "Saved Coolify DB config is unreachable"

        detected = self.detect_coolify_db()
        if not detected:
            return False, "Could not detect Coolify DB container"

        if not self.ping_db(detected):
            return False, "Detected Coolify DB is unreachable"

        self._config = detected
        self.save_config_to_file(detected)
        self._load_resources()
        self._start_background_refresh()
        return True, "Connected to Coolify DB"

    def _load_resources(self) -> None:
        projects, resource_map = self.fetch_resources()
        with self._lock:
            self._projects = projects
            self._resource_map = resource_map

    def _start_background_refresh(self) -> None:
        if self._refresh_thread and self._refresh_thread.is_alive():
            return

        def refresh_loop():
            while not self._stop_event.is_set():
                time.sleep(self._refresh_interval)
                if not self._stop_event.is_set():
                    self._load_resources()

        self._refresh_thread = threading.Thread(target=refresh_loop, daemon=True)
        self._refresh_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._refresh_thread:
            self._refresh_thread.join(timeout=2)

    def get_projects(self) -> List[CoolifyProject]:
        with self._lock:
            return list(self._projects)

    def get_resource(
        self, compose_project: str, service_name: str = ""
    ) -> Optional[CoolifyResource]:
        if not compose_project:
            return None

        with self._lock:
            for uuid, resource in self._resource_map.items():
                if compose_project.lower() in resource.project.lower():
                    if service_name:
                        if (
                            resource.service
                            and service_name.lower() in resource.service.lower()
                        ):
                            return resource
                        if (
                            resource.app
                            and service_name.lower() in resource.app.lower()
                        ):
                            return resource
                    return resource
        return None


    def get_detailed_projects(self) -> List[Dict]:
        if not self._config:
            return []
        try:
            cli = self._docker_client()
            containers_list = cli.containers.list(filters={"status": "running"})
            containers_map = {c.name: {"id": c.id, "short_id": c.short_id} for c in containers_list}

            query = """
            SELECT
                p.id,
                p.name,
                e.name,
                COALESCE(a.name, s.name),
                COALESCE(a.uuid, s.uuid),
                CASE WHEN a.id IS NOT NULL THEN 'application' ELSE 'service' END
            FROM projects p
            JOIN environments e ON e.project_id = p.id
            LEFT JOIN applications a ON a.environment_id = e.id
            LEFT JOIN services s ON s.environment_id = e.id
            WHERE a.id IS NOT NULL OR s.id IS NOT NULL
            ORDER BY p.id, e.name;
            """.strip()

            rows = self._psql_rows(query)
            if not rows:
                return []

            projects_dict: Dict[str, Dict] = {}
            for row in rows:
                parts = row.split("|")
                if len(parts) != 6:
                    continue
                pid, p_name, env, res_name, uuid, res_type = parts

                pid_str = str(pid)
                if pid_str not in projects_dict:
                    projects_dict[pid_str] = {
                        "project_id": pid_str,
                        "project_name": p_name,
                        "stages": {},
                    }

                if env not in projects_dict[pid_str]["stages"]:
                    projects_dict[pid_str]["stages"][env] = {
                        "stage_name": env,
                        "services": [],
                    }

                container_id = "Not Found"
                container_name = "Not Found"
                for c_name, c_info in containers_map.items():
                    if uuid and uuid in c_name:
                        container_id = c_info["short_id"]
                        container_name = c_name
                        break

                projects_dict[pid_str]["stages"][env]["services"].append(
                    {
                        "name": res_name,
                        "type": res_type,
                        "uuid": uuid,
                        "container_id": container_id,
                        "container_name": container_name,
                    }
                )

            final_projects: List[Dict] = []
            for p in projects_dict.values():
                p["stages"] = list(p["stages"].values())
                final_projects.append(p)
            return final_projects
        except Exception:
            return []


coolify_db = CoolifyDBManager()
