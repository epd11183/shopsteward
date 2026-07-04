--[[
  JobFile.lua — pure helpers for the ShopSteward job/result file protocol.
  Schema contract: docs/designs/2026-07-03-m2-editing-module.md §3, mirrored
  by src/shopsteward/adapters/lightroom/fake.py (keep render_name in sync).

  No Lr imports: this module is pure and desk-checkable.
]]

local JsonCodec = require 'JsonCodec'

local M = {}

M.JOB_SCHEMA = 'shopsteward.editjob/1'
M.RESULT_SCHEMA = 'shopsteward.editresult/1'

-- Validate a decoded job table against schema shopsteward.editjob/1.
-- Returns true, or false plus a human-readable reason.
-- A missing/nil `export` means a hero job (JSON null decodes to nil).
function M.validate(tbl)
    if type(tbl) ~= 'table' then
        return false, 'job payload is not a JSON object'
    end
    if tbl.schema ~= M.JOB_SCHEMA then
        return false, 'schema is not ' .. M.JOB_SCHEMA .. ' (got ' .. tostring(tbl.schema) .. ')'
    end
    if type(tbl.job_id) ~= 'string' or tbl.job_id == '' then
        return false, 'job_id must be a non-empty string'
    end
    if tbl.mode ~= 'hero' and tbl.mode ~= 'mass' then
        return false, "mode must be 'hero' or 'mass' (got " .. tostring(tbl.mode) .. ')'
    end
    if type(tbl.develop_settings) ~= 'table' then
        return false, 'develop_settings must be an object'
    end
    if type(tbl.photos) ~= 'table' or #tbl.photos == 0 then
        return false, 'photos must be a non-empty array'
    end
    for i, photo in ipairs(tbl.photos) do
        if type(photo) ~= 'table'
            or type(photo.base_name) ~= 'string'
            or type(photo.raw_path) ~= 'string' then
            return false, 'photos[' .. i .. '] must have string base_name and raw_path'
        end
    end
    if type(tbl.collection) ~= 'string' or tbl.collection == '' then
        return false, 'collection must be a non-empty string'
    end
    if tbl.mode == 'mass' then
        local ex = tbl.export
        if type(ex) ~= 'table' then
            return false, 'mass job requires an export object'
        end
        if type(ex.output_folder) ~= 'string' or ex.output_folder == '' then
            return false, 'export.output_folder must be a non-empty string'
        end
        if type(ex.naming_template) ~= 'string' or ex.naming_template == '' then
            return false, 'export.naming_template must be a non-empty string'
        end
        if type(ex.event) ~= 'string' then
            return false, 'export.event must be a string'
        end
        if type(ex.jpeg_quality) ~= 'number' then
            return false, 'export.jpeg_quality must be a number'
        end
    end
    return true
end

-- Render an export filename from a naming template. Supports {event},
-- {date}, {base}, {seq} and {seq:0N} zero-padding. Must produce output
-- identical to Python str.format in adapters/lightroom/fake.py:
--   render_name('{event}-{seq:04}', event='testev', seq=1) -> 'testev-0001'
function M.render_name(template, ctx)
    return (template:gsub('{([%w_]+)(:?[^}]*)}', function(name, spec)
        local value = ctx[name]
        if value == nil then
            error("unknown template variable '{" .. name .. "}'", 0)
        end
        if spec == '' then
            return tostring(value)
        end
        local width = spec:match('^:0(%d+)$')
        if width and type(value) == 'number' then
            return string.format('%0' .. width .. 'd', value)
        end
        error("unsupported format spec '" .. spec .. "' for {" .. name .. "}", 0)
    end))
end

-- Build a shopsteward.editresult/1 payload. err is nil on success, or a
-- table {code=..., message=...} on failure (the key is omitted when nil,
-- matching the FakeBridge success payload).
function M.result(job_id, status, applied, skipped, exported, err)
    return {
        schema = M.RESULT_SCHEMA,
        job_id = job_id,
        status = status,
        applied = applied or 0,
        skipped = JsonCodec.array(skipped or {}),
        exported = JsonCodec.array(exported or {}),
        error = err,
        finished_at = os.date('!%Y-%m-%dT%H:%M:%SZ'),
    }
end

return M
