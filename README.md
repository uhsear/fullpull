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
- Falls back to `objectIds` batching when a service reports `supportsPagination: false`, and also
  when it does not report the capability at all. Servers before 10.3 omit it and ignore
  `resultOffset`, re-serving page one; if the row count divides evenly by the page size, a count
  check alone would accept the duplicates.
- Retries with backoff on 429 and 5xx, request timeouts, connection reuse, token support.

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
```

```
propy fullpull.py
```

Arguments override CONFIG:

```
propy fullpull.py <service_url> <output_folder> [gdb_name] [token] [out_sr] [where]
```

To use it as an ArcGIS script tool, add it with those six parameters in that order. A script tool
passes parameters as positional arguments, so there is no separate code path.

## Self check

```
propy fullpull.py --self-check
```

Runs against a live layer holding more records than its own `maxRecordCount`, the shape that breaks
a naive pager. It asserts that paging returns every row, that an oversized page request still
returns every row, that the naive 5000 stride demonstrably loses rows, and that a full
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
  about 180 times slower, 254s against 1.4s for the same 3,317 records. For one small layer
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
