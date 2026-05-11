# grib_nomad

A friendly downloader for NOAA weather and ocean forecasts, designed for sailing weather routing. It pulls GRIB2 data from [NOAA NOMADS](https://nomads.ncep.noaa.gov) (for wind, waves, pressure, temperature) and from the [NOAA Open Data archive on AWS S3](https://registry.opendata.aws/noaa-ofs/) (for Gulf of Maine surface currents), then stitches the results into a single GRIB2 file that you can drop into OpenCPN, qtVlm, XyGrib, or any other routing tool.

What makes it different from clicking around NOMADS' web form:

- **Pick a region**, not a bounding box. Built-in presets cover the US East Coast, Gulf Stream, Caribbean, and Gulf of Maine. Custom bounding boxes are also accepted.
- **Pick a model per data category** — for example, HRRR for short-range wind, GFS for long-range wind, GFS Wave for waves, GoMOFS for tidal currents.
- **Tier multiple models within one category** — high-resolution HRRR for the first 48 h, then coarser GFS through 168 h. The output filename and a sidecar JSON manifest record exactly which model produced each timestep, so you can tell at a glance what you're looking at.
- **Save the whole download as a named recipe** and re-run it with one command — useful before every passage so you always get a fresh forecast on the same area with the same models.
- **Local caching**. NOMADS data is rate-limited; if you ran the same recipe an hour ago, the unchanged forecast hours come from your disk, not the server.
- **Output verification**. `grib-nomad inspect <file.grb2>` decodes the GRIB2 parameter codes and tells you which routing programs will recognize them.

---

## Installation

The tool depends on several native libraries (`eccodes` for GRIB encoding, `netCDF4` and `h5netcdf` for the ocean-current path, `scipy` for regridding). The cleanest way to get all of those on macOS or Linux is via Miniconda. The instructions below assume no prior Python experience.

### Step 1 — Install Miniconda

Miniconda is a small Python distribution that comes with its own package manager (`conda`) which can install both Python libraries and native ones.

- Go to <https://docs.conda.io/projects/miniconda/en/latest/> and download the installer for your operating system (macOS Intel, macOS Apple Silicon, or Linux).
- Run the installer and accept the defaults. When it asks if it should initialize Miniconda in your shell, say yes.
- Open a **new** terminal window and confirm `conda` is on your `PATH`:

  ```bash
  conda --version
  ```

  You should see something like `conda 24.1.2`.

### Step 2 — Clone this repository

```bash
git clone <repo-url> grib_nomad
cd grib_nomad
```

(Replace `<repo-url>` with the URL you cloned from. If you downloaded a zip instead, just `cd` into the unzipped folder.)

### Step 3 — Create the conda environment

This creates an isolated Python environment named `grib_nomad` and installs the native dependencies into it. The first time around this can take a few minutes.

```bash
conda create -n grib_nomad -c conda-forge \
    python=3.11 \
    eccodes pygrib \
    xarray netCDF4 h5netcdf s3fs scipy \
    numpy shapely pyshp \
    zarr=2 kerchunk \
    -y
conda activate grib_nomad
```

What this installs and why:
- `eccodes`, `pygrib` — read and write GRIB2 files
- `xarray`, `netCDF4`, `h5netcdf`, `s3fs` — open the Gulf of Maine NetCDF files on S3
- `scipy`, `numpy`, `shapely`, `pyshp` — regridding and coastline masking
- `zarr=2`, `kerchunk` — fast chunked S3 reads (zarr is pinned to 2.x because 3.x has a known incompatibility with kerchunk)

### Step 4 — Install grib_nomad itself

From the project root (with the `grib_nomad` conda env still activated):

```bash
pip install -e ".[gomofs,inspect]"
```

`-e` means "editable install" — any code changes you pull from git take effect without re-installing. `[gomofs,inspect]` pulls in the optional dependencies needed for currents and for `grib-nomad inspect`.

### Step 5 — Verify

```bash
grib-nomad --version
grib-nomad regions list
grib-nomad models list
```

The first command should print a version number; the next two should print tables of the built-in regions and forecast models. If you get a "command not found" error, make sure the `grib_nomad` conda environment is active (`conda activate grib_nomad`).

### Re-activating later

Whenever you open a new terminal to use the tool, run:

```bash
conda activate grib_nomad
```

---

## Quick start

The tool has two ways to drive a download: a **CLI** (one command per download) and a **TUI** (an interactive menu, run by typing `grib-nomad` with no arguments). The CLI is what you'll use day-to-day; the TUI is useful for browsing what's available.

### Example 1 — A 7-day wind forecast for the Gulf Stream

```bash
grib-nomad download \
    --region gulf-stream \
    --duration 7d \
    --tier wind:gfs_0p25
```

This downloads NOAA's GFS model (0.25°) for the next 7 days in the Gulf Stream region. Output goes to `./downloads/` by default — a `.grb2` file plus a `.manifest.json` describing it.

### Example 2 — Tiered wind: HRRR near-term, GFS far-term

HRRR is high-resolution (3 km) but only goes 48 h out. GFS is coarser (~25 km) but goes 384 h out. Use HRRR for the first 48 h and GFS for the rest:

```bash
grib-nomad download \
    --region eastcoast-wide \
    --duration 7d \
    --tier wind:hrrr_conus_sfc:48 \
    --tier wind:gfs_0p25
```

The `:48` after `hrrr_conus_sfc` means "this tier covers up to 48 h from the start"; the next `--tier` with no `:N` covers everything after that.

### Example 3 — A full weather + ocean recipe around Woods Hole

Wind, waves, swell, surface currents — every category that matters for routing:

```bash
grib-nomad download \
    --region woods-hole \
    --duration 3d \
    --tier wind:hrrr_conus_hourly:18 \
    --tier wind:hrrr_conus_sfc:48 \
    --tier wind:gfs_0p25 \
    --tier wave:nwps_box_cg1 \
    --tier swell:nwps_box_cg1 \
    --tier current:gomofs_currents \
    --save-as woods-hole-routing
```

For *coastal* routes (Vineyard/Nantucket Sound, Long Island Sound, Chesapeake, etc.) use `nwps_box_cg1` for waves — it's ~1.5 km resolution and resolves the nearshore where `gfs_wave_atlantic_0p16` (~17 km) treats everything as land. NWPS only covers 144 h and only runs at 00z/12z, so for >6-day forecasts or non-Eastern-Region areas use GFS Wave instead. NWPS does NOT decompose wind-wave vs. swell — request `swell` separately if you want that field.

`--save-as` records this exact recipe so you can re-run it as a one-liner next time:

```bash
grib-nomad recipe run woods-hole-routing
```

### Example 4 — Inspect what you got

```bash
grib-nomad inspect downloads/<your-file>.grb2
```

This decodes the GRIB2 parameter codes (the WMO discipline/category/number triples) inside the file and tells you which variables are present and which routing programs recognize them.

---

## How tiers work

Inside one category (say, `wind`), tiers are sequential time slices that run from the recipe's `--start` (default: now) for `--duration` hours total:

- The first tier covers `[start, start + tier1.until_hours]`.
- The second tier picks up where the first left off and covers up to `start + tier2.until_hours`.
- A tier with no `until_hours` covers everything left over.

So `--tier wind:hrrr_conus_hourly:18 --tier wind:hrrr_conus_sfc:48 --tier wind:gfs_0p25` over `--duration 7d` means:

- HRRR hourly (latest cycle, fresh data, only goes 18 h out) covers hour 0–18.
- HRRR extended (00/06/12/18 cycles, goes 48 h out) covers hour 18–48.
- GFS covers hour 48 through end of duration (168 h).

The runner picks the freshest cycle of each model that's *ready* (NOAA publishes with some latency) and that can *reach* the slice you asked for. You don't have to think about cycle times or forecast hours.

---

## CLI reference

```
grib-nomad regions list                 # built-in region names + bounding boxes
grib-nomad models  list                 # bundled forecast models
grib-nomad models  show <model_id>      # details for one model (cycles, fhour range, categories)
grib-nomad download [options]           # one-shot download (add `--save-as NAME` to keep it as a recipe)
grib-nomad recipe   list                # list saved recipes
grib-nomad recipe   show <name>         # print one recipe as YAML
grib-nomad recipe   run  <name>         # re-run a saved recipe (optional --start / --duration override)
grib-nomad recipe   delete <name>
grib-nomad inspect  <file.grb2>         # decode parameter codes; check routing-app compatibility
```

Every command supports `--help`.

### `download` options at a glance

| Option | What it does |
|---|---|
| `--region NAME` | Use a built-in region. Mutually exclusive with `--bbox`. |
| `--bbox LATMIN,LATMAX,LONMIN,LONMAX` | Custom bounding box. Longitudes in -180..180. |
| `--tier CAT:MODEL[:HOURS[:STEP]]` | Add a tier (repeatable). See *How tiers work* above. |
| `--duration` | Total coverage. `7d`, `168h`, `24`, etc. Default: `7d`. |
| `--start` | Window start (ISO datetime). Default: now. Naive times = local TZ. |
| `--out-dir` | Where to write `.grb2` and `.manifest.json`. Default: `./downloads`. |
| `--save-as NAME` | Also save this run as a named recipe for later. |
| `--workers / -j` | Concurrent NOMADS fetches. Default 8 is sensible. GoMOFS uses its own higher floor. |

---

## Bundled models

| Model id | What it provides | Source | Reach | Update cadence |
|---|---|---|---|---|
| `gfs_0p25` | Global wind, pressure, temp, precip, cloud | NOMADS | 0..384 h | 4× daily |
| `gfs_wave_atlantic_0p16` | Atlantic regional waves (combined, wind-wave, swell) | NOMADS | 0..384 h | 4× daily |
| `gfs_wave_global_0p25` | Global waves | NOMADS | 0..384 h | 4× daily |
| `hrrr_conus_hourly` | High-res CONUS surface (wind, gust, precip), every hour | NOMADS | 0..18 h | hourly |
| `hrrr_conus_sfc` | Same fields, extended runs only | NOMADS | 0..48 h | 4× daily |
| `nam_conusnest` | 3 km CONUS nest (wind, surface) | NOMADS | 0..60 h | 4× daily |
| `nam_12km` | 12 km North America (wind, surface) | NOMADS | 0..84 h | 4× daily |
| `nwps_box_cg1` | Coastal SWAN waves for BOX WFO (~1.5 km — resolves Vineyard/Nantucket Sound) | NOMADS | 0..144 h | 2× daily (00/12z) |
| `gomofs_currents` | Hourly surface currents (Gulf of Maine), with tides | S3 (no auth) | 0..72 h | 4× daily |

`grib-nomad models show <id>` prints the per-model categories and which variables come with each.

## Bundled regions

`grib-nomad regions list` for the full table. Routing-oriented presets cover:

`gulf-stream`, `bermuda-triangle`, `caribbean`, `bahamas`, `new-england-offshore`, `florida-straits`, `eastcoast-wide`, `north-atlantic`, `gulf-of-maine`, `gulf-of-maine-wide`, `woods-hole`.

Any of those names works with `--region`. For an ad-hoc area, use `--bbox 35,45,-75,-65` instead.

---

## Output: GRIB2 + manifest

For each successful run you get two files in `--out-dir`:

```
<recipe_name>_<start_iso>_<categories>.grb2
<recipe_name>_<start_iso>_<categories>.manifest.json
```

The `.grb2` is a plain concatenated GRIB2 — every routing program reads it. The `.manifest.json` records, for every variable and every valid time, which model and which init cycle the data came from. This is how you can tell that "wind at hour 51" was GFS, not HRRR — your routing software can't show you that, but the manifest can.

---

## Caching

To stay polite with NOAA's servers (and to make repeated runs essentially free), forecast data is cached on disk under your OS's standard user cache directory:

- macOS: `~/Library/Caches/grib_nomad/`
- Linux: `~/.cache/grib_nomad/`

Layout:

- `nomads/<model_id>/<YYYYMMDDHH>/<category>_<bbox>_fNNN.grb2` — per-forecast-hour cache, keyed on model + cycle + category + bounding box.
- `nwps/<model_id>/<YYYYMMDDHH>/<category>_<bbox>_bundle.grb2` plus per-fhour split files. NWPS publishes one bundle per cycle containing every fhour; the source downloads the bundle once and splits it locally so the rest of the pipeline sees the same per-fhour layout as NOMADS.
- `gomofs/<model_id>/<YYYYMMDDHH>/...` — per-cycle static fields plus per-fhour subset NetCDF files.

A re-run of the same recipe on the same model cycle does no network I/O for the parts already cached. If you want to force fresh fetches, delete the relevant subdirectory.

Stale cycles are pruned automatically over time, so the cache does not grow without bound.

---

## NOAA rate limits

NOMADS is fronted by Akamai and applies an aggressive per-IP rate limit (~120 hits per minute). The tool targets ~100/min internally via a token bucket, with a small concurrency cap, so individual recipes run politely. If you trip the limit anyway (e.g. by running several recipes in quick succession from the same network), `grib-nomad` exits cleanly with a message — wait 5–15 minutes and retry.

GoMOFS data on S3 has no equivalent limit; that path runs with higher concurrency.

Background on the NOMADS limit: <https://luckgrib.com/blog/2021/04/19/throttling.html>.

---

## Recipes on disk

Saved recipes live under your OS's standard user config directory:

- macOS: `~/Library/Application Support/grib_nomad/recipes.yaml`
- Linux: `~/.config/grib_nomad/recipes.yaml`

The file is plain YAML — safe to edit by hand if you want to tweak a saved recipe.

---

## Roadmap

- Additional NWPS WFOs (currently only BOX/Boston). Adding e.g. OKX (Long Island Sound), PHI (Delaware Bay), LWX (Chesapeake) is just a YAML entry — see `src/grib_nomad/data/nwps_models.yaml`.
- ECMWF Open Data source (IFS 0.25°, free, no auth)
- Map-based GUI driving the same core API

---

## License

MIT.
