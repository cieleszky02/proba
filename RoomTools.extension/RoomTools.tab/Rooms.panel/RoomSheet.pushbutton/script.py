# -*- coding: utf-8 -*-
"""Create a sheet (plan/RCP callouts + 2 sections + 3D view) for each room.

Select one or more rooms before clicking, or the tool will prompt you to
pick them. For every room this creates a floor plan and a reflected
ceiling plan (each a callout, or a standalone view as a fallback) cropped
to the room, an X section and a Y section through the room centre, a 3D
view with a section box around the room, and a sheet with all five views
placed on it. All names are derived from the room name; if a name or the
sheet number already exists, a shared iteration number is appended to
every view of that room.

Runtime: pyRevit / IronPython 2.7. No f-strings, no type hints.
"""

import traceback

from pyrevit import revit, DB, script, forms
from Autodesk.Revit.DB.Architecture import Room
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType

output = script.get_output()
logger = script.get_logger()

doc = revit.doc
uidoc = revit.uidoc


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

# Plan / callout crop, offset beyond the room boundary.
CROP_OFFSET_MM = 300.0
USE_ROOM_SHAPE_CROP = False    # True: crop to room outline. False: rectangle.
CROP_BOX_VISIBLE = False

# Section extents, offset beyond the room in each direction.
SECTION_SIDE_OFFSET_MM = 300.0     # left / right, beyond the room width
SECTION_NEAR_OFFSET_MM = 150.0     # small buffer on the cut side (see note below)
SECTION_DEPTH_OFFSET_MM = 300.0    # far clip, beyond the room depth
SECTION_TOP_OFFSET_MM = 600.0      # above the room's top
SECTION_BOTTOM_OFFSET_MM = 300.0   # below the room's base

# 3D view section box, offset beyond the room bounding box.
BOX_3D_SIDE_OFFSET_MM = 300.0
BOX_3D_TOP_OFFSET_MM = 600.0
BOX_3D_BOTTOM_OFFSET_MM = 300.0

PLAN_VIEW_SCALE = 50
CEILING_VIEW_SCALE = 50
SECTION_VIEW_SCALE = 50

# Substring (case-insensitive) to match a title block family name.
# Leave as None to just use the first title block type found in the model.
SHEET_TITLEBLOCK_FAMILY = None

# Margin inside the title block (or fallback sheet area) reserved for the
# viewport grid.
SHEET_MARGIN_MM = 20.0
# Fallback content area (roughly A1-ish) used only when the sheet has no
# title block instance to measure.
FALLBACK_SHEET_WIDTH_MM = 800.0
FALLBACK_SHEET_HEIGHT_MM = 550.0

VIEW_NAME_TEMPLATE = {
    'plan': u"{room} - Plan",
    'ceiling': u"{room} - RCP",
    'section_x': u"{room} - Section X",
    'section_y': u"{room} - Section Y",
    'view_3d': u"{room} - 3D",
}

ILLEGAL_NAME_CHARS = [
    '\\', ':', '{', '}', '[', ']', '|', ';', '<', '>', '?', '`', '~',
]

NONE_TEMPLATE_LABEL = u"<None>"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def mm(value):
    """Millimeters to feet (Revit's internal length unit)."""
    return value / 304.8


def element_id_value(element_id):
    """ElementId -> plain int. .IntegerValue is deprecated in 2024+."""
    try:
        return element_id.Value
    except AttributeError:
        return element_id.IntegerValue


def sanitize_name(name):
    result = name
    for ch in ILLEGAL_NAME_CHARS:
        result = result.replace(ch, '_')
    result = result.strip()
    if not result:
        result = "Room"
    return result


def get_room_name(room):
    param = room.get_Parameter(DB.BuiltInParameter.ROOM_NAME)
    if param is not None and param.HasValue:
        value = param.AsString()
        if value:
            return sanitize_name(value)
    return "Room"


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

