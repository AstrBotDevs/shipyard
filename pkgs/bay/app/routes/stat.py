"""Statistics and version information endpoints"""
from fastapi import APIRouter
import tomli
from pathlib import Path

router = APIRouter()


def get_version() -> str:
    """Get version from pyproject.toml"""
    try:
        pyproject_path = Path(__file__).parent.parent.parent / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            data = tomli.load(f)
        return data.get("project", {}).get("version", "unknown")
    except Exception:
        return "unknown"


@router.get("/stat")
async def get_stat():
    """Get service statistics and version information"""
    return {
        "service": "bay",
        "version": get_version(),
        "status": "running",
        "author": "AstrBot Team"
    }
