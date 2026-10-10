import logging
import subprocess
import threading


logger = logging.getLogger(__name__)

# Forwarding rules (LocalForward/RemoteForward) live in the ssh config entry of each host alias.
SSH_OPTIONS = [
    "-N",
    "-o", "BatchMode=yes",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
]


class TunnelSupervisor:
    """Keep one `ssh -N <host>` process alive per host and restart it whenever it exits.

    Uses threads and blocking subprocesses because uvicorn runs a selector event loop on
    Windows with --reload, which cannot spawn asyncio subprocesses.
    """

    def __init__(self, hosts: list[str], retry_seconds: float, ssh_command: list[str] | None = None):
        self.hosts = hosts
        self.retry_seconds = retry_seconds
        self.ssh_command = ssh_command or ["ssh"]
        self._stopping = threading.Event()
        self._processes: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        for host in self.hosts:
            thread = threading.Thread(target=self._run, args=(host,), name=f"ssh-tunnel-{host}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self, timeout: float = 5) -> None:
        self._stopping.set()
        with self._lock:
            processes = list(self._processes.values())
        for process in processes:
            process.terminate()
        for thread in self._threads:
            thread.join(timeout)

    def _run(self, host: str) -> None:
        while not self._stopping.is_set():
            try:
                with self._lock:
                    if self._stopping.is_set():
                        return
                    process = subprocess.Popen(
                        [*self.ssh_command, *SSH_OPTIONS, host],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        text=True,
                        errors="replace",
                    )
                    self._processes[host] = process
            except OSError as exc:
                logger.error("SSH tunnel could not start: host=%s error=%s", host, exc)
                self._stopping.wait(self.retry_seconds)
                continue

            logger.info("SSH tunnel started: host=%s pid=%s", host, process.pid)
            _, stderr = process.communicate()
            with self._lock:
                self._processes.pop(host, None)
            if self._stopping.is_set():
                return
            logger.warning(
                "SSH tunnel exited, retrying in %ss: host=%s code=%s stderr=%s",
                self.retry_seconds,
                host,
                process.returncode,
                (stderr or "").strip()[-500:],
            )
            self._stopping.wait(self.retry_seconds)