class RoomSelectionFilter(ISelectionFilter):
    def AllowElement(self, element):
        return isinstance(element, Room)

    def AllowReference(self, reference, position):
        return False


def get_selected_rooms():
    current = revit.get_selection()
    rooms = [el for el in current.elements if isinstance(el, Room)]
    if rooms:
        return rooms

    try:
        refs = uidoc.Selection.PickObjects(
            ObjectType.Element,
            RoomSelectionFilter(),
            "Select one or more rooms, then click Finish",
        )
    except Exception:
        # User cancelled the pick.
        return []

    picked = []
    seen_ids = set()
    for ref in refs:
        el = doc.GetElement(ref.ElementId)
        if isinstance(el, Room) and el.Id not in seen_ids:
            picked.append(el)
            seen_ids.add(el.Id)
    return picked


# ---------------------------------------------------------------------------
# View template picker
# ---------------------------------------------------------------------------

def collect_templates_by_view_type(document):
    by_type = {}
    for view in DB.FilteredElementCollector(document).OfClass(DB.View):
        if not view.IsTemplate:
            continue
        by_type.setdefault(view.ViewType, []).append(view)
    return by_type


def template_options_for(by_type, view_type):
    """(name, ElementId) pairs for templates whose ViewType matches --
    the same rule Revit's own "Apply Template" pickers use, so a Floor
    Plan view only ever offers Floor Plan templates, and so on."""
    templates = by_type.get(view_type, [])
    options = [(t.Name, t.Id) for t in templates]
    options.sort(key=lambda pair: pair[0].lower())
    return options


class ViewTemplatePickerWindow(forms.WPFWindow):
    def __init__(self, xaml_file, template_options):
        forms.WPFWindow.__init__(self, xaml_file)
        self.template_options = template_options
        self.result = None
        self._populate(self.plan_combo, template_options['plan'])
        self._populate(self.ceiling_combo, template_options['ceiling'])
        self._populate(self.section_combo, template_options['section'])
        self._populate(self.view3d_combo, template_options['view_3d'])

    def _populate(self, combo, options):
        combo.ItemsSource = [NONE_TEMPLATE_LABEL] + [name for name, _id in options]
        combo.SelectedIndex = 0

    def _selected_id(self, combo, key):
        selected_name = combo.SelectedItem
        if not selected_name or selected_name == NONE_TEMPLATE_LABEL:
            return None
        for name, template_id in self.template_options[key]:
            if name == selected_name:
                return template_id
        return None

    def ok_click(self, sender, args):
        self.result = {
            'plan': self._selected_id(self.plan_combo, 'plan'),
            'ceiling': self._selected_id(self.ceiling_combo, 'ceiling'),
            'section': self._selected_id(self.section_combo, 'section'),
            'view_3d': self._selected_id(self.view3d_combo, 'view_3d'),
        }
        self.Close()

    def cancel_click(self, sender, args):
        self.result = None
        self.Close()


def choose_view_templates(document):
    """One dialog with a dropdown per view type (Plan, Ceiling Plan,
    Section -- shared by both X and Y, 3D). Returns a dict of
    'plan'/'ceiling'/'section'/'view_3d' -> ElementId or None. Returns {}
    without showing anything if the model has no view templates at all;
    returns None if the user cancelled."""
    by_type = collect_templates_by_view_type(document)
    template_options = {
        'plan': template_options_for(by_type, DB.ViewType.FloorPlan),
        'ceiling': template_options_for(by_type, DB.ViewType.CeilingPlan),
        'section': template_options_for(by_type, DB.ViewType.Section),
        'view_3d': template_options_for(by_type, DB.ViewType.ThreeD),
    }
    if not any(template_options.values()):
        return {}

    xaml_file = script.get_bundle_file('ViewTemplatePicker.xaml')
    window = ViewTemplatePickerWindow(xaml_file, template_options)
    window.ShowDialog()
    return window.result


