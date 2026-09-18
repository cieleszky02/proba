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
7. Before any of this, one dialog with a dropdown per view type (Floor
   Plan, Ceiling Plan, Section, 3D) to optionally apply a view template.
   Each dropdown only lists templates whose own `ViewType` matches, and
   the chosen template (if any) is applied to every room's views of that
   type. Cancelling the dialog aborts the whole run.

All names derive from the room name. If any name or the sheet number already
exists, a shared iteration number is appended so every view of that room
carries the same suffix (e.g. `Kitchen 1 - Plan`, `Kitchen 1 - RCP`,
`Kitchen 1 - Section X`, and so on).

## Structure

```
RoomTools.extension/
  CLAUDE.md
  RoomTools.tab/Rooms.panel/RoomSheet.pushbutton/
    script.py                <- all logic, CONFIG block at the top
    bundle.yaml               <- button title/tooltip
    icon.png                  <- (add a 32x32 / 96x96 png)
    ViewTemplatePicker.xaml   <- the pre-run "apply view templates" dialog
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

* [x] Section box position. Confirmed fixed in Revit: cut lines now pass
      through the room centroid (both on a round room). `Min.Z` is pinned
      to (almost) the transform's origin, `Max.Z` is the far bound; see
      implementation notes below for why it's `Min.Z` and not `Max.Z`.
      Not separately re-confirmed: whether each section's actual content
      shows the intended cardinal side (X north, Y west) — if that's ever
      found to be backwards, negate `basis_z` for the affected axis in
      `create_room_section` (do not touch Min.Z/Max.Z again).
* [ ] Callout creation with `ViewSection.CreateCallout` using the FloorPlan
      and CeilingPlan types, plus the fallback to `ViewPlan.Create`.
* [x] Plan / RCP / 3D / sheet, on a basic rectangular room — confirmed
      working in Revit.
* [ ] RCP: confirmed callout-vs-crop-shape mechanics work on a round room;
      not yet tested on a room with no existing ceiling plan for its level
      (the `ViewPlan.Create` fallback path).
* [x] Room-shape crop, including the offset direction of
      `CurveLoop.CreateViaOffset`. Tessellation for curved boundaries
      (below) is confirmed working. `USE_ROOM_SHAPE_CROP` now defaults to
      `False` (always a rectangle) per explicit request — the room-outline
      crop is still implemented and available by flipping that flag back.
* [x] Curved room boundaries (round/bullnose rooms). Fixed and confirmed:
      a round room's boundary has Arc segments, but
      `CropRegionShapeManager.SetCropShape` only accepts straight lines
      ("Boundary ... should represent one closed curve loop ...
      consisting of non-zero length straight lines").
      `room_outline_curve_loop` tessellates every boundary curve into a
      polyline before offsetting/cropping. Only exercised via
      `USE_ROOM_SHAPE_CROP = True`, now off by default (see above).
* [ ] Iteration naming when running twice on the same room, and on two
      rooms with the same name.
* [ ] Layout on the sheet, with and without a title block.
* [ ] Rooms on levels with base offset or upper limit set.
* [~] View template picker dialog. Dialog itself confirmed showing up at
      the right time (after room selection, before anything is created).
      BUG found and fixed: the Section dropdown came up empty despite the
      project having plenty of Section-type templates (confirmed via
      Revit's own "Assign View Template" dialog) -- filtering candidate
      templates by exact `template.ViewType == DB.ViewType.Section` found
      none of them, for a reason not fully pinned down. Switched to
      Revit's own authoritative `View.IsValidViewTemplate(templateId)`,
      tested against any existing non-template view of that type already
      in the project (`find_any_view_of_type`) -- your prior test runs
      already left several Section views in the document, so this has a
      view to test against. Falls back to the old exact-`ViewType` match
      only when no view of that type exists yet anywhere in the project.
      NOT yet re-verified: confirm the Section dropdown (and, for good
      measure, Plan/Ceiling/3D too, in case they had the same latent bug
      without it being as visible) now lists the applicable templates, and
      that OK still lands the right template on each created view.

## Roadmap / ideas

1. Rotated rooms: use an oriented bounding box, derived from the longest
   boundary segment, instead of the world-aligned bbox for the sections and
   crop.
2. Smarter viewport packing: read `Viewport.GetBoxOutline()` after
   placement and rearrange or rescale so views never overlap. Auto-pick a
   scale that fits.
3. ~~Apply view templates per view type~~ -- done via the pre-run dialog
   (`choose_view_templates` / `ViewTemplatePicker.xaml`). Still open: hide
   the section box and crop region in the 3D view (a template can already
   do this if it controls those visibility settings, but there's no
   built-in fallback if the user picks `<None>` for 3D).
4. Options dialog (`pyrevit.forms`) for offset, scales, title block, and
   plan vs callout, along the same lines as the view template picker.
   Persist the choices (view templates included) with `script.get_config()`
   and Shift+Click to open settings, instead of asking every run.
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
  bbox centre. Empirically (see status checklist), Revit draws the parent
  plan's section cut line at local `Min.Z`, not `Max.Z` — so `Min.Z` is
  pinned to `-SECTION_NEAR_OFFSET_MM` (a small buffer, not exactly `0`, to
  avoid a degenerate zero-thickness bound) and `Max.Z` is the positive far
  bound, `half_depth + SECTION_DEPTH_OFFSET_MM` beyond the room's far
  edge. `BasisX`/`BasisY`/`BasisZ` (and therefore which cardinal direction
  each section looks) were left unchanged by this fix — only *where the
  cut line is drawn* was addressed, not *which side of the room is
  visible content*. Those could turn out to be inconsistent with each
  other (see status checklist); if so, the fix is to negate `basis_z` for
  the affected axis, not to touch Min.Z/Max.Z again.
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
* `choose_view_templates()` runs once, before the transaction (dialogs and
  transactions don't mix well, and there's no reason to ask once per
  room). Candidate templates are filtered per view type with
  `View.IsValidViewTemplate(templateId)` -- Revit's own authoritative
  compatibility check -- called on an existing non-template view of that
  type already in the project (`find_any_view_of_type`), not on the
  templates' own `ViewType` property. That was the original approach and
  it under-matched in Revit (confirmed: a project with several Section
  templates got an empty Section dropdown), for a reason not fully
  diagnosed -- possibly template `ViewType` not mapping 1:1 to view
  `ViewType`, possibly something else. `IsValidViewTemplate` needs an
  actual view instance to call it on, which is why `find_any_view_of_type`
  looks for one already sitting in the project rather than creating one
  (that would need a transaction, and the dialog runs before the
  transaction starts). Falls back to the old exact-`ViewType` match only
  when the project has no view of that type at all yet -- a fresh project
  before this tool has ever run once. Section X and Section Y share one
  dropdown/key (`'section'`) since both are `ViewType.Section`. Returns
  `{}` (skip the dialog silently) if the project has zero templates across
  all four types, or `None` if the user hits Cancel — `main()`
  distinguishes those and aborts the whole run only on the latter.
  `ViewTemplatePickerWindow` is a `pyrevit.forms.WPFWindow` loading
  `ViewTemplatePicker.xaml`; its `Name="..."` elements (not `x:Name`)
  become plain attributes on `self` (`self.plan_combo`, etc.) via
  IronPython's `wpf.LoadComponent`, and the XAML's `Click="ok_click"` /
  `Click="cancel_click"` bind directly to the matching methods on this
  class.
