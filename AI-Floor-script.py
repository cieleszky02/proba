# -*- coding: utf-8 -*-
# pylint: skip-file
# by Roman Golev

__title__ = "Floor\nFinishing"

# TODO: Create Shared Parameter if there is no such parameter in project

__doc__ = """Description:
Creates floors for selected rooms

Follow the steps:
Step 1 — Select room(s)
Step 2 — Select offset option and choose finishing type

Option "Consider Thickness" takes into account the floor's Thickness and shifts it down

Author: Roman Golev"""

__author__ = 'Roman Golev'


import sys
import Autodesk
from Autodesk.Revit.DB.Architecture import Room
from Autodesk.Revit.DB import *
from pyrevit import forms
from System.Collections.Generic import List
from core.selectionhelpers import get_selection_basic, CustomISelectionFilterByIdInclude, ID_ROOMS

doc = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument
uiapp = __revit__
app = uiapp.Application

MAX_HEAL_GAP = 1.0 / 12.0  # 1 inch, in feet (Revit's internal length unit)
MIN_CURVE_LENGTH = 1.0 / 24.0  # 1/2 inch - drop near-zero-length segments (e.g. curtain wall mullion artifacts)
EDGE_INSET = 1.0 / 96.0  # 1/8 inch - nudge the floor edge off any wall it would otherwise sit exactly on
FORCE_CLOSE_MIN_GAP = 1.0 / 1000.0  # ~1/80 inch - anything smaller is treated as already touching
CREATE_AWAY_OFFSET = Autodesk.Revit.DB.XYZ(2000.0, 2000.0, 0.0)  # feet - clear of real model geometry


def translate_curve_loop(curve_loop, vector):
    transform = Autodesk.Revit.DB.Transform.CreateTranslation(vector)
    curves = List[Autodesk.Revit.DB.Curve]()
    for curve in curve_loop:
        curves.Add(curve.CreateTransformed(transform))
    return Autodesk.Revit.DB.CurveLoop.Create(curves)


class SkipOnErrorPreprocessor(IFailuresPreprocessor):
    """Rolls the current transaction back automatically instead of showing Revit's
    interactive "cannot be ignored" error dialog (e.g. the "circular chain of
    references" error some curtain-wall-bounded rooms trigger), so a batch of rooms can
    run unattended and a problematic room is simply skipped and reported instead of
    blocking on a popup."""
    def PreprocessFailures(self, failures_accessor):
        for failure in failures_accessor.GetFailureMessages():
            if failure.GetSeverity() == FailureSeverity.Error:
                return FailureProcessingResult.ProceedWithRollBack
        return FailureProcessingResult.Continue


def to_curve_list(curves):
    curve_list = List[Autodesk.Revit.DB.Curve]()
    for curve in curves:
        curve_list.Add(curve)
    return curve_list


def loop_area_xy(curve_loop):
    points = []
    for curve in curve_loop:
        points.extend(curve.Tessellate())
    area = 0.0
    for i in range(len(points)):
        p1 = points[i]
        p2 = points[(i + 1) % len(points)]
        area += p1.X * p2.Y - p2.X * p1.Y
    return abs(area) / 2.0


def inset_curve_loop(curve_loop, inset):
    """Revit can try to auto-associate a new Floor's/Ceiling's sketch edges with a wall
    they land exactly on - particularly problematic for curtain walls, whose internal
    grid/panel/mullion hierarchy already has its own reference chain - which can trigger
    a "circular chain of references" regeneration error regardless of which Curve object
    was used to build the edge, since it's driven by the edge's geometric position, not
    its data provenance. Nudge the loop inward by a small, visually negligible amount so
    its edges are no longer exactly coincident with any bounding wall. Tries both offset
    directions (loop winding isn't guaranteed) and keeps whichever shrinks the area."""
    original_area = loop_area_xy(curve_loop)
    best = curve_loop
    best_area = original_area
    for offset in (inset, -inset):
        try:
            candidate = Autodesk.Revit.DB.CurveLoop.CreateViaOffset(
                curve_loop, offset, Autodesk.Revit.DB.XYZ.BasisZ)
            candidate_area = loop_area_xy(candidate)
            if candidate_area < best_area:
                best = candidate
                best_area = candidate_area
        except Exception:
            continue
    return best


