# -*- coding: utf-8 -*-
"""Create a section through the connection between two building components.

Pick a first component (floor, wall, roof, ceiling, beam, column or
foundation), then pick a second one that touches it. A new section view is
created through the middle of the shared joint, looking along it, so both
components and their connection are visible.
"""

import math

from Autodesk.Revit.Exceptions import OperationCanceledException
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType
from System.Collections.Generic import List

from pyrevit import revit, DB, forms, script

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()


# ---------------------------------------------------------------- CONFIG ---

# Categories eligible as first/second component.
CATEGORIES = [
    DB.BuiltInCategory.OST_Floors,
    DB.BuiltInCategory.OST_Walls,
    DB.BuiltInCategory.OST_Roofs,
    DB.BuiltInCategory.OST_Ceilings,
    DB.BuiltInCategory.OST_StructuralFraming,
    DB.BuiltInCategory.OST_StructuralColumns,
    DB.BuiltInCategory.OST_Columns,
    DB.BuiltInCategory.OST_StructuralFoundation,
]

BBOX_TOL_MM = 50.0        # how far to grow A's bounding box when looking for neighbours
CONTACT_TOL_MM = 5.0      # coplanarity / "same plane" tolerance for face contact
MIN_OVERLAP_MM = 25.0     # ignore contact patches shorter than this

SECTION_DEPTH_MM = 1500.0   # minimum far clip depth, along the joint direction
NEAR_CLIP_MM = 300.0        # small buffer behind the cut plane
SECTION_WIDTH_MM = 1200.0   # minimum crop box width, centred on the joint origin
SECTION_TOP_MM = 600.0      # crop box extends this far above the combined elements
SECTION_BOTTOM_MM = 600.0   # ... and this far below

VIEW_FAMILY_TYPE_NAME = None   # None = ask the user which Section type to use
VIEW_TEMPLATE_NAME = None      # None = no template applied

NAME_PREFIX = "Connection Detail"   # names look like "Connection Detail Basic Wall / Floor Generic"


def mm(value):
    """Millimetres -> feet (Revit internal units)."""
    return DB.UnitUtils.ConvertToInternalUnits(value, DB.UnitTypeId.Millimeters)


def to_mm(value):
    """Feet (Revit internal units) -> millimetres."""
    return DB.UnitUtils.ConvertFromInternalUnits(value, DB.UnitTypeId.Millimeters)


def get_name(element):
    """Element.Name, read through the base class's property descriptor.

    Several Element subclasses (View, ViewFamilyType, ...) hide the Name
    property, which makes IronPython's normal `element.Name` dynamic lookup
    raise `System.MissingMemberException: Name`. Going through the base
    class's descriptor sidesteps that."""
    return DB.Element.Name.GetValue(element)


def set_name(element, name):
    DB.Element.Name.SetValue(element, name)


# ------------------------------------------------------------- SELECTION ---

class CategorySelectionFilter(ISelectionFilter):
    """Only allow picking elements whose category is in an explicit set."""

    def __init__(self, allowed_category_ids):
        self._ids = list(allowed_category_ids)

    def AllowElement(self, element):
        category = element.Category
        if category is None:
            return False
        return any(category.Id == cid for cid in self._ids)

    def AllowReference(self, reference, position):
        return True


class ElementIdSelectionFilter(ISelectionFilter):
    """Only allow picking elements from an explicit set of ElementIds."""

    def __init__(self, allowed_ids):
        self._ids = list(allowed_ids)

    def AllowElement(self, element):
        return any(element.Id == eid for eid in self._ids)

    def AllowReference(self, reference, position):
        return True


def category_ids():
    return [DB.ElementId(bic) for bic in CATEGORIES]


def pick_first_element():
    sel_filter = CategorySelectionFilter(category_ids())
    try:
        ref = uidoc.Selection.PickObject(
            ObjectType.Element,
            sel_filter,
            "Select the FIRST component (floor, wall, roof, ceiling, beam, "
            "column or foundation)"
        )
    except OperationCanceledException:
        return None
    return doc.GetElement(ref.ElementId)


# --------------------------------------------------------------- GEOMETRY ---

def get_bounding_box(element):
    bbox = element.get_BoundingBox(None)
    if bbox is None:
        bbox = element.get_BoundingBox(doc.ActiveView)
    return bbox


def get_geometry_options():
    options = DB.Options()
    options.ComputeReferences = False
    options.IncludeNonVisibleObjects = False
    options.DetailLevel = DB.ViewDetailLevel.Fine
    return options


