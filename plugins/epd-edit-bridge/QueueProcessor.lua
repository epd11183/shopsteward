--[[
  QueueProcessor.lua — the ShopSteward queue processor (v1.1).
  Menu toggle: first invocation asks for authorization and starts a
  background sweep of the bridge jobs folder; second invocation stops it.

  Folder protocol (mirrors src/shopsteward/core/folderproto.py — the
  protocol root for edit jobs is <bridge>/jobs/):

    <bridge>/jobs/             inbox: Python writes edit_<uuid>.json (atomic
                               '<name>.part' then rename; we skip *.part)
    <bridge>/jobs/done/        processed job + <name>.result.json
    <bridge>/jobs/failed/      failed/malformed job + <name>.result.json
    <bridge>/jobs/quarantine/  used by the Python reader only

  Per job: resolve photos in the catalog (importing missing files when
  import_missing is set), apply the inlined develop settings — one undoable
  history step per photo — add to the named collection, and for mass jobs
  export sRGB JPEGs renamed via JobFile.render_name. Malformed or failing
  jobs land in failed/ with an error result; the loop never crashes.
]]

local LrApplication   = import 'LrApplication'
local LrTasks         = import 'LrTasks'
local LrDialogs       = import 'LrDialogs'
local LrFileUtils     = import 'LrFileUtils'
local LrPathUtils     = import 'LrPathUtils'
local LrPrefs         = import 'LrPrefs'
local LrProgressScope = import 'LrProgressScope'
local LrExportSession = import 'LrExportSession'

local JsonCodec = require 'JsonCodec'
local JobFile   = require 'JobFile'

local prefs = LrPrefs.prefsForPlugin()

local RUNNING_FLAG = 'epdShopStewardQueueRunning'  -- plugin-global, resets on reload

-- Job files use forward slashes; Lightroom wants native separators.
local function toNative(path)
    if WIN_ENV then
        return (path:gsub('/', '\\'))
    end
    return path
end

local function child(dir, name)
    return LrPathUtils.child(dir, name)
end

-- Write the result JSON atomically: '<name>.part' then rename.
local function writeResult(outcomeDir, jobLeaf, resultTbl)
    local stem = jobLeaf:gsub('%.json$', '')
    local finalPath = child(outcomeDir, stem .. '.result.json')
    local partPath = finalPath .. '.part'
    local f, ioErr = io.open(partPath, 'w')
    if not f then
        error('could not write result file: ' .. tostring(ioErr), 0)
    end
    f:write(JsonCodec.encode(resultTbl))
    f:close()
    if LrFileUtils.exists(finalPath) then os.remove(finalPath) end
    os.rename(partPath, finalPath)
end

-- Move the job file into done/ or failed/ and drop the result beside it
-- (same order as folderproto.complete: move first, then write result).
local function finishJob(jobsDir, jobPath, outcome, resultTbl)
    local outcomeDir = child(jobsDir, outcome)
    LrFileUtils.createAllDirectories(outcomeDir)
    local leaf = LrPathUtils.leafName(jobPath)
    local dest = child(outcomeDir, leaf)
    if LrFileUtils.exists(dest) then os.remove(dest) end  -- re-run after crash
    os.rename(jobPath, dest)
    writeResult(outcomeDir, leaf, resultTbl)
end

