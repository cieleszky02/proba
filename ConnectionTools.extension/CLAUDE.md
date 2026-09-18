ConnectionTools – pyRevit extension
===================================

Goal
----
A pyRevit push button ("Connection Section") for looking at how two building components meet:

1. The user picks the FIRST component (floor, wall, roof, ceiling, beam, column, or foundation).
2. The script finds every component that shares a boundary with it and offers only those as choices for the SECOND component. The choice is made from a list dialog, or by clicking in the model, limited to the highlighted candidates.
3. A section is created through the middle of the connection, perpendicular to the joint line, so both components and their connection are visible. The section is named after both components, with an iteration number if the name already exists.

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
2. Face contact: A planar face of A and a planar face of B count as touching when their normals are opposite, they lie in the same plane (within `CONTACT_TOL_MM`), and sample points of one face project onto the other. Joined elements are detected this way, because Revit cuts their geometry so the faces touch.
3. Overlap: if no face contact is found but the elements clash (`ElementIntersectsElementFilter`), the intersection solid is used instead.
4. Result: each candidate gets a `Contact` object with `kind` (side/top/bottom/overlap), `origin` (middle of the contact region), `joint_dir` (horizontal direction along the joint), and `length`.

The section looks ALONG `joint_dir`, with its cut plane through `origin`. Element A is placed on the left side of the view.

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
* [ ] Floor next to floor (side contact): the section should be perpendicular to the shared edge.
* [ ] Floor on wall (top/bottom contact): the section should be perpendicular to the wall's length.
* [ ] Wall to wall at a corner or T-junction. This case is likely weak, because the section is vertical and a plan callout might suit it better.
* [ ] Sloped roofs or floors (a non-horizontal contact face).
* [ ] Performance on big models or elements with many faces.
* [ ] The candidate list labels are readable, and "pick" mode works.
* [ ] `ViewSection.CreateSection` is assumed to accept a `ViewFamily.Detail` type the same way it accepts `ViewFamily.Section` (both are `ViewSection` under the hood). Confirm "Detail" types create cleanly, not just "Section"/"Section Detail"/"Section Detail Number".
* [ ] "Create all N connections" batch mode: confirm section naming stays unique and non-conflicting when many sections are created in the same transaction, and that the view left active at the end is a sensible one.

Roadmap / ideas
-----------------

1. Let the user pick the section position along the joint instead of using the midpoint. Options include `PickPoint` on a work plane, or several sections along long joints.
2. Handle vertical joints (wall corners) with a horizontal plan callout instead of a section.
3. Add an options dialog (`pyrevit.forms`) for depth, width, scale, and template. Persist the settings with `script.get_config()`, and open settings with Shift+Click.
4. Support linked models (`RevitLinkInstance` and `ReferenceIntersector`), so components from structural or MEP links can be chosen.
5. Tag both elements and add material or layer tags in the new section. Optionally place the section on a "Connection details" sheet, reusing the RoomSheet button's sheet logic.
6. Move the geometry helpers into `lib/` and share them with other buttons.