def heal_curve_loop(curves):
    """Room boundary segments (especially on curved/curtain walls, or where they meet
    room separation lines) can include near-zero-length segments (e.g. at curtain wall
    mullions) and leave tiny gaps between consecutive curve endpoints, both of which
    CurveLoop.Create rejects - the former as degenerate geometry, the latter as "not
    contiguous". Drop the degenerate segments, then nudge each remaining curve's start
    point to the previous curve's actual end point when the gap is small; larger,
    genuine gaps are left alone so an actually-open boundary still fails instead of
    being papered over."""
    curves = [c for c in curves if c.Length > MIN_CURVE_LENGTH]
    healed = []
    for curve in curves:
        if healed:
            prev_end = healed[-1].GetEndPoint(1)
            gap = prev_end.DistanceTo(curve.GetEndPoint(0))
            if 0 < gap <= MAX_HEAL_GAP:
                end = curve.GetEndPoint(1)
                if isinstance(curve, Line):
                    curve = Line.CreateBound(prev_end, end)
                elif isinstance(curve, Arc):
                    curve = Arc.Create(prev_end, end, curve.Evaluate(0.5, True))
        healed.append(curve)
    if len(healed) > 1:
        loop_start = healed[0].GetEndPoint(0)
        last = healed[-1]
        gap = last.GetEndPoint(1).DistanceTo(loop_start)
        if 0 < gap <= MAX_HEAL_GAP:
            last_start = last.GetEndPoint(0)
            if isinstance(last, Line):
                healed[-1] = Line.CreateBound(last_start, loop_start)
            elif isinstance(last, Arc):
                healed[-1] = Arc.Create(last_start, loop_start, last.Evaluate(0.5, True))
    return healed


def build_boundary_curves(doc, boundary_segments):
    """Room boundary segments hosted by a curtain wall are reported as a jagged,
    panel-by-panel approximation of the wall's face rather than one clean curve -
    curtain walls don't have a simple planar face the way basic walls do. That
    approximation is what triggers Revit's "circular chain of references" error when
    it's reused to build a new Floor/Ceiling, since Revit tries to auto-associate the
    new sketch edges with the curtain wall's own grid/panel/mullion hierarchy. Where
    consecutive segments are hosted by the same curtain wall, collapse them into a
    single curve using that wall's own Location Curve instead; every other segment
    uses its normal boundary curve unchanged."""
    curves = []
    last_curtain_wall_id = None
    for segment in boundary_segments:
        element = doc.GetElement(segment.ElementId)
        if isinstance(element, Wall) and element.CurtainGrid is not None:
            if element.Id == last_curtain_wall_id:
                continue  # already represented by this wall's location curve
            location = element.Location
            if isinstance(location, LocationCurve):
                curves.append(location.Curve.Clone())
                last_curtain_wall_id = element.Id
                continue
        curves.append(segment.GetCurve().Clone())
        last_curtain_wall_id = None
    return curves


def force_close_loop(curves):
    """Last-resort fallback: bridges any remaining gap between consecutive curves with a
    straight connector line, guaranteeing CurveLoop.Create succeeds. Unlike
    heal_curve_loop, this never adjusts an existing curve's endpoints - it only inserts
    new connector segments - so it can safely close a gap of any size without distorting
    the rest of the shape. That's also its risk: if the room genuinely isn't enclosed at
    that point, this draws a straight edge across the gap instead of failing, so a room
    fixed this way is worth a visual double check."""
    closed = []
    count = len(curves)
    for i in range(count):
        curve = curves[i]
        closed.append(curve)
        next_curve = curves[(i + 1) % count]
        gap_start = curve.GetEndPoint(1)
        gap_end = next_curve.GetEndPoint(0)
        if gap_start.DistanceTo(gap_end) > FORCE_CLOSE_MIN_GAP:
            closed.append(Line.CreateBound(gap_start, gap_end))
    return closed


