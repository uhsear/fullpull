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

arcpy.conversion.ExportFeatures is the built-in alternative for one small layer. In the
original build, on one layer of a few thousand records, it took 254s against 1.4s for this
script (request delay off; not rerun since). The checks, not the speed, are the reason this
script exists.

A short read is refused, never accepted. The rows written are compared with the server's
own count, rows that came back twice are refused even when the count matches by accident,
and a server that returns no count at all is refused because completeness cannot be shown.
--attachments pulls the attachment files too, and refuses a batch of features whose
attachment list came back short of the server's own per-feature count.

A run without --apply only reads: it lists the layers and the server's counts and writes
nothing. --apply lets it create the geodatabase and replace layer classes.

Edit the CONFIG block below and run with no arguments, or pass everything on the command
line. Command-line arguments win over CONFIG when both are set.

A token is read from the FULLPULL_TOKEN environment variable, or from standard input when
the token argument is "-". A token typed as an argument shows in the process list.

    python fullpull.py                          # uses CONFIG below
    python fullpull.py <service_url> <output_folder>             # dry run, writes nothing
    python fullpull.py <service_url> <output_folder> --apply
    python fullpull.py <service_url> <output_folder> --apply --resume
    python fullpull.py <service_url> <output_folder> --apply --attachments
    python fullpull.py <service_url> <output_folder> --apply --delay 1
    python fullpull.py --self-check
    python fullpull.py --self-check-live        # the same layer checks against SELF_CHECK_URL
"""

import argparse
import contextlib
import gzip
import http.server
import importlib.util
import io
import json
import logging
import math
import os
import re
import shutil
import socketserver
import sys
import tempfile
import threading
import time
import types
from urllib.parse import parse_qs, quote, quote_plus, urlparse

# Why a real pull cannot run here: None means arcpy imported, otherwise the licence error text.
ARCPY_LICENCE_ERROR = None
try:
    import arcpy
except ImportError:
    # arcpy ships only with ArcGIS Pro's bundled Python, not a plain system install. The
    # import must not kill --self-check, which runs on a stand-in. A real pull exits in main().
    arcpy = None
except RuntimeError as exc:
    # arcpy raises RuntimeError when Pro is installed but its licence is not initialised.
    arcpy = None
    ARCPY_LICENCE_ERROR = str(exc)

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

# Token for a secured service. Leave "" for public services. Prefer the FULLPULL_TOKEN
# environment variable, so the token is not written into a file that may be committed.
TOKEN = ""

# Output spatial reference as a WKID, e.g. "2237". Leave "" to keep the layer's own SR.
# Do not leave this to chance on a MapServer: its default is the *map's* SR, which need
# not match the layer's, so an unset outSR can silently hand back reprojected geometry.
OUT_SR = ""

# Server-side row filter applied to every layer. "1=1" means all rows.
WHERE = "1=1"

# Ceiling on a single page, regardless of what the service advertises. A service's
# maxRecordCount is not a promise it can honour: one layer tested here advertises
# 250000 and returned HTTP 500 above ~25000 in the original build. At 5000 features a page is ~9 MB and runs
# ~3.6k records/sec, most of the available throughput at a fraction of the memory.
# Raising this trades memory and 500-risk for a little speed.
MAX_PAGE = 5_000

# False means a dry run: the tool lists the layers and the server's counts and writes
# nothing. True lets it create the geodatabase and replace layer classes, the same as
# passing --apply. An ArcGIS script tool cannot pass a flag, so set True here for one.
APPLY = False

# Seconds before a single HTTP request is abandoned, and how many times to retry it
# with exponential backoff on 429/500/502/503/504.
TIMEOUT = 180
RETRIES = 4

# Seconds to wait between the start of one request and the start of the next. On by
# default, because many public servers sit behind a web application firewall that bans
# clients for their request rate. It adds half a second per request, and one page holds
# up to MAX_PAGE rows. Set 0 (or pass --delay 0) for your own server, where speed matters.
DELAY = 0.5

# How many times the envelope strategy may subdivide before it stops splitting a cell
# and fetches that cell by its ID list instead. Only cells that come back full are split,
# so depth costs nothing on the empty parts of an extent. The cap matters for the case
# subdivision cannot fix -- more coincident features at one coordinate than the page size,
# where every split hands the whole pile to one child forever. Lower it to reach the
# ID-list fallback sooner on dense data; raise it if a cell's ID list is itself too big.
# Only the last-resort strategy uses this; the three before it never subdivide.
MAX_ENVELOPE_DEPTH = 12

# How many features one attachment query asks about, with --attachments. The query returns
# one group per feature that has attachments, so a batch can never legitimately hold more
# groups than this. Kept below the layer's own page size so a batch cannot reach the cap
# the server truncates at. Smaller batches cost more requests and verify just as strictly.
ATTACH_BATCH = 100

# With --attachments, a server that cannot count attachments per feature (returnCountOnly)
# gives no way to see a list cut short below the server's cap. The pull is refused by
# default. True (or --allow-uncounted-attachments) accepts the cap rule alone and warns.
ALLOW_UNCOUNTED_ATTACHMENTS = False

# Layer --self-check-live exercises. --self-check itself never leaves the machine: it runs
# the same layer checks against a stub server it starts on 127.0.0.1. This one defaults to
# Esri's public sample server, which exists for this purpose. Point it at your own server
# rather than sending test traffic to someone else's production service. Two constraints,
# or the check proves nothing:
# the layer's record count must exceed its maxRecordCount, and that maxRecordCount must
# be below 5000 (otherwise the naive-stride assertion cannot demonstrate the bug).
# SF311 layer 0: 9,705 records on 2026-11-11, maxRecordCount 1000.
SELF_CHECK_URL = "https://sampleserver6.arcgisonline.com/arcgis/rest/services/SF311/FeatureServer"
SELF_CHECK_LAYER = 0

# =============================================================================
# End of CONFIG.
# =============================================================================


# --------------------------------------------------------------------------- http


def make_session(retries=None, delay=None):
    """Session with backoff on the transient statuses ArcGIS Server actually emits."""
    s = requests.Session()
    # Read by rest_get, which paces every request this session makes.
    s.fullpull_delay = DELAY if delay is None else delay
    s.fullpull_last = None
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


def _pace(session):
    """Wait until the session's delay has passed since its previous request started."""
    delay = getattr(session, "fullpull_delay", 0) or 0
    if delay > 0 and session.fullpull_last is not None:
        wait = session.fullpull_last + delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
    session.fullpull_last = time.monotonic()


def rest_get(session, url, params=None, token=None, timeout=None):
    """Fetch an ArcGIS REST resource as JSON.

    ArcGIS Server signals most failures with HTTP 200 and an {"error": ...} body, so
    raise_for_status() alone silently accepts dead services. Both paths are checked.
    """
    p = dict(params or {})
    p.setdefault("f", "json")
    if token:
        p["token"] = token
    _pace(session)
    # Long where clauses / objectIds lists blow past URL length limits; POST is
    # accepted by every ArcGIS Server for /query and is safe for metadata too.
    resp = session.post(url, data=p, timeout=TIMEOUT if timeout is None else timeout)
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(scrub(f"Non-JSON response from {url}: {resp.text[:200]}", token))
    if isinstance(data, dict) and "error" in data:
        err = data["error"]
        raise RuntimeError(scrub(
            f"ArcGIS error {err.get('code')} from {url}: "
            f"{err.get('message')} {'; '.join(err.get('details') or [])}".strip(), token
        ))
    return data


# ---------------------------------------------------------------- service metadata


def _layer_id(entry):
    """The layer id as an int. It goes into a URL, so a server cannot steer it elsewhere."""
    value = entry.get("id")
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError("the service lists a layer with id %r, which is not an integer, so "
                           "it cannot be queried safely" % (value,))
    return value


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
            arcpy.AddWarning(
                f"Skipping non-queryable layer {entry['id']}: "
                f"{entry.get('name')} ({entry.get('type')})"
            )
            continue
        items.append({"id": _layer_id(entry),
                      "name": entry.get("name") or f"Layer_{entry['id']}"})
    for entry in info.get("tables") or []:
        items.append({"id": _layer_id(entry),
                      "name": entry.get("name") or f"Table_{entry['id']}"})
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
        src = layer_meta.get("sourceSpatialReference") or (
            layer_meta.get("extent") or {}).get("spatialReference") or {}
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
    except Exception as exc:
        arcpy.AddMessage("returnExtentOnly failed (%s); using the published layer extent" % exc)
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


# ------------------------------------------------------------ the pure decision core


def short_read_verdict(expected, wrote, repeated):
    """Why a pull is not whole, or None when it is.

    expected is the server's own record count. wrote is the row count read back from the
    staging class, not the number of rows the pages carried, so a row the conversion
    dropped counts as missing. repeated is how many rows came back with an objectId already
    seen. A repeat is refused even when wrote equals expected: that is the page that was
    served twice, and the rows it displaced were never read.
    """
    if repeated:
        return (
            "%d row(s) came back twice, so the pages overlap and only %d distinct of the "
            "server's %d records were read" % (repeated, wrote - repeated, expected)
        )
    if wrote < expected:
        return "downloaded %d of %d records, %d short" % (wrote, expected, expected - wrote)
    if wrote > expected:
        return "downloaded %d records but the server counted %d" % (wrote, expected)
    return None


def unread_verdict(current_ids, seen):
    """Why a finished offset pull is not whole although its count matched, or None.

    current_ids is the server's object id list after the last page. An id the server holds
    now that no page carried means the layer shifted under the offsets, for example one row
    deleted and one added during the pull: the count is unchanged and no id repeats, yet a
    row slid across a page boundary unread. None for current_ids means the list was not
    obtainable, so the pull cannot be shown whole.
    """
    if current_ids is None:
        return ("the server did not list its object ids, so rows changed during the pull "
                "cannot be ruled out")
    have = set(str(i) for i in seen)
    missing = sorted(i for i in current_ids if str(i) not in have)
    if missing:
        return ("%d row(s) the server holds now were never read (first id %s), so the layer "
                "changed while it was being pulled" % (len(missing), missing[0]))
    return None


def current_ids(session, layer_url, where, token=None):
    """The server's object ids for `where` right now, or None when it gives no list."""
    ids = rest_get(session, f"{layer_url}/query",
                   {"where": where, "returnIdsOnly": "true"}, token=token).get("objectIds")
    return ids if isinstance(ids, list) else None


def layer_count(session, layer_url, label, where, token=None):
    """The server's own record count for `where`. Raises when it gives none.

    No count means nothing to verify against. Accepting whatever arrives would be the false
    clean this tool exists to prevent, so a layer the server cannot count is refused.
    """
    expected = rest_get(
        session, f"{layer_url}/query",
        {"where": where, "returnCountOnly": "true"}, token=token,
    ).get("count")
    if not isinstance(expected, int):
        raise RuntimeError(
            "%s: the server returned no record count, so completeness cannot be verified; "
            "refusing the layer" % label
        )
    return expected


def recount_verdict(expected, now):
    """Why a pull is not whole when only the count can be rechecked, or None.

    expected is the count before the pull, now the count after it. Used when the server
    cannot list its object ids, so the ids read cannot be compared with the ids held.
    """
    if now != expected:
        return ("the server counted %d records before the pull and %d after it, so the layer "
                "changed while it was being pulled" % (expected, now))
    return None


def note_ids(page, oid, seen):
    """Add a page's objectIds to `seen`. Returns how many were already there.

    A feature with no value for the OID field cannot be tracked, so it is not counted. That
    leaves the count comparison as the only check for such a layer, which the README names.
    """
    repeats = 0
    for feat in page.get("features") or []:
        value = (feat.get("attributes") or {}).get(oid) if oid else None
        if value is None:
            continue
        if value in seen:
            repeats += 1
        seen.add(value)
    return repeats


def safe_filename(name):
    """A file name that cannot leave its folder, whatever the server called the attachment."""
    clean = re.sub(r"[^A-Za-z0-9._-]", "_", name if isinstance(name, str) else "")
    clean = clean.lstrip(".")[:100]
    return clean or "attachment"


def file_problem(declared, content_length, got):
    """Why a downloaded attachment body is short, or None.

    Esri's page for the attachment infos resource shows `size` with no unit, so a body longer
    than `size` is not refused: only a body shorter than the server lists can mean lost bytes.
    Content-Length is the transport's own claim. It is skipped when the body was compressed in
    transit, because the length on the wire is then not the length written.
    """
    if content_length is not None and got != content_length:
        return "body was %d bytes but Content-Length said %d" % (got, content_length)
    if isinstance(declared, int) and got < declared:
        return "got %d bytes of the %d the server lists" % (got, declared)
    return None


