from sbmig.build.signatures import _is_external, external_prefixes_from_config, register_external_prefixes


def test_in_house_prefixes_from_config_and_registration(cfg):
    prefixes = external_prefixes_from_config(cfg)
    assert "org.springframework.boot" in prefixes and "com.google.guava" in prefixes
    assert not _is_external("com.company.lib.auth.AuthClient")
    register_external_prefixes(["com.company.lib"])
    assert _is_external("com.company.lib.auth.AuthClient")
    assert not _is_external("com.company.app.service.OrderService")
