"""Bridge protocol between ShopSteward and the Lightroom-side consumer."""

from typing import Protocol

from shopsteward.editing.models import EditJobSpec

JOB_SCHEMA = "shopsteward.editjob/1"
RESULT_SCHEMA = "shopsteward.editresult/1"


class LightroomBridge(Protocol):
    def dispatch(self, job: EditJobSpec) -> str: ...  # returns job file name
    def poll_results(self) -> list[dict]: ...  # raw result payloads