def build_curve_loop(doc, boundary_segments):
    """Builds the profile curve loop for a Floor from a room boundary loop, trying three
    tiers in order of preference and falling through on failure - so a fix aimed at one
    room's geometry can't regress a simpler room that never needed it:
    1. Curtain-wall Location Curve substitution + small-gap healing + a small inset off
       bounding walls (aimed at "circular chain of references" / "not contiguous").
    2. Plain per-segment boundary curves + small-gap healing only.
    3. Force-closing any remaining gap with a straight connector (last resort).
    Returns (curve_loop, was_force_closed)."""
    try:
        curves = heal_curve_loop(build_boundary_curves(doc, boundary_segments))
        loop = Autodesk.Revit.DB.CurveLoop.Create(to_curve_list(curves))
        return inset_curve_loop(loop, EDGE_INSET), False
    except Exception:
        pass

    try:
        curves = heal_curve_loop([segment.GetCurve().Clone() for segment in boundary_segments])
        return Autodesk.Revit.DB.CurveLoop.Create(to_curve_list(curves)), False
    except Exception:
        pass

    curves = force_close_loop([segment.GetCurve().Clone() for segment in boundary_segments])
    return Autodesk.Revit.DB.CurveLoop.Create(to_curve_list(curves)), True


def main():
    # Select rooms
    selobject = get_selection_basic(uidoc, CustomISelectionFilterByIdInclude(ID_ROOMS))
    selected_rooms = [doc.GetElement(sel) for sel in selobject if isinstance(doc.GetElement(sel), Room)]
    if not selected_rooms:
        forms.alert('Please select room', 'Create floor finishing')
        sys.exit()

    def make_opening(floor, boundary_segments):
        co_curves = Autodesk.Revit.DB.CurveArray()
        for segment in boundary_segments:
            co_curves.Append(segment.GetCurve().Clone())
        doc.Create.NewOpening(floor, co_curves, False)

    # Get floor types
    def collect_floor_types(doc):
        return FilteredElementCollector(doc) \
            .OfCategory(BuiltInCategory.OST_Floors) \
            .OfClass(FloorType) \
            .ToElements()

    floor_types = collect_floor_types(doc)
    if not floor_types:
        forms.alert('No floor types found in the project', 'Create floor finishing')
        sys.exit()

    floor_types_by_name = {
        ft.get_Parameter(BuiltInParameter.ALL_MODEL_TYPE_NAME).AsString(): ft
        for ft in floor_types
    }

    switches = ['Consider Thickness']
    cfgs = {'Consider Thickness': {'background': '0xFF55FF'}}
    selected_type_name, rswitches = forms.CommandSwitchWindow.show(
        sorted(floor_types_by_name.keys()), message='Select Option', switches=switches, config=cfgs)

    if selected_type_name is None:
        sys.exit()

    consider_thickness = rswitches['Consider Thickness']
    floor_type = floor_types_by_name[selected_type_name]
    floor_type_default_thickness = floor_type.get_Parameter(
        BuiltInParameter.FLOOR_ATTR_DEFAULT_THICKNESS_PARAM).AsDouble()

    room_boundary_options = Autodesk.Revit.DB.SpatialElementBoundaryOptions()
    room_boundary_options.SpatialElementBoundaryLocation = SpatialElementBoundaryLocation.Finish

    def make_floor(room):
        """Creates a Floor matching the room's footprint. Returns the new Floor,
        or None if the room has no usable boundary (unplaced/unenclosed room)."""
        room_offset = room.get_Parameter(BuiltInParameter.ROOM_LOWER_OFFSET).AsDouble()
        room_name = room.get_Parameter(BuiltInParameter.ROOM_NAME).AsString()
        room_number = room.Number

        all_boundaries = room.GetBoundarySegments(room_boundary_options)
        if not all_boundaries or not all_boundaries[0]:
            print("Skipped room '{}' {} - no boundary found (unplaced or unenclosed room)".format(
                room_name, room_number))
            return None

        floor_curves_loop, was_force_closed = build_curve_loop(doc, all_boundaries[0])
        if was_force_closed:
            print("Warning: room '{}' {} had an open boundary - force-closed it with a "
                  "straight edge, please double check the floor's shape there".format(
                      room_name, room_number))
        # Build the floor far from the room's actual location, then move it back into
        # place. Floor.Create appears to auto-associate a new sketch edge with whatever
        # model geometry (e.g. a curtain wall) is already nearby at creation time, which
        # is what triggers the "circular chain of references" error on some rooms;
        # creating it with nothing nearby avoids that, and MoveElement doesn't re-run
        # that association logic the way Floor.Create's own placement does.
        curve_loops = List[Autodesk.Revit.DB.CurveLoop]()
        curve_loops.Add(translate_curve_loop(floor_curves_loop, CREATE_AWAY_OFFSET))

        f = Autodesk.Revit.DB.Floor.Create(doc, curve_loops, floor_type.Id, room.LevelId)
        Autodesk.Revit.DB.ElementTransformUtils.MoveElement(doc, f.Id, CREATE_AWAY_OFFSET.Negate())

        # Position the floor relative to the room's base offset
        if consider_thickness:
            height_offset = room_offset
        else:
            height_offset = room_offset + floor_type_default_thickness
        f.get_Parameter(BuiltInParameter.FLOOR_HEIGHTABOVELEVEL_PARAM).Set(height_offset)

        # Here we can add custom parameters for floors
        comments_param = f.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        if comments_param and not comments_param.IsReadOnly:
            comments_param.Set('Floor Finishing - Room {} {}'.format(room_number, room_name))

        # Cut openings for any interior boundary loops (islands within the room)
        for inner_boundary in all_boundaries[1:]:
            make_opening(f, inner_boundary)

        return f

    tg = Autodesk.Revit.DB.TransactionGroup(doc, "Make Floor finishing")
    tg.Start()
    skipped_rooms = 0
    try:
        for room in selected_rooms:
            room_label = "'{}' {}".format(
                room.get_Parameter(BuiltInParameter.ROOM_NAME).AsString(), room.Number)
            t = Autodesk.Revit.DB.Transaction(doc, 'Create Floor Finishing')
            t.Start()
            failure_options = t.GetFailureHandlingOptions()
            failure_options.SetFailuresPreprocessor(SkipOnErrorPreprocessor())
            t.SetFailureHandlingOptions(failure_options)
            try:
                new_floor = make_floor(room)
                if new_floor is None:
                    skipped_rooms += 1
                    if not t.HasEnded():
                        t.RollBack()
                else:
                    status = t.Commit()
                    if status != Autodesk.Revit.DB.TransactionStatus.Committed:
                        skipped_rooms += 1
                        print("Failed to create floor for room {}: transaction {}".format(
                            room_label, status))
            except Exception as ex:
                if not t.HasEnded():
                    t.RollBack()
                skipped_rooms += 1
                print("Failed to create floor for room {}: {}".format(room_label, ex))
        tg.Assimilate()
    except Exception:
        tg.RollBack()
        raise

    if skipped_rooms > 0:
        forms.toaster.send_toast(
            '{} room(s) were skipped - see output window for details'.format(skipped_rooms),
            title=None, appid=None, icon=None, click=None, actions=None)


if __name__ == '__main__':
    main()
