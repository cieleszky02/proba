# -*- coding: utf-8 -*-
"""Create section/detail/plan views for every connection of a set of types.

Pick a category, then one or more family types in it (e.g. every window
type). Every instance of those types in the model is checked against every
other configured component; each not-yet-documented connection found gets
its own view, created through the middle of the joint. Wall/column-to-
wall/column connections (a corner or T-junction) can be a Plan callout
instead of a vertical Section/Detail, asked per connection since either
can be right depending on the case.
"""

import math

from Autodesk.Revit.DB.ExtensibleStorage import AccessLevel, Entity, Schema, SchemaBuilder
from System import Guid
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
    DB.BuiltInCategory.OST_Stairs,
    DB.BuiltInCategory.OST_StairsRailing,
    DB.BuiltInCategory.OST_Doors,
    DB.BuiltInCategory.OST_Windows,
    DB.BuiltInCategory.OST_CurtainWallPanels,
    DB.BuiltInCategory.OST_CurtainWallMullions,
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

# Extensible Storage schema used to remember which element pairs already
# have a connection section, so they drop out of the candidate list on the
# next run instead of being offered (and documented) again.
CONNECTION_SCHEMA_GUID = Guid("794ce64a-88a4-4a01-8e8a-738d89795226")
CONNECTION_SCHEMA_FIELD_A = "ElementAId"
CONNECTION_SCHEMA_FIELD_B = "ElementBId"


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


# ---------------------------------------------------------- CONNECTION LOG ---
# Extensible Storage bookkeeping: tags each created section/detail view with
# the pair of elements it documents, so already-documented pairs can be
# dropped from the candidate list on later runs.

def _get_connection_schema():
    schema = Schema.Lookup(CONNECTION_SCHEMA_GUID)
    if schema is not None:
        return schema
    builder = SchemaBuilder(CONNECTION_SCHEMA_GUID)
    builder.SetSchemaName("ConnectionToolsLink")
    builder.SetReadAccessLevel(AccessLevel.Public)
    builder.SetWriteAccessLevel(AccessLevel.Public)
    builder.AddSimpleField(CONNECTION_SCHEMA_FIELD_A, str)
    builder.AddSimpleField(CONNECTION_SCHEMA_FIELD_B, str)
    return builder.Finish()


def tag_connection(section_view, element_a, element_b):
    """Record which two elements this view documents. Must be called
    inside an open transaction."""
    schema = _get_connection_schema()
    entity = Entity(schema)
    entity.Set[str](CONNECTION_SCHEMA_FIELD_A, str(element_a.Id))
    entity.Set[str](CONNECTION_SCHEMA_FIELD_B, str(element_b.Id))
    section_view.SetEntity(entity)


def documented_pairs():
    """Every element-id pair that already has a section/detail/plan view,
    read back from whatever already exists in the model."""
    schema = Schema.Lookup(CONNECTION_SCHEMA_GUID)
    pairs = set()
    if schema is None:
        return pairs
    for view in DB.FilteredElementCollector(doc).OfClass(DB.View):
        if view.IsTemplate:
            continue
        entity = view.GetEntity(schema)
        if not entity.IsValid():
            continue
        id_a = entity.Get[str](CONNECTION_SCHEMA_FIELD_A)
        id_b = entity.Get[str](CONNECTION_SCHEMA_FIELD_B)
        if id_a and id_b:
            pairs.add(frozenset((id_a, id_b)))
    return pairs


# ------------------------------------------------------------ TYPE PICKER ---

def choose_category():
    """Ask which of the configured categories to filter types from."""
    lookup = {}
    for bic in CATEGORIES:
        category = DB.Category.GetCategory(doc, bic)
        name = category.Name if category is not None else str(bic)
        lookup[name] = bic
    chosen = forms.SelectFromList.show(
        sorted(lookup.keys()),
        title="Select a category",
        button_name="Next"
    )
    if not chosen:
        return None
    return lookup[chosen]


def type_label(element_type):
    family_name = element_type.FamilyName
    name = get_name(element_type)
    if family_name:
        return "{} - {}".format(family_name, name)
    return name


def types_in_category(bic):
    category_filter = DB.ElementCategoryFilter(bic)
    return list(
        DB.FilteredElementCollector(doc)
        .WherePasses(category_filter)
        .WhereElementIsElementType()
    )


def choose_types(bic):
    """Multi-select the family types (within one category) to gather
    instances of."""
    types = types_in_category(bic)
    if not types:
        forms.alert("No types found for that category.", title="Connection Section")
        return []

    lookup = dict((type_label(t), t) for t in types)
    chosen = forms.SelectFromList.show(
        sorted(lookup.keys()),
        title="Select type(s)",
        button_name="Use these types",
        multiselect=True
    )
    if not chosen:
        return []
    return [lookup[name] for name in chosen]


