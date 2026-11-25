# Notes and debrief

!!! note "New in FlightLens 1.3"
    Part of the [Replay & Debrief integration plan](replay/integration-plan.md), release 1.3.

FlightLens finds problems automatically; a debrief is what the engineers
conclude about them. While replaying a flight you write **notes** at the
moments that matter; on the **Debrief** tab you write the flight's summary,
conclusion and follow-up actions; the **debrief report** prints it all next to
the automatic health report.

## Notes

In the [Replay](replay.md) tab:

- Press **N** (or the **Note at …** button) to write a note at the current
  moment; the replay pauses. With a range looped on the timeline
  (shift-drag), **Note for …** writes a note for that range.
- Tag it: *pilot, mechanical, software, weather, sensor, procedure, good
  practice*, or your own tags.
- Your name is remembered in this browser. FlightLens has no user accounts:
  it is a local tool.
- Each note remembers the camera you were using; clicking it jumps to its
  moment and restores that view.
- Notes show as purple marks on the timeline and as diamonds on the 3D path.
  Edit or delete them in the Notes panel.

Notes and [reviews](how-it-works.md) are different things and stay separate:

| | Reviews | Notes |
|---|---|---|
| About | one automatic finding | anything, at a moment or a range |
| Content | confirmed, false alarm or accepted, and a short note | free text and tags |
| Changes the verdict | never | never |
| Exported as validation labels | yes | no |

## Debrief

The **Debrief** tab of each flight has:

- **status**: *open* while the investigation goes on, *closed* when it is
  settled;
- **summary** (what happened) and **conclusion** (why, as far as the evidence
  shows);
- **actions**: a short list of follow-ups;
- the author and when it was last saved.

Beside it are the notes from the replay and the findings with their review
status, each a link back into the replay a few seconds before.

The **Library** marks flights *Debrief open* or *Debriefed*, with their number
of notes, and filters by it. The **Aircraft** page shows each flight's debrief
status in its history.

## Debrief report

**Debrief report** (on the Debrief tab) opens a printable page, in the style of
the health report; print it to save a PDF. It contains:

1. the flight's facts (aircraft, date, time in the air, distance, altitude,
   speed, battery) and the automatic verdict;
2. the debrief: status, summary, conclusion, actions, author;
3. one timeline in time order: flight modes, events, findings with their
   review, and the notes (numbered);
4. the track seen from above, north up, in metres from the take-off point,
   with the numbered notes and the findings' stretches on it;
5. provenance: FlightLens version, the log's SHA-256, each video's SHA-256 and
   how it was synced.

## Keeping them

Notes and debriefs are part of every backup (`flightlens backup`, with or
without `--no-analysis` and `--no-video`). `flightlens prune` keeps flights
with notes or a debrief, as it keeps reviewed flights; `--include-reviewed`
removes them too. On a read-only server notes and debriefs can be read and the
report printed, but nothing can be changed.
