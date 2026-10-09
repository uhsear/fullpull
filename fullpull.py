"""Download every layer of an ArcGIS REST service into a File Geodatabase.

Works against MapServer and FeatureServer endpoints. Runs from a shell or as an ArcGIS
script tool; a script tool passes its parameters as positional arguments, so the same
argument list serves both.

The contract that matters: a layer either lands complete or it raises. Every layer is
verified against the server's own record count before it is accepted.

Four paging strategies back that contract, cheapest first: resultOffset paging, objectId
batching, objectId range windows, and a recursive envelope quadtree. A strategy that
raises, or that finishes with the wrong record count, hands the layer to the next one.
Only an exhausted list fails the layer. --resume skips layers an interrupted run finished.

Why not just hand the REST URL to arcpy.conversion.ExportFeatures, which also works and
also pages correctly? Measured on Public/PublicWorks/0 (3,317 records): ExportFeatures
took 254s, this script took 1.4s. Roughly 180x, so the manual paging earns its keep.

Edit the CONFIG block below and run with no arguments, or pass everything on the command
line. Command-line arguments win over CONFIG when both are set.

    python fullpull.py                          # uses CONFIG below
    python fullpull.py <service_url> <output_folder>
    python fullpull.py <service_url> <output_folder> --resume
    python fullpull.py --self-check
"""

import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

try:
    import arcpy
except ModuleNotFoundError:
    # arcpy ships only with ArcGIS Pro's bundled Python, not a plain system install.
    # Without this, a first run on the wrong interpreter is an opaque import error.
    sys.exit(
        "arcpy was not found. Run this with the Python that ships with ArcGIS Pro:\n"
        r'  "C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" '
        "fullpull.py\n"
        r"or the propy.bat in ...\Pro\bin\Python\Scripts\."
    )

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# =============================================================================
# CONFIG. Edit this block, then run the script with no arguments.
# =============================================================================

# The service to download. A MapServer or FeatureServer URL, not a folder URL.
# e.g. "https://example.gov/arcgis/rest/services/Public/Utilities/MapServer"
SERVICE_URL = ""

# Existing folder that will hold the file geodatabase. Relative paths resolve
# against the directory you run the script from.
OUTPUT_FOLDER = "downloads"

# Geodatabase name. Leave "" to name it after the service, so pulling two services
# into one folder cannot have the second overwrite the first.
GDB_NAME = ""

# Token for a secured service. Leave "" for public services.
TOKEN = ""

# Output spatial reference as a WKID, e.g. "2237". Leave "" to keep the layer's own SR.
# Do not leave this to chance on a MapServer: its default is the *map's* SR, which need
# not match the layer's, so an unset outSR can silently hand back reprojected geometry.
OUT_SR = ""

# Server-side row filter applied to every layer. "1=1" means all rows.
WHERE = "1=1"

# Ceiling on a single page, regardless of what the service advertises. A service's
# maxRecordCount is not a promise it can honour: one layer tested here advertises
# 250000 and returns HTTP 500 above ~25000. At 5000 features a page is ~9 MB and runs
# ~3.6k records/sec, most of the available throughput at a fraction of the memory.
# Raising this trades memory and 500-risk for a little speed.
MAX_PAGE = 5_000

# Seconds before a single HTTP request is abandoned, and how many times to retry it
# with exponential backoff on 429/500/502/503/504.
TIMEOUT = 180
RETRIES = 4

# How many times the envelope strategy may subdivide before it stops splitting a cell
# and fetches that cell by its ID list instead. Only cells that come back full are split,
# so depth costs nothing on the empty parts of an extent. The cap matters for the case
# subdivision cannot fix -- more coincident features at one coordinate than the page size,
# where every split hands the whole pile to one child forever. Lower it to reach the
# ID-list fallback sooner on dense data; raise it if a cell's ID list is itself too big.
# Only the last-resort strategy uses this; the three before it never subdivide.
MAX_ENVELOPE_DEPTH = 12

# Layer the --self-check exercises. Defaults to Esri's public sample server, which exists
# for this purpose. Point it at your own server rather than sending test traffic to
# someone else's production service. Two constraints, or the check proves nothing:
# the layer's record count must exceed its maxRecordCount, and that maxRecordCount must
# be below 5000 (otherwise the naive-stride assertion cannot demonstrate the bug).
# SF311 layer 0: 9,687 records, maxRecordCount 1000.
SELF_CHECK_URL = "https://sampleserver6.arcgisonline.com/arcgis/rest/services/SF311/FeatureServer"
SELF_CHECK_LAYER = 0

