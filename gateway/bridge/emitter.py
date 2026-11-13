# fiber.gateway.bridge.emitter
from typing import Any, Callable, Optional
from xphi.arch.contract.event.next import LogEvent
from xphi.watcher.plane.emitter import (
    SurfaceEmitter as _XphiEmitter,
    SurfacePlaneHandler,
    flow_scope,
    get_logger,
    register_interceptor,
    set_log_level,
)

class SurfaceEmitter:
    __slots__ = (
        "_core",
        "debug", "trace", "info", "warning", "warn",
        "error", "critical", "crit", "exception", "signal", "flush",
        "_log", "_format_msg",
    )

    def __init__(self, name: str, phase=None, boundary=None, handler=None, mode: str = "NORMAL"):
        core = _XphiEmitter(name, phase=phase, boundary=boundary, handler=handler, mode=mode)
        object.__setattr__(self, "_core", core)
        object.__setattr__(self, "debug", core.debug)
        object.__setattr__(self, "trace", core.trace)
        object.__setattr__(self, "info", core.info)
        object.__setattr__(self, "warning", core.warning)
        object.__setattr__(self, "warn", core.warn)
        object.__setattr__(self, "error", core.error)
        object.__setattr__(self, "critical", core.critical)
        object.__setattr__(self, "crit", core.crit)
        object.__setattr__(self, "exception", core.exception)
        object.__setattr__(self, "signal", core.signal)
        object.__setattr__(self, "flush", core.flush)
        object.__setattr__(self, "_log", core._log)
        object.__setattr__(self, "_format_msg", core._format_msg)

    def set_mode(self, mode: str) -> "SurfaceEmitter":
        self._core.set_mode(mode)
        return self

    # __getattr__ / __setattr__ 매직 메서드를 제거하고 명시적 프로퍼티로만 연결 (Receptor/Inspect 스캔 안전)
    @property
    def name(self) -> str: return self._core.name
    @property
    def phase(self): return self._core.phase
    @property
    def bound(self): return self._core.bound
    @property
    def mode(self) -> str: return self._core.mode
    @mode.setter
    def mode(self, val: str): self._core.mode = val


def get_emitter(
    name: str,
    phase: Optional[str] = None,
    boundary: Optional[str] = None,
    mode: str = "NORMAL",
) -> SurfaceEmitter:
    return SurfaceEmitter(name, phase=phase, boundary=boundary, mode=mode)