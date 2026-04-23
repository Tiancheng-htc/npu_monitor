import re
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class SSHHost:
    alias: str
    hostname: Optional[str] = None
    user: Optional[str] = None


def parse_ssh_config(path: str) -> List[SSHHost]:
    """Read an OpenSSH config file and return unique host entries.

    The config is only used to enumerate host aliases for the UI; the
    actual ssh calls don't pass ``-F`` and just rely on ssh's default
    behavior of reading ``~/.ssh/config``.
    """
    hosts: List[SSHHost] = []
    seen: set = set()
    current: Optional[dict] = None

    def flush() -> None:
        nonlocal current
        if not current:
            return
        for alias in current.get("_aliases", []):
            if "*" in alias or "?" in alias or alias in seen:
                continue
            seen.add(alias)
            hosts.append(
                SSHHost(
                    alias=alias,
                    hostname=current.get("hostname"),
                    user=current.get("user"),
                )
            )
        current = None

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = re.split(r"[\s=]+", line, maxsplit=1)
            if len(parts) != 2:
                continue
            key = parts[0].lower()
            val = parts[1].strip().strip('"')
            if key == "host":
                flush()
                aliases = re.split(r"\s+", val)
                current = {"_aliases": aliases}
            elif current is not None:
                current[key] = val

    flush()
    return hosts


CREATE_NO_WINDOW = 0x08000000


def _ssh_args(ssh_binary: str, host: str, cmd: str) -> List[str]:
    return [
        ssh_binary,
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=20",
        "-o", "StrictHostKeyChecking=accept-new",
        f"root@{host}",
        cmd,
    ]


def _subprocess_kwargs() -> dict:
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = CREATE_NO_WINDOW
    return kwargs


def run_ssh(
    host: str,
    cmd: str,
    ssh_binary: str = "ssh",
    timeout: float = 45.0,
) -> Tuple[int, str, str]:
    args = _ssh_args(ssh_binary, host, cmd)
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            **_subprocess_kwargs(),
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"SSH timeout after {timeout:.0f}s"
    except FileNotFoundError:
        return -1, "", f"SSH binary not found: {ssh_binary}"
    except Exception as e:
        return -1, "", f"{type(e).__name__}: {e}"


def run_ssh_pipe(
    host: str,
    cmd: str,
    stdin_bytes: bytes,
    ssh_binary: str = "ssh",
    timeout: float = 90.0,
) -> Tuple[int, str, str]:
    args = _ssh_args(ssh_binary, host, cmd)
    try:
        result = subprocess.run(
            args,
            input=stdin_bytes,
            capture_output=True,
            timeout=timeout,
            **_subprocess_kwargs(),
        )
        stdout = result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
        stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
        return result.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"SSH timeout after {timeout:.0f}s"
    except FileNotFoundError:
        return -1, "", f"SSH binary not found: {ssh_binary}"
    except Exception as e:
        return -1, "", f"{type(e).__name__}: {e}"