# =============================================================================
# End of CONFIG.
# =============================================================================


# --------------------------------------------------------------------------- http


def make_session(retries=None):
    """Session with backoff on the transient statuses ArcGIS Server actually emits."""
    s = requests.Session()
    retry = Retry(
        total=RETRIES if retries is None else retries,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


def rest_get(session, url, params=None, token=None, timeout=None):
    """Fetch an ArcGIS REST resource as JSON.

    ArcGIS Server signals most failures with HTTP 200 and an {"error": ...} body, so
    raise_for_status() alone silently accepts dead services. Both paths are checked.
    """
    p = dict(params or {})
    p.setdefault("f", "json")
    if token:
        p["token"] = token
    # Long where clauses / objectIds lists blow past URL length limits; POST is
    # accepted by every ArcGIS Server for /query and is safe for metadata too.
    resp = session.post(url, data=p, timeout=TIMEOUT if timeout is None else timeout)
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(f"Non-JSON response from {url}: {resp.text[:200]}")
    if isinstance(data, dict) and "error" in data:
        err = data["error"]
        raise RuntimeError(
            f"ArcGIS error {err.get('code')} from {url}: "
            f"{err.get('message')} {'; '.join(err.get('details') or [])}".strip()
        )
    return data


# ---------------------------------------------------------------- service metadata


def list_layers(session, service_url, token=None):
    """Queryable layers and standalone tables, group layers excluded.

    Group layers carry subLayerIds and hold no rows of their own; querying them
    errors or returns nothing useful.
    """
    info = rest_get(session, service_url, token=token)
    # Pasting a folder URL instead of a service URL is the easy mistake to make; a
    # folder answers with services/folders and no layers, which is otherwise silent.
    if "layers" not in info and "tables" not in info and ("services" in info or "folders" in info):
        names = list(dict.fromkeys(s["name"] for s in (info.get("services") or [])))[:8]
        raise RuntimeError(
            f"{service_url} is a service folder, not a service. "
            f"Append a service, e.g.: {', '.join(names) if names else '(none listed)'}"
        )
    items = []
    for entry in info.get("layers") or []:
        # Group layers hold no rows of their own. Network Analysis / Network Dataset
        # layers are not queryable either and answer a count query with HTTP 400, so
        # including them turns a clean Routing pull into a run full of failures.
        # Say which ones were dropped, because a silent skip looks identical to a bug.
        if entry.get("subLayerIds"):
            arcpy.AddMessage(f"Skipping group layer {entry['id']}: {entry.get('name')}")
            continue
        if entry.get("type") not in (None, "Feature Layer", "Table"):
            arcpy.AddMessage(
                f"Skipping non-queryable layer {entry['id']}: "
                f"{entry.get('name')} ({entry.get('type')})"
            )
            continue
        items.append({"id": entry["id"], "name": entry.get("name") or f"Layer_{entry['id']}"})
    for entry in info.get("tables") or []:
        items.append({"id": entry["id"], "name": entry.get("name") or f"Table_{entry['id']}"})
    return items, info.get("maxRecordCount")


def oid_field(meta):
    """The OID field name, read from fields[] because objectIdField is often null."""
    if meta.get("objectIdField"):
        return meta["objectIdField"]
    for f in meta.get("fields") or []:
        if f.get("type") == "esriFieldTypeOID":
            return f["name"]
    return None


def page_size(layer_meta, service_max):
    """Server-advertised page size.

    This is the whole ballgame. Asking for more than maxRecordCount does not error --
    the server silently returns maxRecordCount rows. Any client that then strides its
    resultOffset by the number it *asked for* skips every record in between.
    """
    for candidate in (layer_meta.get("maxRecordCount"), service_max):
        if isinstance(candidate, int) and candidate > 0:
            return min(candidate, MAX_PAGE)
    return 1000


# ------------------------------------------------------------------------ paging


def geometry_params(layer_meta, out_sr=None):
    """Query parameters that decide what geometry actually comes back.

    outSR is pinned to the layer's own source SR rather than left to the service default.
    A MapServer defaults to the *map's* spatial reference, which need not match the layer's,
    so omitting outSR can silently hand back reprojected coordinates.

    returnZ/returnM default to false server-side, which quietly flattens a Z- or M-enabled
    layer to 2D. They are only requested when the layer claims to have them.
    """
    params = {}
    wkid = out_sr
    if not wkid:
        src = layer_meta.get("sourceSpatialReference") or layer_meta.get("extent", {}).get(
            "spatialReference", {}
        )
        wkid = src.get("latestWkid") or src.get("wkid")
    if wkid:
        params["outSR"] = str(wkid)
    if layer_meta.get("hasZ"):
        params["returnZ"] = "true"
    if layer_meta.get("hasM"):
        params["returnM"] = "true"
    return params


def iter_pages(session, layer_url, size, oid, expected, token=None, geom=None, where="1=1"):
    """Yield Esri JSON featureset pages covering the whole layer.

    The offset advances by how many features actually came back, never by how many were
    asked for. A server may serve fewer than requested, which is exactly how the previous
    version lost 45% of its records, so a short page is treated as normal, not as the end.

    Termination is driven by the server's own record count where it is available, which is
    independent of the exceededTransferLimit flag that some services omit. An empty page
    always breaks the loop; any resulting shortfall is caught by the caller's verification.
    """
    params = {"where": where, "outFields": "*", "returnGeometry": "true", **(geom or {})}
    # Stable paging order. resultOffset without an ORDER BY is not guaranteed to be
    # consistent between requests, which can duplicate some rows and drop others.
    if oid:
        params["orderByFields"] = oid

    fetched = 0
    while expected is None or fetched < expected:
        page = rest_get(
            session,
            f"{layer_url}/query",
            {**params, "resultOffset": fetched, "resultRecordCount": size},
            token=token,
        )
        feats = page.get("features") or []
        if not feats:
            return
        yield page
        fetched += len(feats)
        if expected is None and not page.get("exceededTransferLimit"):
            return


def iter_pages_by_oid(session, layer_url, size, token=None, geom=None, where="1=1"):
    """Fallback for services that do not support resultOffset paging.

    Pulls the full ID list once, then fetches it in batches of `size`.
    """
    ids = rest_get(
        session, f"{layer_url}/query",
        {"where": where, "returnIdsOnly": "true"}, token=token,
    ).get("objectIds") or []
    params = {"outFields": "*", "returnGeometry": "true", **(geom or {})}
    yield from _pages_by_ids(session, layer_url, ids, size, params, token=token)


def _pages_by_ids(session, layer_url, ids, size, params, token=None):
    """Fetch an explicit ID list in batches of `size`. Shared by two strategies."""
    ids = sorted(ids or [])
    for i in range(0, len(ids), size):
        batch = ids[i:i + size]
        page = rest_get(
            session, f"{layer_url}/query",
            {**params, "objectIds": ",".join(str(x) for x in batch)}, token=token,
        )
        if page.get("features"):
            yield page


def iter_pages_by_oid_range(session, layer_url, size, oid, token=None, geom=None, where="1=1"):
    """Fallback for a service that will not hand over the whole ID list at once.

    returnIdsOnly=true is one request whose *response* grows with the layer, so the
    layer that answers a 25,000-row query with HTTP 500 can refuse the ID list too.
    This asks only for the smallest and largest OID, then walks that range in windows
    of `size` expressed as a where clause. Nothing about the request or the response
    grows with the layer.

    OIDs are unique, so a window `size` wide can never contain more than `size` rows.
    That is what makes this safe where the previous two strategies are not: no window
    can exceed the page size, so no window can be silently truncated. Sparse OIDs cost
    extra empty requests, never lost records.
    """
    if not oid:
        raise RuntimeError("objectId range paging needs an OID field; the layer publishes none")
    stats = [
        {"statisticType": "min", "onStatisticField": oid, "outStatisticFieldName": "oid_min"},
        {"statisticType": "max", "onStatisticField": oid, "outStatisticFieldName": "oid_max"},
    ]
    row = rest_get(
        session, f"{layer_url}/query",
        {"where": where, "outStatistics": json.dumps(stats)}, token=token,
    )
    attrs = ((row.get("features") or [{}])[0].get("attributes")) or {}
    lo, hi = attrs.get("oid_min"), attrs.get("oid_max")
    if lo is None or hi is None:
        raise RuntimeError("server returned no OID statistics")

    params = {"outFields": "*", "returnGeometry": "true", "orderByFields": oid, **(geom or {})}
    start, end_of_layer = int(lo), int(hi)
    while start <= end_of_layer:
        stop = start + size - 1
        window = f"{oid} >= {start} AND {oid} <= {stop}"
        clause = window if where in (None, "", "1=1") else f"({where}) AND {window}"
        page = rest_get(
            session, f"{layer_url}/query", {**params, "where": clause}, token=token,
        )
        if page.get("features"):
            yield page
        start = stop + 1


def _split_envelope(env):
    """Split an extent into four quadrants, carrying its spatialReference along."""
    xmid = (env["xmin"] + env["xmax"]) / 2.0
    ymid = (env["ymin"] + env["ymax"]) / 2.0
    corners = (
        (env["xmin"], env["ymin"], xmid, ymid),
        (xmid, env["ymin"], env["xmax"], ymid),
        (env["xmin"], ymid, xmid, env["ymax"]),
        (xmid, ymid, env["xmax"], env["ymax"]),
    )
    quads = []
    for xmin, ymin, xmax, ymax in corners:
        quad = {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax}
        if env.get("spatialReference"):
            quad["spatialReference"] = env["spatialReference"]
        quads.append(quad)
    return quads


def _query_extent(session, layer_url, where, token=None):
    """The extent of the rows that match `where`, or None if the server will not say.

    The published layer extent is a promise nobody checks. SF311 on Esri's own sample
    server publishes -180..180, a whole world, for data that fits inside San Francisco.
    Subdividing a world extent to reach a city wastes eight levels before the first row
    appears. Asking the server for the extent of the actual result set starts the tree
    where the data is. One extra request, and the strategy degrades to the published
    extent if the server does not support it.
    """
    try:
        env = rest_get(
            session, f"{layer_url}/query",
            {"where": where, "returnExtentOnly": "true"}, token=token,
        ).get("extent")
    except Exception:
        return None
    if not env or env.get("xmin") is None or env.get("xmax") is None:
        return None
    # A degenerate extent (one point, or NaN from an empty result) cannot be split.
    if not (env["xmax"] > env["xmin"] and env["ymax"] > env["ymin"]):
        return None
    return env


def iter_pages_by_envelope(session, layer_url, size, oid, meta,
                           token=None, geom=None, where="1=1", max_depth=None):
    """Last resort: recursively subdivide the layer's extent until every query is small.

    The three strategies above all ask the server to hand back a known slice of the
    layer. This one never asks for a slice that could be too big in the first place: it
    queries an envelope, and any envelope that comes back full is assumed truncated and
    split into quadrants. Requests only ever get smaller, which is why this survives a
    layer that returns HTTP 500 above some row count. It is also the slowest strategy by
    a wide margin, which is why it is tried last.

    A feature straddling a quadrant boundary is returned by both queries, so rows are
    de-duplicated on the OID before they are yielded. That is the whole reason this
    strategy needs an OID field.
    """
    if not oid:
        raise RuntimeError("envelope paging needs an OID field to de-duplicate on; none found")
    if not meta.get("geometryType"):
        raise RuntimeError("envelope paging needs geometry; this is a table")
    extent = _query_extent(session, layer_url, where, token) or meta.get("extent") or {}
    if extent.get("xmin") is None or extent.get("xmax") is None:
        raise RuntimeError("layer publishes no extent; envelope paging has nowhere to start")

    cap = MAX_ENVELOPE_DEPTH if max_depth is None else max_depth
    params = {
        "where": where, "outFields": "*", "returnGeometry": "true",
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        **(geom or {}),
    }
    sr = extent.get("spatialReference") or {}
    in_sr = sr.get("latestWkid") or sr.get("wkid")
    if in_sr:
        params["inSR"] = str(in_sr)

    # A row with no geometry is invisible to every spatial query, so no amount of
    # subdivision will ever reach it. Measured on Esri's own SF311 sample layer: 9,705
    # rows, of which 9,703 answer a full-extent envelope query. Find that out here, in
    # two requests, and refuse -- rather than sweeping the whole layer and handing back
    # a short result that looks like a successful download.
    counted = {"where": where, "returnCountOnly": "true"}
    total = rest_get(session, f"{layer_url}/query", counted, token=token).get("count")
    reachable = rest_get(
        session, f"{layer_url}/query",
        {**counted, "geometry": json.dumps(extent),
         "geometryType": "esriGeometryEnvelope",
         "spatialRel": "esriSpatialRelIntersects",
         **({"inSR": str(in_sr)} if in_sr else {})},
        token=token,
    ).get("count")
    if total is not None and reachable is not None and reachable < total:
        raise RuntimeError(
            f"{total - reachable} of {total} rows have no geometry; "
            "envelope paging cannot reach them"
        )

    seen = set()
    stack = [(extent, 0)]
    while stack:
        env, depth = stack.pop()
        page = rest_get(
            session, f"{layer_url}/query",
            {**params, "geometry": json.dumps(env), "resultRecordCount": size},
            token=token,
        )
        feats = page.get("features") or []
        # A full page is indistinguishable from a truncated one, so treat it as
        # truncated. Splitting a quadrant that was in fact complete costs four
        # requests; trusting a full page that was truncated loses records.
        if len(feats) >= size or page.get("exceededTransferLimit"):
            if depth >= cap:
                # Subdivision has stopped helping: either the rows are coincident, or
                # the cell is small enough that the ID list for it is safe to ask for
                # even though the whole layer's was not. Fetch this cell by ID and
                # move on, so one dense block cannot fail the layer.
                cell = {k: v for k, v in params.items() if k != "resultRecordCount"}
                ids = rest_get(
                    session, f"{layer_url}/query",
                    {**cell, "geometry": json.dumps(env), "returnIdsOnly": "true"},
                    token=token,
                ).get("objectIds") or []
                for page in _pages_by_ids(session, layer_url, ids, size, params, token=token):
                    fresh = [f for f in page["features"]
                             if f.get("attributes", {}).get(oid) not in seen]
                    seen.update(f["attributes"][oid] for f in fresh)
                    if fresh:
                        yield {**page, "features": fresh}
                continue
            stack.extend((quad, depth + 1) for quad in _split_envelope(env))
            continue
        fresh = [f for f in feats if f.get("attributes", {}).get(oid) not in seen]
        seen.update(f["attributes"][oid] for f in fresh)
        if fresh:
            yield {**page, "features": fresh}


# ---------------------------------------------------------------------- ingestion


def _write_page(page, target, tmpdir, seq):
    """Write one page to the target, creating it on the first call and appending after.

    Converting page by page keeps peak memory at a single page. Accumulating pages in
    memory first was measurably worse: 47k polylines buffered at once peaked at 2.8 GB.
    """
    path = os.path.join(tmpdir, f"page_{seq}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(page, fh)
    try:
        if not arcpy.Exists(target):
            arcpy.conversion.JSONToFeatures(path, target)
        else:
            staging = arcpy.CreateUniqueName("stage", "memory")
            arcpy.conversion.JSONToFeatures(path, staging)
            try:
                # NO_TEST: pages of one layer share a schema; skipping validation is
                # markedly faster and avoids spurious field-mapping failures.
                arcpy.management.Append(staging, target, "NO_TEST")
            finally:
                arcpy.management.Delete(staging)
    finally:
        os.remove(path)


def unique_name(name, gdb, taken):
    """GDB-legal, collision-free feature class name."""
    base = arcpy.ValidateTableName(name, gdb)
    candidate, n = base, 1
    while candidate.lower() in taken:
        n += 1
        candidate = f"{base[:max(1, 60 - len(str(n)))]}_{n}"
    taken.add(candidate.lower())
    return candidate


def download_layer(session, service_url, layer, gdb, service_max, taken,
                   token=None, out_sr=None, where="1=1"):
    """Download one layer into the geodatabase. Returns (name, count). Raises on shortfall."""
    layer_url = f"{service_url}/{layer['id']}"
    meta = rest_get(session, layer_url, token=token)
    size = page_size(meta, service_max)
    oid = oid_field(meta)
    # Default to NOT paged when the capability is absent. ArcGIS Server before 10.3 omits
    # advancedQueryCapabilities entirely and silently ignores resultOffset, so assuming
    # pagination there re-serves page one forever. When the row count happens to divide
    # evenly by the page size the totals still match and verification passes on duplicated
    # data, exactly the silent corruption this tool exists to prevent. Anything modern
    # publishes supportsPagination explicitly, so the fast path is unaffected.
    paged = (meta.get("advancedQueryCapabilities") or {}).get("supportsPagination", False)

    expected = rest_get(
        session, f"{layer_url}/query",
        {"where": where, "returnCountOnly": "true"}, token=token,
    ).get("count")

    name = unique_name(layer["name"], gdb, taken)
    target = os.path.join(gdb, name)

    if expected == 0:
        # Say plainly what was left behind. A layer that empties server-side would
        # otherwise keep serving an old copy indefinitely on a clean exit code.
        if arcpy.Exists(target):
            arcpy.AddWarning(
                f"{layer['name']}: 0 records on server; existing {name} left in place "
                "and is now stale. Delete it if the layer is meant to be empty."
            )
        else:
            arcpy.AddWarning(f"{layer['name']}: 0 records on server, nothing written.")
        return name, 0

    # Download into a staging class and only replace the existing output once the new
    # copy is complete and verified. Deleting up front means any later failure, such as a
    # dropped connection or a 500 on page 40, destroys last night's good data and
    # leaves nothing in its place.
    # CreateUniqueName already returns a workspace-qualified path; joining it to the
    # workspace again doubles the prefix and breaks on any relative output folder.
    staging = arcpy.CreateUniqueName(f"stg_{name}"[:60], gdb)
    if arcpy.Exists(staging):
        arcpy.management.Delete(staging)

    geom = geometry_params(meta, out_sr)

    # Four strategies, cheapest first, each one surviving a failure mode the one before
    # it does not. A strategy that raises, or that finishes with the wrong record count,
    # hands over to the next instead of failing the layer. Only an exhausted list is a
    # failure. The old behaviour -- one strategy, raise on shortfall -- turned a layer
    # the server merely paged badly into a failed run.
    strategies = []
    if paged:
        strategies.append((
            "resultOffset paging",
            lambda: iter_pages(session, layer_url, size, oid, expected,
                               token=token, geom=geom, where=where),
        ))
    strategies.append((
        "objectId list",
        lambda: iter_pages_by_oid(session, layer_url, size,
                                  token=token, geom=geom, where=where),
    ))
    strategies.append((
        "objectId range",
        lambda: iter_pages_by_oid_range(session, layer_url, size, oid,
                                        token=token, geom=geom, where=where),
    ))
    if meta.get("geometryType"):
        strategies.append((
            "envelope quadtree",
            lambda: iter_pages_by_envelope(session, layer_url, size, oid, meta,
                                           token=token, geom=geom, where=where),
        ))

    got, failures = None, []
    for index, (label, open_pages) in enumerate(strategies):
        tmpdir = tempfile.mkdtemp(prefix="restdl_")
        try:
            # Each attempt starts from an empty staging class. A half-written attempt
            # left in place would be appended to by the next strategy and verify as
            # a duplicate-laden success.
            if arcpy.Exists(staging):
                arcpy.management.Delete(staging)
            for seq, page in enumerate(open_pages()):
                _write_page(page, staging, tmpdir, seq)
            if not arcpy.Exists(staging):
                raise RuntimeError(f"server reported {expected} records but returned none")
            count = int(arcpy.management.GetCount(staging)[0])
            if expected is not None and count != expected:
                raise RuntimeError(f"downloaded {count} of {expected} records")
        except Exception as exc:
            failures.append(f"{label}: {exc}")
            if arcpy.Exists(staging):
                arcpy.management.Delete(staging)
            continue
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        got = count
        if index:
            # Never let a fallback pass silently. A layer that needs the quadtree is a
            # layer whose server is misreporting something, and that is worth knowing.
            arcpy.AddWarning(
                f"{layer['name']}: {label} succeeded after "
                f"{len(failures)} failed strateg{'y' if len(failures) == 1 else 'ies'}: "
                + " | ".join(failures)
            )
        break

    if got is None:
        raise RuntimeError(
            f"{layer['name']}: every strategy failed, existing output left untouched. "
            + " | ".join(failures)
        )

    if arcpy.Exists(target):
        arcpy.management.Delete(target)
    arcpy.management.Rename(staging, target)

    return name, got


# ----------------------------------------------------------------------- driver


def gdb_name_for(service_url):
    """Default geodatabase name derived from the service path.

    A fixed name means pulling two services into one folder puts same-named layers from
    different services in the same geodatabase, where the second run overwrites the first.
    """
    tail = service_url.rstrip("/").split("/services/")[-1]
    for suffix in ("/MapServer", "/FeatureServer"):
        tail = tail.replace(suffix, "")
    safe = "".join(c if c.isalnum() else "_" for c in tail).strip("_")
    return f"{safe or 'OutputData'}.gdb"


def _progress_path(gdb):
    return gdb + ".progress.json"


def _load_progress(path):
    """Completed layers from an earlier interrupted run, or {} if there is no usable file.

    A missing, truncated or hand-edited progress file must never abort a pull. The worst
    case of ignoring it is that every layer is downloaded again, which is what would have
    happened without the file at all.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_progress(path, done):
    """Write the progress file atomically, so a kill mid-write cannot corrupt it."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(done, fh, indent=1)
    os.replace(tmp, path)


def download_service(service_url, output_folder, gdb_name=None,
                     token=None, out_sr=None, where=None, resume=False):
    """Download every queryable layer of a service. Returns list of (name, count)."""
    service_url = service_url.rstrip("/")
    where = where or WHERE
    output_folder = os.path.abspath(output_folder)
    if not os.path.isdir(output_folder):
        raise RuntimeError(f"Output folder does not exist: {output_folder}")

    # Validate the URL before creating anything on disk, so a typo or a folder URL does
    # not leave an empty geodatabase behind.
    session = make_session()
    layers, service_max = list_layers(session, service_url, token=token)
    if not layers:
        raise RuntimeError(f"No queryable layers or tables found at {service_url}")

    gdb_name = gdb_name or gdb_name_for(service_url)
    gdb = os.path.join(output_folder, gdb_name)
    if not arcpy.Exists(gdb):
        arcpy.management.CreateFileGDB(output_folder, gdb_name)

    # Guards collisions within this run. Two layers in one service can legitimately share
    # a name, and ValidateTableName can fold two distinct names onto one; without this the
    # second layer silently replaces the first and both report success.
    taken = set()
    # Resume trusts the earlier run's word that a layer finished; it does not re-verify
    # the count against the server. A layer that changed since that run stays as it was
    # until the next full pull, which is the trade a resume makes.
    progress_path = _progress_path(gdb)
    done = _load_progress(progress_path) if resume else {}

    results, failures = [], []
    for layer in layers:
        key = str(layer["id"])
        prior = done.get(key)
        if prior and arcpy.Exists(os.path.join(gdb, prior["name"])):
            taken.add(prior["name"].lower())
            results.append((prior["name"], prior["count"]))
            arcpy.AddMessage(
                f"{layer['name']}: already complete ({prior['count']} records), skipped"
            )
            continue
        try:
            name, count = download_layer(session, service_url, layer, gdb, service_max,
                                         taken, token=token, out_sr=out_sr, where=where)
            results.append((name, count))
            done[key] = {"name": name, "count": count}
            _save_progress(progress_path, done)
            arcpy.AddMessage(f"{layer['name']}: {count} records -> {os.path.join(gdb, name)}")
        except Exception as exc:
            failures.append((layer["name"], str(exc)))
            arcpy.AddError(f"{layer['name']}: FAILED - {exc}")

    arcpy.AddMessage(f"Done. {len(results)} layer(s) complete, {len(failures)} failed.")
    if failures:
        # A partial run must not exit clean, or a scheduled job reports success on bad data.
        # The progress file survives deliberately: --resume picks up from here.
        raise RuntimeError("Failed layers: " + "; ".join(n for n, _ in failures))
    # Clean run, so the progress file has nothing left to say. Leaving it would make the
    # next --resume run skip the whole service and report success without fetching a row.
    try:
        os.remove(progress_path)
    except OSError:
        pass
    return results


# ------------------------------------------------------------------- self-check


def _offline_checks():
    """Assertions that need no server, run first so a network fault cannot mask a bug.

    The two pieces of logic here are the ones a live check cannot exercise on demand:
    a server that truncates on cue is not something a public sample service provides,
    and an interrupted run is not something a check can stage against production.
    """
    parent = {"xmin": 0.0, "ymin": 0.0, "xmax": 10.0, "ymax": 20.0,
              "spatialReference": {"wkid": 2237}}
    quads = _split_envelope(parent)
    assert len(quads) == 4, "an envelope splits into exactly four quadrants"
    assert all(q["spatialReference"] == parent["spatialReference"] for q in quads), \
        "quadrants must carry the parent spatial reference or the query reprojects"
    area = sum((q["xmax"] - q["xmin"]) * (q["ymax"] - q["ymin"]) for q in quads)
    assert abs(area - 200.0) < 1e-9, "quadrants must tile the parent exactly, no gaps or overlap"
    assert {(q["xmin"], q["ymin"]) for q in quads} == {(0.0, 0.0), (5.0, 0.0),
                                                       (0.0, 10.0), (5.0, 10.0)}, \
        "quadrants must be the four corners, not four copies"
    assert _split_envelope({"xmin": 0, "ymin": 0, "xmax": 1, "ymax": 1})[0].get(
        "spatialReference") is None, "an extent with no SR must not gain an empty one"

    tmp = tempfile.mkdtemp(prefix="restdl_progress_")
    try:
        path = os.path.join(tmp, "x.gdb.progress.json")
        assert _load_progress(path) == {}, "a missing progress file reads as nothing done"
        _save_progress(path, {"0": {"name": "Roads", "count": 12}})
        assert _load_progress(path)["0"]["count"] == 12, "progress must survive a round trip"
        _save_progress(path, {"0": {"name": "Roads", "count": 12},
                              "3": {"name": "Signs", "count": 4}})
        assert len(_load_progress(path)) == 2, "a later save must not lose an earlier layer"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"0": {"name": "Ro')
        assert _load_progress(path) == {}, "a truncated progress file must not raise"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("[1, 2, 3]")
        assert _load_progress(path) == {}, "a progress file of the wrong shape must not raise"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # Prefix of --self-check (the longest flag). Parser must refuse it, not expand it.
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr):
            _parser().parse_args(["--self"])
        refused = False
    except SystemExit:
        refused = True
    assert refused, "a unique prefix of a flag must be refused, not expanded"  # <-- pinned defect

    print("offline checks OK: 11 assertions")


def self_check():
    """Live check against a layer whose record count exceeds the server page size.

    That shape is the whole point: it is the case where a naive pager silently drops
    rows, so the check proves both that this pager gets them all and that the naive
    stride does not. Configure the target with SELF_CHECK_URL / SELF_CHECK_LAYER.
    """
    _offline_checks()

    url = SELF_CHECK_URL
    session = make_session()
    layer_url = f"{url}/{SELF_CHECK_LAYER}"
    meta = rest_get(session, layer_url)
    size = page_size(meta, None)
    expected = rest_get(session, f"{layer_url}/query",
                        {"where": "1=1", "returnCountOnly": "true"})["count"]
    assert expected > size, f"self-check needs count>{size}, layer has {expected}"

    oid = oid_field(meta)

    def page_all(request_size):
        seen = set()
        for page in iter_pages(session, layer_url, request_size, oid, expected):
            for f in page["features"]:
                seen.add(f["attributes"][oid])
        return seen

    assert len(page_all(size)) == expected, "paging at the advertised page size lost records"

    # The regression that matters most: when the server serves fewer rows than were
    # requested, a short page must not be mistaken for the end of the layer.
    assert len(page_all(size * 3)) == expected, "over-large page request lost records"

    # The original defect: striding by more than the server will serve.
    naive = set()
    for off in range(0, expected, 5000):
        page = rest_get(session, f"{layer_url}/query",
                        {"where": "1=1", "outFields": "*", "returnGeometry": "false",
                         "resultOffset": off, "resultRecordCount": 5000})
        for f in page.get("features", []):
            naive.add(f["attributes"][oid])
    assert len(naive) < expected, "expected the naive stride to lose records"

    tmp = tempfile.mkdtemp(prefix="restdl_check_")
    try:
        name, count = download_layer(session, url,
                                     {"id": SELF_CHECK_LAYER, "name": "SelfCheck"},
                                     _scratch_gdb(tmp), None, set())
        assert count == expected, f"download_layer wrote {count}, expected {expected}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"self-check OK: {expected} records, page size {size}, "
          f"naive 5000-stride would have captured only {len(naive)}")


def _scratch_gdb(folder):
    arcpy.management.CreateFileGDB(folder, "check.gdb")
    return os.path.join(folder, "check.gdb")


# ------------------------------------------------------------------------- main


def _parser():
    # allow_abbrev=False: a unique prefix of a flag must not silently select that flag.
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    ap.add_argument("service_url", nargs="?", help="MapServer or FeatureServer URL")
    ap.add_argument("output_folder", nargs="?", help="Folder to hold the file geodatabase")
    ap.add_argument("gdb_name", nargs="?", default="", help="Geodatabase name")
    ap.add_argument("token", nargs="?", default="", help="Token for a secured service")
    ap.add_argument("out_sr", nargs="?", default="", help="Output WKID, e.g. 2237")
    ap.add_argument("where", nargs="?", default="", help="Server-side filter")
    ap.add_argument("--self-check", action="store_true", help="Run the live self-check and exit")
    ap.add_argument("--resume", action="store_true",
                    help="Skip layers an earlier interrupted run already finished")
    return ap


def main(argv=None):
    # One code path for both entry points: an ArcGIS script tool hands its parameters
    # to the script as sys.argv, so positional arguments serve the tool and the shell
    # alike. Unset tool parameters arrive as empty strings, hence the `or default`.
    ap = _parser()
    args = ap.parse_args(argv)

    if args.self_check:
        self_check()
        return 0

    # Anything not supplied on the command line falls back to the CONFIG block, so the
    # script runs bare for the common case and stays scriptable for the rest.
    service_url = args.service_url or SERVICE_URL
    output_folder = args.output_folder or OUTPUT_FOLDER
    if not service_url or not output_folder:
        ap.error("set SERVICE_URL and OUTPUT_FOLDER in CONFIG, or pass them as arguments")

    download_service(
        service_url,
        output_folder,
        args.gdb_name or GDB_NAME or None,
        token=args.token or TOKEN or None,
        out_sr=args.out_sr or OUT_SR or None,
        where=args.where or WHERE,
        resume=args.resume,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
