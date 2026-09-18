# RoomTools – pyRevit extension

## Goal

A pyRevit push button ("Room Sheet"). The user selects one or more rooms and
clicks. For each room, the script creates:

1. A sheet named after the room.
2. A floor plan or callout, cropped to the room (by rectangle or room outline).
3. A reflected ceiling plan (callout or standalone), cropped the same way.
4. Two sections through the room centre, one along X and one along Y.
5. A 3D view with a section box around the room.
6. Places all five views on the sheet.

All names derive from the room name. If any name or the sheet number already
exists, a shared iteration number is appended so every view of that room
carries the same suffix (e.g. `Kitchen 1 - Plan`, `Kitchen 1 - RCP`,
`Kitchen 1 - Section X`, and so on).

## Structure

```
RoomTools.extension/
  CLAUDE.md
  RoomTools.tab/Rooms.panel/RoomSheet.pushbutton/
    script.py      <- all logic, CONFIG block at the top
    bundle.yaml    <- button title/tooltip
    icon.png       <- (add a 32x32 / 96x96 png)
```

## Hard constraints

* Runtime is pyRevit IronPython 2.7, so no f-strings, no type hints, and no
  Python 3-only syntax. Use `"{}".format()`.
* Revit API internal units are feet. Convert with `mm()` (mm / 304.8).
* All model changes must happen inside a `DB.Transaction`. Views can't be
  activated inside a transaction.
* View names must be unique, and sheet numbers must be unique. Names must
  not contain `` \ : { } [ ] | ; < > ? ` ~ ``.
* Claude Code cannot run Revit. After each change, the user tests in Revit
  and pastes the pyRevit output or traceback back. Keep changes small and
  testable.
* Avoid `ElementId.IntegerValue`, which is deprecated in Revit 2024+. Use
  `.Value` only when needed, with a fallback.

## Status: needs verification in Revit

* [ ] Section box orientation. The code assumes `Max.Z` is the cut plane and
      `Min.Z` is the far clip, with the view looking along `-BasisZ`. Check
      that the X section looks north, the Y section looks west, and both
      cut through the room centre.
* [ ] Callout creation with `ViewSection.CreateCallout` using the FloorPlan
      and CeilingPlan types, plus the fallback to `ViewPlan.Create`.
* [x] Plan / section X / section Y / 3D / sheet, on a basic rectangular
      room — confirmed working in Revit.
* [ ] RCP (new): not yet tested in Revit.
* [ ] Room-shape crop, including the offset direction of
      `CurveLoop.CreateViaOffset`.
* [x] Curved room boundaries (round/bullnose rooms). Fixed: a round room's
      boundary has Arc segments, but `CropRegionShapeManager.SetCropShape`
      only accepts straight lines ("Boundary ... should represent one
      closed curve loop ... consisting of non-zero length straight
      lines"). `room_outline_curve_loop` now tessellates every boundary
      curve into a polyline before offsetting/cropping. Needs a retest on
      the round room that originally hit this.
* [ ] Iteration naming when running twice on the same room, and on two
      rooms with the same name.
* [ ] Layout on the sheet, with and without a title block.
* [ ] Rooms on levels with base offset or upper limit set.

## Roadmap / ideas

1. Rotated rooms: use an oriented bounding box, derived from the longest
   boundary segment, instead of the world-aligned bbox for the sections and
   crop.
2. Smarter viewport packing: read `Viewport.GetBoxOutline()` after
   placement and rearrange or rescale so views never overlap. Auto-pick a
   scale that fits.
3. Apply view templates per view type (config names), and hide the section
   box and crop region in the 3D view.
4. Options dialog (`pyrevit.forms`) for offset, scales, title block, and
   plan vs callout. Persist the choices with `script.get_config()` and
   Shift+Click to open settings.
5. Tag the room in the plan and add dimensions or room tags in the
   sections.
6. Refactor into a `lib/` module so other buttons can reuse naming and
   geometry helpers.
7. Optional CPython 3 engine compatibility. `ISelectionFilter` subclassing
   differs there.

## Implementation notes (for future sessions)

* `script.py` is organized as: CONFIG constants -> small pure helpers
  (`mm`, `sanitize_name`, naming/uniqueness) -> geometry helpers (crop
  shapes, section boxes) -> per-room `create_room_sheet()` -> `main()`.
* Naming/uniqueness (`resolve_names`) tries suffix `""`, `" 1"`, `" 2"`, ...
  and accepts the first suffix where the plan/section/3D view names *and*
  the sheet number are all free. Names reserved by rooms already processed
  in the same run are tracked locally so two rooms with the same name in
  one selection don't collide before the transaction commits.
* The room-shape crop offset direction is resolved defensively: the
  boundary loop's winding is computed with a shoelace sum instead of
  assumed, and the offset sign is flipped so it always grows the loop
  outward, regardless of which way Revit happens to wind the boundary.
* `CropRegionShapeManager.SetCropShape` rejects any curve that isn't a
  straight `Line` (confirmed by a real Revit error on a round room:
  "Boundary ... should represent one closed curve loop ... consisting of
  non-zero length straight lines"). `room_outline_curve_loop` therefore
  calls `Curve.Tessellate()` on every boundary segment (line or arc) and
  rebuilds the loop from the resulting points before offsetting, so a
  curved room gets a polygon-approximated crop instead of failing.
  `Line.Tessellate()` just returns its own two endpoints, so this is a
  no-op for rectangular rooms.
* Section box construction places `Transform.Origin` at the room's 3D
  bbox centre and sets local `Max.Z = 0`, so the cut plane sits exactly at
  the room centre; `Min.Z` is negative and extends past the room's far
  side by `SECTION_DEPTH_OFFSET_MM`. This is the piece most likely to need
  correction after a real Revit test — see the status checklist above.
* Sheet layout is a plain grid (`grid_centers(area_min, area_max, rows,
  cols)`) computed from the placed title block's bounding box (or a
  hard-coded fallback area when there is no title block). The room sheet
  uses a 2x3 grid for its 5 views (plan / RCP / 3D on top, the two
  sections below, one cell unused). It does not yet resolve overlaps —
  that's roadmap item 2.
* Plan and RCP share `create_cropped_plan_view()` (callout on the level's
  existing plan of that `ViewType`, `ViewPlan.Create` fallback) and
  `build_crop_loop()` for the crop shape; only the `ViewFamily` /
  `ViewType` passed in differ.
