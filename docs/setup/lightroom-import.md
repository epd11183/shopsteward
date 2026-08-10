# Lightroom import setup for ShopSteward sidecars

ShopSteward writes an `.xmp` sidecar next to each RAW **before** you import to
Lightroom. For Lightroom to honor it:

1. Run `shopsteward edit run <folder> --look <name>` on the folder of RAWs first.
2. In Lightroom's Import dialog, set **Apply During Import → Develop Settings = None**.
   If you apply a develop preset or "Auto Settings" here, it stacks on or
   overrides the sidecar.
3. Import normally. Each photo shows the correction + look already applied.

If the RAWs are already in your catalog, Lightroom will not auto-read a new
sidecar — select them and use **Metadata → Read Metadata from File**. Running
ShopSteward before import avoids this.