def attachment_batch_problems(batch, groups, counts, cap):
    """Reasons a queryAttachments answer for `batch` cannot be trusted. [] means whole.

    counts maps a feature to the number of attachments the server itself says it has, from
    the returnCountOnly form of the same query, or is None when the server cannot answer that.
    With counts, every feature is compared. Without, the only evidence of truncation is the
    answer reaching the server's own record cap, so that is refused.
    """
    if not isinstance(groups, list):
        return ["attachmentGroups is not a list"]
    wanted = set(batch)
    seen, problems = {}, []
    for group in groups:
        group = group if isinstance(group, dict) else {}
        parent = group.get("parentObjectId")
        infos = group.get("attachmentInfos")
        if parent not in wanted:
            problems.append("a group for feature %r came back that was never asked for" % (parent,))
        elif parent in seen:
            problems.append("feature %s came back twice" % parent)
        elif not isinstance(infos, list) or any(
                not isinstance(i, dict) or isinstance(i.get("id"), bool)
                or not isinstance(i.get("id"), int) for i in infos):
            # The id becomes part of a file name and a URL, so only an integer is trusted.
            problems.append("feature %s has an attachment with no id or a non-integer id" % parent)
        else:
            seen[parent] = infos
    if counts is None:
        total = sum(len(v) for v in seen.values())
        if len(groups) >= cap or total >= cap:
            problems.append(
                "the answer reached the server cap of %d, so it may be cut off" % cap
            )
        return problems
    for parent in sorted(wanted | set(counts)):
        said, got = counts.get(parent, 0), len(seen.get(parent, []))
        if said != got:
            problems.append(
                "feature %s: the server counts %d attachment(s), the query listed %d"
                % (parent, said, got)
            )
    return problems


def scrub(text, token):
    """`text` with the token removed, as sent and in both URL-encoded forms.

    requests writes a space in a query string as a plus sign, quote() as %20, so both are
    removed.
    """
    text = str(text)
    forms = {token, quote(token or "", safe=""), quote_plus(token or "", safe="")}
    for form in forms - {"", None}:
        text = text.replace(form, "<token>")
    return text


class _TokenMask(logging.Filter):
    """Removes every registered token from the log records urllib3 writes about a URL.

    The attachment download is a GET, so its token sits in the query string, and urllib3
    logs the whole path at WARNING level when it retries a dropped connection.
    """

    tokens = set()

    def filter(self, record):
        text = record.getMessage()
        masked = text
        for token in self.tokens:
            masked = scrub(masked, token)
        if masked != text:
            record.msg, record.args = masked, None
        return True


_LOG_MASK = _TokenMask()


def mask_token_in_logs(token):
    """Keep `token` out of the urllib3 log records that quote a request URL."""
    _TokenMask.tokens.add(token)
    for name in ("urllib3.connectionpool", "urllib3.util.retry"):
        logging.getLogger(name).addFilter(_LOG_MASK)


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

    expected = layer_count(session, layer_url, layer["name"], where, token)

    name = unique_name(layer["name"], gdb, taken)
    target = os.path.join(gdb, name)

    if expected == 0:
        # Say plainly what was left behind. A layer that empties server-side would
        # otherwise keep serving an old copy indefinitely on a clean exit code.
        # A server that wrongly answers 0 cannot be told from a layer that was emptied, so
        # a copy that still holds rows is refused rather than kept as stale data on exit 0.
        if arcpy.Exists(target) and int(arcpy.management.GetCount(target)[0]) > 0:
            raise RuntimeError(
                "%s: the server counts 0 records but the existing copy %s holds rows, so it "
                "would be left stale. If the layer really is empty now, delete %s from the "
                "geodatabase and run again" % (layer["name"], name, name)
            )
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
            True,
        ))
    strategies.append((
        "objectId list",
        lambda: iter_pages_by_oid(session, layer_url, size,
                                  token=token, geom=geom, where=where),
        False,
    ))
    strategies.append((
        "objectId range",
        lambda: iter_pages_by_oid_range(session, layer_url, size, oid,
                                        token=token, geom=geom, where=where),
        False,
    ))
    if meta.get("geometryType"):
        strategies.append((
            "envelope quadtree",
            lambda: iter_pages_by_envelope(session, layer_url, size, oid, meta,
                                           token=token, geom=geom, where=where),
            False,
        ))

    got, failures = None, []
    for index, (label, open_pages, shifts) in enumerate(strategies):
        tmpdir = tempfile.mkdtemp(prefix="restdl_")
        try:
            # Each attempt starts from an empty staging class. A half-written attempt
            # left in place would be appended to by the next strategy and verify as
            # a duplicate-laden success.
            if arcpy.Exists(staging):
                arcpy.management.Delete(staging)
            seen_ids, repeated = set(), 0
            for seq, page in enumerate(open_pages()):
                repeated += note_ids(page, oid, seen_ids)
                _write_page(page, staging, tmpdir, seq)
            if not arcpy.Exists(staging):
                raise RuntimeError(f"server reported {expected} records but returned none")
            count = int(arcpy.management.GetCount(staging)[0])
            problem = short_read_verdict(expected, count, repeated)
            # Offsets count rows from the top, so an edit made mid-pull can shift a row
            # across a page boundary with the count unchanged. The other strategies read
            # their id list or id window once, so a row added after that read is never
            # asked for while the count still matches the earlier figure. Every strategy
            # therefore compares the server's ids now with the ids it read. Only the offset
            # strategy refuses when the list cannot be had. The others exist for servers
            # that cannot list ids, so they fall back to comparing the record count taken
            # before the pull with the count now, and refuse when that cannot be had either.
            # Without an OID field there is nothing to compare, which the README names.
            unchecked = False
            if not problem and oid:
                try:
                    now = current_ids(session, layer_url, where, token)
                except (RuntimeError, requests.RequestException):
                    if shifts:
                        raise
                    now = None
                if now is None and not shifts:
                    problem = recount_verdict(
                        expected, layer_count(session, layer_url, layer["name"], where, token))
                    unchecked = not problem
                else:
                    problem = unread_verdict(now, seen_ids)
            if problem:
                raise RuntimeError(problem)
        except Exception as exc:
            failures.append(f"{label}: {exc}")
            if arcpy.Exists(staging):
                arcpy.management.Delete(staging)
            continue
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        got = count
        if unchecked:
            arcpy.AddWarning(
                "%s: the server gave no object id list after the %s pull, so only its record "
                "count was rechecked. A row added while another was deleted cannot be ruled out"
                % (layer["name"], label))
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


# ------------------------------------------------------------------- attachments


def fetch_attachment_infos(session, layer_url, batch, cap, tally, token=None):
    """[(feature, info)] for every attachment of the features in `batch`, or raises.

    Asks queryAttachments for the batch, then asks the same question with returnCountOnly,
    which Esri documents for hosted services. A batch whose list disagrees with the server's
    own per-feature counts is split in two and asked again, because a smaller batch cannot
    overflow a cap the larger one hit. A single feature that still disagrees raises: its
    attachments cannot be shown to be complete. A server that cannot count is refused, unless
    tally["allow_uncounted"] says to accept the cap rule alone.
    """
    url = f"{layer_url}/queryAttachments"
    ids = ",".join(str(x) for x in batch)
    groups = rest_get(session, url, {"objectIds": ids}, token=token).get("attachmentGroups")
    counts = None
    if not tally["counts_unsupported"]:
        try:
            counted = rest_get(session, url, {"objectIds": ids, "returnCountOnly": "true"},
                               token=token).get("attachmentGroups")
            counts = {g["parentObjectId"]: int(g["count"]) for g in counted}
        except requests.RequestException as exc:
            # A 5xx, a 429 or a dead connection says nothing about support, so it must not
            # downgrade the layer to the weaker rule. A 4xx answer means "not supported".
            status = getattr(exc.response, "status_code", None)
            if status is None or status == 429 or status >= 500:
                raise
            tally["counts_unsupported"] = True
        except (RuntimeError, KeyError, TypeError, ValueError):
            # An older or non-hosted service answers with an ArcGIS error, or ignores the form
            # and returns full lists that carry no count. Remember that, so the rest of the
            # layer does not repeat a request that cannot succeed.
            tally["counts_unsupported"] = True
    if counts is None and not tally["allow_uncounted"]:
        raise RuntimeError(
            "the server cannot count attachments per feature (returnCountOnly), so a list "
            "cut short below its record cap could not be seen; refusing. Pass "
            "--allow-uncounted-attachments to accept the cap rule alone")
    problems = attachment_batch_problems(batch, groups, counts, cap)
    if not problems:
        tally["by_count" if counts is not None else "by_cap"] += 1
        return [(g["parentObjectId"], info) for g in groups for info in g["attachmentInfos"]]
    if len(batch) == 1:
        raise RuntimeError("attachments of feature %s cannot be shown complete: %s"
                           % (batch[0], "; ".join(problems)))
    mid = len(batch) // 2
    return (fetch_attachment_infos(session, layer_url, batch[:mid], cap, tally, token)
            + fetch_attachment_infos(session, layer_url, batch[mid:], cap, tally, token))


def download_attachment(session, layer_url, parent, info, folder, token=None):
    """Write one attachment to `folder` and return its size. Raises on a short body.

    The token is sent as a query parameter, because the attachment resource is a GET, so a
    failure message is scrubbed before it is raised: requests puts the whole URL in it.
    """
    url = f"{layer_url}/{parent}/attachments/{info['id']}"
    path = os.path.join(folder, "%s_%s" % (info["id"], safe_filename(info.get("name"))))
    part = path + ".part"
    got = 0
    if token:
        mask_token_in_logs(token)
    _pace(session)
    try:
        resp = session.get(url, params={"token": token} if token else None, stream=True,
                           headers={"Accept-Encoding": "identity"}, timeout=TIMEOUT)
        with contextlib.closing(resp):
            resp.raise_for_status()
            with open(part, "wb") as fh:
                for chunk in resp.iter_content(65536):
                    fh.write(chunk)
                    got += len(chunk)
            length = resp.headers.get("Content-Length")
            if resp.headers.get("Content-Encoding") or not (length or "").isdigit():
                length = None
        problem = file_problem(info.get("size"), None if length is None else int(length), got)
        if problem:
            raise RuntimeError(problem)
        os.replace(part, path)
    except (requests.RequestException, RuntimeError) as exc:
        raise RuntimeError("attachment %s of feature %s: %s"
                           % (info["id"], parent, scrub(exc, token)))
    finally:
        if os.path.exists(part):
            os.remove(part)
    return got


def pull_attachments(session, service_url, layer, name, gdb, expected, token=None, where="1=1",
                     allow_uncounted=False):
    """Download the attachments of a layer. Returns a summary dict, or None for a layer with none.

    The feature list comes from the server, and it must be exactly as long as the verified
    row count: a short ID list would hide whole features from the attachment pass. Then every
    batch is checked as described at fetch_attachment_infos, and every file against its size.
    """
    layer_url = f"{service_url}/{layer['id']}"
    meta = rest_get(session, layer_url, token=token)
    if "hasAttachments" not in meta:
        raise RuntimeError(
            "%s: the layer does not say whether it has attachments (no hasAttachments in its "
            "description), so the tool cannot tell a layer with none from one it cannot list; "
            "refusing rather than reporting none" % layer["name"]
        )
    if not meta.get("hasAttachments"):
        return None
    if not (meta.get("advancedQueryCapabilities") or {}).get("supportsQueryAttachments"):
        raise RuntimeError(
            f"{layer['name']}: the layer has attachments but the server does not advertise "
            "supportsQueryAttachments, so they cannot be listed in batches; refusing rather "
            "than skipping them"
        )
    ids = rest_get(
        session, f"{layer_url}/query",
        {"where": where, "returnIdsOnly": "true"}, token=token,
    ).get("objectIds") or []
    if any(isinstance(i, bool) or not isinstance(i, int) for i in ids):
        raise RuntimeError("%s: the feature list holds an id that is not an integer, so it "
                           "cannot be used in a request or a folder name" % layer["name"])
    ids = sorted(ids)
    if len(ids) != expected:
        raise RuntimeError(
            f"{layer['name']}: the feature list holds {len(ids)} ids for {expected} rows, so "
            "some features would never be asked about their attachments"
        )
    cap = page_size(meta, None)
    step = max(1, min(ATTACH_BATCH, cap - 1))
    # Esri documents the count form for hosted services, gated by this layer property. A
    # layer that says False is not asked, and one that does not say is tried.
    tally = {"counts_unsupported": (meta.get("advancedQueryCapabilities") or {}).get(
                 "supportsQueryAttachmentsCountOnly") is False,
             "allow_uncounted": allow_uncounted, "by_count": 0, "by_cap": 0}
    root = os.path.join(gdb + ".attachments", name)
    files, parents = 0, set()
    for i in range(0, len(ids), step):
        for parent, info in fetch_attachment_infos(session, layer_url, ids[i:i + step],
                                                    cap, tally, token):
            folder = os.path.join(root, str(parent))
            os.makedirs(folder, exist_ok=True)
            download_attachment(session, layer_url, parent, info, folder, token)
            files += 1
            parents.add(parent)
    return {"files": files, "features": len(parents), "by_count": tally["by_count"],
            "by_cap": tally["by_cap"]}


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
    if not isinstance(data, dict):
        return {}
    # An entry the resume cannot use is dropped, so its layer is simply downloaded again.
    return {key: entry for key, entry in data.items()
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
            and isinstance(entry.get("count"), int)}


