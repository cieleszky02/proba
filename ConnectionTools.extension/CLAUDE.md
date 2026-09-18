ConnectionTools – pyRevit extension
===================================

Goal
----
A pyRevit push button ("Connection Section") for documenting how building components meet, in bulk, without picking elements one at a time:

1. The user filters by category, then multi-selects one or more family types actually placed in the model (`types_in_use` — a loaded-but-unplaced type isn't offered, e.g. every window type, or a specific wall type) — no element picking.
2. Every instance of those types in the model is checked against every other configured component. Connections are deduped and documented by TYPE PAIR, not by element instance: once one instance of "Door Type X" has a documented connection to "Wall Type Y", every other instance of that same pair is skipped, since it would produce an identical, redundant view (see "already documented" below).
3. Most connections (floor-to-floor, floor-on-wall, a door/window in its host wall, a stair landing on a floor, ...) only make sense as a vertical Section/Detail. A wall/column-to-wall/column side contact (a corner or T-junction) could be either a Section/Detail or a horizontal Plan callout, so the user is asked, per connection, which fits. Each connection gets its own callout even when the same object has several — an object with plan-type neighbours on multiple sides gets one callout per neighbour, not one combined callout.
4. The user picks which existing Section/Detail view type to create with, and — separately, since a template only applies to a matching view type — which view template (if any) to apply to the Section/Detail views and to the Plan callouts.
5. A view is created through the middle of each connection, perpendicular to the joint line (or, for a Plan job, a callout on the connection's level's Floor Plan view). Views are named after both components' types, with an iteration number if the name already exists, and tagged so the same pair is never offered or documented again.
6. Every view created in the run is placed on a new sheet named "<selected type(s)> - Connections", laid out in a simple grid.

See `docs/images/` for the user's sketches of the intended behaviour. Read them before changing geometry logic.

Structure
---------

```
ConnectionTools.extension/
  CLAUDE.md
  docs/images/                       <- sketches of intended behaviour
  ConnectionTools.tab/Details.panel/ConnectionSection.pushbutton/
    script.py      <- all logic, CONFIG block at the top
    bundle.yaml
    icon.png       <- (add one)

```

How "shares a boundary" is detected (script.py)
------------------------------------------------

1. Coarse filter: a `BoundingBoxIntersectsFilter` on A's bounding box plus a tolerance, limited to the configured categories.
2. Host: if one element is a `FamilyInstance` hosted directly on the other (a door/window in a wall, a railing on a stair), that's the contact — checked before any geometry, since Revit typically cuts an opening for the hosted element so its solid doesn't actually overlap or share a clean coplanar face with the host.
3. Face contact: A planar face of A and a planar face of B count as touching when their normals are opposite, they lie in the same plane (within `CONTACT_TOL_MM`), and sample points of one face project onto the other. Joined elements are detected this way, because Revit cuts their geometry so the faces touch.
4. Overlap: if neither of the above applies but the elements clash (`ElementIntersectsElementFilter`), the intersection solid is used instead.
5. Result: each candidate gets a `Contact` object with `kind` (side/top/bottom/overlap/host), `origin` (middle of the contact region), `joint_dir` (horizontal direction along the joint), and `length`.

The section looks ALONG `joint_dir`, with its cut plane through `origin`. Element A is placed on the left side of the view.

Every created view (Section, Detail, or Plan callout) is tagged with an Extensible Storage entity recording the two TYPE ids it documents (`tag_connection` / `_type_key`, which falls back to the element's own id if it has no type). On the next run, `documented_pairs()` reads those tags back from every `View` in the model and `main()` drops already-documented type pairs before offering them — so once a connection has a view, every other instance of that same type pair stops being offered (and re-documented), on this run and later ones, even in a different session. Deleting the view naturally un-documents the pair, since the tag lives on the view itself. `gather_unique_connections()` applies the same type-pair dedup within a single run, so e.g. five doors of the same type in the same wall type only produce one view, and a connection between two elements both in the selected type set is only found (and only asked about) once, not once from each side.

Because dedup is keyed by type rather than instance, changing which element pair happens to represent a given type pair (e.g. deleting the specific door instance a view was created for) does not undocument that type pair — only deleting the view itself does.

Section/Detail vs. Plan
------------------------

`is_ambiguous_orientation()` flags a connection as needing a per-connection choice when it is a `"side"` OR `"overlap"` contact (see below) between two elements both in `VERTICAL_CATEGORIES` (walls, columns, curtain panels/mullions) — the classic wall/column corner or T-junction, or a column too thick to share a clean coplanar face with the wall it meets (falls through face detection to the overlap fallback instead, but is still fundamentally the same kind of vertical-vertical meeting). Everything else is Section/Detail-only:

* `"top"`/`"bottom"` (e.g. floor on wall) and `"host"` (a door/window in its host) always read naturally as a vertical cut.
* A `"side"`/`"overlap"` contact where at least one element isn't in `VERTICAL_CATEGORIES` (e.g. two floors meeting at an edge) is also Section/Detail-only — the ambiguity is specifically about two vertical, full-height elements meeting edge-on, where looking down in plan shows the joint just as well as cutting across it.

`main()` prints one line per connection (element pair, contact kind, and whether it was flagged ambiguous) right before asking, so a wrong ambiguity call is visible directly in the output log instead of needing to be inferred from which views got created.

For a Plan job, `create_plan_callout()` finds the Floor Plan view already in the project for the connection's level (nearest `Level` by elevation to the contact origin) and adds a callout on it with `ViewSection.CreateCallout`, cropped around that one joint (using `SECTION_WIDTH_MM` centred on the contact origin), using that Floor Plan's own `ViewFamilyType` (so the callout's family always matches its owner view). If no Floor Plan view exists yet for that level, the job is skipped with a note in the output instead of failing the whole run. Each connection gets its own callout, even when the same object has several — this was tried the other way (one combined callout per object) and reverted, since the user wants a separate callout per connection.

Sheet
-----

`create_connection_sheet()` runs at the end of the same transaction that creates the views, after both loops: it creates a `ViewSheet` (using the first title block type found in the project, or none if the project has none), named "<selected_types_label> - Connections" (deduped like a view name, via `unique_sheet_name`), with an auto-generated sheet number (`unique_sheet_number`, "CT-1", "CT-2", ...), and places every view created in the run on it in a fixed grid (`SHEET_LAYOUT_COLUMNS` × `SHEET_LAYOUT_SPACING_MM`). The grid spacing is arbitrary and views vary a lot in size (a small door detail vs. a wide wall-corner plan callout), so a large view can still overlap its neighbours on the sheet — this needs a manual nudge in that case, not a real collision-aware layout.

Hard constraints
-----------------

* The runtime is pyRevit IronPython 2.7. Do not use f-strings or type hints; use `"{}".format()`.
* Revit's internal units are feet. Convert from millimetres with `mm()`.
* Model changes must happen inside a `DB.Transaction`. Set `uidoc.ActiveView` only after committing.
* Compare `ElementId`s with `==` or `str(id)`. Do not use `.IntegerValue`, which is deprecated in Revit 2024+.
* Claude Code cannot run Revit. The user tests each change and pastes back the output or traceback, so keep changes small and log useful info with `output.print_md`.

Status: needs verification in Revit
------------------------------------

* [ ] Section box convention: the code assumes the section box `Transform.Origin` sits on the cut plane (local `Z = 0`), with `Min.Z` a small near-clip buffer and `Max.Z` the far clip depth. Confirm the cut goes through the joint and does not look away from it.
* [x] `build_section_box()` grew the crop to the FULL combined bounding box of both elements with no upper bound, so a large/complex element (a multi-flight stair spanning a full floor height, a long wall) could balloon the crop far beyond the joint itself — seen as an oddly wide elevation-like section for a column-to-floor connection, and reported as a "weirdly placed" section for a stair connection. Added `SECTION_MAX_WIDTH_MM`/`SECTION_MAX_TOP_MM`/`SECTION_MAX_BOTTOM_MM`/`SECTION_MAX_DEPTH_MM` caps, clamped around the joint origin. Needs a real retest on the same stair case to confirm the crop is now reasonable and the joint is actually centred in it (the cap alone doesn't fix a wrong `joint_dir`/`origin` if the stair's many small tread/riser/stringer faces cause `_detect_face_contact` to pick an unexpected face pair — if the section still looks wrong after this, that's the next thing to check).
* [ ] Floor next to floor (side contact): the section should be perpendicular to the shared edge.
* [ ] Floor on wall (top/bottom contact): the section should be perpendicular to the wall's length.
* [ ] Sloped roofs or floors (a non-horizontal contact face).
* [ ] Performance on big models or elements with many faces (stairs especially — many small tread/riser/stringer solids) and on a category/type filter that matches a large number of instances (one `find_candidates` geometry pass per instance).
* [ ] New categories (stairs, railings, doors, windows, curtain wall panels/mullions): confirm `_detect_host_contact` correctly picks up door/window-in-wall and railing-on-stair connections, and that stair-to-floor landings still work through face/overlap detection.
* [x] `ViewSection.CreateSection` rejects `ViewFamily.Detail` directly ("The ViewFamilyType must be a Section ViewFamily" — confirmed in Revit). `resolve_creation_type()` now creates with any Section-family type and switches to the requested Detail type afterwards with `ChangeTypeId`, mirroring what Revit's own type selector allows on an existing section. Needs a real test to confirm `ChangeTypeId` itself succeeds across families.
* [ ] Extensible Storage "already documented" tracking (`tag_connections` / `documented_pairs`): confirm the schema round-trips correctly (`entity.Set[str]`/`Get[str]`, `AddSimpleField(name, str)`), that already-documented TYPE pairs actually disappear on a second run (not just the exact same element instances), and that this still works after closing and reopening the model. The schema GUID was changed when dedup moved from element ids to type ids (`CONNECTION_SCHEMA_GUID` = `992bc80e-...`), so views tagged by an earlier version of this file under the old GUID (`794ce64a-...`) are invisible to `documented_pairs()` now — a clean cutover, but it means a first run after upgrading may re-offer connections whose type pair was already documented under the old scheme. `tag_connections()` takes a list of pairs (currently always called with exactly one) rather than a single pair, so a future combined view could tag several — confirm the "|"-joined encoding round-trips correctly even with just one entry.
* [ ] **New, higher-risk since the last test:** the whole category/type picker flow (`choose_category`, `choose_types`, `instances_of_types`) — untested end to end.
* [ ] `types_in_use()`: confirm it correctly limits the type list to types with a placed instance (a loaded-but-unplaced type should not appear), and that this doesn't get noticeably slow in a category with very many instances (it scans every instance's `GetTypeId()` once up front).
* [ ] **New, higher-risk:** `create_plan_callout()` / `ViewSection.CreateCallout`. This is the least certain piece in the file: confirm it actually accepts a `ViewFamily.FloorPlan` type (not just Section/Detail) and produces a genuine, live plan-style callout rather than throwing or producing something else. If it turns out `CreateCallout` only accepts Section/Detail types, the wall/column-corner "Plan" option needs a different approach (e.g. a horizontal Section, which reuses the already-tested `build_section_box`/`CreateSection` path with a straight-down `view_dir` instead of a genuine plan).
* [x] `is_ambiguous_orientation()` was only checking `kind == "side"`, so a column-to-wall connection detected as `"overlap"` (the column thicker than the wall, no clean coplanar face) never triggered the Section-vs-Plan prompt — confirmed from a real run where it silently defaulted to Section every time. Broadened to also flag `"overlap"`. Needs a real test to confirm the prompt now appears for that case, and that the per-connection diagnostic line in `main()` matches what's expected for other connection kinds too.
* [ ] `VERTICAL_CATEGORIES`: confirm a wall-corner side contact is still flagged, and a floor-to-floor side/overlap contact is still not.
* [ ] `choose_view_template()`: confirm templates are correctly filtered by `ViewType` (Section/Detail/FloorPlan) and that applying one to a freshly created Section, Detail, and Plan callout each succeed.
* [ ] `create_plan_callout()`: confirm a seed element with several plan-type neighbours gets one separate, correctly-cropped callout per neighbour (not merged, not overlapping badly on the model itself).
* [ ] **New:** `create_connection_sheet()` / `ViewSheet.Create`, `DB.ElementId.InvalidElementId` as a title block (when the project has none), `Viewport.CanAddViewToSheet` / `Viewport.Create`, and the grid layout not throwing when two views happen to be large enough to overlap.

Roadmap / ideas
-----------------

1. Let the user pick the section position along the joint instead of using the midpoint. Options include `PickPoint` on a work plane, or several sections along long joints.
2. Add an options dialog (`pyrevit.forms`) for depth, width, and scale, alongside the existing view-type/template prompts. Persist the settings with `script.get_config()`, and open settings with Shift+Click.
3. Support linked models (`RevitLinkInstance` and `ReferenceIntersector`), so components from structural or MEP links can be chosen.
4. Tag both elements and add material or layer tags in the new view. A collision-aware sheet layout (using each view's actual Outline/crop size, or Revit's own auto-place-on-sheet logic if it exists) would replace the current fixed grid, which can overlap for large views.
5. Move the geometry helpers into `lib/` and share them with other buttons.
6. Let the category/type filter step (`choose_category`/`choose_types`) select across more than one category at once, instead of one category per run.
