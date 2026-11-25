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

## Video

Attach the flight's video (onboard camera, or a recording from the ground)
in the **Video** panel; several per flight are fine, and you choose which one
is shown.

- **Formats:** MP4 (H.264) and WebM, the formats browsers play. Anything else
  is refused with a conversion command, for example
  `ffmpeg -i input.mov -c:v libx264 -c:a aac output.mp4`. The limit is 4 GB.
- **The original is kept as uploaded**, read-only, with its SHA-256, like the
  log. Removing a video deletes the file.
- **Sync once.** One number links the two clocks: *log time = offset + rate ×
  video time*. Press **Sync…**:
    - find a sharp moment in both (take-off, a quick turn, landing), put the
      video on it (its own controls, or **◀ frame / frame ▶**) and the replay
      on it (timeline, arrow keys), and press **Add sync point here**. One
      point sets the offset; two or more, far apart, also correct a camera
      clock that runs fast or slow. Each point shows how far it is from the
      fit, and points that cannot belong together are flagged;
    - or nudge the offset by ±1 s, ±0.1 s or one frame until they match;
    - or start from the **camera's clock** (the time written into the file),
      offered as a first guess: cameras are often seconds off.
  The panel always says how the video was synced and how closely.
- **Then it follows the replay:** it plays at the replay's speed, is corrected
  whenever it drifts more than 0.1 s from the clock, and seeks with the
  timeline. Before or after the recording the panel says *No video at this
  time*; the timeline shows which part of the flight has video (a bar under
  the strip). Sound is off until you turn it on.

## Incident mode

On a crash or a Problem finding, **Investigate** (Overview tab) opens the
replay a minute before it, loops the critical window (15 s before to 5 s
after) and shows the incident with the likely causes FlightLens linked to it,
in time order: click one to see it. **Start a debrief** opens the
[Debrief](debrief.md) tab with the incident and its causes already written in.

## Two flights together

On the **Compare** page, **Replay both in 3D** puts the two flights in one
scene on one clock: flight A solid, flight B dashed, each drawn from its own
take-off point, so two flights of the same route overlay even from different
fields. Choose for each flight the moment that becomes t = 0: take-off
(default), arming, the log start, or any mode change or event. The cameras
are orbit, top, chase A and chase B; a table compares height, speed,
attitude, heading and mode at the same aligned time, and the charts overlay
roll, height and battery.

## Notes

Press **N** to write a note at the current moment; see
[Notes and debrief](debrief.md).

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
| Write a note here | Note button | N |

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
  a public demo; videos play there too, but cannot be added, synced or removed.
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
