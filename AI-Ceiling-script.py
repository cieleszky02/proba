# -*- coding: utf-8 -*-
# pylint: skip-file
# by Roman Golev

__title__ = "Ceiling\nFinishing"
__doc__ = """Description:
Creates ceilings for selected rooms

Follow the steps:
Step 1 — Select room(s)
Step 2 — Select offset option and choose finishing type

Option "Consider Thickness" takes into account the ceiling's Thickness and shifts it down

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
EDGE_INSET = 1.0 / 96.0  # 1/8 inch - nudge the ceiling edge off any wall it would otherwise sit exactly on
FORCE_CLOSE_MIN_GAP = 1.0 / 1000.0  # ~1/80 inch - anything smaller is treated as already touching


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
    """Builds the profile curve loop for a Ceiling from a room boundary loop, trying
    three tiers in order of preference and falling through on failure - so a fix aimed
    at one room's geometry can't regress a simpler room that never needed it:
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
        forms.alert('Please select room(s)', 'Create ceiling finishing')
        sys.exit()

    def make_opening(ceiling, boundary_segments):
        co_curves = Autodesk.Revit.DB.CurveArray()
        for segment in boundary_segments:
            co_curves.Append(segment.GetCurve().Clone())
        doc.Create.NewOpening(ceiling, co_curves, False)

    # Get ceiling types
    def collect_ceiling_types(doc):
        return FilteredElementCollector(doc) \
            .OfCategory(BuiltInCategory.OST_Ceilings) \
            .OfClass(CeilingType) \
            .ToElements()

    ceiling_types = collect_ceiling_types(doc)
    if not ceiling_types:
        forms.alert('No ceiling types found in the project', 'Create ceiling finishing')
        sys.exit()

    ceiling_types_by_name = {
        ct.get_Parameter(BuiltInParameter.ALL_MODEL_TYPE_NAME).AsString(): ct
        for ct in ceiling_types
    }

    switches = ['Consider Thickness']
    cfgs = {'Consider Thickness': {'background': '0xFF55FF'}}
    selected_type_name, rswitches = forms.CommandSwitchWindow.show(
        sorted(ceiling_types_by_name.keys()), message='Select Option', switches=switches, config=cfgs)

    if selected_type_name is None:
        sys.exit()

    consider_thickness = rswitches['Consider Thickness']
    ceiling_type = ceiling_types_by_name[selected_type_name]
    is_compound_ceiling = ceiling_type.FamilyName == 'Compound Ceiling'
    ceiling_type_thickness = None
    if is_compound_ceiling:
        ceiling_type_thickness = ceiling_type.get_Parameter(BuiltInParameter.CEILING_THICKNESS).AsDouble()

    room_boundary_options = Autodesk.Revit.DB.SpatialElementBoundaryOptions()
    room_boundary_options.SpatialElementBoundaryLocation = SpatialElementBoundaryLocation.Finish

    def make_ceiling(room):
        """Creates a Ceiling matching the room's footprint, at the room's height (underside of
        the room's upper limit). Returns the new Ceiling, or None if the room has no usable
        boundary (unplaced/unenclosed room)."""
        room_height = room.get_Parameter(BuiltInParameter.ROOM_HEIGHT).AsDouble()
        room_name = room.get_Parameter(BuiltInParameter.ROOM_NAME).AsString()
        room_number = room.Number

        all_boundaries = room.GetBoundarySegments(room_boundary_options)
        if not all_boundaries or not all_boundaries[0]:
            print("Skipped room '{}' {} - no boundary found (unplaced or unenclosed room)".format(
                room_name, room_number))
            return None

        ceiling_curves_loop, was_force_closed = build_curve_loop(doc, all_boundaries[0])
        if was_force_closed:
            print("Warning: room '{}' {} had an open boundary - force-closed it with a "
                  "straight edge, please double check the ceiling's shape there".format(
                      room_name, room_number))
        curve_loops = List[Autodesk.Revit.DB.CurveLoop]()
        curve_loops.Add(ceiling_curves_loop)

        c = Autodesk.Revit.DB.Ceiling.Create(doc, curve_loops, ceiling_type.Id, room.LevelId)

        # Position the ceiling relative to the room's height (underside of the room)
        if not consider_thickness and is_compound_ceiling:
            height_offset = room_height + ceiling_type_thickness
        else:
            height_offset = room_height
        c.get_Parameter(BuiltInParameter.CEILING_HEIGHTABOVELEVEL_PARAM).Set(height_offset)

        # Here we can add custom parameters for ceilings
        comments_param = c.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        if comments_param and not comments_param.IsReadOnly:
            comments_param.Set('Ceiling Finishing - Room {} {}'.format(room_number, room_name))

        # Cut openings for any interior boundary loops (islands within the room)
        for inner_boundary in all_boundaries[1:]:
            make_opening(c, inner_boundary)

        return c

    tg = Autodesk.Revit.DB.TransactionGroup(doc, "Make Ceiling finishing")
    tg.Start()
    skipped_rooms = 0
    try:
        for room in selected_rooms:
            room_label = "'{}' {}".format(
                room.get_Parameter(BuiltInParameter.ROOM_NAME).AsString(), room.Number)
            t = Autodesk.Revit.DB.Transaction(doc, 'Create Ceiling Finishing')
            t.Start()
            failure_options = t.GetFailureHandlingOptions()
            failure_options.SetFailuresPreprocessor(SkipOnErrorPreprocessor())
            t.SetFailureHandlingOptions(failure_options)
            try:
                new_ceiling = make_ceiling(room)
                if new_ceiling is None:
                    skipped_rooms += 1
                    if not t.HasEnded():
                        t.RollBack()
                else:
                    status = t.Commit()
                    if status != Autodesk.Revit.DB.TransactionStatus.Committed:
                        skipped_rooms += 1
                        print("Failed to create ceiling for room {}: transaction {}".format(
                            room_label, status))
            except Exception as ex:
                if not t.HasEnded():
                    t.RollBack()
                skipped_rooms += 1
                print("Failed to create ceiling for room {}: {}".format(room_label, ex))
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
