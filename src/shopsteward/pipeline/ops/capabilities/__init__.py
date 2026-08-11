"""Real M8a capabilities (M8a spec §3/§4). Each module here defines exactly
one Capability implementation; none of them self-register at import time --
registration is always an explicit `registry.register(SomeCapability(adapter))`
call made by the CLI or a test, after it has decided which adapter (fake vs.
live) to inject. Importing this package (or a module in it) must never touch
a live adapter."""
