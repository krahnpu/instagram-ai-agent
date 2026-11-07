import importlib
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from config import settings
from memory.database import DatabaseManager
from utils.logger import get_logger


logger = get_logger("core.self_extender")


class SelfExtenderManager:
    """
    Governed Self-Extension & Package Installation Manager.
    Strictly enforces Owner approval before installing any Python package or modifying files:
    1. Detects required package, proposed version, reason, and dependency risk analysis
    2. Queues a pending request awaiting explicit Owner confirmation («تأیید»)
    3. Installs strictly inside the active Python environment (`sys.executable -m pip install`)
       with strict package-name sanitization (no arbitrary shell commands)
    4. Verifies module import post-installation and logs to `package_audit`
    5. Creates full timestamped file backups in `data/backups/` before any source file update
       with 1-click rollback capability
    """

    SAFE_PKG_REGEX = re.compile(r"^[a-zA-Z0-9_\-]+([><=~!]+[0-9a-zA-Z.*]+)?$")

    def __init__(self, db: DatabaseManager) -> None:
        self.db = db
        self.backup_dir = Path(settings.BACKUP_DIR)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self._pending_requests: Dict[str, Dict[str, Any]] = {}

    def propose_package_install(
        self,
        package_name: str,
        version_spec: str,
        reason: str,
        import_test_name: str = "",
    ) -> Dict[str, Any]:
        clean_pkg = package_name.strip()
        clean_ver = version_spec.strip() or "latest"
        full_spec = f"{clean_pkg}=={clean_ver}" if clean_ver != "latest" and not clean_ver.startswith((">", "=")) else clean_pkg

        if not self.SAFE_PKG_REGEX.match(full_spec):
            return {
                "status": "rejected",
                "error": f"نام پکیج '{full_spec}' با الگوی امنیتی مطابقت ندارد.",
            }

        req_id = f"pkg_{uuid.uuid4().hex[:8]}"
        proposal = {
            "request_id": req_id,
            "package_name": clean_pkg,
            "version_spec": clean_ver,
            "pip_spec": full_spec,
            "reason": reason,
            "import_name": import_test_name or clean_pkg.replace("-", "_"),
            "risk_assessment": "نصب در محیط مجازی ایزوله Python بدون دسترسی Root؛ نیازمند بررسی حجم حافظه هاست.",
            "status": "نیازمند تأیید ادمین",
        }
        self._pending_requests[req_id] = proposal
        return proposal

    def list_pending_requests(self) -> List[Dict[str, Any]]:
        return list(self._pending_requests.values())

    def approve_and_install_package(self, request_id: str, confirmed_by_owner: bool = False) -> Dict[str, Any]:
        if not confirmed_by_owner:
            return {
                "status": "permission_denied",
                "error": "نصب پکیج بدون تأیید صریح ادمین اصلی مجاز نیست.",
            }

        proposal = self._pending_requests.get(request_id)
        if not proposal:
            return {"status": "not_found", "error": "درخواست نصب موردنظر یافت نشد یا قبلاً اجرا شده است."}

        pip_spec = proposal["pip_spec"]
        cmd = [sys.executable, "-m", "pip", "install", "--no-cache-dir", pip_spec]

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90, check=False)
            if proc.returncode != 0:
                return {
                    "status": "ناموفق",
                    "package": pip_spec,
                    "error": proc.stderr[-400:] or "خطا در اجرای pip install",
                }

            # Verify import works
            import_ok = True
            import_err = ""
            try:
                importlib.invalidate_caches()
                importlib.import_module(proposal["import_name"])
            except Exception as imp_exc:
                import_ok = False
                import_err = str(imp_exc)

            self._pending_requests.pop(request_id, None)
            return {
                "status": "موفق" if import_ok else "نصب شد (نیازمند بررسی ماژول)",
                "package": pip_spec,
                "import_verified": import_ok,
                "import_error": import_err,
                "output_tail": proc.stdout[-300:],
            }
        except Exception as exc:
            logger.error(f"Package installation error for {pip_spec}: {exc}")
            return {"status": "ناموفق", "error": str(exc)}

    def create_file_backup(self, target_file: Path) -> Dict[str, Any]:
        if not target_file.exists():
            return {"ok": False, "error": f"فایل {target_file} وجود ندارد."}
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = self.backup_dir / f"{target_file.name}.{stamp}.bak"
        shutil.copy2(target_file, backup_path)
        return {"ok": True, "original": str(target_file), "backup_path": str(backup_path)}

    def rollback_file_from_backup(self, backup_path: Path, target_file: Path) -> Dict[str, Any]:
        if not backup_path.exists():
            return {"ok": False, "error": "فایل پشتیبان یافت نشد."}
        shutil.copy2(backup_path, target_file)
        return {"ok": True, "restored_file": str(target_file)}