def _flatten_solids(geometry_element):
    solids = []
    for geo_object in geometry_element:
        if isinstance(geo_object, DB.Solid):
            if geo_object.Volume > 1e-6:
                solids.append(geo_object)
        elif isinstance(geo_object, DB.GeometryInstance):
            solids.extend(_flatten_solids(geo_object.GetInstanceGeometry()))
    return solids


def get_solids(element):
    geometry = element.get_Geometry(get_geometry_options())
    if geometry is None:
        return []
    return _flatten_solids(geometry)


def get_planar_faces(element):
    faces = []
    for solid in get_solids(element):
        for face in solid.Faces:
            if isinstance(face, DB.PlanarFace) and face.Area > 1e-6:
                faces.append(face)
    return faces


def _face_vertices(face):
    points = []
    mesh = face.Triangulate()
    if mesh is None:
        return points
    for i in range(mesh.NumTriangles):
        triangle = mesh.get_Triangle(i)
        points.append(triangle.get_Vertex(0))
        points.append(triangle.get_Vertex(1))
        points.append(triangle.get_Vertex(2))
    return points


def _overlap_points(face_a, face_b):
    tol = mm(CONTACT_TOL_MM)
    points = []
    for point in _face_vertices(face_b):
        result = face_a.Project(point)
        if result is not None and result.Distance < tol:
            points.append(result.XYZPoint)
    for point in _face_vertices(face_a):
        result = face_b.Project(point)
        if result is not None and result.Distance < tol:
            points.append(point)
    return points


def _faces_touch(face_a, face_b):
    normal_a = face_a.FaceNormal
    normal_b = face_b.FaceNormal
    if normal_a.DotProduct(normal_b) > -0.999:
        return None

    gap = abs((face_b.Origin - face_a.Origin).DotProduct(normal_a))
    if gap > mm(CONTACT_TOL_MM):
        return None

    points = _overlap_points(face_a, face_b)
    if len(points) < 3:
        return None
    return points


def _principal_axis(points, axis_u, axis_v, origin_ref):
    """2D PCA of `points` projected onto (axis_u, axis_v), both assumed
    unit length and orthogonal. Returns (direction, length, centroid)."""
    us = []
    vs = []
    for point in points:
        vector = point - origin_ref
        us.append(vector.DotProduct(axis_u))
        vs.append(vector.DotProduct(axis_v))

    count = len(us)
    mean_u = sum(us) / count
    mean_v = sum(vs) / count

    sxx = sum((u - mean_u) ** 2 for u in us)
    syy = sum((v - mean_v) ** 2 for v in vs)
    sxy = sum((us[i] - mean_u) * (vs[i] - mean_v) for i in range(count))

    if abs(sxx - syy) < 1e-12 and abs(sxy) < 1e-12:
        theta = 0.0
    else:
        theta = 0.5 * math.atan2(2.0 * sxy, sxx - syy)

    cos_t = math.cos(theta)
    sin_t = math.sin(theta)
    direction = axis_u.Multiply(cos_t).Add(axis_v.Multiply(sin_t))

    projections = [(us[i] - mean_u) * cos_t + (vs[i] - mean_v) * sin_t
                   for i in range(count)]
    length = max(projections) - min(projections)

    centroid = origin_ref.Add(axis_u.Multiply(mean_u)).Add(axis_v.Multiply(mean_v))
    return direction, length, centroid


def _horizontal(direction, fallback):
    flat = DB.XYZ(direction.X, direction.Y, 0)
    if flat.GetLength() < 1e-6:
        flat = DB.XYZ(fallback.X, fallback.Y, 0)
    if flat.GetLength() < 1e-6:
        flat = DB.XYZ.BasisX
    return flat.Normalize()


def _in_plane_axes(normal):
    """Two orthonormal vectors spanning the plane with the given normal,
    chosen so PCA on them finds the true horizontal joint direction whether
    the face is vertical (side contact) or horizontal (top/bottom contact)."""
    up = DB.XYZ.BasisZ
    if abs(normal.DotProduct(up)) > 0.999:
        axis_u = DB.XYZ.BasisX
        axis_v = normal.CrossProduct(axis_u).Normalize()
    else:
        axis_v = up.Subtract(normal.Multiply(normal.DotProduct(up))).Normalize()
        axis_u = normal.CrossProduct(axis_v).Normalize()
    return axis_u, axis_v


def _classify_kind(normal_a):
    if normal_a.Z > 0.7:
        return "top"
    if normal_a.Z < -0.7:
        return "bottom"
    return "side"


