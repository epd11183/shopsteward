--[[
  JsonCodec.lua — pure JSON encode/decode for the EPD Edit Bridge.
  Encoder adapted from ExportSettings.lua; decoder is a minimal
  recursive-descent parser for the flat ShopSteward job schema.

  JSON null maps to Lua nil on decode (the key simply disappears);
  JobFile.validate treats a missing/nil `export` as a hero job, so
  nothing is lost for our contract. Nulls inside arrays are unsupported.

  Empty Lua tables are ambiguous (array or object?). Wrap a table in
  JsonCodec.array(t) to force `[]` output — e.g. an empty skipped list.

  No Lr imports: this module is pure and desk-checkable.
]]

local M = {}

local ARRAY_MT = { __jsonarray = true }

-- Mark a table so encode() always emits a JSON array (even when empty).
function M.array(t)
    return setmetatable(t or {}, ARRAY_MT)
end

-- ---------------------------------------------------------------- encode

local STR_ESCAPES = {
    ['"'] = '\\"', ['\\'] = '\\\\', ['\b'] = '\\b', ['\f'] = '\\f',
    ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t',
}

local function escapeString(s)
    return (s:gsub('[%c"\\]', function(c)
        return STR_ESCAPES[c] or string.format('\\u%04x', c:byte())
    end))
end

local function isArray(v)
    local mt = getmetatable(v)
    if mt and mt.__jsonarray then return true end
    local n = 0
    for k in pairs(v) do
        n = n + 1
        if type(k) ~= 'number' then return false end
    end
    return n > 0 and n == #v
end

