from modules.catch_assets import AssetRef, PLACEHOLDER, import_manifest, validate_ref


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