class Contact(object):
    def __init__(self, element, kind, origin, joint_dir, length):
        self.element = element
        self.kind = kind                # "side" | "top" | "bottom" | "overlap"
        self.origin = origin            # XYZ, middle of the contact region
        self.joint_dir = joint_dir      # XYZ, horizontal unit vector along the joint
        self.length = length            # feet


def _detect_face_contact(element_a, element_b):
    faces_a = get_planar_faces(element_a)
    faces_b = get_planar_faces(element_b)

    best = None
    for face_a in faces_a:
        for face_b in faces_b:
            points = _faces_touch(face_a, face_b)
            if points is None:
                continue

            axis_u, axis_v = _in_plane_axes(face_a.FaceNormal)
            direction, length, centroid = _principal_axis(points, axis_u, axis_v, points[0])
            if length < mm(MIN_OVERLAP_MM):
                continue
            if best is not None and length <= best.length:
                continue

            joint_dir = _horizontal(direction, axis_u)
            kind = _classify_kind(face_a.FaceNormal)
            best = Contact(element_b, kind, centroid, joint_dir, length)
    return best


def _detect_overlap_contact(element_a, element_b):
    intersects_filter = DB.ElementIntersectsElementFilter(element_a)
    if not intersects_filter.PassesFilter(element_b):
        return None

    best_solid = None
    for solid_a in get_solids(element_a):
        for solid_b in get_solids(element_b):
            try:
                result = DB.BooleanOperationsUtils.ExecuteBooleanOperation(
                    solid_a, solid_b, DB.BooleanOperationsType.Intersect
                )
            except Exception:
                continue
            if result is None or result.Volume < 1e-9:
                continue
            if best_solid is None or result.Volume > best_solid.Volume:
                best_solid = result

    if best_solid is None:
        return None

    points = []
    for edge in best_solid.Edges:
        curve = edge.AsCurve()
        points.append(curve.GetEndPoint(0))
        points.append(curve.GetEndPoint(1))
    if len(points) < 2:
        return None

    direction, length, centroid = _principal_axis(
        points, DB.XYZ.BasisX, DB.XYZ.BasisY, points[0]
    )
    joint_dir = _horizontal(direction, DB.XYZ.BasisX)
    return Contact(element_b, "overlap", centroid, joint_dir, length)


def detect_contact(element_a, element_b):
    contact = _detect_face_contact(element_a, element_b)
    if contact is not None:
        return contact
    return _detect_overlap_contact(element_a, element_b)


def find_candidates(element_a):
    bbox = get_bounding_box(element_a)
    if bbox is None:
        return []

    tol = mm(BBOX_TOL_MM)
    outline = DB.Outline(
        bbox.Min - DB.XYZ(tol, tol, tol),
        bbox.Max + DB.XYZ(tol, tol, tol)
    )
    bbox_filter = DB.BoundingBoxIntersectsFilter(outline)
    category_filter = DB.ElementMulticategoryFilter(List[DB.BuiltInCategory](CATEGORIES))

    collector = DB.FilteredElementCollector(doc) \
        .WherePasses(bbox_filter) \
        .WherePasses(category_filter) \
        .WhereElementIsNotElementType()

    candidates = []
    for element_b in collector:
        if element_b.Id == element_a.Id:
            continue
        contact = detect_contact(element_a, element_b)
        if contact is not None:
            candidates.append(contact)
    return candidates


# ------------------------------------------------------------------- UI ---

def element_label(element):
    category_name = element.Category.Name if element.Category else "Element"
    return "{} {}".format(category_name, element.Id)


def type_name(element):
    """The element's type name (e.g. a wall or floor type), falling back to
    `element_label` if it has none."""
    type_id = element.GetTypeId()
    if type_id != DB.ElementId.InvalidElementId:
        element_type = doc.GetElement(type_id)
        if element_type is not None and get_name(element_type):
            return get_name(element_type)
    return element_label(element)


def _contact_label(contact):
    element = contact.element
    length_mm = int(round(to_mm(contact.length)))
    return "{} - {} contact, {} mm (id {})".format(
        element_label(element), contact.kind, length_mm, element.Id
    )


def _pick_contact_from_list(contacts):
    lookup = {}
    labels = []
    for contact in contacts:
        label = _contact_label(contact)
        labels.append(label)
        lookup[label] = contact

    chosen = forms.SelectFromList.show(
        sorted(labels),
        title="Select the SECOND component",
        button_name="Create Section"
    )
    if not chosen:
        return None
    return lookup[chosen]