def instances_of_types(bic, type_ids):
    id_set = set(str(tid) for tid in type_ids)
    category_filter = DB.ElementCategoryFilter(bic)
    collector = DB.FilteredElementCollector(doc) \
        .WherePasses(category_filter) \
        .WhereElementIsNotElementType()
    return [e for e in collector if str(e.GetTypeId()) in id_set]


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
        self.kind = kind                # "side" | "top" | "bottom" | "overlap" | "host"
        self.origin = origin            # XYZ, middle of the contact region
        self.joint_dir = joint_dir      # XYZ, horizontal unit vector along the joint
        self.length = length            # feet


def _host_of(element):
    if isinstance(element, DB.FamilyInstance):
        return element.Host
    return None


def _hosted_joint_dir(host):
    """The host's own running direction (a wall's length, say), so a
    section through a door/window cuts across the host the same way a
    floor-on-wall contact does. Falls back to a fixed direction when the
    host has no simple location curve (e.g. hosted in a floor or roof)."""
    location = getattr(host, "Location", None)
    if isinstance(location, DB.LocationCurve):
        curve = location.Curve
        direction = curve.GetEndPoint(1) - curve.GetEndPoint(0)
        return _horizontal(direction, DB.XYZ.BasisX)
    return DB.XYZ.BasisX


def _detect_host_contact(element_a, element_b):
    """Doors, windows, railings and similar hosted family instances always
    touch their host, but Revit usually cuts an opening for them so their
    solids don't actually overlap or share a clean coplanar face the way
    two floors or a floor-on-wall contact would. Element.Host is a direct,
    reliable signal for this case, so it's checked before any geometry."""
    if _host_of(element_b) is not None and _host_of(element_b).Id == element_a.Id:
        hosted, host = element_b, element_a
    elif _host_of(element_a) is not None and _host_of(element_a).Id == element_b.Id:
        hosted, host = element_a, element_b
    else:
        return None

    bbox = get_bounding_box(hosted)
    if bbox is None:
        return None
    origin = bbox.Min.Add(bbox.Max).Multiply(0.5)
    joint_dir = _hosted_joint_dir(host)
    length = max(bbox.Max.X - bbox.Min.X, bbox.Max.Y - bbox.Min.Y, bbox.Max.Z - bbox.Min.Z)
    return Contact(element_b, "host", origin, joint_dir, length)


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
    contact = _detect_host_contact(element_a, element_b)
    if contact is not None:
        return contact
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


def gather_unique_connections(seed_elements):
    """(element_a, Contact) for every connection touching any of the seed
    elements, each unordered pair appearing once even if both of its
    elements are seeds (so it isn't found, and later documented, twice)."""
    seen_pairs = set()
    connections = []
    for seed in seed_elements:
        for contact in find_candidates(seed):
            pair_key = frozenset((str(seed.Id), str(contact.element.Id)))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            connections.append((seed, contact))
    return connections


# Categories treated as "vertical" for deciding whether a side contact
# could be shown as a horizontal Plan callout instead of a vertical
# Section/Detail (e.g. two walls meeting at a corner). A floor-to-floor
# side contact stays Section-only, since neither element is in this set.
VERTICAL_CATEGORIES = (
    DB.BuiltInCategory.OST_Walls,
    DB.BuiltInCategory.OST_StructuralColumns,
    DB.BuiltInCategory.OST_Columns,
    DB.BuiltInCategory.OST_CurtainWallMullions,
    DB.BuiltInCategory.OST_CurtainWallPanels,
)


def _is_vertical_category(element):
    if element.Category is None:
        return False
    return any(
        element.Category.Id == DB.ElementId(bic) for bic in VERTICAL_CATEGORIES
    )


def is_ambiguous_orientation(element_a, contact):
    """True for a side contact between two vertical elements (a wall/column
    corner or T-junction), where a horizontal Plan callout can show the
    connection at least as well as a vertical Section/Detail."""
    return (
        contact.kind == "side"
        and _is_vertical_category(element_a)
        and _is_vertical_category(contact.element)
    )


def choose_view_kind(element_a, contact):
    """Ask whether one ambiguous connection should get a Section/Detail or
    a Plan callout. Returns "section", "plan", or None if skipped."""
    label = "{} ↔ {}".format(
        family_type_label(element_a), family_type_label(contact.element)
    )
    choice = forms.CommandSwitchWindow.show(
        ["Section/Detail", "Plan", "Skip"],
        message="{}: which view fits this connection?".format(label)
    )
    if choice == "Section/Detail":
        return "section"
    if choice == "Plan":
        return "plan"
    return None