def _save_progress(path, done):
    """Write the progress file atomically, so a kill mid-write cannot corrupt it."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(done, fh, indent=1)
    os.replace(tmp, path)


def _report_attachments(layer, summary):
    """Say what the attachment pass did, and how strong the evidence for it was."""
    if summary is None:
        arcpy.AddMessage(f"{layer['name']}: no attachments on this layer")
        return
    arcpy.AddMessage(
        "%s: %d attachment(s) for %d feature(s); %d batch(es) checked against the server's "
        "per-feature counts, %d against the server cap only"
        % (layer["name"], summary["files"], summary["features"],
           summary["by_count"], summary["by_cap"])
    )
    if summary["by_cap"]:
        arcpy.AddWarning(
            f"{layer['name']}: this server cannot count attachments per feature "
            "(returnCountOnly), so completeness rests on the cap rule alone"
        )


def _dry_run(session, service_url, layers, token, where, attachments):
    """Read each layer's record count and report the plan. Writes nothing."""
    plan, refused = [], []
    for layer in layers:
        try:
            count = layer_count(session, "%s/%s" % (service_url, layer["id"]),
                                layer["name"], where, token)
        except Exception as exc:
            refused.append((layer["name"], str(exc)))
            arcpy.AddError("%s: WOULD BE REFUSED - %s" % (layer["name"], exc))
            continue
        plan.append((layer["name"], count))
        arcpy.AddMessage("%s: %d records on the server, would be pulled" % (layer["name"], count))
    arcpy.AddMessage(
        "Dry run: %d layer(s) would be pulled%s, %d would be refused. Nothing was written. "
        "Run again with --apply to download." % (
            len(plan), " with their attachments" if attachments else "", len(refused)))
    if refused:
        raise RuntimeError("Layers that would be refused: " + "; ".join(n for n, _ in refused))
    return plan


def download_service(service_url, output_folder, gdb_name=None,
                     token=None, out_sr=None, where=None, resume=False, attachments=False,
                     delay=None, allow_uncounted=False, apply=False):
    """Download every queryable layer of a service. Returns list of (name, count).

    Without apply this only reads: it lists the layers and the server's own counts, writes
    nothing to disk and returns what it would pull. Only apply creates the geodatabase,
    replaces layer classes, writes the progress file and pulls attachments.
    """
    parts = urlparse(service_url)
    if parts.query or parts.fragment:
        # A token pasted from a browser address bar would become part of the geodatabase
        # name, the progress file name and every error message, all written to disk or a log.
        raise RuntimeError(
            "The service URL has a query string or fragment (everything after a ? or #). "
            "Remove it, and pass any token through the FULLPULL_TOKEN environment variable "
            "or standard input (token argument -)."
        )
    if parts.username is not None or parts.password is not None:
        # requests puts the full URL, password included, into its error text, and the default
        # geodatabase name is built from the URL.
        raise RuntimeError(
            "The service URL carries a user name or password (user:password@ before the host). "
            "Remove it, so the password cannot reach an error message or a file name."
        )
    service_url = service_url.rstrip("/")
    where = where or WHERE
    output_folder = os.path.abspath(output_folder)
    if not os.path.isdir(output_folder):
        raise RuntimeError(f"Output folder does not exist: {output_folder}")

    # Validate the URL before creating anything on disk, so a typo or a folder URL does
    # not leave an empty geodatabase behind.
    session = make_session(delay=delay)
    layers, service_max = list_layers(session, service_url, token=token)
    if not layers:
        raise RuntimeError(f"No queryable layers or tables found at {service_url}")

    if not apply:
        return _dry_run(session, service_url, layers, token, where, attachments)

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
        # A finished layer holds only what its run asked for: the same service, filter and
        # output SR. A different ask means the old copy answers another question.
        asked = {"service": service_url, "where": where, "out_sr": str(out_sr) if out_sr else None}
        if prior and any(prior.get(k) != v for k, v in asked.items()):
            arcpy.AddMessage("%s: the earlier run used a different service, filter or output "
                             "SR, so the layer is downloaded again" % layer["name"])
            prior = None
        # A layer finished without its attachments is not finished for a run that wants them.
        if prior and (prior.get("attachments") or not attachments) \
                and arcpy.Exists(os.path.join(gdb, prior["name"])):
            taken.add(prior["name"].lower())
            results.append((prior["name"], prior["count"]))
            arcpy.AddMessage(
                f"{layer['name']}: already complete ({prior['count']} records), skipped"
            )
            continue
        try:
            name, count = download_layer(session, service_url, layer, gdb, service_max,
                                         taken, token=token, out_sr=out_sr, where=where)
            arcpy.AddMessage(f"{layer['name']}: {count} records -> {os.path.join(gdb, name)}")
            if attachments:
                _report_attachments(layer, pull_attachments(
                    session, service_url, layer, name, gdb, count, token=token, where=where,
                    allow_uncounted=allow_uncounted))
            done[key] = dict(asked, name=name, count=count, attachments=bool(attachments))
            _save_progress(progress_path, done)
            results.append((name, count))
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
#
# --self-check never leaves the machine. It swaps in an in-memory stand-in for arcpy and
# starts a stub ArcGIS REST server on 127.0.0.1, so the whole pull runs on any Python that
# has requests, with or without ArcGIS Pro. The stub can be told to misbehave in exactly the
# ways this tool exists to catch. The real arcpy calls are never exercised by it.


class _Harness(object):
    """check() and raises(), with the count the footer reports."""

    def __init__(self, quiet=False):
        self.passed = 0
        self.failed = []
        self.quiet = quiet

    def check(self, cond, label):
        if cond:
            self.passed += 1
        else:
            self.failed.append(label)
        if not self.quiet:
            print("%s  %s" % ("PASS" if cond else "FAIL", label))

    def raises(self, fn, label, exc=RuntimeError, text=None):
        try:
            fn()
        except exc as err:
            self.check(text is None or text in str(err), label)
            return str(err)
        except Exception as err:
            self.check(False, "%s (wrong exception %r)" % (label, err))
        else:
            self.check(False, "%s (no error raised)" % label)
        return ""

    def footer(self):
        print("-" * 68)
        print("%d assertions, %d failed" % (self.passed + len(self.failed), len(self.failed)))
        return 1 if self.failed else 0


@contextlib.contextmanager
def _swapped(name, value):
    """Set a module global for the duration of a block."""
    old = globals()[name]
    globals()[name] = value
    try:
        yield
    finally:
        globals()[name] = old


class _FakeArcpy(object):
    """In-memory stand-in for the arcpy calls this tool makes. Feature classes are lists."""

    def __init__(self):
        self.classes = {}
        self.messages, self.warnings, self.errors = [], [], []
        self.drop_rows = 0
        self.stale = None
        self.skip_deletes = 0
        self.on_message = None
        self._serial = 0
        self.management = types.SimpleNamespace(
            CreateFileGDB=self._create_gdb, Delete=self._delete, Rename=self._rename,
            Append=self._append, GetCount=self._count)
        self.conversion = types.SimpleNamespace(JSONToFeatures=self._from_json)

    def Exists(self, path):
        return path in self.classes or os.path.isdir(path)

    def AddMessage(self, text):
        self.messages.append(text)
        if self.on_message:
            self.on_message(text)

    def AddWarning(self, text):
        self.warnings.append(text)

    def AddError(self, text):
        self.errors.append(text)

    def ValidateTableName(self, name, workspace):
        clean = re.sub(r"[^A-Za-z0-9_]", "_", name)
        return "T" + clean if clean[:1].isdigit() else clean

    def CreateUniqueName(self, base, workspace):
        if self.stale and base.startswith("stg_"):
            return self.stale
        self._serial += 1
        return os.path.join(workspace, "%s_%d" % (base, self._serial))

    def _create_gdb(self, folder, name):
        os.makedirs(os.path.join(folder, name))

    def _delete(self, path):
        if self.skip_deletes and os.path.basename(path).startswith("stg_"):
            self.skip_deletes -= 1
            return
        self.classes.pop(path, None)

    def _rename(self, old, new):
        self.classes[new] = self.classes.pop(old)

    def _append(self, source, target, test):
        self.classes[target].extend(self.classes[source])

    def _count(self, path):
        return [str(len(self.classes[path]))]

    def _from_json(self, path, target):
        with open(path, encoding="utf-8") as fh:
            self.classes[target] = json.load(fh)["features"][self.drop_rows:]


class _Layer(object):
    """One stub layer: its rows, and the faults it is told to show. See _Stub.respond."""

    def __init__(self, n=0, cap=1000, geom=True, oids=None, nogeom=(), coincident=False,
                 name="Pts", table=False, **flags):
        self.cap, self.name, self.table, self.geom, self.f = cap, name, table, geom, flags
        self.rows = []
        for i, oid in enumerate(list(oids) if oids is not None else range(1, n + 1)):
            at = coincident if isinstance(coincident, tuple) else (5.0, 5.0)
            x, y = at if coincident else ((i % 51) * 10.0, (i // 51) * 10.0)
            self.rows.append({"oid": oid, "x": x, "y": y, "geom": geom and oid not in nogeom})
        self.att = {}

    def select(self, p):
        rows = self.rows
        window = re.search(r"OBJECTID >= (\d+) AND OBJECTID <= (\d+)", p.get("where") or "")
        if window:
            lo, hi = int(window.group(1)), int(window.group(2))
            rows = [r for r in rows if lo <= r["oid"] <= hi]
        if p.get("objectIds"):
            ids = set(int(x) for x in p["objectIds"].split(","))
            rows = [r for r in rows if r["oid"] in ids]
        if p.get("geometry"):
            env = json.loads(p["geometry"])
            rows = [r for r in rows if r["geom"] and env["xmin"] <= r["x"] <= env["xmax"]
                    and env["ymin"] <= r["y"] <= env["ymax"]]
        return rows

    def meta(self, lid):
        out = {"id": lid, "name": self.name, "maxRecordCount": self.cap,
               "fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID"}],
               "advancedQueryCapabilities": {
                   "supportsPagination": self.f.get("paging", True),
                   "supportsQueryAttachments": self.f.get("query_att", True)},
               "hasAttachments": bool(self.f.get("has_att", False))}
        if "att_count_cap" in self.f:
            out["advancedQueryCapabilities"]["supportsQueryAttachmentsCountOnly"] = \
                self.f["att_count_cap"]
        if self.f.get("no_has_att"):
            del out["hasAttachments"]
        if not self.f.get("oid_in_fields_only"):
            out["objectIdField"] = "OBJECTID"
        if self.geom:
            out["geometryType"] = "esriGeometryPoint"
            out["extent"] = {"xmin": -10.0, "ymin": -10.0, "xmax": 520.0, "ymax": 500.0,
                             "spatialReference": {"wkid": 102100, "latestWkid": 3857}}
        return out

    def feature(self, row):
        feat = {"attributes": {"OBJECTID": row["oid"], "NAME": "r%d" % row["oid"]}}
        if row["geom"]:
            feat["geometry"] = {"x": row["x"], "y": row["y"]}
        return feat


_ERR = {"error": {"code": 400, "message": "stub refuses", "details": ["on purpose"]}}