def apply_view_template(view, template_id):
    if template_id is None:
        return
    try:
        view.ViewTemplateId = template_id
    except Exception:
        logger.warning("Could not apply view template to '{}'.".format(view.Name))


# ---------------------------------------------------------------------------
# Naming / uniqueness
# ---------------------------------------------------------------------------

def collect_existing_view_names(document):
    names = set()
    for view in DB.FilteredElementCollector(document).OfClass(DB.View):
        if isinstance(view, DB.ViewSheet):
            continue
        if view.IsTemplate:
            continue
        names.add(view.Name)
    return names


def collect_existing_sheet_numbers(document):
    return set(
        sheet.SheetNumber
        for sheet in DB.FilteredElementCollector(document).OfClass(DB.ViewSheet)
    )


def resolve_names(base_name, taken_view_names, taken_sheet_numbers):
    """Find the lowest shared suffix so plan/section/3D/sheet names are
    all free at once. Mutates the taken_* sets to reserve the result."""
    n = 0
    while True:
        suffix = u"" if n == 0 else u" {}".format(n)
        candidate = u"{}{}".format(base_name, suffix)

        names = {
            'plan': VIEW_NAME_TEMPLATE['plan'].format(room=candidate),
            'ceiling': VIEW_NAME_TEMPLATE['ceiling'].format(room=candidate),
            'section_x': VIEW_NAME_TEMPLATE['section_x'].format(room=candidate),
            'section_y': VIEW_NAME_TEMPLATE['section_y'].format(room=candidate),
            'view_3d': VIEW_NAME_TEMPLATE['view_3d'].format(room=candidate),
        }
        sheet_number = sanitize_name(candidate)

        view_names_free = all(
            nm not in taken_view_names for nm in names.values()
        )
        sheet_number_free = sheet_number not in taken_sheet_numbers

        if view_names_free and sheet_number_free:
            names['sheet_name'] = candidate
            names['sheet_number'] = sheet_number
            taken_view_names.update(names[k] for k in
                                     ('plan', 'ceiling', 'section_x', 'section_y', 'view_3d'))
            taken_sheet_numbers.add(sheet_number)
            return names

        n += 1


# ---------------------------------------------------------------------------
# View family type lookup
# ---------------------------------------------------------------------------

def find_view_family_type(document, view_family):
    for vft in DB.FilteredElementCollector(document).OfClass(DB.ViewFamilyType):
        if vft.ViewFamily == view_family:
            return vft
    return None


def find_plan_view_for_level(document, level_id, view_type):
    for view in DB.FilteredElementCollector(document).OfClass(DB.ViewPlan):
        if view.IsTemplate:
            continue
        if view.ViewType != view_type:
            continue
        gen_level = view.GenLevel
        if gen_level is not None and gen_level.Id == level_id:
            return view
    return None


# ---------------------------------------------------------------------------
# Crop shapes
# ---------------------------------------------------------------------------

def rectangle_curve_loop(bbox_min, bbox_max, offset, z):
    p0 = DB.XYZ(bbox_min.X - offset, bbox_min.Y - offset, z)
    p1 = DB.XYZ(bbox_max.X + offset, bbox_min.Y - offset, z)
    p2 = DB.XYZ(bbox_max.X + offset, bbox_max.Y + offset, z)
    p3 = DB.XYZ(bbox_min.X - offset, bbox_max.Y + offset, z)
    loop = DB.CurveLoop()
    loop.Append(DB.Line.CreateBound(p0, p1))
    loop.Append(DB.Line.CreateBound(p1, p2))
    loop.Append(DB.Line.CreateBound(p2, p3))
    loop.Append(DB.Line.CreateBound(p3, p0))
    return loop


