from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import docker
from docker.errors import DockerException


@dataclass
class CoolifyResource:
    container_id: str
    project: str
    environment: str
    app: str
    service: str = ""


@dataclass
class CoolifyConfig:
    host: str
    port: int
    user: str
    password: str
    database: str

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "database": self.database,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CoolifyConfig":
        return cls(
            host=data.get("host", ""),
            port=data.get("port", 5432),
            user=data.get("user", ""),
            password=data.get("password", ""),
            database=data.get("database", "coolify"),
        )


class CoolifyDBManager:
    def __init__(self):
        self._resource_map: Dict[str, CoolifyResource] = {}
        self._config: Optional[CoolifyConfig] = None
        self._lock = threading.RLock()
        self._refresh_interval = 300
        self._stop_event = threading.Event()
        self._refresh_thread: Optional[threading.Thread] = None

    @property
    def config(self) -> Optional[CoolifyConfig]:
        return self._config

    @property
    def is_configured(self) -> bool:
        return self._config is not None

    def load_config_from_db(self) -> Optional[CoolifyConfig]:
        config_path = "/data/coolify_db_config.json"
        try:
            import json
            from pathlib import Path

            if Path(config_path).exists():
                data = json.loads(Path(config_path).read_text())
                return CoolifyConfig.from_dict(data)
        except Exception:
            pass
        return None

    def save_config_to_db(self, config: CoolifyConfig) -> None:
        config_path = "/data/coolify_db_config.json"
        try:
            import json
            from pathlib import Path

            Path(config_path).parent.mkdir(parents=True, exist_ok=True)
            Path(config_path).write_text(json.dumps(config.to_dict()))
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
            name = c.name.lower()
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

            host = db_container.attrs.get("NetworkSettings", {}).get("IPAddress", "")
            if not host:
                networks = db_container.attrs.get("NetworkSettings", {}).get(
                    "Networks", {}
                )
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

    def fetch_resources(self) -> Dict[str, CoolifyResource]:
        if not self._config:
            return {}

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

            resources: Dict[str, CoolifyResource] = {}

            try:
                cur.execute("""
                    SELECT a.uuid, a.name, p.name, e.name
                    FROM applications a
                    JOIN environments e ON a.environment_id = e.id
                    JOIN projects p ON e.project_id = p.id
                """)
                for row in cur.fetchall():
                    uuid, app_name, project_name, env_name = row
                    resources[uuid] = CoolifyResource(
                        container_id=uuid,
                        project=project_name,
                        environment=env_name,
                        app=app_name,
                        service="",
                    )
            except Exception:
                pass

            try:
                cur.execute("""
                    SELECT s.uuid, s.name, p.name, e.name
                    FROM services s
                    JOIN environments e ON s.environment_id = e.id
                    JOIN projects p ON e.project_id = p.id
                """)
                for row in cur.fetchall():
                    uuid, service_name, project_name, env_name = row
                    resources[uuid] = CoolifyResource(
                        container_id=uuid,
                        project=project_name,
                        environment=env_name,
                        app="",
                        service=service_name,
                    )
            except Exception:
                pass

            cur.close()
            conn.close()
            return resources

        except Exception:
            return {}

    def initialize(self) -> tuple[bool, str]:
        config = self.load_config_from_db()

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
        self.save_config_to_db(detected)
        self._load_resources()
        self._start_background_refresh()
        return True, "Connected to Coolify DB"

    def _load_resources(self) -> None:
        resources = self.fetch_resources()
        with self._lock:
            self._resource_map = resources

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

    def get_resource(self, container_name: str) -> Optional[CoolifyResource]:
        clean_name = container_name.lstrip("/")

        with self._lock:
            for uuid, resource in self._resource_map.items():
                if uuid in clean_name or clean_name in uuid:
                    return resource

        for uuid, resource in self._resource_map.items():
            if resource.service and resource.service in clean_name:
                return resource

        return None

    def get_all_resources(self) -> List[CoolifyResource]:
        with self._lock:
            return list(self._resource_map.values())


coolify_db = CoolifyDBManager()
