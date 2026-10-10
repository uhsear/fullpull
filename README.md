# fullpull

Download every layer of an ArcGIS REST service into a File Geodatabase, verified complete.

Point it at a MapServer or FeatureServer URL. It pulls every layer and standalone table into a file
geodatabase you can open in ArcGIS Pro. Before accepting a layer it compares the rows it wrote
against the count the server reports. If they disagree it raises, and leaves your previous copy
alone.

## Why

The obvious way to page a REST service is wrong, and it fails quietly.

```python
max_records = 5000
for offset in range(0, total_count, max_records):
    query(resultOffset=offset, resultRecordCount=max_records)
```

Most services cap `maxRecordCount` at 1000 or 2000. Asking for 5000 does not raise an error. The
server returns its cap, and the loop then strides ahead by the number it asked for. Every row in
between is never requested.

Run against a live 11 layer service, that loop retrieved 19,760 of 36,240 records and printed a
success message for every layer:

| Layer | On server | Retrieved | Lost |
|---|---:|---:|---:|
| Storm Pipes | 16,316 | 7,316 | 55% |
| Storm Inlets | 10,226 | 4,226 | 59% |
| Mitered End Sections | 2,901 | 2,000 | 31% |
| Stormwater Ditches | 2,579 | 2,000 | 22% |

No exception, no warning, and a feature class that opens fine. You find out when someone notices
the map is missing features.

## What it handles

- Page size read from the layer's own `maxRecordCount`, capped by `MAX_PAGE`.
- Offset advances by rows actually returned, never by the number requested.
- Row count verified per layer. A shortfall raises instead of shipping.
- Writes to a staging class and swaps on success, so a failure halfway through leaves the previous
  copy intact.
- HTTP 200 responses carrying an `{"error": ...}` body are treated as failures, which is how ArcGIS
  Server reports a dead or renamed service.
- Output spatial reference pinned to the layer's own. A MapServer otherwise defaults to the map's
  spatial reference, which need not match.
- Z and M values requested when the layer has them. They default off server side, quietly
  flattening a Z enabled layer to 2D.
- Standalone tables as well as feature layers.
- Group, Network Analysis, and Network Dataset layers skipped and logged. They hold no rows and
  return HTTP 400 on a count query.
- Name collisions suffixed rather than overwritten. Two layers in one service can share a name, and
  geodatabase name validation can fold two names into one.
- Four paging strategies, cheapest first. A strategy that raises, or that finishes with the wrong
  record count, hands the layer to the next one. Only an exhausted list fails the layer.
- Resume: `--resume` skips layers an interrupted run already finished.
- Retries with backoff on 429 and 5xx, request timeouts, connection reuse, token support.
- An optional delay between requests (`--delay` or `DELAY`), for public servers behind a web
  application firewall that bans clients for their request rate. On by default at 0.5 seconds;
  `--delay 0` turns it off for your own server.

## The four strategies

| # | Strategy | Used when | Survives |
|---|---|---|---|
| 1 | `resultOffset` paging | the layer reports `supportsPagination` | nothing extra; it is simply the fastest |
| 2 | `objectIds` batching | pagination is absent, or strategy 1 came up short | a server that ignores `resultOffset` and re-serves page one |
| 3 | `objectId` range windows | the ID list request itself fails | a layer too large to hand back its own ID list |
| 4 | envelope quadtree | everything else failed | a layer that returns HTTP 500 above some row count |

Strategy 2 is the pre-10.3 fallback that was already here. Servers before 10.3 omit
`supportsPagination` and ignore `resultOffset`, re-serving page one; if the row count divides evenly
by the page size, a count check alone would accept the duplicates.

Strategy 3 asks only for the smallest and largest OID, then walks that range in where-clause windows
`MAX_PAGE` wide. OIDs are unique, so a window that wide can never hold more rows than one page.
No window can be truncated. Sparse OIDs cost empty requests, never lost rows.

Strategy 4 never asks for a slice that could be too big. It queries an envelope, treats any full
response as truncated, and splits it into quadrants. Requests only ever get smaller, which is why
this is the one that survives a server that 500s on large responses. It is also by far the slowest.
Three things it does that a naive quadtree does not:

- It starts from `returnExtentOnly` on the actual query, not the published layer extent. Esri's own
  SF311 sample layer publishes a whole-world extent for data that fits inside San Francisco;
  starting there wastes eight levels of subdivision before the first row appears.
- Boundary features are returned by every quadrant that touches them, so rows are de-duplicated on
  the OID. This is why strategy 4 needs an OID field.
- At `MAX_ENVELOPE_DEPTH` it stops splitting and fetches the cell by its ID list, so a block of
  coincident features cannot subdivide forever.

Strategy 4 refuses up front if any row has no geometry, because no spatial query can ever reach one.
It costs two count requests to find out. Measured on SF311: 9,705 rows, of which 9,703 answer a
full-extent envelope query. Refusing is the point — the alternative is sweeping the whole layer and
returning 9,703 rows that look like a complete download.

