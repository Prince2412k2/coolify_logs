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


    def get_project_for_resource(self, resource_uuid: str) -> Optional[str]:
        """Look up which Coolify project_id a resource (app or service) belongs to.

        Returns the project_id as a string, or None if no match.
        Reads through the live tree — cheap enough since get_detailed_projects
        is already pulled by every UI render.
        """
        if not resource_uuid:
            return None
        for p in self.get_detailed_projects():
            for stage in p.get("stages", []):
                for s in stage.get("services", []):
                    if s.get("uuid") == resource_uuid:
                        return str(p.get("project_id"))
        return None

    def get_project_for_container(self, container_name: str) -> Optional[str]:
        """Look up which project_id a Docker container belongs to.

        Matches via container_name from get_detailed_projects().
        Returns None if the container isn't associated with any known project.
        """
        if not container_name:
            return None
        for p in self.get_detailed_projects():
            for stage in p.get("stages", []):
                for s in stage.get("services", []):
                    if s.get("container_name") == container_name:
                        return str(p.get("project_id"))
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

                container_id = ""
                container_name = ""
                reachable = False
                for c_name, c_info in containers_map.items():
                    if uuid and uuid in c_name:
                        container_id = c_info["short_id"]
                        container_name = c_name
                        reachable = True
                        break

                projects_dict[pid_str]["stages"][env]["services"].append(
                    {
                        "name": res_name,
                        "type": res_type,
                        "uuid": uuid,
                        "container_id": container_id,
                        "container_name": container_name,
                        "reachable": reachable,
                    }
                )

            final_projects: List[Dict] = []
            for p in projects_dict.values():
                p["stages"] = list(p["stages"].values())
                final_projects.append(p)
            return final_projects
        except Exception:
            return []


    def get_deployments(self, application_uuid: str, limit: int = 25) -> List[Dict]:
        """Return the most-recent deployments for an application UUID.

        Reads Coolify's application_deployment_queues table. Services don't
        produce deployments in this table — callers should pass only
        application UUIDs and expect an empty list otherwise.
        """
        if not self._config or not application_uuid:
            return []
        # UUID-only allowlist to keep SQL injection-proof without a real param binder.
        safe = "".join(c for c in application_uuid if c.isalnum() or c in "-_")
        if safe != application_uuid:
            return []
        try:
            lim = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            lim = 25

        # application_id is the integer id of applications stored as varchar.
        # Resolve UUID → id::text once and feed it in.
        sql = f"""
        SELECT
            d.deployment_uuid,
            d.status,
            COALESCE(d.commit, ''),
            COALESCE(REPLACE(d.commit_message, '|', ' '), ''),
            COALESCE(d.pull_request_id::text, ''),
            COALESCE(d.force_rebuild::text, 'f'),
            COALESCE(d.restart_only::text, 'f'),
            COALESCE(d.rollback::text, 'f'),
            COALESCE(d.is_webhook::text, 'f'),
            COALESCE(d.is_api::text, 'f'),
            EXTRACT(EPOCH FROM d.created_at)::bigint,
            EXTRACT(EPOCH FROM COALESCE(d.finished_at, d.updated_at))::bigint
        FROM application_deployment_queues d
        JOIN applications a ON a.id::text = d.application_id
        WHERE a.uuid = '{safe}'
        ORDER BY d.id DESC
        LIMIT {lim};
        """.strip()

        out: List[Dict] = []
        for row in self._psql_rows(sql):
            parts = row.split("|")
            if len(parts) != 12:
                continue
            (dep_uuid, status, commit, commit_msg, pr_id, force, restart,
             rollback, is_webhook, is_api, created, finished) = parts
            try:
                created_ts = int(created) if created else 0
            except ValueError:
                created_ts = 0
            try:
                finished_ts = int(finished) if finished else 0
            except ValueError:
                finished_ts = 0
            duration = 0
            terminal = status in ("finished", "failed", "cancelled-by-user")
            if terminal and finished_ts > created_ts:
                duration = finished_ts - created_ts
            short = commit[:7] if commit else ""
            # First line of commit message — UI uses short form.
            short_msg = commit_msg.split("\n", 1)[0].strip()
            # Derive trigger label from the flag columns.
            if rollback == "t":
                trigger = "rollback"
            elif is_webhook == "t":
                trigger = "webhook"
            elif is_api == "t":
                trigger = "api"
            elif restart == "t":
                trigger = "restart"
            elif force == "t":
                trigger = "manual (force)"
            else:
                trigger = "manual"
            out.append({
                "uuid": dep_uuid,
                "status": status,
                "commit": short,
                "full_commit": commit,
                "commit_message": short_msg,
                "pr_id": pr_id if pr_id and pr_id != "0" else "",
                "trigger": trigger,
                "force_rebuild": force == "t",
                "created_at": created_ts,
                "finished_at": finished_ts,
                "duration_seconds": duration,
            })
        return out


    def get_build_log(self, application_uuid: str) -> Dict:
        """Return the build log for the latest deployment of an application.

        Coolify stores build output in application_deployment_queues.logs as
        either a JSON array of `{output, type, order, timestamp}` entries, or
        plain text. We return the parsed text plus metadata about the deploy.
        """
        if not self._config or not application_uuid:
            return {}
        safe = "".join(c for c in application_uuid if c.isalnum() or c in "-_")
        if safe != application_uuid:
            return {}

        # Newest deployment for this application. We use \\gset-free output so
        # the logs column survives intact (it can contain newlines).
        sql = f"""
        SELECT
            d.deployment_uuid,
            d.status,
            COALESCE(d.commit, ''),
            COALESCE(REPLACE(d.commit_message, '|', ' '), ''),
            EXTRACT(EPOCH FROM d.created_at)::bigint,
            EXTRACT(EPOCH FROM COALESCE(d.finished_at, d.updated_at))::bigint,
            COALESCE(d.logs, '')
        FROM application_deployment_queues d
        JOIN applications a ON a.id::text = d.application_id
        WHERE a.uuid = '{safe}'
        ORDER BY d.id DESC
        LIMIT 1;
        """.strip()

        rows = self._psql_rows(sql)
        if not rows:
            return {}
        # Logs may contain `|` itself, so use maxsplit on the first 6 cols and
        # keep the rest as the logs blob.
        parts = rows[0].split("|", 6)
        if len(parts) != 7:
            return {}
        dep_uuid, status, commit, commit_msg, created, finished, raw_logs = parts
        # Multi-row outputs join logs with newlines; rebuild.
        if len(rows) > 1:
            raw_logs = "|".join([raw_logs] + rows[1:])

        try:
            created_ts = int(created) if created else 0
        except ValueError:
            created_ts = 0
        try:
            finished_ts = int(finished) if finished else 0
        except ValueError:
            finished_ts = 0

        import json as _json
        text_lines: List[str] = []

        def _push(s: str, prefix: str = "") -> None:
            # Each emitted line must be single-row — split on embedded newlines
            # so multi-line Dockerfile/script content doesn't break TUI layout.
            s = s.replace("\r", "")
            for sub in s.split("\n"):
                sub = sub.rstrip()
                if prefix and sub:
                    text_lines.append(prefix + sub)
                else:
                    text_lines.append(sub)

        try:
            entries = _json.loads(raw_logs) if raw_logs else []
            if isinstance(entries, list):
                for e in entries:
                    if isinstance(e, dict):
                        cmd = e.get("command")
                        if cmd:
                            _push(str(cmd), prefix="$ ")
                        out = e.get("output", "")
                        if not out:
                            continue
                        # Mark stderr lines so callers can render them
                        # distinctly. stdout entries get no prefix.
                        if str(e.get("type", "")) == "stderr":
                            _push(str(out), prefix="[stderr] ")
                        else:
                            _push(str(out))
                    elif isinstance(e, str):
                        _push(e)
            else:
                _push(raw_logs)
        except (ValueError, TypeError):
            _push(raw_logs)

        return {
            "deployment_uuid": dep_uuid,
            "status": status,
            "commit": commit[:7] if commit else "",
            "commit_message": commit_msg.split("\n", 1)[0].strip(),
            "created_at": created_ts,
            "finished_at": finished_ts,
            "lines": text_lines,
        }

    def get_application_config(self, application_uuid: str) -> Dict:
        """Read non-sensitive config columns from applications for one UUID."""
        if not self._config or not application_uuid:
            return {}
        safe = "".join(c for c in application_uuid if c.isalnum() or c in "-_")
        if safe != application_uuid:
            return {}
        sql = f"""
        SELECT
            COALESCE(name, ''),
            COALESCE(fqdn, ''),
            COALESCE(git_repository, ''),
            COALESCE(git_branch, ''),
            COALESCE(git_commit_sha, ''),
            COALESCE(build_pack, ''),
            COALESCE(base_directory, ''),
            COALESCE(install_command, ''),
            COALESCE(build_command, ''),
            COALESCE(start_command, ''),
            COALESCE(ports_exposes, ''),
            COALESCE(ports_mappings, ''),
            COALESCE(dockerfile, ''),
            COALESCE(docker_registry_image_name, ''),
            COALESCE(docker_registry_image_tag, ''),
            COALESCE(health_check_enabled::text, 'f'),
            COALESCE(health_check_path, ''),
            COALESCE(status, ''),
            EXTRACT(EPOCH FROM COALESCE(updated_at, created_at))::bigint
        FROM applications
        WHERE uuid = '{safe}'
        LIMIT 1;
        """.strip()
        rows = self._psql_rows(sql)
        if not rows:
            return {}
        parts = rows[0].split("|")
        if len(parts) != 19:
            return {}
        keys = [
            "name", "fqdn", "git_repository", "git_branch", "git_commit",
            "build_pack", "base_directory", "install_command", "build_command",
            "start_command", "ports_exposed", "ports_mappings", "dockerfile",
            "image_name", "image_tag", "health_check_enabled", "health_check_path",
            "status", "updated_at",
        ]
        out: Dict = {}
        for k, v in zip(keys, parts):
            if k == "health_check_enabled":
                out[k] = (v == "t")
            elif k == "updated_at":
                try:
                    out[k] = int(v) if v else 0
                except ValueError:
                    out[k] = 0
            else:
                out[k] = v
        out["kind"] = "application"
        return out

    def get_service_config(self, service_uuid: str) -> Dict:
        if not self._config or not service_uuid:
            return {}
        safe = "".join(c for c in service_uuid if c.isalnum() or c in "-_")
        if safe != service_uuid:
            return {}
        sql = f"""
        SELECT
            COALESCE(name, ''),
            COALESCE(description, ''),
            COALESCE(docker_compose_raw, ''),
            COALESCE(status, ''),
            EXTRACT(EPOCH FROM COALESCE(updated_at, created_at))::bigint
        FROM services
        WHERE uuid = '{safe}'
        LIMIT 1;
        """.strip()
        rows = self._psql_rows(sql)
        if not rows:
            return {}
        # Compose can contain |, so split with maxsplit and absorb the rest.
        parts = rows[0].split("|", 4)
        if len(parts) != 5:
            return {}
        if len(rows) > 1:
            parts[2] = "|".join([parts[2]] + rows[1:])
        try:
            updated_at = int(parts[4]) if parts[4] else 0
        except ValueError:
            updated_at = 0
        return {
            "name": parts[0],
            "description": parts[1],
            "docker_compose_raw": parts[2],
            "status": parts[3],
            "updated_at": updated_at,
            "kind": "service",
        }

    def get_environment_variables(self, resource_uuid: str, resource_type: str) -> List[Dict]:
        """Return env-var metadata (keys only — values are not exposed)."""
        if not self._config or not resource_uuid:
            return []
        safe = "".join(c for c in resource_uuid if c.isalnum() or c in "-_")
        if safe != resource_uuid:
            return []

        # Coolify's environment_variables uses (resourceable_type, resourceable_id)
        # with resourceable_id being the integer PK of the parent. So we need a
        # join through applications/services on uuid.
        # NB: Coolify stores Laravel polymorphic names with single backslashes —
        # the SQL literal needs ONE backslash per separator, which in a Python
        # string is the two-char escape "\\".
        if resource_type == "application":
            join_table = "applications"
            laravel_class = "App\\Models\\Application"
        elif resource_type == "service":
            join_table = "services"
            laravel_class = "App\\Models\\Service"
        else:
            return []

        sql = f"""
        SELECT
            ev.key,
            COALESCE(ev.is_preview::text, 'f'),
            COALESCE(ev.is_buildtime::text, 'f'),
            COALESCE(ev.is_runtime::text, 't'),
            COALESCE(ev.is_literal::text, 'f'),
            COALESCE(ev.is_shared::text, 'f'),
            COALESCE(LENGTH(ev.value), 0)
        FROM environment_variables ev
        JOIN {join_table} r ON r.id = ev.resourceable_id
        WHERE r.uuid = '{safe}'
          AND ev.resourceable_type = '{laravel_class}'
        ORDER BY ev.is_preview, ev.key;
        """.strip()
        out: List[Dict] = []
        for row in self._psql_rows(sql):
            parts = row.split("|")
            if len(parts) != 7:
                continue
            key, is_preview, is_build, is_runtime, is_literal, is_shared, value_len = parts
            try:
                vlen = int(value_len) if value_len else 0
            except ValueError:
                vlen = 0
            out.append({
                "key": key,
                "is_preview": is_preview == "t",
                "is_build_time": is_build == "t",
                "is_runtime": is_runtime == "t",
                "is_literal": is_literal == "t",
                "is_shared": is_shared == "t",
                "value_length": vlen,
            })
        return out


coolify_db = CoolifyDBManager()
