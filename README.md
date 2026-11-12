# fullpull

Download every layer of an ArcGIS REST service into a File Geodatabase, and refuse any layer or
attachment batch that came back short.

## The pull that said it worked

Picture a copy job that pulls a service of several layers into a geodatabase. Every layer prints a
success line. Nobody sees a problem until someone notices the map is missing features. The original
build of this tool ran the loop below against a live 11 layer service and watched exactly that.

The job pages the obvious way:

```python
max_records = 5000
for offset in range(0, total_count, max_records):
    query(resultOffset=offset, resultRecordCount=max_records)
```

Most services cap `maxRecordCount` at 1000 or 2000. Asking for 5000 does not raise an error. The
server returns its cap, and the loop strides ahead by the number it asked for. Every row in between
is never requested. Run against four layers on a stub server with a cap of 1,000, with invented
row counts, the loop retrieves 8,500 of 30,700 records:

| Layer | On server | Retrieved | Lost |
|---|---:|---:|---:|
| Layer A | 16,000 | 4,000 | 75% |
| Layer B | 10,500 | 2,500 | 76% |
| Layer C | 3,000 | 1,000 | 67% |
| Layer D | 1,200 | 1,000 | 17% |

The table was produced by a one-off script that ran the naive 5,000-row stride loop against the stub
of the self-check, with a cap of 1,000 rows per page on each layer. The self-check does not rerun
it, so no command in this repository regenerates the table. It was not copied from a service.

Fixing the stride fixed that failure. It left three quieter ones, and this round closes them:

- **The count that matches by accident.** A server that ignores `resultOffset` serves page one
  again and again. When the layer holds exactly a whole number of pages, the row count at the end
  equals the server count, so a count check passes. The old release (commit 18fe1b7), run once
  against the same stub, accepted a 2,000 row layer that held 1,000 distinct rows. It now counts
  rows whose objectId was already seen, and refuses that strategy. The layer is then recovered by
  the next strategy, objectId batching, which landed 2,000 distinct rows with a warning (the old
  release wrote 2,000 rows holding 1,000 distinct). The layer fails only when every strategy fails.
- **The count that never came.** A server that answers the count query with no `count` gave the old
  release nothing to compare with. The same run showed it accepting 100 rows with no count to check
  them against. It now refuses the layer, because completeness cannot be shown.
- **The attachments.** A pull that copies the rows but not the photos is complete only on paper.
  `--attachments` pulls the files. It refuses any batch of features whose attachment list is shorter
  than the server's own per-feature count, splits a batch that hit the server's cap, refuses a file
  whose body is shorter than the size the server lists, and refuses a file whose body length differs
  from its own `Content-Length`. A server that cannot count attachments per feature is refused unless
  you opt in.

## What already exists

