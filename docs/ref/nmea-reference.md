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
| **THS** | HE | **heading_true** | **heading_true_deg**; mode `A` (autonomous) / `S` (simulator) / `V` (invalid) |

Formatting invariants: UTC `hhmmss.ss` (zero-padded); RMC date is `ddmmyy` (2-digit year) while ZDA is
`dd,mm,yyyy` (4-digit year); speed is **knots** for RMC/VTG(N) but **km/h** for VTG(K); empty fields are
adjacent commas. **RMC/VTG read `cog_deg`; HDT/HDG/HDM/THS read `heading_*` — never cross-wire.** `THS`
rides the **heading channel** (talker `HE`) alongside HDT/HDG/HDM — it is the modern true-heading-with-
status sentence, mode `S` while simulating.

## Instrument channel sentences (talker `II`)

The optional instrument channel emits the motion/environment suite off the shared vessel state. Fields
between `$II<type>,` and `*hh`:

| Sentence | Reads | Fields (in order) |
|---|---|---|
| **VHW** | stw / heading | heading_true,`T`; heading_mag,`M`; **stw_kn**,`N`; stw_kmh,`K` |
| **DPT** | depth | **depth_m** (below transducer); transducer_offset_m; max_range_m |
| **DBT** | depth | depth_ft,`f`; **depth_m**,`M`; depth_fathoms,`F` |
| **MWV** | **apparent** wind | wind_angle_deg (0–359, relative to bow); reference `R`; wind_speed; units `N`; status `A` |
| **MWD** | **true** wind | wind_dir_true,`T`; wind_dir_mag,`M`; wind_speed_kn,`N`; wind_speed_ms,`M` |
| **ROT** | rate-of-turn | **rot_dpm** (deg/min, `−` = bow to port); status `A` |
| **XDR** | pitch / roll | per measurement: type `A` (angular); **value_deg**; `D`; id (`PTCH` / `ROLL`) |
| **RSA** | rudder | **rudder_angle_deg** (starboard), status `A`; port_angle, status `A` |
| **VDR** | set / drift | **set_deg**,`T`; set_mag,`M`; **drift_kn**,`N` |
| **$PASHR** | attitude | utc `hhmmss.sss`; heading_true,`T`; **roll_deg**; **pitch_deg**; heave_m; roll_stdev; pitch_stdev; heading_stdev; gps_qual; ins_status |

`$PASHR` is a proprietary attitude sentence (leading `$P…`, no talker split); pynmea2 doesn't model it, so
it is hand-built and checksummed with the XOR helper above. `pitch_deg` / `roll_deg` are the sea-state
motion-model outputs (see architecture.md), never a stored-flat pair.

### Apparent wind is computed on read, not stored

Vessel state holds only **true** wind — `wind_speed_kn` and `wind_dir_deg` (direction is **FROM**,
referenced to **true** north). Both the apparent and the true forms are derived at emit time so they can
never disagree:

- **MWV** (reference `R`, apparent) = the vector sum of true wind and the vessel's **motion over ground**,
  computed from **SOG at COG** — *not* heading. The boat's track (COG) differs from where the bow points
  (heading) under set/drift and leeway, and apparent wind follows the track, so the derivation uses the
  COG vector, not the heading vector.
- **MWD** (true) reports the stored true wind; its **magnetic** direction is filled from
  `mag_variation_deg`, keeping true/magnetic consistent exactly as HDT/HDG do for heading.

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

## Time and position: single-source Time Authority

Four sentences carry a clock on the GPS bus, and they do **not** all carry the same thing:

| Sentence | Time content |
|---|---|
| **RMC** | time-of-day (`hhmmss.ss`) **+ date** (`ddmmyy`) — the canonical fix time |
| **ZDA** | full date/time — time-of-day + `dd,mm,yyyy` (4-digit year) + local-zone offset |
| **GGA** | time-of-day only (no date) |
| **GLL** | time-of-day only (no date) |

Because a consumer can pick up its clock from any of these, they must all agree. The program derives every
timestamp on the GPS output from **one** authority, on the priority chain:

```
GPS input → SAT-compass input → NTP → system
```

"NTP" here means **reading the locally chrony-disciplined system clock** — no network query, so it adds no
latency and holds the no-runtime-internet-dependency posture. Position and time are bound to the **same**
active source (see architecture.md → priority-routed passthrough), so a fix and its timestamp can never
come from two different clocks.

### The ZDA carve-out — never emit a divergent pair

The program **always** provides a `ZDA` on the GPS output. When the active priority-1/2 source sends `RMC`
but no `ZDA`, `ZDA` is **synthesized from that same source's `RMC` time** (identical time, add-only) — it
is never minted from an independent clock. In passthrough the program does **not** inject its own
NTP-derived `ZDA`.

**The evidence rationale:** **no marine device cross-validates `ZDA` against `RMC`/`GGA`.** A divergent
`ZDA`/`RMC` time pair on one bus therefore fails *silently* — clock jitter, wrong timestamps, and
source-flapping downstream, with nothing to flag it. Binding position and time to a single source, and
deriving any missing `ZDA` from that source's own `RMC`, guarantees the pair always agrees rather than
trusting a consumer to catch a mismatch it structurally cannot see.

## Baud budget guard

At 8N1, each character = **10 bits**, so a line runs at `baud/10` chars/s. For a port, sum the byte cost
of all enabled sentences × their rates and **warn/refuse when the total exceeds 80% of `baud/10`**:

```
port_load_cps = Σ_over_emit ( (avg_sentence_len + 2_for_CRLF) * rate_hz )
guard: port_load_cps <= 0.80 * (baud / 10)
```

Reference: at 4800 baud (~480 cps), `HDG`+`HDT` @10 Hz ≈ 400 cps ≈ **83%** — over the guard. Prefer
HDT-only @10 Hz, or both @≤5 Hz, or raise that port's baud.
