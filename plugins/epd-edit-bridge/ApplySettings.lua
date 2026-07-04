--[[
  ApplySettings.lua
  Lets you pick a .lua settings file (authored by Claude) and applies it to the
  selected photos. The file returns a table:

    return {
      global = { Exposure2012 = 0.0, Contrast2012 = 18, ... },   -- applied to all selected
      byFile = {                                                  -- optional per-photo overrides
        ["1J4A3264"] = { Temperature = 4800, Highlights2012 = -60, Exposure2012 = -0.2 },
      },
    }

  Keys are exact Lightroom develop-setting names (same as the export report).
  To set white balance, include Temperature + Tint AND WhiteBalance = "Custom".
]]

local LrApplication = import 'LrApplication'
local LrTasks       = import 'LrTasks'
local LrDialogs     = import 'LrDialogs'

local function baseName(n)
    n = n or ''
    return (n:gsub('%.%w+$', ''))   -- strip extension: "1J4A3264.CR3" -> "1J4A3264"
end

local function merge(a, b)
    local m = {}
    if a then for k, v in pairs(a) do m[k] = v end end
    if b then for k, v in pairs(b) do m[k] = v end end
    return m
end

local function count(t)
    local n = 0; for _ in pairs(t) do n = n + 1 end; return n
end

LrTasks.startAsyncTask(function()
    local catalog = LrApplication.activeCatalog()
    local photos  = catalog:getTargetPhotos()
    if (not photos) or #photos == 0 then
        LrDialogs.message('EPD Edit Bridge', 'Select the photos to edit first.', 'warning')
        return
    end

    local chosen = LrDialogs.runOpenPanel({
        title = 'Choose the settings .lua file from Claude',
        canChooseFiles = true,
        canChooseDirectories = false,
        allowsMultipleSelection = false,
        fileTypes = { 'lua' },
    })
    if (not chosen) or #chosen == 0 then return end

    local chunk, err = loadfile(chosen[1])
    if not chunk then
        LrDialogs.message('EPD Edit Bridge', 'Could not read the file:\n' .. tostring(err), 'error')
        return
    end
    local ok, spec = pcall(chunk)
    if (not ok) or type(spec) ~= 'table' then
        LrDialogs.message('EPD Edit Bridge', 'That file did not return a settings table.', 'error')
        return
    end

    local global = spec.global or {}
    local byFile = spec.byFile or {}

    -- Preview what will happen before touching anything
    local proceed = LrDialogs.confirm('EPD Edit Bridge',
        'Apply ' .. count(global) .. ' global setting(s)' ..
        (count(byFile) > 0 and (' + per-file overrides for ' .. count(byFile) .. ' file(s)') or '') ..
        ' to ' .. #photos .. ' selected photo(s)?',
        'Apply', 'Cancel')
    if proceed ~= 'ok' then return end

    local applied = 0
    catalog:withWriteAccessDo('Apply Claude settings', function()
        for _, photo in ipairs(photos) do
            local name     = baseName(photo:getFormattedMetadata('fileName'))
            local settings = merge(global, byFile[name])
            if next(settings) ~= nil then
                photo:applyDevelopSettings(settings, 'Claude edit')
                applied = applied + 1
            end
        end
    end)

    LrDialogs.message('EPD Edit Bridge', 'Applied settings to ' .. applied .. ' photo(s).')
end)
