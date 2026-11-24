# Replay

!!! note "New in FlightLens 1.1"
    This page was written before the code, as the specification Replay is
    built and tested against ([integration plan](replay/integration-plan.md),
    release 1.1). Try it with `flightlens demo` and the flight
    `quad-12-manoeuvres.bin`: a scripted 30° roll, 45° pitch, 90° turn and
    20 m square.

Replay plays an analyzed flight back: a 3D aircraft flies the logged path with
the logged attitude, while flight instruments, charts, the timeline and a map
of the path follow one clock. It works the same for PX4 (`.ulg`), ArduPilot
(`.bin`) and telemetry (`.tlog`) logs, fully offline, from data FlightLens has
already extracted, so there is nothing to set up.

## Opening it

- On any analyzed flight, open the **Replay** tab (next to Overview, Signals,
  Tuning, ...).
- From a finding or an event on the **Overview** or **Events** tab, use
  **Replay from here**: the replay opens a few seconds *before* it (5 s by
  default), so you see what led up to it.
- A link with a time, such as `/flights/<id>/replay?t=123.4`, opens the replay
  at 2:03.4. The address bar keeps the current time when you pause, so you can
  copy and share the exact moment.

The Replay tab loads its 3D code only when you open it; the rest of FlightLens
stays as fast as before.

## The screen

```
┌───────────────────────────────────────────┬──────────────────────┐
│ 3D view                                   │ Instruments (HUD)    │
│ aircraft, path, finding windows           │ Map of the path      │
├───────────────────────────────────────────┴──────────────────────┤
│ Charts: attitude · altitude · battery · motors (choose)          │
├──────────────────────────────────────────────────────────────────┤
│ ▶ 1×  Timeline: flight modes · events · findings · time cursor   │
├──────────────────────────────────────────────────────────────────┤
│ Events and findings (click to jump)                              │
└──────────────────────────────────────────────────────────────────┘
```

Panels can be hidden to give the 3D view more room; the layout is remembered
in this browser. On a narrow screen the panels stack: 3D view, instruments,
timeline, then the rest.

### 3D view

- **Ground:** a grid with distance labels (in metres), centred on the take-off
  point, with north marked. There is no map imagery or terrain: FlightLens
  never downloads anything.
- **Path:** the whole flight as a line; the part already flown is drawn
  stronger. A faint vertical line drops from the aircraft to the ground to
  show its height.
- **Findings on the path:** the stretch of path during a finding's time window
  is coloured by severity (problem, attention), so you can see *where* it
  happened.
- **Aircraft:** a simple model matching the vehicle type: quad, hexa, octo,
  fixed wing or VTOL (a generic model for other types). Its orientation is the
  logged attitude.
- **Cameras** (buttons, or keys 1-4):
    1. **Chase:** behind and above the aircraft, following its heading.
    2. **Orbit:** around the aircraft; drag to rotate, scroll to zoom.
    3. **Top:** straight down, north up, without perspective, like the map.
    4. **Cockpit:** from the aircraft, looking forward; the horizon tilts
       with the bank.

### Instruments (HUD)

Drawn like aircraft instruments, in the current theme:

- artificial horizon (roll and pitch), with the roll angle
- heading
- altitude above take-off, with altitude above sea level when it is logged
- ground speed, and airspeed when the aircraft logs it
- climb rate
- flight mode, armed state
- battery voltage and remaining charge
- GPS satellites and fix

A value that is not in the log shows as **—**, never as 0. The HUD also shows
the log's real data rate for position and attitude, so a slow telemetry log
is not mistaken for smooth data.

### Map

The existing flight-path map, as a small panel, with the aircraft's position
and heading. Its origin is the take-off point, the same as the 3D view, so the
two always agree. Click the path to jump there.

### Charts

The same charts as the Signals tab (attitude, altitude, battery and motors by
default; choose others from the signal list), with a moving time cursor. In
**follow** mode the charts show a window of time around the cursor that moves
with it; otherwise they show the whole flight. Click a chart to jump to that
time; zoom as on the Signals tab.

### Timeline

The existing timeline (flight modes, events, finding windows), with play
controls added. Clicking or dragging it moves the replay. **Shift-drag**
selects a range to loop.

### Events and findings list

Every event and finding in time order, with the finding's review status if an
engineer reviewed it. Click one to jump there (findings: a few seconds
before). The current one is highlighted as the replay passes it.

## Controls

| Action | Mouse | Keyboard |
|---|---|---|
| Play / pause | ▶ button | Space, or K |
| Slower / faster | speed menu (0.25× to 16×) | J / L |
| Step 1 s back / forward | | ← / → |
| Step 10 s | | Shift + ← / → |
| Step one sample | | , / . |
| Go to start / end | | Home / End |
| Loop a range | Shift-drag the timeline | |
| Camera | buttons | 1 – 4 |

Playback stops at the end of the log, or loops the selected range. Every
control can be reached with the keyboard and has a label for screen readers.

The replay's time is also the time cursor of the other tabs: pause on a
moment, switch to Signals or Overview, and they show the same moment.

## When data is missing

Replay never invents data. It shows what the log contains and says what it
lacks:

| The log has | Replay shows |
|---|---|
| position and attitude | the full replay |
| attitude, no position (indoor flight, no GPS) | the aircraft in the centre, turning with the logged attitude; no path; a note: *No position in this log* |
| position, no attitude | the path and a level aircraft model pointing along the direction of travel; a note: *No attitude in this log* |
| neither | the instruments and charts only, with a note |
| a gap in the data | below 1 s: interpolated. Longer: the aircraft stops at its last known state, the gap is marked on the timeline and the path is broken there; nothing is smoothed over |

Telemetry logs (`.tlog`) are recorded at 1-10 Hz by the ground station, often
with dropouts. They replay at their own rate, which can look less smooth than
an onboard log; the HUD shows the real rate.

## How accurate it is

- **Position** is the flight controller's estimated position (GPS position
  when no estimate is logged), in metres east, north and up from the
  take-off point. Near the take-off point this is accurate to a few
  centimetres per kilometre.
- **Height** in the 3D view is the height above take-off (the logged relative
  altitude), so the aircraft never appears underground because of GPS or
  barometer offsets. The HUD labels which altitude it shows.
- **Attitude** comes from the logged attitude quaternion when the log has one
  (PX4), otherwise from roll, pitch and yaw. It is interpolated in a way that
  has no flips near 90° pitch or at the 359° → 0° heading wrap.
- Everything is resampled to 25 samples a second for onboard logs, or the
  log's own rate (at most 10 Hz) for telemetry logs; the exact logged values
  are always on the Signals tab.

## Read-only and offline

- Replay works on a read-only server (`flightlens serve --read-only`), such as
  a public demo.
- It needs no network: no maps, fonts or scripts are loaded from anywhere
  else.

## Older flights

A flight analyzed before Replay existed shows as **outdated** (as when a check
changes). Re-analyze it, one flight or all of them from the library, to make
it replayable.

## Command line

```bash
flightlens replay-info flight.ulg
```

prints what a replay of the log would contain: duration, rate, where position
and attitude come from, and any gaps.
