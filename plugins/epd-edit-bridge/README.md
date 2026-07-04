# EPD Edit Bridge — a Lua round-trip between Lightroom Classic and Claude

Two commands. No server, no API key, no ports. Everything stays on your machine,
and nothing changes your catalog until you explicitly apply it.

## What it does

**Export** reads the develop settings + key EXIF (WB, ISO, shutter, lens) of your
selected photos and writes a JSON file to your Desktop. You send that to Claude, so
it can tune against your *real numbers* instead of guessing from rendered JPEGs.

**Apply** takes a `.lua` settings file Claude hands back and writes those settings
into the selected photos — with a confirmation step first.

```
  LrC (select photos) --Export--> epd_develop_report.json --> Claude
  Claude --> settings.lua --Apply--> LrC (settings written, as a history step)
```

## Install (one time)

1. Unzip so you have the folder `EPD Edit Bridge.lrplugin`.
2. Lightroom Classic → **File → Plug-in Manager → Add** → select that folder → **Done**.
3. The two commands now live under **Library menu → Plug-in Extras**:
   - *EPD: Export Develop Settings (selected) to Desktop*
   - *EPD: Apply Settings from Claude (.lua file)...*

(If you edit the `.lua` source later, click **Reload Plug-in** in Plug-in Manager.)

## The loop, step by step

1. In the **Library** module, select the photos for one scene (e.g. your overcast
   outdoor frames).
2. Run **Export**. It drops `epd_develop_report.json` on your Desktop.
3. Upload that JSON to Claude. Claude reads your actual settings and returns a
   tuned `settings.lua`.
4. Select the same photos, run **Apply**, pick the file, confirm. Done — it lands as
   a single "Claude edit" history step you can undo.

## The settings file format

```lua
return {
  global = {                      -- applied to every selected photo
    Contrast2012 = 18, Vibrance = 24, ...
  },
  byFile = {                      -- optional, per-photo, matched by filename (no extension)
    ["1J4A3264"] = { Temperature = 4800, Highlights2012 = -60, Exposure2012 = -0.20 },
  },
}
```

- Keys are exact Lightroom develop-setting names — identical to what the export
  report shows, so you can copy them straight across.
- **White balance:** to pin it, set `Temperature` + `Tint` **and** `WhiteBalance = "Custom"`.
  Otherwise leave WB out and set it per frame by hand (recommended).
- See `example_apply.lua` for a working sample (the Overcast look + two per-file fixes).

## What this is and isn't

- It **is** a clean, fully-local way for Claude to read your real edits and write
  precise settings back — the tight feedback loop the .xmp presets couldn't give us.
- It **is not** an autonomous editor. Claude still makes the decisions here in chat;
  the plugin just moves data in and out. That's deliberate — you see and confirm
  everything.
- Want it to run unattended (Claude reads/writes the catalog live, no copy-paste)?
  That's the MCP upgrade path — the same Lua SDK, plus a local bridge server. This
  two-script version is the no-friction starting point; we can graduate to the MCP
  once the workflow proves out.

## Queue processor (v1.1)

The third menu item — *EPD: Start/Stop ShopSteward Queue Processor* — is the
Lightroom half of the ShopSteward editing module. It polls a bridge folder
for job files written by `shopsteward ingest`, and for each job: imports
missing photos, applies the inlined develop settings (one undoable history
step per photo), adds them to a named collection, and — for mass jobs —
exports sRGB JPEGs renamed via the job's naming template.

- **Per-session authorization.** Starting the processor always shows a
  confirmation dialog naming the watched folder and the exact powers it
  will use (import, apply, collections, export). Decline and nothing runs.
  Authorization does not persist across Lightroom sessions.
- **Bridge folder layout** (shared contract with
  `src/shopsteward/core/folderproto.py`):

  ```
  <bridge>/jobs/             inbox — ShopSteward writes edit_<uuid>.json
  <bridge>/jobs/done/        processed job + <name>.result.json
  <bridge>/jobs/failed/      failed/malformed job + <name>.result.json
  <bridge>/jobs/quarantine/  used by the Python reader only
  ```

  Writers use `<name>.part` + rename so consumers never see partial files.
- Malformed job files land in `failed/` with an error result — the loop
  never crashes on bad input.
- Manual test plan: see `TESTING.md` (canned jobs via `make_test_jobs.py`).

## Safety notes

- Export is strictly read-only.
- Apply writes inside a single catalog write-access step with a confirmation prompt,
  and shows up as one undoable history state per photo.
- Back up your catalog before any plugin touches it — standard practice, not a
  specific worry about this one.
