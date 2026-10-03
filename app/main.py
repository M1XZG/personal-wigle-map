from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import sqlite3
import threading
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import queries, wigle_sync

try:
    from . import ingestion
except ImportError:
    try:
        from . import importer as ingestion
    except ImportError:
        ingestion = None


ALLOWED_SUFFIXES = (".sqlite", ".db", ".csv", ".csv.gz", ".kml", ".gpx")
DEVICE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
EXECUTABLE_MAGIC = (
    b"\x7fELF",
    b"MZ",
    b"#!",
    b"\xca\xfe\xba\xbe",
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
)
logger = logging.getLogger(__name__)


def _boolean_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false")


def _positive_int_env(name: str, default: int, allow_zero: bool = False) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return value


def _text_env(name: str, default: str, max_length: int = 120) -> str:
    value = os.getenv(name, default).strip()
    if not value:
        return default
    if len(value) > max_length:
        raise RuntimeError(f"{name} must be at most {max_length} characters")
    return value


def _optional_http_url_env(name: str, default: str = "") -> str:
    value = os.getenv(name, default).strip()
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(f"{name} must be an absolute HTTP or HTTPS URL")
    if len(value) > 2048:
        raise RuntimeError(f"{name} must be at most 2048 characters")
    return value


def _safe_filename(filename: str | None) -> str:
    if not filename or filename in {".", ".."}:
        raise HTTPException(400, "Each upload must have a filename")
    if "/" in filename or "\\" in filename or Path(filename).name != filename:
        raise HTTPException(400, "Filename paths are not allowed")
    cleaned = SAFE_FILENAME_RE.sub("_", filename).strip("._")
    if not cleaned or cleaned in {".", ".."}:
        raise HTTPException(400, "Filename is invalid")
    lower = cleaned.lower()
    if not any(lower.endswith(suffix) for suffix in ALLOWED_SUFFIXES):
        raise HTTPException(415, f"Unsupported file type: {filename}")
    return cleaned


def _split_values(values: list[str] | None) -> list[str]:
    result: list[str] = []
    for value in values or []:
        result.extend(part.strip() for part in value.split(",") if part.strip())
    if len(result) > 50 or any(len(value) > 64 for value in result):
        raise HTTPException(400, "Too many or overly long filter values")
    return list(dict.fromkeys(result))


def _call_ingestion(function_name: str, **values: Any) -> Any:
    if ingestion is None:
        return None
    function = getattr(ingestion, function_name, None)
    if function is None:
        return None
    signature = inspect.signature(function)
    aliases = {
        "database": "db_path",
        "database_path": "db_path",
        "root": "import_root",
        "source_root": "import_root",
        "path": "file_path",
        "source_path": "file_path",
        "device_slug": "device",
        "device_hint": "device",
    }
    kwargs = {}
    for name, parameter in signature.parameters.items():
        source_name = name if name in values else aliases.get(name)
        if source_name in values:
            kwargs[name] = values[source_name]
        elif parameter.default is inspect.Parameter.empty:
            raise RuntimeError(f"Unsupported ingestion parameter: {name}")
    return function(**kwargs)


def _safe_runtime_status(value: Any) -> Any:
    if not isinstance(value, dict):
        return value if isinstance(value, (bool, int, float, str, type(None))) else None
    safe = {}
    for key in (
        "running",
        "queued",
        "operation",
        "status",
        "progress",
        "total",
        "imported",
        "skipped",
        "failed",
        "started_at",
        "finished_at",
    ):
        if key in value and isinstance(
            value[key], (bool, int, float, str, type(None))
        ):
            safe[key] = value[key]
    if value.get("current_file"):
        safe["current_file"] = Path(str(value["current_file"]).replace("\\", "/")).name
    if value.get("error"):
        safe["error"] = "Import failed"
    return safe


def _safe_sync_status(value: dict[str, Any]) -> dict[str, Any]:
    safe = {
        key: value[key]
        for key in (
            "enabled",
            "running",
            "last_started_at",
            "last_finished_at",
            "transactions",
            "downloaded",
            "existing",
            "failed",
        )
        if key in value
        and isinstance(value[key], (bool, int, float, str, type(None)))
    }
    if value.get("error"):
        safe["error"] = "WiGLE sync failed"
    return safe