`arcpy.conversion.ExportFeatures` needs no code, and for one small layer, once, it is the first
thing to try. Its [tool page](https://pro.arcgis.com/en/pro-app/latest/tool-reference/conversion/export-features.htm)
does not say whether a REST layer URL is accepted as input, how such an input is paged, or whether a
short read is detected. This round did not test any of that, because real arcpy would not initialise
here. The original build measured it once on a layer of a few thousand records: 254 seconds against 1.4 seconds
for this script, with the request delay off. That result was not repeated.

Esri's [Maintain Attachments](https://pro.arcgis.com/en/pro-app/latest/tool-reference/environment-settings/maintain-attachments.htm)
page lists Export Features among the tools that copy attachments from the input features, with the
setting on by default. Whether that holds when the input is a REST URL was not tested. fullpull does
not rely on it. It lists attachments from the server and checks each batch.

fullpull adds the whole service in one run, a verdict per layer from the server's own count, four
paging strategies that hand over to each other, a staging class that swaps in only after the
verdict, `--resume`, and the attachment checks above.

## What it handles

- Page size read from the layer's own `maxRecordCount`, capped by `MAX_PAGE`.
- Offset advances by rows actually returned, never by the number requested.
- A layer is accepted only when the rows read back from the staging class equal the server count and
  no objectId came back twice. A shortfall, an overshoot, a repeat and a missing count each refuse.
- Writes to a staging class and swaps on success, so a failure leaves the previous copy intact.
- HTTP 200 responses carrying an `{"error": ...}` body are failures, which is how ArcGIS Server
  reports a dead or renamed service.
- Output spatial reference pinned to the layer's own. A MapServer otherwise defaults to the map's.
- Z and M requested only when the layer has them.
- Feature layers. Group layers are skipped and logged. Other layer types that cannot be queried are
  skipped with a warning. Standalone tables are attempted too; see Limits.
- Name collisions suffixed rather than overwritten.
- Four paging strategies, cheapest first. A strategy that raises, or that finishes with the wrong
  count, hands the layer to the next one. Only an exhausted list fails the layer.
- `--resume` skips layers an interrupted run finished. A layer finished without attachments is not
  skipped by a run that asks for them, and a layer finished for another service, `--where` filter or
  output SR is downloaded again.
- Retries with backoff on 429 and 5xx, request timeouts, token support. The token travels in the
  POST body of every query, and in the URL only for the GET of an attachment. It is scrubbed from
  the attachment error messages and from the non-JSON and ArcGIS error messages, and masked in the
  log lines urllib3 writes when it retries a dropped attachment download.
- An optional delay between requests (`--delay` or `DELAY`), for public servers behind a web
  application firewall that bans clients for their request rate. On by default at 0.5 seconds;
  `--delay 0` turns it off for your own server.
- A pull by resultOffset is checked again at the end against the server's current object id list,
  because an edit made mid-pull (one row deleted, one added) can keep the count and shift a row
  across a page boundary unread. An id the server holds that no page carried refuses that strategy,
  and the next one fetches by id.
- A service URL with a query string or fragment, such as a token pasted from a browser address bar,
  is refused before anything is written, and the message does not echo it. So is a URL with a user
  name or password in front of the host, which `requests` would put in its error text and the default
  geodatabase name.
- With `--attachments`, a layer whose description has no `hasAttachments` key fails the run, because
  the tool cannot tell it from a layer with none.
- A layer the server now counts as empty, while the geodatabase still holds rows for it, is refused
  rather than left stale. A layer with no old copy completes with a warning.

## The four strategies

| # | Strategy | Used when | Survives |
|---|---|---|---|
| 1 | `resultOffset` paging | the layer reports `supportsPagination` | nothing extra; it is simply the fastest |
| 2 | `objectIds` batching | pagination is absent, or strategy 1 came up short | a server that ignores `resultOffset` and re-serves page one |
| 3 | `objectId` range windows | the ID list request itself fails | a layer too large to hand back its own ID list |
| 4 | envelope quadtree | everything else failed | a layer that returns HTTP 500 above some row count |

Strategy 3 asks only for the smallest and largest OID, then walks that range in where-clause windows
as wide as the page size. OIDs are unique, so a window that wide never holds more rows than one page.
Sparse OIDs cost empty requests, never lost rows.

Strategy 4 never asks for a slice that could be too big. It queries an envelope, treats any full
response as truncated, and splits it into quadrants. It starts from `returnExtentOnly` on the actual
query, not the published extent, de-duplicates boundary rows on the OID, and at `MAX_ENVELOPE_DEPTH`
fetches the cell by its ID list. It refuses up front when any row has no geometry, because no spatial
query can reach one.

A layer that falls back logs an `AddWarning` naming the strategy that worked and every failure
before it.

## Attachments

```
propy fullpull.py <service_url> <output_folder> --attachments
```

Files land in `<geodatabase>.attachments/<layer>/<feature objectId>/<attachment id>_<name>`. The
name is reduced to ASCII letters, digits, `.`, `_` and `-`. The feature objectId and the attachment
id must be integers. A list that holds anything else is refused, so a server cannot place a file
outside its folder through the name or through an id. A layer id that is not an integer is refused
too, because it goes into every request path.

For a layer with `hasAttachments` the pass does this:

1. Ask the server for the object ID list. It must be exactly as long as the verified row count. A
   short list would hide whole features from the pass. Esri's Query page, under `returnIdsOnly`,
   documents a limit of 1 million IDs in that response, so a larger layer would refuse here the
   same way, not be partly pulled. The Query Attachments page states no limit of its own.
2. Ask `queryAttachments` for a batch of at most `ATTACH_BATCH` features.
3. Ask the same question with `returnCountOnly=true`, and compare, feature by feature, the count
   with the list. A feature the server counts but the list lacks, a feature listed twice or never
   asked for, and an attachment with no id or a non-integer id each refuse the batch.
4. A refused batch is split in half and asked again. A smaller batch cannot overflow a cap the
   larger one hit. A single feature that still disagrees raises, and the run fails.
5. If the server cannot answer the count form, the layer is refused, because a list cut short
   below the server's record cap could not be seen. The same holds when the layer's
   `supportsQueryAttachmentsCountOnly` is false (the count form is then not even sent) and when the
   count form answers with a 4xx status, an ArcGIS error, or full lists with no counts. A 429, a 5xx
   or a dead connection on the count form is not read as "cannot count": it fails the layer.
   `--allow-uncounted-attachments` (or `ALLOW_UNCOUNTED_ATTACHMENTS = True`) accepts the cap rule
   instead: the batch is taken when its answer is under the server's record cap, and the run warns
   that completeness rests on that rule alone.
6. Every file is written to `.part`, checked, and renamed. A body whose length differs from its
   `Content-Length`, or that is shorter than the `size` in the list, is refused and no file is kept.

A layer that has attachments but does not advertise `supportsQueryAttachments` is refused, not
skipped. A run that fails on an attachment fails its layer and does not record it in the progress file.

### What the documentation says, and what was seen

- `queryAttachments` takes `objectIds`, returns `attachmentGroups` with `parentObjectId` and
  `attachmentInfos` (`id`, `name`, `contentType`, `size`). It also supports `returnCountOnly`, but
  the page says so for hosted feature services only, from 10.9.1, and only when the layer's
  `supportsQueryAttachmentsCountOnly` property under `advancedQueryCapabilities` is true. For the
  count form the page says it "only returns results for features matching the query criteria that
  have a non-zero number of attachments", which is why a feature missing from a count answer is read
  as having none (`counts.get(parent, 0)`). For the list form the page does not say whether a
  feature with no attachments appears.
  [Query Attachments](https://developers.arcgis.com/rest/services-reference/enterprise/query-attachments-feature-service-layer/)
- An attachment is downloaded from `<layer>/<featureId>/attachments/<attachmentId>`.
  [Attachment](https://developers.arcgis.com/rest/services-reference/enterprise/attachment-feature-service/)
- The Attachment Infos page shows `size` with no unit, while the Query Attachments page says its
  `size` filter is "specified in bytes".
  [Attachment Infos](https://developers.arcgis.com/rest/services-reference/enterprise/attachment-infos-feature-service/)
  The unit of `size` in the list is not stated on its own page, so a body longer than `size` is
  accepted and only a shorter one is refused.
- `hasAttachments` and `advancedQueryCapabilities.supportsQueryAttachments` gate the operation.
  [Layer](https://developers.arcgis.com/rest/services-reference/enterprise/layer-feature-service/)
- `returnCountOnly` returns `count`, and `returnIdsOnly` returns `objectIds` and is limited to 1
  million IDs. `resultOffset` skips that many records and applies only if `supportsPagination` is
  true. When `outSR` is not given, the geometry comes back in the map's spatial reference, which is
  why fullpull pins `outSR`. `returnExtentOnly` is supported when `supportsReturningQueryExtent` is
  true, which fullpull does not read: it tries the request and falls back to the published extent.
  `outStatistics` takes `MIN` and `MAX`. fullpull does not rely on `exceededTransferLimit` to mark
  a last page: an offset pull ends on the server's own count, or on an empty page, and the count is
  then checked. `returnZ` and `returnM` default to false and apply only when `returnGeometry` is true
  and the layer's `hasZ` or `hasM` is true, which is why fullpull asks for them only then.
  `orderByFields` is "supported on only those layers/tables that indicate supportsAdvancedQueries is
  true". fullpull sends it whenever it knows the OID field, and a refusal there hands the layer to
  the next strategy.
  [Query](https://developers.arcgis.com/rest/services-reference/enterprise/query-feature-service-layer/)
- `Append` with `NO_TEST` does not require the fields of the input to match the target, and fields
  that do not match are not mapped unless a field map says so.
  [Append](https://pro.arcgis.com/en/pro-app/latest/tool-reference/data-management/append.htm)
- Seen once on 2026-11-11 against Esri's public SF311 sample layer (three read-only requests and
  one 12,516 byte download): the layer advertises `hasAttachments` and `supportsQueryAttachments`,
  it answers `returnCountOnly` with the full lists and no counts, and the body, `Content-Length` and
  `size` of the one file fetched were all 12,516. fullpull treats that count answer as "cannot
  count". A run of `--attachments` against that layer is therefore refused unless
  `--allow-uncounted-attachments` is given. The self-check exercises both paths with a stub that
  answers the same way.

## Requirements

A real pull needs ArcGIS Pro. The original build ran against Pro 3.6; this round did not run real
arcpy. `arcpy` lives in Pro's bundled Python:

```
"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" fullpull.py
```

`propy.bat` in `...\Pro\bin\Python\Scripts\` does the same. `requests` and `urllib3` ship with that
environment. `--self-check` needs only Python 3.9 or later with `requests`. It starts its own stub.
The file still tries `import arcpy` when it loads, and ignores a failed import, but the self-check
never calls arcpy. On a host where `import arcpy` is missing, a real pull exits with the message
above. Where it imports but raises, such as a Pro install whose licence is not initialised, a real
pull exits with the licence error text instead of a traceback or a claim that arcpy is missing.

## Usage

Set the CONFIG block at the top of the file and run with no arguments. A run without `--apply`
(or `APPLY = True` in CONFIG) is a dry run that writes nothing. See "Dry run" below.

```python
SERVICE_URL   = "https://example.gov/arcgis/rest/services/Public/Utilities/MapServer"
OUTPUT_FOLDER = "downloads"   # must already exist
GDB_NAME      = ""        # "" names the gdb after the service
TOKEN         = ""        # for secured services; prefer the FULLPULL_TOKEN variable
OUT_SR        = ""        # "" keeps the layer's own spatial reference
WHERE         = "1=1"     # server side row filter

MAX_PAGE      = 5_000     # ceiling on one page, whatever the service advertises
TIMEOUT       = 180       # seconds per request
RETRIES       = 4         # retries with backoff on 429 and 5xx
DELAY         = 0.5       # seconds between requests; 0 for your own server

MAX_ENVELOPE_DEPTH = 12   # subdivisions before strategy 4 fetches a cell by ID list
ATTACH_BATCH       = 100  # features per attachment query
ALLOW_UNCOUNTED_ATTACHMENTS = False  # True accepts the cap rule alone
APPLY         = False     # False is a dry run; True writes the geodatabase
```

```
propy fullpull.py
```

Arguments override CONFIG:

```
propy fullpull.py <service_url> <output_folder> [gdb_name] [token] [out_sr] [where]
```

| Flag | Does |
|---|---|
| `--apply` | Write. Without it the run lists the layers and the server's counts and writes nothing |
| `--self-check` | Run the offline checks against a local stub and exit |
| `--self-check-live` | Run the layer checks against `SELF_CHECK_URL`, a real server, and exit |
| `--resume` | Skip layers an earlier interrupted run already finished |
| `--attachments` | Also download attachments, refusing any batch that came back short |
| `--allow-uncounted-attachments` | With `--attachments`, accept a server that cannot count attachments per feature, on the cap rule alone |
| `--delay SECONDS` | Wait this long between the start of one request and the next. Overrides `DELAY` (0.5). `--delay 0` turns it off. A negative value is refused |

A unique prefix of a flag is refused, so `--attach` is an error and not `--attachments`.

Give the token in the `FULLPULL_TOKEN` environment variable, or give `-` as the token argument to
read it from standard input. A token typed as an argument shows in the process list and the shell
history, and one pasted into `TOKEN` sits in a file that may be committed. The order of precedence is
the argument, then `FULLPULL_TOKEN`, then `TOKEN`. Do not leave the token on the service URL: a URL
with a query string or fragment is refused. In an ArcGIS Pro script tool, set `FULLPULL_TOKEN` and
leave the token parameter empty. Esri's geoprocessing history page says history keeps a tool's
parameter settings and does not say it masks them, so a token typed into a parameter may be saved
with the project.
[Geoprocessing history](https://doc.esri.com/en/arcgis-pro/latest/help/analysis/geoprocessing/basics/geoprocessing-history.html)

To use it as an ArcGIS script tool, add it with the six positional parameters in that order. A script
tool passes parameters as positional arguments, so there is no separate code path. Flags are for the
shell.

### Dry run and `--apply`

Only `--apply` writes. A run without it reads the layer list, asks each layer for its record count,
and with `--attachments` reads each layer's description to check that the server can list
attachments in batches. It then prints how many layers answered, writes no geodatabase, class,
progress file or attachment, and does not use `--resume`. It does not run a paging strategy, so it
cannot see a short read, a repeated page or an uncounted attachment batch. Those are refused only
by `--apply`. A clean dry run is therefore not a promise that `--apply` will succeed. A layer whose
count fails, or whose description fails the attachment check, is named as "WOULD BE REFUSED" and the
dry run fails with exit 1 and a Python traceback.

With `--apply`, a layer is replaced only after its staging class passed the verdict. `--attachments`
is opt-in and overwrites files of the same name. It does not delete files left by an earlier run.
An ArcGIS script tool passes only the six positional parameters and no flag, so set `APPLY = True`
in CONFIG there, or the run only reads.

## Resume

Every completed layer is recorded in `<geodatabase>.progress.json` next to the geodatabase, written
atomically after each layer, with whether its attachments were pulled and the service URL, `--where`
filter and output SR it was pulled with. A run that fails part way
leaves that file behind. `--resume` reads it and skips the layers it names, provided the feature class
is still there, the service, filter and output SR match, and, for a run with `--attachments`, the
layer was recorded with its attachments. A layer that does not match is downloaded again. An entry
written by an earlier release has none of these fields, so its layer is downloaded again too. A
clean run deletes the file, so a scheduled `--resume` job cannot skip a whole service and report
success without fetching a row.

Resume trusts the earlier run's word that a layer finished. It does not re-verify the count against
the server, and it does not check that the attachment folder is still there.

## Self-check

```
$ python fullpull.py --self-check
fullpull self-check: stub server on 127.0.0.1, in-memory arcpy, no real network
--------------------------------------------------------------------
...
PASS  a hand-edited entry the resume cannot use is dropped, not allowed to abort the pull  <-- pinned defect
...
PASS  a unique prefix of a flag must be refused, not expanded  <-- pinned defect
...
PASS  a repeated page is refused although the count matches  <-- pinned defect
...
PASS  an attachment id of '../../x' is refused, so no id can steer a file or a URL  <-- pinned defect
...
PASS  the notjson message never echoes the token, in either form  <-- pinned defect
...
PASS  one row deleted and one added mid-pull keeps the count but shifts a row across a page; it is refused and refetched by id, never accepted short  <-- pinned defect
...
PASS  a row added after the id list was read is refused on every strategy  <-- pinned defect
...
PASS  a server that returns no count is refused, not trusted  <-- pinned defect
...
PASS  a server that counts 0 while the old copy holds rows is refused, not kept stale on exit 0  <-- pinned defect
...
PASS  attachment downloads obey --delay like every other request  <-- pinned defect
...
PASS  a list that drops a feature the server counts is refused  <-- pinned defect
...
PASS  a list cut off at the server's cap is split until every attachment arrives  <-- pinned defect
...
PASS  a server that cannot count is refused unless the owner opts in  <-- pinned defect
...
PASS  a body cut short of its Content-Length is refused  <-- pinned defect
...
PASS  a feature list holding ids that are not integers is refused  <-- pinned defect
...
PASS  a feature list one id short of the row count is refused  <-- pinned defect
...
PASS  an attachment named ../../evil.txt stays inside its feature's folder  <-- pinned defect
...
PASS  urllib3's log line for a retried attachment download never carries the token  <-- pinned defect
...
PASS  a resume with another filter downloads the layer again, not 'already complete'  <-- pinned defect
...
PASS  a resume that wants attachments does not skip a layer finished without them  <-- pinned defect
...
PASS  a layer that does not say whether it has attachments fails the run, not exit 0  <-- pinned defect
...
PASS  a dry run with attachments refuses a layer that does not say whether it has them  <-- pinned defect
...
PASS  the token reaches no message, warning, error, progress file or output file  <-- pinned defect
...
PASS  a service URL with a query string or fragment is refused  <-- pinned defect
...
PASS  a service URL with a user name or password is refused  <-- pinned defect
...
PASS  a delay that is not a finite number is refused, not read as no pacing  <-- pinned defect
...
PASS  a prefix of --attachments is refused, not expanded  <-- pinned defect
...
PASS  an arcpy that imports but fails its licence is not reported as missing  <-- pinned defect
...
--------------------------------------------------------------------
244 assertions, 0 failed
```

The full run prints all 244 assertions. Each `...` line is where this block is cut.

It starts an HTTP server on `127.0.0.1` that behaves like ArcGIS Server and can be told to fail in
the ways above: re-serve page one, drop rows it counted, return no count, truncate an attachment
list, send half a body, answer a count query with full lists, demand a token. A small in-memory
stand-in replaces arcpy. The assertions of the earlier releases, including the request delay, are
kept with their meaning. The layer assertions used to need Esri's sample server. They now run
against the stub, and `--self-check-live` runs the same function against `SELF_CHECK_URL` for a
real server.

Exit code 0 means every assertion held. Any failure prints `FAIL` and exits 1.

### Counts per host

| Host | Python | Assertions |
|---|---|---:|
| Windows | 3.13.2 (`C:\Python313`, needs `requests`, see below) | 244 |
| Windows, ArcGIS conda env | 3.13.7 | 244 |
| Windows, venv | 3.9.25 | 244 |
| Windows, venv | 3.12.10 | 244 |
| Linux | 3.12.3 | 244 |

`C:\Python313` has no `requests` of its own. The 244 assertions ran there because `requests` comes
from the user site packages, and `PYTHONNOUSERSITE=1` or `-I` fails with `ModuleNotFoundError`. Run
`python -m pip install requests` first on a bare interpreter.

The counts are equal because the self-check never calls the real arcpy. On the Windows ArcGIS
environment `import arcpy` raised `RuntimeError: The Product License has not been initialized` during
this build. The self-check runs the same either way, and one assertion pins the message a real pull
gives on such a host.

### Coverage

```
python -m coverage run --branch fullpull.py --self-check
python -m coverage report -m --include=fullpull.py
```

reports 1,541 statements with none missed and 434 branches with none partial, 100 percent, on the
geo venv (Python 3.13.2). `--include=fullpull.py` matters on the ArcGIS environment, where the
report would otherwise list arcpy's own files. The two lines that look unreachable offline, the
`except` branches around `import arcpy` and the false side of `if __name__ == "__main__"`, are
reached by running the file again under another name with arcpy made to fail in each way. What
coverage cannot show is whether real arcpy behaves as the stand-in does.

Mutating the tool by hand and re-running the self-check turned it red (a failed assertion, or a
crash inside one) for all 16 changes tried in the first round: ignoring a repeated page, accepting a missing count, an
unsanitised file name, a token that is not scrubbed, a resume that skips a layer without its
attachments, counts that are not compared, an ID list whose length is not checked, no batch
splitting, no size check, the cap rule off, a progress file that forgets attachments, a shortfall
that is not a verdict, no Content-Length check, an accepted error body, a foreign group, and flag
abbreviation switched on. The last one first survived: adding `--self-check-live` made the old
`--self` prefix ambiguous, so argparse refused it for the wrong reason. The assertion now uses
`--self-check-l`, which is a unique prefix. In this round 13 further changes each turned it red: an
attachment id that is not checked as an integer, progress entries that are not filtered, a 5xx on the
count form read as "cannot count", the opt-in check removed, a stale copy kept on a count of 0, a
null extent not guarded, a layer id not checked, the environment token ignored, a licence error
reported as a missing arcpy, feature ids not checked, the count capability ignored, no request
pacing, and the licence text dropped. A third round turned it red for five more: the end-of-pull id
check off, attachment downloads not paced, the query-string refusal off, the token left in the
non-JSON message, and a delay of NaN accepted. A fourth round turned it red for four more, one
assertion group each: the user name and password refusal off, the missing `hasAttachments` check off,
the resume comparison of service, filter and output SR off, and the skipped-layer warning turned
back into a plain message. A fifth round turned it red for three more: the token mask
on urllib3's log records off, the end-of-pull id check limited again to the offset strategy, and the
warning-only pass for the strategies that cannot list ids removed.

## Claims and where each was exercised

| Claim | Exercised by |
|---|---|
| Page size from the layer cap, offset by rows returned | `--self-check` layer checks, stub with cap 1000 and 2,500 rows |
| Short, overshoot, repeated page, missing count each refuse | stub with hidden rows, ignored offset, no count; mutation sweep |
| Staging swap leaves the old copy on failure | stub run with a seeded old class |
| HTTP 200 with an error body fails | stub error body |
| Spatial reference pinned to the layer's | stub receives `outSR` it was not given |
| Four strategies and their hand-over | stub with pagination off, ID list failing, statistics failing |
| Quadtree start extent, de-duplication, depth cap, no-geometry refusal | stub with a grid, a pile on a coordinate, rows with no geometry |
| Retry on 429 and 503, timeout | stub that fails once, stub that sleeps. One retry each; the backoff delay is urllib3's and was not timed |
| The old release accepted a repeated page and a missing count | commit 18fe1b7 run once against the stub, outside the self-check |
| Token in POST, scrubbed from GET failures, absent from every output | stub that demands a token, scan of messages and files |
| The token is masked in urllib3's retry log line | stub that hangs up on the first attachment GET, `urllib3` logging at WARNING, scan of the records |
| A row added after an id list or OID range was read is not accepted | stub that adds a row right after the id list, and after the OID statistics, were answered |
| Resume, with and without attachments | `download_service` against the stub |
| Attachment batches, splitting, cap rule, file checks | stub with each fault in turn |
| `--attachments`, `--resume`, prefix refusal, CONFIG fallback | `main()` against the stub |
| One real server answers `returnCountOnly` with full lists | three requests to Esri's SF311 sample layer, 2026-11-11 |
| An uncounted attachment server, a 5xx on the count form, an id that is not an integer | stub with each fault in turn, `main()` with and without the opt-in |
| Token from `FULLPULL_TOKEN` or standard input | `main()` against a stub that demands a token |
| An empty server count over an old copy with rows is refused | stub layer with 0 rows over a seeded class |
| Requests are paced, attachment downloads included | assertions on the gap between request starts, on three timed downloads, and no sleep at 0 |
| A row shifted across a page by a delete plus an add is not accepted | stub that deletes one row and adds one after page one; the next strategy refetches by id |
| A token on the service URL is refused and never echoed | `download_service` against the stub with `?token=` and `#` tails; messages and the output folder scanned |

## Limits

- **Real arcpy was not run in this round.** `JSONToFeatures`, `Append`, `CreateFileGDB`, `Rename`,
  `GetCount`, `CreateUniqueName` and `ValidateTableName` ran only against the in-memory stand-in,
  because arcpy would not initialise here. The stand-in proves this tool calls them in the right
  order and reads the count back. It does not prove their behaviour. The earlier release's measured
  notes below came from real runs by its author.
- **A run without `--apply` lists layers and counts only.** It does not use `--resume`, and it does
  not download or recount attachments. Its attachment check reads only the layer description.
- **In a script tool, set `APPLY = True` in CONFIG, or the run only reads and writes nothing.**
  A script tool passes no flag, and a dry run exits 0.
- **The dry run still needs the ArcGIS Pro Python, although it reads only.** On a plain Python it
  exits with "arcpy was not found" before it asks any question.
- **After download the tool trusts the per-batch check and does not recount files on disk.** The sum
  of the server's counts is not compared with the files found in the attachment folder.
- **`--self-check-live` was not run.** It needs the real arcpy and network. Its layer checks run in
  `--self-check` against the stub, and `main(["--self-check-live"])` runs against the stub and exits 0.
- **No counts, hidden cap.** When a server cannot answer `returnCountOnly`, a batch is checked only
  against the record cap the layer advertises. A server that cuts an answer short of that cap would
  pass. Lower `ATTACH_BATCH` to shrink the answers. There is no complete fix without counts.
- **The row count is read once, before the pull.** Rows deleted or added during the pull make the
  count disagree, or leave an id the server now holds unread; either way that strategy is refused
  and the next one tries. After every strategy the tool lists the server's ids again and compares
  them with the ids it read, which catches a row added after an id list or an OID range was read, and
  a row shifted across a page by `resultOffset`. The check needs an OID field. When the server will
  not list ids, the offset strategy is refused, and the other three pass with a warning, because they
  exist for servers that cannot list ids. A layer edited constantly can fail every strategy and need
  a retry.
- **Repeats need an objectId.** A feature with no value for the OID field is not tracked, so for
  such a layer the count is the only check. A layer whose OID values are not unique is refused.
- **The unit of `size` in the attachment list is not stated on its own page.** The Query
  Attachments page says its `size` filter is in bytes. A longer body is accepted and only a shorter
  one is refused.
- **Count form on every layer.** Counts are tried on every layer, but Esri documents the count form
  only for hosted services, so a layer that does not state support is asked and a 4xx answer is
  read as "cannot count".
- **A layer whose attachment pass fails keeps its new rows.** The rows were verified and swapped in
  before the attachment pass ran. Only the progress file is withheld, so `--resume` redoes the
  layer.
- **Edits during a pull make the run refuse.** The attachment ID list is read once and the rows
  once, at different moments. A feature added between them fails the id-count check for that layer,
  so a busy layer can fail repeatedly and need a retry.
- **A count of 0 cannot be told from a wrong answer of 0.** A layer with no old copy completes with
  a warning. One with an old copy that holds rows is refused until you delete the old copy.
- **Duplicate attachment ids** from a server are not detected: the list check compares the number
  of ids with the number of rows, not the number of distinct ids. ArcGIS does not return duplicates.
- **A body with neither `Content-Length` nor a listed `size` is not length-checked.** A connection
  closed early on such a body would be accepted.
- **Resume compares the filter and output SR as typed.** Two spellings of the same filter count as
  different, and the layer is downloaded again. Use one output geodatabase per service and per
  filter anyway, because names in a shared geodatabase can still collide.
- **Exit code 1 covers a refused layer, a dry run that would refuse a layer, a crash, a missing arcpy
  and a licence failure.** Only the
  message says which, so a scheduled job cannot tell "tool failed" from "findings exist" by the code
  alone. Argument errors exit 2.
- **The mutation counts above come from by-hand edits that are not shipped,** so they cannot be
  regenerated from this repository.
- **An ArcGIS error body on the count form is read as "cannot count", whatever its code.** The server
  answers HTTP 200 with `{"error": {"code": 500}}`, which the tool cannot tell from "not supported".
  Under the default the layer is refused. Under `--allow-uncounted-attachments` a transient server
  error therefore downgrades the rest of that layer to the cap rule, and only the warning at the end
  of the run says so.
- **The cap rule assumes `queryAttachments` stops at the layer's record cap.** Esri's Query
  Attachments page documents `maxRecordCount` only for `resultRecordCount` and says nothing of a
  truncated attachment list. The rule matters only under `--allow-uncounted-attachments`.
- **A layer with no OID field gets no end-of-pull id check and no repeat check.** The count is the
  only check, and the tool does not warn about it. Published feature layers nearly always have one.
- **A token typed into a wrong or extra argument slot is echoed in the usage error.** argparse prints
  an unrecognised argument back, so a stray seventh positional argument shows on the terminal.
- **A token typed at the `-` prompt shows on screen.** The tool reads it with `readline()`, not
  `getpass`. Pipe the token in instead.
- **The token goes wherever a redirect sends it.** If the server answers a POST with a 307 or 308,
  `requests` re-sends the body, token included, to the new host. Give fullpull the final https URL.
- **The attachment download puts the token in the URL.** It can appear in the server's own access
  logs. The tool masks the token in the records of the `urllib3.connectionpool` and
  `urllib3.util.retry` loggers, which is where the retry line (WARNING) and the request line (DEBUG)
  name the URL. A handler that reads the URL from somewhere else, such as a proxy or a packet
  capture, is not covered. Esri's header form was not checked.
- **A service URL with `http://` sends the token in clear text.** The tool neither refuses nor
  warns. Use https.
- **A non-JSON answer is quoted in the error.** The first 200 characters of the body go into the
  message with the token removed, and the ArcGIS error message and details are scrubbed of the token
  the same way. Other server text, such as layer names, reaches the console as received, control
  characters included, and an uncaught failure prints a Python traceback. A server answer that is a
  JSON list, or an error whose details are not strings, fails only that layer with a Python-style
  message.
- **A non-standard service URL** with no `/services/` segment makes the default geodatabase name from
  the whole URL, host and port included. Pass a name.
- **A layer with no extent** gets no `outSR` from its extent. It is then asked for geometry in the
  map's spatial reference unless you pass an out SR.
- **Paging while the data changes.** Esri's Query page does not say that `resultOffset` pages stay
  stable while the data is edited. A shifted page shows up as a repeat, a short count or, by the end-of-pull id check, an unread id, and refuses.
- **Attachment folders are not cleaned.** Files from an earlier run stay.
- **`f`-strings remain.** This file predates the house style of `%` formatting. It still holds
  f-strings in the code that dates from the original build, and the attachment code added for the
  short-read upgrade (`pull_attachments`, `fetch_attachment_infos`, `download_attachment`,
  `_report_attachments` and one self-check assertion) uses them too. It runs on Python 3.9 and later.
- **`--self-check`, not `--self-test`.** The flag keeps its original name. `--self-test` exits 2 as
  an unrecognised argument, so a mechanical check that calls it will not pass.
- **Name collisions are suffixed within one run only.** A second service pulled into the same
  explicit geodatabase name replaces same-named layers from the first.
- **A layer JSON that omits `hasAttachments`**, as a server before 10.1 does, fails the layer when
  `--attachments` is given, and the run exits 1 with a message that says the tool could not tell. A
  run without `--attachments` does not read the key.
- **Without `--attachments` the run does not warn that a layer has attachments.** The geodatabase
  then holds rows without their photos and nothing in the output says so.
- **Layer types other than Feature Layer and Table** are skipped as non-queryable with a warning and
  exit 0. The closing "Done" line does not count them. Whether an Annotation Layer should be pulled
  was not checked against Esri's layer-type documentation.
- **A `parentObjectId` that is a list** in an attachment answer raises a `TypeError` that fails only
  that layer. Real servers return an integer.
- **An attachment-free batch can count as checked against counts.** A server that ignores
  `returnCountOnly` answers an attachment-free batch with an empty group list, as Esri's SF311 sample
  does, so the `by_count` figure in the run message can overstate the evidence on such a stretch. The
  verdict holds, because the first batch with attachments flips the layer to "cannot count".
- **The attachment download follows redirects to any host the server names.** That serves blob
  storage. The token is not forwarded, because it sits only in the first query string, but a hostile
  server can make the client request any URL, an internal one included. Only point the tool at a
  server you trust.
- **Relied-on arcpy behaviours and their Esri pages** (read 2026-11-12; the pages say nothing
  beyond the quotes below, and none of it was run against real arcpy):
  [JSON To Features](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/conversion/json-to-features.html)
  converts an Esri JSON file to "a feature class", which the tool relies on for each page.
  [Append](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/append.html)
  with `NO_TEST` lets the input schema differ from the target, and unmapped fields stay null.
  [Rename](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/rename.html)
  fails if the new name exists, "even when the overwrite setting" is on, which is why the swap
  deletes the old class first.
  [Create File Geodatabase](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/create-file-gdb.html)
  creates a geodatabase in a folder that must exist. Its page does not say what happens when the
  geodatabase exists, so the tool calls it only when `arcpy.Exists` says no.
  [Get Count](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/get-count.html)
  returns a result read as `int(result[0])`.
  [CreateUniqueName](https://doc.esri.com/en/arcgis-pro/latest/arcpy/functions/createuniquename.html),
  [ValidateTableName](https://doc.esri.com/en/arcgis-pro/latest/arcpy/functions/validatetablename.html)
  and [Exists](https://doc.esri.com/en/arcgis-pro/latest/arcpy/functions/exists.html) return a
  unique name, a valid name and a Boolean. The Export Features page lists its input as a "Feature
  Layer" and does not say whether a REST layer URL is accepted.
- **Standalone tables rest on the in-memory stand-in only.** Esri's JSON To Features page documents
  an Output Feature Class and does not mention table input, and real arcpy would not initialise
  here, so a table pull was never run for real.
- **A file body longer than its listed size is kept.** The tool does not compare the response
  `Content-Type` with the listed `contentType`, so an error page returned with HTTP 200 for a very
  small attachment would be saved as that file.
- **The swap is delete then rename.** An error between the two loses the old copy of that layer. A
  lock fails the delete first, so this is rare.
- **Only `FULLPULL_TOKEN` and the `-` standard-input form keep the token out of the process list and
  the geoprocessing history.** The argument and CONFIG forms exist for script-tool compatibility.
  Giving `-` with an empty standard input yields no token and ignores `FULLPULL_TOKEN`, and the run
  then fails at the server with no hint that standard input was empty.
- **`--self-check-live` pulls the whole sample layer, several times, from Esri's public server by
  default.** Its layer checks page the layer at three page sizes, run a naive stride and then call
  `download_layer`. Against a stub shaped like SF311 layer 0 (9,705 rows, cap 1,000) that was 37
  requests, and at the default delay of 0.5 seconds the pacing alone takes about 18 seconds. Point
  `SELF_CHECK_URL` at your own server to avoid sending that traffic to someone else's.
- **The numbers under "Notes from the original build"** are from one earlier run on a server that is
  not named here. The 254 s against 1.4 s figure in the file's docstring has the same status. They
  were not measured again.
- **Related tables, coded value domains, date time zones, parallel fetching** are not handled.
  Conversion, not HTTP, is the bottleneck and `arcpy` is not thread safe.

## Notes from the original build

Measured once against a live ArcGIS Server 11.3 in the original build, not repeated by the
self-check or by this round:

- `maxRecordCount` is a cap, not a promise. One layer advertised 250,000 and returned HTTP 500 above
  roughly 25,000.
- `objectIdField` can be absent from MapServer layer JSON. Read the OID field from `fields[]` where
  `type` is `esriFieldTypeOID`.
- Buffering pages in memory does not scale. Holding 47k polylines before writing peaked at 2.8 GB.
  Converting page by page held near 0.55 GB.
- On Esri's SF311 sample layer, 9,705 rows existed and 9,703 answered a full-extent envelope query.
  Two had no geometry.

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