class _Stub(object):
    """An ArcGIS REST impostor. Layers live under /svc/FeatureServer, faults in layer.f."""

    def __init__(self):
        self.layers, self.extra, self.extra_tables, self.token = {}, [], [], None
        self.hits, self.flaky = [], set()

    def respond(self, method, path, p):
        """(status, headers, body, claimed_length). claimed_length None means len(body)."""
        self.hits.append((method, path, p))
        if self.token and p.get("token") != self.token and "/misc/" not in path:
            if method == "GET":
                return 403, {}, b"forbidden", None
            return self.json(_ERR)
        if path == "/misc/folder":
            return self.json({"folders": ["F"], "services": [{"name": "A/B"}]})
        if path == "/misc/emptyfolder":
            return self.json({"folders": [], "services": []})
        if path.startswith("/misc/flaky"):
            first = path not in self.flaky
            self.flaky.add(path)
            if first:
                return (429 if path.endswith("429") else 503), {}, b"busy", None
            return self.json({"ok": True})
        if path == "/misc/slow":
            time.sleep(1.5)
            return self.json({})
        if path == "/misc/notjson":
            return 200, {}, b"<html>sign in</html>", None
        if path == "/misc/http500":
            return 500, {}, b"boom", None
        if path == "/misc/errbody":
            return self.json(_ERR)
        if path == "/misc/errplain":
            return self.json({"error": {"code": 500}})
        if path == "/misc/nolayers":
            return self.json({"layers": [], "tables": []})
        if path == "/svc/FeatureServer":
            return self.json(self.service())
        found = re.match(r"^/svc/FeatureServer/(\d+)(?:/(.*))?$", path)
        if not found or int(found.group(1)) not in self.layers:
            return 404, {}, b"no such layer", None
        lid, rest = int(found.group(1)), found.group(2)
        layer = self.layers[lid]
        if rest is None:
            return self.json(layer.meta(lid))
        if rest == "query":
            return self.json(self.query(layer, p))
        if rest == "queryAttachments":
            if p.get("returnCountOnly") == "true" and layer.f.get("att_count_http"):
                return layer.f["att_count_http"], {}, b"no", None
            return self.json(self.query_attachments(layer, p))
        grab = re.match(r"^(\d+)/attachments/(\d+)$", rest)
        return self.download(layer, int(grab.group(1)), int(grab.group(2)))

    @staticmethod
    def json(obj):
        return 200, {"Content-Type": "application/json"}, json.dumps(obj).encode("utf-8"), None

    def service(self):
        layers = [{"id": i, "name": l.name} for i, l in sorted(self.layers.items())
                  if not l.table] + self.extra
        tables = [{"id": i, "name": l.name} for i, l in sorted(self.layers.items())
                  if l.table] + self.extra_tables
        return {"maxRecordCount": 2000, "layers": layers, "tables": tables}

    def query(self, layer, p):
        f = layer.f
        rows = layer.select(p)
        if p.get("returnCountOnly") == "true":
            return {} if f.get("no_count") else {"count": len(rows)}
        if p.get("returnIdsOnly") == "true":
            if f.get("ids_absent"):
                return {}
            if f.get("ids_fail") == "all" or (f.get("ids_fail") and not p.get("geometry")):
                return _ERR
            if f.get("ids_fail_once") and not f.get("ids_failed"):
                f["ids_failed"] = True
                return _ERR
            ids = [str(r["oid"]) if f.get("ids_str") else r["oid"] for r in rows]
            self.add_row_after(layer, "ids")
            return {"objectIdFieldName": "OBJECTID",
                    "objectIds": ids[:-1] if f.get("ids_short") else ids}
        if p.get("outStatistics"):
            if f.get("stats_fail"):
                return _ERR
            ids = [r["oid"] for r in rows]
            self.add_row_after(layer, "stats")
            return {"features": [{"attributes": {
                "oid_min": min(ids) if ids else None, "oid_max": max(ids) if ids else None}}]}
        if p.get("returnExtentOnly") == "true":
            if f.get("extent_fail"):
                return _ERR
            if f.get("extent_nox"):
                return {"extent": {"xmax": 1}}
            pts = [r for r in rows if r["geom"]]
            if not pts:
                return {"extent": None}
            env = {"xmin": min(r["x"] for r in pts), "xmax": max(r["x"] for r in pts),
                   "ymin": min(r["y"] for r in pts), "ymax": max(r["y"] for r in pts)}
            if not f.get("extent_nosr"):
                env["spatialReference"] = {"wkid": 2237}
            return {"extent": env}
        if f.get("race") and not f.get("raced") and p.get("resultOffset") == "1000":
            # Another editor deletes the first row after page one is read, and perhaps adds one.
            f["raced"] = True
            layer.rows = [r for r in layer.rows if r["oid"] != 1]
            if f["race"] == "swap":
                layer.rows.append({"oid": 90001, "x": 1.0, "y": 1.0, "geom": True})
        start = 0 if (f.get("ignore_offset") or p.get("objectIds")) \
            else int(p.get("resultOffset") or 0)
        size = min(int(p.get("resultRecordCount") or layer.cap), layer.cap)
        chosen = rows[start:start + size]
        page = {"features": [layer.feature(r) for r in chosen if r["oid"] not in f.get("hide", ())]}
        if len(rows) > start + len(chosen) and f.get("flag", True):
            page["exceededTransferLimit"] = True
        return page

    @staticmethod
    def add_row_after(layer, moment):
        """Another editor adds one row right after the answer named by `moment` was built."""
        if layer.f.get("add_after") == moment and not layer.f.get("added"):
            layer.f["added"] = True
            layer.rows.append({"oid": layer.f.get("add_oid") or max(r["oid"] for r in layer.rows) + 1,
                               "x": 1.0, "y": 1.0, "geom": True})

    def query_attachments(self, layer, p):
        f = layer.f
        wanted = [int(x) for x in p["objectIds"].split(",")]
        held = [o for o in wanted if layer.att.get(o)]
        if p.get("returnCountOnly") == "true":
            if f.get("att_count_fail"):
                return _ERR
            if f.get("att_count_ignored"):
                return {"attachmentGroups": self.groups(layer, held)}
            return {"attachmentGroups": [{"parentObjectId": o, "count": len(layer.att[o])}
                                         for o in held]}
        if f.get("att_notlist"):
            return {"attachmentGroups": None}
        groups = self.groups(layer, held)
        if f.get("att_drop_groups"):
            groups = groups[:-1]
        if f.get("att_cap"):
            left = f["att_cap"]
            for g in groups:
                g["attachmentInfos"] = g["attachmentInfos"][:left]
                left -= len(g["attachmentInfos"])
            groups = [g for g in groups if g["attachmentInfos"]]
        if f.get("att_foreign"):
            groups.append({"parentObjectId": 999999, "attachmentInfos": [{"id": 1}]})
        if f.get("att_dup") and groups:
            groups.append(groups[0])
        if f.get("att_noid") and groups:
            groups[0]["attachmentInfos"][0].pop("id")
        return {"attachmentGroups": groups}

    @staticmethod
    def groups(layer, held):
        add = layer.f.get("att_size_add", 0)
        return [{"parentObjectId": o, "attachmentInfos": [
            {"id": i, "name": name, "size": len(data) + add, "contentType": "text/plain"}
            for i, name, data in layer.att[o]]} for o in held]

    def download(self, layer, parent, att_id):
        f = layer.f
        data = next((d for i, n, d in layer.att.get(parent, []) if i == att_id), None)
        if data is None or f.get("att_404"):
            return 404, {}, b"missing", None
        if f.get("att_drop") and not f.get("dropped"):
            f["dropped"] = True
            return 0, {}, b"", None
        if f.get("att_gzip"):
            return 200, {"Content-Encoding": "gzip"}, gzip.compress(data), None
        if f.get("att_short"):
            return 200, {}, data[:len(data) // 2], len(data)
        return 200, {}, data, -1 if f.get("att_nolen") else None


class _StubHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _serve(self, params):
        flat = {k: v[0] for k, v in params.items()}
        status, headers, body, claimed = self.server.stub.respond(
            self.command, urlparse(self.path).path, flat)
        if status == 0:
            self.close_connection = True  # hang up without answering
            return
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        if claimed != -1:
            self.send_header("Content-Length", str(len(body) if claimed is None else claimed))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._serve(parse_qs(urlparse(self.path).query))

    def do_POST(self):
        size = int(self.headers.get("Content-Length") or 0)
        self._serve(parse_qs(self.rfile.read(size).decode("utf-8")))


class _StubServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # a client that gave up on /misc/slow is the test, not a fault


def _exit_code(fn):
    """The SystemExit code fn raises with stderr muted, or None when it returns."""
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            fn()
    except SystemExit as err:
        return err.code
    return None


def _offline_checks(t):
    """Assertions that need no server and no stand-in, run first so a fault cannot mask them.

    The two pieces of logic here are the ones a live check cannot exercise on demand:
    a server that truncates on cue is not something a public sample service provides,
    and an interrupted run is not something a check can stage against production.
    """
    check = t.check
    parent = {"xmin": 0.0, "ymin": 0.0, "xmax": 10.0, "ymax": 20.0,
              "spatialReference": {"wkid": 2237}}
    quads = _split_envelope(parent)
    check(len(quads) == 4, "an envelope splits into exactly four quadrants")
    check(all(q["spatialReference"] == parent["spatialReference"] for q in quads),
          "quadrants must carry the parent spatial reference or the query reprojects")
    area = sum((q["xmax"] - q["xmin"]) * (q["ymax"] - q["ymin"]) for q in quads)
    check(abs(area - 200.0) < 1e-9, "quadrants must tile the parent exactly, no gaps or overlap")
    check({(q["xmin"], q["ymin"]) for q in quads} == {(0.0, 0.0), (5.0, 0.0),
                                                       (0.0, 10.0), (5.0, 10.0)},
          "quadrants must be the four corners, not four copies")
    check(_split_envelope({"xmin": 0, "ymin": 0, "xmax": 1, "ymax": 1})[0].get(
        "spatialReference") is None, "an extent with no SR must not gain an empty one")

    tmp = tempfile.mkdtemp(prefix="restdl_progress_")
    try:
        path = os.path.join(tmp, "x.gdb.progress.json")
        check(_load_progress(path) == {}, "a missing progress file reads as nothing done")
        _save_progress(path, {"0": {"name": "Roads", "count": 12}})
        check(_load_progress(path)["0"]["count"] == 12, "progress must survive a round trip")
        _save_progress(path, {"0": {"name": "Roads", "count": 12},
                              "3": {"name": "Signs", "count": 4}})
        check(len(_load_progress(path)) == 2, "a later save must not lose an earlier layer")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"0": {"name": "Ro')
        check(_load_progress(path) == {}, "a truncated progress file must not raise")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("[1, 2, 3]")
        check(_load_progress(path) == {}, "a progress file of the wrong shape must not raise")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"0": "done", "1": {"name": "Roads"}, "2": {"name": "Ok", "count": 3}}')
        check(_load_progress(path) == {"2": {"name": "Ok", "count": 3}},
              "a hand-edited entry the resume cannot use is dropped, not allowed to abort the "
              "pull  <-- pinned defect")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # Prefix of --self-check-live (the longest flag). Parser must refuse it, not expand it.
    check(_exit_code(lambda: _parser().parse_args(["--self-check-l"])) == 2,
          "a unique prefix of a flag must be refused, not expanded  <-- pinned defect")
    check(_exit_code(lambda: _parser().parse_args(["--self-check"])) is None,
          "the whole flag is still accepted")

    # --delay paces requests, and a negative delay is refused rather than ignored.
    class _Resp(object):
        def raise_for_status(self):
            pass

        def json(self):
            return {}

    class _Session(object):
        fullpull_delay, fullpull_last = 0.2, None

        def __init__(self, delay):
            self.fullpull_delay, self.starts = delay, []

        def post(self, url, data=None, timeout=None):
            self.starts.append(time.monotonic())
            return _Resp()

    paced = _Session(0.2)
    for _ in range(3):
        rest_get(paced, "https://example.org/arcgis/rest/services")
    gaps = [b - a for a, b in zip(paced.starts, paced.starts[1:])]
    check(all(g >= 0.15 for g in gaps), "requests must be at least DELAY apart, got %s" % gaps)
    unpaced = _Session(0)
    began = time.monotonic()
    for _ in range(3):
        rest_get(unpaced, "https://example.org/arcgis/rest/services")
    check(time.monotonic() - began < 0.1, "a delay of 0 must not sleep")
    check(_exit_code(lambda: _parser().parse_args(["--delay", "-1"])) == 2,
          "a negative --delay must be refused")
    check(_parser().parse_args(["--delay", "0.5"]).delay == 0.5, "--delay must parse seconds")
    late = _Session(0.02)
    rest_get(late, "https://example.org/arcgis/rest/services")
    time.sleep(0.06)
    rest_get(late, "https://example.org/arcgis/rest/services")
    check(late.starts[1] - late.starts[0] < 0.4,
          "a request made after the delay has already passed does not wait again")

    # Run this file under another name, with arcpy made to fail in each way it can.
    class _BlockArcpy(object):
        def __init__(self, exc):
            self.exc = exc

        def find_spec(self, name, path=None, target=None):
            # Only arcpy is ever asked for: every other import of this file is already loaded.
            raise self.exc

    def import_copy(exc):
        held = sys.modules.pop("arcpy", None)
        blocker = _BlockArcpy(exc)
        sys.meta_path.insert(0, blocker)
        try:
            spec = importlib.util.spec_from_file_location("fullpull_copy", os.path.abspath(__file__))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        finally:
            sys.meta_path.remove(blocker)
            sys.modules.update({"arcpy": held} if held else {})
        return module

    missing = import_copy(ImportError("no arcpy"))
    check(missing.arcpy is None and missing.ARCPY_LICENCE_ERROR is None,
          "a missing arcpy is told apart from an arcpy that cannot get its licence")
    unlicensed = import_copy(RuntimeError("licence not initialised"))
    check(unlicensed.arcpy is None and unlicensed.ARCPY_LICENCE_ERROR == "licence not initialised",
          "an arcpy that raises RuntimeError on import keeps the reason  <-- pinned defect")


def _pure_checks(t):
    """The decision core: every verdict on a hand-made input, no server."""
    check = t.check
    probe = _Harness(quiet=True)
    probe.check(False, "x")
    probe.raises(lambda: None, "y")
    probe.raises(lambda: 1 // 0, "z")
    probe.raises(lambda: 1 // 0, "w", exc=ZeroDivisionError, text="division")
    check(len(probe.failed) == 3 and probe.passed == 1,
          "check() and raises() really do record a failure  <-- pinned defect")

    check(short_read_verdict(2500, 2500, 0) is None, "a pull of exactly the server count is whole")
    check("downloaded 2497 of 2500 records, 3 short" == short_read_verdict(2500, 2497, 0),
          "three rows short of the count is named with both numbers  <-- pinned defect")
    check("counted 2500" in short_read_verdict(2500, 2503, 0),
          "more rows than the server counted is refused too")
    check("twice" in short_read_verdict(2000, 2000, 1000),
          "a repeated page is refused although the count matches  <-- pinned defect")
    check("only 1000 distinct" in short_read_verdict(2000, 2000, 1000),
          "the refusal says how many distinct rows were really read")

    seen = set()
    page = {"features": [{"attributes": {"OID": 1}}, {"attributes": {"OID": 2}}]}
    check(note_ids(page, "OID", seen) == 0 and seen == {1, 2}, "a first page repeats nothing")
    check(note_ids(page, "OID", seen) == 2, "the same page again repeats both rows")
    check(note_ids({"features": [{"attributes": {}}, {}]}, "OID", seen) == 0,
          "a feature with no OID value is not tracked and not counted")
    check(note_ids({}, "OID", seen) == 0 and note_ids(page, None, set()) == 0,
          "a page with no features, or a layer with no OID field, repeats nothing")

    check(safe_filename("../../evil.txt") == "_.._evil.txt",
          "an attachment named with ../ cannot leave its folder  <-- pinned defect")
    check(safe_filename("a\\b:c.txt") == "a_b_c.txt", "a backslash or colon in a name becomes _")
    check(safe_filename(None) == "attachment" and safe_filename("...") == "attachment",
          "a missing or all-dot name falls back to a fixed one")
    check(len(safe_filename("x" * 500)) == 100, "a very long name is cut to 100 characters")
    check(safe_filename("caf\u00e9.png") == "caf_.png", "a non-ASCII name is reduced to ASCII")

    check(file_problem(10, 10, 10) is None, "a body of the listed size is whole")
    check("Content-Length said 10" in file_problem(10, 10, 5),
          "a body shorter than its Content-Length is refused")
    check("5 bytes of the 10" in file_problem(10, None, 5),
          "a body shorter than the size the server lists is refused")
    check(file_problem(1, None, 10) is None,
          "a body longer than the listed size is accepted, since the unit is undocumented")
    check(file_problem("n/a", None, 5) is None and file_problem(None, None, 0) is None,
          "a size that is not a number gives nothing to compare")

    two = [{"parentObjectId": 1, "attachmentInfos": [{"id": 1}, {"id": 2}]},
           {"parentObjectId": 2, "attachmentInfos": [{"id": 3}]}]
    check(attachment_batch_problems([1, 2, 3], two, {1: 2, 2: 1}, 100) == [],
          "a list that matches the per-feature counts is whole")
    short = attachment_batch_problems([1, 2], two[:1], {1: 2, 2: 1}, 100)
    check(len(short) == 1 and "feature 2" in short[0],
          "a feature the server counts but the list lacks is named  <-- pinned defect")
    check("counts 2 attachment(s), the query listed 1" in attachment_batch_problems(
        [1], [{"parentObjectId": 1, "attachmentInfos": [{"id": 1}]}], {1: 2}, 100)[0],
        "a feature listed short of its count says both numbers")
    check("never asked for" in attachment_batch_problems([1], two, {1: 2}, 100)[0],
          "a group for a feature nobody asked about is refused")
    check("twice" in attachment_batch_problems([1], two[:1] * 2, {1: 2}, 100)[0],
          "a feature answered twice is refused")
    check("no id" in attachment_batch_problems(
        [1], [{"parentObjectId": 1, "attachmentInfos": [{"name": "x"}]}], {1: 1}, 100)[0],
        "an attachment with no id is refused")
    for bad in ("../../x", "7", True, 1.5):
        check("non-integer id" in attachment_batch_problems(
            [1], [{"parentObjectId": 1, "attachmentInfos": [{"id": bad}]}], {1: 1}, 100)[0],
            "an attachment id of %r is refused, so no id can steer a file or a URL  "
            "<-- pinned defect" % (bad,))
    check("no id" in attachment_batch_problems(
        [1], [{"parentObjectId": 1, "attachmentInfos": "x"}], {1: 1}, 100)[0]
          and "never asked" in attachment_batch_problems([1], ["x"], {}, 100)[0],
          "an attachment list of the wrong shape, or a group that is not an object, is refused")
    check(attachment_batch_problems([1], None, {}, 100) == ["attachmentGroups is not a list"],
          "an answer with no group list is refused")
    check(attachment_batch_problems([1, 2, 3], two, {1: 2, 2: 1, 9: 1}, 100)[0].startswith(
        "feature 9"), "a count for a feature that was not asked about is a disagreement")
    check(attachment_batch_problems([1, 2], two, None, 100) == [],
          "with no counts, an answer under the server cap is accepted")
    check("cap of 3" in attachment_batch_problems([1, 2], two, None, 3)[0],
          "with no counts, an answer that reached the server cap is refused  <-- pinned defect")
    check("cap of 2" in attachment_batch_problems([1, 2], two, None, 2)[0],
          "with no counts, as many groups as the cap is refused too")

    check(scrub("GET /x?token=ab%2Bc failed", "ab+c") == "GET /x?token=<token> failed",
          "a token is removed from a message in its URL-encoded form  <-- pinned defect")
    check(scrub("sent ab+c", "ab+c") == "sent <token>" and scrub("plain", None) == "plain",
          "the raw token is removed too, and no token leaves the message alone")
    check(scrub("GET /x?token=a+b&f=json", "a b") == "GET /x?token=<token>&f=json"
          and scrub("GET /x?token=a%20b", "a b") == "GET /x?token=<token>",
          "a token with a space is removed in the plus form requests sends  <-- pinned defect")

    check(gdb_name_for("https://h/arcgis/rest/services/Public/Utilities/MapServer/")
          == "Public_Utilities.gdb", "the default gdb name comes from the service path")
    check(gdb_name_for("https://h/x") == "https___h_x.gdb" and gdb_name_for("///") == "OutputData.gdb",
          "a URL with no services segment still yields a legal name")
    check(page_size({"maxRecordCount": 1000}, 2000) == 1000
          and page_size({}, 2000) == 2000 and page_size({"maxRecordCount": 250000}, None)
          == MAX_PAGE and page_size({}, None) == 1000 and page_size({"maxRecordCount": 0}, 7) == 7,
          "page size is the layer cap, else the service cap, capped by MAX_PAGE, else 1000")
    check(oid_field({"objectIdField": "FID"}) == "FID" and oid_field(
        {"fields": [{"name": "N", "type": "esriFieldTypeString"},
                    {"name": "ID", "type": "esriFieldTypeOID"}]}) == "ID"
          and oid_field({}) is None, "the OID field comes from objectIdField, else fields[]")
    check(geometry_params({"hasZ": True, "hasM": True,
                           "sourceSpatialReference": {"wkid": 2237}})
          == {"outSR": "2237", "returnZ": "true", "returnM": "true"}
          and geometry_params({"extent": {"spatialReference": {"latestWkid": 3857, "wkid": 1}}})
          == {"outSR": "3857"} and geometry_params({}, "4326") == {"outSR": "4326"}
          and geometry_params({}) == {}, "outSR, returnZ and returnM are requested only when known")
    check(geometry_params({"extent": None}) == {} and geometry_params(
        {"extent": {"spatialReference": None}}) == {},
        "a layer whose extent or its spatial reference is null does not crash  <-- pinned defect")
    check(_progress_path("a.gdb") == "a.gdb.progress.json", "the progress file sits beside the gdb")


def _http_checks(t, env):
    """rest_get and list_layers against stub answers that look like ArcGIS Server's failures."""
    check, raises, s = t.check, t.raises, env.session
    misc = env.root_url + "/misc/"
    raises(lambda: rest_get(s, misc + "notjson"), "an HTML answer is refused as non-JSON",
           text="Non-JSON response")
    raises(lambda: rest_get(s, misc + "errbody"),
           "a 200 answer carrying an error body is a failure  <-- pinned defect",
           text="ArcGIS error 400 from")
    for leaky in ("notjson", "errbody"):
        msg = raises(lambda: rest_get(s, misc + leaky + "?token=SECRET%2Btok", token="SECRET+tok"),
                     "a %s failure raised for a URL that carries the token" % leaky)
        check("SECRET" not in msg and "<token>" in msg,
              "the %s message never echoes the token, in either form  <-- pinned defect" % leaky)
    raises(lambda: rest_get(s, misc + "errplain"), "an error body with no details is still named",
           text="ArcGIS error 500")
    raises(lambda: rest_get(s, misc + "http500"), "an HTTP 500 raises", exc=requests.HTTPError)
    raises(lambda: rest_get(s, misc + "flaky503"), "with no retries a 503 raises", exc=requests.HTTPError)
    patient = make_session(retries=1)
    check(rest_get(patient, misc + "flaky503") == {"ok": True}
          and rest_get(patient, misc + "flaky429") == {"ok": True},
          "a single 503 and a single 429 are retried and the request succeeds")
    raises(lambda: rest_get(s, misc + "slow", timeout=0.3), "a request that outlives its timeout is abandoned",
           exc=requests.RequestException, text="timed out")
    raises(lambda: rest_get(s, env.base + "/9"), "an unknown layer raises", exc=requests.HTTPError)
    raises(lambda: list_layers(s, misc + "folder"), "a folder URL is refused and says what to append",
           text="A/B")
    raises(lambda: list_layers(s, misc + "emptyfolder"), "an empty folder is refused too",
           text="(none listed)")
    check(list_layers(s, misc + "nolayers") == ([], None),
          "a service with no layers lists nothing")

    env.stub.layers = {0: _Layer(n=3, name="Roads"), 4: _Layer(n=2, geom=False, table=True,
                                                               name="Tbl")}
    env.stub.extra = [{"id": 2, "name": "Grp", "subLayerIds": [0]},
                      {"id": 3, "name": "Net", "type": "Network Analysis Layer"},
                      {"id": 5}]
    env.stub.extra_tables = [{"id": 6}]
    before = len(env.fake.messages)
    items, service_max = list_layers(s, env.base)
    check([(i["id"], i["name"]) for i in items]
          == [(0, "Roads"), (5, "Layer_5"), (4, "Tbl"), (6, "Table_6")] and service_max == 2000,
          "layers and tables are listed, a missing name is made up, the service cap is returned")
    said = " ".join(env.fake.messages[before:])
    check("Skipping group layer 2" in said and "Skipping non-queryable layer 3" in " ".join(
        env.fake.warnings), "group and network layers are skipped and the skip is said aloud")
    check("Skipping non-queryable layer 3" not in said,
          "a skipped layer that may hold data is a warning, not a plain message  <-- pinned defect")
    env.stub.extra, env.stub.extra_tables = [], []
    for bad in ("../x", True):
        env.stub.extra = [{"id": bad, "name": "Bad"}]
        raises(lambda: list_layers(s, env.base), "a layer id of %r is refused  <-- pinned defect"
               % (bad,), text="not an integer")
    env.stub.extra = []
    env.stub.extra_tables = [{"id": "1/../../x"}]
    raises(lambda: list_layers(s, env.base), "a table id that is not an integer is refused",
           text="not an integer")
    env.stub.extra_tables = []


def _layer_checks(t, url, layer_id, folder):
    """The layer checks that predate the stub. They run against a stub here and against a
    real server with --self-check-live. The layer must hold more records than its own cap."""
    check = t.check
    session = make_session(retries=0)
    layer_url = "%s/%s" % (url, layer_id)
    meta = rest_get(session, layer_url)
    size = page_size(meta, None)
    expected = rest_get(session, layer_url + "/query",
                        {"where": "1=1", "returnCountOnly": "true"})["count"]
    check(expected > size, "the layer holds more records than its page size (%d > %d)"
          % (expected, size))
    oid = oid_field(meta)

    def page_all(request_size):
        seen = set()
        for page in iter_pages(session, layer_url, request_size, oid, expected):
            for f in page["features"]:
                seen.add(f["attributes"][oid])
        return seen

    check(len(page_all(size)) == expected, "paging at the advertised page size lost no records")

    # The regression that matters most: when the server serves fewer rows than were
    # requested, a short page must not be mistaken for the end of the layer.
    check(len(page_all(size * 3)) == expected,
          "an over-large page request lost no records  <-- pinned defect")

    # The original defect: striding by more than the server will serve.
    naive = set()
    for off in range(0, expected, 5000):
        page = rest_get(session, layer_url + "/query",
                        {"where": "1=1", "outFields": "*", "returnGeometry": "false",
                         "resultOffset": off, "resultRecordCount": 5000})
        for f in page.get("features", []):
            naive.add(f["attributes"][oid])
    check(len(naive) < expected,
          "the naive 5000 stride demonstrably loses records  <-- pinned defect")

    name, count = download_layer(session, url, {"id": layer_id, "name": "SelfCheck"},
                                 _scratch_gdb(folder), None, set())
    check(count == expected, "download_layer wrote the verified count (%d of %d)"
          % (count, expected))
    return expected, size, len(naive)


def _scratch_gdb(folder):
    arcpy.management.CreateFileGDB(folder, "check.gdb")
    return os.path.join(folder, "check.gdb")


def _strategy_checks(t, env):
    """Every paging strategy, and every way a pull can come back short."""
    check, raises, fake, stub = t.check, t.raises, env.fake, env.stub

    mark = len(stub.hits)
    (name, count), gdb = env.pull(_Layer(n=2500))
    check(count == 2500 and name == "Pts" and not fake.warnings
          and len(fake.classes[os.path.join(gdb, name)]) == 2500
          and not [k for k in fake.classes if "stg_" in k],
          "a clean paged pull lands every row, warns of nothing and leaves no staging class")
    check(any(h[2].get("outSR") == "3857" for h in stub.hits[mark:]),
          "the output spatial reference is pinned to the layer's own, not left to the service")

    (name, count), gdb = env.pull(_Layer(n=2000, ignore_offset=True))
    rows = fake.classes[os.path.join(gdb, name)]
    check(count == 2000 and len({r["attributes"]["OBJECTID"] for r in rows}) == 2000,
          "a server that ignores resultOffset still yields 2000 distinct rows by fallback")
    check("came back twice" in fake.warnings[0] and "resultOffset paging" in fake.warnings[0],
          "the fallback names the strategy that failed and why, because a repeated page whose "
          "count matches is refused  <-- pinned defect")

    (name, count), gdb = env.pull(_Layer(n=2500, race="swap"))
    oids = {r["attributes"]["OBJECTID"] for r in fake.classes[os.path.join(gdb, name)]}
    check(count == 2500 and 1001 in oids and 90001 in oids and 1 not in oids
          and "never read" in fake.warnings[0] and "resultOffset paging" in fake.warnings[0],
          "one row deleted and one added mid-pull keeps the count but shifts a row across a "
          "page; it is refused and refetched by id, never accepted short  <-- pinned defect")
    raises(lambda: env.pull(_Layer(n=2500, race="delete")),
           "one row deleted mid-pull is refused on every strategy", text="downloaded 2499 of 2500")
    (name, count), gdb = env.pull(_Layer(n=100, ids_absent=True))
    check(count == 100 and "did not list its object ids" in " ".join(fake.warnings),
          "a server that will not list its ids cannot show an offset pull whole, so the range "
          "windows take over  <-- pinned defect")
    check("no object id list after the objectId range pull" in fake.warnings[0],
          "a range pull that cannot compare its ids with the server's says so, and passes")
    msg = raises(lambda: env.pull(_Layer(n=30, paging=False, add_after="ids")),
                 "a row added after the id list was read is refused on every strategy  "
                 "<-- pinned defect", text="every strategy failed")
    check("objectId list: 1 row(s) the server holds now were never read (first id 31)" in msg,
          "the id-list strategy names the row it never asked for")
    msg = raises(lambda: env.pull(_Layer(n=30, cap=10, paging=False, ids_fail_once=True,
                                       add_after="stats")),
                 "a row added after the OID range was read is refused on every strategy  "
                 "<-- pinned defect", text="every strategy failed")
    check("objectId range: 1 row(s) the server holds now were never read (first id 31)" in msg,
          "the range strategy names the row its windows never reached")
    msg = raises(lambda: env.pull(_Layer(n=2500, cap=1000, paging=False, ids_absent=True,
                                       add_after="stats", add_oid=9000)),
                 "a row added far above the id window is refused when the server lists no ids  "
                 "<-- pinned defect", text="every strategy failed")
    check("objectId range: the server counted 2500 records before the pull and 2501 after it"
          in msg, "with no id list the range strategy rechecks the record count")
    check(recount_verdict(5, 5) is None and "7 after" in recount_verdict(5, 7),
          "an unchanged count passes the recount and a changed one is named")
    (name, count), gdb = env.pull(_Layer(n=30, ids_fail_once=True))
    check(count == 30 and "resultOffset paging: ArcGIS error" in fake.warnings[0],
          "an id list that fails after an offset pull is a failure of that strategy, not a pass")
    check(unread_verdict([1, 2, "3"], {1, 2, 3}) is None,
          "an id list the pages all covered, in either number form, is whole")
    check("never read (first id 7)" in unread_verdict([9, 7], {1}),
          "an id no page carried is named")
    check(unread_verdict([1], {1, 2}) is None, "a row deleted during the pull is not an unread row")

    seed = {"seed": ["old"]}
    msg = raises(lambda: env.pull(_Layer(n=100, hide={98, 99, 100}), seed=seed),
                 "a pull three rows short on every strategy fails the layer  <-- pinned defect",
                 text="every strategy failed, existing output left untouched")
    check("downloaded 97 of 100 records, 3 short" in msg and "envelope quadtree" in msg,
          "the failure lists each strategy that came up short, the quadtree included")
    check(fake.classes[seed["target"]] == ["old"] and not [k for k in fake.classes if "stg_" in k],
          "the previous copy survives a short read and no staging class is left behind")

    raises(lambda: env.pull(_Layer(n=100, no_count=True)),
           "a server that returns no count is refused, not trusted  <-- pinned defect",
           text="no record count")
    fake.drop_rows = 1
    raises(lambda: env.pull(_Layer(n=10)),
           "a row the conversion drops is caught, the count is read back from the class",
           text="downloaded 9 of 10")
    fake.drop_rows = 0

    seed = {"seed": ["old"]}
    msg = raises(lambda: env.pull(_Layer(n=0), seed=seed),
                 "a server that counts 0 while the old copy holds rows is refused, not kept "
                 "stale on exit 0  <-- pinned defect", text="would be left stale")
    check(fake.classes[seed["target"]] == ["old"] and "delete Pts" in msg,
          "the refused old copy is left untouched and the message says how to proceed")
    seed = {"seed": []}
    (name, count), gdb = env.pull(_Layer(n=0), seed=seed)
    check(count == 0 and "nothing written" in fake.warnings[-1],
          "an empty layer over an empty old copy completes with a warning")
    env.pull(_Layer(n=0))
    check("nothing written" in fake.warnings[-1], "an empty layer with no copy says nothing was written")

    (name, count), gdb = env.pull(_Layer(n=300, paging=False, ids_fail="plain", oid_in_fields_only=True))
    check(count == 300 and "objectId range succeeded after 1 failed strategy" in fake.warnings[-1],
          "with no pagination and no ID list the range windows pull 300 rows and say so")

    mark = len(stub.hits)
    (name, count), gdb = env.pull(_Layer(n=500, cap=100, paging=False, ids_fail="plain",
                                         stats_fail=True, flag=False))
    check(count == 500 and "envelope quadtree succeeded after 2 failed strategies"
          in fake.warnings[-1], "with nothing else working the quadtree pulls 500 rows")
    first = next(json.loads(h[2]["geometry"]) for h in stub.hits[mark:] if h[2].get("geometry"))
    check(first["xmax"] == 500.0 and first["ymax"] == 90.0 and first["xmin"] == 0.0,
          "the quadtree starts from the extent of the rows, not the published layer extent")

    (name, count), gdb = env.pull(_Layer(n=300, cap=100, coincident=True, paging=False,
                                         ids_fail="plain", stats_fail=True, extent_fail=True))
    check(count == 300, "300 rows on one coordinate fall back to the cell's ID list at the depth cap")

    # Four chains reach the depth cap when the pile sits on the centre of the start extent,
    # so three of the four ID lists hold only rows the first one already yielded.
    (name, count), gdb = env.pull(_Layer(n=300, cap=100, coincident=(255.0, 245.0), paging=False,
                                         ids_fail="plain", stats_fail=True, extent_fail=True))
    rows = fake.classes[os.path.join(gdb, name)]
    check(count == 300 and len({r["attributes"]["OBJECTID"] for r in rows}) == 300,
          "a pile on a quadrant corner is reached by four cells and is de-duplicated to 300 rows")

    # A delete that silently fails leaves the half-written attempt in place for the next one.
    fake.skip_deletes = 1
    (name, count), gdb = env.pull(_Layer(n=2000, ignore_offset=True))
    rows = fake.classes[os.path.join(gdb, name)]
    check(count == 2000 and len({r["attributes"]["OBJECTID"] for r in rows}) == 2000,
          "a half-written attempt that could not be deleted is cleared before the next strategy"
          "  <-- pinned defect")
    fake.stale = os.path.join(env.root, "stale_staging")
    fake.classes[fake.stale] = [{"attributes": {"OBJECTID": -1}}]
    (name, count), gdb = env.pull(_Layer(n=20))
    check(count == 20 and not any(r["attributes"]["OBJECTID"] < 0
                                  for r in fake.classes[os.path.join(gdb, name)]),
          "a staging name that already exists is cleared before use")
    fake.stale = None
    raises(lambda: env.pull(_Layer(n=5, hide={1, 2, 3, 4, 5})),
           "a server that counts rows but serves none is refused on every strategy",
           text="returned none")

    # -- strategies called directly
    layer = _Layer(n=10, nogeom={3, 4})
    stub.layers = {0: layer}
    meta = rest_get(env.session, env.url(0))
    raises(lambda: list(iter_pages_by_envelope(env.session, env.url(0), 100, "OBJECTID", meta)),
           "rows with no geometry make the quadtree refuse up front",
           text="2 of 10 rows have no geometry")
    raises(lambda: list(iter_pages_by_envelope(env.session, env.url(0), 100, None, meta)),
           "the quadtree refuses a layer with no OID field", text="needs an OID field")
    raises(lambda: list(iter_pages_by_envelope(env.session, env.url(0), 100, "OBJECTID", {})),
           "the quadtree refuses a table", text="this is a table")
    stub.layers = {0: _Layer(n=3, extent_fail=True)}
    raises(lambda: list(iter_pages_by_envelope(env.session, env.url(0), 100, "OBJECTID",
                                               {"geometryType": "x"})),
           "the quadtree refuses a layer that publishes no extent", text="no extent")
    stub.layers = {0: _Layer(n=3, nogeom={1, 2, 3})}
    check(_query_extent(env.session, env.url(0), "1=1") is None,
          "a result set with no geometry has no extent")
    stub.layers = {0: _Layer(n=3, extent_nox=True)}
    check(_query_extent(env.session, env.url(0), "1=1") is None,
          "an extent with no xmin is not an extent")

    stub.layers = {0: _Layer(oids=[1, 5000, 5001], cap=1000)}
    got = [f["attributes"]["OBJECTID"] for p in iter_pages_by_oid_range(
        env.session, env.url(0), 1000, "OBJECTID", where="OBJECTID>0") for f in p["features"]]
    check(got == [1, 5000, 5001], "sparse OIDs cost empty windows, never rows, under a where clause")
    raises(lambda: list(iter_pages_by_oid_range(env.session, env.url(0), 1000, None)),
           "range paging refuses a layer with no OID field", text="needs an OID field")
    stub.layers = {0: _Layer(n=0)}
    raises(lambda: list(iter_pages_by_oid_range(env.session, env.url(0), 1000, "OBJECTID")),
           "range paging refuses an answer with no OID statistics", text="no OID statistics")

    stub.layers = {0: _Layer(n=60, extent_nosr=True)}
    got = list(iter_pages_by_envelope(env.session, env.url(0), 100, "OBJECTID", {
        "geometryType": "esriGeometryPoint"}))
    check(len(got) == 1 and len(got[0]["features"]) == 60,
          "the quadtree runs on an extent that carries no spatial reference")
    stub.layers = {0: _Layer(n=500, cap=1000)}
    check(sum(len(p["features"]) for p in iter_pages(env.session, env.url(0), 1000, None, 500)) == 500,
          "paging works on a layer with no OID field, unordered")
    pages = list(iter_pages(env.session, env.url(0), 1000, "OBJECTID", None))
    check(len(pages) == 1 and len(pages[0]["features"]) == 500,
          "with no server count a page that is not flagged as truncated ends the layer")
    stub.layers = {0: _Layer(n=2500, cap=1000)}
    check(len(list(iter_pages(env.session, env.url(0), 1000, "OBJECTID", None))) == 3,
          "with no server count a flagged page is followed to the end")
    stub.layers = {0: _Layer(n=3, hide={1, 2, 3})}
    check(list(_pages_by_ids(env.session, env.url(0), [1, 2, 3], 2, {"outFields": "*"})) == [],
          "an ID batch that returns nothing yields no page")

    taken = set()
    first, second = unique_name("Pts", gdb, taken), unique_name("Pts", gdb, taken)
    check((first, second) == ("Pts", "Pts_2"), "two layers called Pts become Pts and Pts_2")
    check(len(unique_name("x" * 70, gdb, {"x" * 70})) <= 62, "a long colliding name stays short")


def _att_layer(n=250, every=7, per=2, **flags):
    """A layer in which every `every`th feature carries `per` small text attachments."""
    layer = _Layer(n=n, has_att=True, **flags)
    serial = 0
    for row in layer.rows:
        if row["oid"] % every == 0:
            for _ in range(per):
                serial += 1
                layer.att.setdefault(row["oid"], []).append(
                    (serial, "f%d.txt" % serial, ("data-%d-" % serial).encode("ascii") * 5))
    return layer


def _files(folder):
    return sorted(os.path.join(d, f) for d, _, fs in os.walk(folder) for f in fs)


def _attachment_checks(t, env):
    """Attachment batches: whole, short, overflowing, and every way a body can be cut."""
    check, raises, stub = t.check, t.raises, env.stub

    stub.layers = {0: _att_layer(n=14, every=7, per=1)}
    slow, began = make_session(retries=0, delay=0.2), time.monotonic()
    for parent, (serial, fname, data) in ((7, stub.layers[0].att[7][0]),
                                          (14, stub.layers[0].att[14][0]), (7, stub.layers[0].att[7][0])):
        download_attachment(slow, env.base + "/0", parent,
                            {"id": serial, "name": fname, "size": len(data)},
                            tempfile.mkdtemp(dir=env.root))
    check(time.monotonic() - began >= 0.35,
          "attachment downloads obey --delay like every other request  <-- pinned defect")

    layer = _att_layer()
    truth = sum(len(v) for v in layer.att.values())
    summary, folder = env.att(layer)
    check(summary == {"files": truth, "features": len(layer.att), "by_count": 3, "by_cap": 0}
          and len(_files(folder)) == truth,
          "a clean pull writes every attachment and checks all three batches against the counts")
    first = sorted(layer.att)[0]
    one = layer.att[first][0]
    with open(os.path.join(folder, "a.gdb.attachments", "Pts", str(first),
                           "%d_%s" % (one[0], one[1])), "rb") as fh:
        check(fh.read() == one[2], "an attachment is written byte for byte")
    check(not [f for f in _files(folder) if f.endswith(".part")], "no .part file is left behind")

    raises(lambda: env.att(_att_layer(att_drop_groups=True)),
           "a list that drops a feature the server counts is refused  <-- pinned defect",
           text="cannot be shown complete")
    summary, folder = env.att(_att_layer(n=60, every=2, per=3, att_cap=5))
    check(summary["files"] == 90 and len(_files(folder)) == 90,
          "a list cut off at the server's cap is split until every attachment arrives"
          "  <-- pinned defect")

    raises(lambda: env.att(_att_layer(att_count_fail=True)),
           "a server that cannot count is refused unless the owner opts in  <-- pinned defect",
           text="cannot count attachments")
    raises(lambda: env.att(_att_layer(att_count_ignored=True)),
           "a server that answers the count form with full lists is refused too",
           text="cannot count attachments")
    raises(lambda: env.att(_att_layer(att_count_http=503)),
           "a 503 on the count form is a failure, not proof the server cannot count  "
           "<-- pinned defect", exc=requests.HTTPError)
    raises(lambda: env.att(_att_layer(att_count_http=429)),
           "a 429 on the count form is a failure too", exc=requests.HTTPError)
    raises(lambda: env.att(_att_layer(att_count_http=400)),
           "a 400 on the count form means not supported, so it is refused as uncounted",
           text="cannot count attachments")
    mark = len(stub.hits)
    raises(lambda: env.att(_att_layer(att_count_cap=False)),
           "a layer that says it cannot count attachments is refused without being asked to",
           text="cannot count attachments")
    check(not [h for h in stub.hits[mark:] if h[2].get("returnCountOnly") == "true"],
          "a layer whose supportsQueryAttachmentsCountOnly is false is not sent the count form")
    summary, folder = env.att(_att_layer(att_count_cap=True))
    check(summary["by_count"] == 3, "a layer that says it can count attachments is counted")
    capped = _att_layer(n=250, every=2, per=3, cap=30, att_cap=30, att_count_fail=True)
    mark = len(stub.hits)
    summary, folder = env.att(capped, allow=True)
    check(summary["files"] == 375 and summary["by_cap"] > 0 and summary["by_count"] == 0
          and len([h for h in stub.hits[mark:] if h[2].get("returnCountOnly") == "true"]) == 1,
          "with no counts the cap rule splits a batch that reached the cap, and asks for "
          "counts once, not per batch")
    summary, folder = env.att(_att_layer(att_count_ignored=True), allow=True)
    check(summary["by_cap"] == 3 and summary["files"] == truth,
          "a server that answers the count query with full lists is treated as unable to count")
    summary, folder = env.att(_att_layer(n=250, every=7, per=2), token=None)
    check(summary["by_cap"] == 0, "a fresh pull counts again")

    for flag, text in (("att_foreign", "never asked for"), ("att_dup", "twice"),
                       ("att_noid", "no id"), ("att_notlist", "not a list")):
        raises(lambda f=flag: env.att(_att_layer(**{f: True})),
               "a %s answer refuses the layer" % flag[4:], text=text)

    raises(lambda: env.att(_att_layer(att_short=True)),
           "a body cut short of its Content-Length is refused  <-- pinned defect",
           text="attachment")
    check(not [f for f in _files(env.last_folder) if f.endswith(".part")],
          "a refused body leaves no .part file")
    raises(lambda: env.att(_att_layer(att_size_add=10)),
           "a body shorter than the size the server lists is refused", text="of the")
    summary, folder = env.att(_att_layer(att_size_add=-1))
    check(summary["files"] == truth, "a body longer than the listed size is accepted")
    summary, folder = env.att(_att_layer(att_gzip=True))
    with open(_files(folder)[0], "rb") as fh:
        body = fh.read()
    check(summary["files"] == truth and body.startswith(b"data-"),
          "a gzip body is decoded and its Content-Length not compared")
    summary, folder = env.att(_att_layer(att_nolen=True))
    check(summary["files"] == truth, "a body with no Content-Length is accepted on its size alone")

    raises(lambda: env.att(_att_layer(ids_str=True)),
           "a feature list holding ids that are not integers is refused  <-- pinned defect",
           text="not an integer")
    raises(lambda: env.att(_att_layer(ids_short=True)),
           "a feature list one id short of the row count is refused  <-- pinned defect",
           text="ids for 250 rows")
    check(env.att(_Layer(n=5))[0] is None, "a layer with no attachments reports none")
    raises(lambda: env.att(_att_layer(query_att=False)),
           "a layer that cannot list attachments in batches is refused, not skipped",
           text="supportsQueryAttachments")
    check(env.att(_att_layer(every=1000))[0]["files"] == 0,
          "a layer that has attachments in name only completes with none")

    evil = _att_layer(n=10, every=5, per=1)
    evil.att[5] = [(1, "../../evil.txt", b"x")]
    summary, folder = env.att(evil)
    check(os.path.exists(os.path.join(folder, "a.gdb.attachments", "Pts", "5", "1__.._evil.txt"))
          and not os.path.exists(os.path.join(folder, "evil.txt"))
          and sorted(os.listdir(folder)) == ["a.gdb.attachments"],
          "an attachment named ../../evil.txt stays inside its feature's folder  <-- pinned defect")

    tok = "s3+cr/et="
    stub.token = tok
    layer = _att_layer(att_404=True)
    msg = raises(lambda: env.att(layer, token=tok), "a missing attachment is refused", text="attachment")
    check(tok not in msg and quote(tok, safe="") not in msg and "<token>" in msg,
          "the failure of a GET that carried the token does not repeat it  <-- pinned defect")
    summary, folder = env.att(_att_layer(), token=tok)
    check(summary["files"] == truth, "a token reaches both the POST queries and the GET downloads")
    raises(lambda: download_attachment(env.session, env.url(0), 1, {"id": 1, "name": "a"},
                                       folder, token="wrong"),
           "a download with the wrong token is refused", text="403")
    sent = []

    class _Grab(logging.Handler):
        def emit(self, record):
            sent.append(record.getMessage())

    grab, urllib_log = _Grab(level=logging.WARNING), logging.getLogger("urllib3")
    was = urllib_log.level
    urllib_log.addHandler(grab)
    urllib_log.setLevel(logging.WARNING)
    try:
        drop = _att_layer(n=14, every=7, per=1, att_drop=True)
        stub.layers = {0: drop}
        serial, fname, data = drop.att[7][0]
        download_attachment(make_session(retries=1), env.base + "/0", 7,
                            {"id": serial, "name": fname, "size": len(data)},
                            tempfile.mkdtemp(dir=env.root), token=tok)
        logging.getLogger("urllib3.connectionpool").warning("unrelated %s", "line")
    finally:
        urllib_log.removeHandler(grab)
        urllib_log.setLevel(was)
    retried = [m for m in sent if "Retrying" in m]
    check(len(retried) == 1 and "<token>" in retried[0] and not any(
              form in m for m in sent for form in (tok, quote(tok, safe=""), quote_plus(tok))),
          "urllib3's log line for a retried attachment download never carries the token  "
          "<-- pinned defect")
    check("unrelated line" in sent, "a log record without the token is passed on unchanged")
    stub.token = None


def _service_checks(t, env):
    """download_service end to end: names, resume, attachments, a refused layer, the token."""
    check, raises, fake, stub = t.check, t.raises, env.fake, env.stub
    stub.layers = {0: _att_layer(n=30, every=3, per=1), 1: _Layer(n=5, geom=False, name="Pts"),
                   4: _Layer(n=2, geom=False, table=True, name="Tbl")}
    stub.extra = [{"id": 2, "name": "Grp", "subLayerIds": [0]}]
    folder = tempfile.mkdtemp(dir=env.root)
    gdb = os.path.join(folder, "Stub.gdb")
    results = download_service(env.base, folder, "Stub.gdb", apply=True)
    check(results == [("Pts", 30), ("Pts_2", 5), ("Tbl", 2)] and os.path.isdir(gdb),
          "a service pulls every layer and table under collision-free names")
    check(not os.path.exists(gdb + ".progress.json") and not os.path.exists(gdb + ".attachments"),
          "a clean run deletes its progress file and writes no attachments unless asked")

    # Without apply nothing may be written, however the call is made.
    folder = tempfile.mkdtemp(dir=env.root)
    classes_before = len(fake.classes)
    plan = download_service(env.base, folder, "Stub.gdb")
    check([c for _, c in plan] == [30, 5, 2] and os.listdir(folder) == []
          and len(fake.classes) == classes_before and "Nothing was written" in fake.messages[-1],
          "a run without apply reads the counts and writes no geodatabase, class or file"
          "  <-- pinned defect")
    stub.layers[1].f["no_count"] = True
    raises(lambda: download_service(env.base, folder, "Stub.gdb", attachments=True),
           "a dry run reports a layer it would refuse and fails, so it cannot pass a bad service",
           text="Layers that would be refused: Pts")
    check("WOULD BE REFUSED" in fake.errors[-1] and "with their attachments" in fake.messages[-1]
          and os.listdir(folder) == [], "the dry run names the layer and still writes nothing")
    stub.layers[1].f.pop("no_count")
    folder = tempfile.mkdtemp(dir=env.root)

    # Another process removes the progress file between the last layer and the cleanup.
    folder = tempfile.mkdtemp(dir=env.root)
    gdb = os.path.join(folder, "Stub.gdb")
    fake.on_message = lambda text: text.startswith("Done.") and os.remove(gdb + ".progress.json")
    results = download_service(env.base, folder, "Stub.gdb", apply=True)
    fake.on_message = None
    check(len(results) == 3, "a progress file that vanished before the cleanup does not fail a clean run")

    stub.layers[1].f["no_count"] = True
    folder = tempfile.mkdtemp(dir=env.root)
    gdb = os.path.join(folder, "Stub.gdb")
    raises(lambda: download_service(env.base, folder, "Stub.gdb", apply=True),
           "one refused layer fails the run, so a scheduled job cannot report success",
           text="Failed layers: Pts")
    check(sorted(_load_progress(gdb + ".progress.json")) == ["0", "4"]
          and "FAILED" in fake.errors[-1],
          "the progress file keeps the layers that finished and the error names the one that did not")
    stub.layers[1].f.pop("no_count")
    results = download_service(env.base, folder, "Stub.gdb", resume=True, apply=True)
    check(results[0] == ("Pts", 30) and "already complete" in " ".join(fake.messages)
          and not os.path.exists(gdb + ".progress.json"),
          "resume skips what finished, redoes what did not, and removes its file")

    # Resume after a run with a filter: the default-filter run must not accept the filtered copy.
    stub.layers[1].f["no_count"] = True
    folder = tempfile.mkdtemp(dir=env.root)
    gdb = os.path.join(folder, "Stub.gdb")
    raises(lambda: download_service(env.base, folder, "Stub.gdb",
                                    where="OBJECTID >= 1 AND OBJECTID <= 3", apply=True),
           "a filtered run with a refused layer fails", text="Failed layers: Pts")
    stub.layers[1].f.pop("no_count")
    results = download_service(env.base, folder, "Stub.gdb", resume=True, apply=True)
    check(results[0] == ("Pts", 30) and "different service, filter" in " ".join(fake.messages),
          "a resume with another filter downloads the layer again, not 'already complete'"
          "  <-- pinned defect")
    check(len(fake.classes[os.path.join(gdb, "Pts")]) == 30,
          "the layer holds every row the server has, not the earlier filter's three"
          "  <-- pinned defect")

    # Finished without attachments, resumed with them: the layer must not be skipped.
    stub.layers[1].f["no_count"] = True
    folder = tempfile.mkdtemp(dir=env.root)
    gdb = os.path.join(folder, "Stub.gdb")
    raises(lambda: download_service(env.base, folder, "Stub.gdb", apply=True), "the plain run fails on layer 1")
    stub.layers[1].f.pop("no_count")
    download_service(env.base, folder, "Stub.gdb", resume=True, attachments=True, apply=True)
    check(len(_files(os.path.join(gdb + ".attachments", "Pts"))) == 10,
          "a resume that wants attachments does not skip a layer finished without them"
          "  <-- pinned defect")
    check("no attachments on this layer" in " ".join(fake.messages)
          and "1 batch(es) checked against the server's per-feature counts, 0 against the server cap only"
          in " ".join(fake.messages),
          "the run says which layers had attachments and how they were checked")

    # A layer description without hasAttachments cannot be told from a layer with none.
    stub.layers[1].f["no_has_att"] = True
    folder = tempfile.mkdtemp(dir=env.root)
    msg = raises(lambda: download_service(env.base, folder, "Stub.gdb", attachments=True, apply=True),
                 "a layer that does not say whether it has attachments fails the run, not exit 0"
                 "  <-- pinned defect", text="Failed layers: Pts")
    check("does not say whether it has attachments" in fake.errors[-1],
          "the failure says the tool could not tell")
    check("Done. 2 layer(s) complete, 1 failed." in fake.messages,
          "a layer whose rows landed but whose attachment pass failed counts as failed only"
          "  <-- pinned defect")
    results = download_service(env.base, folder, "Stub.gdb", apply=True)
    check(len(results) == 3, "the same layer is pulled without --attachments")
    stub.layers[1].f.pop("no_has_att")

    stub.layers[0].f["att_count_fail"] = True
    folder = tempfile.mkdtemp(dir=env.root)
    raises(lambda: download_service(env.base, folder, "Stub.gdb", attachments=True, apply=True),
           "a run against a server that cannot count attachments fails by default",
           text="Failed layers: Pts")
    check("--allow-uncounted-attachments" in fake.errors[-1],
          "the failure names the flag that accepts the weaker rule")
    folder = tempfile.mkdtemp(dir=env.root)
    download_service(env.base, folder, "Stub.gdb", attachments=True, allow_uncounted=True, apply=True)
    check("cap rule alone" in fake.warnings[-1],
          "with the opt-in a server that cannot count attachments is reported as weaker evidence")
    stub.layers[0].f.pop("att_count_fail")

    tok = "s3+cr/et="
    stub.token = tok
    stub.layers[0].f["att_404"] = True
    folder = tempfile.mkdtemp(dir=env.root)
    raises(lambda: download_service(env.base, folder, "Stub.gdb", token=tok, attachments=True, apply=True),
           "an attachment failure fails the layer and the run", text="Failed layers: Pts")
    check("0" not in _load_progress(os.path.join(folder, "Stub.gdb.progress.json")),
          "a layer whose attachments failed is not recorded as finished")
    blob = "\n".join(fake.messages + fake.warnings + fake.errors)
    for path in _files(folder):
        with open(path, "rb") as fh:
            blob += fh.read().decode("latin-1")
    check(tok not in blob and quote(tok, safe="") not in blob,
          "the token reaches no message, warning, error, progress file or output file"
          "  <-- pinned defect")
    raises(lambda: download_service(env.base, folder, "Stub.gdb", token="wrong", apply=True),
           "a wrong token is refused by the server", text="stub refuses")
    stub.token = None
    stub.extra = []

    held = sorted(os.listdir(folder))
    for tail in ("?token=SECRETTOKEN123", "#SECRETTOKEN123"):
        msg = raises(lambda: download_service(env.base + tail, folder, token="SECRETTOKEN123", apply=True),
                     "a service URL with a query string or fragment is refused  <-- pinned defect",
                     text="query string or fragment")
        check("SECRETTOKEN123" not in msg and sorted(os.listdir(folder)) == held,
              "the refusal neither echoes the token nor writes a geodatabase or progress file")
    held = sorted(os.listdir(folder))
    secret = env.base.replace("http://", "http://admin:SECRETPW@")
    for url in (secret, env.base.replace("http://", "http://admin@"),
                env.base.replace("http://", "http://:SECRETPW@")):
        msg = raises(lambda: download_service(url, folder, apply=True),
                     "a service URL with a user name or password is refused  <-- pinned defect",
                     text="user name or password")
        check("SECRETPW" not in msg and sorted(os.listdir(folder)) == held,
              "the refusal neither echoes the password nor writes a geodatabase or progress file")
    raises(lambda: download_service(env.base, os.path.join(folder, "nope"), apply=True),
           "a missing output folder is refused", text="does not exist")
    raises(lambda: download_service(env.root_url + "/misc/nolayers", folder, apply=True),
           "a service with no queryable layers is refused", text="No queryable layers")


def _cli_checks(t, env):
    """main(): argument handling, the CONFIG fallback, and the arcpy-less exit."""
    check, raises, stub = t.check, t.raises, env.stub
    stub.layers = {0: _Layer(n=12)}
    folder = tempfile.mkdtemp(dir=env.root)
    check(main([env.base, folder, "cli.gdb", "", "2237", "1=1", "--apply"]) == 0 and os.path.isdir(
        os.path.join(folder, "cli.gdb")), "main pulls a service from positional arguments")
    check(len([h for h in stub.hits if h[2].get("outSR") == "2237"]) > 0,
          "a positional out_sr reaches the query")
    with _swapped("SERVICE_URL", env.base), _swapped("OUTPUT_FOLDER", folder), \
            _swapped("GDB_NAME", "cfg.gdb"), _swapped("APPLY", True):
        check(main([]) == 0 and os.path.isdir(os.path.join(folder, "cfg.gdb")),
              "main falls back to the CONFIG block when given nothing")
    check(main([env.base, folder, "att.gdb", "--apply", "--attachments", "--resume"]) == 0,
          "main accepts --attachments and --resume together")
    stub.layers = {0: _att_layer(n=20, att_count_fail=True)}
    raises(lambda: main([env.base, folder, "unc.gdb", "--apply", "--attachments"]),
           "main refuses an uncounted attachment server by default", text="Failed layers")
    check(main([env.base, folder, "unc.gdb", "--apply", "--attachments", "--allow-uncounted-attachments",
                "--delay", "0"]) == 0, "main accepts --allow-uncounted-attachments and --delay")
    with _swapped("ALLOW_UNCOUNTED_ATTACHMENTS", True):
        check(main([env.base, folder, "unc2.gdb", "--apply", "--attachments"]) == 0,
              "the CONFIG switch accepts an uncounted attachment server too")
    check(_exit_code(lambda: _parser().parse_args(["--delay", "nan"])) == 2
          and _exit_code(lambda: _parser().parse_args(["--delay", "inf"])) == 2,
          "a delay that is not a finite number is refused, not read as no pacing  <-- pinned defect")
    stub.layers = {0: _Layer(n=12)}
    tok = "env+tok/1="
    stub.token = tok
    os.environ["FULLPULL_TOKEN"] = tok
    check(main([env.base, folder, "tok1.gdb", "--apply"]) == 0,
          "a token in FULLPULL_TOKEN reaches the server with no token argument")
    del os.environ["FULLPULL_TOKEN"]
    raises(lambda: main([env.base, folder, "tok2.gdb", "--apply"]), "with no token anywhere the server refuses",
           text="stub refuses")
    with _swapped("sys", types.SimpleNamespace(stdin=io.StringIO(tok + "\n"), exit=sys.exit)):
        check(main([env.base, folder, "tok3.gdb", "-", "--apply"]) == 0,
              "a token argument of - is read from standard input")
    with _swapped("sys", types.SimpleNamespace(stdin=io.StringIO("\n"), exit=sys.exit)):
        raises(lambda: main([env.base, folder, "tok4.gdb", "-", "--apply"]),
               "an empty standard input is no token", text="stub refuses")
    stub.token = None
    dry = tempfile.mkdtemp(dir=env.root)
    check(main([env.base, dry, "dry.gdb"]) == 0 and os.listdir(dry) == [],
          "main without --apply exits 0 and writes nothing  <-- pinned defect")
    check(_exit_code(lambda: _parser().parse_args(["u", "f", "--ap"])) == 2
          and _exit_code(lambda: _parser().parse_args(["u", "f", "--appl"])) == 2
          and _parser().parse_args(["u", "f", "--apply"]).apply,
          "a prefix of --apply is refused, only the whole flag writes  <-- pinned defect")
    check(_exit_code(lambda: _parser().parse_args(["u", "f", "--attach"])) == 2,
          "a prefix of --attachments is refused, not expanded  <-- pinned defect")
    with _swapped("SERVICE_URL", ""):
        check(_exit_code(lambda: main([])) == 2,
              "main with no service says what to set and exits 2")
    with _swapped("arcpy", None), _swapped("ARCPY_LICENCE_ERROR", None):
        raises(lambda: main([env.base, folder]), "main without arcpy exits with the interpreter to use",
               exc=SystemExit, text="arcpy was not found")
        with _swapped("ARCPY_LICENCE_ERROR", "The Product License has not been initialized."):
            raises(lambda: main([env.base, folder]),
                   "an arcpy that imports but fails its licence is not reported as missing  "
                   "<-- pinned defect", exc=SystemExit, text="has not been initialized")
        raises(lambda: main(["--self-check-live"]), "--self-check-live without arcpy says so",
               exc=SystemExit, text="needs the real arcpy")
    stub.layers = {0: _Layer(n=2500, cap=1000)}
    out = io.StringIO()
    with _swapped("SELF_CHECK_URL", env.base), contextlib.redirect_stdout(out):
        code = main(["--self-check-live"])
    check(code == 0 and "0 failed" in out.getvalue() and "live: 2500 records" in out.getvalue(),
          "--self-check-live runs the layer checks and exits 0 (here against the stub)")


def self_check():
    """Run every check against a stub on 127.0.0.1. Returns the process exit code."""
    t = _Harness()
    print("fullpull self-check: stub server on 127.0.0.1, in-memory arcpy, no real network")
    print("-" * 68)
    _offline_checks(t)
    _pure_checks(t)

    stub, fake = _Stub(), _FakeArcpy()
    server = _StubServer(("127.0.0.1", 0), _StubHandler)
    server.stub = stub
    threading.Thread(target=server.serve_forever, daemon=True).start()
    root = tempfile.mkdtemp(prefix="restdl_check_")
    root_url = "http://127.0.0.1:%d" % server.server_address[1]
    env = types.SimpleNamespace(stub=stub, fake=fake, root=root, root_url=root_url,
                                base=root_url + "/svc/FeatureServer",
                                session=make_session(retries=0, delay=0), last_folder=None)
    env.url = lambda lid: "%s/%d" % (env.base, lid)

    def pull(layer, where="1=1", seed=None):
        stub.layers = {0: layer}
        del fake.warnings[:]
        gdb = _scratch_gdb(tempfile.mkdtemp(dir=root))
        if seed is not None:
            seed["target"] = os.path.join(gdb, "Pts")
            fake.classes[seed["target"]] = seed["seed"]
        return download_layer(env.session, env.base, {"id": 0, "name": layer.name}, gdb,
                              None, set(), where=where), gdb

    def attach(layer, token=None, allow=False):
        stub.layers = {0: layer}
        env.last_folder = tempfile.mkdtemp(dir=root)
        gdb = os.path.join(env.last_folder, "a.gdb")
        summary = pull_attachments(env.session, env.base, {"id": 0, "name": "Pts"}, "Pts", gdb,
                                   len(layer.rows), token=token, allow_uncounted=allow)
        return summary, env.last_folder

    env.pull, env.att = pull, attach
    saved_env = dict(os.environ)
    os.environ["NO_PROXY"] = "127.0.0.1"
    try:
        with _swapped("arcpy", fake), _swapped("RETRIES", 0), _swapped("DELAY", 0):
            _http_checks(t, env)
            stub.layers = {0: _Layer(n=2500, cap=1000)}
            _layer_checks(t, env.base, 0, tempfile.mkdtemp(dir=root))
            _strategy_checks(t, env)
            _attachment_checks(t, env)
            _service_checks(t, env)
            _cli_checks(t, env)
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        server.shutdown()
        server.server_close()
        shutil.rmtree(root, ignore_errors=True)
    return t.footer()


def self_check_live():
    """The layer checks against SELF_CHECK_URL, a real server, with the real arcpy."""
    if arcpy is None:
        sys.exit("--self-check-live needs the real arcpy. Run it with ArcGIS Pro's Python.")
    t = _Harness()
    folder = tempfile.mkdtemp(prefix="restdl_check_")
    try:
        expected, size, naive = _layer_checks(t, SELF_CHECK_URL, SELF_CHECK_LAYER, folder)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    print("live: %d records, page size %d, a naive 5000 stride would have captured only %d"
          % (expected, size, naive))
    return t.footer()


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
    ap.add_argument("--self-check", action="store_true",
                    help="Run the offline self-check against a local stub and exit")
    ap.add_argument("--self-check-live", action="store_true",
                    help="Run the layer checks against SELF_CHECK_URL and exit")
    ap.add_argument("--apply", action="store_true",
                    help="Write the geodatabase. Without it the run only reads and reports")
    ap.add_argument("--resume", action="store_true",
                    help="Skip layers an earlier interrupted run already finished")
    ap.add_argument("--attachments", action="store_true",
                    help="Also download attachments, refusing any batch that came back short")
    ap.add_argument("--delay", type=_seconds, default=None,
                    help="Seconds between requests, to stay under a server's rate limit "
                         "(default: DELAY in CONFIG)")
    ap.add_argument("--allow-uncounted-attachments", action="store_true",
                    help="Accept attachment lists checked only against the server cap, "
                         "when the server cannot count them per feature")
    return ap


def _seconds(text):
    value = float(text)
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("delay must be a finite number of 0 or more seconds")
    return value


def _token(args):
    """The token: standard input for "-", else the argument, else FULLPULL_TOKEN, else CONFIG."""
    if args.token == "-":
        return sys.stdin.readline().strip() or None
    return args.token or os.environ.get("FULLPULL_TOKEN") or TOKEN or None


def main(argv=None):
    # One code path for both entry points: an ArcGIS script tool hands its parameters
    # to the script as sys.argv, so positional arguments serve the tool and the shell
    # alike. Unset tool parameters arrive as empty strings, hence the `or default`.
    ap = _parser()
    args = ap.parse_args(argv)

    if args.self_check:
        return self_check()
    if args.self_check_live:
        return self_check_live()

    # Anything not supplied on the command line falls back to the CONFIG block, so the
    # script runs bare for the common case and stays scriptable for the rest.
    service_url = args.service_url or SERVICE_URL
    output_folder = args.output_folder or OUTPUT_FOLDER
    if not service_url or not output_folder:
        ap.error("set SERVICE_URL and OUTPUT_FOLDER in CONFIG, or pass them as arguments")
    if arcpy is None and ARCPY_LICENCE_ERROR:
        sys.exit(
            "arcpy imported but raised: %s\nSign in to ArcGIS Pro, or set up its licence, "
            "then run this again with the same Python." % ARCPY_LICENCE_ERROR
        )
    if arcpy is None:
        # arcpy ships only with ArcGIS Pro's bundled Python, not a plain system install.
        # Without this, a first run on the wrong interpreter is an opaque import error.
        sys.exit(
            "arcpy was not found. Run this with the Python that ships with ArcGIS Pro:\n"
            r'  "C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" '
            "fullpull.py\n"
            r"or the propy.bat in ...\Pro\bin\Python\Scripts\."
        )

    download_service(
        service_url,
        output_folder,
        args.gdb_name or GDB_NAME or None,
        token=_token(args),
        out_sr=args.out_sr or OUT_SR or None,
        where=args.where or WHERE,
        resume=args.resume,
        attachments=args.attachments,
        delay=args.delay,
        allow_uncounted=args.allow_uncounted_attachments or ALLOW_UNCOUNTED_ATTACHMENTS,
        apply=args.apply or APPLY,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