function M.encode(v, indent)
    indent = indent or ''
    local t = type(v)
    if t == 'nil' then return 'null'
    elseif t == 'boolean' then return tostring(v)
    elseif t == 'number' then return tostring(v)
    elseif t == 'string' then
        return '"' .. escapeString(v) .. '"'
    elseif t == 'table' then
        local ni, parts = indent .. '  ', {}
        if isArray(v) then
            for i = 1, #v do parts[#parts + 1] = ni .. M.encode(v[i], ni) end
            if #parts == 0 then return '[]' end
            return '[\n' .. table.concat(parts, ',\n') .. '\n' .. indent .. ']'
        else
            local keys = {}
            for k in pairs(v) do keys[#keys + 1] = tostring(k) end
            table.sort(keys)
            for _, k in ipairs(keys) do
                parts[#parts + 1] = ni .. '"' .. escapeString(k) .. '": ' .. M.encode(v[k], ni)
            end
            if #parts == 0 then return '{}' end
            return '{\n' .. table.concat(parts, ',\n') .. '\n' .. indent .. '}'
        end
    end
    return '"<' .. t .. '>"'
end

-- ---------------------------------------------------------------- decode

local function utf8Char(cp)
    if cp < 0x80 then
        return string.char(cp)
    elseif cp < 0x800 then
        return string.char(0xC0 + math.floor(cp / 0x40), 0x80 + cp % 0x40)
    elseif cp < 0x10000 then
        return string.char(0xE0 + math.floor(cp / 0x1000),
                           0x80 + math.floor(cp / 0x40) % 0x40,
                           0x80 + cp % 0x40)
    else
        return string.char(0xF0 + math.floor(cp / 0x40000),
                           0x80 + math.floor(cp / 0x1000) % 0x40,
                           0x80 + math.floor(cp / 0x40) % 0x40,
                           0x80 + cp % 0x40)
    end
end

local DECODE_ESCAPES = {
    ['"'] = '"', ['\\'] = '\\', ['/'] = '/',
    b = '\b', f = '\f', n = '\n', r = '\r', t = '\t',
}

function M.decode(str)
    if type(str) ~= 'string' then error('decode expects a string', 0) end
    local pos = 1

    local function fail(msg)
        error(string.format('JSON parse error at position %d: %s', pos, msg), 0)
    end

    local function skipWs()
        local _, e = str:find('^[ \t\r\n]*', pos)
        pos = e + 1
    end

    local parseValue  -- forward declaration

    local function parseString()
        pos = pos + 1  -- skip opening quote
        local out = {}
        while true do
            local c = str:sub(pos, pos)
            if c == '' then fail('unterminated string') end
            if c == '"' then
                pos = pos + 1
                break
            elseif c == '\\' then
                local esc = str:sub(pos + 1, pos + 1)
                if esc == 'u' then
                    local hex = str:sub(pos + 2, pos + 5)
                    if not hex:match('^%x%x%x%x$') then fail('bad \\u escape') end
                    local cp = tonumber(hex, 16)
                    pos = pos + 6
                    if cp >= 0xD800 and cp <= 0xDBFF and str:sub(pos, pos + 1) == '\\u' then
                        local lo = tonumber(str:sub(pos + 2, pos + 5), 16)
                        if lo and lo >= 0xDC00 and lo <= 0xDFFF then
                            cp = 0x10000 + (cp - 0xD800) * 0x400 + (lo - 0xDC00)
                            pos = pos + 6
                        end
                    end
                    out[#out + 1] = utf8Char(cp)
                else
                    local ch = DECODE_ESCAPES[esc]
                    if not ch then fail('bad escape \\' .. tostring(esc)) end
                    out[#out + 1] = ch
                    pos = pos + 2
                end
            else
                out[#out + 1] = c
                pos = pos + 1
            end
        end
        return table.concat(out)
    end

    local function parseNumber()
        local numStr = str:match('^-?%d+%.?%d*[eE]?[%+%-]?%d*', pos)
        local n = numStr and tonumber(numStr)
        if not n then fail('invalid number') end
        pos = pos + #numStr
        return n
    end

    local function parseArray()
        pos = pos + 1  -- skip '['
        local arr = M.array({})
        skipWs()
        if str:sub(pos, pos) == ']' then
            pos = pos + 1
            return arr
        end
        while true do
            arr[#arr + 1] = parseValue()
            skipWs()
            local c = str:sub(pos, pos)
            if c == ',' then
                pos = pos + 1
                skipWs()
            elseif c == ']' then
                pos = pos + 1
                return arr
            else
                fail("expected ',' or ']' in array")
            end
        end
    end

    local function parseObject()
        pos = pos + 1  -- skip '{'
        local obj = {}
        skipWs()
        if str:sub(pos, pos) == '}' then
            pos = pos + 1
            return obj
        end
        while true do
            if str:sub(pos, pos) ~= '"' then fail('expected string key in object') end
            local key = parseString()
            skipWs()
            if str:sub(pos, pos) ~= ':' then fail("expected ':' after object key") end
            pos = pos + 1
            skipWs()
            obj[key] = parseValue()  -- null value -> nil: key is dropped (documented)
            skipWs()
            local c = str:sub(pos, pos)
            if c == ',' then
                pos = pos + 1
                skipWs()
            elseif c == '}' then
                pos = pos + 1
                return obj
            else
                fail("expected ',' or '}' in object")
            end
        end
    end

    parseValue = function()
        skipWs()
        local c = str:sub(pos, pos)
        if c == '{' then return parseObject()
        elseif c == '[' then return parseArray()
        elseif c == '"' then return parseString()
        elseif c == 't' then
            if str:sub(pos, pos + 3) ~= 'true' then fail('invalid literal') end
            pos = pos + 4
            return true
        elseif c == 'f' then
            if str:sub(pos, pos + 4) ~= 'false' then fail('invalid literal') end
            pos = pos + 5
            return false
        elseif c == 'n' then
            if str:sub(pos, pos + 3) ~= 'null' then fail('invalid literal') end
            pos = pos + 4
            return nil
        elseif c == '-' or c:match('%d') then
            return parseNumber()
        else
            fail('unexpected character ' .. (c == '' and '<eof>' or ("'" .. c .. "'")))
        end
    end

    local value = parseValue()
    skipWs()
    if pos <= #str then fail('trailing garbage after JSON value') end
    return value
end

return M
