from typing import Any, Dict
import psutil
from pathlib import Path
from config import settings


def get_vps_resource_snapshot() -> Dict[str, Any]:
    """Measures current RAM, CPU, and Storage consumption on the Linux host."""
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage(str(Path(".").resolve()))
    return {
        "ram_used_mb": round(vm.used / (1024 * 1024), 1),
        "ram_total_mb": round(vm.total / (1024 * 1024), 1),
        "ram_percent": vm.percent,
        "cpu_percent": psutil.cpu_percent(interval=0.1),
        "disk_free_mb": round(disk.free / (1024 * 1024), 1),
        "log_file_path": settings.LOG_FILE_PATH,
    }
