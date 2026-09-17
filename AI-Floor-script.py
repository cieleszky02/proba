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
            co_curves.Append(segment.GetCurve())
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

        floor_curves = List[Autodesk.Revit.DB.Curve]()
        for boundary_segment in all_boundaries[0]:
            floor_curves.Add(boundary_segment.GetCurve())
        floor_curves_loop = Autodesk.Revit.DB.CurveLoop.Create(floor_curves)
        curve_loops = List[Autodesk.Revit.DB.CurveLoop]()
        curve_loops.Add(floor_curves_loop)

        f = Autodesk.Revit.DB.Floor.Create(doc, curve_loops, floor_type.Id, room.LevelId)

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
            t = Autodesk.Revit.DB.Transaction(doc, 'Create Floor Finishing')
            t.Start()
            try:
                new_floor = make_floor(room)
                if new_floor is None:
                    skipped_rooms += 1
                    t.RollBack()
                else:
                    t.Commit()
            except Exception as ex:
                t.RollBack()
                skipped_rooms += 1
                print("Failed to create floor for room '{}' {}: {}".format(
                    room.get_Parameter(BuiltInParameter.ROOM_NAME).AsString(), room.Number, ex))
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
