"""Keeps the scan from disturbing the user.

A monitor thread samples the machine every few seconds. Workers call ``wait()`` before each
directory; while the computer is busy (sustained CPU from *other* processes, a full-screen
game/app, or memory pressure) they block until it has been calm for a while.
"""
import threading
import time
from collections import deque

from . import winapi


class LoadGuard(threading.Thread):
    def __init__(self, cpu_limit=60, mem_limit=90, interval=5.0, calm_seconds=30, enabled=True):
        super().__init__(daemon=True, name="LoadGuard")
        self.cpu_limit = cpu_limit
        self.mem_limit = mem_limit
        self.interval = interval
        self.calm_needed = max(1, int(calm_seconds / interval))
        self.enabled = enabled
        self.samples = deque(maxlen=max(6, self.calm_needed))
        self._allow = threading.Event()
        self._allow.set()
        self._stop = threading.Event()
        self.reason = None          # why we are paused (shown in the UI)
        self.last_cpu = 0.0          # CPU used by other processes, %
        self.paused_seconds = 0.0
        self._paused_at = None

    # --- public -----------------------------------------------------------------------
    @property
    def paused(self):
        return not self._allow.is_set()

    def force(self):
        """User said 'continue anyway': ignore load from now on."""
        self.enabled = False
        self._resume()

    def wait(self, stop_event):
        """Block while paused. Returns False if the scan was stopped meanwhile."""
        while not self._allow.wait(1.0):
            if stop_event.is_set():
                return False
        return not stop_event.is_set()

    def shutdown(self):
        self._stop.set()
        self._resume()

    # --- internals --------------------------------------------------------------------
    def _pause(self, reason):
        self.reason = reason
        if self._allow.is_set():
            self._paused_at = time.monotonic()
            self._allow.clear()

    def _resume(self):
        if not self._allow.is_set() and self._paused_at is not None:
            self.paused_seconds += time.monotonic() - self._paused_at
        self._paused_at = None
        self.reason = None
        self._allow.set()

    def run(self):
        prev_sys = winapi.system_times()
        prev_own = winapi.process_cpu_time()
        while not self._stop.wait(self.interval):
            idle, kern, user = winapi.system_times()
            own = winapi.process_cpu_time()
            total = (kern - prev_sys[1]) + (user - prev_sys[2])
            busy = total - (idle - prev_sys[0])
            own_d = own - prev_own
            prev_sys, prev_own = (idle, kern, user), own
            other = max(0.0, (busy - own_d) / total * 100) if total > 0 else 0.0
            self.last_cpu = other
            self.samples.append(other)

            if not self.enabled:
                continue

            busy_ui = winapi.user_busy_reason()
            recent = list(self.samples)[-3:]
            avg3 = sum(recent) / len(recent)
            over = sum(1 for s in recent if s > self.cpu_limit)
            mem = winapi.memory_load()

            reason = None
            if busy_ui:
                reason = busy_ui
            elif len(recent) >= 3 and (avg3 > self.cpu_limit or over >= 2):
                reason = f"Processador ocupado ({avg3:.0f}% em uso por outros programas)"
            elif mem >= self.mem_limit:
                reason = f"Memória quase cheia ({mem}%)"

            if reason:
                self._pause(reason)
            elif self.paused:
                calm = list(self.samples)[-self.calm_needed:]
                if len(calm) >= self.calm_needed and max(calm) < self.cpu_limit:
                    self._resume()
                else:
                    self.reason = "Aguardando o computador ficar livre"
