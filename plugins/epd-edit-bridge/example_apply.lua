--[[
  example_apply.lua  —  sample settings file for "EPD: Apply Settings from Claude"

  This demonstrates the format. `global` is applied to every selected photo;
  `byFile` overrides specific frames (matched by filename without extension).

  The global block below is the "Outdoor – Overcast & Rain" look, so select your
  overcast outdoor frames before running it. The byFile entries show how a single
  warm-indoor or colored-gel frame gets corrected differently.

  WHITE BALANCE: leave it out of `global` (it's per-frame). To pin WB on a specific
  shot, set Temperature + Tint AND WhiteBalance = "Custom", as shown in byFile below.
]]

return {

  global = {
    -- tone
    Contrast2012   = 18,
    Highlights2012 = -30,
    Shadows2012    = 30,
    Whites2012     = 18,
    Blacks2012     = -10,
    -- presence (orange held back for fair/freckled skin)
    Texture        = 10,
    Clarity2012    = 12,
    Dehaze         = 14,
    Vibrance       = 24,
    Saturation     = 5,
    -- HSL: punchy skies + rich Colorado greens, gentle skin
    HueAdjustmentOrange        = 3,
    SaturationAdjustmentOrange = 4,
    LuminanceAdjustmentOrange  = 8,
    SaturationAdjustmentGreen  = 14,
    HueAdjustmentGreen         = 12,
    SaturationAdjustmentBlue   = 20,
    HueAdjustmentBlue          = -5,
    LuminanceAdjustmentBlue    = -8,
    -- camera calibration primaries (where the vibrant base lives)
    RedHue = 5,  RedSaturation = 6,
    GreenHue = -8, GreenSaturation = 10,
    BlueHue = 5, BlueSaturation = 16,
    -- subtle warm color grade
    ColorGradeMidtoneHue = 44,
    ColorGradeMidtoneSat = 9,
    -- detail
    Sharpness = 40, SharpenRadius = 1.0, SharpenDetail = 25, SharpenEdgeMasking = 40,
    ColorNoiseReduction = 25,
  },

  byFile = {

    -- Signing the certificate: warm tungsten, hot skin highlights -> cool + recover
    ["1J4A3264"] = {
      WhiteBalance   = "Custom",
      Temperature    = 4800,
      Tint           = 6,
      Exposure2012   = -0.20,
      Highlights2012 = -60,
      Vibrance       = 10,
      HueAdjustmentOrange        = 4,
      SaturationAdjustmentOrange = -6,
      Dehaze         = 0,
    },

    -- Red dance floor: pure red gel -> desaturate the wash, lean into mood
    ["1J4A3494"] = {
      Vibrance                   = -8,
      Saturation                 = -5,
      SaturationAdjustmentRed    = -25,
      SaturationAdjustmentOrange = -18,
      SaturationAdjustmentMagenta = -15,
      LuminanceSmoothing         = 30,
      ColorNoiseReduction        = 35,
    },

  },
}
