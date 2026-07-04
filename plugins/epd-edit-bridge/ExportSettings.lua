--[[
  ExportSettings.lua
  Reads the develop settings + key EXIF of the selected photos and writes a
  JSON report to the Desktop (epd_develop_report.json). Read-only: changes nothing.
  Send that file to Claude so it can tune against your real numbers.
]]

local LrApplication = import 'LrApplication'
local LrTasks       = import 'LrTasks'
local LrDialogs     = import 'LrDialogs'
local LrPathUtils   = import 'LrPathUtils'

-- Curated set of develop keys worth reporting (keeps the file small + readable)
local KEYS = {
    'WhiteBalance', 'Temperature', 'Tint',
    'Exposure2012', 'Contrast2012', 'Highlights2012', 'Shadows2012',
    'Whites2012', 'Blacks2012',
    'Texture', 'Clarity2012', 'Dehaze', 'Vibrance', 'Saturation',
    'HueAdjustmentRed','HueAdjustmentOrange','HueAdjustmentYellow','HueAdjustmentGreen',
    'HueAdjustmentAqua','HueAdjustmentBlue','HueAdjustmentPurple','HueAdjustmentMagenta',
    'SaturationAdjustmentRed','SaturationAdjustmentOrange','SaturationAdjustmentYellow',
    'SaturationAdjustmentGreen','SaturationAdjustmentAqua','SaturationAdjustmentBlue',
    'SaturationAdjustmentPurple','SaturationAdjustmentMagenta',
    'LuminanceAdjustmentRed','LuminanceAdjustmentOrange','LuminanceAdjustmentYellow',
    'LuminanceAdjustmentGreen','LuminanceAdjustmentAqua','LuminanceAdjustmentBlue',
    'LuminanceAdjustmentPurple','LuminanceAdjustmentMagenta',
    'RedHue','RedSaturation','GreenHue','GreenSaturation','BlueHue','BlueSaturation',
    'ColorGradeMidtoneHue','ColorGradeMidtoneSat','ColorGradeShadowHue','ColorGradeShadowSat',
    'ColorGradeHighlightHue','ColorGradeHighlightSat','ColorGradeGlobalHue','ColorGradeGlobalSat',
    'ColorGradeBlending','ColorGradeBalance',
    'Sharpness','SharpenRadius','SharpenDetail','SharpenEdgeMasking',
    'LuminanceSmoothing','ColorNoiseReduction',
    'ConvertToGrayscale','Treatment','ProcessVersion',
}

-- Minimal JSON encoder (scalars + tables; stable key order)
local function encode(v, indent)
    indent = indent or ''
    local t = type(v)
    if t == 'nil' then return 'null'
    elseif t == 'boolean' then return tostring(v)
    elseif t == 'number' then return tostring(v)
    elseif t == 'string' then
        return '"' .. v:gsub('\\','\\\\'):gsub('"','\\"'):gsub('\n','\\n') .. '"'
    elseif t == 'table' then
        local isArray, n = true, 0
        for k in pairs(v) do n = n + 1; if type(k) ~= 'number' then isArray = false end end
        local ni, parts = indent .. '  ', {}
        if isArray and n > 0 then
            for i = 1, #v do parts[#parts+1] = ni .. encode(v[i], ni) end
            return '[\n' .. table.concat(parts, ',\n') .. '\n' .. indent .. ']'
        else
            local keys = {}
            for k in pairs(v) do keys[#keys+1] = tostring(k) end
            table.sort(keys)
            for _, k in ipairs(keys) do
                parts[#parts+1] = ni .. '"' .. k .. '": ' .. encode(v[k], ni)
            end
            if #parts == 0 then return '{}' end
            return '{\n' .. table.concat(parts, ',\n') .. '\n' .. indent .. '}'
        end
    end
    return '"<' .. t .. '>"'
end

LrTasks.startAsyncTask(function()
    local catalog = LrApplication.activeCatalog()
    local photos  = catalog:getTargetPhotos()
    if (not photos) or #photos == 0 then
        LrDialogs.message('EPD Edit Bridge', 'Select one or more photos first.', 'warning')
        return
    end

    local report = {}
    for _, photo in ipairs(photos) do
        local ds = photo:getDevelopSettings() or {}
        local develop = {}
        for _, key in ipairs(KEYS) do
            if ds[key] ~= nil then develop[key] = ds[key] end
        end
        report[#report+1] = {
            fileName    = photo:getFormattedMetadata('fileName'),
            iso         = photo:getFormattedMetadata('isoSpeedRating'),
            shutter     = photo:getFormattedMetadata('shutterSpeed'),
            aperture    = photo:getFormattedMetadata('aperture'),
            focalLength = photo:getFormattedMetadata('focalLength'),
            camera      = photo:getFormattedMetadata('cameraModel'),
            lens        = photo:getFormattedMetadata('lens'),
            develop     = develop,
        }
    end

    local path = LrPathUtils.child(LrPathUtils.getStandardFilePath('desktop'),
                                   'epd_develop_report.json')
    local f, err = io.open(path, 'w')
    if not f then
        LrDialogs.message('EPD Edit Bridge', 'Could not write file:\n' .. tostring(err), 'error')
        return
    end
    f:write(encode({ photos = report }))
    f:close()

    LrDialogs.message('EPD Edit Bridge',
        'Wrote settings for ' .. #photos .. ' photo(s) to:\n' .. path ..
        '\n\nSend that file to Claude.')
end)