def _pick_contact_in_model(contacts):
    allowed_ids = [contact.element.Id for contact in contacts]
    by_id = dict((str(contact.element.Id), contact) for contact in contacts)

    previous_selection = list(uidoc.Selection.GetElementIds())
    uidoc.Selection.SetElementIds(List[DB.ElementId](allowed_ids))
    try:
        ref = uidoc.Selection.PickObject(
            ObjectType.Element,
            ElementIdSelectionFilter(allowed_ids),
            "Click the SECOND component (highlighted candidates only)"
        )
    except OperationCanceledException:
        return None
    finally:
        uidoc.Selection.SetElementIds(List[DB.ElementId](previous_selection))

    return by_id.get(str(ref.ElementId))


def choose_contacts(contacts):
    """Return the list of Contact objects to create sections for (possibly
    all of them), or an empty list if the user cancelled."""
    if not contacts:
        forms.alert(
            "No neighbouring floor, wall, roof, ceiling, beam, column or "
            "foundation was found touching the selected element.",
            title="Connection Section"
        )
        return []

    if len(contacts) == 1:
        return contacts

    all_option = "Create all {} connections".format(len(contacts))
    mode = forms.CommandSwitchWindow.show(
        ["Pick in model", "Choose from list", all_option],
        message="{} candidates found. How do you want to choose the second "
                 "component?".format(len(contacts))
    )
    if mode is None:
        return []
    if mode == all_option:
        return contacts
    if mode == "Pick in model":
        contact = _pick_contact_in_model(contacts)
    else:
        contact = _pick_contact_from_list(contacts)
    return [contact] if contact else []


# --------------------------------------------------------------- SECTION ---

SECTION_LIKE_FAMILIES = (DB.ViewFamily.Section, DB.ViewFamily.Detail)


def choose_section_type():
    """Ask which of the project's existing Section or Detail View types to
    use (e.g. Section / Section Detail / Section Detail Number / Detail),
    unless VIEW_FAMILY_TYPE_NAME pins one, or only one type exists.

    ViewSection.CreateSection only accepts a Section-family type ("The
    ViewFamilyType must be a Section ViewFamily"), so a Detail-family
    choice is created as a Section first and then switched with
    ChangeTypeId — see resolve_creation_type() — the same way Revit's own
    type selector lets you reassign an existing section to a Detail type."""
    section_types = [
        t for t in DB.FilteredElementCollector(doc).OfClass(DB.ViewFamilyType)
        if t.ViewFamily in SECTION_LIKE_FAMILIES
    ]
    if not section_types:
        forms.alert(
            "No Section or Detail View type found in this project.",
            title="Connection Section"
        )
        return None

    if VIEW_FAMILY_TYPE_NAME:
        for view_type in section_types:
            if get_name(view_type) == VIEW_FAMILY_TYPE_NAME:
                return view_type

    if len(section_types) == 1:
        return section_types[0]

    lookup = dict((get_name(view_type), view_type) for view_type in section_types)
    chosen = forms.SelectFromList.show(
        sorted(lookup.keys()),
        title="Select the section view type",
        button_name="Use this type"
    )
    if not chosen:
        return None
    return lookup[chosen]


def _find_view_template(name):
    for view in DB.FilteredElementCollector(doc).OfClass(DB.View):
        if view.IsTemplate and get_name(view) == name:
            return view
    return None


def _combined_bbox(element_a, element_b):
    bbox_a = get_bounding_box(element_a)
    bbox_b = get_bounding_box(element_b)
    min_pt = DB.XYZ(
        min(bbox_a.Min.X, bbox_b.Min.X),
        min(bbox_a.Min.Y, bbox_b.Min.Y),
        min(bbox_a.Min.Z, bbox_b.Min.Z),
    )
    max_pt = DB.XYZ(
        max(bbox_a.Max.X, bbox_b.Max.X),
        max(bbox_a.Max.Y, bbox_b.Max.Y),
        max(bbox_a.Max.Z, bbox_b.Max.Z),
    )
    return min_pt, max_pt


def _bbox_corners(min_pt, max_pt):
    corners = []
    for x in (min_pt.X, max_pt.X):
        for y in (min_pt.Y, max_pt.Y):
            for z in (min_pt.Z, max_pt.Z):
                corners.append(DB.XYZ(x, y, z))
    return corners


