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
        import os

        # Check for manual override via environment variable
        db_url = os.getenv("COOLIFY_DB_URL", "").strip()
        if db_url:
            try:
                import re

                match = re.match(r"postgresql://(\w+):(\w+)@(.+?):(\d+)/(\w+)", db_url)
                if match:
                    user, password, host, port, database = match.groups()
                    return CoolifyConfig(
                        host=host,
                        port=int(port),
                        user=user,
                        password=password,
                        database=database,
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
                    }
                )
            )
        except Exception:
            pass

    def detect_coolify_db(self) -> Optional[CoolifyConfig]:
        try:
            cli = docker.from_env()
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
            )
        except Exception:
            return None

    def ping_db(self, config: CoolifyConfig) -> bool:
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

        try:
            import psycopg2

            conn = psycopg2.connect(
                host=self._config.host,
                port=self._config.port,
                user=self._config.user,
                password=self._config.password,
                database=self._config.database,
                connect_timeout=5,
            )
            cur = conn.cursor()

            projects_map: Dict[str, Dict[str, List[str]]] = {}
            resource_map: Dict[str, CoolifyResource] = {}

            try:
                cur.execute("""
                    SELECT p.name as project, e.name as environment
                    FROM projects p
                    JOIN environments e ON e.project_id = p.id
                """)
                for row in cur.fetchall():
                    proj_name, env_name = row
                    key = f"{proj_name}|{env_name}"
                    if key not in projects_map:
                        projects_map[key] = {
                            "project": proj_name,
                            "environment": env_name,
                            "services": [],
                        }
            except Exception:
                pass

            try:
                cur.execute("""
                    SELECT s.name, p.name, e.name
                    FROM services s
                    JOIN environments e ON s.environment_id = e.id
                    JOIN projects p ON e.project_id = p.id
                """)
                for row in cur.fetchall():
                    service_name, proj_name, env_name = row
                    key = f"{proj_name}|{env_name}"
                    if key in projects_map:
                        projects_map[key]["services"].append(service_name)
                    resource_map[service_name.lower()] = CoolifyResource(
                        project=proj_name,
                        environment=env_name,
                        service=service_name,
                    )
            except Exception:
                pass

            try:
                cur.execute("""
                    SELECT a.name, p.name, e.name
                    FROM applications a
                    JOIN environments e ON a.environment_id = e.id
                    JOIN projects p ON e.project_id = p.id
                """)
                for row in cur.fetchall():
                    app_name, proj_name, env_name = row
                    key = f"{proj_name}|{env_name}"
                    if key in projects_map:
                        projects_map[key]["services"].append(app_name)
                    resource_map[app_name.lower()] = CoolifyResource(
                        project=proj_name,
                        environment=env_name,
                        app=app_name,
                    )
            except Exception:
                pass

            cur.close()
            conn.close()

            projects = []
            for key, data in projects_map.items():
                projects.append(
                    CoolifyProject(
                        name=data["project"],
                        environment=data["environment"],
                        services=data["services"],
                    )
                )

            return projects, resource_map

        except Exception:
            return [], {}

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
        try:
            cli = docker.from_env()
            containers_list = cli.containers.list()
            
            # Find coolify-db
            db_container = None
            for c in containers_list:
                name = (c.name or "").lower()
                if "coolify-db" in name or "coolify-postgres" in name:
                    db_container = c
                    break
                if "postgres" in name and "coolify" in name:
                    db_container = c
                    break
            
            if not db_container:
                return []

            # Query for projects and their resources (apps/services)
            query = """
            SELECT 
                p.id as project_id,
                p.name as project_name,
                e.name as env_name,
                COALESCE(a.name, s.name) as name,
                COALESCE(a.uuid, s.uuid) as uuid,
                CASE WHEN a.id IS NOT NULL THEN 'application' ELSE 'service' END as type
            FROM projects p
            JOIN environments e ON e.project_id = p.id
            LEFT JOIN applications a ON a.environment_id = e.id
            LEFT JOIN services s ON s.environment_id = e.id
            WHERE a.id IS NOT NULL OR s.id IS NOT NULL;
            """
            
            # Execute via docker exec
            cmd = f'psql -U coolify -d coolify -c "{query}" -t -A'
            result = db_container.exec_run(cmd)
            if result.exit_code != 0:
                return []
                
            rows = result.output.decode().strip().split('\n')

            # Map UUID to container ID
            containers_map = {
                c.name: {"id": c.id, "short_id": c.short_id}
                for c in containers_list
            }

            projects_dict = {}
            for row in rows:
                if not row or '|' not in row:
                    continue
                pid, p_name, env, res_name, uuid, res_type = row.split('|')

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
                        "services": []
                    }

                # Match container
                container_id = "Not Found"
                container_name = "Not Found"
                for c_name, c_info in containers_map.items():
                    if uuid in c_name:
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

            # Transform stages dict to list
            final_projects = []
            for p in projects_dict.values():
                p["stages"] = list(p["stages"].values())
                final_projects.append(p)

            return final_projects
        except Exception:
            return []


coolify_db = CoolifyDBManager()
