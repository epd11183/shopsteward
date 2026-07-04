"""Pydantic v2 boundary models for the editing module."""

from pydantic import BaseModel, Field


class PhotoPair(BaseModel):
    photo_id: str  # sha256 of the RAW — content-addressed identity
    base_name: str
    raw_path: str
    jpeg_path: str
    raw_sha256: str
    exif: dict = Field(default_factory=dict)


class IngestReport(BaseModel):
    ingest_job_id: str
    mode: str
    paired: int = 0
    duplicates: int = 0
    unpaired: int = 0
    photo_ids: list[str] = Field(default_factory=list)


class PresetFamily(BaseModel):
    name: str
    description: str = ""
    settings: dict[str, float | int | str] = Field(default_factory=dict)


class ExportSpec(BaseModel):
    output_folder: str
    naming_template: str
    event: str
    jpeg_quality: int = 92
    color_space: str = "sRGB"


class EditJobSpec(BaseModel):
    job_id: str
    user_id: int
    mode: str  # hero | mass
    preset_family: str
    develop_settings: dict[str, float | int | str]
    photos: list[dict]  # [{base_name, raw_path}]
    collection: str
    import_missing: bool = True
    export: ExportSpec | None = None  # None for hero jobs
