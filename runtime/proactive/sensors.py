"""Environmental System Sensors
Samples Windows 11 hardware telemetry: CPU, RAM, GPU, storage, and battery metrics.
Generates structured health snapshots for proactive companion assistance.
"""

import os
import logging
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
import psutil

logger = logging.getLogger("presence.proactive.sensors")


@dataclass
class SystemHealthSnapshot:
    """Represents a momentary hardware and resource telemetry snapshot."""
    timestamp: float
    cpu_percent: float
    cpu_cores: int
    ram_total_gb: float
    ram_used_gb: float
    ram_available_gb: float
    ram_percent: float
    disk_total_gb: float
    disk_free_gb: float
    disk_percent: float
    gpu_name: Optional[str] = None
    gpu_vram_total_mb: Optional[int] = None
    gpu_vram_used_mb: Optional[int] = None
    battery_percent: Optional[float] = None
    battery_plugged: Optional[bool] = None
    is_healthy: bool = True
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "cpu_percent": self.cpu_percent,
            "cpu_cores": self.cpu_cores,
            "ram": {
                "total_gb": round(self.ram_total_gb, 2),
                "used_gb": round(self.ram_used_gb, 2),
                "available_gb": round(self.ram_available_gb, 2),
                "percent": self.ram_percent,
            },
            "disk": {
                "total_gb": round(self.disk_total_gb, 2),
                "free_gb": round(self.disk_free_gb, 2),
                "percent": self.disk_percent,
            },
            "gpu": {
                "name": self.gpu_name or "Standard Display",
                "vram_total_mb": self.gpu_vram_total_mb,
                "vram_used_mb": self.gpu_vram_used_mb,
            },
            "battery": {
                "percent": self.battery_percent,
                "plugged": self.battery_plugged,
            },
            "is_healthy": self.is_healthy,
            "warnings": self.warnings,
        }


class SystemSensors:
    """Proactive sensor interface querying host system resource utilization."""

    @classmethod
    def sample(cls) -> SystemHealthSnapshot:
        """Collects current hardware and operational metrics."""
        import time
        now = time.time()
        warnings: List[str] = []

        # 1. CPU Metrics
        cpu_percent = psutil.cpu_percent(interval=None)
        cpu_cores = psutil.cpu_count(logical=True) or 1
        if cpu_percent > 90.0:
            warnings.append(f"High CPU utilization: {cpu_percent}%")

        # 2. RAM Metrics
        vm = psutil.virtual_memory()
        ram_total_gb = vm.total / (1024 ** 3)
        ram_used_gb = vm.used / (1024 ** 3)
        ram_available_gb = vm.available / (1024 ** 3)
        ram_percent = vm.percent
        if ram_percent > 88.0:
            warnings.append(f"High RAM pressure: {ram_percent}% used ({ram_available_gb:.1f} GB free)")

        # 3. Disk Metrics (System Drive C:)
        try:
            sys_drive = os.environ.get("SystemDrive", "C:") + "\\"
            du = psutil.disk_usage(sys_drive)
            disk_total_gb = du.total / (1024 ** 3)
            disk_free_gb = du.free / (1024 ** 3)
            disk_percent = du.percent
            if disk_free_gb < 15.0:
                warnings.append(f"Low system disk space: {disk_free_gb:.1f} GB free on {sys_drive}")
        except Exception:
            disk_total_gb = 500.0
            disk_free_gb = 100.0
            disk_percent = 80.0

        # 4. GPU Metrics (Detection via torch/nvidia or standard query)
        gpu_name = None
        gpu_vram_total = None
        gpu_vram_used = None
        try:
            import torch
            if torch.cuda.is_available():
                gpu_name = torch.cuda.get_device_name(0)
                gpu_vram_total = int(torch.cuda.get_device_properties(0).total_memory / (1024 ** 2))
                gpu_vram_used = int(torch.cuda.memory_allocated(0) / (1024 ** 2))
        except Exception:
            pass

        # 5. Battery Metrics
        battery_pct = None
        battery_plugged = None
        try:
            bat = psutil.sensors_battery()
            if bat:
                battery_pct = bat.percent
                battery_plugged = bat.power_plugged
                if battery_pct < 20.0 and not battery_plugged:
                    warnings.append(f"Battery critically low: {battery_pct:.0f}% (not plugged in)")
        except Exception:
            pass

        is_healthy = len(warnings) == 0

        return SystemHealthSnapshot(
            timestamp=now,
            cpu_percent=cpu_percent,
            cpu_cores=cpu_cores,
            ram_total_gb=ram_total_gb,
            ram_used_gb=ram_used_gb,
            ram_available_gb=ram_available_gb,
            ram_percent=ram_percent,
            disk_total_gb=disk_total_gb,
            disk_free_gb=disk_free_gb,
            disk_percent=disk_percent,
            gpu_name=gpu_name,
            gpu_vram_total_mb=gpu_vram_total,
            gpu_vram_used_mb=gpu_vram_used,
            battery_percent=battery_pct,
            battery_plugged=battery_plugged,
            is_healthy=is_healthy,
            warnings=warnings,
        )
