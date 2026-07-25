"""Realism knobs: profile load, region containment, type-mix and speed statistics, motion."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace

import pytest

from nmea_sim.realism import (
    CATEGORY_SHIP_TYPE,
    SYNTHETIC_MMSI_MAX,
    SYNTHETIC_MMSI_MIN,
    RealismProfile,
    Region,
    SpeedProfile,
    TargetSpawner,
)


def _bay_profile() -> RealismProfile:
    """A region-neutral profile that merely happens to use a small box (no real locale)."""
    return RealismProfile(
        region=Region(min_lat=10.0, max_lat=10.5, min_lon=-30.5, max_lon=-30.0),
        target_count=50,
        type_mix={"cargo": 0.5, "fishing": 0.3, "pleasure": 0.2},
        speed_profiles={
            "cargo": SpeedProfile(mean_kn=14, std_kn=2, min_kn=6, max_kn=20),
            "fishing": SpeedProfile(mean_kn=4, std_kn=2, min_kn=0, max_kn=9),
        },
        motion_model="transiting",
        class_a_fraction=0.6,
    )


def test_region_clamp_holds_points_already_inside() -> None:
    region = Region(min_lat=10.0, max_lat=10.5, min_lon=-30.5, max_lon=-30.0)
    assert region.clamp(10.25, -30.25) == (10.25, -30.25)


def test_region_clamp_pins_each_edge_to_its_boundary() -> None:
    region = Region(min_lat=10.0, max_lat=10.5, min_lon=-30.5, max_lon=-30.0)
    assert region.clamp(9.0, -30.25) == (10.0, -30.25)  # below min_lat
    assert region.clamp(11.0, -30.25) == (10.5, -30.25)  # above max_lat
    assert region.clamp(10.25, -31.0) == (10.25, -30.5)  # below min_lon
    assert region.clamp(10.25, -29.0) == (10.25, -30.0)  # above max_lon
    # A point outside on both axes clamps both independently.
    assert region.clamp(20.0, -40.0) == (10.5, -30.5)


def test_default_profile_is_area_neutral() -> None:
    p = RealismProfile.default()
    # Neutral defaults sit on the equator/prime meridian — no location baked in.
    assert p.region.contains(0.0, 0.0)
    assert p.target_count > 0


def test_spawn_count_matches_profile() -> None:
    spawner = TargetSpawner(_bay_profile(), seed=1)
    assert len(spawner.spawn()) == 50


def test_all_targets_stay_within_region() -> None:
    profile = _bay_profile()
    spawner = TargetSpawner(profile, seed=2)
    for t in spawner.spawn(200):
        assert profile.region.contains(t.lat, t.lon), (t.lat, t.lon)


def test_type_mix_tracks_configured_weights() -> None:
    profile = _bay_profile()
    spawner = TargetSpawner(profile, seed=3)
    # Reverse-map ship_type back to category to count the realised mix.
    type_to_cat = {v: k for k, v in CATEGORY_SHIP_TYPE.items()}
    counts = Counter(type_to_cat[t.ship_type] for t in spawner.spawn(2000))
    total = sum(counts.values())
    assert counts["cargo"] / total == pytest.approx(0.5, abs=0.06)
    assert counts["fishing"] / total == pytest.approx(0.3, abs=0.06)
    assert counts["pleasure"] / total == pytest.approx(0.2, abs=0.06)


def test_speeds_stay_within_profile_bounds() -> None:
    profile = _bay_profile()
    spawner = TargetSpawner(profile, seed=4)
    for t in spawner.spawn(500):
        assert 0.0 <= t.sog_kn <= 25.0


def test_class_ratio_approximates_fraction() -> None:
    profile = _bay_profile()
    spawner = TargetSpawner(profile, seed=5)
    targets = spawner.spawn(1000)
    frac_a = sum(1 for t in targets if t.class_type == "A") / len(targets)
    assert frac_a == pytest.approx(0.6, abs=0.05)


def test_seed_makes_spawn_deterministic() -> None:
    a = TargetSpawner(_bay_profile(), seed=42).spawn(10)
    b = TargetSpawner(_bay_profile(), seed=42).spawn(10)
    assert [t.mmsi for t in a] == [t.mmsi for t in b]


def test_anchored_targets_do_not_move() -> None:
    profile = RealismProfile(motion_model="anchored", target_count=1)
    spawner = TargetSpawner(profile, seed=6)
    t = spawner.spawn(1)[0]
    moved = spawner.advance(t, dt_s=60.0)
    assert (moved.lat, moved.lon) == (t.lat, t.lon)


def test_transiting_targets_move_and_stay_in_region() -> None:
    profile = _bay_profile()
    spawner = TargetSpawner(profile, seed=7)
    t = spawner.spawn(1)[0]
    moved = spawner.advance(t, dt_s=120.0)
    if t.sog_kn > 0:
        assert (moved.lat, moved.lon) != (t.lat, t.lon)
    assert profile.region.contains(moved.lat, moved.lon)


def test_transiting_target_reflects_off_boundary_not_pinned() -> None:
    """A fast target driven due east for a long dt must **reflect** back inside a tiny region
    rather than pin to the boundary: it stays inside, stays interior (not stuck on the edge),
    and keeps its speed — so a multi-hour run never freezes the fleet on the perimeter."""
    region = Region(min_lat=10.0, max_lat=10.1, min_lon=-30.1, max_lon=-30.0)
    profile = RealismProfile(region=region, motion_model="transiting", target_count=1)
    spawner = TargetSpawner(profile, seed=11)
    base = spawner.spawn(1)[0]
    target = replace(base, lat=10.05, lon=-30.05, sog_kn=20.0, cog_deg=90.0)  # due east, fast

    moved = spawner.advance(target, dt_s=36_000.0)  # ~10h at 20kn: far past the 0.1deg box

    assert region.contains(moved.lat, moved.lon)
    # Strictly interior on the crossed axis: reflected, not clamped onto the eastern edge.
    assert region.min_lon < moved.lon < region.max_lon
    assert moved.lon != pytest.approx(region.max_lon, abs=1e-6)
    assert moved.sog_kn == pytest.approx(20.0)  # still under way, not frozen


def test_single_boundary_bounce_reverses_course() -> None:
    """One eastward reflection off the east edge flips COG 90 -> 270 and heading with it,
    so a bounced contact broadcasts a course consistent with its (reversed) motion."""
    region = Region(min_lat=10.0, max_lat=10.1, min_lon=-30.1, max_lon=-30.0)
    profile = RealismProfile(region=region, motion_model="transiting", target_count=1)
    spawner = TargetSpawner(profile, seed=11)
    base = spawner.spawn(1)[0]
    target = replace(base, lat=10.05, lon=-30.02, sog_kn=20.0, cog_deg=90.0, heading_deg=90)

    moved = spawner.advance(target, dt_s=600.0)  # crosses the east edge exactly once

    assert region.contains(moved.lat, moved.lon)
    assert moved.cog_deg == pytest.approx(270.0)  # eastbound reflected to westbound
    assert moved.heading_deg == 270


def test_multi_hour_run_does_not_pile_fleet_on_boundary() -> None:
    """A whole fleet advanced repeatedly over a multi-hour horizon must stay inside the region
    without collapsing onto its perimeter — the M17 regression."""
    region = Region(min_lat=10.0, max_lat=10.2, min_lon=-30.2, max_lon=-30.0)
    profile = RealismProfile(region=region, motion_model="transiting", target_count=20)
    spawner = TargetSpawner(profile, seed=99)
    targets = spawner.spawn(20)
    for _ in range(60):  # 60 * 5 min = 5 h
        targets = [spawner.advance(t, dt_s=300.0) for t in targets]

    for t in targets:
        assert region.contains(t.lat, t.lon)
    on_edge = sum(
        1
        for t in targets
        if t.lon in (region.min_lon, region.max_lon) or t.lat in (region.min_lat, region.max_lat)
    )
    assert on_edge == 0  # reflection never leaves a contact stuck on the boundary


def test_invalid_motion_model_rejected() -> None:
    with pytest.raises(ValueError):
        RealismProfile(motion_model="teleporting")


def test_empty_type_mix_rejected() -> None:
    with pytest.raises(ValueError):
        RealismProfile(type_mix={})


def test_inverted_region_rejected() -> None:
    with pytest.raises(ValueError):
        Region(min_lat=10.0, max_lat=9.0, min_lon=-30.0, max_lon=-31.0)


def test_out_of_range_region_rejected() -> None:
    with pytest.raises(ValueError):
        Region(min_lat=-91.0, max_lat=10.0, min_lon=0.0, max_lon=1.0)


def test_non_positive_target_count_rejected() -> None:
    with pytest.raises(ValueError):
        RealismProfile(target_count=0)


def test_class_a_fraction_out_of_range_rejected() -> None:
    with pytest.raises(ValueError):
        RealismProfile(class_a_fraction=1.5)


def test_spawned_mmsis_are_in_synthetic_block() -> None:
    """Targets never draw from the real ship-station range: MID 8xx is unassigned, so a spawned
    identity cannot collide with a registered vessel."""
    spawner = TargetSpawner(_bay_profile(), seed=8)
    for t in spawner.spawn(500):
        assert SYNTHETIC_MMSI_MIN <= t.mmsi <= SYNTHETIC_MMSI_MAX


def test_profile_roundtrips_through_dict_and_file(tmp_path) -> None:
    data = {
        "region": {"min_lat": 1.0, "max_lat": 2.0, "min_lon": 3.0, "max_lon": 4.0},
        "target_count": 12,
        "type_mix": {"cargo": 0.7, "other": 0.3},
        "speed_profiles": {"cargo": {"mean_kn": 15, "std_kn": 1, "min_kn": 8, "max_kn": 22}},
        "motion_model": "drifting",
        "class_a_fraction": 0.8,
    }
    path = tmp_path / "area.local.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    loaded = RealismProfile.from_path(path)
    assert loaded.target_count == 12
    assert loaded.motion_model == "drifting"
    assert loaded.class_a_fraction == 0.8
    assert loaded.speed_for("cargo").mean_kn == 15
    assert loaded.region.contains(1.5, 3.5)
