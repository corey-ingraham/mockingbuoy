"""Optional differential line-voltage sensing for the bench diagnostics surface.

The diagnostics analyzer can INFER a reversed-A/B pair from the byte stream alone (no printable
structure at any baud). That is a ranked guess, not a measurement. When — and only when — sensing
hardware is wired in, this module reads the idle differential voltage on a slot's A/B pair and
turns that guess into a MEASURED verdict: the sign of the idle differential tells you which line
is which.

Everything here is off by default and hardware-agnostic. The whole point of the Protocol +
``NullVoltageProvider`` split is that the app imports and runs fine on a box with no ADC library
present; a concrete driver is lazy-imported at construction/read time, so importing this module
never pulls in a hardware dependency. A missing driver library surfaces a clear error only when
someone actually asks a live provider to read — never at import, never at app start.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# Idle differential magnitudes below this (volts) are treated as "no meaningful signal", so a
# floating/open pair reads as ambiguous rather than being coerced into a polarity verdict.
_IDLE_DEADBAND_V = 0.2


@dataclass(frozen=True)
class VoltageReading:
    """A single differential line-voltage sample for one slot's A/B pair.

    ``a_v``/``b_v`` are the two conductors referenced to signal common; ``diff_v`` is A minus B
    (its idle SIGN is what discriminates polarity); ``common_v`` is the common-mode level (useful
    for spotting a wrong electrical standard or a ground offset). ``present`` is false whenever no
    sensing hardware answered — every consumer must treat a not-present reading as "unknown", not
    as zero volts.
    """

    a_v: float
    b_v: float
    diff_v: float
    common_v: float
    present: bool


@runtime_checkable
class VoltageProvider(Protocol):
    """Anything that can answer the idle differential voltage for a named slot."""

    def read(self, slot: str) -> VoltageReading:
        """Return the current differential reading for ``slot`` (present=False if unavailable)."""
        ...


class NullVoltageProvider:
    """The default provider: no sensing hardware installed.

    Every read reports ``present=False`` with zeroed rails, which the advisor reads as "voltage
    sensing not installed" and simply declines to upgrade its inferred polarity guess.
    """

    def read(self, slot: str) -> VoltageReading:
        return VoltageReading(a_v=0.0, b_v=0.0, diff_v=0.0, common_v=0.0, present=False)


class AdcVoltageProvider:
    """Generic ADC-backed provider behind :class:`VoltageProvider`.

    The concrete ADC library is intentionally NOT imported at module load — it is lazy-imported
    inside ``__init__`` (to fail fast with a clear message when the box is configured for sensing
    but the library is missing) and used again in ``read``. This keeps the whole app importable
    and runnable on a machine that has no ADC library at all: the failure is deferred until
    something genuinely tries to use live sensing.

    ``channels`` maps a slot id to that slot's ADC-input wiring; the shape is opaque here and
    handed straight to the driver. ``divider_ratio`` scales a resistor-divider'd raw reading back
    up to the true line voltage (1.0 == no divider).
    """

    def __init__(
        self,
        driver: str,
        channels: dict[str, Any],
        *,
        i2c_address: int = 0,
        divider_ratio: float = 1.0,
    ) -> None:
        self._driver_name = driver
        self._channels = dict(channels)
        self._i2c_address = i2c_address
        self._divider_ratio = divider_ratio
        # Lazy import: resolve the driver only now, so importing this module never needs the lib.
        self._driver = self._load_driver(driver)

    @staticmethod
    def _load_driver(driver: str) -> Any:
        """Import the named ADC driver module; raise a clear error if it (or the name) is absent."""
        if not driver:
            raise RuntimeError(
                "voltage sensing enabled but no ADC driver configured (set voltage_sense.driver)"
            )
        import importlib

        try:
            return importlib.import_module(driver)
        except ImportError as exc:  # library genuinely not installed on this box
            raise RuntimeError(
                f"ADC driver {driver!r} is not installed; install it or disable voltage_sense"
            ) from exc

    def read(self, slot: str) -> VoltageReading:
        wiring = self._channels.get(slot)
        if wiring is None:
            # Slot has no sensing wiring — indistinguishable from "not installed" for this slot.
            return VoltageReading(a_v=0.0, b_v=0.0, diff_v=0.0, common_v=0.0, present=False)
        # The driver contract is deliberately minimal and duck-typed: it exposes read_pair(wiring,
        # i2c_address) -> (a_raw, b_raw). Concrete adapters live outside tracked code (hardware-
        # specific), so we stay agnostic to any particular vendor library here.
        a_raw, b_raw = self._driver.read_pair(wiring, self._i2c_address)
        a_v = float(a_raw) * self._divider_ratio
        b_v = float(b_raw) * self._divider_ratio
        return VoltageReading(
            a_v=a_v,
            b_v=b_v,
            diff_v=a_v - b_v,
            common_v=(a_v + b_v) / 2.0,
            present=True,
        )


def polarity_from_voltage(reading: VoltageReading) -> str | None:
    """Derive a MEASURED A/B polarity verdict from the idle differential sign.

    On an idle RS-422/485 pair the two conductors sit at a known, non-symmetric differential; its
    SIGN says which line is A and which is B. A positive idle differential (A above B) is the
    normal orientation; a negative one means the pair is physically swapped. This is what promotes
    the analyzer's INFERRED reversed-A/B guess to a measured fact.

    Returns None (ambiguous — do not upgrade the inference) when no reading is present or the idle
    differential is inside the deadband, i.e. a floating/open pair with nothing to measure.
    """
    if not reading.present:
        return None
    if abs(reading.diff_v) < _IDLE_DEADBAND_V:
        return None
    return "ab-normal" if reading.diff_v > 0 else "ab-reversed"
