"""Validation suite for the mindroom-sygnal NixOS module flake check."""

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


def _run_cond(script: Path) -> int:
    return subprocess.run([str(script)], check=False).returncode


def _check_common(
    name: str,
    item: dict[str, Any],
    *,
    pkg: str,
    default_image: str,
    bash_bin: str,
    expected_port: str = "127.0.0.1:5000:5000",
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    assert item["failedAssertions"] == [], f"{name}: {item['failedAssertions']}"
    assert item["podmanEnabled"] is True
    assert item["ociBackend"] == "podman"
    assert not any("/run/mindroom-sygnal" in r for r in item["tmpfilesRules"]), item["tmpfilesRules"]
    assert item["hasPodmanPkg"] is True

    container = item["container"]
    assert container["image"] == default_image
    assert container["autoStart"] is True
    assert container["environment"] == {
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": "/sygnal-addons",
        "SYGNAL_CONF": "/sygnal/sygnal.yaml",
    }
    assert container["ports"] == [expected_port]
    assert container["volumes"] == ["/run/mindroom-sygnal:/sygnal:ro", f"{pkg}:/sygnal-addons:ro"]

    setup = item["setup"]
    assert "podman-sygnal.service" in setup["before"]
    assert setup["serviceType"] == "oneshot"
    assert setup["remainAfterExit"] is True
    assert setup["runtimeDirectory"] == "mindroom-sygnal"
    assert setup["runtimeDirectoryMode"] == "0750"
    assert pkg in setup["restartTriggers"]
    assert setup["templatePath"] in setup["restartTriggers"]
    assert "install -d -m 0750 /run/mindroom-sygnal" in setup["script"]
    assert "rm -f /run/mindroom-sygnal/mindroom_*.py /run/sygnal/mindroom_*.py" in setup["script"]
    assert "mindroom_pushkin.py" not in setup["script"]
    assert "mindroom_pushkin_loader.py" not in setup["script"]
    assert "mindroom_fcm_pushkin.py" not in setup["script"]
    subprocess.run([bash_bin, "-n"], input=setup["script"], text=True, check=True)

    podman_service = item["podmanService"]
    assert "network-online.target" in podman_service["after"]
    assert "sygnal-setup.service" in podman_service["after"]
    assert "sygnal-setup.service" in podman_service["requires"]
    assert "network-online.target" in podman_service["wants"]
    assert podman_service["restartTriggers"] == setup["restartTriggers"]
    subprocess.run([bash_bin, "-n", podman_service["execCondition"]], check=True)

    cond_text = Path(podman_service["execCondition"]).read_text()
    parsed = yaml.safe_load(Path(setup["templatePath"]).read_text())
    assert parsed["http"] == {"bind_addresses": ["0.0.0.0"], "port": 5000}
    assert parsed["metrics"] == {
        "prometheus": {"enabled": False},
        "opentracing": {"enabled": False},
        "sentry": {"enabled": False},
    }
    return setup, cond_text, parsed


def _check_apns_and_fcm(
    data: dict[str, Any],
    *,
    pkg: str,
    default_image: str,
    bash_bin: str,
    envsubst_bin: str,
    test_tmp: Path,
) -> None:
    setup, cond, parsed = _check_common(
        "apnsAndFcm",
        data["apnsAndFcm"],
        pkg=pkg,
        default_image=default_image,
        bash_bin=bash_bin,
    )
    assert data["extraTriggerPath"] in setup["restartTriggers"]
    assert "secret-trigger" in setup["restartTriggers"]
    assert "install -m 0400 /run/agenix/sygnal-apns-key /run/mindroom-sygnal/sygnal-apns-key.p8" in setup["script"]
    assert (
        "install -m 0400 /run/agenix/sygnal-fcm-service-account /run/mindroom-sygnal/sygnal-fcm-service-account.json"
        in setup["script"]
    )
    assert ". /run/agenix/sygnal-env" in setup["script"]
    assert f"-o /run/mindroom-sygnal/sygnal.yaml -i {setup['templatePath']}" in setup["script"]
    assert "envsubst" in setup["script"]
    assert "[ -s /run/agenix/sygnal-apns-key ]" in cond
    assert 'grep -q "CHANGE_ME" /run/agenix/sygnal-apns-key' in cond
    assert "[ -s /run/agenix/sygnal-env ]" in cond
    assert 'grep -q "^SYGNAL_APNS_KEY_ID=" /run/agenix/sygnal-env' in cond
    assert 'grep -q "CHANGE_ME" /run/agenix/sygnal-env' in cond
    assert "[ -s /run/agenix/sygnal-fcm-service-account ]" in cond
    assert 'grep -q "CHANGE_ME" /run/agenix/sygnal-fcm-service-account' in cond
    assert parsed["log"]["setup"]["root"]["level"] == "INFO"
    assert parsed["log"]["setup"]["loggers"]["sygnal.access"]["level"] == "INFO"
    assert parsed["log"]["access"]["x_forwarded_for"] is True
    assert set(parsed["apps"].keys()) == {"chat.mindroom.app", "com.mindroom_ai.app"}
    assert parsed["apps"]["chat.mindroom.app"] == {
        "type": "mindroom_pushkin_loader.SafeApnsPushkin",
        "keyfile": "/sygnal/sygnal-apns-key.p8",
        "key_id": "$SYGNAL_APNS_KEY_ID",
        "team_id": "$SYGNAL_APNS_TEAM_ID",
        "topic": "$SYGNAL_APNS_TOPIC",
        "platform": "$SYGNAL_APNS_PLATFORM",
        "convert_device_token_to_hex": False,
    }
    assert parsed["apps"]["com.mindroom_ai.app"] == {
        "type": "mindroom_pushkin_loader.SafeFcmPushkin",
        "api_version": "v1",
        "project_id": "mindroom-prod",
        "service_account_file": "/sygnal/sygnal-fcm-service-account.json",
    }

    # Verify Go envsubst renders valid YAML and preserves all-digit IDs as strings.
    sub_env = {
        **os.environ,
        "SYGNAL_APNS_KEY_ID": "0123456789",
        "SYGNAL_APNS_TEAM_ID": "TEAM123456",
        "SYGNAL_APNS_TOPIC": "chat.mindroom.app",
        "SYGNAL_APNS_PLATFORM": "production",
    }
    rendered = subprocess.run(
        [envsubst_bin, "-i", setup["templatePath"]],
        env=sub_env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    rendered_yaml = yaml.safe_load(rendered)
    assert rendered_yaml["apps"]["chat.mindroom.app"]["key_id"] == "0123456789"
    assert isinstance(rendered_yaml["apps"]["chat.mindroom.app"]["key_id"], str)
    assert rendered_yaml["apps"]["chat.mindroom.app"]["platform"] == "production"

    # Verify runtime ExecCondition behavior (APNs+FCM with envFile + $SYGNAL_APNS_KEY_ID).
    secret_dir = test_tmp / "apns-fcm"
    secret_dir.mkdir()
    cond_copy = secret_dir / "cond.sh"
    cond_copy.write_text(cond.replace("/run/agenix", str(secret_dir)))
    cond_copy.chmod(stat.S_IRWXU)
    apns_key = secret_dir / "sygnal-apns-key"
    env_file = secret_dir / "sygnal-env"
    fcm_file = secret_dir / "sygnal-fcm-service-account"

    assert 1 <= _run_cond(cond_copy) <= 254  # missing files
    apns_key.write_text("")
    env_file.write_text("SYGNAL_APNS_KEY_ID=ABC1234567\n")
    fcm_file.write_text('{"type": "service_account"}\n')
    assert 1 <= _run_cond(cond_copy) <= 254  # empty APNs key
    apns_key.write_text("CHANGE_ME\n")
    assert 1 <= _run_cond(cond_copy) <= 254  # placeholder APNs key
    apns_key.write_text("-----BEGIN PRIVATE KEY-----\n")
    env_file.write_text("")
    assert 1 <= _run_cond(cond_copy) <= 254  # empty envFile
    env_file.write_text("OTHER=1\n")
    assert 1 <= _run_cond(cond_copy) <= 254  # missing SYGNAL_APNS_KEY_ID
    env_file.write_text("SYGNAL_APNS_KEY_ID=CHANGE_ME\n")
    assert 1 <= _run_cond(cond_copy) <= 254  # placeholder envFile
    env_file.write_text("SYGNAL_APNS_KEY_ID=ABC1234567\n")
    fcm_file.write_text('{"private_key": "CHANGE_ME"}\n')
    assert 1 <= _run_cond(cond_copy) <= 254  # placeholder FCM file
    fcm_file.write_text('{"type": "service_account"}\n')
    assert _run_cond(cond_copy) == 0


def _check_apns_variants_and_fcm(
    data: dict[str, Any],
    *,
    pkg: str,
    default_image: str,
    bash_bin: str,
    test_tmp: Path,
) -> None:
    # 2. APNs-only (no envFile, custom port/logLevel/xForwardedFor, path with space).
    setup, cond, parsed = _check_common(
        "apnsOnly",
        data["apnsOnly"],
        pkg=pkg,
        default_image=default_image,
        bash_bin=bash_bin,
        expected_port="127.0.0.2:5500:5000",
    )
    assert "install -m 0400 '/run/secrets/apns key.p8' /run/mindroom-sygnal/sygnal-apns-key.p8" in setup["script"]
    assert "rm -f /run/mindroom-sygnal/sygnal-fcm-service-account.json" in setup["script"]
    assert f"install -m 0400 {setup['templatePath']} /run/mindroom-sygnal/sygnal.yaml" in setup["script"]
    assert "envsubst" not in setup["script"]
    assert "[ -s '/run/secrets/apns key.p8' ]" in cond
    assert "SYGNAL_APNS_KEY_ID" not in cond
    assert "fcm" not in cond
    assert parsed["log"]["setup"]["root"]["level"] == "DEBUG"
    assert parsed["log"]["setup"]["loggers"]["sygnal.access"]["level"] == "DEBUG"
    assert parsed["log"]["access"]["x_forwarded_for"] is False
    assert set(parsed["apps"].keys()) == {"chat.mindroom.app"}
    assert parsed["apps"]["chat.mindroom.app"] == {
        "type": "mindroom_pushkin_loader.SafeApnsPushkin",
        "keyfile": "/sygnal/sygnal-apns-key.p8",
        "key_id": "ABC1234567",
        "team_id": "DEF8901234",
        "topic": "chat.mindroom.app",
        "platform": "production",
        "convert_device_token_to_hex": True,
    }

    # 3. Mixed literal apns.keyId + apns.envFile (and pre-bracketed IPv6 listenAddress).
    setup, cond, parsed = _check_common(
        "apnsMixedLiteralKeyId",
        data["apnsMixedLiteralKeyId"],
        pkg=pkg,
        default_image=default_image,
        bash_bin=bash_bin,
        expected_port="[::1]:5000:5000",
    )
    assert "[ -s /run/secrets/sygnal-env ]" in cond
    assert 'grep -q "CHANGE_ME" /run/secrets/sygnal-env' in cond
    assert "SYGNAL_APNS_KEY_ID" not in cond
    assert parsed["apps"]["chat.mindroom.app"]["key_id"] == "LITERALKEY1"
    assert parsed["apps"]["chat.mindroom.app"]["team_id"] == "$SYGNAL_APNS_TEAM_ID"

    mixed_dir = test_tmp / "apns-mixed"
    mixed_dir.mkdir()
    mixed_cond = mixed_dir / "cond.sh"
    mixed_cond.write_text(cond.replace("/run/secrets", str(mixed_dir)))
    mixed_cond.chmod(stat.S_IRWXU)
    (mixed_dir / "apns.p8").write_text("-----BEGIN PRIVATE KEY-----\n")
    assert 1 <= _run_cond(mixed_cond) <= 254  # missing envFile
    (mixed_dir / "sygnal-env").write_text("")
    assert 1 <= _run_cond(mixed_cond) <= 254  # empty envFile
    (mixed_dir / "sygnal-env").write_text("SYGNAL_APNS_TEAM_ID=CHANGE_ME\n")
    assert 1 <= _run_cond(mixed_cond) <= 254  # placeholder envFile
    (mixed_dir / "sygnal-env").write_text("SYGNAL_APNS_TEAM_ID=TEAM123456\n")
    assert _run_cond(mixed_cond) == 0  # passes without SYGNAL_APNS_KEY_ID

    # 4. FCM-only (no apns options set, unbracketed IPv6 listenAddress ::1).
    setup, cond, parsed = _check_common(
        "fcmOnly",
        data["fcmOnly"],
        pkg=pkg,
        default_image=default_image,
        bash_bin=bash_bin,
        expected_port="[::1]:5000:5000",
    )
    assert "rm -f /run/mindroom-sygnal/sygnal-apns-key.p8" in setup["script"]
    assert (
        "install -m 0400 /run/secrets/fcm-sa.json /run/mindroom-sygnal/sygnal-fcm-service-account.json"
        in setup["script"]
    )
    assert f"install -m 0400 {setup['templatePath']} /run/mindroom-sygnal/sygnal.yaml" in setup["script"]
    assert "envsubst" not in setup["script"]
    assert "[ -s /run/secrets/fcm-sa.json ]" in cond
    assert 'grep -q "CHANGE_ME" /run/secrets/fcm-sa.json' in cond
    assert "apns" not in cond
    assert "SYGNAL_APNS_KEY_ID" not in cond
    assert set(parsed["apps"].keys()) == {"com.mindroom_ai.app"}
    assert parsed["apps"]["com.mindroom_ai.app"] == {
        "type": "mindroom_pushkin_loader.SafeFcmPushkin",
        "api_version": "v1",
        "project_id": "mindroom-android",
        "service_account_file": "/sygnal/sygnal-fcm-service-account.json",
    }


def _main() -> None:
    data = json.loads(Path(sys.argv[1]).read_text())
    bash_bin, envsubst_bin = sys.argv[2], sys.argv[3]
    test_tmp = Path(sys.argv[4])
    pkg = data["pkg"]
    default_image = data["defaultImage"]

    ci_images = set(
        re.findall(
            r"docker\.io/matrixdotorg/sygnal@sha256:[0-9a-f]{64}",
            Path(data["ciWorkflow"]).read_text(),
        )
    )
    assert ci_images == {default_image}, (ci_images, default_image)

    assert not data["disabledHasContainer"]
    assert not data["disabledHasSetup"]

    fa = data["failedAssertions"]
    assert any("at least one of apns.enable or fcm.enable" in m for m in fa["noPlatforms"]), fa["noPlatforms"]
    assert any("must be distinct" in m for m in fa["duplicateAppId"]), fa["duplicateAppId"]
    assert any("set envFile or provide literal values" in m for m in fa["apnsPlaceholdersNoEnv"]), fa[
        "apnsPlaceholdersNoEnv"
    ]
    assert data["legacyDockerBackend"]["ociBackend"] == "docker"
    assert any(
        'services.mindroom-sygnal requires virtualisation.oci-containers.backend = "podman".' in m
        for m in data["legacyDockerBackend"]["failedAssertions"]
    ), data["legacyDockerBackend"]["failedAssertions"]

    _check_apns_and_fcm(
        data,
        pkg=pkg,
        default_image=default_image,
        bash_bin=bash_bin,
        envsubst_bin=envsubst_bin,
        test_tmp=test_tmp,
    )
    _check_apns_variants_and_fcm(
        data,
        pkg=pkg,
        default_image=default_image,
        bash_bin=bash_bin,
        test_tmp=test_tmp,
    )


if __name__ == "__main__":
    _main()