def loop_is_counterclockwise(curve_loop):
    """Shoelace sum on the loop's vertices, viewed from +Z."""
    points = [curve.GetEndPoint(0) for curve in curve_loop]
    count = len(points)
    area = 0.0
    for i in range(count):
        p1 = points[i]
        p2 = points[(i + 1) % count]
        area += (p1.X * p2.Y - p2.X * p1.Y)
    return area > 0


def tessellate_curves_to_points(curves):
    """Flatten a closed chain of curves (lines, arcs, ...) into a single
    polyline point list, since crop shapes must be straight lines only."""
    points = []
    for curve in curves:
        pts = list(curve.Tessellate())
        if points and points[-1].IsAlmostEqualTo(pts[0]):
            pts = pts[1:]
        points.extend(pts)
    if len(points) > 1 and not points[-1].IsAlmostEqualTo(points[0]):
        points.append(points[0])
    return points


def curve_loop_from_points(points):
    loop = DB.CurveLoop()
    for i in range(len(points) - 1):
        p1 = points[i]
        p2 = points[i + 1]
        if p1.DistanceTo(p2) < 0.0005:  # drop near-zero-length segments
            continue
        loop.Append(DB.Line.CreateBound(p1, p2))
    return loop


def room_outline_curve_loop(room, offset):
    options = DB.SpatialElementBoundaryOptions()
    loops = room.GetBoundarySegments(options)
    if not loops or not loops[0]:
        return None

    outer = loops[0]
    # Crop shapes only accept straight lines (SetCropShape rejects arcs),
    # so curved boundaries -- round rooms, bullnose walls -- are tessellated
    # into a polygon approximation before anything else touches them.
    points = tessellate_curves_to_points([segment.GetCurve() for segment in outer])
    curve_loop = curve_loop_from_points(points)
    if curve_loop.NumberOfCurves() < 3:
        return None

    # CreateViaOffset's sign is relative to the loop's winding direction,
    # which Revit does not guarantee here, so we measure it instead of
    # assuming it and flip the sign to always grow the loop outward.
    signed_offset = offset if loop_is_counterclockwise(curve_loop) else -offset
    try:
        return DB.CurveLoop.CreateViaOffset(curve_loop, signed_offset, DB.XYZ.BasisZ)
    except Exception:
        logger.warning("Room shape offset failed, falling back to rectangle crop.")
        return None


def apply_crop_shape(view, curve_loop):
    view.CropBoxActive = True
    view.CropBoxVisible = CROP_BOX_VISIBLE
    crop_manager = view.GetCropRegionShapeManager()
    crop_manager.SetCropShape(curve_loop)


# ---------------------------------------------------------------------------
# View creation
# ---------------------------------------------------------------------------

def build_crop_loop(room, room_bbox, offset, z):
    crop_loop = None
    if USE_ROOM_SHAPE_CROP:
        crop_loop = room_outline_curve_loop(room, offset)
    if crop_loop is None:
        crop_loop = rectangle_curve_loop(room_bbox.Min, room_bbox.Max, offset, z)
    return crop_loop


def create_cropped_plan_view(document, plan_vft, view_type, room_bbox, level, crop_loop):
    offset = mm(CROP_OFFSET_MM)
    z = room_bbox.Min.Z

    plan_view = None
    parent_plan = find_plan_view_for_level(document, level.Id, view_type)
    if parent_plan is not None:
        p1 = DB.XYZ(room_bbox.Min.X - offset, room_bbox.Min.Y - offset, z)
        p2 = DB.XYZ(room_bbox.Max.X + offset, room_bbox.Max.Y + offset, z)
        try:
            plan_view = DB.ViewSection.CreateCallout(
                document, plan_vft.Id, parent_plan.Id, p1, p2
            )
        except Exception:
            logger.warning("Callout creation failed, falling back to a standalone view.")
            plan_view = None

    if plan_view is None:
        plan_view = DB.ViewPlan.Create(document, plan_vft.Id, level.Id)

    apply_crop_shape(plan_view, crop_loop)
    return plan_view


