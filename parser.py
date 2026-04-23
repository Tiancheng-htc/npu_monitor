import re
from dataclasses import dataclass, field
from typing import Dict, List, Tuple


@dataclass
class Process:
    pid: int
    name: str
    memory_mb: int


@dataclass
class ChipStatus:
    npu_id: int
    chip_id: int
    phy_id: int = -1
    health: str = ""
    aicore_pct: int = 0
    mem_used: int = 0
    mem_total: int = 0
    hbm_used: int = 0
    hbm_total: int = 0
    temp_c: int = 0
    power_w: str = "-"
    processes: List[Process] = field(default_factory=list)

    @property
    def is_idle(self) -> bool:
        return len(self.processes) == 0

    @property
    def key(self) -> Tuple[int, int]:
        return (self.npu_id, self.chip_id)


_HEADER_RE = re.compile(
    r"^\|\s*(\d+)\s+(\S+)\s*\|\s*(\S+)\s*\|\s*([-\d.]+)\s+(\d+)\s+(\d+)\s*/\s*(\d+)\s*\|?\s*$"
)
_DETAIL_RE = re.compile(
    r"^\|\s*(\d+)\s+(\d+)\s*\|\s*([\w:.]+)\s*\|\s*(\d+)\s+(\d+)\s*/\s*(\d+)\s+(\d+)\s*/\s*(\d+)\s*\|?\s*$"
)
_PROCESS_RE = re.compile(
    r"^\|\s*(\d+)\s+(\d+)\s*\|\s*(\d+)\s*\|\s*(.+?)\s*\|\s*(\d+)\s*\|?\s*$"
)


def parse_npu_smi(text: str) -> List[ChipStatus]:
    lines = text.splitlines()

    process_start = -1
    for i, line in enumerate(lines):
        if "Process id" in line and "Process name" in line:
            process_start = i
            break

    status_lines = lines if process_start < 0 else lines[:process_start]
    process_lines = [] if process_start < 0 else lines[process_start + 1 :]

    chips: Dict[Tuple[int, int], ChipStatus] = {}
    pending: dict | None = None

    for raw in status_lines:
        line = raw.rstrip()
        m = _HEADER_RE.match(line)
        if m:
            name = m.group(2)
            if name in ("Name", "Chip"):
                continue
            pending = {
                "npu_id": int(m.group(1)),
                "name": name,
                "health": m.group(3),
                "power": m.group(4),
                "temp": int(m.group(5)),
            }
            continue
        if pending is not None:
            m = _DETAIL_RE.match(line)
            if m:
                chip_id = int(m.group(1))
                chips[(pending["npu_id"], chip_id)] = ChipStatus(
                    npu_id=pending["npu_id"],
                    chip_id=chip_id,
                    phy_id=int(m.group(2)),
                    health=pending["health"],
                    aicore_pct=int(m.group(4)),
                    mem_used=int(m.group(5)),
                    mem_total=int(m.group(6)),
                    hbm_used=int(m.group(7)),
                    hbm_total=int(m.group(8)),
                    temp_c=pending["temp"],
                    power_w=pending["power"],
                )
                pending = None

    for raw in process_lines:
        line = raw.rstrip()
        m = _PROCESS_RE.match(line)
        if not m:
            continue
        npu_id = int(m.group(1))
        chip_id = int(m.group(2))
        key = (npu_id, chip_id)
        if key in chips:
            chips[key].processes.append(
                Process(
                    pid=int(m.group(3)),
                    name=m.group(4).strip(),
                    memory_mb=int(m.group(5)),
                )
            )

    return sorted(chips.values(), key=lambda c: (c.phy_id if c.phy_id >= 0 else c.npu_id * 100 + c.chip_id))