-- Run one valid job. Returns a completed result table; raises on failure
-- (the caller's pcall turns that into a failed/ outcome).
local function processJob(catalog, job)
    local skipped = {}
    local resolvedByIndex = {}  -- photo index -> LrPhoto
    local toImport = {}         -- { {index=, path=, base_name=}, ... }

    for i, p in ipairs(job.photos) do
        local nativePath = toNative(p.raw_path)
        local photo = catalog:findPhotoByPath(nativePath)
        if photo then
            resolvedByIndex[i] = photo
        elseif job.import_missing and LrFileUtils.exists(nativePath) == 'file' then
            toImport[#toImport + 1] = { index = i, path = nativePath, base_name = p.base_name }
        else
            skipped[#skipped + 1] = { base_name = p.base_name, reason = 'not_in_catalog' }
        end
    end

    -- One write-access block: import + apply + collection. Each
    -- applyDevelopSettings call is its own named, undoable history step.
    catalog:withWriteAccessDo('ShopSteward: apply ' .. (job.preset_family or 'settings'),
        function()
            for _, item in ipairs(toImport) do
                local ok, photoOrErr = pcall(function() return catalog:addPhoto(item.path) end)
                if ok and photoOrErr then
                    resolvedByIndex[item.index] = photoOrErr
                else
                    skipped[#skipped + 1] = { base_name = item.base_name, reason = 'not_in_catalog' }
                end
            end

            local lrPhotos = {}
            for i = 1, #job.photos do
                local photo = resolvedByIndex[i]
                if photo then
                    photo:applyDevelopSettings(job.develop_settings, 'ShopSteward preset')
                    lrPhotos[#lrPhotos + 1] = photo
                end
            end

            local collection = catalog:createCollection(job.collection, nil, true)
            if collection and #lrPhotos > 0 then
                collection:addPhotos(lrPhotos)
            end
        end,
        { timeout = 60 })

    -- Ordered list of resolved photos (job order drives {seq} numbering).
    local ordered = {}
    for i, p in ipairs(job.photos) do
        if resolvedByIndex[i] then
            ordered[#ordered + 1] = { photo = resolvedByIndex[i], base_name = p.base_name }
        end
    end

    local exported = {}
    if job.export and #ordered > 0 then
        local outDir = toNative(job.export.output_folder)
        LrFileUtils.createAllDirectories(outDir)

        local lrPhotos = {}
        for _, item in ipairs(ordered) do lrPhotos[#lrPhotos + 1] = item.photo end

        local session = LrExportSession({
            photosToExport = lrPhotos,
            exportSettings = {
                LR_format = 'JPEG',
                LR_jpeg_quality = (job.export.jpeg_quality or 92) / 100,
                LR_export_colorSpace = 'sRGB',
                LR_export_destinationType = 'specificFolder',
                LR_export_destinationPathPrefix = outDir,
                LR_export_useSubfolder = false,
                LR_renamingTokensOn = false,
                LR_size_doConstrain = false,
            },
        })

        local date = os.date('!%Y-%m-%d')
        local seq = 0
        for _, rendition in session:renditions() do
            local success, pathOrMsg = rendition:waitForRender()
            seq = seq + 1
            if not success then
                error('export_error: ' .. tostring(pathOrMsg), 0)
            end
            local item = ordered[seq]
            local base = item and item.base_name
                or LrPathUtils.removeExtension(LrPathUtils.leafName(pathOrMsg))
            local rendered = JobFile.render_name(job.export.naming_template, {
                event = job.export.event,
                date = date,
                seq = seq,
                base = base,
            })
            local finalPath = child(outDir, rendered .. '.jpg')
            if pathOrMsg ~= finalPath then
                local n = 2
                while LrFileUtils.exists(finalPath) do  -- collision-avoid: -2, -3, ...
                    finalPath = child(outDir, rendered .. '-' .. n .. '.jpg')
                    n = n + 1
                end
                os.rename(pathOrMsg, finalPath)
            end
            exported[#exported + 1] = LrPathUtils.leafName(finalPath)
        end
    end

    return JobFile.result(job.job_id, 'completed', #ordered, skipped, exported, nil)
end

-- Parse, validate, process one job file; route to done/ or failed/.
-- Never raises: every failure path lands the file in failed/ with a result.
local function handleJobFile(catalog, jobsDir, path, leaf)
    local content = LrFileUtils.readFile(path)
    local okDecode, jobOrErr
    if content then
        okDecode, jobOrErr = pcall(JsonCodec.decode, content)
    else
        okDecode, jobOrErr = false, 'could not read job file'
    end

    local valid, validErr = false, nil
    if okDecode then
        valid, validErr = JobFile.validate(jobOrErr)
    else
        validErr = tostring(jobOrErr)
    end

    if not valid then
        local jobId = leaf:gsub('%.json$', '')
        if okDecode and type(jobOrErr) == 'table' and type(jobOrErr.job_id) == 'string' then
            jobId = jobOrErr.job_id
        end
        local result = JobFile.result(jobId, 'failed', 0, {}, {},
            { code = 'malformed', message = tostring(validErr) })
        result.file_name = leaf
        pcall(finishJob, jobsDir, path, 'failed', result)
        return
    end

    local job = jobOrErr
    local progress = LrProgressScope({
        title = 'ShopSteward: ' .. (job.preset_family or job.job_id),
    })
    local okRun, resultOrErr = pcall(processJob, catalog, job)
    progress:done()

    if okRun then
        pcall(finishJob, jobsDir, path, 'done', resultOrErr)
    else
        local msg = tostring(resultOrErr)
        local code = msg:find('export_error', 1, true) and 'export_error' or 'apply_error'
        local result = JobFile.result(job.job_id, 'failed', 0, {}, {},
            { code = code, message = msg })
        result.file_name = leaf
        pcall(finishJob, jobsDir, path, 'failed', result)
    end
end

local function chooseBridgeRoot()
    local chosen = LrDialogs.runOpenPanel({
        title = 'Choose the ShopSteward bridge folder (the one that contains jobs/)',
        canChooseFiles = false,
        canChooseDirectories = true,
        allowsMultipleSelection = false,
    })
    if chosen and chosen[1] then return chosen[1] end
    return nil
end

LrTasks.startAsyncTask(function()
    -- Toggle: if already running, this invocation stops it.
    if rawget(_G, RUNNING_FLAG) then
        rawset(_G, RUNNING_FLAG, false)
        LrDialogs.message('EPD Edit Bridge',
            'ShopSteward queue processor will stop after the current sweep.')
        return
    end

    local root = prefs.bridgeRoot
    if type(root) ~= 'string' or not LrFileUtils.exists(root) then
        root = chooseBridgeRoot()
        if not root then return end
        prefs.bridgeRoot = root
    end
    local jobsDir = child(root, 'jobs')

    -- Per-session authorization: declined means the task never starts.
    local answer = LrDialogs.confirm('EPD Edit Bridge — ShopSteward queue processor',
        'Watch this folder for ShopSteward edit jobs?\n\n' .. jobsDir ..
        '\n\nWhile running, jobs in that folder may:\n' ..
        '  - import photos into this catalog\n' ..
        '  - apply develop settings (one undoable history step per photo)\n' ..
        '  - create collections and add photos to them\n' ..
        '  - export JPEGs to job-specified folders\n\n' ..
        'Authorization lasts for this Lightroom session only.',
        'Start processor', 'Cancel')
    if answer ~= 'ok' then return end

    rawset(_G, RUNNING_FLAG, true)
    LrFileUtils.createAllDirectories(jobsDir)
    LrFileUtils.createAllDirectories(child(jobsDir, 'done'))
    LrFileUtils.createAllDirectories(child(jobsDir, 'failed'))

    local catalog = LrApplication.activeCatalog()

    while rawget(_G, RUNNING_FLAG) do
        for path in LrFileUtils.directoryEntries(jobsDir) do
            local leaf = LrPathUtils.leafName(path)
            local isJobFile = leaf:match('%.json$')
                and not leaf:match('%.part$')
                and not leaf:match('%.result%.json$')
            if isJobFile and LrFileUtils.exists(path) == 'file' then
                handleJobFile(catalog, jobsDir, path, leaf)
            end
        end
        LrTasks.sleep(3)
    end
end)
