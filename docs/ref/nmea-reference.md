# NMEA 0183 Reference

## XOR checksum

Bitwise XOR of every character **between** `$` (or `!`) and `*`, exclusive of both. Emit as
2-digit **uppercase** hex, prefixed by `*`.

```python
def xor_checksum(body: str) -> int:      # body = chars between $/! and *
    c = 0
    for ch in body:
        c ^= ord(ch)
    return c
# full sentence: f"${body}*{xor_checksum(body):02X}"
```

`pynmea2` appends this automatically when you `str(msg)`. Keep the hand-rolled helper as a fallback for
sentence types pynmea2 doesn't model. AIS `!AIVDM` lines are already checksummed by `pyais`.

## Decimal degrees → ddmm.mmmm / dddmm.mmmm

Latitude uses **2** degree digits; longitude uses **3**. Minutes = fractional degrees × 60 with a
leading zero. Hemisphere from the sign; format the absolute value.

```python
def deg_to_nmea(value, is_lon):
    hemi = ("E" if value >= 0 else "W") if is_lon else ("N" if value >= 0 else "S")
    value = abs(value); deg = int(value); minutes = (value - deg) * 60
    width = 3 if is_lon else 2
    return f"{deg:0{width}d}{minutes:07.4f}", hemi   # 37.802 -> ('3748.1200', 'N')
```

## Sentence field maps (fields between `$TT<type>,` and `*hh`)

| Sentence | Talker | Course/heading | Fields (in order) |
|---|---|---|---|
| **GGA** | GP | — | utc `hhmmss.ss`; lat,N/S; lon,E/W; fix_quality; sats(02d); hdop; alt,`M`; geoid_sep,`M`; dgps_age; dgps_ref |
| **RMC** | GP | **COG** | utc; status `A`; lat,N/S; lon,E/W; sog_kn; **cog_deg**; date `ddmmyy`; mag_var,`E`/`W`; mode `A` |
| **VTG** | GP | **COG** | **cog_deg**,`T`; cog_mag,`M`; sog_kn,`N`; kmh,`K`; mode `A` |
| **ZDA** | GP | — | utc; day; month; **4-digit year**; local_zone_hr `00`; local_zone_min `00` |
| **GLL** | GP | — | lat,N/S; lon,E/W; utc; status `A`; mode `A` |
| **HDT** | HE | **heading_true** | **heading_true_deg**; `T` |
| **HDG** | HE | **heading_mag** | **heading_mag_deg**; deviation; dev dir; mag_var; var dir |
| **HDM** | HE | **heading_mag** | **heading_mag_deg**; `M` |

Formatting invariants: UTC `hhmmss.ss` (zero-padded); RMC date is `ddmmyy` (2-digit year) while ZDA is
`dd,mm,yyyy` (4-digit year); speed is **knots** for RMC/VTG(N) but **km/h** for VTG(K); empty fields are
adjacent commas. **RMC/VTG read `cog_deg`; HDT/HDG/HDM read `heading_*` — never cross-wire.**

## AIS (via `pyais`)

Encode with `pyais.encode.encode_dict(data, radio_channel="A"|"B", talker_id="AIVDM"|"AIVDO")` →
returns a **list** of `!AIVDM,...` lines (multi-fragment auto-split; index `[0]` for single). Pass
coordinates/speed/course in **real units** (deg, knots, deg) — pyais scales internally.

- **Class A** targets/own-ship → **Type 1/2/3** position report. **Class B** → **Type 18**.
- **Type 5** (static & voyage) — send about **every 6 minutes**, staggered so it doesn't crowd position
  reports; it is a 2-fragment burst (emit both).
- `talker_id="AIVDO"` = own-ship; `"AIVDM"` = other vessels/targets.
- **Radio channel A/B** should alternate (`radio_channel`).
- **"Not available" sentinels:** heading `511`, course `360.0`, speed `1023`, rate-of-turn `-128`,
  lat `91`, lon `181`.

Type 1 dict:
```python
{ "type": 1, "mmsi": 366000001, "status": 0, "turn": 0, "speed": sog_kn,
  "accuracy": 0, "lon": lon, "lat": lat, "course": cog_deg,
  "heading": int(heading_true_deg), "second": utc.second, "maneuver": 0, "raim": 0, "radio": 0 }
```

## Baud budget guard

At 8N1, each character = **10 bits**, so a line runs at `baud/10` chars/s. For a port, sum the byte cost
of all enabled sentences × their rates and **warn/refuse when the total exceeds 80% of `baud/10`**:

```
port_load_cps = Σ_over_emit ( (avg_sentence_len + 2_for_CRLF) * rate_hz )
guard: port_load_cps <= 0.80 * (baud / 10)
```

Reference: at 4800 baud (~480 cps), `HDG`+`HDT` @10 Hz ≈ 400 cps ≈ **83%** — over the guard. Prefer
HDT-only @10 Hz, or both @≤5 Hz, or raise that port's baud.