def create_room_plan(document, plan_vft, room, room_bbox, level):
    crop_loop = build_crop_loop(room, room_bbox, mm(CROP_OFFSET_MM), room_bbox.Min.Z)
    plan_view = create_cropped_plan_view(
        document, plan_vft, DB.ViewType.FloorPlan, room_bbox, level, crop_loop
    )
    try:
        plan_view.Scale = PLAN_VIEW_SCALE
    except Exception:
        pass
    return plan_view


def create_room_ceiling_plan(document, ceiling_vft, room, room_bbox, level):
    crop_loop = build_crop_loop(room, room_bbox, mm(CROP_OFFSET_MM), room_bbox.Min.Z)
    ceiling_view = create_cropped_plan_view(
        document, ceiling_vft, DB.ViewType.CeilingPlan, room_bbox, level, crop_loop
    )
    try:
        ceiling_view.Scale = CEILING_VIEW_SCALE
    except Exception:
        pass
    return ceiling_view


def create_room_section(document, section_vft, room_bbox, center, axis):
    """axis 'x': cut line along X, looking north (+Y).
    axis 'y': cut line along Y, looking west (-X)."""
    if axis == 'x':
        basis_x = DB.XYZ.BasisX
        basis_z = -DB.XYZ.BasisY               # view direction = +Y (north)
        half_width = (room_bbox.Max.X - room_bbox.Min.X) / 2.0
        half_depth = (room_bbox.Max.Y - room_bbox.Min.Y) / 2.0
    else:
        basis_x = DB.XYZ.BasisY
        basis_z = DB.XYZ.BasisX                # view direction = -X (west)
        half_width = (room_bbox.Max.Y - room_bbox.Min.Y) / 2.0
        half_depth = (room_bbox.Max.X - room_bbox.Min.X) / 2.0

    basis_y = DB.XYZ.BasisZ

    transform = DB.Transform.Identity
    transform.Origin = center
    transform.BasisX = basis_x
    transform.BasisY = basis_y
    transform.BasisZ = basis_z

    side_offset = mm(SECTION_SIDE_OFFSET_MM)
    near_offset = mm(SECTION_NEAR_OFFSET_MM)
    depth_offset = mm(SECTION_DEPTH_OFFSET_MM)
    bottom_offset = mm(SECTION_BOTTOM_OFFSET_MM)
    top_offset = mm(SECTION_TOP_OFFSET_MM)

    section_box = DB.BoundingBoxXYZ()
    section_box.Transform = transform
    # Confirmed against a real Revit section: the cut line Revit draws in
    # the parent plan sits at local Min.Z, not Max.Z as the usual "near
    # clip" assumption would suggest. So Min.Z is pinned to (nearly) the
    # transform's origin -- the room centre -- and Max.Z is the far side,
    # beyond the room's far edge in the view direction. Min.Z is offset by
    # a small SECTION_NEAR_OFFSET_MM rather than exactly 0 to avoid a
    # degenerate zero-thickness bound.
    section_box.Min = DB.XYZ(
        -(half_width + side_offset),
        room_bbox.Min.Z - center.Z - bottom_offset,
        -near_offset,
    )
    section_box.Max = DB.XYZ(
        half_width + side_offset,
        room_bbox.Max.Z - center.Z + top_offset,
        half_depth + depth_offset,
    )

    section_view = DB.ViewSection.CreateSection(document, section_vft.Id, section_box)
    try:
        section_view.Scale = SECTION_VIEW_SCALE
    except Exception:
        pass
    return section_view


