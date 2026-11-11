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

The table was produced by running that loop against the stub of the self-check, not copied from a
service.

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
- Standalone tables as well as feature layers. Group and non-queryable layers skipped and logged.
- Name collisions suffixed rather than overwritten.
- Four paging strategies, cheapest first. A strategy that raises, or that finishes with the wrong
  count, hands the layer to the next one. Only an exhausted list fails the layer.
- `--resume` skips layers an interrupted run finished. A layer finished without attachments is not
  skipped by a run that asks for them.
- Retries with backoff on 429 and 5xx, request timeouts, token support. The token travels in the
  POST body of every query, and in the URL only for the GET of an attachment, where it is scrubbed
  from any error message.
- An optional delay between requests (`--delay` or `DELAY`), for public servers behind a web
  application firewall that bans clients for their request rate. On by default at 0.5 seconds;
  `--delay 0` turns it off for your own server.
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
   short list would hide whole features from the pass. Esri documents a limit of 1 million IDs in
   that response, so a larger layer would refuse here the same way, not be partly pulled.
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
  `supportsQueryAttachmentsCountOnly` property under `advancedQueryCapabilities` is true. The page
  does not say whether a feature with no attachments appears in the list.
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
  `outStatistics` takes `MIN` and `MAX`. The page shows `"exceededTransferLimit" : true` in a
  response and `false` only in an example, so fullpull does not rely on `false` to mark a last page.
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

Set the CONFIG block at the top of the file and run with no arguments:

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
the argument, then `FULLPULL_TOKEN`, then `TOKEN`.

To use it as an ArcGIS script tool, add it with the six positional parameters in that order. A script
tool passes parameters as positional arguments, so there is no separate code path. Flags are for the
shell.

A run writes by default. That is the earlier design and it is kept, because a script tool has no flag
to hold a gate. What it protects is the existing copy: a layer is replaced only after its staging
class passed the verdict. `--attachments` is opt-in and overwrites files of the same name. It does not
delete files left by an earlier run.

## Resume

Every completed layer is recorded in `<geodatabase>.progress.json` next to the geodatabase, written
atomically after each layer, with whether its attachments were pulled. A run that fails part way
leaves that file behind. `--resume` reads it and skips the layers it names, provided the feature class
is still there and, for a run with `--attachments`, the layer was recorded with its attachments. A
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
PASS  a server that returns no count is refused, not trusted  <-- pinned defect
...
PASS  a server that counts 0 while the old copy holds rows is refused, not kept stale on exit 0  <-- pinned defect
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
PASS  the token reaches no message, warning, error, progress file or output file  <-- pinned defect
...
PASS  a resume that wants attachments does not skip a layer finished without them  <-- pinned defect
...
PASS  a prefix of --attachments is refused, not expanded  <-- pinned defect
...
PASS  an arcpy that imports but fails its licence is not reported as missing  <-- pinned defect
...
--------------------------------------------------------------------
194 assertions, 0 failed
```

The full run prints all 194 assertions. Each `...` line is where this block is cut.

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
| Windows | 3.13.2 (`C:\Python313`) | 194 |
| Windows, ArcGIS conda env | 3.13.7 | 194 |
| Windows, venv | 3.9.25 | 194 |
| Windows, venv | 3.12.10 | 194 |
| Linux | 3.12.3 | 194 |

The counts are equal because the self-check never calls the real arcpy. On the Windows ArcGIS
environment `import arcpy` raised `RuntimeError: The Product License has not been initialized` during
this build. The self-check runs the same either way, and one assertion pins the message a real pull
gives on such a host.

### Coverage

```
python -m coverage run --branch fullpull.py --self-check
python -m coverage report -m --include=fullpull.py
```

reports 1,334 statements with none missed and 370 branches with none partial, 100 percent, on the
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
pacing, and the licence text dropped.

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
| Resume, with and without attachments | `download_service` against the stub |
| Attachment batches, splitting, cap rule, file checks | stub with each fault in turn |
| `--attachments`, `--resume`, prefix refusal, CONFIG fallback | `main()` against the stub |
| One real server answers `returnCountOnly` with full lists | three requests to Esri's SF311 sample layer, 2026-11-11 |
| An uncounted attachment server, a 5xx on the count form, an id that is not an integer | stub with each fault in turn, `main()` with and without the opt-in |
| Token from `FULLPULL_TOKEN` or standard input | `main()` against a stub that demands a token |
| An empty server count over an old copy with rows is refused | stub layer with 0 rows over a seeded class |
| Requests are paced | assertions on the gap between request starts, and no sleep at 0 |

## Limits

- **Real arcpy was not run in this round.** `JSONToFeatures`, `Append`, `CreateFileGDB`, `Rename`,
  `GetCount`, `CreateUniqueName` and `ValidateTableName` ran only against the in-memory stand-in,
  because arcpy would not initialise here. The stand-in proves this tool calls them in the right
  order and reads the count back. It does not prove their behaviour. The earlier release's measured
  notes below came from real runs by its author.
- **There is no `--apply` flag.** A run always writes the geodatabase, and only the staging swap
  protects the previous copy of a layer. This is the earlier arcpy script-tool design, kept.
- **`--self-check-live` was not run.** It needs the real arcpy and network. Its layer checks run in
  `--self-check` against the stub, and `main(["--self-check-live"])` runs against the stub and exits 0.
- **No counts, hidden cap.** When a server cannot answer `returnCountOnly`, a batch is checked only
  against the record cap the layer advertises. A server that cuts an answer short of that cap would
  pass. Lower `ATTACH_BATCH` to shrink the answers. There is no complete fix without counts.
- **The row count is read once, before the pull.** Rows added or deleted during the pull make the
  count disagree, so the layer is refused and retried by the next strategy. It is never accepted.
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
- **Resume does not record the filter, spatial reference or service.** A layer finished with one
  `--where` is skipped by a later resume with another. Use one output geodatabase per service and
  per filter.
- **Exit code 1 covers both a refused layer and a crash.** The message says which. Argument errors
  exit 2.
- **The token goes wherever a redirect sends it.** If the server answers a POST with a 307 or 308,
  `requests` re-sends the body, token included, to the new host. Give fullpull the final https URL.
- **The attachment download puts the token in the URL.** It can appear in the server's own access
  logs, and in the log line of urllib3 at DEBUG level, so do not enable that logging on a run that
  uses a token. Esri's header form was not checked.
- **A service URL with `http://` sends the token in clear text.** The tool neither refuses nor
  warns. Use https.
- **A non-JSON answer is quoted in the error.** The first 200 characters of the body go into the
  message. A proxy that echoes the request would echo the token there. Scrubbing covers only the
  attachment GET.
- **A non-standard service URL** with no `/services/` segment makes the default geodatabase name from
  the whole URL, host and port included. Pass a name.
- **A layer with no extent** gets no `outSR` from its extent. It is then asked for geometry in the
  map's spatial reference unless you pass an out SR.
- **Paging while the data changes.** Esri's Query page does not say that `resultOffset` pages stay
  stable while the data is edited. A shifted page shows up as a repeat or a short count and refuses.
- **Attachment folders are not cleaned.** Files from an earlier run stay.
- **`f`-strings remain.** This file predates the house style of `%` formatting and still holds
  f-strings in the code that dates from the original build. It runs on Python 3.9 and later.
- **`--self-check`, not `--self-test`.** The flag keeps its original name.
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
