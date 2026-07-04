--[[
  EPD Edit Bridge — Info.lua
  A minimal Lightroom Classic plugin that round-trips develop settings
  between your catalog and Claude. Read-only export + explicit apply.
]]

return {
    LrSdkVersion = 13.0,
    LrSdkMinimumVersion = 10.0,  -- LrC 10+; you're on current, so fine

    LrToolkitIdentifier = 'com.ericdipietro.epdeditbridge',
    LrPluginName = 'EPD Edit Bridge',

    -- These appear under: Library menu > Plug-in Extras
    LrLibraryMenuItems = {
        {
            title = 'EPD: Export Develop Settings (selected) to Desktop',
            file  = 'ExportSettings.lua',
        },
        {
            title = 'EPD: Apply Settings from Claude (.lua file)...',
            file  = 'ApplySettings.lua',
        },
        {
            title = 'EPD: Start/Stop ShopSteward Queue Processor',
            file  = 'QueueProcessor.lua',
        },
    },

    VERSION = { major = 1, minor = 1, revision = 0 },
}