def create_room_3d_view(document, view3d_vft, room_bbox):
    view_3d = DB.View3D.CreateIsometric(document, view3d_vft.Id)

    side_offset = mm(BOX_3D_SIDE_OFFSET_MM)
    top_offset = mm(BOX_3D_TOP_OFFSET_MM)
    bottom_offset = mm(BOX_3D_BOTTOM_OFFSET_MM)

    box_3d = DB.BoundingBoxXYZ()
    box_3d.Min = DB.XYZ(
        room_bbox.Min.X - side_offset,
        room_bbox.Min.Y - side_offset,
        room_bbox.Min.Z - bottom_offset,
    )
    box_3d.Max = DB.XYZ(
        room_bbox.Max.X + side_offset,
        room_bbox.Max.Y + side_offset,
        room_bbox.Max.Z + top_offset,
    )
    view_3d.SetSectionBox(box_3d)
    view_3d.IsSectionBoxActive = True
    return view_3d


# ---------------------------------------------------------------------------
# Sheet + layout
# ---------------------------------------------------------------------------

def find_titleblock_type_id(document):
    collector = (
        DB.FilteredElementCollector(document)
        .OfCategory(DB.BuiltInCategory.OST_TitleBlocks)
        .WhereElementIsElementType()
    )
    if SHEET_TITLEBLOCK_FAMILY:
        needle = SHEET_TITLEBLOCK_FAMILY.lower()
        for tb_type in collector:
            if needle in tb_type.FamilyName.lower():
                return tb_type.Id
        return DB.ElementId.InvalidElementId

    first = collector.FirstElement()
    return first.Id if first else DB.ElementId.InvalidElementId


def get_sheet_content_area(document, sheet):
    margin = mm(SHEET_MARGIN_MM)
    tb_instance = None
    for el in (
        DB.FilteredElementCollector(document, sheet.Id)
        .OfCategory(DB.BuiltInCategory.OST_TitleBlocks)
        .WhereElementIsNotElementType()
    ):
        tb_instance = el
        break

    if tb_instance is not None:
        tb_bbox = tb_instance.get_BoundingBox(sheet)
        if tb_bbox is not None:
            return (
                DB.XYZ(tb_bbox.Min.X + margin, tb_bbox.Min.Y + margin, 0),
                DB.XYZ(tb_bbox.Max.X - margin, tb_bbox.Max.Y - margin, 0),
            )

    return (
        DB.XYZ(margin, margin, 0),
        DB.XYZ(mm(FALLBACK_SHEET_WIDTH_MM) - margin, mm(FALLBACK_SHEET_HEIGHT_MM) - margin, 0),
    )


def grid_centers(area_min, area_max, rows, cols):
    """Row-major centers (top row first) of a rows x cols grid over the area."""
    width = area_max.X - area_min.X
    height = area_max.Y - area_min.Y
    col_w = width / cols
    row_h = height / rows
    centers = []
    for r in range(rows):
        row_y = area_max.Y - row_h * (r + 0.5)
        for c in range(cols):
            col_x = area_min.X + col_w * (c + 0.5)
            centers.append(DB.XYZ(col_x, row_y, 0))
    return centers


def place_views_on_sheet(document, sheet, views_in_order, rows, cols):
    area_min, area_max = get_sheet_content_area(document, sheet)
    centers = grid_centers(area_min, area_max, rows, cols)
    for view, point in zip(views_in_order, centers):
        if DB.Viewport.CanAddViewToSheet(document, sheet.Id, view.Id):
            DB.Viewport.Create(document, sheet.Id, view.Id, point)
        else:
            logger.warning("Could not place view '{}' on sheet '{}'.".format(
                view.Name, sheet.Name))


# ---------------------------------------------------------------------------
# Per-room orchestration
# ---------------------------------------------------------------------------