A layer that falls back logs an `AddWarning` naming the strategy that worked and every failure
before it. A silent fallback would hide a server that is misreporting its own capabilities.

## Requirements

ArcGIS Pro, tested on 3.6. `arcpy` lives in Pro's bundled Python, not on a system Python:

```
"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" fullpull.py
```

`propy.bat` in `...\Pro\bin\Python\Scripts\` does the same. `requests` and `urllib3` ship with that
environment. Nothing to install.

## Usage

Set the CONFIG block at the top of the file and run with no arguments:

```python
SERVICE_URL   = "https://example.gov/arcgis/rest/services/Public/Utilities/MapServer"
OUTPUT_FOLDER = "downloads"   # must already exist
GDB_NAME      = ""        # "" names the gdb after the service
TOKEN         = ""        # for secured services
OUT_SR        = ""        # "" keeps the layer's own spatial reference
WHERE         = "1=1"     # server side row filter

MAX_PAGE      = 5_000     # ceiling on one page, whatever the service advertises
TIMEOUT       = 180       # seconds per request
RETRIES       = 4         # retries with backoff on 429 and 5xx
DELAY         = 0.5       # seconds between requests; 0 for your own server

MAX_ENVELOPE_DEPTH = 12   # subdivisions before strategy 4 fetches a cell by ID list
```

```
propy fullpull.py
```

Arguments override CONFIG:

```
propy fullpull.py <service_url> <output_folder> [gdb_name] [token] [out_sr] [where]
```

Flags:

| Flag | Does |
|---|---|
| `--self-check` | Run the checks below and exit |
| `--resume` | Skip layers an earlier interrupted run already finished |
| `--delay SECONDS` | Wait this long between the start of one request and the next. Overrides `DELAY` (0.5). `--delay 0` turns it off. A negative value is refused |

To use it as an ArcGIS script tool, add it with those six parameters in that order. A script tool
passes parameters as positional arguments, so there is no separate code path.

## Resume

Every completed layer is recorded in `<geodatabase>.progress.json` next to the geodatabase, written
atomically after each layer. A run that fails part way through leaves that file behind; `--resume`
reads it and skips the layers it names, provided the feature class is still there. A clean run
deletes it, so a scheduled `--resume` job cannot skip an entire service and report success without
fetching a row.

Resume trusts the earlier run's word that a layer finished. It does not re-verify that layer's count
against the server, so a layer that changed between the two runs stays as the first run left it
until the next full pull. That is the trade a resume makes.

## Self check

```
propy fullpull.py --self-check
```

Fifteen offline assertions run first, so a network fault cannot mask a logic bug: envelope splitting
(four quadrants, tiling the parent exactly, spatial reference carried, no invented empty one), the
progress file (round trip, a later save not losing an earlier layer, a truncated file and a
wrong-shaped file both reading as nothing done rather than raising), the parser refusing an
abbreviated flag, and request pacing (requests at least `DELAY` apart, no sleep at 0, a negative
`--delay` refused).

The live half then runs against a layer holding more records than its own `maxRecordCount`, the
shape that breaks a naive pager. It asserts that paging returns every row, that an oversized page
request still returns every row, that the naive 5000 stride demonstrably loses rows, and that a full
`download_layer` writes the verified count.

It defaults to Esri's public sample server. Point it at your own with `SELF_CHECK_URL` and
`SELF_CHECK_LAYER`. The target layer needs more records than its `maxRecordCount`, and that
`maxRecordCount` must be under 5000, or the stride assertion has nothing to show.

## Notes

Verified against a live ArcGIS Server 11.3:

- `maxRecordCount` is a cap, not a promise. One layer advertises 250,000 and returns HTTP 500 above
  roughly 25,000.
- `objectIdField` can be absent from MapServer layer JSON. Read the OID field from `fields[]` where
  `type` is `esriFieldTypeOID`.
- `arcpy.conversion.ExportFeatures` accepts a REST URL directly and pages correctly. It is also
  about 180 times slower, 254s against 1.4s for the same 3,317 records, measured with `--delay 0`. For one small layer
  occasionally, use it and skip this.
- Buffering pages in memory does not scale. Holding 47k polylines before writing peaked at 2.8 GB.
  Converting page by page holds near 0.55 GB.

## Not included

- Coded value domains. Domain definitions live on layer metadata, not the query response. Worth
  adding if the output becomes an editing target rather than a read only snapshot.
- Date timezone correction. ArcGIS serialises dates as UTC epoch milliseconds, and shifting them
  needs each layer's `dateFieldsTimeReference`. A blanket shift would corrupt layers that declare
  none.
- Parallel page fetching. Conversion, not HTTP, is the bottleneck, and `arcpy` is not thread safe.
- Attachments and related tables.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [restfake](https://github.com/uhsear/restfake) - a fake REST endpoint to test this against, including the page that comes back short
- [fcload](https://github.com/uhsear/fcload) - loading the downloaded geodatabase somewhere else, without corrupting it
- [svcdrift](https://github.com/uhsear/svcdrift) - check the schema still matches its source before you pull