def create_app() -> FastAPI:
    data_dir = Path(os.getenv("DATA_DIR", "/data")).resolve()
    db_path = Path(os.getenv("DATABASE_PATH", data_dir / "wigle-map.sqlite")).resolve()
    import_root = Path(os.getenv("IMPORT_ROOT", "/imports")).resolve()
    web_dir = Path(os.getenv("WEB_DIR", "/app/web")).resolve()
    max_upload_bytes = _positive_int_env("MAX_UPLOAD_BYTES", 512 * 1024 * 1024)
    rescan_seconds = _positive_int_env("RESCAN_SECONDS", 0, allow_zero=True)
    app_title = _text_env("APP_TITLE", "Personal WiGLE Map")
    app_eyebrow = _text_env("APP_EYEBROW", "Wireless survey archive")
    app_version = _text_env("APP_VERSION", "development", 64)
    expose_network_identifiers = _boolean_env("EXPOSE_NETWORK_IDENTIFIERS")
    wigle_badge_url = _optional_http_url_env("WIGLE_BADGE_URL")
    wigle_profile_url = _optional_http_url_env(
        "WIGLE_PROFILE_URL", "https://wigle.net"
    )
    wigle_api_name = os.getenv("WIGLE_API_NAME", "").strip()
    wigle_api_token = os.getenv("WIGLE_API_TOKEN", "").strip()
    wigle_sync_seconds = _positive_int_env(
        "WIGLE_SYNC_SECONDS", 86400, allow_zero=True
    )
    if 0 < wigle_sync_seconds < 300:
        raise RuntimeError("WIGLE_SYNC_SECONDS must be 0 or at least 300")
    wigle_sync_on_start = _boolean_env("WIGLE_SYNC_ON_START", True)
    if bool(wigle_api_name) != bool(wigle_api_token):
        raise RuntimeError(
            "WIGLE_API_NAME and WIGLE_API_TOKEN must both be set or both be empty"
        )
    wigle_sync_enabled = bool(
        wigle_api_name and wigle_api_token and wigle_sync_seconds
    )
    job_lock = threading.Lock()
    job_tasks: set[asyncio.Task[Any]] = set()
    job_state: dict[str, Any] = {
        "running": False,
        "queued": False,
        "operation": None,
        "started_at": None,
        "finished_at": None,
        "imported": 0,
        "skipped": 0,
        "failed": 0,
        "error": None,
    }
    sync_state: dict[str, Any] = {
        "enabled": wigle_sync_enabled,
        "running": False,
        "last_started_at": None,
        "last_finished_at": None,
        "transactions": 0,
        "downloaded": 0,
        "existing": 0,
        "failed": 0,
        "error": None,
    }

    async def run_import(
        operation: str,
        file_paths: list[Path] | None = None,
        device: str | None = None,
    ) -> None:
        if not job_lock.acquire(blocking=False):
            return
        job_state.update(
            running=True,
            queued=False,
            operation=operation,
            started_at=datetime.now(timezone.utc).isoformat(),
            finished_at=None,
            imported=0,
            skipped=0,
            failed=0,
            error=None,
        )
        logger.info("Starting %s import", operation)
        try:
            if file_paths:
                results = []
                for file_path in file_paths:
                    results.append(
                        await asyncio.to_thread(
                            _call_ingestion,
                            "import_file",
                            db_path=db_path,
                            file_path=file_path,
                            device=device,
                            rebuild_routes_after=False,
                        )
                    )
                await asyncio.to_thread(
                    _call_ingestion, "rebuild_routes", db_path=db_path
                )
                job_state["imported"] = sum(
                    isinstance(result, dict) and result.get("status") == "complete"
                    for result in results
                )
                job_state["skipped"] = sum(
                    isinstance(result, dict) and result.get("status") == "skipped"
                    for result in results
                )
                job_state["failed"] = sum(
                    isinstance(result, dict) and result.get("status") == "failed"
                    for result in results
                )
            else:
                result = await asyncio.to_thread(
                    _call_ingestion,
                    "import_tree",
                    db_path=db_path,
                    import_root=import_root,
                )
                if isinstance(result, dict):
                    job_state["imported"] = int(result.get("complete", 0))
                    job_state["skipped"] = int(result.get("skipped", 0))
                    job_state["failed"] = int(result.get("failed", 0))
            if job_state["failed"]:
                job_state["error"] = f"{job_state['failed']} file(s) failed"
                logger.error("%s import completed with %s failed file(s)", operation, job_state["failed"])
            else:
                logger.info(
                    "%s import finished: %s imported, %s skipped",
                    operation,
                    job_state["imported"],
                    job_state["skipped"],
                )
        except Exception as exc:
            job_state["error"] = f"Import failed ({type(exc).__name__})"
            logger.exception("%s import failed", operation)
        finally:
            job_state.update(
                running=False,
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
            job_lock.release()

    def schedule_import(
        operation: str,
        file_paths: list[Path] | None = None,
        device: str | None = None,
    ) -> None:
        task = asyncio.create_task(run_import(operation, file_paths, device))
        job_tasks.add(task)
        task.add_done_callback(job_tasks.discard)

    async def periodic_rescan() -> None:
        while True:
            await asyncio.sleep(rescan_seconds)
            await run_import("periodic-rescan")

    async def run_wigle_sync() -> None:
        while job_state["running"] or job_state["queued"] or job_lock.locked():
            await asyncio.sleep(1)
        sync_state.update(
            running=True,
            last_started_at=datetime.now(timezone.utc).isoformat(),
            last_finished_at=None,
            transactions=0,
            downloaded=0,
            existing=0,
            failed=0,
            error=None,
        )
        logger.info("Starting automatic WiGLE account sync")
        try:
            result = await asyncio.to_thread(
                wigle_sync.sync_once,
                wigle_api_name,
                wigle_api_token,
                import_root,
                data_dir / "wigle-sync",
            )
            sync_state.update(result)
            logger.info(
                "WiGLE sync finished: %s downloaded, %s existing, %s failed",
                result["downloaded"],
                result["existing"],
                result["failed"],
            )
            if result["transactions"]:
                while (
                    job_state["running"]
                    or job_state["queued"]
                    or job_lock.locked()
                ):
                    await asyncio.sleep(1)
                await run_import("wigle-sync")
        except Exception as error:
            sync_state["error"] = f"WiGLE sync failed ({type(error).__name__})"
            logger.exception("Automatic WiGLE account sync failed")
        finally:
            sync_state.update(
                running=False,
                last_finished_at=datetime.now(timezone.utc).isoformat(),
            )

    async def periodic_wigle_sync() -> None:
        if not wigle_sync_on_start:
            await asyncio.sleep(wigle_sync_seconds)
        while True:
            await run_wigle_sync()
            await asyncio.sleep(wigle_sync_seconds)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        data_dir.mkdir(parents=True, exist_ok=True)
        import_root.mkdir(parents=True, exist_ok=True)
        if ingestion is not None:
            await asyncio.to_thread(
                _call_ingestion, "initialize_database", db_path=db_path
            )
            job_state["queued"] = True
            schedule_import("startup")
        periodic_task = (
            asyncio.create_task(periodic_rescan()) if rescan_seconds else None
        )
        wigle_sync_task = (
            asyncio.create_task(periodic_wigle_sync())
            if wigle_sync_enabled
            else None
        )
        yield
        for task in (periodic_task, wigle_sync_task):
            if not task:
                continue
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    application = FastAPI(title=app_title, lifespan=lifespan)
    application.state.db_path = db_path
    application.state.import_root = import_root
    application.state.job_state = job_state
    application.state.sync_state = sync_state

    def ensure_database() -> None:
        if not db_path.is_file():
            raise HTTPException(503, "Map database is not initialized")

    @application.get("/health")
    def health() -> dict[str, Any]:
        database_ok = False
        if db_path.is_file():
            try:
                with sqlite3.connect(db_path, timeout=2) as db:
                    database_ok = db.execute("SELECT 1").fetchone()[0] == 1
            except sqlite3.Error:
                database_ok = False
        return {
            "status": "ok" if database_ok else "degraded",
            "database": database_ok,
            "import_job": _safe_runtime_status(job_state),
            "wigle_sync": _safe_sync_status(sync_state),
        }

    @application.get("/api/config")
    def get_config() -> dict[str, Any]:
        return {
            "title": app_title,
            "eyebrow": app_eyebrow,
            "version": app_version,
            "badge": {
                "image_url": wigle_badge_url,
                "link_url": wigle_profile_url,
            },
            "wigle_sync": {
                "enabled": wigle_sync_enabled,
                "interval_seconds": wigle_sync_seconds,
                "on_start": wigle_sync_on_start,
            },
        }

    @application.get("/api/summary")
    def get_summary() -> dict[str, Any]:
        ensure_database()
        try:
            return queries.summary(db_path, _safe_runtime_status(job_state))
        except sqlite3.Error as exc:
            raise HTTPException(503, f"Database schema is unavailable: {exc}") from exc

    @application.get("/api/networks")
    def get_networks(
        bbox: str = Query(..., description="west,south,east,north"),
        zoom: int = Query(ge=0, le=22),
        types: list[str] | None = Query(None),
        devices: list[str] | None = Query(None),
        from_date: str | None = Query(None, alias="from"),
        to_date: str | None = Query(None, alias="to"),
    ) -> dict[str, Any]:
        ensure_database()
        try:
            bounds = queries.parse_bbox(bbox)
            start = queries.parse_date(from_date, "from")
            end = queries.parse_date(to_date, "to")
            if start and end and start > end:
                raise ValueError("from must not be later than to")
            return queries.networks(
                db_path,
                bounds,
                zoom,
                _split_values(types),
                _split_values(devices),
                start,
                end,
                expose_network_identifiers,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @application.get("/api/routes")
    def get_routes(
        bbox: str = Query(..., description="west,south,east,north"),
        devices: list[str] | None = Query(None),
        from_date: str | None = Query(None, alias="from"),
        to_date: str | None = Query(None, alias="to"),
    ) -> dict[str, Any]:
        ensure_database()
        try:
            bounds = queries.parse_bbox(bbox)
            start = queries.parse_date(from_date, "from")
            end = queries.parse_date(to_date, "to")
            if start and end and start > end:
                raise ValueError("from must not be later than to")
            return queries.routes(
                db_path, bounds, _split_values(devices), start, end
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @application.get("/api/imports")
    def get_imports() -> dict[str, Any]:
        ensure_database()
        return {"imports": queries.imports(db_path), "job": dict(job_state)}

    async def schedule_tree_import(operation: str) -> dict[str, Any]:
        if job_state["running"] or job_state["queued"] or job_lock.locked():
            raise HTTPException(409, "An import job is already running")
        job_state["queued"] = True
        schedule_import(operation)
        return {"accepted": True, "operation": operation}

    @application.post("/api/import", status_code=status.HTTP_202_ACCEPTED)
    async def trigger_import() -> dict[str, Any]:
        return await schedule_tree_import("import")

    @application.post("/api/rescan", status_code=status.HTTP_202_ACCEPTED)
    async def trigger_rescan() -> dict[str, Any]:
        return await schedule_tree_import("rescan")

    @application.post("/api/upload", status_code=status.HTTP_202_ACCEPTED)
    async def upload(
        device: str = Form(...),
        files: list[UploadFile] = File(...),
    ) -> dict[str, Any]:
        if not DEVICE_RE.fullmatch(device):
            raise HTTPException(
                422, "device must be a lowercase letter/number slug with optional hyphens"
            )
        if not files or len(files) > 100:
            raise HTTPException(400, "Upload between 1 and 100 files")
        if job_state["running"] or job_state["queued"] or job_lock.locked():
            raise HTTPException(409, "An import job is already running")

        destination = (
            import_root / device / datetime.now(timezone.utc).date().isoformat()
        ).resolve()
        if not destination.is_relative_to(import_root):
            raise HTTPException(400, "Invalid upload destination")
        destination.mkdir(parents=True, exist_ok=True)
        saved: list[Path] = []
        job_state["queued"] = True
        try:
            for uploaded in files:
                filename = _safe_filename(uploaded.filename)
                target = destination / filename
                if target.exists():
                    suffix = "".join(target.suffixes)
                    stem = target.name[: -len(suffix)] if suffix else target.name
                    target = destination / f"{stem}-{uuid.uuid4().hex[:8]}{suffix}"
                total = 0
                first_chunk = True
                with target.open("xb") as output:
                    while chunk := await uploaded.read(1024 * 1024):
                        if first_chunk:
                            lower = filename.lower()
                            stripped = chunk.lstrip(b"\xef\xbb\xbf \t\r\n")
                            invalid_content = (
                                chunk.startswith(EXECUTABLE_MAGIC)
                                or (
                                    b"\x00" in chunk[:4096]
                                    and not lower.endswith(
                                        (".sqlite", ".db", ".csv.gz")
                                    )
                                )
                                or (
                                    lower.endswith((".sqlite", ".db"))
                                    and not chunk.startswith(b"SQLite format 3\x00")
                                )
                                or (
                                    lower.endswith(".csv.gz")
                                    and not chunk.startswith(b"\x1f\x8b")
                                )
                                or (
                                    lower.endswith((".kml", ".gpx"))
                                    and not stripped.startswith(b"<")
                                )
                            )
                            if invalid_content:
                                raise HTTPException(
                                    415,
                                    f"Content does not match the allowed file type: {filename}",
                                )
                            first_chunk = False
                        total += len(chunk)
                        if total > max_upload_bytes:
                            raise HTTPException(
                                413, f"{filename} exceeds MAX_UPLOAD_BYTES"
                            )
                        output.write(chunk)
                if total == 0:
                    raise HTTPException(400, f"{filename} is empty")
                saved.append(target)
                await uploaded.close()
        except Exception:
            job_state["queued"] = False
            for path in saved:
                path.unlink(missing_ok=True)
            if "target" in locals():
                target.unlink(missing_ok=True)
            raise

        schedule_import("upload", saved, device)
        return {
            "accepted": True,
            "device": device,
            "files": [path.name for path in saved],
            "message": "Upload saved; import queued.",
        }

    if web_dir.is_dir():
        index = web_dir / "index.html"

        @application.get("/", include_in_schema=False)
        def index_page() -> FileResponse:
            if not index.is_file():
                raise HTTPException(404, "index.html is unavailable")
            return FileResponse(index)

        application.mount(
            "/", StaticFiles(directory=web_dir, html=True), name="web"
        )

    return application


app = create_app()