def build_section_box(element_a, element_b, contact):
    # Element A is placed on the left: "right" is the horizontal direction
    # perpendicular to the joint, "up" is world Z, "view direction" (Z of
    # the section box transform) is the joint direction itself, so the cut
    # plane through `origin` is perpendicular to the joint line.
    view_dir = contact.joint_dir
    right = DB.XYZ.BasisZ.CrossProduct(view_dir)
    if right.GetLength() < 1e-6:
        right = DB.XYZ.BasisX
    right = right.Normalize()
    up = view_dir.CrossProduct(right).Normalize()

    transform = DB.Transform.Identity
    transform.Origin = contact.origin
    transform.BasisX = right
    transform.BasisY = up
    transform.BasisZ = view_dir
    inverse = transform.Inverse

    world_min, world_max = _combined_bbox(element_a, element_b)
    local_min = local_max = None
    for corner in _bbox_corners(world_min, world_max):
        local = inverse.OfPoint(corner)
        if local_min is None:
            local_min, local_max = local, local
        else:
            local_min = DB.XYZ(
                min(local_min.X, local.X), min(local_min.Y, local.Y), min(local_min.Z, local.Z)
            )
            local_max = DB.XYZ(
                max(local_max.X, local.X), max(local_max.Y, local.Y), max(local_max.Z, local.Z)
            )

    half_width = max(mm(SECTION_WIDTH_MM) / 2.0, (local_max.X - local_min.X) / 2.0 + mm(300.0))
    top = local_max.Y + mm(SECTION_TOP_MM)
    bottom = local_min.Y - mm(SECTION_BOTTOM_MM)
    far_depth = max(mm(SECTION_DEPTH_MM), (local_max.Z - local_min.Z) / 2.0 + mm(300.0))

    section_box = DB.BoundingBoxXYZ()
    section_box.Transform = transform
    section_box.Min = DB.XYZ(-half_width, bottom, -mm(NEAR_CLIP_MM))
    section_box.Max = DB.XYZ(half_width, top, far_depth)
    return section_box


def unique_section_name(name_a, name_b):
    base = "{} {} / {}".format(NAME_PREFIX, name_a, name_b)
    existing = set(
        get_name(v) for v in DB.FilteredElementCollector(doc).OfClass(DB.ViewSection)
    )
    if base not in existing:
        return base

    counter = 2
    while True:
        candidate = "{} ({})".format(base, counter)
        if candidate not in existing:
            return candidate
        counter += 1


def resolve_creation_type(view_family_type):
    """CreateSection rejects anything but a Section-family type. If the
    user picked a Detail type, create with any Section-family type instead
    and switch it afterwards with ChangeTypeId."""
    if view_family_type.ViewFamily == DB.ViewFamily.Section:
        return view_family_type
    for t in DB.FilteredElementCollector(doc).OfClass(DB.ViewFamilyType):
        if t.ViewFamily == DB.ViewFamily.Section:
            return t
    return None


def create_section_view(element_a, contact, creation_type, final_type):
    """Create one section/detail view for a single contact. Must be called
    inside an open transaction."""
    element_b = contact.element
    section_box = build_section_box(element_a, element_b, contact)
    name = unique_section_name(type_name(element_a), type_name(element_b))

    section_view = DB.ViewSection.CreateSection(doc, creation_type.Id, section_box)
    if final_type.Id != creation_type.Id:
        section_view.ChangeTypeId(final_type.Id)
    set_name(section_view, name)
    if VIEW_TEMPLATE_NAME:
        template = _find_view_template(VIEW_TEMPLATE_NAME)
        if template is not None:
            section_view.ViewTemplateId = template.Id
    return section_view


# --------------------------------------------------------------------- ---

def main():
    element_a = pick_first_element()
    if element_a is None:
        return
    output.print_md("**First component:** {}".format(element_label(element_a)))

    candidates = find_candidates(element_a)
    output.print_md("Found **{}** touching candidate(s).".format(len(candidates)))

    contacts = choose_contacts(candidates)
    if not contacts:
        return

    view_family_type = choose_section_type()
    if view_family_type is None:
        return

    creation_type = resolve_creation_type(view_family_type)
    if creation_type is None:
        forms.alert(
            "No Section view type exists in this project to create the view "
            "with (one is needed even to create a Detail View type).",
            title="Connection Section"
        )
        return

    created_views = []
    with revit.Transaction("Create Connection Section(s)"):
        for contact in contacts:
            section_view = create_section_view(element_a, contact, creation_type, view_family_type)
            created_views.append(section_view)
            output.print_md(
                "Created **{}** — {} contact with {}, {:.0f} mm long".format(
                    get_name(section_view), contact.kind,
                    element_label(contact.element), to_mm(contact.length)
                )
            )

    if not created_views:
        return

    uidoc.ActiveView = created_views[-1]
    output.print_md("Done — created **{}** connection section(s).".format(len(created_views)))


main()
