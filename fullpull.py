"""Download every layer of an ArcGIS REST service into a File Geodatabase.

Works against MapServer and FeatureServer endpoints. Runs from a shell or as an ArcGIS
script tool; a script tool passes its parameters as positional arguments, so the same
argument list serves both.

The contract that matters: a layer either lands complete or it raises. Every layer is
verified against the server's own record count before it is accepted.

Why not just hand the REST URL to arcpy.conversion.ExportFeatures, which also works and
also pages correctly? Measured on Public/PublicWorks/0 (3,317 records): ExportFeatures
took 254s, this script took 1.4s. Roughly 180x, so the manual paging earns its keep.

Edit the CONFIG block below and run with no arguments, or pass everything on the command
line. Command-line arguments win over CONFIG when both are set.

    python fullpull.py                          # uses CONFIG below
    python fullpull.py <service_url> <output_folder>
    python fullpull.py --self-check
"""

import argparse
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
    ids.sort()
    params = {"outFields": "*", "returnGeometry": "true", **(geom or {})}
    for i in range(0, len(ids), size):
        batch = ids[i:i + size]
        page = rest_get(
            session, f"{layer_url}/query",
            {**params, "objectIds": ",".join(str(x) for x in batch)}, token=token,
        )
        if page.get("features"):
            yield page


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
    if paged:
        pages = iter_pages(session, layer_url, size, oid, expected,
                           token=token, geom=geom, where=where)
    else:
        pages = iter_pages_by_oid(session, layer_url, size,
                                  token=token, geom=geom, where=where)

    tmpdir = tempfile.mkdtemp(prefix="restdl_")
    try:
        for seq, page in enumerate(pages):
            _write_page(page, staging, tmpdir, seq)

        if not arcpy.Exists(staging):
            raise RuntimeError(
                f"{layer['name']}: server reported {expected} records but returned none."
            )
        got = int(arcpy.management.GetCount(staging)[0])
        if expected is not None and got != expected:
            raise RuntimeError(
                f"{layer['name']}: downloaded {got} of {expected} records; "
                "existing output left untouched."
            )

        if arcpy.Exists(target):
            arcpy.management.Delete(target)
        arcpy.management.Rename(staging, target)
    except Exception:
        if arcpy.Exists(staging):
            arcpy.management.Delete(staging)
        raise
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

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


def download_service(service_url, output_folder, gdb_name=None,
                     token=None, out_sr=None, where=None):
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
    results, failures = [], []
    for layer in layers:
        try:
            name, count = download_layer(session, service_url, layer, gdb, service_max,
                                         taken, token=token, out_sr=out_sr, where=where)
            results.append((name, count))
            arcpy.AddMessage(f"{layer['name']}: {count} records -> {os.path.join(gdb, name)}")
        except Exception as exc:
            failures.append((layer["name"], str(exc)))
            arcpy.AddError(f"{layer['name']}: FAILED - {exc}")

    arcpy.AddMessage(f"Done. {len(results)} layer(s) complete, {len(failures)} failed.")
    if failures:
        # A partial run must not exit clean, or a scheduled job reports success on bad data.
        raise RuntimeError("Failed layers: " + "; ".join(n for n, _ in failures))
    return results


# ------------------------------------------------------------------- self-check


def self_check():
    """Live check against a layer whose record count exceeds the server page size.

    That shape is the whole point: it is the case where a naive pager silently drops
    rows, so the check proves both that this pager gets them all and that the naive
    stride does not. Configure the target with SELF_CHECK_URL / SELF_CHECK_LAYER.
    """
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


def main(argv=None):
    # One code path for both entry points: an ArcGIS script tool hands its parameters
    # to the script as sys.argv, so positional arguments serve the tool and the shell
    # alike. Unset tool parameters arrive as empty strings, hence the `or default`.
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("service_url", nargs="?", help="MapServer or FeatureServer URL")
    ap.add_argument("output_folder", nargs="?", help="Folder to hold the file geodatabase")
    ap.add_argument("gdb_name", nargs="?", default="", help="Geodatabase name")
    ap.add_argument("token", nargs="?", default="", help="Token for a secured service")
    ap.add_argument("out_sr", nargs="?", default="", help="Output WKID, e.g. 2237")
    ap.add_argument("where", nargs="?", default="", help="Server-side filter")
    ap.add_argument("--self-check", action="store_true", help="Run the live self-check and exit")
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
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
