"""Optional voltage-sensing tests: the null default, a fake differential provider, the measured
polarity verdict, and the config round-trip.

No hardware and no ADC library are touched: the default provider reports "not installed", and a
tiny fake provider stands in for real sensing so the polarity logic can be driven through both
signs of the idle differential. The ``AdcVoltageProvider`` is only poked to prove its driver import
is LAZY — importing this module never needs an ADC lib, and a missing driver surfaces a clear error
only when a live provider is actually constructed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nmea_sim.config import EngineConfig, VoltageSenseSpec
from nmea_sim.diagnostics import classify_fault
from nmea_sim.voltage import (
    AdcVoltageProvider,
    NullVoltageProvider,
    VoltageProvider,
    VoltageReading,
    polarity_from_voltage,
)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


class _FakeProvider:
    """A hardware-free provider that reports a fixed idle differential for any slot.

    ``diff_v``'s sign is the whole point: it is what ``polarity_from_voltage`` turns into a measured
    verdict, so a test can flip the sign to drive both the normal and the reversed orientation.
    """

    def __init__(self, diff_v: float, *, present: bool = True) -> None:
        self._diff_v = diff_v
        self._present = present

    def read(self, slot: str) -> VoltageReading:
        half = self._diff_v / 2.0
        return VoltageReading(
            a_v=half,
            b_v=-half,
            diff_v=self._diff_v,
            common_v=0.0,
            present=self._present,
        )


# --- null provider: sensing not installed ------------------------------------------


def test_null_provider_reports_not_present() -> None:
    reading = NullVoltageProvider().read("p1")
    assert reading.present is False
    # A not-present reading is "unknown", never a measured zero, so polarity declines to upgrade.
    assert polarity_from_voltage(reading) is None


def test_null_provider_satisfies_the_protocol() -> None:
    assert isinstance(NullVoltageProvider(), VoltageProvider)
    assert isinstance(_FakeProvider(1.0), VoltageProvider)


# --- fake provider: the differential sign flips the verdict -------------------------


def test_polarity_flips_with_the_differential_sign() -> None:
    """A positive idle differential is the normal orientation; a negative one is a swapped pair."""
    normal = _FakeProvider(diff_v=2.5).read("p1")
    reversed_pair = _FakeProvider(diff_v=-2.5).read("p1")
    assert polarity_from_voltage(normal) == "ab-normal"
    assert polarity_from_voltage(reversed_pair) == "ab-reversed"


def test_polarity_ambiguous_inside_deadband() -> None:
    """A floating/open pair sits inside the idle deadband -> ambiguous (None), never coerced."""
    floating = _FakeProvider(diff_v=0.05).read("p1")
    assert polarity_from_voltage(floating) is None


# --- the measured verdict upgrades the inferred one --------------------------------


def test_measured_voltage_upgrades_inferred_reversed_ab() -> None:
    """The analyzer INFERS ``reversed-ab`` from the byte stream alone (a ranked guess). When a
    present provider answers, its measured polarity supersedes that inference; with no sensing
    hardware there is nothing to upgrade and the inference stands on its own."""
    inferred, _ = classify_fault(
        {"bytes": 200, "printable_ratio": 0.1, "valid": 0, "bad_checksum": 0, "malformed": 5}
    )
    assert inferred == "reversed-ab"  # the wire-only guess

    # A present sensor turns the guess into a measured fact (both orientations resolve).
    measured_reversed = polarity_from_voltage(_FakeProvider(diff_v=-3.3).read("p1"))
    measured_normal = polarity_from_voltage(_FakeProvider(diff_v=3.3).read("p1"))
    assert measured_reversed == "ab-reversed"
    assert measured_normal == "ab-normal"

    # No hardware: the inference cannot be upgraded and remains the only signal.
    assert polarity_from_voltage(NullVoltageProvider().read("p1")) is None


# --- AdcVoltageProvider: import is lazy, not at module load -------------------------


def test_adc_provider_missing_driver_raises_only_at_construction() -> None:
    """Importing the module never needs an ADC lib; a live provider with an absent driver raises a
    clear error only when actually constructed."""
    with pytest.raises(RuntimeError, match="not installed"):
        AdcVoltageProvider("nmea_sim._definitely_not_a_real_adc_driver", {"p1": {"a": 0}})


def test_adc_provider_empty_driver_name_is_a_clear_error() -> None:
    with pytest.raises(RuntimeError, match="no ADC driver"):
        AdcVoltageProvider("", {})


# --- config round-trip -------------------------------------------------------------


def test_voltage_sense_spec_round_trips() -> None:
    spec = VoltageSenseSpec(
        enabled=True,
        driver="some.adc.module",
        i2c_address=0x48,
        channels={"p1": {"a": 0, "b": 1}},
        divider_ratio=2.0,
    )
    assert VoltageSenseSpec.from_dict(spec.to_dict()) == spec


def test_voltage_sense_defaults_to_disabled_when_absent() -> None:
    spec = VoltageSenseSpec.from_dict({})
    assert spec.enabled is False
    assert spec.driver == ""
    assert spec.divider_ratio == 1.0


def test_engine_config_carries_voltage_sense_only_when_present(tmp_path: Path) -> None:
    """A config without a ``voltage_sense`` block leaves the field None and never emits the key
    (existing configs are unaffected); adding the block round-trips through the engine config."""
    base = json.loads(CONFIG_PATH.read_text())

    absent = EngineConfig.from_dict(base)
    assert absent.voltage_sense is None
    assert "voltage_sense" not in absent.to_dict()

    enriched = dict(base)
    enriched["voltage_sense"] = {
        "enabled": True,
        "driver": "some.adc.module",
        "i2c_address": 72,
        "channels": {"p1": {"a": 0}},
        "divider_ratio": 2.0,
    }
    cfg = EngineConfig.from_dict(enriched)
    assert cfg.voltage_sense is not None
    assert cfg.voltage_sense.enabled is True
    assert cfg.to_dict()["voltage_sense"]["driver"] == "some.adc.module"