# ------------------------------------------------------------------- UI ---

def element_label(element):
    category_name = element.Category.Name if element.Category else "Element"
    return "{} {}".format(category_name, element.Id)


def _get_type(element):
    type_id = element.GetTypeId()
    if type_id == DB.ElementId.InvalidElementId:
        return None
    return doc.GetElement(type_id)


def type_name(element):
    """The element's type name (e.g. a wall or floor type), falling back to
    `element_label` if it has none."""
    element_type = _get_type(element)
    if element_type is not None and get_name(element_type):
        return get_name(element_type)
    return element_label(element)


def family_type_label(element):
    """"Category, Family name, Type name" - e.g.
    "Walls, Basic Wall, Generic - 200mm"."""
    category_name = element.Category.Name if element.Category else "Element"
    parts = [category_name]
    element_type = _get_type(element)
    if element_type is not None:
        if element_type.FamilyName:
            parts.append(element_type.FamilyName)
        if get_name(element_type):
            parts.append(get_name(element_type))
    return ", ".join(parts)


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


# A template can only be applied to a view whose ViewType matches (Revit
# groups templates by ViewType, not raw ViewFamily).
VIEW_FAMILY_TO_VIEW_TYPE = {
    DB.ViewFamily.Section: DB.ViewType.Section,
    DB.ViewFamily.Detail: DB.ViewType.Detail,
    DB.ViewFamily.FloorPlan: DB.ViewType.FloorPlan,
}


