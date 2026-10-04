from scripts.import_catch_assets import read_manifest
from modules.catch_assets import AssetRef, PLACEHOLDER, import_manifest, validate_ref


def test_read_manifest_requires_json_array(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"kind": "creature"}', encoding="utf-8")

    try:
        read_manifest(manifest)
    except ValueError as exc:
        assert "JSON array" in str(exc)
    else:
        raise AssertionError("non-array manifests should fail")


def test_read_manifest_rejects_non_object_entries(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text('[{"kind": "creature"}, "invalid"]', encoding="utf-8")

    try:
        read_manifest(manifest)
    except ValueError as exc:
        assert "entry must be an object" in str(exc)
    else:
        raise AssertionError("non-object manifest entries should fail")


def test_read_manifest_rejects_invalid_json(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"kind":', encoding="utf-8")

    try:
        read_manifest(manifest)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid JSON manifests should fail")


def test_placeholder_is_safe_fallback():
    assert validate_ref(PLACEHOLDER).status == "placeholder"


def test_manifest_rejects_duplicate_versions():
    record = {"kind": "creature", "key": "fox", "status": "draft"}
    try:
        import_manifest([record, record])
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate manifest entries should fail")


def test_asset_validation_rejects_bad_variant():
    try:
        validate_ref(AssetRef(kind="creature", key="fox", variant="glow"))
    except ValueError as exc:
        assert "variant" in str(exc)
    else:
        raise AssertionError("bad variants should fail")
