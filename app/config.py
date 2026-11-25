import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./data/platform.db"
    import_root: Path = ROOT / "data/imports"
    demo_mode: bool = True
    demo_base_url: str = "http://127.0.0.1:8000"
    http_allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost")
    batch_size: int = 50
    lease_seconds: int = 300
    max_source_records: int = 10000

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env", override=False)
        url = os.getenv("DATABASE_URL", "sqlite:///./data/platform.db")
        if url.startswith("sqlite:///./"):
            url = "sqlite:///" + (ROOT / url.removeprefix("sqlite:///./")).as_posix()
        imports = Path(os.getenv("IMPORT_ROOT", "./data/imports"))
        if not imports.is_absolute():
            imports = ROOT / imports
        batch = int(os.getenv("SYNC_BATCH_SIZE", "50"))
        lease = int(os.getenv("SOURCE_LEASE_SECONDS", "300"))
        if not 1 <= batch <= 1000 or not 10 <= lease <= 3600:
            raise ValueError("Invalid SYNC_BATCH_SIZE or SOURCE_LEASE_SECONDS")
        return cls(
            database_url=url,
            import_root=imports.resolve(),
            demo_mode=os.getenv("DEMO_MODE", "true").lower() == "true",
            demo_base_url=os.getenv("DEMO_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
            http_allowed_hosts=tuple(
                h.strip() for h in os.getenv("HTTP_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")
            ),
            batch_size=batch,
            lease_seconds=lease,
        )