def choose_view_template(view_family):
    """Ask which view template (if any) to apply to views of this family,
    e.g. once for the Section/Detail views and once for the Plan callouts
    in a batch. VIEW_TEMPLATE_NAME pins one without asking, as before."""
    if VIEW_TEMPLATE_NAME:
        return _find_view_template(VIEW_TEMPLATE_NAME)

    wanted_view_type = VIEW_FAMILY_TO_VIEW_TYPE.get(view_family)
    templates = [
        v for v in DB.FilteredElementCollector(doc).OfClass(DB.View)
        if v.IsTemplate and (wanted_view_type is None or v.ViewType == wanted_view_type)
    ]
    if not templates:
        return None

    lookup = dict((get_name(t), t) for t in templates)
    options = ["(none)"] + sorted(lookup.keys())
    chosen = forms.SelectFromList.show(
        options,
        title="Select a view template to apply (optional)",
        button_name="Use this template"
    )
    if not chosen or chosen == "(none)":
        return None
    return lookup[chosen]


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
        get_name(v) for v in DB.FilteredElementCollector(doc).OfClass(DB.View)
        if not v.IsTemplate
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
    inside an open transaction. The caller applies a view template, if any
    (see choose_view_template)."""
    element_b = contact.element
    section_box = build_section_box(element_a, element_b, contact)
    name = unique_section_name(type_name(element_a), type_name(element_b))

    section_view = DB.ViewSection.CreateSection(doc, creation_type.Id, section_box)
    if final_type.Id != creation_type.Id:
        section_view.ChangeTypeId(final_type.Id)
    set_name(section_view, name)
    return section_view


# ------------------------------------------------------------------ PLAN ---

def _nearest_level(z):
    levels = list(DB.FilteredElementCollector(doc).OfClass(DB.Level))
    if not levels:
        return None
    return min(levels, key=lambda lvl: abs(lvl.Elevation - z))


def _floor_plan_for_level(level):
    for view in DB.FilteredElementCollector(doc).OfClass(DB.ViewPlan):
        if view.IsTemplate or view.ViewType != DB.ViewType.FloorPlan:
            continue
        gen_level = view.GenLevel
        if gen_level is not None and gen_level.Id == level.Id:
            return view
    return None


def create_plan_callout(element_a, contact):
    """Create a Plan callout, cropped around the joint, on the existing
    Floor Plan view of the connection's level. Must be called inside an
    open transaction. Returns (view, error_message) - view is None and
    error_message is set when the callout couldn't be created (e.g. no
    Floor Plan view exists yet for that level). The caller applies a view
    template, if any (see choose_view_template)."""
    element_b = contact.element
    level = _nearest_level(contact.origin.Z)
    if level is None:
        return None, "no Level found in the project"

    owner_view = _floor_plan_for_level(level)
    if owner_view is None:
        return None, "no Floor Plan view exists for level '{}'".format(get_name(level))

    callout_type = doc.GetElement(owner_view.GetTypeId())
    if callout_type is None:
        return None, "the level's Floor Plan view has no view type"

    half_width = mm(SECTION_WIDTH_MM) / 2.0
    origin = contact.origin
    point1 = DB.XYZ(origin.X - half_width, origin.Y - half_width, 0)
    point2 = DB.XYZ(origin.X + half_width, origin.Y + half_width, 0)

    callout_view = DB.ViewSection.CreateCallout(
        doc, owner_view.Id, callout_type.Id, point1, point2
    )
    name = unique_section_name(type_name(element_a), type_name(element_b))
    set_name(callout_view, name)
    return callout_view, None


# --------------------------------------------------------------------- ---

def main():
    output.show()
    output.print_md("**Filter which existing families and types to connect.**")

    bic = choose_category()
    if bic is None:
        output.print_md("Cancelled — no category was selected.")
        return

    types = choose_types(bic)
    if not types:
        output.print_md("Cancelled — no type was selected.")
        return
    output.print_md(
        "**Type(s):** {}".format(", ".join(type_label(t) for t in types))
    )

    seed_elements = instances_of_types(bic, [t.Id for t in types])
    output.print_md("**{}** instance(s) of the selected type(s) found.".format(len(seed_elements)))
    if not seed_elements:
        return

    raw_connections = gather_unique_connections(seed_elements)
    already_documented = documented_pairs()
    connections = [
        (a, c) for (a, c) in raw_connections
        if frozenset((str(a.Id), str(c.element.Id))) not in already_documented
    ]
    skipped = len(raw_connections) - len(connections)
    output.print_md(
        "Found **{}** connection(s) to document{}.".format(
            len(connections),
            " (**{}** already documented, skipped)".format(skipped) if skipped else ""
        )
    )
    if not connections:
        return

    # A wall/column-to-wall/column side contact (a corner or T-junction)
    # could be a vertical Section/Detail or a horizontal Plan callout;
    # everything else (floor-to-floor, top/bottom, host, overlap) only
    # makes sense as a Section/Detail.
    section_jobs = []
    plan_jobs = []
    for element_a, contact in connections:
        if is_ambiguous_orientation(element_a, contact):
            kind = choose_view_kind(element_a, contact)
            if kind is None:
                continue
        else:
            kind = "section"
        if kind == "plan":
            plan_jobs.append((element_a, contact))
        else:
            section_jobs.append((element_a, contact))

    if not section_jobs and not plan_jobs:
        output.print_md("Nothing left to create.")
        return

    creation_type = None
    view_family_type = None
    section_template = None
    if section_jobs:
        view_family_type = choose_section_type()
        if view_family_type is None:
            section_jobs = []
        else:
            creation_type = resolve_creation_type(view_family_type)
            if creation_type is None:
                forms.alert(
                    "No Section view type exists in this project to create "
                    "the view with (one is needed even to create a Detail "
                    "View type).",
                    title="Connection Section"
                )
                section_jobs = []
            else:
                section_template = choose_view_template(view_family_type.ViewFamily)

    plan_template = None
    if plan_jobs:
        plan_template = choose_view_template(DB.ViewFamily.FloorPlan)

    if not section_jobs and not plan_jobs:
        return

    created_views = []
    plan_failures = []
    with revit.Transaction("Create Connection Views"):
        for element_a, contact in section_jobs:
            section_view = create_section_view(element_a, contact, creation_type, view_family_type)
            if section_template is not None:
                section_view.ViewTemplateId = section_template.Id
            tag_connection(section_view, element_a, contact.element)
            created_views.append(section_view)
            output.print_md(
                "Created **{}** (section) — {} contact with {}, {:.0f} mm long".format(
                    get_name(section_view), contact.kind,
                    element_label(contact.element), to_mm(contact.length)
                )
            )

        for element_a, contact in plan_jobs:
            callout_view, error = create_plan_callout(element_a, contact)
            if callout_view is None:
                plan_failures.append(
                    "{} ↔ {}: {}".format(
                        element_label(element_a), element_label(contact.element), error
                    )
                )
                continue
            if plan_template is not None:
                callout_view.ViewTemplateId = plan_template.Id
            tag_connection(callout_view, element_a, contact.element)
            created_views.append(callout_view)
            output.print_md(
                "Created **{}** (plan callout) — {} contact with {}".format(
                    get_name(callout_view), contact.kind, element_label(contact.element)
                )
            )

    if plan_failures:
        output.print_md("**Skipped {} plan callout(s):**".format(len(plan_failures)))
        for note in plan_failures:
            output.print_md("- {}".format(note))

    if not created_views:
        return

    uidoc.ActiveView = created_views[-1]
    output.print_md("Done — created **{}** connection view(s).".format(len(created_views)))


main()