def create_room_sheet(document, room, vfts, taken_view_names, taken_sheet_numbers, template_ids):
    room_bbox = room.get_BoundingBox(None)
    if room_bbox is None or room.Area <= 0:
        raise Exception("Room has no valid geometry (unplaced or unbounded).")

    level = document.GetElement(room.LevelId)
    if level is None:
        raise Exception("Room has no valid level.")

    center = DB.XYZ(
        (room_bbox.Min.X + room_bbox.Max.X) / 2.0,
        (room_bbox.Min.Y + room_bbox.Max.Y) / 2.0,
        (room_bbox.Min.Z + room_bbox.Max.Z) / 2.0,
    )

    base_name = get_room_name(room)
    names = resolve_names(base_name, taken_view_names, taken_sheet_numbers)

    plan_view = create_room_plan(document, vfts['plan'], room, room_bbox, level)
    plan_view.Name = names['plan']
    apply_view_template(plan_view, template_ids.get('plan'))

    ceiling_view = create_room_ceiling_plan(document, vfts['ceiling'], room, room_bbox, level)
    ceiling_view.Name = names['ceiling']
    apply_view_template(ceiling_view, template_ids.get('ceiling'))

    section_x = create_room_section(document, vfts['section'], room_bbox, center, 'x')
    section_x.Name = names['section_x']
    apply_view_template(section_x, template_ids.get('section'))

    section_y = create_room_section(document, vfts['section'], room_bbox, center, 'y')
    section_y.Name = names['section_y']
    apply_view_template(section_y, template_ids.get('section'))

    view_3d = create_room_3d_view(document, vfts['view_3d'], room_bbox)
    view_3d.Name = names['view_3d']
    apply_view_template(view_3d, template_ids.get('view_3d'))

    titleblock_type_id = find_titleblock_type_id(document)
    sheet = DB.ViewSheet.Create(document, titleblock_type_id)
    sheet.Name = names['sheet_name']
    sheet.SheetNumber = names['sheet_number']

    # 2 rows x 3 cols: plan / ceiling / 3D on top, the two sections below.
    views_in_order = [plan_view, ceiling_view, view_3d, section_x, section_y]
    place_views_on_sheet(document, sheet, views_in_order, rows=2, cols=3)

    return sheet


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    rooms = get_selected_rooms()
    if not rooms:
        output.print_md("**No rooms selected.** Nothing to do.")
        return

    vfts = {
        'plan': find_view_family_type(doc, DB.ViewFamily.FloorPlan),
        'ceiling': find_view_family_type(doc, DB.ViewFamily.CeilingPlan),
        'section': find_view_family_type(doc, DB.ViewFamily.Section),
        'view_3d': find_view_family_type(doc, DB.ViewFamily.ThreeDimensional),
    }
    missing = [key for key, vft in vfts.items() if vft is None]
    if missing:
        output.print_md(
            "**Missing view family type(s) in this model:** {}".format(", ".join(missing))
        )
        return

    template_ids = choose_view_templates(doc)
    if template_ids is None:
        output.print_md("**Cancelled.** No sheets created.")
        return

    taken_view_names = collect_existing_view_names(doc)
    taken_sheet_numbers = collect_existing_sheet_numbers(doc)

    created_sheets = []
    failures = []

    t = DB.Transaction(doc, "Create Room Sheet(s)")
    t.Start()
    try:
        for room in rooms:
            room_label = get_room_name(room)
            try:
                sheet = create_room_sheet(
                    doc, room, vfts, taken_view_names, taken_sheet_numbers, template_ids
                )
                created_sheets.append(sheet)
                output.print_md("Created sheet **{}** ({}) for room **{}**.".format(
                    sheet.Name, sheet.SheetNumber, room_label))
            except Exception:
                failures.append((room_label, traceback.format_exc()))
        t.Commit()
    except Exception:
        if t.HasStarted() and not t.HasEnded():
            t.RollBack()
        raise

    if failures:
        output.print_md("### Errors")
        for room_label, tb in failures:
            output.print_md("**Room: {}**".format(room_label))
            output.print_md("```\n{}\n```".format(tb))

    output.print_md("Done. {} sheet(s) created, {} failure(s).".format(
        len(created_sheets), len(failures)))

    if created_sheets:
        try:
            uidoc.ActiveView = created_sheets[-1]
        except Exception:
            pass


main()
